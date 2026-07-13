"""Deepest adverse extreme + later recovery for Momentum-confirmed signals.

Research-only. No swing engine, no hedge simulation.
Reference = Momentum confirmation close. Forward window = 96×5m candles.

Rule: the favorable extreme must occur on a candle **strictly after** the candle
that prints the maximum adverse extreme. No intrabar order is invented when both
occur on the same 5m bar.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .data_loader import load_symbol_candles
from .momentum_forward_audit import (
    COHORT_MOMENTUM_CONFIRMED,
    _candle_maps,
    _ts_str,
    build_signal_rows,
    load_pipeline_artifacts,
    ohlc_valid,
)
from .point_audit import json_safe
from .signal_tp_audit import prepare_candle_window

FORWARD_CANDLES = 96
LEVELS_VS_SIGNAL = (-1.00, -0.75, -0.50, -0.25, 0.00, 0.25, 0.50)


def _pct(from_price: float, to_price: float) -> float:
    if from_price == 0.0:
        raise ValueError("from_price must be non-zero")
    return (to_price - from_price) / abs(from_price) * 100.0


def directional_from_signal(*, side: str, signal_price: float, price: float) -> float:
    """Positive = with signal, negative = against signal."""
    raw = _pct(signal_price, price)
    return raw if side == "long" else -raw


def adverse_extreme_price(*, side: str, high: float, low: float) -> float:
    return low if side == "long" else high


def favorable_extreme_price(*, side: str, high: float, low: float) -> float:
    return high if side == "long" else low


def is_more_adverse(*, side: str, candidate: float, current: float) -> bool:
    if side == "long":
        return candidate < current
    return candidate > current


def is_more_favorable(*, side: str, candidate: float, current: float) -> bool:
    if side == "long":
        return candidate > current
    return candidate < current


def compute_deepest_drop_recovery(
    *,
    side: str,
    signal_price: float,
    future_candles: list[dict[str, Any]],
    horizon: int = FORWARD_CANDLES,
) -> dict[str, Any]:
    """Find max adverse extreme, then max favorable extreme on later candles only."""
    if len(future_candles) < horizon:
        return {
            "evaluable": False,
            "reason": "INSUFFICIENT_FUTURE_CANDLES",
            "available_future_candles": len(future_candles),
        }
    window = future_candles[:horizon]
    if any(not ohlc_valid(c) for c in window):
        return {
            "evaluable": False,
            "reason": "INVALID_OHLC",
            "available_future_candles": len(future_candles),
        }

    # Pass 1: deepest adverse extreme (first age on ties — earliest)
    adv_price = adverse_extreme_price(
        side=side, high=float(window[0]["high"]), low=float(window[0]["low"])
    )
    adv_age = 0
    for age, c in enumerate(window):
        cand = adverse_extreme_price(
            side=side, high=float(c["high"]), low=float(c["low"])
        )
        if is_more_adverse(side=side, candidate=cand, current=adv_price):
            adv_price = cand
            adv_age = age

    adverse_vs_signal = directional_from_signal(
        side=side, signal_price=signal_price, price=adv_price
    )
    # adverse_vs_signal is negative when against; report magnitude as positive drop
    max_adverse_drop_pct = -adverse_vs_signal

    if adv_age >= horizon - 1:
        levels = _level_flags(None)
        return {
            "evaluable": True,
            "reason": None,
            "adverse_extreme_price": adv_price,
            "adverse_extreme_age": adv_age,
            "max_adverse_drop_pct": max_adverse_drop_pct,
            "adverse_vs_signal_pct": adverse_vs_signal,
            "later_favorable_price": None,
            "later_favorable_age": None,
            "recovery_from_adverse_pct": None,
            "later_favorable_vs_signal_pct": None,
            "no_future_recovery_data": True,
            "returned_to_signal": False,
            "reached_plus_025": False,
            **levels,
        }

    # Pass 2: best favorable extreme strictly after adv_age
    later = window[adv_age + 1 :]
    fav_price = favorable_extreme_price(
        side=side, high=float(later[0]["high"]), low=float(later[0]["low"])
    )
    fav_age = adv_age + 1
    for offset, c in enumerate(later):
        cand = favorable_extreme_price(
            side=side, high=float(c["high"]), low=float(c["low"])
        )
        age = adv_age + 1 + offset
        if is_more_favorable(side=side, candidate=cand, current=fav_price):
            fav_price = cand
            fav_age = age

    # Recovery from adverse extreme in signal-favorable direction.
    if side == "long":
        recovery_from_adverse_pct = _pct(adv_price, fav_price)
    else:
        recovery_from_adverse_pct = (adv_price - fav_price) / abs(adv_price) * 100.0

    later_vs_signal = directional_from_signal(
        side=side, signal_price=signal_price, price=fav_price
    )
    levels = _level_flags(later_vs_signal)

    return {
        "evaluable": True,
        "reason": None,
        "adverse_extreme_price": adv_price,
        "adverse_extreme_age": adv_age,
        "max_adverse_drop_pct": max_adverse_drop_pct,
        "adverse_vs_signal_pct": adverse_vs_signal,
        "later_favorable_price": fav_price,
        "later_favorable_age": fav_age,
        "recovery_from_adverse_pct": recovery_from_adverse_pct,
        "later_favorable_vs_signal_pct": later_vs_signal,
        "no_future_recovery_data": False,
        "returned_to_signal": later_vs_signal + 1e-15 >= 0.0,
        "reached_plus_025": later_vs_signal + 1e-15 >= 0.25,
        **levels,
    }


def _level_flags(later_vs_signal: float | None) -> dict[str, bool]:
    """Flags for later favorable level vs signal (directional)."""
    def hit(threshold: float) -> bool:
        if later_vs_signal is None:
            return False
        return float(later_vs_signal) + 1e-15 >= threshold

    # "weiterhin unter −1,00 %" means later level still < -1.00
    still_below_m100 = (
        later_vs_signal is not None and float(later_vs_signal) < -1.00 - 1e-15
    )
    return {
        "still_below_minus_1_00": still_below_m100,
        "reached_at_least_minus_0_75": hit(-0.75),
        "reached_at_least_minus_0_50": hit(-0.50),
        "reached_at_least_minus_0_25": hit(-0.25),
        "reached_at_least_0_00": hit(0.00),
        "reached_at_least_plus_0_25": hit(0.25),
        "reached_at_least_plus_0_50": hit(0.50),
    }


def analyze_signal(
    signal: dict[str, Any],
    *,
    candles: list[dict[str, Any]],
    ts_to_i: dict[str, int],
    horizon: int = FORWARD_CANDLES,
) -> dict[str, Any]:
    base = {
        "setup_id": signal.get("setup_id"),
        "side": signal.get("side"),
        "pattern_type": signal.get("pattern_type"),
        "momentum_confidence": signal.get("momentum_confidence"),
        "confirmation_age": signal.get("confirmation_age"),
        "signal_timestamp": signal.get("momentum_confirmation_timestamp"),
    }
    ts = signal.get("momentum_confirmation_timestamp")
    if not ts:
        return {**base, "evaluable": False, "reason": "MISSING_TIMESTAMP"}
    key = _ts_str(ts)
    if key not in ts_to_i:
        return {**base, "evaluable": False, "reason": "NOT_IN_FRAME"}
    i0 = ts_to_i[key]
    measure = candles[i0]
    if not ohlc_valid(measure):
        return {**base, "evaluable": False, "reason": "INVALID_OHLC"}
    signal_price = float(measure["close"])
    future = candles[i0 + 1 :]
    metrics = compute_deepest_drop_recovery(
        side=str(signal["side"]),
        signal_price=signal_price,
        future_candles=future,
        horizon=horizon,
    )
    return {**base, "signal_price": signal_price, **metrics}


def run_deepest_drop_recovery_audit(
    *,
    price_action_confirmations: list[dict[str, Any]],
    momentum_confirmations: list[dict[str, Any]],
    momentum_events: list[dict[str, Any]],
    candles: pd.DataFrame,
    horizon: int = FORWARD_CANDLES,
) -> dict[str, Any]:
    signals = [
        s
        for s in build_signal_rows(
            price_action_confirmations=price_action_confirmations,
            momentum_confirmations=momentum_confirmations,
            momentum_events=momentum_events,
        )
        if s.get("cohort") == COHORT_MOMENTUM_CONFIRMED
    ]
    _, ts_to_i, candle_rows = _candle_maps(candles)
    rows = [
        analyze_signal(s, candles=candle_rows, ts_to_i=ts_to_i, horizon=horizon)
        for s in signals
    ]
    summary = build_summary(rows)
    return {"signals": signals, "rows": rows, "summary": summary}


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("evaluable")]
    drops = [float(r["max_adverse_drop_pct"]) for r in ok]
    with_rec = [r for r in ok if not r.get("no_future_recovery_data")]
    returned = [r for r in with_rec if r.get("returned_to_signal")]
    plus025 = [r for r in with_rec if r.get("reached_plus_025")]

    # User asked: how many only came back to -0.25, -0.50, or deeper
    band_m025 = []  # reached >= -0.25 but not 0
    band_m050 = []  # reached >= -0.50 but not -0.25
    band_deeper = []  # later level < -0.50
    for r in with_rec:
        lv = r.get("later_favorable_vs_signal_pct")
        if lv is None:
            continue
        x = float(lv)
        if x >= 0.0:
            continue
        if x >= -0.25:
            band_m025.append(r)
        elif x >= -0.50:
            band_m050.append(r)
        else:
            band_deeper.append(r)

    weak = [
        r
        for r in with_rec
        if (r.get("later_favorable_vs_signal_pct") is None)
        or float(r.get("later_favorable_vs_signal_pct") or -999) < 0.0
    ]

    def _side(side: str) -> dict[str, Any]:
        sub = [r for r in ok if r.get("side") == side]
        w = [r for r in sub if not r.get("no_future_recovery_data")]
        return {
            "n": len(sub),
            "median_drop": _median(
                [float(r["max_adverse_drop_pct"]) for r in sub]
            ),
            "max_drop": max((float(r["max_adverse_drop_pct"]) for r in sub), default=None),
            "returned_to_signal": sum(1 for r in w if r.get("returned_to_signal")),
            "reached_plus_025": sum(1 for r in w if r.get("reached_plus_025")),
        }

    return {
        "n_signals": len(rows),
        "n_evaluable": len(ok),
        "max_adverse_drop_pct": max(drops) if drops else None,
        "median_adverse_drop_pct": _median(drops),
        "n_returned_to_signal": len(returned),
        "n_reached_plus_025": len(plus025),
        "n_later_only_to_minus_025_band": len(band_m025),
        "n_later_only_to_minus_050_band": len(band_m050),
        "n_later_still_deeper_than_minus_050": len(band_deeper),
        "weak_recovery_setup_ids": [r.get("setup_id") for r in weak],
        "n_no_future_recovery_data": sum(1 for r in ok if r.get("no_future_recovery_data")),
        "by_side": {"long": _side("long"), "short": _side("short")},
    }


def format_readme(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "# Deepest Drop → Later Recovery (March week)",
        "",
        "Reference = Momentum confirmation **close**. Window = 96×5m. "
        "Favorable extreme counted only on candles **after** the adverse-extreme candle.",
        "",
        f"Evaluable: **{summary.get('n_evaluable')}** / {summary.get('n_signals')}",
        "",
        "| Setup | Richtung | tiefster Rückgang | spätere Erholung vom Tief | späteres Level vs Signal | zurück auf Signal? | später +0,25 %? |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for r in rows:
        if not r.get("evaluable"):
            lines.append(
                f"| {r.get('setup_id')} | {r.get('side')} | — | — | — | — | — |"
            )
            continue
        if r.get("no_future_recovery_data"):
            rec = "no_future_recovery_data"
            lvl = "—"
            back = "—"
            p25 = "—"
        else:
            rec = _fmt(r.get("recovery_from_adverse_pct"))
            lvl = _fmt(r.get("later_favorable_vs_signal_pct"))
            back = "yes" if r.get("returned_to_signal") else "no"
            p25 = "yes" if r.get("reached_plus_025") else "no"
        lines.append(
            "| {id} | {side} | {drop} | {rec} | {lvl} | {back} | {p25} |".format(
                id=r.get("setup_id"),
                side=r.get("side"),
                drop=_fmt(r.get("max_adverse_drop_pct")),
                rec=rec,
                lvl=lvl,
                back=back,
                p25=p25,
            )
        )
    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Max drop: **{_fmt(summary.get('max_adverse_drop_pct'))}%**",
            f"- Median drop: **{_fmt(summary.get('median_adverse_drop_pct'))}%**",
            f"- Returned to signal: **{summary.get('n_returned_to_signal')}**",
            f"- Later ≥ +0.25%: **{summary.get('n_reached_plus_025')}**",
            f"- Later only in [−0.25, 0): **{summary.get('n_later_only_to_minus_025_band')}**",
            f"- Later only in [−0.50, −0.25): **{summary.get('n_later_only_to_minus_050_band')}**",
            f"- Later still < −0.50%: **{summary.get('n_later_still_deeper_than_minus_050')}**",
            f"- Weak (never back to signal): `{summary.get('weak_recovery_setup_ids')}`",
            f"- By side: `{summary.get('by_side')}`",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(v: object) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return str(v)


def write_outputs(payload: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": out / "signal_deepest_drop_recovery.csv",
        "summary": out / "summary.json",
        "readme": out / "README.md",
    }
    pd.DataFrame(json_safe(payload["rows"])).to_csv(paths["csv"], index=False)
    paths["summary"].write_text(
        json.dumps(json_safe(payload["summary"]), indent=2), encoding="utf-8"
    )
    paths["readme"].write_text(
        format_readme(payload["rows"], payload["summary"]), encoding="utf-8"
    )
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deepest-drop + later recovery audit.")
    p.add_argument(
        "--pipeline-dir",
        default=(
            "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
        ),
    )
    p.add_argument(
        "--output-dir",
        default=(
            "research/backtests/results/regime_scanner_momentum_deepest_drop_recovery_march_week1"
        ),
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-12")  # buffer for 96 forward candles
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    arts = load_pipeline_artifacts(args.pipeline_dir)
    raw = load_symbol_candles(args.symbol)
    prepared = prepare_candle_window(
        raw,
        start=args.start,
        end=args.end,
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    payload = run_deepest_drop_recovery_audit(
        price_action_confirmations=arts["price_action_confirmations"],
        momentum_confirmations=arts["momentum_confirmations"],
        momentum_events=arts["momentum_events"],
        candles=prepared["candles"],
    )
    paths = write_outputs(payload, args.output_dir)
    s = payload["summary"]
    print(
        f"Deepest-drop audit: n={s.get('n_evaluable')} "
        f"max_drop={s.get('max_adverse_drop_pct')} "
        f"median={s.get('median_adverse_drop_pct')} "
        f"back={s.get('n_returned_to_signal')} plus025={s.get('n_reached_plus_025')}"
    )
    for p in paths.values():
        print(f"Wrote: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
