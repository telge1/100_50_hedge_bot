"""Load normalized OHLCV candles from CSV or Feather for backtests."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures")

CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def symbol_to_feather_name(symbol: str, timeframe: str = "5m") -> str:
    """Map exchange symbol to feather filename, e.g. APTUSDT -> APT_USDT_USDT-5m-futures.feather."""
    normalized = str(symbol or "").strip().upper()
    if normalized.endswith("USDT"):
        base = normalized[: -len("USDT")]
    else:
        base = normalized
    return f"{base}_USDT_USDT-{timeframe}-futures.feather"


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _normalize_row(raw: dict[str, Any]) -> dict[str, Any]:
    timestamp_raw = raw.get("timestamp")
    if timestamp_raw is None:
        timestamp_raw = raw.get("date")
    if timestamp_raw is None:
        raise ValueError("candle row missing timestamp/date column")

    row: dict[str, Any] = {
        "timestamp": _parse_timestamp(timestamp_raw),
        "open": float(raw["open"]),
        "high": float(raw["high"]),
        "low": float(raw["low"]),
        "close": float(raw["close"]),
    }
    volume_raw = raw.get("volume")
    row["volume"] = float(volume_raw) if volume_raw is not None else None
    return row


def _load_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            rows.append(_normalize_row(raw))
    return rows


def _load_feather(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.feather as feather
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError(
            "Feather candle files require pyarrow. Install pyarrow or use CSV input."
        ) from exc

    table = feather.read_table(path)
    columns = table.to_pydict()
    column_names = list(columns.keys())
    if "date" in column_names and "timestamp" not in column_names:
        columns["timestamp"] = columns.pop("date")
    length = len(next(iter(columns.values())))
    rows: list[dict[str, Any]] = []
    for index in range(length):
        raw = {name: columns[name][index] for name in columns}
        rows.append(_normalize_row(raw))
    return rows


def load_candles(path: str | Path, limit: int | None = None) -> list[dict[str, Any]]:
    """
    Load candles from CSV or Feather and normalize to timestamp/open/high/low/close/volume.

    When ``limit`` is set, returns the **last** N rows (most recent candles), preserving
    chronological order oldest-first within the returned slice.
    """
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(path_obj)

    suffix = path_obj.suffix.lower()
    if suffix == ".csv":
        rows = _load_csv(path_obj)
    elif suffix == ".feather":
        rows = _load_feather(path_obj)
    else:
        raise ValueError(f"unsupported candle file type: {suffix}")

    if limit is not None:
        if limit <= 0:
            return []
        rows = rows[-limit:]
    return rows


def load_candles_for_symbol(
    symbol: str = "APTUSDT",
    timeframe: str = "5m",
    data_dir: str | Path = DEFAULT_DATA_DIR,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve symbol to feather path under ``data_dir`` and load via :func:`load_candles`."""
    filename = symbol_to_feather_name(symbol, timeframe=timeframe)
    path = Path(data_dir) / filename
    return load_candles(path, limit=limit)
