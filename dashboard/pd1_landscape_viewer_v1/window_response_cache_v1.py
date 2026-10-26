"""BT-D34 — versioned in-process cache for PD1 landscape window API responses."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

WINDOW_RESPONSE_CACHE_CONTRACT_V1 = "bt_d34_pd1_window_response_cache_v1"
WINDOW_BUILDER_VERSION_V1 = "pd1_landscape_historical_window_v1"

COMPUTE_MODE_FULL = "full"
COMPUTE_MODE_EXACT_ORIGIN_V1 = "exact_origin_v1"
ALLOWED_COMPUTE_MODES = frozenset({COMPUTE_MODE_FULL, COMPUTE_MODE_EXACT_ORIGIN_V1})

DEFAULT_MAX_ENTRIES = 16


def _max_entries() -> int:
    raw = os.environ.get("PD1_WINDOW_RESPONSE_CACHE_MAX", "").strip()
    if not raw:
        return DEFAULT_MAX_ENTRIES
    try:
        return max(1, min(64, int(raw)))
    except ValueError:
        return DEFAULT_MAX_ENTRIES


def cache_enabled() -> bool:
    return os.environ.get("PD1_WINDOW_RESPONSE_CACHE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


@dataclass(frozen=True)
class WindowCacheKeyParts:
    symbol: str
    from_utc: str
    to_utc: str
    timeframe: str
    pool_detail: int
    pipeline_line_id: str
    config_hash: str
    compute_mode: str
    builder_version: str = WINDOW_BUILDER_VERSION_V1
    cache_contract: str = WINDOW_RESPONSE_CACHE_CONTRACT_V1

    def digest(self) -> str:
        payload = {
            "symbol": self.symbol.upper(),
            "from_utc": self.from_utc,
            "to_utc": self.to_utc,
            "timeframe": self.timeframe,
            "pool_detail": int(self.pool_detail),
            "pipeline_line_id": self.pipeline_line_id,
            "config_hash": self.config_hash,
            "compute_mode": self.compute_mode,
            "builder_version": self.builder_version,
            "cache_contract": self.cache_contract,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_compute_mode(raw: str | None) -> str:
    mode = str(raw or COMPUTE_MODE_FULL).strip().lower()
    if mode in ("exact_origin", "exact_origin_v1", "b_layer", "b-layer"):
        return COMPUTE_MODE_EXACT_ORIGIN_V1
    if mode == COMPUTE_MODE_EXACT_ORIGIN_V1:
        return COMPUTE_MODE_EXACT_ORIGIN_V1
    return COMPUTE_MODE_FULL


def payload_cache_eligible(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("code"):
        return False
    pools = payload.get("pools")
    if not isinstance(pools, list):
        return False
    prov = payload.get("provenance") or {}
    if prov.get("remaining"):
        return False
    if not prov.get("pipeline_line_id") or not prov.get("config_hash"):
        return False
    if not prov.get("timeframe") or prov.get("pool_detail") is None:
        return False
    win = payload.get("window") or {}
    if not win.get("from_utc") or not win.get("to_utc"):
        return False
    return True


def attach_response_cache_meta(
    payload: dict[str, Any],
    *,
    hit: bool,
    key_digest: str,
    compute_mode: str,
    stored_at_unix: float | None = None,
) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out["response_cache"] = {
        "contract": WINDOW_RESPONSE_CACHE_CONTRACT_V1,
        "hit": bool(hit),
        "key_digest": key_digest[:16],
        "compute_mode": compute_mode,
        "stored_at_unix": stored_at_unix,
    }
    return out


class _WindowResponseCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._max = _max_entries()

    def get(self, digest: str) -> dict[str, Any] | None:
        with self._lock:
            ent = self._entries.get(digest)
            if not ent:
                return None
            self._entries.move_to_end(digest)
            return copy.deepcopy(ent[1])

    def put(self, digest: str, payload: dict[str, Any]) -> None:
        if not payload_cache_eligible(payload):
            return
        stored = copy.deepcopy(payload)
        stored.pop("response_cache", None)
        with self._lock:
            self._entries[digest] = (time.time(), stored)
            self._entries.move_to_end(digest)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "contract": WINDOW_RESPONSE_CACHE_CONTRACT_V1,
                "max_entries": self._max,
                "size": len(self._entries),
                "enabled": cache_enabled(),
            }


_CACHE = _WindowResponseCache()
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_WAITERS: dict[str, threading.Event] = {}
_INFLIGHT_RESULT: dict[str, dict[str, Any] | Exception] = {}


def get_cached_window(digest: str) -> dict[str, Any] | None:
    if not cache_enabled():
        return None
    return _CACHE.get(digest)


def store_cached_window(digest: str, payload: dict[str, Any]) -> None:
    if not cache_enabled():
        return
    _CACHE.put(digest, payload)


def cache_stats() -> dict[str, Any]:
    return _CACHE.stats()


def clear_cache_for_tests() -> None:
    with _CACHE._lock:
        _CACHE._entries.clear()


def begin_inflight(digest: str) -> tuple[bool, threading.Event | None]:
    """Returns (is_leader, event_to_wait_on)."""
    with _INFLIGHT_LOCK:
        if digest in _INFLIGHT_WAITERS:
            ev = _INFLIGHT_WAITERS[digest]
            return False, ev
        ev = threading.Event()
        _INFLIGHT_WAITERS[digest] = ev
        return True, None


def finish_inflight(digest: str, payload: dict[str, Any] | None, err: BaseException | None) -> None:
    with _INFLIGHT_LOCK:
        ev = _INFLIGHT_WAITERS.pop(digest, None)
        if err is not None:
            _INFLIGHT_RESULT[digest] = err
        elif payload is not None:
            _INFLIGHT_RESULT[digest] = copy.deepcopy(payload)
    if ev is not None:
        ev.set()


def wait_inflight(digest: str, event: threading.Event) -> dict[str, Any]:
    event.wait()
    # Leader stores payload in LRU before finish_inflight; multiple followers may wake together.
    cached = get_cached_window(digest)
    if cached is not None:
        return cached
    with _INFLIGHT_LOCK:
        result = _INFLIGHT_RESULT.pop(digest, None)
    if isinstance(result, BaseException):
        raise result
    if result is None:
        raise RuntimeError("inflight_wait_missing_result")
    return copy.deepcopy(result)
