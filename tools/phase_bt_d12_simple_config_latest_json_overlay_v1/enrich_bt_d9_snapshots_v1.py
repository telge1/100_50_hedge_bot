#!/usr/bin/env python3
"""Enrich BT-D9 integration signals with RECONSTRUCTED signal_snapshot (cache as-of only)."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
BT_D12 = Path(__file__).resolve().parent
for p in (str(BT_D6), str(BT_D7), str(BT_D8), str(BT_D12), str(ROOT / "tools/phase4d_pool_landscape_backtester_v2")):
    sys.path.insert(0, p)

from config_validate_v1 import load_and_validate_config  # noqa: E402
from result_publish_v1 import build_result_document, publish_result  # noqa: E402
from signal_snapshot_v1 import snapshot_from_legacy_signal  # noqa: E402
from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402

INTEGRATION = (
    ROOT
    / "results/phase_bt_d6_config_experiments_v1/bt_d9_24h_run_304d6a244138_20261025T105617Z/integration_result.json"
)
CFG = ROOT / "tools/phase_bt_d9_first_24h_short_pnl_v1/configs/bt_d9_24h_v1.json"
CACHE = ROOT / "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"
RUN_ID = "bt_d9_24h_import_v2_snapshots"


def main() -> int:
    cfg = load_and_validate_config(CFG)
    raw = json.loads(INTEGRATION.read_text(encoding="utf-8"))
    integration = deepcopy(raw)
    session = ValidatedDensityCacheSessionV1(CACHE, strategy_config=cfg)
    ch = str(integration.get("config_hash") or "")
    enriched = []
    for sig in integration.get("signals") or []:
        close = sig["decision_close_utc"]
        snap = session.collect_at_close(close)
        ss = snapshot_from_legacy_signal(cfg, sig, snap, config_hash=ch, provenance_tag="RECONSTRUCTED")
        row = dict(sig)
        row["signal_snapshot"] = ss
        enriched.append(row)
    integration["signals"] = enriched
    density_prov = json.loads(
        (ROOT / "results/phase_bt_d9_first_24h_short_pnl_v1/backtest_result.json").read_text(encoding="utf-8")
    ).get("density_provenance", {})
    doc = build_result_document(
        run_id=RUN_ID,
        config=cfg,
        integration=integration,
        density_provenance=density_prov,
        meta={"enrichment": "RECONSTRUCTED_signal_snapshot_from_bt_d9_cache"},
    )
    # Replace latest only (new run dir)
    from result_publish_v1 import RUNS_ROOT

    out = RUNS_ROOT / RUN_ID
    if out.exists():
        import shutil

        shutil.rmtree(out)
    publish_result(RUN_ID, doc)
    for i, s in enumerate(doc["signals"]):
        ss = s.get("signal_snapshot") or {}
        print(
            f"signal {i+1} entry={ss.get('entry_price_planned')} sl={ss.get('stop_loss_price_planned')} "
            f"tp={ss.get('take_profit_price_planned')} tag={ss.get('snapshot_provenance')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
