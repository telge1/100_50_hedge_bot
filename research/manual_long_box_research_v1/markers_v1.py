"""Research-chart marker bundle (read-only display; does not edit drawings.json)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import LongBoxSetup, ReplayResult, SimEvent, SimTrade


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_marker_bundle(
    setup: LongBoxSetup,
    replay: ReplayResult,
    *,
    symbol: str = "DOGEUSDT",
) -> dict[str, Any]:
    markers: list[dict[str, Any]] = []
    for ev in replay.events:
        if ev.phase not in (
            "TOUCH",
            "WATCH",
            "CONFIRMED",
            "FILL_CANDIDATE",
            "BLOCKED_TP_DISTANCE",
            "FILLED",
            "CLOSED_TP",
            "CLOSED_SL",
        ):
            continue
        label = {
            "TOUCH": "TOUCH",
            "WATCH": "WATCH",
            "CONFIRMED": "CONFIRM",
            "FILL_CANDIDATE": "FILL candidate",
            "BLOCKED_TP_DISTANCE": "BLOCKED TP dist",
            "FILLED": "LONG",
            "CLOSED_TP": "EXIT TP",
            "CLOSED_SL": "EXIT SL",
        }[ev.phase]
        markers.append(
            {
                "id": f"mlb1-v1-{ev.phase}-{ev.utc}",
                "kind": "vline",
                "time": ev.utc,
                "time_utc": _iso(ev.utc),
                "price": ev.price,
                "label": label,
                "text": f"{label} @ {_iso(ev.utc)} price={ev.price}",
                "drawing_id": setup.drawing_id,
            }
        )
    trade = replay.trades[0] if replay.trades else None
    blocked = next((e for e in replay.events if e.phase == "BLOCKED_TP_DISTANCE"), None)
    status = "OPEN"
    exit_pct = None
    if blocked:
        status = "BLOCKED_TP_DISTANCE"
    elif not any(e.phase == "TOUCH" for e in replay.events):
        status = "NO_TOUCH"
    elif not any(e.phase == "CONFIRMED" for e in replay.events):
        status = "NO_CONFIRM"
    elif trade:
        if trade.exit_reason == "TP":
            status = "CLOSED_TP"
            exit_pct = (trade.target_price - trade.fill_price) / trade.fill_price * 100.0
        elif trade.exit_reason == "SL":
            status = "CLOSED_SL"
            exit_pct = (trade.stop_price - trade.fill_price) / trade.fill_price * 100.0
        elif trade.exit_reason:
            status = trade.exit_reason
        else:
            status = "OPEN"
    return {
        "schema": "manual_long_box_v1/overlay",
        "symbol": symbol,
        "drawing_id": setup.drawing_id,
        "entry_price": setup.entry_price,
        "stop_price": setup.stop_price,
        "target_price": setup.target_price,
        "markers": markers,
        "trade_status": status,
        "trade_exit_pct": exit_pct,
        "open_decisions": replay.open_decisions,
    }


def report_rows(setup: LongBoxSetup, replay: ReplayResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "event": "Box erkannt",
            "utc": "",
            "price": f"E={setup.entry_price} SL={setup.stop_price} TP={setup.target_price}",
            "result": "OK",
        }
    ]
    for phase, label in (
        ("TOUCH", "TOUCH"),
        ("WATCH", "WATCH aktiviert"),
        ("CONFIRMED", "A2 CONFIRM"),
        ("FILL_CANDIDATE", "FILL-Kandidat (nächster 5m-Open)"),
        ("TP_DISTANCE_CHECK", "TP-Abstand-Prüfung"),
        ("BLOCKED_TP_DISTANCE", "Entry blockiert (TP-Abstand)"),
        ("FILLED", "LONG Fill"),
    ):
        ev = next((e for e in replay.events if e.phase == phase), None)
        result = "—"
        if ev:
            if phase == "TP_DISTANCE_CHECK":
                result = ev.message
            elif phase == "BLOCKED_TP_DISTANCE":
                result = "BLOCKED_TP_DISTANCE"
            else:
                result = "OK"
        rows.append(
            {
                "event": label,
                "utc": _iso(ev.utc) if ev else "—",
                "price": str(ev.price) if ev and ev.price is not None else "—",
                "result": result,
            }
        )
    blocked = next((e for e in replay.events if e.phase == "BLOCKED_TP_DISTANCE"), None)
    trade = replay.trades[0] if replay.trades else None
    if blocked:
        rows.append(
            {
                "event": "LONG erlaubt/blockiert",
                "utc": _iso(blocked.utc),
                "price": str(blocked.price),
                "result": "BLOCKED_TP_DISTANCE",
            }
        )
    elif trade:
        rows.append(
            {
                "event": "LONG erlaubt/blockiert",
                "utc": _iso(trade.fill_utc),
                "price": str(trade.fill_price),
                "result": "ENTRY_FILLED",
            }
        )
    if trade and trade.exit_utc:
        phase = "CLOSED_TP" if trade.exit_reason == "TP" else "CLOSED_SL"
        ev = next((e for e in replay.events if e.phase == phase), None)
        rows.append(
            {
                "event": f"EXIT {trade.exit_reason}",
                "utc": _iso(trade.exit_utc),
                "price": str(ev.price if ev and ev.price else ""),
                "result": trade.exit_reason,
            }
        )
    elif trade:
        rows.append(
            {"event": "EXIT", "utc": "—", "price": "—", "result": "OPEN (kein simuliertes Exit im Fenster)"}
        )
    return rows
