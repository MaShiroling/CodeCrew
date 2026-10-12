"""Qdrant 1.19 conditional upserts with persistent revision tombstones.

Separate namespace: the step-2 unversioned API must not erase sync fences.
"""

import hashlib
import math
from uuid import NAMESPACE_URL, UUID, uuid5

from app.chat.qdrant_vector_backend import QdrantMemoryVectorBackend, _match
from app.chat.vector_contracts import VectorBackendError, VectorSearch
from app.chat.vector_maintenance_contracts import (
    RemoteIdentity,
    RemotePage,
    VectorInspection,
    cursor_key,
)
from app.chat.vector_sync_contracts import MAX_SYNC_REVISION, VectorMutation

READINESS_CHECK_REVISION = "codecrew-qdrant-readiness-v2"


class QdrantVersionedVectorBackend(QdrantMemoryVectorBackend):
    def __init__(self, endpoint, model):
        super().__init__(endpoint, model)
        self.collection_name += "_sync_v1"
        self.sync_target_id = hashlib.sha256(
            (endpoint.base_url.rstrip("/") + "\0" + self.collection_name).encode()
        ).hexdigest()

    def initialize(self):
        super().initialize()
        schema = self._collection()
        if "sync_revision" not in schema:
            self._completed(self._request("PUT", self._path + "/index?wait=true", {
                "field_name": "sync_revision", "field_schema": "integer"}))
        self._require_sync_ready()

    def _require_sync_ready(self):
        schema = self._require_ready()
        revision_index = schema.get("sync_revision")
        if not isinstance(revision_index, dict) or revision_index.get("data_type") != "integer":
            raise VectorBackendError("Qdrant synchronization revision index incompatible")

    def _require_search_ready(self):
        # The base search invokes exactly one fresh operation-specific check.
        # Keep the separate pre-embedding check in the recall orchestrator.
        self._require_sync_ready()

    def upsert(self, records):
        raise ValueError("versioned namespace requires apply_mutation")

    def delete(self, room_id, memory_ids):
        raise ValueError("versioned namespace requires persistent tombstone mutations")

    def _payload_matches(self, anchor, payload):
        if not isinstance(payload, dict):
            return False
        revision = payload.get("sync_revision")
        vector_hash = payload.get("sync_vector_hash")
        return (type(revision) is int and 1 <= revision <= MAX_SYNC_REVISION
                and isinstance(vector_hash, str) and len(vector_hash) == 64
                and all(char in "0123456789abcdef" for char in vector_hash)
                and {k: v for k, v in payload.items()
                     if k not in ("sync_revision", "sync_vector_hash")} == self._payload(anchor))

    @staticmethod
    def mutation_point_id(mutation):
        return str(uuid5(NAMESPACE_URL, f"codecrew:vector:v1:{mutation.room_id}:{mutation.memory_id}"))

    def mutation_payload(self, mutation):
        payload = {"room_id": str(mutation.room_id), "memory_id": str(mutation.memory_id),
                   "model_fingerprint": self.model.fingerprint, "dimensions": self.model.dimensions,
                   "status": "tombstone", "sync_revision": mutation.revision,
                   "sync_vector_hash": mutation.vector_hash}
        if mutation.record is not None:
            payload.update(self._payload(mutation.record.anchor))
        return payload

    @staticmethod
    def mutation_vector(mutation):
        # A tombstone replaces old semantic data with a fixed unit vector.
        return (mutation.record.vector if mutation.record is not None else
                (1.0,) + (0.0,) * (mutation.model.dimensions - 1))

    def apply_mutation(self, mutation: VectorMutation) -> int:
        if not isinstance(mutation, VectorMutation) or mutation.model != self.model:
            raise ValueError("typed same-model vector mutation required")
        self._require_sync_ready()
        point_id = self.mutation_point_id(mutation)
        payload = self.mutation_payload(mutation)
        self._completed(self._request("PUT", self._path + "/points?wait=true&ordering=strong", {
            "points": [{"id": point_id, "vector": self.mutation_vector(mutation), "payload": payload}],
            "update_filter": {"must": [
                _match("room_id", str(mutation.room_id)),
                _match("memory_id", str(mutation.memory_id)),
                _match("model_fingerprint", self.model.fingerprint),
                _match("dimensions", self.model.dimensions),
            ], "should": [{"is_empty": {"key": "sync_revision"}},
                          {"key": "sync_revision", "range": {"lt": mutation.revision}}]},
        }))
        # completed alone also covers a filtered no-op. Confirm a matching equal
        # revision or a strictly newer fence before acknowledging the event.
        result = self._request("GET", self._path + "/points/" + point_id)
        actual = result.get("payload") if isinstance(result, dict) else None
        if not isinstance(actual, dict) or result.get("id") != point_id:
            raise VectorBackendError("invalid Qdrant synchronization receipt")
        revision = actual.get("sync_revision")
        if (type(revision) is not int or not mutation.revision <= revision <= MAX_SYNC_REVISION
                or any(actual.get(key) != payload[key] for key in
                       ("room_id", "memory_id", "model_fingerprint", "dimensions"))
                or actual.get("status") not in ("active", "tombstone")
                or (revision == mutation.revision and actual != payload)):
            raise VectorBackendError("Qdrant synchronization receipt conflicts with mutation")
        return revision

    def _inspect_point(self, mutation, point):
        if point is None:
            return VectorInspection(mutation.memory_id, "missing")
        payload = point.get("payload")
        expected = self.mutation_payload(mutation)
        if not isinstance(payload, dict) or any(payload.get(key) != expected[key] for key in
                ("room_id", "memory_id", "model_fingerprint", "dimensions")):
            return VectorInspection(mutation.memory_id, "scope_conflict")
        revision = payload.get("sync_revision")
        if type(revision) is not int or not 1 <= revision <= MAX_SYNC_REVISION:
            return VectorInspection(mutation.memory_id, "invalid_revision")
        if revision > mutation.revision:
            return VectorInspection(mutation.memory_id, "ahead", revision)
        vector = point.get("vector")
        wanted = self.mutation_vector(mutation)
        norm = math.sqrt(math.fsum(value * value for value in wanted))
        normalized = tuple(value / norm for value in wanted)
        valid_vector = (isinstance(vector, list) and len(vector) == mutation.model.dimensions
                        and all(type(value) in (int, float) and math.isfinite(value) for value in vector))
        matches = payload == expected and valid_vector and all(
            abs(actual - desired) <= 1e-5 for actual, desired in zip(vector, normalized, strict=True))
        return VectorInspection(mutation.memory_id, "current" if matches else "drift", revision)

    def inspect_mutations(self, mutations):
        if (not isinstance(mutations, tuple) or len(mutations) > 16
                or any(not isinstance(item, VectorMutation) or item.model != self.model for item in mutations)
                or len({item.memory_id for item in mutations}) != len(mutations)):
            raise ValueError("inspection requires at most 16 distinct same-model mutations")
        if not mutations:
            return ()
        self._require_sync_ready()
        identities = {self.mutation_point_id(item): item for item in mutations}
        result = self._request("POST", self._path + "/points", {
            "ids": list(identities), "with_payload": True, "with_vector": True})
        if not isinstance(result, list) or len(result) > len(identities):
            raise VectorBackendError("invalid Qdrant inspection response")
        points = {}
        for point in result:
            point_id = point.get("id") if isinstance(point, dict) else None
            if not isinstance(point_id, str) or point_id not in identities or point_id in points:
                raise VectorBackendError("out-of-scope Qdrant inspection point")
            points[point_id] = point
        return tuple(self._inspect_point(item, points.get(self.mutation_point_id(item))) for item in mutations)

    def search_current(self, request, mutations):
        """Exact, candidate-scoped recall fenced by canonical revision/vector hash.

        This does not trust the remote version alone. Caller rechecks canonical
        state after HTTP; pending events may safely cause missing semantic hits.
        """
        if (not isinstance(request, VectorSearch) or request.model != self.model or not isinstance(mutations, tuple)
                or len(mutations) != len(request.candidates)
                or any(not isinstance(item, VectorMutation) or item.model != self.model
                       or item.record is None or item.record.anchor != anchor
                       for item, anchor in zip(mutations, request.candidates, strict=True))):
            raise ValueError("current search requires matching active canonical mutations")
        return self._search(request, expected_payloads={
            self.mutation_point_id(item): self.mutation_payload(item) for item in mutations})

    def scan_identities(self, cursor=None, *, limit=16):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("remote audit page must be within 1–16")
        if cursor is not None:
            cursor_key(cursor)
        self._require_sync_ready()
        body = {"limit": limit, "with_vector": False, "with_payload": [
            "room_id", "memory_id", "model_fingerprint", "dimensions"]}
        if cursor is not None:
            body["offset"] = cursor
        result = self._request("POST", self._path + "/points/scroll", body)
        if (not isinstance(result, dict) or not isinstance(result.get("points"), list)
                or "next_page_offset" not in result or len(result["points"]) > limit):
            raise VectorBackendError("invalid Qdrant audit scroll response")
        identities = []
        try:
            for point in result["points"]:
                point_id = point["id"]
                if cursor is not None and cursor_key(point_id) < cursor_key(cursor):
                    raise ValueError("point below cursor")
                payload = point.get("payload")
                room_id = memory_id = None
                if isinstance(payload, dict):
                    try:
                        room, memory = UUID(payload["room_id"]), UUID(payload["memory_id"])
                        scoped = str(uuid5(NAMESPACE_URL, f"codecrew:vector:v1:{room}:{memory}"))
                        if (point_id == scoped and payload.get("model_fingerprint") == self.model.fingerprint
                                and payload.get("dimensions") == self.model.dimensions):
                            room_id, memory_id = room, memory
                    except (KeyError, ValueError, TypeError, AttributeError):
                        pass  # Report unowned/malformed metadata, never adopt or delete it.
                identities.append(RemoteIdentity(point_id, room_id, memory_id))
            return RemotePage(tuple(identities), result["next_page_offset"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise VectorBackendError("invalid Qdrant audit cursor or identity") from None
