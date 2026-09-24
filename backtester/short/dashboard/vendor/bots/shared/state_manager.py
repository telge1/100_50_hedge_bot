"""
State Management Module

This module provides functions for loading and saving bot state.
Currently, the functions are still in the bot files, but this module will serve as
the target location for refactoring.

TODO: Move the following functions here:
- load_bot_state(state_file: str, bot_type: str, ...) -> dict
- save_bot_state(state_file: str, bot_type: str, burn_state: dict, ...) -> None
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

ACCOUNT_SCOPE_BY_BOT_TYPE = {
    "long": "main",
    "short": "sub",
}


def _account_state_file(state_file: str, bot_type: str) -> str | None:
    """Resolve account-level state file path for a bot type."""
    account_scope = ACCOUNT_SCOPE_BY_BOT_TYPE.get(str(bot_type or "").lower())
    if not account_scope:
        return None
    state_dir = os.path.dirname(state_file) if state_file else ""
    if not state_dir:
        return None
    return os.path.join(state_dir, f"account_state_{account_scope}.json")


def _load_account_state(account_state_file: str) -> dict:
    """Load account-level burn state (safe defaults on missing/invalid)."""
    default_state = {
        "burn_count": 0,
        "total_burned": 0.0,
        "lifetime_burn_count": 0,
        "lifetime_total_burned": 0.0,
    }
    if not account_state_file or not os.path.exists(account_state_file):
        return default_state
    try:
        with open(account_state_file, "r") as f:
            data = json.load(f) or {}
        return {
            "burn_count": int(data.get("burn_count", 0) or 0),
            "total_burned": float(data.get("total_burned", 0.0) or 0.0),
            "lifetime_burn_count": int(data.get("lifetime_burn_count", data.get("burn_count", 0)) or 0),
            "lifetime_total_burned": float(
                data.get("lifetime_total_burned", data.get("total_burned", 0.0)) or 0.0
            ),
        }
    except Exception as e:
        logger.warning(f"⚠️ Fehler beim Laden des Account-States: {e} - verwende Standardwerte")
        return default_state


def _save_account_state(account_state_file: str, state: dict) -> None:
    """Persist account-level burn state."""
    if not account_state_file:
        return
    try:
        os.makedirs(os.path.dirname(account_state_file), exist_ok=True)
        with open(account_state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"⚠️ Fehler beim Speichern des Account-States {account_state_file}: {e}")


def load_bot_state(state_file: str, bot_type: str = 'short', default_state: dict = None) -> dict:
    """
    Lädt den Bot-State aus der Datei.
    
    Args:
        state_file: Pfad zur State-Datei (z.B. 'short_bot_state_SAROSUSDT.json')
        bot_type: Bot-Typ ('short' oder 'long') - bestimmt die Standard-Keys
        default_state: Standard-State, wenn die Datei nicht existiert
    
    Returns:
        dict: Bot-State
    """
    if default_state is None:
        if bot_type == 'short':
            default_state = {
                "long_tp_fill_count": 0,
                "total_long_profits": 0.0,
                "burn_count": 0,
                "total_burned": 0.0,
                "tp_level_index": 0,
                "last_processed_position": {},
                # Long-TP Pipeline (exactly-once, persistent)
                "long_tp_pipeline_active": False,
                "pipeline_long_tp_order_id": None,
                "pipeline_long_tp_filled_at": None,
                "long_reentry_executed": False,
                "burn_executed": False,
                "recalc_executed": False,
            }
        else:  # long
            default_state = {
                "short_tp_fill_count": 0,
                "total_short_profits": 0.0,
                "burn_count": 0,
                "total_burned": 0.0,
                "tp_level_index": 0,
                "last_processed_position": {},
                # Short-TP Pipeline (exactly-once, persistent) - mirrored to short bot's long-tp pipeline
                "short_tp_pipeline_active": False,
                "pipeline_short_tp_order_id": None,
                "pipeline_short_tp_filled_at": None,
                "short_reentry_executed": False,
                "burn_executed": False,
                "recalc_executed": False,
            }
    
    try:
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state = json.load(f)
                
                # Logging abhängig vom Bot-Typ
                if bot_type == 'short':
                    logger.info(
                        f"📂 Bot-State geladen: "
                        f"long_tp_fill_count={state.get('long_tp_fill_count', 0)}, "
                        f"total_long_profits={state.get('total_long_profits', 0.0):.2f} USDT, "
                        f"burn_count={state.get('burn_count', 0)}, "
                        f"total_burned={state.get('total_burned', 0.0):.6f}"
                    )
                else:  # long
                    logger.info(
                        f"📂 Bot-State geladen: "
                        f"short_tp_fill_count={state.get('short_tp_fill_count', 0)}, "
                        f"total_short_profits={state.get('total_short_profits', 0.0):.2f} USDT, "
                        f"burn_count={state.get('burn_count', 0)}, "
                        f"total_burned={state.get('total_burned', 0.0):.6f}"
                    )
                
                # last_processed_position aus State laden (restart-sicher)
                if 'last_processed_position' in state:
                    logger.debug(f"📂 last_processed_position geladen: {state.get('last_processed_position', {})}")

                # Burn: Pro-Symbol-State (short_bot_state_SYMBOL.json) hat Priorität, damit
                # burn_count nach Restart erhalten bleibt. account_state_* nur Fallback.
                account_state_file = _account_state_file(state_file, bot_type)
                account_state = _load_account_state(account_state_file) if account_state_file else None
                if account_state:
                    state["burn_count"] = int(state.get("burn_count", account_state.get("burn_count", 0)) or 0)
                    state["total_burned"] = float(state.get("total_burned", account_state.get("total_burned", 0.0)) or 0.0)
                    state["lifetime_burn_count"] = int(state.get(
                        "lifetime_burn_count",
                        account_state.get("lifetime_burn_count", state.get("burn_count", 0)),
                    ) or 0)
                    state["lifetime_total_burned"] = float(state.get(
                        "lifetime_total_burned",
                        account_state.get("lifetime_total_burned", state.get("total_burned", 0.0)),
                    ) or 0.0)
                logger.info(
                    "[SPREAD-CONTROL] State geladen: "
                    f"cycle_index={state.get('cycle_index', 1)}, "
                    f"spread_ls={state.get('spread_ls')}, "
                    f"current_rebuy_factor={state.get('current_rebuy_factor')}, "
                    f"current_hedge_ratio={state.get('current_hedge_ratio')}, "
                    f"target_short_notional={state.get('target_short_notional')}, "
                    f"target_long_notional={state.get('target_long_notional')}"
                )
                return state
    except Exception as e:
        logger.warning(f"⚠️ Fehler beim Laden des Bot-States: {e} - verwende Standardwerte")
    
    return default_state


def save_bot_state(state_file: str, bot_type: str, burn_state: dict, 
                   burns_before_rebuy: int, last_processed_position: dict = None,
                   order_config: dict = None, context_state: dict = None) -> None:
    """
    Speichert den Bot-State in die Datei.
    
    Args:
        state_file: Pfad zur State-Datei (z.B. 'short_bot_state_SAROSUSDT.json')
        bot_type: Bot-Typ ('short' oder 'long') - bestimmt die Keys
        burn_state: Dictionary mit Burn-State-Daten
        burns_before_rebuy: Anzahl Burns vor Rebuy
        last_processed_position: Dictionary mit letzter verarbeiteter Position
        order_config: Dictionary mit aktuellen TP/SL-Preisen und -Konfigurationen für Wiederherstellung
                     Format: {
                         'long_tp_price': float,
                         'long_sl_price': float,
                         'short_tp_price': float,
                         'short_sl_price': float,
                         'long_tp_percentage': float,
                         'short_tp_percentage': float,
                         'long_position_size': float,
                         'long_position_avg': float,
                         'short_position_size': float,
                         'short_position_avg': float
                     }
    """
    try:
        if bot_type == 'short':
            state = {
                "long_tp_fill_count": burn_state.get("long_tp_fill_count", 0),
                "total_long_profits": burn_state.get("total_long_profits", 0.0),
                "burn_count": burn_state.get("burn_count", 0),
                "total_burned": burn_state.get("total_burned", 0.0),
                "tp_level_index": int(burn_state.get("tp_level_index", 0) or 0),
                # Optional / extended fields (restart-safety)
                "lifetime_burn_count": burn_state.get("lifetime_burn_count", burn_state.get("burn_count", 0)),
                "lifetime_total_burned": burn_state.get("lifetime_total_burned", burn_state.get("total_burned", 0.0)),
                "current_burn_index": burn_state.get("current_burn_index", 0),
                "active_burn_level_index": burn_state.get("active_burn_level_index"),
                "active_burn_level_price": burn_state.get("active_burn_level_price"),
                "last_long_tp_order_id": burn_state.get("last_long_tp_order_id"),
                "stage": burn_state.get("stage"),
                "burn_planned": burn_state.get("burn_planned", False),
                "planned_burn_size": burn_state.get("planned_burn_size", 0.0),
                "planned_burn_price": burn_state.get("planned_burn_price"),
                "planned_burn_profit": burn_state.get("planned_burn_profit", 0.0),
                "planned_long_profit": burn_state.get("planned_long_profit", 0.0),
                "planned_short_loss": burn_state.get("planned_short_loss", 0.0),
                "planned_target_long_size": burn_state.get("planned_target_long_size", 0.0),
                "planned_target_short_size": burn_state.get("planned_target_short_size", 0.0),
                "planned_new_short_size": burn_state.get("planned_new_short_size", 0.0),
                "planned_cycle_rebuy_factor": burn_state.get("planned_cycle_rebuy_factor"),
                "planned_cycle_hedge_ratio": burn_state.get("planned_cycle_hedge_ratio"),
                "planned_cycle_zone": burn_state.get("planned_cycle_zone"),
                "planned_cycle_profile_idx": burn_state.get("planned_cycle_profile_idx"),
                "planned_cycle_selection_mode": burn_state.get("planned_cycle_selection_mode"),
                "planned_cycle_projected_spread_pct": burn_state.get("planned_cycle_projected_spread_pct"),
                "planned_cycle_baseline_spread_pct": burn_state.get("planned_cycle_baseline_spread_pct"),
                "spread_recovery": burn_state.get("spread_recovery", {}),
                "profile_locked_for_cycle": burn_state.get("profile_locked_for_cycle", False),
                "burns_before_rebuy": burns_before_rebuy,
                "last_processed_position": last_processed_position.copy() if last_processed_position else {},
                # Long-TP Pipeline (exactly-once, persistent)
                "long_tp_pipeline_active": burn_state.get("long_tp_pipeline_active", False),
                "pipeline_long_tp_order_id": burn_state.get("pipeline_long_tp_order_id"),
                "pipeline_long_tp_filled_at": burn_state.get("pipeline_long_tp_filled_at"),
                "long_reentry_executed": burn_state.get("long_reentry_executed", False),
                "burn_executed": burn_state.get("burn_executed", False),
                "recalc_executed": burn_state.get("recalc_executed", False),
            }
            logger.debug(
                f"💾 Bot-State gespeichert: "
                f"long_tp_fill_count={state['long_tp_fill_count']}, "
                f"total_long_profits={state['total_long_profits']:.2f} USDT, "
                f"burn_count={state['burn_count']}, "
                f"total_burned={state['total_burned']:.6f}"
            )
        else:  # long
            state = {
                "short_tp_fill_count": burn_state.get("short_tp_fill_count", 0),
                "total_short_profits": burn_state.get("total_short_profits", 0.0),
                "burn_count": burn_state.get("burn_count", 0),
                "total_burned": burn_state.get("total_burned", 0.0),
                "tp_level_index": int(burn_state.get("tp_level_index", 0) or 0),
                # Optional / extended fields (restart-safety)
                "lifetime_burn_count": burn_state.get("lifetime_burn_count", burn_state.get("burn_count", 0)),
                "lifetime_total_burned": burn_state.get("lifetime_total_burned", burn_state.get("total_burned", 0.0)),
                "current_burn_index": burn_state.get("current_burn_index", 0),
                "active_burn_level_index": burn_state.get("active_burn_level_index"),
                "active_burn_level_price": burn_state.get("active_burn_level_price"),
                "last_short_tp_order_id": burn_state.get("last_short_tp_order_id"),
                "stage": burn_state.get("stage"),
                "burn_planned": burn_state.get("burn_planned", False),
                "planned_burn_size": burn_state.get("planned_burn_size", 0.0),
                "planned_burn_price": burn_state.get("planned_burn_price"),
                "planned_burn_profit": burn_state.get("planned_burn_profit", 0.0),
                "planned_short_profit": burn_state.get("planned_short_profit", 0.0),
                "planned_long_loss": burn_state.get("planned_long_loss", 0.0),
                "planned_target_short_size": burn_state.get("planned_target_short_size", 0.0),
                "planned_target_long_size": burn_state.get("planned_target_long_size", 0.0),
                "planned_new_long_size": burn_state.get("planned_new_long_size", 0.0),
                "planned_cycle_rebuy_factor": burn_state.get("planned_cycle_rebuy_factor"),
                "planned_cycle_hedge_ratio": burn_state.get("planned_cycle_hedge_ratio"),
                "planned_cycle_zone": burn_state.get("planned_cycle_zone"),
                "planned_cycle_profile_idx": burn_state.get("planned_cycle_profile_idx"),
                "planned_cycle_selection_mode": burn_state.get("planned_cycle_selection_mode"),
                "planned_cycle_projected_spread_pct": burn_state.get("planned_cycle_projected_spread_pct"),
                "planned_cycle_baseline_spread_pct": burn_state.get("planned_cycle_baseline_spread_pct"),
                "spread_recovery": burn_state.get("spread_recovery", {}),
                "profile_locked_for_cycle": burn_state.get("profile_locked_for_cycle", False),
                "burns_before_rebuy": burns_before_rebuy,
                "last_processed_position": last_processed_position.copy() if last_processed_position else {},
                # Short-TP Pipeline (exactly-once, persistent)
                "short_tp_pipeline_active": burn_state.get("short_tp_pipeline_active", False),
                "pipeline_short_tp_order_id": burn_state.get("pipeline_short_tp_order_id"),
                "pipeline_short_tp_filled_at": burn_state.get("pipeline_short_tp_filled_at"),
                "short_reentry_executed": burn_state.get("short_reentry_executed", False),
                "burn_executed": burn_state.get("burn_executed", False),
                "recalc_executed": burn_state.get("recalc_executed", False),
            }
            logger.debug(
                f"💾 Bot-State gespeichert: "
                f"short_tp_fill_count={state['short_tp_fill_count']}, "
                f"total_short_profits={state['total_short_profits']:.2f} USDT, "
                f"burn_count={state['burn_count']}, "
                f"total_burned={state['total_burned']:.6f}"
            )
        
        # Füge Order-Konfiguration hinzu (für schnelle Wiederherstellung)
        if order_config:
            state['order_config'] = order_config.copy()
            logger.debug(f"💾 Order-Konfiguration gespeichert: TP/SL-Preise für schnelle Wiederherstellung")

        if context_state:
            for key in (
                "cycle_index",
                "spread_ls",
                "current_rebuy_factor",
                "current_hedge_ratio",
                "target_short_notional",
                "target_long_notional",
            ):
                if key in context_state:
                    state[key] = context_state.get(key)
            logger.info(
                "[SPREAD-CONTROL] State speichern: "
                f"cycle_index={context_state.get('cycle_index')}, "
                f"spread_ls={context_state.get('spread_ls')}, "
                f"current_rebuy_factor={context_state.get('current_rebuy_factor')}, "
                f"current_hedge_ratio={context_state.get('current_hedge_ratio')}, "
                f"target_short_notional={context_state.get('target_short_notional')}, "
                f"target_long_notional={context_state.get('target_long_notional')}"
            )

        # Account-Source-of-Truth für Burn-Metriken (pro Account ein Wert)
        account_state_file = _account_state_file(state_file, bot_type)
        if account_state_file:
            account_state = _load_account_state(account_state_file)
            account_state["burn_count"] = int(state.get("burn_count", 0) or 0)
            account_state["total_burned"] = float(state.get("total_burned", 0.0) or 0.0)
            account_state["lifetime_burn_count"] = int(
                state.get("lifetime_burn_count", state.get("burn_count", 0)) or 0
            )
            account_state["lifetime_total_burned"] = float(
                state.get("lifetime_total_burned", state.get("total_burned", 0.0)) or 0.0
            )
            _save_account_state(account_state_file, account_state)

            # Stelle sicher, dass Bot-State denselben Account-Wert trägt
            state["burn_count"] = account_state["burn_count"]
            state["total_burned"] = account_state["total_burned"]
            state["lifetime_burn_count"] = account_state["lifetime_burn_count"]
            state["lifetime_total_burned"] = account_state["lifetime_total_burned"]
        
        with open(state_file, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"❌ Fehler beim Speichern des Bot-States: {e}")

