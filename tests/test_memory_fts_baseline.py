"""Readiness probing and immutable lexical replay are strictly offline."""

import copy
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals import check_memory_fts_baseline as baseline
from evals import run_memory_retrieval_ab as paired
from evals.validate_memory_unseen import validate_unseen


def _frozen() -> dict:
    return json.loads(baseline.FROZEN_PATH.read_text(encoding="utf-8"))


def test_frozen_replay_does_not_read_settings_call_cloud_or_touch_user_data(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("offline replay must not use settings or cloud embedding")

    monkeypatch.setattr(paired, "Settings", forbidden)
    monkeypatch.setattr(paired, "OpenAICompatibleEmbedder", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    original_read = Path.read_text

    def safe_read(path, *args, **kwargs):
        assert path.name != ".env"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", safe_read)
    original_seed = baseline._seed_case
    databases = []

    def recording_seed(case, path):
        assert path.parent.name.startswith("codecrew-memory-fts-baseline-")
        databases.append(path)
        return original_seed(case, path)

    monkeypatch.setattr(baseline, "_seed_case", recording_seed)
    report = baseline.inspect_baseline()
    assert report["baseline_matches_freeze"] is True
    assert report["agent_calls"] == report["embedding_calls"] == 0
    assert report["legacy"] == _frozen()["legacy"]
    assert len(databases) == 24 and all(not path.exists() for path in databases)
    assert report["legacy"]["summary"] == {
        "cases": 24, "tp": 10, "fp": 0, "fn": 2,
        "recall_at_3": 10 / 12, "precision_at_3": 1.0,
        "false_positive_queries_with_empty_gold": 0,
    }


class ProbeConnection:
    """Simulate extension builds without depending on CI's SQLite features."""

    def __init__(self, *, advertised=True, fail=None):
        self.advertised = advertised
        self.fail = fail
        self.closed = False

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA"):
            return [("ENABLE_FTS5",)] if self.advertised else []
        if sql.startswith("CREATE") and self.fail == "fts5":
            raise sqlite3.OperationalError("no such module: fts5")
        if "bm25" in sql and self.fail == "bm25":
            raise sqlite3.OperationalError("no such function: bm25")
        self.rows = [(1, -0.000001)] if "bm25" in sql else [(1,)]
        return self

    def executemany(self, *args):
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


def test_actual_probe_outweighs_missing_compile_flag(monkeypatch) -> None:
    connection = ProbeConnection(advertised=False)
    calls = []

    def connect(path):
        calls.append(path)
        return connection

    monkeypatch.setattr(baseline.sqlite3, "connect", connect)
    report = baseline.probe_fts5()
    assert calls == [":memory:"]
    assert report["compile_option_enable_fts5"] is False
    assert report["fts5_supported"] is report["bm25_supported"] is True
    assert report["error_stage"] is None
    assert connection.closed


@pytest.mark.parametrize("failure,fts5,stage", [
    ("fts5", False, "create_and_match"), ("bm25", True, "bm25"),
])
def test_probe_failure_is_not_hidden_by_compile_flag(monkeypatch, failure, fts5, stage) -> None:
    connection = ProbeConnection(fail=failure)
    monkeypatch.setattr(baseline.sqlite3, "connect", lambda path: connection)
    report = baseline.probe_fts5()
    assert report["compile_option_enable_fts5"] is True
    assert report["fts5_supported"] is fts5
    assert report["bm25_supported"] is False
    assert report["error_stage"] == stage
    assert report["error_type"] == "OperationalError"
    assert connection.closed


@pytest.mark.parametrize("field,error", [
    ("version", "unsupported"),
    ("dataset_sha256", "dataset changed"),
    ("label_lock_sha256", "label review changed"),
    ("contract", "contract changed"),
])
def test_frozen_metadata_drift_is_rejected(field, error) -> None:
    frozen = _frozen()
    measured = copy.deepcopy(frozen["legacy"])
    frozen[field] = "changed"
    with pytest.raises(ValueError, match=error):
        baseline.verify_frozen(measured, frozen)


def test_order_change_is_detected_even_when_aggregate_metrics_are_identical() -> None:
    frozen = _frozen()
    frozen["legacy"]["results"][0]["sources"] = ["m1", "m2"]
    measured = copy.deepcopy(frozen["legacy"])
    measured["results"][0]["sources"].reverse()
    assert measured["summary"] == frozen["legacy"]["summary"]
    with pytest.raises(ValueError, match="ordered sources"):
        baseline.verify_frozen(measured, frozen)


def test_code_fingerprint_changes_are_reported_not_silently_re_frozen(monkeypatch) -> None:
    original = baseline.FROZEN_PATH.read_bytes()
    frozen = _frozen()
    # Isolate this synthetic single-file change from legitimate newer source
    # revisions; never rewrite the on-disk historical freeze to satisfy a test.
    frozen["code_sha256"] = {name: baseline._sha256(baseline.ROOT / name) for name in baseline.CODE_PATHS}
    frozen["code_sha256"]["app/chat/memory.py"] = "old-code"
    # Replace only this module's parser binding, not the shared json module.
    monkeypatch.setattr(baseline, "json", SimpleNamespace(loads=lambda value: frozen))
    report = baseline.inspect_baseline()
    assert report["code_changed_since_freeze"] == ["app/chat/memory.py"]
    assert baseline.FROZEN_PATH.read_bytes() == original


@pytest.mark.parametrize("limit", [0, 4])
def test_existing_retrieve_limit_contract(tmp_path, limit) -> None:
    store, room_id, discussion_id, _ = paired._seed_case(
        validate_unseen()[0], tmp_path / "chat.sqlite3",
    )
    with pytest.raises(ValueError, match="recall limit"):
        store.memory.retrieve(room_id, "API", limit=limit,
                              exclude_correlation_id=discussion_id)


def test_cli_returns_failure_when_runtime_cannot_run_bm25(monkeypatch, capsys) -> None:
    frozen = _frozen()
    monkeypatch.setattr(baseline, "inspect_baseline", lambda: {
        "capability": {"python_version": "test", "sqlite_version": "test",
                       "fts5_supported": True, "bm25_supported": False},
        "legacy": frozen["legacy"], "code_changed_since_freeze": [],
    })
    monkeypatch.setattr(sys, "argv", ["check_memory_fts_baseline"])
    with pytest.raises(SystemExit) as error:
        baseline.main()
    assert error.value.code == 1
    assert "unavailable" in capsys.readouterr().err
