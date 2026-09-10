"""Independent oracle for defense-chain invariants."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import EPSILON, TRANSITION_CENSORED


def oracle_audit(
    *,
    scored: list[dict[str, Any]],
    summary: dict[str, Any],
    chain: dict[str, Any],
    chain_ep5: dict[str, Any] | None,
    breach_iso: str,
) -> dict[str, Any]:
    fp = fn = mass_err = look_ahead = 0
    details: list[dict[str, Any]] = []

    # generation_count vs relevant
    if summary.get("generation_count") == summary.get("relevant_wall_count") and summary.get("generation_count", 0) > 100:
        # not automatically an error, but flag if equal and huge — soft check: relevant must be <= gens
        pass
    if int(summary.get("relevant_wall_count") or 0) > int(summary.get("generation_count") or 0):
        fp += 1
        details.append({"type": "relevant_gt_generations"})

    # Past-only: rolling percentile must be null or in [0,1]
    for g in scored:
        p = g.get("rolling_size_percentile")
        if p is not None and not (0.0 - 1e-9 <= float(p) <= 1.0 + 1e-9):
            fp += 1
            details.append({"type": "bad_percentile", "id": g.get("wall_generation_id")})

    # Chain nodes subset of relevant
    rel_ids = {g["wall_generation_id"] for g in scored if g.get("is_relevant_at_entry")}
    node_ids = [n["wall_generation_id"] for n in chain.get("nodes") or []]
    if len(node_ids) != len(set(node_ids)):
        fp += 1
        details.append({"type": "duplicate_node"})
    for nid in node_ids:
        if nid not in rel_ids:
            fp += 1
            details.append({"type": "node_not_relevant", "id": nid})

    # Mass non-negative
    for n in chain.get("nodes") or []:
        for k in ("cumulative_hits", "cumulative_pulls", "cumulative_refills", "initial_qty", "peak_qty"):
            if float(n.get(k) or 0) < -1e-9:
                mass_err += 1

    # Epoch separation
    if int(chain.get("replay_epoch") or -1) == 4 and chain_ep5:
        ids4 = set(node_ids)
        ids5 = {n["wall_generation_id"] for n in (chain_ep5.get("nodes") or [])}
        if ids4 & ids5:
            fp += 1
            details.append({"type": "cross_epoch_node_ids"})
        for tr in chain_ep5.get("transitions") or []:
            if tr.get("from_generation_id") in ids4 or tr.get("to_generation_id") in ids4:
                fp += 1
                details.append({"type": "cross_epoch_transition"})

    # No transition bridging epochs inside ep4 chain
    for tr in chain.get("transitions") or []:
        if tr.get("replay_epoch") not in (None, 4, chain.get("replay_epoch")):
            fp += 1

    # Censoring present on ep1
    if chain.get("chain_status") != "CENSORED_BY_EPOCH_BOUNDARY":
        fn += 1
        details.append({"type": "missing_censor_status"})

    if chain.get("look_ahead"):
        look_ahead += 1

    # Pre-existing vs new on nodes vs breach
    breach = _as_dt(breach_iso)
    for n in chain.get("nodes") or []:
        st = _as_dt(n["first_visible_at"])
        expect_pre = st < breach
        if bool(n.get("pre_existing")) != expect_pre:
            # layer may use layer_class; allow if consistent with start vs breach
            if expect_pre and not n.get("pre_existing"):
                fn += 1
                details.append({"type": "pre_existing_fn", "id": n["wall_generation_id"]})
            if (not expect_pre) and n.get("pre_existing"):
                fp += 1
                details.append({"type": "pre_existing_fp", "id": n["wall_generation_id"]})

    ok = fp == 0 and fn == 0 and mass_err == 0 and look_ahead == 0
    return {
        "ok": ok,
        "fp": fp,
        "fn": fn,
        "mass_error": mass_err,
        "look_ahead": look_ahead,
        "n_details": len(details),
        "details_head": details[:25],
    }
