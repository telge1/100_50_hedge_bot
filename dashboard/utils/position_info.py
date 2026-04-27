"""
Position and order information utilities
"""
import yaml
import sys
import os
import re
import json
import math
from pathlib import Path
from datetime import datetime
import time

from utils.config_manager import load_config

# Add project root to path (für Zugriff auf bots.shared.burn_logic)
project_dir = Path(__file__).parent.parent.parent
if str(project_dir) not in sys.path:
    sys.path.insert(0, str(project_dir))

# Jetzt, nachdem project_dir im sys.path ist, kann burn_logic importiert werden
from bots.shared.burn_logic import plan_profit_burn

# Import wird verzögert, bis Working Directory gesetzt ist
# from bybit_order_manager import BybitOrderManager


def _resolve_bot_log_file(symbol: str, bot_type: str = "long") -> Path | None:
    """
    Return the most relevant log file for dashboard parsing.

    Historically the dashboard read `short_bot_<SYMBOL>.log` / `long_bot_<SYMBOL>.log`,
    but active bot instances may currently log to launcher files like
    `launcher_short_<SYMBOL>_bot_1.log`. Prefer the most recently modified match.
    """
    sym = (symbol or "").strip().upper()
    bt = (bot_type or "long").strip().lower()
    logs_dir = project_dir / "data" / "logs"
    if not sym or not logs_dir.exists():
        return None

    if bt == "short":
        patterns = [
            f"short_bot_{sym}.log",
            f"launcher_short_{sym}_*.log",
        ]
    else:
        patterns = [
            f"long_bot_{sym}.log",
            f"launcher_long_{sym}_*.log",
        ]

    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(p for p in logs_dir.glob(pattern) if p.is_file())

    if not candidates:
        return None

    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except Exception:
        return candidates[0]


def _load_live_positions_from_api(symbol: str) -> tuple[float, float, float, float] | None:
    """
    Fallback for dashboard simulations when log parsing is unavailable.

    Returns:
        tuple(long_size, long_avg, short_size, short_avg) or None
    """
    try:
        original_cwd = os.getcwd()
        os.chdir(str(project_dir))
        try:
            from bybit_order_manager import BybitOrderManager  # type: ignore

            master_config = load_master_config()
            api_key = master_config.get("api_key")
            secret_key = master_config.get("secret_key")
            if not api_key or not secret_key:
                return None

            order_manager = BybitOrderManager(api_key, secret_key)
            long_size, long_avg = order_manager.get_long_position(symbol)
            short_size, short_avg = order_manager.get_short_position(symbol)
        finally:
            os.chdir(original_cwd)
    except Exception:
        return None

    if not long_size or not long_avg or not short_size or not short_avg:
        return None

    return (
        float(long_size or 0.0),
        float(long_avg or 0.0),
        float(short_size or 0.0),
        float(short_avg or 0.0),
    )


def has_log_file_changed(symbol: str, last_mtime: float = None, bot_type: str = "long") -> tuple[bool, float]:
    """
    Prüft ob sich die Log-Datei seit dem letzten Check geändert hat.
    Returns: (has_changed: bool, current_mtime: float)
    """
    log_file = _resolve_bot_log_file(symbol, bot_type)
    if not log_file or not log_file.exists():
        return (False, 0.0)
    
    try:
        current_mtime = log_file.stat().st_mtime
        
        if last_mtime is None:
            # First check - return True to force update
            return (True, current_mtime)
        
        # Check if file has been modified
        has_changed = current_mtime > last_mtime
        
        return (has_changed, current_mtime)
    except Exception:
        return (False, last_mtime or 0.0)


def parse_burn_size_from_logs(symbol: str, bot_type: str = "long") -> dict:
    """
    Parse next burn size from bot log files.
    Looks for: "[SL-LONG] ✅ FINALE LONG-SL-SIZE: 104.530693 Coins (werden verbrannt bei SL)"
    Returns: dict with burn_size_coins and burn_size_usdt or None if not found
    """
    log_file = _resolve_bot_log_file(symbol, bot_type)
    if not log_file or not log_file.exists():
        return None
    
    try:
        # Read last 500 lines of log file
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            recent_lines = lines[-500:] if len(lines) > 500 else lines
        
        burn_size_coins = None
        short_tp_price = None
        
        # Pattern: "[SL-LONG] ✅ FINALE LONG-SL-SIZE: 104.530693 Coins (werden verbrannt bei SL)"
        burn_size_pattern = r'\[SL-LONG\] ✅ FINALE LONG-SL-SIZE: ([\d.]+) Coins'
        
        # Pattern: "[SL-LONG] Setze Long-SL bei 0.059611 (0.01% tiefer als Short-TP 0.059617), Size=104.530693 Coins"
        # Or: "[SL-LONG] Berechnung verwendet Short-TP-Preis für Burn: 0.059617"
        short_tp_pattern = r'Short-TP-Preis für Burn: ([\d.]+)'
        
        # Search from end to beginning (most recent first)
        for line in reversed(recent_lines):
            # Check for Burn Size
            burn_match = re.search(burn_size_pattern, line)
            if burn_match and burn_size_coins is None:
                burn_size_coins = float(burn_match.group(1))
            
            # Check for Short-TP Price (needed for USDT calculation)
            tp_match = re.search(short_tp_pattern, line)
            if tp_match and short_tp_price is None:
                short_tp_price = float(tp_match.group(1))
            
            # Stop if we found both
            if burn_size_coins is not None and short_tp_price is not None:
                break
        
        # If we found burn size, return it
        if burn_size_coins is not None:
            burn_size_usdt = burn_size_coins * short_tp_price if short_tp_price else None
            return {
                "burn_size_coins": burn_size_coins,
                "burn_size_usdt": burn_size_usdt,
                "fill_price": short_tp_price,
                "from_logs": True,
                "valid": True
            }
        
        return None
    except Exception as e:
        return None


def parse_position_from_logs(symbol: str, bot_type: str = "long") -> dict:
    """
    Parse position information from bot log files.
    This is much faster than API calls and contains all the data we need.
    Returns: dict with position info or None if not found
    """
    log_file = _resolve_bot_log_file(symbol, bot_type)
    if not log_file or not log_file.exists():
        return None
    
    try:
        # Read last 2000 lines of log file (should contain recent position updates and TP/SL orders)
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            # Read last 2000 lines (increased to catch TP/SL orders that might be older)
            recent_lines = lines[-2000:] if len(lines) > 2000 else lines
        
        long_size = None
        long_avg = None
        short_size = None
        short_avg = None
        long_tp_price = None
        short_tp_price = None
        long_sl_price = None
        short_sl_price = None
        current_price = None
        
        # Parse position data from logs
        # Legacy pattern: "Aktuelle Long-Position: Size=2599.4, AvgPrice=0.06786334"
        long_pattern = r'Aktuelle Long-Position: Size=([\d.]+), AvgPrice=([\d.]+)'
        short_pattern = r'Aktuelle Short-Position: Size=([\d.]+), AvgPrice=([\d.]+)'
        # Legacy pattern: "✅ Long Position erkannt: Size=25.5, Entry Price=1.40797294"
        long_detect_pattern = r'✅ Long Position erkannt: Size=([\d.]+), Entry Price=([\d.]+)'
        short_detect_pattern = r'✅ Short Position erkannt: Size=([\d.]+), Entry Price=([\d.]+)'
        # Legacy pattern: "Symbol: AXSUSDT, Size: 51.0, Entry Price: 1.3812, PositionIdx: 2"
        symbol_line_pattern = r'Symbol: ([A-Z0-9]+), Size: ([\d.]+), Entry Price: ([\d.]+), PositionIdx: (\d+)'
        # Current websocket pattern:
        # "[WS-RAW] Position Update empfangen: Symbol=DOGEUSDT, Side=Buy, Size=1058, EntryPrice=0.09501654, PositionIdx=1"
        ws_position_pattern = (
            r'Position Update empfangen: Symbol=([A-Z0-9]+), Side=(Buy|Sell).*?'
            r'Size=([\d.]+), EntryPrice=([\d.]+), PositionIdx=(\d+)'
        )
        
        # Legacy and current TP log patterns
        tp_long_pattern = (
            r'(?:Long TP Order erfolgreich gesetzt:|\[TP-LONG\] ✅ Long TP erfolgreich gesetzt:|'
            r'\[POST-SETTLE-RECALC\] ✅ Long-TP gesetzt:)\s*([\d.]+)'
        )
        tp_short_pattern = (
            r'(?:Short TP Order erfolgreich gesetzt:|\[TP-SHORT\] ✅ Short TP erfolgreich gesetzt:|'
            r'\[POST-SETTLE-RECALC\] ✅ Short-TP gesetzt:)\s*([\d.]+)'
        )
        
        # Legacy SL patterns
        sl_pattern = r'Stop-Order \((Long|Short)\).*?Trigger: ([\d.]+)'
        # Current SL confirmation lines
        sl_long_set_pattern = (
            r'(?:Long SL Order erfolgreich gesetzt:|\[POST-SETTLE-RECALC\] ✅ Long-SL gesetzt:)\s*([\d.]+)'
        )
        sl_short_set_pattern = (
            r'(?:Short SL Order erfolgreich gesetzt:|\[POST-SETTLE-RECALC\] ✅ Short-SL gesetzt:)\s*([\d.]+)'
        )
        
        # Search from end to beginning (most recent first)
        for line in reversed(recent_lines):
            # Check for Long Position
            long_match = re.search(long_pattern, line)
            if long_match and long_size is None:
                long_size = float(long_match.group(1))
                long_avg = float(long_match.group(2))
            
            # Check for Short Position
            short_match = re.search(short_pattern, line)
            if short_match and short_size is None:
                short_size = float(short_match.group(1))
                short_avg = float(short_match.group(2))

            # Check for detected Long/Short lines
            if long_size is None:
                long_detect_match = re.search(long_detect_pattern, line)
                if long_detect_match:
                    long_size = float(long_detect_match.group(1))
                    long_avg = float(long_detect_match.group(2))

            if short_size is None:
                short_detect_match = re.search(short_detect_pattern, line)
                if short_detect_match:
                    short_size = float(short_detect_match.group(1))
                    short_avg = float(short_detect_match.group(2))

            # Check for generic symbol line with PositionIdx
            if (long_size is None or short_size is None):
                symbol_match = re.search(symbol_line_pattern, line)
                if symbol_match:
                    line_symbol = symbol_match.group(1)
                    if line_symbol == symbol:
                        size_val = float(symbol_match.group(2))
                        entry_val = float(symbol_match.group(3))
                        pos_idx = int(symbol_match.group(4))
                        if pos_idx == 1 and long_size is None:
                            long_size = size_val
                            long_avg = entry_val
                        elif pos_idx == 2 and short_size is None:
                            short_size = size_val
                            short_avg = entry_val

            # Check for current websocket position lines
            ws_position_match = re.search(ws_position_pattern, line)
            if ws_position_match:
                line_symbol = ws_position_match.group(1)
                side = ws_position_match.group(2)
                size_val = float(ws_position_match.group(3))
                entry_val = float(ws_position_match.group(4))
                pos_idx = int(ws_position_match.group(5))
                if line_symbol == symbol:
                    if (side == "Buy" or pos_idx == 1) and long_size is None:
                        long_size = size_val
                        long_avg = entry_val
                    elif (side == "Sell" or pos_idx == 2) and short_size is None:
                        short_size = size_val
                        short_avg = entry_val
            
            # Check for Long TP
            tp_long_match = re.search(tp_long_pattern, line)
            if tp_long_match and long_tp_price is None:
                long_tp_price = float(tp_long_match.group(1))
            
            # Check for Short TP
            tp_short_match = re.search(tp_short_pattern, line)
            if tp_short_match and short_tp_price is None:
                short_tp_price = float(tp_short_match.group(1))
            
            # Check for SL Orders - prioritize active orders (Status: Untriggered)
            sl_match = re.search(sl_pattern, line)
            if sl_match:
                side = sl_match.group(1)
                trigger_price = float(sl_match.group(2))
                # Only use if status is Untriggered (active order)
                if "Status: Untriggered" in line:
                    if side == "Long" and long_sl_price is None:
                        long_sl_price = trigger_price
                    elif side == "Short" and short_sl_price is None:
                        short_sl_price = trigger_price

            sl_long_set_match = re.search(sl_long_set_pattern, line)
            if sl_long_set_match and long_sl_price is None:
                long_sl_price = float(sl_long_set_match.group(1))

            sl_short_set_match = re.search(sl_short_set_pattern, line)
            if sl_short_set_match and short_sl_price is None:
                short_sl_price = float(sl_short_set_match.group(1))
        
        # If we found position data, return it
        if long_size is not None or short_size is not None:
            # Calculate USDT values if we have price
            long_value_usdt = long_size * long_avg if long_size and long_avg else None
            short_value_usdt = short_size * short_avg if short_size and short_avg else None
            
            return {
                "long": {
                    "size": long_size,
                    "entry_price": long_avg,
                    "value_usdt": long_value_usdt,
                    "tp_price": long_tp_price,
                    "tp_set": long_tp_price is not None,
                    "sl_price": long_sl_price,
                    "sl_set": long_sl_price is not None
                },
                "short": {
                    "size": short_size,
                    "entry_price": short_avg,
                    "value_usdt": short_value_usdt,
                    "tp_price": short_tp_price,
                    "tp_set": short_tp_price is not None,
                    "sl_price": short_sl_price,
                    "sl_set": short_sl_price is not None
                },
                "current_price": None,  # Would need to parse from logs or API
                "from_logs": True  # Flag to indicate data came from logs
            }
        
        return None
    except Exception as e:
        # If parsing fails, return None (will fall back to API)
        return None


def simulate_tp_profit(
    symbol: str,
    bot_type: str,
    profit_pct: float | None = None,
    tp_price: float | None = None,
) -> dict | None:
    """
    Simuliert den Profit in USDT für einen gegebenen TP-Prozentwert
    (bezogen auf die jeweilige Leg-Notional) ODER einen expliziten Exit-Preis.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    p: float | None = None
    if tp_price is None:
        try:
            p = float(profit_pct) if profit_pct is not None else None
        except (TypeError, ValueError):
            return None
        if p is None or p <= 0:
            return None

    bt = (bot_type or "long").strip().lower()
    if bt not in ("long", "short"):
        return None

    # Verwende primär die Positionsdaten direkt aus den Bot-Logs.
    # Diese enthalten beide Legs (Long & Short) für den jeweiligen Bot
    # und sind damit für die Hedge-Simulation am zuverlässigsten.
    info = parse_position_from_logs(sym, bot_type=bt) or {}
    long_pos = info.get("long") or {}
    short_pos = info.get("short") or {}

    long_size = float(long_pos.get("size") or 0.0)
    long_avg = float(long_pos.get("entry_price") or 0.0)
    short_size = float(short_pos.get("size") or 0.0)
    short_avg = float(short_pos.get("entry_price") or 0.0)

    # Trunkiere Entry-Preise auf 5 Dezimalstellen, um näher an der UI-/Exchange-Anzeige zu sein
    def _trunc(x: float, decimals: int = 5) -> float:
        if x <= 0:
            return x
        factor = 10 ** decimals
        return math.trunc(x * factor) / factor

    long_avg = _trunc(long_avg, 5)
    short_avg = _trunc(short_avg, 5)

    if bt == "long":
        size = long_size
        avg = long_avg
    else:
        size = short_size
        avg = short_avg

    if size <= 0 or avg <= 0 or long_size <= 0 or long_avg <= 0 or short_size <= 0 or short_avg <= 0:
        api_positions = _load_live_positions_from_api(sym)
        if api_positions is None:
            return None
        long_size, long_avg, short_size, short_avg = api_positions
        if bt == "long":
            size = long_size
            avg = long_avg
        else:
            size = short_size
            avg = short_avg
        if size <= 0 or avg <= 0:
            return None

    notional = size * avg

    # TP-Preis (entweder direkt vorgegeben oder aus Prozent abgeleitet)
    if tp_price is not None and tp_price > 0:
        tp = float(tp_price)
    elif p is not None:
        if bt == "long":
            tp = avg * (1.0 + p / 100.0)
        else:
            tp = avg * (1.0 - p / 100.0)
    else:
        return None

    # Brutto-Gewinn/Verlust je nach Sicht
    if bt == "long":
        tp_price_eff = tp
        long_profit = max(0.0, (tp_price_eff - long_avg) * long_size)
        # Short-Verlust, wenn Short-Position existiert und bei diesem Preis im Minus wäre
        short_loss = 0.0
        if short_size > 0 and short_avg > 0 and tp_price_eff > 0:
            short_loss = max(0.0, (tp_price_eff - short_avg) * short_size)
        profit_usdt = long_profit - short_loss
    else:
        tp_price_eff = tp
        short_profit = max(0.0, (short_avg - tp_price_eff) * short_size)
        # Long-Verlust, wenn Long-Position existiert und bei diesem Preis im Minus wäre
        long_loss = 0.0
        if long_size > 0 and long_avg > 0 and tp_price_eff > 0:
            # Preis unter Long-Entry → Verlust = (Entry - Exit) * Size
            long_loss = max(0.0, (long_avg - tp_price_eff) * long_size)
        profit_usdt = short_profit - long_loss

    return {
        "symbol": sym,
        "bot_type": bt,
        "profit_pct": p if p is not None else 0.0,
        "notional_usdt": notional,
        "profit_usdt": profit_usdt,
        "entry_price": avg,
        "tp_price": tp_price_eff,
        "size_coins": size,
    }

def load_master_config():
    """Load master config for API keys"""
    project_dir = Path(__file__).parent.parent.parent
    config_file = project_dir / "config/config.yaml"
    
    with open(config_file, 'r') as f:
        return yaml.safe_load(f)


def get_burn_stats(symbol: str, bot_type: str = "long") -> dict | None:
    """
    Liest geplanten Burn-Plan aus den Bot-State-Dateien (data/state/*_bot_state_<SYMBOL>.json).
    Gibt geplanten Netto-Burn-Profit sowie Komponenten zurück.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    bt = (bot_type or "long").strip().lower()
    if bt == "short":
        state_path = project_dir / "data" / "state" / f"short_bot_state_{sym}.json"
    else:
        state_path = project_dir / "data" / "state" / f"long_bot_state_{sym}.json"

    if not state_path.exists():
        return None

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return None

    planned_profit = float(data.get("planned_burn_profit", 0.0) or 0.0)
    planned_long_profit = float(data.get("planned_long_profit", 0.0) or 0.0)
    planned_short_loss = float(data.get("planned_short_loss", 0.0) or 0.0)
    planned_burn_size = float(data.get("planned_burn_size", 0.0) or 0.0)
    burn_planned = bool(data.get("burn_planned", False))
    stage = str(data.get("stage") or "").strip()
    burn_count = int(data.get("burn_count", 0) or 0)
    burns_before_rebuy = int(data.get("burns_before_rebuy", 0) or 0)

    has_plan = burn_planned and planned_burn_size > 0

    return {
        "symbol": sym,
        "bot_type": bt,
        "has_plan": has_plan,
        "stage": stage,
        "burn_planned": burn_planned,
        "planned_burn_size": planned_burn_size,
        "planned_burn_profit": planned_profit,
        "planned_long_profit": planned_long_profit,
        "planned_short_loss": planned_short_loss,
        "burn_count": burn_count,
        "burns_before_rebuy": burns_before_rebuy,
    }


def simulate_burn_profit(
    symbol: str,
    bot_type: str,
    distance_pct: float | None = None,
    burn_price: float | None = None,
    profile: str | None = None,
) -> dict | None:
    """
    Simuliert den Burn-Profit für eine feste Distanz (distance_pct in %) auf Basis
    der aktuellen Positionen (Spread) und der Burn-Config, indem intern
    plan_profit_burn() wie im Bot verwendet wird.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    d: float | None = None
    if distance_pct is not None:
        try:
            d = float(distance_pct)
        except (TypeError, ValueError):
            return None
        if d <= 0:
            d = None

    bt = (bot_type or "long").strip().lower()
    if bt not in ("long", "short"):
        return None

    # 1) Aktuelle Positionen aus Logs
    info = parse_position_from_logs(sym, bot_type=bt) or {}
    long_pos = info.get("long") or {}
    short_pos = info.get("short") or {}
    long_size = float(long_pos.get("size") or 0.0)
    long_avg = float(long_pos.get("entry_price") or 0.0)
    short_size = float(short_pos.get("size") or 0.0)
    short_avg = float(short_pos.get("entry_price") or 0.0)

    # Fallback: Wenn Logs (z.B. direkt nach Bot-Start) noch keine gültigen Positionsdaten enthalten,
    # hole die aktuellen Positionen einmalig direkt über die Bybit-API.
    if long_size <= 0 or long_avg <= 0 or short_size <= 0 or short_avg <= 0:
        api_positions = _load_live_positions_from_api(sym)
        if api_positions is None:
            return None
        long_size, long_avg, short_size, short_avg = api_positions

    # 2) Burn-Config laden (profil-sensitiv, z.B. bot_1/bot_2)
    cfg = load_config(symbol=sym, bot_type=bt, fallback_to_global=True, profile=profile) or {}
    try:
        burn_pct = float(cfg.get("burn_pct", 0.27) or 0.27)
    except (TypeError, ValueError):
        burn_pct = 0.27
    try:
        burn_profit_pct = float(cfg.get("burn_profit_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        burn_profit_pct = 1.0

    # 3) Gewinnseite / Verlustseite + realized_profit
    # Entweder über Distanz in % (altes Verhalten) oder über expliziten Burn-Preis.
    tp_price: float | None = None
    if burn_price is not None and burn_price > 0:
        tp_price = float(burn_price)
        if bt == "short":
            # Short-Bot: Long gewinnt, Short verliert
            realized_profit = max(0.0, (tp_price - long_avg) * long_size)
            loss_price = tp_price
            position_avg = short_avg
            position_size = short_size
        else:
            # Long-Bot: Short gewinnt, Long verliert
            realized_profit = max(0.0, (short_avg - tp_price) * short_size)
            loss_price = tp_price
            position_avg = long_avg
            position_size = long_size
    else:
        if d is None:
            return None
        dist = d / 100.0
        if bt == "short":
            # Short-Bot: Long gewinnt, Short verliert
            tp_price = long_avg * (1.0 + dist)
            realized_profit = max(0.0, (tp_price - long_avg) * long_size)
            loss_price = tp_price
            position_avg = short_avg
            position_size = short_size
        else:
            # Long-Bot: Short gewinnt, Long verliert
            tp_price = short_avg * (1.0 - dist)
            realized_profit = max(0.0, (short_avg - tp_price) * short_size)
            loss_price = tp_price
            position_avg = long_avg
            position_size = long_size

    if realized_profit <= 0:
        return None

    # 4) Exakt denselben Burn-Plan wie der Bot rechnen
    burn_plan = plan_profit_burn(
        realized_profit=realized_profit,
        burn_pct=burn_pct,
        loss_price=loss_price,
        position_avg=position_avg,
        position_size=position_size,
        qty_step=0.0,   # im Dashboard keine zusätzlichen Step-Clamps
        min_qty=0.0,
        burn_profit_pct=burn_profit_pct,
    )
    if not burn_plan:
        return None

    burn_usdt_target = float(burn_plan.get("burn_usdt_target") or 0.0)
    burn_coins = float(burn_plan.get("burn_coins_clamped") or 0.0)
    loss_per_coin = float(burn_plan.get("loss_per_coin") or 0.0)

    # 5) Netto wie im Bot: Gewinnseite - Verlustseite
    if bt == "short":
        long_profit = realized_profit
        short_loss = burn_usdt_target
        net = long_profit - short_loss
        long_p = long_profit
        short_p = -short_loss
    else:
        short_profit = realized_profit
        long_loss = burn_usdt_target
        net = short_profit - long_loss
        long_p = -long_loss
        short_p = short_profit

    return {
        "symbol": sym,
        "bot_type": bt,
        "distance_pct": float(distance_pct) if distance_pct is not None else 0.0,
        "tp_price": tp_price,
        "net_profit": net,
        "long_profit": long_p,
        "short_profit": short_p,
        "burn_usdt_target": burn_usdt_target,
        "burn_coins": burn_coins,
        "loss_per_coin": loss_per_coin,
        "long_size": long_size,
        "long_avg": long_avg,
        "short_size": short_size,
        "short_avg": short_avg,
        "burn_pct": burn_pct,
        "burn_profit_pct": burn_profit_pct,
    }

def fetch_tp_sl_from_api(symbol: str, order_manager) -> dict:
    """
    Fetches TP/SL prices from Bybit API via open orders and position data.
    Returns: dict with long_tp_price, long_sl_price, short_tp_price, short_sl_price
    """
    result = {
        "long_tp_price": None,
        "long_sl_price": None,
        "short_tp_price": None,
        "short_sl_price": None
    }
    
    try:
        # First try to get from positions
        # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
        positions = order_manager.fetch_positions_direct(symbol=symbol, timeout=5)
        
        for pos in positions:
            info = pos.get('info', {})
            side = info.get('side', '')
            size = float(info.get('size', 0) or 0)
            
            if size <= 0:
                continue
            
            if side == 'Buy':  # Long position
                tp_price = info.get('takeProfit', None)
                sl_price = info.get('stopLoss', None)
                
                if tp_price:
                    try:
                        result["long_tp_price"] = float(tp_price)
                    except:
                        pass
                
                if sl_price:
                    try:
                        result["long_sl_price"] = float(sl_price)
                    except:
                        pass
            
            elif side == 'Sell':  # Short position
                tp_price = info.get('takeProfit', None)
                sl_price = info.get('stopLoss', None)
                
                if tp_price:
                    try:
                        result["short_tp_price"] = float(tp_price)
                    except:
                        pass
                
                if sl_price:
                    try:
                        result["short_sl_price"] = float(sl_price)
                    except:
                        pass
        
        # Also check open orders (more reliable for TP/SL)
        try:
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = order_manager.fetch_open_orders_direct(symbol, timeout=5)
            
            for order in open_orders:
                order_info = order.get('info', {})
                stop_order_type = order_info.get('stopOrderType', '')
                side = order_info.get('side', '')
                
                # For TP orders, use limit price if available, otherwise trigger price
                # For SL orders, use trigger price
                tp_limit_price = order_info.get('tpLimitPrice', None)
                trigger_price = order_info.get('triggerPrice', None)
                price = order.get('price', None) or order_info.get('price', None)
                
                # Determine the price to use
                if stop_order_type in ['TakeProfit', 'PartialTakeProfit']:
                    # TP orders: prefer limit price, fallback to trigger or regular price
                    order_price = None
                    if tp_limit_price:
                        try:
                            order_price = float(tp_limit_price)
                        except:
                            pass
                    if not order_price and price:
                        try:
                            order_price = float(price)
                        except:
                            pass
                    if not order_price and trigger_price:
                        try:
                            order_price = float(trigger_price)
                        except:
                            pass
                else:
                    # SL orders: use trigger price
                    order_price = None
                    if trigger_price:
                        try:
                            order_price = float(trigger_price)
                        except:
                            pass
                
                if not order_price:
                    continue
                
                # Long TP/SL (Sell side closes long)
                if side == 'Sell' and stop_order_type in ['TakeProfit', 'PartialTakeProfit']:
                    if not result["long_tp_price"]:
                        result["long_tp_price"] = order_price
                elif side == 'Sell' and stop_order_type in ['StopLoss', 'PartialStopLoss']:
                    if not result["long_sl_price"]:
                        result["long_sl_price"] = order_price
                # Note: Long-SL for burn is classified as PartialTakeProfit with side="Buy"
                elif side == 'Buy' and stop_order_type == 'PartialTakeProfit':
                    # This could be a Long-SL for burn, check if it's lower than entry price
                    # For now, we'll treat it as SL if no SL is set yet
                    if not result["long_sl_price"]:
                        # Get long entry price to verify it's a SL (should be lower)
                        # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                        positions_check = order_manager.fetch_positions_direct(symbol=symbol, timeout=5)
                        for pos_check in positions_check:
                            info_check = pos_check.get('info', {})
                            if info_check.get('side') == 'Buy':
                                entry_price = info_check.get('entryPrice') or info_check.get('avgPrice')
                                if entry_price:
                                    try:
                                        entry_price = float(entry_price)
                                        if order_price < entry_price:  # SL should be below entry
                                            result["long_sl_price"] = order_price
                                    except:
                                        pass
                
                # Short TP/SL (Buy side closes short)
                if side == 'Buy' and stop_order_type in ['TakeProfit', 'PartialTakeProfit']:
                    if not result["short_tp_price"]:
                        result["short_tp_price"] = order_price
                elif side == 'Buy' and stop_order_type in ['StopLoss', 'PartialStopLoss']:
                    if not result["short_sl_price"]:
                        result["short_sl_price"] = order_price
        except Exception as e:
            # If we can't get orders, continue without them
            pass
    except Exception as e:
        # If we can't get TP/SL, return empty result
        pass
    
    return result


def get_position_info(symbol: str, bot_type: str = "long") -> dict:
    """Get current position and order information for a symbol"""
    # Return empty data if there's an error to prevent dashboard timeout
    default_return = {
        "long": {"size": None, "entry_price": None, "value_usdt": None, "tp_price": None, "tp_set": False, "sl_price": None, "sl_set": False},
        "short": {"size": None, "entry_price": None, "value_usdt": None, "tp_price": None, "tp_set": False, "sl_price": None, "sl_set": False},
        "current_price": None
    }
    
    # FIRST: Try to get position data from logs (much faster than API!)
    log_data = parse_position_from_logs(symbol, bot_type=bot_type)
    if log_data:
        # Check if TP/SL are missing from log data
        long_missing_tp = log_data.get("long", {}).get("size") and not log_data.get("long", {}).get("tp_set")
        long_missing_sl = log_data.get("long", {}).get("size") and not log_data.get("long", {}).get("sl_set")
        short_missing_tp = log_data.get("short", {}).get("size") and not log_data.get("short", {}).get("tp_set")
        short_missing_sl = log_data.get("short", {}).get("size") and not log_data.get("short", {}).get("sl_set")
        
        # If TP/SL are missing, fetch them from API
        if long_missing_tp or long_missing_sl or short_missing_tp or short_missing_sl:
            try:
                project_dir = Path(__file__).parent.parent.parent
                original_cwd = os.getcwd()
                
                # Change to project directory so bybit_order_manager can find config.yaml
                os.chdir(str(project_dir))
                
                try:
                    # Import BybitOrderManager AFTER changing directory
                    from bybit_order_manager import BybitOrderManager
                    
                    # Load API keys
                    master_config = load_master_config()
                    api_key = master_config['api_key']
                    secret_key = master_config['secret_key']
                    
                    # Create order manager
                    order_manager = BybitOrderManager(api_key, secret_key)
                    
                    # Fetch TP/SL from API
                    tp_sl_data = fetch_tp_sl_from_api(symbol, order_manager)
                    
                    # Merge TP/SL data into log data
                    if tp_sl_data.get("long_tp_price") and long_missing_tp:
                        log_data["long"]["tp_price"] = tp_sl_data["long_tp_price"]
                        log_data["long"]["tp_set"] = True
                    
                    if tp_sl_data.get("long_sl_price") and long_missing_sl:
                        log_data["long"]["sl_price"] = tp_sl_data["long_sl_price"]
                        log_data["long"]["sl_set"] = True
                    
                    if tp_sl_data.get("short_tp_price") and short_missing_tp:
                        log_data["short"]["tp_price"] = tp_sl_data["short_tp_price"]
                        log_data["short"]["tp_set"] = True
                    
                    if tp_sl_data.get("short_sl_price") and short_missing_sl:
                        log_data["short"]["sl_price"] = tp_sl_data["short_sl_price"]
                        log_data["short"]["sl_set"] = True
                    
                    # Also get current price if missing
                    if not log_data.get("current_price"):
                        try:
                            log_data["current_price"] = order_manager.get_current_price(symbol)
                        except:
                            pass
                finally:
                    # Restore original working directory
                    os.chdir(original_cwd)
            except Exception as e:
                # If API fetch fails, continue with log data as-is
                pass
        
        return log_data
    
    # FALLBACK: If logs don't have data, use API (slower but more complete)
    try:
        project_dir = Path(__file__).parent.parent.parent
        original_cwd = os.getcwd()
        
        # Change to project directory so bybit_order_manager can find config.yaml
        os.chdir(str(project_dir))
        
        try:
            # Import BybitOrderManager AFTER changing directory
            from bybit_order_manager import BybitOrderManager
            
            # Load API keys
            master_config = load_master_config()
            api_key = master_config['api_key']
            secret_key = master_config['secret_key']
            
            # Create order manager
            order_manager = BybitOrderManager(api_key, secret_key)
            
            # Get positions with full data from exchange
            # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            positions = order_manager.fetch_positions_direct(symbol=symbol, timeout=5)
            
            long_size = None
            long_avg = None
            long_value_usdt = None
            short_size = None
            short_avg = None
            short_value_usdt = None
            current_price = None
            
            # Extract position data and values from exchange
            for pos in positions:
                info = pos.get('info', {})
                side = info.get('side', '')
                size = float(info.get('size', 0) or 0)
                
                if size <= 0:
                    continue
                
                # Get position value in USDT directly from exchange
                position_value = info.get('positionValue', None) or info.get('notional', None)
                if position_value:
                    try:
                        position_value_usdt = float(position_value)
                    except:
                        position_value_usdt = None
                else:
                    position_value_usdt = None
                
                avg_price = info.get('entryPrice') or info.get('avgPrice')
                if avg_price:
                    try:
                        avg_price = float(avg_price)
                    except:
                        avg_price = None
                
                if side == 'Buy':  # Long
                    long_size = size
                    long_avg = avg_price
                    long_value_usdt = position_value_usdt
                elif side == 'Sell':  # Short
                    short_size = size
                    short_avg = avg_price
                    short_value_usdt = position_value_usdt
            
            # Get current price
            current_price = order_manager.get_current_price(symbol)
            
            # Fallback: Calculate if exchange doesn't provide value
            if long_size and long_avg and not long_value_usdt and current_price:
                long_value_usdt = long_size * current_price
            if short_size and short_avg and not short_value_usdt and current_price:
                short_value_usdt = short_size * current_price
            
            # Get TP/SL orders using helper function
            tp_sl_data = fetch_tp_sl_from_api(symbol, order_manager)
            long_tp_price = tp_sl_data.get("long_tp_price")
            long_sl_price = tp_sl_data.get("long_sl_price")
            short_tp_price = tp_sl_data.get("short_tp_price")
            short_sl_price = tp_sl_data.get("short_sl_price")
            
            # Position values already extracted from exchange data above
            
            result = {
                "long": {
                    "size": long_size,
                    "entry_price": long_avg,
                    "value_usdt": long_value_usdt,
                    "tp_price": long_tp_price,
                    "tp_set": long_tp_price is not None,
                    "sl_price": long_sl_price,
                    "sl_set": long_sl_price is not None
                },
                "short": {
                    "size": short_size,
                    "entry_price": short_avg,
                    "value_usdt": short_value_usdt,
                    "tp_price": short_tp_price,
                    "tp_set": short_tp_price is not None,
                    "sl_price": short_sl_price,
                    "sl_set": short_sl_price is not None
                },
                "current_price": current_price
            }
            
            return result
        finally:
            # Restore original working directory
            os.chdir(original_cwd)
    except Exception as e:
        # Return empty data on error to prevent dashboard timeout
        return default_return


def calculate_next_burn_size(symbol: str) -> dict:
    """
    Berechnet die nächste Burn-Size in Coins und USDT.
    Verwendet die gleiche Berechnungslogik wie set_short_tp_at_percentage() in hedge_bot.py
    
    Returns:
        {
            "burn_size_coins": float,      # Anzahl Coins die verbrannt werden
            "burn_size_usdt": float,        # Dollar-Wert der Coins zum Fill-Preis
            "fill_price": float,            # Short-TP-Preis (Fill-Preis)
            "short_profit_usdt": float,     # Short-Profit in USDT
            "valid": bool                   # Ob Berechnung gültig ist
        }
    """
    default_return = {
        "burn_size_coins": None,
        "burn_size_usdt": None,
        "fill_price": None,
        "short_profit_usdt": None,
        "valid": False
    }
    
    try:
        project_dir = Path(__file__).parent.parent.parent
        original_cwd = os.getcwd()
        
        # Change to project directory so bybit_order_manager can find config.yaml
        os.chdir(str(project_dir))
        
        try:
            # Import BybitOrderManager AFTER changing directory
            from bybit_order_manager import BybitOrderManager
            
            # Load API keys
            master_config = load_master_config()
            api_key = master_config['api_key']
            secret_key = master_config['secret_key']
            
            # Create order manager
            order_manager = BybitOrderManager(api_key, secret_key)
            
            # Get positions
            long_size, long_avg = order_manager.get_long_position(symbol)
            short_size, short_avg = order_manager.get_short_position(symbol)
            
            # Check if we have required positions
            if not short_size or not short_avg:
                return default_return  # No short position = no burn
            
            if not long_size or not long_avg:
                return default_return  # No long position = no burn
            
            # Get Short-TP price (fill price for next burn)
            # Use get_position_info but with timeout protection
            try:
                position_info = get_position_info(symbol)
                short_tp_price = position_info.get("short", {}).get("tp_price")
            except Exception as e:
                # If get_position_info fails, return default
                return default_return
            
            if not short_tp_price:
                return default_return  # No Short-TP set = no burn
            
            # Calculate Short-Profit in USDT (same as in set_short_tp_at_percentage, line 628)
            short_profit_usdt = (short_avg - short_tp_price) * short_size
            
            if short_profit_usdt <= 0:
                return default_return  # Invalid profit
            
            # Calculate Loss per Long Coin (same as in set_short_tp_at_percentage, line 700)
            # burn_price = tp_price (the price at which burn happens)
            burn_price = short_tp_price
            loss_per_long = long_avg - burn_price
            
            if loss_per_long <= 0:
                return default_return  # Invalid loss per long coin
            
            # Calculate Burn-Size in Coins (same as in set_short_tp_at_percentage, line 706)
            burn_size_coins = short_profit_usdt / loss_per_long
            
            # Validate: Burn-Size should not exceed Long-Size
            if burn_size_coins > long_size:
                # Limit to 90% of Long-Size (same as in hedge_bot.py line 729)
                burn_size_coins = long_size * 0.9
            
            # Calculate Burn-Size in USDT (at fill price)
            burn_size_usdt = burn_size_coins * short_tp_price
            
            return {
                "burn_size_coins": burn_size_coins,
                "burn_size_usdt": burn_size_usdt,
                "fill_price": short_tp_price,
                "short_profit_usdt": short_profit_usdt,
                "valid": True
            }
            
        finally:
            # Restore original working directory
            os.chdir(original_cwd)
    except Exception as e:
        # Return empty data on error to prevent dashboard timeout
        return default_return

def calculate_next_burn_size_from_position(symbol: str, position_info: dict) -> dict:
    """
    Berechnet die nächste Burn-Size in Coins und USDT basierend auf bereits geladener Position-Info.
    Schnellere Version, die keine zusätzlichen API-Calls macht.
    
    Args:
        symbol: Trading symbol
        position_info: Bereits geladene Position-Info von get_position_info()
    
    Returns:
        {
            "burn_size_coins": float,
            "burn_size_usdt": float,
            "fill_price": float,
            "short_profit_usdt": float,
            "valid": bool
        }
    """
    default_return = {
        "burn_size_coins": None,
        "burn_size_usdt": None,
        "fill_price": None,
        "short_profit_usdt": None,
        "valid": False
    }
    
    # Validate position_info
    if not position_info or not isinstance(position_info, dict):
        return default_return
    
    # FIRST: Try to get burn size from logs (much faster!)
    log_burn_data = parse_burn_size_from_logs(symbol)
    if log_burn_data and log_burn_data.get("valid"):
        # We have burn data from logs - return it immediately!
        return {
            "burn_size_coins": log_burn_data.get("burn_size_coins"),
            "burn_size_usdt": log_burn_data.get("burn_size_usdt"),
            "fill_price": log_burn_data.get("fill_price"),
            "short_profit_usdt": None,  # Not in logs, but not critical
            "valid": True,
            "from_logs": True  # Flag to indicate data came from logs
        }
    
    # FALLBACK: Calculate from position info (slower but more complete)
    try:
        # Get position data from already loaded info
        long_data = position_info.get("long", {})
        short_data = position_info.get("short", {})
        
        long_size = long_data.get("size")
        long_avg = long_data.get("entry_price")
        short_size = short_data.get("size")
        short_avg = short_data.get("entry_price")
        short_tp_price = short_data.get("tp_price")
        
        # Check if we have required data and convert to float if needed
        try:
            if short_size is None or short_avg is None or short_tp_price is None:
                return default_return  # No short position or TP = no burn
            
            if long_size is None or long_avg is None:
                return default_return  # No long position = no burn
            
            # Convert to float to ensure numeric types
            short_size = float(short_size)
            short_avg = float(short_avg)
            short_tp_price = float(short_tp_price)
            long_size = float(long_size)
            long_avg = float(long_avg)
            
            # Validate that all values are positive numbers
            if short_size <= 0 or short_avg <= 0 or short_tp_price <= 0:
                return default_return
            
            if long_size <= 0 or long_avg <= 0:
                return default_return
        except (ValueError, TypeError):
            return default_return  # Invalid data types
        
        # Calculate Short-Profit in USDT (same as in set_short_tp_at_percentage, line 628)
        short_profit_usdt = (short_avg - short_tp_price) * short_size
        
        if short_profit_usdt <= 0:
            return default_return  # Invalid profit
        
        # Calculate Loss per Long Coin (same as in set_short_tp_at_percentage, line 700)
        burn_price = short_tp_price
        loss_per_long = long_avg - burn_price
        
        if loss_per_long <= 0:
            return default_return  # Invalid loss per long coin
        
        # Calculate Burn-Size in Coins (same as in set_short_tp_at_percentage, line 706)
        burn_size_coins = short_profit_usdt / loss_per_long
        
        # Validate: Burn-Size should not exceed Long-Size
        if burn_size_coins > long_size:
            # Limit to 90% of Long-Size (same as in hedge_bot.py line 729)
            burn_size_coins = long_size * 0.9
        
        # Calculate Burn-Size in USDT (at fill price)
        burn_size_usdt = burn_size_coins * short_tp_price
        
        return {
            "burn_size_coins": burn_size_coins,
            "burn_size_usdt": burn_size_usdt,
            "fill_price": short_tp_price,
            "short_profit_usdt": short_profit_usdt,
            "valid": True
        }
    except Exception as e:
        # Return empty data on error
        return default_return


def calculate_rebuy_info(symbol: str, position_info: dict, bot_state: dict = None) -> dict:
    """
    Berechnet die Rebuy-Informationen basierend auf der neuen Notional-basierten Logik.
    
    Args:
        symbol: Trading symbol
        position_info: Bereits geladene Position-Info von get_position_info()
        bot_state: Optional bot state dict (für burn_count prüfung)
    
    Returns:
        {
            "rebuy_size_coins": float,
            "rebuy_size_usdt": float,
            "target_notional": float,
            "current_notional": float,
            "rebuy_entry_price": float,
            "new_long_avg": float,
            "new_long_size_usdt": float,
            "valid": bool,
            "not_needed": bool  # True wenn Long bereits über Ziel-Notional
        }
    """
    default_return = {
        "rebuy_size_coins": None,
        "rebuy_size_usdt": None,
        "target_notional": None,
        "current_notional": None,
        "rebuy_entry_price": None,
        "new_long_avg": None,
        "new_long_size_usdt": None,
        "valid": False,
        "not_needed": False
    }
    
    # Validate position_info
    if not position_info or not isinstance(position_info, dict):
        return default_return
    
    try:
        # Load config to get initial_long_usdt
        from utils.config_manager import load_config, get_default_config
        config = load_config(symbol)
        if not config:
            config = get_default_config()
        
        target_long_notional = config.get("initial_long_usdt", 500)
        
        # Get current long position
        long_data = position_info.get("long", {})
        long_size = long_data.get("size")
        long_avg = long_data.get("entry_price")
        
        # Validate and convert to float
        try:
            if long_size is None or long_avg is None:
                return default_return  # No long position = no rebuy calculation
            
            long_size = float(long_size)
            long_avg = float(long_avg)
            
            if long_size <= 0 or long_avg <= 0:
                return default_return
        except (ValueError, TypeError):
            return default_return  # Invalid data types
        
        # Get current price (use short TP as approximation, or long avg as fallback)
        # For rebuy, we need the current market price - we'll use the burn price (short TP)
        # This is the same logic as in hedge_bot.py where rebuy happens after burn
        short_data = position_info.get("short", {})
        current_price = short_data.get("tp_price")  # Burn price = Short TP price
        
        try:
            if current_price is None:
                # Fallback: use long_avg (not ideal but better than nothing)
                current_price = long_avg
            else:
                current_price = float(current_price)
            
            if current_price <= 0:
                current_price = long_avg  # Fallback to long_avg if invalid
        except (ValueError, TypeError):
            current_price = long_avg  # Fallback to long_avg if conversion fails
        
        # Calculate current long notional
        current_long_notional = long_size * current_price
        
        # Calculate required rebuy in USDT
        rebuy_usdt = target_long_notional - current_long_notional
        
        if rebuy_usdt <= 0:
            # No rebuy needed - already at or above target
            return {
                "rebuy_size_coins": 0.0,
                "rebuy_size_usdt": 0.0,
                "target_notional": target_long_notional,
                "current_notional": current_long_notional,
                "rebuy_entry_price": current_price,
                "new_long_avg": long_avg,
                "new_long_size_usdt": current_long_notional,
                "valid": True,
                "not_needed": True
            }
        
        # Calculate rebuy size in coins
        rebuy_size_coins = rebuy_usdt / current_price
        
        # Calculate new average price after rebuy (Weighted Average)
        old_cost_basis = long_size * long_avg
        rebuy_cost_basis = rebuy_size_coins * current_price
        new_total_long_size = long_size + rebuy_size_coins
        new_long_avg = (old_cost_basis + rebuy_cost_basis) / new_total_long_size if new_total_long_size > 0 else long_avg
        
        # Calculate new long size in USDT (should be target notional)
        new_long_size_usdt = new_total_long_size * current_price
        
        return {
            "rebuy_size_coins": rebuy_size_coins,
            "rebuy_size_usdt": rebuy_usdt,
            "target_notional": target_long_notional,
            "current_notional": current_long_notional,
            "rebuy_entry_price": current_price,
            "new_long_avg": new_long_avg,
            "new_long_size_usdt": new_long_size_usdt,
            "valid": True,
            "not_needed": False
        }
    except Exception as e:
        # Return empty data on error
        return default_return
