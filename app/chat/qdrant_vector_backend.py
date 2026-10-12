"""Opt-in Qdrant REST adapter. No embedding, canonical writes or automatic retry.

Collections are explicitly initialized, model-specific and bounded by the
existing candidate allowlist. This is storage plumbing, not production recall.
"""

import hashlib
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, UUID, uuid5

from app.chat.vector_contracts import (
    VectorAnchor,
    VectorBackendError,
    VectorHit,
    VectorModel,
    VectorRecord,
    VectorSearch,
)

PAYLOAD_SCHEMA = {
    "memory_id": "keyword", "room_id": "keyword", "source_message_id": "keyword",
    "source_correlation_id": "keyword", "content_hash": "keyword", "status": "keyword",
    "model_fingerprint": "keyword", "dimensions": "integer",
}


@dataclass(frozen=True, slots=True)
class QdrantEndpoint:
    base_url: str = field(default="http://127.0.0.1:6333", repr=False)
    collection_prefix: str = "codecrew_memory"
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 5.0

    def __post_init__(self):
        try:
            url = urllib.parse.urlsplit(self.base_url)
            port = url.port
        except ValueError:
            raise ValueError("invalid Qdrant URL") from None
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/") or port == 0
                or (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise ValueError("Qdrant requires HTTPS or loopback HTTP API root")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", self.collection_prefix):
            raise ValueError("invalid Qdrant collection prefix")
        if (type(self.timeout_seconds) not in (int, float)
                or not 0 < self.timeout_seconds <= 30):
            raise ValueError("invalid Qdrant timeout")
        if any(char in self.api_key for char in "\r\n"):
            raise ValueError("invalid Qdrant API key")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _match(key, value):
    return {"key": key, "match": {"value": value}}


class QdrantMemoryVectorBackend:
    def __init__(self, endpoint: QdrantEndpoint, model: VectorModel):
        self.endpoint = endpoint
        self.model = model
        digest = hashlib.sha256(model.fingerprint.encode()).hexdigest()
        self.collection_name = f"{endpoint.collection_prefix}_{digest}"
        # Do not inherit environment proxies or follow redirects with credentials.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @staticmethod
    def point_id(anchor: VectorAnchor) -> str:
        # Prevent same logical UUID in another room from overwriting this room.
        return str(uuid5(NAMESPACE_URL, f"codecrew:vector:v1:{anchor.room_id}:{anchor.memory_id}"))

    def _request(self, method, path, body=None, *, missing_ok=False):
        request = urllib.request.Request(
            self.endpoint.base_url.rstrip("/") + path, method=method,
            data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(
                {"api-key": self.endpoint.api_key} if self.endpoint.api_key else {})},
        )
        try:
            with self._opener.open(request, timeout=self.endpoint.timeout_seconds) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if missing_ok and code == 404:
                return None
            raise VectorBackendError("Qdrant HTTP request failed") from None
        except OSError:
            raise VectorBackendError("Qdrant connection failed") from None
        try:
            if len(raw) > 2_000_000:
                raise ValueError("large response")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("status") != "ok" or "result" not in data:
                raise ValueError("invalid envelope")
            return data["result"]
        except (ValueError, TypeError, UnicodeError):
            raise VectorBackendError("invalid Qdrant response") from None

    @property
    def _path(self):
        return "/collections/" + self.collection_name

    def _collection(self):
        result = self._request("GET", self._path, missing_ok=True)
        if result is None:
            return None
        try:
            vectors = result["config"]["params"]["vectors"]
            if vectors["size"] != self.model.dimensions or vectors["distance"] != "Cosine":
                raise ValueError("incompatible model")
            schema = result["payload_schema"]
            if not isinstance(schema, dict):
                raise TypeError("invalid payload schema")
            for key, kind in PAYLOAD_SCHEMA.items():
                if key in schema and schema[key]["data_type"] != kind:
                    raise ValueError("incompatible payload index")
            return schema
        except (KeyError, TypeError, ValueError):
            raise VectorBackendError("incompatible Qdrant collection") from None

    @staticmethod
    def _completed(result):
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise VectorBackendError("Qdrant operation not completed")

    def initialize(self):
        """Explicit collection/index writes; never recreate or drop an existing collection."""
        schema = self._collection()
        if schema is None:
            if self._request("PUT", self._path, {"vectors": {
                    "size": self.model.dimensions, "distance": "Cosine"}}) is not True:
                raise VectorBackendError("Qdrant collection creation failed")
            schema = self._collection()
            if schema is None:
                raise VectorBackendError("Qdrant collection missing after creation")
        for key, kind in PAYLOAD_SCHEMA.items():
            if key not in schema:
                self._completed(self._request("PUT", self._path + "/index?wait=true",
                                              {"field_name": key, "field_schema": kind}))
        self._require_ready()

    def _require_ready(self):
        schema = self._collection()
        if schema is None or set(PAYLOAD_SCHEMA) - set(schema):
            raise VectorBackendError("Qdrant collection not initialized")
        return schema  # One fresh response; versioned callers also inspect its fence index.

    def _require_search_ready(self):
        """Operation-specific readiness hook; never skips checks or caches state."""
        self._require_ready()

    def _payload(self, anchor):
        return {"memory_id": str(anchor.memory_id), "room_id": str(anchor.room_id),
                "source_message_id": str(anchor.source_message_id),
                "source_correlation_id": str(anchor.source_correlation_id),
                "content_hash": anchor.content_hash, "status": anchor.status,
                "model_fingerprint": self.model.fingerprint, "dimensions": self.model.dimensions}

    def _payload_matches(self, anchor, payload):
        return payload == self._payload(anchor)

    def upsert(self, records: Sequence[VectorRecord]) -> tuple[UUID, ...]:
        records = tuple(records)
        if (len(records) > 32 or any(not isinstance(item, VectorRecord) for item in records)
                or len({(item.anchor.room_id, item.anchor.memory_id) for item in records}) != len(records)
                or any(item.model != self.model for item in records)):
            raise ValueError("Qdrant upsert requires at most 32 distinct same-model records")
        if not records:
            return ()
        if any(not any(item.vector) for item in records):
            raise ValueError("Qdrant cosine records must have nonzero norm")
        self._require_ready()
        self._completed(self._request("PUT", self._path + "/points?wait=true", {"points": [
            {"id": self.point_id(item.anchor), "vector": item.vector,
             "payload": self._payload(item.anchor)} for item in records]}))
        return tuple(item.anchor.memory_id for item in records)

    def delete(self, room_id: UUID, memory_ids: Sequence[UUID]) -> None:
        memory_ids = tuple(memory_ids)
        if (not isinstance(room_id, UUID) or len(memory_ids) > 200
                or any(not isinstance(item, UUID) for item in memory_ids)):
            raise ValueError("delete requires room UUID and at most 200 memory UUIDs")
        if not memory_ids:
            return
        self._require_ready()
        self._completed(self._request("POST", self._path + "/points/delete?wait=true", {"filter": {
            "must": [_match("room_id", str(room_id)), _match("model_fingerprint", self.model.fingerprint),
                     {"key": "memory_id", "match": {"any": [str(item) for item in memory_ids]}}]}}))

    def search(self, request: VectorSearch) -> tuple[VectorHit, ...]:
        return self._search(request)

    def _search(self, request: VectorSearch, *, expected_payloads=None) -> tuple[VectorHit, ...]:
        # Versioned recall can supply full canonical revision/hash payloads.
        # Legacy callers retain the original matching hook and filter contract.
        if not isinstance(request, VectorSearch):
            raise TypeError("typed vector search required")
        if request.model != self.model:
            raise ValueError("query model differs from Qdrant collection")
        if not request.candidates:
            return ()
        if not any(request.vector):
            raise ValueError("Qdrant cosine query must have nonzero norm")
        self._require_search_ready()
        anchors = {self.point_id(item): item for item in request.candidates}
        condition = {"must": [_match("room_id", str(request.room_id)), _match("status", "active"),
                              _match("model_fingerprint", self.model.fingerprint),
                              _match("dimensions", self.model.dimensions)],
                     "should": [{"must": [{"has_id": [point_id]}, *[
                         _match(key, value) for key, value in (
                             expected_payloads[point_id] if expected_payloads is not None
                             else self._payload(anchor)).items()
                     ]]} for point_id, anchor in anchors.items()]}
        if request.exclude_correlation_id is not None:
            condition["must_not"] = [_match("source_correlation_id", str(request.exclude_correlation_id))]
        result = self._request("POST", self._path + "/points/query", {
            "query": request.vector, "filter": condition, "params": {"exact": True},
            "with_payload": True, "with_vector": False, "limit": len(anchors),
            "score_threshold": request.min_similarity,
        })
        if not isinstance(result, dict) or not isinstance(result.get("points"), list):
            raise VectorBackendError("invalid Qdrant query result")
        if len(result["points"]) > len(anchors):
            raise VectorBackendError("Qdrant exceeded query candidate bound")
        ranked, seen = [], set()
        for point in result["points"]:
            if not isinstance(point, dict):
                raise VectorBackendError("invalid Qdrant point")
            point_id = point.get("id")
            if not isinstance(point_id, str) or point_id not in anchors or point_id in seen:
                raise VectorBackendError("Qdrant returned an out-of-scope point")
            seen.add(point_id)
            anchor = anchors[point_id]
            matches = (point.get("payload") == expected_payloads[point_id] if expected_payloads is not None
                       else self._payload_matches(anchor, point.get("payload")))
            if not matches:
                raise VectorBackendError("Qdrant returned stale point metadata")
            score = point.get("score")
            if (type(score) not in (int, float) or not -1.000001 <= score <= 1.000001
                    or not math.isfinite(score)):
                raise VectorBackendError("invalid Qdrant cosine score")
            score = max(-1.0, min(1.0, score))
            if score >= request.min_similarity:
                ranked.append(VectorHit(anchor, self.model, score))
        ranked.sort(key=lambda hit: (-hit.similarity, str(hit.anchor.memory_id)))
        return tuple(ranked[:request.limit])
