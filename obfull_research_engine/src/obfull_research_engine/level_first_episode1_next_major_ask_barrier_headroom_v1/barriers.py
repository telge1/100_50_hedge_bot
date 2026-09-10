"""Nearest / strongest / first-blocking barrier selection — never skip nearer walls."""

from __future__ import annotations

from typing import Any

from . import MAJOR_PERCENTILE_VIEWS
from .walls import next_wall_at_percentile


def select_barriers(
    *,
    candidates: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    default_major_q: float = 0.95,
) -> dict[str, Any]:
    """A/B/C views. A nearer relevant wall is never hidden by a farther giant."""
    major_wall = next_wall_at_percentile(candidates, percentile=default_major_q)

    def _cluster_major_edge(c: dict[str, Any]) -> float | None:
        """Nearest price among members that themselves meet the major Q view."""
        # member_prices alone insufficient — use peak if combined pct ok and
        # prefer nearest_price only when combined_past_only_percentile meets Q
        # and number_of_relevant_walls>=1; edge = nearest_price of cluster
        # but only if peak percentile-capable (combined >= Q).
        if (c.get("combined_past_only_percentile") or 0) + 1e-15 < float(default_major_q):
            return None
        if int(c.get("number_of_relevant_walls") or 0) < 1:
            return None
        return float(c["nearest_price"])

    major_clusters = []
    for c in clusters:
        edge = _cluster_major_edge(c)
        if edge is not None:
            major_clusters.append({**c, "major_edge_price": edge})

    nearest_cluster = min(major_clusters, key=lambda c: c["major_edge_price"]) if major_clusters else None

    # NEAREST_MAJOR_BARRIER: nearer of Q-major wall vs major-cluster edge
    nearest = None
    opts = []
    if major_wall:
        opts.append(("wall", float(major_wall["price"]), major_wall, nearest_cluster))
    if nearest_cluster:
        opts.append(("cluster", float(nearest_cluster["major_edge_price"]), major_wall, nearest_cluster))
    if opts:
        kind, px, w, c = min(opts, key=lambda x: x[1])
        nearest = {"kind": kind, "price": px, "wall": major_wall, "cluster": c}

    strongest_wall = max(candidates, key=lambda c: c["qty_base"]) if candidates else None
    strongest_cluster = max(clusters, key=lambda c: c["total_cluster_notional"]) if clusters else None

    return {
        "NEAREST_MAJOR_BARRIER": nearest,
        "STRONGEST_VISIBLE_BARRIER": {
            "wall": strongest_wall,
            "cluster": strongest_cluster,
        },
        "FIRST_BLOCKING_BARRIER": {
            "wall": major_wall,
            "cluster": nearest_cluster,
            "research_view_percentile": default_major_q,
            "note": "First barrier on the upward path that satisfies past-only Q view.",
        },
        "default_major_percentile_view": default_major_q,
        "all_percentile_views_reported_separately": list(MAJOR_PERCENTILE_VIEWS),
    }
