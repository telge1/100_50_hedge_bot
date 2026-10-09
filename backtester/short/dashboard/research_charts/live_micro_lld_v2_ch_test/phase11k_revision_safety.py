"""Phase 11K — isolated candle input revision helpers (research only, no CH writes)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from data.models import Candle


def ohlcv_fingerprint(c: Candle) -> str:
    return "|".join(
        (
            str(int(c.unix_seconds)),
            f"{float(c.open):.8f}",
            f"{float(c.high):.8f}",
            f"{float(c.low):.8f}",
            f"{float(c.close):.8f}",
            f"{float(c.volume):.8f}",
        )
    )


def block_content_hash(fingerprints: Sequence[str]) -> str:
    h = hashlib.sha256()
    for fp in fingerprints:
        h.update(fp.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


@dataclass
class ImmutableInputSnapshot:
    """Frozen OHLCV basis for one backfill run (does not detect post-hoc CH edits alone)."""

    snapshot_id: str
    symbol: str
    fingerprints_by_unix: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_candles(cls, snapshot_id: str, candles: Iterable[Candle]) -> ImmutableInputSnapshot:
        fps: dict[int, str] = {}
        sym = ""
        for c in candles:
            sym = str(c.symbol).upper()
            fps[int(c.unix_seconds)] = ohlcv_fingerprint(c)
        return cls(snapshot_id=snapshot_id, symbol=sym, fingerprints_by_unix=fps)

    def content_hash(self) -> str:
        keys = sorted(self.fingerprints_by_unix)
        return block_content_hash([self.fingerprints_by_unix[k] for k in keys])

    def assert_compatible(self, candles: Sequence[Candle]) -> list[str]:
        errs: list[str] = []
        for c in candles:
            u = int(c.unix_seconds)
            fp = ohlcv_fingerprint(c)
            expected = self.fingerprints_by_unix.get(u)
            if expected is None:
                errs.append(f"missing_unix:{u}")
            elif expected != fp:
                errs.append(f"revision:{u}")
        return errs


def affected_5m_bucket_opens(one_m_unix: int, tf_sec: int = 300) -> set[int]:
    step = 60
    bo = one_m_unix - (one_m_unix % tf_sec)
    return {bo, bo - tf_sec}


def invalidate_buckets(
    materialized: dict[int, object],
    one_m_unix: int,
    *,
    tf_sec: int = 300,
) -> None:
    for bo in affected_5m_bucket_opens(one_m_unix, tf_sec=tf_sec):
        materialized.pop(bo, None)


def detect_ohlcv_change(before: Candle, after: Candle) -> bool:
    return ohlcv_fingerprint(before) != ohlcv_fingerprint(after)
