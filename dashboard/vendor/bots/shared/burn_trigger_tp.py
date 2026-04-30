"""
Burn Trigger TP Calculation Helper

Pure calculation function for burn-trigger TP price computation.
Supports both Long-TP (TP > base) and Short-TP (TP < base).
"""

from typing import Callable, Any


def calculate_burn_trigger_tp_price(
    # Position-Daten
    tp_side: str,  # "long" oder "short"
    base_avg: float,  # Entry-Preis der Position (long_avg für Long-TP, short_avg für Short-TP)
    current_mark_price: float,
    
    # Config-Werte
    burn_mode: str,
    burn_levels: list[float] | None,
    start_burn_index: int,
    tp_percentage: float,
    
    # Tick/EPS-Parameter
    tick_size: float,
    strategy_eps_ticks: int,
    
    # Helper-Funktionen (müssen übergeben werden)
    ceil_to_tick_fn: Callable[[float, float], float],
    floor_to_tick_fn: Callable[[float, float], float],
    compute_long_tp_from_base_fn: Callable[[float, float, float], float],
    eps_price_fn: Callable[[float, float, int, str], float],
    
    # Optional: BE-Preis (für Warning)
    be_price: float | None = None,
) -> dict[str, Any]:
    """
    Berechnet den Burn-Trigger-TP-Preis (Long-TP im Short-Bot, Short-TP im Long-Bot).
    
    Args:
        tp_side: "long" (TP > base) oder "short" (TP < base)
        base_avg: Entry-Preis der Position (long_avg für Long-TP, short_avg für Short-TP)
    
    Returns:
        {
            "tp_price": float | None,  # Berechneter TP-Preis oder None bei Fehler
            "tp_mode_info": str,  # "percentage(+X%)" oder "fixed_levels(idx=X, level=Y.YY)"
            "chosen_level_index": int | None,  # Nur wenn fixed_levels und erfolgreich
            "warning": str | None,  # Warning-Text wenn tp_price < be_price (Long) oder tp_price > be_price (Short)
            "is_valid": bool,  # False wenn Guard verletzt
            "error": str | None,  # Error-Text wenn is_valid == False
        }
    """
    # Schritt 1: Initialisierung
    tp_price = None
    tp_mode_info = ""
    chosen_idx = None
    is_long = tp_side == "long"
    
    # Schritt 2: Fixed-Levels-Modus (wenn burn_mode == "fixed_levels" und burn_levels existiert)
    if burn_mode == "fixed_levels" and burn_levels:
        start_idx = int(start_burn_index)
        for i in range(max(0, start_idx), len(burn_levels)):
            lvl = float(burn_levels[i])
            
            if is_long:
                # Long-TP: Level muss über base_avg und über mark sein
                lvl_rounded = ceil_to_tick_fn(lvl, tick_size)
                if lvl_rounded <= float(base_avg):
                    continue
                if lvl_rounded <= float(current_mark_price):
                    continue
            else:
                # Short-TP: Level muss unter base_avg und unter mark sein
                lvl_rounded = floor_to_tick_fn(lvl, tick_size)
                if lvl_rounded >= float(base_avg):
                    continue
                if lvl_rounded >= float(current_mark_price):
                    continue
            
            if lvl_rounded <= 0:
                continue
            
            chosen_idx = i
            tp_price = lvl_rounded
            break
        
        if chosen_idx is not None:
            tp_mode_info = f"fixed_levels(idx={chosen_idx}, level={tp_price:.6f})"
    
    # Schritt 3: Percentage-Modus (wenn tp_price is None)
    if tp_price is None:
        if is_long:
            # Long-TP: TP > base, ceil_to_tick, eps "down"
            tp_price = compute_long_tp_from_base_fn(base_avg, tp_percentage, tick_size)
            tp_price = eps_price_fn(tp_price, tick_size, strategy_eps_ticks, "down")
            tp_mode_info = f"percentage(+{tp_percentage}%)"
        else:
            # Short-TP: TP < base, floor_to_tick, eps "up"
            raw_tp = base_avg * (1 - tp_percentage / 100.0)
            tp_price = floor_to_tick_fn(raw_tp, tick_size)
            tp_price = eps_price_fn(tp_price, tick_size, strategy_eps_ticks, "up")
            tp_mode_info = f"percentage(-{tp_percentage}%)"
    
    # Schritt 4: Optional Warning
    warning = None
    if be_price is not None:
        if is_long and tp_price < be_price:
            warning = (
                "[BURN] ⚠️ Entry-basierter Long-TP liegt unter Hedge-BE "
                f"(TP={tp_price:.6f} < BE={be_price:.6f}) – Burn-Trigger kann mathematisch aggressiv sein"
            )
        elif not is_long and tp_price > be_price:
            warning = (
                "[BURN] ⚠️ Entry-basierter Short-TP liegt über Hedge-BE "
                f"(TP={tp_price:.6f} > BE={be_price:.6f}) – Burn-Trigger kann mathematisch aggressiv sein"
            )
    
    # Schritt 5: MarkPrice-Guard (Validierung)
    is_valid = True
    error = None
    if is_long:
        # Long-TP: TP muss über Mark sein
        if tp_price <= current_mark_price:
            is_valid = False
            be_price_str = f"{be_price:.6f}" if be_price is not None else "None"
            error = (
                "[BURN] ❌ Entry-basierter Long-TP liegt nicht über dem Markt – skip "
                f"(LongAvg={float(base_avg):.6f}, TP={tp_price:.6f}, Mark={current_mark_price:.6f}, BE={be_price_str})"
            )
    else:
        # Short-TP: TP muss unter Mark sein
        if tp_price >= current_mark_price:
            is_valid = False
            be_price_str = f"{be_price:.6f}" if be_price is not None else "None"
            error = (
                "[BURN] ❌ Entry-basierter Short-TP liegt nicht unter dem Markt – skip "
                f"(ShortAvg={float(base_avg):.6f}, TP={tp_price:.6f}, Mark={current_mark_price:.6f}, BE={be_price_str})"
            )
    
    # Schritt 6: Rückgabe
    return {
        "tp_price": tp_price,
        "tp_mode_info": tp_mode_info,
        "chosen_level_index": chosen_idx,
        "warning": warning,
        "is_valid": is_valid,
        "error": error,
    }
