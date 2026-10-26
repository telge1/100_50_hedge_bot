"""Read-only API helpers for /ema-signale V2 backtest mode."""

from __future__ import annotations

from typing import Any

from research_charts.historical_lmv2.smoke_runs import (
    config_has_run,
    get_run,
    list_runs,
    paginated_results,
)


def api_list_runs() -> dict[str, Any]:
    return {"success": True, "runs": list_runs()}


def api_results(
    run_id: str,
    variant_key: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    return paginated_results(
        run_id,
        variant_key=variant_key,
        page=page,
        page_size=page_size,
    )


def api_resolve_config(pool_detail: int, guard_on: bool) -> dict[str, Any]:
    rid = config_has_run(pool_detail, guard_on)
    if rid is None:
        return {
            "success": True,
            "available": False,
            "message": "Noch kein Backtest-Ergebnis für diese Konfiguration vorhanden.",
        }
    spec = get_run(rid)
    vkey = f"15m_pd{int(pool_detail)}_guard_{'on' if guard_on else 'off'}"
    return {
        "success": True,
        "available": True,
        "run_id": rid,
        "variant_key": vkey,
        "warning": spec.warning,
    }
