"""Integrity checks and prefix parity."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc


class AEFCausalityError(RuntimeError):
    pass


def assert_finite_episode(ep: dict[str, Any]) -> None:
    for k, v in ep.items():
        if isinstance(v, float) and not math.isfinite(v):
            raise AEFCausalityError(f"non_finite:{k}")


def assert_entry_after_final(ep: dict[str, Any]) -> None:
    final = parse_utc(ep["final_decision_ts"])
    entry = parse_utc(ep["diagnostic_earliest_entry_ts"])
    if not (entry > final):
        raise AEFCausalityError("earliest_entry_not_after_final")


def prefix_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Comparable subset for prefix parity."""
    cands = []
    for c in result.get("candidates") or []:
        cands.append(
            {
                "episode_id": c["episode_id"],
                "direction": c["direction"],
                "final_decision_ts": c["final_decision_ts"],
                "compression_confirmed_ts": c["compression_confirmed_ts"],
                "counter_confirmed_ts": c["counter_confirmed_ts"],
                "ordinal_flip_score": c["ordinal_flip_score"],
                "strong_same_side_impact_veto": c["strong_same_side_impact_veto"],
            }
        )
    comps = [
        {
            "t0": x["t0"],
            "direction": x["direction"],
            "allowed": x["allowed"],
            "reason_code": x["reason_code"],
            "strong_same_side_impact_veto": x["strong_same_side_impact_veto"],
        }
        for x in (result.get("compressions") or [])
    ]
    return {
        "candidates": sorted(cands, key=lambda x: (x["final_decision_ts"], x["episode_id"])),
        "compressions": sorted(comps, key=lambda x: (x["t0"], x["direction"])),
        "n_transitions": len(result.get("transitions") or []),
    }


def compare_prefix(full_snap: dict[str, Any], prefix_snap: dict[str, Any], *, cutoff: str) -> list[str]:
    """Ensure every prefix-decided compression/candidate matches full run."""
    errors: list[str] = []
    cut = parse_utc(cutoff)
    full_c = {
        (c["episode_id"]): c
        for c in full_snap["candidates"]
        if parse_utc(c["final_decision_ts"]) <= cut
    }
    pref_c = {c["episode_id"]: c for c in prefix_snap["candidates"]}
    if full_c.keys() != pref_c.keys():
        errors.append(f"candidate_set_mismatch_at_{cutoff}")
    for eid, fc in full_c.items():
        pc = pref_c.get(eid)
        if pc != fc:
            errors.append(f"candidate_diff:{eid}")
    full_comp = [
        c for c in full_snap["compressions"] if parse_utc(c["t0"]) < cut  # decided by t0+10s approx
    ]
    # Compare compression decisions whose confirmed ts <= cut
    # Use reason for same t0/direction present in prefix
    pref_map = {(c["t0"], c["direction"]): c for c in prefix_snap["compressions"]}
    full_map = {(c["t0"], c["direction"]): c for c in full_snap["compressions"]}
    for key, fc in full_map.items():
        # compression confirmed at t0+10s
        from datetime import timedelta

        conf = parse_utc(key[0]) + timedelta(seconds=10)
        if conf > cut:
            continue
        pc = pref_map.get(key)
        if pc is None:
            errors.append(f"missing_compression:{key}")
        elif (pc["allowed"], pc["reason_code"], pc["strong_same_side_impact_veto"]) != (
            fc["allowed"],
            fc["reason_code"],
            fc["strong_same_side_impact_veto"],
        ):
            errors.append(f"compression_diff:{key}")
    return errors
