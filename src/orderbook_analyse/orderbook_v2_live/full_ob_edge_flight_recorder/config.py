"""Full-OB Edge Flight Recorder V1 — config (shadow pilot, disabled by default)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONTRACT_VERSION = "full_ob_edge_capture_timing_v1"
SCHEMA_VERSION = "1.0.0"
DEFAULT_PILOT = frozenset({"BTCUSDT", "DOGEUSDT"})

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "orderbook_raw_shadow" / "full_ob_edge_flight_recorder"

KEEPER_LEASE_PREFIX = "full-ob-fr-keeper-"
KEEPER_SESSION = "full-ob-edge-flight-recorder"


@dataclass(frozen=True)
class FlightRecorderSettings:
    enabled: bool = False
    symbols: frozenset[str] = DEFAULT_PILOT
    capture_root: Path = DEFAULT_ROOT
    arm_distance_bps: float = 50.0
    capture_distance_bps: float = 20.0
    disarm_distance_bps: float = 75.0
    fast_approach_bps_per_sec: float = 8.0
    ringbuffer_minutes: float = 10.0
    max_buffer_messages: int = 50_000
    max_buffer_bytes: int = 256 * 1024 * 1024
    minimum_post_capture_minutes: float = 60.0
    reclaim_post_capture_minutes: float = 10.0
    maximum_event_minutes: float = 180.0
    extension_minutes: float = 30.0
    cooldown_minutes: float = 5.0
    acceptance_hold_sec: float = 60.0
    profile_poll_sec: float = 20.0
    profile_window_minutes: int = 30
    queue_size: int = 16384
    writer_batch_max_messages: int = 64
    writer_batch_max_bytes: int = 256 * 1024
    writer_flush_interval_sec: float = 1.0
    min_free_disk_gb: float = 5.0
    warn_free_disk_gb: float = 20.0
    compression_level: int = 3
    max_parallel_events: int = 2
    segment_minutes: float = 30.0
    max_open_tmp_bytes: int = 256 * 1024 * 1024
    projected_daily_warn_bytes: int = 2 * 1024 * 1024 * 1024
    nested_profile_signals_enabled: bool = True
    max_active_profile_watches: int = 8

    def should_watch(self, symbol: str) -> bool:
        return self.enabled and symbol.upper() in self.symbols

    @property
    def pre_seconds(self) -> float:
        return self.ringbuffer_minutes * 60.0

    @property
    def min_post_seconds(self) -> float:
        return self.minimum_post_capture_minutes * 60.0

    @property
    def result_tail_seconds(self) -> float:
        return self.reclaim_post_capture_minutes * 60.0

    @property
    def max_seconds(self) -> float:
        return self.maximum_event_minutes * 60.0

    @property
    def extension_seconds(self) -> float:
        return self.extension_minutes * 60.0

    @property
    def segment_seconds(self) -> float:
        return self.segment_minutes * 60.0


def _b(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _first_float(names: list[str], default: float) -> float:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip() != "":
            return float(raw)
    return default


def load_flight_recorder_settings() -> FlightRecorderSettings:
    enabled = _b("OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE", False)
    symbols_raw = os.environ.get("OB_V3_FULL_OB_FR_SYMBOLS") or ""
    symbols = frozenset(s.strip().upper() for s in symbols_raw.split(",") if s.strip())
    if enabled and not symbols:
        symbols = DEFAULT_PILOT
    root = Path(os.environ.get("OB_V3_FULL_OB_FR_ROOT") or str(DEFAULT_ROOT))

    pre_sec = os.environ.get("EDGE_CAPTURE_PRE_SECONDS")
    ringbuffer_minutes = (
        float(pre_sec) / 60.0
        if pre_sec
        else _first_float(["OB_V3_FULL_OB_FR_RINGBUFFER_MIN"], 10.0)
    )
    min_post_sec = os.environ.get("EDGE_CAPTURE_MIN_POST_SECONDS")
    minimum_post_capture_minutes = (
        float(min_post_sec) / 60.0
        if min_post_sec
        else _first_float(["OB_V3_FULL_OB_FR_MIN_POST_MIN"], 60.0)
    )
    tail_sec = os.environ.get("EDGE_CAPTURE_RESULT_TAIL_SECONDS")
    reclaim_post_capture_minutes = (
        float(tail_sec) / 60.0
        if tail_sec
        else _first_float(["OB_V3_FULL_OB_FR_RECLAIM_MIN"], 10.0)
    )
    max_sec = os.environ.get("EDGE_CAPTURE_MAX_SECONDS")
    maximum_event_minutes = (
        float(max_sec) / 60.0
        if max_sec
        else _first_float(["OB_V3_FULL_OB_FR_MAX_EVENT_MIN"], 180.0)
    )
    ext_sec = os.environ.get("EDGE_CAPTURE_EXTENSION_SECONDS")
    extension_minutes = (
        float(ext_sec) / 60.0
        if ext_sec
        else _first_float(["OB_V3_FULL_OB_FR_EXTENSION_MIN"], 30.0)
    )
    seg_sec = os.environ.get("EDGE_CAPTURE_SEGMENT_SECONDS")
    segment_minutes = (
        float(seg_sec) / 60.0
        if seg_sec
        else _first_float(["OB_V3_FULL_OB_FR_SEGMENT_MIN"], 30.0)
    )
    seg_bytes = os.environ.get("EDGE_CAPTURE_SEGMENT_MAX_BYTES")
    max_open_tmp_bytes = (
        int(float(seg_bytes))
        if seg_bytes
        else int(_first_float(["OB_V3_FULL_OB_FR_MAX_OPEN_TMP_BYTES"], float(256 * 1024 * 1024)))
    )

    return FlightRecorderSettings(
        enabled=enabled,
        symbols=symbols,
        capture_root=root,
        arm_distance_bps=float(os.environ.get("OB_V3_FULL_OB_FR_ARM_BPS") or "50"),
        capture_distance_bps=float(os.environ.get("OB_V3_FULL_OB_FR_CAPTURE_BPS") or "20"),
        disarm_distance_bps=float(os.environ.get("OB_V3_FULL_OB_FR_DISARM_BPS") or "75"),
        fast_approach_bps_per_sec=float(os.environ.get("OB_V3_FULL_OB_FR_FAST_APPROACH") or "8"),
        ringbuffer_minutes=ringbuffer_minutes,
        max_buffer_messages=int(os.environ.get("OB_V3_FULL_OB_FR_MAX_MSGS") or "50000"),
        max_buffer_bytes=int(os.environ.get("OB_V3_FULL_OB_FR_MAX_BYTES") or str(256 * 1024 * 1024)),
        minimum_post_capture_minutes=minimum_post_capture_minutes,
        reclaim_post_capture_minutes=reclaim_post_capture_minutes,
        maximum_event_minutes=maximum_event_minutes,
        extension_minutes=extension_minutes,
        cooldown_minutes=float(os.environ.get("OB_V3_FULL_OB_FR_COOLDOWN_MIN") or "5"),
        profile_poll_sec=float(os.environ.get("OB_V3_FULL_OB_FR_PROFILE_POLL_SEC") or "20"),
        profile_window_minutes=int(os.environ.get("OB_V3_FULL_OB_FR_PROFILE_WINDOW_MIN") or "30"),
        queue_size=int(os.environ.get("OB_V3_FULL_OB_FR_QUEUE_SIZE") or "16384"),
        writer_batch_max_messages=int(os.environ.get("OB_V3_FULL_OB_FR_BATCH_MAX_MSGS") or "64"),
        writer_batch_max_bytes=int(os.environ.get("OB_V3_FULL_OB_FR_BATCH_MAX_BYTES") or str(256 * 1024)),
        writer_flush_interval_sec=float(os.environ.get("OB_V3_FULL_OB_FR_FLUSH_SEC") or "1.0"),
        min_free_disk_gb=float(os.environ.get("OB_V3_FULL_OB_FR_MIN_FREE_GB") or "5"),
        warn_free_disk_gb=float(os.environ.get("OB_V3_FULL_OB_FR_WARN_FREE_GB") or "20"),
        max_parallel_events=int(os.environ.get("OB_V3_FULL_OB_FR_MAX_PARALLEL") or "2"),
        segment_minutes=segment_minutes,
        max_open_tmp_bytes=max_open_tmp_bytes,
        projected_daily_warn_bytes=int(
            os.environ.get("OB_V3_FULL_OB_FR_PROJECTED_DAILY_WARN_BYTES") or str(2 * 1024 * 1024 * 1024)
        ),
        nested_profile_signals_enabled=_b("OB_V3_FULL_OB_FR_NESTED_SIGNALS_ENABLE", True),
        max_active_profile_watches=int(os.environ.get("OB_V3_FULL_OB_FR_MAX_PROFILE_WATCHES") or "8"),
    )
