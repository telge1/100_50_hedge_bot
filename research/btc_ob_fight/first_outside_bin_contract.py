"""FIRST_OUTSIDE_BIN scope contract — deterministic from profile price bin contract."""

from __future__ import annotations

from typing import Any

from .profile_edge_state import get_active_symbol, price_to_tick
from .instrument_contract import instrument_for
from .profile_price_bin_contract import INTERVAL_SEMANTICS, build_profile_price_bin_contract

FIRST_OUTSIDE_BIN_CONTRACT = "first_outside_bin_contract_v1"


def build_first_outside_bin_contract(
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    edges: dict[str, Any],
) -> dict[str, Any]:
    """Export FIRST_OUTSIDE_BIN intervals per edge with tick bounds."""
    bin_contract = build_profile_price_bin_contract(tpo_profile, volume_profile, edges=edges)
    step = bin_contract.get("price_step")
    out: dict[str, Any] = {
        "contract_version": FIRST_OUTSIDE_BIN_CONTRACT,
        "interval_semantics": INTERVAL_SEMANTICS,
        "price_step": step,
        "orderbook_tick_size": float(instrument_for(get_active_symbol()).tick_size),
        "edges": {},
    }
    if edges.get("profile_state") != "VALID" or not step:
        out["status"] = "NOT_COMPUTED"
        out["reason"] = "INVALID_PROFILE_EDGES_OR_PRICE_STEP"
        return out

    fo_bins = bin_contract.get("first_outside_bins") or {}
    for edge_key, edge_label in (("upper", "UPPER"), ("lower", "LOWER")):
        fo = fo_bins.get(edge_key) or {}
        lo, hi = fo.get("price_low"), fo.get("price_high")
        if lo is None or hi is None:
            out["edges"][edge_label] = {
                "status": "NOT_COMPUTED",
                "reason": "FIRST_OUTSIDE_BIN_NOT_DERIVABLE",
            }
            continue
        ticks = list(range(price_to_tick(lo), price_to_tick(hi - 1e-9) + 1))
        out["edges"][edge_label] = {
            "status": "COMPUTED",
            "price_low": lo,
            "price_high": hi,
            "price_step": step,
            "tick_low": price_to_tick(lo),
            "tick_high": price_to_tick(hi - 1e-9),
            "requested_tick_count": len(ticks),
            "interval_semantics": INTERVAL_SEMANTICS,
            "definition": (
                f"[{lo}, {hi}) first bin outside value area on {edge_label} edge"
            ),
        }
    out["status"] = "COMPUTED"
    return out
