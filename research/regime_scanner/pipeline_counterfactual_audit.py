"""March-week C0–C5 pipeline counterfactual audit (research-only).

Loads existing pipeline Setup/PA/Momentum CSVs plus precomputed B3 and R2
timelines. Does not mutate pipeline inputs, live strategy, or enable blockers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.direction_gate import expand_15m_state_to_5m_decisions
from research.regime_scanner.pipeline_counterfactual import (
    CounterfactualVariant,
    classify_entry_quality,
    compute_forward_outcome,
    simulate_sequence,
    variant_config,
)
from research.regime_scanner.point_audit import json_safe

FOCUS_SETUPS = ("setup_00055", "setup_00056", "setup_00057", "setup_00058", "setup_00059")
VARIANTS: tuple[CounterfactualVariant, ...] = ("C0", "C1", "C2", "C3", "C4", "C5")
ENTRY_STATES = frozenset({"ENTRY_ALLOWED_AFTER_2", "ENTRY_ALLOWED_AFTER_3"})
TS_TOLERANCE = pd.Timedelta(minutes=1)

DEFAULT_PIPELINE = (
    "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
)
DEFAULT_B3 = (
    "research/backtests/results/regime_scanner_direction_gate_audit_march_week1/"
    "direction_gate_timeline_15m.csv"
)
DEFAULT_RISK = (
    "research/backtests/results/regime_scanner_risk_off_audit_march_week1/risk_off_timeline.csv"
)
DEFAULT_OUT = "research/backtests/results/regime_scanner_pipeline_counterfactual_march_week1"


def _to_utc(ts: object) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _truthy(v: object) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    return str(v).strip().lower() in {"true", "1", "yes"}


def _side_of(row: Mapping[str, Any]) -> str:
    return str(row.get("side") or row.get("setup_side") or "").strip().lower()


def _first_by_setup(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if df is None or df.empty or "setup_id" not in df.columns:
        return out
    for _, row in df.iterrows():
        sid = str(row["setup_id"])
        if sid not in out:
            out[sid] = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
    return out


def _ts_close(a: object, b: object, tol: pd.Timedelta = TS_TOLERANCE) -> bool:
    if a is None or b is None or (isinstance(a, float) and pd.isna(a)):
        return a is None and b is None
    try:
        return abs(_to_utc(a) - _to_utc(b)) <= tol
    except (TypeError, ValueError):
        return str(a) == str(b)


def _json_list(v: object) -> str:
    return json.dumps(json_safe(v), ensure_ascii=True)


def load_pipeline_tables(pipeline_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    setups = pd.read_csv(pipeline_dir / "setup_activations.csv")
    pa = pd.read_csv(pipeline_dir / "price_action_confirmations.csv")
    mom = pd.read_csv(pipeline_dir / "momentum_confirmations.csv")
    return setups, pa, mom


def prepare_candles(
    symbol: str,
    window_start: str,
    window_end: str,
    r2: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    raw = load_symbol_candles(symbol)
    c = raw.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    start = _to_utc(window_start) - pd.Timedelta(days=2)
    end = _to_utc(window_end) + pd.Timedelta(hours=12)
    c = c[(c["timestamp"] >= start) & (c["timestamp"] < end)].copy()
    c["decision_time"] = c["timestamp"] + pd.Timedelta(minutes=5)

    if r2 is not None and not r2.empty:
        ind_cols = [
            cname
            for cname in (
                "atr",
                "ema_9",
                "ema_20",
                "ema9_slope",
                "ema20_slope",
                "plus_di",
                "minus_di",
                "adx",
                "ret_1",
                "di_spread",
            )
            if cname in r2.columns
        ]
        if ind_cols:
            right = r2[["decision_time", *ind_cols]].drop_duplicates("decision_time", keep="last")
            right["decision_time"] = pd.to_datetime(right["decision_time"], utc=True)
            c = c.merge(right, on="decision_time", how="left", suffixes=("", "_r2"))

    idx = pd.DatetimeIndex(
        sorted(
            c[
                (c["decision_time"] >= _to_utc(window_start))
                & (c["decision_time"] < _to_utc(window_end))
            ]["decision_time"].unique()
        )
    )
    return c.reset_index(drop=True), idx


def prepare_b3_map(b3_csv: Path, decision_index: pd.DatetimeIndex) -> pd.DataFrame:
    g15 = pd.read_csv(b3_csv)
    g15 = g15[g15["gate_variant"] == "B3"].copy()
    if g15.empty:
        return pd.DataFrame(columns=["decision_time", "direction_gate_state"])
    # Include a day of history before the audit window for asof lookups.
    expanded = expand_15m_state_to_5m_decisions(g15, pd.Series(decision_index))
    keep = ["decision_time", "direction_gate_state"]
    for extra in ("would_block_long", "would_block_short", "bar_close_time", "gate_variant"):
        if extra in expanded.columns:
            keep.append(extra)
    out = expanded[keep].copy()
    out["decision_time"] = pd.to_datetime(out["decision_time"], utc=True)
    return out.sort_values("decision_time").reset_index(drop=True)


def prepare_r2_timeline(
    risk_csv: Path,
    window_start: str,
    window_end: str,
) -> pd.DataFrame:
    tl = pd.read_csv(risk_csv)
    tl = tl[tl["risk_variant"] == "R2"].copy()
    if tl.empty:
        return tl
    tl["decision_time"] = pd.to_datetime(tl["decision_time"], utc=True)
    warm = _to_utc(window_start) - pd.Timedelta(days=1)
    end = _to_utc(window_end)
    tl = tl[(tl["decision_time"] >= warm) & (tl["decision_time"] < end)].copy()
    if "risk_score_long" not in tl.columns and "long_risk_score" in tl.columns:
        tl["risk_score_long"] = tl["long_risk_score"]
    if "risk_score_short" not in tl.columns and "short_risk_score" in tl.columns:
        tl["risk_score_short"] = tl["short_risk_score"]
    return tl.sort_values("decision_time").reset_index(drop=True)


def filter_activated_setups(
    setups: pd.DataFrame,
    window_start: str,
    window_end: str,
) -> pd.DataFrame:
    s = setups.copy()
    s["setup_activation_timestamp"] = pd.to_datetime(s["setup_activation_timestamp"], utc=True)
    s = s[
        (s["setup_activation_timestamp"] >= _to_utc(window_start))
        & (s["setup_activation_timestamp"] < _to_utc(window_end))
    ]
    if "setup_activated" in s.columns:
        s = s[s["setup_activated"].map(_truthy)]
    return s.sort_values("setup_activation_timestamp").reset_index(drop=True)


def flatten_sequence(seq: Mapping[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in seq.items() if k not in {"state_path", "confirmation_candles", "forward_outcome", "config", "abort_reasons"}}
    row["abort_reasons"] = _json_list(seq.get("abort_reasons") or [])
    row["config_json"] = _json_list(seq.get("config") or {})
    row["n_state_path"] = len(seq.get("state_path") or [])
    row["n_confirm_candles"] = len(seq.get("confirmation_candles") or [])
    fo = seq.get("forward_outcome") or {}
    if fo:
        row["outcome_entry_quality"] = fo.get("entry_quality")
        row["outcome_mfe_pct"] = fo.get("mfe_pct")
        row["outcome_mae_pct"] = fo.get("mae_pct")
        row["outcome_reached_plus_025"] = fo.get("reached_plus_025")
    return row


def state_change_rows(sequences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seq in sequences:
        path = seq.get("state_path") or []
        prev = None
        for step in path:
            st = step.get("state")
            if prev is not None and st != prev:
                rows.append(
                    {
                        "setup_id": seq.get("setup_id"),
                        "variant": seq.get("variant"),
                        "side": seq.get("side"),
                        "from_state": prev,
                        "to_state": st,
                        "timestamp": step.get("timestamp"),
                        "stage": step.get("stage"),
                        "reasons": _json_list(step.get("reasons") or []),
                    }
                )
            prev = st
    return rows


def c0_reproduction_check(
    sequences_c0: list[dict[str, Any]],
    setups: pd.DataFrame,
    pa: pd.DataFrame,
    mom: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    setup_ids = set(setups["setup_id"].astype(str))
    pa_ids = set(pa["setup_id"].astype(str)) if len(pa) else set()
    mom_by = _first_by_setup(mom)
    c0_by = {str(s["setup_id"]): s for s in sequences_c0}

    rows: list[dict[str, Any]] = []
    for sid in sorted(setup_ids):
        seq = c0_by.get(sid) or {}
        baseline_mom = mom_by.get(sid)
        entry_ok = False
        ts_match = False
        if baseline_mom is not None:
            entry_ok = str(seq.get("final_state") or "") in ENTRY_STATES
            ts_match = _ts_close(
                seq.get("entry_timestamp"),
                baseline_mom.get("confirmation_timestamp"),
            )
        elif str(seq.get("final_state") or "").startswith("ENTRY_ALLOWED"):
            entry_ok = False
            ts_match = False
        else:
            entry_ok = True
            ts_match = True
        pa_match = (sid in pa_ids) == bool(seq.get("pa_structure_break_timestamp"))
        rows.append(
            {
                "setup_id": sid,
                "in_c0": sid in c0_by,
                "baseline_has_pa": sid in pa_ids,
                "baseline_has_mom": baseline_mom is not None,
                "c0_final_state": seq.get("final_state"),
                "c0_entry_timestamp": seq.get("entry_timestamp"),
                "baseline_mom_timestamp": (
                    baseline_mom.get("confirmation_timestamp") if baseline_mom else None
                ),
                "match_pa_presence": pa_match,
                "match_mom_entry": entry_ok and ts_match if baseline_mom else entry_ok,
                "match_entry_timestamp": ts_match if baseline_mom else True,
                "matches": bool(
                    (sid in c0_by)
                    and pa_match
                    and (entry_ok and ts_match if baseline_mom else entry_ok)
                ),
            }
        )

    df = pd.DataFrame(rows)
    summary = {
        "n_baseline_setups": int(len(setup_ids)),
        "n_c0_sequences": int(len(sequences_c0)),
        "setup_id_count_match": len(setup_ids) == len(c0_by) and setup_ids == set(c0_by),
        "n_baseline_pa": int(len(pa_ids)),
        "n_c0_with_pa": int(sum(1 for s in sequences_c0 if s.get("pa_structure_break_timestamp"))),
        "pa_setup_ids_match": pa_ids
        == {str(s["setup_id"]) for s in sequences_c0 if s.get("pa_structure_break_timestamp")},
        "n_baseline_mom": int(len(mom_by)),
        "n_c0_entries": int(sum(1 for s in sequences_c0 if s.get("entry_allowed"))),
        "n_mom_timestamp_matches": int(
            df.loc[df["baseline_has_mom"], "match_entry_timestamp"].sum()
        )
        if len(df) and "baseline_has_mom" in df.columns
        else 0,
        "n_full_matches": int(df["matches"].sum()) if len(df) else 0,
        "all_match": bool(df["matches"].all()) if len(df) else False,
    }
    return df, summary


def variant_metrics(
    variant: str,
    sequences: list[dict[str, Any]],
    outcomes: pd.DataFrame,
    baseline_outcomes: pd.DataFrame | None = None,
) -> dict[str, Any]:
    seqs = sequences
    n = len(seqs)
    longs = [s for s in seqs if s.get("side") == "long"]
    shorts = [s for s in seqs if s.get("side") == "short"]
    with_pa = [s for s in seqs if s.get("pa_structure_break_timestamp")]
    no_pa = [s for s in seqs if not s.get("pa_structure_break_timestamp")]
    entries = [s for s in seqs if s.get("entry_allowed")]
    after2 = [s for s in seqs if s.get("final_state") == "ENTRY_ALLOWED_AFTER_2"]
    after3 = [s for s in seqs if s.get("final_state") == "ENTRY_ALLOWED_AFTER_3"]

    def count_final(state: str) -> int:
        return sum(1 for s in seqs if s.get("final_state") == state)

    def count_abort_prefix(prefix: str) -> int:
        return sum(
            1
            for s in seqs
            for r in (s.get("abort_reasons") or [])
            if str(r).startswith(prefix)
        )

    def count_abort_contains(token: str) -> int:
        return sum(
            1
            for s in seqs
            if any(token in str(r) for r in (s.get("abort_reasons") or []))
        )

    blocked_confirm = [s for s in seqs if s.get("final_state") == "ABORTED_DURING_CONFIRMATION"]
    confirm_stage_counts = {"confirm_1": 0, "confirm_2": 0, "confirm_3": 0}
    for s in blocked_confirm:
        path = s.get("state_path") or []
        last_wait = None
        for step in path:
            st = str(step.get("state") or "")
            if st.startswith("WAITING_CONFIRMATION_"):
                last_wait = st
            if st == "ABORTED_DURING_CONFIRMATION" and last_wait:
                key = last_wait.replace("WAITING_CONFIRMATION_", "confirm_")
                if key in confirm_stage_counts:
                    confirm_stage_counts[key] += 1

    out_v = outcomes[outcomes["variant"] == variant] if len(outcomes) else pd.DataFrame()
    weak_allowed = int((out_v["entry_quality"] == "weak").sum()) if len(out_v) else 0
    good_allowed = int((out_v["entry_quality"] == "good").sum()) if len(out_v) else 0

    weak_prevented = good_prevented = 0
    if baseline_outcomes is not None and len(baseline_outcomes) and variant != "C0":
        base_by = {
            str(r["setup_id"]): r for _, r in baseline_outcomes.iterrows() if r.get("entry_allowed")
        }
        for s in seqs:
            if s.get("entry_allowed"):
                continue
            b = base_by.get(str(s["setup_id"]))
            if b is None:
                continue
            q = b.get("entry_quality")
            if q == "weak":
                weak_prevented += 1
            elif q == "good":
                good_prevented += 1

    blocked_total = sum(
        1
        for s in seqs
        if s.get("final_state")
        in {"BLOCKED_AT_SETUP", "ABORTED_AT_PA", "ABORTED_DURING_CONFIRMATION"}
    )
    precision = (
        (weak_prevented / (weak_prevented + good_prevented))
        if (weak_prevented + good_prevented)
        else None
    )
    # Recall of weak baseline entries prevented
    n_weak_base = (
        int((baseline_outcomes["entry_quality"] == "weak").sum())
        if baseline_outcomes is not None and len(baseline_outcomes)
        else 0
    )
    recall_weak = (weak_prevented / n_weak_base) if n_weak_base else None
    false_block = (
        (good_prevented / (good_prevented + good_allowed))
        if (good_prevented + good_allowed)
        else None
    )

    delays: list[float] = []
    for s in entries:
        pa_ts = s.get("pa_structure_break_timestamp")
        et = s.get("entry_timestamp")
        if pa_ts and et:
            try:
                delays.append((_to_utc(et) - _to_utc(pa_ts)).total_seconds() / 60.0)
            except (TypeError, ValueError):
                pass

    return {
        "variant": variant,
        "n_setups": n,
        "n_long_setups": len(longs),
        "n_short_setups": len(shorts),
        "n_pa_confirmations": len(with_pa),
        "n_sequences_without_pa": len(no_pa),
        "n_sequences_with_pa": len(with_pa),
        "n_entries_after_2": len(after2),
        "n_entries_after_3": len(after3),
        "n_blocked_at_setup": count_final("BLOCKED_AT_SETUP"),
        "n_blocked_at_pa": count_final("ABORTED_AT_PA"),
        "n_blocked_at_confirm_1": confirm_stage_counts["confirm_1"],
        "n_blocked_at_confirm_2": confirm_stage_counts["confirm_2"],
        "n_blocked_at_confirm_3": confirm_stage_counts["confirm_3"],
        "n_b3_blocks": count_abort_prefix("B3_"),
        "n_r2_blocks": count_abort_prefix("R2_"),
        "n_pa_invalidations": count_abort_contains("PA_"),
        "n_momentum_invalidations": count_abort_contains("MOMENTUM_"),
        "n_expiries": count_final("EXPIRED"),
        "n_long_entries": sum(1 for s in entries if s.get("side") == "long"),
        "n_short_entries": sum(1 for s in entries if s.get("side") == "short"),
        "n_entries": len(entries),
        "n_weak_entries_prevented": weak_prevented,
        "n_good_entries_prevented": good_prevented,
        "n_weak_entries_allowed": weak_allowed,
        "n_good_entries_allowed": good_allowed,
        "precision_blocks_are_weak": precision,
        "recall_weak_entries": recall_weak,
        "false_block_rate_good": false_block,
        "avg_entry_delay_minutes_from_pa": (sum(delays) / len(delays)) if delays else None,
        "share_entries_after_2": (len(after2) / len(entries)) if entries else None,
        "share_entries_after_3": (len(after3) / len(entries)) if entries else None,
        "n_prevented_by_gate": sum(1 for s in seqs if s.get("prevented_by_gate")),
        "n_blocked_entry_sequences": blocked_total,
        "mean_mfe_pct": float(out_v["mfe_pct"].mean()) if len(out_v) and "mfe_pct" in out_v else None,
        "mean_mae_pct": float(out_v["mae_pct"].mean()) if len(out_v) and "mae_pct" in out_v else None,
    }


def build_entry_outcomes(
    sequences: list[dict[str, Any]],
    candles: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for seq in sequences:
        if not seq.get("entry_allowed"):
            continue
        fo = seq.get("forward_outcome")
        if fo is None and seq.get("entry_timestamp") and seq.get("entry_price") is not None:
            fo = compute_forward_outcome(
                candles,
                seq["entry_timestamp"],
                float(seq["entry_price"]),
                str(seq.get("side") or ""),
            )
        fo = fo or {}
        quality = fo.get("entry_quality") or classify_entry_quality(
            fo.get("mfe_pct"), fo.get("mae_pct"), fo.get("reached_plus_025")
        )
        rows.append(
            {
                "setup_id": seq.get("setup_id"),
                "variant": seq.get("variant"),
                "side": seq.get("side"),
                "entry_allowed": True,
                "entry_timestamp": seq.get("entry_timestamp"),
                "entry_price": seq.get("entry_price"),
                "final_state": seq.get("final_state"),
                "required_confirm_candles": seq.get("required_confirm_candles"),
                "entry_quality": quality,
                "mfe_pct": fo.get("mfe_pct"),
                "mae_pct": fo.get("mae_pct"),
                "deepest_adverse": fo.get("deepest_adverse"),
                "reached_plus_025": fo.get("reached_plus_025"),
                "minutes_to_025": fo.get("minutes_to_025"),
                "returned_to_entry": fo.get("returned_to_entry"),
                "minutes_to_return": fo.get("minutes_to_return"),
                "adverse_15m": fo.get("adverse_15m"),
                "adverse_30m": fo.get("adverse_30m"),
                "adverse_60m": fo.get("adverse_60m"),
                "favorable_15m": fo.get("favorable_15m"),
                "favorable_30m": fo.get("favorable_30m"),
                "favorable_60m": fo.get("favorable_60m"),
                "evaluable": fo.get("evaluable"),
            }
        )
    return pd.DataFrame(rows)


def remaining_weak_leaks(
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if outcomes.empty:
        return pd.DataFrame()
    weak = outcomes[
        (outcomes["variant"].isin(["C3", "C4", "C5"])) & (outcomes["entry_quality"] == "weak")
    ]
    for _, o in weak.iterrows():
        variant = str(o["variant"])
        sid = str(o["setup_id"])
        seq = next(
            (s for s in sequences_by_variant.get(variant, []) if str(s.get("setup_id")) == sid),
            {},
        )
        confirms = seq.get("confirmation_candles") or []
        leak_cat = "MOMENTUM_QUALITY_LEAK"
        if not seq.get("pa_structure_break_timestamp"):
            leak_cat = "SETUP_QUALITY_LEAK"
        elif not confirms:
            leak_cat = "PA_QUALITY_LEAK"
        rows.append(
            {
                "setup_id": sid,
                "variant": variant,
                "side": o.get("side"),
                "setup_activation_timestamp": seq.get("setup_activation_timestamp"),
                "pa_structure_break_timestamp": seq.get("pa_structure_break_timestamp"),
                "entry_timestamp": o.get("entry_timestamp"),
                "entry_price": o.get("entry_price"),
                "confirmation_candles_json": _json_list(confirms),
                "risk_state_at_setup": seq.get("risk_state_at_setup"),
                "b3_state_at_setup": seq.get("b3_state_at_setup"),
                "risk_state_at_pa": seq.get("risk_state_at_pa"),
                "b3_state_at_pa": seq.get("b3_state_at_pa"),
                "momentum_qualities": _json_list([c.get("momentum_quality") for c in confirms]),
                "mfe_pct": o.get("mfe_pct"),
                "mae_pct": o.get("mae_pct"),
                "reached_plus_025": o.get("reached_plus_025"),
                "why_r2_missed": "R2 not risk_off at abort checkpoints on this path",
                "why_b3_missed": "B3 not opposing strong trend at abort checkpoints",
                "leak_category": leak_cat,
            }
        )
    return pd.DataFrame(rows)


def false_blocked_good_entries(
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    c0 = {str(s["setup_id"]): s for s in sequences_by_variant.get("C0", [])}
    c0_out = {
        str(r["setup_id"]): r
        for _, r in outcomes[outcomes["variant"] == "C0"].iterrows()
    }
    for variant in ("C1", "C2", "C3", "C4", "C5"):
        for seq in sequences_by_variant.get(variant, []):
            sid = str(seq.get("setup_id"))
            base = c0.get(sid)
            bout = c0_out.get(sid)
            if not base or not base.get("entry_allowed") or bout is None:
                continue
            if bout.get("entry_quality") != "good":
                continue
            if seq.get("entry_allowed"):
                continue
            rows.append(
                {
                    "setup_id": sid,
                    "variant": variant,
                    "side": seq.get("side"),
                    "block_final_state": seq.get("final_state"),
                    "primary_abort_reason": seq.get("primary_abort_reason"),
                    "abort_reasons": _json_list(seq.get("abort_reasons") or []),
                    "risk_state_at_setup": seq.get("risk_state_at_setup"),
                    "risk_state_at_pa": seq.get("risk_state_at_pa"),
                    "b3_state_at_setup": seq.get("b3_state_at_setup"),
                    "b3_state_at_pa": seq.get("b3_state_at_pa"),
                    "baseline_entry_timestamp": base.get("entry_timestamp"),
                    "baseline_mfe_pct": bout.get("mfe_pct"),
                    "baseline_mae_pct": bout.get("mae_pct"),
                    "baseline_entry_quality": bout.get("entry_quality"),
                    "lost_vs_delayed": "fully_blocked",
                }
            )
    return pd.DataFrame(rows)


def two_vs_three_table(
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for variant, seqs in sequences_by_variant.items():
        after2 = [s for s in seqs if s.get("final_state") == "ENTRY_ALLOWED_AFTER_2"]
        after3 = [s for s in seqs if s.get("final_state") == "ENTRY_ALLOWED_AFTER_3"]
        out_v = outcomes[outcomes["variant"] == variant] if len(outcomes) else pd.DataFrame()
        q2 = out_v[out_v["final_state"] == "ENTRY_ALLOWED_AFTER_2"] if len(out_v) else out_v
        q3 = out_v[out_v["final_state"] == "ENTRY_ALLOWED_AFTER_3"] if len(out_v) else out_v
        rows.append(
            {
                "variant": variant,
                "n_entry_after_2": len(after2),
                "n_entry_after_3": len(after3),
                "n_required_3": sum(1 for s in seqs if int(s.get("required_confirm_candles") or 0) >= 3),
                "weak_after_2": int((q2["entry_quality"] == "weak").sum()) if len(q2) else 0,
                "good_after_2": int((q2["entry_quality"] == "good").sum()) if len(q2) else 0,
                "weak_after_3": int((q3["entry_quality"] == "weak").sum()) if len(q3) else 0,
                "good_after_3": int((q3["entry_quality"] == "good").sum()) if len(q3) else 0,
            }
        )
    return pd.DataFrame(rows)


def r2_vs_b3_incremental(
    sequences_by_variant: dict[str, list[dict[str, Any]]],
) -> pd.DataFrame:
    def blocked_ids(variant: str) -> set[str]:
        return {
            str(s["setup_id"])
            for s in sequences_by_variant.get(variant, [])
            if s.get("prevented_by_gate")
            or s.get("final_state")
            in {"BLOCKED_AT_SETUP", "ABORTED_AT_PA", "ABORTED_DURING_CONFIRMATION"}
        }

    c0_entries = {
        str(s["setup_id"])
        for s in sequences_by_variant.get("C0", [])
        if s.get("entry_allowed")
    }
    b3 = blocked_ids("C1")
    r2 = blocked_ids("C2")
    both = blocked_ids("C3")
    return pd.DataFrame(
        [
            {
                "metric": "n_setups_blocked_b3_only_C1",
                "value": len(b3),
                "setup_ids": _json_list(sorted(b3)),
            },
            {
                "metric": "n_setups_blocked_r2_only_C2",
                "value": len(r2),
                "setup_ids": _json_list(sorted(r2)),
            },
            {
                "metric": "n_setups_blocked_b3_plus_r2_C3",
                "value": len(both),
                "setup_ids": _json_list(sorted(both)),
            },
            {
                "metric": "incremental_r2_vs_b3_on_c0_entries",
                "value": len((r2 - b3) & c0_entries),
                "setup_ids": _json_list(sorted((r2 - b3) & c0_entries)),
            },
            {
                "metric": "incremental_b3_vs_r2_on_c0_entries",
                "value": len((b3 - r2) & c0_entries),
                "setup_ids": _json_list(sorted((b3 - r2) & c0_entries)),
            },
            {
                "metric": "overlap_b3_and_r2",
                "value": len(b3 & r2),
                "setup_ids": _json_list(sorted(b3 & r2)),
            },
            {
                "metric": "n_c0_entries",
                "value": len(c0_entries),
                "setup_ids": _json_list(sorted(c0_entries)),
            },
        ]
    )


def focus_table(
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    out_map = {
        (str(r["variant"]), str(r["setup_id"])): r for _, r in outcomes.iterrows()
    } if len(outcomes) else {}
    for sid in FOCUS_SETUPS:
        for variant in VARIANTS:
            seq = next(
                (s for s in sequences_by_variant.get(variant, []) if str(s.get("setup_id")) == sid),
                None,
            )
            if seq is None:
                rows.append({"setup_id": sid, "variant": variant, "present": False})
                continue
            o = out_map.get((variant, sid), {})
            confirms = seq.get("confirmation_candles") or []
            rows.append(
                {
                    "setup_id": sid,
                    "variant": variant,
                    "present": True,
                    "side": seq.get("side"),
                    "setup_activation_timestamp": seq.get("setup_activation_timestamp"),
                    "pa_structure_break_timestamp": seq.get("pa_structure_break_timestamp"),
                    "final_state": seq.get("final_state"),
                    "primary_abort_reason": seq.get("primary_abort_reason"),
                    "abort_reasons": _json_list(seq.get("abort_reasons") or []),
                    "entry_allowed": seq.get("entry_allowed"),
                    "entry_timestamp": seq.get("entry_timestamp"),
                    "entry_price": seq.get("entry_price"),
                    "required_confirm_candles": seq.get("required_confirm_candles"),
                    "risk_state_at_setup": seq.get("risk_state_at_setup"),
                    "b3_state_at_setup": seq.get("b3_state_at_setup"),
                    "risk_state_at_pa": seq.get("risk_state_at_pa"),
                    "b3_state_at_pa": seq.get("b3_state_at_pa"),
                    "momentum_qualities": _json_list([c.get("momentum_quality") for c in confirms]),
                    "entry_quality": o.get("entry_quality") if o is not None else None,
                    "mfe_pct": o.get("mfe_pct") if o is not None else None,
                    "mae_pct": o.get("mae_pct") if o is not None else None,
                    "reached_plus_025": o.get("reached_plus_025") if o is not None else None,
                }
            )
    return pd.DataFrame(rows)


def _focus_seq(sequences_by_variant: dict[str, list[dict[str, Any]]], variant: str, sid: str) -> dict[str, Any]:
    return next(
        (s for s in sequences_by_variant.get(variant, []) if str(s.get("setup_id")) == sid),
        {},
    )


def build_answers(
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    metrics: dict[str, dict[str, Any]],
    outcomes: pd.DataFrame,
    c0_summary: dict[str, Any],
    leaks: pd.DataFrame,
    false_blocks: pd.DataFrame,
    tvt: pd.DataFrame,
    incremental: pd.DataFrame,
) -> dict[str, str]:
    c0 = sequences_by_variant.get("C0", [])
    c1 = sequences_by_variant.get("C1", [])
    c2 = sequences_by_variant.get("C2", [])
    c3 = sequences_by_variant.get("C3", [])

    n_pa = metrics.get("C0", {}).get("n_pa_confirmations")
    n_mom_stage = sum(
        1
        for s in c0
        if s.get("pa_structure_break_timestamp")
        and s.get("final_state") not in {"ABORTED_AT_PA", "NO_PA_CONFIRMATION", "BLOCKED_AT_SETUP"}
    )
    n_entries_c0 = metrics.get("C0", {}).get("n_entries", 0)

    def blocked_entry_count(seqs: list[dict[str, Any]]) -> int:
        c0_entry_ids = {str(s["setup_id"]) for s in c0 if s.get("entry_allowed")}
        return sum(
            1
            for s in seqs
            if str(s.get("setup_id")) in c0_entry_ids and not s.get("entry_allowed")
        )

    inc = {r["metric"]: r["value"] for _, r in incremental.iterrows()} if len(incremental) else {}

    s55_c2 = _focus_seq(sequences_by_variant, "C2", "setup_00055")
    s55_c3 = _focus_seq(sequences_by_variant, "C3", "setup_00055")
    s58_c0 = _focus_seq(sequences_by_variant, "C0", "setup_00058")
    s58_c3 = _focus_seq(sequences_by_variant, "C3", "setup_00058")
    s58_out = outcomes[
        (outcomes["variant"] == "C3") & (outcomes["setup_id"] == "setup_00058")
    ] if len(outcomes) else pd.DataFrame()
    s58_c0_out = outcomes[
        (outcomes["variant"] == "C0") & (outcomes["setup_id"] == "setup_00058")
    ] if len(outcomes) else pd.DataFrame()

    no_pa_ok = all(
        _focus_seq(sequences_by_variant, "C0", sid).get("final_state") == "NO_PA_CONFIRMATION"
        for sid in ("setup_00056", "setup_00057", "setup_00059")
    )

    c3_good_blocked = int((false_blocks["variant"] == "C3").sum()) if len(false_blocks) else 0
    leak_cats = (
        leaks["leak_category"].value_counts().to_dict() if len(leaks) and "leak_category" in leaks else {}
    )

    c0_ok = bool(c0_summary.get("setup_id_count_match")) and bool(
        c0_summary.get("n_mom_timestamp_matches", 0) >= c0_summary.get("n_baseline_mom", 0)
    )
    recommend = None
    if (
        c0_ok
        and metrics.get("C2", {}).get("false_block_rate_good") in (0, 0.0, None)
        and (metrics.get("C3", {}).get("n_weak_entries_prevented") or 0) > 0
    ):
        recommend = "C3"
    elif c0_ok:
        recommend = "C3_for_multi_week_only_if_selective_blocks_hold"
    else:
        recommend = "None — fix C0 reproduction first"

    answers = {
        "q1_c0_reproduces_pipeline": (
            f"{'Mostly yes' if c0_ok else 'No / partial'}. "
            f"setup_id_count_match={c0_summary.get('setup_id_count_match')}, "
            f"pa_ids_match={c0_summary.get('pa_setup_ids_match')}, "
            f"mom_ts_matches={c0_summary.get('n_mom_timestamp_matches')}/"
            f"{c0_summary.get('n_baseline_mom')}, "
            f"full_row_matches={c0_summary.get('n_full_matches')}/"
            f"{c0_summary.get('n_baseline_setups')}. "
            "C0 uses existing momentum CSV timestamps; non-mom terminals may be EXPIRED/INVALIDATED."
        ),
        "q2_n_setups_with_pa": str(n_pa),
        "q3_n_sequences_reached_momentum_stage": str(n_mom_stage),
        "q4_n_actual_entry_candidates_c0": str(n_entries_c0),
        "q5_n_entry_candidates_blocked_b3_alone": str(blocked_entry_count(c1)),
        "q6_n_entry_candidates_blocked_r2_alone": str(blocked_entry_count(c2)),
        "q7_n_entry_candidates_blocked_b3_plus_r2": str(blocked_entry_count(c3)),
        "q8_incremental_value_r2_vs_b3": (
            f"C0-entry setups blocked by R2 but not B3: {inc.get('incremental_r2_vs_b3_on_c0_entries')}. "
            f"Detail ids in r2_vs_b3_incremental_value.csv."
        ),
        "q9_incremental_value_b3_vs_r2": (
            f"C0-entry setups blocked by B3 but not R2: {inc.get('incremental_b3_vs_r2_on_c0_entries')}."
        ),
        "q10_c3_blocks_good_entries": (
            f"Yes, n={c3_good_blocked}." if c3_good_blocked else "No good C0 entries fully blocked under C3 in this week."
        ),
        "q11_c4_or_c5_better_when_elevated": (
            f"C4 entries={metrics.get('C4', {}).get('n_entries')}, "
            f"C5 entries={metrics.get('C5', {}).get('n_entries')}, "
            f"C3 entries={metrics.get('C3', {}).get('n_entries')}. "
            "Prefer the more selective of C4/C5 only if elevated paths differ materially; "
            "this March week alone is thin for choosing between them."
        ),
        "q12_two_candles_enough_when_normal": (
            f"two_vs_three: {tvt.to_dict(orient='records') if len(tvt) else []}. "
            "When required_confirm_candles=2 and gates stay clear, AFTER_2 is the design default."
        ),
        "q13_third_candle_measurable_benefit": (
            f"Entries after 3 under C2/C3/C4/C5: "
            f"C2={metrics.get('C2', {}).get('n_entries_after_3')}, "
            f"C3={metrics.get('C3', {}).get('n_entries_after_3')}, "
            f"C4={metrics.get('C4', {}).get('n_entries_after_3')}, "
            f"C5={metrics.get('C5', {}).get('n_entries_after_3')}. "
            "Benefit is measurable only if weak paths abort on candle 3 without broad good delays."
        ),
        "q14_00055_aborted_at_pa_under_r2": (
            f"C2 final={s55_c2.get('final_state')} reason={s55_c2.get('primary_abort_reason')}; "
            f"C3 final={s55_c3.get('final_state')} reason={s55_c3.get('primary_abort_reason')}. "
            "Expected ABORTED_AT_PA / R2_LONG_RISK_OFF_AT_PA for C2–C5."
        ),
        "q15_00056_57_59_no_pa_no_entry": (
            f"{'Yes' if no_pa_ok else 'No'}. "
            + "; ".join(
                f"{sid}={_focus_seq(sequences_by_variant, 'C0', sid).get('final_state')}"
                for sid in ("setup_00056", "setup_00057", "setup_00059")
            )
        ),
        "q16_00058_opened_after_two_candles": (
            f"C0 final={s58_c0.get('final_state')} entry={s58_c0.get('entry_allowed')} "
            f"ts={s58_c0.get('entry_timestamp')}; "
            f"C3 final={s58_c3.get('final_state')} entry={s58_c3.get('entry_allowed')} "
            f"ts={s58_c3.get('entry_timestamp')}. "
            "Baseline had PA but no momentum CSV row — C0 may be EXPIRED; C1–C5 walk confirmation."
        ),
        "q17_00058_is_weak_entry": (
            (
                f"C3 quality={s58_out.iloc[0].get('entry_quality')} "
                f"mfe={s58_out.iloc[0].get('mfe_pct')} mae={s58_out.iloc[0].get('mae_pct')}"
                if len(s58_out)
                else (
                    f"C0 quality={s58_c0_out.iloc[0].get('entry_quality')}"
                    if len(s58_c0_out)
                    else "No entry outcome for 00058 under C0/C3."
                )
            )
        ),
        "q18_00058_confirmation_quality": (
            _json_list(s58_c3.get("confirmation_candles") or s58_c0.get("confirmation_candles") or [])
        ),
        "q19_remaining_weak_under_c3_c5": (
            f"n={len(leaks)}; ids="
            + _json_list(sorted(leaks["setup_id"].astype(str).unique()))
            if len(leaks)
            else "None observed under C3–C5."
        ),
        "q20_leak_primary_layer": (
            f"Leak category counts: {leak_cats}" if leak_cats else "No remaining weak leaks to attribute."
        ),
        "q21_further_filter_needed": (
            "Possibly at momentum/PA quality if weak leaks remain after B3+R2; "
            "do not invent a new filter from one week."
            if len(leaks)
            else "No strong evidence from this week that another filter is required beyond studying leaks."
        ),
        "q22_is_b3_r2_adaptive_enough": (
            "Promising as a research stack (B3 for confirmed counter-trends, R2 for selective risk-off, "
            "adaptive 2/3 confirms), but March alone cannot prove sufficiency."
        ),
        "q23_recommended_variant_for_multi_week": str(recommend),
        "q24_productive_integration_justified": (
            "No. Research-only; enabled=False; March week is insufficient for live pipeline integration."
        ),
        "q25_march_week_too_little_evidence": (
            "Yes — recommend multi-week audit only after C0 reproduction is solid and block/quality "
            "criteria hold; do not integrate from this single week."
        ),
    }
    return answers


def write_readme(summary: dict[str, Any], path: Path) -> None:
    answers = summary.get("answers") or {}
    metrics = summary.get("variant_metrics") or {}
    lines = [
        "# Pipeline counterfactual audit (March week 1)",
        "",
        "Research-only C0–C5 simulation over existing Setup / PA / Momentum artifacts,",
        "overlaying precomputed B3 Strong-Trend and R2 Risk-Off timelines.",
        "",
        "## Window",
        f"- symbol: `{summary.get('symbol')}`",
        f"- window: `{summary.get('window_start')}` → `{summary.get('window_end')}`",
        f"- pipeline_dir: `{summary.get('pipeline_dir')}`",
        f"- b3_csv: `{summary.get('b3_csv')}` (gate_variant==B3)",
        f"- risk_csv: `{summary.get('risk_csv')}` (risk_variant==R2)",
        f"- focus: {summary.get('focus_setups')}",
        "",
        "## Variants",
        "- C0: existing pipeline momentum (no B3/R2)",
        "- C1: B3 only",
        "- C2: R2 only (elevated → 3 confirms)",
        "- C3: B3 + R2",
        "- C4: C3 + require clear-to-normal after candle 3",
        "- C5: C3 + score-drop + strong momentum flexibility",
        "",
        "## C0 reproduction",
        f"```json\n{json.dumps(json_safe(summary.get('c0_reproduction') or {}), indent=2)}\n```",
        "",
        "## Variant metrics (compact)",
    ]
    for v in VARIANTS:
        m = metrics.get(v) or {}
        lines.append(
            f"- **{v}**: setups={m.get('n_setups')} pa={m.get('n_pa_confirmations')} "
            f"entries={m.get('n_entries')} blocked_setup={m.get('n_blocked_at_setup')} "
            f"blocked_pa={m.get('n_blocked_at_pa')} weak_prev={m.get('n_weak_entries_prevented')} "
            f"good_prev={m.get('n_good_entries_prevented')}"
        )
    lines.extend(["", "## Answers", ""])
    for k, v in answers.items():
        lines.append(f"- **{k}**: {v}")
    lines.extend(
        [
            "",
            "## Safety",
            "- no live strategy changes",
            "- pipeline input CSVs not overwritten",
            "- B3/R2 remain research overlays; productive blockers not enabled",
            "- outcomes computed after entry decisions only",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    pipeline_dir = Path(args.pipeline_dir)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    setups_raw, pa_raw, mom_raw = load_pipeline_tables(pipeline_dir)
    setups = filter_activated_setups(setups_raw, args.window_start, args.window_end)
    # Keep PA/mom rows that belong to window setups (first row per setup_id).
    setup_ids = set(setups["setup_id"].astype(str))
    pa = pa_raw[pa_raw["setup_id"].astype(str).isin(setup_ids)].copy() if len(pa_raw) else pa_raw
    mom = mom_raw[mom_raw["setup_id"].astype(str).isin(setup_ids)].copy() if len(mom_raw) else mom_raw
    pa_by = _first_by_setup(pa)
    mom_by = _first_by_setup(mom)

    r2 = prepare_r2_timeline(Path(args.risk_csv), args.window_start, args.window_end)
    candles, decision_index = prepare_candles(args.symbol, args.window_start, args.window_end, r2=r2)
    # Expand B3 onto the full candle decision grid (not only in-window index) for asof.
    all_dec = pd.DatetimeIndex(sorted(candles["decision_time"].unique()))
    b3 = prepare_b3_map(Path(args.b3_csv), all_dec)
    # Restrict b3 map slightly for size but keep warmup.
    warm = _to_utc(args.window_start) - pd.Timedelta(days=1)
    end = _to_utc(args.window_end)
    b3 = b3[(b3["decision_time"] >= warm) & (b3["decision_time"] < end + pd.Timedelta(hours=12))].copy()

    sequences_by_variant: dict[str, list[dict[str, Any]]] = {v: [] for v in VARIANTS}
    confirm_rows: list[dict[str, Any]] = []
    setup_rows: list[dict[str, Any]] = []

    for variant in VARIANTS:
        print(f"Running pipeline counterfactual {variant}...")
        cfg = variant_config(variant)
        use_r2 = r2 if cfg.use_r2 else None
        use_b3 = b3 if cfg.use_b3 else None
        for _, setup_row in setups.iterrows():
            sid = str(setup_row["setup_id"])
            setup_map = {k: (None if pd.isna(v) else v) for k, v in setup_row.to_dict().items()}
            if "side" not in setup_map or not setup_map.get("side"):
                setup_map["side"] = setup_map.get("setup_side")
            pa_row = pa_by.get(sid)
            mom_row = mom_by.get(sid)
            seq = simulate_sequence(
                setup_row=setup_map,
                pa_row=pa_row,
                existing_mom_row=mom_row,
                r2_timeline=use_r2,
                b3_timeline=use_b3,
                candles_5m=candles,
                decision_index=decision_index,
                cfg=cfg,
            )
            sequences_by_variant[variant].append(seq)
            flat = flatten_sequence(seq)
            setup_rows.append(flat)
            for c in seq.get("confirmation_candles") or []:
                confirm_rows.append(dict(c))

    all_sequences = [s for v in VARIANTS for s in sequences_by_variant[v]]
    outcomes = build_entry_outcomes(all_sequences, candles)
    # Attach quality onto sequences for downstream convenience
    out_by = {(str(r["variant"]), str(r["setup_id"])): r for _, r in outcomes.iterrows()} if len(outcomes) else {}
    for variant, seqs in sequences_by_variant.items():
        for s in seqs:
            key = (variant, str(s.get("setup_id")))
            if key in out_by:
                s["entry_quality"] = out_by[key].get("entry_quality")

    c0_check, c0_summary = c0_reproduction_check(
        sequences_by_variant["C0"], setups, pa, mom
    )
    baseline_out = outcomes[outcomes["variant"] == "C0"] if len(outcomes) else pd.DataFrame()

    metrics: dict[str, dict[str, Any]] = {}
    comparison_rows: list[dict[str, Any]] = []
    for variant in VARIANTS:
        m = variant_metrics(
            variant,
            sequences_by_variant[variant],
            outcomes,
            baseline_outcomes=baseline_out if variant != "C0" else None,
        )
        # Incremental fields vs C1/C2
        metrics[variant] = m
        comparison_rows.append(m)

    # Fill incremental metrics on comparison using C1/C2/C3
    incremental = r2_vs_b3_incremental(sequences_by_variant)
    for row in comparison_rows:
        if row["variant"] == "C3":
            row["additional_benefit_r2_vs_b3"] = next(
                (
                    r["value"]
                    for _, r in incremental.iterrows()
                    if r["metric"] == "incremental_r2_vs_b3_on_c0_entries"
                ),
                None,
            )
            row["additional_benefit_b3_vs_r2"] = next(
                (
                    r["value"]
                    for _, r in incremental.iterrows()
                    if r["metric"] == "incremental_b3_vs_r2_on_c0_entries"
                ),
                None,
            )
            row["overlap_r2_b3"] = next(
                (r["value"] for _, r in incremental.iterrows() if r["metric"] == "overlap_b3_and_r2"),
                None,
            )

    leaks = remaining_weak_leaks(sequences_by_variant, outcomes)
    false_blocks = false_blocked_good_entries(sequences_by_variant, outcomes)
    tvt = two_vs_three_table(sequences_by_variant, outcomes)
    focus = focus_table(sequences_by_variant, outcomes)
    changes = state_change_rows(all_sequences)
    answers = build_answers(
        sequences_by_variant,
        metrics,
        outcomes,
        c0_summary,
        leaks,
        false_blocks,
        tvt,
        incremental,
    )

    # --- write outputs ---
    pd.DataFrame(json_safe(setup_rows)).to_csv(out / "pipeline_counterfactual_setups.csv", index=False)
    seq_flat = [flatten_sequence(s) for s in all_sequences]
    pd.DataFrame(json_safe(seq_flat)).to_csv(out / "pipeline_counterfactual_sequences.csv", index=False)
    pd.DataFrame(json_safe(confirm_rows)).to_csv(
        out / "pipeline_counterfactual_confirmation_candles.csv", index=False
    )
    entries = [flatten_sequence(s) for s in all_sequences if s.get("entry_allowed")]
    pd.DataFrame(json_safe(entries)).to_csv(out / "pipeline_counterfactual_entries.csv", index=False)
    pd.DataFrame(json_safe(comparison_rows)).to_csv(
        out / "pipeline_counterfactual_variant_comparison.csv", index=False
    )
    outcomes_safe = pd.DataFrame(json_safe(outcomes.to_dict(orient="records"))) if len(outcomes) else pd.DataFrame()
    outcomes_safe.to_csv(out / "pipeline_counterfactual_entry_outcomes.csv", index=False)
    outcomes_safe.to_csv(out / "pipeline_counterfactual_vs_outcomes.csv", index=False)
    pd.DataFrame(json_safe(changes)).to_csv(out / "pipeline_counterfactual_state_changes.csv", index=False)
    pd.DataFrame(json_safe(focus.to_dict(orient="records"))).to_csv(
        out / "focus_setups_00055_00059.csv", index=False
    )
    pd.DataFrame(json_safe(leaks.to_dict(orient="records")) if len(leaks) else []).to_csv(
        out / "remaining_weak_entry_leaks.csv", index=False
    )
    pd.DataFrame(json_safe(false_blocks.to_dict(orient="records")) if len(false_blocks) else []).to_csv(
        out / "false_blocked_good_entries.csv", index=False
    )
    pd.DataFrame(json_safe(tvt.to_dict(orient="records"))).to_csv(
        out / "two_vs_three_confirmation_entries.csv", index=False
    )
    pd.DataFrame(json_safe(incremental.to_dict(orient="records"))).to_csv(
        out / "r2_vs_b3_incremental_value.csv", index=False
    )
    pd.DataFrame(json_safe(c0_check.to_dict(orient="records"))).to_csv(
        out / "c0_reproduction_check.csv", index=False
    )

    summary = {
        "symbol": args.symbol,
        "window_start": args.window_start,
        "window_end": args.window_end,
        "pipeline_dir": str(pipeline_dir),
        "b3_csv": args.b3_csv,
        "risk_csv": args.risk_csv,
        "output_dir": str(out),
        "focus_setups": list(FOCUS_SETUPS),
        "n_activated_setups": int(len(setups)),
        "c0_reproduction": c0_summary,
        "variant_metrics": metrics,
        "answers": answers,
        "safety": {
            "no_live_changes": True,
            "no_pipeline_csv_mutation": True,
            "outcomes_not_used_in_decisions": True,
            "counterfactual_enabled_default_false": True,
            "b3_config": "gate_variant==B3 from direction_gate_audit timeline",
            "r2_config": "risk_variant==R2 from risk_off_audit timeline",
        },
    }
    (out / "audit_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, allow_nan=False), encoding="utf-8"
    )
    write_readme(summary, out / "README.md")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--window-start", default="2026-03-01T00:00:00+00:00")
    p.add_argument("--window-end", default="2026-03-08T00:00:00+00:00")
    p.add_argument("--pipeline-dir", default=DEFAULT_PIPELINE)
    p.add_argument("--b3-csv", default=DEFAULT_B3)
    p.add_argument("--risk-csv", default=DEFAULT_RISK)
    p.add_argument("--output-dir", default=DEFAULT_OUT)
    args = p.parse_args(argv)
    run_audit(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
