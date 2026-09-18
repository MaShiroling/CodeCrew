import hashlib
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class SQLiteStorageError(RuntimeError):
    """Base error for the SQLite storage boundary."""


class MigrationConflictError(SQLiteStorageError):
    """Raised when an applied migration version has different content."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise ValueError("migration version must be positive")
        if not self.name.strip():
            raise ValueError("migration name must not be empty")
        if not self.statements or any(not statement.strip() for statement in self.statements):
            raise ValueError("migration statements must not be empty")

    @property
    def checksum(self) -> str:
        content = "\x00".join(self.statements).encode("utf-8")
        return hashlib.sha256(content).hexdigest()


class SQLiteDatabase:
    """Small file-backed SQLite boundary with deterministic migrations."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if str(path) == ":memory:":
            raise ValueError("use a file-backed temporary database instead of :memory:")
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._write_lock = threading.RLock()

    def initialize(self, migrations: Sequence[Migration] = ()) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
        self.apply_migrations(migrations)

    def apply_migrations(self, migrations: Sequence[Migration]) -> None:
        ordered = sorted(migrations, key=lambda migration: migration.version)
        versions = [migration.version for migration in ordered]
        if len(versions) != len(set(versions)):
            raise ValueError("migration versions must be unique")

        for migration in ordered:
            with self.transaction() as connection:
                applied = connection.execute(
                    "SELECT name, checksum FROM schema_migrations WHERE version = ?",
                    (migration.version,),
                ).fetchone()
                if applied is not None:
                    if applied["name"] != migration.name or applied["checksum"] != migration.checksum:
                        raise MigrationConflictError(
                            f"migration version {migration.version} differs from applied content"
                        )
                    continue

                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

    @property
    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run one atomic transaction and serialize in-process writers."""

        lock = self._write_lock if immediate else _NullLock()
        with lock, self.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

