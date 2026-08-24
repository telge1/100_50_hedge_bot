"""Gated local 3-coin smoke for EDC multicoin runner (P2D2).

Requires ClickHouse. Enable with STRATEGY_LAB_EDC_MULTICOIN_SMOKE=1.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.edc_multicoin_v2 import run_edc_m0_multicoin_v2
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2
from orderbook_analyse.strategy_lab.validation.p4c import require_valid_strategy_v2_p4c

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[2]
EDC_YAML = REPO / "strategies" / "strategy_lab" / "edc_m0_strict_sync_v2.yaml"
UNIVERSE = REPO / "config" / "universe_tradeable_51.json"
HASH_EDC = "fb0ebc45827c68ab60d3a920c2d5d68651857080cf950fc020994044935f81ea"
WINDOW_START = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)

# Request order; runner executes in full-universe order: XRP → NEAR → LIT.
SMOKE_SYMBOLS = ("XRPUSDT", "LITUSDT", "NEARUSDT")
EXPECTED_ORDER = ("XRPUSDT", "NEARUSDT", "LITUSDT")
EXPECTED_TRADES = {
    "XRPUSDT": 15,
    "NEARUSDT": 12,
    "LITUSDT": 5,
}


def _require_smoke_env() -> None:
    if os.environ.get("STRATEGY_LAB_EDC_MULTICOIN_SMOKE") != "1":
        pytest.skip(
            "local EDC multicoin smoke disabled; export "
            "STRATEGY_LAB_EDC_MULTICOIN_SMOKE=1 to run (ClickHouse). "
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


def test_three_coin_smoke_and_resume(tmp_path: Path) -> None:
    _require_smoke_env()

    catalogs = production_catalog_bundle_v2()
    spec = load_strategy_v2_yaml_file(EDC_YAML)
    require_valid_strategy_v2_p4c(spec, catalogs)
    compiled = compile_strategy_v2(spec, catalogs)
    assert compiled.strategy_hash == HASH_EDC
    assert UNIVERSE.is_file()

    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

    raw = get_clickhouse_client()
    client = _CountingClient(raw)
    ck = tmp_path / "ck"

    t0 = time.perf_counter()
    first = run_edc_m0_multicoin_v2(
        spec,
        compiled,
        catalogs,
        client=client,
        universe_path=UNIVERSE,
        start=WINDOW_START,
        end=WINDOW_END,
        checkpoint_dir=ck,
        symbols=SMOKE_SYMBOLS,
        resume=False,
    )
    first_elapsed = time.perf_counter() - t0
    first_queries = client.query_count

    assert first.requested_symbols == EXPECTED_ORDER
    assert first.completed_symbols == EXPECTED_ORDER
    assert first.failed_symbols == ()
    assert first.trade_count == 32
    assert first.candidate_count == 32
    assert first_queries == 15  # 3 symbols × 5 loader queries

    by_sym = {run.symbols[0]: run for run in first.completed_runs}
    for sym, n in EXPECTED_TRADES.items():
        assert by_sym[sym].trade_count == n
        assert by_sym[sym].candidate_count == n

    # Control values @ Spec 0.11% — confirmed from live smoke (NEAR is not 16.35).
    assert by_sym["XRPUSDT"].gross_pnl_usdt == Decimal("50")
    assert by_sym["XRPUSDT"].costs_usdt == Decimal("16.5")
    assert by_sym["XRPUSDT"].net_pnl_usdt == Decimal("33.5")
    assert by_sym["LITUSDT"].gross_pnl_usdt == Decimal("37.5")
    assert by_sym["LITUSDT"].costs_usdt == Decimal("5.5")
    assert by_sym["LITUSDT"].net_pnl_usdt == Decimal("32")
    assert by_sym["NEARUSDT"].gross_pnl_usdt == Decimal("29.54916")
    assert by_sym["NEARUSDT"].costs_usdt == Decimal("13.2")
    assert by_sym["NEARUSDT"].net_pnl_usdt == Decimal("16.34916")
    assert first.costs_usdt == Decimal("35.2")  # 32 × 1.10
    assert first.gross_pnl_usdt == Decimal("117.04916")
    assert first.net_pnl_usdt == Decimal("81.84916")
    assert first.strategy_hash == HASH_EDC
    assert all(
        t.roundtrip_cost_pct == Decimal("0.11")
        for run in first.completed_runs
        for t in run.trades
    )
    assert first.completed_runs[0].roundtrip_cost.value == Decimal("0.11")

    sizes = {
        sym: (ck / "symbols" / f"{sym}.json").stat().st_size for sym in EXPECTED_ORDER
    }
    assert all(sz > 0 for sz in sizes.values())
    before_bytes = {
        sym: (ck / "symbols" / f"{sym}.json").read_bytes() for sym in EXPECTED_ORDER
    }

    print("first_elapsed_sec", round(first_elapsed, 3))
    print("first_query_count", first_queries)
    for sym in EXPECTED_ORDER:
        run = by_sym[sym]
        print(
            sym,
            "trades",
            run.trade_count,
            "gross",
            run.gross_pnl_usdt,
            "costs",
            run.costs_usdt,
            "net",
            run.net_pnl_usdt,
            "ck_bytes",
            sizes[sym],
        )
    print(
        "pooled",
        "gross",
        first.gross_pnl_usdt,
        "costs",
        first.costs_usdt,
        "net",
        first.net_pnl_usdt,
    )

    client.query_count = 0
    t1 = time.perf_counter()
    second = run_edc_m0_multicoin_v2(
        spec,
        compiled,
        catalogs,
        client=client,
        universe_path=UNIVERSE,
        start=WINDOW_START,
        end=WINDOW_END,
        checkpoint_dir=ck,
        symbols=SMOKE_SYMBOLS,
        resume=True,
    )
    resume_elapsed = time.perf_counter() - t1

    assert client.query_count == 0
    assert second == first
    after_bytes = {
        sym: (ck / "symbols" / f"{sym}.json").read_bytes() for sym in EXPECTED_ORDER
    }
    assert after_bytes == before_bytes
    print("resume_elapsed_sec", round(resume_elapsed, 3))
    print("resume_query_count", client.query_count)
