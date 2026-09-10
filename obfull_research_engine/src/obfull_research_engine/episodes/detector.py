"""Episode candidate detector V1 — causal, outcome-blind."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .baseline import contiguous_suffix_after_gaps, validate_baseline_length
from .compositions import evaluate_compositions
from .cooldown import CooldownBook
from .triggers import evaluate_primary_triggers


PRIMARY_DIRECTION = {
    "BUY_AGGRESSION_BURST": "UP_PRESSURE",
    "SELL_AGGRESSION_BURST": "DOWN_PRESSURE",
    "TAKER_DELTA_POSITIVE_EXTREME": "UP_PRESSURE",
    "TAKER_DELTA_NEGATIVE_EXTREME": "DOWN_PRESSURE",
    "BID_LIQUIDITY_ADD_BURST": "UP_PRESSURE",
    "ASK_LIQUIDITY_ADD_BURST": "DOWN_PRESSURE",
    "BID_LIQUIDITY_REMOVE_BURST": "DOWN_PRESSURE",
    "ASK_LIQUIDITY_REMOVE_BURST": "UP_PRESSURE",
    "BID_IMBALANCE_SHIFT": "UP_PRESSURE",
    "ASK_IMBALANCE_SHIFT": "DOWN_PRESSURE",
    "PRICE_UP_RESPONSE_EXTREME": "UP_PRESSURE",
    "PRICE_DOWN_RESPONSE_EXTREME": "DOWN_PRESSURE",
    "HIGH_VOLUME_LOW_PRICE_RESPONSE": "UNCLEAR",
    "LOW_VISIBLE_RESISTANCE_HIGH_RESPONSE": "UNCLEAR",
    "OI_CHANGE_BURST": "UNCLEAR",
    "LONG_LIQUIDATION_BURST": "DOWN_PRESSURE",
    "SHORT_LIQUIDATION_BURST": "UP_PRESSURE",
}


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def config_sha256(cfg: dict[str, Any] | Path) -> str:
    if isinstance(cfg, Path):
        blob = cfg.read_bytes()
    else:
        blob = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def candidate_id(symbol: str, candidate_type: str, trigger_ts: pd.Timestamp, threshold_version: str) -> str:
    ts = pd.to_datetime(trigger_ts, utc=True).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = f"{symbol}|{candidate_type}|{ts}|{threshold_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def assert_contiguous_seconds(df: pd.DataFrame) -> None:
    if df.empty:
        raise ValueError("empty_state_frame")
    ts = pd.to_datetime(df["state_ts"], utc=True)
    diffs = ts.diff().dt.total_seconds().iloc[1:]
    bad = diffs[diffs != 1.0]
    if len(bad):
        raise ValueError(f"non_contiguous_state_seconds count={len(bad)} first={bad.index[0]}")


def detect_candidates(
    df: pd.DataFrame,
    *,
    cfg: dict[str, Any],
    symbol: str,
    coverage_status: str,
    source_state_hash: str,
    replay_epoch: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Run causal detector. Returns candidates + diagnostics (no future peeking)."""
    df = df.sort_values("state_ts").reset_index(drop=True).copy()
    assert_contiguous_seconds(df)

    lookback = int(cfg["baseline_lookback_seconds"])
    min_base = int(cfg["minimum_baseline_seconds"])
    cooldown = CooldownBook(int(cfg["cooldown_seconds"]))
    emit_primary = bool(cfg.get("emit_primary_as_candidates", True))
    primary_prefix = str(cfg.get("primary_candidate_prefix", "PRIMARY_"))

    primary_history: list[dict[str, bool]] = []
    rows: list[dict[str, Any]] = []
    baseline_diag: list[dict[str, Any]] = []
    trigger_fire_counts: dict[str, int] = {k: 0 for k in cfg["primary_triggers"]}
    candidate_counts: dict[str, int] = {}

    for i, row in df.iterrows():
        i = int(i)
        base = validate_baseline_length(
            contiguous_suffix_after_gaps(df["state_ts"], i, lookback),
            min_base,
        )
        ts = pd.to_datetime(row["state_ts"], utc=True)
        baseline_diag.append(
            {
                "state_ts": ts.isoformat().replace("+00:00", "Z"),
                "baseline_valid": bool(base.valid),
                "baseline_n": int(base.n),
                "baseline_start_ts": None
                if pd.isna(base.start_ts)
                else pd.to_datetime(base.start_ts, utc=True).isoformat().replace("+00:00", "Z"),
                "baseline_end_ts": None
                if pd.isna(base.end_ts)
                else pd.to_datetime(base.end_ts, utc=True).isoformat().replace("+00:00", "Z"),
                "invalid_reason": base.invalid_reason,
            }
        )

        if not base.valid:
            primary_history.append({k: False for k in cfg["primary_triggers"]})
            continue

        if int(row.get("feature_row_valid", 0)) != 1:
            primary_history.append({k: False for k in cfg["primary_triggers"]})
            continue

        prim = evaluate_primary_triggers(df=df, i=i, row=row, baseline=base, cfg=cfg)
        fired_map = {k: bool(v.get("fired")) for k, v in prim.items()}
        primary_history.append(fired_map)
        for k, v in fired_map.items():
            if v:
                trigger_fire_counts[k] = trigger_fire_counts.get(k, 0) + 1

        # Emit primary candidates
        if emit_primary:
            for name, detail in prim.items():
                if not detail.get("fired"):
                    continue
                ctype = f"{primary_prefix}{name}"
                if not cooldown.allow(ctype, ts.to_pydatetime()):
                    continue
                cooldown.register(ctype, ts.to_pydatetime())
                snap = {
                    "primary": name,
                    "detail": {kk: vv for kk, vv in detail.items() if kk != "detail"},
                }
                cand = _make_candidate(
                    cfg=cfg,
                    symbol=symbol,
                    candidate_type=ctype,
                    trigger_ts=ts,
                    baseline=base,
                    coverage_status=coverage_status,
                    source_state_hash=source_state_hash,
                    replay_epoch=replay_epoch,
                    trigger_names=[name],
                    primary_trigger=name,
                    direction_hint=PRIMARY_DIRECTION.get(name, "UNCLEAR"),
                    exact_fields=[name],
                    proxy_fields=[],
                    feature_snapshot=snap,
                    context_start_ts=pd.to_datetime(base.start_ts, utc=True),
                )
                rows.append(cand)
                candidate_counts[ctype] = candidate_counts.get(ctype, 0) + 1

        comps = evaluate_compositions(
            df=df,
            i=i,
            row=row,
            baseline=base,
            cfg=cfg,
            primary_history=primary_history,
            primary_now=fired_map,
        )
        for cname, cdetail in comps.items():
            if not cdetail.get("fired"):
                continue
            if not cooldown.allow(cname, ts.to_pydatetime()):
                continue
            cooldown.register(cname, ts.to_pydatetime())
            ctx_start = ts - pd.Timedelta(seconds=int(cfg["composition_window_seconds"]) - 1)
            if not pd.isna(base.start_ts):
                ctx_start = max(ctx_start, pd.to_datetime(base.start_ts, utc=True))
            cand = _make_candidate(
                cfg=cfg,
                symbol=symbol,
                candidate_type=cname,
                trigger_ts=ts,
                baseline=base,
                coverage_status=coverage_status,
                source_state_hash=source_state_hash,
                replay_epoch=replay_epoch,
                trigger_names=list(cdetail.get("trigger_names") or []),
                primary_trigger=(cdetail.get("trigger_names") or [None])[0],
                direction_hint=cdetail.get("direction_hint") or "UNCLEAR",
                exact_fields=list(cdetail.get("exact_fields") or []),
                proxy_fields=list(cdetail.get("proxy_fields") or []),
                feature_snapshot={"composition": cname, "primaries_in_window": cdetail.get("trigger_names")},
                context_start_ts=ctx_start,
            )
            rows.append(cand)
            candidate_counts[cname] = candidate_counts.get(cname, 0) + 1

    out_df = pd.DataFrame(rows)
    if not out_df.empty:
        out_df = out_df.sort_values(["trigger_ts", "candidate_type"]).reset_index(drop=True)

    diagnostics = {
        "baseline_diagnostics": baseline_diag,
        "trigger_fire_counts": trigger_fire_counts,
        "candidate_counts": candidate_counts,
        "cooldown": cooldown.snapshot(),
        "n_state_rows": int(len(df)),
        "n_candidates": int(len(out_df)),
    }
    return out_df, diagnostics


def _make_candidate(
    *,
    cfg: dict[str, Any],
    symbol: str,
    candidate_type: str,
    trigger_ts: pd.Timestamp,
    baseline,
    coverage_status: str,
    source_state_hash: str,
    replay_epoch: int,
    trigger_names: list[str],
    primary_trigger: str | None,
    direction_hint: str,
    exact_fields: list[str],
    proxy_fields: list[str],
    feature_snapshot: dict[str, Any],
    context_start_ts: pd.Timestamp,
) -> dict[str, Any]:
    thr_v = cfg["threshold_version"]
    ts = pd.to_datetime(trigger_ts, utc=True)
    ctx = pd.to_datetime(context_start_ts, utc=True)
    if ctx > ts:
        ctx = ts
    return {
        "schema_version": cfg["schema_version"],
        "candidate_id": candidate_id(symbol, candidate_type, ts, thr_v),
        "symbol": symbol,
        "candidate_type": candidate_type,
        "candidate_status": cfg["candidate_status"],
        "context_start_ts": ctx,
        "trigger_ts": ts,
        "detection_available_at": ts,
        "baseline_start_ts": pd.to_datetime(baseline.start_ts, utc=True),
        "baseline_end_ts": pd.to_datetime(baseline.end_ts, utc=True),
        "baseline_valid": bool(baseline.valid),
        "coverage_status": coverage_status,
        "replay_epoch": int(replay_epoch),
        "source_state_schema": cfg["source_state_schema"],
        "source_state_hash": source_state_hash,
        "trigger_names": list(trigger_names),
        "trigger_count": int(len(trigger_names)),
        "primary_trigger": primary_trigger,
        "direction_hint": direction_hint,
        "confidence_kind": cfg["confidence_kind"],
        "threshold_version": thr_v,
        "threshold_source": cfg["threshold_source"],
        "feature_snapshot_json": json.dumps(feature_snapshot, sort_keys=True, default=str),
        "proxy_fields": list(proxy_fields),
        "exact_fields": list(exact_fields),
        "quality_valid": True,
        "invalid_reason": None,
    }


def candidates_content_sha256(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        payload = b"[]"
    else:
        cols = [
            "candidate_id",
            "symbol",
            "candidate_type",
            "trigger_ts",
            "context_start_ts",
            "detection_available_at",
            "baseline_start_ts",
            "baseline_end_ts",
            "trigger_names",
            "primary_trigger",
            "direction_hint",
            "threshold_version",
            "feature_snapshot_json",
            "proxy_fields",
            "exact_fields",
        ]
        use = [c for c in cols if c in df.columns]
        payload_df = df[use].copy()
        for c in payload_df.columns:
            if pd.api.types.is_datetime64_any_dtype(payload_df[c]):
                payload_df[c] = pd.to_datetime(payload_df[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            elif payload_df[c].dtype == object:
                payload_df[c] = payload_df[c].map(lambda x: json.dumps(x, sort_keys=True, default=str) if isinstance(x, (list, dict)) else x)
        records = payload_df.to_dict(orient="records")
        payload = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()
