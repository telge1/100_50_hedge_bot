"""Load normalized OHLCV candles from CSV or Feather for backtests."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures")

CANDLE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class CandleSliceInfo:
    input_slice_start_index: int
    candle_source_total_count: int
    candles_loaded: int
    input_slice_first_timestamp: str | None
    input_slice_last_timestamp: str | None


def compute_input_slice_start_index(*, total_candle_count: int, slice_candle_count: int) -> int:
    """Return the absolute index of the first candle in a tail slice."""
    total = int(total_candle_count)
    loaded = int(slice_candle_count)
    if loaded <= 0 or loaded >= total:
        return 0
    return total - loaded


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
    rows, _slice_info = load_candles_with_slice_info(path, limit=limit)
    return rows


def load_candles_with_slice_info(
    path: str | Path,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], CandleSliceInfo]:
    """Load candles and return slice metadata for absolute index resolution."""
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(path_obj)

    suffix = path_obj.suffix.lower()
    if suffix == ".csv":
        all_rows = _load_csv(path_obj)
    elif suffix == ".feather":
        all_rows = _load_feather(path_obj)
    else:
        raise ValueError(f"unsupported candle file type: {suffix}")

    total_count = len(all_rows)
    if limit is not None and limit > 0 and limit < total_count:
        rows = all_rows[-limit:]
        start_index = compute_input_slice_start_index(
            total_candle_count=total_count,
            slice_candle_count=limit,
        )
    else:
        rows = all_rows
        start_index = 0

    first_ts = rows[0]["timestamp"] if rows else None
    last_ts = rows[-1]["timestamp"] if rows else None
    slice_info = CandleSliceInfo(
        input_slice_start_index=start_index,
        candle_source_total_count=total_count,
        candles_loaded=len(rows),
        input_slice_first_timestamp=first_ts.isoformat() if isinstance(first_ts, datetime) else None,
        input_slice_last_timestamp=last_ts.isoformat() if isinstance(last_ts, datetime) else None,
    )
    return rows, slice_info


def _load_candles_unlimited(path: str | Path) -> list[dict[str, Any]]:
    path_obj = Path(path)
    suffix = path_obj.suffix.lower()
    if suffix == ".csv":
        return _load_csv(path_obj)
    if suffix == ".feather":
        return _load_feather(path_obj)
    raise ValueError(f"unsupported candle file type: {suffix}")


def load_candles_for_symbol(
    symbol: str = "APTUSDT",
    timeframe: str = "5m",
    data_dir: str | Path = DEFAULT_DATA_DIR,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve symbol to feather path under ``data_dir`` and load via :func:`load_candles`."""
    rows, _slice_info = load_candles_for_symbol_with_slice_info(
        symbol,
        timeframe=timeframe,
        data_dir=data_dir,
        limit=limit,
    )
    return rows


def load_candles_for_symbol_with_slice_info(
    symbol: str = "APTUSDT",
    timeframe: str = "5m",
    data_dir: str | Path = DEFAULT_DATA_DIR,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], CandleSliceInfo]:
    """Load symbol candles and return slice metadata for absolute index resolution."""
    filename = symbol_to_feather_name(symbol, timeframe=timeframe)
    path = Path(data_dir) / filename
    return load_candles_with_slice_info(path, limit=limit)
