"""Audit window and path configuration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

SYMBOL = "BTCUSDT"
ANCHOR = datetime(2026, 8, 31, 19, 0, 0, tzinfo=timezone.utc)
CORE_START = datetime(2026, 8, 31, 18, 30, 0, tzinfo=timezone.utc)
CORE_END = datetime(2026, 8, 31, 19, 30, 0, tzinfo=timezone.utc)
EXTENDED_END = datetime(2026, 8, 31, 21, 30, 0, tzinfo=timezone.utc)

ROOT = Path(__file__).resolve().parents[2]
RUN_017 = ROOT / "results/btc_ob_fight_cases/20260831T190000Z/run_017"
OUT = ROOT / "results/btc_ob_fight_explanatory_audit_20260831_1900_v1"

# Frozen profile levels from run_017 (causal at anchor)
TPO_VAH = 79080.0
TPO_VAL = 78230.0
VOLUME_VVAH = 79140.0
VOLUME_VVAL = 78190.0
UPPER_OUTER = 79140.0
UPPER_INNER = 79080.0
TICK_SIZE = 0.1

VERDICT_COMPLETE = "BTC_OB_FIGHT_EXPLANATORY_AUDIT_COMPLETE"
VERDICT_PARTIAL = "BTC_OB_FIGHT_EXPLANATORY_AUDIT_PARTIAL"
VERDICT_BLOCKED = "BTC_OB_FIGHT_EXPLANATORY_AUDIT_BLOCKED"
