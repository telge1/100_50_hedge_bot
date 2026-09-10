"""Composite proxy candidate rules (outcome-blind, causal window only)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .baseline import BaselineSlice, compare_extreme, robust_stats
from .triggers import _baseline_feature_array, _feature_value


def _fired_in_window(
    primary_history: list[dict[str, bool]],
    i: int,
    window: int,
    name: str,
) -> bool:
    lo = max(0, i - window + 1)
    for j in range(lo, i + 1):
        if primary_history[j].get(name):
            return True
    return False


def _names_in_window(
    primary_history: list[dict[str, bool]],
    i: int,
    window: int,
) -> list[str]:
    lo = max(0, i - window + 1)
    names: list[str] = []
    seen: set[str] = set()
    for j in range(lo, i + 1):
        for k, v in primary_history[j].items():
            if v and k not in seen:
                seen.add(k)
                names.append(k)
    return names


def _extra_check(
    *,
    name: str,
    df: pd.DataFrame,
    i: int,
    row: pd.Series,
    baseline: BaselineSlice,
    cfg: dict[str, Any],
    primary_history: list[dict[str, bool]],
) -> bool:
    params = cfg["extra_check_params"][name]
    z_thr = float(cfg["robust_z_threshold"])
    q_thr = float(cfg["quantile_threshold"])
    mad_scale = float(cfg["mad_scale"])

    if name == "compression_then_expansion":
        look = int(params["lookback_seconds"])
        feat = params["activity_feature"]
        lo = max(baseline.start_idx, i - look)
        if i - lo < 5:
            return False
        past = df[feat].iloc[lo:i].to_numpy(dtype=float)
        if past.size == 0:
            return False
        q_low = float(np.quantile(past, float(params["low_quantile"])))
        # compression: recent half below q_low often; expansion: current extreme high
        mid = lo + (i - lo) // 2
        early = df[feat].iloc[lo:mid].to_numpy(dtype=float)
        if early.size == 0:
            return False
        compressed = float(np.median(early)) <= q_low
        stats = robust_stats(
            _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, feat),
            q_thr,
        )
        cur = float(row[feat])
        expand = compare_extreme(x=cur, stats=stats, compare="high", z_threshold=z_thr, mad_scale=mad_scale)
        return bool(compressed and expand["fired"])

    feature = params["feature"]
    compare = params["compare"]
    x = _feature_value(row, feature)
    arr = _baseline_feature_array(df, baseline.start_idx, baseline.end_idx_exclusive, feature)
    stats = robust_stats(arr, q_thr)
    return bool(
        compare_extreme(x=x, stats=stats, compare=compare, z_threshold=z_thr, mad_scale=mad_scale)["fired"]
    )


def evaluate_compositions(
    *,
    df: pd.DataFrame,
    i: int,
    row: pd.Series,
    baseline: BaselineSlice,
    cfg: dict[str, Any],
    primary_history: list[dict[str, bool]],
    primary_now: dict[str, bool],
) -> dict[str, dict[str, Any]]:
    window = int(cfg["composition_window_seconds"])
    comps = cfg["compositions"]
    out: dict[str, dict[str, Any]] = {}
    fired_now_types: list[str] = []

    def win(name: str) -> bool:
        return _fired_in_window(primary_history, i, window, name)

    # Evaluate named compositions except UNCLEAR first
    for cname, spec in comps.items():
        if cname == "UNCLEAR_HIGH_ACTIVITY":
            continue

        if cname == "REFILL_DEFENSE_PROXY":
            ok = False
            for a, b in spec.get("all_of_window") or []:
                if win(a) and win(b):
                    ok = True
                    break
            out[cname] = {
                "fired": ok,
                "direction_hint": spec["direction_hint"],
                "exact_fields": list(spec.get("exact_fields") or []),
                "proxy_fields": list(spec.get("proxy_fields") or []),
                "trigger_names": _names_in_window(primary_history, i, window),
            }
            if ok:
                fired_now_types.append(cname)
            continue

        if cname == "COMPRESSION_EXPANSION_PROXY":
            ok = all(
                _extra_check(
                    name=chk, df=df, i=i, row=row, baseline=baseline, cfg=cfg, primary_history=primary_history
                )
                for chk in (spec.get("extra_checks") or [])
            )
            out[cname] = {
                "fired": ok,
                "direction_hint": spec["direction_hint"],
                "exact_fields": list(spec.get("exact_fields") or []),
                "proxy_fields": list(spec.get("proxy_fields") or []),
                "trigger_names": _names_in_window(primary_history, i, window),
            }
            if ok:
                fired_now_types.append(cname)
            continue

        all_ok = all(win(t) for t in (spec.get("all_of") or []))
        absent_ok = all(not win(t) for t in (spec.get("require_absent") or []))
        any_list = list(spec.get("any_of") or [])
        any_ok = True if not any_list else any(win(t) for t in any_list)
        any2 = list(spec.get("any_of_secondary") or [])
        any2_ok = True if not any2 else any(win(t) for t in any2)
        extras = list(spec.get("extra_checks") or [])
        extra_ok = all(
            _extra_check(
                name=chk, df=df, i=i, row=row, baseline=baseline, cfg=cfg, primary_history=primary_history
            )
            for chk in extras
        )
        ok = bool(all_ok and absent_ok and any_ok and any2_ok and extra_ok)
        out[cname] = {
            "fired": ok,
            "direction_hint": spec["direction_hint"],
            "exact_fields": list(spec.get("exact_fields") or []),
            "proxy_fields": list(spec.get("proxy_fields") or []),
            "trigger_names": _names_in_window(primary_history, i, window),
        }
        if ok:
            fired_now_types.append(cname)

    # UNCLEAR_HIGH_ACTIVITY last
    uspec = comps["UNCLEAR_HIGH_ACTIVITY"]
    names = _names_in_window(primary_history, i, window)
    min_n = int(uspec.get("min_primary_triggers", 3))
    excluded = set(uspec.get("exclude_if_any_composition") or [])
    blocked = any(out.get(x, {}).get("fired") for x in excluded)
    ok_u = (len(names) >= min_n) and (not blocked) and any(primary_now.values())
    out["UNCLEAR_HIGH_ACTIVITY"] = {
        "fired": bool(ok_u),
        "direction_hint": uspec["direction_hint"],
        "exact_fields": list(uspec.get("exact_fields") or []),
        "proxy_fields": list(uspec.get("proxy_fields") or []),
        "trigger_names": names,
    }
    return out
