from window_v1 import iter_5m_closes_half_open


def test_24h_half_open_close_count():
    closes = iter_5m_closes_half_open("2026-09-11T12:00:00Z", "2026-09-12T12:00:00Z")
    assert len(closes) == 288
    assert closes[0] == "2026-09-11T12:00:00Z"
    assert closes[-1] == "2026-09-12T11:55:00Z"
