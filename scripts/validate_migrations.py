"""Parse PostgreSQL migrations without needing a running database server."""

from __future__ import annotations

from pathlib import Path

try:
    from pglast import parse_sql
except ImportError as error:  # pragma: no cover - developer environment guard
    raise SystemExit("Install the development dependencies: pip install -e '.[dev]'") from error


def main() -> int:
    migration_paths = sorted(Path("migrations").glob("*.sql"))
    if not migration_paths:
        raise SystemExit("No migration files found")
    for path in migration_paths:
        try:
            statements = parse_sql(path.read_text(encoding="utf-8"))
        except Exception as error:
            raise SystemExit(f"{path}: PostgreSQL parse error: {error}") from error
        if not statements:
            raise SystemExit(f"{path}: migration contains no SQL statements")
        print(f"{path}: parsed {len(statements)} statements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
