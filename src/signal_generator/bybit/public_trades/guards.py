"""Guards for canonical public-trade writes."""

from __future__ import annotations

import re

CANONICAL_DATABASE = "orderbook_analysis"
CANONICAL_TABLE = "public_trades_canonical"
CANONICAL_FQN = f"{CANONICAL_DATABASE}.{CANONICAL_TABLE}"

FORBIDDEN_DATABASES = frozenset({"signal_generator"})
FORBIDDEN_TABLES = frozenset(
    {
        "candles_1m",
        "signals",
        "signal_outcomes",
        "signal_processing_state",
        "public_trades",
        "public_trades_archive",
        "orderbook_deltas",
        "ticker_samples",
        "liquidations",
        "recorder_health",
    }
)

_SQL_TABLE_RE = re.compile(
    r"(?:FROM|INTO|TABLE|UPDATE|JOIN)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?:`?(\w+)`?\.)?`?(\w+)`?",
    re.IGNORECASE,
)

ALLOWED_SOURCES = frozenset({"archive", "live", "gap_fill"})


class CanonicalTradeGuardError(RuntimeError):
    """Refused canonical public-trade operation."""


def assert_canonical_table(table: str) -> str:
    raw = str(table).strip().strip("`")
    db = ""
    name = raw
    if "." in raw:
        db, name = raw.split(".", 1)
    name = name.strip("`")
    db = db.strip("`")
    if name in FORBIDDEN_TABLES:
        raise CanonicalTradeGuardError(f"refusing write to forbidden table: {raw}")
    if db in FORBIDDEN_DATABASES:
        raise CanonicalTradeGuardError(f"refusing write to scanner database: {raw}")
    if name != CANONICAL_TABLE:
        raise CanonicalTradeGuardError(
            f"canonical ingest only allows {CANONICAL_TABLE}; got {raw}"
        )
    if db and db != CANONICAL_DATABASE:
        raise CanonicalTradeGuardError(
            f"canonical ingest only allows {CANONICAL_DATABASE}; got {raw}"
        )
    return CANONICAL_FQN


def assert_canonical_sql(sql: str) -> None:
    upper = f" {sql.lstrip().upper()} "
    for token in (" DROP ", " DELETE ", " TRUNCATE ", " ALTER ", " RENAME ", " OPTIMIZE "):
        if token in upper:
            raise CanonicalTradeGuardError(f"forbidden SQL token: {token.strip()}")
    if "CANDLES_1M" in upper:
        raise CanonicalTradeGuardError("refusing SQL that mentions candles_1m")
    if "ORDERBOOK_DELTAS" in upper:
        raise CanonicalTradeGuardError("refusing SQL that mentions orderbook_deltas")
    if "PUBLIC_TRADES_ARCHIVE" in upper and "CREATE TABLE" not in upper:
        raise CanonicalTradeGuardError("refusing SQL that mentions public_trades_archive")
    for match in _SQL_TABLE_RE.finditer(sql):
        db, tbl = match.group(1) or "", match.group(2)
        if tbl and tbl.lower() in {"if", "not", "exists"}:
            continue
        if tbl in FORBIDDEN_TABLES:
            raise CanonicalTradeGuardError(f"refusing SQL table {tbl}")
        if db in FORBIDDEN_DATABASES and tbl != CANONICAL_TABLE:
            raise CanonicalTradeGuardError(f"refusing SQL database {db}")


def assert_source(source: str) -> str:
    value = str(source).strip().lower()
    if value not in ALLOWED_SOURCES:
        raise CanonicalTradeGuardError(f"invalid source={source!r}")
    return value
