"""Frozen causal episode construction and deterministic artifact writing."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .contract import (
    BUILDER_VERSION,
    CONTRACT_VERSION,
    EPISODE_COLUMNS,
    EXCLUDED_COLUMNS,
    GRID_SECONDS,
    MAX_CANDIDATE_WINDOW_SECONDS,
    PRICE_SOURCE,
    TARGET_CONTRACT_VERSION,
    TARGET_SOURCE,
    WARMUP_SECONDS,
    Eligibility,
    ExclusionReason,
    Outcome,
    contract_dict,
)
from .eligibility import evaluate_candidate
from .models import FrozenEpisode, iso_z, utc
from .path_label import label_first_touch
from .sources import TradeSecondSeries, load_trade_seconds
from .targets import CanonicalLldPoolProvider, PoolSnapshotProvider, select_frozen_targets

MAX_BOUNDED_EXPAND_HOURS = 168.0
DEFAULT_TRADE_CHUNK_SECONDS = MAX_CANDIDATE_WINDOW_SECONDS
TradeSeriesLoader = Callable[..., TradeSecondSeries]


@dataclass(frozen=True)
class BuildConfig:
    symbol: str
    start: datetime
    end: datetime
    horizon_minutes: int
    require_complete: bool
    target_source: str = TARGET_SOURCE
    max_episodes: int | None = None
    allow_bounded_expand: bool = False
    max_window_hours: float | None = None
    trade_chunk_seconds: int = DEFAULT_TRADE_CHUNK_SECONDS

    def __post_init__(self) -> None:
        start, end = utc(self.start), utc(self.end)
        window_seconds = (end - start).total_seconds()
        if window_seconds <= 0:
            raise ValueError("end must be after start")
        if (
            not isinstance(self.trade_chunk_seconds, int)
            or isinstance(self.trade_chunk_seconds, bool)
            or self.trade_chunk_seconds <= 0
            or self.trade_chunk_seconds > MAX_CANDIDATE_WINDOW_SECONDS
        ):
            raise ValueError("trade chunk must be an integer in (0,7200] seconds")
        if self.allow_bounded_expand:
            if (
                self.max_window_hours is None
                or isinstance(self.max_window_hours, bool)
                or not math.isfinite(self.max_window_hours)
                or self.max_window_hours <= 0
                or self.max_window_hours > MAX_BOUNDED_EXPAND_HOURS
            ):
                raise ValueError(
                    "bounded expand requires max-window-hours in (0,168]"
                )
            if window_seconds > self.max_window_hours * 60 * 60:
                raise ValueError("requested window exceeds max-window-hours")
        elif self.max_window_hours is not None:
            raise ValueError("max-window-hours requires allow-bounded-expand")
        elif window_seconds > MAX_CANDIDATE_WINDOW_SECONDS:
            raise ValueError("Phase-1 candidate window is limited to two hours")
        if self.horizon_minutes <= 0 or self.horizon_minutes > 60:
            raise ValueError("horizon-minutes must be in 1..60")
        if self.target_source != TARGET_SOURCE:
            raise ValueError(ExclusionReason.UNSUPPORTED_TARGET_SOURCE.value)
        if self.symbol not in {"BTCUSDT", "DOGEUSDT"}:
            raise ValueError("unsupported symbol")


@dataclass(frozen=True)
class BuildResult:
    episodes: tuple[FrozenEpisode, ...]
    excluded: tuple[dict[str, Any], ...]
    summary: dict[str, Any]


def deterministic_episode_id(
    *, symbol: str, t0: datetime, upper_id: str, lower_id: str, horizon_minutes: int
) -> str:
    key = "|".join(
        [
            CONTRACT_VERSION,
            symbol,
            iso_z(t0),
            upper_id,
            lower_id,
            str(horizon_minutes),
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _excluded(symbol: str, t0: datetime, reason: ExclusionReason, detail: str = "") -> dict:
    return {
        "symbol": symbol,
        "t0_utc": iso_z(t0),
        "eligibility": Eligibility.INELIGIBLE.value,
        "outcome": Outcome.INELIGIBLE.value,
        "exclusion_reason": reason.value,
        "detail": detail,
        "target_source": TARGET_SOURCE,
        "builder_version": BUILDER_VERSION,
        "contract_version": CONTRACT_VERSION,
    }


def _grid(start: datetime, end: datetime):
    cursor, end_u = utc(start), utc(end)
    while cursor < end_u:
        yield cursor
        cursor += timedelta(seconds=GRID_SECONDS)


def _process_grid(
    config: BuildConfig,
    *,
    start: datetime,
    end: datetime,
    series: TradeSecondSeries,
    target_provider: PoolSnapshotProvider,
    episodes: list[FrozenEpisode],
    excluded: list[dict[str, Any]],
    active_until: datetime | None,
) -> tuple[datetime | None, bool]:
    horizon = timedelta(minutes=config.horizon_minutes)
    for t0 in _grid(start, end):
        if config.max_episodes is not None and len(episodes) >= config.max_episodes:
            return active_until, True
        if active_until is not None and t0 < active_until:
            excluded.append(
                _excluded(
                    config.symbol,
                    t0,
                    ExclusionReason.DUPLICATE_OR_OVERLAPPING_EPISODE,
                    f"active_until={iso_z(active_until)}",
                )
            )
            continue

        price_t0 = series.price_before(t0)
        if price_t0 is None:
            excluded.append(
                _excluded(config.symbol, t0, ExclusionReason.PRICE_MISSING_AT_T0)
            )
            continue

        try:
            snapshot = target_provider.snapshot(config.symbol, t0)
            upper, lower = select_frozen_targets(
                snapshot, price_t0=price_t0, t0=t0
            )
        except Exception as exc:
            excluded.append(
                _excluded(
                    config.symbol,
                    t0,
                    ExclusionReason.SOURCE_COVERAGE_INCOMPLETE,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        horizon_end = t0 + horizon
        warmup_complete = series.covers(
            t0 - timedelta(seconds=WARMUP_SECONDS), t0
        )
        path_complete = series.coverage_complete(t0, horizon_end)
        horizon_closed = (
            series.coverage_end is not None and series.coverage_end >= horizon_end
        )
        reason = evaluate_candidate(
            t0=t0,
            price_t0=price_t0,
            upper=upper,
            lower=lower,
            warmup_complete=warmup_complete,
            source_complete=path_complete,
            horizon_closed=horizon_closed,
            require_complete=config.require_complete,
        )
        if reason is not None:
            excluded.append(_excluded(config.symbol, t0, reason))
            continue
        assert upper is not None and lower is not None

        label = label_first_touch(
            series.window(t0, horizon_end),
            t0=t0,
            horizon_end=horizon_end,
            upper=upper,
            lower=lower,
        )
        first_touch = label["first_touch"]
        values = {
            "episode_id": deterministic_episode_id(
                symbol=config.symbol,
                t0=t0,
                upper_id=upper.target_id,
                lower_id=lower.target_id,
                horizon_minutes=config.horizon_minutes,
            ),
            "symbol": config.symbol,
            "t0_utc": iso_z(t0),
            "knowledge_cutoff_utc": iso_z(t0),
            "horizon_end_utc": iso_z(horizon_end),
            "price_t0": price_t0,
            "upper_target_id": upper.target_id,
            "upper_target_lower_price": upper.lower_price,
            "upper_target_upper_price": upper.upper_price,
            "upper_touch_price": upper.touch_price,
            "upper_target_available_at": iso_z(upper.available_at),
            "lower_target_id": lower.target_id,
            "lower_target_lower_price": lower.lower_price,
            "lower_target_upper_price": lower.upper_price,
            "lower_touch_price": lower.touch_price,
            "lower_target_available_at": iso_z(lower.available_at),
            "distance_upper_bps": (upper.touch_price - price_t0) / price_t0 * 10000,
            "distance_lower_bps": (price_t0 - lower.touch_price) / price_t0 * 10000,
            "target_source": TARGET_SOURCE,
            "target_contract_version": TARGET_CONTRACT_VERSION,
            "outcome": label["outcome"],
            "first_touch_utc": iso_z(first_touch),
            "upper_touch_utc": iso_z(label["upper_touch"]),
            "lower_touch_utc": iso_z(label["lower_touch"]),
            "eligibility": Eligibility.ELIGIBLE.value,
            "exclusion_reason": "",
            "source_coverage_start_utc": iso_z(series.coverage_start),
            "source_coverage_end_utc": iso_z(series.coverage_end),
            "canonical_snapshot_sha256": str(
                snapshot.get("canonical_snapshot_sha256") or ""
            ),
            "builder_version": BUILDER_VERSION,
            "contract_version": CONTRACT_VERSION,
        }
        episodes.append(FrozenEpisode(values))
        active_until = first_touch or horizon_end
    return active_until, False


def _result(
    config: BuildConfig,
    episodes: list[FrozenEpisode],
    excluded: list[dict[str, Any]],
) -> BuildResult:
    start, end = utc(config.start), utc(config.end)
    outcome_counts = Counter(e.values["outcome"] for e in episodes)
    exclusion_counts = Counter(x["exclusion_reason"] for x in excluded)
    summary = {
        "contract_version": CONTRACT_VERSION,
        "builder_version": BUILDER_VERSION,
        "research_only": True,
        "symbol": config.symbol,
        "candidate_start_utc": iso_z(start),
        "candidate_end_utc": iso_z(end),
        "horizon_minutes": config.horizon_minutes,
        "require_complete": config.require_complete,
        "target_source": config.target_source,
        "price_source": PRICE_SOURCE,
        "candidate_count": len(episodes) + len(excluded),
        "eligible_episode_count": len(episodes),
        "excluded_candidate_count": len(excluded),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "exclusion_counts": dict(sorted(exclusion_counts.items())),
    }
    return BuildResult(tuple(episodes), tuple(excluded), summary)


def _with_coverage(
    episode: FrozenEpisode,
    start: datetime | None,
    end: datetime | None,
) -> FrozenEpisode:
    values = episode.to_dict()
    values["source_coverage_start_utc"] = iso_z(start)
    values["source_coverage_end_utc"] = iso_z(end)
    return FrozenEpisode(values)


def build_episodes(
    config: BuildConfig,
    *,
    trades: TradeSecondSeries | None = None,
    provider: PoolSnapshotProvider | None = None,
    trade_loader: TradeSeriesLoader | None = None,
) -> BuildResult:
    """Build one chronological run; long runs bound trade RAM with internal chunks."""
    start, end = utc(config.start), utc(config.end)
    horizon = timedelta(minutes=config.horizon_minutes)
    target_provider = provider or CanonicalLldPoolProvider()
    episodes: list[FrozenEpisode] = []
    excluded: list[dict[str, Any]] = []
    active_until: datetime | None = None
    window_seconds = (end - start).total_seconds()

    if (
        trades is not None
        or (
            window_seconds <= MAX_CANDIDATE_WINDOW_SECONDS
            and trade_loader is None
        )
    ):
        query_start = start - timedelta(seconds=WARMUP_SECONDS)
        query_end = end + horizon
        series = trades or load_trade_seconds(
            symbol=config.symbol, start=query_start, end=query_end
        )
        _process_grid(
            config,
            start=start,
            end=end,
            series=series,
            target_provider=target_provider,
            episodes=episodes,
            excluded=excluded,
            active_until=active_until,
        )
        return _result(config, episodes, excluded)

    loader = trade_loader or load_trade_seconds
    chunk_start = start
    global_coverage_start: datetime | None = None
    global_coverage_end: datetime | None = None
    carried_bucket = None

    while chunk_start < end:
        chunk_end = min(
            chunk_start + timedelta(seconds=config.trade_chunk_seconds),
            end,
        )
        query_start = chunk_start - timedelta(seconds=WARMUP_SECONDS)
        query_end = chunk_end + horizon
        raw_series = loader(
            symbol=config.symbol,
            start=query_start,
            end=query_end,
        )
        if raw_series.coverage_start is not None:
            global_coverage_start = min(
                x for x in (global_coverage_start, raw_series.coverage_start) if x
            )
        if raw_series.coverage_end is not None:
            global_coverage_end = max(
                x for x in (global_coverage_end, raw_series.coverage_end) if x
            )

        buckets = dict(raw_series.buckets)
        if (
            carried_bucket is not None
            and not any(ts < chunk_start for ts in buckets)
        ):
            buckets[carried_bucket.bucket_start] = carried_bucket
        effective_start = (
            global_coverage_start
            if chunk_start > start
            else raw_series.coverage_start
        )
        effective_end = (
            query_end if chunk_end < end and buckets else raw_series.coverage_end
        )
        series = TradeSecondSeries(
            buckets=buckets,
            coverage_start=effective_start,
            coverage_end=effective_end,
            source_complete=raw_series.source_complete,
        )
        active_until, stopped = _process_grid(
            config,
            start=chunk_start,
            end=chunk_end,
            series=series,
            target_provider=target_provider,
            episodes=episodes,
            excluded=excluded,
            active_until=active_until,
        )
        if stopped:
            break

        next_query_start = chunk_end - timedelta(seconds=WARMUP_SECONDS)
        historical = [
            bucket
            for ts, bucket in raw_series.buckets.items()
            if ts < next_query_start
        ]
        if historical:
            carried_bucket = max(historical, key=lambda x: x.bucket_start)
        chunk_start = chunk_end

    normalized = [
        _with_coverage(e, global_coverage_start, global_coverage_end)
        for e in episodes
    ]
    return _result(config, normalized, excluded)


def _write_csv(path: Path, columns: tuple[str, ...], rows_: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows_)


def _json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_artifacts(
    result: BuildResult,
    output_dir: Path,
    *,
    generated_at: datetime | None = None,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        output_dir / "episodes.csv",
        EPISODE_COLUMNS,
        [e.to_dict() for e in result.episodes],
    )
    _write_csv(
        output_dir / "excluded_candidates.csv",
        EXCLUDED_COLUMNS,
        list(result.excluded),
    )
    _json(output_dir / "summary.json", result.summary)
    _json(output_dir / "contract.json", contract_dict())
    core_names = ("episodes.csv", "excluded_candidates.csv", "summary.json", "contract.json")
    hashes = {name: _sha(output_dir / name) for name in core_names}
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "builder_version": BUILDER_VERSION,
        "research_only": True,
        "generated_at_utc": iso_z(generated_at or datetime.now(timezone.utc)),
        "core_artifact_sha256": hashes,
        "core_fingerprint_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    _json(output_dir / "run_manifest.json", manifest)
    return hashes
