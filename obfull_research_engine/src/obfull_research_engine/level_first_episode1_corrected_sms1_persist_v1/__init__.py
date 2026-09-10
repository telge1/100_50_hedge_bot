"""LEVEL_FIRST_EPISODE1_CORRECTED_SMS1_PERSIST_V1.

Isolated corrected sms1 persistence + derived-event rebuild for Episode 1.
Does not overwrite sms1_c509bf7f7fbc2e0b or gop1_72e90fdfc1243a7a.
No outcomes, OI, AVR, footprint, or liquidations.
"""

from __future__ import annotations

from datetime import datetime, timezone

AUDIT_ID = "LEVEL_FIRST_EPISODE1_CORRECTED_SMS1_PERSIST_V1"
CONTRACT_VERSION = "1.0.0"
SCHEMA_VERSION = "level_first_episode1_corrected_sms1_persist_v1"
RUN_PREFIX = "csp1_"
SYMBOL = "BTCUSDT"
EPISODE_ID = "ep:pc_7775a856ab22f006:1788725942"
EVIDENCE_START_ISO = "2026-09-06T20:14:02.229Z"
# TEST EXPECTATIONS ONLY — never feed these into derivation/calc paths.
EXPECTED_ZONE_FIRST_TOUCH_ISO = "2026-09-06T20:19:02.229Z"
EXPECTED_DETECTION_ISO = "2026-09-06T20:21:00Z"
FIRST_TOUCH_ISO = EXPECTED_ZONE_FIRST_TOUCH_ISO  # alias for regression tests
DETECTION_ISO = EXPECTED_DETECTION_ISO  # alias for regression tests
EFFECTIVE_BUCKET_START_ISO = "2026-09-06T20:14:02.229Z"
FIRST_BUCKET_AVAILABLE_AT_ISO = "2026-09-06T20:14:02.300Z"
PARENT_GOLDEN_RUN = "gop1_72e90fdfc1243a7a"
PARENT_GOLDEN_VERDICT = "EPISODE1_FULL_OB_STATE_GOLDEN_PARITY_PROVEN"

DEPTH_ABS_TOL_USDT = 1e-6
BUCKET_MS = 100
ALLOW_ARCHIVE_REPLAY = True
ALLOW_CLICKHOUSE_WRITES = False
FORBIDDEN_FIELDS = ("mfe", "mae", "outcome", "pnl", "entry_price", "exit_price", "trade_result")

FROZEN = {
    "sms1": "results/level_first_selective_ob_persist_smoke_v1/BTCUSDT/sms1_c509bf7f7fbc2e0b",
    "csp1": "results/level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/csp1_13058debfd4bba04",
    "gop1": "results/level_first_episode1_full_ob_state_golden_parity_v1/BTCUSDT/gop1_72e90fdfc1243a7a",
    "fp1": "results/level_first_episode1_chart_footprint_parity_audit_v1/BTCUSDT/fp1_2cd2bbcbc1fe4441",
    "msa1": "results/level_first_multimodal_raw_data_semantics_audit_v1/BTCUSDT/msa1_d20a334a8d2708f3",
    "fo1": "results/level_first_window_native_full_ob_direction_v1/BTCUSDT/fo1_12013db82d76a976",
    "fo2": "results/level_first_window_native_full_ob_direction_v2/BTCUSDT/fo2_fbd50c505e56d9ed",
    "lf1": "results/bounded_level_first_analyzer_pilot_v1/BTCUSDT/lf1_69e21d12d280596e",
}

SMS1_EP1_DIR = (
    "results/level_first_selective_ob_persist_smoke_v1/BTCUSDT/sms1_c509bf7f7fbc2e0b/"
    "episodes/ep_pc_7775a856ab22f006_1788725942"
)
SMS1_EP1_STATES = SMS1_EP1_DIR + "/states_100ms.jsonl.zst"
SMS1_EP1_WALLS = SMS1_EP1_DIR + "/walls.jsonl.zst"
SMS1_EP1_REFILLS = SMS1_EP1_DIR + "/refill_removal.jsonl.zst"
SMS1_EP1_TRADES = SMS1_EP1_DIR + "/public_trades.jsonl.zst"
SMS1_EP1_STATES_SHA256 = "fae86da05c927290b1754c0a4718d1e36610fbabbcd467f76e1f0b2c766ece87"

# Old sms1 field mapping (documented, not re-emitted):
#   bucket_ts -> bucket_start  (NOT available_at)
#   no bucket_end_exclusive, no available_at, no effective_bucket_start
#   replay_epoch -> single final epoch stamped on every row
FIELD_MAP_OLD_TO_NEW = {
    "bucket_ts": "bucket_start (not available_at)",
    "mid": "mid / mid_price",
    "spread": "spread_bps in old sms1; new also persists price spread",
    "replay_epoch": "per-state replay_epoch from reset stream (not final stamp)",
}

VERDICTS = (
    "EPISODE1_CORRECTED_SMS1_PERSISTENCE_AND_DERIVED_EVENTS_PARITY_PROVEN",
    "EPISODE1_CORRECTED_SMS1_GOLDEN_PARITY_FAILED",
    "EPISODE1_CORRECTED_SMS1_DETERMINISM_FAILED",
    "EPISODE1_CORRECTED_SMS1_ROUNDTRIP_FAILED",
    "EPISODE1_CORRECTED_SMS1_AVAILABLE_AT_VIOLATION",
    "EPISODE1_CORRECTED_SMS1_EPOCH_PERSIST_FAILED",
    "ARCHIVE_OR_COVERAGE_BLOCKED",
)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


EVIDENCE_START = parse_iso(EVIDENCE_START_ISO)
FIRST_TOUCH = parse_iso(FIRST_TOUCH_ISO)
DETECTION = parse_iso(DETECTION_ISO)
EXPECTED_ZONE_FIRST_TOUCH = parse_iso(EXPECTED_ZONE_FIRST_TOUCH_ISO)
EXPECTED_DETECTION = parse_iso(EXPECTED_DETECTION_ISO)
EFFECTIVE_BUCKET_START = parse_iso(EFFECTIVE_BUCKET_START_ISO)
FIRST_BUCKET_AVAILABLE_AT = parse_iso(FIRST_BUCKET_AVAILABLE_AT_ISO)

def __getattr__(name: str):
    if name == "run_audit":
        from .runner import run_audit

        return run_audit
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AUDIT_ID", "run_audit", "ALLOW_ARCHIVE_REPLAY", "ALLOW_CLICKHOUSE_WRITES"]
