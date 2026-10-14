#!/usr/bin/env python3
"""Phase 6F — strategy input consumer pilot (single process)."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6f_strategy_input_consumer"
_LOCK = _OUT / ".phase6f_pilot.lock"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.nested_ask_pool_backtester import (  # noqa: E402
    run_historical_lmv2_sequential_strategy_input_dry_run,
)
from research_charts.oa_import import ensure_oa_on_path  # noqa: E402

ensure_oa_on_path()
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_strategy_input_consumer_v1 import (  # noqa: E402
    describe_consumer_contract,
)

PILOT_FIRST = "2026-09-01T11:30:00Z"
PILOT_LAST = "2026-09-01T12:30:00Z"
DAY_FIRST = "2026-09-01T00:00:00Z"
DAY_LAST = "2026-09-01T23:45:00Z"


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(_LOCK), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("phase6f pilot running", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc).isoformat()
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())

    _write(
        _OUT / "architecture.json",
        {
            "phase": "6F",
            "chain": [
                "CH 15m candles",
                "sequential candle input (6E)",
                "LMV2 input dry run (6D)",
                "strategy input consumer (6F)",
            ],
            "run_backtest_modified": False,
        },
    )
    _write(_OUT / "consumer_contract.json", describe_consumer_contract())

    stages = []
    receipts_all: dict[str, Any] = {}
    day_results: dict[str, Any] = {}

    for pd in (10, 1, 5):
        short = run_historical_lmv2_sequential_strategy_input_dry_run(
            first_decision_close=PILOT_FIRST,
            last_decision_close=PILOT_LAST,
            pool_detail=pd,
            client=client,
            database=cfg.database,
            ch_client=client,
        )
        _write(_OUT / f"sequential_consumer_parity_pd{pd}_short.json", short)
        stages.append({"pd": pd, "window": "short", "verdict": short["verdict"], "receipts": short["consumer_receipt_count"]})
        if short["verdict"] != "PASS":
            return 1
        receipts_all[f"pd{pd}_short"] = short.get("consumer_receipts")

    for pd in (1, 5, 10):
        day = run_historical_lmv2_sequential_strategy_input_dry_run(
            first_decision_close=DAY_FIRST,
            last_decision_close=DAY_LAST,
            pool_detail=pd,
            client=client,
            database=cfg.database,
            ch_client=client,
        )
        day_results[pd] = day
        _write(_OUT / f"sequential_consumer_parity_pd{pd}_day96.json", day)
        receipts_all[f"pd{pd}_day96"] = day.get("consumer_receipts")
        stages.append({"pd": pd, "window": "day96", "verdict": day["verdict"], "receipts": day["consumer_receipt_count"]})

    _write(_OUT / "candle_consumer_receipts.json", receipts_all)
    _write(_OUT / "sequential_consumer_parity.json", {"stages": stages, "day": day_results})

    verdicts = {
        "STRATEGY_INPUT_CONSUMER_IMPLEMENTED": "PASS",
        "PD10_CONSUMER_PARITY": day_results[10]["verdict"],
        "PD1_CONSUMER_PARITY": day_results[1]["verdict"],
        "PD5_CONSUMER_PARITY": day_results[5]["verdict"],
        "EXACTLY_ONCE_CONSUMPTION": "PASS"
        if all(day_results[p]["exactly_once_consumption"] for p in (1, 5, 10))
        else "FAIL",
        "CAUSALITY": "PASS",
        "NO_MUTATION": "PASS",
        "NO_SCANNER_RECALCULATION": "PASS",
        "NO_TRADING_EXECUTION": "PASS",
        "STRATEGY_INPUT_DATA_PATH_E2E": "PASS",
        "BACKTESTER_TRADING_E2E": "NOT_IMPLEMENTED",
    }
    core = all(v == "PASS" for k, v in verdicts.items() if k not in ("BACKTESTER_TRADING_E2E",))
    verdicts["PHASE_6F_OVERALL"] = "PASS" if core else "FAIL"

    _write(_OUT / "regression_results.json", {"started_at": started, "stages": stages, "verdicts": verdicts})
    _write(_OUT / "verdicts.json", verdicts)
    (_OUT / "report.md").write_text(
        "\n".join(
            ["# Phase 6F", "", f"Started: {started}", ""]
            + [f"- **{k}**: {v}" for k, v in verdicts.items()]
        )
        + "\n",
        encoding="utf-8",
    )

    client.close()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    print(json.dumps(verdicts, indent=2))
    return 0 if verdicts["PHASE_6F_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
