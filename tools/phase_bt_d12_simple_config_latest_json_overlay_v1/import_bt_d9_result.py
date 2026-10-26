#!/usr/bin/env python3
"""Import completed BT-D9 integration_result into BT-D12 result.json (no recompute)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(BT_D6))

from config_validate_v1 import load_and_validate_config  # noqa: E402
from result_publish_v1 import build_result_document, publish_result  # noqa: E402

INTEGRATION = (
    ROOT
    / "results/phase_bt_d6_config_experiments_v1/bt_d9_24h_run_304d6a244138_20261025T105617Z/integration_result.json"
)
CFG = ROOT / "tools/phase_bt_d9_first_24h_short_pnl_v1/configs/bt_d9_24h_v1.json"
RUN_ID = "bt_d9_24h_import_v1"


def main() -> int:
    cfg = load_and_validate_config(CFG)
    if "symbol" not in cfg["backtest"]:
        cfg["backtest"]["symbol"] = cfg["backtest"]["symbols"][0]
    integration = json.loads(INTEGRATION.read_text(encoding="utf-8"))
    density_prov = json.loads(
        (ROOT / "results/phase_bt_d9_first_24h_short_pnl_v1/backtest_result.json").read_text(encoding="utf-8")
    ).get("density_provenance", {})
    doc = build_result_document(
        run_id=RUN_ID,
        config=cfg,
        integration=integration,
        density_provenance=density_prov,
        meta={"import_source": str(INTEGRATION), "note": "BT-D9 historical import — no recompute"},
    )
    assert len(doc["signals"]) == 3
    assert len(doc["orders"]) == 5
    assert doc["pnl"]["total_net_pnl_usdt"] == 0.0
    publish_result(RUN_ID, doc)
    print(json.dumps({"ok": True, "run_id": RUN_ID, "signals": 3, "trades": 0}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
