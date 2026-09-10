"""Atomic partition writes for hourly state parquet + manifest."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .paths import DATA
from .schema_freeze import load_frozen_sha


def partition_dir(symbol: str, hour_start: datetime) -> Path:
    hour_start = hour_start.astimezone(timezone.utc)
    return (
        DATA
        / f"symbol={symbol.upper()}"
        / f"date={hour_start.strftime('%Y-%m-%d')}"
        / f"hour={hour_start.strftime('%H')}"
    )


def content_sha256_df(df: pd.DataFrame) -> str:
    # Stable hash over sorted columns / rows by state_ts
    cols = list(df.columns)
    payload = df.sort_values("state_ts")[cols].copy()
    for c in payload.columns:
        if pd.api.types.is_datetime64_any_dtype(payload[c]):
            payload[c] = pd.to_datetime(payload[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # round floats
    for c in payload.select_dtypes(include=["float", "float64", "Float64"]).columns:
        payload[c] = payload[c].map(lambda x: None if pd.isna(x) else round(float(x), 12))
    records = payload.to_dict(orient="records")
    blob = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def write_partition_atomic(
    *,
    symbol: str,
    hour_start: datetime,
    df: pd.DataFrame,
    manifest_extra: dict[str, Any],
) -> dict[str, Any]:
    out_dir = partition_dir(symbol, hour_start)
    out_dir.mkdir(parents=True, exist_ok=True)
    pq = out_dir / "state_1s.parquet"
    man = out_dir / "manifest.json"
    tmp_pq = out_dir / "state_1s.parquet.tmp"
    tmp_man = out_dir / "manifest.json.tmp"
    # cleanup stale tmp
    for t in (tmp_pq, tmp_man):
        if t.exists():
            t.unlink()
    sha = content_sha256_df(df)
    hour_end = hour_start.astimezone(timezone.utc).replace(microsecond=0)
    from datetime import timedelta
    hour_end = hour_start.astimezone(timezone.utc) + timedelta(hours=1)
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    df.to_parquet(tmp_pq, index=False)
    manifest = {
        "schema_version": "mb_state_1s_v1",
        "schema_sha256": load_frozen_sha(),
        "symbol": symbol.upper(),
        "window_start": hour_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_end": hour_end.isoformat().replace("+00:00", "Z"),
        "row_count": int(len(df)),
        "content_sha256": sha,
        "status": "COMPLETE",
        "build_started_at": manifest_extra.get("build_started_at", started),
        "build_finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **{k: v for k, v in manifest_extra.items() if k not in {"build_started_at"}},
    }
    tmp_man.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_pq, pq)
    os.replace(tmp_man, man)
    return manifest


def read_partition_manifest(symbol: str, hour_start: datetime) -> dict[str, Any] | None:
    man = partition_dir(symbol, hour_start) / "manifest.json"
    if not man.exists():
        return None
    return json.loads(man.read_text(encoding="utf-8"))


def partition_is_complete_identical(
    symbol: str,
    hour_start: datetime,
    *,
    expected_input_hashes: dict[str, Any],
    schema_sha: str,
) -> bool:
    man = read_partition_manifest(symbol, hour_start)
    if man is None or man.get("status") != "COMPLETE":
        return False
    if man.get("schema_sha256") != schema_sha:
        return False
    if man.get("row_count") != 3600:
        return False
    # compare key input identity
    for k, v in expected_input_hashes.items():
        if man.get(k) != v:
            return False
    pq = partition_dir(symbol, hour_start) / "state_1s.parquet"
    return pq.exists() and not (partition_dir(symbol, hour_start) / "state_1s.parquet.tmp").exists()
