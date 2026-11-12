"""Single DOGE manual box selection rules."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from research.manual_long_box_research_v1.single_box import (
    SingleBoxError,
    require_single_manual_doge_long,
)


def _write_drawings(path: Path, drawings: list) -> None:
    path.write_text(json.dumps({"version": 1, "drawings": drawings}), encoding="utf-8")


def _box(did: str, sym: str = "DOGEUSDT") -> dict:
    return {
        "drawing_id": did,
        "drawing_type": "long_position",
        "symbol": sym,
        "visible": True,
        "start_timestamp": 1_700_000_000,
        "end_timestamp": 1_700_086_400,
        "entry_price": 0.0842,
        "stop_price": 0.083,
        "target_price": 0.086,
        "created_on_timeframe": "5m",
    }


def test_none_manual_doge():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "drawings.json"
        _write_drawings(p, [_box("p17-x")])
        with pytest.raises(SingleBoxError) as ei:
            require_single_manual_doge_long(drawings_path=p)
        assert ei.value.code == "NO_MANUAL_DOGE_LONG"


def test_multiple_manual_doge():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "drawings.json"
        _write_drawings(p, [_box("d-a"), _box("d-b")])
        with pytest.raises(SingleBoxError) as ei:
            require_single_manual_doge_long(drawings_path=p)
        assert ei.value.code == "MULTIPLE_MANUAL_DOGE_LONG"
        assert len(ei.value.ids) == 2


def test_exactly_one():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "drawings.json"
        _write_drawings(p, [_box("d-ok")])
        sel = require_single_manual_doge_long(drawings_path=p)
        assert sel.setup.drawing_id == "d-ok"
        assert sel.setup.entry_price == 0.0842
