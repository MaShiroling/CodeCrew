"""Conditional revision REST contract; fake server, no real network."""

import io
import json
import urllib.error
from dataclasses import replace
from uuid import uuid4

import pytest

from app.chat.qdrant_vector_backend import QdrantEndpoint, QdrantMemoryVectorBackend
from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_contracts import (
    VectorAnchor,
    VectorBackendError,
    VectorModel,
    VectorRecord,
    VectorSearch,
)
from app.chat.vector_maintenance_contracts import cursor_key
from app.chat.vector_sync_contracts import VectorMutation


class Response:
    def __init__(self, result):
        self.data = json.dumps({"status": "ok", "result": result}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, maximum):
        return self.data[:maximum]


class ConditionalREST:
    def __init__(self, model):
        self.model, self.schema, self.points = model, {}, {}
        self.exists = False
        self.requests = []
        self.receipt_change = None
        self.completed = True
        self.fail_after_write = False
        self.read_change = None

    def open(self, request, *, timeout):
        body = json.loads(request.data) if request.data else None
        method, url = request.get_method(), request.full_url
        self.requests.append((method, url, body))
        if method == "POST" and url.endswith("/points"):
            result = [self.points[key] for key in body["ids"] if key in self.points]
            return Response(self.read_change(result) if self.read_change else result)
        if method == "POST" and url.endswith("/points/scroll"):
            ordered = sorted(self.points.values(), key=lambda point: cursor_key(point["id"]))
            selected = [point for point in ordered if "offset" not in body
                        or cursor_key(point["id"]) >= cursor_key(body["offset"])]
            result = {"points": selected[:body["limit"]], "next_page_offset":
                      selected[body["limit"]]["id"] if len(selected) > body["limit"] else None}
            return Response(self.read_change(result) if self.read_change else result)
        if method == "GET" and "/points/" in url:
            result = dict(self.points[url.rsplit("/", 1)[1]])
            result["payload"] = dict(result["payload"])
            if self.receipt_change:
                self.receipt_change(result)
            return Response(result)
        if method == "GET":
            if not self.exists:
                raise urllib.error.HTTPError(url, 404, "missing", {}, io.BytesIO())
            return Response({"config": {"params": {"vectors": {"size": self.model.dimensions, "distance": "Cosine"}}},
                             "payload_schema": self.schema})
        if "/index?" in url:
            self.schema[body["field_name"]] = {"data_type": body["field_schema"]}
            return Response({"status": "completed"})
        if url.endswith("/points/query"):
            return Response({"points": [{**point, "score": 1.0} for point in self.points.values()
                                        if point["payload"]["status"] == "active"]})
        if "/points?" in url:
            point = body["points"][0]
            previous = self.points.get(point["id"])
            condition = body["update_filter"]
            eligible = previous is None or (
                all(previous["payload"].get(c["key"]) == c["match"]["value"] for c in condition["must"])
                and previous["payload"].get("sync_revision", 0) < condition["should"][1]["range"]["lt"])
            if eligible:
                self.points[point["id"]] = point
            if self.fail_after_write:
                raise OSError("private-provider-error")
            return Response({"status": "completed" if self.completed else "acknowledged"})
        self.exists = True
        return Response(True)


@pytest.fixture
def setup():
    model = VectorModel("model-v1", 2)
    backend = QdrantVersionedVectorBackend(QdrantEndpoint(), model)
    rest = ConditionalREST(model)
    backend._opener = rest
    backend.initialize()
    anchor = VectorAnchor(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64)
    mutation = VectorMutation(anchor.room_id, anchor.memory_id, 1, model, VectorRecord(anchor, model, (1., 0.)))
    return backend, rest, mutation


def test_conditional_version_tombstone_rejects_delayed_old_upsert(setup):
    backend, rest, original = setup
    assert backend.apply_mutation(original) == 1
    tombstone = replace(original, revision=3, record=None)
    assert backend.apply_mutation(tombstone) == 3
    assert backend.apply_mutation(original) == 3
    point = rest.points[backend.point_id(original.record.anchor)]
    assert point["payload"]["status"] == "tombstone"
    assert point["payload"]["sync_vector_hash"] == "tombstone"
    assert "content" not in point["payload"]
    assert backend.search(VectorSearch(original.room_id, original.model, (1., 0.), (original.record.anchor,))) == ()
    writes = [item for item in rest.requests if "/points?" in item[1]]
    assert all(item[1].endswith("?wait=true&ordering=strong") for item in writes)
    assert writes[-1][2]["update_filter"]["should"] == [
        {"is_empty": {"key": "sync_revision"}}, {"key": "sync_revision", "range": {"lt": 1}}]


def test_out_of_order_delete_first_and_duplicate_events(setup):
    backend, rest, original = setup
    assert backend.apply_mutation(replace(original, revision=2, record=None)) == 2
    assert backend.apply_mutation(original) == 2
    assert backend.apply_mutation(replace(original, revision=2, record=None)) == 2
    assert len(rest.points) == 1


def test_same_revision_conflicting_payload_not_acknowledged(setup):
    backend, _, original = setup
    backend.apply_mutation(original)
    with pytest.raises(VectorBackendError):
        backend.apply_mutation(replace(original, record=None))


def test_connection_lost_after_remote_write_retry_confirms_same_revision(setup):
    backend, rest, original = setup
    rest.fail_after_write = True
    with pytest.raises(VectorBackendError):
        backend.apply_mutation(original)
    rest.fail_after_write = False
    assert backend.apply_mutation(original) == 1
    assert len(rest.points) == 1


def test_versioned_active_search_and_no_legacy_mutations(setup):
    backend, rest, original = setup
    backend.apply_mutation(original)
    hits = backend.search(VectorSearch(original.room_id, original.model, (1., 0.), (original.record.anchor,)))
    assert len(hits) == 1
    with pytest.raises(ValueError):
        backend.upsert((original.record,))
    with pytest.raises(ValueError):
        backend.delete(original.room_id, (original.memory_id,))
    plain = QdrantMemoryVectorBackend(backend.endpoint, backend.model)
    assert plain.collection_name != backend.collection_name
    assert rest.schema["sync_revision"]["data_type"] == "integer"


@pytest.mark.parametrize("kind", ["revision_bool", "revision_older", "scope", "same_hash", "point_id", "payload", "status"])
def test_remote_receipts_fail_closed(setup, kind):
    backend, rest, original = setup
    def corrupt(result):
        if kind == "point_id":
            result["id"] = str(uuid4())
        elif kind == "payload":
            result["payload"] = None
        else:
            key, value = {"revision_bool": ("sync_revision", True),
                          "revision_older": ("sync_revision", 0), "scope": ("room_id", str(uuid4())),
                          "same_hash": ("sync_vector_hash", "b" * 64), "status": ("status", "unknown")}[kind]
            result["payload"][key] = value
    rest.receipt_change = corrupt
    with pytest.raises(VectorBackendError):
        backend.apply_mutation(original)


def test_pending_operation_and_bad_revision_index_rejected(setup):
    backend, rest, original = setup
    rest.completed = False
    with pytest.raises(VectorBackendError):
        backend.apply_mutation(original)
    rest.schema["sync_revision"]["data_type"] = "keyword"
    count = len(rest.requests)
    with pytest.raises(VectorBackendError):
        backend.initialize()
    assert not any(method == "PUT" for method, *_ in rest.requests[count:])


def test_targets_identify_endpoint_collection_model_not_credentials():
    model = VectorModel("v1", 2)
    a = QdrantVersionedVectorBackend(QdrantEndpoint(api_key="private-a"), model)
    b = QdrantVersionedVectorBackend(QdrantEndpoint(api_key="private-b"), model)
    c = QdrantVersionedVectorBackend(QdrantEndpoint(base_url="http://127.0.0.1:6334"), model)
    d = QdrantVersionedVectorBackend(QdrantEndpoint(), VectorModel("v2", 2))
    assert a.sync_target_id == b.sync_target_id
    assert len({a.sync_target_id, c.sync_target_id, d.sync_target_id}) == 3


def test_inspect_bulk_reads_actual_normalized_vector_and_missing_points(setup):
    backend, rest, original = setup
    nonunit = replace(original, record=replace(original.record, vector=(3., 4.)))
    backend.apply_mutation(nonunit)
    rest.points[backend.mutation_point_id(original)]["vector"] = [.6, .8]  # Cosine storage normalization.
    assert backend.inspect_mutations((nonunit,))[0].code == "current"
    request = rest.requests[-1][2]
    assert request["with_payload"] is True and request["with_vector"] is True
    rest.points[backend.mutation_point_id(original)]["vector"] = [0., 1.]
    assert backend.inspect_mutations((nonunit,))[0].code == "drift"  # Same payload hash is insufficient.
    rest.points.clear()
    assert backend.inspect_mutations((nonunit,))[0].code == "missing"


@pytest.mark.parametrize("kind,code", [
    ("hash", "drift"), ("older", "drift"), ("ahead", "ahead"), ("bool_revision", "invalid_revision"),
    ("zero_revision", "invalid_revision"), ("scope", "scope_conflict"), ("payload", "scope_conflict"),
    ("vector_none", "drift"), ("vector_bool", "drift"), ("vector_short", "drift"), ("vector_nan", "drift"),
])
def test_inspection_classifies_corruption_without_overwriting(setup, kind, code):
    backend, rest, original = setup
    backend.apply_mutation(original)
    point = rest.points[backend.mutation_point_id(original)]
    if kind == "payload":
        point["payload"] = None
    elif kind.startswith("vector_"):
        point["vector"] = {"vector_none": None, "vector_bool": [True, 0], "vector_short": [1],
                           "vector_nan": [float("nan"), 0]}[kind]
    else:
        key, value = {"hash": ("sync_vector_hash", "b" * 64), "older": ("sync_revision", 1),
                      "ahead": ("sync_revision", 3), "bool_revision": ("sync_revision", True),
                      "zero_revision": ("sync_revision", 0), "scope": ("room_id", str(uuid4()))}[kind]
        point["payload"][key] = value
    count = len(rest.requests)
    assert backend.inspect_mutations((replace(original, revision=2),))[0].code == code
    assert all(method != "PUT" for method, *_ in rest.requests[count:])


@pytest.mark.parametrize("kind", ["duplicate", "unexpected", "not_list", "malformed"])
def test_inspection_rejects_ambiguous_bulk_results(setup, kind):
    backend, rest, original = setup
    backend.apply_mutation(original)
    rest.read_change = lambda points: {"duplicate": points + points,
        "unexpected": [{**points[0], "id": str(uuid4())}], "not_list": {}, "malformed": [None]}[kind]
    with pytest.raises(VectorBackendError):
        backend.inspect_mutations((original,))


def test_inspection_empty_invalid_batch_never_connects(setup):
    backend, rest, original = setup
    count = len(rest.requests)
    assert backend.inspect_mutations(()) == ()
    for items in ([original], (original, original), (original,) * 17,
                  (replace(original, model=VectorModel("other", 2), record=None),)):
        with pytest.raises(ValueError):
            backend.inspect_mutations(items)
    assert len(rest.requests) == count


def test_remote_audit_includes_tombstones_and_unmanaged_integer_ids(setup):
    backend, rest, original = setup
    backend.apply_mutation(replace(original, revision=2, record=None))
    rest.points[0] = {"id": 0, "payload": {}}
    first = backend.scan_identities(limit=1)
    assert first.points[0].point_id == 0 and first.points[0].memory_id is None
    second = backend.scan_identities(first.next_cursor, limit=1)
    assert second.points[0].memory_id == original.memory_id and second.next_cursor is None
    request = rest.requests[-1][2]
    assert request["offset"] == first.next_cursor and "filter" not in request
    assert request["with_vector"] is False and len(request["with_payload"]) == 4
    backend.scan_identities(0, limit=1)
    assert rest.requests[-1][2]["offset"] == 0  # Never treat zero as absent.


@pytest.mark.parametrize("kind", ["missing_next", "duplicate", "backward_next", "invalid_next",
                                  "too_many", "not_dict", "below_cursor", "malformed"])
def test_remote_audit_rejects_broken_cursor_or_page(setup, kind):
    backend, rest, original = setup
    backend.apply_mutation(original)
    point_id = backend.mutation_point_id(original)
    def corrupt(result):
        point = result["points"][0]
        return {"missing_next": {"points": [point]}, "duplicate": {"points": [point, point], "next_page_offset": None},
                "backward_next": {"points": [point], "next_page_offset": point_id},
                "invalid_next": {"points": [point], "next_page_offset": True},
                "too_many": {"points": [point] * 17, "next_page_offset": None}, "not_dict": [],
                "below_cursor": {"points": [{"id": 0}], "next_page_offset": None},
                "malformed": {"points": [None], "next_page_offset": None}}[kind]
    rest.read_change = corrupt
    with pytest.raises(VectorBackendError):
        backend.scan_identities(point_id if kind == "below_cursor" else None)


def test_current_search_filters_revision_and_vector_hash_before_limit(setup):
    backend, rest, original = setup
    backend.apply_mutation(original)
    request = VectorSearch(original.room_id, original.model, (1., 0.), (original.record.anchor,))
    assert backend.search_current(request, (original,))[0].anchor == original.record.anchor
    body = rest.requests[-1][2]
    assert body["params"] == {"exact": True} and body["limit"] == 1
    clauses = body["filter"]["should"][0]["must"]
    assert {"key": "sync_revision", "match": {"value": original.revision}} in clauses
    assert {"key": "sync_vector_hash", "match": {"value": original.vector_hash}} in clauses
    assert body["with_payload"] is True and body["with_vector"] is False


@pytest.mark.parametrize("kind", ["older", "ahead", "hash"])
def test_current_search_rejects_wrong_revision_or_hash_even_with_matching_content(setup, kind):
    backend, rest, original = setup
    backend.apply_mutation(original)
    point = rest.points[backend.mutation_point_id(original)]
    if kind == "hash":
        point["payload"]["sync_vector_hash"] = "b" * 64
    else:
        point["payload"]["sync_revision"] = 3 if kind == "ahead" else 1
    request = VectorSearch(original.room_id, original.model, (1., 0.), (original.record.anchor,))
    with pytest.raises(VectorBackendError):
        backend.search_current(request, (replace(original, revision=2),))


@pytest.mark.parametrize("kind", ["tombstone", "anchor", "count", "list", "query_model"])
def test_current_search_rejects_untrusted_request_without_http(setup, kind):
    backend, rest, original = setup
    request = VectorSearch(original.room_id, original.model, (1., 0.), (original.record.anchor,))
    items = {"tombstone": (replace(original, record=None),),
             "anchor": (replace(original, record=replace(original.record,
                         anchor=replace(original.record.anchor, content_hash="b" * 64))),),
             "count": (), "list": [original], "query_model": (original,)}[kind]
    if kind == "query_model":
        request = replace(request, model=VectorModel("other", 2))
    before = len(rest.requests)
    with pytest.raises(ValueError):
        backend.search_current(request, items)
    assert len(rest.requests) == before
