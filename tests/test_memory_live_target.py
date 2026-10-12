"""Preparation with real temporary SQLite/FTS, fake remote and synthetic evidence.

No user DB/.env/network/providers. These controls are not real model evidence.
"""

import hashlib
import json
import stat
import urllib.request
from pathlib import Path

import pytest

from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_memory import EmbeddingEndpoint, OpenAICompatibleEmbedder
from app.config import Settings
from evals.run_memory_fts_ab import SharedEmbeddings
from scripts import prepare_memory_live_target as probe


@pytest.fixture(autouse=True)
def no_network_or_model(monkeypatch):
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(OpenAICompatibleEmbedder, "embed", lambda *a, **k: pytest.fail("paid embedding"))


def fixture(tmp_path, **changes):
    values = {"memory_embedding_mode": "on", "memory_embedding_base_url": "https://embedding.example/v1",
              "memory_embedding_model": "synthetic-model", "memory_embedding_revision": "unit-only",
              "memory_embedding_api_key": "synthetic-private-key", "memory_qdrant_api_key": "synthetic-qdrant-key"}
    values.update(changes)
    settings = Settings(_env_file=None, **values)
    endpoint = EmbeddingEndpoint(settings.memory_embedding_base_url, settings.memory_embedding_model,
        settings.memory_embedding_revision, settings.memory_embedding_api_key.get_secret_value())
    cache = SharedEmbeddings(fingerprint=endpoint.model_fingerprint)
    cache.vectors[hashlib.sha256(b"synthetic").hexdigest()] = cache._validate((1., 0.))
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(cache.artifact(identity={"model": endpoint.model, "revision": endpoint.revision}, complete=True)))
    return settings, path


class Remote:
    def __init__(self, endpoint, model, *, existing=False, fail=False, points=0):
        backend = QdrantVersionedVectorBackend(endpoint, model)  # Constructor only, no network.
        self.model, self.collection_name, self.sync_target_id = model, backend.collection_name, backend.sync_target_id
        self.exists, self.fail, self.points, self.initialized, self.checked = existing, fail, points, 0, 0

    def _collection(self):
        return {} if self.exists else None

    def initialize(self):
        self.exists = True
        self.initialized += 1
        if self.fail:
            raise RuntimeError("synthetic-private-error-key")

    def _require_sync_ready(self):
        self.checked += 1
        assert self.exists

    def _request(self, method, path):
        assert method == "GET"
        return {"points_count": self.points}

    @property
    def _path(self):
        return "/collections/" + self.collection_name

    def apply_mutation(self, *args):
        pytest.fail("preparation delivered events")


def prepare(tmp_path, **options):
    settings, cache = fixture(tmp_path)
    remotes = []
    def factory(endpoint, model):
        remote = Remote(endpoint, model, **options)
        remotes.append(remote)
        return remote
    output, report = probe.prepare_target(settings, cache_path=cache, output_root=tmp_path / "targets", backend_factory=factory)
    return output, report, remotes[0], settings


def test_plan_does_not_read_settings_or_allocate(monkeypatch, capsys):
    monkeypatch.setattr(probe, "Settings", lambda: pytest.fail("settings opened"))
    monkeypatch.setattr(probe, "prepare_target", lambda *a, **k: pytest.fail("target prepared"))
    probe.main([])
    assert "Plan only" in capsys.readouterr().out


def test_prepares_only_new_empty_target_no_start_or_seed_and_private_manifest(tmp_path):
    output, report, remote, settings = prepare(tmp_path)
    assert report["complete"] and report["phase"] == "ready_not_started"
    assert report["startup_preflight_passed"] and report["fts_usable"]
    assert report["inventory"]["schema_version"] == 29 and report["dimensions"] == 2
    assert report["inventory"]["counts"]["standalone_chat_vector_targets"] == 1
    assert all(n == 0 for t, n in report["inventory"]["counts"].items() if t != "standalone_chat_vector_targets")
    assert remote.initialized == remote.checked == 1
    assert report["embedding_calls"] == report["agent_calls"] == 0
    assert not report["worker_started"] and not report["chat_started"] and not report["old_data_imported"]
    assert report["dimension_evidence"]["current_credentials_verified"] is False
    assert report["runtime_overrides"]["memory_vector_store"] == "qdrant"
    assert report["runtime_overrides"]["memory_lexical_backend"] == "bm25"
    assert settings.memory_vector_store == "sqlite" and settings.memory_vector_worker_enabled is False
    text = (output / "prepare.json").read_text()
    assert "synthetic-private-key" not in text and "synthetic-qdrant-key" not in text
    assert "api_key" not in text
    assert json.loads(Path(report["backup_manifest"]).read_text())["complete"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(report["database"]).stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "prepare.json").stat().st_mode) == 0o600


def test_repeated_preparation_allocates_different_db_namespace(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    a, ra, _, _ = prepare(first)
    second = tmp_path / "second"
    second.mkdir()
    b, rb, _, _ = prepare(second)
    assert a != b and ra["collection"] != rb["collection"] and ra["target_id"] != rb["target_id"]


def test_cache_fingerprint_mismatch_refused_before_allocating(tmp_path):
    settings, cache = fixture(tmp_path)
    changed = settings.model_copy(update={"memory_embedding_revision": "different"})
    with pytest.raises(ValueError, match="evidence_mismatch"):
        probe.prepare_target(changed, cache_path=cache, output_root=tmp_path / "targets")
    assert not (tmp_path / "targets").exists()


def test_cache_tampering_refused_before_allocating(tmp_path):
    settings, cache = fixture(tmp_path)
    data = json.loads(cache.read_text())
    data["vectors"][next(iter(data["vectors"]))] = [0., 1.]
    cache.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checksum mismatch"):
        probe.prepare_target(settings, cache_path=cache, output_root=tmp_path / "targets")
    assert not (tmp_path / "targets").exists()


def test_missing_cache_refused_without_resource_creation(tmp_path):
    settings, _ = fixture(tmp_path)
    with pytest.raises(FileNotFoundError):
        probe.prepare_target(settings, cache_path=tmp_path / "absent", output_root=tmp_path / "targets")
    assert not (tmp_path / "targets").exists()


def test_external_qdrant_refused_before_allocation(tmp_path):
    settings, cache = fixture(tmp_path, memory_qdrant_url="https://foreign.example")
    with pytest.raises(ValueError, match="selected_local"):
        probe.prepare_target(settings, cache_path=cache, output_root=tmp_path / "targets")
    assert not (tmp_path / "targets").exists()


def test_collection_collision_not_repaired_or_reused(tmp_path):
    output, report, remote, _ = prepare(tmp_path, existing=True)
    assert not report["complete"] and report["phase"] == "schema_prepared"
    assert remote.initialized == 0 and report["resources_retained_for_review"]
    assert "synthetic-private" not in (output / "prepare.json").read_text()


def test_partial_remote_failure_retained_safe_and_no_local_subscription(tmp_path):
    output, report, _, _ = prepare(tmp_path, fail=True)
    assert not report["complete"] and report["phase"] == "creating_dedicated_collection"
    assert report["error_type"] == "RuntimeError"
    assert probe.inventory(Path(report["database"]))["counts"]["standalone_chat_vector_targets"] == 0
    assert "synthetic-private-error" not in (output / "prepare.json").read_text()


def test_nonempty_remote_never_marked_ready(tmp_path):
    _, report, _, _ = prepare(tmp_path, points=1)
    assert not report["complete"] and report["phase"] == "target_registered"
    assert report["resources_retained_for_review"]


def test_disabled_embedding_refused_before_allocation(tmp_path):
    settings, cache = fixture(tmp_path, memory_embedding_mode="off")
    with pytest.raises(ValueError, match="not_enabled"):
        probe.prepare_target(settings, cache_path=cache, output_root=tmp_path / "targets")
    assert not (tmp_path / "targets").exists()


def test_existing_user_db_never_opened_or_migrated(tmp_path):
    old = tmp_path / "old.sqlite3"
    old.write_bytes(b"synthetic do not open")
    original = old.read_bytes()
    prepare(tmp_path)
    assert old.read_bytes() == original
