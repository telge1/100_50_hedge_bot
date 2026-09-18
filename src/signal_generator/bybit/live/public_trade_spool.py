"""Durable overflow spool for live public trades (batch WAL).

Hot path stays in the in-memory asyncio queue. Only overflow bursts are
spilled here. Writes are batched (one JSON array line per batch) so we never
JSON-encode or fsync per individual trade under normal load.

Unacked batches are never deleted. Corrupt trailing lines fail closed.
Ack cursor is atomic via meta.json rename.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from signal_generator.bybit.live.ws_public_trade import WsPublicTrade


class PublicTradeSpoolError(RuntimeError):
    pass


class PublicTradeSpoolFullError(PublicTradeSpoolError):
    pass


class PublicTradeSpoolCorruptError(PublicTradeSpoolError):
    pass


def _canonical_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


def _trade_to_compact(trade: WsPublicTrade) -> dict[str, Any]:
    return {
        "s": trade.symbol,
        "i": trade.trade_id,
        "T": int(trade.trade_ts.timestamp() * 1000),
        "S": trade.side,
        "p": str(trade.price),
        "v": str(trade.size),
        "L": trade.tick_direction,
        "BT": int(trade.is_rpi_trade),
        "sf": trade.source_file,
    }


def _trade_from_compact(row: dict[str, Any]) -> WsPublicTrade:
    ts_ms = int(row["T"])
    price = Decimal(str(row["p"]))
    size = Decimal(str(row["v"]))
    return WsPublicTrade(
        symbol=str(row["s"]).upper(),
        trade_id=str(row["i"]),
        trade_ts=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
        side=str(row["S"]),
        price=price,
        size=size,
        notional=price * size,
        tick_direction=str(row.get("L") or ""),
        is_rpi_trade=int(row.get("BT") or 0),
        source_file=str(row.get("sf") or "bybit_ws"),
    )


def _batch_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_dumps(payload).encode("utf-8")).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class SpoolBatch:
    seq: int
    trades: list[WsPublicTrade]
    enqueued_at_unix: float
    checksum: str


class PublicTradeSpool:
    """Segmented batch WAL for public-trade overflow."""

    def __init__(
        self,
        root: Path,
        *,
        segment_max_bytes: int = 16_000_000,
        max_bytes: int = 1_000_000_000,
        min_free_bytes: int = 2_000_000_000,
    ) -> None:
        self.root = Path(root)
        self.segments_dir = self.root / "segments"
        self.meta_path = self.root / "meta.json"
        self.segment_max_bytes = int(segment_max_bytes)
        self.max_bytes = int(max_bytes)
        self.min_free_bytes = int(min_free_bytes)
        self._lock = threading.RLock()
        self._next_seq = 1
        self._last_acked_seq = 0
        self._active_path: Path | None = None
        self._active_fp = None
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        self._load_meta()
        self._open_active_for_append()

    def _disk_free(self) -> int:
        st = os.statvfs(self.root)
        return int(st.f_bavail * st.f_frsize)

    def _bytes_used(self) -> int:
        total = 0
        for p in self.segments_dir.glob("*.jsonl"):
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return total

    def _load_meta(self) -> None:
        if not self.meta_path.exists():
            self._persist_meta()
            return
        try:
            raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
            self._next_seq = int(raw.get("next_seq") or 1)
            self._last_acked_seq = int(raw.get("last_acked_seq") or 0)
            if self._last_acked_seq < 0 or self._next_seq <= self._last_acked_seq:
                raise PublicTradeSpoolCorruptError("meta_seq_invariant_broken")
        except PublicTradeSpoolCorruptError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublicTradeSpoolCorruptError(f"meta_load_failed:{exc}") from exc

    def _persist_meta(self) -> None:
        payload = {
            "next_seq": self._next_seq,
            "last_acked_seq": self._last_acked_seq,
            "updated_at_unix": time.time(),
        }
        tmp = self.meta_path.with_suffix(f".tmp.{os.getpid()}")
        data = _canonical_dumps(payload) + "\n"
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, self.meta_path)
        _fsync_dir(self.root)

    def _open_active_for_append(self) -> None:
        segments = sorted(self.segments_dir.glob("seg_*.jsonl"))
        if segments and segments[-1].stat().st_size < self.segment_max_bytes:
            self._active_path = segments[-1]
            self._active_fp = open(self._active_path, "a", encoding="utf-8")
            return
        idx = 0
        if segments:
            try:
                idx = int(segments[-1].stem.split("_")[1])
            except Exception:  # noqa: BLE001
                idx = len(segments)
        self._active_path = self.segments_dir / f"seg_{idx + 1:06d}.jsonl"
        self._active_fp = open(self._active_path, "a", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        if self._active_path is None or self._active_fp is None:
            self._open_active_for_append()
            return
        if self._active_path.stat().st_size < self.segment_max_bytes:
            return
        self._active_fp.flush()
        os.fsync(self._active_fp.fileno())
        self._active_fp.close()
        self._active_fp = None
        self._open_active_for_append()

    def append_trades(self, trades: list[WsPublicTrade]) -> int:
        """Append one batch. Returns assigned seq. Raises if disk/spool limits hit."""
        if not trades:
            return self._last_acked_seq
        with self._lock:
            used = self._bytes_used()
            if used >= self.max_bytes:
                raise PublicTradeSpoolFullError(f"spool_max_bytes:{used}>={self.max_bytes}")
            free = self._disk_free()
            if free < self.min_free_bytes:
                raise PublicTradeSpoolFullError(f"disk_min_free:{free}<{self.min_free_bytes}")
            self._rotate_if_needed()
            assert self._active_fp is not None
            seq = self._next_seq
            body = {
                "seq": seq,
                "enqueued_at_unix": time.time(),
                "n": len(trades),
                "trades": [_trade_to_compact(t) for t in trades],
            }
            body["checksum"] = _batch_checksum(
                {k: v for k, v in body.items() if k != "checksum"}
            )
            line = _canonical_dumps(body) + "\n"
            self._active_fp.write(line)
            self._active_fp.flush()
            # fsync once per overflow batch (not per trade)
            os.fsync(self._active_fp.fileno())
            self._next_seq = seq + 1
            self._persist_meta()
            return seq

    def iter_unacked(self) -> Iterator[SpoolBatch]:
        with self._lock:
            paths = sorted(self.segments_dir.glob("seg_*.jsonl"))
        for path in paths:
            with open(path, "r", encoding="utf-8") as fp:
                for line_no, line in enumerate(fp, start=1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                        seq = int(obj["seq"])
                        if seq <= self._last_acked_seq:
                            continue
                        chk = str(obj.get("checksum") or "")
                        body = {k: v for k, v in obj.items() if k != "checksum"}
                        if chk != _batch_checksum(body):
                            raise PublicTradeSpoolCorruptError(
                                f"checksum_mismatch:{path.name}:{line_no}"
                            )
                        trades = [_trade_from_compact(t) for t in obj.get("trades") or []]
                        yield SpoolBatch(
                            seq=seq,
                            trades=trades,
                            enqueued_at_unix=float(obj.get("enqueued_at_unix") or 0.0),
                            checksum=chk,
                        )
                    except PublicTradeSpoolCorruptError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        # Trailing partial line after crash → fail closed.
                        raise PublicTradeSpoolCorruptError(
                            f"corrupt_line:{path.name}:{line_no}:{exc}"
                        ) from exc

    def ack(self, seq: int) -> None:
        with self._lock:
            if seq < self._last_acked_seq:
                return
            self._last_acked_seq = seq
            self._persist_meta()
            self._gc_segments_unlocked()

    def _gc_segments_unlocked(self) -> None:
        """Delete fully-acked prefix segments; never delete active/unacked."""
        paths = sorted(self.segments_dir.glob("seg_*.jsonl"))
        if len(paths) <= 1:
            return
        for path in paths[:-1]:
            max_seq = 0
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    for line in fp:
                        raw = line.strip()
                        if not raw:
                            continue
                        obj = json.loads(raw)
                        max_seq = max(max_seq, int(obj.get("seq") or 0))
            except Exception:  # noqa: BLE001
                continue
            if max_seq > 0 and max_seq <= self._last_acked_seq:
                try:
                    path.unlink()
                except OSError:
                    pass

    def pending_batches(self) -> int:
        return sum(1 for _ in self.iter_unacked())

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "spool_root": str(self.root),
                "next_seq": self._next_seq,
                "last_acked_seq": self._last_acked_seq,
                "bytes_used": self._bytes_used(),
                "disk_free_bytes": self._disk_free(),
            }

    def close(self) -> None:
        with self._lock:
            if self._active_fp is not None:
                try:
                    self._active_fp.flush()
                    os.fsync(self._active_fp.fileno())
                except Exception:  # noqa: BLE001
                    pass
                self._active_fp.close()
                self._active_fp = None
