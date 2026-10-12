"""Temporary SQLite only; no actual user DB/settings/network/model usage."""

import json
import sqlite3
import stat
from contextlib import closing

import pytest

from scripts import backup_memory_live_database as backup


def source_database(tmp_path):
    path = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE schema_migrations(version INTEGER)")
        conn.execute("INSERT INTO schema_migrations VALUES(19)")
        conn.execute("CREATE TABLE standalone_chat_messages(content TEXT)")
        conn.execute("INSERT INTO standalone_chat_messages VALUES('synthetic private content')")
        conn.commit()
    return path


def test_inspection_only_aggregates_without_content(tmp_path):
    source = source_database(tmp_path)
    report = backup.inventory(source)
    assert report["schema_version"] == 19 and report["counts"] == {"standalone_chat_messages": 1}
    assert report["memory_schema_present"] is False
    assert "synthetic private content" not in json.dumps(report)
    assert not (tmp_path / "backups").exists()


def test_missing_source_never_created(tmp_path):
    source = tmp_path / "absent.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup.backup_database(source, output_root=tmp_path / "backups")
    assert not source.exists() and not (tmp_path / "backups").exists()


def test_source_connection_refuses_writes(tmp_path):
    source = source_database(tmp_path)
    with closing(backup.readonly(source)) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO schema_migrations VALUES(29)")


def test_online_backup_includes_committed_wal_and_private_permissions(tmp_path):
    source = source_database(tmp_path)
    # Keep writer open so committed data still lives in WAL, not only main file.
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("INSERT INTO standalone_chat_messages VALUES('second private row')")
        writer.commit()
        output, report = backup.backup_database(source, output_root=tmp_path / "backups")
        assert report["complete"] and report["inventory"]["counts"]["standalone_chat_messages"] == 2
        assert report["inventory"]["schema_version"] == 19  # No hidden migration.
        assert backup.inventory(source) == report["inventory"]
        assert "private row" not in (output / "manifest.json").read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for path in (output / "database.sqlite3", output / "manifest.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (output / "partial.sqlite3").exists()


def test_repeated_backups_never_overwrite(tmp_path):
    source = source_database(tmp_path)
    first, a = backup.backup_database(source, output_root=tmp_path / "backups")
    second, b = backup.backup_database(source, output_root=tmp_path / "backups")
    assert first != second and a["sha256"] == b["sha256"] and a["complete"] and b["complete"]


def test_timeout_keeps_unusable_partial_and_does_not_print_private_error(tmp_path, monkeypatch):
    source = source_database(tmp_path)
    ticks = iter((0, 100))
    monkeypatch.setattr(backup.time, "monotonic", lambda: next(ticks))
    output, report = backup.backup_database(source, output_root=tmp_path / "backups", timeout_seconds=1)
    assert not report["complete"] and report["error_type"] == "TimeoutError"
    assert (output / "partial.sqlite3").exists() and not (output / "database.sqlite3").exists()
    assert "backup" not in report


def test_cli_readonly_mode_no_backup(tmp_path, monkeypatch, capsys):
    source = source_database(tmp_path)
    monkeypatch.setattr(backup, "backup_database", lambda *a, **k: pytest.fail("inspection backed up"))
    backup.main(["--database", str(source)])
    assert json.loads(capsys.readouterr().out)["schema_version"] == 19


@pytest.mark.parametrize("timeout", [True, 0, -1, 31, float("nan"), float("inf")])
def test_backup_timeout_bounds(tmp_path, timeout):
    source = source_database(tmp_path)
    with pytest.raises(ValueError):
        backup.backup_database(source, output_root=tmp_path / "backups", timeout_seconds=timeout)
    assert not (tmp_path / "backups").exists()
