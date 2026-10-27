from ema_research_v1 import ema_map_for_bars, slope_pct
from ema59_ema200_slope_research_v1 import ema_slopes_at_close, research_slope_composites


def test_slope_horizons_causal():
    bars = [f"2026-01-01T12:{i:02d}:00Z" for i in range(40)]
    prices = {b: {"o": 1, "h": 1, "l": 1, "c": 1.0 - i * 0.001} for i, b in enumerate(bars)}
    emas = ema_map_for_bars(bars, prices)
    bar = bars[30]
    s = ema_slopes_at_close(bar=bar, bars=bars, ema59=emas[59], ema200=emas[200])
    assert s["ema59_slope_6b_pct"] is not None
    assert s["ema200_slope_24b_pct"] is not None
    comp = research_slope_composites(
        s,
        ema9_20_crosses_past=0,
        net_move_24b_pct=-1.0,
        chop_ratio_24b=3.0,
        stack_growth_warning=False,
        local_stack_density=1,
    )
    assert "combo_flat59_fall200_12" in comp
