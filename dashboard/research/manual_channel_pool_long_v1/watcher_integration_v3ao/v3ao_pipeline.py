"""V3AK downstream stack reused for V3AO (no duplicated exit engine)."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
V3AK = PROBE / "substantial_entry_gate_v3ak"
V3AH = PROBE / "realistic_pool_tp_v3ah"
V3AB = PROBE / "support_break_exit_v3ab"
V3Y = PROBE / "support_break_exit_v3y"
for p in (V3AK, V3AH, V3AB, PROBE, V3Y):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from config import L1_V3E_VARIANT_B
from substantial_entry_gate import ENTRY_GATE_SPECS, apply_substantial_entry_gate
from v3ah_tp import pending_with_realistic_tp
from v3ak_context import TP_SCENARIO
from zone_density import RealisticTPParams

_v3y_engine = importlib.util.spec_from_file_location("v3y_engine", V3Y / "engine.py")
assert _v3y_engine and _v3y_engine.loader
_v3y_engine_mod = importlib.util.module_from_spec(_v3y_engine)
_v3y_engine.loader.exec_module(_v3y_engine_mod)
execute_variant_v3y = _v3y_engine_mod.execute_variant_v3y

VARIANT_B = L1_V3E_VARIANT_B
CAP6_TP = RealisticTPParams(
    code=TP_SCENARIO,
    cap_dist_pct=6.0,
    dominance="D20",
    no_tp_policy="BLOCK",
)


def pending_candidates(pending: list[dict]) -> list[dict]:
    return [p for p in pending if p.get("long_trigger_allowed") == "yes" and p.get("_entry_idx")]


def prepare_pending_for_execution(
    pending: list[dict],
    lines: list,
    *,
    entry_gate_code: str = "E3_SUBSTANTIAL_TP2c",
    upper_line=None,
) -> list[dict]:
    mod = sys.modules[l1.__name__]
    tp_pending = pending_with_realistic_tp(copy.deepcopy(pending), lines, l1_module=mod, params=CAP6_TP)
    spec = next(s for s in ENTRY_GATE_SPECS if s.code == entry_gate_code)
    apply_substantial_entry_gate(tp_pending, spec)
    pe.enrich_pending_metrics(tp_pending, upper_line, mod, independent=True)
    return tp_pending


def execute_trades(
    pending_ready: list[dict],
    candles: list[dict],
    upper_line,
    lines_by_id: dict,
) -> list[dict]:
    mod = sys.modules[l1.__name__]
    return execute_variant_v3y(
        VARIANT_B,
        copy.deepcopy(pending_ready),
        candles,
        upper_line=upper_line,
        lines_by_id=lines_by_id,
        l1_module=mod,
    )


def executed_trades(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("exit_kind") not in ("", "NO_TRADE", None)]


def run_pipeline_on_pending(
    pending: list[dict],
    *,
    lines: list,
    candles: list[dict],
    upper_line,
    lines_by_id: dict,
    entry_gate_code: str = "E3_SUBSTANTIAL_TP2c",
) -> dict[str, Any]:
    ready = prepare_pending_for_execution(
        pending, lines, entry_gate_code=entry_gate_code, upper_line=upper_line
    )
    trades = execute_trades(ready, candles, upper_line, lines_by_id)
    return {
        "pending_ready": ready,
        "trades": trades,
        "executed": executed_trades(trades),
    }


def compare_trade_lists(a: list[dict], b: list[dict]) -> list[str]:
    mismatches: list[str] = []
    if len(a) != len(b):
        mismatches.append(f"trade_count: {len(a)} vs {len(b)}")
    n = min(len(a), len(b))
    keys = (
        "touch_event_id",
        "entry_utc",
        "entry_price",
        "exit_utc",
        "exit_price",
        "exit_kind",
        "gross_pnl_pct",
        "sl_price",
        "tp_price",
    )
    for i in range(n):
        for k in keys:
            va = str(a[i].get(k, ""))
            vb = str(b[i].get(k, ""))
            if va != vb:
                mismatches.append(f"trade[{i}].{k}: {va!r} != {vb!r}")
    return mismatches
