"""Diagnose why Research-v1 misclassifies the March APTUSDT reference cycle.

Read-only forensics over existing trend-state modules. Does not change
thresholds, transitions, events, pipeline, or live code.

Outputs under research/backtests/results/regime_scanner_trend_state_misclassification_audit/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import filter_pivots_as_of, find_confirmed_pivots
from research.regime_scanner.trend_state_audit import DEFAULT_AUDIT_END, DEFAULT_AUDIT_START
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    default_trend_state_config,
    min_hold_for,
    step_trend_state,
)
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    _protective_high,
    _protective_low,
    has_hh_hl,
    has_lh_ll,
)

DEFAULT_OUT = (
    "research/backtests/results/regime_scanner_trend_state_misclassification_audit"
)
PRIOR_AUDIT = "research/backtests/results/regime_scanner_trend_state_audit_march_0608"
WARM_PAD_DAYS = 3

# Forensic focus times are *analysis anchors* for this diagnostic report only —
# they are not trading rules and are not imported by the state machine.
FOCUS_DECISION_TIMES = (
    "2026-03-05T18:00:00+00:00",
    "2026-03-05T22:30:00+00:00",
    "2026-03-06T00:30:00+00:00",
    "2026-03-06T01:35:00+00:00",
    "2026-03-06T08:00:00+00:00",
    "2026-03-06T14:45:00+00:00",
    "2026-03-06T16:00:00+00:00",
    "2026-03-07T03:05:00+00:00",
    "2026-03-07T12:10:00+00:00",
    "2026-03-07T15:30:00+00:00",
)


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _pivot_dict(p: Any) -> dict[str, Any] | None:
    if p is None:
        return None
    return p.to_dict() if hasattr(p, "to_dict") else dict(p)


def structure_forensics(state: MarketStructureState) -> dict[str, Any]:
    prot_low, prot_low_p = _protective_low(state)
    prot_high, prot_high_p = _protective_high(state)
    return {
        "bias": state.current_structure_bias,
        "last_high_label": state.last_high_label,
        "last_low_label": state.last_low_label,
        "has_lh_ll": has_lh_ll(state),
        "has_hh_hl": has_hh_hl(state),
        "last_confirmed_swing_high": _pivot_dict(state.last_confirmed_swing_high),
        "last_confirmed_swing_low": _pivot_dict(state.last_confirmed_swing_low),
        "previous_confirmed_swing_high": _pivot_dict(state.previous_confirmed_swing_high),
        "previous_confirmed_swing_low": _pivot_dict(state.previous_confirmed_swing_low),
        "last_higher_high": _pivot_dict(state.last_higher_high),
        "last_higher_low": _pivot_dict(state.last_higher_low),
        "last_lower_high": _pivot_dict(state.last_lower_high),
        "last_lower_low": _pivot_dict(state.last_lower_low),
        "protective_low": prot_low,
        "protective_low_pivot": _pivot_dict(prot_low_p),
        "protective_high": prot_high,
        "protective_high_pivot": _pivot_dict(prot_high_p),
        "active_break_level": state.active_break_level,
        "active_retest_direction": state.active_retest_direction,
        "last_bos": None if state.last_bos is None else state.last_bos.to_dict(),
        "last_choch": None if state.last_choch is None else state.last_choch.to_dict(),
        "last_failed_breakout": (
            None if state.last_failed_breakout is None else state.last_failed_breakout.to_dict()
        ),
        "last_failed_breakdown": (
            None
            if state.last_failed_breakdown is None
            else state.last_failed_breakdown.to_dict()
        ),
        "prior_close": state.prior_close,
        "recent_event_types": [e.event_type for e in state.recent_events[-12:]],
    }


def early_to_strong_gate_check(rt: TrendRuntime, row: dict[str, Any], cfg: Any) -> dict[str, Any]:
    """Explain why early_bearish did / did not promote to strong_bearish (read-only)."""
    from research.regime_scanner.trend_state_machine import _htf_bias, _htf_veto_strong_bullish, _indicator_confirms

    s5 = rt.structure_5m
    s15 = rt.structure_15m
    s30 = rt.structure_30m
    bear_conf, bear_codes = _indicator_confirms(row, side="bearish", cfg=cfg)
    checks = {
        "state": rt.state,
        "age_5m_bars": rt.age_5m_bars,
        "min_hold_early": min_hold_for("early_bearish", cfg),
        "hold_satisfied": rt.age_5m_bars >= min_hold_for("early_bearish", cfg),
        "has_lh_ll": has_lh_ll(s5),
        "bias_5m": s5.current_structure_bias,
        "bias_5m_is_bearish": s5.current_structure_bias == "bearish",
        "last_high_label": s5.last_high_label,
        "last_low_label": s5.last_low_label,
        "htf_15m_bias": _htf_bias(s15),
        "htf_15m_ok": _htf_bias(s15) in {"bearish", "neutral"},
        "htf_veto_strong_bullish": _htf_veto_strong_bullish(s15, s30),
        "bear_confirm_count": bear_conf,
        "bear_confirm_codes": bear_codes,
        "retest_holds_needed_or_conf2": "see machine: retest_holds OR bear_conf>=2",
        "would_need_for_strong": [
            "has_lh_ll",
            "bias_5m_bearish",
            "15m bearish|neutral OR bearish_bos event",
            "not 15m+30m strong bullish veto",
            "bearish_retest_holds OR bear_conf>=2",
            "min_hold early satisfied",
        ],
    }
    missing = []
    if not checks["hold_satisfied"]:
        missing.append("min_hold")
    if not checks["has_lh_ll"]:
        missing.append("lh_ll")
    if not checks["bias_5m_is_bearish"]:
        missing.append("bias_not_bearish")
    if not checks["htf_15m_ok"]:
        missing.append("15m_not_bearish_or_neutral")
    if checks["htf_veto_strong_bullish"]:
        missing.append("15m_30m_bullish_veto")
    if bear_conf < 2:
        missing.append("bear_conf<2_and_no_retest_checked_separately")
    checks["blocking_factors"] = missing
    return checks


def run_forensic_replay(
    *,
    symbol: str = "APTUSDT",
    audit_start: object = DEFAULT_AUDIT_START,
    audit_end: object = DEFAULT_AUDIT_END,
    out_dir: str | Path = DEFAULT_OUT,
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    start = _ts(audit_start)
    end = _ts(audit_end)
    warm_start = start - pd.Timedelta(days=WARM_PAD_DAYS)

    raw = load_symbol_candles(symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    slice_ = raw[(raw["timestamp"] >= warm_start) & (raw["timestamp"] < end)].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=scfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)

    pivots = find_confirmed_pivots(frame, config=scfg)
    cfg = default_trend_state_config()
    rt = TrendRuntime()

    focus = {_ts(t) for t in FOCUS_DECISION_TIMES}
    focus_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    early_bearish_bars: list[dict[str, Any]] = []
    prev_state = None

    for i, row in frame.iterrows():
        decision_ts = _ts(row["decision_time"])
        candles_as_of = frame.iloc[: int(i) + 1][
            [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
        ]
        prev = rt.state
        age_before = rt.age_5m_bars
        rt, snap, events = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=candles_as_of,
            bar_index=int(i),
            cfg=cfg,
            scanner_cfg=scfg,
        )

        in_window = start <= decision_ts <= end
        candle_pack = {
            "timestamp": str(row["timestamp"]),
            "decision_time": decision_ts.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }

        if in_window and prev != rt.state and prev is not None:
            transition_rows.append(
                {
                    "decision_time": decision_ts.isoformat(),
                    "from_state": prev,
                    "to_state": rt.state,
                    "age_before": age_before,
                    "reasons": list(snap.active_reasons),
                    "events_this_bar": [e.to_dict() for e in events],
                    "candle": candle_pack,
                    "structure_5m": structure_forensics(rt.structure_5m),
                    "structure_15m": structure_forensics(rt.structure_15m),
                    "structure_30m": structure_forensics(rt.structure_30m),
                    "early_to_strong_gate": early_to_strong_gate_check(rt, row.to_dict(), cfg),
                }
            )

        if in_window and (rt.state == "early_bearish" or prev == "early_bearish"):
            early_bearish_bars.append(
                {
                    "decision_time": decision_ts.isoformat(),
                    "state": rt.state,
                    "age": rt.age_5m_bars,
                    "reasons": list(snap.active_reasons),
                    "event_types": [e.event_type for e in events],
                    "candle": candle_pack,
                    "structure_5m_summary": rt.structure_5m.summary(),
                    "gate": early_to_strong_gate_check(rt, row.to_dict(), cfg),
                }
            )

        if decision_ts in focus:
            as_of_pivots = filter_pivots_as_of(pivots, decision_ts)
            focus_rows.append(
                {
                    "decision_time": decision_ts.isoformat(),
                    "state": rt.state,
                    "previous_state": snap.previous_state,
                    "age_5m_bars": rt.age_5m_bars,
                    "reasons": list(snap.active_reasons),
                    "candle": candle_pack,
                    "events_this_bar": [e.to_dict() for e in events],
                    "confirmed_pivots_as_of_count": len(as_of_pivots),
                    "last_two_highs": [
                        p.to_dict()
                        for p in [p for p in as_of_pivots if p.pivot_type == "high"][-2:]
                    ],
                    "last_two_lows": [
                        p.to_dict()
                        for p in [p for p in as_of_pivots if p.pivot_type == "low"][-2:]
                    ],
                    "structure_5m": structure_forensics(rt.structure_5m),
                    "structure_15m": structure_forensics(rt.structure_15m),
                    "structure_30m": structure_forensics(rt.structure_30m),
                    "scores": {
                        "bearish": snap.bearish_score,
                        "bullish": snap.bullish_score,
                        "weakening": snap.weakening_score,
                        "bottoming": snap.bottoming_score,
                    },
                    "policy": {
                        "allow_long": snap.allow_long,
                        "allow_short": snap.allow_short,
                    },
                    "early_to_strong_gate": early_to_strong_gate_check(rt, row.to_dict(), cfg),
                }
            )

        prev_state = rt.state

    # Causal price path on 6 March morning (analysis only)
    day = frame[
        (frame["decision_time"] >= _ts("2026-03-06T00:00:00+00:00"))
        & (frame["decision_time"] <= _ts("2026-03-06T18:00:00+00:00"))
    ][["decision_time", "open", "high", "low", "close"]].copy()

    # Map prior audit timeline states onto price for same window
    prior_tl_path = Path(PRIOR_AUDIT) / "trend_state_timeline.csv"
    state_on_day = []
    if prior_tl_path.exists():
        tl = pd.read_csv(prior_tl_path)
        tl["decision_time"] = pd.to_datetime(tl["decision_time"], utc=True)
        merged = day.merge(
            tl[["decision_time", "current_state", "active_reasons", "age_5m_bars"]],
            on="decision_time",
            how="left",
        )
        state_on_day = json_safe(merged.to_dict(orient="records"))

    # Root-cause synthesis from transition forensics
    synthesis = synthesize_root_causes(transition_rows, focus_rows, early_bearish_bars)

    report = {
        "symbol": symbol,
        "audit_start": start.isoformat(),
        "audit_end": end.isoformat(),
        "note": (
            "FOCUS_DECISION_TIMES are diagnostic anchors only; they are not trading rules "
            "and do not affect the state machine."
        ),
        "transitions": transition_rows,
        "focus_snapshots": focus_rows,
        "early_bearish_episode_bars": early_bearish_bars,
        "march6_price_with_states": state_on_day,
        "root_cause_synthesis": synthesis,
    }

    (out / "misclassification_forensics.json").write_text(
        json.dumps(json_safe(report), indent=2),
        encoding="utf-8",
    )
    (out / "transitions_forensics.json").write_text(
        json.dumps(json_safe(transition_rows), indent=2),
        encoding="utf-8",
    )
    (out / "focus_snapshots.json").write_text(
        json.dumps(json_safe(focus_rows), indent=2),
        encoding="utf-8",
    )
    write_markdown(synthesis, transition_rows, focus_rows, out)
    return report


def synthesize_root_causes(
    transitions: list[dict[str, Any]],
    focus: list[dict[str, Any]],
    early_bars: list[dict[str, Any]],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    # 1) Warmup / start state
    if transitions:
        t0 = transitions[0] if transitions[0].get("from_state") else None
    start_focus = next((f for f in focus if f["decision_time"].startswith("2026-03-05T18:00")), None)
    if start_focus and start_focus["state"] == "topping":
        findings.append(
            {
                "rank_candidate": 7,
                "id": "warmup_start_topping",
                "severity": True,
                "evidence": {
                    "state_at_audit_open": start_focus["state"],
                    "structure_5m_bias": start_focus["structure_5m"]["bias"],
                    "structure_5m_labels": {
                        "high": start_focus["structure_5m"]["last_high_label"],
                        "low": start_focus["structure_5m"]["last_low_label"],
                    },
                    "15m_bias": start_focus["structure_15m"]["bias"],
                    "30m_bias": start_focus["structure_30m"]["bias"],
                    "note": (
                        "Audit window opens mid-cycle in topping (from warmup replay). "
                        "Reference narrative expects approaching downtrend, not a topping "
                        "policy state that already blocks longs."
                    ),
                },
            }
        )

    # 2) early invalidation at 00:30
    inv = next(
        (
            t
            for t in transitions
            if t["from_state"] == "early_bearish" and t["to_state"] == "bearish_weakening"
            and t["decision_time"].startswith("2026-03-06T00:30")
        ),
        None,
    )
    if inv:
        s5 = inv["structure_5m"]
        findings.append(
            {
                "rank_candidate": 3,
                "id": "early_invalidation_too_permissive",
                "severity": True,
                "evidence": {
                    "decision_time": inv["decision_time"],
                    "reasons": inv["reasons"],
                    "events": inv["events_this_bar"],
                    "candle": inv["candle"],
                    "structure_after": {
                        "bias": s5["bias"],
                        "labels": (s5["last_high_label"], s5["last_low_label"]),
                        "has_hh_hl": s5["has_hh_hl"],
                        "has_lh_ll": s5["has_lh_ll"],
                    },
                    "rule": (
                        "early_bearish → bearish_weakening on "
                        "failed_breakdown | bearish_retest_fails | (bullish_choch & higher_low)"
                    ),
                    "note": (
                        "At invalidation, 5m bias is already bullish HH+HL — machine never "
                        "held a bearish LH+LL regime inside early_bearish long enough to "
                        "reach strong_bearish. Invalidation fires while the later selloff "
                        "has not started."
                    ),
                },
            }
        )

    # 3) bottoming at 01:35
    bot = next(
        (
            t
            for t in transitions
            if t["from_state"] == "bearish_weakening"
            and t["to_state"] == "bottoming"
            and t["decision_time"].startswith("2026-03-06T01:35")
        ),
        None,
    )
    if bot:
        findings.append(
            {
                "rank_candidate": 4,
                "id": "bottoming_too_permissive",
                "severity": True,
                "evidence": {
                    "decision_time": bot["decision_time"],
                    "reasons": bot["reasons"],
                    "events": bot["events_this_bar"],
                    "candle": bot["candle"],
                    "protective_low": bot["structure_5m"]["protective_low"],
                    "protective_high": bot["structure_5m"]["protective_high"],
                    "last_failed_breakdown": bot["structure_5m"]["last_failed_breakdown"],
                    "last_choch": bot["structure_5m"]["last_choch"],
                    "15m_bias_still": bot["structure_15m"]["bias"],
                    "rule": (
                        "bearish_weakening → bottoming when >=2 of "
                        "{failed_breakdown, bullish_choch, higher_low, bullish_bos}"
                    ),
                    "note": (
                        "Same-bar bullish_choch + failed_breakdown satisfy the 2-hit rule "
                        "hours before the main 06.03 selloff. 15m is already bearish at "
                        "bottoming entry — HTF does not veto bottoming."
                    ),
                },
            }
        )

    # 4) never strong_bearish — inspect early episode gates
    blockers: dict[str, int] = {}
    for bar in early_bars:
        if bar["state"] != "early_bearish":
            continue
        for b in bar["gate"].get("blocking_factors") or []:
            blockers[b] = blockers.get(b, 0) + 1
    findings.append(
        {
            "rank_candidate": 5,
            "id": "early_to_strong_never_cleared",
            "severity": True,
            "evidence": {
                "early_bearish_bar_count": sum(1 for b in early_bars if b["state"] == "early_bearish"),
                "blocking_factor_counts": blockers,
                "note": (
                    "During both early_bearish episodes, LH+LL + bearish bias rarely "
                    "coexist with hold + confirms. First episode ends via invalidation "
                    "while structure is HH+HL bullish; second episode (07.03) also "
                    "invalidates before strong."
                ),
            },
        }
    )

    # 5) protective level / choch semantics
    if bot:
        choch = bot["structure_5m"].get("last_choch") or {}
        findings.append(
            {
                "rank_candidate": 1,
                "id": "protective_level_is_last_micro_swing",
                "severity": True,
                "evidence": {
                    "protective_high_used_for_bullish_choch": bot["structure_5m"]["protective_high"],
                    "protective_high_pivot": bot["structure_5m"]["protective_high_pivot"],
                    "choch_event": choch,
                    "note": (
                        "Protective high/low = last_lower_high / last_higher_low or last "
                        "confirmed swing — i.e. the most recent micro-swing, not the "
                        "swing that defines the active trend leg. Micro CHoCH therefore "
                        "fires on noise levels during chop before the real breakdown."
                    ),
                },
            }
        )

    # 6) BOS vs CHoCH sticky labels
    findings.append(
        {
            "rank_candidate": 2,
            "id": "bos_choch_bias_coupled_to_last_pair_only",
            "severity": True,
            "evidence": {
                "note": (
                    "Bias = last high-label + last low-label only. One HH after a LH "
                    "flips bias toward bullish/neutral and remaps the next downside "
                    "break to bearish_choch instead of bearish_bos. last_bos/last_choch "
                    "remain sticky forever for audit display and can show bullish_bos "
                    "while price is falling."
                ),
            },
        }
    )

    # 7) stuck in bottoming during selloff
    selloff = next((f for f in focus if f["decision_time"].startswith("2026-03-06T14:45")), None)
    selloff2 = next((f for f in focus if f["decision_time"].startswith("2026-03-06T16:00")), None)
    findings.append(
        {
            "rank_candidate": 8,
            "id": "bottoming_sticky_through_selloff",
            "severity": True,
            "evidence": {
                "state_1445": None if selloff is None else selloff["state"],
                "state_1600": None if selloff2 is None else selloff2["state"],
                "structure_1445": None if selloff is None else {
                    "bias": selloff["structure_5m"]["bias"],
                    "labels": (
                        selloff["structure_5m"]["last_high_label"],
                        selloff["structure_5m"]["last_low_label"],
                    ),
                    "last_bos": selloff["structure_5m"]["last_bos"],
                    "last_choch": selloff["structure_5m"]["last_choch"],
                },
                "exit_paths_from_bottoming": [
                    "false_bottom: lower_low + (bearish_bos|bearish_choch) → early_bearish",
                    "early_bullish path (HL + bullish break)",
                ],
                "note": (
                    "Once in bottoming from 01:35, the machine stays there through the "
                    "main 06.03 selloff unless false_bottom fires. A lone lower_low "
                    "without simultaneous bos/choch does not exit bottoming — so the "
                    "downtrend is classified as bottoming, not strong_bearish."
                ),
            },
        }
    )

    # 8) failed breakdown contribution
    if bot and any(e.get("event_type") == "failed_breakdown" for e in bot.get("events_this_bar") or []):
        findings.append(
            {
                "rank_candidate": 9,
                "id": "failed_breakdown_micro_reclaim",
                "severity": True,
                "evidence": {
                    "event": next(
                        e for e in bot["events_this_bar"] if e["event_type"] == "failed_breakdown"
                    ),
                    "note": (
                        "failed_breakdown uses last_confirmed_swing_low with a short "
                        "return window — micro liquidity reclaim counts as failed "
                        "breakdown and pairs with micro bullish_choch to enter bottoming."
                    ),
                },
            }
        )

    # Rank order by severity for the reference-cycle failure mode
    ranked = [
        ("4", "bottoming_too_permissive", "Primary: enters bottoming at 01:35 via micro CHoCH+FailedBD; blocks correct downtrend labeling"),
        ("3", "early_invalidation_too_permissive", "Primary: exits early_bearish at 00:30 while 5m already HH+HL — never reaches strong"),
        ("1", "protective_level_is_last_micro_swing", "Enables micro CHoCH/BOS on last swing instead of trend-defining level"),
        ("8", "bottoming_sticky_through_selloff", "Once wrong, stays wrong through afternoon selloff"),
        ("5", "early_to_strong_never_cleared", "LH+LL+bearish bias+confirms rarely align; gate stricter than invalidation"),
        ("2", "bos_choch_bias_coupled_to_last_pair_only", "Last-pair bias remaps continuation breaks to CHoCH and flips structure"),
        ("7", "warmup_start_topping", "Wrong phase at window open; narrative already off before 06.03"),
        ("9", "failed_breakdown_micro_reclaim", "Contributes the second hit for premature bottoming"),
        ("6", "htf_15m_not_primary_bug", "15m was bearish at bottoming entry but does not veto; secondary"),
    ]

    return {
        "findings": findings,
        "ranked_causes": [
            {"rank": i + 1, "cause_id": cid, "category_number": num, "summary": summary}
            for i, (num, cid, summary) in enumerate(ranked)
        ],
        "verdict": (
            "Multi-cause failure. Dominant path: premature early_bearish invalidation "
            "(permissive weakening) → premature bottoming (2-hit micro CHoCH + "
            "failed_breakdown on last swing levels) → sticky bottoming through the "
            "real selloff, so strong_bearish is never reached. Protective micro-swing "
            "selection and last-pair bias are the structural enablers."
        ),
    }


def write_markdown(
    synthesis: dict[str, Any],
    transitions: list[dict[str, Any]],
    focus: list[dict[str, Any]],
    out: Path,
) -> None:
    lines = [
        "# Trend-State Misclassification Forensics",
        "",
        synthesis.get("verdict", ""),
        "",
        "## Ranked causes",
        "",
    ]
    for item in synthesis.get("ranked_causes") or []:
        lines.append(
            f"{item['rank']}. **[{item['category_number']}]** `{item['cause_id']}` — {item['summary']}"
        )
    lines.extend(["", "## Transitions (compact)", ""])
    for t in transitions:
        lines.append(
            f"- `{t['decision_time']}`: `{t['from_state']}` → `{t['to_state']}` "
            f"reasons={t['reasons']} events={[e.get('event_type') for e in t.get('events_this_bar') or []]}"
        )
    lines.extend(["", "## Focus states", ""])
    for f in focus:
        lines.append(f"- `{f['decision_time']}` state=`{f['state']}` reasons={f['reasons']}")
    (out / "misclassification_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    args = p.parse_args(argv)
    report = run_forensic_replay(symbol=args.symbol, out_dir=args.out_dir)
    print(json.dumps(json_safe(report["root_cause_synthesis"]["ranked_causes"]), indent=2))
    print(report["root_cause_synthesis"]["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
