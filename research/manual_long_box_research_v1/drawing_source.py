"""Read-only load of drawings.json (never writes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_DRAWINGS_PATH = (
    Path(__file__).resolve().parents[2]
    / "backtester/short/dashboard/research_charts/user_data/drawings.json"
)


def load_drawings_payload(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_DRAWINGS_PATH
    if not p.is_file():
        raise FileNotFoundError(f"drawings not found: {p}")
    text = p.read_text(encoding="utf-8")
    return json.loads(text)
