"""Small July 2026 E2E smoke — PD10 guard off/on (read-only)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .july_catalog import count_closes_in_month, validate_july_catalog
from .provider import HistoricalLmV2PoolStore
from .strategy_loop import enrich_trades, run_baseline_walk
from .strategy_paths import ensure_strategy_paths
from .variants import BaselineVariant

UTC = timezone.utc
SYMBOL = "DOGEUSDT"
PANE_FROM = datetime(2026, 2, 10, tzinfo=UTC)
PANE_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC)
REPLAY_FROM = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
REPORT_FROM = datetime(2026, 7, 7, 0, 0, tzinfo=UTC)
REPORT_TO = datetime(2026, 7, 9, 23, 59, 59, tzinfo=UTC)


def _load_bars(symbol: str):
    ensure_strategy_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    market = load_market(symbol)
    _packed, candles15 = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(REPORT_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles15)
    return market, bars15


def _summarize_trades(trades: list[dict]) -> dict:
    sl = sum(1 for t in trades if t.get("first_hit") == "SL")
    tp = sum(1 for t in trades if t.get("first_hit") == "TP")
    horizon = sum(1 for t in trades if t.get("first_hit") == "none")
    pnl_sum = sum(float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None)
    peak = 0.0
    eq = 0.0
    max_dd = 0.0
    for t in trades:
        step = float(t["pnl_pct"]) if t.get("pnl_pct") is not None else 0.0
        eq += step
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return {
        "trades": len(trades),
        "sl_exits": sl,
        "tp_exits": tp,
        "horizon_exits": horizon,
        "pnl_sum_pct": round(pnl_sum, 4) if trades else None,
        "max_drawdown_pct": round(max_dd, 4) if trades else None,
    }


def run_smoke(production_root: Path) -> dict:
    sample = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    cat, july_report = validate_july_catalog(production_root, sample_close=sample)
    store = HistoricalLmV2PoolStore(cat)

    n15 = count_closes_in_month(production_root, "15m", 10, "2026-07")
    n4 = count_closes_in_month(production_root, "4h", 10, "2026-07")

    market, bars15 = _load_bars(SYMBOL)

    # Full pane warmup for EMA; guard/seen replay from July 1 only.
    state_warmup = (
        "PARTIAL_GUARD_WARMUP"
        if REPLAY_FROM > PANE_FROM
        else "PANE_FROM_ALIGNED"
    )
    if REPLAY_FROM > PANE_FROM:
        state_warmup = "STATE_WARMUP_NOT_PROVEN_FOR_PRE_JULY_GUARD_EPISODES"

    results = {}
    for guard_on in (False, True):
        variant = BaselineVariant(
            f"PD10_guard_{'on' if guard_on else 'off'}",
            pool_detail_15m=10,
            floor_guard_enabled=guard_on,
        )
        signals, _st, funnel = run_baseline_walk(
            SYMBOL,
            bars15,
            market,
            store,
            variant,
            pane_from=PANE_FROM,
            pane_to=PANE_TO,
            replay_from=REPLAY_FROM,
            report_from=REPORT_FROM,
            report_to=REPORT_TO,
        )
        trades = enrich_trades(bars15, signals, SYMBOL)
        results[variant.run_key] = {
            "funnel": asdict(funnel),
            "trade_summary": _summarize_trades(trades),
            "trades": trades,
        }

    return {
        "ENTRY_FILL_AT_15M_CLOSE": "RESEARCH_ASSUMPTION",
        "FEES_AND_SLIPPAGE": "NOT_INCLUDED",
        "STATE_WARMUP_VALIDATION": state_warmup,
        "EMA_SOURCE": "find_short_entry_15m_v1.load_market + build_15m_bars",
        "replay_from": REPLAY_FROM.isoformat(),
        "report_from": REPORT_FROM.isoformat(),
        "report_to": REPORT_TO.isoformat(),
        "july_catalog": asdict(july_report),
        "july_close_counts": {"15m_PD10": n15, "4h_PD10": n4},
        "frozen_input_id": cat.frozen_input_id,
        "variants": results,
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "live_micro_lld_v2_ch_test" / "doge_multi_month_2026_history"
    payload = run_smoke(root)
    out = Path(__file__).resolve().parent / "artifacts" / "july_smoke_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Trim trade list in stdout summary
    summary = json.loads(json.dumps(payload, default=str))
    for v in summary.get("variants", {}).values():
        v.pop("trades", None)
    print(json.dumps(summary, indent=2))
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
