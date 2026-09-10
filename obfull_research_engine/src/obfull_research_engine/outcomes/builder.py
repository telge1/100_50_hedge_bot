"""Build episode_outcome_v1 rows (research labels only)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .boundaries import classify_missing_point, classify_path_boundary
from .metrics import directional_mfe_mae, path_extremes_bps, return_bps
from .overlap import apply_all_horizons
from .prices import CausalPriceIndex, PricePoint


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def config_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _null_metrics() -> dict[str, Any]:
    return {
        "endpoint_mid": None,
        "endpoint_age_ms": None,
        "return_bps": None,
        "max_up_bps": None,
        "max_down_bps": None,
        "mfe_bps": None,
        "mae_bps": None,
        "realized_range_bps": None,
        "path_volatility_bps": None,
        "time_to_max_up_ms": None,
        "time_to_max_down_ms": None,
        "path_sample_count": 0,
        "path_coverage_pct": 0.0,
    }


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


def evaluate_horizon(
    *,
    episode: pd.Series,
    horizon_seconds: int,
    price_index: CausalPriceIndex,
    cfg: dict[str, Any],
    outcome_config_hash: str,
    source_episode_config_hash: str,
) -> dict[str, Any]:
    bucket_s = int(cfg.get("state_bucket_seconds", 1))
    max_anchor_age = int(cfg["max_anchor_age_ms"])
    max_endpoint_age = int(cfg["max_endpoint_age_ms"])
    min_cov = float(cfg.get("min_path_coverage_pct", 100.0))
    require_valid = bool(cfg.get("require_price_valid", True))

    det = pd.to_datetime(episode["first_detection_available_at"], utc=True)
    start_ts = pd.to_datetime(episode["episode_start_ts"], utc=True)
    horizon_end = det + timedelta(seconds=int(horizon_seconds))
    expected = int(horizon_seconds)

    base = {
        "episode_id": str(episode["episode_id"]),
        "symbol": str(episode["symbol"]).upper(),
        "schema_version": "episode_outcome_v1",
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
        "expected_path_sample_count": expected,
        "outcome_config_hash": outcome_config_hash,
        "source_episode_config_hash": source_episode_config_hash,
        "overlap_cluster_id": "",
        "overlap_count": 1,
        "proxy_flags": _as_list(episode.get("proxy_flags")),
        "quality_flags": list(_as_list(episode.get("quality_flags"))),
        "anchor_mid": None,
        "anchor_spread_bps": None,
        "anchor_price_available_at": None,
        "anchor_replay_epoch": None,
        **_null_metrics(),
        "outcome_status": "CENSORED",
        "censor_reason": None,
    }

    anchor = price_index.latest_at_or_before(det, max_age_ms=max_anchor_age, require_valid=require_valid)
    if anchor is None:
        base["outcome_status"] = "NO_VALID_ANCHOR"
        base["censor_reason"] = "MISSING_PRICE"
        base["quality_flags"] = sorted(set(base["quality_flags"] + ["NO_VALID_ANCHOR"]))
        return base

    # Causality assert: never after detection
    if anchor.available_at > det:
        base["outcome_status"] = "NO_VALID_ANCHOR"
        base["censor_reason"] = "INVALID_PRICE"
        base["quality_flags"] = sorted(set(base["quality_flags"] + ["ANCHOR_AFTER_DETECTION_REFUSED"]))
        return base

    base["anchor_mid"] = float(anchor.mid)
    base["anchor_spread_bps"] = anchor.spread_bps
    base["anchor_price_available_at"] = anchor.available_at
    base["anchor_replay_epoch"] = int(anchor.replay_epoch)

    # Source window: need last price availability covering horizon
    if price_index.last_available_at is None or horizon_end > price_index.last_available_at:
        # Still walk what exists to classify; if endpoint unreachable → SOURCE_WINDOW_END
        pass

    path_mids: list[float] = []
    path_ms: list[float] = []
    path_points: list[PricePoint] = []
    censor_reason: str | None = None
    qflags = list(base["quality_flags"])
    prev_pt: PricePoint | None = None

    for k in range(1, expected + 1):
        avail = det + timedelta(seconds=k)
        if avail > horizon_end:
            break
        pt = price_index.get_exact(avail)
        if pt is None:
            decision = classify_missing_point(
                requested_available_at=avail,
                last_available_at=price_index.last_available_at,
            )
            censor_reason = decision.censor_reason
            if decision.quality_flag:
                qflags.append(decision.quality_flag)
            break
        if require_valid and pt.price_valid != 1:
            censor_reason = "INVALID_PRICE"
            break
        decision = classify_path_boundary(
            prev=prev_pt,
            current=pt,
            anchor_replay_epoch=int(anchor.replay_epoch),
        )
        if decision.quality_flag:
            qflags.append(decision.quality_flag)
        if decision.is_hard:
            censor_reason = decision.censor_reason
            break
        path_mids.append(float(pt.mid))
        path_ms.append((avail - det).total_seconds() * 1000.0)
        path_points.append(pt)
        prev_pt = pt

    path_count = len(path_mids)
    coverage = 100.0 * path_count / max(expected, 1)
    base["path_sample_count"] = path_count
    base["path_coverage_pct"] = round(coverage, 6)
    base["quality_flags"] = sorted(set(qflags))

    if censor_reason is not None:
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = censor_reason
        return base

    if coverage + 1e-9 < min_cov:
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = "INSUFFICIENT_PATH_COVERAGE"
        base["quality_flags"] = sorted(set(base["quality_flags"] + ["PATH_COVERAGE_BELOW_MIN"]))
        return base

    endpoint = price_index.latest_at_or_before(
        horizon_end, max_age_ms=max_endpoint_age, require_valid=require_valid
    )
    if endpoint is None:
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = "STALE_ENDPOINT"
        return base
    if endpoint.available_at > horizon_end:
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = "INVALID_PRICE"
        base["quality_flags"] = sorted(set(base["quality_flags"] + ["ENDPOINT_AFTER_HORIZON_REFUSED"]))
        return base
    # Endpoint hard-boundary check uses the same classifier (not raw replay_epoch alone).
    endpoint_decision = classify_path_boundary(
        prev=path_points[-1] if path_points else None,
        current=endpoint,
        anchor_replay_epoch=int(anchor.replay_epoch),
    )
    if endpoint_decision.quality_flag:
        base["quality_flags"] = sorted(set(base["quality_flags"] + [endpoint_decision.quality_flag]))
    if endpoint_decision.is_hard:
        base["outcome_status"] = "CENSORED"
        base["censor_reason"] = endpoint_decision.censor_reason
        return base

    # Endpoint should be the last path sample when grid is complete
    if path_points and endpoint.available_at != path_points[-1].available_at:
        # Allow if endpoint equals last path; otherwise stale/mismatch
        if endpoint.available_at < path_points[-1].available_at:
            base["outcome_status"] = "CENSORED"
            base["censor_reason"] = "STALE_ENDPOINT"
            return base

    extremes = path_extremes_bps(path_mids, path_ms, anchor=float(anchor.mid))
    mfe, mae, mflags = directional_mfe_mae(
        str(episode.get("direction_hint") or ""),
        max_up_bps=extremes["max_up_bps"],
        max_down_bps=extremes["max_down_bps"],
    )
    base.update(
        {
            "endpoint_mid": float(endpoint.mid),
            "endpoint_age_ms": round((horizon_end - endpoint.available_at).total_seconds() * 1000.0, 6),
            "return_bps": float(return_bps(float(endpoint.mid), float(anchor.mid))),
            "max_up_bps": extremes["max_up_bps"],
            "max_down_bps": extremes["max_down_bps"],
            "mfe_bps": mfe,
            "mae_bps": mae,
            "realized_range_bps": extremes["realized_range_bps"],
            "path_volatility_bps": extremes["path_volatility_bps"],
            "time_to_max_up_ms": extremes["time_to_max_up_ms"],
            "time_to_max_down_ms": extremes["time_to_max_down_ms"],
            "outcome_status": "COMPLETE",
            "censor_reason": None,
            "quality_flags": sorted(set(base["quality_flags"] + mflags)),
        }
    )
    return base


def build_episode_outcomes(
    episodes: pd.DataFrame,
    *,
    price_index: CausalPriceIndex,
    cfg: dict[str, Any],
    outcome_config_hash: str,
    source_episode_config_hash: str,
) -> pd.DataFrame:
    horizons = [int(h) for h in cfg["horizons_seconds"]]
    rows: list[dict[str, Any]] = []
    ep = episodes.sort_values(["first_detection_available_at", "episode_id"]).reset_index(drop=True)
    for _, episode in ep.iterrows():
        for h in horizons:
            rows.append(
                evaluate_horizon(
                    episode=episode,
                    horizon_seconds=h,
                    price_index=price_index,
                    cfg=cfg,
                    outcome_config_hash=outcome_config_hash,
                    source_episode_config_hash=source_episode_config_hash,
                )
            )
    rows = apply_all_horizons(rows, horizons)
    df = pd.DataFrame(rows)
    # Stable column order
    preferred = [
        "episode_id",
        "symbol",
        "schema_version",
        "episode_start_ts",
        "first_detection_available_at",
        "outcome_anchor_ts",
        "horizon_seconds",
        "horizon_end_ts",
        "direction_hint",
        "support_status",
        "primary_trigger_type",
        "primary_behavior_type",
        "candidate_count",
        "anchor_mid",
        "anchor_spread_bps",
        "anchor_price_available_at",
        "anchor_replay_epoch",
        "endpoint_mid",
        "endpoint_age_ms",
        "return_bps",
        "max_up_bps",
        "max_down_bps",
        "mfe_bps",
        "mae_bps",
        "realized_range_bps",
        "path_volatility_bps",
        "time_to_max_up_ms",
        "time_to_max_down_ms",
        "path_sample_count",
        "expected_path_sample_count",
        "path_coverage_pct",
        "outcome_status",
        "censor_reason",
        "overlap_cluster_id",
        "overlap_count",
        "quality_flags",
        "proxy_flags",
        "outcome_config_hash",
        "source_episode_config_hash",
    ]
    cols = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    return df[cols].sort_values(["horizon_seconds", "outcome_anchor_ts", "episode_id"]).reset_index(drop=True)


def content_hash_outcomes(df: pd.DataFrame) -> str:
    payload = df.copy()
    for c in payload.columns:
        if pd.api.types.is_datetime64_any_dtype(payload[c]):
            payload[c] = pd.to_datetime(payload[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        elif payload[c].dtype == object:
            payload[c] = payload[c].map(lambda x: json.dumps(x, sort_keys=True, default=str) if isinstance(x, (list, dict)) else x)
    records = payload.to_dict(orient="records")
    blob = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def validate_row_contract(df: pd.DataFrame, *, n_episodes: int, horizons: list[int]) -> dict[str, Any]:
    expected = int(n_episodes) * len(horizons)
    ok = len(df) == expected
    # each episode × horizon once
    dup = df.duplicated(subset=["episode_id", "horizon_seconds"]).sum()
    return {
        "ok": bool(ok and dup == 0),
        "n_rows": int(len(df)),
        "expected_rows": expected,
        "n_episodes": int(n_episodes),
        "n_horizons": len(horizons),
        "duplicate_pairs": int(dup),
    }
