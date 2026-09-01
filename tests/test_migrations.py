from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Self

from kla_sync.db.migrations import (
    LEDGER_DDL,
    MigrationError,
    discover_migrations,
    run_migrations,
)


class FakeCursor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.executed: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        self.db.execute(sql)

    def fetchone(self) -> tuple[object, ...] | None:
        return self.db.fetchone()

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.db.fetchall()


class FakeDb:
    """Minimal DB-API emulation for the ledger/runner logic.

    Execute writes into a transaction-local staging copy; ``commit`` makes it
    visible and ``rollback`` discards it, mirroring a real connection.
    """

    def __init__(self, fail_on: str | None = None) -> None:
        self.committed_ledger: dict[str, str] = {}
        self.has_table = False
        self.commits = 0
        self.rollbacks = 0
        self.applied_scripts: list[str] = []
        self.fail_on = fail_on
        self._staging: dict[str, str] | None = None
        self._next_rows: list[tuple[object, ...]] | None = None

    @property
    def ledger(self) -> dict[str, str]:
        return self._staging if self._staging is not None else self.committed_ledger

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        if self._staging is not None:
            self.committed_ledger = self._staging
            self._staging = None

    def rollback(self) -> None:
        self.rollbacks += 1
        self._staging = None

    def execute(self, sql: str) -> None:
        stripped = sql.strip()
        if self._staging is None:
            # Any write starts an implicit transaction; reads see committed state.
            self._staging = dict(self.committed_ledger)
        if "CREATE TABLE IF NOT EXISTS kla_schema_migrations" in stripped:
            self.has_table = True
            return
        if stripped.startswith("SELECT to_regclass"):
            self._next_rows = [(self.has_table,)]
            return
        if stripped.startswith("SELECT filename, checksum"):
            self._next_rows = [(name, checksum) for name, checksum in self.ledger.items()]
            return
        if self.fail_on and self.fail_on in stripped:
            raise RuntimeError(f"simulated failure executing: {self.fail_on}")
        # The apply script is multi-statement; find the spliced ledger insert.
        if "INSERT INTO kla_schema_migrations" in stripped:
            fragment = stripped[stripped.index("INSERT INTO kla_schema_migrations"):]
            name = self._between(fragment, "VALUES ('", "', '")
            checksum = self._between(fragment, "', '", "');")
            self.ledger[name] = checksum
            self.applied_scripts.append(stripped)

    @staticmethod
    def _between(text: str, start: str, end: str) -> str:
        i = text.index(start) + len(start)
        j = text.index(end, i)
        return text[i:j]

    def fetchone(self) -> tuple[object, ...] | None:
        if self._next_rows:
            return self._next_rows.pop(0)
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = self._next_rows or []
        self._next_rows = []
        return rows


def _write_migrations(directory: Path, specs: dict[str, str]) -> None:
    for name, body in specs.items():
        (directory / name).write_text(body, encoding="utf-8")


WRAPPED = "BEGIN;\nCREATE TABLE thing_{n} (id INT);\nCOMMIT;\n"
PLAIN = "CREATE TABLE thing_{n} (id INT);\n"


class DiscoverTests(unittest.TestCase):
    def test_discover_orders_and_checksums_repo_migrations(self) -> None:
        migrations = discover_migrations()
        self.assertGreaterEqual(len(migrations), 4)
        sequences = [m.sequence for m in migrations]
        self.assertEqual(sequences, list(range(1, len(migrations) + 1)))
        for migration in migrations:
            self.assertEqual(len(migration.checksum_sha256), 64)

    def test_supabase_migration_declares_requirement(self) -> None:
        migrations = {m.filename: m for m in discover_migrations()}
        self.assertIn("supabase", migrations["002_supabase_rls.sql"].requires)
        self.assertEqual(migrations["001_core_schema.sql"].requires, ())

    def test_sequence_gap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_migrations(directory, {"001_a.sql": WRAPPED.format(n=1), "003_c.sql": WRAPPED.format(n=3)})
            with self.assertRaises(MigrationError):
                discover_migrations(directory)


class PlanTests(unittest.TestCase):
    def test_dry_run_plans_all_pending_without_applying(self) -> None:
        db = FakeDb()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_migrations(
                directory,
                {
                    "001_a.sql": WRAPPED.format(n=1),
                    "002_b.sql": PLAIN.format(n=2),
                },
            )
            plan = run_migrations(db, directory, dry_run=True)
        self.assertTrue(plan.is_clean)
        self.assertEqual(len(plan.pending), 2)
        self.assertEqual(plan.applied, ())
        self.assertEqual(db.ledger, {})

    def test_applies_wrapped_and_plain_migrations_once(self) -> None:
        db = FakeDb()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_migrations(
                directory,
                {
                    "001_wrapped.sql": WRAPPED.format(n=1),
                    "002_plain.sql": PLAIN.format(n=2),
                },
            )
            run_migrations(db, directory)
            # Idempotent: a second run applies nothing.
            plan = run_migrations(db, directory)
        self.assertEqual(set(db.committed_ledger), {"001_wrapped.sql", "002_plain.sql"})
        self.assertEqual(plan.pending, ())
        self.assertEqual(len(plan.applied), 2)
        # DDL commit + one commit per applied migration; the re-plan only reads.
        self.assertGreaterEqual(db.commits, 3)

    def test_checksum_drift_is_an_error(self) -> None:
        db = FakeDb()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_migrations(directory, {"001_a.sql": WRAPPED.format(n=1)})
            run_migrations(db, directory)
            # Modify the applied migration file.
            (directory / "001_a.sql").write_text("BEGIN;\nCREATE TABLE thing_99 (id INT);\nCOMMIT;\n")
            plan = run_migrations(db, directory, dry_run=True)
        self.assertFalse(plan.is_clean)
        self.assertTrue(any("checksum drift" in error for error in plan.errors))

    def test_skipped_environment_migration_blocks_later_pending(self) -> None:
        db = FakeDb()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_migrations(
                directory,
                {
                    "001_core.sql": WRAPPED.format(n=1),
                    "002_env.sql": "-- @requires supabase\n" + WRAPPED.format(n=2),
                    "003_after.sql": WRAPPED.format(n=3),
                },
            )
            plan = run_migrations(db, directory, enabled_requirements=(), dry_run=True)
        self.assertEqual(len(plan.skipped), 1)
        self.assertFalse(plan.is_clean)
        self.assertTrue(any("pending after a skipped" in error for error in plan.errors))

    def test_enabling_requirement_applies_chain(self) -> None:
        db = FakeDb()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_migrations(
                directory,
                {
                    "001_core.sql": WRAPPED.format(n=1),
                    "002_env.sql": "-- @requires supabase\n" + WRAPPED.format(n=2),
                },
            )
            run_migrations(db, directory, enabled_requirements=("supabase",))
        self.assertEqual(set(db.committed_ledger), {"001_core.sql", "002_env.sql"})

    def test_failure_rolls_back_and_reports(self) -> None:
        db = FakeDb(fail_on="thing_2")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _write_migrations(
                directory,
                {
                    "001_a.sql": WRAPPED.format(n=1),
                    "002_b.sql": PLAIN.format(n=2),
                },
            )
            with self.assertRaises(MigrationError):
                run_migrations(db, directory)
        self.assertIn("001_a.sql", db.committed_ledger)
        self.assertNotIn("002_b.sql", db.committed_ledger)
        self.assertGreaterEqual(db.rollbacks, 1)

    def test_ledger_ddl_is_present(self) -> None:
        self.assertIn("kla_schema_migrations", LEDGER_DDL)


if __name__ == "__main__":
    unittest.main()
