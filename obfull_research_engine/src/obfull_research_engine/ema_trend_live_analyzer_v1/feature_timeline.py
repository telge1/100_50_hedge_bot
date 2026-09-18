"""Real-time incremental timeline — horizons are elapsed windows, not synthetic clones."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import FEATURE_HORIZONS_S, FIRST_CANDIDATE_ELIGIBLE_SECONDS, FIRST_EARLY_EVIDENCE_SECONDS
from .schema import CoverageFlags, WindowState, iso_z


@dataclass
class IncrementalTimeline:
    snapshot_ready_at: datetime
    windows: dict[float, WindowState] = field(default_factory=dict)
    book_100ms: list[dict[str, Any]] = field(default_factory=list)

    def elapsed(self, now: datetime) -> float:
        return max(0.0, (now - self.snapshot_ready_at).total_seconds())

    def note_book_state(self, *, now: datetime, features: dict[str, Any], coverage: CoverageFlags) -> None:
        self.book_100ms.append(
            {
                "at": iso_z(now),
                "elapsed": self.elapsed(now),
                "features": features,
                "coverage": coverage.to_dict(),
            }
        )

    def materialize_horizons(
        self,
        *,
        now: datetime,
        book_event_count: int,
        trade_count: int,
        first_ts: str | None,
        last_ts: str | None,
        coverage: CoverageFlags,
        mass_balance_ok: bool | None,
        candidate_state: str,
        features: dict[str, Any],
    ) -> list[WindowState]:
        elapsed = self.elapsed(now)
        out: list[WindowState] = []
        for h in FEATURE_HORIZONS_S:
            if elapsed + 1e-9 < float(h):
                continue
            end = self.snapshot_ready_at + timedelta(seconds=float(h))
            # Use actual now if horizon just reached; window_end is nominal horizon end
            ws = WindowState(
                window_start=self.snapshot_ready_at,
                window_end=end,
                actual_elapsed=min(elapsed, float(h)) if elapsed >= h else elapsed,
                book_event_count=book_event_count,
                trade_count=trade_count,
                first_event_ts=first_ts,
                last_event_ts=last_ts,
                coverage=coverage,
                mass_balance_ok=mass_balance_ok,
                is_complete=elapsed >= float(h),
                horizon_s=float(h),
                candidate_state=candidate_state,  # type: ignore[arg-type]
                features=dict(features),
            )
            # Enforce early/candidate time rules in recorded state
            if float(h) <= FIRST_EARLY_EVIDENCE_SECONDS and candidate_state not in {
                "UNRESOLVED",
                "EARLY_EVIDENCE",
                "BLOCKED_COVERAGE",
                "BLOCKED_RAW_ARCHIVE",
            }:
                ws.candidate_state = "EARLY_EVIDENCE"  # type: ignore[assignment]
            if float(h) < FIRST_CANDIDATE_ELIGIBLE_SECONDS and "CANDIDATE" in str(candidate_state):
                ws.candidate_state = "EARLY_EVIDENCE"  # type: ignore[assignment]
            self.windows[float(h)] = ws
            out.append(ws)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_ready_at": iso_z(self.snapshot_ready_at),
            "horizons": {str(k): v.to_dict() for k, v in sorted(self.windows.items())},
            "book_100ms_count": len(self.book_100ms),
        }
