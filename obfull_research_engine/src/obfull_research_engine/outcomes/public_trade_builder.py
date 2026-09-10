"""Build episode_outcome_public_trade rows (research labels only).

Supports:
- STRICT_1000MS_V1 (config episode_outcome_public_trade_v1.json)
- LAST_TRADE_CARRY_WITH_SOURCE_GAP_GUARD_V1 (config ..._v1_1.json)
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .metrics import directional_mfe_mae, path_extremes_bps, return_bps
from .overlap import apply_all_horizons
from .public_trade_coverage import (
    SOURCE_COVERAGE_UNKNOWN,
    SOURCE_GAP_CONFIRMED,
    age_quality_flags,
    assess_source_coverage,
)
from .public_trade_index import PublicTradeIndex


def load_pt_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pt_config_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_carry_policy(cfg: dict[str, Any]) -> bool:
    policy = str(cfg.get("price_policy") or cfg.get("price_policy_version") or "")
    return bool(cfg.get("carry_allowed")) or policy == "LAST_TRADE_CARRY_WITH_SOURCE_GAP_GUARD_V1"


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    if isinstance(v, str):
        try:
            x = json.loads(v)
            return list(x) if isinstance(x, list) else [v]
        except json.JSONDecodeError:
            return [v]
    return list(v)


def _null_trade_fields() -> dict[str, Any]:
    return {
        "anchor_trade_ts": None,
        "anchor_trade_id": None,
        "anchor_trade_price": None,
        "anchor_trade_age_ms": None,
        "anchor_carried_forward": False,
        "anchor_source_coverage_status": None,
        "endpoint_trade_ts": None,
        "endpoint_trade_id": None,
        "endpoint_trade_price": None,
        "endpoint_trade_age_ms": None,
        "endpoint_carried_forward": False,
        "endpoint_source_coverage_status": None,
        "price_age_quality_class": None,
        "return_bps": None,
        "max_up_bps": None,
        "max_down_bps": None,
        "mfe_bps": None,
        "mae_bps": None,
        "realized_range_bps": None,
        "time_to_max_up_ms": None,
        "time_to_max_down_ms": None,
        "path_trade_count": 0,
        "path_unique_trade_count": 0,
        "path_first_trade_ts": None,
        "path_last_trade_ts": None,
        "path_volatility_bps": None,
    }


def evaluate_pt_horizon(
    *,
    episode: pd.Series,
    horizon_seconds: int,
    trade_index: PublicTradeIndex,
    cfg: dict[str, Any],
    outcome_config_hash: str,
    source_episode_config_hash: str,
    full_ob_post_detection_gap: bool = False,
    load_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    carry = is_carry_policy(cfg)
    hard_stale = int(cfg.get("hard_stale_price_ms") or cfg.get("max_anchor_trade_age_ms") or 1000)
    max_anchor_age = int(cfg["max_anchor_trade_age_ms"])
    max_endpoint_age = int(cfg["max_endpoint_trade_age_ms"])
    if not carry:
        hard_stale = max(max_anchor_age, max_endpoint_age)

    det = pd.to_datetime(episode["first_detection_available_at"], utc=True)
    start_ts = pd.to_datetime(episode["episode_start_ts"], utc=True)
    horizon_end = det + timedelta(seconds=int(horizon_seconds))

    price_policy_version = str(
        cfg.get("price_policy_version")
        or cfg.get("price_policy")
        or ("LAST_TRADE_CARRY_WITH_SOURCE_GAP_GUARD_V1" if carry else "STRICT_1000MS_V1")
    )

    base: dict[str, Any] = {
        "episode_id": str(episode["episode_id"]),
        "symbol": str(episode["symbol"]).upper(),
        "schema_version": "episode_outcome_public_trade_v1",
        "episode_status_label": "RESEARCH_LABEL_ONLY",
        "episode_start_ts": start_ts,
        "first_detection_available_at": det,
        "outcome_anchor_ts": det,
        "horizon_seconds": int(horizon_seconds),
        "horizon_end_ts": horizon_end,
        "direction_hint": str(episode.get("direction_hint") or "UNCLEAR"),
        "support_status": str(episode.get("support_status") or "NOT_EVALUATED"),
        "primary_trigger_type": str(episode.get("primary_trigger_type") or ""),
        "primary_behavior_type": str(episode.get("primary_behavior_type") or ""),
        "candidate_count": int(episode.get("candidate_count") or 0),
        "outcome_config_hash": outcome_config_hash,
        "source_episode_config_hash": source_episode_config_hash,
        "overlap_cluster_id": "",
        "overlap_count": 1,
        "proxy_flags": _as_list(episode.get("proxy_flags")),
        "quality_flags": list(_as_list(episode.get("quality_flags"))),
        "price_source": "BYBIT_PUBLIC_TRADES",
        "price_contract_version": str(
            cfg.get("price_contract_version") or "PUBLIC_TRADE_EPISODE_OUTCOME_PRICE_CONTRACT_V1"
        ),
        "price_policy_version": price_policy_version,
        "public_trade_coverage_status": "UNKNOWN",
        "outcome_status": "CENSORED",
        "censor_reason": None,
        **_null_trade_fields(),
    }
    qflags = list(base["quality_flags"])
    if full_ob_post_detection_gap:
        qflags.append("FULL_OB_POST_DETECTION_GAP_CONTEXT")
    if carry:
        qflags.append("INGEST_TIMESTAMP_NOT_RECEIVE_TIME")

    if trade_index.last_trade_ts is None:
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = "PUBLIC_TRADE_SOURCE_END" if carry else "SOURCE_WINDOW_END"
        base["public_trade_coverage_status"] = "SOURCE_EMPTY"
        base["quality_flags"] = sorted(set(qflags))
        return base

    anchor, a_rej = trade_index.latest_before(det, max_age_ms=10**12, strict=True)
    if a_rej == "MISSING" or anchor is None:
        base["outcome_status"] = "NO_VALID_ANCHOR"
        base["censor_reason"] = "MISSING_PRICE"
        base["public_trade_coverage_status"] = "NO_ANCHOR_TRADE"
        base["quality_flags"] = sorted(set(qflags + ["NO_VALID_ANCHOR"]))
        return base
    if anchor.trade_ts >= det:
        base["outcome_status"] = "NO_VALID_ANCHOR"
        base["censor_reason"] = "INVALID_PRICE"
        base["quality_flags"] = sorted(set(qflags + ["ANCHOR_AFTER_DETECTION_REFUSED"]))
        return base

    anchor_age = (det - anchor.trade_ts).total_seconds() * 1000.0
    base["anchor_trade_ts"] = anchor.trade_ts
    base["anchor_trade_id"] = anchor.trade_id
    base["anchor_trade_age_ms"] = round(anchor_age, 6)

    cov_a = assess_source_coverage(
        target_ts=det,
        last_trade_ts=anchor.trade_ts,
        trade_index=trade_index,
        load_meta=load_meta,
    )
    base["anchor_source_coverage_status"] = cov_a.status

    if anchor_age > float(hard_stale):
        base["outcome_status"] = "NO_VALID_ANCHOR"
        base["censor_reason"] = "HARD_STALE_PRICE_GT_60S" if carry else "MISSING_PRICE"
        base["public_trade_coverage_status"] = "HARD_STALE_ANCHOR"
        base["quality_flags"] = sorted(set(qflags + ["NO_VALID_ANCHOR", "HARD_STALE_PRICE"]))
        return base

    if not carry:
        if anchor_age > float(max_anchor_age):
            base["outcome_status"] = "NO_VALID_ANCHOR"
            base["censor_reason"] = "MISSING_PRICE"
            base["public_trade_coverage_status"] = "STALE_ANCHOR"
            base["quality_flags"] = sorted(set(qflags + ["NO_VALID_ANCHOR", "STALE_ANCHOR_TRADE"]))
            return base
    else:
        if cov_a.status == SOURCE_GAP_CONFIRMED:
            base["outcome_status"] = "NO_VALID_ANCHOR"
            base["censor_reason"] = "PUBLIC_TRADE_SOURCE_GAP"
            base["public_trade_coverage_status"] = SOURCE_GAP_CONFIRMED
            base["quality_flags"] = sorted(set(qflags))
            return base
        if cov_a.status == SOURCE_COVERAGE_UNKNOWN:
            base["outcome_status"] = "NO_VALID_ANCHOR"
            base["censor_reason"] = "PUBLIC_TRADE_COVERAGE_UNKNOWN"
            base["public_trade_coverage_status"] = SOURCE_COVERAGE_UNKNOWN
            base["quality_flags"] = sorted(set(qflags))
            return base
        qflags.append("PUBLIC_TRADE_SOURCE_COVERAGE_CONFIRMED")
        a_flags, a_cls = age_quality_flags(anchor_age)
        qflags.extend(a_flags)
        base["price_age_quality_class"] = a_cls
        if anchor_age > 1000:
            base["anchor_carried_forward"] = True
            qflags.append("LAST_TRADE_CARRIED_FORWARD")

    if anchor.ingest_timestamp is not None and anchor.ingest_timestamp > det:
        qflags.append("COLLECTOR_INGEST_AFTER_DETECTION")

    base["anchor_trade_price"] = float(anchor.price)

    path = trade_index.path_slice(det, horizon_end)
    path_ids = [t.trade_id for t in path]
    base["path_trade_count"] = len(path)
    base["path_unique_trade_count"] = len(set(path_ids))
    if path:
        base["path_first_trade_ts"] = path[0].trade_ts
        base["path_last_trade_ts"] = path[-1].trade_ts

    endpoint, e_rej = trade_index.latest_before(horizon_end, max_age_ms=10**12, strict=False)
    if endpoint is None or e_rej == "MISSING":
        if trade_index.last_trade_ts is not None and horizon_end > trade_index.last_trade_ts:
            base["outcome_status"] = "CENSORED"
            base["censor_reason"] = "PUBLIC_TRADE_SOURCE_END" if carry else "SOURCE_WINDOW_END"
            base["public_trade_coverage_status"] = "SOURCE_WINDOW_END"
        else:
            base["outcome_status"] = "CENSORED"
            base["censor_reason"] = "STALE_OR_MISSING_ENDPOINT_TRADE"
            base["public_trade_coverage_status"] = "MISSING_ENDPOINT"
        base["quality_flags"] = sorted(set(qflags))
        return base
    if endpoint.trade_ts > horizon_end:
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = "INVALID_PRICE"
        base["quality_flags"] = sorted(set(qflags + ["ENDPOINT_AFTER_HORIZON_REFUSED"]))
        return base

    endpoint_age = (horizon_end - endpoint.trade_ts).total_seconds() * 1000.0
    base["endpoint_trade_ts"] = endpoint.trade_ts
    base["endpoint_trade_id"] = endpoint.trade_id
    base["endpoint_trade_age_ms"] = round(endpoint_age, 6)

    cov_e = assess_source_coverage(
        target_ts=horizon_end,
        last_trade_ts=endpoint.trade_ts,
        trade_index=trade_index,
        load_meta=load_meta,
    )
    base["endpoint_source_coverage_status"] = cov_e.status

    if endpoint_age > float(hard_stale):
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = "HARD_STALE_PRICE_GT_60S" if carry else "STALE_OR_MISSING_ENDPOINT_TRADE"
        base["public_trade_coverage_status"] = "HARD_STALE_ENDPOINT"
        base["quality_flags"] = sorted(set(qflags + ["HARD_STALE_PRICE"]))
        return base

    if not carry:
        if endpoint_age > float(max_endpoint_age):
            base["outcome_status"] = "CENSORED"
            base["censor_reason"] = "STALE_OR_MISSING_ENDPOINT_TRADE"
            base["public_trade_coverage_status"] = "STALE_ENDPOINT"
            base["quality_flags"] = sorted(set(qflags + ["STALE_ENDPOINT_TRADE"]))
            return base
        if not path:
            base["outcome_status"] = "CENSORED"
            base["censor_reason"] = "NO_PATH_TRADES"
            base["public_trade_coverage_status"] = "NO_PATH_TRADES"
            base["quality_flags"] = sorted(set(qflags))
            return base
    else:
        if cov_e.status == SOURCE_GAP_CONFIRMED:
            base["outcome_status"] = "CENSORED"
            base["censor_reason"] = "PUBLIC_TRADE_SOURCE_GAP"
            base["public_trade_coverage_status"] = SOURCE_GAP_CONFIRMED
            base["quality_flags"] = sorted(set(qflags))
            return base
        if cov_e.status == SOURCE_COVERAGE_UNKNOWN:
            base["outcome_status"] = "CENSORED"
            base["censor_reason"] = "PUBLIC_TRADE_COVERAGE_UNKNOWN"
            base["public_trade_coverage_status"] = SOURCE_COVERAGE_UNKNOWN
            base["quality_flags"] = sorted(set(qflags))
            return base
        e_flags, e_cls = age_quality_flags(endpoint_age)
        qflags.extend(e_flags)
        order = [
            "AGE_LE_1S",
            "AGE_GT_1S_LE_5S",
            "AGE_GT_5S_LE_10S",
            "AGE_GT_10S_LE_30S",
            "AGE_GT_30S_LE_60S",
            "AGE_GT_60S",
        ]
        cur = base.get("price_age_quality_class") or "AGE_LE_1S"
        if order.index(e_cls) > order.index(str(cur)):
            base["price_age_quality_class"] = e_cls
        if endpoint_age > 1000:
            base["endpoint_carried_forward"] = True
            qflags.append("LAST_TRADE_CARRIED_FORWARD")

    base["endpoint_trade_price"] = float(endpoint.price)

    if path:
        prices = [float(t.price) for t in path]
        ms = [(t.trade_ts - det).total_seconds() * 1000.0 for t in path]
        extremes = path_extremes_bps(prices, ms, anchor=float(anchor.price))
    else:
        extremes = {
            "max_up_bps": 0.0,
            "max_down_bps": 0.0,
            "time_to_max_up_ms": 0.0,
            "time_to_max_down_ms": 0.0,
            "realized_range_bps": 0.0,
            "path_volatility_bps": None,
        }
        qflags.append("NO_NEW_TRADES_IN_HORIZON")

    mfe, mae, mflags = directional_mfe_mae(
        str(episode.get("direction_hint") or ""),
        max_up_bps=extremes["max_up_bps"],
        max_down_bps=extremes["max_down_bps"],
    )
    qflags.extend(mflags)

    base.update(
        {
            "return_bps": float(return_bps(float(endpoint.price), float(anchor.price))),
            "max_up_bps": extremes["max_up_bps"],
            "max_down_bps": extremes["max_down_bps"],
            "mfe_bps": mfe,
            "mae_bps": mae,
            "realized_range_bps": extremes["realized_range_bps"],
            "time_to_max_up_ms": extremes["time_to_max_up_ms"],
            "time_to_max_down_ms": extremes["time_to_max_down_ms"],
            "path_volatility_bps": extremes["path_volatility_bps"],
            "outcome_status": "COMPLETE",
            "censor_reason": None,
            "public_trade_coverage_status": "COMPLETE",
            "quality_flags": sorted(set(qflags)),
        }
    )
    return base


def build_pt_episode_outcomes(
    episodes: pd.DataFrame,
    *,
    trade_index: PublicTradeIndex,
    cfg: dict[str, Any],
    outcome_config_hash: str,
    source_episode_config_hash: str,
    full_ob_post_detection_gap: bool = False,
    load_meta: dict[str, Any] | None = None,
) -> pd.DataFrame:
    horizons = [int(h) for h in cfg["horizons_seconds"]]
    rows: list[dict[str, Any]] = []
    ep = episodes.sort_values(["first_detection_available_at", "episode_id"]).reset_index(drop=True)
    for _, episode in ep.iterrows():
        for h in horizons:
            rows.append(
                evaluate_pt_horizon(
                    episode=episode,
                    horizon_seconds=h,
                    trade_index=trade_index,
                    cfg=cfg,
                    outcome_config_hash=outcome_config_hash,
                    source_episode_config_hash=source_episode_config_hash,
                    full_ob_post_detection_gap=full_ob_post_detection_gap,
                    load_meta=load_meta,
                )
            )
    rows = apply_all_horizons(rows, horizons)
    df = pd.DataFrame(rows)
    preferred = [
        "episode_id",
        "symbol",
        "schema_version",
        "episode_start_ts",
        "first_detection_available_at",
        "outcome_anchor_ts",
        "anchor_trade_ts",
        "anchor_trade_id",
        "anchor_trade_price",
        "anchor_trade_age_ms",
        "anchor_carried_forward",
        "anchor_source_coverage_status",
        "horizon_seconds",
        "horizon_end_ts",
        "endpoint_trade_ts",
        "endpoint_trade_id",
        "endpoint_trade_price",
        "endpoint_trade_age_ms",
        "endpoint_carried_forward",
        "endpoint_source_coverage_status",
        "return_bps",
        "max_up_bps",
        "max_down_bps",
        "mfe_bps",
        "mae_bps",
        "realized_range_bps",
        "time_to_max_up_ms",
        "time_to_max_down_ms",
        "path_trade_count",
        "path_unique_trade_count",
        "path_first_trade_ts",
        "path_last_trade_ts",
        "public_trade_coverage_status",
        "outcome_status",
        "censor_reason",
        "price_age_quality_class",
        "direction_hint",
        "support_status",
        "overlap_cluster_id",
        "overlap_count",
        "quality_flags",
        "proxy_flags",
        "price_source",
        "price_contract_version",
        "price_policy_version",
        "outcome_config_hash",
        "source_episode_config_hash",
    ]
    cols = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    return df[cols].sort_values(["horizon_seconds", "outcome_anchor_ts", "episode_id"]).reset_index(drop=True)


def content_hash_pt_outcomes(df: pd.DataFrame) -> str:
    payload = df.copy()
    for c in payload.columns:
        if pd.api.types.is_datetime64_any_dtype(payload[c]):
            payload[c] = pd.to_datetime(payload[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        elif payload[c].dtype == object:
            payload[c] = payload[c].map(
                lambda x: json.dumps(x, sort_keys=True, default=str) if isinstance(x, (list, dict)) else x
            )
    records = payload.to_dict(orient="records")
    blob = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def validate_pt_row_contract(df: pd.DataFrame, *, n_episodes: int, horizons: list[int]) -> dict[str, Any]:
    expected = int(n_episodes) * len(horizons)
    dup = df.duplicated(subset=["episode_id", "horizon_seconds"]).sum()
    mid_leak = [c for c in df.columns if c in {"mid_price", "anchor_mid", "endpoint_mid"}]
    bad_src = int((df["price_source"] != "BYBIT_PUBLIC_TRADES").sum()) if "price_source" in df.columns else -1
    return {
        "ok": bool(len(df) == expected and dup == 0 and not mid_leak and bad_src == 0),
        "n_rows": int(len(df)),
        "expected_rows": expected,
        "duplicate_pairs": int(dup),
        "mid_columns_present": mid_leak,
        "non_pt_price_source_rows": bad_src,
    }
