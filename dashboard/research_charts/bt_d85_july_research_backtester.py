"""BT-D85 July 2026 — backward-compatible re-exports (BT-D95 shared overlay)."""

from __future__ import annotations

from typing import Any

from .frozen_month_research_overlay_v1 import (
    CONTRACT_VERSION,
    FILL_DISCLAIMER,
    SYMBOL,
    build_drawing_specs as _build_drawing_specs,
    build_summary as _build_summary,
    get_profile,
    load_trade_ledger as _load_trade_ledger,
    validate_ledger as _validate_ledger,
)

_PROFILE = get_profile("july")

STRATEGY_ID = _PROFILE.strategy_id
DISPLAY_NAME = _PROFILE.display_name
BACKTESTER_SOURCE = _PROFILE.backtester_source
BOX_PREFIX = _PROFILE.box_prefix
LINK_PREFIX = _PROFILE.link_prefix
MARKER_PREFIX = "btd85-mk-"
EXPECTED_FILLS = _PROFILE.expected_fills
EXPECTED_TP = _PROFILE.expected_tp
EXPECTED_SL = _PROFILE.expected_sl


def ledger_paths():
    from .frozen_month_research_overlay_v1 import ledger_paths as _lp

    return _lp(_PROFILE)


def load_trade_ledger():
    return _load_trade_ledger(_PROFILE)


def validate_ledger(rows: list[dict[str, Any]]) -> list[str]:
    return _validate_ledger(_PROFILE, rows)


def build_summary(rows: list[dict[str, Any]], *, ledger_path: Any) -> dict[str, Any]:
    return _build_summary(_PROFILE, rows, ledger_path=ledger_path)


def build_drawing_specs(rows: list[dict[str, Any]]):
    return _build_drawing_specs(_PROFILE, rows)


def trade_to_position_spec(row: dict[str, Any]) -> dict[str, Any]:
    from .frozen_month_research_overlay_v1 import trade_to_position_spec

    return trade_to_position_spec(_PROFILE, row)


def trade_to_link_spec(row: dict[str, Any]) -> dict[str, Any]:
    from .frozen_month_research_overlay_v1 import trade_to_link_spec

    return trade_to_link_spec(_PROFILE, row)
