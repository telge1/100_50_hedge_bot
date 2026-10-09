"""Phase 11K — rolling trim + reference parity (ClickHouse integration where marked)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from indicators.live_micro_lld_v2.as_of import filter_candles_closed_at_or_before

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CanonicalCandleSession,
    candles_equal,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    assert_rolling_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    enumerate_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash
from research_charts.live_micro_lld_v2_ch_test.phase11k_buffer_policy import (
    max_1m_bars_sufficient,
    required_1m_unix_span,
)
from research_charts.live_micro_lld_v2_ch_test.serialize import pool_fingerprint

SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
START = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


def _active_pool_fps(result, dc: datetime) -> dict[str, tuple]:
    dc = dc.astimezone(timezone.utc)
    out = {}
    for p in result.pools:
        if not p.active or p.known_at.astimezone(timezone.utc) > dc:
            continue
        if p.break_at is not None:
            continue
        out[p.pool_id] = pool_fingerprint(p)
    return out


@pytest.mark.integration
def test_trim_under_small_max_matches_reference_chronological():
    """Forces _trim_left by keeping max_1m_bars small while walking forward in time."""
    closes = enumerate_decision_closes(START, 35, TF)
    session = CanonicalCandleSession(
        SYMBOL, TF, use_incremental_5m=True, max_1m_bars=9_000
    )
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        ref, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, PD)
        cand, _ = session.load_for_window(hf, dc, PD)
        assert candles_equal(ref, cand)
        assert all(int(c.unix_seconds) <= int(dc.timestamp()) for c in cand)
    assert len(session._1m_by_unix) > 9_000


@pytest.mark.integration
def test_incremental_5m_after_trim_matches_full_trp():
    session = CanonicalCandleSession(
        SYMBOL, TF, use_incremental_5m=True, max_1m_bars=8_000
    )
    closes = enumerate_decision_closes(START, 28, TF)
    for dc in closes:
        hf = assert_rolling_coverage(dc)
        session.load_for_window(hf, dc, PD, verify_incremental_5m=True)


@pytest.mark.integration
def test_small_max_exceeds_cap_but_retains_required_window():
    """Documented: _trim_left stops when oldest >= keep_from_unix even if len > max_1m_bars."""
    dc = enumerate_decision_closes(START, 1, TF)[0]
    hf = assert_rolling_coverage(dc)
    src_lo, to_u = required_1m_unix_span(hf, dc, PD, TF)
    assert not max_1m_bars_sufficient(500, src_lo, to_u)
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True, max_1m_bars=500)
    ref, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, PD)
    cand, _ = session.load_for_window(hf, dc, PD)
    assert candles_equal(ref, cand)
    assert len(session._1m_by_unix) > 500
    assert min(session._1m_by_unix) <= src_lo


@pytest.mark.integration
@pytest.mark.parametrize(
    "decision",
    [
        datetime(2026, 4, 30, 23, 55, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 23, 55, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 30, 21, 35, tzinfo=timezone.utc),
    ],
)
def test_month_boundary_decision_close_parity(decision):
    hf = assert_rolling_coverage(decision)
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    ref, _ = _load_candles_for_window(SYMBOL, TF, hf, decision, PD)
    cand, meta = session.load_for_window(hf, decision, PD)
    assert candles_equal(ref, cand)
    assert meta["candle_load_start"].astimezone(timezone.utc) <= hf


@pytest.mark.integration
def test_engine_and_strength_parity_with_trim_session():
    session = CanonicalCandleSession(
        SYMBOL, TF, use_incremental_5m=True, max_1m_bars=10_000
    )
    for dc in enumerate_decision_closes(START, 12, TF)[::4]:
        hf = assert_rolling_coverage(dc)
        ref_snap, _, ref_res = build_active_state_at_decision_close(
            SYMBOL, TF, hf, dc, pool_detail=PD, history_policy="ROLLING_4M"
        )
        cand_snap, _, cand_res = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=PD,
            history_policy="ROLLING_4M",
            candle_session=session,
        )
        assert ref_snap.engine_input_hash == cand_snap.engine_input_hash
        assert _active_pool_fps(ref_res, dc) == _active_pool_fps(cand_res, dc)


@pytest.mark.integration
def test_warmup_load_start_matches_reference():
    dc = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    hf = assert_rolling_coverage(dc)
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True, max_1m_bars=12_000)
    ref, ref_meta = _load_candles_for_window(SYMBOL, TF, hf, dc, PD)
    cand, cand_meta = session.load_for_window(hf, dc, PD)
    assert ref_meta["candle_load_start"] == cand_meta["candle_load_start"]
    assert len(cand) == len(ref)
    warmup = cand_meta["warmup_bars"]
    assert warmup > 0
    first = cand[0]
    assert int(first.unix_seconds) >= int(cand_meta["candle_load_start"].timestamp())
    assert filter_candles_closed_at_or_before(cand, dc, TF) == cand


@pytest.mark.integration
def test_phase10_published_sample_with_trim_session():
    dc = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    hf = assert_rolling_coverage(dc)
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True, max_1m_bars=11_000)
    snap, _, _ = build_active_state_at_decision_close(
        SYMBOL,
        TF,
        hf,
        dc,
        pool_detail=PD,
        history_policy="ROLLING_4M",
        candle_session=session,
    )
    line = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
    cfg_h = config_hash(build_live_micro_lld_v2_config(PD))
    pub = PublishedActiveReader().load_published(line, dc, config_hash=cfg_h)
    assert pub["active_state_fingerprint"] == snap.active_state_fingerprint
