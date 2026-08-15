"""CLI: extract Cobertura start states for 27 historical TEM blockers (no backtest)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.eval_common import csv_dicts
from research.regime_scanner.tem_structure_break.frozen_v2 import FROZEN_RULE_ID

from .historical_blocker_state_extraction import (
    APT_REFERENCE_TRADE_ID,
    POSITION_STATE_SEMANTICS_MD,
    QTY_TOL,
    Frame5mCache,
    apt_reference_check,
    compute_neutralization,
    parse_ts,
    select_break_event,
    select_causal_5m_candles,
    select_position_state,
    short_fill_price,
    _f,
)

DEFAULT_STRUCTURE = Path(
    "research/backtests/results/tem_structure_break_27_blockers_v2_20260723"
)
DEFAULT_ROOT_CAUSE = Path(
    "research/backtests/results/tem_continuous_27_blocker_root_cause_20260722"
)
DEFAULT_OUT = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_states_20260726"
)
FROZEN_COMMIT = "f828296a061354c8ddf867ff719f5bc37fdef0a8"


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b


def _flags_join(flags: list[str]) -> str:
    return "|".join(dict.fromkeys(flags))


def load_sources(structure_dir: Path, root_cause_dir: Path) -> dict[str, Any]:
    summaries = csv_dicts(structure_dir / "per_trade_summary.csv")
    if len(summaries) != 27:
        raise ValueError(f"expected 27 per_trade_summary rows, got {len(summaries)}")
    ids = [r["trade_id"] for r in summaries]
    if len(set(ids)) != 27:
        raise ValueError("duplicate trade_id in per_trade_summary")

    root_blockers = csv_dicts(root_cause_dir / "tem_end_blockers_27.csv")
    root_ids = {r["trade_id"] for r in root_blockers}
    if set(ids) != root_ids:
        missing = set(ids) ^ root_ids
        raise ValueError(f"trade_id mismatch structure vs root-cause: {sorted(missing)}")

    cycles_by: dict[str, list[dict[str, str]]] = {}
    for r in csv_dicts(root_cause_dir / "blocker_cycle_timelines.csv"):
        cycles_by.setdefault(r["trade_id"], []).append(r)
    for tid in ids:
        if tid not in cycles_by:
            raise ValueError(f"missing cycle timeline for {tid}")

    return {
        "summaries": summaries,
        "state_events": csv_dicts(structure_dir / "state_events.csv"),
        "break_episodes": csv_dicts(structure_dir / "break_episodes.csv"),
        "cycle_join": csv_dicts(structure_dir / "cycle_join.csv"),
        "cycles_by": cycles_by,
        "entry_context": {
            r["trade_id"]: r
            for r in csv_dicts(root_cause_dir / "blocker_entry_context.csv")
        },
        "root_causes": {
            r["trade_id"]: r for r in csv_dicts(root_cause_dir / "blocker_root_causes.csv")
        },
        "root_blockers": {r["trade_id"]: r for r in root_blockers},
    }


def extract_one(
    *,
    summary: dict[str, str],
    trigger_mode: str,
    sources: dict[str, Any],
    frames: Frame5mCache,
    slippage_bps: float,
    taker_fee_rate: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    trade_id = summary["trade_id"]
    coin = summary["coin"]
    quality_flags: list[str] = []
    violations: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    brk = select_break_event(
        trade_id=trade_id,
        trigger_mode=trigger_mode,
        summary=summary,
        state_events=sources["state_events"],
        break_episodes=sources["break_episodes"],
    )
    ambiguous.extend(brk.get("ambiguous") or [])
    quality_flags.extend(brk.get("flags") or [])

    break_row = {
        "trade_id": trade_id,
        "coin": coin,
        "trigger_mode": trigger_mode,
        "trigger_event_timestamp": brk.get("trigger_event_timestamp"),
        "signal_available_ts": brk.get("signal_available_ts"),
        "structure_break_level": brk.get("structure_break_level"),
        "structure_break_kind": brk.get("structure_break_kind"),
        "structure_break_timeframe": brk.get("structure_break_timeframe"),
        "break_cycle_id": brk.get("break_cycle_id"),
        "confirmation_ts": brk.get("confirmation_ts"),
        "event_bar": brk.get("event_bar"),
        "selection_rule": brk.get("selection_rule"),
        "ok": brk.get("ok"),
        "flags": _flags_join(brk.get("flags") or []),
    }

    market_row: dict[str, Any] = {
        "trade_id": trade_id,
        "coin": coin,
        "trigger_mode": trigger_mode,
        "signal_available_ts": brk.get("signal_available_ts"),
    }
    pos_row: dict[str, Any] = {
        "trade_id": trade_id,
        "coin": coin,
        "trigger_mode": trigger_mode,
    }
    neut_row: dict[str, Any] = {
        "trade_id": trade_id,
        "coin": coin,
        "trigger_mode": trigger_mode,
    }

    frame = frames.get(coin)
    sig = brk.get("signal_available_ts")
    if not brk.get("ok") or not sig:
        quality_flags.append("BREAK_EVENT_UNRESOLVED")
        final = _unresolved_shell(summary, trigger_mode, quality_flags, brk)
        return final, break_row, market_row, pos_row, neut_row, ambiguous, violations

    candles = select_causal_5m_candles(frame, str(sig))
    quality_flags.extend(candles.get("flags") or [])
    market_row.update({k: v for k, v in candles.items() if k not in ("ok", "flags")})
    market_row["ok"] = candles.get("ok")
    market_row["flags"] = _flags_join(candles.get("flags") or [])

    fill_raw = None
    fill_px = None
    if candles.get("ok"):
        fill_raw = float(candles["tradeable_5m_open"])
        fill_px = short_fill_price(fill_raw, slippage_bps)
        market_row["neutralization_raw_fill_price"] = fill_raw
        market_row["neutralization_fill_price"] = fill_px
        market_row["slippage_bps"] = slippage_bps
        lvl = _f(brk.get("structure_break_level"))
        prev_c = _f(candles.get("previous_5m_close"))
        market_row["structure_level_to_fill_pct"] = _pct(fill_px, lvl)
        market_row["previous_close_to_fill_pct"] = _pct(fill_px, prev_c)
        market_row["gap_at_signal_pct"] = _pct(fill_raw, prev_c)
        # Lookahead: fill candle must not be before signal
        trade_ts = parse_ts(candles.get("tradeable_5m_timestamp"))
        sig_ts = parse_ts(sig)
        if trade_ts is not None and sig_ts is not None and trade_ts < sig_ts:
            quality_flags.append("SAME_CANDLE_LOOKAHEAD")
            violations.append(
                {
                    "trade_id": trade_id,
                    "check": "fill_before_signal",
                    "detail": f"tradeable={trade_ts} signal={sig_ts}",
                    "pass_fail": "FAIL",
                }
            )
    else:
        quality_flags.append("CANDLE_UNRESOLVED")

    pos = select_position_state(
        trade_id=trade_id,
        cycles=sources["cycles_by"][trade_id],
        signal_available_ts=str(sig),
        frame=frame,
    )
    quality_flags.extend(pos.get("flags") or [])
    pos_row.update(
        {
            "signal_available_ts": sig,
            "source_cycle_index": pos.get("source_cycle_index"),
            "source_state_timestamp": pos.get("source_state_timestamp"),
            "state_selection_rule": pos.get("state_selection_rule"),
            "long_qty_before": pos.get("long_qty_before"),
            "long_avg_before": pos.get("long_avg_before"),
            "short_qty_before": pos.get("short_qty_before"),
            "short_avg_before": pos.get("short_avg_before"),
            "net_long_qty_before": (
                None
                if pos.get("long_qty_before") is None or pos.get("short_qty_before") is None
                else float(pos["long_qty_before"]) - float(pos["short_qty_before"])
            ),
            "long_notional_at_avg": (
                None
                if pos.get("long_qty_before") is None or pos.get("long_avg_before") is None
                else float(pos["long_qty_before"]) * float(pos["long_avg_before"])
            ),
            "short_notional_at_avg": (
                None
                if pos.get("short_qty_before") is None or pos.get("short_avg_before") is None
                else float(pos["short_qty_before"]) * float(pos["short_avg_before"])
            ),
            "cycle_total_pnl_before": pos.get("cycle_total_pnl_before"),
            "cycle_open_mtm_before": pos.get("cycle_open_mtm_before"),
            "realized_economics_before": pos.get("realized_economics_before"),
            "fees_before": pos.get("fees_before"),
            "state_quality": pos.get("state_quality"),
            "state_quality_flags": _flags_join(pos.get("flags") or []),
            "candidate_long_qty": pos.get("candidate_long_qty"),
            "candidate_short_qty": pos.get("candidate_short_qty"),
            "candidate_long_avg": pos.get("candidate_long_avg"),
            "candidate_short_avg": pos.get("candidate_short_avg"),
            "reference_cycle_at_first_break": summary.get("cycle_at_first_break"),
            "tradeable_bar": pos.get("tradeable_bar"),
        }
    )

    # Reject state after signal
    st_ts = parse_ts(pos.get("source_state_timestamp"))
    sig_ts = parse_ts(sig)
    if (
        st_ts is not None
        and sig_ts is not None
        and st_ts >= sig_ts
        and pos.get("ok")
    ):
        quality_flags.append("SIGNAL_BEFORE_STATE")
        violations.append(
            {
                "trade_id": trade_id,
                "check": "state_after_signal",
                "detail": f"state={st_ts} signal={sig_ts}",
                "pass_fail": "FAIL",
            }
        )
        pos["ok"] = False
        pos["state_quality"] = "STATE_UNRESOLVED"

    neut: dict[str, Any] = {}
    if (
        pos.get("ok")
        and candles.get("ok")
        and fill_px is not None
        and pos.get("long_qty_before") is not None
    ):
        neut = compute_neutralization(
            long_qty=float(pos["long_qty_before"]),
            long_avg=float(pos["long_avg_before"]),
            short_qty=float(pos["short_qty_before"]),
            short_avg=float(pos["short_avg_before"]),
            fill_price=float(fill_px),
            taker_fee_rate=taker_fee_rate,
        )
        quality_flags.extend(neut.get("flags") or [])
        # Invariants
        if neut["neutralization_short_qty"] < -QTY_TOL:
            violations.append(
                {
                    "trade_id": trade_id,
                    "check": "negative_neutralization_qty",
                    "detail": str(neut["neutralization_short_qty"]),
                    "pass_fail": "FAIL",
                }
            )
        if neut["neutralization_status"] == "NEEDS_SHORT_FILL":
            if (
                abs(
                    float(neut["post_neutralization_long_qty"])
                    - float(neut["post_neutralization_short_qty"])
                )
                > QTY_TOL
            ):
                violations.append(
                    {
                        "trade_id": trade_id,
                        "check": "post_neutralization_qty_mismatch",
                        "detail": (
                            f"long={neut['post_neutralization_long_qty']} "
                            f"short={neut['post_neutralization_short_qty']}"
                        ),
                        "pass_fail": "FAIL",
                    }
                )
            expected_avg = (
                float(pos["short_qty_before"]) * float(pos["short_avg_before"])
                + float(neut["neutralization_short_qty"]) * float(fill_px)
            ) / float(neut["new_short_qty"])
            if abs(float(neut["new_short_avg"]) - expected_avg) > 1e-9:
                violations.append(
                    {
                        "trade_id": trade_id,
                        "check": "weighted_short_avg",
                        "detail": f"got={neut['new_short_avg']} expected={expected_avg}",
                        "pass_fail": "FAIL",
                    }
                )
            expected_fee = (
                float(neut["neutralization_short_qty"])
                * float(fill_px)
                * float(taker_fee_rate)
            )
            if abs(float(neut["neutralization_open_fee"]) - expected_fee) > 1e-9:
                violations.append(
                    {
                        "trade_id": trade_id,
                        "check": "fee",
                        "detail": f"got={neut['neutralization_open_fee']} expected={expected_fee}",
                        "pass_fail": "FAIL",
                    }
                )
    neut_row.update(
        {
            "neutralization_fill_price": fill_px,
            "taker_fee_rate": taker_fee_rate,
            **{k: neut.get(k) for k in (
                "neutralization_status",
                "neutralization_short_qty",
                "new_short_qty",
                "new_short_avg",
                "neutralization_notional",
                "neutralization_open_fee",
                "post_neutralization_long_qty",
                "post_neutralization_short_qty",
                "post_neutralization_long_avg",
                "post_neutralization_short_avg",
                "post_neutralization_avg_spread_abs",
                "post_neutralization_avg_spread_pct_from_long",
                "post_neutralization_net_qty",
            )},
        }
    )

    realized = pos.get("realized_economics_before")
    mtm = pos.get("cycle_open_mtm_before")
    fees_before = pos.get("fees_before")
    neut_fee = neut.get("neutralization_open_fee")
    total_after = None
    if realized is not None and mtm is not None and neut_fee is not None:
        # MTM not recomputed at fill; fee only additive known component
        total_after = float(realized) + float(mtm) - float(neut_fee)
        quality_flags.append("TOTAL_AFTER_USES_PRE_SIGNAL_MTM_MINUS_NEUT_FEE")
    elif neut_fee is not None:
        quality_flags.append("INCOMPLETE_ECONOMICS_BEFORE")

    # Fees never available from cycle timelines
    if "FEES_NOT_IN_SOURCE" not in quality_flags:
        quality_flags.append("FEES_NOT_IN_SOURCE")
    if fees_before is None:
        quality_flags.append("INCOMPLETE_ECONOMICS_BEFORE")

    ready = bool(
        brk.get("ok")
        and candles.get("ok")
        and pos.get("ok")
        and pos.get("state_quality") == "EXACT_CYCLE_END_BEFORE_SIGNAL"
        and neut.get("neutralization_status") in (
            "NEEDS_SHORT_FILL",
            "ALREADY_SIZE_NEUTRAL",
        )
        and not any(v.get("pass_fail") == "FAIL" for v in violations)
        and "BREAK_EVENT_UNRESOLVED" not in quality_flags
        and "CANDLE_UNRESOLVED" not in quality_flags
        and "POSITION_SEMANTICS_UNRESOLVED" not in quality_flags
        and "SHORT_ALREADY_LARGER_THAN_LONG" not in quality_flags
    )

    # Deduplicate flags
    quality_flags = list(dict.fromkeys(quality_flags))
    state_quality = pos.get("state_quality") or "STATE_UNRESOLVED"
    if not brk.get("ok"):
        state_quality = "BREAK_EVENT_UNRESOLVED"
    elif not candles.get("ok"):
        state_quality = "CANDLE_UNRESOLVED"
    elif not pos.get("ok"):
        state_quality = pos.get("state_quality") or "STATE_UNRESOLVED"

    final = {
        "trade_id": trade_id,
        "coin": coin,
        "trigger_mode": trigger_mode,
        "trade_entry_timestamp": summary.get("entry_ts"),
        "trade_entry_price": _f(summary.get("entry_price")),
        "trigger_event_timestamp": brk.get("trigger_event_timestamp"),
        "signal_available_ts": sig,
        "structure_break_level": brk.get("structure_break_level"),
        "structure_break_kind": brk.get("structure_break_kind"),
        "structure_break_timeframe": brk.get("structure_break_timeframe"),
        "break_cycle_id": brk.get("break_cycle_id"),
        "confirmation_ts": brk.get("confirmation_ts"),
        "tradeable_5m_timestamp": candles.get("tradeable_5m_timestamp"),
        "tradeable_5m_open": candles.get("tradeable_5m_open"),
        "previous_5m_timestamp": candles.get("previous_5m_timestamp"),
        "previous_5m_close": candles.get("previous_5m_close"),
        "neutralization_raw_fill_price": fill_raw,
        "neutralization_fill_price": fill_px,
        "structure_level_to_fill_pct": market_row.get("structure_level_to_fill_pct"),
        "previous_close_to_fill_pct": market_row.get("previous_close_to_fill_pct"),
        "gap_at_signal_pct": market_row.get("gap_at_signal_pct"),
        "source_cycle_index": pos.get("source_cycle_index"),
        "source_state_timestamp": pos.get("source_state_timestamp"),
        "state_selection_rule": pos.get("state_selection_rule"),
        "long_qty_before": pos.get("long_qty_before"),
        "long_avg_before": pos.get("long_avg_before"),
        "short_qty_before": pos.get("short_qty_before"),
        "short_avg_before": pos.get("short_avg_before"),
        "net_long_qty_before": pos_row.get("net_long_qty_before"),
        "long_notional_at_avg": pos_row.get("long_notional_at_avg"),
        "short_notional_at_avg": pos_row.get("short_notional_at_avg"),
        "cycle_total_pnl_before": pos.get("cycle_total_pnl_before"),
        "cycle_open_mtm_before": pos.get("cycle_open_mtm_before"),
        "realized_economics_before": realized,
        "unrealized_economics_before": mtm,
        "fees_before": fees_before,
        "neutralization_short_qty": neut.get("neutralization_short_qty"),
        "neutralization_notional": neut.get("neutralization_notional"),
        "neutralization_open_fee": neut_fee,
        "neutralization_status": neut.get("neutralization_status"),
        "new_short_qty": neut.get("new_short_qty"),
        "new_short_avg": neut.get("new_short_avg"),
        "post_neutralization_long_qty": neut.get("post_neutralization_long_qty"),
        "post_neutralization_short_qty": neut.get("post_neutralization_short_qty"),
        "post_neutralization_long_avg": neut.get("post_neutralization_long_avg"),
        "post_neutralization_short_avg": neut.get("post_neutralization_short_avg"),
        "post_neutralization_avg_spread_abs": neut.get("post_neutralization_avg_spread_abs"),
        "post_neutralization_avg_spread_pct_from_long": neut.get(
            "post_neutralization_avg_spread_pct_from_long"
        ),
        "post_neutralization_net_qty": neut.get("post_neutralization_net_qty"),
        "total_economics_immediately_after_neutralization": total_after,
        "candidate_long_qty": pos.get("candidate_long_qty"),
        "candidate_short_qty": pos.get("candidate_short_qty"),
        "candidate_long_avg": pos.get("candidate_long_avg"),
        "candidate_short_avg": pos.get("candidate_short_avg"),
        "reference_cycle_at_first_break": summary.get("cycle_at_first_break"),
        "state_quality": state_quality,
        "state_quality_flags": _flags_join(quality_flags),
        "ready_for_cobertura": ready,
    }
    return final, break_row, market_row, pos_row, neut_row, ambiguous, violations


def _unresolved_shell(
    summary: dict[str, str],
    trigger_mode: str,
    flags: list[str],
    brk: dict[str, Any],
) -> dict[str, Any]:
    flags = list(dict.fromkeys(flags + ["FEES_NOT_IN_SOURCE", "INCOMPLETE_ECONOMICS_BEFORE"]))
    return {
        "trade_id": summary["trade_id"],
        "coin": summary["coin"],
        "trigger_mode": trigger_mode,
        "trade_entry_timestamp": summary.get("entry_ts"),
        "trade_entry_price": _f(summary.get("entry_price")),
        "trigger_event_timestamp": brk.get("trigger_event_timestamp"),
        "signal_available_ts": brk.get("signal_available_ts"),
        "structure_break_level": brk.get("structure_break_level"),
        "structure_break_kind": brk.get("structure_break_kind"),
        "structure_break_timeframe": brk.get("structure_break_timeframe"),
        "state_quality": "BREAK_EVENT_UNRESOLVED",
        "state_quality_flags": _flags_join(flags),
        "ready_for_cobertura": False,
        "fees_before": None,
    }


def run_extraction(
    *,
    structure_dir: Path,
    root_cause_dir: Path,
    output_dir: Path,
    trigger_mode: str = "first_break",
    slippage_bps: float = 0.0,
    taker_fee_rate: float = 0.00055,
    only_trade_id: str | None = None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    structure_dir = Path(structure_dir)
    root_cause_dir = Path(root_cause_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    modes = (
        ["first_break", "final_invalidation"]
        if trigger_mode == "both"
        else [trigger_mode]
    )
    sources = load_sources(structure_dir, root_cause_dir)
    summaries = list(sources["summaries"])
    if only_trade_id:
        summaries = [s for s in summaries if s["trade_id"] == only_trade_id]
        if not summaries:
            raise ValueError(f"trade_id not found: {only_trade_id}")
    if max_cases is not None:
        summaries = summaries[: int(max_cases)]

    frames = Frame5mCache()
    finals: list[dict[str, Any]] = []
    breaks: list[dict[str, Any]] = []
    markets: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    neuts: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    for summary in summaries:
        for mode in modes:
            print(f"extract {summary['trade_id']} [{mode}]", flush=True)
            final, br, mr, pr, nr, amb, viol = extract_one(
                summary=summary,
                trigger_mode=mode,
                sources=sources,
                frames=frames,
                slippage_bps=slippage_bps,
                taker_fee_rate=taker_fee_rate,
            )
            finals.append(final)
            breaks.append(br)
            markets.append(mr)
            positions.append(pr)
            neuts.append(nr)
            ambiguous.extend(amb)
            violations.extend(viol)

    # Extra audits
    if trigger_mode == "first_break" and only_trade_id is None and max_cases is None:
        if len(finals) != 27:
            violations.append(
                {
                    "trade_id": "",
                    "check": "row_count",
                    "detail": f"expected 27 got {len(finals)}",
                    "pass_fail": "FAIL",
                }
            )
        ids = [r["trade_id"] for r in finals]
        if len(ids) != len(set(ids)):
            violations.append(
                {
                    "trade_id": "",
                    "check": "duplicate_trigger_rows",
                    "detail": "duplicate trade_id",
                    "pass_fail": "FAIL",
                }
            )

    unresolved = [
        r
        for r in finals
        if not r.get("ready_for_cobertura")
        or str(r.get("state_quality")) != "EXACT_CYCLE_END_BEFORE_SIGNAL"
    ]
    apt_rows = [r for r in finals if r["trade_id"] == APT_REFERENCE_TRADE_ID]
    apt_check = (
        apt_reference_check(apt_rows[0])
        if apt_rows
        else {"status": "SKIPPED", "details": ["APT row not in run"]}
    )

    n_full = sum(
        1
        for r in finals
        if r.get("state_quality") == "EXACT_CYCLE_END_BEFORE_SIGNAL"
        and r.get("structure_break_level") is not None
        and r.get("tradeable_5m_timestamp")
    )
    n_break_ok = sum(1 for r in breaks if r.get("ok") in (True, "True", "true"))
    n_candle_ok = sum(1 for r in markets if r.get("ok") in (True, "True", "true"))
    n_pos_exact = sum(
        1 for r in finals if r.get("state_quality") == "EXACT_CYCLE_END_BEFORE_SIGNAL"
    )
    n_ready = sum(1 for r in finals if r.get("ready_for_cobertura") in (True, "True", "true"))
    n_short_larger = sum(
        1
        for r in finals
        if r.get("neutralization_status") == "SHORT_ALREADY_LARGER_THAN_LONG"
        or "SHORT_ALREADY_LARGER_THAN_LONG" in str(r.get("state_quality_flags") or "")
    )
    n_fee_incomplete = sum(
        1
        for r in finals
        if "FEES_NOT_IN_SOURCE" in str(r.get("state_quality_flags") or "")
        or "INCOMPLETE_ECONOMICS" in str(r.get("state_quality_flags") or "")
    )
    n_fail_inv = sum(1 for v in violations if v.get("pass_fail") == "FAIL")

    decision = "BLOCKER_STATE_EXTRACTION_PASS"
    if n_fail_inv > 0 or apt_check["status"] == "APT_REFERENCE_FAIL":
        decision = "BLOCKER_STATE_EXTRACTION_FAIL"
    elif n_ready < len(finals) or apt_check["status"] == "APT_REFERENCE_WARNING":
        decision = "BLOCKER_STATE_EXTRACTION_PASS_WITH_WARNINGS"

    # Manifest
    manifest = []
    for label, path in (
        ("structure_per_trade_summary", structure_dir / "per_trade_summary.csv"),
        ("structure_break_episodes", structure_dir / "break_episodes.csv"),
        ("structure_state_events", structure_dir / "state_events.csv"),
        ("structure_cycle_join", structure_dir / "cycle_join.csv"),
        ("structure_frozen_semantics", structure_dir / "frozen_v2_semantics.json"),
        ("structure_summary", structure_dir / "summary.json"),
        ("root_cycle_timelines", root_cause_dir / "blocker_cycle_timelines.csv"),
        ("root_entry_context", root_cause_dir / "blocker_entry_context.csv"),
        ("root_causes", root_cause_dir / "blocker_root_causes.csv"),
        ("root_tem_end_blockers", root_cause_dir / "tem_end_blockers_27.csv"),
        ("scanner_runner", Path("research/regime_scanner/run_tem_structure_break_27_blockers.py")),
        ("frozen_commit", Path(f"git:{FROZEN_COMMIT}")),
    ):
        manifest.append(
            {
                "label": label,
                "path": str(path),
                "exists": path.exists() if not str(path).startswith("git:") else True,
            }
        )

    write_csv(output_dir / "historical_blocker_states.csv", finals)
    atomic_write_json(output_dir / "historical_blocker_states.json", finals)
    write_csv(output_dir / "blocker_break_events.csv", breaks)
    write_csv(output_dir / "blocker_market_prices.csv", markets)
    write_csv(output_dir / "blocker_position_states.csv", positions)
    write_csv(output_dir / "blocker_neutralization_calculation.csv", neuts)
    write_csv(
        output_dir / "unresolved_states.csv",
        unresolved
        or [
            {
                "trade_id": "",
                "state_quality": "none",
                "ready_for_cobertura": True,
            }
        ],
    )
    write_csv(
        output_dir / "ambiguous_event_matches.csv",
        ambiguous
        or [
            {
                "trade_id": "",
                "reason": "none",
                "n_matches": 0,
            }
        ],
    )
    write_csv(
        output_dir / "invariant_violations.csv",
        violations
        or [
            {
                "trade_id": "",
                "check": "none",
                "detail": "no violations",
                "pass_fail": "PASS",
            }
        ],
    )
    write_csv(output_dir / "source_file_manifest.csv", manifest)
    (output_dir / "position_state_semantics.md").write_text(
        POSITION_STATE_SEMANTICS_MD, encoding="utf-8"
    )

    # Neutralization size summary for report
    neut_qtys = [
        _f(r.get("neutralization_short_qty"))
        for r in finals
        if _f(r.get("neutralization_short_qty")) is not None
    ]
    neut_notional = [
        _f(r.get("neutralization_notional"))
        for r in finals
        if _f(r.get("neutralization_notional")) is not None
    ]

    atomic_write_json(
        output_dir / "config_snapshot.json",
        {
            "structure_dir": str(structure_dir),
            "root_cause_dir": str(root_cause_dir),
            "trigger_mode": trigger_mode,
            "modes": modes,
            "slippage_bps": slippage_bps,
            "taker_fee_rate": taker_fee_rate,
            "frozen_rule_id": FROZEN_RULE_ID,
            "frozen_commit": FROZEN_COMMIT,
            "candle_loader": "load_candles_for_symbol+normalize_candles+candles_to_frame (eval_common 5m path)",
            "n_rows": len(finals),
            "decision": decision,
            "apt_reference": apt_check,
            "short_slippage_sign": (
                "additional short fill_price = open * (1 - slippage_bps/10000); "
                "lower fill price is worse for short entry"
            ),
        },
    )
    atomic_write_json(
        output_dir / "integrity.json",
        {
            "n_rows": len(finals),
            "n_unique_trade_ids": len({r["trade_id"] for r in finals}),
            "n_break_ok": n_break_ok,
            "n_candle_ok": n_candle_ok,
            "n_exact_position": n_pos_exact,
            "n_fully_resolvable": n_full,
            "n_ready_for_cobertura": n_ready,
            "n_short_larger_than_long": n_short_larger,
            "n_fee_incomplete": n_fee_incomplete,
            "n_invariant_fails": n_fail_inv,
            "n_ambiguous": len(ambiguous),
            "apt_reference_status": apt_check["status"],
            "decision": decision,
        },
    )

    report = [
        "# Historical Blocker State Extraction",
        "",
        f"**Decision: `{decision}`**",
        "",
        f"APT reference: **{apt_check['status']}**",
        "",
        *(f"- {d}" for d in apt_check.get("details") or []),
        "",
        "## Answers",
        "",
        f"1. Fully resolvable (exact position + break + candle): **{n_full} / {len(finals)}**",
        f"2. Unique break event OK: **{n_break_ok} / {len(finals)}**",
        f"3. Causal 5m candle OK: **{n_candle_ok} / {len(finals)}**",
        f"4. Exact position state before signal: **{n_pos_exact} / {len(finals)}**",
        f"5. Ready for Cobertura backtest: **{n_ready} / {len(finals)}**",
        f"6. Extra short qty (resolved rows): "
        f"n={len(neut_qtys)} min={min(neut_qtys) if neut_qtys else None} "
        f"max={max(neut_qtys) if neut_qtys else None} "
        f"sum={sum(neut_qtys) if neut_qtys else None}",
        f"7. Extra short notional: "
        f"min={min(neut_notional) if neut_notional else None} "
        f"max={max(neut_notional) if neut_notional else None} "
        f"sum={sum(neut_notional) if neut_notional else None}",
        "8. Short-average change: only computed when exact pre-signal state exists "
        "(see `blocker_neutralization_calculation.csv`).",
        "9. New long/short avg spread: "
        "`post_neutralization_avg_spread_pct_from_long` in outputs.",
        f"10. Short already larger than long: **{n_short_larger}**",
        f"11. Incomplete fee/economics reconstruction: **{n_fee_incomplete}** "
        "(fees never in cycle timelines; flagged `FEES_NOT_IN_SOURCE`).",
        f"12. Lookahead/invariant fails: **{n_fail_inv}**; "
        f"ambiguous event matches: **{len(ambiguous)}**.",
        "",
        "## Key finding",
        "",
        "Most blockers have a recovery cycle whose fill span **straddles** "
        "`signal_available_ts`. Cycle-end inventory therefore cannot be proven as the "
        "pre-signal book without a fill-level ledger. Extraction refuses those states "
        "(`POSITION_SEMANTICS_UNRESOLVED` / `CYCLE_ACTIVE_ACROSS_SIGNAL`) instead of "
        "estimating. Candidate cycle-end snapshots are retained for audit only.",
        "",
        "## Decision",
        "",
        f"`{decision}`",
        "",
    ]
    (output_dir / "REPORT.md").write_text("\n".join(report), encoding="utf-8")

    return {
        "output_dir": str(output_dir),
        "decision": decision,
        "n_rows": len(finals),
        "n_ready": n_ready,
        "apt_reference": apt_check["status"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Extract historical TEM blocker Cobertura start states (no backtest)"
    )
    p.add_argument("--structure-dir", type=Path, default=DEFAULT_STRUCTURE)
    p.add_argument("--root-cause-dir", type=Path, default=DEFAULT_ROOT_CAUSE)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--trigger-mode",
        choices=["first_break", "final_invalidation", "both"],
        default="first_break",
    )
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--taker-fee-rate", type=float, default=0.00055)
    p.add_argument("--only-trade-id", type=str, default=None)
    p.add_argument("--max-cases", type=int, default=None)
    args = p.parse_args(argv)
    payload = run_extraction(
        structure_dir=args.structure_dir,
        root_cause_dir=args.root_cause_dir,
        output_dir=args.output_dir,
        trigger_mode=args.trigger_mode,
        slippage_bps=args.slippage_bps,
        taker_fee_rate=args.taker_fee_rate,
        only_trade_id=args.only_trade_id,
        max_cases=args.max_cases,
    )
    print(f"Wrote {payload['output_dir']}")
    print(
        f"Decision={payload['decision']} rows={payload['n_rows']} "
        f"ready={payload['n_ready']} apt={payload['apt_reference']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
