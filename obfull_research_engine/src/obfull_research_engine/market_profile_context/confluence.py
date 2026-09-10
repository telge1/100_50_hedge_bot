"""Confluence of actually nearby edges — no silent VAL=VAL merge."""

from __future__ import annotations

from typing import Any

from . import CONFLUENCE_BINS, TIMEFRAMES


def collect_edges(bundles: dict[str, dict[str, Any]], *, price: float | None) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for tf in TIMEFRAMES:
        bun = bundles.get(tf) or {}
        for kind in ("previous_closed", "developing"):
            prof = bun.get(kind)
            if not prof:
                continue
            step = float(prof.get("price_step") or 0) or None
            tpo = prof.get("tpo") or {}
            vol = prof.get("volume") or {}
            cands = [
                ("tpo_val", tpo.get("val"), "lower"),
                ("tpo_vah", tpo.get("vah"), "upper"),
                ("volume_val", vol.get("val"), "lower"),
                ("volume_vah", vol.get("vah"), "upper"),
                ("range_low", prof.get("range_low"), "lower"),
                ("range_high", prof.get("range_high"), "upper"),
            ]
            for etype, level, side in cands:
                if level is None:
                    continue
                dist = None if price is None else abs(price - float(level))
                edges.append(
                    {
                        "timeframe": tf,
                        "profile_kind": kind,
                        "edge_type": etype,
                        "side": side,
                        "level": float(level),
                        "price_step": step,
                        "distance_to_price": dist,
                    }
                )
    return edges


def confluence_report(edges: list[dict[str, Any]], *, price: float | None) -> dict[str, Any]:
    lower = [e for e in edges if e["side"] == "lower" and e["profile_kind"] == "previous_closed"]
    upper = [e for e in edges if e["side"] == "upper" and e["profile_kind"] == "previous_closed"]
    # developing edges are also relevant if available
    lower += [e for e in edges if e["side"] == "lower" and e["profile_kind"] == "developing"]
    upper += [e for e in edges if e["side"] == "upper" and e["profile_kind"] == "developing"]

    def clusters(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        used = set()
        out = []
        for i, a in enumerate(group):
            if i in used:
                continue
            members = [a]
            used.add(i)
            sa = float(a["price_step"] or 0) or 1.0
            for j, b in enumerate(group):
                if j in used:
                    continue
                sb = float(b["price_step"] or 0) or sa
                tol = CONFLUENCE_BINS * max(sa, sb)
                if abs(a["level"] - b["level"]) <= tol:
                    members.append(b)
                    used.add(j)
            if len(members) < 2:
                continue
            levels = [m["level"] for m in members]
            out.append(
                {
                    "n": len(members),
                    "min_level": min(levels),
                    "max_level": max(levels),
                    "max_span": max(levels) - min(levels),
                    "timeframes": sorted({m["timeframe"] for m in members}),
                    "edge_types": sorted({m["edge_type"] for m in members}),
                    "profile_kinds": sorted({m["profile_kind"] for m in members}),
                    "members": members,
                }
            )
        return out

    near_lower = []
    near_upper = []
    if price is not None:
        for e in edges:
            step = float(e["price_step"] or 0) or 0
            if step <= 0 or e["distance_to_price"] is None:
                continue
            if e["distance_to_price"] <= CONFLUENCE_BINS * step:
                (near_lower if e["side"] == "lower" else near_upper).append(e)

    return {
        "lower_clusters": clusters(lower),
        "upper_clusters": clusters(upper),
        "near_lower_edges": near_lower,
        "near_upper_edges": near_upper,
        "n_near_lower": len(near_lower),
        "n_near_upper": len(near_upper),
        "note": "Confluence requires levels within CONFLUENCE_BINS * max(price_step); not claimed equal.",
    }
