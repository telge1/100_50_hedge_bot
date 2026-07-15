"""Unified read-only candle sources for the regime scanner (Feather | MySQL).

Both sources emit the scanner-canonical OHLCV frame:

    timestamp (UTC open), open, high, low, close, volume  [float64]
    optional: close_time (UTC)

Scanner HTF (15m/30m) continues to be aggregated from 5m via
``timeframes.aggregate_candles``. Direct HTF feathers/MySQL rows are only used
by the parity audit, not by the live scanner path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import pandas as pd

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
    symbol_to_feather_name,
)
from research.regime_scanner.timeframes import TIMEFRAME_MINUTES, ensure_utc_timestamp, timeframe_timedelta

DataSourceName = Literal["feather", "mysql"]

CANONICAL_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

DEFAULT_5M_FEATHER = DEFAULT_DATA_DIR / "APT_USDT_USDT-5m-futures.feather"
DEFAULT_15M_FEATHER = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/"
    "APT_USDT_USDT-15m-futures.feather"
)
DEFAULT_30M_FEATHER = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_apt_htf_staging/futures/"
    "APT_USDT_USDT-30m-futures.feather"
)

DEFAULT_FEATHER_PATHS: dict[str, Path] = {
    "5m": DEFAULT_5M_FEATHER,
    "15m": DEFAULT_15M_FEATHER,
    "30m": DEFAULT_30M_FEATHER,
}

REGIME_ENV_FILE = Path(__file__).resolve().parent / ".env.regime_db"


class CandleSourceError(ValueError):
    """Raised for invalid source selection or load failures."""


class CandleSource(Protocol):
    """Read-only candle provider. Must not mutate markets or scanner state."""

    name: str

    def load_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_time: object | None = None,
        end_time: object | None = None,
        decision_time: object | None = None,
        closed_only: bool = True,
    ) -> pd.DataFrame:
        """Return canonical OHLCV sorted by open time.

        When ``decision_time`` is set, only candles with
        ``close_time <= decision_time`` are returned (closed-candle semantics).
        """
        ...

    def close(self) -> None:
        ...


def _normalize_canonical(
    frame: pd.DataFrame,
    *,
    timeframe: str,
    include_close_time: bool = True,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        cols = list(CANONICAL_COLUMNS) + (["close_time"] if include_close_time else [])
        return pd.DataFrame(columns=cols)

    out = frame.copy()
    if "timestamp" not in out.columns and "date" in out.columns:
        out = out.rename(columns={"date": "timestamp"})
    if "timestamp" not in out.columns and "open_time" in out.columns:
        out = out.rename(columns={"open_time": "timestamp"})

    missing = [c for c in CANONICAL_COLUMNS if c not in out.columns]
    if missing:
        raise CandleSourceError(f"candle frame missing columns: {missing}")

    out = out.loc[:, [c for c in list(CANONICAL_COLUMNS) + ["close_time"] if c in out.columns]].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")

    duration = timeframe_timedelta(timeframe)
    if "close_time" not in out.columns:
        out["close_time"] = out["timestamp"] + duration
    else:
        out["close_time"] = pd.to_datetime(out["close_time"], utc=True)

    out = out.sort_values("timestamp").reset_index(drop=True)
    if out["timestamp"].duplicated().any():
        raise CandleSourceError("duplicate open timestamps in candle source result")
    if not bool(out["timestamp"].is_monotonic_increasing):
        raise CandleSourceError("timestamps are not strictly ascending")

    if not include_close_time:
        return out.loc[:, list(CANONICAL_COLUMNS)].reset_index(drop=True)
    return out.loc[:, list(CANONICAL_COLUMNS) + ["close_time"]].reset_index(drop=True)


def _apply_window(
    frame: pd.DataFrame,
    *,
    start_time: object | None,
    end_time: object | None,
    decision_time: object | None,
    closed_only: bool,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame
    if start_time is not None:
        start = ensure_utc_timestamp(start_time)
        out = out.loc[out["timestamp"] >= start]
    if end_time is not None:
        end = ensure_utc_timestamp(end_time)
        out = out.loc[out["timestamp"] <= end]
    if decision_time is not None:
        decision = ensure_utc_timestamp(decision_time)
        out = out.loc[out["close_time"] <= decision]
    elif closed_only:
        # Without an explicit decision_time, all imported historical bars are closed.
        pass
    return out.reset_index(drop=True)


def load_regime_db_env_file(path: Path | None = None) -> dict[str, str]:
    """Load ``REGIME_DB_*`` from a gitignored env file into ``os.environ`` if unset.

    Never returns or logs password values.
    """
    env_path = path or REGIME_ENV_FILE
    loaded: dict[str, str] = {}
    if not env_path.is_file():
        return loaded
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if not key.startswith("REGIME_DB_"):
            continue
        if key not in os.environ or not str(os.environ.get(key, "")).strip():
            os.environ[key] = value
            loaded[key] = "set" if key == "REGIME_DB_PASSWORD" else value
        else:
            loaded[key] = "already_set"
    return loaded


@dataclass
class FeatherCandleSource:
    """Load candles from existing Freqtrade feather files (read-only)."""

    data_dir: Path | None = None
    paths: dict[str, Path] | None = None
    name: str = "feather"

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir) if self.data_dir is not None else DEFAULT_DATA_DIR
        self.paths = dict(DEFAULT_FEATHER_PATHS if self.paths is None else self.paths)

    def resolve_path(self, *, symbol: str, timeframe: str) -> Path:
        tf = str(timeframe).strip().lower()
        if self.paths and tf in self.paths:
            return Path(self.paths[tf])
        return Path(self.data_dir) / symbol_to_feather_name(symbol, timeframe=tf)

    def load_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_time: object | None = None,
        end_time: object | None = None,
        decision_time: object | None = None,
        closed_only: bool = True,
    ) -> pd.DataFrame:
        _ = exchange  # feather layout is path-based; exchange kept for API symmetry
        tf = str(timeframe).strip().lower()
        if tf not in TIMEFRAME_MINUTES:
            raise CandleSourceError(f"unsupported timeframe: {timeframe!r}")

        path = self.resolve_path(symbol=symbol, timeframe=tf)
        if not path.is_file():
            raise FileNotFoundError(path)

        # Prefer explicit path (supports staging 15m/30m outside DEFAULT_DATA_DIR).
        import pyarrow.feather as feather

        table = feather.read_table(path)
        raw = table.to_pandas()
        frame = _normalize_canonical(raw, timeframe=tf, include_close_time=True)
        return _apply_window(
            frame,
            start_time=start_time,
            end_time=end_time,
            decision_time=decision_time,
            closed_only=closed_only,
        )

    def close(self) -> None:
        return None


@dataclass
class MySQLCandleSource:
    """Read-only MySQL candle source via ``mysql_candle_store``."""

    exchange_default: str = "bybit"
    name: str = "mysql"
    _store: object | None = None

    def _ensure_store(self):
        if self._store is not None:
            return self._store
        load_regime_db_env_file()
        from research.regime_scanner.mysql_candle_store.config import (
            RegimeDbConfigError,
            has_regime_db_config,
            load_regime_db_config,
        )
        from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore

        if not has_regime_db_config():
            raise CandleSourceError(
                "MySQL data source selected but REGIME_DB_* is not configured. "
                "Set environment variables or provide research/regime_scanner/.env.regime_db."
            )
        try:
            config = load_regime_db_config()
        except RegimeDbConfigError as exc:
            raise CandleSourceError(str(exc)) from exc
        self._store = MySQLCandleStore(config)
        return self._store

    def load_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_time: object | None = None,
        end_time: object | None = None,
        decision_time: object | None = None,
        closed_only: bool = True,
    ) -> pd.DataFrame:
        tf = str(timeframe).strip().lower()
        if tf not in TIMEFRAME_MINUTES:
            raise CandleSourceError(f"unsupported timeframe: {timeframe!r}")
        store = self._ensure_store()
        from research.regime_scanner.mysql_candle_store.repository import load_candles as repo_load

        frame = repo_load(
            store,
            exchange=exchange,
            symbol=symbol,
            timeframe=tf,
            start_time=start_time,
            end_time=end_time,
            decision_time=decision_time,
            closed_only=closed_only,
        )
        return _normalize_canonical(frame, timeframe=tf, include_close_time=True)

    def close(self) -> None:
        if self._store is not None:
            closer = getattr(self._store, "close", None)
            if callable(closer):
                closer()
            self._store = None


def create_candle_source(
    data_source: str = "feather",
    *,
    data_dir: str | Path | None = None,
    feather_paths: dict[str, Path] | None = None,
) -> CandleSource:
    """Factory. Unknown sources fail loudly; no silent fallback."""
    key = str(data_source or "").strip().lower()
    if key == "feather":
        return FeatherCandleSource(
            data_dir=Path(data_dir) if data_dir is not None else None,
            paths=feather_paths,
        )
    if key == "mysql":
        if data_dir is not None:
            # Explicit: MySQL mode ignores feather data_dir; do not open feather files.
            pass
        return MySQLCandleSource()
    raise CandleSourceError(
        f"unknown data_source={data_source!r}; allowed: feather, mysql"
    )


def load_scanner_5m_candles(
    *,
    symbol: str = "APTUSDT",
    exchange: str = "bybit",
    data_source: str = "feather",
    data_dir: str | Path | None = None,
    start_time: object | None = None,
    end_time: object | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Load scanner-input 5m candles from the selected source (no decision_time filter).

    Returns scanner-canonical columns without requiring ``close_time`` downstream.
    HTF must still be derived via ``aggregate_candles``.
    """
    source = create_candle_source(data_source, data_dir=data_dir)
    try:
        if str(data_source).lower() == "feather" and start_time is None and end_time is None:
            # Preserve exact historical load path for default feather (incl. limit).
            from research.regime_scanner.data_loader import (
                candles_to_dataframe,
                validate_candle_dataframe,
            )

            rows = load_candles_for_symbol(
                symbol=symbol,
                timeframe="5m",
                data_dir=Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR,
                limit=limit,
            )
            frame = validate_candle_dataframe(candles_to_dataframe(rows))
            return frame

        frame = source.load_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe="5m",
            start_time=start_time,
            end_time=end_time,
            decision_time=None,
            closed_only=True,
        )
        out = frame.loc[:, list(CANONICAL_COLUMNS)].copy()
        if limit is not None and limit > 0 and len(out) > limit:
            out = out.iloc[-limit:].reset_index(drop=True)
        return out.reset_index(drop=True)
    finally:
        source.close()
