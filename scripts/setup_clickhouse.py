#!/usr/bin/env python3
"""Idempotent ClickHouse setup for the signal generator store.

Usage:
  python scripts/setup_clickhouse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.setup import setup_clickhouse, table_exists  # noqa: E402


def main() -> int:
    settings = get_clickhouse_settings()
    print(f"Connecting to {settings.host}:{settings.port} as {settings.user}")
    print(f"Target database: {settings.database}")

    client = setup_clickhouse(settings=settings)
    try:
        version, db, user = client.ping()
        print(f"Connected: version={version} database={db} user={user}")
        for table in ("candles_1m", "signals", "signal_outcomes"):
            exists = table_exists(client, table)
            print(f"  table {table}: {'OK' if exists else 'MISSING'}")
            if not exists:
                return 1
    finally:
        client.close()

    print("Setup complete (idempotent CREATE IF NOT EXISTS).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
