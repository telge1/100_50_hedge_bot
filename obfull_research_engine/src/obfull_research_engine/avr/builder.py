"""Build AVR 1s series and episode context using Dashboard pure functions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .provenance import collect_provenance, ensure_avr_import_path


def _ch_client():
    """Read-only ClickHouse client — same config loader as Dashboard footprint smoke."""
    import clickhouse_connect

    ensure_avr_import_path()
    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())



def _direction_from_state(state: str) -> str:
    s = str(state)
    if s in {"BUYER_CONTROL", "BUY_ABSORPTION_CANDIDATE", "VACUUM_UP_PROXY"}:
        return "BULLISH"
    if s in {"SELLER_CONTROL", "SELL_ABSORPTION_CANDIDATE", "VACUUM_DOWN_PROXY"}:
        return "BEARISH"
    if s in {"BALANCED"}:
        return "NEUTRAL"
    return "UNCLEAR"


def _is_proxy(state: str, confirmation: str | None) -> bool:
    s = str(state)
    if "PROXY" in s or "CANDIDATE" in s:
        return True
    if confirmation == "PROXY_UNCONFIRMED":
        return True
    return False


def build_avr_state_1s(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    provenance: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Compute AVR classification for each completed second in [start, end)."""
    ensure_avr_import_path()
    from footprint_candles.response_baseline import build_baseline_from_feature_rows
    from footprint_candles.response_contracts import (
        BASELINE_LOOKBACK_S,
        DEFAULT_THRESHOLDS,
        PRIMARY_WINDOW_S,
        RESPONSE_ENGINE_VERSION,
        config_hash,
    )
    from footprint_candles.response_engine import (
        SecondSeries,
        classify_features,
        collect_baseline_rows,
    )
    from footprint_candles.response_service import (
        fetch_second_buckets,
        precompute_primary_features,
    )

    prov = provenance or collect_provenance()
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    start_u = int(start.timestamp())
    end_u = int(end.timestamp())
    preroll_start = start_u - int(BASELINE_LOOKBACK_S)
    # Load slightly earlier for acceleration/features at preroll edge
    load_start = preroll_start - int(PRIMARY_WINDOW_S)

    client = _ch_client()
    try:
        buckets = fetch_second_buckets(client, symbol, load_start, end_u)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    series = SecondSeries(buckets)
    thr = DEFAULT_THRESHOLDS
    feats_map = precompute_primary_features(series, load_start, end_u, thresholds=thr)

    by_sec = {int(b.second_ts): b for b in buckets}
    rows: list[dict[str, Any]] = []
    for state_ts in range(start_u, end_u):
        available_at = state_ts + 1
        bucket = by_sec.get(state_ts)
        feats = feats_map.get(available_at)
        quality_flags: list[str] = []
        if feats is None:
            cl = {
                "state": "INSUFFICIENT_DATA",
                "confirmation": None,
                "evidence": {"reason": "no_features"},
                "ranks": {},
                "strength": 0.0,
            }
            quality_flags.append("NO_FEATURES")
            baseline_ready = False
            warmup_status = "NO_FEATURES"
        else:
            bl_rows, valid_secs = collect_baseline_rows(
                series,
                available_at,
                lookback_s=BASELINE_LOOKBACK_S,
                window_s=int(thr.primary_window_s),
                thresholds=thr,
                precomputed_feats=feats_map,
            )
            baseline = build_baseline_from_feature_rows(
                bl_rows, valid_second_buckets=valid_secs
            )
            baseline_ready = bool(baseline.sufficient)
            warmup_status = "READY" if baseline_ready else "INSUFFICIENT_BASELINE"
            if not baseline_ready:
                quality_flags.append("INSUFFICIENT_BASELINE")
            if feats.get("insufficient_coverage"):
                quality_flags.append("INSUFFICIENT_WINDOW_COVERAGE")
            cl = classify_features(feats, baseline, thr)

        state = str(cl.get("state"))
        confirmation = cl.get("confirmation")
        evidence = cl.get("evidence") or {}
        ranks = cl.get("ranks") or {}
        contradiction: list[str] = []
        # Contradiction guard is encoded as absorption blocked; surface triggered notes
        trig = evidence.get("triggered") or []
        if isinstance(trig, list) and any("contradiction" in str(x).lower() for x in trig):
            contradiction.append("CONTRADICTION_GUARD")

        buy_n = float(bucket.buy_notional) if bucket else 0.0
        sell_n = float(bucket.sell_notional) if bucket else 0.0
        trade_count = int((bucket.buy_trade_count + bucket.sell_trade_count) if bucket else 0)

        rows.append(
            {
                "symbol": symbol,
                "state_ts": datetime.fromtimestamp(state_ts, tz=timezone.utc),
                "state_ts_unix": state_ts,
                "available_at": datetime.fromtimestamp(available_at, tz=timezone.utc),
                "available_at_unix": available_at,
                "avr_source_contract_version": RESPONSE_ENGINE_VERSION,
                "avr_config_hash": config_hash(thr),
                "avr_state": state,
                "avr_direction": _direction_from_state(state),
                "avr_is_proxy": _is_proxy(state, confirmation),
                "avr_confirmation": confirmation,
                "avr_strength": float(cl.get("strength") or 0.0),
                "avr_buy_notional": buy_n,
                "avr_sell_notional": sell_n,
                "avr_delta_notional": buy_n - sell_n,
                "avr_trade_count": trade_count,
                "avr_open": None if bucket is None else bucket.first_price,
                "avr_high": None if bucket is None else bucket.high_price,
                "avr_low": None if bucket is None else bucket.low_price,
                "avr_close": None if bucket is None else bucket.last_price,
                "avr_imbalance": (
                    (buy_n - sell_n) / (buy_n + sell_n) if (buy_n + sell_n) > 0 else 0.0
                ),
                "avr_price_velocity_bps": (
                    None
                    if feats is None
                    else feats.get("price_velocity_bps_per_second")
                ),
                "avr_up_velocity_percentile": ranks.get("up_velocity_percentile"),
                "avr_down_velocity_percentile": ranks.get("down_velocity_percentile"),
                "avr_response_bps": (
                    None
                    if feats is None
                    else (
                        feats.get("response_ratio_buy")
                        if _direction_from_state(state) == "BULLISH"
                        else feats.get("response_ratio_sell")
                    )
                ),
                "avr_efficiency": (
                    None
                    if feats is None
                    else (
                        feats.get("buy_efficiency")
                        if _direction_from_state(state) == "BULLISH"
                        else feats.get("sell_efficiency")
                    )
                ),
                "avr_evidence": json.dumps(evidence, sort_keys=True, default=str),
                "avr_ranks": json.dumps(ranks, sort_keys=True, default=str),
                "avr_contradiction_flags": contradiction,
                "avr_baseline_ready": baseline_ready if feats is not None else False,
                "avr_warmup_status": warmup_status if feats is not None else "NO_FEATURES",
                "avr_quality_flags": quality_flags,
                "window_buy_notional": None if feats is None else feats.get("buy_notional"),
                "window_sell_notional": None if feats is None else feats.get("sell_notional"),
                "rolling_window_s": int(thr.primary_window_s),
            }
        )

    df = pd.DataFrame(rows)
    meta = {
        "n_rows": int(len(df)),
        "n_buckets_loaded": len(buckets),
        "load_start_unix": load_start,
        "preroll_start_unix": preroll_start,
        "start_unix": start_u,
        "end_unix": end_u,
        "config_hash": config_hash(thr),
        "contract_version": RESPONSE_ENGINE_VERSION,
        "provenance": prov,
    }
    return df, meta


def enrich_episodes_with_avr(
    *,
    episodes: pd.DataFrame,
    avr_1s: pd.DataFrame,
    state_df: pd.DataFrame,
    provenance: dict[str, Any],
) -> pd.DataFrame:
    """One AVR context row per episode; causal join on available_at <= detection."""
    ensure_avr_import_path()
    from footprint_candles.response_contracts import RESPONSE_ENGINE_VERSION

    if avr_1s is None or avr_1s.empty:
        avr_sorted = avr_1s
        avr_avail = []
    else:
        avr_sorted = avr_1s.sort_values("available_at_unix").reset_index(drop=True)
        avr_avail = avr_sorted["available_at_unix"].to_numpy()

    # Index OB states by unix second of state_ts (available typically state_ts+1 for book,
    # but state_ts is the second label; use state_ts <= detection floor).
    state_by_ts: dict[int, pd.Series] = {}
    if state_df is not None and not state_df.empty:
        sdf = state_df.copy()
        sdf["_u"] = pd.to_datetime(sdf["state_ts"], utc=True).astype("int64") // 10**9
        for _, r in sdf.iterrows():
            state_by_ts[int(r["_u"])] = r

    import bisect

    out_rows: list[dict[str, Any]] = []
    for _, ep in episodes.iterrows():
        det = pd.to_datetime(ep["first_detection_available_at"], utc=True)
        det_unix = float(det.timestamp())
        episode_id = str(ep["episode_id"])

        avr_row = None
        if len(avr_avail):
            # rightmost available_at <= det_unix
            idx = bisect.bisect_right(avr_avail, det_unix) - 1
            if idx >= 0:
                avr_row = avr_sorted.iloc[idx]

        # OB state at last state_ts with state_ts < detection (causal 1s state)
        # Prefer state_ts <= floor(det)-epsilon: use largest state_ts with state_ts < det
        det_floor = int(det_unix)  # exclusive if we require state completed before det
        # state_ts S describes second [S,S+1); available conceptually at S+1.
        # For detection at D, use state with state_ts+1 <= D → state_ts <= D-1
        ob_state = None
        cand_ts = int(det_unix) - 1
        while cand_ts >= int(det_unix) - 5 and cand_ts not in state_by_ts:
            cand_ts -= 1
        # also try exact floor-1
        if cand_ts in state_by_ts:
            ob_state = state_by_ts[cand_ts]
        else:
            # fallback: latest state_ts < det
            prior = [t for t in state_by_ts if t < det_unix]
            if prior:
                ob_state = state_by_ts[max(prior)]

        base = {
            "episode_id": episode_id,
            "symbol": str(ep.get("symbol") or "").upper(),
            "schema_version": "avr_episode_context_v1",
            "episode_status_label": "RESEARCH_CONTEXT_ONLY",
            "first_detection_available_at": det,
            "ob_primary_behavior_type": str(ep.get("primary_behavior_type") or ""),
            "ob_direction_hint": str(ep.get("direction_hint") or ""),
            "ob_support_status": str(ep.get("support_status") or ""),
            "ob_primary_trigger_type": str(ep.get("primary_trigger_type") or ""),
            "candidate_count": int(ep.get("candidate_count") or 0),
            "avr_source_contract_version": provenance.get("avr_source_contract_version")
            or RESPONSE_ENGINE_VERSION,
            "avr_source_commit": provenance.get("srh_commit"),
            "avr_source_code_hash": provenance.get("avr_source_code_hash"),
            "avr_config_hash": provenance.get("avr_config_hash"),
        }

        # OI / liquidations from existing state only
        if ob_state is not None:
            base.update(
                {
                    "oi_value": float(ob_state["open_interest"])
                    if pd.notna(ob_state.get("open_interest"))
                    else None,
                    "oi_age_seconds": (
                        float(ob_state["open_interest_age_ms"]) / 1000.0
                        if pd.notna(ob_state.get("open_interest_age_ms"))
                        else None
                    ),
                    "oi_delta_1s": float(ob_state["open_interest_change_1s_or_last_valid"])
                    if pd.notna(ob_state.get("open_interest_change_1s_or_last_valid"))
                    else None,
                    "oi_delta_5s": None,  # not in mb_state_1s_v1 schema
                    "oi_delta_15s": None,
                    "oi_valid": bool(ob_state.get("oi_valid"))
                    if "oi_valid" in ob_state.index
                    else None,
                    "liquidation_long_notional_1s": float(
                        ob_state.get("long_liquidation_notional_usdt") or 0.0
                    ),
                    "liquidation_short_notional_1s": float(
                        ob_state.get("short_liquidation_notional_usdt") or 0.0
                    ),
                    "liquidation_count_1s": int(ob_state.get("liquidation_count") or 0),
                    "liquidations_valid": bool(ob_state.get("liquidations_valid"))
                    if "liquidations_valid" in ob_state.index
                    else None,
                    "ob_state_ts": pd.to_datetime(ob_state["state_ts"], utc=True),
                }
            )
        else:
            base.update(
                {
                    "oi_value": None,
                    "oi_age_seconds": None,
                    "oi_delta_1s": None,
                    "oi_delta_5s": None,
                    "oi_delta_15s": None,
                    "oi_valid": None,
                    "liquidation_long_notional_1s": None,
                    "liquidation_short_notional_1s": None,
                    "liquidation_count_1s": None,
                    "liquidations_valid": None,
                    "ob_state_ts": None,
                }
            )

        if avr_row is None:
            base.update(
                {
                    "avr_state_ts": None,
                    "avr_available_at": None,
                    "avr_age_ms": None,
                    "avr_state": None,
                    "avr_direction": None,
                    "avr_is_proxy": None,
                    "avr_quality_status": "NOT_AVAILABLE",
                    "avr_quality_flags": ["NO_VALID_AVR_BEFORE_DETECTION"],
                    "avr_buy_notional": None,
                    "avr_sell_notional": None,
                    "avr_delta_notional": None,
                    "avr_imbalance": None,
                    "avr_price_velocity_bps": None,
                    "avr_up_velocity_percentile": None,
                    "avr_down_velocity_percentile": None,
                    "avr_response_bps": None,
                    "avr_efficiency": None,
                    "avr_evidence": None,
                    "avr_contradiction_flags": [],
                    "avr_baseline_ready": False,
                    "avr_warmup_status": "NOT_AVAILABLE",
                }
            )
        else:
            age_ms = (det_unix - float(avr_row["available_at_unix"])) * 1000.0
            qflags = list(avr_row.get("avr_quality_flags") or [])
            qstatus = "OK"
            if avr_row.get("avr_warmup_status") == "INSUFFICIENT_BASELINE":
                qstatus = "INSUFFICIENT_BASELINE"
            elif str(avr_row.get("avr_state")) in {
                "INSUFFICIENT_DATA",
                "INSUFFICIENT_BASELINE",
            }:
                qstatus = str(avr_row.get("avr_state"))
            base.update(
                {
                    "avr_state_ts": avr_row["state_ts"],
                    "avr_available_at": avr_row["available_at"],
                    "avr_age_ms": round(age_ms, 3),
                    "avr_state": avr_row["avr_state"],
                    "avr_direction": avr_row["avr_direction"],
                    "avr_is_proxy": bool(avr_row["avr_is_proxy"]),
                    "avr_quality_status": qstatus,
                    "avr_quality_flags": qflags,
                    "avr_buy_notional": avr_row["avr_buy_notional"],
                    "avr_sell_notional": avr_row["avr_sell_notional"],
                    "avr_delta_notional": avr_row["avr_delta_notional"],
                    "avr_imbalance": avr_row["avr_imbalance"],
                    "avr_price_velocity_bps": avr_row["avr_price_velocity_bps"],
                    "avr_up_velocity_percentile": avr_row["avr_up_velocity_percentile"],
                    "avr_down_velocity_percentile": avr_row["avr_down_velocity_percentile"],
                    "avr_response_bps": avr_row["avr_response_bps"],
                    "avr_efficiency": avr_row["avr_efficiency"],
                    "avr_evidence": avr_row["avr_evidence"],
                    "avr_contradiction_flags": list(avr_row.get("avr_contradiction_flags") or []),
                    "avr_baseline_ready": bool(avr_row.get("avr_baseline_ready")),
                    "avr_warmup_status": avr_row.get("avr_warmup_status"),
                }
            )
            # Causality assert
            if float(avr_row["available_at_unix"]) > det_unix + 1e-9:
                raise RuntimeError("future_avr_join_bug")

        out_rows.append(base)

    return pd.DataFrame(out_rows)


def build_agreement_matrix(ctx: pd.DataFrame) -> pd.DataFrame:
    """Descriptive OB vs AVR direction matrix — no outcomes."""

    def _ob_bucket(d: str) -> str:
        d = str(d or "").upper()
        if d == "BULLISH":
            return "OB_BULLISH"
        if d == "BEARISH":
            return "OB_BEARISH"
        if d in {"UNCLEAR", "CONFLICTING", "NEUTRAL", ""}:
            return "OB_UNCLEAR"
        return "OB_UNCLEAR"

    def _avr_bucket(row) -> str:
        if row.get("avr_quality_status") == "NOT_AVAILABLE" or row.get("avr_state") is None:
            return "AVR_NOT_AVAILABLE"
        st = str(row.get("avr_state") or "")
        if st in {"INSUFFICIENT_DATA", "INSUFFICIENT_BASELINE"}:
            return "AVR_UNCLEAR_OR_PROXY"
        if bool(row.get("avr_is_proxy")):
            # still count direction but tag proxy separately in matrix columns
            pass
        d = str(row.get("avr_direction") or "")
        if d == "BULLISH":
            return "AVR_BULLISH" + ("_PROXY" if row.get("avr_is_proxy") else "")
        if d == "BEARISH":
            return "AVR_BEARISH" + ("_PROXY" if row.get("avr_is_proxy") else "")
        return "AVR_UNCLEAR_OR_PROXY"

    rows = []
    n = len(ctx)
    both_bull = both_bear = same = opposite = 0
    ob_unclear = avr_unclear = avr_na = 0
    for _, r in ctx.iterrows():
        ob = _ob_bucket(r.get("ob_direction_hint"))
        av = _avr_bucket(r)
        if ob == "OB_UNCLEAR":
            ob_unclear += 1
        if av == "AVR_NOT_AVAILABLE":
            avr_na += 1
        elif av == "AVR_UNCLEAR_OR_PROXY" or av.endswith("_PROXY") and "BULLISH" not in av and "BEARISH" not in av:
            avr_unclear += 1
        if "BULLISH" in ob and "BULLISH" in av:
            both_bull += 1
        if "BEARISH" in ob and "BEARISH" in av:
            both_bear += 1
        ob_dir = "BULLISH" if "BULLISH" in ob else ("BEARISH" if "BEARISH" in ob else None)
        av_dir = "BULLISH" if "BULLISH" in av else ("BEARISH" if "BEARISH" in av else None)
        if ob_dir and av_dir:
            if ob_dir == av_dir:
                same += 1
            else:
                opposite += 1
        rows.append({"ob_bucket": ob, "avr_bucket": av})

    summary = pd.DataFrame(
        [
            {"metric": "n_episodes", "value": n},
            {"metric": "ob_bullish_avr_bullish", "value": both_bull},
            {"metric": "ob_bearish_avr_bearish", "value": both_bear},
            {"metric": "same_direction", "value": same},
            {"metric": "opposite_direction", "value": opposite},
            {"metric": "ob_unclear", "value": ob_unclear},
            {"metric": "avr_unclear_or_proxy_or_insufficient", "value": avr_unclear},
            {"metric": "avr_not_available", "value": avr_na},
            {"metric": "note", "value": "DESCRIPTIVE_ONLY_NO_OUTCOMES"},
        ]
    )
    # also cross counts
    cross = pd.crosstab(
        pd.Series([r["ob_bucket"] for r in rows], name="ob"),
        pd.Series([r["avr_bucket"] for r in rows], name="avr"),
        dropna=False,
    )
    cross_reset = cross.reset_index()
    return summary, cross_reset
