"""Pilot scope: DOGEUSDT 15m PD10, research week 2026-08-01 … 2026-08-07."""

from __future__ import annotations

from pathlib import Path

CATALOG_ROOT = (
    Path(__file__).resolve().parents[1]
    / "doge_apr_sep_2026_v1_history"
).resolve()

PILOT_SYMBOL = "DOGEUSDT"
PILOT_TIMEFRAME = "15m"
PILOT_POOL_DETAIL = 10
PILOT_RANGE_START = "2026-08-01T00:00:00+00:00"
PILOT_RANGE_END_EXCLUSIVE = "2026-08-08T00:00:00+00:00"

# Isolated publish ledger line for historical mixed-frozen pilot (Sep 5m line stays separate).
PILOT_PIPELINE_LINE_ID = "historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1"

# Full ACTIVE membership per close (see historical_json_import/pilot_selection.py).
PILOT_DECISION_CLOSE_STEMS: tuple[str, ...] = (
    "20260801T000000Z",
    "20260801T164500Z",
    "20260802T093000Z",
    "20260803T021500Z",
    "20260803T190000Z",
    "20260804T114500Z",
    "20260805T043000Z",
    "20260805T211500Z",
    "20260806T140000Z",
    "20260807T064500Z",
)

AUDIT_POOL_IDS: tuple[str, ...] = (
    "lldv2:DOGEUSDT:15m:micro:lower:1785744900",
    "lldv2:DOGEUSDT:15m:micro:lower:1784898000",
    "lldv2:DOGEUSDT:15m:micro:lower:1785944700",
    "lldv2:DOGEUSDT:15m:micro:lower:1785609900",
    "lldv2:DOGEUSDT:15m:micro:lower:1785506400",
    "lldv2:DOGEUSDT:15m:micro:lower:1785541500",
    "lldv2:DOGEUSDT:15m:micro:lower:1785530700",
    "lldv2:DOGEUSDT:15m:micro:upper:1779068700",
    "lldv2:DOGEUSDT:15m:micro:upper:1785708900",
    "lldv2:DOGEUSDT:15m:micro:upper:1778813100",
    "lldv2:DOGEUSDT:15m:micro:upper:1786023900",
    "lldv2:DOGEUSDT:15m:micro:upper:1786015800",
    "lldv2:DOGEUSDT:15m:micro:upper:1778782500",
    "lldv2:DOGEUSDT:15m:normal:upper:1785540600",
    "lldv2:DOGEUSDT:15m:micro:upper:1778807700",
    "lldv2:DOGEUSDT:15m:micro:lower:1785769200",
    "lldv2:DOGEUSDT:15m:micro:lower:1784904300",
    "lldv2:DOGEUSDT:15m:micro:lower:1786023000",
    "lldv2:DOGEUSDT:15m:micro:lower:1785614400",
    "lldv2:DOGEUSDT:15m:micro:lower:1785512700",
)
