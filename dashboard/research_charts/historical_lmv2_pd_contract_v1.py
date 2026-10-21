"""Authoritative historical LMV2 PD mapping (Phase 5V inventory, read-only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)

HISTORICAL_SYMBOL = "DOGEUSDT"
HISTORICAL_TIMEFRAME = "15m"

# Viewer / landscape API: only these (timeframe, pool_detail) pairs.
ALLOWED_VIEWER_SCOPES: frozenset[tuple[str, int]] = frozenset(
    {
        ("15m", 1),
        ("15m", 5),
        ("15m", 10),
        ("5m", 1),
        ("5m", 5),
        ("5m", 10),
        ("4h", 10),
    }
)

_4H_PD10_PIPELINE_LINE_ID = "historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1"

_PD1_LINE_BY_TIMEFRAME: dict[str, str] = {
    "15m": "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1",
    "5m": "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1",
}

_5M_LINE_BY_POOL_DETAIL: dict[int, str] = {
    1: _PD1_LINE_BY_TIMEFRAME["5m"],
    5: "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd5_v1",
    10: "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd10_v1",
}

# Confirmed production lines (phase5v pd_matrix.json + CH publish metadata).
_PD_REGISTRY: dict[int, dict[str, str]] = {
    1: {
        "pipeline_line_id": _PD1_LINE_BY_TIMEFRAME["15m"],
    },
    5: {
        "pipeline_line_id": "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd5_v1",
    },
    10: {
        "pipeline_line_id": "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1",
    },
}

# Phase 5V verified config hashes (identity.py effective_config per pool_detail).
_PHASE5V_CONFIG_HASH = {
    1: "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9",
    5: "533e04c7cd0e1c93540ed9466a2a5a2eb626f82b828b9a45d1da234703ebd03c",
    10: "bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2",
}


@dataclass(frozen=True)
class HistoricalPdConfig:
    pool_detail: int
    symbol: str
    timeframe: str
    pipeline_line_id: str
    config_hash: str

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "pool_detail": self.pool_detail,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pipeline_line_id": self.pipeline_line_id,
            "config_hash": self.config_hash,
        }


def supported_historical_pool_details() -> tuple[int, ...]:
    return (1, 5, 10)


def membership_slot_minutes(timeframe: str) -> int:
    tf = str(timeframe).strip()
    if tf == "5m":
        return 5
    if tf == "15m":
        return 15
    if tf == "4h":
        return 240
    raise ValueError(f"unsupported membership timeframe {tf!r}")


def resolve_viewer_historical_scope(
    *,
    timeframe: str | None = None,
    pool_detail: int | None = None,
) -> HistoricalPdConfig:
    from .historical_pool_state_v1 import HistoricalPoolStateError

    tf = str(timeframe or HISTORICAL_TIMEFRAME).strip()
    pd = int(pool_detail if pool_detail is not None else 1)
    if (tf, pd) not in ALLOWED_VIEWER_SCOPES:
        raise HistoricalPoolStateError(
            "INVALID_SCOPE",
            "viewer scope not in release matrix",
            {"timeframe": tf, "pool_detail": pd, "allowed": list(ALLOWED_VIEWER_SCOPES)},
        )
    return resolve_historical_pd_config(pd, timeframe=tf)


def resolve_historical_pd_config(
    pool_detail: int,
    *,
    timeframe: str = HISTORICAL_TIMEFRAME,
) -> HistoricalPdConfig:
    from .historical_pool_state_v1 import HistoricalPoolStateError

    pd = int(pool_detail)
    tf = str(timeframe).strip()
    if pd not in _PD_REGISTRY:
        raise HistoricalPoolStateError(
            "INVALID_HISTORICAL_PD",
            f"unsupported historical pool_detail {pd}; allowed: {list(_PD_REGISTRY)}",
            {"pool_detail": pd, "allowed": list(_PD_REGISTRY.keys())},
        )
    if tf == "4h":
        if pd != 10:
            raise HistoricalPoolStateError(
                "INVALID_SCOPE",
                "4h viewer supports PD10 only",
                {"timeframe": tf, "pool_detail": pd},
            )
        line_id = _4H_PD10_PIPELINE_LINE_ID
    elif tf == "5m":
        if pd not in _5M_LINE_BY_POOL_DETAIL:
            raise HistoricalPoolStateError(
                "INVALID_SCOPE",
                f"unsupported 5m pool_detail {pd}",
                {"timeframe": tf, "pool_detail": pd, "allowed": list(_5M_LINE_BY_POOL_DETAIL)},
            )
        line_id = _5M_LINE_BY_POOL_DETAIL[pd]
    elif pd == 1:
        if tf not in _PD1_LINE_BY_TIMEFRAME:
            raise HistoricalPoolStateError(
                "INVALID_SCOPE",
                f"unsupported PD1 timeframe {tf!r}",
                {"timeframe": tf, "pool_detail": pd, "allowed_timeframes": list(_PD1_LINE_BY_TIMEFRAME)},
            )
        line_id = _PD1_LINE_BY_TIMEFRAME[tf]
    else:
        if tf != HISTORICAL_TIMEFRAME:
            raise HistoricalPoolStateError(
                "INVALID_SCOPE",
                "PD5/PD10 viewer data is 15m-line only; 5m uses dedicated publish lines",
                {"timeframe": tf, "pool_detail": pd},
            )
        line_id = _PD_REGISTRY[pd]["pipeline_line_id"]
    derived = config_hash(build_live_micro_lld_v2_config(pd))
    expected = _PHASE5V_CONFIG_HASH[pd]
    if derived != expected:
        raise HistoricalPoolStateError(
            "CONFIG_HASH_DRIFT",
            "identity config_hash does not match phase5v inventory",
            {"pool_detail": pd, "derived": derived, "expected": expected},
        )
    return HistoricalPdConfig(
        pool_detail=pd,
        symbol=HISTORICAL_SYMBOL,
        timeframe=tf,
        pipeline_line_id=line_id,
        config_hash=derived,
    )


def historical_pd_registry_export() -> dict[str, Any]:
    return {
        "version": "historical_lmv2_pd_contract_v1",
        "symbol": HISTORICAL_SYMBOL,
        "timeframe": HISTORICAL_TIMEFRAME,
        "supported_pool_details": list(_PD_REGISTRY.keys()),
        "entries": {
            str(pd): resolve_historical_pd_config(pd).to_api_dict() for pd in _PD_REGISTRY
        },
    }
