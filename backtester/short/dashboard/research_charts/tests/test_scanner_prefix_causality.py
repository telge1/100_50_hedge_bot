"""Scanner LLD prefix causality: full pane index i must match truncated pane at T."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[5]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_repo / "backtester" / "short") not in sys.path:
    sys.path.insert(0, str(_repo / "backtester" / "short"))
_ctx = _repo / "results/pool_scan/context_study_v1"
if str(_ctx) not in sys.path:
    sys.path.insert(0, str(_ctx))

from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    parity_diff_pool_rows,
    scanner_pools_for_index,
    ui_lld_config,
)
from pool_scan.clock import bar_close
from scripts.audit_backtest_signal_causality import (
    collect_short_causal_at,
    collect_short_full,
    _parse_dt,
)

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
NOW_UNIX = int(datetime.now(timezone.utc).timestamp())
PANE_FROM_UNIX = int(PANE_FROM.timestamp())

XRP_20260618_SIGNAL_DECISION = "2026-06-18T00:30:00+00:00"
XRP_20260618_POOL_ID = "lld:XRPUSDT:15m:upper:1781738100"
XRP_20260618_POOL_BAR_CLOSE = "2026-06-18T00:45:00+00:00"


def _require_full_product(
    full_map: dict,
    *,
    symbol: str,
    decision_iso: str,
    pool_id: str,
    window_day_prefix: str | None = None,
) -> dict:
    decision_key = _parse_dt(decision_iso).isoformat()
    matches = [
        v
        for v in full_map.values()
        if v["symbol"] == symbol
        and v["decision_time"] == decision_key
        and v.get("pool_id") == pool_id
    ]
    if len(matches) == 1:
        return matches[0]
    if window_day_prefix:
        in_window = [
            v
            for v in full_map.values()
            if v["symbol"] == symbol and window_day_prefix in v["decision_time"]
        ]
        window_times = sorted({v["decision_time"] for v in in_window})
        window_pools = sorted({v.get("pool_id") or "" for v in in_window})
    else:
        window_times = sorted({v["decision_time"] for v in full_map.values() if v["symbol"] == symbol})
        window_pools = sorted({v.get("pool_id") or "" for v in full_map.values() if v["symbol"] == symbol})
    raise AssertionError(
        f"expected full-replay product symbol={symbol} decision_time={decision_key} pool_id={pool_id}; "
        f"matches={len(matches)}; window decision_times={window_times}; window pool_ids={window_pools}"
    )


def _scanner_snapshot(candles, idx, cfg):
    pools, _ui, _proof = scanner_pools_for_index(candles, "15m", idx, cfg, cache={})
    return pools


@pytest.mark.slow
@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_xrp_20260619_full_replay_matches_causal_tp():
    """Regression: TP 1.1322 vs 1.1372 before fix; must match after."""
    T = _parse_dt("2026-06-19T02:00:00+00:00")
    full_map = collect_short_full("XRPUSDT")
    causal = collect_short_causal_at("XRPUSDT", T)
    full_row = next(v for v in full_map.values() if v["decision_time"].startswith("2026-06-19T02:00"))
    causal_row = next(c for c in causal if c["pool_id"] == full_row["pool_id"])
    assert full_row["tp"] == causal_row["tp"]
    assert full_row["tp"] == pytest.approx(1.1372, abs=1e-4)
    assert full_row["entry"] == causal_row["entry"]
    assert full_row["sl"] == causal_row["sl"]
    assert full_row["status"] == causal_row["status"]


@pytest.mark.slow
@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_xrp_20260618_full_and_causal_same_signal():
    # E1R short products use entry-bar close as decision_time (00:30), not the later
    # pool-bar close at 00:45 used in scanner_pools_prefix_invariant LLD fixtures.
    signal_decision = _parse_dt(XRP_20260618_SIGNAL_DECISION)
    causal = collect_short_causal_at("XRPUSDT", signal_decision)
    full_map = collect_short_full("XRPUSDT")
    full_row = _require_full_product(
        full_map,
        symbol="XRPUSDT",
        decision_iso=XRP_20260618_SIGNAL_DECISION,
        pool_id=XRP_20260618_POOL_ID,
        window_day_prefix="2026-06-18",
    )
    assert len(causal) == 1, (
        f"expected one causal product at {XRP_20260618_SIGNAL_DECISION}; got {causal}"
    )
    causal_row = causal[0]
    assert causal_row["decision_time"] == full_row["decision_time"]
    assert causal_row["pool_id"] == full_row["pool_id"] == XRP_20260618_POOL_ID
    assert causal_row["status"] == full_row["status"] == "ALLOWED"
    assert causal_row["entry"] == full_row["entry"]
    assert causal_row["tp"] == full_row["tp"]
    assert causal_row["sl"] == full_row["sl"]


def test_xrp_20260618_pool_bar_close_differs_from_signal_decision_time():
    """Pool oracle at 00:45 bar close is not the same axis as E1R signal decision_time."""
    pool_close = _parse_dt(XRP_20260618_POOL_BAR_CLOSE)
    signal_decision = _parse_dt(XRP_20260618_SIGNAL_DECISION)
    assert pool_close != signal_decision
    # test_scanner_pools_prefix_invariant parametrizes LLD at 00:45; E1R uses entry close 00:30.


@pytest.mark.parametrize(
    "symbol,decision_iso",
    [
        ("XRPUSDT", "2026-06-19T02:00:00+00:00"),
        ("XRPUSDT", "2026-06-18T00:45:00+00:00"),
        ("XRPUSDT", "2026-06-07T05:15:00+00:00"),
        ("ADAUSDT", "2026-06-24T01:45:00+00:00"),
        ("ADAUSDT", "2026-06-07T05:00:00+00:00"),
        ("DOGEUSDT", "2026-06-11T12:00:00+00:00"),
        ("DOGEUSDT", "2026-06-25T17:30:00+00:00"),
    ],
)
def test_scanner_pools_prefix_invariant(symbol: str, decision_iso: str):
    T = _parse_dt(decision_iso)
    cfg = ui_lld_config("15m")
    _, trunc = load_pane_candles(
        symbol, "15m", from_unix=PANE_FROM_UNIX, to_unix=int(T.timestamp()), history_weeks=12
    )
    _, full = load_pane_candles(
        symbol, "15m", from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=12
    )
    stamps_t = candle_stamps(trunc)
    idx_t = next(i for i, s in enumerate(stamps_t) if bar_close(s, "15m") == T)
    idx_f = next(i for i, s in enumerate(candle_stamps(full)) if s == stamps_t[idx_t])
    pools_full = _scanner_snapshot(full, idx_f, cfg)
    pools_trunc = _scanner_snapshot(trunc, idx_t, cfg)
    diff = parity_diff_pool_rows(pools_trunc, pools_full)
    assert diff["ids_match"], diff
