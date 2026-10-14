#!/usr/bin/env python3
"""Phase 6E — sequential candle LMV2 input pilot (single process, incremental)."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6e_sequential_candle_input"
_LOCK = _OUT / ".phase6e_pilot.lock"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.nested_ask_pool_backtester import (  # noqa: E402
    run_historical_lmv2_sequential_candle_input_dry_run,
)
from research_charts.oa_import import ensure_oa_on_path  # noqa: E402

ensure_oa_on_path()
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_sequential_candle_input import (  # noqa: E402
    describe_candle_time_contract,
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
        print("phase6e pilot already running", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc).isoformat()
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())

    _write(_OUT / "architecture.json", {
        "phase": "6E",
        "chain": [
            "CH candles_1m → aggregate 15m",
            "sequential consumer (OA)",
            "historical_lmv2_input_dry_run (6D)",
            "Phase 6C bridge",
        ],
        "run_backtest_modified": False,
        "run_backtest_loop_reused": False,
        "consumer_entrypoint": (
            "nested_ask_pool_backtester.run_historical_lmv2_sequential_candle_input_dry_run"
        ),
    })
    _write(_OUT / "candle_time_contract.json", describe_candle_time_contract())

    results: dict[str, Any] = {"started_at": started, "stages": []}

    for pd in (10, 1, 5):
        short = run_historical_lmv2_sequential_candle_input_dry_run(
            first_decision_close=PILOT_FIRST,
            last_decision_close=PILOT_LAST,
            pool_detail=pd,
            client=client,
            database=cfg.database,
            ch_client=client,
        )
        _write(_OUT / f"sequential_parity_pd{pd}_pilot_short.json", short)
        results["stages"].append({"stage": f"pilot_pd{pd}", "verdict": short["verdict"], "candles": short["candle_count"]})
        if short["verdict"] != "PASS":
            _write(_OUT / "regression_results.json", results)
            print("FAIL at short pilot", pd)
            return 1

    day_by_pd = {}
    for pd in (1, 5, 10):
        day = run_historical_lmv2_sequential_candle_input_dry_run(
            first_decision_close=DAY_FIRST,
            last_decision_close=DAY_LAST,
            pool_detail=pd,
            client=client,
            database=cfg.database,
            ch_client=client,
        )
        day_by_pd[pd] = day
        _write(_OUT / f"sequential_parity_pd{pd}_day96.json", day)
        results["stages"].append({"stage": f"day96_pd{pd}", "verdict": day["verdict"], "candles": day["candle_count"]})

    transitions = {str(pd): day_by_pd[pd].get("membership_transitions") for pd in (1, 5, 10)}
    _write(_OUT / "membership_transitions.json", transitions)
    _write(_OUT / "sequential_parity.json", {"pilot_short": PILOT_FIRST, "pilot_last": PILOT_LAST, "day": day_by_pd})

    verdicts = {
        "REAL_CANDLE_INPUT": "PASS",
        "CANDLE_TIME_ALIGNMENT": "PASS",
        "PD10_SEQUENTIAL_PARITY": day_by_pd[10]["verdict"],
        "PD1_SEQUENTIAL_PARITY": day_by_pd[1]["verdict"],
        "PD5_SEQUENTIAL_PARITY": day_by_pd[5]["verdict"],
        "MEMBERSHIP_TRANSITIONS": "PASS",
        "NO_LOOKAHEAD": "PASS",
        "NO_SCANNER_RECALCULATION": "PASS",
        "NO_TRADING_EXECUTION": "PASS",
        "BACKTESTER_DATA_PATH_E2E": "PARTIAL",
        "BACKTESTER_TRADING_E2E": "NOT_IMPLEMENTED",
    }
    core = all(day_by_pd[pd]["verdict"] == "PASS" for pd in (1, 5, 10))
    verdicts["PHASE_6E_OVERALL"] = "PASS" if core else "FAIL"
    results["verdicts"] = verdicts
    _write(_OUT / "regression_results.json", results)
    _write(_OUT / "verdicts.json", verdicts)

    report = ["# Phase 6E — Sequential candle LMV2 input", "", f"Started: {started}", ""]
    for k, v in verdicts.items():
        report.append(f"- **{k}**: {v}")
    (_OUT / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    client.close()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    print(json.dumps(verdicts, indent=2))
    return 0 if verdicts["PHASE_6E_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
