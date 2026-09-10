"""Causal 1s mid-price index for outcome labeling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from ..partition_io import partition_dir


@dataclass(frozen=True)
class PricePoint:
    state_ts: pd.Timestamp
    available_at: pd.Timestamp
    mid: float
    spread_bps: float | None
    replay_epoch: int
    resync_flag: int
    sequence_gap_count: int
    price_valid: int


class CausalPriceIndex:
    """Index of Full-OB book mids keyed by causal availability time."""

    def __init__(self, points: list[PricePoint], *, bucket_seconds: int = 1):
        self.bucket_seconds = int(bucket_seconds)
        self.points = sorted(points, key=lambda p: p.available_at)
        self._avail = np.array([p.available_at.value for p in self.points], dtype=np.int64)
        self.by_available: dict[pd.Timestamp, PricePoint] = {p.available_at: p for p in self.points}
        self.last_available_at = self.points[-1].available_at if self.points else None
        self.first_available_at = self.points[0].available_at if self.points else None

    @classmethod
    def from_state_df(cls, df: pd.DataFrame, *, bucket_seconds: int = 1) -> "CausalPriceIndex":
        bucket = timedelta(seconds=int(bucket_seconds))
        work = df.copy()
        work["state_ts"] = pd.to_datetime(work["state_ts"], utc=True)
        work = work.dropna(subset=["mid_price"]).sort_values("state_ts")
        pts: list[PricePoint] = []
        has_spread = "spread_bps" in work.columns
        has_epoch = "replay_epoch" in work.columns
        has_resync = "resync_flag" in work.columns
        has_gap = "sequence_gap_count" in work.columns
        has_valid = "price_valid" in work.columns
        for row in work.itertuples(index=False):
            st = pd.Timestamp(getattr(row, "state_ts")).tz_convert("UTC")
            mid = float(getattr(row, "mid_price"))
            spread = getattr(row, "spread_bps", None) if has_spread else None
            pts.append(
                PricePoint(
                    state_ts=st,
                    available_at=st + bucket,
                    mid=mid,
                    spread_bps=None if spread is None or (isinstance(spread, float) and np.isnan(spread)) else float(spread),
                    replay_epoch=int(getattr(row, "replay_epoch", 0) or 0) if has_epoch else 0,
                    resync_flag=int(getattr(row, "resync_flag", 0) or 0) if has_resync else 0,
                    sequence_gap_count=int(getattr(row, "sequence_gap_count", 0) or 0) if has_gap else 0,
                    price_valid=int(getattr(row, "price_valid", 1) or 0) if has_valid else 1,
                )
            )
        return cls(pts, bucket_seconds=bucket_seconds)

    def latest_at_or_before(
        self,
        ts: pd.Timestamp,
        *,
        max_age_ms: int,
        require_valid: bool = True,
    ) -> PricePoint | None:
        ts = pd.Timestamp(ts).tz_convert("UTC")
        if not len(self._avail):
            return None
        tval = int(ts.value)
        idx = int(np.searchsorted(self._avail, tval, side="right") - 1)
        if idx < 0:
            return None
        p = self.points[idx]
        age_ms = (ts - p.available_at).total_seconds() * 1000.0
        if age_ms > float(max_age_ms):
            return None
        if require_valid and p.price_valid != 1:
            return None
        return p

    def get_exact(self, available_at: pd.Timestamp) -> PricePoint | None:
        available_at = pd.Timestamp(available_at).tz_convert("UTC")
        return self.by_available.get(available_at)


def load_states_for_outcome_window(
    *,
    symbol: str,
    feature_start: datetime,
    feature_end: datetime,
    max_horizon_seconds: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load feature window plus forward partitions needed for max horizon (read-only).

    Missing future partitions are allowed; caller censors. Feature window partitions
    must exist (coverage gate already required DATA_COMPLETE).
    """
    feature_start = feature_start.astimezone(timezone.utc)
    feature_end = feature_end.astimezone(timezone.utc)
    outcome_end = feature_end + timedelta(seconds=int(max_horizon_seconds))

    h = feature_start.replace(minute=0, second=0, microsecond=0)
    frames: list[pd.DataFrame] = []
    missing_hours: list[str] = []
    loaded_hours: list[str] = []
    while h < outcome_end:
        pdir = partition_dir(symbol, h)
        pq = pdir / "state_1s.parquet"
        label = h.isoformat().replace("+00:00", "Z")
        if pq.exists():
            frames.append(pd.read_parquet(pq))
            loaded_hours.append(label)
        else:
            # Only require feature-window hours; future may be missing.
            if h < feature_end:
                raise FileNotFoundError(f"missing_feature_state_partition: {pq}")
            missing_hours.append(label)
        h += timedelta(hours=1)

    if not frames:
        raise FileNotFoundError("no_state_partitions_loaded")
    full = pd.concat(frames, ignore_index=True)
    full["state_ts"] = pd.to_datetime(full["state_ts"], utc=True)
    full = full.sort_values("state_ts").drop_duplicates(subset=["state_ts"], keep="last").reset_index(drop=True)
    # Keep from one bucket before feature start (for early anchors) through outcome end.
    keep_from = feature_start - timedelta(seconds=2)
    mask = (full["state_ts"] >= keep_from) & (full["state_ts"] < outcome_end)
    sliced = full.loc[mask].reset_index(drop=True)
    meta = {
        "loaded_hours": loaded_hours,
        "missing_future_hours": missing_hours,
        "n_rows": int(len(sliced)),
        "state_ts_min": sliced["state_ts"].min().isoformat().replace("+00:00", "Z") if len(sliced) else None,
        "state_ts_max": sliced["state_ts"].max().isoformat().replace("+00:00", "Z") if len(sliced) else None,
        "price_source": "mb_state_1s_v1.mid_price",
        "fallback_price_source": None,
    }
    return sliced, meta
