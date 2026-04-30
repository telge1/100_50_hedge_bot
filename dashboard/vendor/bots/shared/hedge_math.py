EPS = 1e-8


def compute_hedge_be(
    long_avg: float,
    long_size: float,
    short_avg: float,
    short_size: float,
    burned_usdt: float = 0.0,
    be_target_profit: float = 0.0,
) -> float:
    """
    Computes the hedge break-even price (pure math).

    Definition:
      The BE price is the price at which the portfolio PnL from closing BOTH
      positions equals an *effective* USDT target profit.

      effective_target = max(0, be_target_profit - burned_usdt)

    Notes:
      - Works for both dominance cases (long_size > short_size OR short_size > long_size).
      - burned_usdt is treated as already realized profits that reduce the remaining
        target profit requirement (NOT as a cost-basis adjustment).

    Args:
        long_avg: Average entry price of long position
        long_size: Size of long position
        short_avg: Average entry price of short position
        short_size: Size of short position
        burned_usdt: Total realized burn profits in USDT
        be_target_profit: Target profit in USDT at break-even
        
    Returns:
        Break-even price (float)
        
    Raises:
        ValueError: If hedge is delta-neutral (net_size ≈ 0)
    """
    net_exposure = long_size - short_size
    if abs(net_exposure) < EPS:
        raise ValueError("Delta-neutral hedge: BE undefined")

    effective_target = max(0.0, float(be_target_profit) - float(burned_usdt))

    long_cost = float(long_avg) * float(long_size)
    short_cost = float(short_avg) * float(short_size)

    # Long dominates (net_exposure > 0): price needs to move up to realize target profit.
    if net_exposure > 0:
        return (long_cost - short_cost + effective_target) / net_exposure

    # Short dominates (net_exposure < 0): re-express with positive denominator.
    return (short_cost - long_cost + effective_target) / (-net_exposure)


def compute_tp_from_be(be_price: float, profit_percent: float, side: str) -> float:
    """
    Computes TP price from a BE price (pure math, no rounding).

    - side="Buy"  => TP above BE: BE * (1 + pct/100)
    - side="Sell" => TP below BE: BE * (1 - pct/100)
    """
    be_price = float(be_price)
    profit_percent = float(profit_percent)

    if be_price <= 0:
        raise ValueError("Invalid BE price (<= 0)")
    if profit_percent < 0:
        raise ValueError("Invalid profit_percent (< 0)")

    if side == "Buy":
        return be_price * (1 + profit_percent / 100.0)
    if side == "Sell":
        return be_price * (1 - profit_percent / 100.0)

    raise ValueError(f"Invalid side: {side!r}")
