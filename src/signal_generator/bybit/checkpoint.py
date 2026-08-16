"""Resumable backfill checkpoint persistence."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from signal_generator.bybit.history import ensure_utc

SymbolStatus = Literal["PENDING", "RUNNING", "COMPLETE", "PARTIAL", "FAILED"]
FinalStatus = Literal[
    "COMPLETE_CLEAN",
    "COMPLETE_WITH_INTERNAL_GAPS",
    "PARTIAL",
    "FAILED",
    "NO_HISTORY",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ensure_utc(ts).isoformat()


def _dt_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text))


@dataclass(slots=True)
class SymbolCheckpoint:
    symbol: str
    requested_start: datetime
    requested_end: datetime
    effective_start: datetime | None = None
    last_completed_timestamp: datetime | None = None  # exclusive progress cursor
    status: SymbolStatus = "PENDING"
    rows_inserted: int = 0
    last_error: str | None = None
    updated_at: datetime = field(default_factory=_utcnow)
    final_status: FinalStatus | None = None
    unique_final: int | None = None
    physical_rows: int | None = None
    gap_count: int | None = None
    largest_gap_seconds: int | None = None
    missing_candle_count: int | None = None
    ohlc_error_count: int | None = None
    # Missing-range repair telemetry (optional; backward compatible)
    repair_started_at: datetime | None = None
    repair_completed_at: datetime | None = None
    missing_ranges_detected: int | None = None
    missing_candles_detected: int | None = None
    ranges_repaired: int | None = None
    candles_repaired: int | None = None
    unresolved_ranges: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "requested_start": _dt_to_iso(self.requested_start),
            "requested_end": _dt_to_iso(self.requested_end),
            "effective_start": _dt_to_iso(self.effective_start),
            "last_completed_timestamp": _dt_to_iso(self.last_completed_timestamp),
            "status": self.status,
            "rows_inserted": self.rows_inserted,
            "last_error": self.last_error,
            "updated_at": _dt_to_iso(self.updated_at),
            "final_status": self.final_status,
            "unique_final": self.unique_final,
            "physical_rows": self.physical_rows,
            "gap_count": self.gap_count,
            "largest_gap_seconds": self.largest_gap_seconds,
            "missing_candle_count": self.missing_candle_count,
            "ohlc_error_count": self.ohlc_error_count,
            "repair_started_at": _dt_to_iso(self.repair_started_at),
            "repair_completed_at": _dt_to_iso(self.repair_completed_at),
            "missing_ranges_detected": self.missing_ranges_detected,
            "missing_candles_detected": self.missing_candles_detected,
            "ranges_repaired": self.ranges_repaired,
            "candles_repaired": self.candles_repaired,
            "unresolved_ranges": self.unresolved_ranges,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SymbolCheckpoint:
        return cls(
            symbol=str(raw["symbol"]),
            requested_start=_dt_from_iso(raw["requested_start"]) or _utcnow(),
            requested_end=_dt_from_iso(raw["requested_end"]) or _utcnow(),
            effective_start=_dt_from_iso(raw.get("effective_start")),
            last_completed_timestamp=_dt_from_iso(raw.get("last_completed_timestamp")),
            status=raw.get("status") or "PENDING",  # type: ignore[arg-type]
            rows_inserted=int(raw.get("rows_inserted") or 0),
            last_error=raw.get("last_error"),
            updated_at=_dt_from_iso(raw.get("updated_at")) or _utcnow(),
            final_status=raw.get("final_status"),
            unique_final=raw.get("unique_final"),
            physical_rows=raw.get("physical_rows"),
            gap_count=raw.get("gap_count"),
            largest_gap_seconds=raw.get("largest_gap_seconds"),
            missing_candle_count=raw.get("missing_candle_count"),
            ohlc_error_count=raw.get("ohlc_error_count"),
            repair_started_at=_dt_from_iso(raw.get("repair_started_at")),
            repair_completed_at=_dt_from_iso(raw.get("repair_completed_at")),
            missing_ranges_detected=raw.get("missing_ranges_detected"),
            missing_candles_detected=raw.get("missing_candles_detected"),
            ranges_repaired=raw.get("ranges_repaired"),
            candles_repaired=raw.get("candles_repaired"),
            unresolved_ranges=raw.get("unresolved_ranges"),
        )


@dataclass(slots=True)
class CheckpointStore:
    version: int
    requested_start: datetime
    requested_end: datetime
    chunk_days: int
    symbols: dict[str, SymbolCheckpoint] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "requested_start": _dt_to_iso(self.requested_start),
            "requested_end": _dt_to_iso(self.requested_end),
            "chunk_days": self.chunk_days,
            "symbols": {k: v.to_dict() for k, v in sorted(self.symbols.items())},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CheckpointStore:
        symbols = {
            k: SymbolCheckpoint.from_dict(v) for k, v in (raw.get("symbols") or {}).items()
        }
        return cls(
            version=int(raw.get("version") or 1),
            requested_start=_dt_from_iso(raw["requested_start"]) or _utcnow(),
            requested_end=_dt_from_iso(raw["requested_end"]) or _utcnow(),
            chunk_days=int(raw.get("chunk_days") or 7),
            symbols=symbols,
        )


def load_checkpoint(path: Path) -> CheckpointStore | None:
    if not path.is_file():
        return None
    return CheckpointStore.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_checkpoint(store: CheckpointStore, path: Path) -> None:
    """Atomic write to avoid corrupt checkpoints on crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store.to_dict(), indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=".checkpoint_",
        suffix=".tmp",
    ) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def ensure_symbol_checkpoint(
    store: CheckpointStore,
    symbol: str,
    *,
    requested_start: datetime,
    requested_end: datetime,
) -> SymbolCheckpoint:
    existing = store.symbols.get(symbol)
    if existing is None:
        cp = SymbolCheckpoint(
            symbol=symbol,
            requested_start=ensure_utc(requested_start),
            requested_end=ensure_utc(requested_end),
        )
        store.symbols[symbol] = cp
        return cp
    return existing


def resume_start_for(cp: SymbolCheckpoint) -> datetime:
    """Return the exclusive-progress cursor to continue from."""
    if cp.last_completed_timestamp is not None:
        return ensure_utc(cp.last_completed_timestamp)
    return ensure_utc(cp.requested_start)


def should_skip_symbol(
    cp: SymbolCheckpoint,
    *,
    resume: bool,
    retry_failed: bool,
    repair_missing: bool = False,
) -> bool:
    """Skip symbol for this run.

    COMPLETE is only a blind skip when ``repair_missing`` is False.
    With repair enabled, COMPLETE symbols are re-checked for missing ranges.
    """
    if not resume:
        return False
    if cp.status == "COMPLETE" and not repair_missing:
        return True
    if cp.status == "FAILED" and not retry_failed:
        return True
    return False


def merge_checkpoint_window(
    store: CheckpointStore,
    *,
    requested_start: datetime,
    requested_end: datetime,
) -> None:
    """Update store/symbol windows without wiping progress cursors.

    Extending ``requested_end`` beyond a prior COMPLETE end reopens the symbol as
    PARTIAL from the previous end so cursor resume can continue; internal gaps
    still require ``--repair-missing``.
    """
    old_end = ensure_utc(store.requested_end)
    new_start = ensure_utc(requested_start)
    new_end = ensure_utc(requested_end)
    store.requested_start = new_start
    store.requested_end = new_end
    for cp in store.symbols.values():
        cp.requested_start = new_start
        cp.requested_end = new_end
        if new_end > old_end and cp.status == "COMPLETE":
            # Re-open trailing work from prior window end (not from requested_start).
            cursor = (
                ensure_utc(cp.last_completed_timestamp)
                if cp.last_completed_timestamp is not None
                else old_end
            )
            if cursor > old_end:
                cursor = old_end
            cp.last_completed_timestamp = cursor
            cp.status = "PARTIAL"
            cp.final_status = None
        # Earlier requested_start alone keeps cursors; use --repair-missing for leading gaps.
