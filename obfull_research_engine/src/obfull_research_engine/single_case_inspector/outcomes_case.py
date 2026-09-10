"""Post-focus public-trade outcomes (strictly after focus)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..outcomes.metrics import directional_mfe_mae, path_extremes_bps, return_bps
from ..outcomes.public_trade_builder import evaluate_pt_horizon, load_pt_config, pt_config_sha256
from ..outcomes.public_trade_index import PublicTradeIndex
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import OUTCOME_HORIZONS_S, PRICE_POLICY, PRICE_SOURCE


def default_pt_config_path() -> Path:
    return ENGINE_ROOT / "config" / "episode_outcome_public_trade_v1_1.json"


def build_post_focus_outcomes(
    *,
    symbol: str,
    focus_ts: datetime,
    trade_index: PublicTradeIndex,
    load_meta: dict[str, Any],
    direction_hint: str,
    cfg_path: Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    focus_ts = focus_ts.astimezone(timezone.utc)
    path = Path(cfg_path or default_pt_config_path())
    cfg = load_pt_config(path)
    assert str(cfg.get("price_policy_version") or cfg.get("price_policy")) == PRICE_POLICY
    och = pt_config_sha256(path)

    # Synthetic episode row — focus is visual selection used as outcome anchor only
    episode = pd.Series(
        {
            "episode_id": f"case_{symbol.upper()}_{format_utc_z(focus_ts).replace(':','').replace('-','')}",
            "symbol": symbol.upper(),
            "episode_start_ts": focus_ts,
            "first_detection_available_at": focus_ts,
            "direction_hint": direction_hint or "UNCLEAR",
            "support_status": "NOT_EVALUATED",
            "primary_trigger_type": "CHART_FOCUS_SELECTION",
            "primary_behavior_type": "SINGLE_CASE_INSPECTOR",
            "candidate_count": 0,
            "proxy_flags": ["FOCUS_IS_NOT_AUTO_SIGNAL"],
            "quality_flags": ["CHART_FOCUS_SELECTION"],
        }
    )

    rows: list[dict[str, Any]] = []
    for h in OUTCOME_HORIZONS_S:
        r = evaluate_pt_horizon(
            episode=episode,
            horizon_seconds=int(h),
            trade_index=trade_index,
            cfg=cfg,
            outcome_config_hash=och,
            source_episode_config_hash="single_case_inspector_v1",
            full_ob_post_detection_gap=True,
            load_meta=load_meta,
        )
        # Neutral MFE/MAE from path extremes
        neut_mfe = r.get("max_up_bps")
        neut_mae = None if r.get("max_down_bps") is None else abs(float(r["max_down_bps"]))
        # first direction from path
        first_dir = _first_path_direction(trade_index, focus_ts, int(h), float(r["anchor_trade_price"]) if r.get("anchor_trade_price") else None)
        r["neutral_mfe_bps"] = neut_mfe
        r["neutral_mae_bps"] = neut_mae
        r["first_path_direction"] = first_dir
        r["price_source"] = PRICE_SOURCE
        r["price_policy_version"] = PRICE_POLICY
        # directional already in mfe_bps/mae_bps when direction clear
        rows.append(r)

    df = pd.DataFrame(rows)
    meta = {
        "price_source": PRICE_SOURCE,
        "price_policy": PRICE_POLICY,
        "config_path": str(path),
        "outcome_config_hash": och,
        "anchor_cut": "trade_ts < focus_ts",
        "path_interval": "[focus_ts, focus_ts+horizon]",
        "separated_from_early_evidence": True,
    }
    return df, meta


def _first_path_direction(
    trade_index: PublicTradeIndex,
    focus_ts: datetime,
    horizon_s: int,
    anchor: float | None,
) -> str:
    if anchor is None or anchor <= 0:
        return "UNAVAILABLE"
    end = pd.Timestamp(focus_ts) + pd.Timedelta(seconds=horizon_s)
    path = trade_index.path_slice(pd.Timestamp(focus_ts), end)
    for t in path:
        ret = (t.price - anchor) / anchor
        if abs(ret) < 1e-10:
            continue
        return "BULLISH" if ret > 0 else "BEARISH"
    return "FLAT_OR_NO_MOVE"
