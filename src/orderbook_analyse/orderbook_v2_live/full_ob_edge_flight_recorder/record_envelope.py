"""One queue/ringbuffer item == one Bybit WebSocket delta packet (compact envelope)."""

from __future__ import annotations

from typing import Any


def _copy_levels(rows: Any) -> list[list[Any]]:
    out: list[list[Any]] = []
    if not rows:
        return out
    for row in rows:
        if isinstance(row, (list, tuple)):
            out.append(list(row))
        else:
            out.append([row])
    return out


def level_update_count(payload: dict[str, Any]) -> int:
    data = payload.get("data") or {}
    if isinstance(data, list):
        n = 0
        for part in data:
            if isinstance(part, dict):
                n += len(part.get("b") or []) + len(part.get("a") or [])
        return n
    return len(data.get("b") or []) + len(data.get("a") or [])


def approx_envelope_bytes(payload: dict[str, Any]) -> int:
    """Cheap size estimate — never run orjson on the hot ingress path."""
    return 160 + 28 * level_update_count(payload)


def build_delta_envelope(
    payload: dict[str, Any],
    *,
    receive_time_ns: int,
    phase: str,
    outcome: str | None = None,
) -> dict[str, Any]:
    """
    Compact immutable-ish envelope for exactly one Bybit WS delta message.

    - Does not copy full book state
    - Copies only delta bid/ask rows + continuity metadata
    - Keeps u/seq/ts/cts/receive_time_ns in the same item
    """
    raw_data = payload.get("data") or {}
    # Bybit normally sends a dict; tolerate a single-element list.
    if isinstance(raw_data, list):
        data = raw_data[0] if raw_data and isinstance(raw_data[0], dict) else {}
    else:
        data = raw_data if isinstance(raw_data, dict) else {}

    bids = _copy_levels(data.get("b") or [])
    asks = _copy_levels(data.get("a") or [])
    u = data.get("u")
    seq = data.get("seq")
    ts = payload.get("ts") if payload.get("ts") is not None else data.get("ts")
    cts = payload.get("cts") if payload.get("cts") is not None else data.get("cts")

    env: dict[str, Any] = {
        "topic": payload.get("topic"),
        "type": str(payload.get("type") or data.get("type") or "delta").lower(),
        "ts": ts,
        "cts": cts,
        "data": {
            "s": data.get("s"),
            "b": bids,
            "a": asks,
            "u": u,
            "seq": seq,
        },
        "local_receive_time_ns": int(receive_time_ns),
        "flight_phase": phase,
        "level_update_count": len(bids) + len(asks),
    }
    if outcome:
        env["apply_outcome"] = outcome
    return env
