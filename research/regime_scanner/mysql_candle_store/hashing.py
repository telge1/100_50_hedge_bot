"""Deterministic hashing helpers for candle export reproducibility."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd

from research.regime_scanner.timeframes import ensure_utc_timestamp

# Documented HTF equality audit hash (serialization of that audit, not DB export).
HTF_EQUALITY_AUDIT_HASH = (
    "b795131e7360a5b3a2e217e5d37a5d8d50cba0dd36c74354739bfc6f7b4f6d42"
)


def sha256_file(path: str | bytes | Any) -> str:
    from pathlib import Path

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def candles_export_hash(frame: pd.DataFrame) -> str:
    """Stable hash over sorted OHLCV export lines (not the HTF audit hash)."""
    if frame is None or frame.empty:
        payload = "EMPTY"
    else:
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        work = frame.copy()
        if "timestamp" not in work.columns and "open_time" in work.columns:
            work = work.rename(columns={"open_time": "timestamp"})
        if "timestamp" not in work.columns and "date" in work.columns:
            work = work.rename(columns={"date": "timestamp"})
        work = work.loc[:, [c for c in cols if c in work.columns]].copy()
        work["timestamp"] = pd.to_datetime(work["timestamp"], utc=True)
        work = work.sort_values("timestamp").reset_index(drop=True)
        lines = []
        for _, row in work.iterrows():
            lines.append(
                "{ts},{o:.17g},{h:.17g},{l:.17g},{c:.17g},{v:.17g}".format(
                    ts=ensure_utc_timestamp(row["timestamp"]).isoformat(),
                    o=float(row["open"]),
                    h=float(row["high"]),
                    l=float(row["low"]),
                    c=float(row["close"]),
                    v=float(row["volume"]),
                )
            )
        payload = "\n".join(lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def json_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
