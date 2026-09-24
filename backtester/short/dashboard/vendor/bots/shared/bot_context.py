"""
BotContext Module für Hedge Bots

Dieses Modul enthält die BotContext-Klasse, die alle globalen Bot-State-Variablen
in einem einzigen Objekt kapselt. Dies verbessert Thread-Safety, Testbarkeit
und Wartbarkeit erheblich.
"""

import threading
import time
from typing import Optional, Dict, Any, Set
from core.ws_state import WSState
from core.ws_events import WSEvents
from bots.shared.spread_profile import resolve_rebuy_profile as shared_resolve_rebuy_profile


class BotContext:
    """
    Zentrale Kapselung aller Bot-State-Variablen.
    
    Statt vieler globaler Variablen, die von mehreren Threads (WS, Main-Loop, Init, Recovery)
    gleichzeitig verwendet werden, kapseln wir alles in einem Objekt.
    
    Dies reduziert:
    - State-Drift zwischen Threads
    - Race Conditions
    - Schwer nachvollziehbare Abhängigkeiten
    
    Vorteile:
    - Thread-Safety durch klare Ownership
    - Einfacheres Testing (ctx kann gemockt werden)
    - Bessere Debugging-Möglichkeiten
    - Einfacheres Recovery (ctx kann serialisiert werden)
    """
    
    def __init__(self, bot_type: str = 'short'):
        """
        Initialisiert den BotContext mit Standardwerten.
        
        Args:
            bot_type: 'short' oder 'long' - bestimmt welche Position-Tracking-Variablen gesetzt werden
        """
        # WebSocket State (zentral für alle Threads)
        self.ws_state = WSState()  # Position/Order/Health-Tracking
        self.ws_events = WSEvents()  # Events und Intent-Flags
        
        # Order Tracking
        self.current_short_tp_price: Optional[float] = None
        self.current_long_tp_price: Optional[float] = None
        self.current_short_sl_price: Optional[float] = None
        self.current_long_sl_price: Optional[float] = None
        self.planned_long_sl_price: Optional[float] = None  # Burn-Plan SL (noch nicht gefüllt)
        
        # State Flags
        self.bot_running: bool = True
        self.first_position_closed: bool = False
        self.cycle_active: bool = True  # Zyklus-Flag: False wenn Long-TP oder Short-SL (Exit-SL) gefüllt wurde
        # Bot Phase State Machine (verhindert Logik-Ausführung außerhalb erlaubter Phasen)
        self.bot_phase: str = "INIT"  # INIT, RUNNING, BURN, REBUY, EXIT
        # Guard: verhindert mehrfaches Setzen des Short-TP für dieselbe Short-Position
        self.short_tp_set_for_current_short: bool = False
        # Guard: verhindert mehrfaches Setzen des Long-TP für dieselbe Long-Position
        self.long_tp_set_for_current_long: bool = False
        
        # Position Tracking (bot-spezifisch)
        if bot_type == 'short':
            self.short_positions_count: int = 0
            self.long_position_exists: bool = False
        else:  # long
            self.long_positions_count: int = 0
        
        self.last_processed_position: Dict[str, Any] = {}
        
        # Position Size Tracking (für echte Positionsänderungserkennung)
        # WICHTIG: Nur echte Size-Änderungen triggern cancel_all_orders_complete()
        self.last_long_size: float = 0.0
        self.last_short_size: float = 0.0
        
        # Long-Snapshot (WS-stabil, REST-frei) - für Burn-/Spread-Berechnungen
        # WICHTIG: Wird nur bei WS-Position-Updates aktualisiert, niemals REST-abhängig
        self.last_long_snapshot: Dict[str, Optional[float]] = {
            "size": None,
            "avg": None,
        }
        # Optionaler Short-Snapshot (für Spread-Berechnung und Rebuy-Logik)
        self.last_short_snapshot: Dict[str, Optional[float]] = {
            "size": None,
            "avg": None,
        }
        
        # Burn State (wird aus State-File geladen)
        # Unterstützt sowohl Long-Bot (short_tp_fill_count) als auch Short-Bot (long_tp_fill_count)
        self.burn_state: Dict[str, Any] = {
            # Long-Bot spezifisch (Short-TP-Fills)
            'short_tp_fill_count': 0,
            'total_short_profits': 0.0,
            # Short-Bot spezifisch (Long-TP-Fills)
            'long_tp_fill_count': 0,
            'total_long_profits': 0.0,
            # Gemeinsam
            'burn_count': 0,
            'total_burned': 0.0,
            # Explizites Burn-Tracking
            'burn_pending': False,
            'burn_planned': False,  # Flag für erfolgreichen Burn-Plan (robust gegen zukünftige Guards)
            'planned_burn_size': 0.0,
            'planned_burn_price': None,
            'planned_burn_profit': 0.0,
            'planned_target_short_size': 0.0,
            'planned_new_long_size': 0.0,
        }
        # Hedge Safety State (für Reentry-Airbag-Mechanismus)
        self.hedge_safety: Dict[str, Any] = {
            "active": False,
            "expected_short_size": None,
            "armed_at": None,
            "source": None,  # Herkunft der Hedge-Safety (z. B. "burn_plan")
        }
        
        # Race-Condition Protection: Burn-Cycle Lock
        self.in_burn_cycle: bool = False  # NOTE: Long-Bot verwendet noch, Short-Bot entfernt nach Init
        self.burn_cycle_lock = threading.Lock()
        
        # Locks
        self.tp_order_lock = threading.Lock()
        self.reentry_state: str = "IDLE"
        self.action_lock = threading.Lock()
        self._inflight_actions: Set[str] = set()
        self._action_last_done_at: Dict[str, float] = {}

        # Order pending flags (Race-Condition Protection)
        # Diese Flags werden vor dem API-Call gesetzt (atomar) um Double-Submit zu verhindern.
        self.short_rebuy_order_pending: bool = False
        self.long_rebalance_order_pending: bool = False
        # Mirror flags for the other bot direction (Long-Bot)
        self.long_rebuy_order_pending: bool = False
        self.short_rebalance_order_pending: bool = False
        
        # Settle-Phase State (verhindert Actions während Position-Settling nach TP-Fills)
        self.settle_active: bool = False
        self.settle_until: Optional[float] = None  # Timestamp bis wann Settle-Phase aktiv ist
        self.reentry_in_progress: bool = False  # Verhindert doppelte Reentry-Auslösung
        self.reentry_was_done: bool = False  # Markiert dass Reentry wirklich ausgeführt wurde (nicht nur Flag gesetzt)
        self.recalc_needed: bool = False  # Markiert ob Re-Berechnung nach Settle-Phase nötig ist
        self.long_tp_triggered_pending: bool = False  # Guard: verhindert Reentry zwischen TP-Trigger und TP-Fill
        
        # Timestamps
        self.bot_start_time: Optional[float] = None
        
        # Bot-spezifische Target-Notional (intern, nicht in Config)
        self.target_short_notional: Optional[float] = None
        self.target_long_notional: Optional[float] = None
        
        self.test_mode: bool = False
        self.test_fake_profit_used: bool = False

        # Spread-Control / Zyklus-Tracking
        # cycle_index ist 1-basiert und wird NUR nach vollständigem Zyklus erhöht
        # (Burn1 + Burn2 + Rebuy + Short-Reentry).
        self.cycle_index: int = 1
        # Spread_LS = (LongAvg - ShortAvg) / ShortAvg
        self.spread_ls: Optional[float] = None
        # Effektive Parameter aus rebuy_profile/spread_zones
        self.current_rebuy_factor: Optional[float] = None
        self.current_hedge_ratio: Optional[float] = None

    def try_begin_action(self, key: str, cooldown_seconds: float = 0.0) -> bool:
        """
        Simple idempotency guard for concurrent code paths.
        Returns True if the action may run now, False if it is already running
        or within the cooldown window.
        """
        now = time.time()
        with self.action_lock:
            if key in self._inflight_actions:
                return False
            last_done = self._action_last_done_at.get(key)
            if cooldown_seconds > 0.0 and last_done is not None and (now - last_done) < cooldown_seconds:
                return False
            self._inflight_actions.add(key)
            return True

    def end_action(self, key: str, mark_done: bool = True) -> None:
        """Marks an action as finished and optionally records a done timestamp."""
        now = time.time()
        with self.action_lock:
            self._inflight_actions.discard(key)
            if mark_done:
                self._action_last_done_at[key] = now
    
    def reset_websocket_state(self):
        """
        Setzt WebSocket-State zurück (wird beim Bot-Start aufgerufen).
        """
        self.ws_state.reset()
        self.ws_events.reset()
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Konvertiert den Context in ein Dictionary für Serialisierung (z.B. für State-File).
        WICHTIG: Nur persistente Daten, keine Threading-Objekte!
        """
        return {
            'current_short_tp_price': self.current_short_tp_price,
            'current_long_tp_price': self.current_long_tp_price,
            'current_short_sl_price': self.current_short_sl_price,
            'current_long_sl_price': self.current_long_sl_price,
            'planned_long_sl_price': self.planned_long_sl_price,
            'reentry_state': self.reentry_state,
            'first_position_closed': self.first_position_closed,
            'last_processed_position': self.last_processed_position,
            'last_long_size': self.last_long_size,
            'last_short_size': self.last_short_size,
            'burn_state': self.burn_state,
            'cycle_index': self.cycle_index,
            'spread_ls': self.spread_ls,
            'current_rebuy_factor': self.current_rebuy_factor,
            'current_hedge_ratio': self.current_hedge_ratio,
            'target_short_notional': self.target_short_notional,
            'target_long_notional': self.target_long_notional,
            'test_mode': self.test_mode,
            'test_fake_profit_used': self.test_fake_profit_used,
        }
    
    def from_dict(self, data: Dict[str, Any]):
        """
        Lädt den Context aus einem Dictionary (z.B. aus State-File).
        WICHTIG: Nur persistente Daten, keine Threading-Objekte!
        """
        self.current_short_tp_price = data.get('current_short_tp_price')
        self.current_long_tp_price = data.get('current_long_tp_price')
        self.current_short_sl_price = data.get('current_short_sl_price')
        self.current_long_sl_price = data.get('current_long_sl_price')
        self.planned_long_sl_price = data.get('planned_long_sl_price')
        state = data.get('reentry_state', "IDLE")
        self.reentry_state = state if state in ["IDLE", "DONE"] else "IDLE"
        self.first_position_closed = data.get('first_position_closed', False)
        self.last_processed_position = data.get('last_processed_position', {})
        self.last_long_size = data.get('last_long_size', 0.0)
        self.last_short_size = data.get('last_short_size', 0.0)
        self.burn_state = data.get('burn_state', self.burn_state)
        self.cycle_index = int(data.get('cycle_index', 1) or 1)
        self.spread_ls = data.get('spread_ls')
        self.current_rebuy_factor = data.get('current_rebuy_factor')
        self.current_hedge_ratio = data.get('current_hedge_ratio')
        self.target_short_notional = data.get('target_short_notional')
        self.target_long_notional = data.get('target_long_notional')
        self.test_mode = data.get('test_mode', False)
        self.test_fake_profit_used = data.get('test_fake_profit_used', False)

    @staticmethod
    def compute_ls_spread(long_avg: Optional[float], short_avg: Optional[float]) -> Optional[float]:
        """
        Hilfsfunktion zur Berechnung des Long–Short-Spreads:
        Spread_LS = (LongAvg - ShortAvg) / ShortAvg.
        """
        if long_avg is None or short_avg is None:
            return None
        try:
            if short_avg <= 0:
                return None
            return (float(long_avg) - float(short_avg)) / float(short_avg)
        except Exception:
            return None

    @staticmethod
    def resolve_rebuy_profile(
        profile: Any,
        base_hedge_ratio: float,
        cycle_index: int,
        spread_pct: Optional[float] = None,
        zones: Optional[Dict[str, Any]] = None,
    ) -> tuple[float, float, Optional[str], Optional[int]]:
        return shared_resolve_rebuy_profile(
            profile=profile,
            base_hedge_ratio=base_hedge_ratio,
            cycle_index=cycle_index,
            spread_pct=spread_pct,
            zones=zones,
        )

