# bots/shared/hedge_accounting.py
"""
📊 HEDGE ACCOUNTING / EXIT LOGIC

WICHTIG:
- Berücksichtigt REALISIERTE Profite
- DARF NICHT für Burn / TP / SL genutzt werden
"""

from .hedge_math import compute_hedge_be


def calculate_exit_be_price(
    long_avg: float,
    long_size: float,
    short_avg: float,
    short_size: float,
    total_burn_profits: float,
    be_target_profit: float,
) -> float:
    """
    Exit-BE für vollständigen Hedge-Exit (Accounting).
    """
    return compute_hedge_be(
        long_avg=long_avg,
        long_size=long_size,
        short_avg=short_avg,
        short_size=short_size,
        burned_usdt=total_burn_profits,
        be_target_profit=be_target_profit,
    )
