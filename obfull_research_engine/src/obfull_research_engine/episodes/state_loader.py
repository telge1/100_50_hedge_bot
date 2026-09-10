"""Load mb_state_1s_v1 partitions for a covered interval (read-only)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..partition_io import content_sha256_df, partition_dir
from ..schema_freeze import align_dataframe_to_frozen_schema


def load_states_for_interval(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load and concatenate hourly state partitions covering [start, end)."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    h = start.replace(minute=0, second=0, microsecond=0)
    frames: list[pd.DataFrame] = []
    manifests: list[dict[str, Any]] = []
    while h < end:
        pdir = partition_dir(symbol, h)
        pq = pdir / "state_1s.parquet"
        man = pdir / "manifest.json"
        if not pq.exists():
            raise FileNotFoundError(f"missing_state_partition: {pq}")
        df = pd.read_parquet(pq)
        frames.append(df)
        if man.exists():
            import json

            manifests.append(json.loads(man.read_text(encoding="utf-8")))
        h += timedelta(hours=1)

    full = pd.concat(frames, ignore_index=True)
    full, col_check = align_dataframe_to_frozen_schema(full)
    if not col_check["ok"]:
        raise RuntimeError(f"state_concat_schema:{col_check}")
    full["state_ts"] = pd.to_datetime(full["state_ts"], utc=True)
    full = full.sort_values("state_ts").reset_index(drop=True)
    mask = (full["state_ts"] >= start) & (full["state_ts"] < end)
    sliced = full.loc[mask].reset_index(drop=True)
    meta = {
        "partitions": manifests,
        "n_rows": int(len(sliced)),
        "content_sha256": content_sha256_df(sliced) if len(sliced) else hashlib.sha256(b"[]").hexdigest(),
        "window_start": start.isoformat().replace("+00:00", "Z"),
        "window_end": end.isoformat().replace("+00:00", "Z"),
        "replay_epoch": int(sliced["replay_epoch"].max()) if len(sliced) and "replay_epoch" in sliced.columns else 0,
    }
    return sliced, meta
