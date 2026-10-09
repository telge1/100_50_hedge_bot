#!/usr/bin/env python3
"""Compare Jun–Jul backtest signals: full pane vs pane truncated at decision_time."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "results/pool_scan/context_study_v1"))

from pool_pattern.market import ensure_paths

ensure_paths()

from bot.e1r_live_scanner.config import ScannerConfig as ShortCfg
from bot.e1r_live_scanner.config import PANE_FROM_DEFAULT as SHORT_PANE_FROM
from bot.e1r_live_scanner.config import E1R_SIM_FROM_DEFAULT
from bot.e1r_live_scanner.parity import PANE_TO as SHORT_PANE_TO
from bot.e1r_live_scanner.parity import SIM_TO
from bot.e1r_live_scanner.processing import SymbolProcessor as ShortProc
from bot.e1r_live_scanner.state import Readiness
from bot.long_v1_live_scanner.config import ScannerConfig as LongCfg
from bot.long_v1_live_scanner.config import PARITY_COINS as LONG_COINS
from bot.long_v1_live_scanner.parity import JUN_FROM, JUN_JUL_TO
from bot.long_v1_live_scanner.processing import SymbolProcessor as LongProc
from bot.shadow_signal_registry.short_product import consolidate_short_product

SHORT_COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
LONG_STATUSES = ("ALLOWED", "LADDER_BLOCKED")


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return _utc(v)
    return _utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))


def _sig_key(side: str, row: dict) -> tuple:
    return (
        side,
        row["symbol"],
        _parse_dt(row["decision_time"]).isoformat(),
        row.get("pool_id") or "",
    )


def _norm_product(row: dict) -> dict:
    return {
        "symbol": row["symbol"],
        "decision_time": _parse_dt(row["decision_time"]).isoformat(),
        "pool_id": row.get("pool_id") or "",
        "status": row.get("signal_status") or ("ALLOWED" if row.get("allowed") else "BLOCKED"),
        "block_reason": row.get("block_reason") or row.get("raw_block_reason") or "",
        "entry": float(row.get("entry_price") or row.get("entry") or 0),
        "sl": float(row.get("stop") or row.get("initial_sl") or 0),
        "tp": float(row.get("tp") or 0),
    }


def _long_from_log(row: dict) -> dict | None:
    if row.get("event") != "signal":
        return None
    st = row.get("signal_status")
    if st not in LONG_STATUSES:
        return None
    return _norm_product(
        {
            "symbol": row["symbol"],
            "decision_time": row["decision_time"],
            "pool_id": row.get("pool_id"),
            "signal_status": st,
            "block_reason": "LADDER24" if st == "LADDER_BLOCKED" else "",
            "entry_price": row.get("entry_price"),
            "stop": row.get("stop"),
            "tp": row.get("tp"),
        }
    )


def _short_products_from_rows(rows: list[dict]) -> list[dict]:
    by_pool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        pid = r.get("pool_id")
        if pid:
            by_pool[pid].append(r)
    out: list[dict] = []
    for grp in by_pool.values():
        p = consolidate_short_product(grp)
        if p:
            out.append(_norm_product(p))
    return out


def short_full_cfg() -> ShortCfg:
    return ShortCfg(
        pane_from=SHORT_PANE_FROM,
        pane_to=SHORT_PANE_TO,
        e1r_sim_from=E1R_SIM_FROM_DEFAULT,
        report_from=JUN_FROM,
        report_to=JUN_JUL_TO,
        live=False,
    )


def short_causal_cfg() -> ShortCfg:
    return ShortCfg(
        pane_from=SHORT_PANE_FROM,
        pane_to=SHORT_PANE_TO,
        e1r_sim_from=E1R_SIM_FROM_DEFAULT,
        report_from=JUN_FROM,
        report_to=JUN_JUL_TO,
        live=True,
    )


def collect_short_full(symbol: str) -> dict[tuple, dict]:
    proc = ShortProc(short_full_cfg())
    st, logs = proc.replay_through(symbol, JUN_JUL_TO, emit_signals=True)
    if st.readiness == Readiness.DATA_MISSING:
        raise RuntimeError(st.data_missing_reason or "data_missing")
    by_decision: dict[str, list[dict]] = defaultdict(list)
    for r in logs:
        if r.get("variant") not in ("with_guard", "floor_blocked"):
            continue
        dt = _parse_dt(r["decision_time"])
        if dt < JUN_FROM or dt > JUN_JUL_TO:
            continue
        by_decision[dt.isoformat()].append(r)
    out: dict[tuple, dict] = {}
    for _dt, rows in by_decision.items():
        for p in _short_products_from_rows(rows):
            key = _sig_key("short", p)
            out[key] = p
    return out


def collect_short_causal_at(symbol: str, decision: datetime) -> list[dict]:
    T = _utc(decision)
    proc = ShortProc(short_causal_cfg())
    st = proc.load_state(symbol, T)
    if st.readiness in (Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
        return []
    rows_at_t: list[dict] = []
    for bar in st.bars15:
        ct = _utc(bar["close_time"])
        if ct > T:
            break
        if ct < E1R_SIM_FROM_DEFAULT:
            proc._process_one_bar(st, bar, emit_signals=False)
            continue
        emit = ct >= JUN_FROM
        rows = proc._process_one_bar(st, bar, emit_signals=emit)
        if ct == T:
            rows_at_t = [r for r in rows if r.get("variant") in ("with_guard", "floor_blocked")]
    return _short_products_from_rows(rows_at_t)


def long_full_cfg() -> LongCfg:
    return LongCfg(live=False, report_from=JUN_FROM, report_to=JUN_JUL_TO)


def long_causal_cfg() -> LongCfg:
    return LongCfg(live=True, report_from=JUN_FROM, report_to=JUN_JUL_TO)


def collect_long_full(symbol: str) -> dict[tuple, dict]:
    proc = LongProc(long_full_cfg())
    st, logs = proc.replay_through(
        symbol, JUN_JUL_TO, emit_signals=True, report_from=JUN_FROM, report_to=JUN_JUL_TO
    )
    if st.readiness == Readiness.DATA_MISSING:
        raise RuntimeError(st.data_missing_reason or "data_missing")
    out: dict[tuple, dict] = {}
    for r in logs:
        p = _long_from_log(r)
        if not p:
            continue
        dt = _parse_dt(p["decision_time"])
        if dt < JUN_FROM or dt > JUN_JUL_TO:
            continue
        out[_sig_key("long", p)] = p
    return out


def collect_long_causal_at(symbol: str, decision: datetime) -> list[dict]:
    T = _utc(decision)
    proc = LongProc(long_causal_cfg())
    st = proc.load_state(symbol, T)
    if st.readiness in (Readiness.DATA_MISSING, Readiness.WAITING_FOR_DATA):
        return []
    found: list[dict] = []
    for bar in st.bars15:
        ct = _utc(bar["close_time"])
        if ct > T:
            break
        emit = ct >= JUN_FROM
        rows = proc._process_one_15m(
            st, bar, emit_signals=emit, log_ts=ct, report_from=JUN_FROM, report_to=JUN_JUL_TO
        )
        if ct == T:
            for r in rows:
                p = _long_from_log(r)
                if p:
                    found.append(p)
    return found


def _match(a: dict, b: dict) -> bool:
    for k in ("decision_time", "pool_id", "status", "block_reason", "entry", "sl", "tp"):
        if k in ("entry", "sl", "tp"):
            if abs(float(a[k]) - float(b[k])) > 1e-8:
                return False
        elif str(a.get(k, "")) != str(b.get(k, "")):
            return False
    return True


def classify(full: dict, causal_map: dict[tuple, dict]) -> tuple[dict[str, int], list[dict]]:
    counts = {
        "EXACT_MATCH": 0,
        "SIGNAL_MISSING_TRUNCATED": 0,
        "SIGNAL_ONLY_TRUNCATED": 0,
        "SIGNAL_CHANGED": 0,
    }
    divergences: list[dict] = []
    for key, frow in full.items():
        crow = causal_map.get(key)
        if crow is None:
            # try match same symbol+time different pool
            dt = frow["decision_time"]
            sym = frow["symbol"]
            alts = [v for k, v in causal_map.items() if k[1] == sym and k[2] == dt]
            if not alts:
                counts["SIGNAL_MISSING_TRUNCATED"] += 1
                divergences.append({"class": "SIGNAL_MISSING_TRUNCATED", "full": frow, "causal": None})
            else:
                counts["SIGNAL_CHANGED"] += 1
                divergences.append(
                    {
                        "class": "SIGNAL_CHANGED",
                        "full": frow,
                        "causal": alts[0],
                        "note": "pool or fields differ",
                    }
                )
        elif _match(frow, crow):
            counts["EXACT_MATCH"] += 1
        else:
            counts["SIGNAL_CHANGED"] += 1
            divergences.append({"class": "SIGNAL_CHANGED", "full": frow, "causal": crow})
    for key, crow in causal_map.items():
        if key not in full:
            counts["SIGNAL_ONLY_TRUNCATED"] += 1
            divergences.append({"class": "SIGNAL_ONLY_TRUNCATED", "full": None, "causal": crow})
    return counts, divergences


def run_side(
    side: str,
    symbols: tuple[str, ...],
    full_fn,
    causal_fn,
) -> dict[str, Any]:
    full_all: dict[tuple, dict] = {}
    causal_all: dict[tuple, dict] = {}
    for sym in symbols:
        print(f"[{side}] full replay {sym}...", flush=True)
        full_all.update(full_fn(sym))
        keys = [k for k in full_all if k[0] == side and k[1] == sym]
        print(f"[{side}] causal {sym} n={len(keys)}...", flush=True)
        for i, key in enumerate(keys):
            frow = full_all[key]
            dt = _parse_dt(frow["decision_time"])
            for p in causal_fn(sym, dt):
                causal_all[_sig_key(side, p)] = p
            if (i + 1) % 10 == 0:
                print(f"  {sym} {i+1}/{len(keys)}", flush=True)
    counts, divergences = classify(full_all, causal_all)
    total = len(full_all)
    rate = counts["EXACT_MATCH"] / total if total else 0.0
    return {
        "counts": counts,
        "total_full": total,
        "causal_exact_rate": rate,
        "divergences": divergences,
    }


def main() -> None:
    short_res = run_side("short", SHORT_COINS, collect_short_full, collect_short_causal_at)
    long_res = run_side("long", LONG_COINS, collect_long_full, collect_long_causal_at)
    report = {"short": short_res, "long": long_res, "window": {"from": JUN_FROM.isoformat(), "to": JUN_JUL_TO.isoformat()}}
    out_path = REPO / "results" / "audit_backtest_signal_causality_jun_jul.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("WROTE", out_path)
    print("SHORT", short_res["counts"], "rate", short_res["causal_exact_rate"])
    print("LONG", long_res["counts"], "rate", long_res["causal_exact_rate"])


if __name__ == "__main__":
    main()
