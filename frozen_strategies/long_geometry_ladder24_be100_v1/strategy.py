"""Run frozen long_geometry_ladder24_be100_v1 signal pipeline."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from . import config as C
from .geometry import entry_setup_long, row_for_signal
from .ladder import m15_lower_2_age_h, passes_ladder_filter
from .outcome import baseline_path_15m, simulate_be_management, slice_1m_after_entry

REPO = Path(__file__).resolve().parents[2]


def _ensure_repo_paths() -> None:
    root = str(REPO)
    short_root = str(REPO / "results" / "pool_scan" / "context_study_v1")
    long_root = str(REPO / "results" / "pool_scan" / "context_study_v1_long")
    for p in (root, short_root, long_root):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _load_1m(symbol: str) -> list[dict] | None:
    from datetime import timezone

    from dashboard.research_charts.service import _candles_from_packed, load_candles
    from pool_scan.clock import bar_close

    packed = load_candles(
        symbol,
        "1m",
        start=int(C.BE_1M_LOAD_START.timestamp()),
        end=int(C.BE_1M_LOAD_END.timestamp()),
        limit=500_000,
    )
    candles = _candles_from_packed(packed)
    if not candles:
        return None
    bars = []
    for idx, candle in enumerate(candles):
        ot = candle.timestamp
        if ot.tzinfo is None:
            ot = ot.replace(tzinfo=timezone.utc)
        bars.append(
            {
                "open_time": ot,
                "close_time": bar_close(ot, "1m"),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "candle_index": idx,
            }
        )
    return bars


def run_period(period_key: str) -> tuple[list[dict], dict]:
    _ensure_repo_paths()
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, scanner_pools_for_index, ui_lld_config
    from find_short_entry_15m_v1 import build_15m_bars

    report_from, report_to = C.PERIODS[period_key]
    lld_cfg = ui_lld_config("15m")
    trades: list[dict] = []
    audit: list[dict] = []

    for symbol in C.SYMBOLS:
        _packed, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(C.PANE_FROM.timestamp()),
            to_unix=int(C.PANE_TO.timestamp()),
            history_weeks=C.HISTORY_WEEKS,
        )
        bars15 = build_15m_bars(candles)
        by_open = {b["open_time"]: b for b in bars15}
        cache: dict = {}
        seen: set[str] = set()
        bars1m = _load_1m(symbol)

        for bar in bars15:
            moment = bar["close_time"]
            if moment < report_from or moment > report_to:
                continue
            idx = bar["candle_index"]
            pools, _ui, proof = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
            setup = entry_setup_long(pools, bar, moment)
            if setup is None:
                continue
            pool, bridge_lower, upper, gap = setup
            if pool["pool_id"] in seen:
                continue
            seen.add(pool["pool_id"])
            source_t = pool.get("source")
            birth = by_open.get(source_t, bar) if source_t is not None else bar
            sig = row_for_signal(symbol, pool, birth, bar, bridge_lower, upper, gap)
            age_h = m15_lower_2_age_h(pools, moment, sig["entry_price"])
            if not passes_ladder_filter(age_h):
                continue

            base = baseline_path_15m(bars15, idx, sig["entry_price"], sig["stop"], sig["tp"])
            sim_bars = bars15
            sim_idx = idx
            bar_min = 15
            if bars1m:
                slice_bars, sidx = slice_1m_after_entry(bars1m, bar["close_time"])
                if slice_bars:
                    sim_bars = slice_bars
                    sim_idx = sidx
                    bar_min = 1
            managed = simulate_be_management(
                sim_bars, sim_idx, sig["entry_price"], sig["stop"], sig["tp"], bar_minutes=bar_min
            )

            trade = {
                **sig,
                "decision_time": moment.isoformat(),
                C.LADDER_FEATURE: age_h,
                "mae_pct": base["mae_pct"],
                "mfe_pct": base["mfe_pct"],
                "baseline_first_hit": base["first_hit"],
                "baseline_pnl_pct": base["pnl_pct"],
                "managed_outcome": managed.outcome,
                "managed_pnl_pct": managed.pnl_pct,
                "intrabar_ambiguous": managed.intrabar_ambiguous,
                "be_trigger_fired": managed.trigger_fired,
                "management_tf": managed.resolution_tf,
            }
            trades.append(trade)
            audit.append(
                {
                    "symbol": symbol,
                    "entry_time": sig["entry_time"],
                    "pool_known_ok": pool["known"] <= moment,
                    "pool_break_ok": pool.get("break_at") is None or pool["break_at"] > moment,
                    "scanner_tip_ok": proof.decision_time == moment,
                    "ladder_causal": age_h is not None,
                }
            )

    trades.sort(key=lambda t: (t["entry_time"], t["symbol"]))
    causality_pass = all(
        a["pool_known_ok"] and a["pool_break_ok"] and a["scanner_tip_ok"] and a["ladder_causal"] for a in audit
    )
    return trades, {"causality_pass": causality_pass, "audit_samples": len(audit)}
