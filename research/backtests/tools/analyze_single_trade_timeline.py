#!/usr/bin/env python3
import argparse
import csv
from collections import defaultdict
from pathlib import Path


IMPORTANT_PURPOSES = {
    "INITIAL_LONG_ENTRY",
    "INITIAL_SHORT_ENTRY",
    "LONG_TP_EXIT",
    "SHORT_SL_EXIT",
    "REFILL_LONG",
    "REFILL_SHORT",
}


def safe_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def first_non_empty(row, keys):
    for k in keys:
        v = row.get(k)
        if v not in ("", None):
            return v
    return ""


def is_relevant_purpose(purpose):
    if not purpose:
        return False
    if purpose in IMPORTANT_PURPOSES:
        return True
    if purpose.startswith("CYCLE_") and (
        purpose.endswith("_LONG_ADD") or purpose.endswith("_SHORT_REDUCE")
    ):
        return True
    return False


def find_summary_file(outdir, symbol):
    matches = list(outdir.glob(f"{symbol}_original_hedge_*_multi_start_summary.csv"))
    if not matches:
        raise FileNotFoundError(f"No summary CSV found for symbol={symbol} in {outdir}")
    return matches[0]


def find_trade_block_file(outdir, symbol, direction, start_index):
    p = outdir / f"{symbol}_{direction}_start{start_index}_conservative_live_trade_blocks.csv"
    if p.exists():
        return p

    matches = list(outdir.glob(f"{symbol}_{direction}_start{start_index}_*_trade_blocks.csv"))
    if matches:
        return matches[0]

    raise FileNotFoundError(
        f"No trade block CSV found for symbol={symbol}, direction={direction}, start_index={start_index}"
    )


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def print_trade_header(summary_row):
    print("=" * 100)
    print("SINGLE TRADE TIMELINE ANALYSIS")
    print("=" * 100)
    for k in [
        "symbol",
        "direction",
        "start_index",
        "start_time",
        "end_time",
        "final_status",
        "exit_reason",
        "realized_pnl",
        "unrealized_pnl",
        "overall_pnl",
        "final_price",
        "final_long_qty",
        "final_long_avg_price",
        "final_short_qty",
        "final_short_avg_price",
        "end_last_price",
        "end_long_qty",
        "end_long_avg_price",
        "end_short_qty",
        "end_short_avg_price",
        "end_unrealized_pnl",
        "end_overall_pnl",
    ]:
        if k in summary_row:
            print(f"{k:28} = {summary_row.get(k)}")
    print()


def row_price(row):
    return first_non_empty(
        row,
        [
            "fill_price",
            "price",
            "trigger_price",
            "order_check_price",
        ],
    )


def row_note(row):
    purpose = row.get("purpose") or row.get("purpose_original") or ""
    notes = []

    if purpose in {"REFILL_LONG", "REFILL_SHORT"}:
        event_type = row.get("event_type") or ""
        status = row.get("status") or ""
        # Nur echte Fills (order filled) auf Vollständigkeit prüfen. Für
        # intent/submitted-Zeilen sind leere Felder erwartbar.
        if event_type == "filled" or status == "FILLED":
            missing = []
            for k in [
                "price",
                "fill_price",
                "long_qty_after",
                "long_avg_after",
                "short_qty_after",
                "short_avg_after",
            ]:
                if row.get(k) in ("", None):
                    missing.append(k)
            if missing:
                notes.append("MISSING_EXPORT_FIELDS:" + ",".join(missing))

    return "|".join(notes)


def print_timeline(rows):
    relevant = []
    for r in rows:
        purpose = r.get("purpose") or r.get("purpose_original") or ""
        if not is_relevant_purpose(purpose):
            continue
        if r.get("row_type") not in {"intent", "order", "fill", "final_active_order"}:
            continue
        relevant.append(r)

    print("=" * 100)
    print("FULL RELEVANT TIMELINE")
    print("=" * 100)
    print(
        f"{'#':>4} {'timestamp':<25} {'idx':>6} {'row':<18} {'event':<14} "
        f"{'purpose':<24} {'cy':>3} {'side':<6} {'qty':>10} {'price':>10} "
        f"{'fill':>10} {'trig':>10} {'cum_pnl':>12} "
        f"{'L_qty':>10} {'L_avg':>10} {'S_qty':>10} {'S_avg':>10} note"
    )
    print("-" * 190)

    for i, r in enumerate(relevant, 1):
        purpose = r.get("purpose") or r.get("purpose_original") or ""
        print(
            f"{i:>4} "
            f"{r.get('timestamp',''):<25} "
            f"{r.get('candle_index',''):>6} "
            f"{r.get('row_type',''):<18} "
            f"{r.get('event_type',''):<14} "
            f"{purpose:<24} "
            f"{r.get('cycle_index',''):>3} "
            f"{r.get('side',''):<6} "
            f"{r.get('qty',''):>10} "
            f"{r.get('price',''):>10} "
            f"{r.get('fill_price',''):>10} "
            f"{r.get('trigger_price',''):>10} "
            f"{r.get('cumulative_pnl',''):>12} "
            f"{r.get('long_qty_after',''):>10} "
            f"{r.get('long_avg_after',''):>10} "
            f"{r.get('short_qty_after',''):>10} "
            f"{r.get('short_avg_after',''):>10} "
            f"{row_note(r)}"
        )
    print()


def print_refill_analysis(rows):
    refill_rows = []
    for r in rows:
        purpose = r.get("purpose") or r.get("purpose_original") or ""
        if purpose in {"REFILL_LONG", "REFILL_SHORT"}:
            refill_rows.append(r)

    groups = defaultdict(list)
    for r in refill_rows:
        key = (r.get("timestamp", ""), r.get("candle_index", ""))
        groups[key].append(r)

    print("=" * 100)
    print("REFILL SPECIAL ANALYSIS")
    print("=" * 100)

    if not groups:
        print("No refill rows found.")
        print()
        return

    for (ts, idx), group in sorted(groups.items()):
        print("-" * 100)
        print(f"timestamp={ts} candle_index={idx}")

        for r in group:
            purpose = r.get("purpose") or r.get("purpose_original") or ""
            print(
                f"{purpose:<12} row_type={r.get('row_type',''):<18} "
                f"event={r.get('event_type',''):<10} status={r.get('status',''):<8} "
                f"side={r.get('side',''):<6} qty={r.get('qty','')} "
                f"price={r.get('price','')} fill_price={r.get('fill_price','')} "
                f"L_qty_after={r.get('long_qty_after','')} L_avg_after={r.get('long_avg_after','')} "
                f"S_qty_after={r.get('short_qty_after','')} S_avg_after={r.get('short_avg_after','')} "
                f"note={row_note(r)}"
            )

        # Nur echte Fills bewerten; intent/submitted-Zeilen dürfen leere Felder haben.
        filled = [r for r in group if r.get("event_type") == "filled" or r.get("status") == "FILLED"]
        missing_any = False
        for r in filled:
            for k in [
                "price",
                "fill_price",
                "long_qty_after",
                "long_avg_after",
                "short_qty_after",
                "short_avg_after",
            ]:
                if r.get(k) in ("", None):
                    missing_any = True

        if missing_any:
            print("REFILL_RESULT: MISSING_EXPORT_FIELDS -> Refill avg/price cannot be reconstructed safely from export.")
        else:
            print("REFILL_RESULT: OK")
    print()


def print_chart_points(rows):
    print("=" * 100)
    print("CHART POINTS")
    print("=" * 100)
    print(
        f"{'timestamp':<25} "
        f"{'label':<28} "
        f"{'price':>10} "
        f"{'order_qty':>12} "
        f"{'long_size_after':>14} "
        f"{'short_size_after':>14} "
        f"{'long_avg_after':>14} "
        f"{'short_avg_after':>14}"
    )
    print("-" * 120)

    def _fmt2(value: object) -> str:
        if value in ("", None):
            return ""
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    # Merke Orders, für die es bereits eine eigene fill-Row gibt, damit wir
    # den Chart-Punkt nicht doppelt (fill + order filled) ausgeben.
    fill_order_ids: set[str] = set()
    for r in rows:
        if r.get("row_type") != "fill":
            continue
        oid = str(r.get("order_id") or "").strip()
        if oid:
            fill_order_ids.add(oid)

    for r in rows:
        purpose = r.get("purpose") or r.get("purpose_original") or ""
        if not is_relevant_purpose(purpose):
            continue

        is_refill = purpose in {"REFILL_LONG", "REFILL_SHORT"}
        row_type = r.get("row_type")
        event_type = r.get("event_type")
        status = r.get("status")

        is_fill_row = row_type == "fill"
        is_filled_order = row_type == "order" and (event_type == "filled" or status == "FILLED")
        is_final_active = row_type == "final_active_order"

        # Für normale Entry/Cycle-Fills bevorzugt nur die fill-Row anzeigen.
        # Order-Event "filled" wird nur genutzt, wenn es keine eigene fill-Row gibt.
        if is_filled_order and not is_refill:
            oid = str(r.get("order_id") or "").strip()
            if oid and oid in fill_order_ids:
                continue

        # Für Chart: normale Fills (fill), REFILL-Fills (order filled) und finale aktive Orders.
        if not (is_fill_row or (is_refill and is_filled_order) or is_final_active):
            continue

        price = row_price(r)
        label = purpose
        if is_final_active:
            label = f"FINAL_ACTIVE_{purpose}"

        price_str = _fmt2(price)
        order_qty_str = _fmt2(r.get("qty"))
        long_size_after_str = _fmt2(r.get("long_qty_after"))
        short_size_after_str = _fmt2(r.get("short_qty_after"))
        long_avg_after_str = _fmt2(r.get("long_avg_after"))
        short_avg_after_str = _fmt2(r.get("short_avg_after"))

        print(
            f"{r.get('timestamp',''):<25} "
            f"{label:<28} "
            f"{price_str:>10} "
            f"{order_qty_str:>12} "
            f"{long_size_after_str:>14} "
            f"{short_size_after_str:>14} "
            f"{long_avg_after_str:>14} "
            f"{short_avg_after_str:>14}"
        )
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--direction", required=True)
    ap.add_argument("--start-index", required=True)
    args = ap.parse_args()

    outdir = Path(args.output_dir)

    summary_path = find_summary_file(outdir, args.symbol)
    summary_rows = read_csv(summary_path)

    summary_row = None
    for r in summary_rows:
        if str(r.get("start_index")) == str(args.start_index):
            summary_row = r
            break

    if summary_row is None:
        raise SystemExit(f"No summary row found for start_index={args.start_index}")

    trade_block_path = find_trade_block_file(outdir, args.symbol, args.direction, args.start_index)
    trade_rows = read_csv(trade_block_path)

    print_trade_header(summary_row)
    print(f"summary_csv      = {summary_path}")
    print(f"trade_block_csv  = {trade_block_path}")
    print()

    print_timeline(trade_rows)
    print_refill_analysis(trade_rows)
    print_chart_points(trade_rows)


if __name__ == "__main__":
    main()
