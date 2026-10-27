from ema_research_v1 import compute_ema_series, ema_map_for_bars


def test_ema_first_close_seed():
    s = compute_ema_series([1.0, 2.0, 3.0], 9)
    assert s[0] == 1.0
    assert s[1] is not None and s[1] > 1.0


def test_ema_map_keys():
    prices = {
        "2026-01-01T12:00:00Z": {"o": 1, "h": 1, "l": 1, "c": 1.0},
        "2026-01-01T12:05:00Z": {"o": 1, "h": 1, "l": 1, "c": 2.0},
    }
    emas = ema_map_for_bars(list(prices.keys()), prices)
    assert 9 in emas and "2026-01-01T12:05:00Z" in emas[9]
