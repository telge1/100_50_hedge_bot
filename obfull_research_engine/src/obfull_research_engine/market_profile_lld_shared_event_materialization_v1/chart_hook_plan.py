"""Plan only — no live endpoint, route, or dashboard change."""

from __future__ import annotations

from typing import Any

HOOK_PLAN: dict[str, Any] = {
    "status": "DOCUMENTED_NOT_IMPLEMENTED",
    "live_endpoint_changed": False,
    "feature_flag_default": "off",
    "hook_site": (
        "dashboard/market_profile_v1/service.py::load_profiles after payload build, "
        "and dashboard/research_charts/service.py::pane_bundle after causal/live LLD pack"
    ),
    "payload_to_store": {
        "market_profile": "full GET /api/market-profile/profiles JSON minus optional bins if size-capped",
        "lld": "POST /api/research/pane liquidity overlays + liquidity_location_as_of + config",
    },
    "privacy_storage": (
        "Research-only local disk under results/; no user PII; symbol/time/config only. "
        "Do not ship payloads to an external store in the first flag."
    ),
    "rotation": "keep last N requests per symbol (suggest 64) or 14 days; delete oldest complete files",
    "atomic_storage": "write *.json.tmp then os.replace; never mutate COMPLETE snapshots",
    "feature_flag": "MP_PERSIST_CHART_PAYLOADS=0 default; process env, not a new route",
    "expected_cost": (
        "One BTCUSDT 30-day 30m profile JSON with bins is large (many windows × bins). "
        "Estimate: persist only the visible window set for the current request, "
        "or persist without bins plus a hash of bins. Disk: tens of MB per heavy request. "
        "CPU: negligible vs ClickHouse compute."
    ),
    "do_not_in_phase_2": [
        "change routes",
        "restart dashboard",
        "alter responses",
        "enable persistence",
    ],
}
