"""MC-LOAD-P4A — cache hit must honor newest-first ``limit``."""

from research_charts import service as svc


def test_tail_limit_candles_unbounded_window():
    rows = list(range(100))
    out = svc._tail_limit_candles(rows, None, None, 12)
    assert len(out) == 12
    assert out[0] == 88


def test_tail_limit_candles_preserves_pinned_historical_range():
    rows = list(range(100))
    # ``end`` far in the past → historical pin, no tail trim.
    out = svc._tail_limit_candles(rows, 1, 1_600_000_000, 12)
    assert out is rows
    assert len(out) == 100


def test_tail_limit_candles_live_pinned_range_honors_limit(monkeypatch):
    rows = list(range(100))

    class _Time:
        @staticmethod
        def time():
            return 1_700_000_000

    monkeypatch.setattr(svc.time, "time", _Time.time)
    out = svc._tail_limit_candles(rows, 1, 1_699_999_500, 12)
    assert len(out) == 12
    assert out[0] == 88


def test_packed_from_cached_trims_oversized_blob():
    class C:
        def __init__(self, t):
            self.unix_seconds = t
            self.open = self.high = self.low = self.close = self.volume = 1.0

    oversized = [C(i) for i in range(32000)]
    packed = svc._packed_from_cached(
        oversized, "SOLUSDT", "5m", "test", None, None, 24192, 0
    )
    assert packed["limit"] == 24192
    assert len(packed["candles"]) == 24192
    assert packed["cache"] == "hit"