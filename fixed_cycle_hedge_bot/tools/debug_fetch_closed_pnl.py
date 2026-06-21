#!/usr/bin/env python3
"""
Diagnose-Script für Bybit /v5/position/closed-pnl über den bestehenden
BybitOrderManager.fetch_closed_pnl().

Ziele:
- Keine Änderung am Bot-State.
- Keine Orders, kein Cancel, keine Positionsänderung.
- Nur readonly-Abfrage von Closed-PnL-Historie.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv
from fixed_cycle_hedge_bot.order_manager import BybitOrderManager


@dataclass
class MatchSummary:
    rows_count: int
    exact_order_id_match: bool
    exact_order_link_id_match: bool
    fallback_candidates_count: int
    best_candidate: Mapping[str, Any] | None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose-Tool für Bybit /v5/position/closed-pnl über BybitOrderManager.fetch_closed_pnl()."
    )
    parser.add_argument("--symbol", required=True, help="Symbol, z.B. BTCUSDT")
    parser.add_argument(
        "--start-ms",
        type=int,
        dest="start_ms",
        help="Startzeit in Millisekunden seit Epoch (UTC).",
    )
    parser.add_argument(
        "--end-ms",
        type=int,
        dest="end_ms",
        help="Endzeit in Millisekunden seit Epoch (UTC).",
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=24,
        help="Zeitraum in Stunden rückwärts, falls start-ms/end-ms fehlen (Default: 24).",
    )
    parser.add_argument(
        "--side",
        choices=["Buy", "Sell", "long", "short", "LONG", "SHORT"],
        help="Optionale Filter-Seite (Bybit side-Feld, z.B. Buy/Sell).",
    )
    parser.add_argument(
        "--order-id",
        dest="order_id",
        help="Optional: erwartete orderId für exact match.",
    )
    parser.add_argument(
        "--order-link-id",
        dest="order_link_id",
        help="Optional: erwartete orderLinkId für exact match.",
    )
    parser.add_argument(
        "--min-closed-size",
        type=float,
        default=None,
        help="Optionaler Mindestwert für closedSize/qty bei Fallback-Kandidaten.",
    )
    parser.add_argument(
        "--write-test-history",
        action="store_true",
        help=(
            "Wenn gesetzt, schreibe Diagnose-Eintrag nach "
            "logs/confirmed_order_pnl_history.debug.jsonl (nicht produktiv)."
        ),
    )
    parser.add_argument(
        "--category",
        default="linear",
        help="Bybit category (Default: linear).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximale Anzahl Rows (Default: 100).",
    )
    return parser.parse_args()


def _resolve_time_window(args: argparse.Namespace) -> tuple[int | None, int | None]:
    if args.start_ms is not None or args.end_ms is not None:
        return args.start_ms, args.end_ms
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(hours=args.hours_back)).timestamp() * 1000)
    return start_ms, end_ms


def _load_api_keys() -> tuple[str, str]:
    """
    Lädt API-Keys analog zum Fixed-Cycle-Runner:
    - zuerst env/.env.local (falls vorhanden),
    - dann ENV:
      - BYBIT_API_KEY / BYBIT_API_SECRET
      - Fallback: API_KEY / SECRET_KEY
    """
    # 1) env/.env.local wie im Fixed-Cycle-Runner laden
    project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / "env" / ".env.local"
    if env_path.exists():
        load_dotenv(env_path)

    # 2) ENV-Variablen lesen
    api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY") or ""
    secret_key = os.getenv("BYBIT_API_SECRET") or os.getenv("SECRET_KEY") or ""
    if not api_key or not secret_key:
        raise SystemExit(
            "API-Keys fehlen. Bitte entweder env/.env.local mit BYBIT_API_KEY/BYBIT_API_SECRET "
            "befüllen oder BYBIT_API_KEY/BYBIT_API_SECRET (bzw. API_KEY/SECRET_KEY) im Environment setzen."
        )
    return api_key, secret_key


def _normalize_side_filter(raw: str | None) -> str | None:
    if not raw:
        return None
    side = raw.strip().lower()
    if side in {"buy", "long"}:
        return "Buy"
    if side in {"sell", "short"}:
        return "Sell"
    return raw


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_size(row: Mapping[str, Any]) -> float:
    """
    Liefert closedSize oder qty als float (0.0 bei Fehlern).
    """
    for key in ("closedSize", "qty", "size", "closedQty"):
        value = row.get(key)
        size = _safe_float(value)
        if size is not None:
            return size
    return 0.0


def _sort_key(row: Mapping[str, Any]) -> tuple[int, float]:
    """
    Sortierhilfe: zuerst nach updatedTime/createdTime, dann nach |closedPnl|.
    """
    updated_raw = row.get("updatedTime") or row.get("createdTime")
    try:
        updated_int = int(updated_raw)
    except (TypeError, ValueError):
        updated_int = 0
    pnl = _safe_float(row.get("closedPnl")) or 0.0
    return updated_int, abs(pnl)


def _format_time_ms(raw: Any) -> str | None:
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _summarize_matches(
    rows: list[Mapping[str, Any]],
    *,
    expected_order_id: str | None,
    expected_order_link_id: str | None,
    expected_side: str | None,
    min_closed_size: float | None,
    symbol: str,
) -> MatchSummary:
    rows_count = len(rows)
    exact_order_id_match = False
    exact_order_link_id_match = False
    fallback_candidates: list[Mapping[str, Any]] = []

    side_filter = _normalize_side_filter(expected_side)
    min_size = float(min_closed_size or 0.0)

    for row in rows:
        row_order_id = str(row.get("orderId") or "").strip()
        row_link_id = str(row.get("orderLinkId") or "").strip()
        if expected_order_id and row_order_id == expected_order_id:
            exact_order_id_match = True
        if expected_order_link_id and row_link_id == expected_order_link_id:
            exact_order_link_id_match = True

        # Fallback-Kandidaten nach Symbol + optional Side + min_closed_size
        row_symbol = str(row.get("symbol") or "").upper()
        if row_symbol != symbol.upper():
            continue
        if side_filter:
            row_side = str(row.get("side") or "").strip()
            # Bybit liefert i.d.R. "Buy"/"Sell" für side
            if row_side and row_side != side_filter:
                continue
        size = _row_size(row)
        if size < min_size:
            continue
        fallback_candidates.append(row)

    fallback_candidates.sort(key=_sort_key, reverse=True)
    best_candidate = fallback_candidates[0] if fallback_candidates else None

    return MatchSummary(
        rows_count=rows_count,
        exact_order_id_match=exact_order_id_match,
        exact_order_link_id_match=exact_order_link_id_match,
        fallback_candidates_count=len(fallback_candidates),
        best_candidate=best_candidate,
    )


def _print_row(row: Mapping[str, Any]) -> None:
    def f(key: str, default: Any = None) -> Any:
        return row.get(key, default)

    created_iso = _format_time_ms(row.get("createdTime"))
    updated_iso = _format_time_ms(row.get("updatedTime"))

    payload = {
        "orderId": f("orderId"),
        "orderLinkId": f("orderLinkId"),
        "symbol": f("symbol"),
        "side": f("side"),
        "qty": f("qty"),
        "closedSize": f("closedSize"),
        "avgEntryPrice": f("avgEntryPrice"),
        "avgExitPrice": f("avgExitPrice"),
        "closedPnl": f("closedPnl"),
        "openFee": f("openFee"),
        "closeFee": f("closeFee"),
        "createdTime": f("createdTime"),
        "createdTime_iso_utc": created_iso,
        "updatedTime": f("updatedTime"),
        "updatedTime_iso_utc": updated_iso,
    }
    print(json.dumps(payload, ensure_ascii=False))


def _write_debug_history_record(
    *,
    symbol: str,
    summary: MatchSummary,
    sample_row: Mapping[str, Any] | None,
    args: argparse.Namespace,
) -> None:
    """
    Optionale, rein diagnostische History-Datei:
    logs/confirmed_order_pnl_history.debug.jsonl
    """
    debug_path = Path("logs") / "confirmed_order_pnl_history.debug.jsonl"
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "category": args.category,
        "source": "debug_fetch_closed_pnl",
        "rows_count": summary.rows_count,
        "exact_order_id_match": summary.exact_order_id_match,
        "exact_order_link_id_match": summary.exact_order_link_id_match,
        "fallback_candidates_count": summary.fallback_candidates_count,
        "expected_order_id": args.order_id,
        "expected_order_link_id": args.order_link_id,
        "side_filter": _normalize_side_filter(args.side),
        "min_closed_size": args.min_closed_size,
    }
    if sample_row is not None:
        record["sample_row"] = {
            "orderId": sample_row.get("orderId"),
            "orderLinkId": sample_row.get("orderLinkId"),
            "symbol": sample_row.get("symbol"),
            "side": sample_row.get("side"),
            "closedPnl": sample_row.get("closedPnl"),
            "closedSize": sample_row.get("closedSize") or sample_row.get("qty"),
            "avgEntryPrice": sample_row.get("avgEntryPrice"),
            "avgExitPrice": sample_row.get("avgExitPrice"),
            "createdTime": sample_row.get("createdTime"),
            "updatedTime": sample_row.get("updatedTime"),
        }
    with debug_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    args = _parse_args()
    start_ms, end_ms = _resolve_time_window(args)
    api_key, secret_key = _load_api_keys()

    manager = BybitOrderManager(api_key, secret_key)
    rows = manager.fetch_closed_pnl(
        symbol=args.symbol,
        category=args.category,
        limit=args.limit,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
    ) or []

    print(f"# closed_pnl rows fetched: {len(rows)}")
    for row in rows:
        _print_row(row)

    summary = _summarize_matches(
        rows,
        expected_order_id=(args.order_id or "").strip() or None,
        expected_order_link_id=(args.order_link_id or "").strip() or None,
        expected_side=args.side,
        min_closed_size=args.min_closed_size,
        symbol=args.symbol,
    )

    print("\n# Summary")
    print(json.dumps(
        {
            "rows_count": summary.rows_count,
            "exact_order_id_match": summary.exact_order_id_match,
            "order_link_id_match": summary.exact_order_link_id_match,
            "fallback_candidates_count": summary.fallback_candidates_count,
        },
        ensure_ascii=False,
    ))

    if summary.best_candidate is not None:
        print("\n# Best fallback candidate")
        _print_row(summary.best_candidate)

    if args.write_test_history:
        _write_debug_history_record(
            symbol=args.symbol,
            summary=summary,
            sample_row=summary.best_candidate or (rows[0] if rows else None),
            args=args,
        )


if __name__ == "__main__":
    main()

