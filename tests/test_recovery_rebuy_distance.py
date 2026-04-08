import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategy.config import StrategyConfig
from strategy.psrh_strategy import PSRHStrategy


@pytest.fixture
def strategy():
    config = StrategyConfig()
    strat = PSRHStrategy(config)
    yield strat
    strat.stop()


def test_select_rebuy_divider(strategy):
    assert strategy._select_rebuy_divider(0.02) == 3.0
    assert strategy._select_rebuy_divider(0.05) == 4.0
    assert strategy._select_rebuy_divider(0.08) == 5.0
    assert strategy._select_rebuy_divider(1.2) == 5.0
    assert strategy._select_rebuy_divider(0.03) == 4.0
    assert strategy._select_rebuy_divider(0.06) == 5.0


def test_calc_rebuy_distance_pct(strategy):
    def calc(spread):
        return strategy._calc_rebuy_distance_pct(spread)[0]

    assert math.isclose(calc(0.02), 0.02 / 3.0, rel_tol=1e-6)
    assert math.isclose(calc(0.04), 0.01, rel_tol=1e-6)
    assert math.isclose(calc(0.08), 0.016, rel_tol=1e-6)
    assert math.isclose(calc(0.03), 0.03 / 4.0, rel_tol=1e-6)
    assert math.isclose(calc(0.06), 0.06 / 5.0, rel_tol=1e-6)
    assert math.isclose(calc(0.0), strategy.config.step_size_pct, rel_tol=1e-6)
    assert math.isclose(calc(-0.01), strategy.config.step_size_pct, rel_tol=1e-6)
    assert math.isclose(
        calc(0.30),
        strategy.config.recovery_max_rebuy_distance_pct,
        rel_tol=1e-6,
    )


@pytest.mark.parametrize(
    "spread,expected",
    [
        (0.02, 0.10),
        (0.025, 0.125),
        (0.03, 0.15),
        (0.035, 0.175),
        (0.04, 0.20),
        (0.045, 0.225),
        (0.05, 0.25),
        (0.055, 0.275),
        (0.06, 0.30),
    ],
)
def test_calc_rebuy_size_multiplier(strategy, spread, expected):
    calc = strategy._calc_rebuy_size_multiplier
    strategy.config.user.rebuy_size_multiplier_base = 0.1
    strategy.config.user.rebuy_size_multiplier_increment = 0.025
    strategy.config.user.rebuy_size_multiplier_span = 0.005
    strategy.config.user.spread_threshold = 0.02
    assert math.isclose(calc(spread), expected, rel_tol=1e-6)
