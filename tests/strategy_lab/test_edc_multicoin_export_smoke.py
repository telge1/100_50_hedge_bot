"""Gated local 3-coin export smoke (P2D3).

Requires ClickHouse. Enable with STRATEGY_LAB_EDC_MULTICOIN_EXPORT_SMOKE=1.
"""

from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.edc_multicoin_export_v2 import (
    run_and_export_edc_m0_multicoin_v2,
)

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
EDC_YAML = REPO / "strategies" / "strategy_lab" / "edc_m0_strict_sync_v2.yaml"
UNIVERSE = REPO / "config" / "universe_tradeable_51.json"
HASH_EDC = "4aced6b481d19eadd5505afc535e6fb4976f231fd2894b11f7d79acebc53598f"
WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
SMOKE_SYMBOLS = ("XRPUSDT", "LITUSDT", "NEARUSDT")
EXPECTED_ORDER = ("XRPUSDT", "NEARUSDT", "LITUSDT")


def _require_smoke_env() -> None:
    if os.environ.get("STRATEGY_LAB_EDC_MULTICOIN_EXPORT_SMOKE") != "1":
        pytest.skip(
            "local EDC multicoin export smoke disabled; export "
            "STRATEGY_LAB_EDC_MULTICOIN_EXPORT_SMOKE=1 to run (ClickHouse). "
            "Not a silent pass."
        )


class _CountingClient:
    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.query_count = 0

    def query(
        self,
        sql: str,
        parameters: object = None,
        settings: object = None,
    ) -> object:
        self.query_count += 1
        return self._inner.query(sql, parameters=parameters, settings=settings)


def test_three_coin_export_smoke_and_resume(tmp_path: Path) -> None:
    _require_smoke_env()
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

    client = _CountingClient(get_clickhouse_client())
    out = tmp_path / "export"

    t0 = time.perf_counter()
    first = run_and_export_edc_m0_multicoin_v2(
        strategy_path=EDC_YAML,
        universe_path=UNIVERSE,
        start=WINDOW_START,
        end=WINDOW_END,
        output_dir=out,
        client=client,
        symbols=SMOKE_SYMBOLS,
        resume=False,
    )
    first_elapsed = time.perf_counter() - t0
    first_queries = client.query_count

    assert first.requested_symbols == EXPECTED_ORDER
    assert first.completed_symbols == EXPECTED_ORDER
    assert first.failed_symbols == ()
    assert first_queries == 15
    assert first.trade_count == 32
    assert first.gross_pnl_usdt == Decimal("117.04916")
    assert first.costs_usdt == Decimal("35.2")
    assert first.net_pnl_usdt == Decimal("81.84916")
    assert first.strategy_hash == HASH_EDC

    for name in (
        "run_manifest.json",
        "coin_summary.csv",
        "trades.csv",
        "failures.json",
    ):
        assert (out / name).is_file()

    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["strategy_hash"] == HASH_EDC
    assert manifest["execution_order"] == list(EXPECTED_ORDER)
    assert manifest["trade_count"] == 32
    assert manifest["gross_pnl_usdt"] == "117.04916"
    assert manifest["costs_usdt"] == "35.2"
    assert manifest["net_pnl_usdt"] == "81.84916"
    assert manifest["roundtrip_cost_value"] == "0.11"

    coins = list(csv.DictReader((out / "coin_summary.csv").open(encoding="utf-8")))
    assert [c["symbol"] for c in coins] == list(EXPECTED_ORDER)
    trades = list(csv.DictReader((out / "trades.csv").open(encoding="utf-8")))
    assert len(trades) == 32
    assert json.loads((out / "failures.json").read_text(encoding="utf-8")) == []

    sizes = {name: (out / name).stat().st_size for name in (
        "run_manifest.json",
        "coin_summary.csv",
        "trades.csv",
        "failures.json",
    )}
    before = {name: (out / name).read_bytes() for name in sizes}
    ck_dir = out / "checkpoints" / "symbols"
    before_ck = {
        p.name: p.read_bytes() for p in sorted(ck_dir.glob("*.json"))
    }
    assert set(before_ck) == {"XRPUSDT.json", "NEARUSDT.json", "LITUSDT.json"}

    print("first_elapsed_sec", round(first_elapsed, 3))
    print("first_query_count", first_queries)
    print("artifact_bytes", sizes)

    client.query_count = 0
    t1 = time.perf_counter()
    second = run_and_export_edc_m0_multicoin_v2(
        strategy_path=EDC_YAML,
        universe_path=UNIVERSE,
        start=WINDOW_START,
        end=WINDOW_END,
        output_dir=out,
        client=client,
        symbols=SMOKE_SYMBOLS,
        resume=True,
    )
    resume_elapsed = time.perf_counter() - t1
    assert client.query_count == 0
    assert second == first
    after = {name: (out / name).read_bytes() for name in before}
    assert after == before
    after_ck = {p.name: p.read_bytes() for p in sorted(ck_dir.glob("*.json"))}
    assert after_ck == before_ck
    print("resume_elapsed_sec", round(resume_elapsed, 3))
    print("resume_query_count", client.query_count)
