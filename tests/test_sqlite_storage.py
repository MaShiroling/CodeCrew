import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.storage import Migration, MigrationConflictError, SQLiteDatabase

CREATE_WIDGETS = Migration(
    version=1,
    name="create_widgets",
    statements=(
        """
        CREATE TABLE widgets (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        )
        """,
    ),
)


def make_database(tmp_path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(tmp_path / "nested" / "codecrew.sqlite3")
    database.initialize([CREATE_WIDGETS])
    return database


def test_initializes_file_wal_foreign_keys_and_version(tmp_path: Path) -> None:
    database = make_database(tmp_path)

    with database.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

    assert database.path.is_file()
    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert database.schema_version == 1


def test_migrations_are_idempotent_across_restarts(tmp_path: Path) -> None:
    path = tmp_path / "codecrew.sqlite3"
    first = SQLiteDatabase(path)
    first.initialize([CREATE_WIDGETS])
    second = SQLiteDatabase(path)

    second.initialize([CREATE_WIDGETS])

    with second.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 1


def test_changed_applied_migration_is_rejected(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    changed = Migration(
        version=1,
        name="create_widgets",
        statements=("CREATE TABLE other_table (id INTEGER PRIMARY KEY)",),
    )

    with pytest.raises(MigrationConflictError, match="differs from applied content"):
        database.apply_migrations([changed])


def test_transaction_commits_and_rolls_back_atomically(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    with database.transaction() as connection:
        connection.execute("INSERT INTO widgets(name) VALUES (?)", ("committed",))

    with pytest.raises(RuntimeError, match="abort"), database.transaction() as connection:
        connection.execute("INSERT INTO widgets(name) VALUES (?)", ("rolled-back",))
        raise RuntimeError("abort")

    with database.connect() as connection:
        names = [row["name"] for row in connection.execute("SELECT name FROM widgets")]
    assert names == ["committed"]


def test_failed_migration_leaves_no_partial_schema_or_version(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    database.initialize()
    broken = Migration(
        version=1,
        name="broken",
        statements=(
            "CREATE TABLE created_before_failure (id INTEGER PRIMARY KEY)",
            "THIS IS NOT SQL",
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        database.apply_migrations([broken])

    with database.connect() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'created_before_failure'"
        ).fetchone()
    assert table is None
    assert database.schema_version == 0


def test_foreign_key_violations_are_enforced(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    database.initialize(
        [
            Migration(
                version=1,
                name="foreign_keys",
                statements=(
                    "CREATE TABLE parents (id INTEGER PRIMARY KEY)",
                    """
                    CREATE TABLE children (
                        id INTEGER PRIMARY KEY,
                        parent_id INTEGER NOT NULL REFERENCES parents(id)
                    )
                    """,
                ),
            )
        ]
    )

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute("INSERT INTO children(parent_id) VALUES (999)")


def test_concurrent_writes_are_serialized(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "codecrew.sqlite3")
    database.initialize(
        [
            Migration(
                version=1,
                name="counter",
                statements=("CREATE TABLE counter (value INTEGER NOT NULL)",),
            )
        ]
    )
    with database.transaction() as connection:
        connection.execute("INSERT INTO counter(value) VALUES (0)")

    def increment() -> None:
        with database.transaction() as connection:
            connection.execute("UPDATE counter SET value = value + 1")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: increment(), range(40)))

    with database.connect() as connection:
        value = connection.execute("SELECT value FROM counter").fetchone()[0]
    assert value == 40


def test_migration_validation() -> None:
    with pytest.raises(ValueError, match="positive"):
        Migration(version=0, name="bad", statements=("SELECT 1",))
    with pytest.raises(ValueError, match="name"):
        Migration(version=1, name=" ", statements=("SELECT 1",))
    with pytest.raises(ValueError, match="statements"):
        Migration(version=1, name="bad", statements=())


def test_rejects_in_memory_database_and_invalid_timeout() -> None:
    with pytest.raises(ValueError, match="file-backed"):
        SQLiteDatabase(Path(":memory:"))
    with pytest.raises(ValueError, match="positive"):
        SQLiteDatabase(Path("database.sqlite3"), busy_timeout_ms=0)
