"""BT-D82 — document and reuse frozen D46/D7 short exit semantics (research-only)."""

from __future__ import annotations

from typing import Any

# Bar timestamps are 5m **decision close** keys (UTC). OHLC is the closed bucket for that key.
# Short stop: adverse move up → high >= sl_price (BT-D46 integrated_backtester_v1._try_exit_on_bar).
# Short take-profit: favorable move down → low <= tp_price.
# Same-bar both touched: config pnl.intrabar_ambiguity_policy (isolated strategy: no_trade_ambiguous).

EXIT_CONTRACT_SOURCE = "phase_bt_d7_config_driven_short_pnl_integration_v1/execution_v1.py:simulate_short_exit"
D46_PER_BAR_SOURCE = "phase_bt_d46_integrated_short_backtester_v1/integrated_backtester_v1.py:_try_exit_on_bar"
ISOLATED_RUNNER_GAP = (
    "isolated_chronological_runner_v1._try_exit_sl records SL only (high >= sl); "
    "does not evaluate TP or intrabar_ambiguity_policy."
)


def intrabar_exit_decision(
    *,
    ohlc: dict[str, float],
    sl_price: float,
    tp_price: float,
    policy: str,
) -> tuple[str | None, float | None, bool]:
    """Mirror D46 per-bar exit resolution for one closed 5m bar."""
    h, l = float(ohlc["h"]), float(ohlc["l"])
    hit_sl = h >= sl_price
    hit_tp = l <= tp_price
    if hit_sl and hit_tp:
        if policy == "no_trade_ambiguous":
            return "AMBIGUOUS_INTRABAR", None, True
        return "SL", sl_price, True
    if hit_sl:
        return "SL", sl_price, False
    if hit_tp:
        return "TP", tp_price, False
    return None, None, False


def research_exit_from_fill(
    config: dict[str, Any],
    *,
    entry_bar_utc: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    prices: dict[str, dict[str, float]],
    end_utc: str,
) -> dict[str, Any]:
    """Deterministic exit path via frozen simulate_short_exit (includes fill bar)."""
    import sys
    from pathlib import Path

    bt_d7 = Path(__file__).resolve().parents[1] / "phase_bt_d7_config_driven_short_pnl_integration_v1"
    if str(bt_d7) not in sys.path:
        sys.path.insert(0, str(bt_d7))
    from execution_v1 import simulate_short_exit  # noqa: E402

    ex_t, ex_p, ex_reason, amb, bars_held = simulate_short_exit(
        config,
        entry_utc=entry_bar_utc,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        prices=prices,
        end_utc=end_utc,
    )
    gross_return_pct = None
    if ex_p is not None and entry_price:
        gross_return_pct = round((entry_price - ex_p) / entry_price * 100.0, 6)
    return {
        "entry_bar_utc": entry_bar_utc,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "exit_bar_utc": ex_t,
        "exit_price": ex_p,
        "exit_reason": ex_reason,
        "ambiguous_intrabar": amb,
        "bars_held": bars_held,
        "gross_return_pct_short": gross_return_pct,
        "fees_slippage": "none_configured",
        "contract": EXIT_CONTRACT_SOURCE,
    }


def ohlc_touch_audit(
    prices: dict[str, dict[str, float]],
    *,
    from_bar: str,
    to_bar: str,
    sl_price: float,
    tp_price: float,
) -> dict[str, Any]:
    """Evidence-only OHLC touches (not execution ordering)."""
    sl_bars = []
    tp_bars = []
    both_bars = []
    times = sorted(t for t in prices if from_bar <= t <= to_bar)
    for t in times:
        h, l = float(prices[t]["h"]), float(prices[t]["l"])
        hs, ht = h >= sl_price, l <= tp_price
        if hs and ht:
            both_bars.append(t)
        elif hs:
            sl_bars.append(t)
        elif ht:
            tp_bars.append(t)
    return {
        "from_bar": from_bar,
        "to_bar": to_bar,
        "first_sl_only_touch_bar": sl_bars[0] if sl_bars else None,
        "first_tp_only_touch_bar": tp_bars[0] if tp_bars else None,
        "bars_with_both_touched": both_bars,
        "note": "OHLC cannot prove intrabar order when both touched; use intrabar_ambiguity_policy.",
    }
