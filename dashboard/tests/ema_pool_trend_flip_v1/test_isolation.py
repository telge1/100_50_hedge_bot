from __future__ import annotations

from pathlib import Path


def test_baseline_modules_not_rewritten():
    """Isolation: Pool V1 strategy id and default dropdown stay in place."""
    html = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/templates/stoch_signale.html").read_text(
        encoding="utf-8"
    )
    assert 'value="wave_fade_no_be50_v1" selected' in html
    assert "POOL_ORDER_PLAN_V1" in html
    assert "EMA_POOL_TREND_FLIP_V1" in html
