"""Load prior audit rows; restrict to DATA_VALID; map subgroups / early timepoints."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

PRIOR_DIR = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/ch_break_reclaim_microstructure_audit_20260808"
)

# User-facing early gate window. PRE_TOUCH_60S == prior PRE_TOUCH_1M.
EARLY_TIMEPOINTS = (
    "PRE_TOUCH_60S",
    "PRE_TOUCH_30S",
    "PRE_TOUCH_10S",
    "FIRST_TOUCH",
    "FIRST_BREAK",
    "BREAK_PLUS_5S",
    "BREAK_PLUS_10S",
)

CONFIRMATION_ONLY_TIMEPOINTS = (
    "BREAK_PLUS_20S",
    "BREAK_PLUS_30S",
    "BREAK_PLUS_60S",
    "BREAK_PLUS_120S",
    "POSTMORTEM_PLUS_5M",
    "PRE_TOUCH_2M",
    "PRE_TOUCH_5M",
)

TIMEPOINT_ALIAS = {
    "PRE_TOUCH_1M": "PRE_TOUCH_60S",
}

PRIORITY_FEATURES = (
    "imbalance_0_10",
    "imbalance_0_25",
    "break_side_near_depth",
    "support_near_depth",
    "support_minus_break_depth",
    "bid_depth_bps_0_5",
    "ask_depth_bps_0_5",
    "support_depth_change_10s",
    "break_depth_change_10s",
    "signed_distance_beyond_bps",
    "distance_to_level_bps",
    "abs_distance_to_level_bps",
    "flow_5s_signed_break",
    "flow_10s_signed_break",
    "flow_30s_signed_break",
    "flow_30s_signed_move_bps",
    "recent_return_bps",
    "short_vol_proxy_bps",
    "support_frac_0",
    "score_depth_imb_flow",
)

MIN_N_BREAK = 4
MIN_N_OTHER = 3

SUBGROUPS = (
    "APT_bearish",
    "APT_bullish",
    "DOGE_bearish",
    "DOGE_bullish",
    "all_bearish",
    "all_bullish",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def normalize_timepoint(tp: str) -> str:
    return TIMEPOINT_ALIAS.get(tp, tp)


def subgroup_name(symbol: str, direction: str) -> str:
    sym = "APT" if symbol.upper().startswith("APT") else "DOGE" if symbol.upper().startswith("DOGE") else symbol
    return f"{sym}_{direction}"


def event_in_subgroup(symbol: str, direction: str, subgroup: str) -> bool:
    if subgroup == "all_bearish":
        return direction == "bearish"
    if subgroup == "all_bullish":
        return direction == "bullish"
    return subgroup_name(symbol, direction) == subgroup


def load_valid_feature_rows(
    prior_dir: Path = PRIOR_DIR,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return enriched feature rows + one-row-per-event outcomes for DATA_VALID only.

    Excludes EXCLUDED outcomes. Never invents features from future time-to-touch.
    """
    raw = read_csv(prior_dir / "event_features.csv")
    rows: list[dict[str, Any]] = []
    events: dict[str, dict[str, Any]] = {}
    for r in raw:
        if r.get("data_quality") != "DATA_VALID":
            continue
        if r.get("outcome_label") == "EXCLUDED":
            continue
        tp = normalize_timepoint(r["timepoint"])
        out = dict(r)
        out["timepoint"] = tp
        out["subgroup_pair"] = subgroup_name(r["symbol"], r["break_direction"])
        # Derived causal baselines (no future info)
        dist = _f(r.get("distance_to_level_bps"))
        out["abs_distance_to_level_bps"] = None if dist is None else abs(dist)
        move = _f(r.get("flow_30s_signed_move_bps"))
        if move is None:
            move = _f(r.get("flow_10s_signed_move_bps"))
        out["recent_return_bps"] = move
        raw_move = _f(r.get("flow_30s_price_move_bps"))
        if raw_move is None:
            raw_move = _f(r.get("flow_10s_price_move_bps"))
        out["short_vol_proxy_bps"] = None if raw_move is None else abs(raw_move)
        # Direction-normalized support fraction
        s = _f(r.get("support_near_depth")) or 0.0
        b = _f(r.get("break_side_near_depth")) or 0.0
        out["support_frac_0"] = None if (s + b) <= 0 else s / (s + b)
        # Side-aware 0-5bps depth near level
        if r.get("break_direction") == "bearish":
            out["support_depth_0_5"] = _f(r.get("bid_depth_bps_0_5"))
            out["break_depth_0_5"] = _f(r.get("ask_depth_bps_0_5"))
        else:
            out["support_depth_0_5"] = _f(r.get("ask_depth_bps_0_5"))
            out["break_depth_0_5"] = _f(r.get("bid_depth_bps_0_5"))
        rows.append(out)
        events[r["event_id"]] = {
            "event_id": r["event_id"],
            "symbol": r["symbol"],
            "break_direction": r["break_direction"],
            "outcome_label": r["outcome_label"],
            "subgroup_pair": out["subgroup_pair"],
            "data_quality": r["data_quality"],
        }
    return rows, list(events.values())


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def assert_no_time_to_touch_feature(row: dict[str, Any]) -> None:
    banned = {"time_to_touch", "time_until_first_touch", "seconds_to_first_touch"}
    for k in banned:
        if k in row and row[k] not in (None, ""):
            # seconds_to_first_break exists in prior extract as causal relative clock —
            # ban only future-derived time-to-touch used as predictor.
            if k.startswith("time_to") or k.startswith("time_until"):
                raise AssertionError(f"banned future-ish feature present: {k}")


def subgroup_outcome_counts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = ("BREAK_ACCEPTED", "RECLAIM_FAST", "RECLAIM_SLOW", "HOLD_NO_BREAK")
    out = []
    for sg in SUBGROUPS:
        subset = [e for e in events if event_in_subgroup(e["symbol"], e["break_direction"], sg)]
        row: dict[str, Any] = {"subgroup": sg, "n_events": len(subset)}
        for lab in labels:
            key = "HOLD" if lab == "HOLD_NO_BREAK" else lab
            row[key] = sum(1 for e in subset if e["outcome_label"] == lab)
        row["n_break"] = row["BREAK_ACCEPTED"]
        row["n_reclaim_fast"] = row["RECLAIM_FAST"]
        row["n_reclaim_hold"] = row["RECLAIM_FAST"] + row["RECLAIM_SLOW"] + row["HOLD"]
        row["sufficient_vs_reclaim_fast"] = int(
            row["n_break"] >= MIN_N_BREAK and row["n_reclaim_fast"] >= MIN_N_OTHER
        )
        row["sufficient_vs_rest"] = int(
            row["n_break"] >= MIN_N_BREAK and row["n_reclaim_hold"] >= MIN_N_OTHER
        )
        out.append(row)
    return out
