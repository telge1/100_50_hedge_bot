"""Select exactly one manual DOGEUSDT long box (read-only drawings)."""

from __future__ import annotations

from dataclasses import dataclass

from .drawing_source import DEFAULT_DRAWINGS_PATH, load_drawings_payload
from .models import LongBoxSetup
from .recognition import recognize_long_boxes


class SingleBoxError(RuntimeError):
    def __init__(self, code: str, message: str, *, ids: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.ids = ids or []


@dataclass(frozen=True)
class SingleBoxSelection:
    setup: LongBoxSetup
    drawings_path: str


def require_single_manual_doge_long(
    *,
    symbol: str = "DOGEUSDT",
    drawings_path=None,
) -> SingleBoxSelection:
    path = drawings_path or DEFAULT_DRAWINGS_PATH
    payload = load_drawings_payload(path)
    setups, _ = recognize_long_boxes(payload, symbol=symbol, manual_only=True)
    if not setups:
        raise SingleBoxError(
            "NO_MANUAL_DOGE_LONG",
            "Keine manuelle DOGEUSDT-Long-Box in drawings.json. "
            "Bitte zuerst eine long_position im Research Chart zeichnen und speichern.",
            ids=[],
        )
    if len(setups) > 1:
        ids = [s.drawing_id for s in setups]
        raise SingleBoxError(
            "MULTIPLE_MANUAL_DOGE_LONG",
            f"Mehr als eine manuelle DOGEUSDT-Long-Box ({len(setups)}). "
            f"Bitte nur eine Box behalten. IDs: {', '.join(ids)}",
            ids=ids,
        )
    return SingleBoxSelection(setup=setups[0], drawings_path=str(path))
