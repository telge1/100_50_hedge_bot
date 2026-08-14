"""Deterministic (symbol, entry_time) dedupe. Independent of timeframe and direction."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from .candles import ensure_utc
from .schema import REASON_DUP, STATUS_IGNORED


def _sort_key(row: dict[str, Any]) -> tuple:
    def ts(name: str) -> str:
        raw = row.get(name)
        if raw is None or raw == "":
            return "9999-12-31T00:00:00Z"
        return ensure_utc(raw).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    return (ts("available_at"), ts("created_at"), str(row.get("signal_id") or ""))


def dedupe_key(symbol: str, entry_time: datetime | str) -> tuple[str, str]:
    et = ensure_utc(entry_time)
    return (str(symbol or "").strip().upper(), et.strftime("%Y-%m-%dT%H:%M:%SZ"))


def dedupe_signals(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    invalid: list[dict[str, Any]] = []
    for row in rows:
        try:
            key = dedupe_key(str(row.get("symbol") or ""), row["entry_time"])
        except Exception:
            invalid.append(dict(row))
            continue
        groups.setdefault(key, []).append(dict(row))

    winners: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for key, members in groups.items():
        ordered = sorted(members, key=_sort_key)
        winner = dict(ordered[0])
        winner["dedupe_key"] = f"{key[0]}|{key[1]}"
        winners.append(winner)
        for extra in ordered[1:]:
            item = dict(extra)
            item["plan_status"] = STATUS_IGNORED
            item["no_plan_reason"] = REASON_DUP
            item["winner_signal_id"] = winner.get("signal_id")
            item["dedupe_key"] = f"{key[0]}|{key[1]}"
            ignored.append(item)
    winners.sort(key=lambda r: (str(r.get("symbol")), str(r.get("entry_time")), str(r.get("signal_id"))))
    return {"winners": winners, "ignored": ignored, "invalid": invalid}
