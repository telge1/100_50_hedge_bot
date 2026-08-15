"""No-hardcoding gate for decisive signal modules."""

from __future__ import annotations

from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "tem_structure_break"
FORBIDDEN = ["DOTUSDT", "ATOMUSDT", "LTCUSDT", "INJUSDT", "AAVEUSDT", "2026-01-19", "170.86"]


def test_decisive_signal_code_has_no_coin_or_chart_literals() -> None:
    for name in ("decisive_break.py", "decisive_levels.py", "decisive_models.py", "decisive_evaluation.py"):
        text = (PKG / name).read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in text, f"{name} contains {token}"
