"""PostgreSQL migration runner with an applied-migrations ledger.

Migrations are the ordered ``migrations/*.sql`` files. Each file is applied
exactly once, in filename order, inside a transaction that also records its
SHA-256 checksum in ``kla_schema_migrations``. A recorded migration whose file
has changed checksum is an error — never silently re-run — so drift against the
pilot database is caught loudly.

Environment-specific migrations (the Supabase Auth/RLS baseline) declare an
opt-in marker line ``-- @requires <extension>`` and are skipped unless that
extension is requested. The runner never connects to the network and never
contains credentials; it receives an open DB-API connection.

The heavy PostgreSQL driver (psycopg v3) is imported lazily so edge machines
without the ``production`` extra can still import the package.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

MIGRATIONS_DIR_DEFAULT = Path(__file__).resolve().parents[3] / "migrations"

_REQUIRES_RE = re.compile(r"^--\s*@requires\s+([a-z0-9_.-]+)\s*$", re.IGNORECASE)
_BEGIN_RE = re.compile(r"^\s*BEGIN\s*;\s*$", re.IGNORECASE | re.MULTILINE)
_COMMIT_RE = re.compile(r"\bCOMMIT\s*;\s*$", re.IGNORECASE)

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS kla_schema_migrations (
    filename     TEXT PRIMARY KEY,
    checksum_sha256 CHAR(64) NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    """A migration cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One ordered SQL migration file."""

    filename: str
    sql: str
    checksum_sha256: str
    requires: tuple[str, ...]

    @property
    def sequence(self) -> int:
        """The leading integer of the filename (``001_core`` -> 1)."""

        match = re.match(r"^(\d+)", self.filename)
        if not match:
            raise MigrationError(f"migration filename must start with digits: {self.filename}")
        return int(match.group(1))


def discover_migrations(migrations_dir: Path | str = MIGRATIONS_DIR_DEFAULT) -> list[Migration]:
    """Read and checksum every ``*.sql`` migration in deterministic order."""

    directory = Path(migrations_dir)
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        requires = tuple(
            match.group(1).lower()
            for line in sql.splitlines()
            if (match := _REQUIRES_RE.match(line.strip()))
        )
        migrations.append(
            Migration(
                filename=path.name,
                sql=sql,
                checksum_sha256=sha256(sql.encode("utf-8")).hexdigest(),
                requires=requires,
            )
        )
    _assert_ordered_and_complete(migrations)
    return migrations


def _assert_ordered_and_complete(migrations: Sequence[Migration]) -> None:
    sequences = [migration.sequence for migration in migrations]
    if sequences != sorted(sequences):
        raise MigrationError("migration files are not in filename order")
    if len(set(sequences)) != len(sequences):
        raise MigrationError("migration files share a leading sequence number")
    for expected, actual in enumerate(sequences, start=1):
        if actual != expected:
            raise MigrationError(
                f"migration sequence gap: expected {expected:03d}, found {actual:03d}"
            )


def _ledger_insert_sql(migration: Migration) -> str:
    return (
        "INSERT INTO kla_schema_migrations (filename, checksum_sha256) VALUES ("
        f"'{migration.filename}', '{migration.checksum_sha256}');"
    )


def _apply_sql(migration: Migration) -> str:
    """SQL that applies the migration and records it atomically.

    Migration files are written as wrapped transactions (``BEGIN; ... COMMIT;``).
    The ledger insert is spliced in just before the final ``COMMIT`` so that a
    migrated schema and its ledger row commit together; a failed migration
    rolls back both and stays safely re-runnable.
    """

    stripped = migration.sql.strip()
    insert = _ledger_insert_sql(migration)
    if _BEGIN_RE.search(stripped) and _COMMIT_RE.search(stripped.rstrip()):
        last_commit = stripped.rfind("COMMIT;")
        prefix = stripped[:last_commit]
        return f"{prefix}\n{insert}\nCOMMIT;\n"
    return f"BEGIN;\n{stripped}\n{insert}\nCOMMIT;\n"


class DbConnection(Protocol):
    """Minimal DB-API surface the runner needs (psycopg v3 connection)."""

    def cursor(self) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


def _recorded_migrations(connection: DbConnection) -> dict[str, str]:
    """Return ``{filename: checksum}`` from the ledger, or ``{}`` if absent."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('public.kla_schema_migrations') IS NOT NULL AS present"
        )
        present = cursor.fetchone()[0]
        if not present:
            return {}
        cursor.execute("SELECT filename, checksum_sha256 FROM kla_schema_migrations")
        return {str(name): str(checksum) for name, checksum in cursor.fetchall()}


@dataclass(frozen=True, slots=True)
class PlannedMigration:
    migration: Migration
    reason: str  # "pending" | "skipped-requirement" | "applied"


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """The deterministic, connection-derived outcome of ``plan_migrations``."""

    applied: tuple[str, ...] = ()
    pending: tuple[PlannedMigration, ...] = ()
    skipped: tuple[PlannedMigration, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.errors


def plan_migrations(
    connection: DbConnection,
    migrations_dir: Path | str = MIGRATIONS_DIR_DEFAULT,
    enabled_requirements: Iterable[str] = (),
) -> MigrationPlan:
    """Decide what a subsequent :func:`run_migrations` would do, without doing it."""

    enabled = {item.lower() for item in enabled_requirements}
    recorded = _recorded_migrations(connection)
    all_migrations = discover_migrations(migrations_dir)

    pending: list[PlannedMigration] = []
    skipped: list[PlannedMigration] = []
    errors: list[str] = []
    skipped_sequences: set[int] = set()

    for migration in all_migrations:
        prior_checksum = recorded.get(migration.filename)
        if prior_checksum is not None:
            if prior_checksum != migration.checksum_sha256:
                errors.append(
                    f"{migration.filename}: checksum drift — applied {prior_checksum[:12]}… "
                    f"but file is {migration.checksum_sha256[:12]}…; restore the original "
                    "migration or review the change before proceeding"
                )
            continue
        unsatisfied = tuple(req for req in migration.requires if req not in enabled)
        if unsatisfied:
            skipped.append(
                PlannedMigration(
                    migration=migration,
                    reason=f"skipped-requirement (needs {', '.join(unsatisfied)})",
                )
            )
            skipped_sequences.add(migration.sequence)
            continue
        if any(migration.sequence > seq for seq in skipped_sequences):
            errors.append(
                f"{migration.filename}: pending after a skipped environment migration; "
                "enable the required environment (e.g. --require supabase) or the chain "
                "cannot continue"
            )
        pending.append(PlannedMigration(migration=migration, reason="pending"))

    known = {migration.filename for migration in all_migrations}
    for filename in sorted(set(recorded) - known):
        errors.append(f"{filename}: recorded in ledger but no migration file exists")

    return MigrationPlan(
        applied=tuple(sorted(recorded)),
        pending=tuple(pending),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def run_migrations(
    connection: DbConnection,
    migrations_dir: Path | str = MIGRATIONS_DIR_DEFAULT,
    enabled_requirements: Iterable[str] = (),
    *,
    dry_run: bool = False,
) -> MigrationPlan:
    """Apply every pending migration in filename order.

    Each migration is executed as one multi-statement transaction that also
    inserts the ledger row. On failure the transaction is rolled back, the
    error is reported, and no further migrations run.
    """

    plan = plan_migrations(connection, migrations_dir, enabled_requirements)
    if dry_run:
        return plan
    if not plan.is_clean:
        raise MigrationError("refusing to migrate: " + "; ".join(plan.errors))

    with connection.cursor() as cursor:
        cursor.execute(LEDGER_DDL)
    connection.commit()

    for item in plan.pending:
        try:
            with connection.cursor() as cursor:
                cursor.execute(_apply_sql(item.migration))
            connection.commit()
        except Exception as error:
            connection.rollback()
            raise MigrationError(f"{item.migration.filename}: apply failed: {error}") from error

    return plan_migrations(connection, migrations_dir, enabled_requirements)


def connect(dsn: str) -> DbConnection:
    """Open a psycopg v3 connection, imported lazily from the production extra."""

    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - exercised without driver
        raise MigrationError(
            "PostgreSQL driver not installed; run `pip install -e '.[production]'`"
        ) from error
    return psycopg.connect(dsn)
