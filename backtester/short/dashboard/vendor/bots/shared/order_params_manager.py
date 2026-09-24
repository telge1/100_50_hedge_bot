"""
Order Parameters Manager

Dieses Modul verwaltet die Speicherung und das Laden von Order-Parametern
für schnelle Wiederherstellung bei manuellem Cancel.
"""

import os
import json
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def get_order_params_file(symbol: str, bot_type: str) -> str:
    """
    Gibt den Pfad zur Order-Parameter-Datei zurück.
    
    Args:
        symbol: Handelssymbol (z.B. "HUSDT")
        bot_type: Bot-Typ ('long' oder 'short')
    
    Returns:
        str: Pfad zur JSON-Datei
    """
    return f"bots/order_params_{bot_type}_{symbol}.json"


def save_order_params(symbol: str, bot_type: str, order_params: Dict[str, Any]) -> bool:
    """
    Speichert Order-Parameter in eine JSON-Datei für schnelle Wiederherstellung.
    
    Args:
        symbol: Handelssymbol (z.B. "HUSDT")
        bot_type: Bot-Typ ('long' oder 'short')
        order_params: Dictionary mit Order-Parametern
                     Format für Long-Bot:
                     {
                         'short_tp_price': float,
                         'short_tp_size': float,
                         'long_sl_price': float,
                         'long_sl_size': float,
                         'timestamp': float
                     }
                     Format für Short-Bot:
                     {
                         'long_tp_price': float,
                         'long_tp_size': float,
                         'short_sl_price': float,
                         'short_sl_size': float,
                         'timestamp': float
                     }
    
    Returns:
        bool: True wenn erfolgreich, False sonst
    """
    try:
        import time
        order_params_file = get_order_params_file(symbol, bot_type)
        
        # Füge Timestamp hinzu
        params_with_timestamp = order_params.copy()
        params_with_timestamp['timestamp'] = time.time()
        
        # Erstelle Verzeichnis falls nicht vorhanden
        os.makedirs(os.path.dirname(order_params_file) if os.path.dirname(order_params_file) else '.', exist_ok=True)
        
        with open(order_params_file, 'w') as f:
            json.dump(params_with_timestamp, f, indent=2)
        
        logger.info(f"💾 Order-Parameter gespeichert: {order_params_file}")
        return True
    except Exception as e:
        logger.error(f"❌ Fehler beim Speichern der Order-Parameter: {e}", exc_info=True)
        return False


def load_order_params(symbol: str, bot_type: str) -> Optional[Dict[str, Any]]:
    """
    Lädt Order-Parameter aus einer JSON-Datei.
    
    Args:
        symbol: Handelssymbol (z.B. "HUSDT")
        bot_type: Bot-Typ ('long' oder 'short')
    
    Returns:
        Optional[Dict[str, Any]]: Order-Parameter oder None wenn nicht gefunden
    """
    try:
        order_params_file = get_order_params_file(symbol, bot_type)
        
        if not os.path.exists(order_params_file):
            logger.debug(f"📂 Order-Parameter-Datei nicht gefunden: {order_params_file}")
            return None
        
        with open(order_params_file, 'r') as f:
            params = json.load(f)
        
        logger.info(f"📂 Order-Parameter geladen: {order_params_file}")
        return params
    except Exception as e:
        logger.warning(f"⚠️ Fehler beim Laden der Order-Parameter: {e}")
        return None


def update_order_param(symbol: str, bot_type: str, param_key: str, param_value: Any) -> bool:
    """
    Aktualisiert einen einzelnen Order-Parameter in der JSON-Datei.
    
    Args:
        symbol: Handelssymbol (z.B. "HUSDT")
        bot_type: Bot-Typ ('long' oder 'short')
        param_key: Key des Parameters (z.B. 'short_tp_price')
        param_value: Neuer Wert
    
    Returns:
        bool: True wenn erfolgreich, False sonst
    """
    try:
        import time
        order_params_file = get_order_params_file(symbol, bot_type)
        
        # Lade bestehende Parameter
        params = load_order_params(symbol, bot_type) or {}
        
        # Aktualisiere Parameter
        params[param_key] = param_value
        params['timestamp'] = time.time()
        
        # Speichere zurück
        os.makedirs(os.path.dirname(order_params_file) if os.path.dirname(order_params_file) else '.', exist_ok=True)
        with open(order_params_file, 'w') as f:
            json.dump(params, f, indent=2)
        
        logger.debug(f"💾 Order-Parameter aktualisiert: {param_key} = {param_value}")
        return True
    except Exception as e:
        logger.error(f"❌ Fehler beim Aktualisieren der Order-Parameter: {e}", exc_info=True)
        return False


def delete_order_params(symbol: str, bot_type: str) -> bool:
    """
    Löscht die Order-Parameter-Datei.
    Wird aufgerufen, wenn neue Positionen über das Dashboard geöffnet werden,
    damit beim nächsten Start wieder config.yaml verwendet wird.
    
    Args:
        symbol: Handelssymbol (z.B. "HUSDT")
        bot_type: Bot-Typ ('long' oder 'short')
    
    Returns:
        bool: True wenn erfolgreich gelöscht oder nicht vorhanden, False bei Fehler
    """
    try:
        order_params_file = get_order_params_file(symbol, bot_type)
        
        if os.path.exists(order_params_file):
            os.remove(order_params_file)
            logger.info(f"🗑️ Order-Parameter-Datei gelöscht (neue Positionen → verwende config.yaml): {order_params_file}")
        else:
            logger.debug(f"📂 Order-Parameter-Datei existiert nicht: {order_params_file}")
        
        return True
    except Exception as e:
        logger.error(f"❌ Fehler beim Löschen der Order-Parameter: {e}", exc_info=True)
        return False

