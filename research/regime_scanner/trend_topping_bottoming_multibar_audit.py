"""Phase C2B1: topping/bottoming multi-bar turning exit audit (research-only).

Compares under C1-C strict weakening:
  C2B-A  turning_multi_bar_mode=off     (baseline)
  C2B-B  turning_multi_bar_mode=loose   (window=24 primary)
  C2B-C  turning_multi_bar_mode=strict  (window=24)

Also records window sensitivity for loose @ 12/36 (secondary).

Does not change policy, default modes (remain off), Phase-B/C0/C1/C2A dirs,
or research/regime_scanner/results/.

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_topping_bottoming_multibar_audit \\
    --symbol APTUSDT \\
    --output-dir research/regime_scanner/results_trend_topping_bottoming_multibar_phase_c2b1
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
    AUDIT_CLASS_MAP,
    LOAD_END,
    LOAD_START,
    contiguous_episodes,
    delay_summary,
    ground_truth_label,
    install_htf_cache,
    load_analysis_frame,
)
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    TrendStateConfig,
    default_trend_state_config,
    step_trend_state,
    trend_state_config_c2b,
)
from research.regime_scanner.trend_state_policy import policy_for_state
from research.regime_scanner.trend_structure import has_hh_hl, has_lh_ll

DEFAULT_OUT = Path("research/regime_scanner/results_trend_topping_bottoming_multibar_phase_c2b1")
FORBIDDEN = (
    Path("research/regime_scanner/results"),
    Path("research/regime_scanner/results_trend_robustness_phase_b"),
    Path("research/regime_scanner/results_trend_mapping_root_cause_phase_c0"),
    Path("research/regime_scanner/results_trend_weakening_multi_bar_phase_c1"),
    Path("research/regime_scanner/results_trend_topping_bottoming_phase_c2a"),
)

PRIMARY_WINDOW = 24
WINDOWS_SENSITIVITY = (12, 24, 36)

MARCH06 = ("2026-03-06T00:00:00+00:00", "2026-03-07T00:00:00+00:00")
MARCH0809 = ("2026-03-08T00:00:00+00:00", "2026-03-10T00:00:00+00:00")
CASE = ("2026-03-05T18:00:00+00:00", "2026-03-10T00:00:00+00:00")
CAUSAL_MAR6_REF = "2026-03-06T05:05:00+00:00"

CODE_AUDIT: dict[str, Any] = {
    "phase": "C2B1_pre_fix_code_audit",
    "file": "research/regime_scanner/trend_state_machine.py",
    "persistent_available": [
        "last_bos / last_choch (StructureEvent with event_time)",
        "last_high_label / last_low_label",
        "protective levels",
        "consecutive_*_closes impulse",
        "structure_15m/30m bias",
        "C1 weakening_evidence_* ledger (separate; cleared on enter)",
    ],
    "same_bar_only_before_fix": [
        "topping→early_bearish required bearish_bos|choch in types (current bar)",
        "LH could already use last_high_label (persisted)",
        "bottoming mirror for bullish_bos|choch",
    ],
    "reuse_c1": [
        "_accumulate_category_evidence shared helper",
        "same expire / no-double-key / continuation-reset patterns",
        "separate turning_evidence_* fields (weakening ledger cleared on enter topping)",
    ],
    "new_fields": [
        "TrendStateConfig.turning_multi_bar_mode / turning_evidence_window_bars",
        "TrendRuntime.turning_evidence_keys / turning_evidence_seen_age",
    ],
    "reset_rules": [
        "clear on _enter any state",
        "clear when mode=off or not in topping|bottoming",
        "window expiry by age",
        "continuation HH/LL path clears",
        "opposite-direction events not admitted into ledger",
    ],
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    return None if v is None else _ts(v).isoformat()


def assert_safe_output_dir(path: Path) -> None:
    resolved = path.resolve()
    for f in FORBIDDEN:
        if resolved == f.resolve():
            raise ValueError(f"refusing forbidden output path: {f}")


def _run_stats(lengths: list[int]) -> dict[str, Any]:
    if not lengths:
        return {
            "n_runs": 0,
            "median": 0.0,
            "p75": 0.0,
            "p90": 0.0,
            "maximum": 0,
            "ge24": 0,
            "ge48": 0,
            "ge96": 0,
            "ge288": 0,
        }
    arr = np.asarray(lengths, dtype=float)
    return {
        "n_runs": int(len(lengths)),
        "median": float(np.median(arr)),
        "p75": float(np.percentile(arr, 75)),
        "p90": float(np.percentile(arr, 90)),
        "maximum": int(arr.max()),
        "ge24": int((arr >= 24).sum()),
        "ge48": int((arr >= 48).sum()),
        "ge96": int((arr >= 96).sum()),
        "ge288": int((arr >= 288).sum()),
    }


def make_cfg(turning_mode: str, window: int = PRIMARY_WINDOW) -> TrendStateConfig:
    if turning_mode == "off":
        # C1-C strict + turning off
        return trend_state_config_c2b("off", weakening_mode="strict", turning_window_bars=window)
    return trend_state_config_c2b(turning_mode, weakening_mode="strict", turning_window_bars=window)  # type: ignore[arg-type]


def replay(
    frame: pd.DataFrame,
    *,
    cfg: TrendStateConfig,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    variant: str,
) -> dict[str, Any]:
    end_decision = _ts(frame["decision_time"].iloc[-1])
    install_htf_cache(frame, end_decision)
    scfg = default_regime_scanner_config().with_timeframe("5m")
    pivots = find_confirmed_pivots(frame, config=scfg)
    rt = TrendRuntime()

    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    state_counts: Counter[str] = Counter()
    monthly_state: dict[str, Counter[str]] = {}
    transitions: list[tuple[str, str, pd.Timestamp]] = []
    turning_exits: list[dict[str, Any]] = []
    runs_top: list[dict[str, Any]] = []
    runs_bot: list[dict[str, Any]] = []
    open_run: dict[str, Any] | None = None

    # Compact analyze timeline for GT / false-transition / ping-pong
    tl_state: list[str] = []
    tl_time: list[str] = []
    tl_ym: list[str] = []
    tl_hhhl: list[bool] = []
    tl_lhll: list[bool] = []
    tl_adx: list[float] = []
    tl_di: list[float] = []
    tl_net48: list[float] = []
    tl_net288: list[float] = []
    tl_allow_long: list[bool] = []

    closes = frame["close"].to_numpy(dtype=float)

    m6a, m6b = _ts(MARCH06[0]), _ts(MARCH06[1])
    m8a, m8b = _ts(MARCH0809[0]), _ts(MARCH0809[1])
    march06_rows: list[dict[str, Any]] = []
    march0809_rows: list[dict[str, Any]] = []

    n = len(frame)
    for i in range(n):
        row = frame.iloc[i]
        decision_ts = _ts(row["decision_time"])
        prev = rt.state
        prev_age = rt.age_5m_bars
        prev_turn_keys = dict(rt.turning_evidence_keys)
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
        monthly_state.setdefault(ym, Counter())[state] += 1

        hhhl = has_hh_hl(rt.structure_5m)
        lhll = has_lh_ll(rt.structure_5m)
        # rolling nets for GT (as-of, closed)
        # index i in full frame
        c0 = float(closes[i])
        j48 = max(0, i - 48)
        j288 = max(0, i - 288)
        net48 = 100.0 * (c0 / float(closes[j48]) - 1.0) if closes[j48] else 0.0
        net288 = 100.0 * (c0 / float(closes[j288]) - 1.0) if closes[j288] else 0.0
        adx = float(row["adx"]) if "adx" in frame.columns and pd.notna(row["adx"]) else 0.0
        di = float(row["di_spread"]) if "di_spread" in frame.columns and pd.notna(row["di_spread"]) else 0.0

        tl_state.append(state)
        tl_time.append(_iso(decision_ts) or "")
        tl_ym.append(ym)
        tl_hhhl.append(bool(hhhl))
        tl_lhll.append(bool(lhll))
        tl_adx.append(adx)
        tl_di.append(di)
        tl_net48.append(net48)
        tl_net288.append(net288)
        tl_allow_long.append(bool(snap.allow_long))

        if state != prev:
            transitions.append((prev, state, decision_ts))
            reasons = list(snap.active_reasons)
            is_turn = any(
                r in {"turning_multi_bar_early_bearish", "turning_multi_bar_early_bullish"}
                for r in reasons
            ) or any(r.startswith("turning_multi_bar_") for r in reasons)
            if is_turn and prev in {"topping", "bottoming"}:
                pol = policy_for_state(state)
                turning_exits.append(
                    {
                        "variant": variant,
                        "from_state": prev,
                        "to_state": state,
                        "decision_time": _iso(decision_ts),
                        "state_age_at_exit": prev_age,
                        "evidence_cats": ",".join(sorted(prev_turn_keys.keys())),
                        "reasons": "|".join(reasons),
                        "bias_15m": rt.structure_15m.current_structure_bias,
                        "bias_30m": rt.structure_30m.current_structure_bias,
                        "consec_bearish": rt.consecutive_bearish_closes,
                        "consec_bullish": rt.consecutive_bullish_closes,
                        "last_bos": None if rt.structure_5m.last_bos is None else rt.structure_5m.last_bos.event_type,
                        "last_choch": None if rt.structure_5m.last_choch is None else rt.structure_5m.last_choch.event_type,
                        "last_high_label": rt.structure_5m.last_high_label,
                        "last_low_label": rt.structure_5m.last_low_label,
                        "protective_high": rt.structure_5m.protective_high_level,
                        "protective_low": rt.structure_5m.protective_low_level,
                        "allow_long_after": pol.allow_long,
                        "allow_short_after": pol.allow_short,
                        "close": float(row["close"]),
                        "year_month": ym,
                    }
                )

            if open_run is not None and open_run["state"] == prev:
                open_run["end"] = _iso(decision_ts)
                open_run["exit_to"] = state
                open_run["exit_reasons"] = "|".join(reasons)
                open_run["turning_exit"] = bool(is_turn)
                (runs_top if open_run["state"] == "topping" else runs_bot).append(open_run)
                open_run = None

            if state in {"topping", "bottoming"}:
                open_run = {
                    "variant": variant,
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "|".join(reasons),
                }
        elif state in {"topping", "bottoming"}:
            if open_run is None:
                open_run = {
                    "variant": variant,
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "window_start",
                }
            else:
                open_run["length_bars"] = int(open_run["length_bars"]) + 1

        # March compact rows
        if m6a <= decision_ts < m6b or m8a <= decision_ts < m8b:
            row_out = {
                "variant": variant,
                "decision_time": _iso(decision_ts),
                "state": state,
                "previous_state": prev if state != prev else snap.previous_state,
                "reasons": "|".join(snap.active_reasons),
                "turning_evidence": ",".join(sorted(rt.turning_evidence_keys.keys())),
                "allow_long": snap.allow_long,
                "allow_short": snap.allow_short,
                "close": float(row["close"]),
                "last_choch": None if rt.structure_5m.last_choch is None else rt.structure_5m.last_choch.event_type,
                "last_bos": None if rt.structure_5m.last_bos is None else rt.structure_5m.last_bos.event_type,
                "last_high_label": rt.structure_5m.last_high_label,
                "last_low_label": rt.structure_5m.last_low_label,
            }
            if m6a <= decision_ts < m6b:
                march06_rows.append(row_out)
            if m8a <= decision_ts < m8b:
                march0809_rows.append(row_out)

    if open_run is not None:
        open_run["end"] = None
        open_run["exit_to"] = open_run["state"]
        open_run["exit_reasons"] = "open_at_end"
        open_run["turning_exit"] = False
        (runs_top if open_run["state"] == "topping" else runs_bot).append(open_run)

    # GT + detection on analyze timeline
    audit_class = [AUDIT_CLASS_MAP.get(s, "UNCLEAR") for s in tl_state]
    gt = [
        ground_truth_label(
            has_hh_hl_flag=tl_hhhl[i],
            has_lh_ll_flag=tl_lhll[i],
            net_48=tl_net48[i],
            net_288=tl_net288[i],
            adx=tl_adx[i],
            di_spread=tl_di[i],
        )
        for i in range(len(tl_state))
    ]
    clear_mask = np.array([g.startswith("CLEAR_") for g in gt], dtype=bool)
    match = np.array(
        [
            (g == "CLEAR_UPTREND" and a == "UPTREND")
            or (g == "CLEAR_DOWNTREND" and a == "DOWNTREND")
            or (g == "CLEAR_SIDEWAYS" and a == "SIDEWAYS")
            for g, a in zip(gt, audit_class)
        ],
        dtype=bool,
    )
    clear_match_rate = float(match[clear_mask].mean()) if clear_mask.any() else None

    up_mask = np.array([g == "CLEAR_UPTREND" for g in gt], dtype=bool)
    dn_mask = np.array([g == "CLEAR_DOWNTREND" for g in gt], dtype=bool)
    mapped = np.array(audit_class)

    def _simple_delays(ep_mask: np.ndarray, match_mask: np.ndarray, side: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ep_id, (s, e) in enumerate(contiguous_episodes(ep_mask)):
            first = None
            for i in range(s, e + 1):
                if match_mask[i]:
                    first = i
                    break
            delay = None if first is None else first - s
            rows.append(
                {
                    "episode_id": ep_id,
                    "side": side,
                    "delay_first_match_candles": delay,
                    "missed": first is None,
                }
            )
        return rows

    delays_up = _simple_delays(up_mask, mapped == "UPTREND", "up")
    delays_dn = _simple_delays(dn_mask, mapped == "DOWNTREND", "down")

    # False transitions after turning exits
    false_rows: list[dict[str, Any]] = []
    state_arr = np.array(tl_state)
    time_index = {_ts(t): i for i, t in enumerate(tl_time)}
    for ex in turning_exits:
        ti = time_index.get(_ts(ex["decision_time"]))
        if ti is None:
            continue
        for horizon in (3, 6, 12):
            end = min(len(state_arr), ti + horizon + 1)
            window_states = state_arr[ti:end]
            flipped = False
            if ex["to_state"] == "early_bearish":
                flipped = any(s in {"early_bullish", "strong_bullish", "bullish_weakening", "bottoming"} for s in window_states[1:])
            elif ex["to_state"] == "early_bullish":
                flipped = any(s in {"early_bearish", "strong_bearish", "bearish_weakening", "topping"} for s in window_states[1:])
            hold = 0
            for s in window_states[1:]:
                if s == ex["to_state"]:
                    hold += 1
                else:
                    break
            gt_at = gt[ti] if ti < len(gt) else None
            false_rows.append(
                {
                    "variant": variant,
                    "decision_time": ex["decision_time"],
                    "from_state": ex["from_state"],
                    "to_state": ex["to_state"],
                    "horizon": horizon,
                    "flipped_within_horizon": flipped,
                    "target_hold_bars": hold,
                    "short_target_lt_horizon": hold < horizon,
                    "gt_at_exit": gt_at,
                    "exit_in_clear_sideways": gt_at == "CLEAR_SIDEWAYS",
                    "year_month": ex["year_month"],
                }
            )

    # Ping-pong A-B-A within 6/12
    ping = {"h6": 0, "h12": 0}
    for i in range(len(state_arr) - 2):
        a, b = state_arr[i], state_arr[i + 1]
        if a == b:
            continue
        for h, key in ((6, "h6"), (12, "h12")):
            if any(state_arr[j] == a for j in range(i + 2, min(len(state_arr), i + 1 + h))):
                # crude: count when return to a after leaving to b within h bars
                if b != a:
                    ping[key] += 1
                    break

    short_early = sum(
        1
        for s, nxt in zip(state_arr[:-1], state_arr[1:])
        if s in {"early_bearish", "early_bullish"} and nxt != s
    )

    return {
        "variant": variant,
        "config": cfg.to_dict(),
        "n_analyze_bars": int(sum(state_counts.values())),
        "state_counts": dict(state_counts),
        "state_shares": {k: v / max(1, sum(state_counts.values())) for k, v in state_counts.items()},
        "monthly_state": {k: dict(v) for k, v in monthly_state.items()},
        "runs_topping": runs_top,
        "runs_bottoming": runs_bot,
        "topping_stats": _run_stats([int(r["length_bars"]) for r in runs_top]),
        "bottoming_stats": _run_stats([int(r["length_bars"]) for r in runs_bot]),
        "turning_exits": turning_exits,
        "n_turning_exits": len(turning_exits),
        "clear_match_rate": clear_match_rate,
        "delays_up": delay_summary(delays_up),
        "delays_dn": delay_summary(delays_dn),
        "false_rows": false_rows,
        "ping_pong": ping,
        "short_early_leaves": short_early,
        "neutral_bars": state_counts.get("neutral", 0),
        "march06_rows": march06_rows,
        "march0809_rows": march0809_rows,
        "n_transitions": len(transitions),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def recommend(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    a, b, c = results["C2B_A_baseline"], results["C2B_B_loose_w24"], results["C2B_C_strict_w24"]
    # Acceptance checklist
    def ok(r: dict[str, Any], name: str) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        good = True
        if r["topping_stats"]["maximum"] >= a["topping_stats"]["maximum"] * 0.85 and r["topping_stats"]["median"] >= a["topping_stats"]["median"] * 0.7:
            # require clear reduction
            if r["topping_stats"]["median"] > a["topping_stats"]["median"] * 0.5:
                good = False
                reasons.append("topping_duration_not_reduced_enough")
        if r["bottoming_stats"]["median"] > a["bottoming_stats"]["median"] * 0.55:
            good = False
            reasons.append("bottoming_duration_not_reduced_enough")
        # Mar6 exit to early_bearish
        m6 = r["march06_rows"]
        exited = any(
            row["previous_state"] == "topping" and row["state"] == "early_bearish"
            for row in m6
            if row.get("previous_state") != row.get("state")
        ) or any(
            "turning_multi_bar_early_bearish" in str(row.get("reasons", "")) for row in m6
        )
        # also detect via turning_exits
        exited = exited or any(
            e["from_state"] == "topping"
            and e["to_state"] == "early_bearish"
            and str(e["decision_time"]).startswith("2026-03-06")
            for e in r["turning_exits"]
        )
        if not exited:
            good = False
            reasons.append("mar6_no_early_bearish_exit")
        # ping-pong not exploding
        if r["ping_pong"]["h6"] > a["ping_pong"]["h6"] * 2 + 50:
            good = False
            reasons.append("ping_pong_spike")
        # short early not exploding
        if r["short_early_leaves"] > a["short_early_leaves"] * 2.5 + 100:
            good = False
            reasons.append("short_early_spike")
        # clear match not collapsing vs A
        if r["clear_match_rate"] is not None and a["clear_match_rate"] is not None:
            if r["clear_match_rate"] + 0.01 < a["clear_match_rate"] * 0.85:
                good = False
                reasons.append("clear_match_worsened")
        return good, reasons

    ok_b, why_b = ok(b, "B")
    ok_c, why_c = ok(c, "C")
    if ok_c:
        choice, why = "C2B_C_strict_w24", "strict reduces stickiness with extra HTF/indicator gate; passes acceptance"
    elif ok_b:
        choice, why = "C2B_B_loose_w24", "loose reduces stickiness; strict failed: " + ",".join(why_c)
    else:
        choice, why = "no recommendation", f"B:{','.join(why_b)} C:{','.join(why_c)}"
    return {
        "recommended_research_default": choice,
        "reason": why,
        "production_default_remains": "turning_multi_bar_mode=off",
        "acceptance_B": {"ok": ok_b, "issues": why_b},
        "acceptance_C": {"ok": ok_c, "issues": why_c},
        "c2b2_neutral_still_needed": (
            max(b["topping_stats"]["maximum"], c["topping_stats"]["maximum"]) >= 96
            or b["neutral_bars"] == 0
        ),
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

    assert default_trend_state_config().turning_multi_bar_mode == "off"
    assert default_trend_state_config().weakening_multi_bar_mode == "off"

    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    a0, a1 = _ts(analyze_start), _ts(analyze_end)

    variant_specs = [
        ("C2B_A_baseline", "off", PRIMARY_WINDOW),
        ("C2B_B_loose_w24", "loose", 24),
        ("C2B_C_strict_w24", "strict", 24),
        # window sensitivity (loose only)
        ("C2B_B_loose_w12", "loose", 12),
        ("C2B_B_loose_w36", "loose", 36),
    ]

    results: dict[str, dict[str, Any]] = {}
    for name, mode, win in variant_specs:
        cfg = make_cfg(mode, win)
        print(f"replay {name} …", flush=True)
        results[name] = replay(frame, cfg=cfg, analyze_start=a0, analyze_end=a1, variant=name)

    primary = ["C2B_A_baseline", "C2B_B_loose_w24", "C2B_C_strict_w24"]
    comparison = []
    for name in primary + ["C2B_B_loose_w12", "C2B_B_loose_w36"]:
        r = results[name]
        comparison.append(
            {
                "variant": name,
                "turning_mode": r["config"]["turning_multi_bar_mode"],
                "window": r["config"]["turning_evidence_window_bars"],
                "weakening_mode": r["config"]["weakening_multi_bar_mode"],
                "topping_share": r["state_shares"].get("topping", 0),
                "bottoming_share": r["state_shares"].get("bottoming", 0),
                "early_bearish_share": r["state_shares"].get("early_bearish", 0),
                "early_bullish_share": r["state_shares"].get("early_bullish", 0),
                "strong_bearish_share": r["state_shares"].get("strong_bearish", 0),
                "strong_bullish_share": r["state_shares"].get("strong_bullish", 0),
                "neutral_share": r["state_shares"].get("neutral", 0),
                "neutral_bars": r["neutral_bars"],
                "n_turning_exits": r["n_turning_exits"],
                "topping_median": r["topping_stats"]["median"],
                "topping_max": r["topping_stats"]["maximum"],
                "topping_ge288": r["topping_stats"]["ge288"],
                "bottoming_median": r["bottoming_stats"]["median"],
                "bottoming_max": r["bottoming_stats"]["maximum"],
                "bottoming_ge288": r["bottoming_stats"]["ge288"],
                "clear_match_rate": r["clear_match_rate"],
                "delay_up_median": (r["delays_up"] or {}).get("median"),
                "delay_dn_median": (r["delays_dn"] or {}).get("median"),
                "ping_pong_h6": r["ping_pong"]["h6"],
                "short_early_leaves": r["short_early_leaves"],
            }
        )

    # state distribution long form
    dist_rows = []
    for name in primary:
        r = results[name]
        for st, cnt in sorted(r["state_counts"].items()):
            dist_rows.append(
                {
                    "variant": name,
                    "state": st,
                    "count": cnt,
                    "share": r["state_shares"].get(st, 0),
                }
            )

    # monthly stability
    monthly_rows = []
    for name in primary:
        r = results[name]
        for ym, ctr in sorted(r["monthly_state"].items()):
            total = sum(ctr.values())
            monthly_rows.append(
                {
                    "variant": name,
                    "year_month": ym,
                    "n_bars": total,
                    "topping": ctr.get("topping", 0),
                    "bottoming": ctr.get("bottoming", 0),
                    "early_bearish": ctr.get("early_bearish", 0),
                    "early_bullish": ctr.get("early_bullish", 0),
                    "neutral": ctr.get("neutral", 0),
                    "n_turning_exits": sum(
                        1 for e in r["turning_exits"] if e["year_month"] == ym
                    ),
                }
            )

    trend_rows = []
    for name in primary:
        r = results[name]
        trend_rows.append(
            {
                "variant": name,
                "clear_match_rate": r["clear_match_rate"],
                "up_episodes": r["delays_up"].get("n_episodes"),
                "up_missed": r["delays_up"].get("n_missed"),
                "up_delay_median": r["delays_up"].get("median"),
                "up_delay_p75": r["delays_up"].get("p75"),
                "up_delay_p90": r["delays_up"].get("p90"),
                "dn_episodes": r["delays_dn"].get("n_episodes"),
                "dn_missed": r["delays_dn"].get("n_missed"),
                "dn_delay_median": r["delays_dn"].get("median"),
                "dn_delay_p75": r["delays_dn"].get("p75"),
                "dn_delay_p90": r["delays_dn"].get("p90"),
            }
        )

    remaining = []
    for name in primary:
        r = results[name]
        for run in r["runs_topping"] + r["runs_bottoming"]:
            if int(run["length_bars"]) >= 96:
                remaining.append(run)

    # Mar6 narrative
    ref = _ts(CAUSAL_MAR6_REF)
    mar6_summary = {}
    for name in primary:
        r = results[name]
        exits = [
            e
            for e in r["turning_exits"]
            if str(e["decision_time"]).startswith("2026-03-06") and e["from_state"] == "topping"
        ]
        first = exits[0] if exits else None
        delay = None
        if first:
            delay = int(round((_ts(first["decision_time"]) - ref) / pd.Timedelta(minutes=5)))
        # first early/strong bearish on mar6
        early_rows = [row for row in r["march06_rows"] if row["state"] in {"early_bearish", "strong_bearish"}]
        mar6_summary[name] = {
            "first_turning_exit": first,
            "delay_vs_0505_bars": delay,
            "first_early_or_strong_bearish": early_rows[0] if early_rows else None,
            "topping_bars": sum(1 for row in r["march06_rows"] if row["state"] == "topping"),
        }

    rec = recommend(results)

    write_csv(output_dir / "variant_comparison.csv", comparison)
    write_csv(output_dir / "state_distribution.csv", dist_rows)
    write_csv(
        output_dir / "topping_runs.csv",
        [r for name in primary for r in results[name]["runs_topping"]],
    )
    write_csv(
        output_dir / "bottoming_runs.csv",
        [r for name in primary for r in results[name]["runs_bottoming"]],
    )
    write_csv(
        output_dir / "turning_state_exits.csv",
        [e for name in primary for e in results[name]["turning_exits"]],
    )
    # evidence timelines = exit catalog compact
    write_csv(
        output_dir / "evidence_timelines.csv",
        [
            {
                "variant": e["variant"],
                "decision_time": e["decision_time"],
                "from_state": e["from_state"],
                "to_state": e["to_state"],
                "evidence_cats": e["evidence_cats"],
                "state_age_at_exit": e["state_age_at_exit"],
                "last_bos": e["last_bos"],
                "last_choch": e["last_choch"],
                "reasons": e["reasons"],
            }
            for name in primary
            for e in results[name]["turning_exits"]
        ],
    )
    write_csv(output_dir / "trend_detection_comparison.csv", trend_rows)
    write_csv(
        output_dir / "false_transition_analysis.csv",
        [f for name in primary for f in results[name]["false_rows"]],
    )
    write_csv(output_dir / "monthly_stability.csv", monthly_rows)
    write_csv(
        output_dir / "march_06_comparison.csv",
        [row for name in primary for row in results[name]["march06_rows"]],
    )
    write_csv(
        output_dir / "march_08_09_comparison.csv",
        [row for name in primary for row in results[name]["march0809_rows"]],
    )
    write_csv(output_dir / "remaining_stuck_runs.csv", remaining)

    (output_dir / "code_audit.json").write_text(
        json.dumps(json_safe(CODE_AUDIT), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary = {
        "phase": "C2B1_turning_multi_bar",
        "symbol": symbol,
        "primary_window_bars": PRIMARY_WINDOW,
        "windows_tested": list(WINDOWS_SENSITIVITY),
        "c1_strict_enabled": True,
        "production_defaults_off": True,
        "n_load_bars": int(len(frame)),
        "comparison": comparison,
        "mar6": mar6_summary,
        "recommendation": rec,
        "code_audit": CODE_AUDIT,
        "safety": {
            "no_neutral_timeout": True,
            "no_policy_change": True,
            "did_not_write_forbidden_dirs": True,
        },
    }
    blob = json.dumps(json_safe(summary), sort_keys=True, separators=(",", ":"))
    summary["deterministic_hash"] = hashlib.sha256(blob.encode()).hexdigest()
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README_results.md").write_text(
        f"""# Phase C2B1 — Topping/Bottoming multi-bar turning exits

C1-C strict + `turning_multi_bar_mode` ∈ {{off, loose, strict}}.

Primary window: **{PRIMARY_WINDOW}** bars (also sensitivity 12/36 for loose).

## Recommendation
`{rec['recommended_research_default']}` — {rec['reason']}

Production default remains **`turning_multi_bar_mode=off`**.

## Neutral
No →neutral timeout in C2B1. C2B2 still needed?: {rec['c2b2_neutral_still_needed']}
""",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase C2B1 turning multi-bar audit")
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
                "recommendation": summary["recommendation"],
                "comparison": [
                    {
                        k: r[k]
                        for k in (
                            "variant",
                            "topping_median",
                            "topping_max",
                            "bottoming_median",
                            "bottoming_max",
                            "n_turning_exits",
                            "clear_match_rate",
                            "neutral_bars",
                        )
                    }
                    for r in summary["comparison"]
                    if r["variant"].startswith("C2B_") and "w12" not in r["variant"] and "w36" not in r["variant"] or r["variant"] in {
                        "C2B_A_baseline",
                        "C2B_B_loose_w24",
                        "C2B_C_strict_w24",
                    }
                ],
                "mar6": summary["mar6"],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
