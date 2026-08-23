"""Causal EMA episode state — one normal entry per direction episode."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig
from .models import CandidateType, EpisodeState, FinalVerdict


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def make_episode_id(symbol: str, tf: str, direction: str, start_at: datetime) -> str:
    key = f"{symbol}|{tf}|{direction}|{_utc(start_at).isoformat()}"
    return "edcep:" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _metrics_compressed(metrics: dict[str, Any], cfg: EmaDualCrossConfig) -> bool:
    gap_pct = metrics.get("ema_9_20_gap_pct")
    gap_atr = metrics.get("ema_9_20_gap_atr")
    band_atr = metrics.get("ema_band_width_atr")
    if gap_pct is not None and gap_pct > cfg.band_compression_pct:
        return False
    if gap_atr is not None and gap_atr > cfg.band_compression_atr:
        return False
    if band_atr is not None and band_atr > cfg.max_total_band_atr:
        return False
    return True


@dataclass
class EpisodeTracker:
    """Per-direction episode state (causal, no lookahead)."""

    cfg: EmaDualCrossConfig = field(default_factory=lambda: EMA_DUAL_CROSS_DEFAULTS)
    state: dict[str, EpisodeState] = field(
        default_factory=lambda: {"BULLISH": EpisodeState.NEUTRAL, "BEARISH": EpisodeState.NEUTRAL}
    )
    episode_id: dict[str, str | None] = field(default_factory=lambda: {"BULLISH": None, "BEARISH": None})
    episode_start_bar: dict[str, int | None] = field(default_factory=lambda: {"BULLISH": None, "BEARISH": None})
    compression_active: dict[str, bool] = field(default_factory=lambda: {"BULLISH": False, "BEARISH": False})
    rebound_emitted: dict[str, bool] = field(default_factory=lambda: {"BULLISH": False, "BEARISH": False})
    # Active entry episode — only opened by ALLOW
    active_entry_bar: dict[str, int | None] = field(default_factory=lambda: {"BULLISH": None, "BEARISH": None})
    active_entry_type: dict[str, str | None] = field(default_factory=lambda: {"BULLISH": None, "BEARISH": None})

    def update_compression(self, direction: str, *, compressed: bool, bar_index: int) -> None:
        d = direction.upper()
        if compressed and self.state[d] in (EpisodeState.NEUTRAL, EpisodeState.RESET_COMPLETE):
            self.state[d] = EpisodeState.COMPRESSION_ACTIVE
            self.compression_active[d] = True
            self.rebound_emitted[d] = False
        elif compressed and self.state[d] == EpisodeState.RESET_PENDING:
            self.state[d] = EpisodeState.COMPRESSION_ACTIVE
            self.compression_active[d] = True
            self.rebound_emitted[d] = False
        elif not compressed and self.compression_active[d]:
            if self.state[d] in (
                EpisodeState.COMPRESSION_ACTIVE,
                EpisodeState.CANDIDATE_REJECTED,
                EpisodeState.CANDIDATE_BLOCKED,
                EpisodeState.CANDIDATE_INCONCLUSIVE,
            ):
                self.state[d] = EpisodeState.RESET_PENDING
                self.compression_active[d] = False

    def try_reset(self, direction: str, *, opposite_signal: bool, bar_index: int) -> None:
        d = direction.upper()
        if opposite_signal:
            self._reset_direction(d)
            return
        start = self.active_entry_bar.get(d)
        if start is not None and bar_index - start >= self.cfg.episode_reset_bars:
            if self.state[d] == EpisodeState.TREND_EPISODE_ACTIVE:
                self._reset_direction(d)

    def notify_opposite_sync_cross(self, direction: str) -> None:
        """Valid sync cross in one direction invalidates opposite entry episode."""
        opp = "BEARISH" if direction.upper() == "BULLISH" else "BULLISH"
        self._reset_direction(opp)

    def _reset_direction(self, d: str) -> None:
        self.state[d] = EpisodeState.RESET_COMPLETE
        self.episode_id[d] = None
        self.episode_start_bar[d] = None
        self.compression_active[d] = False
        self.rebound_emitted[d] = False
        self.active_entry_bar[d] = None
        self.active_entry_type[d] = None
        self.state[d] = EpisodeState.NEUTRAL

    def admit_candidate(self, raw: dict[str, Any]) -> tuple[bool, str | None, str | None]:
        """Return (allowed, reject_reason, research_relation)."""
        d = str(raw["direction"]).upper()
        bar_i = int(raw["bar_index"])
        ctype = str(raw.get("candidate_type") or "")

        metrics = raw.get("ema_metrics") or {}
        self.update_compression(d, compressed=_metrics_compressed(metrics, self.cfg), bar_index=bar_i)
        self.try_reset(d, opposite_signal=False, bar_index=bar_i)

        sync = ctype == CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value
        rebound = ctype == CandidateType.COMPRESSED_EMA59_REBOUND.value

        active_bar = self.active_entry_bar.get(d)
        if sync and active_bar is not None:
            if self.active_entry_type.get(d) == CandidateType.COMPRESSED_EMA59_REBOUND.value:
                if bar_i - active_bar < self.cfg.episode_reset_bars:
                    raw["research_relation"] = "SYNC_CONFIRMATION"
                    return True, None, "SYNC_CONFIRMATION"
            if self.active_entry_type.get(d) == CandidateType.SYNCHRONOUS_DUAL_EMA_CROSS.value:
                if bar_i - active_bar < self.cfg.episode_reset_bars:
                    return False, "REJECTED_EPISODE_ALREADY_SIGNALED", None

        if active_bar is not None and bar_i - active_bar < self.cfg.episode_reset_bars:
            if rebound:
                return False, "REJECTED_EPISODE_ALREADY_SIGNALED", None
            if sync and not raw.get("research_relation"):
                return False, "REJECTED_EPISODE_ALREADY_SIGNALED", None

        if rebound and self.rebound_emitted.get(d) and self.compression_active.get(d):
            return False, "REJECTED_REBOUND_ALREADY_IN_COMPRESSION", None

        if self.episode_id[d] is None:
            ts = raw["candidate_at"]
            if not isinstance(ts, datetime):
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            self.episode_id[d] = make_episode_id(raw["symbol"], raw["timeframe"], d, ts)
            self.episode_start_bar[d] = bar_i

        raw["episode_id"] = self.episode_id[d]
        self.state[d] = EpisodeState.CROSS_CANDIDATE
        relation = raw.get("research_relation")
        return True, None, relation

    def record_verdict(self, raw: dict[str, Any], verdict: FinalVerdict) -> None:
        d = str(raw["direction"]).upper()
        bar_i = int(raw["bar_index"])
        ctype = str(raw.get("candidate_type") or "")

        if verdict == FinalVerdict.ALLOW:
            self.active_entry_bar[d] = bar_i
            self.active_entry_type[d] = ctype
            self.state[d] = EpisodeState.TREND_EPISODE_ACTIVE
            if ctype == CandidateType.COMPRESSED_EMA59_REBOUND.value:
                self.rebound_emitted[d] = True
        elif verdict == FinalVerdict.BLOCK:
            self.state[d] = EpisodeState.CANDIDATE_BLOCKED
        elif verdict == FinalVerdict.INCONCLUSIVE_DATA:
            self.state[d] = EpisodeState.CANDIDATE_INCONCLUSIVE
        else:
            self.state[d] = EpisodeState.CANDIDATE_REJECTED
