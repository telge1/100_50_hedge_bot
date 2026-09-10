"""Independent oracle for wall-structure invariants (no production helpers)."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import LAYER_POST_BREACH, LAYER_POST_EPOCH, LAYER_PRE_EXISTING


def oracle_audit(
    *,
    gens_ep4: list[dict[str, Any]],
    gens_ep5: list[dict[str, Any]],
    liquidity_rows: list[dict[str, Any]],
    proxies: list[dict[str, Any]],
    breach_iso: str,
) -> dict[str, Any]:
    fp = fn = mass_err = look_ahead = 0
    details: list[dict[str, Any]] = []
    breach = _as_dt(breach_iso)

    # Epoch separation: no shared generation ids
    ids4 = {g["wall_generation_id"] for g in gens_ep4}
    ids5 = {g["wall_generation_id"] for g in gens_ep5}
    if ids4 & ids5:
        fp += 1
        details.append({"type": "epoch_id_overlap", "ids": list(ids4 & ids5)})

    # Same price different epochs → different ids (if both present)
    prices4 = {}
    for g in gens_ep4:
        prices4.setdefault(g["price"], []).append(g["wall_generation_id"])
    for g in gens_ep5:
        if g["price"] in prices4 and g["wall_generation_id"] in prices4[g["price"]]:
            fp += 1
            details.append({"type": "same_id_across_epochs", "price": g["price"]})

    # Layer classification
    for g in gens_ep4:
        st = _as_dt(g["start_time"])
        expect = LAYER_PRE_EXISTING if st < breach else LAYER_POST_BREACH
        if g.get("layer_class") != expect:
            fn += 1
            details.append(
                {
                    "type": "layer_class",
                    "id": g["wall_generation_id"],
                    "got": g.get("layer_class"),
                    "expect": expect,
                }
            )
    for g in gens_ep5:
        if g.get("layer_class") != LAYER_POST_EPOCH:
            fp += 1
            details.append({"type": "ep5_not_post_epoch", "id": g["wall_generation_id"]})

    # Proxies must stay in one epoch
    for p in proxies:
        if p.get("replay_epoch") not in (4, 5):
            fp += 1
        if p.get("previous_wall_generation_id") in ids5 and p.get("replay_epoch") == 4:
            fp += 1
            details.append({"type": "proxy_cross_epoch"})

    # Centroid recompute for a sample of liquidity rows
    for r in liquidity_rows[:: max(1, len(liquidity_rows) // 20 or 1)]:
        if r.get("look_ahead"):
            look_ahead += 1
        # mass non-negative
        for k in ("total_ask_qty", "quantity_above_original_wall", "quantity_at_original_wall"):
            v = r.get(k)
            if v is not None and float(v) < -1e-9:
                mass_err += 1
                details.append({"type": "negative_mass", "k": k, "v": v})

    # Fill/pull non-negative on gens
    for g in gens_ep4 + gens_ep5:
        for k in ("cumulative_attributed_fills", "cumulative_residual_pulls", "cumulative_refills"):
            if float(g.get(k) or 0) < -1e-9:
                mass_err += 1

    ok = fp == 0 and fn == 0 and mass_err == 0 and look_ahead == 0
    return {
        "ok": ok,
        "fp": fp,
        "fn": fn,
        "mass_error": mass_err,
        "look_ahead": look_ahead,
        "n_details": len(details),
        "details_head": details[:30],
    }
