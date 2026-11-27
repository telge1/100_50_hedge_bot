"""MC3A — isolated 5m PD1 pipeline_line_id per symbol (no shared pool state)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from research_charts.historical_lmv2_pd_contract_v1 import (
    HISTORICAL_SYMBOL,
    resolve_historical_pd_config,
)

# MC4 live lines (completed MC3D/MC3E replays) — string literals avoid import cycles.
MC3D_SOL_FULL_LINE = "historical_mixed_frozen_sol_mc3d_full_2026_v1_5mpd1"
MC3E_BNB_FULL_LINE = "historical_mixed_frozen_bnb_mc3e_full_2026_v1_5mpd1"
MC5B_LINK_FULL_LINE = "historical_mixed_frozen_link_mc5b_full_2026_v1_5mpd1"
MC5C_XAU_FULL_LINE = "historical_mixed_frozen_xau_mc5c_full_2026_v1_5mpd1"
MC6A_LIT_FULL_LINE = "historical_mixed_frozen_lit_mc6a_full_2026_v1_5mpd1"
MC6A_HYPE_FULL_LINE = "historical_mixed_frozen_hype_mc6a_full_2026_v1_5mpd1"
MC7A_ZEC_FULL_LINE = "historical_mixed_frozen_zec_mc7a_full_2026_v1_5mpd1"
MC7A_QNT_FULL_LINE = "historical_mixed_frozen_qnt_mc7a_full_2026_v1_5mpd1"

# Production DOGE 5m PD1 line — never write from multi-coin runner unless symbol=DOGEUSDT + explicit opt-in.
DOGE_PROTECTED_5M_PD1_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"

PD1_CONFIG_HASH_5M = "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9"

MC3A_TARGET_SYMBOLS: tuple[str, ...] = (
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "XAUUSDT",
)

# MC4 — symbols with completed full-history 5m PD1 replay + live publisher.
MC4_LIVE_SYMBOLS: tuple[str, ...] = ("SOLUSDT", "BNBUSDT")

# MC5E — multicoin 5m PD1 viewer (historical publish SSOT; LINK/XAU ohne Live-Publisher).
MC5E_5M_PD1_VIEWER_SYMBOLS: tuple[str, ...] = MC4_LIVE_SYMBOLS + (
    "LINKUSDT",
    "XAUUSDT",
    "LITUSDT",
    "HYPEUSDT",
    "ZECUSDT",
    "QNTUSDT",
)

# First 1m candle in ClickHouse (post-MC1); rolling 4-month start derives from these.
_SYMBOL_DATA_EARLIEST_UTC: dict[str, str] = {
    "DOGEUSDT": "2025-12-11",
    "SOLUSDT": "2026-01-01",
    "BNBUSDT": "2026-01-01",
    "XRPUSDT": "2026-01-01",
    "LINKUSDT": "2026-01-01",
    "XAUUSDT": "2026-03-09",  # Bybit listing; first CH minute ~02:14 UTC
    "LITUSDT": "2025-12-30",  # Bybit launch 2025-12-30; first 1m 13:48 UTC
    "HYPEUSDT": "2025-12-11",  # shared research floor; Bybit lists earlier
    "ZECUSDT": "2025-12-11",  # Bybit linear launch 2021-11-24; CH floor matches the collector
    "QNTUSDT": "2026-01-01",  # Bybit linear launch 2023-06-19; MC7A backfill floor
}

# One dedicated publish line per symbol (MC3A prep; no CH data until MC3B replay).
_SYMBOL_5M_PD1_PIPELINE_LINE: dict[str, str] = {
    "DOGEUSDT": DOGE_PROTECTED_5M_PD1_LINE,
    "SOLUSDT": MC3D_SOL_FULL_LINE,
    "BNBUSDT": MC3E_BNB_FULL_LINE,
    "XRPUSDT": "historical_mixed_frozen_xrp_jan_nov_2026_v1_5mpd1_v1",
    "LINKUSDT": MC5B_LINK_FULL_LINE,
    "XAUUSDT": MC5C_XAU_FULL_LINE,
    "LITUSDT": MC6A_LIT_FULL_LINE,
    "HYPEUSDT": MC6A_HYPE_FULL_LINE,
    "ZECUSDT": MC7A_ZEC_FULL_LINE,
    "QNTUSDT": MC7A_QNT_FULL_LINE,
}


@dataclass(frozen=True)
class MulticoinPd1Scope:
    symbol: str
    timeframe: str
    pool_detail: int
    pipeline_line_id: str
    config_hash: str
    data_earliest_utc: str
    origin_namespace: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pool_detail": self.pool_detail,
            "pipeline_line_id": self.pipeline_line_id,
            "config_hash": self.config_hash,
            "data_earliest_utc": self.data_earliest_utc,
            "origin_namespace": self.origin_namespace,
        }


def _origin_namespace(symbol: str, pipeline_line_id: str) -> str:
    return f"{symbol}:{pipeline_line_id}:pd1"


def assert_symbol_allowed(symbol: str) -> str:
    sym = str(symbol).strip().upper()
    if sym not in _SYMBOL_5M_PD1_PIPELINE_LINE:
        raise ValueError(
            f"unsupported symbol {sym!r}; allowed: {sorted(_SYMBOL_5M_PD1_PIPELINE_LINE)}"
        )
    return sym


def resolve_multicoin_pd1_scope(symbol: str) -> MulticoinPd1Scope:
    sym = assert_symbol_allowed(symbol)
    baseline = resolve_historical_pd_config(1, timeframe="5m")
    line_id = _SYMBOL_5M_PD1_PIPELINE_LINE[sym]
    if sym == HISTORICAL_SYMBOL:
        assert line_id == baseline.pipeline_line_id
    if baseline.config_hash != PD1_CONFIG_HASH_5M:
        raise RuntimeError("PD1 config_hash drift vs MC3A baseline")
    return MulticoinPd1Scope(
        symbol=sym,
        timeframe="5m",
        pool_detail=1,
        pipeline_line_id=line_id,
        config_hash=baseline.config_hash,
        data_earliest_utc=_SYMBOL_DATA_EARLIEST_UTC[sym],
        origin_namespace=_origin_namespace(sym, line_id),
    )


def assert_pipeline_line_matches_symbol(symbol: str, pipeline_line_id: str) -> None:
    sym = assert_symbol_allowed(symbol)
    expected = _SYMBOL_5M_PD1_PIPELINE_LINE[sym]
    got = str(pipeline_line_id).strip()
    if got != expected:
        raise ValueError(
            f"pipeline_line_id {got!r} does not match {sym} registry line {expected!r}"
        )


def assert_safe_write_target(symbol: str, pipeline_line_id: str, *, allow_doge_production: bool = False) -> None:
    """Block accidental writes to foreign lines or DOGE production without explicit flag."""
    sym = assert_symbol_allowed(symbol)
    if pipeline_line_id == DOGE_PROTECTED_5M_PD1_LINE and sym != "DOGEUSDT":
        raise PermissionError("refusing write: DOGE production line for non-DOGE symbol")
    assert_pipeline_line_matches_symbol(symbol, pipeline_line_id)
    if sym == "DOGEUSDT" and pipeline_line_id == DOGE_PROTECTED_5M_PD1_LINE and not allow_doge_production:
        raise PermissionError(
            "MC3A: refuse writes to DOGE production 5m PD1 line; use --allow-doge-production in MC3B"
        )


def multicoin_pd1_registry_export() -> dict[str, Any]:
    return {
        "version": "multicoin_pd1_scope_v1_mc3a",
        "pool_detail": 1,
        "timeframe": "5m",
        "symbols": {s: resolve_multicoin_pd1_scope(s).to_dict() for s in sorted(_SYMBOL_5M_PD1_PIPELINE_LINE)},
        "protected_doge_line": DOGE_PROTECTED_5M_PD1_LINE,
    }
