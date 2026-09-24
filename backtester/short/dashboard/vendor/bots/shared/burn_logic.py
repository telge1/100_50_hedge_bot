"""
Burn Logic Helper Functions für Hedge Bots

Dieses Modul enthält gemeinsame Helper-Funktionen für die Burn-Logik,
die von beiden Bots (Long und Short) verwendet werden.
"""

import logging
import math

logger = logging.getLogger('BurnLogic')


def calculate_profit(entry_price, trigger_price, qty, bot_type):
    """
    Berechnet den Profit basierend auf Bot-Typ.
    
    Args:
        entry_price: Entry-Preis der Position
        trigger_price: TP-Preis (Trigger-Preis)
        qty: Menge der gefüllten Position
        bot_type: 'long' oder 'short'
    
    Returns:
        float: Profit in USDT, oder None bei Fehler
    """
    try:
        entry_price = float(entry_price or 0)
        trigger_price = float(trigger_price or 0)
        qty = float(qty or 0)
    except (ValueError, TypeError):
        logger.error(f"❌ Ungültige Zahlenwerte in Profit-Berechnung: entry={entry_price}, trigger={trigger_price}, qty={qty}")
        return None
    
    if qty <= 0 or trigger_price <= 0 or entry_price <= 0:
        logger.error(f"❌ Profit-Berechnung übersprungen – ungültige Eingabewerte: qty={qty}, trigger={trigger_price}, entry={entry_price}")
        return None
    
    if bot_type == 'long':
        # Long: TP > Entry = Profit
        profit = (trigger_price - entry_price) * qty
    else:  # short
        # Short: Entry > TP = Profit
        profit = (entry_price - trigger_price) * qty
    
    if profit <= 0:
        logger.warning(f"⚠️ Profit <= 0: {profit}")
        return None
    
    return profit


def calculate_loss_per_coin(burn_price, position_avg, bot_type):
    """
    Berechnet den Loss pro Coin basierend auf Bot-Typ.
    
    Args:
        burn_price: Preis bei dem der Burn passiert (trigger_price)
        position_avg: Durchschnittlicher Entry-Preis der zu reduzierenden Position
        bot_type: 'long' oder 'short'
    
    Returns:
        float: Loss pro Coin in USDT, oder None bei Fehler
    """
    try:
        burn_price = float(burn_price or 0)
        position_avg = float(position_avg or 0)
    except (ValueError, TypeError):
        logger.error(f"❌ Ungültige Zahlenwerte in Loss-Berechnung: burn_price={burn_price}, position_avg={position_avg}")
        return None
    
    if burn_price <= 0 or position_avg <= 0:
        logger.error(f"❌ Loss-Berechnung übersprungen – ungültige Eingabewerte: burn_price={burn_price}, position_avg={position_avg}")
        return None
    
    if bot_type == 'long':
        # Long: Loss = avg - burn_price (wenn Preis fällt, Long verliert)
        loss_per_coin = position_avg - burn_price
    else:  # short
        # Short: Loss = burn_price - avg (wenn Preis steigt, Short verliert)
        loss_per_coin = burn_price - position_avg
    
    if loss_per_coin <= 0:
        logger.warning(f"⚠️ LossPerCoin <= 0 (position_avg={position_avg}, burn_price={burn_price})")
        return None
    
    return loss_per_coin


def calculate_burn_size(burn_profit, loss_per_coin):
    """
    Berechnet die Burn-Size in Coins.
    
    Args:
        burn_profit: Profit der für den Burn verwendet wird (in USDT)
        loss_per_coin: Loss pro Coin (in USDT)
    
    Returns:
        float: Burn-Size in Coins, oder None bei Fehler
    """
    try:
        burn_profit = float(burn_profit or 0)
        loss_per_coin = float(loss_per_coin or 0)
    except (ValueError, TypeError):
        logger.error(f"❌ Ungültige Zahlenwerte in Burn-Size-Berechnung: burn_profit={burn_profit}, loss_per_coin={loss_per_coin}")
        return None
    
    if burn_profit <= 0 or loss_per_coin <= 0:
        logger.error(f"❌ Burn-Size-Berechnung übersprungen – ungültige Eingabewerte: burn_profit={burn_profit}, loss_per_coin={loss_per_coin}")
        return None
    
    burn_size = burn_profit / loss_per_coin
    
    if burn_size <= 0:
        logger.warning(f"⚠️ Berechnete BurnSize <= 0: {burn_size}")
        return None
    
    return burn_size


def limit_burn_size(burn_size, position_size):
    """
    Begrenzt die Burn-Size auf maximal 90% der Position-Size.
    
    Args:
        burn_size: Berechnete Burn-Size
        position_size: Aktuelle Position-Size
    
    Returns:
        float: Begrenzte Burn-Size
    """
    if burn_size >= position_size:
        logger.warning(f"⚠️ BurnSize ({burn_size:.6f}) >= PositionSize ({position_size:.6f}) – begrenze auf 90% der Position-Size.")
        burn_size = position_size * 0.9
        logger.info(f"  • BurnSize (begrenzt auf 90%) = {burn_size:.6f} Coins")
    
    return burn_size


def round_down_to_step(value, step):
    """
    Rundet den Wert auf das nächste Vielfache von ``step`` ab.
    """
    try:
        value = float(value or 0)
        step = float(step or 0)
    except (ValueError, TypeError):
        return value

    if step <= 0:
        return value

    rounded = math.floor(value / step) * step
    return max(rounded, 0.0)


def plan_profit_burn(
    realized_profit: float,
    burn_pct: float,
    loss_price: float,
    position_avg: float,
    position_size: float,
    qty_step: float,
    min_qty: float,
    burn_profit_pct: float | None = None,
):
    """
    Berechnet den Burn in Coins.

    WICHTIG:
    - burn_pct ist ein Anteil der Position (z. B. 0.25 = 25% der aktuellen Position)
      und wirkt als Obergrenze auf die zu reduzierende Positionsgroesse.
    - burn_profit_pct (falls gesetzt) begrenzt zusaetzlich den maximal zulaessigen
      Verlust auf einen Anteil des realisierten TP-Profits:

          max_burn_loss_usdt = realized_profit * burn_profit_pct

      Damit ist der Netto-PnL pro Burn immer
          >= realized_profit * (1 - burn_profit_pct),
      solange realized_profit > 0.
    """
    try:
        realized_profit = float(realized_profit or 0)
        burn_pct = float(burn_pct or 0)
        loss_price = float(loss_price or 0)
        position_avg = float(position_avg or 0)
        position_size = float(position_size or 0)
        burn_profit_pct = float(burn_profit_pct) if burn_profit_pct is not None else None
    except (ValueError, TypeError):
        logger.error(
            "[PLAN-BURN] Ungültige Werte: "
            f"profit={realized_profit}, burn_pct={burn_pct}, loss_price={loss_price}, avg={position_avg}, size={position_size}"
        )
        return None

    if position_size <= 0 or burn_pct <= 0:
        logger.warning(
            "[PLAN-BURN] Burn skipped – size/burn_pct ungueltig "
            f"(profit={realized_profit}, size={position_size}, burn_pct={burn_pct})"
        )
        return None

    # Obergrenze 1: Anteil der Positionsgroesse
    burn_coins = position_size * burn_pct
    if burn_coins <= 0:
        logger.warning(
            "[PLAN-BURN] Berechnete Burn-Coins <= 0 – Burn skipped "
            f"(position_size={position_size}, burn_pct={burn_pct})"
        )
        return None

    loss_per_coin = abs(loss_price - position_avg)
    if loss_per_coin <= 0:
        logger.warning(
            "[PLAN-BURN] Verlust pro Coin <= 0 – Burn nicht geplant "
            f"(loss_price={loss_price}, avg={position_avg})"
        )
        return None

    # Obergrenze 2 (optional): Profit-Budget
    max_coins = min(position_size, burn_coins)
    if burn_profit_pct is not None and realized_profit > 0 and loss_per_coin > 0:
        profit_budget = realized_profit * burn_profit_pct
        max_coins_by_profit = profit_budget / loss_per_coin
        if max_coins_by_profit <= 0:
            logger.warning(
                "[PLAN-BURN] Profit-Budget ergibt keine positive Burn-Menge – skipped "
                f"(profit={realized_profit}, burn_profit_pct={burn_profit_pct})"
            )
            return None
        max_coins = min(max_coins, max_coins_by_profit)

    qty_step = float(qty_step or 0)
    min_qty = float(min_qty or 0)
    if qty_step > 0:
        max_coins = round_down_to_step(max_coins, qty_step)

    if max_coins <= 0 or (min_qty > 0 and max_coins < min_qty):
        logger.warning(
            "[PLAN-BURN] Burn-Menge unter Minimum – skipped "
            f"(coins={max_coins}, min_qty={min_qty})"
        )
        return None

    # Reporting: "burn_usdt_target" entspricht dem erwarteten Hedging-Verlust
    # fuer die effektiv ausfuehrbare Burn-Menge.
    burn_usdt_target = max_coins * loss_per_coin

    return {
        "burn_usdt_target": burn_usdt_target,
        "loss_per_coin": loss_per_coin,
        "burn_coins": burn_coins,
        "burn_coins_clamped": max_coins,
    }


def estimate_fee_target_usd(
    *,
    fee_scope: str,
    fee_rate: float,
    fee_buffer_pct: float | None,
    tp_price: float,
    profit_position_size: float,
    burn_plan: dict[str, float] | None = None,
    total_notional: float | None = None,
) -> float:
    """
    Schätzt das Netto-PnL-Ziel, das mindestens zur Deckung der Fees nötig ist.

    fee_scope:
    - "full_hedge": Fees auf das komplette Hedge-Notional
    - "burn_cycle": Fees nur auf TP-Exit + effektiven Burn
    - "none": keine Fee-Anforderung
    """
    try:
        fee_scope = str(fee_scope or "none").strip().lower()
        fee_rate = float(fee_rate or 0.0)
        fee_buffer_pct = float(fee_buffer_pct) if fee_buffer_pct is not None else 0.0
        tp_price = float(tp_price or 0.0)
        profit_position_size = float(profit_position_size or 0.0)
        total_notional = float(total_notional) if total_notional is not None else None
    except (TypeError, ValueError):
        return 0.0

    if fee_rate <= 0:
        return 0.0

    if fee_scope == "full_hedge":
        fee_notional = abs(total_notional or 0.0)
    elif fee_scope == "burn_cycle":
        burn_size = 0.0
        if burn_plan:
            try:
                burn_size = float(burn_plan.get("burn_coins_clamped", 0.0) or 0.0)
            except (TypeError, ValueError):
                burn_size = 0.0
        fee_notional = abs(tp_price * profit_position_size) + abs(tp_price * burn_size)
    else:
        fee_notional = 0.0

    fees_usd = max(fee_notional * fee_rate, 0.0)
    return fees_usd * (1.0 + max(fee_buffer_pct, 0.0) / 100.0)


def _simulate_burn_net_pnl(
    *,
    tp_side: str,
    profit_position_avg: float,
    profit_position_size: float,
    loss_position_avg: float,
    loss_position_size: float,
    distance_pct: float,
    burn_pct: float | None,
    burn_profit_pct: float | None,
) -> dict[str, float | dict[str, float] | None] | None:
    """Simulate exact net burn pnl for a candidate TP distance."""
    if tp_side == "short":
        tp_price = profit_position_avg * (1.0 - distance_pct / 100.0)
    elif tp_side == "long":
        tp_price = profit_position_avg * (1.0 + distance_pct / 100.0)
    else:
        return None

    if tp_price <= 0:
        return None

    realized_profit = calculate_profit(
        entry_price=profit_position_avg,
        trigger_price=tp_price,
        qty=profit_position_size,
        bot_type=tp_side,
    )
    if realized_profit is None or realized_profit <= 0:
        return None

    burn_plan = plan_profit_burn(
        realized_profit=realized_profit,
        burn_pct=burn_pct if burn_pct is not None else 0.0,
        loss_price=tp_price,
        position_avg=loss_position_avg,
        position_size=loss_position_size,
        qty_step=0.0,
        min_qty=0.0,
        burn_profit_pct=burn_profit_pct,
    )
    burn_loss = float(burn_plan["burn_usdt_target"]) if burn_plan else 0.0
    return {
        "tp_price": tp_price,
        "realized_profit": realized_profit,
        "burn_plan": burn_plan,
        "burn_loss": burn_loss,
        "net_burn_pnl": realized_profit - burn_loss,
    }


def evaluate_burn_plan_at_tp(
    *,
    tp_side: str,
    tp_price: float,
    profit_position_avg: float,
    profit_position_size: float,
    loss_position_avg: float,
    loss_position_size: float,
    burn_pct: float | None,
    burn_profit_pct: float | None,
    qty_step: float,
    min_qty: float,
) -> dict[str, float | dict[str, float] | None] | None:
    """Evaluate realized profit, burn plan and net pnl at a concrete TP price."""
    if tp_price <= 0:
        return None

    realized_profit = calculate_profit(
        entry_price=profit_position_avg,
        trigger_price=tp_price,
        qty=profit_position_size,
        bot_type=tp_side,
    )
    if realized_profit is None or realized_profit <= 0:
        return None

    burn_plan = plan_profit_burn(
        realized_profit=realized_profit,
        burn_pct=burn_pct if burn_pct is not None else 0.0,
        loss_price=tp_price,
        position_avg=loss_position_avg,
        position_size=loss_position_size,
        qty_step=qty_step,
        min_qty=min_qty,
        burn_profit_pct=burn_profit_pct,
    )
    burn_loss = float(burn_plan["burn_usdt_target"]) if burn_plan else 0.0
    return {
        "tp_price": tp_price,
        "realized_profit": realized_profit,
        "burn_plan": burn_plan,
        "burn_loss": burn_loss,
        "net_burn_pnl": realized_profit - burn_loss,
    }


def adjust_burn_tp_for_required_net(
    *,
    tp_side: str,
    initial_tp_price: float,
    tick_size: float,
    required_net_pnl: float,
    profit_position_avg: float,
    profit_position_size: float,
    loss_position_avg: float,
    loss_position_size: float,
    burn_pct: float | None,
    burn_profit_pct: float | None,
    qty_step: float,
    min_qty: float,
    limit_tp_price: float | None = None,
    fee_scope: str = "none",
    fee_rate: float = 0.0,
    fee_buffer_pct: float | None = None,
    total_notional: float | None = None,
    max_adjustment_ticks: int = 5000,
) -> dict[str, float | dict[str, float] | None] | None:
    """Nudge TP outward tick-by-tick until the rounded burn plan meets the target."""
    try:
        tick_size = float(tick_size or 0.0)
        required_net_pnl = float(required_net_pnl or 0.0)
        limit_tp_price = float(limit_tp_price) if limit_tp_price is not None else None
    except (TypeError, ValueError):
        return None

    if tick_size <= 0:
        return None

    fee_scope = str(fee_scope or "none").strip().lower()

    def _required_net_for_candidate(candidate: dict[str, float | dict[str, float] | None]) -> float:
        fee_target = estimate_fee_target_usd(
            fee_scope=fee_scope,
            fee_rate=fee_rate,
            fee_buffer_pct=fee_buffer_pct,
            tp_price=float(candidate.get("tp_price") or 0.0),
            profit_position_size=profit_position_size,
            burn_plan=candidate.get("burn_plan"),
            total_notional=total_notional,
        )
        candidate["fee_target_usd"] = fee_target
        candidate["required_net_pnl"] = max(required_net_pnl, fee_target)
        return float(candidate["required_net_pnl"])

    initial = evaluate_burn_plan_at_tp(
        tp_side=tp_side,
        tp_price=initial_tp_price,
        profit_position_avg=profit_position_avg,
        profit_position_size=profit_position_size,
        loss_position_avg=loss_position_avg,
        loss_position_size=loss_position_size,
        burn_pct=burn_pct,
        burn_profit_pct=burn_profit_pct,
        qty_step=qty_step,
        min_qty=min_qty,
    )
    if initial is None:
        return None
    initial["adjustment_ticks"] = 0

    burn_plan = initial.get("burn_plan")
    initial_required_net_pnl = _required_net_for_candidate(initial)
    if burn_plan is not None and float(initial["net_burn_pnl"]) >= initial_required_net_pnl:
        return initial

    direction = -1.0 if tp_side == "short" else 1.0
    best = initial
    price = float(initial_tp_price)

    for ticks in range(1, max_adjustment_ticks + 1):
        price += direction * tick_size
        if price <= 0:
            break
        if limit_tp_price is not None:
            if tp_side == "short" and price < limit_tp_price:
                break
            if tp_side == "long" and price > limit_tp_price:
                break

        candidate = evaluate_burn_plan_at_tp(
            tp_side=tp_side,
            tp_price=price,
            profit_position_avg=profit_position_avg,
            profit_position_size=profit_position_size,
            loss_position_avg=loss_position_avg,
            loss_position_size=loss_position_size,
            burn_pct=burn_pct,
            burn_profit_pct=burn_profit_pct,
            qty_step=qty_step,
            min_qty=min_qty,
        )
        if candidate is None:
            continue
        candidate["adjustment_ticks"] = ticks
        best = candidate
        burn_plan = candidate.get("burn_plan")
        candidate_required_net_pnl = _required_net_for_candidate(candidate)
        if burn_plan is not None and float(candidate["net_burn_pnl"]) >= candidate_required_net_pnl:
            return candidate

    return best


def compute_dynamic_burn_distance_pct(
    *,
    tp_side: str,
    reference_notional: float,
    profit_position_avg: float,
    profit_position_size: float,
    loss_position_avg: float,
    loss_position_size: float,
    target_net_burn_profit_pct: float,
    fee_rate: float = 0.00055,
    fee_factor_k: float | None = None,
    fee_buffer_pct: float | None = None,
    burn_pct: float | None = None,
    burn_profit_pct: float | None = None,
    min_burn_distance_pct: float = 0.3,
    max_burn_distance_pct: float = 2.5,
    atr_floor_pct: float = 0.0,
    fee_scope: str = "full_hedge",
) -> float | None:
    """
    Berechnet die Burn-Distanz in Prozent relativ zum TP-Entry der Profit-Seite,
    so dass der exakte Netto-Burn-Profit (TP-Profit minus effektiver Burn-Verlust)
    Fees + Puffer bzw. das konfigurierte Ziel erreicht.
    """
    try:
        reference_notional = float(reference_notional or 0.0)
        profit_position_avg = float(profit_position_avg or 0.0)
        profit_position_size = float(profit_position_size or 0.0)
        loss_position_avg = float(loss_position_avg or 0.0)
        loss_position_size = float(loss_position_size or 0.0)
        target_net_burn_profit_pct = float(target_net_burn_profit_pct or 0.0) / 100.0
        fee_rate = float(fee_rate or 0.0)
        fee_factor_k = float(fee_factor_k) if fee_factor_k is not None else None
        fee_buffer_pct = float(fee_buffer_pct) if fee_buffer_pct is not None else None
        burn_pct = float(burn_pct) if burn_pct is not None else None
        burn_profit_pct = float(burn_profit_pct) if burn_profit_pct is not None else None
        min_burn_distance_pct = float(min_burn_distance_pct or 0.0)
        max_burn_distance_pct = float(max_burn_distance_pct or 0.0)
        atr_floor_pct = float(atr_floor_pct or 0.0)
        fee_scope = str(fee_scope or "full_hedge").strip().lower()
    except (TypeError, ValueError):
        logger.warning("[DYN-BURN] Ungültige Eingaben für compute_dynamic_burn_distance_pct")
        return None

    if (
        reference_notional <= 0
        or profit_position_avg <= 0
        or profit_position_size <= 0
        or loss_position_avg <= 0
        or loss_position_size <= 0
    ):
        return None

    target_net_usd = reference_notional * target_net_burn_profit_pct if target_net_burn_profit_pct > 0 else 0.0
    total_notional = abs(profit_position_avg * profit_position_size) + abs(loss_position_avg * loss_position_size)
    if fee_buffer_pct is None:
        if fee_factor_k is not None:
            fee_buffer_pct = max((fee_factor_k - 1.0) * 100.0, 0.0)
        else:
            fee_buffer_pct = 0.0
    fee_profit_target_usd = estimate_fee_target_usd(
        fee_scope=fee_scope,
        fee_rate=fee_rate,
        fee_buffer_pct=fee_buffer_pct,
        tp_price=profit_position_avg,
        profit_position_size=profit_position_size,
        burn_plan=None,
        total_notional=total_notional,
    )
    if target_net_usd <= 0 and fee_profit_target_usd <= 0:
        return None

    floor_pct = max(min_burn_distance_pct, atr_floor_pct)

    def _margin_at(distance_pct: float) -> float | None:
        simulated = _simulate_burn_net_pnl(
            tp_side=tp_side,
            profit_position_avg=profit_position_avg,
            profit_position_size=profit_position_size,
            loss_position_avg=loss_position_avg,
            loss_position_size=loss_position_size,
            distance_pct=distance_pct,
            burn_pct=burn_pct,
            burn_profit_pct=burn_profit_pct,
        )
        if simulated is None:
            return None
        fee_target = estimate_fee_target_usd(
            fee_scope=fee_scope,
            fee_rate=fee_rate,
            fee_buffer_pct=fee_buffer_pct,
            tp_price=float(simulated["tp_price"] or 0.0),
            profit_position_size=profit_position_size,
            burn_plan=simulated.get("burn_plan"),
            total_notional=total_notional,
        )
        required_target = max(target_net_usd, fee_target)
        return float(simulated["net_burn_pnl"]) - required_target

    floor_margin = _margin_at(floor_pct)
    if floor_margin is not None and floor_margin >= 0:
        return floor_pct

    hi = max(floor_pct, 0.5)
    if max_burn_distance_pct > 0:
        hi = max(hi, max_burn_distance_pct)
        hi_margin = _margin_at(hi)
        if hi_margin is None:
            return None
        if hi_margin < 0:
            return hi
    else:
        hi_margin = _margin_at(hi)
        attempts = 0
        while (hi_margin is None or hi_margin < 0) and hi < 500.0 and attempts < 24:
            hi *= 2.0
            hi_margin = _margin_at(hi)
            attempts += 1
        if hi_margin is None:
            return None
        if hi_margin < 0:
            return hi

    lo = floor_pct
    for _ in range(40):
        mid = (lo + hi) / 2.0
        mid_margin = _margin_at(mid)
        if mid_margin is None:
            lo = mid
            continue
        if mid_margin >= 0:
            hi = mid
        else:
            lo = mid

    eff = hi
    if eff <= 0:
        return None
    return eff


def calculate_burn_profit(profit, burn_percentage):
    """
    Berechnet den Burn-Profit (nur ein Teil des Profits wird für den Burn verwendet).
    
    Args:
        profit: Gesamter Profit in USDT
        burn_percentage: Prozentsatz des Profits der für den Burn verwendet wird (z.B. 0.2 für 20%)
            Typischerweise stammt dieser Wert aus der Config (`burn_pct`).
    
    Returns:
        float: Burn-Profit in USDT
    """
    return profit * burn_percentage


def calculate_new_position_after_burn(position_size, position_avg, burn_size, burn_price):
    """
    Berechnet die neue Position nach dem Burn.
    
    Args:
        position_size: Aktuelle Position-Size
        position_avg: Aktueller Durchschnittspreis
        burn_size: Burn-Size in Coins
        burn_price: Preis bei dem der Burn passiert
    
    Returns:
        tuple: (new_size, new_avg, new_cost_basis, old_cost_basis)
    """
    old_cost_basis = position_size * position_avg
    new_size = position_size - burn_size
    new_cost_basis = old_cost_basis - (burn_size * burn_price)
    new_avg = new_cost_basis / new_size if new_size > 0 else 0.0
    
    return new_size, new_avg, new_cost_basis, old_cost_basis

