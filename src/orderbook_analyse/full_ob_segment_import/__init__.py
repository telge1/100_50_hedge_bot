"""full_ob_finalized_segment_clickhouse_import_v1

Async-friendly, idempotent importer for finalized Full-OB flight-recorder segments.
Never runs inside the live collector process.
"""

from __future__ import annotations

CONTRACT_ID = "full_ob_finalized_segment_clickhouse_import_v1"
DEFAULT_PILOT_DATABASE = "research_full_ob_import_pilot_v1"
FORBIDDEN_DATABASES = frozenset(
    {
        "orderbook_analysis",
        "signal_generator",
        "default",
        "system",
        "INFORMATION_SCHEMA",
        "information_schema",
    }
)
# Known research DBs that must not be mutated by this importer unless explicitly allowed later.
PROTECTED_RESEARCH_DATABASES = frozenset(
    {
        "research_full_ob_smoke",
        "research_full_ob_btc_20260904_signal_analysis",
        "btc_doge_research",
    }
)

OPEN_SUFFIXES = (".tmp", ".partial", ".open")
DELTA_FILENAME = "full_ob_raw_deltas.jsonl.zst"
