"""Build multiscale episode context rows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ..avr.builder import _ch_client, build_avr_state_1s
from ..avr.provenance import collect_provenance, ensure_avr_import_path
from . import (
    ADAPTER_VERSION,
    CANDLE_5M_S,
    CONTRACT_VERSION,
    JOIN_VERSION,
    LOOKBACK_S,
    WINDOWS_S,
)
from .candles_5m import build_candle_at, build_complete_5m_candles, parity_5m
from .config import multiscale_config_hash, multiscale_config_payload
from .footprint import (
    aggregate_avr_persistence,
    floor_to_5m,
    footprint_window_from_series,
)
from .oi_liq import build_state_index, liq_window_from_states, oi_window_from_states


def load_second_series(symbol: str, start: datetime, end: datetime):
    ensure_avr_import_path()
    from footprint_candles.response_engine import SecondSeries
    from footprint_candles.response_service import fetch_second_buckets

    start_u = int(start.timestamp())
    end_u = int(end.timestamp())
    client = _ch_client()
    try:
        buckets = fetch_second_buckets(client, symbol.upper(), start_u, end_u)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return SecondSeries(buckets), buckets


def enrich_episodes_multiscale(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    episodes: pd.DataFrame,
    state_df: pd.DataFrame,
    avr_1s_feature: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Return episode context df, 5m candles, parity, meta."""
    ensure_avr_import_path()
    from footprint_candles.contracts import TRADES_FQN

    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    start_u = int(start.timestamp())
    end_u = int(end.timestamp())
    load_start = datetime.fromtimestamp(start_u - LOOKBACK_S, tz=timezone.utc)

    provenance = collect_provenance()
    cfg = multiscale_config_payload()
    cfg_hash = multiscale_config_hash()

    series, _buckets = load_second_series(symbol, load_start, end)

    # AVR lookback covering [start-300, end) for persistence
    avr_lookback_start = datetime.fromtimestamp(start_u - LOOKBACK_S, tz=timezone.utc)
    avr_1s_ext, avr_meta = build_avr_state_1s(
        symbol=symbol, start=avr_lookback_start, end=end, provenance=provenance
    )
    if avr_1s_feature is not None and not avr_1s_feature.empty:
        # Prefer feature-window rows from stage artifact when present (same contract)
        pass

    state_by_ts = build_state_index(state_df)

    # Complete 5m candles in feature window + independent parity pass
    candles = build_complete_5m_candles(
        symbol=symbol,
        series=series,
        avr_1s=avr_1s_ext,
        state_by_ts=state_by_ts,
        feature_start_unix=start_u,
        feature_end_unix=end_u,
    )
    series_ref, _ = load_second_series(symbol, load_start, end)
    candles_ref = build_complete_5m_candles(
        symbol=symbol,
        series=series_ref,
        avr_1s=avr_1s_ext,
        state_by_ts=state_by_ts,
        feature_start_unix=start_u,
        feature_end_unix=end_u,
    )
    preport = parity_5m(candles, candles_ref)

    rows: list[dict[str, Any]] = []
    for _, ep in episodes.iterrows():
        det = pd.to_datetime(ep["first_detection_available_at"], utc=True)
        t = float(det.timestamp())
        row: dict[str, Any] = {
            "episode_id": str(ep["episode_id"]),
            "symbol": symbol,
            "schema_version": "avr_multiscale_episode_context_v1",
            "episode_status_label": "RESEARCH_CONTEXT_ONLY",
            "adapter_version": ADAPTER_VERSION,
            "multiscale_contract_version": CONTRACT_VERSION,
            "multiscale_config_hash": cfg_hash,
            "avr_config_hash": provenance.get("avr_config_hash"),
            "avr_source_contract_version": provenance.get("avr_source_contract_version"),
            "avr_source_code_hash": provenance.get("avr_source_code_hash"),
            "oi_liq_join_version": JOIN_VERSION,
            "fp_source_fqn": TRADES_FQN,
            "first_detection_available_at": det,
            "first_detection_available_at_unix": t,
            "ob_primary_behavior_type": str(ep.get("primary_behavior_type") or ""),
            "ob_direction_hint": str(ep.get("direction_hint") or ""),
            "ob_support_status": str(ep.get("support_status") or ""),
            "candidate_count": int(ep.get("candidate_count") or 0),
        }

        avail_i = int(t)  # floor(t); second buckets with second_ts < avail_i ⇒ trade_ts < t when t integer; if fractional, still no future seconds

        for w in WINDOWS_S:
            pfx = f"w{w}s"
            fp = footprint_window_from_series(
                series, available_at=avail_i, window_s=w, prefix=f"fp_{pfx}"
            )
            avr = aggregate_avr_persistence(
                avr_1s_ext, t_unix=float(avail_i), window_s=w, prefix=f"avr_{pfx}"
            )
            row.update(fp)
            row.update(avr)

        # OI/liq for 60s and 300s
        for w in (60, 300):
            row.update(
                oi_window_from_states(
                    state_by_ts, t_unix=float(avail_i), window_s=w, prefix=f"w{w}s"
                )
            )
            row.update(
                liq_window_from_states(
                    state_by_ts, t_unix=float(avail_i), window_s=w, prefix=f"w{w}s"
                )
            )

        # Current partial 5m
        cur_start = floor_to_5m(avail_i if avail_i > 0 else t)
        # If detection falls exactly on candle open, partial is empty
        if avail_i <= cur_start:
            elapsed = 0
            cur_fp = {
                "current_5m_fp_window_s": 0,
                "current_5m_fp_coverage_status": "EMPTY_AT_CANDLE_OPEN",
                "current_5m_fp_trade_count": 0,
                "current_5m_fp_buy_notional": 0.0,
                "current_5m_fp_sell_notional": 0.0,
                "current_5m_fp_delta_notional": 0.0,
                "current_5m_fp_active_seconds": 0,
                "current_5m_fp_no_trade_seconds": 0,
            }
            cur_avr = {"current_5m_avr_coverage_status": "EMPTY"}
            cur_oi = {"current_5m_oi_coverage_status": "EMPTY"}
            cur_liq = {"current_5m_liq_coverage_status": "EMPTY"}
        else:
            elapsed = avail_i - cur_start
            cur_fp = footprint_window_from_series(
                series, available_at=avail_i, window_s=elapsed, prefix="current_5m_fp"
            )
            cur_avr = aggregate_avr_persistence(
                avr_1s_ext, t_unix=float(avail_i), window_s=elapsed, prefix="current_5m_avr"
            )
            cur_oi = oi_window_from_states(
                state_by_ts, t_unix=float(avail_i), window_s=elapsed, prefix="current_5m"
            )
            cur_liq = liq_window_from_states(
                state_by_ts, t_unix=float(avail_i), window_s=elapsed, prefix="current_5m"
            )

        row.update(
            {
                "current_5m_is_partial": True,
                "current_5m_start": datetime.fromtimestamp(cur_start, tz=timezone.utc),
                "current_5m_start_unix": cur_start,
                "current_5m_end_exclusive": det,
                "current_5m_elapsed_seconds": elapsed,
                "current_5m_expected_seconds": CANDLE_5M_S,
                "current_5m_completion_frac": elapsed / float(CANDLE_5M_S),
            }
        )
        row.update(cur_fp)
        row.update(cur_avr)
        row.update(cur_oi)
        row.update(cur_liq)

        # Previous closed 5m
        closed_end = floor_to_5m(t)
        closed_start = closed_end - CANDLE_5M_S
        closed = build_candle_at(
            symbol=symbol,
            series=series,
            avr_1s=avr_1s_ext,
            state_by_ts=state_by_ts,
            candle_start=closed_start,
            candle_end=closed_end,
            is_complete=True,
            is_partial=False,
        )
        # namespace as previous_closed_5m_*
        for k, v in closed.items():
            if k in ("schema_version", "symbol", "is_complete", "is_partial"):
                continue
            row[f"previous_closed_5m_{k}"] = v
        row["previous_closed_5m_available_at"] = datetime.fromtimestamp(
            closed_end, tz=timezone.utc
        )
        row["previous_closed_5m_available"] = closed_end <= t

        rows.append(row)

    ctx = pd.DataFrame(rows)
    meta = {
        "adapter_version": ADAPTER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "config": cfg,
        "config_hash": cfg_hash,
        "provenance": provenance,
        "avr_meta": {k: avr_meta[k] for k in ("n_rows", "load_start_unix", "config_hash") if k in avr_meta},
        "n_episodes": int(len(ctx)),
        "n_5m_candles": int(len(candles)),
        "parity": preport,
        "join_version": JOIN_VERSION,
    }
    return {
        "episode_context": ctx,
        "candles_5m": candles,
        "parity": preport,
        "avr_1s_lookback": avr_1s_ext,
        "meta": meta,
    }
