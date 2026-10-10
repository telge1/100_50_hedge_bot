"""Causal and isolation checks for Phase 11M. No full historical engine run."""

from datetime import datetime, timedelta, timezone

import pytest

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CanonicalCandleSession,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import extract_active_from_result
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import active_pools_record
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config
from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import FrozenInputError, verify_manifest
from research_charts.live_micro_lld_v2_ch_test.phase11l.publish import disjoint_blocks, merge_worker_payloads
from research_charts.live_micro_lld_v2_ch_test.phase11m.coverage import warmup_covers
from research_charts.trp_import import load_trp

NOON = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc)


def _pool(known_at):
    from indicators.live_micro_lld_v2.models import LiquidityPoolV2

    return LiquidityPoolV2(
        pool_id="lldv2:DOGEUSDT:5m:normal:upper:1",
        pool_type="normal",
        symbol="DOGEUSDT",
        timeframe="5m",
        side="upper",
        source_timestamp=known_at,
        created_timestamp=known_at,
        known_at=known_at,
        top_price=0.2,
        bottom_price=0.1,
        strength=1.0,
        strength_class="Weak",
        active=True,
        created_index=1,
    )


class _Result:
    def __init__(self, pools):
        self.pools = pools


def test_pool_known_at_1205_is_not_visible_at_1200():
    pool = _pool(LATER)
    assert active_pools_record(_Result([pool]), NOON) == {}
    with pytest.raises(ValueError, match="future-known"):
        extract_active_from_result(_Result([pool]), decision_close=NOON)


def test_incomplete_15m_and_4h_buckets_are_rejected():
    trp = load_trp()
    from data.models import Candle

    def minutes(n, start):
        out = []
        for i in range(n):
            ts = start + timedelta(minutes=i)
            out.append(Candle(ts, 1, 1, 1, 1, 1, symbol="DOGEUSDT", timeframe="1m"))
        return out

    start = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert trp["aggregate"](minutes(14, start), "15m", strict_complete_buckets=True) == []
    assert trp["aggregate"](minutes(239, start), "4h", strict_complete_buckets=True) == []
    assert len(trp["aggregate"](minutes(15, start), "15m", strict_complete_buckets=True)) == 1


def test_sessions_and_presets_do_not_share_state():
    five = CanonicalCandleSession("DOGEUSDT", "5m", use_incremental_5m=True)
    fifteen = CanonicalCandleSession("DOGEUSDT", "15m", use_incremental_5m=True)
    five._5m_by_open_unix[1] = object()
    assert 1 not in fifteen._5m_by_open_unix
    pd1 = build_live_micro_lld_v2_config(1)
    pd10 = build_live_micro_lld_v2_config(10)
    assert pd1.volume_lookback != pd10.volume_lookback
    assert pd1 is not pd10


def test_worker_blocks_stay_disjoint_and_fail_closed_on_a_foreign_revision():
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import enumerate_decision_closes
    from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import PRICE_HASH, REVISION_HASH, SNAPSHOT_ID

    closes = enumerate_decision_closes(datetime(2026, 9, 30, 7, 5, tzinfo=timezone.utc), 60, "5m")
    blocks = disjoint_blocks(closes, 3)
    assert [len(b) for b in blocks] == [20, 20, 20]
    assert blocks[0][-1] < blocks[1][0] < blocks[2][0]

    def payload(worker_id, block, revision=REVISION_HASH):
        return {
            "status": "PASS",
            "snapshot_id": SNAPSHOT_ID,
            "price_hash": PRICE_HASH,
            "revision_hash": revision,
            "clickhouse_candle_reads": 0,
            "records": [{"decision_close": c.isoformat(), "n": worker_id} for c in block],
        }

    good = [payload(i, block) for i, block in enumerate(blocks)]
    merged = merge_worker_payloads(good, closes)
    assert len(merged["records"]) == 60
    with pytest.raises(Exception, match="frozen input"):
        merge_worker_payloads([payload(0, blocks[0], revision="ab" * 32), good[1], good[2]], closes)


def test_changed_manifest_hash_fails_closed(tmp_path):
    with pytest.raises(FrozenInputError):
        verify_manifest(tmp_path, need_from=NOON, need_to=NOON)


def test_frozen_snapshot_does_not_cover_pd1_or_4h_warmup():
    assert warmup_covers("5m", 10)["fits_frozen_snapshot"] is True
    assert warmup_covers("4h", 10)["fits_frozen_snapshot"] is False
    assert warmup_covers("5m", 1)["fits_frozen_snapshot"] is False
