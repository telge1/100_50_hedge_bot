"""Reference-counted on-demand OB1000 leases (pure logic, no I/O)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_HEARTBEAT_SEC = 15.0
DEFAULT_LEASE_TTL_SEC = 45.0
PILOT_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT", "DOGEUSDT"})
ON_DEMAND_DEPTH = 1000
# Bybit linear USDT perpetual syntax — on-demand Walls may lease any such symbol.
BYBIT_LINEAR_USDT_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")


def resolve_ob1000_keeper_symbols() -> frozenset[str]:
    """Symbols eligible for OB1000 on-demand / keeper leases.

    Precedence matches live archive SoT:
      1) OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS (set by start script after JSON resolve)
      2) config/ob1000_live_symbols.json
      3) legacy BTC/DOGE pilot fallback
    """
    import json
    import os
    from pathlib import Path

    raw = (os.environ.get("OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS") or "").strip()
    if raw:
        out = frozenset(s.strip().upper() for s in raw.split(",") if s.strip())
        if out:
            return out
    env_cfg = (os.environ.get("OB1000_SYMBOLS_FILE") or "").strip()
    repo_root = Path(__file__).resolve().parents[3]
    cfg = Path(env_cfg) if env_cfg else (repo_root / "config" / "ob1000_live_symbols.json")
    if cfg.is_file():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        syms = data.get("symbols") if isinstance(data, dict) else data
        if isinstance(syms, list) and syms:
            return frozenset(str(s).strip().upper() for s in syms if str(s).strip())
    return PILOT_SYMBOLS


def normalize_linear_usdt_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if not BYBIT_LINEAR_USDT_SYMBOL_RE.match(sym):
        raise ValueError(f"invalid_symbol_syntax:{symbol!r}")
    return sym


@dataclass(frozen=True)
class LeaseKey:
    symbol: str
    depth: int = ON_DEMAND_DEPTH


@dataclass
class Lease:
    lease_id: str
    session_id: str
    symbol: str
    depth: int
    created_at: datetime
    last_heartbeat: datetime
    expires_at: datetime


@dataclass
class LeaseManager:
    heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC
    lease_ttl_sec: float = DEFAULT_LEASE_TTL_SEC
    max_active_topics: int = 4
    # Keeper / archive registry (SoT). Not a hard gate when allow_any_linear_usdt.
    pilot_symbols: frozenset[str] = PILOT_SYMBOLS
    # Walls/Levels: lease any valid linear USDT symbol (starts collector WS on demand).
    allow_any_linear_usdt: bool = True
    _leases: dict[str, Lease] = field(default_factory=dict)

    def _now(self, now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _validate_symbol(self, symbol: str) -> str:
        if self.allow_any_linear_usdt:
            return normalize_linear_usdt_symbol(symbol)
        sym = str(symbol or "").strip().upper()
        if sym not in self.pilot_symbols:
            raise ValueError(f"symbol_not_registered_for_ob1000:{sym}")
        return sym

    def acquire(
        self,
        *,
        symbol: str,
        session_id: str,
        lease_id: str | None = None,
        depth: int = ON_DEMAND_DEPTH,
        now: datetime | None = None,
    ) -> tuple[Lease, bool]:
        """Return (lease, subscribe_required). subscribe_required=True iff first active lease for key."""
        sym = self._validate_symbol(symbol)
        if depth != ON_DEMAND_DEPTH:
            raise ValueError("only_depth_1000_supported")
        ts = self._now(now)
        lid = lease_id or str(uuid.uuid4())
        key = LeaseKey(sym, depth)
        existing = self._leases.get(lid)
        if existing is not None:
            if existing.symbol != sym or existing.depth != depth:
                raise ValueError("lease_symbol_mismatch")
            existing.last_heartbeat = ts
            existing.expires_at = ts + timedelta(seconds=self.lease_ttl_sec)
            return existing, False
        had_active = self.active_count(key) > 0
        if not had_active and self.active_topic_count() >= self.max_active_topics:
            raise RuntimeError("capacity_reached")
        lease = Lease(
            lease_id=lid,
            session_id=str(session_id or lid),
            symbol=sym,
            depth=depth,
            created_at=ts,
            last_heartbeat=ts,
            expires_at=ts + timedelta(seconds=self.lease_ttl_sec),
        )
        self._leases[lid] = lease
        return lease, not had_active

    def heartbeat(
        self,
        lease_id: str,
        *,
        symbol: str | None = None,
        depth: int | None = None,
        now: datetime | None = None,
    ) -> Lease:
        ts = self._now(now)
        lease = self._leases.get(lease_id)
        if lease is None:
            raise KeyError(lease_id)
        if symbol is not None:
            sym = self._validate_symbol(symbol)
            if lease.symbol != sym:
                raise ValueError("lease_symbol_mismatch")
        if depth is not None and depth != lease.depth:
            raise ValueError("lease_depth_mismatch")
        lease.last_heartbeat = ts
        lease.expires_at = ts + timedelta(seconds=self.lease_ttl_sec)
        return lease

    def release(self, lease_id: str) -> tuple[LeaseKey | None, bool]:
        """Return (key, unsubscribe_required)."""
        lease = self._leases.pop(lease_id, None)
        if lease is None:
            return None, False
        key = LeaseKey(lease.symbol, lease.depth)
        still_active = self.active_count(key) > 0
        return key, not still_active

    def expire_due(self, *, now: datetime | None = None) -> list[tuple[LeaseKey, bool]]:
        ts = self._now(now)
        expired_ids = [lid for lid, lease in self._leases.items() if lease.expires_at <= ts]
        out: list[tuple[LeaseKey, bool]] = []
        for lid in expired_ids:
            key, unsub = self.release(lid)
            if key is not None:
                out.append((key, unsub))
        return out

    def active_count(self, key: LeaseKey) -> int:
        return sum(
            1
            for lease in self._leases.values()
            if lease.symbol == key.symbol and lease.depth == key.depth
        )

    def active_topic_count(self) -> int:
        keys = {(lease.symbol, lease.depth) for lease in self._leases.values()}
        return len(keys)

    def active_keys(self) -> set[LeaseKey]:
        return {LeaseKey(lease.symbol, lease.depth) for lease in self._leases.values()}

    def lease_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "lease_id": lease.lease_id,
                "session_id": lease.session_id,
                "symbol": lease.symbol,
                "depth": lease.depth,
                "last_heartbeat": lease.last_heartbeat.isoformat(),
                "expires_at": lease.expires_at.isoformat(),
            }
            for lease in self._leases.values()
        ]
