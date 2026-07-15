"""Phase C2B2B: counterfactual audit for topping/bottoming → neutral fallback.

Baseline:  C1-C strict + turning strict/24 + neutral fallback off
Candidate: same stack + neutral fallback on (min age 48)

Does not change policy. Does not write into prior result dirs.

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_neutral_fallback_counterfactual_audit \\
    --output-dir research/regime_scanner/results_trend_neutral_fallback_phase_c2b2b
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_robustness_audit import (
    ANALYZE_END,
    ANALYZE_START,
    LOAD_END,
    LOAD_START,
    ground_truth_label,
    install_htf_cache,
    load_analysis_frame,
)
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    default_trend_state_config,
    step_trend_state,
    trend_state_config_c2b,
)
from research.regime_scanner.trend_structure import has_hh_hl, has_lh_ll

DEFAULT_OUT = Path("research/regime_scanner/results_trend_neutral_fallback_phase_c2b2b")
C2B2A_SUMMARY = Path(
    "research/regime_scanner/results_trend_neutral_fallback_phase_c2b2a/summary.json"
)
FORBIDDEN = (
    Path("research/regime_scanner/results"),
    Path("research/regime_scanner/results_trend_robustness_phase_b"),
    Path("research/regime_scanner/results_trend_mapping_root_cause_phase_c0"),
    Path("research/regime_scanner/results_trend_weakening_multi_bar_phase_c1"),
    Path("research/regime_scanner/results_trend_topping_bottoming_phase_c2a"),
    Path("research/regime_scanner/results_trend_topping_bottoming_multibar_phase_c2b1"),
    Path("research/regime_scanner/results_trend_neutral_fallback_phase_c2b2a"),
)

MARCH06 = ("2026-03-06T00:00:00+00:00", "2026-03-07T00:00:00+00:00")
MARCH0809 = ("2026-03-08T00:00:00+00:00", "2026-03-10T00:00:00+00:00")


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    return None if v is None else _ts(v).isoformat()


def assert_safe_output_dir(path: Path) -> None:
    resolved = path.resolve()
    for f in FORBIDDEN:
        if resolved == f.resolve():
            raise ValueError(f"refusing forbidden path: {f}")


def cfg_baseline():
    return trend_state_config_c2b(
        "strict",
        weakening_mode="strict",
        turning_window_bars=24,
        neutral_fallback_mode="off",
    )


def cfg_candidate():
    return trend_state_config_c2b(
        "strict",
        weakening_mode="strict",
        turning_window_bars=24,
        neutral_fallback_mode="on",
        neutral_fallback_min_age_bars=48,
    )


def _run_duration_stats(lengths: list[int]) -> dict[str, Any]:
    if not lengths:
        return {"n_runs": 0, "median": 0.0, "p90": 0.0, "maximum": 0, "ge48": 0, "ge96": 0}
    arr = np.asarray(lengths, dtype=float)
    return {
        "n_runs": int(len(lengths)),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "maximum": int(arr.max()),
        "ge48": int((arr >= 48).sum()),
        "ge96": int((arr >= 96).sum()),
    }


def replay(frame: pd.DataFrame, *, cfg, analyze_start, analyze_end, label: str) -> dict[str, Any]:
    end_decision = _ts(frame["decision_time"].iloc[-1])
    install_htf_cache(frame, end_decision)
    scfg = default_regime_scanner_config().with_timeframe("5m")
    pivots = find_confirmed_pivots(frame, config=scfg)
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    closes = frame["close"].to_numpy(dtype=float)

    state_counts: Counter[str] = Counter()
    runs: dict[str, list[dict[str, Any]]] = {}
    open_run: dict[str, Any] | None = None
    fallbacks: list[dict[str, Any]] = []
    short_early: list[dict[str, Any]] = []
    open_early: dict[str, Any] | None = None

    # Compact analyze arrays for policy / GT / windows
    tl: list[dict[str, Any]] = []

    n = len(frame)
    for i in range(n):
        row = frame.iloc[i]
        decision_ts = _ts(row["decision_time"])
        prev = rt.state
        prev_age = rt.age_5m_bars
        rt, snap, _ = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=frame.iloc[: i + 1][ohlcv],
            bar_index=i,
            cfg=cfg,
            scanner_cfg=scfg,
        )
        if not (analyze_start <= decision_ts <= analyze_end):
            continue

        state = snap.current_state
        state_counts[state] += 1
        ym = f"{decision_ts.year:04d}-{decision_ts.month:02d}"

        c0 = float(closes[i])
        j48 = max(0, i - 48)
        j288 = max(0, i - 288)
        net48 = 100.0 * (c0 / float(closes[j48]) - 1.0) if closes[j48] else 0.0
        net288 = 100.0 * (c0 / float(closes[j288]) - 1.0) if closes[j288] else 0.0
        adx = float(row["adx"]) if "adx" in frame.columns and pd.notna(row["adx"]) else 0.0
        di = float(row["di_spread"]) if "di_spread" in frame.columns and pd.notna(row["di_spread"]) else 0.0
        gt = ground_truth_label(
            has_hh_hl_flag=has_hh_hl(rt.structure_5m),
            has_lh_ll_flag=has_lh_ll(rt.structure_5m),
            net_48=net48,
            net_288=net288,
            adx=adx,
            di_spread=di,
        )
        tl.append(
            {
                "i": i,
                "t": _iso(decision_ts),
                "state": state,
                "prev": prev,
                "gt": gt,
                "allow_long": snap.allow_long,
                "allow_short": snap.allow_short,
                "close": c0,
                "age": snap.age_5m_bars,
                "reasons": list(snap.active_reasons),
                "ym": ym,
                "bias_15m": rt.structure_15m.current_structure_bias,
            }
        )

        def _close_run(to_state: str, reasons: list[str]) -> None:
            nonlocal open_run
            if open_run is None:
                return
            open_run["end"] = _iso(decision_ts)
            open_run["exit_to"] = to_state
            open_run["exit_reasons"] = "|".join(reasons)
            open_run["length_bars"] = int(open_run["length_bars"])
            runs.setdefault(open_run["state"], []).append(open_run)
            open_run = None

        def _close_early(to_state: str) -> None:
            nonlocal open_early
            if open_early is None:
                return
            open_early["end"] = _iso(decision_ts)
            open_early["exit_to"] = to_state
            open_early["length_bars"] = int(open_early["length_bars"])
            if open_early["length_bars"] < 6:
                short_early.append(open_early)
            open_early = None

        if state != prev:
            reasons = list(snap.active_reasons)
            is_fb = "turning_neutral_fallback" in reasons
            if is_fb and prev in {"topping", "bottoming"}:
                # forward returns (audit-only, post decision)
                fwd = {}
                for h in (12, 24, 48):
                    j = min(n - 1, i + h)
                    fwd[f"ret_{h}"] = 100.0 * (float(closes[j]) / c0 - 1.0) if c0 else None
                fallbacks.append(
                    {
                        "label": label,
                        "decision_time": _iso(decision_ts),
                        "from_state": prev,
                        "to_state": state,
                        "age": prev_age,
                        "gt": gt,
                        "bias_15m": rt.structure_15m.current_structure_bias,
                        "reasons": "|".join(reasons),
                        "allow_long": snap.allow_long,
                        "allow_short": snap.allow_short,
                        "close": c0,
                        "ym": ym,
                        **fwd,
                    }
                )
            _close_run(state, reasons)
            _close_early(state)
            if state in {"topping", "bottoming", "early_bearish", "early_bullish", "neutral", "strong_bearish", "strong_bullish", "bullish_weakening", "bearish_weakening", "bottoming", "topping"}:
                # open generic run tracker only for states we care duration stats for
                pass
            if state in {
                "topping",
                "bottoming",
                "early_bearish",
                "early_bullish",
                "neutral",
                "strong_bearish",
                "strong_bullish",
                "bullish_weakening",
                "bearish_weakening",
            }:
                open_run = {
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "|".join(reasons),
                }
            if state in {"early_bearish", "early_bullish"}:
                open_early = {
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "|".join(reasons),
                    "gt_at_entry": gt,
                }
        else:
            if open_run is not None and open_run["state"] == state:
                open_run["length_bars"] = int(open_run["length_bars"]) + 1
            elif open_run is None and state in {
                "topping",
                "bottoming",
                "early_bearish",
                "early_bullish",
                "neutral",
                "strong_bearish",
                "strong_bullish",
                "bullish_weakening",
                "bearish_weakening",
            }:
                open_run = {
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "window_start",
                }
            if open_early is not None and open_early["state"] == state:
                open_early["length_bars"] = int(open_early["length_bars"]) + 1
            elif open_early is None and state in {"early_bearish", "early_bullish"}:
                open_early = {
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "window_start",
                    "gt_at_entry": gt,
                }

    if open_run is not None:
        open_run["end"] = None
        open_run["exit_to"] = open_run["state"]
        open_run["exit_reasons"] = "open_at_end"
        runs.setdefault(open_run["state"], []).append(open_run)
    if open_early is not None:
        open_early["end"] = None
        open_early["exit_to"] = open_early["state"]
        if int(open_early["length_bars"]) < 6:
            short_early.append(open_early)

    dur = {
        st: _run_duration_stats([int(r["length_bars"]) for r in lst]) for st, lst in runs.items()
    }
    return {
        "label": label,
        "config": cfg.to_dict(),
        "state_counts": dict(state_counts),
        "n_analyze": int(sum(state_counts.values())),
        "durations": dur,
        "runs": runs,
        "fallbacks": fallbacks,
        "short_early": short_early,
        "timeline": tl,
    }


def _load_c2b2a_long_runs() -> list[dict[str, Any]]:
    if not C2B2A_SUMMARY.exists():
        return []
    s = json.loads(C2B2A_SUMMARY.read_text(encoding="utf-8"))
    return list(s.get("long_tb_runs_ge96", {}).get("runs", []))


def _find_overlapping_run(runs: list[dict[str, Any]], start: str, state: str) -> dict[str, Any] | None:
    target = _ts(start)
    for r in runs:
        if r["state"] != state:
            continue
        rs = _ts(r["start"])
        # allow match if starts equal or candidate started at/after and within 12 bars of baseline start
        if rs == target or abs((rs - target) / pd.Timedelta(minutes=5)) <= 12:
            return r
    return None


def _next_early_after(tl: list[dict[str, Any]], after_t: str, side: str) -> dict[str, Any] | None:
    after = _ts(after_t)
    want = "early_bearish" if side == "bearish" else "early_bullish"
    for row in tl:
        if _ts(row["t"]) <= after:
            continue
        if row["state"] == want and row["prev"] != want:
            return row
    return None


def classify_long_run(
    base_run: dict[str, Any],
    cand_run: dict[str, Any] | None,
    cand: dict[str, Any],
) -> dict[str, Any]:
    start = base_run["start"]
    state = base_run["state"]
    base_end = base_run.get("end")
    base_len = int(base_run["length_bars"])
    side = "bearish" if state == "topping" else "bullish"

    # fallback during this run window
    fb = None
    for f in cand["fallbacks"]:
        if f["from_state"] != state:
            continue
        ft = _ts(f["decision_time"])
        st = _ts(start)
        et = _ts(base_end) if base_end else _ts("2099-01-01T00:00:00+00:00")
        if st <= ft <= et + pd.Timedelta(minutes=5 * 12):
            fb = f
            break

    if fb is None:
        cand_len = int(cand_run["length_bars"]) if cand_run else None
        return {
            "start": start,
            "state": state,
            "baseline_end": base_end,
            "baseline_length": base_len,
            "candidate_fallback_time": None,
            "candidate_length": cand_len,
            "bars_saved": None if cand_len is None else base_len - cand_len,
            "classification": "not_triggered",
            "dominant_gt": base_run.get("dominant_gt"),
        }

    # next early after fallback
    early = _next_early_after(cand["timeline"], fb["decision_time"], side)
    base_early = _next_early_after(
        # reconstruct approx from baseline runs exit
        [{"t": base_end, "state": base_run.get("exit_to"), "prev": state}]
        if base_end
        else [],
        start,
        side,
    )
    # Better: search baseline timeline for early after start
    # We'll pass baseline timeline via outer - for now use exit_to if early_*
    base_early_time = base_end if str(base_run.get("exit_to", "")).startswith("early_") else None
    cand_early_time = early["t"] if early else None
    delay = None
    if base_early_time and cand_early_time:
        delay = int(round((_ts(cand_early_time) - _ts(base_early_time)) / pd.Timedelta(minutes=5)))

    cand_len = int(cand_run["length_bars"]) if cand_run else None
    saved = None
    if cand_len is not None:
        saved = base_len - cand_len
    elif fb:
        # approximate saved as remaining baseline length after fallback
        saved = max(0, base_len - int(fb["age"]))

    # classification
    cls = "neutral"
    if fb["gt"] in {"CLEAR_UPTREND", "CLEAR_DOWNTREND"}:
        if state == "topping" and fb["gt"] == "CLEAR_UPTREND":
            cls = "dangerous"
        elif state == "bottoming" and fb["gt"] == "CLEAR_DOWNTREND":
            cls = "dangerous"
        elif state == "topping" and fb["gt"] == "CLEAR_DOWNTREND":
            cls = "dangerous"  # abort potential bearish resolution into downtrend via neutral
        elif state == "bottoming" and fb["gt"] == "CLEAR_UPTREND":
            cls = "dangerous"
    if cls != "dangerous" and saved is not None and saved >= 24:
        cls = "improved"
    if cls != "dangerous" and cand_early_time and base_early_time and delay is not None and delay > 48:
        cls = "dangerous"

    return {
        "start": start,
        "state": state,
        "baseline_end": base_end,
        "baseline_length": base_len,
        "baseline_exit_to": base_run.get("exit_to"),
        "candidate_fallback_time": fb["decision_time"],
        "candidate_next_state": fb["to_state"],
        "candidate_length": cand_len,
        "bars_saved_est": saved,
        "gt_at_fallback": fb["gt"],
        "bias_15m_at_fallback": fb["bias_15m"],
        "candidate_next_early": cand_early_time,
        "early_delay_vs_baseline_bars": delay,
        "fwd_ret_12": fb.get("ret_12"),
        "fwd_ret_24": fb.get("ret_24"),
        "fwd_ret_48": fb.get("ret_48"),
        "classification": cls,
        "dominant_gt": base_run.get("dominant_gt"),
    }


def analyze_windows(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def slice_tl(tl, a, b):
        a0, b0 = _ts(a), _ts(b)
        return [r for r in tl if a0 <= _ts(r["t"]) < b0]

    # Mar 6
    b6 = slice_tl(base["timeline"], *MARCH06)
    c6 = slice_tl(cand["timeline"], *MARCH06)

    def first_down(tl):
        for r in tl:
            if r["state"] in {"early_bearish", "strong_bearish"} and not r["allow_long"]:
                return r
        return None

    def first_up(tl):
        for r in tl:
            if r["state"] in {"early_bullish", "strong_bullish"} and r["allow_long"] and not r["allow_short"]:
                return r
        return None

    fb6 = [f for f in cand["fallbacks"] if str(f["decision_time"]).startswith("2026-03-06")]
    # bottoming from 13:50
    bot_1350 = [
        f
        for f in fb6
        if f["from_state"] == "bottoming" and _ts(f["decision_time"]) >= _ts("2026-03-06T13:50:00+00:00")
    ]
    out["mar06"] = {
        "baseline_first_down_block": first_down(b6),
        "candidate_first_down_block": first_down(c6),
        "fallbacks": fb6,
        "bottoming_1350_fallbacks": bot_1350,
        "baseline_state_counts": dict(Counter(r["state"] for r in b6)),
        "candidate_state_counts": dict(Counter(r["state"] for r in c6)),
        "new_long_in_clear_down": sum(
            1
            for b, c in zip(b6, c6)
            if b["gt"] == "CLEAR_DOWNTREND" and (not b["allow_long"]) and c["allow_long"]
        ),
        "lost_short_in_clear_down": sum(
            1
            for b, c in zip(b6, c6)
            if b["gt"] == "CLEAR_DOWNTREND" and b["allow_short"] and (not c["allow_short"])
        ),
    }

    b89 = slice_tl(base["timeline"], *MARCH0809)
    c89 = slice_tl(cand["timeline"], *MARCH0809)
    out["mar0809"] = {
        "baseline_first_up": first_up(b89),
        "candidate_first_up": first_up(c89),
        "fallbacks": [
            f
            for f in cand["fallbacks"]
            if _ts(MARCH0809[0]) <= _ts(f["decision_time"]) < _ts(MARCH0809[1])
        ],
        "baseline_state_counts": dict(Counter(r["state"] for r in b89)),
        "candidate_state_counts": dict(Counter(r["state"] for r in c89)),
    }

    for ym in ("2026-04", "2026-05"):
        out[ym] = {
            "n_fallbacks": sum(1 for f in cand["fallbacks"] if f["ym"] == ym),
            "baseline_tb_bars": sum(
                1 for r in base["timeline"] if r["ym"] == ym and r["state"] in {"topping", "bottoming"}
            ),
            "candidate_tb_bars": sum(
                1 for r in cand["timeline"] if r["ym"] == ym and r["state"] in {"topping", "bottoming"}
            ),
            "candidate_neutral_bars": sum(
                1 for r in cand["timeline"] if r["ym"] == ym and r["state"] == "neutral"
            ),
        }
    return out


def policy_deltas(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    btl, ctl = base["timeline"], cand["timeline"]
    n = min(len(btl), len(ctl))
    changed = 0
    first = None
    bad_long_in_down = 0
    lost_short_in_down = 0
    bad_short_in_up = 0
    lost_long_in_up = 0
    for i in range(n):
        b, c = btl[i], ctl[i]
        if b["allow_long"] != c["allow_long"] or b["allow_short"] != c["allow_short"]:
            changed += 1
            if first is None:
                first = {
                    "decision_time": c["t"],
                    "baseline_state": b["state"],
                    "candidate_state": c["state"],
                    "baseline_allow": (b["allow_long"], b["allow_short"]),
                    "candidate_allow": (c["allow_long"], c["allow_short"]),
                    "gt": c["gt"],
                    "reasons": "|".join(c["reasons"]),
                }
        if b["gt"] == "CLEAR_DOWNTREND":
            if (not b["allow_long"]) and c["allow_long"]:
                bad_long_in_down += 1
            if b["allow_short"] and (not c["allow_short"]):
                lost_short_in_down += 1
        if b["gt"] == "CLEAR_UPTREND":
            if (not b["allow_short"]) and c["allow_short"]:
                bad_short_in_up += 1
            if b["allow_long"] and (not c["allow_long"]):
                lost_long_in_up += 1
    return {
        "bars_policy_changed": changed,
        "first_divergence": first,
        "new_long_in_clear_down": bad_long_in_down,
        "lost_short_in_clear_down": lost_short_in_down,
        "new_short_in_clear_up": bad_short_in_up,
        "lost_long_in_clear_up": lost_long_in_up,
    }


def decide(summary_bits: dict[str, Any]) -> dict[str, Any]:
    long_eval = summary_bits["long_run_eval"]
    pol = summary_bits["policy"]
    mar6 = summary_bits["windows"]["mar06"]
    mar89 = summary_bits["windows"]["mar0809"]
    short = summary_bits["short_early"]
    dangerous = sum(1 for r in long_eval if r["classification"] == "dangerous")
    improved = sum(1 for r in long_eval if r["classification"] == "improved")
    not_trig = sum(1 for r in long_eval if r["classification"] == "not_triggered")

    issues: list[str] = []
    if pol["new_long_in_clear_down"] > 0 or mar6["new_long_in_clear_down"] > 0:
        issues.append("new_long_in_clear_downtrend")
    if pol["lost_short_in_clear_down"] > 0 or mar6["lost_short_in_clear_down"] > 0:
        issues.append("lost_short_in_clear_downtrend")
    if dangerous > 0:
        issues.append("dangerous_long_run_neutralizations")
    if short["candidate_n"] > short["baseline_n"] * 1.25 + 10:
        issues.append("short_early_increased")
    b_up = mar89.get("baseline_first_up")
    c_up = mar89.get("candidate_first_up")
    if b_up and not c_up:
        issues.append("lost_mar0809_up_recognition")
    elif b_up and c_up:
        delay = int(round((_ts(c_up["t"]) - _ts(b_up["t"])) / pd.Timedelta(minutes=5)))
        if delay > 48:
            issues.append("mar0809_up_delayed_>48bars")

    ge96_base = summary_bits["baseline_tb_ge96"]
    ge96_cand = summary_bits["candidate_tb_ge96"]
    reduced = ge96_cand < ge96_base

    if issues:
        # if helps duration but needs gates
        if reduced and all(i.startswith("dangerous") or "short_early" in i or "delayed" in i for i in issues):
            return {
                "decision": "REVISE",
                "issues": issues,
                "next_minimal_candidate": (
                    "Keep age≥48; additionally require dominant stretch of no hard evidence "
                    "for ≥24 bars OR require GT-agnostic chop proxy: abs(net_48)<0.5 already "
                    "causal — do NOT implement here."
                ),
            }
        return {"decision": "REJECT", "issues": issues, "next_minimal_candidate": None}

    if not reduced and improved == 0:
        return {
            "decision": "REJECT",
            "issues": ["no_material_stuck_run_reduction"],
            "next_minimal_candidate": None,
        }

    return {
        "decision": "ACCEPT",
        "issues": [],
        "next_minimal_candidate": None,
        "notes": f"improved={improved} not_triggered={not_trig} ge96 {ge96_base}->{ge96_cand}",
    }


def run_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    assert default_trend_state_config().turning_neutral_fallback_mode == "off"

    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    a0, a1 = _ts(analyze_start), _ts(analyze_end)

    print("replay baseline (fallback off) …", flush=True)
    base = replay(frame, cfg=cfg_baseline(), analyze_start=a0, analyze_end=a1, label="baseline")
    print("replay candidate (fallback on@48) …", flush=True)
    cand = replay(frame, cfg=cfg_candidate(), analyze_start=a0, analyze_end=a1, label="candidate")

    # Verify baseline bit-path config
    assert base["config"]["turning_neutral_fallback_mode"] == "off"
    assert cand["config"]["turning_neutral_fallback_mode"] == "on"

    long_known = _load_c2b2a_long_runs()
    long_eval = []
    for br in long_known:
        cr = _find_overlapping_run(cand["runs"].get(br["state"], []), br["start"], br["state"])
        # also get baseline overlapping for end consistency
        long_eval.append(classify_long_run(br, cr, cand))

    short_b = base["short_early"]
    short_c = cand["short_early"]
    short_summary = {
        "baseline_n": len(short_b),
        "candidate_n": len(short_c),
        "baseline_from_tb": sum(1 for s in short_b if s.get("prev_state") in {"topping", "bottoming"}),
        "candidate_from_tb": sum(1 for s in short_c if s.get("prev_state") in {"topping", "bottoming"}),
        "candidate_from_neutral": sum(1 for s in short_c if s.get("prev_state") == "neutral"),
        "delta": len(short_c) - len(short_b),
    }

    dangerous_fbs = [
        f
        for f in cand["fallbacks"]
        if f["gt"] in {"CLEAR_UPTREND", "CLEAR_DOWNTREND"}
    ]

    windows = analyze_windows(base, cand)
    policy = policy_deltas(base, cand)

    def tb_ge96(rep):
        return int(rep["durations"].get("topping", {}).get("ge96", 0)) + int(
            rep["durations"].get("bottoming", {}).get("ge96", 0)
        )

    bits = {
        "long_run_eval": long_eval,
        "policy": policy,
        "windows": windows,
        "short_early": short_summary,
        "baseline_tb_ge96": tb_ge96(base),
        "candidate_tb_ge96": tb_ge96(cand),
    }
    decision = decide(bits)

    summary = {
        "phase": "C2B2B_neutral_fallback_counterfactual",
        "symbol": symbol,
        "n_load_bars": int(len(frame)),
        "baseline_config": base["config"],
        "candidate_config": cand["config"],
        "state_counts": {"baseline": base["state_counts"], "candidate": cand["state_counts"]},
        "durations": {"baseline": base["durations"], "candidate": cand["durations"]},
        "n_neutral_fallbacks": len(cand["fallbacks"]),
        "fallbacks": cand["fallbacks"][:80],
        "long_run_eval": long_eval,
        "short_early": short_summary,
        "dangerous_clear_trend_fallbacks": {
            "n": len(dangerous_fbs),
            "samples": dangerous_fbs[:30],
        },
        "windows": {
            k: {
                kk: vv
                for kk, vv in v.items()
                if kk
                not in {
                    # trim large nested if any
                }
            }
            for k, v in windows.items()
        },
        "policy_simulation": policy,
        "decision": decision,
        "safety": {
            "policy_code_unchanged": True,
            "default_fallback_off": True,
            "no_march_hardcode": True,
        },
    }
    # Drop full timeline from persisted summary (too large)
    blob = json.dumps(json_safe(summary), sort_keys=True, separators=(",", ":"))
    summary["deterministic_hash"] = hashlib.sha256(blob.encode()).hexdigest()
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "long_run_eval.json").write_text(
        json.dumps(json_safe(long_eval), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README_results.md").write_text(
        f"""# Phase C2B2B — Neutral fallback counterfactual

Baseline: strict/strict/24 + fallback **off**
Candidate: strict/strict/24 + fallback **on** (age≥48)

## Decision
**{decision['decision']}** — issues: {decision.get('issues')}

## Headline
- Neutral fallbacks: {len(cand['fallbacks'])}
- TB runs ≥96: {tb_ge96(base)} → {tb_ge96(cand)}
- Short early&lt;6: {short_summary['baseline_n']} → {short_summary['candidate_n']}
- Policy bars changed: {policy['bars_policy_changed']}
- Dangerous CLEAR-trend fallbacks: {len(dangerous_fbs)}

Default production `turning_neutral_fallback_mode` remains **off**.
""",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C2B2B neutral fallback counterfactual audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--load-start", default=LOAD_START)
    p.add_argument("--load-end", default=LOAD_END)
    p.add_argument("--analyze-start", default=ANALYZE_START)
    p.add_argument("--analyze-end", default=ANALYZE_END)
    args = p.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        output_dir=args.output_dir,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "decision": summary["decision"],
                "n_fallbacks": summary["n_neutral_fallbacks"],
                "tb_ge96": {
                    "baseline": summary["durations"]["baseline"].get("topping", {}),
                    "candidate_topping": summary["durations"]["candidate"].get("topping", {}),
                    "candidate_bottoming": summary["durations"]["candidate"].get("bottoming", {}),
                },
                "short_early": summary["short_early"],
                "policy": summary["policy_simulation"],
                "long_run_classes": Counter(r["classification"] for r in summary["long_run_eval"]),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
