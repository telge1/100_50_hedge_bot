"""On-demand live symbols (chart selection). Empty → use config/live_universe.json."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_demand_path() -> Path:
    return (
        Path(__file__).resolve().parents[4]
        / "results"
        / "live_collector"
        / "demand_symbols.json"
    )


@dataclass(slots=True)
class DemandSymbolStore:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def read(self) -> list[str]:
        if not self.path.is_file():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        seen: set[str] = set()
        ordered: list[str] = []
        for item in raw.get("symbols") or []:
            sym = str(item).strip().upper()
            if not sym or sym == "BTCUSDT" or sym in seen:
                continue
            seen.add(sym)
            ordered.append(sym)
        return ordered

    def write(self, symbols: list[str], *, reason: str = "") -> dict[str, Any]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in symbols:
            sym = str(item).strip().upper()
            if not sym or sym == "BTCUSDT" or sym in seen:
                continue
            seen.add(sym)
            ordered.append(sym)
        payload = {
            "symbols": ordered,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason or None,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
            prefix=".demand_",
            suffix=".tmp",
        ) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)
        return payload

    def write_singleton(self, symbol: str, *, reason: str = "ensure_symbol") -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise ValueError("symbol required")
        if sym == "BTCUSDT":
            raise ValueError("BTCUSDT must not be in signal demand")
        return self.write([sym], reason=reason)
