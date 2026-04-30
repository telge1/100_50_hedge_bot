"""
TP-Fill Detection Module für Hedge Bots

Dieses Modul enthält Funktionen zur präzisen Erkennung von TP-Fills im Gegensatz
zu manuellen Cancels/Verschiebungen.

WICHTIGER MERKSATZ:
"Deactivated ist KEIN Event. Kontext entscheidet, ob es ein Fill oder ein Cancel ist."

Dieses Modul wird verwendet, um zwischen:
- TP-Fill (Order wurde gefüllt) → Burn-Logik + Stop-Order setzen
- Manueller Cancel/Verschiebung → Repair/Re-Open-Logik NICHT ausführen
"""

import logging

logger = logging.getLogger('TPFillDetection')


def should_ignore_cancel_info(
    order_status: str,
    cancel_type: str,
    reject_reason: str,
) -> bool:
    """
    Filtert Bybit Cancel-Info-Messages nach Fill.
    
    Laut Bybit-Dokumentation können wir zwei Filled-Messages bekommen:
    1. Die echte: orderStatus=Filled, rejectReason=EC_NoError (oder leer)
    2. Die Cancel-Info: orderStatus=Filled, cancelType=CancelByUser, rejectReason=EC_OrigClOrdIDDoesNotExist
    
    Die Cancel-Info-Message sollte ignoriert werden.
    
    Args:
        order_status: Order-Status (z.B. "Filled")
        cancel_type: Cancel-Type (z.B. "CancelByUser")
        reject_reason: Reject-Reason (z.B. "EC_OrigClOrdIDDoesNotExist")
    
    Returns:
        bool: True wenn Cancel-Info-Message (sollte ignoriert werden), False sonst
    """
    return (
        order_status == "Filled"
        and cancel_type == "CancelByUser"
        and reject_reason == "EC_OrigClOrdIDDoesNotExist"
    )


def check_order_exists_on_exchange(
    order_manager,
    symbol: str,
    order_id: str,
) -> bool | None:
    """
    Prüft, ob eine Order noch auf der Exchange existiert.
    
    Args:
        order_manager: OrderManager-Instanz mit exchange.fetch_open_orders()
        symbol: Trading-Symbol (z.B. "XPINUSDT")
        order_id: Order-ID zum Prüfen
    
    Returns:
        bool: True wenn Order existiert, False wenn nicht
        None: Konnte nicht geprüft werden (Fehler)
    """
    try:
        # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
        open_orders = order_manager.fetch_open_orders_direct(symbol, timeout=5)
        for order in open_orders:
            # Bybit kann Order-ID in verschiedenen Feldern speichern
            order_id_from_exchange = (
                order.get('id') or 
                order.get('info', {}).get('orderId') or
                order.get('info', {}).get('orderID')
            )
            if order_id_from_exchange == order_id:
                return True
        return False  # Order nicht gefunden → existiert nicht mehr
    except Exception as e:
        logger.warning(f"[TP-FILL-DETECT] Fehler bei Order-Prüfung: {e}")
        return None


def check_position_reduced_after_tp(
    order_manager,
    symbol: str,
    position_idx: int,
    expected_size_before_tp: float | None = None,
) -> bool | None:
    """
    Prüft, ob eine Position nach einem TP-Fill reduziert wurde.
    
    Args:
        order_manager: OrderManager-Instanz mit get_long_position() oder get_short_position()
        symbol: Trading-Symbol (z.B. "XPINUSDT")
        position_idx: Position-Index (1 für Long, 2 für Short)
        expected_size_before_tp: Erwartete Position-Size vor TP (optional, für Vergleich)
    
    Returns:
        bool: True wenn Position reduziert/geschlossen, False wenn unverändert
        None: Konnte nicht geprüft werden (Fehler)
    """
    try:
        if position_idx == 1:  # Long
            current_size, _ = order_manager.get_long_position(symbol)
        elif position_idx == 2:  # Short
            current_size, _ = order_manager.get_short_position(symbol)
        else:
            logger.warning(f"[TP-FILL-DETECT] Unbekannter position_idx: {position_idx}")
            return None
        
        if current_size is None or current_size <= 0:
            return True  # Position geschlossen → definitiv Fill
        
        if expected_size_before_tp is not None:
            # Prüfe ob Position reduziert wurde (mit kleiner Toleranz für Rundungsfehler)
            tolerance = expected_size_before_tp * 0.001  # 0.1% Toleranz
            if current_size < (expected_size_before_tp - tolerance):
                return True  # Reduziert → wahrscheinlich Fill
        
        return False  # Unverändert → wahrscheinlich kein Fill
    except Exception as e:
        logger.warning(f"[TP-FILL-DETECT] Fehler bei Position-Prüfung: {e}")
        return None


def is_tp_fill(
    order_status: str,
    cancel_type: str,
    reject_reason: str,
    *,
    order_exists_on_exchange: bool | None = None,
    position_reduced: bool | None = None,
) -> bool:
    """
    Prüft, ob eine Order-Update ein TP-Fill ist (nicht manueller Cancel/Verschiebung).
    
    WICHTIGER MERKSATZ:
    "Deactivated ist KEIN Event. Kontext entscheidet, ob es ein Fill oder ein Cancel ist."
    
    Args:
        order_status: Order-Status (z.B. "Filled", "Deactivated")
        cancel_type: Cancel-Type (z.B. "CancelByUser", "UNKNOWN", None, "")
        reject_reason: Reject-Reason (z.B. "EC_NoError", "EC_OrigClOrdIDDoesNotExist")
        order_exists_on_exchange: None=unbekannt, True=existiert, False=existiert nicht
        position_reduced: None=unbekannt, True=reduziert, False=unverändert
    
    Returns:
        bool: True wenn TP-Fill, False wenn nicht (Cancel/Verschiebung/Unbekannt)
    """
    # FALL A: Klarer Fill (orderStatus=Filled)
    if order_status == "Filled":
        # Cancel-Info-Message erkennen (zweite Message nach echtem Fill)
        if (
            cancel_type == "CancelByUser"
            and reject_reason == "EC_OrigClOrdIDDoesNotExist"
        ):
            return False  # nur Cancel-Info, kein echter Fill
        return True  # echter Fill
    
    # FALL B: Deactivated (Bybit sendet manchmal Deactivated nach Fill)
    if order_status == "Deactivated":
        # Wenn CancelByUser → definitiv manueller Cancel/Verschiebung
        if cancel_type == "CancelByUser":
            return False
        
        # ✅ VERBESSERUNG 1: Robuste cancel_type-Prüfung
        # Wenn cancel_type leer/None/UNKNOWN/N/A → braucht Kontext
        if cancel_type in (None, "", "UNKNOWN", "N/A"):
            # ✅ VERBESSERUNG 2: position_reduced ZUERST prüfen (direkter Indikator)
            if position_reduced is True:
                return True  # Position reduziert → Fill sehr wahrscheinlich
            
            if order_exists_on_exchange is False:
                return True  # Order existiert nicht mehr → Fill sehr wahrscheinlich
            
            # Ohne Kontext → kein Fill (sicherer Ansatz)
            return False
        
        # Andere cancelType-Werte → kein Fill
        return False
    
    # Alle anderen Status → kein Fill
    return False


def detect_tp_fill_in_order_update(
    ctx,
    order_status,
    stop_order_type,
    order_type,
    side,
    position_idx,
    reduce_only,
    cancel_type,
    reject_reason,
    order_id,
    symbol,
    order_manager,
    main_order_manager,
    sub_order_manager,
    bot_type,
    logger_instance=None
):
    """
    Erkennt TP-Fills in Order-Updates für beide Bots.
    
    Diese Funktion:
    1. Prüft ob es eine TP-Order ist
    2. Prüft Kontext für Deactivated-Status
    3. Erkennt TP-Fill mit is_tp_fill()
    4. Setzt tp_reset_in_progress Flag zurück
    
    Args:
        ctx: BotContext-Instanz
        order_status: Order-Status
        stop_order_type: Stop-Order-Typ
        order_type: Order-Typ
        side: Order-Side
        position_idx: Position-Index
        reduce_only: Reduce-Only Flag
        cancel_type: Cancel-Type
        reject_reason: Reject-Reason
        order_id: Order-ID
        symbol: Trading-Symbol
        order_manager: Primärer OrderManager
        main_order_manager: Main-Account OrderManager (kann None sein)
        sub_order_manager: Sub-Account OrderManager (kann None sein)
        bot_type: 'long' oder 'short'
        logger_instance: Optional logger (falls None, verwendet Modul-Logger)
    
    Returns:
        bool: True wenn TP-Fill erkannt, False sonst
    """
    log = logger_instance if logger_instance else logger
    
    # Initialisiere Flag für TP-Fill-Erkennung
    tp_fill_detected = False
    
    # Prüfe nur TP-Orders (nicht SL-Orders)
    # Long-Bot: Prüfe Long-TP (position_idx=1) und Short-TP (position_idx=2)
    # Short-Bot: Prüfe Long-TP (position_idx=1) und Short-TP (position_idx=2)
    is_tp_order_check = is_tp_order(stop_order_type, order_type, side, position_idx, reduce_only, 1, bot_type) or \
                        is_tp_order(stop_order_type, order_type, side, position_idx, reduce_only, 2, bot_type)
    
    if not is_tp_order_check:
        return False
    
    # Prüfe Kontext für Deactivated-Status (nur wenn Deactivated)
    order_exists = None
    pos_reduced = None
    
    if order_status == "Deactivated":
        # Nur prüfen wenn Deactivated (bei Filled ist es klar)
        try:
            # Bestimme richtigen OrderManager basierend auf position_idx und bot_type
            if bot_type == 'long':
                # Long Bot: Long-TP auf Main-Account, Short-TP auf Sub-Account
                if position_idx == 1 or (position_idx is None and side == "Sell"):
                    # Long-TP: Main-Account
                    target_order_manager = order_manager
                else:
                    # Short-TP: Sub-Account
                    target_order_manager = sub_order_manager if sub_order_manager else order_manager
            else:  # short
                # Short Bot: Short-TP auf Sub-Account (order_manager), Long-TP auf Main-Account
                if position_idx == 1 or (position_idx is None and side == "Sell"):
                    # Long-TP: Main-Account
                    target_order_manager = main_order_manager if main_order_manager else order_manager
                else:
                    # Short-TP: Sub-Account (order_manager ist primärer OrderManager)
                    target_order_manager = order_manager
            
            order_exists = check_order_exists_on_exchange(
                target_order_manager,
                symbol,
                order_id
            )
            # Position-Check (optional, kann später ergänzt werden wenn nötig)
            # pos_reduced = check_position_reduced_after_tp(...)
        except Exception as e:
            log.debug(f"[ORDER-UPDATE] Kontext-Check fehlgeschlagen: {e}")
    
    # Prüfe TP-Fill mit neuer Erkennungslogik
    if is_tp_fill(
        order_status,
        cancel_type,
        reject_reason,
        order_exists_on_exchange=order_exists,
        position_reduced=pos_reduced,
    ):
        log.info("[ORDER-UPDATE] 🔥 TP-FILL erkannt (neue Erkennung) – starte bestehende Fill-Logik")
        
        # ⛔ WICHTIG: Verhindere, dass manuelle Verschiebung danach greift
        ctx.tp_reset_in_progress = False
        
        # Bestehende Fill-Logik wird weiter unten aufgerufen
        # Diese wird durch die bestehenden Checks getriggert (order_status == "Filled" etc.)
        # Wir müssen hier nur sicherstellen, dass die manuelle Verschiebung NICHT greift
        
        # Setze Flag, damit manuelle Verschiebung übersprungen wird
        tp_fill_detected = True
    
    return tp_fill_detected

