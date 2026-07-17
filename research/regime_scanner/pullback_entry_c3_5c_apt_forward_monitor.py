"""C3.5c APTUSDT 15m A6 paper-forward monitor (research-only).

Frozen hypothesis: TRIGGER on confirmed 15m close → fill next open;
exit / reverse on next opposite filled entry open.

No live orders. No SM / Pine changes. No historical trades as forward.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from research.regime_scanner.indicator_feature_store import (
    detect_timestamp_gaps,
    load_ohlcv_with_warmup,
    required_indicator_warmup_bars,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    apply_pullback_entry,
    config_hash,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    aggregate_complete_from_5m,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

LOGGER = logging.getLogger("c35c_apt_forward_monitor")

SCHEMA_VERSION = 1
SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
VARIANT = "A6"
BAR_MINUTES = 15
WARMUP_CALENDAR_DAYS = 45
ROUNDTRIP_COSTS = (0.10, 0.20, 0.30, 0.40)
SNAPSHOT_THRESHOLDS = (10, 25, 50, 75, 100)

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/c35c_apt_forward_monitor"
)
SM_SOURCE_PATH = Path("research/regime_scanner/pullback_entry_c3_5.py")

TRADE_CSV_HEADER = [
    "trade_id",
    "symbol",
    "timeframe",
    "variant",
    "arming_mode",
    "side",
    "setup_id",
    "trigger_timestamp",
    "trigger_price",
    "entry_timestamp",
    "entry_price",
    "exit_trigger_timestamp",
    "exit_timestamp",
    "exit_price",
    "exit_reason",
    "holding_bars",
    "holding_hours",
    "gross_return_pct",
    "net_return_0_10_pct",
    "net_return_0_20_pct",
    "net_return_0_30_pct",
    "net_return_0_40_pct",
    "mfe_pct",
    "mae_pct",
    "mfe_timestamp",
    "mae_timestamp",
    "best_move_before_worst",
    "worst_move_before_best",
    "entry_month",
    "forward_sequence_number",
    "config_hash",
    "source_hash",
]


class ConfigMismatchError(RuntimeError):
    pass


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def parse_utc(value: str | pd.Timestamp | datetime | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def last_complete_15m_open(now: pd.Timestamp | None = None) -> pd.Timestamp:
    now = parse_utc(now) or utc_now()
    return (now - pd.Timedelta(minutes=BAR_MINUTES)).floor(f"{BAR_MINUTES}min")


def file_sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest() if path.exists() else "missing"


def frozen_config() -> Any:
    return baseline_a6()


def frozen_hashes() -> dict[str, str]:
    cfg = frozen_config()
    return {
        "config_hash": config_hash(cfg),
        "source_hash": file_sha1(SM_SOURCE_PATH),
        "arming_mode": str(cfg.arming_type),
        "variant": str(cfg.name),
        "mtf_mode": str(cfg.mtf_mode),
    }


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(json_safe(row), sort_keys=True) + "\n")


def ensure_trade_csv(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(",".join(TRADE_CSV_HEADER) + "\n", encoding="utf-8")


def append_trade_csv(path: Path, row: Mapping[str, Any]) -> None:
    ensure_trade_csv(path)
    values = []
    for col in TRADE_CSV_HEADER:
        v = row.get(col, "")
        if v is None:
            v = ""
        s = str(v)
        if any(ch in s for ch in [",", '"', "\n"]):
            s = '"' + s.replace('"', '""') + '"'
        values.append(s)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(",".join(values) + "\n")


def ret_pct(side: str, entry: float, exit_px: float) -> float:
    if side == "long":
        return (exit_px / entry - 1.0) * 100.0
    return (entry / exit_px - 1.0) * 100.0


def mfe_mae_long(entry: float, high: float, low: float) -> tuple[float, float]:
    return (high / entry - 1.0) * 100.0, (low / entry - 1.0) * 100.0


def mfe_mae_short(entry: float, high: float, low: float) -> tuple[float, float]:
    return (entry - low) / entry * 100.0, (entry - high) / entry * 100.0


@dataclass
class PendingTrigger:
    side: str
    setup_id: int | None
    trigger_timestamp: str
    trigger_price: float
    trigger_bar_timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "PendingTrigger | None":
        if not d:
            return None
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


@dataclass
class OpenPosition:
    trade_id: str
    side: str
    setup_id: int | None
    trigger_timestamp: str
    trigger_price: float
    entry_timestamp: str
    entry_price: float
    entry_bar_timestamp: str
    entry_bar_index: int | None = None
    status: str = "open"
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    mfe_timestamp: str | None = None
    mae_timestamp: str | None = None
    bars_held: int = 0
    hours_held: float = 0.0
    path_high: float = 0.0
    path_low: float = 0.0
    best_move_before_worst: bool | None = None
    worst_move_before_best: bool | None = None
    first_ext_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "OpenPosition | None":
        if not d:
            return None
        keys = cls.__dataclass_fields__
        return cls(**{k: d[k] for k in keys if k in d})


@dataclass
class MonitorState:
    schema_version: int = SCHEMA_VERSION
    symbol: str = SYMBOL
    timeframe: str = TIMEFRAME
    variant: str = VARIANT
    arming_mode: str = "external_bos"
    forward_start_utc: str | None = None
    last_processed_bar_timestamp: str | None = None
    last_processed_bar_close: float | None = None
    active_position: dict[str, Any] | None = None
    pending_trigger: dict[str, Any] | None = None
    last_trigger: dict[str, Any] | None = None
    last_fill: dict[str, Any] | None = None
    n_closed_trades: int = 0
    n_long_trades: int = 0
    n_short_trades: int = 0
    cum_gross_pct: float = 0.0
    cum_net_0_10_pct: float = 0.0
    cum_net_0_20_pct: float = 0.0
    cum_net_0_30_pct: float = 0.0
    cum_net_0_40_pct: float = 0.0
    config_hash: str = ""
    source_hash: str = ""
    review_status: str = "collecting_forward_data"
    snapshots_written: list[int] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "MonitorState":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        if "snapshots_written" in known and known["snapshots_written"] is None:
            known["snapshots_written"] = []
        return cls(**known)


class ForwardMonitor:
    """Persistent paper-forward monitor for frozen C3.5c A6 APT 15m."""

    def __init__(
        self,
        output_dir: Path = DEFAULT_OUT,
        *,
        forward_start_utc: str | pd.Timestamp | None = None,
        dry_run: bool = False,
        data_source: str = "feather",
    ) -> None:
        assert_safe_output_dir(output_dir)
        self.output_dir = Path(output_dir)
        self.dry_run = dry_run
        self.data_source = data_source
        self.cli_forward_start = parse_utc(forward_start_utc)
        self.cfg = frozen_config()
        hashes = frozen_hashes()
        self.config_hash = hashes["config_hash"]
        self.source_hash = hashes["source_hash"]
        self.arming_mode = hashes["arming_mode"]

        self.state_path = self.output_dir / "state.json"
        self.events_path = self.output_dir / "events.jsonl"
        self.trades_path = self.output_dir / "forward_trades.csv"
        self.open_path = self.output_dir / "open_position.json"
        self.status_path = self.output_dir / "status.json"
        self.report_path = self.output_dir / "report.md"
        self.lock_path = self.output_dir / ".monitor.lock"
        self.snapshots_dir = self.output_dir / "snapshots"
        self.logs_dir = self.output_dir / "logs"
        self.meta_path = self.output_dir / "metadata.json"

        self.state: MonitorState | None = None
        self._event_seq = 0
        self._seen_bar_emits: set[str] = set()

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        ensure_trade_csv(self.trades_path)

    def acquire_lock(self):
        self.ensure_dirs()
        fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise RuntimeError("another monitor instance holds the lock") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} ts={utc_now().isoformat()}\n")
        fh.flush()
        return fh

    def load_or_init_state(self, *, last_complete_open: pd.Timestamp) -> MonitorState:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            st = MonitorState.from_dict(raw)
            if st.config_hash and st.config_hash != self.config_hash:
                self.state = st
                self._emit(
                    "config_mismatch",
                    reason=f"config_hash {st.config_hash} != {self.config_hash}",
                    processed_bar_timestamp=st.last_processed_bar_timestamp,
                )
                raise ConfigMismatchError(
                    f"config_hash mismatch: state={st.config_hash} current={self.config_hash}"
                )
            if st.source_hash and st.source_hash != self.source_hash:
                self.state = st
                self._emit(
                    "config_mismatch",
                    reason=f"source_hash {st.source_hash} != {self.source_hash}",
                    processed_bar_timestamp=st.last_processed_bar_timestamp,
                )
                raise ConfigMismatchError(
                    f"source_hash mismatch: state={st.source_hash} current={self.source_hash}"
                )
            if st.forward_start_utc is None:
                raise ConfigMismatchError("state missing forward_start_utc")
            if self.cli_forward_start is not None:
                existing = parse_utc(st.forward_start_utc)
                if existing != self.cli_forward_start:
                    raise ConfigMismatchError(
                        f"forward_start_utc frozen at {st.forward_start_utc}; "
                        f"CLI asked {self.cli_forward_start.isoformat()}"
                    )
            self.state = st
            self._emit("monitor_resumed", reason="state_loaded", processed_bar_timestamp=st.last_processed_bar_timestamp)
            self._emit("state_recovered", reason="ok", processed_bar_timestamp=st.last_processed_bar_timestamp)
            return st

        if self.cli_forward_start is not None:
            fwd = self.cli_forward_start
            origin = "cli"
        else:
            fwd = last_complete_open
            origin = "last_complete_15m_bar"
            LOGGER.info(
                "No --forward-start-utc; using last complete 15m open as forward boundary: %s",
                fwd.isoformat(),
            )
        now = utc_now().isoformat()
        st = MonitorState(
            arming_mode=self.arming_mode,
            forward_start_utc=fwd.isoformat(),
            # Skip trading-processing of bars at/before the boundary; they remain
            # in the research frame as SM/indicator warmup only.
            last_processed_bar_timestamp=fwd.isoformat(),
            config_hash=self.config_hash,
            source_hash=self.source_hash,
            created_at=now,
            updated_at=now,
            review_status="collecting_forward_data",
        )
        self.state = st
        self._emit(
            "monitor_started",
            reason=f"forward_start_origin={origin}",
            processed_bar_timestamp=None,
            extra={"forward_start_utc": st.forward_start_utc},
        )
        return st

    def save_state(self) -> None:
        assert self.state is not None
        self.state.updated_at = utc_now().isoformat()
        if self.dry_run:
            return
        atomic_write_json(self.state_path, self.state.to_dict())
        pos = self.state.active_position
        atomic_write_json(self.open_path, pos if pos is not None else {"active_position": None})
        self._emit(
            "checkpoint_written",
            reason="state.json",
            processed_bar_timestamp=self.state.last_processed_bar_timestamp,
        )

    def _emit(
        self,
        event_type: str,
        *,
        reason: str | None = None,
        processed_bar_timestamp: str | None = None,
        extra: Mapping[str, Any] | None = None,
        side: str | None = None,
        setup_id: Any = None,
        trade_id: str | None = None,
        price: float | None = None,
    ) -> None:
        self._event_seq += 1
        active = None
        if self.state and self.state.active_position:
            active = self.state.active_position.get("side")
        row = {
            "event_id": f"{utc_now().strftime('%Y%m%dT%H%M%S')}-{self._event_seq:06d}-{uuid.uuid4().hex[:8]}",
            "timestamp_utc": utc_now().isoformat(),
            "processed_bar_timestamp": processed_bar_timestamp,
            "event_type": event_type,
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "variant": VARIANT,
            "setup_id": setup_id,
            "trade_id": trade_id,
            "side": side,
            "active_position_side": active,
            "price": price,
            "reason": reason,
            "config_hash": self.config_hash,
            "source_hash": self.source_hash,
        }
        if extra:
            row["extra"] = dict(extra)
        if self.dry_run:
            return
        append_jsonl(self.events_path, row)


    def load_15m_frame(self, *, asof: pd.Timestamp | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
        asof = parse_utc(asof) or utc_now()
        last_open = last_complete_15m_open(asof)
        decision = last_open + pd.Timedelta(minutes=BAR_MINUTES)
        fwd = parse_utc(self.state.forward_start_utc) if self.state else (self.cli_forward_start or last_open)
        analyze_start = fwd - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
        warm_bars = max(required_indicator_warmup_bars() * 3, 2000)
        full_5m, _ = load_ohlcv_with_warmup(
            SYMBOL,
            "5m",
            analyze_start=analyze_start,
            analyze_end=decision,
            warmup_bars=warm_bars,
        )
        ohlcv = aggregate_complete_from_5m(full_5m, TIMEFRAME, decision_time=decision)
        gaps5 = detect_timestamp_gaps(full_5m, "5m") if not full_5m.empty else []
        if ohlcv.empty:
            return pd.DataFrame(), {
                "last_complete_open": last_open.isoformat(),
                "decision": decision.isoformat(),
                "n_5m": len(full_5m),
                "n_15m": 0,
                "n_5m_gaps": len(gaps5),
            }
        frame = prepare_research_frame(ohlcv, ohlcv_15m=None, ohlcv_30m=None)
        ts = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame.loc[ts <= last_open].copy().reset_index(drop=True)
        frame["bar_index"] = range(len(frame))
        frame["symbol"] = SYMBOL
        frame["timeframe"] = TIMEFRAME
        gaps15 = detect_timestamp_gaps(frame, TIMEFRAME) if len(frame) > 1 else []
        meta = {
            "last_complete_open": last_open.isoformat(),
            "decision": decision.isoformat(),
            "n_5m": len(full_5m),
            "n_15m": len(frame),
            "n_5m_gaps": len(gaps5),
            "n_15m_gaps": len(gaps15),
            "gap_5m_samples": gaps5[:3],
            "gap_15m_samples": gaps15[:3],
            "data_source": self.data_source,
            "exchange": "bybit",
            "aggregation": "complete_5m_buckets_only",
        }
        return frame, meta

    def _triggers_by_bar(
        self, entries: Sequence[Mapping[str, Any]], frame: pd.DataFrame
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        ts = list(pd.to_datetime(frame["timestamp"], utc=True))
        closes = frame["close"].astype(float).to_numpy()
        for e in entries:
            tb = e.get("trigger_bar")
            if tb is None:
                continue
            tb = int(tb)
            if tb < 0 or tb >= len(frame):
                continue
            side = int(e.get("side") or 0)
            if side not in (-1, 1):
                continue
            trig_ts = pd.Timestamp(ts[tb]).tz_convert("UTC")
            out[trig_ts.isoformat()] = {
                "side": "long" if side > 0 else "short",
                "setup_id": e.get("setup_id"),
                "trigger_timestamp": trig_ts.isoformat(),
                "trigger_price": float(closes[tb]),
                "trigger_bar": tb,
                "entry_price_next_open": e.get("entry_price"),
            }
        return out

    def process_frame(
        self, frame: pd.DataFrame, *, data_meta: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        assert self.state is not None
        if frame.empty:
            return self.build_status(data_meta=data_meta)

        fwd = parse_utc(self.state.forward_start_utc)
        assert fwd is not None
        _tl, entries, lives = apply_pullback_entry(frame, self.cfg, return_lifecycles=True)
        triggers = self._triggers_by_bar(entries, frame)
        ts_series = pd.to_datetime(frame["timestamp"], utc=True)

        if self.state.last_processed_bar_timestamp:
            last = parse_utc(self.state.last_processed_bar_timestamp)
            expected = last + pd.Timedelta(minutes=BAR_MINUTES)
            after = frame.loc[ts_series > last]
            if len(after):
                first_new = pd.Timestamp(after.iloc[0]["timestamp"]).tz_convert("UTC")
                if first_new != expected:
                    self._emit(
                        "data_gap_detected",
                        reason=f"expected {expected.isoformat()} got {first_new.isoformat()}",
                        processed_bar_timestamp=first_new.isoformat(),
                    )

        for i in range(len(frame)):
            bar_ts = pd.Timestamp(ts_series.iloc[i]).tz_convert("UTC")
            bar_iso = bar_ts.isoformat()
            if self.state.last_processed_bar_timestamp:
                last = parse_utc(self.state.last_processed_bar_timestamp)
                if bar_ts <= last:
                    # idempotent skip — do not spam duplicate events on every resume
                    continue

            row = frame.iloc[i]
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])

            pending = PendingTrigger.from_dict(self.state.pending_trigger)
            if pending is not None:
                self._handle_fill(
                    pending, fill_ts=bar_ts, fill_price=o, bar_iso=bar_iso, bar_index=i
                )

            if self.state.active_position is not None:
                self._update_open_path(h=h, l=l, c=c, bar_ts=bar_ts)

            if bar_ts > fwd:
                self._log_lifecycle_events(lives, bar_index=i, bar_iso=bar_iso)

            trig = triggers.get(bar_iso)
            if trig is not None:
                trig_ts = parse_utc(trig["trigger_timestamp"])
                if trig_ts is not None and trig_ts > fwd:
                    self._confirm_trigger(trig, bar_iso=bar_iso, close=c)
                elif trig_ts is not None and trig_ts <= fwd:
                    self._emit(
                        "trigger_confirmed",
                        reason="ignored_pre_forward_start",
                        processed_bar_timestamp=bar_iso,
                        side=trig["side"],
                        setup_id=trig.get("setup_id"),
                        price=float(c),
                        extra={"forward_start_utc": self.state.forward_start_utc},
                    )

            self.state.last_processed_bar_timestamp = bar_iso
            self.state.last_processed_bar_close = c
            self._emit("bar_processed", processed_bar_timestamp=bar_iso, price=c)

        self._refresh_review_status()
        self.save_state()
        status = self.build_status(data_meta=data_meta)
        if not self.dry_run:
            atomic_write_json(self.status_path, status)
            self.write_report(status)
            self._write_metadata(data_meta or {})
            self._write_derived_tables()
        return status

    def process_with_synthetic_triggers(
        self,
        frame: pd.DataFrame,
        triggers_by_iso: Mapping[str, Mapping[str, Any]],
        *,
        data_meta: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Test helper: process bars using injected trigger map (no SM required)."""
        assert self.state is not None
        fwd = parse_utc(self.state.forward_start_utc)
        assert fwd is not None
        ts_series = pd.to_datetime(frame["timestamp"], utc=True)
        for i in range(len(frame)):
            bar_ts = pd.Timestamp(ts_series.iloc[i]).tz_convert("UTC")
            bar_iso = bar_ts.isoformat()
            if self.state.last_processed_bar_timestamp:
                last = parse_utc(self.state.last_processed_bar_timestamp)
                if bar_ts <= last:
                    continue
            row = frame.iloc[i]
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            pending = PendingTrigger.from_dict(self.state.pending_trigger)
            if pending is not None:
                self._handle_fill(
                    pending, fill_ts=bar_ts, fill_price=o, bar_iso=bar_iso, bar_index=i
                )
            if self.state.active_position is not None:
                self._update_open_path(h=h, l=l, c=c, bar_ts=bar_ts)
            trig = triggers_by_iso.get(bar_iso)
            if trig is not None:
                trig_ts = parse_utc(trig["trigger_timestamp"])
                if trig_ts is not None and trig_ts > fwd:
                    self._confirm_trigger(trig, bar_iso=bar_iso, close=c)
            self.state.last_processed_bar_timestamp = bar_iso
            self.state.last_processed_bar_close = c
            self._emit("bar_processed", processed_bar_timestamp=bar_iso, price=c)
        self._refresh_review_status()
        self.save_state()
        status = self.build_status(data_meta=data_meta)
        if not self.dry_run:
            atomic_write_json(self.status_path, status)
            self.write_report(status)
            self._write_metadata(data_meta or {})
            self._write_derived_tables()
        return status


    def _confirm_trigger(self, trig: Mapping[str, Any], *, bar_iso: str, close: float) -> None:
        assert self.state is not None
        pending = PendingTrigger(
            side=str(trig["side"]),
            setup_id=trig.get("setup_id"),
            trigger_timestamp=str(trig["trigger_timestamp"]),
            trigger_price=float(trig["trigger_price"]),
            trigger_bar_timestamp=bar_iso,
        )
        self.state.pending_trigger = pending.to_dict()
        self.state.last_trigger = pending.to_dict()
        self._emit(
            "trigger_confirmed",
            reason="forward_trigger",
            processed_bar_timestamp=bar_iso,
            side=pending.side,
            setup_id=pending.setup_id,
            price=close,
        )

    def _handle_fill(
        self,
        pending: PendingTrigger,
        *,
        fill_ts: pd.Timestamp,
        fill_price: float,
        bar_iso: str,
        bar_index: int | None = None,
    ) -> None:
        assert self.state is not None
        active = OpenPosition.from_dict(self.state.active_position)
        self.state.pending_trigger = None

        if active is not None and active.side == pending.side:
            self._emit(
                "same_direction_signal_ignored",
                reason="same_direction_while_open",
                processed_bar_timestamp=bar_iso,
                side=pending.side,
                setup_id=pending.setup_id,
                price=fill_price,
                trade_id=active.trade_id,
            )
            return

        if active is not None and active.side != pending.side:
            self._close_position(
                active,
                exit_trigger_timestamp=pending.trigger_timestamp,
                exit_ts=fill_ts,
                exit_price=fill_price,
                exit_reason="opposite_c35c_entry",
                bar_iso=bar_iso,
                reversed_=True,
            )
            self._emit(
                "opposite_signal_confirmed",
                reason="closes_and_opens",
                processed_bar_timestamp=bar_iso,
                side=pending.side,
                setup_id=pending.setup_id,
                price=fill_price,
            )

        trade_id = f"fwd-{self.state.n_closed_trades + 1:05d}-{uuid.uuid4().hex[:8]}"
        pos = OpenPosition(
            trade_id=trade_id,
            side=pending.side,
            setup_id=pending.setup_id,
            trigger_timestamp=pending.trigger_timestamp,
            trigger_price=pending.trigger_price,
            entry_timestamp=fill_ts.isoformat(),
            entry_price=fill_price,
            entry_bar_timestamp=bar_iso,
            entry_bar_index=bar_index,
            path_high=fill_price,
            path_low=fill_price,
            mfe_pct=0.0,
            mae_pct=0.0,
            mfe_timestamp=fill_ts.isoformat(),
            mae_timestamp=fill_ts.isoformat(),
        )
        self.state.active_position = pos.to_dict()
        self.state.last_fill = {
            "trade_id": trade_id,
            "side": pending.side,
            "entry_timestamp": fill_ts.isoformat(),
            "entry_price": fill_price,
            "setup_id": pending.setup_id,
        }
        self._emit(
            "entry_filled",
            reason="next_open_fill",
            processed_bar_timestamp=bar_iso,
            side=pending.side,
            setup_id=pending.setup_id,
            trade_id=trade_id,
            price=fill_price,
        )
        if active is not None and active.side != pending.side:
            self._emit(
                "position_reversed",
                reason="opposite_fill",
                processed_bar_timestamp=bar_iso,
                side=pending.side,
                trade_id=trade_id,
                price=fill_price,
            )

    def _update_open_path(self, *, h: float, l: float, c: float, bar_ts: pd.Timestamp) -> None:
        assert self.state is not None
        pos = OpenPosition.from_dict(self.state.active_position)
        assert pos is not None
        pos.path_high = max(pos.path_high, h)
        pos.path_low = min(pos.path_low, l) if pos.path_low != 0 else l
        if pos.side == "long":
            mfe, mae = mfe_mae_long(pos.entry_price, pos.path_high, pos.path_low)
            mtm = (c / pos.entry_price - 1.0) * 100.0
        else:
            mfe, mae = mfe_mae_short(pos.entry_price, pos.path_high, pos.path_low)
            mtm = (pos.entry_price / c - 1.0) * 100.0
        if pos.first_ext_kind is None:
            if pos.side == "long":
                up = (h / pos.entry_price - 1.0) * 100.0
                dn = (l / pos.entry_price - 1.0) * 100.0
                if up > 0 and (dn >= 0 or up >= abs(dn)):
                    pos.first_ext_kind = "mfe"
                elif dn < 0:
                    pos.first_ext_kind = "mae"
            else:
                dn = (pos.entry_price - l) / pos.entry_price * 100.0
                up = (pos.entry_price - h) / pos.entry_price * 100.0
                if dn > 0 and (up >= 0 or dn >= abs(up)):
                    pos.first_ext_kind = "mfe"
                elif up < 0:
                    pos.first_ext_kind = "mae"
        if mfe > pos.mfe_pct:
            pos.mfe_pct = mfe
            pos.mfe_timestamp = bar_ts.isoformat()
        if mae < pos.mae_pct:
            pos.mae_pct = mae
            pos.mae_timestamp = bar_ts.isoformat()
        entry_ts = parse_utc(pos.entry_timestamp)
        pos.bars_held = (
            int((bar_ts - entry_ts) / pd.Timedelta(minutes=BAR_MINUTES)) if entry_ts else pos.bars_held
        )
        pos.hours_held = pos.bars_held * (BAR_MINUTES / 60.0)
        pos.best_move_before_worst = pos.first_ext_kind == "mfe"
        pos.worst_move_before_best = pos.first_ext_kind == "mae"
        self.state.active_position = pos.to_dict()
        self.state.active_position["mtm_pct"] = mtm

    def _close_position(
        self,
        pos: OpenPosition,
        *,
        exit_trigger_timestamp: str,
        exit_ts: pd.Timestamp,
        exit_price: float,
        exit_reason: str,
        bar_iso: str,
        reversed_: bool,
    ) -> None:
        assert self.state is not None
        gross = ret_pct(pos.side, pos.entry_price, exit_price)
        nets = {c: gross - c for c in ROUNDTRIP_COSTS}
        entry_ts = parse_utc(pos.entry_timestamp)
        hold_bars = int((exit_ts - entry_ts) / pd.Timedelta(minutes=BAR_MINUTES)) if entry_ts else 0
        seq = self.state.n_closed_trades + 1
        trade = {
            "trade_id": pos.trade_id,
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "variant": VARIANT,
            "arming_mode": self.arming_mode,
            "side": pos.side,
            "setup_id": pos.setup_id,
            "trigger_timestamp": pos.trigger_timestamp,
            "trigger_price": pos.trigger_price,
            "entry_timestamp": pos.entry_timestamp,
            "entry_price": pos.entry_price,
            "exit_trigger_timestamp": exit_trigger_timestamp,
            "exit_timestamp": exit_ts.isoformat(),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "holding_bars": hold_bars,
            "holding_hours": hold_bars * (BAR_MINUTES / 60.0),
            "gross_return_pct": gross,
            "net_return_0_10_pct": nets[0.10],
            "net_return_0_20_pct": nets[0.20],
            "net_return_0_30_pct": nets[0.30],
            "net_return_0_40_pct": nets[0.40],
            "mfe_pct": pos.mfe_pct,
            "mae_pct": pos.mae_pct,
            "mfe_timestamp": pos.mfe_timestamp,
            "mae_timestamp": pos.mae_timestamp,
            "best_move_before_worst": pos.best_move_before_worst,
            "worst_move_before_best": pos.worst_move_before_best,
            "entry_month": pd.Timestamp(pos.entry_timestamp).tz_convert("UTC").strftime("%Y-%m"),
            "forward_sequence_number": seq,
            "config_hash": self.config_hash,
            "source_hash": self.source_hash,
        }
        if not self.dry_run:
            append_trade_csv(self.trades_path, trade)

        self.state.n_closed_trades = seq
        if pos.side == "long":
            self.state.n_long_trades += 1
        else:
            self.state.n_short_trades += 1
        self.state.cum_gross_pct += gross
        self.state.cum_net_0_10_pct += nets[0.10]
        self.state.cum_net_0_20_pct += nets[0.20]
        self.state.cum_net_0_30_pct += nets[0.30]
        self.state.cum_net_0_40_pct += nets[0.40]
        self.state.active_position = None

        self._emit(
            "position_closed",
            reason=exit_reason,
            processed_bar_timestamp=bar_iso,
            side=pos.side,
            setup_id=pos.setup_id,
            trade_id=pos.trade_id,
            price=exit_price,
            extra={"gross_return_pct": gross, "reversed": reversed_},
        )
        self._maybe_snapshot()

    def _log_lifecycle_events(
        self, lives: Sequence[Mapping[str, Any]], *, bar_index: int, bar_iso: str
    ) -> None:
        for life in lives:
            if life.get("entry_created"):
                continue
            tb = life.get("terminal_bar")
            if tb is None or int(tb) != bar_index:
                continue
            reason = str(life.get("terminal_reason") or life.get("terminal_outcome") or "invalidated")
            self._emit(
                "setup_invalidated",
                reason=reason,
                processed_bar_timestamp=bar_iso,
                setup_id=life.get("setup_id"),
                side=life.get("direction"),
            )

    def _refresh_review_status(self) -> None:
        assert self.state is not None
        n = self.state.n_closed_trades
        if n >= 100:
            self.state.review_status = "review_due_100"
        elif n >= 50:
            self.state.review_status = "review_due_50"
        else:
            self.state.review_status = "collecting_forward_data"

    def _maybe_snapshot(self) -> None:
        assert self.state is not None
        n = self.state.n_closed_trades
        if n not in SNAPSHOT_THRESHOLDS:
            return
        if n in self.state.snapshots_written:
            return
        status = self.build_status()
        snap = {
            "threshold": n,
            "created_at": utc_now().isoformat(),
            "review_status": self.state.review_status,
            "metrics": status.get("metrics"),
            "forward_start_utc": self.state.forward_start_utc,
            "immutable": True,
        }
        if not self.dry_run:
            path = self.snapshots_dir / f"forward_snapshot_{n:04d}.json"
            if not path.exists():
                atomic_write_json(path, snap)
            if n in (50, 100):
                md = self.snapshots_dir / f"forward_snapshot_{n:04d}.md"
                if not md.exists():
                    md.write_text(
                        f"# Forward snapshot @ {n} trades\n\n"
                        f"status: {self.state.review_status}\n\n"
                        f"```json\n{json.dumps(json_safe(snap), indent=2)}\n```\n",
                        encoding="utf-8",
                    )
        self.state.snapshots_written = sorted(set(self.state.snapshots_written) | {n})
        self._emit(
            "evaluation_threshold_reached",
            reason=f"n_closed={n}",
            processed_bar_timestamp=self.state.last_processed_bar_timestamp,
            extra={"threshold": n, "review_status": self.state.review_status},
        )


    def read_trades(self) -> pd.DataFrame:
        if not self.trades_path.exists():
            return pd.DataFrame(columns=TRADE_CSV_HEADER)
        return pd.read_csv(self.trades_path)

    def compute_metrics(self, trades: pd.DataFrame | None = None) -> dict[str, Any]:
        df = self.read_trades() if trades is None else trades
        n = len(df)
        empty = {
            "n_closed": 0,
            "winrate_net_0_20": None,
            "sum_gross": 0.0,
            "sum_net_0_20": 0.0,
            "profit_factor_net_0_20": None,
            "best_trade": None,
            "worst_trade": None,
            "without_best": None,
            "without_top2": None,
            "without_top3": None,
            "top1_share": None,
            "top3_share": None,
            "max_dd_net_0_20": 0.0,
            "longest_loss_streak": 0,
            "median_holding_hours": None,
            "positive_months": 0,
            "negative_months": 0,
        }
        if n == 0:
            return empty
        net = df["net_return_0_20_pct"].astype(float)
        gross = df["gross_return_pct"].astype(float)
        ordered = net.sort_values(ascending=False)
        sum_net = float(net.sum())
        best = float(ordered.iloc[0])
        top3 = ordered.iloc[: min(3, n)]
        wins = net[net > 0]
        losses = net[net < 0]
        if len(losses) and float(losses.sum()) != 0:
            pf = float(wins.sum() / abs(losses.sum()))
        else:
            pf = float("inf") if len(wins) else None
        streak = best_loss = 0
        for v in net.tolist():
            if v < 0:
                streak += 1
                best_loss = max(best_loss, streak)
            else:
                streak = 0
        eq = peak = 0.0
        dd = 0.0
        for v in net.tolist():
            eq += float(v)
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        months = df.copy()
        months["entry_month"] = months["entry_month"].astype(str)
        msum = months.groupby("entry_month")["net_return_0_20_pct"].sum()
        return {
            "n_closed": n,
            "n_long": int((df["side"] == "long").sum()),
            "n_short": int((df["side"] == "short").sum()),
            "winrate_net_0_20": float((net > 0).mean()),
            "sum_gross": float(gross.sum()),
            "sum_net_0_10": float(df["net_return_0_10_pct"].sum()),
            "sum_net_0_20": sum_net,
            "sum_net_0_30": float(df["net_return_0_30_pct"].sum()),
            "sum_net_0_40": float(df["net_return_0_40_pct"].sum()),
            "mean_net_0_20": float(net.mean()),
            "median_net_0_20": float(net.median()),
            "profit_factor_net_0_20": pf,
            "avg_win": float(wins.mean()) if len(wins) else None,
            "avg_loss": float(losses.mean()) if len(losses) else None,
            "payoff_ratio": (
                float(wins.mean() / abs(losses.mean()))
                if len(wins) and len(losses) and float(losses.mean()) != 0
                else None
            ),
            "best_trade": best,
            "worst_trade": float(ordered.iloc[-1]),
            "without_best": float(ordered.iloc[1:].sum()) if n > 1 else None,
            "without_top2": float(ordered.iloc[2:].sum()) if n > 2 else None,
            "without_top3": float(ordered.iloc[3:].sum()) if n > 3 else None,
            "top1_share": (best / sum_net) if sum_net != 0 else None,
            "top3_share": (float(top3.sum()) / sum_net) if sum_net != 0 else None,
            "max_dd_net_0_20": float(dd),
            "longest_loss_streak": best_loss,
            "median_holding_hours": float(df["holding_hours"].median()),
            "positive_months": int((msum > 0).sum()),
            "negative_months": int((msum < 0).sum()),
        }

    def build_status(self, *, data_meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        assert self.state is not None
        metrics = self.compute_metrics()
        pos = self.state.active_position
        n = self.state.n_closed_trades
        decision = "observe only"
        if self.state.review_status == "review_due_100":
            decision = "review_due_100 — no live unlock"
        elif self.state.review_status == "review_due_50":
            decision = "review_due_50 — no live unlock"
        return {
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "variant": VARIANT,
            "arming_mode": self.arming_mode,
            "forward_start_utc": self.state.forward_start_utc,
            "last_processed_bar_timestamp": self.state.last_processed_bar_timestamp,
            "data": data_meta or {},
            "active_position": pos,
            "pending_trigger": self.state.pending_trigger,
            "n_closed_trades": n,
            "n_long_trades": self.state.n_long_trades,
            "n_short_trades": self.state.n_short_trades,
            "cum_gross_pct": self.state.cum_gross_pct,
            "cum_net_0_20_pct": self.state.cum_net_0_20_pct,
            "progress_50": f"{n} / 50",
            "progress_100": f"{n} / 100",
            "review_status": self.state.review_status,
            "decision_locked": decision,
            "metrics": metrics,
            "config_hash": self.config_hash,
            "source_hash": self.source_hash,
            "updated_at": utc_now().isoformat(),
        }

    def format_status_text(self, status: Mapping[str, Any]) -> str:
        pos = status.get("active_position")
        lines = [
            f"Forward start: {status.get('forward_start_utc')}",
            f"Last processed: {status.get('last_processed_bar_timestamp')}",
            f"Forward trades: {status.get('progress_50')}",
            f"Extended target: {status.get('progress_100')}",
            f"Review status: {status.get('review_status')}",
        ]
        if pos:
            mtm = pos.get("mtm_pct")
            mtm_s = f"{float(mtm):+.2f} %" if mtm is not None else "n/a"
            lines.append(
                f"Active: {str(pos.get('side')).upper()} since {pos.get('entry_timestamp')} @ {pos.get('entry_price')}"
            )
            lines.append(f"MTM: {mtm_s}")
            lines.append(f"MFE: {float(pos.get('mfe_pct', 0)):+.2f} %")
            lines.append(f"MAE: {float(pos.get('mae_pct', 0)):.2f} %")
            lines.append(f"Held: {pos.get('hours_held')} h ({pos.get('bars_held')} bars)")
        else:
            lines.append("Active: none")
            if status.get("pending_trigger"):
                lines.append(f"Pending trigger: {status['pending_trigger']}")
        m = status.get("metrics") or {}
        lines.append(f"Net 0.20 sum: {m.get('sum_net_0_20')}")
        lines.append(f"Winrate net0.20: {m.get('winrate_net_0_20')}")
        lines.append(f"PF net0.20: {m.get('profit_factor_net_0_20')}")
        lines.append(f"Best / Worst: {m.get('best_trade')} / {m.get('worst_trade')}")
        lines.append(f"Without best: {m.get('without_best')} · Top3 share: {m.get('top3_share')}")
        lines.append(f"Decision locked: {status.get('decision_locked')}")
        return "\n".join(lines)

    def write_report(self, status: Mapping[str, Any]) -> None:
        text = [
            "# C3.5c APT Forward Monitor",
            "",
            "Paper / research only. No live trading unlock.",
            "",
            "```",
            self.format_status_text(status),
            "```",
            "",
            f"config_hash: `{status.get('config_hash')}`",
            f"source_hash: `{status.get('source_hash')}`",
            "",
        ]
        atomic_write_text(self.report_path, "\n".join(text))

    def _write_metadata(self, data_meta: Mapping[str, Any]) -> None:
        assert self.state is not None
        meta = {
            "schema_version": SCHEMA_VERSION,
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "variant": VARIANT,
            "arming_mode": self.arming_mode,
            "forward_start_utc": self.state.forward_start_utc,
            "forward_start_immutable": True,
            "config_hash": self.config_hash,
            "source_hash": self.source_hash,
            "cost_model": {"roundtrip_pct": list(ROUNDTRIP_COSTS), "not_per_side": True},
            "data": data_meta,
            "no_historical_forward_import": True,
            "production_sm_unchanged": True,
            "pine_unchanged": True,
        }
        atomic_write_json(self.meta_path, meta)

    def _write_derived_tables(self) -> None:
        df = self.read_trades()
        if df.empty:
            pd.DataFrame().to_csv(self.output_dir / "monthly_summary.csv", index=False)
            pd.DataFrame().to_csv(self.output_dir / "rolling_30d.csv", index=False)
            pd.DataFrame().to_csv(self.output_dir / "equity_curve.csv", index=False)
            return
        df["entry_timestamp"] = pd.to_datetime(df["entry_timestamp"], utc=True)
        (
            df.groupby("entry_month")
            .agg(
                n_closed=("trade_id", "count"),
                sum_net_0_20=("net_return_0_20_pct", "sum"),
                mean_net_0_20=("net_return_0_20_pct", "mean"),
            )
            .reset_index()
            .to_csv(self.output_dir / "monthly_summary.csv", index=False)
        )
        eq_rows = []
        cum = 0.0
        for _, r in df.sort_values("exit_timestamp").iterrows():
            cum += float(r["net_return_0_20_pct"])
            eq_rows.append(
                {
                    "exit_timestamp": r["exit_timestamp"],
                    "trade_id": r["trade_id"],
                    "net_return_0_20_pct": r["net_return_0_20_pct"],
                    "equity_net_0_20": cum,
                }
            )
        pd.DataFrame(eq_rows).to_csv(self.output_dir / "equity_curve.csv", index=False)
        rows = []
        t0 = df["entry_timestamp"].min().normalize()
        t1 = df["entry_timestamp"].max().normalize()
        cur = t0
        while cur + pd.Timedelta(days=30) <= t1 + pd.Timedelta(days=1):
            end = cur + pd.Timedelta(days=30)
            sub = df[(df["entry_timestamp"] >= cur) & (df["entry_timestamp"] < end)]
            if len(sub):
                rows.append(
                    {
                        "window_start": cur.isoformat(),
                        "window_end": end.isoformat(),
                        "n_closed": len(sub),
                        "sum_net_0_20": float(sub["net_return_0_20_pct"].sum()),
                    }
                )
            cur += pd.Timedelta(days=10)
        pd.DataFrame(rows).to_csv(self.output_dir / "rolling_30d.csv", index=False)

    def run_once(self) -> dict[str, Any]:
        lock = self.acquire_lock()
        try:
            self.ensure_dirs()
            last_open = last_complete_15m_open()
            self.state = self.load_or_init_state(last_complete_open=last_open)
            try:
                frame, meta = self.load_15m_frame()
            except Exception as exc:
                self._emit("monitor_error", reason=str(exc))
                raise
            if meta.get("n_15m_gaps"):
                self._emit(
                    "data_gap_detected",
                    reason=f"n_15m_gaps={meta['n_15m_gaps']}",
                    processed_bar_timestamp=meta.get("last_complete_open"),
                    extra={"samples": meta.get("gap_15m_samples")},
                )
            return self.process_frame(frame, data_meta=meta)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def run_loop(self, *, poll_seconds: int = 60) -> None:
        while True:
            try:
                status = self.run_once()
                print(self.format_status_text(status), flush=True)
            except ConfigMismatchError as exc:
                LOGGER.error("Stopping: %s", exc)
                raise
            except Exception:
                LOGGER.exception("run-once failed")
            time.sleep(max(5, int(poll_seconds)))

    def status_cmd(self) -> str:
        if self.status_path.exists():
            status = json.loads(self.status_path.read_text(encoding="utf-8"))
        elif self.state_path.exists():
            self.state = MonitorState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
            status = self.build_status()
        else:
            return "Monitor not started (no state.json)."
        return self.format_status_text(status)

    def report_cmd(self) -> Path:
        if not self.state_path.exists():
            raise FileNotFoundError("state.json missing — run run-once first")
        self.state = MonitorState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        status = self.build_status()
        atomic_write_json(self.status_path, status)
        self.write_report(status)
        self._write_derived_tables()
        return self.report_path

    def verify_replay(self, frame: pd.DataFrame | None = None) -> dict[str, Any]:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="c35c_replay_") as tmp:
            tmp_path = Path(tmp)
            fwd = None
            if self.state_path.exists():
                st = json.loads(self.state_path.read_text(encoding="utf-8"))
                fwd = st.get("forward_start_utc")
            m1 = ForwardMonitor(tmp_path / "a", forward_start_utc=fwd, dry_run=False)
            m2 = ForwardMonitor(tmp_path / "b", forward_start_utc=fwd, dry_run=False)
            if frame is None:
                s1 = m1.run_once()
                s2 = m2.run_once()
            else:
                last_open = pd.Timestamp(frame["timestamp"].iloc[-1])
                m1.state = m1.load_or_init_state(last_complete_open=parse_utc(fwd) or last_open)
                m2.state = m2.load_or_init_state(last_complete_open=parse_utc(fwd) or last_open)
                if fwd:
                    m1.state.forward_start_utc = parse_utc(fwd).isoformat()
                    m2.state.forward_start_utc = parse_utc(fwd).isoformat()
                s1 = m1.process_frame(frame)
                s2 = m2.process_frame(frame)
            t1 = m1.read_trades()
            t2 = m2.read_trades()
            # compare without trade_id randomness
            cols = [c for c in TRADE_CSV_HEADER if c != "trade_id"]
            same = (
                t1.reindex(columns=cols).fillna("").astype(str).reset_index(drop=True)
                .equals(t2.reindex(columns=cols).fillna("").astype(str).reset_index(drop=True))
            )
            return {
                "trades_identical": same,
                "n_trades_a": len(t1),
                "n_trades_b": len(t2),
                "status_n_a": s1.get("n_closed_trades"),
                "status_n_b": s2.get("n_closed_trades"),
            }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C3.5c APT paper-forward monitor")
    parser.add_argument(
        "command",
        choices=["run-once", "run", "status", "report", "verify-replay"],
    )
    parser.add_argument("--forward-start-utc", default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--data-source", default="feather")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    mon = ForwardMonitor(
        args.output_dir,
        forward_start_utc=args.forward_start_utc,
        dry_run=args.dry_run,
        data_source=args.data_source,
    )

    if args.command == "run-once":
        status = mon.run_once()
        print(mon.format_status_text(status))
        return 0
    if args.command == "run":
        mon.run_loop(poll_seconds=args.poll_seconds)
        return 0
    if args.command == "status":
        print(mon.status_cmd())
        return 0
    if args.command == "report":
        path = mon.report_cmd()
        print(f"wrote {path}")
        return 0
    if args.command == "verify-replay":
        result = mon.verify_replay()
        print(json.dumps(result, indent=2))
        return 0 if result.get("trades_identical") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
