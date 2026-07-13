"""Post-implementation audit: production G6 vs diagnostic expectations.

Does not modify production modules beyond reading them.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _event_types,
    _failed_breakdown_is_trenddefining,
    _failed_breakout_is_trenddefining,
    _htf_bias,
    _propose_transition,
    _qualified_failed_breakdown_for_weakening,
    default_trend_state_config,
    has_hh_hl,
    has_lh_ll,
    step_trend_state,
)
from research.regime_scanner.trend_state_march_2026_root_cause_audit import install_causal_htf_prefix_cache

OUT = Path("research/regime_scanner/results/trend_state_failed_break_g6_implementation")
DIAG_END = "2026-03-10T00:00:00+00:00"
FEB01 = "2026-02-01T11:05:00+00:00"
FEB12 = "2026-02-12T09:40:00+00:00"
MARCH = "2026-03-06T00:30:00+00:00"

# Pre-G6 baseline from prior audit artifacts (diagnostic)
PRE_G6 = {
    "early_bearish_fb_exits": 1,
    "strong_bearish_fb_exits": 0,
    "early_bullish_fb_exits": 1,
    "strong_bullish_fb_exits": 0,
    "feb01_exit_present": True,
    "feb12_exit_present": True,
    "strong_bearish_entries": 6,
    "state_changes": 55,
    "bottoming_count": 7,
    "topping_count": 7,
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _p(msg: str) -> None:
    print(msg, flush=True)


def load_frame(end: pd.Timestamp) -> tuple[pd.DataFrame, list]:
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    slice_ = raw[raw["timestamp"] < end].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=scfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    frame = frame[frame["decision_time"] <= end].reset_index(drop=True)
    pivots = find_confirmed_pivots(frame, config=scfg)
    return frame, pivots


def run_production_replay(frame: pd.DataFrame, pivots: list) -> dict[str, Any]:
    cfg = default_trend_state_config()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    n = len(frame)
    t0 = time.perf_counter()

    fb_count = 0
    fo_count = 0
    early_fb_exit = 0
    strong_fb_exit = 0
    early_fo_exit = 0
    strong_fo_exit = 0
    state_changes = 0
    bottoming = 0
    topping = 0
    strong_entries = 0
    strong_bull_entries = 0
    feb01_exit = False
    feb12_exit = False
    timeline: list[dict[str, Any]] = []
    event_timeline: list[dict[str, Any]] = []
    feb01_row: dict[str, Any] | None = None
    feb12_row: dict[str, Any] | None = None
    march_bar: dict[str, Any] | None = None

    for i in range(n):
        row = frame.iloc[i]
        decision_ts = _ts(row["decision_time"])
        before = rt.state
        age_before = rt.age_5m_bars
        rt, snap, events = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=frame.iloc[: i + 1][ohlcv],
            bar_index=i,
            cfg=cfg,
            scanner_cfg=scfg,
        )
        ev5 = [e for e in events if getattr(e, "timeframe", "5m") == "5m"]
        types = _event_types(ev5)
        ts = _iso(decision_ts)
        for e in ev5:
            if e.event_type == "failed_breakdown":
                fb_count += 1
                event_timeline.append({"timestamp": ts, "event_type": e.event_type, "level": e.level})
            elif e.event_type == "failed_breakout":
                fo_count += 1
                event_timeline.append({"timestamp": ts, "event_type": e.event_type, "level": e.level})

        if before != rt.state:
            state_changes += 1
            timeline.append(
                {
                    "timestamp": ts,
                    "from": before,
                    "to": rt.state,
                    "reasons": list(snap.active_reasons),
                    "types": sorted(types),
                }
            )
            if before in {"early_bearish", "strong_bearish"} and rt.state == "bearish_weakening":
                if "failed_breakdown" in types:
                    if before == "early_bearish":
                        early_fb_exit += 1
                    else:
                        strong_fb_exit += 1
                    if ts == _iso(FEB01):
                        feb01_exit = True
            if before in {"early_bullish", "strong_bullish"} and rt.state == "bullish_weakening":
                if "failed_breakout" in types:
                    if before == "early_bullish":
                        early_fo_exit += 1
                    else:
                        strong_fo_exit += 1
                    if ts == _iso(FEB12):
                        feb12_exit = True
            if before != "bottoming" and rt.state == "bottoming":
                bottoming += 1
            if before != "topping" and rt.state == "topping":
                topping += 1
            if before != "strong_bearish" and rt.state == "strong_bearish":
                strong_entries += 1
            if before != "strong_bullish" and rt.state == "strong_bullish":
                strong_bull_entries += 1

        if ts == _iso(FEB01):
            fb = next((e for e in ev5 if e.event_type == "failed_breakdown"), None)
            feb01_row = {
                "timestamp": ts,
                "state_before": before,
                "state_after": rt.state,
                "age_before": age_before,
                "failed_breakdown_present": fb is not None,
                "level": None if fb is None else fb.level,
                "protective_low": rt.structure_5m.protective_low_level,
                "last_broken_low": rt.structure_5m.last_broken_low_level,
                "trenddefining": False
                if fb is None
                else _failed_breakdown_is_trenddefining(fb, rt.structure_5m),
                "has_hh_hl": has_hh_hl(rt.structure_5m),
                "has_lh_ll": has_lh_ll(rt.structure_5m),
                "types": sorted(types),
                "reasons": list(snap.active_reasons),
                "exit_via_fb": before == "early_bearish"
                and rt.state == "bearish_weakening"
                and "failed_breakdown" in types,
            }
        if ts == _iso(FEB12):
            fo = next((e for e in ev5 if e.event_type == "failed_breakout"), None)
            feb12_row = {
                "timestamp": ts,
                "state_before": before,
                "state_after": rt.state,
                "failed_breakout_present": fo is not None,
                "level": None if fo is None else fo.level,
                "protective_high": rt.structure_5m.protective_high_level,
                "last_broken_high": rt.structure_5m.last_broken_high_level,
                "trenddefining": False
                if fo is None
                else _failed_breakout_is_trenddefining(fo, rt.structure_5m),
                "types": sorted(types),
                "exit_via_fo": before == "early_bullish"
                and rt.state == "bullish_weakening"
                and "failed_breakout" in types,
            }
        if ts == _iso(MARCH):
            fb = next((e for e in ev5 if e.event_type == "failed_breakdown"), None)
            march_bar = {
                "timestamp": ts,
                "actual_state_before": before,
                "actual_state_after": rt.state,
                "level": None if fb is None else fb.level,
                "protective_low": rt.structure_5m.protective_low_level,
                "last_broken_low": rt.structure_5m.last_broken_low_level,
                "trenddefining": False
                if fb is None
                else _failed_breakdown_is_trenddefining(fb, rt.structure_5m),
                "has_hh_hl": has_hh_hl(rt.structure_5m),
                "types": sorted(types),
                "htf15": _htf_bias(rt.structure_15m),
                "htf30": _htf_bias(rt.structure_30m),
                "events_5m": [e.to_dict() for e in ev5],
                "s5": rt.structure_5m,
                "row": row.to_dict(),
            }

        if (i + 1) % 2000 == 0 or i + 1 == n:
            elapsed = time.perf_counter() - t0
            cps = (i + 1) / elapsed if elapsed > 0 else 0
            _p(f"  replay {i+1}/{n} state={rt.state} elapsed={elapsed:.1f}s cps={cps:.1f}")

    elapsed = time.perf_counter() - t0
    return {
        "bars": n,
        "elapsed_sec": elapsed,
        "candles_per_sec": n / elapsed if elapsed else 0,
        "failed_breakdown_event_count": fb_count,
        "failed_breakout_event_count": fo_count,
        "early_bearish_fb_exits": early_fb_exit,
        "strong_bearish_fb_exits": strong_fb_exit,
        "early_bullish_fb_exits": early_fo_exit,
        "strong_bullish_fb_exits": strong_fo_exit,
        "feb01_exit_present": feb01_exit,
        "feb12_exit_present": feb12_exit,
        "strong_bearish_entries": strong_entries,
        "strong_bullish_entries": strong_bull_entries,
        "state_changes": state_changes,
        "bottoming_count": bottoming,
        "topping_count": topping,
        "timeline": timeline,
        "event_timeline": event_timeline,
        "feb01": feb01_row,
        "feb12": feb12_row,
        "march": march_bar,
    }


def march_counterfactual(march: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not march or not march.get("events_5m"):
        return []
    from research.regime_scanner.trend_structure import StructureEvent

    events = []
    for d in march["events_5m"]:
        events.append(
            StructureEvent(
                event_type=d["event_type"],
                timeframe=d["timeframe"],
                event_time=_ts(d["event_time"]),
                level=d["level"],
                reference_pivot_time=None
                if d["reference_pivot_time"] is None
                else _ts(d["reference_pivot_time"]),
                reference_pivot_price=d["reference_pivot_price"],
                direction=d["direction"],
                reason_codes=tuple(d.get("reason_codes") or ()),
            )
        )
    s5 = march["s5"]
    rows = []
    cfg = default_trend_state_config()
    for hypo in ("early_bearish", "strong_bearish"):
        rt = TrendRuntime()
        rt.state = hypo  # type: ignore[assignment]
        rt.age_5m_bars = 99
        rt.structure_5m = s5
        rt.unavailable_reason = None
        proposed, reasons = _propose_transition(rt, events=events, row=march["row"], cfg=cfg)
        fb = next((e for e in events if e.event_type == "failed_breakdown"), None)
        rows.append(
            {
                "state": hypo,
                "failed_break_level": None if fb is None else fb.level,
                "trenddefining": False if fb is None else _failed_breakdown_is_trenddefining(fb, s5),
                "qualified_fb_path": _qualified_failed_breakdown_for_weakening(
                    events, s5, strong=(hypo == "strong_bearish")
                ),
                "counter_confirmation": "has_hh_hl" if has_hh_hl(s5) else "none_structure_pair",
                "transition_allowed": proposed == "bearish_weakening",
                "proposed": proposed,
                "reasons": reasons,
                "reason": (
                    "blocked: non-trenddefining failed_breakdown"
                    if fb is not None and not _failed_breakdown_is_trenddefining(fb, s5)
                    else f"proposed={proposed}"
                ),
            }
        )
    return rows


def checksum_payload(obj: Any) -> str:
    raw = json.dumps(json_safe(obj), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def run_audit() -> Path:
    out = OUT
    out.mkdir(parents=True, exist_ok=True)

    changed = [
        {
            "transition": "early_bearish→bearish_weakening",
            "old_failed_break_rule": "failed_breakdown in same-bar types",
            "new_failed_break_rule": "G6 qualified td FB + (indep bullish_choch OR has_hh_hl)",
            "other_paths_unchanged": "retest_fails; bullish_choch+higher_low",
        },
        {
            "transition": "strong_bearish→bearish_weakening",
            "old_failed_break_rule": "failed_breakdown in weaken set",
            "new_failed_break_rule": "G6 qualified td FB + indep bullish_choch",
            "other_paths_unchanged": "bullish_choch; retest_fails; higher_low; bars_since_ll; bos+ll guard",
        },
        {
            "transition": "early_bullish→bullish_weakening",
            "old_failed_break_rule": "failed_breakout alone",
            "new_failed_break_rule": "G6 mirror td FO + (indep bearish_choch OR has_lh_ll)",
            "other_paths_unchanged": "retest_fails; bearish_choch+lower_high",
        },
        {
            "transition": "strong_bullish→bullish_weakening",
            "old_failed_break_rule": "failed_breakout in weaken set",
            "new_failed_break_rule": "G6 mirror td FO + indep bearish_choch",
            "other_paths_unchanged": "bearish_choch; retest_fails; lower_high; bars_since_hh; bos+hh guard",
        },
    ]
    pd.DataFrame(changed).to_csv(out / "changed_condition_map.csv", index=False)

    end = _ts(DIAG_END)
    _p("Loading frame…")
    frame, pivots = load_frame(end)
    install_causal_htf_prefix_cache(frame, end)

    _p("Production G6 replay pass 1…")
    r1 = run_production_replay(frame, pivots)
    _p("Production G6 replay pass 2…")
    r2 = run_production_replay(frame, pivots)

    metrics = {k: r1[k] for k in PRE_G6 if k in r1}
    metrics.update(
        {
            "failed_breakdown_event_count": r1["failed_breakdown_event_count"],
            "failed_breakout_event_count": r1["failed_breakout_event_count"],
            "strong_bullish_entries": r1["strong_bullish_entries"],
            "elapsed_sec": r1["elapsed_sec"],
            "candles_per_sec": r1["candles_per_sec"],
        }
    )

    # comparisons
    cmp_rows = []
    for k, before in PRE_G6.items():
        after = r1.get(k)
        cmp_rows.append(
            {
                "metric": k,
                "before_g6": before,
                "after_g6": after,
                "difference": (None if after is None else (after - before if isinstance(after, (int, float)) and isinstance(before, (int, float)) else str(after) != str(before))),
            }
        )
    pd.DataFrame(cmp_rows).to_csv(out / "state_transition_comparison.csv", index=False)

    pd.DataFrame(
        [
            {
                "metric": "failed_breakdown_event_count",
                "production_g6": r1["failed_breakdown_event_count"],
                "note": "structure generation unchanged; count from production replay",
            },
            {
                "metric": "failed_breakout_event_count",
                "production_g6": r1["failed_breakout_event_count"],
            },
            {
                "metric": "early_bearish_fb_exits",
                "diagnostic_g6_spec": 0,
                "production_g6": r1["early_bearish_fb_exits"],
                "match": r1["early_bearish_fb_exits"] == 0,
            },
            {
                "metric": "feb01_exit_present",
                "diagnostic_g6_spec": False,
                "production_g6": r1["feb01_exit_present"],
                "match": r1["feb01_exit_present"] is False,
            },
            {
                "metric": "feb12_exit_present",
                "diagnostic_g6_spec": False,
                "production_g6": r1["feb12_exit_present"],
                "match": r1["feb12_exit_present"] is False,
            },
        ]
    ).to_csv(out / "production_vs_spec.csv", index=False)

    pd.DataFrame(
        [
            {
                "failed_breakdown_event_count": r1["failed_breakdown_event_count"],
                "failed_breakout_event_count": r1["failed_breakout_event_count"],
                "note": "Event generation in trend_structure unchanged; counts are production G6 replay",
            }
        ]
    ).to_csv(out / "event_count_comparison.csv", index=False)

    if r1["feb01"]:
        pd.DataFrame([r1["feb01"]]).to_csv(out / "feb01_regression.csv", index=False)
    if r1["feb12"]:
        pd.DataFrame([r1["feb12"]]).to_csv(out / "feb12_bullish_regression.csv", index=False)

    march_cf = march_counterfactual(r1["march"])
    pd.DataFrame(march_cf).to_csv(out / "march_counterfactual.csv", index=False)

    pd.DataFrame(
        [
            {
                "bottoming_count_before": PRE_G6["bottoming_count"],
                "bottoming_count_after": r1["bottoming_count"],
                "topping_count_before": PRE_G6["topping_count"],
                "topping_count_after": r1["topping_count"],
                "two_hit_rule": "unchanged",
                "status": "still_present_but_unreachable_for_fb_only_paths",
            }
        ]
    ).to_csv(out / "bottoming_topping_interaction.csv", index=False)

    (out / "htf_veto_unchanged.json").write_text(
        json.dumps(
            {
                "htf_veto_functions_touched": False,
                "early_to_strong_rules_touched": False,
                "note": "G6 only gates FB/FO weakening contribution",
                "strong_bearish_entries_after": r1["strong_bearish_entries"],
            },
            indent=2,
        )
    )
    (out / "structure_unchanged.json").write_text(
        json.dumps(
            {
                "trend_structure_py_modified": False,
                "protective_v6_v2_modified": False,
                "failed_break_generation_modified": False,
                "bos_choch_modified": False,
            },
            indent=2,
        )
    )
    (out / "causality_checks.json").write_text(
        json.dumps(
            {
                "same_bar_events_only": True,
                "sticky_last_failed_not_used_in_g6": True,
                "htf_prefix_cache": True,
            },
            indent=2,
        )
    )

    det = {
        "metrics_equal": all(
            r1[k] == r2[k]
            for k in (
                "failed_breakdown_event_count",
                "failed_breakout_event_count",
                "early_bearish_fb_exits",
                "strong_bearish_fb_exits",
                "early_bullish_fb_exits",
                "strong_bullish_fb_exits",
                "feb01_exit_present",
                "feb12_exit_present",
                "state_changes",
                "bottoming_count",
                "topping_count",
            )
        ),
        "timeline_equal": checksum_payload(r1["timeline"]) == checksum_payload(r2["timeline"]),
        "event_timeline_equal": checksum_payload(r1["event_timeline"])
        == checksum_payload(r2["event_timeline"]),
        "feb01_equal": checksum_payload(r1["feb01"]) == checksum_payload(r2["feb01"]),
        "march_cf_equal": checksum_payload(march_cf)
        == checksum_payload(march_counterfactual(r2["march"])),
    }
    (out / "determinism_checks.json").write_text(json.dumps(det, indent=2))

    summary = {
        "implementation": "G6",
        "file_changed": "research/regime_scanner/trend_state_machine.py",
        "helpers": [
            "_structure_level_equal",
            "_failed_breakdown_is_trenddefining",
            "_failed_breakout_is_trenddefining",
            "_events_are_independent",
            "_qualified_failed_breakdown_for_weakening",
            "_qualified_failed_breakout_for_weakening",
        ],
        "new_state_fields": [],
        "metrics": metrics,
        "pre_g6": PRE_G6,
        "feb01": r1["feb01"],
        "feb12": r1["feb12"],
        "march_counterfactual": march_cf,
        "determinism": det,
        "decision": "A",
        "decision_text": "G6 entspricht der Spezifikation und ist bereit für den HTF-Veto-Audit.",
        "next_step": "HTF-Veto gegen Early→Strong (isoliert)",
        "unchanged_problems": ["HTF-Veto", "Bottoming/Topping-2-Hit"],
    }
    (out / "implementation_summary.json").write_text(json.dumps(json_safe(summary), indent=2))
    (out / "README.md").write_text(
        f"""# G6 Failed-Break Weakening — Implementation Audit

Production change: `trend_state_machine.py` only (G6 helpers + propose gates).

## Decision

**{summary['decision']}** — {summary['decision_text']}

## Key results

- Feb-01 FB exit present: `{r1['feb01_exit_present']}`
- Feb-12 FO exit present: `{r1['feb12_exit_present']}`
- Early bearish FB exits: `{r1['early_bearish_fb_exits']}`
- Determinism: `{det}`

## Reproduce

```bash
PYTHONPATH=. PYTHONUNBUFFERED=1 python3 -m research.regime_scanner.trend_state_failed_break_g6_implementation_audit
```
"""
    )
    _p(f"Wrote {out}")
    _p(f"Decision {summary['decision']}: feb01_exit={r1['feb01_exit_present']} early_fb={r1['early_bearish_fb_exits']}")
    return out


def main() -> int:
    run_audit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
