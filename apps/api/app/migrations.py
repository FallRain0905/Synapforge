"""Minimal versioned PostgreSQL migration runner.

SQLite remains the default development backend. This module deliberately keeps
the production migration boundary explicit and only imports psycopg when used.
"""

from __future__ import annotations

import argparse
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_migrations(dsn: str) -> list[str]:
    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - optional production dependency
        raise RuntimeError("psycopg_required_for_postgres_migrations") from error

    applied: list[str] = []
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        connection.commit()
        existing = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for path in migration_files():
            if path.name in existing:
                continue
            with connection.transaction():
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Math Agent Platform PostgreSQL migrations")
    parser.add_argument("dsn", help="PostgreSQL DSN")
    args = parser.parse_args()
    for name in apply_migrations(args.dsn):
        print(f"applied {name}")


if __name__ == "__main__":
    main()
