"""Per-box watcher state; max one global long position."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import LongBoxSetup, WatcherPhase

ConfirmationMode = Literal["A1", "A2", "A3"]


@dataclass
class BoxWatcher:
    setup: LongBoxSetup
    phase: WatcherPhase = "IDLE"
    touch_bar_idx: int | None = None
    touch_utc: int | None = None
    watch_bars_left: int = 0
    confirmed_bar_idx: int | None = None


@dataclass
class WatcherPool:
    """One open position max; competing watchers stay WATCHING until slot free."""

    watchers: dict[str, BoxWatcher] = field(default_factory=dict)
    global_position_open: bool = False
    filled_drawing_id: str | None = None
    watch_window_bars: int = 12
    confirmation_mode: ConfirmationMode = "A2"
    confirmation_min_gain_pct: float = 0.20
    touch_tolerance_pct: float = 0.0

    def register(self, setup: LongBoxSetup) -> None:
        if setup.drawing_id not in self.watchers:
            self.watchers[setup.drawing_id] = BoxWatcher(setup=setup)

    def can_schedule_entry(self, drawing_id: str) -> bool:
        return not self.global_position_open and self.filled_drawing_id is None

    def mark_filled(self, drawing_id: str) -> None:
        self.global_position_open = True
        self.filled_drawing_id = drawing_id
        w = self.watchers.get(drawing_id)
        if w:
            w.phase = "FILLED"

    def mark_closed(self, drawing_id: str) -> None:
        self.global_position_open = False
        self.filled_drawing_id = None
