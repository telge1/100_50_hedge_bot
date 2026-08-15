#!/usr/bin/env python3
"""Smoke validation for historical Bybit OB replay (APTUSDT 2026-01-06 only)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.orderbook.historical_bybit_replay import (  # noqa: E402
    HistoricalBybitReplayer,
    OrderBook,
    SequenceStatus,
    apply_levels_trace,
    day_file_path,
    iter_messages,
    parse_ob_line,
    replay_symbol_day,
)

SYMBOL = "APTUSDT"
DATE = "2026-01-06"

# Chosen from the file itself (coverage guaranteed):
# A) shortly after day-open snapshot (4th message ts)
# B) mid-day ~line 50000
# C) late day ~line 500000
TARGETS = {
    "A_after_snapshot": 1767657602183,
    "B_mid_day": 1767664618983,
    "C_late_day": 1767723622883,
}


def _print_top(result, n: int = 10) -> None:
    print(f"\nTOP {n} BIDS:")
    print("price | qty")
    for p, q in result.bid_levels[:n]:
        print(f"{p} | {q}")
    print(f"\nTOP {n} ASKS:")
    print("price | qty")
    for p, q in result.ask_levels[:n]:
        print(f"{p} | {q}")


def reference_first_10_deltas(path: Path) -> dict:
    """Independent mini-check: snapshot + next 10 deltas with before/delta/after."""
    book = OrderBook()
    traces = []
    msgs = []
    for item in iter_messages(path, expected_symbol=SYMBOL, skip_malformed=False):
        assert not isinstance(item, tuple)
        msgs.append(item)
        if len(msgs) >= 11:  # 1 snap + 10 deltas
            break
    assert msgs[0].message_type == "snapshot"
    book.apply_snapshot(msgs[0])
    for msg in msgs[1:]:
        assert msg.message_type == "delta"
        rows = apply_levels_trace(book, msg)
        traces.append(
            {
                "line": msg.source_line,
                "ts": msg.ts_ms,
                "u": msg.update_id,
                "seq": msg.cross_sequence,
                "touched": rows[:8],  # cap print
            }
        )
    return {
        "snapshot_u": msgs[0].update_id,
        "snapshot_seq": msgs[0].cross_sequence,
        "final_best_bid": format(book.best_bid(), "f") if book.best_bid() else None,
        "final_best_ask": format(book.best_ask(), "f") if book.best_ask() else None,
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
        "delta_traces": traces,
    }


def main() -> int:
    path = day_file_path(SYMBOL, DATE)
    print(f"project_root: {PROJECT_ROOT}")
    print(f"file: {path}")
    print(f"exists: {path.exists()} size_mb={path.stat().st_size / (1024*1024):.1f}")
    print("cutoff_field: ts (exchange/stream ms); cts retained diagnostic-only")
    print(
        "reason: ts is the published message time on the orderbook stream; "
        "cts is matching-engine time usually a few ms earlier."
    )

    reports = []
    all_ok = True
    warnings = []

    for label, target in TARGETS.items():
        print("\n" + "=" * 72)
        print(f"TARGET {label} = {target} ({datetime.fromtimestamp(target/1000, tz=timezone.utc).isoformat()})")
        r1 = replay_symbol_day(SYMBOL, DATE, target)
        r2 = replay_symbol_day(SYMBOL, DATE, target)
        det_ok = r1.fingerprint() == r2.fingerprint()
        if not det_ok:
            all_ok = False
            print("DETERMINISM: FAILED")
        else:
            print("DETERMINISM: OK")

        print(
            f"snapshot_ts={r1.last_snapshot_ts_ms} "
            f"target_ts={r1.target_ts_ms} "
            f"last_applied_ts={r1.last_applied_message_ts_ms}"
        )
        print(
            f"deltas_applied={r1.deltas_applied} messages_applied={r1.messages_applied} "
            f"u={r1.last_update_id} seq={r1.last_seq}"
        )
        print(
            f"bid_levels={r1.bid_level_count} ask_levels={r1.ask_level_count} "
            f"best_bid={r1.best_bid} best_ask={r1.best_ask} spread={r1.spread}"
        )
        print(f"sequence_status={r1.sequence_status.value}")
        print(
            "diag:",
            {
                "snapshots_seen": r1.sequence_diagnostics.snapshots_seen,
                "u_gap_count": r1.sequence_diagnostics.u_gap_count,
                "duplicate_u_count": r1.sequence_diagnostics.duplicate_u_count,
                "midstream_snapshot_resets": r1.sequence_diagnostics.midstream_snapshot_resets,
                "malformed_lines": r1.sequence_diagnostics.malformed_lines,
                "ts_backward_count": r1.sequence_diagnostics.ts_backward_count,
            },
        )
        inv = r1.invariants
        print(
            "invariants:",
            {
                "ok": inv.ok,
                "best_bid_lt_best_ask": inv.best_bid_lt_best_ask,
                "no_nonpositive_qty": inv.no_nonpositive_qty,
                "book_nonempty": inv.book_nonempty,
                "last_applied_le_target": inv.last_applied_le_target,
                "details": inv.details,
            },
        )
        _print_top(r1, 10)

        if not inv.ok or not det_ok or r1.sequence_status == SequenceStatus.INVALID:
            all_ok = False
        if r1.sequence_status in {
            SequenceStatus.POSSIBLE_GAP,
            SequenceStatus.DUPLICATES_SEEN,
            SequenceStatus.RESET_SEEN,
        }:
            warnings.append(f"{label}:{r1.sequence_status.value}")

        reports.append(
            {
                "label": label,
                "target_ts_ms": target,
                "target_iso": datetime.fromtimestamp(target / 1000, tz=timezone.utc).isoformat(),
                "snapshot_ts_ms": r1.last_snapshot_ts_ms,
                "last_applied_ts_ms": r1.last_applied_message_ts_ms,
                "deltas_applied": r1.deltas_applied,
                "u": r1.last_update_id,
                "seq": r1.last_seq,
                "bid_level_count": r1.bid_level_count,
                "ask_level_count": r1.ask_level_count,
                "best_bid": r1.best_bid,
                "best_ask": r1.best_ask,
                "spread": r1.spread,
                "sequence_status": r1.sequence_status.value,
                "invariants_ok": inv.ok,
                "determinism_ok": det_ok,
            }
        )

    print("\n" + "=" * 72)
    print("REFERENCE: snapshot + next 10 deltas")
    ref = reference_first_10_deltas(path)
    print(
        f"after 10 deltas: best_bid={ref['final_best_bid']} best_ask={ref['final_best_ask']} "
        f"levels={ref['bid_levels']}/{ref['ask_levels']}"
    )
    for t in ref["delta_traces"][:3]:
        print(f"\n delta line={t['line']} u={t['u']} ts={t['ts']}")
        for row in t["touched"][:4]:
            print(
                f"  {row['side']} {row['price']}: before={row['before']} "
                f"delta={row['delta_qty']} after={row['after']}"
            )

    out_dir = PROJECT_ROOT / "results" / "historical_ob_replay_smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{SYMBOL}_{DATE}_smoke.json"
    decision = (
        "HISTORICAL_OB_REPLAY_VALID"
        if all_ok and not warnings
        else (
            "HISTORICAL_OB_REPLAY_VALID_WITH_WARNINGS"
            if all_ok
            else "HISTORICAL_OB_REPLAY_INVALID"
        )
    )
    payload = {
        "decision": decision,
        "cutoff_field": "ts",
        "symbol": SYMBOL,
        "date": DATE,
        "targets": reports,
        "warnings": warnings,
        "reference_summary": {
            "snapshot_u": ref["snapshot_u"],
            "final_best_bid": ref["final_best_bid"],
            "final_best_ask": ref["final_best_ask"],
            "bid_levels": ref["bid_levels"],
            "ask_levels": ref["ask_levels"],
        },
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out_path}")
    print(f"DECISION: {decision}")
    return 0 if decision != "HISTORICAL_OB_REPLAY_INVALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
