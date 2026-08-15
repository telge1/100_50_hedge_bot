"""Orchestrate HTF pivot level preview audit (read-only)."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any

import pandas as pd

from research.regime_scanner.htf_pivot_level_preview.config import (
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_REPLACEMENT,
    HtfPivotPreviewConfig,
    default_config,
    invalidation_mode_for_lifecycle,
)
from research.regime_scanner.htf_pivot_level_preview.levels import build_all_levels
from research.regime_scanner.htf_pivot_level_preview.pine_export import (
    build_htf_pivot_preview_pine,
    filter_htf_only_levels,
)
from research.regime_scanner.derivatives.config import load_target_config
from research.regime_scanner.liquidation_exhaustion.loader import (
    store_fetch_ohlcv,
    validate_symbols,
)

logger = logging.getLogger(__name__)


def load_preview_ohlcv_5m(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Load market_candles 5m only (HTF pivots do not need derivative join).

    The joined derivatives path ends ~2026-05-05; OHLCV extends further and is
    required for near-current-price level review.
    """
    symbols = validate_symbols(symbols)
    cfg = load_target_config()
    ohlcv = store_fetch_ohlcv(cfg, symbols=symbols, start=start, end=end)
    if ohlcv.empty:
        return ohlcv
    out = ohlcv.copy()
    out["bucket_start"] = pd.to_datetime(out["open_time"], utc=True)
    out["timestamp"] = out["bucket_start"]
    out["sequence_id"] = 0
    return out.sort_values(["symbol", "bucket_start"]).reset_index(drop=True)


def summarize_levels(levels: list[dict[str, Any]]) -> dict[str, Any]:
    by_tf_side: dict[str, int] = {}
    touches = breaks = replacements = 0
    active = 0
    n_with_first_touch = 0
    for r in levels:
        key = f"{r.get('timeframe')}|{r.get('side')}"
        by_tf_side[key] = by_tf_side.get(key, 0) + 1
        touches += int(r.get("touch_count") or 0)
        if r.get("first_touch_timestamp"):
            n_with_first_touch += 1
        if r.get("invalidation_reason") == "close_break":
            breaks += 1
        if r.get("invalidation_reason") == "replacement":
            replacements += 1
        if r.get("active"):
            active += 1
    by_source: dict[str, int] = {}
    for r in levels:
        s = str(r.get("source_type"))
        by_source[s] = by_source.get(s, 0) + 1
    return {
        "n_levels": len(levels),
        "n_active": active,
        "by_tf_side": by_tf_side,
        "by_source": by_source,
        "touch_events": touches,
        "n_levels_with_first_touch": n_with_first_touch,
        "close_breaks": breaks,
        "replacements": replacements,
    }


def cfg_for_lifecycle(base: HtfPivotPreviewConfig, lifecycle: str) -> HtfPivotPreviewConfig:
    return replace(
        base,
        lifecycle_mode=lifecycle,
        invalidation_mode=invalidation_mode_for_lifecycle(lifecycle),
        include_external_swing=False,
        include_protected=False,
        htf_only=True,
        embed_all_htf_levels=True,
    )


def run_symbol(df: pd.DataFrame, cfg: HtfPivotPreviewConfig) -> dict[str, Any]:
    symbol = str(df["symbol"].iloc[0])
    levels = build_all_levels(df, symbol=symbol, cfg=cfg)
    if cfg.htf_only:
        levels = filter_htf_only_levels(levels)
    ref = float(df["close"].iloc[-1]) if len(df) and "close" in df.columns else None
    pine = build_htf_pivot_preview_pine(levels, symbol=symbol, cfg=cfg, reference_price=ref)
    return {
        "symbol": symbol,
        "levels": levels,
        "pine": pine,
        "summary": summarize_levels(levels),
        "lifecycle_mode": cfg.lifecycle_mode,
        "reference_price": ref,
    }


def run_preview(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    cfg: HtfPivotPreviewConfig | None = None,
) -> dict[str, Any]:
    """Single-lifecycle preview (HTF-only by default)."""
    cfg = cfg or default_config()
    joined = load_preview_ohlcv_5m(symbols=symbols, start=start, end=end)
    cov = []
    if not joined.empty:
        for sym, g in joined.groupby("symbol", sort=True):
            cov.append(
                {
                    "symbol": str(sym),
                    "joined_rows": int(len(g)),
                    "min_bucket": str(g["bucket_start"].min()),
                    "max_bucket": str(g["bucket_start"].max()),
                }
            )

    all_levels: list[dict[str, Any]] = []
    pines: dict[str, str] = {}
    summaries: dict[str, Any] = {}

    for sym in symbols:
        g = (
            joined[joined["symbol"] == sym].sort_values("bucket_start").reset_index(drop=True)
            if len(joined)
            else pd.DataFrame()
        )
        if g.empty:
            logger.info("symbol=%s rows=0", sym)
            pines[sym] = build_htf_pivot_preview_pine([], symbol=sym, cfg=cfg)
            summaries[sym] = summarize_levels([])
            continue
        logger.info("symbol=%s rows=%s lifecycle=%s", sym, len(g), cfg.lifecycle_mode)
        res = run_symbol(g, cfg)
        all_levels.extend(res["levels"])
        pines[sym] = res["pine"]
        summaries[sym] = res["summary"]

    return {
        "cfg": cfg.to_dict(),
        "config_hash": cfg.config_hash(),
        "lifecycle_mode": cfg.lifecycle_mode,
        "joined_rows": int(len(joined)),
        "coverage": cov,
        "levels": all_levels,
        "pines": pines,
        "summaries": summaries,
        "db_writes": False,
        "causality_flags": {
            "htf_closed_bars_only": True,
            "visible_from_is_confirm_bar_close": True,
            "no_line_start_at_pivot_open": True,
            "no_lookahead_on": True,
            "no_extend_both": True,
            "no_retroactive_invalidation": True,
            "sequence_gap_resets_segments": True,
            "pine_embeds_python_levels": True,
            "htf_only": bool(cfg.htf_only),
            "embed_all_htf_levels": bool(cfg.embed_all_htf_levels),
            "touch_marker_at_first_touch_only": True,
        },
    }


def run_dual_lifecycle_htf_preview(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    base_cfg: HtfPivotPreviewConfig | None = None,
) -> dict[str, Any]:
    """Run replacement + persistent HTF-only previews (separate inventories/pines)."""
    base = base_cfg or default_config()
    joined = load_preview_ohlcv_5m(symbols=symbols, start=start, end=end)
    cov = []
    if not joined.empty:
        for sym, g in joined.groupby("symbol", sort=True):
            cov.append(
                {
                    "symbol": str(sym),
                    "joined_rows": int(len(g)),
                    "min_bucket": str(g["bucket_start"].min()),
                    "max_bucket": str(g["bucket_start"].max()),
                }
            )

    by_mode: dict[str, dict[str, Any]] = {}
    for lifecycle in (LIFECYCLE_REPLACEMENT, LIFECYCLE_PERSISTENT):
        cfg = cfg_for_lifecycle(base, lifecycle)
        levels_all: list[dict[str, Any]] = []
        pines: dict[str, str] = {}
        summaries: dict[str, Any] = {}
        for sym in symbols:
            g = (
                joined[joined["symbol"] == sym].sort_values("bucket_start").reset_index(drop=True)
                if len(joined)
                else pd.DataFrame()
            )
            if g.empty:
                pines[sym] = build_htf_pivot_preview_pine([], symbol=sym, cfg=cfg)
                summaries[sym] = summarize_levels([])
                continue
            logger.info("symbol=%s rows=%s lifecycle=%s", sym, len(g), lifecycle)
            res = run_symbol(g, cfg)
            levels_all.extend(res["levels"])
            pines[sym] = res["pine"]
            summaries[sym] = res["summary"]
        by_mode[lifecycle] = {
            "cfg": cfg.to_dict(),
            "config_hash": cfg.config_hash(),
            "lifecycle_mode": lifecycle,
            "levels": levels_all,
            "pines": pines,
            "summaries": summaries,
        }

    return {
        "coverage": cov,
        "joined_rows": int(len(joined)),
        "modes": by_mode,
        "db_writes": False,
        "causality_flags": {
            "htf_closed_bars_only": True,
            "visible_from_is_confirm_bar_close": True,
            "no_line_start_at_pivot_open": True,
            "no_lookahead_on": True,
            "no_extend_both": True,
            "htf_only": True,
            "embed_all_htf_levels": True,
            "touch_marker_at_first_touch_only": True,
            "dual_lifecycle": True,
        },
    }
