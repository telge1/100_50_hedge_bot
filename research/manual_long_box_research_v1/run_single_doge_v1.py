#!/usr/bin/env python3
"""Single manual DOGEUSDT long box — recognition + A2 watcher simulation + report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASH = ROOT / "backtester/short/dashboard"
OVERLAY_OUT = DASH / "research_charts/user_data/manual_long_box_v1_doge.json"
REPORT_OUT = Path(__file__).resolve().parent / "reports/MANUAL_LONG_BOX_V1_SINGLE_DOGE_REPORT.md"

WATCH_WINDOW_BARS = 12  # watcher_pool.WatcherPool default — unchanged from V1 research


def _bootstrap():
    if str(DASH) not in sys.path:
        sys.path.insert(0, str(DASH))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _load_candles_5m(symbol: str, start_ts: int, end_ts: int) -> list[dict]:
    from research_charts.service import load_candles

    packed = load_candles(symbol, "5m", start=start_ts, end=end_ts, limit=50000)
    rows = packed.get("candles") or packed.get("bars") or []
    out: list[dict] = []
    for c in rows:
        t = c.get("time")
        if isinstance(t, (int, float)):
            ts = int(t)
        else:
            continue
        out.append(
            {
                "time": ts,
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            }
        )
    out.sort(key=lambda x: x["time"])
    return out


def _write_report(
    *,
    status: str,
    setup_dict: dict | None,
    rows: list[dict],
    metrics: dict,
    tests: str,
    browser: str,
    err: str = "",
) -> None:
    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Manual Long Box V1 — Einzeltest DOGEUSDT",
        "",
        f"**Status:** {status}",
        f"**Tests:** {tests}",
        f"**Browser-Overlay:** {browser}",
        "",
    ]
    if err:
        lines += [f"**Hinweis:** {err}", ""]
    if setup_dict:
        lines += [
            f"- **Drawing-ID:** `{setup_dict['drawing_id']}`",
            f"- **Entry:** {setup_dict['entry_price']}",
            f"- **SL:** {setup_dict['stop_price']}",
            f"- **TP:** {setup_dict['target_price']}",
            f"- **Watcher-Fenster:** {WATCH_WINDOW_BARS} Kerzen (L1-Forschungsdefault)",
            "",
        ]
    if metrics:
        lines += [
            "## Kennzahlen",
            "",
            f"- Tatsächlicher simulierter Entry: {metrics.get('fill_price', '—')}",
            f"- SL-Abstand %: {metrics.get('sl_pct', '—')}",
            f"- TP-Abstand %: {metrics.get('tp_pct', '—')}",
            f"- Trade-Ergebnis %: {metrics.get('trade_pct', '—')}",
            f"- Watcher-Status: {metrics.get('watcher_status', '—')}",
            "",
        ]
    lines += [
        "## Ereignistabelle",
        "",
        "| Ereignis | UTC-Timestamp | Preis | Ergebnis |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['event']} | {r['utc']} | {r['price']} | {r['result']} |")
    lines.append("")
    lines.append("— STOP (Forschung; keine Orders)")
    REPORT_OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    _bootstrap()
    from research.manual_long_box_research_v1.markers_v1 import build_marker_bundle, report_rows
    from research.manual_long_box_research_v1.recognition import recognize_long_boxes
    from research.manual_long_box_research_v1.simulator import replay_long_boxes
    from research.manual_long_box_research_v1.single_box import SingleBoxError, require_single_manual_doge_long
    from research.manual_long_box_research_v1.validation import pct_dist

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--drawings",
        type=Path,
        default=None,
        help="Optional drawings.json (default: backtester/.../user_data/drawings.json)",
    )
    args = ap.parse_args()

    try:
        sel = require_single_manual_doge_long(drawings_path=args.drawings)
    except SingleBoxError as exc:
        _write_report(
            status=f"BLOCKED ({exc.code})",
            setup_dict=None,
            rows=[],
            metrics={},
            tests="FAIL (Voraussetzung)",
            browser="MANUAL_PENDING",
            err=str(exc),
        )
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc), "ids": exc.ids}, indent=2))
        return 2

    setup = sel.setup
    margin = timedelta(days=14)
    t0 = setup.start_ts - int(margin.total_seconds())
    t1 = setup.end_ts + int(margin.total_seconds())
    if t1 <= t0:
        t1 = t0 + 86400

    try:
        candles = _load_candles_5m(setup.symbol, t0, t1)
    except Exception as exc:  # noqa: BLE001 — research runner surfaces CH/import errors
        _write_report(
            status="BLOCKED (CANDLES)",
            setup_dict=setup.__dict__,
            rows=report_rows(setup, type("R", (), {"events": [], "trades": []})()),  # type: ignore
            metrics={},
            tests="FAIL",
            browser="MANUAL_PENDING",
            err=f"5m-Kerzen nicht geladen: {exc}",
        )
        return 3

    if not candles:
        _write_report(
            status="BLOCKED (NO_CANDLES)",
            setup_dict={
                "drawing_id": setup.drawing_id,
                "entry_price": setup.entry_price,
                "stop_price": setup.stop_price,
                "target_price": setup.target_price,
            },
            rows=[],
            metrics={},
            tests="FAIL",
            browser="MANUAL_PENDING",
            err="Keine 5m-Kerzen im Fenster",
        )
        return 3

    payload = json.loads(Path(sel.drawings_path).read_text(encoding="utf-8"))
    # Single-box payload filter — only selected id
    filtered = {
        "version": payload.get("version", 1),
        "drawings": [d for d in payload.get("drawings") or [] if d.get("drawing_id") == setup.drawing_id],
    }
    replay = replay_long_boxes(
        candles,
        filtered,
        symbol="DOGEUSDT",
        manual_only=True,
        confirmation_mode="A2",
        watch_window_bars=WATCH_WINDOW_BARS,
    )

    bundle = build_marker_bundle(setup, replay)
    OVERLAY_OUT.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_OUT.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    rows = report_rows(setup, replay)
    trade = replay.trades[0] if replay.trades else None
    watcher_status = "IDLE"
    for ev in replay.events:
        watcher_status = ev.phase
    if trade and trade.exit_reason:
        watcher_status = f"CLOSED_{trade.exit_reason}"
    elif trade:
        watcher_status = "OPEN"

    metrics = {
        "fill_price": trade.fill_price if trade else None,
        "sl_pct": round(pct_dist(trade.fill_price, trade.stop_price), 4) if trade else None,
        "tp_pct": round(pct_dist(trade.fill_price, trade.target_price), 4) if trade else None,
        "trade_pct": bundle.get("trade_exit_pct"),
        "watcher_status": watcher_status,
    }

    checks = {
        "box_ok": len(replay.boxes) == 1,
        "prices_match": replay.boxes[0].entry_price == setup.entry_price if replay.boxes else False,
        "no_trade_without_confirm": not any(e.phase == "FILLED" for e in replay.events) or any(
            e.phase == "CONFIRMED" for e in replay.events
        ),
        "single_long": len(replay.trades) <= 1,
    }
    tests = "PASS" if all(checks.values()) else f"FAIL {checks}"

    _write_report(
        status="OK",
        setup_dict={
            "drawing_id": setup.drawing_id,
            "entry_price": setup.entry_price,
            "stop_price": setup.stop_price,
            "target_price": setup.target_price,
        },
        rows=rows,
        metrics=metrics,
        tests=tests,
        browser="READY (JSON geschrieben; Dashboard-Neustart für API/Checkbox)",
    )

    print(
        json.dumps(
            {
                "ok": True,
                "drawing_id": setup.drawing_id,
                "overlay": str(OVERLAY_OUT),
                "report": str(REPORT_OUT),
                "checks": checks,
                "tests": tests,
                "watcher_window_bars": WATCH_WINDOW_BARS,
            },
            indent=2,
        )
    )
    return 0 if tests == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
