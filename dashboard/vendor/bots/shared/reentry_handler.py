"""
Reentry Handler für Hedge Bots

Dieses Modul enthält gemeinsame Funktionen für Reentry-Logik nach TP-Fills,
die von beiden Bots (Long und Short) verwendet werden.
"""

import logging
import time

logger = logging.getLogger('ReentryHandler')


def _effective_hedge_ratio(ctx) -> float:
    hedge_ratio = getattr(ctx, "current_hedge_ratio", None)
    try:
        hedge_ratio = float(hedge_ratio)
    except (TypeError, ValueError):
        hedge_ratio = None
    if hedge_ratio is None or hedge_ratio <= 0 or hedge_ratio > 1.0:
        try:
            hedge_ratio = float(getattr(ctx, "hedge_ratio", 0.8) or 0.8)
        except (TypeError, ValueError):
            hedge_ratio = 0.8
    if hedge_ratio <= 0 or hedge_ratio > 1.0:
        hedge_ratio = 0.8
    return float(hedge_ratio)


def place_stop_short_reentry(
    ctx,
    sub_order_manager,
    symbol,
    logger_instance=None
):
    """
    Setzt eine direkte Short-Market-Order nach Short-TP-Fill.
    Wird NACH Short-TP + Burn aufgerufen.
    Size = 50% der erwarteten Long-Size (nach Burn) - Short = 50% von Long (50% vs 100%).
    
    WICHTIG: Direkte Market-Order - keine Stop-Order mehr!
    
    Args:
        ctx: BotContext-Instanz
        sub_order_manager: Sub-Account OrderManager
        symbol: Trading-Symbol
        logger_instance: Optional logger (falls None, verwendet Modul-Logger)
    
    Returns:
        bool: True wenn erfolgreich, False sonst
    """
    log = logger_instance or logger
    
    # Hole erwartete Long-Size nach Burn aus ctx.burn_state (bereits in perform_burn_logic() berechnet)
    expected_long_size = ctx.burn_state.get("expected_long_size_after_burn")
    last_short_tp_price = ctx.burn_state.get("last_short_tp_price")
    
    if not expected_long_size or expected_long_size <= 0:
        log.warning("[REENTRY] Erwartete Long-Size nach Burn nicht verfügbar – Reentry übersprungen.")
        return False
    
    if not last_short_tp_price or last_short_tp_price <= 0:
        log.warning("[REENTRY] Short-TP-Preis nicht verfügbar – Reentry übersprungen.")
        return False

    # Short-Size berechnen: Erwartete Long-Size × hedge_ratio (Standard 0.8, Fallback 0.5)
    hedge_ratio = _effective_hedge_ratio(ctx)
    short_size = expected_long_size * hedge_ratio

    if short_size <= 0:
        log.warning(f"[REENTRY] Berechnete Short-Size zu klein ({short_size:.6f}) – Reentry übersprungen.")
        return False

    # WICHTIG: KEIN Config-Write!
    # target_short_notional ist ein absoluter Config-Wert (Start-/Rebuy-Parameter).
    # Zyklus-/Reentry-Werte bleiben nur im Runtime-State (ctx/state file), damit ein Restart keinen absoluten Drift reaktiviert.
    short_notional = short_size * last_short_tp_price
    ctx.target_short_notional = short_notional
    log.info(f"[REENTRY] ℹ️ cycle_short_notional (RAM/state): {short_notional:.2f} USDT (Config bleibt unverändert)")

    if sub_order_manager is None:
        log.error("[REENTRY] ❌ Sub-Account OrderManager nicht verfügbar – kann Short-Reentry nicht setzen")
        return False

    log.info("=" * 60)
    log.info(f"[REENTRY] ⚡ Short-Reentry: Direkte Market-Order")
    log.info(f"  • Short-TP-Preis: {last_short_tp_price:.6f}")
    log.info(f"  • Erwartete Long-Size (nach Burn): {expected_long_size:.6f} Coins")
    log.info(f"  • Short-Size ({hedge_ratio*100:.1f}% von Long): {short_size:.6f} Coins")
    log.info(f"  • Short Notional: {short_notional:.2f} USDT")
    log.info("=" * 60)

    # WICHTIG: Double-Check - prüfe nochmal, ob Short-Position wirklich geschlossen ist
    # TP-Orders schließen immer die komplette Position, aber es kann Timing-Probleme geben
    log.info("[REENTRY] 🔍 Double-Check: Prüfe, ob Short-Position wirklich geschlossen ist...")
    final_short_size_check, _ = sub_order_manager.get_short_position(symbol)
    if final_short_size_check and final_short_size_check > 0:
        log.warning(f"[REENTRY] ⚠️ Short-Position existiert noch ({final_short_size_check:.6f}) nach TP-Fill – könnte Teil-Fill oder Timing-Problem sein")
        log.warning(f"[REENTRY] ⚠️ Warte kurz und prüfe nochmal...")
        # Kurze Wartezeit für API-Update
        time.sleep(0.5)
        # Prüfe nochmal
        final_short_size_check_retry, _ = sub_order_manager.get_short_position(symbol)
        if final_short_size_check_retry and final_short_size_check_retry > 0:
            log.error(f"[REENTRY] ❌ Short-Position existiert immer noch ({final_short_size_check_retry:.6f}) – ABBRUCH: Keine Market-Order gesetzt")
            log.error(f"[REENTRY] ❌ Dies könnte ein Teil-Fill sein oder die Position wurde nicht vollständig geschlossen")
            return False  # ABBRUCH: Keine Market-Order setzen, wenn Position noch existiert
        else:
            log.info(f"[REENTRY] ✅ Short-Position ist jetzt geschlossen (nach Retry) – setze Market-Order")
    else:
        log.info(f"[REENTRY] ✅ Short-Position ist geschlossen (Size=0 oder None) – setze Market-Order")

    # WICHTIG: Direkte Market-Order - keine Stop-Order mehr!
    market_order = sub_order_manager.open_short_market(symbol, short_size)
    
    if market_order:
        log.info(f"[REENTRY] ✅ Short Market-Order erfolgreich gesetzt: {short_size:.6f} Coins")
        log.info(f"[REENTRY] ℹ️ TP/SL wird automatisch über handle_position_update() gesetzt, wenn Position eröffnet wird")
        # WICHTIG: Stelle sicher, dass die Position-Update-Logik nicht blockiert wird
        # Setze last_processed_position zurück, damit die neue Position erkannt wird
        ctx.last_processed_position = {}
        log.info(f"[REENTRY] 🔄 Position-Tracking zurückgesetzt, damit neue Position erkannt wird")
        return True
    else:
        log.error("[REENTRY] ❌ Short Market-Order fehlgeschlagen")
        return False


def place_stop_long_reentry(
    ctx,
    main_order_manager,
    order_manager,
    symbol,
    target_short_notional,
    logger_instance=None
):
    """
    Setzt eine direkte Long-Market-Order nach Long-TP-Fill.
    Wird NACH Long-TP + Burn aufgerufen.
    Size = 2x der erwarteten Short-Size (nach Burn) - Long = 2x Short (100% vs 50%).
    
    WICHTIG: Direkte Market-Order - keine Stop-Order mehr!
    
    Args:
        ctx: BotContext-Instanz
        main_order_manager: Main-Account OrderManager
        order_manager: Sub-Account OrderManager (für Short-Position)
        symbol: Trading-Symbol
        target_short_notional: Target Short Notional aus Config (für Fallback)
        logger_instance: Optional logger (falls None, verwendet Modul-Logger)
    
    Returns:
        bool: True wenn erfolgreich, False sonst
    """
    log = logger_instance or logger
    
    # Hole erwartete Short-Size nach Burn aus ctx.burn_state (bereits in perform_burn_logic() berechnet)
    expected_short_size = ctx.burn_state.get("expected_short_size_after_burn")
    last_long_tp_price = ctx.burn_state.get("last_long_tp_price")
    
    # FALLBACK: Wenn expected_short_size nicht verfügbar ist, berechne es basierend auf aktueller Short-Position
    if not expected_short_size or expected_short_size <= 0:
        log.warning("[REENTRY] ⚠️ Erwartete Short-Size nach Burn nicht verfügbar – verwende Fallback-Berechnung")
        try:
            # Hole aktuelle Short-Position als Fallback
            short_size_now, short_avg_now = order_manager.get_short_position(symbol)
            if short_size_now and short_size_now > 0:
                # Verwende aktuelle Short-Size als erwartete Size (nach Burn sollte sie ähnlich sein)
                expected_short_size = short_size_now
                log.info(f"[REENTRY] ✅ Fallback: Verwende aktuelle Short-Size als erwartete Size: {expected_short_size:.6f}")
            else:
                # Wenn keine Short-Position existiert, verwende TARGET_SHORT_NOTIONAL
                if last_long_tp_price and last_long_tp_price > 0:
                    expected_short_size = target_short_notional / last_long_tp_price
                    log.info(f"[REENTRY] ✅ Fallback: Berechne erwartete Short-Size aus TARGET_SHORT_NOTIONAL: {expected_short_size:.6f}")
                else:
                    log.error("[REENTRY] ❌ Kann erwartete Short-Size nicht berechnen – Reentry übersprungen.")
                    return False
        except Exception as e:
            log.error(f"[REENTRY] ❌ Fehler bei Fallback-Berechnung: {e}", exc_info=True)
            return False
    
    if not last_long_tp_price or last_long_tp_price <= 0:
        log.warning("[REENTRY] ⚠️ Long-TP-Preis nicht verfügbar – verwende Fallback")
        try:
            # Verwende aktuellen Preis als Fallback
            if main_order_manager:
                current_price = main_order_manager.get_current_price(symbol)
                if current_price and current_price > 0:
                    last_long_tp_price = current_price
                    log.info(f"[REENTRY] ✅ Fallback: Verwende aktuellen Preis als Long-TP-Preis: {last_long_tp_price:.6f}")
                else:
                    log.error("[REENTRY] ❌ Kann Long-TP-Preis nicht ermitteln – Reentry übersprungen.")
                    return False
            else:
                log.error("[REENTRY] ❌ Main-Account OrderManager nicht verfügbar – Reentry übersprungen.")
                return False
        except Exception as e:
            log.error(f"[REENTRY] ❌ Fehler bei Fallback-Berechnung: {e}", exc_info=True)
            return False

    # Long-Size berechnen: Erwartete Short-Size × hedge_ratio_inverse (Long = 100%, Short = hedge_ratio).
    # Wenn ctx.hedge_ratio z.B. 0.8 ist, dann ist Long = Short / 0.8.
    hedge_ratio = _effective_hedge_ratio(ctx)
    long_size = expected_short_size / hedge_ratio

    if long_size <= 0:
        log.warning(f"[REENTRY] Berechnete Long-Size zu klein ({long_size:.6f}) – Reentry übersprungen.")
        return False

    # WICHTIG: KEIN Config-Write!
    # target_long_notional ist ein absoluter Config-Wert (Start-/Rebuy-Parameter).
    # Zyklus-/Reentry-Werte bleiben nur im Runtime-State (ctx/state file), damit ein Restart keinen absoluten Drift reaktiviert.
    long_notional = long_size * last_long_tp_price
    ctx.target_long_notional = long_notional
    log.info(f"[REENTRY] ℹ️ cycle_long_notional (RAM/state): {long_notional:.2f} USDT (Config bleibt unverändert)")

    if main_order_manager is None:
        log.error("[REENTRY] ❌ Main-Account OrderManager nicht verfügbar - kann Long-Reentry nicht setzen")
        return False

    log.info("=" * 60)
    log.info(f"[REENTRY] ⚡ Long-Reentry: Direkte Market-Order")
    log.info(f"  • Long-TP-Preis: {last_long_tp_price:.6f}")
    log.info(f"  • Erwartete Short-Size (nach Burn): {expected_short_size:.6f} Coins")
    log.info(f"  • Long-Size (Ziel {100:.1f}% vs Short {hedge_ratio*100:.1f}%): {long_size:.6f} Coins")
    log.info(f"  • Long Notional: {long_notional:.2f} USDT")
    log.info("=" * 60)

    # WICHTIG: Direkte Market-Order - keine Stop-Order mehr!
    market_order = main_order_manager.open_long_market(symbol, long_size)
    
    if market_order:
        log.info(f"[REENTRY] ✅ Long Market-Order erfolgreich gesetzt: {long_size:.6f} Coins")
        log.info(f"[REENTRY] ℹ️ TP/SL wird automatisch über handle_position_update() gesetzt, wenn Position eröffnet wird")
        # WICHTIG: Stelle sicher, dass die Position-Update-Logik nicht blockiert wird
        # Setze last_processed_position zurück, damit die neue Position erkannt wird
        ctx.last_processed_position = {}
        log.info(f"[REENTRY] 🔄 Position-Tracking zurückgesetzt, damit neue Position erkannt wird")
        return True
    else:
        log.error("[REENTRY] ❌ Long Market-Order fehlgeschlagen")
        return False

