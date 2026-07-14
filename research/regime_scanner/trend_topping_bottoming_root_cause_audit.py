"""Phase C2A: read-only root-cause audit for sticky topping/bottoming (research-only).

Replays the existing state machine with C1-C strict multi-bar weakening
(`weakening_multi_bar_mode=strict`). Does **not** modify transitions, policy,
thresholds, live bots, or Phase-B/C0/C1 result directories.

Diagnostic counterfactuals (audit-only, never wired into production SM):
  CF0 existing same-bar / label gates as in `_propose_transition`
  CF1 persisted last_bos / last_choch may satisfy BOS/CHoCH legs
  CF2 multi-bar structure sequence (LH+BOS etc. across closed bars)
  CF3 neutral fallback after long absence of directional confirmation

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_topping_bottoming_root_cause_audit \\
    --symbol APTUSDT \\
    --output-dir research/regime_scanner/results_trend_topping_bottoming_phase_c2a
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from collections import Counter
from dataclasses import dataclass, field
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
    install_htf_cache,
    load_analysis_frame,
)
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    TrendStateConfig,
    _can_leave,
    _event_types,
    _htf_bias,
    _htf_veto_strong_bearish,
    _indicator_confirms,
    default_trend_state_config,
    step_trend_state,
    trend_state_config_c1,
)
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    StructureEvent,
    has_hh_hl,
    has_lh_ll,
)

DEFAULT_OUT = Path("research/regime_scanner/results_trend_topping_bottoming_phase_c2a")
FORBIDDEN_OVERWRITE = (
    Path("research/regime_scanner/results"),
    Path("research/regime_scanner/results_trend_robustness_phase_b"),
    Path("research/regime_scanner/results_trend_mapping_root_cause_phase_c0"),
    Path("research/regime_scanner/results_trend_weakening_multi_bar_phase_c1"),
)

LONG_RUN_MIN_BARS = 24
NEUTRAL_TIMEOUT_BARS = 48  # diagnostic CF3 only
MULTI_BAR_WINDOW = 36  # diagnostic CF2 only

MARCH06_START = "2026-03-06T00:00:00+00:00"
MARCH06_END = "2026-03-07T00:00:00+00:00"
MARCH08_START = "2026-03-08T00:00:00+00:00"
MARCH10_END = "2026-03-10T00:00:00+00:00"

# Frozen code-audit narrative (maps 1:1 to trend_state_machine._propose_transition).
CODE_AUDIT: dict[str, Any] = {
    "phase": "C2A_topping_bottoming_code_audit",
    "file": "research/regime_scanner/trend_state_machine.py",
    "function": "_propose_transition",
    "conditions": [
        {
            "id": "topping_to_early_bearish",
            "allows": "topping → early_bearish",
            "if": (
                "(lower_high in types OR s5.last_high_label == 'lower_high') "
                "AND (bearish_bos in types OR bearish_choch in types) "
                "AND (consecutive_bearish_closes >= bearish_impulse_min_closes OR bear_conf >= 2)"
            ),
            "events_needed": ["lower_high (same-bar OR persisted label)", "bearish_bos|bearish_choch (SAME BAR)"],
            "scope": "mixed: LH label may be persisted; BOS/CHoCH current-bar only; impulse/indicator current",
            "effect": "Only productive exit from topping toward bearish early trend",
        },
        {
            "id": "topping_to_neutral",
            "allows": "topping → neutral",
            "if": "NONE — no branch exists",
            "events_needed": [],
            "scope": "n/a",
            "effect": "neutral is unreachable from topping",
        },
        {
            "id": "topping_false_top",
            "allows": "topping → early_bullish",
            "if": "higher_high in types AND (bullish_bos OR bullish_choch) in types",
            "events_needed": ["higher_high", "bullish_bos|bullish_choch"],
            "scope": "same-bar only",
            "effect": "Abort topping back to bullish early",
        },
        {
            "id": "topping_hold",
            "allows": "hold topping",
            "if": "min_hold_topping OR exit conjunction incomplete OR impulse/indicator missing",
            "events_needed": [],
            "scope": "current evaluation",
            "effect": "Sticky topping when BOS/CHoCH rare on same bar as LH satisfaction",
        },
        {
            "id": "bottoming_to_early_bullish",
            "allows": "bottoming → early_bullish",
            "if": (
                "(higher_low in types OR last_low_label == higher_low OR has_hh_hl(s5)) "
                "AND (bullish_bos OR bullish_choch in types) "
                "AND NOT (15m_bearish AND bearish_bos same-bar) "
                "AND NOT (htf_veto_strong_bearish AND bullish_bos missing) "
                "AND (consec_bullish >= bullish_impulse_min_closes OR bull_conf >= 2)"
            ),
            "events_needed": ["HL/HHHL (mixed)", "bullish_bos|choch SAME BAR", "impulse/indicator"],
            "scope": "mixed + HTF gates",
            "effect": "Only productive exit from bottoming toward bullish early",
        },
        {
            "id": "bottoming_to_neutral",
            "allows": "bottoming → neutral",
            "if": "NONE — no branch exists",
            "events_needed": [],
            "scope": "n/a",
            "effect": "neutral unreachable from bottoming",
        },
        {
            "id": "bottoming_false_bottom",
            "allows": "bottoming → early_bearish",
            "if": "lower_low in types AND (bearish_bos OR bearish_choch) in types",
            "events_needed": ["lower_low", "bearish_bos|choch"],
            "scope": "same-bar only",
            "effect": "Abort bottoming back to bearish early",
        },
        {
            "id": "neutral_entry_sources",
            "allows": "→ neutral",
            "if": "only bearish_warning/bullish_warning invalidation paths",
            "events_needed": ["opposite structure + opposite impulse exits"],
            "scope": "warning states only",
            "effect": "After warmup neutral→warning quickly; topping/bottoming cannot return to neutral",
        },
        {
            "id": "unused_persisted",
            "allows": "n/a",
            "if": "last_bos / last_choch / protective levels unused for topping/bottoming exits",
            "events_needed": [],
            "scope": "persisted but unread for exit conjunction BOS/CHoCH leg",
            "effect": "Same-candle BOS/CHoCH requirement recreates the C0/C1 stuck pattern one layer later",
        },
    ],
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def assert_safe_output_dir(path: Path) -> None:
    resolved = path.resolve()
    for forbidden in FORBIDDEN_OVERWRITE:
        if resolved == forbidden.resolve():
            raise ValueError(f"refusing to write into forbidden path: {forbidden}")


def c1_strict_config() -> TrendStateConfig:
    """Recommended research config: C1-C strict; production default remains off elsewhere."""
    cfg = trend_state_config_c1("strict")
    assert cfg.weakening_multi_bar_mode == "strict"
    assert default_trend_state_config().weakening_multi_bar_mode == "off"
    return cfg


def _event_type(ev: StructureEvent | None) -> str | None:
    return None if ev is None else str(ev.event_type)


@dataclass
class TbEvidence:
    """Audit-only multi-bar ledger for topping/bottoming (CF2). Cleared on leave."""

    cats: dict[str, str] = field(default_factory=dict)
    seen_age: dict[str, int] = field(default_factory=dict)

    def clear(self) -> None:
        self.cats.clear()
        self.seen_age.clear()

    def update(self, *, state: str, types: set[str], age: int, window: int = MULTI_BAR_WINDOW) -> None:
        if state == "topping":
            allowed = {"lower_high", "bearish_bos", "bearish_choch", "failed_breakout"}
            reset = "higher_high" in types
        elif state == "bottoming":
            allowed = {"higher_low", "bullish_bos", "bullish_choch", "failed_breakdown"}
            reset = "lower_low" in types
        else:
            self.clear()
            return
        if reset:
            self.clear()
            return
        for cat, seen in list(self.seen_age.items()):
            if age - int(seen) > window:
                self.cats.pop(cat, None)
                self.seen_age.pop(cat, None)
        for t in types & allowed:
            if t not in self.cats:
                self.cats[t] = t
                self.seen_age[t] = age


def diagnose_topping_exit(
    rt: TrendRuntime,
    *,
    types: set[str],
    row: dict[str, Any],
    cfg: TrendStateConfig,
    evidence: TbEvidence,
) -> dict[str, Any]:
    """Mirror topping branch + audit-only counterfactuals."""
    s5 = rt.structure_5m
    missing: list[str] = []
    target = "early_bearish"

    if not _can_leave(rt, cfg):
        return {
            "possible_target": target,
            "would_exit_existing": False,
            "block_reasons": ["min_hold_topping"],
            "cf1_persist_would_exit": False,
            "cf2_multibar_would_exit": False,
            "cf3_neutral_timeout_would_exit": rt.age_5m_bars >= NEUTRAL_TIMEOUT_BARS,
            "missing": ["min_hold"],
        }

    if "higher_high" in types and ("bullish_bos" in types or "bullish_choch" in types):
        return {
            "possible_target": "early_bullish",
            "would_exit_existing": True,
            "block_reasons": [],
            "cf1_persist_would_exit": True,
            "cf2_multibar_would_exit": True,
            "cf3_neutral_timeout_would_exit": False,
            "missing": [],
            "exit_kind": "false_top",
        }

    lh_ok = "lower_high" in types or s5.last_high_label == "lower_high"
    bos_same = "bearish_bos" in types or "bearish_choch" in types
    last_bos = _event_type(s5.last_bos)
    last_choch = _event_type(s5.last_choch)
    bos_persist = last_bos == "bearish_bos" or last_choch == "bearish_choch"
    bear_conf, _ = _indicator_confirms(row, side="bearish", cfg=cfg)
    impulse_ok = rt.consecutive_bearish_closes >= int(cfg.bearish_impulse_min_closes) or bear_conf >= 2

    if not lh_ok:
        missing.append("lower_high_or_label")
    if not bos_same:
        missing.append("bearish_bos_or_choch_same_bar")
    if not impulse_ok:
        missing.append("bearish_impulse_or_indicator")

    existing_ok = lh_ok and bos_same and impulse_ok
    cf1_ok = lh_ok and (bos_same or bos_persist) and impulse_ok

    cats = set(evidence.cats.keys())
    # require swing + hard structure across bars
    cf2_struct = len(cats & {"lower_high", "bearish_bos", "bearish_choch"}) >= 2 and bool(
        cats & {"bearish_bos", "bearish_choch"}
    )
    cf2_ok = cf2_struct and impulse_ok and "higher_high" not in types

    return {
        "possible_target": target,
        "would_exit_existing": existing_ok,
        "block_reasons": missing if not existing_ok else [],
        "cf1_persist_would_exit": cf1_ok,
        "cf2_multibar_would_exit": cf2_ok,
        "cf3_neutral_timeout_would_exit": (not existing_ok) and rt.age_5m_bars >= NEUTRAL_TIMEOUT_BARS,
        "missing": missing,
        "lh_ok": lh_ok,
        "bos_same_bar": bos_same,
        "bos_persist": bos_persist,
        "impulse_ok": impulse_ok,
        "last_bos": last_bos,
        "last_choch": last_choch,
        "evidence_cats": ",".join(sorted(cats)),
        "htf_15m": _htf_bias(rt.structure_15m),
        "htf_30m": _htf_bias(rt.structure_30m),
    }


def diagnose_bottoming_exit(
    rt: TrendRuntime,
    *,
    types: set[str],
    row: dict[str, Any],
    cfg: TrendStateConfig,
    evidence: TbEvidence,
) -> dict[str, Any]:
    s5 = rt.structure_5m
    s15 = rt.structure_15m
    s30 = rt.structure_30m
    missing: list[str] = []
    target = "early_bullish"

    if not _can_leave(rt, cfg):
        return {
            "possible_target": target,
            "would_exit_existing": False,
            "block_reasons": ["min_hold_bottoming"],
            "cf1_persist_would_exit": False,
            "cf2_multibar_would_exit": False,
            "cf3_neutral_timeout_would_exit": rt.age_5m_bars >= NEUTRAL_TIMEOUT_BARS,
            "missing": ["min_hold"],
        }

    if "lower_low" in types and ("bearish_bos" in types or "bearish_choch" in types):
        return {
            "possible_target": "early_bearish",
            "would_exit_existing": True,
            "block_reasons": [],
            "cf1_persist_would_exit": True,
            "cf2_multibar_would_exit": True,
            "cf3_neutral_timeout_would_exit": False,
            "missing": [],
            "exit_kind": "false_bottom",
        }

    hl_ok = (
        "higher_low" in types
        or s5.last_low_label == "higher_low"
        or has_hh_hl(s5)
    )
    bos_same = "bullish_bos" in types or "bullish_choch" in types
    last_bos = _event_type(s5.last_bos)
    last_choch = _event_type(s5.last_choch)
    bos_persist = last_bos == "bullish_bos" or last_choch == "bullish_choch"
    bull_conf, _ = _indicator_confirms(row, side="bullish", cfg=cfg)
    impulse_ok = rt.consecutive_bullish_closes >= int(cfg.bullish_impulse_min_closes) or bull_conf >= 2

    gate_block: list[str] = []
    if hl_ok and bos_same:
        if _htf_bias(s15) == "bearish" and "bearish_bos" in types:
            gate_block.append("15m_bearish_bos_blocks_early")
        if _htf_veto_strong_bearish(s15, s30) and "bullish_bos" not in types:
            gate_block.append("30m_15m_bearish_veto_early")

    if not hl_ok:
        missing.append("higher_low_or_hhhl")
    if not bos_same:
        missing.append("bullish_bos_or_choch_same_bar")
    if not impulse_ok:
        missing.append("bullish_impulse_or_indicator")
    missing.extend(gate_block)

    existing_ok = hl_ok and bos_same and impulse_ok and not gate_block
    cf1_struct = hl_ok and (bos_same or bos_persist)
    cf1_gates = []
    if cf1_struct:
        if _htf_bias(s15) == "bearish" and ("bearish_bos" in types or last_bos == "bearish_bos"):
            # keep same semantics: same-bar bearish_bos only for this gate
            if "bearish_bos" in types:
                cf1_gates.append("15m_bearish_bos_blocks_early")
        if _htf_veto_strong_bearish(s15, s30) and not (
            "bullish_bos" in types or last_bos == "bullish_bos"
        ):
            cf1_gates.append("30m_15m_bearish_veto_early")
    cf1_ok = cf1_struct and impulse_ok and not cf1_gates

    cats = set(evidence.cats.keys())
    cf2_struct = len(cats & {"higher_low", "bullish_bos", "bullish_choch"}) >= 2 and bool(
        cats & {"bullish_bos", "bullish_choch"}
    )
    cf2_ok = cf2_struct and impulse_ok and "lower_low" not in types

    return {
        "possible_target": target,
        "would_exit_existing": existing_ok,
        "block_reasons": missing if not existing_ok else [],
        "cf1_persist_would_exit": cf1_ok,
        "cf2_multibar_would_exit": cf2_ok,
        "cf3_neutral_timeout_would_exit": (not existing_ok) and rt.age_5m_bars >= NEUTRAL_TIMEOUT_BARS,
        "missing": missing,
        "hl_ok": hl_ok,
        "bos_same_bar": bos_same,
        "bos_persist": bos_persist,
        "impulse_ok": impulse_ok,
        "last_bos": last_bos,
        "last_choch": last_choch,
        "evidence_cats": ",".join(sorted(cats)),
        "htf_15m": _htf_bias(s15),
        "htf_30m": _htf_bias(s30),
        "gate_blocks": "|".join(gate_block),
    }


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


def state_machine_source_unchanged_for_topping_paths() -> bool:
    """Guard: audit module must not be the SM; SM topping block still has no →neutral."""
    import research.regime_scanner.trend_state_machine as m

    src = inspect.getsource(m._propose_transition)
    # topping→neutral never appears
    topping_idx = src.find('if state == "topping"')
    bottom_idx = src.find('if state == "bottoming"')
    early_idx = src.find('if state == "early_bearish"')
    topping_src = src[topping_idx:early_idx] if topping_idx >= 0 else ""
    return "return \"neutral\"" not in topping_src and "→ neutral" not in topping_src


def replay_c1_strict(
    frame: pd.DataFrame,
    *,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    cfg: TrendStateConfig,
) -> dict[str, Any]:
    end_decision = _ts(frame["decision_time"].iloc[-1])
    install_htf_cache(frame, end_decision)
    scfg = default_regime_scanner_config().with_timeframe("5m")
    pivots = find_confirmed_pivots(frame, config=scfg)
    rt = TrendRuntime()
    evidence = TbEvidence()

    state_counts: Counter[str] = Counter()
    monthly_state: dict[str, Counter[str]] = {}
    transition_from_to: Counter[str] = Counter()
    neutral_entries: list[dict[str, Any]] = []

    runs_top: list[dict[str, Any]] = []
    runs_bot: list[dict[str, Any]] = []
    open_run: dict[str, Any] | None = None

    block_reason_counts: Counter[str] = Counter()
    long_run_candle_rows: list[dict[str, Any]] = []  # only while in long open run
    march06_rows: list[dict[str, Any]] = []
    march0809_rows: list[dict[str, Any]] = []

    cf_would: Counter[str] = Counter()

    # Precompute ohlcv columns for as_of slices
    ohlcv_cols = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]

    m6a, m6b = _ts(MARCH06_START), _ts(MARCH06_END)
    m8a, m8b = _ts(MARCH08_START), _ts(MARCH10_END)

    n = len(frame)
    for i in range(n):
        row_s = frame.iloc[i]
        decision_ts = _ts(row_s["decision_time"])
        candles_as_of = frame.iloc[: i + 1][ohlcv_cols]
        prev = rt.state
        prev_age = rt.age_5m_bars
        prev_reasons_placeholder = None  # filled after step

        # Capture structure snapshot before step for entry attribution after transition
        pre_last_bos = _event_type(rt.structure_5m.last_bos)
        pre_last_choch = _event_type(rt.structure_5m.last_choch)
        pre_evidence = ",".join(sorted(rt.weakening_evidence_keys.keys()))

        rt, snap, events = step_trend_state(
            rt,
            candle_row=row_s,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=candles_as_of,
            bar_index=i,
            cfg=cfg,
            scanner_cfg=scfg,
        )
        types = _event_types(events) if events else set()
        # Prefer 5m event types for diagnosis (step returns 5m+htf combined in `events`,
        # but propose uses 5m only). Recompute from active_structure_events if needed.
        types_5m = {
            str(e.get("event_type"))
            for e in (snap.active_structure_events or [])
            if isinstance(e, dict) and e.get("event_type")
        } or types

        in_window = analyze_start <= decision_ts <= analyze_end
        if not in_window:
            # still maintain evidence lifecycle outside window for causality continuity
            if snap.current_state in {"topping", "bottoming"}:
                evidence.update(state=snap.current_state, types=types_5m, age=rt.age_5m_bars)
            else:
                evidence.clear()
            continue

        state = snap.current_state
        state_counts[state] += 1
        ym = f"{decision_ts.year:04d}-{decision_ts.month:02d}"
        monthly_state.setdefault(ym, Counter())[state] += 1

        row_dict = row_s.to_dict() if hasattr(row_s, "to_dict") else dict(row_s)

        # Close/open runs on transition
        if state != prev:
            transition_from_to[f"{prev}->{state}"] += 1
            if state == "neutral":
                neutral_entries.append(
                    {
                        "decision_time": _iso(decision_ts),
                        "from_state": prev,
                        "reasons": "|".join(snap.active_reasons),
                        "close": float(row_s["close"]),
                    }
                )
            if open_run is not None and open_run["state"] == prev:
                open_run["end"] = _iso(decision_ts)
                open_run["exit_to"] = state
                open_run["exit_reasons"] = "|".join(snap.active_reasons)
                open_run["length_bars"] = int(open_run["length_bars"])
                (runs_top if open_run["state"] == "topping" else runs_bot).append(open_run)
                open_run = None

            if state in {"topping", "bottoming"}:
                evidence.clear()
                evidence.update(state=state, types=types_5m, age=0)
                open_run = {
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "|".join(snap.active_reasons),
                    "entry_weakening_evidence": pre_evidence,
                    "entry_last_bos": pre_last_bos or _event_type(rt.structure_5m.last_bos),
                    "entry_last_choch": pre_last_choch or _event_type(rt.structure_5m.last_choch),
                    "entry_last_high_label": rt.structure_5m.last_high_label,
                    "entry_last_low_label": rt.structure_5m.last_low_label,
                    "entry_protective_high": rt.structure_5m.protective_high_level,
                    "entry_protective_low": rt.structure_5m.protective_low_level,
                    "entry_bias_15m": _htf_bias(rt.structure_15m),
                    "entry_bias_30m": _htf_bias(rt.structure_30m),
                    "entry_close": float(row_s["close"]),
                    "max_age": 0,
                    "block_reason_hist": Counter(),
                    "cf1_would_bars": 0,
                    "cf2_would_bars": 0,
                    "cf3_would_bars": 0,
                }
            else:
                evidence.clear()
        elif state in {"topping", "bottoming"}:
            evidence.update(state=state, types=types_5m, age=rt.age_5m_bars)
            if open_run is None:
                open_run = {
                    "state": state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "already_in_state_at_window_start",
                    "entry_weakening_evidence": "",
                    "entry_last_bos": _event_type(rt.structure_5m.last_bos),
                    "entry_last_choch": _event_type(rt.structure_5m.last_choch),
                    "entry_last_high_label": rt.structure_5m.last_high_label,
                    "entry_last_low_label": rt.structure_5m.last_low_label,
                    "entry_protective_high": rt.structure_5m.protective_high_level,
                    "entry_protective_low": rt.structure_5m.protective_low_level,
                    "entry_bias_15m": _htf_bias(rt.structure_15m),
                    "entry_bias_30m": _htf_bias(rt.structure_30m),
                    "entry_close": float(row_s["close"]),
                    "max_age": rt.age_5m_bars,
                    "block_reason_hist": Counter(),
                    "cf1_would_bars": 0,
                    "cf2_would_bars": 0,
                    "cf3_would_bars": 0,
                }
            else:
                open_run["length_bars"] = int(open_run["length_bars"]) + 1
                open_run["max_age"] = max(int(open_run.get("max_age", 0)), rt.age_5m_bars)

            # Diagnose hold while staying in TB
            if state == "topping":
                diag = diagnose_topping_exit(
                    rt, types=types_5m, row=row_dict, cfg=cfg, evidence=evidence
                )
            else:
                diag = diagnose_bottoming_exit(
                    rt, types=types_5m, row=row_dict, cfg=cfg, evidence=evidence
                )

            for br in diag.get("block_reasons") or ["hold_unknown"]:
                block_reason_counts[f"{state}:{br}"] += 1
                open_run["block_reason_hist"][br] += 1
            if diag.get("cf1_persist_would_exit"):
                open_run["cf1_would_bars"] += 1
                cf_would["cf1"] += 1
            if diag.get("cf2_multibar_would_exit"):
                open_run["cf2_would_bars"] += 1
                cf_would["cf2"] += 1
            if diag.get("cf3_neutral_timeout_would_exit"):
                open_run["cf3_would_bars"] += 1
                cf_would["cf3"] += 1

            # Per-candle rows for long runs + March windows
            is_long = int(open_run["length_bars"]) >= LONG_RUN_MIN_BARS
            in_m6 = m6a <= decision_ts < m6b
            in_m89 = m8a <= decision_ts < m8b
            if is_long or in_m6 or in_m89:
                candle_row = {
                    "decision_time": _iso(decision_ts),
                    "state": state,
                    "age": rt.age_5m_bars,
                    "run_length_so_far": open_run["length_bars"],
                    "close": float(row_s["close"]),
                    "possible_target": diag.get("possible_target"),
                    "block_reasons": "|".join(diag.get("block_reasons") or []),
                    "types_5m": ",".join(sorted(types_5m)),
                    "lh_or_hl_ok": diag.get("lh_ok", diag.get("hl_ok")),
                    "bos_same_bar": diag.get("bos_same_bar"),
                    "bos_persist": diag.get("bos_persist"),
                    "impulse_ok": diag.get("impulse_ok"),
                    "last_bos": diag.get("last_bos"),
                    "last_choch": diag.get("last_choch"),
                    "evidence_cats": diag.get("evidence_cats"),
                    "htf_15m": diag.get("htf_15m"),
                    "htf_30m": diag.get("htf_30m"),
                    "transition_reasons": "|".join(snap.active_reasons),
                    "cf1_would": bool(diag.get("cf1_persist_would_exit")),
                    "cf2_would": bool(diag.get("cf2_multibar_would_exit")),
                    "cf3_would": bool(diag.get("cf3_neutral_timeout_would_exit")),
                    "consec_bearish": rt.consecutive_bearish_closes,
                    "consec_bullish": rt.consecutive_bullish_closes,
                    "last_high_label": rt.structure_5m.last_high_label,
                    "last_low_label": rt.structure_5m.last_low_label,
                    "protective_high": rt.structure_5m.protective_high_level,
                    "protective_low": rt.structure_5m.protective_low_level,
                }
                if is_long:
                    long_run_candle_rows.append(candle_row)
                if in_m6:
                    march06_rows.append(candle_row)
                if in_m89:
                    march0809_rows.append(candle_row)
        else:
            evidence.clear()

    if open_run is not None:
        open_run["end"] = None
        open_run["exit_to"] = open_run["state"]
        open_run["exit_reasons"] = "still_open_at_analyze_end"
        (runs_top if open_run["state"] == "topping" else runs_bot).append(open_run)

    return {
        "state_counts": dict(state_counts),
        "monthly_state": {k: dict(v) for k, v in monthly_state.items()},
        "transition_from_to": dict(transition_from_to),
        "neutral_entries": neutral_entries,
        "runs_topping": runs_top,
        "runs_bottoming": runs_bot,
        "block_reason_counts": dict(block_reason_counts),
        "long_run_candle_rows": long_run_candle_rows,
        "march06_rows": march06_rows,
        "march0809_rows": march0809_rows,
        "cf_would_bars": dict(cf_would),
        "n_analyze_bars": int(sum(state_counts.values())),
        "config": cfg.to_dict(),
    }


def _serialize_run(run: dict[str, Any]) -> dict[str, Any]:
    hist = run.get("block_reason_hist") or Counter()
    if isinstance(hist, Counter):
        top_blocks = "|".join(f"{k}:{v}" for k, v in hist.most_common(8))
    else:
        top_blocks = ""
    out = {k: v for k, v in run.items() if k != "block_reason_hist"}
    out["top_block_reasons"] = top_blocks
    out["is_long_ge24"] = int(run.get("length_bars", 0)) >= 24
    return out


def build_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    top_lens = [int(r["length_bars"]) for r in payload["runs_topping"]]
    bot_lens = [int(r["length_bars"]) for r in payload["runs_bottoming"]]
    ts = _run_stats(top_lens)
    bs = _run_stats(bot_lens)
    blocks = Counter(payload["block_reason_counts"])
    top_blocks = blocks.most_common(6)
    n_neutral = payload["state_counts"].get("neutral", 0)
    n_neu_entries = len(payload["neutral_entries"])
    findings = [
        {
            "id": "F1",
            "severity": "critical",
            "claim": "topping/bottoming have no path to neutral",
            "evidence": "CODE_AUDIT topping_to_neutral / bottoming_to_neutral = NONE",
        },
        {
            "id": "F2",
            "severity": "critical",
            "claim": "BOS/CHoCH leg for topping→early_bearish and bottoming→early_bullish is same-bar only",
            "evidence": "persisted last_bos/last_choch unused; mirrors C1 stuck pattern",
        },
        {
            "id": "F3",
            "severity": "result",
            "claim": "Long TB runs dominate after C1-C weakening exits",
            "evidence": (
                f"topping max={ts['maximum']} ge24={ts['ge24']} median={ts['median']}; "
                f"bottoming max={bs['maximum']} ge24={bs['ge24']} median={bs['median']}"
            ),
        },
        {
            "id": "F4",
            "severity": "result",
            "claim": "Most frequent exit blockers",
            "evidence": "; ".join(f"{k}={v}" for k, v in top_blocks),
        },
        {
            "id": "F5",
            "severity": "result",
            "claim": "neutral nearly absent in analyze window under C1-C replay",
            "evidence": f"neutral_bars={n_neutral} neutral_entries={n_neu_entries} / {payload['n_analyze_bars']}",
        },
        {
            "id": "F6",
            "severity": "diagnostic",
            "claim": "CF1/CF2 would unlock many sticky bars without March hardcodes",
            "evidence": str(payload.get("cf_would_bars")),
        },
        {
            "id": "F7",
            "severity": "proposal",
            "claim": "Smallest C2B root-fix: multi-bar or persisted BOS/CHoCH for TB exits (mirror C1), optional long-age → neutral diagnostic next",
            "evidence": "Do not change policy in C2B; keep default off until validated",
        },
    ]
    return findings


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


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

    cfg = c1_strict_config()
    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    payload = replay_c1_strict(
        frame,
        analyze_start=_ts(analyze_start),
        analyze_end=_ts(analyze_end),
        cfg=cfg,
    )

    top_runs = [_serialize_run(r) for r in payload["runs_topping"]]
    bot_runs = [_serialize_run(r) for r in payload["runs_bottoming"]]
    long_top = [r for r in top_runs if r["is_long_ge24"]]
    long_bot = [r for r in bot_runs if r["is_long_ge24"]]

    # Distributions overall + monthly
    dist_rows: list[dict[str, Any]] = []
    for label, runs in (("topping", payload["runs_topping"]), ("bottoming", payload["runs_bottoming"])):
        by_month: dict[str, list[int]] = {}
        for r in runs:
            by_month.setdefault(str(r.get("year_month")), []).append(int(r["length_bars"]))
        all_lens = [int(r["length_bars"]) for r in runs]
        stats = _run_stats(all_lens)
        dist_rows.append({"scope": "all", "state": label, "year_month": "", **stats})
        for ym, lens in sorted(by_month.items()):
            dist_rows.append({"scope": "month", "state": label, "year_month": ym, **_run_stats(lens)})

    block_rows = [
        {"block_key": k, "count": v}
        for k, v in sorted(payload["block_reason_counts"].items(), key=lambda kv: -kv[1])
    ]

    # Neutral reachability
    neu_from = Counter(e["from_state"] for e in payload["neutral_entries"])
    neutral_rows = [
        {
            "metric": "neutral_bars",
            "value": payload["state_counts"].get("neutral", 0),
            "note": "bars spent in neutral during analyze window",
        },
        {
            "metric": "neutral_entries",
            "value": len(payload["neutral_entries"]),
            "note": "transitions into neutral",
        },
        {
            "metric": "neutral_from_topping",
            "value": neu_from.get("topping", 0),
            "note": "must be 0 — no SM path",
        },
        {
            "metric": "neutral_from_bottoming",
            "value": neu_from.get("bottoming", 0),
            "note": "must be 0 — no SM path",
        },
        {
            "metric": "neutral_share",
            "value": payload["state_counts"].get("neutral", 0) / max(1, payload["n_analyze_bars"]),
            "note": "share of analyze bars",
        },
    ]
    for src, cnt in neu_from.most_common():
        neutral_rows.append(
            {"metric": f"entries_from_{src}", "value": cnt, "note": "observed transition sources"}
        )

    monthly_rows = []
    for ym, ctr in sorted(payload["monthly_state"].items()):
        total = sum(ctr.values())
        monthly_rows.append(
            {
                "year_month": ym,
                "n_bars": total,
                "topping": ctr.get("topping", 0),
                "bottoming": ctr.get("bottoming", 0),
                "bullish_weakening": ctr.get("bullish_weakening", 0),
                "bearish_weakening": ctr.get("bearish_weakening", 0),
                "early_bullish": ctr.get("early_bullish", 0),
                "early_bearish": ctr.get("early_bearish", 0),
                "neutral": ctr.get("neutral", 0),
                "topping_share": ctr.get("topping", 0) / max(1, total),
                "bottoming_share": ctr.get("bottoming", 0) / max(1, total),
                "neutral_share": ctr.get("neutral", 0) / max(1, total),
            }
        )

    findings = build_findings(payload)

    # Mar6 narrative helpers
    m6 = payload["march06_rows"]
    m6_states = Counter(r["state"] for r in m6)
    first_top = next((r for r in m6 if r["state"] == "topping"), None)
    m6_first_cf = next((r for r in m6 if r.get("cf1_would") or r.get("cf2_would")), None)

    write_csv(output_dir / "state_run_distribution.csv", dist_rows)
    write_csv(output_dir / "long_topping_runs.csv", long_top)
    write_csv(output_dir / "long_bottoming_runs.csv", long_bot)
    write_csv(output_dir / "transition_block_reasons.csv", block_rows)
    write_csv(output_dir / "neutral_reachability.csv", neutral_rows)
    write_csv(output_dir / "march_06_topping_analysis.csv", m6)
    write_csv(output_dir / "march_08_09_bottoming_analysis.csv", payload["march0809_rows"])
    write_csv(output_dir / "monthly_summary.csv", monthly_rows)
    write_csv(output_dir / "root_cause_findings.csv", findings)
    # Extra detail for long-run candles (not in mandatory list but useful; keep lean name)
    write_csv(output_dir / "long_run_hold_candles.csv", payload["long_run_candle_rows"])

    (output_dir / "code_audit.json").write_text(
        json.dumps(json_safe(CODE_AUDIT), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary = {
        "phase": "C2A_topping_bottoming_root_cause",
        "read_only": True,
        "symbol": symbol,
        "config_mode": "C1_C_strict",
        "production_default_still_off": True,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "n_load_bars": int(len(frame)),
        "n_analyze_bars": payload["n_analyze_bars"],
        "state_counts": payload["state_counts"],
        "topping_run_stats": _run_stats([int(r["length_bars"]) for r in payload["runs_topping"]]),
        "bottoming_run_stats": _run_stats([int(r["length_bars"]) for r in payload["runs_bottoming"]]),
        "n_long_topping_runs": len(long_top),
        "n_long_bottoming_runs": len(long_bot),
        "top_block_reasons": block_rows[:12],
        "neutral": {
            "bars": payload["state_counts"].get("neutral", 0),
            "entries": len(payload["neutral_entries"]),
            "from_counts": dict(neu_from),
            "path_from_topping_exists": False,
            "path_from_bottoming_exists": False,
        },
        "cf_would_bars": payload["cf_would_bars"],
        "march06": {
            "state_counts": dict(m6_states),
            "first_topping_row": first_top,
            "first_cf_unlock_row": m6_first_cf,
        },
        "code_audit": CODE_AUDIT,
        "findings": findings,
        "smallest_c2b_proposal": {
            "fix": (
                "Allow topping→early_bearish / bottoming→early_bullish when BOS/CHoCH "
                "evidence is persisted or multi-bar (mirror C1), still requiring LH/HL + impulse; "
                "separately evaluate optional age→neutral for chop without direction."
            ),
            "risks": [
                "Earlier early_bearish/bullish may increase false trend entries",
                "Neutral timeout can skip genuine slow reversals",
                "Must remain config-gated like C1; default off until validated",
            ],
        },
        "safety": {
            "no_sm_transition_change": True,
            "no_policy_change": True,
            "did_not_write_forbidden_dirs": True,
            "default_weakening_mode_off": default_trend_state_config().weakening_multi_bar_mode == "off",
        },
    }
    blob = json.dumps(json_safe(summary), sort_keys=True, separators=(",", ":"))
    summary["deterministic_hash"] = hashlib.sha256(blob.encode()).hexdigest()
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    readme = f"""# Phase C2A — Topping / Bottoming sticky-state root-cause audit

Read-only diagnosis under **C1-C strict** research replay.

## Central question
Why does the SM often fail to leave `topping` → `early_bearish`/`neutral` and
`bottoming` → `early_bullish`/`neutral` after C1 weakening exits?

## Code answers (see code_audit.json)
- **No** `topping→neutral` or `bottoming→neutral` branch exists.
- Productive exits require **same-bar** BOS/CHoCH (LH/HL labels may be persisted).
- `neutral` is only reached from warning invalidation — practically rare after warmup.

## Headline stats
- Topping runs: {summary['topping_run_stats']}
- Bottoming runs: {summary['bottoming_run_stats']}
- Neutral bars: {summary['neutral']['bars']}
- CF unlock bars (diagnostic): {summary['cf_would_bars']}

## C2B proposal
{summary['smallest_c2b_proposal']['fix']}

Default production `weakening_multi_bar_mode=off` unchanged. No transitions changed in C2A.
"""
    (output_dir / "README_results.md").write_text(readme, encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase C2A topping/bottoming root-cause audit")
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
                "topping": summary["topping_run_stats"],
                "bottoming": summary["bottoming_run_stats"],
                "neutral": summary["neutral"],
                "top_blocks": summary["top_block_reasons"][:8],
                "cf_would": summary["cf_would_bars"],
                "c2b": summary["smallest_c2b_proposal"],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
