#!/usr/bin/env python3
"""CLI: config-driven pool landscape short backtest → result.json + latest_result.json."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D9 = ROOT / "tools/phase_bt_d9_first_24h_short_pnl_v1"
BT_D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
for p in (
    str(TOOLS),
    str(BT_D6),
    str(BT_D7),
    str(BT_D9),
    str(BT_D8),
    str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_validate_v1 import ConfigValidationError, load_and_validate_config  # noqa: E402
from config_yaml_v1 import load_config_with_provenance  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from short_research_engine_v1 import run_short_research_backtest  # noqa: E402

from cache_store_v1 import load_bundle_manifest  # noqa: E402
from preflight_v1 import run_preflight  # noqa: E402
from result_publish_v1 import (  # noqa: E402
    build_result_document,
    integration_from_engine,
    publish_result,
)
from window_v1 import iter_5m_closes_half_open  # noqa: E402


def _required_closes(cfg: dict) -> list[str]:
    bt = cfg["backtest"]
    if bt.get("end_utc_exclusive"):
        return iter_5m_closes_half_open(bt["start_utc"], bt["end_utc"])
    from cache_builder_v1 import iter_5m_closes_inclusive  # noqa: WPS433

    return iter_5m_closes_inclusive(bt["start_utc"], bt["end_utc"])


def _check_cache(cfg: dict) -> dict:
    dens = cfg.get("density") or {}
    if dens.get("density_source") != "validated_cache":
        return {"ok": True, "mode": "canonical_provider"}
    vc = dens.get("validated_cache") or {}
    path = Path(str(vc.get("bundle_path") or ""))
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_dir():
        return {"ok": False, "blockers": ["CACHE_BUNDLE_MISSING"], "path": str(path)}
    manifest = load_bundle_manifest(path)
    closes = _required_closes(cfg)
    have = {e["decision_close_utc"] for e in manifest.get("entries") or []}
    missing = [c for c in closes if c not in have]
    return {
        "ok": len(missing) == 0,
        "missing_count": len(missing),
        "missing_sample": missing[:5],
        "bundle": str(path),
    }


def run_backtest(config_path: Path, run_id: str | None = None, *, publish_latest: bool = True) -> int:
    cfg, config_provenance = load_config_with_provenance(config_path)
    closes = _required_closes(cfg)
    pf = run_preflight(config_path, required_closes=closes, require_complete_cache=False)
    if pf.get("verdict") == "BT_D9_24H_PNL_BLOCKED" and pf.get("blockers"):
        # reuse BT-D9 preflight codes
        blockers = [b for b in pf["blockers"] if b not in ("CACHE_INCOMPLETE", "CACHE_BUNDLE_MISSING")]
        if blockers:
            print(json.dumps({"verdict": "BLOCKED", "preflight": pf}, indent=2))
            return 2

    cache_chk = _check_cache(cfg)
    if not cache_chk.get("ok"):
        print(json.dumps({"verdict": "BLOCKED", "cache": cache_chk}, indent=2))
        return 2

    prices = load_prices_from_ssot_window(
        cfg["backtest"]["start_utc"],
        cfg["backtest"]["end_utc"],
        end_utc_exclusive=bool(cfg["backtest"].get("end_utc_exclusive")),
    )
    if not prices:
        print(json.dumps({"verdict": "BLOCKED", "error": "NO_OHLC"}, indent=2))
        return 2

    t0 = time.perf_counter()
    out = run_short_research_backtest(cfg, create_density_session(cfg), prices)
    wall = round(time.perf_counter() - t0, 3)

    rid = run_id or datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    integration = integration_from_engine(cfg, out)
    density_prov = {}
    if cfg["density"].get("density_source") == "validated_cache":
        bp = Path(cfg["density"]["validated_cache"]["bundle_path"])
        if not bp.is_absolute():
            bp = ROOT / bp
        m = load_bundle_manifest(bp)
        density_prov = {
            "bundle": str(bp),
            "pipeline_line_id": m.get("required_pipeline_line_id"),
            "provider_config_hash": m.get("required_config_hash"),
            "entry_count": m.get("entry_count"),
            "density_source": "validated_cache",
        }
    else:
        density_prov = {"density_source": "canonical_provider"}

    doc = build_result_document(
        run_id=rid,
        config=cfg,
        integration=integration,
        density_provenance=density_prov,
        meta={"backtest_wall_s": wall},
    )
    path = publish_result(rid, doc, publish_latest=publish_latest)
    run_dir = path.parent
    norm_path = run_dir / "config.normalized.json"
    prov_path = run_dir / "config.provenance.json"
    norm_path.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
    prov_path.write_text(json.dumps(config_provenance, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": "OK",
                "run_id": rid,
                "result": str(path),
                "signals": len(doc["signals"]),
                "config_hash": config_provenance.get("normalized_config_hash"),
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument(
        "--no-publish-latest",
        action="store_true",
        help="write run dir only; do not update results/backtester_runs/latest_result.json",
    )
    args = parser.parse_args()
    try:
        return run_backtest(
            args.config.resolve(),
            args.run_id,
            publish_latest=not args.no_publish_latest,
        )
    except ConfigValidationError as exc:
        print(json.dumps({"verdict": "BLOCKED", "code": exc.code, "message": exc.message}, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"verdict": "FAILED", "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
