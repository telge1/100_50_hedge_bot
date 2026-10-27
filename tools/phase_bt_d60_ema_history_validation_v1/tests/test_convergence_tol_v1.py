from convergence_v1 import compare_warmups_at_probes


def test_warmup_convergence_synthetic() -> None:
    prices = {}
    t0 = __import__("datetime").datetime(2026, 9, 1, 0, 0, tzinfo=__import__("datetime").timezone.utc)
    for i in range(9000):
        ts = t0 + __import__("datetime").timedelta(minutes=5 * i)
        key = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        c = 0.1 + 0.00001 * i
        prices[key] = {"o": c, "h": c, "l": c, "c": c}
    bar_keys = sorted(prices.keys())
    probe = bar_keys[5000]
    res = compare_warmups_at_probes(
        bar_keys=bar_keys,
        prices=prices,
        probe_closes=[probe],
        warmup_hours=72,
    )
    assert res["rows"][0]["ok"] is True
