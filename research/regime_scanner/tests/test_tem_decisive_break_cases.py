"""Case-study related unit checks (no live network)."""

from __future__ import annotations

from research.regime_scanner.tem_structure_break.decisive_root_cause import ROOT_CAUSE_DEV_CASES


def test_root_cause_docs_cover_four_dev_coins() -> None:
    assert set(ROOT_CAUSE_DEV_CASES) == {"DOTUSDT", "ATOMUSDT", "LTCUSDT", "INJUSDT"}
    for coin, doc in ROOT_CAUSE_DEV_CASES.items():
        assert "v2_path" in doc and "why_v2_rebreak_insufficient" in doc
