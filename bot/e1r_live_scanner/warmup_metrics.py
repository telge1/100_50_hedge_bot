"""Compact warmup timing summaries (P1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class WarmupMetrics:
    symbol: str
    warmup_started_at: str
    warmup_finished_at: str
    bars_processed: int
    first_close: str | None
    last_close: str | None
    pane_load_seconds: float
    replay_seconds: float
    legacy_prime_seconds: float
    total_seconds: float
    startup_mode: str = "full"
    resume_used: bool = False
    resume_fallback_reason: str | None = None
    checkpoint_schema_version: int | None = None
    checkpoint_decision_close: str | None = None
    resume_load_seconds: float | None = None
    resume_prime_seconds: float | None = None
    resume_tail_seconds: float | None = None

    def as_summary(self) -> dict:
        summary = {
            "event": "warmup_summary",
            "symbol": self.symbol,
            "warmup_started_at": self.warmup_started_at,
            "warmup_finished_at": self.warmup_finished_at,
            "bars_processed": self.bars_processed,
            "first_close": self.first_close,
            "last_close": self.last_close,
            "pane_load_seconds": round(self.pane_load_seconds, 3),
            "replay_seconds": round(self.replay_seconds, 3),
            "legacy_prime_seconds": round(self.legacy_prime_seconds, 3),
            "total_seconds": round(self.total_seconds, 3),
            "startup_mode": self.startup_mode,
            "resume_used": self.resume_used,
            "resume_fallback_reason": self.resume_fallback_reason,
            "checkpoint_schema_version": self.checkpoint_schema_version,
            "checkpoint_decision_close": self.checkpoint_decision_close,
            "resume_load_seconds": self.resume_load_seconds,
            "resume_prime_seconds": self.resume_prime_seconds,
            "resume_tail_seconds": self.resume_tail_seconds,
        }
        return summary


@dataclass(frozen=True)
class WarmupStartupSummary:
    warmup_started_at: str
    warmup_finished_at: str
    symbols_total: int
    symbols_warmed: int
    symbols_skipped: int
    symbols_waiting: int
    total_seconds: float
    per_symbol: list[dict]

    def as_summary(self) -> dict:
        return {
            "event": "warmup_startup_summary",
            "warmup_started_at": self.warmup_started_at,
            "warmup_finished_at": self.warmup_finished_at,
            "symbols_total": self.symbols_total,
            "symbols_warmed": self.symbols_warmed,
            "symbols_skipped": self.symbols_skipped,
            "symbols_waiting": self.symbols_waiting,
            "total_seconds": round(self.total_seconds, 3),
            "per_symbol": self.per_symbol,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
