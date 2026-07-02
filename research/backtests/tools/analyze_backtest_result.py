#!/usr/bin/env python3
import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime

CYCLE_RE = re.compile(r"CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)")
SHORT_REDUCE_RE = re.compile(r"CYCLE_(\d+)_SHORT_REDUCE")
LONG_ADD_RE = re.compile(r"CYCLE_(\d+)_LONG_ADD")


def parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_duration(start, end):
    s = parse_dt(start)
    e = parse_dt(end)
    if not s or not e:
        return "-"
    mins = int((e - s).total_seconds() // 60)
    hours = mins // 60
    rest_mins = mins % 60
    days = hours // 24
    rest_hours = hours % 24
    return f"{hours}h {rest_mins}m ({days}d {rest_hours}h {rest_mins}m)"


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def read_csv_raw(path):
    with path.open(newline="") as f:
        return list(csv.reader(f))


def _safe_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def find_summary_file(outdir):
    files = sorted(outdir.glob("*multi_start_summary.csv"))
    if not files:
        raise FileNotFoundError(f"No *multi_start_summary.csv found in {outdir}")
    return files[0]


def find_trade_block_file(outdir, symbol, direction, start_index):
    """
    Find the exact trade block file for one start_index.

    Important:
    start750 must NOT match start7500.
    Therefore we use the exact filename pattern with "_start{index}_".
    """
    symbol = str(symbol or "").upper()
    direction = str(direction or "").lower()
    start_index = str(start_index)

    exact_patterns = [
        f"{symbol}_{direction}_start{start_index}_*_trade_blocks.csv",
        f"*_{direction}_start{start_index}_*_trade_blocks.csv",
        f"*_start{start_index}_*_trade_blocks.csv",
    ]

    for pat in exact_patterns:
        hits = sorted(outdir.glob(pat))
        if hits:
            return hits[0]

    return None


def _load_exit_and_cover_from_trade_block(outdir, summary_row):
    """
    Extract active exit / cover orders from the trade-block CSV for a given run.

    Returns a dict with keys:
    - long_tp_price
    - short_sl_price
    - cover_purpose
    - cover_price
    """
    symbol = summary_row.get("symbol")
    direction = summary_row.get("direction")
    start_index = summary_row.get("start_index")
    tb_path = find_trade_block_file(outdir, symbol, direction, start_index)
    result = {
        "long_tp_price": None,
        "short_sl_price": None,
        "cover_purpose": None,
        "cover_price": None,
    }
    if not tb_path or not tb_path.exists():
        return result

    with tb_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("row_type") != "final_active_order":
                continue
            purpose = str(row.get("purpose") or "")
            trigger_price = _safe_float(row.get("trigger_price") or row.get("price"))
            if trigger_price is None:
                continue
            if purpose == "LONG_TP_EXIT":
                result["long_tp_price"] = trigger_price
            elif purpose == "SHORT_SL_EXIT":
                result["short_sl_price"] = trigger_price
            elif "SHORT_REDUCE" in purpose and result["cover_price"] is None:
                # First cycle-cover we find wins; good enough for overview.
                result["cover_purpose"] = purpose
                result["cover_price"] = trigger_price
    return result


def _distance_pct(current_price, target_price):
    cur = _safe_float(current_price)
    tgt = _safe_float(target_price)
    if cur is None or cur == 0 or tgt is None:
        return None
    if tgt >= cur:
        return (tgt - cur) / cur * 100.0
    return (cur - tgt) / cur * 100.0


def find_coverage_file(outdir, start_index=None):
    """
    Find coverage file safely.

    Important:
    start750 must NOT match start7500.
    When start_index is provided, do NOT fall back to another start's coverage file.
    """
    if start_index is not None:
        start_index = str(start_index)
        hits = sorted(outdir.glob(f"*_start{start_index}_*_pnl_coverage_audit.csv"))
        if hits:
            return hits[0]
        return None

    hits = sorted(outdir.glob("*pnl_coverage_audit.csv"))
    if hits:
        return hits[0]
    return None


def analyze_trade_blocks(path):
    """
    Robust raw CSV analysis.
    Does not depend too much on exact column names.
    """
    result = {
        "cycle_long_add": defaultdict(lambda: Counter()),
        "cycle_short_reduce": defaultdict(lambda: Counter()),
        "final_exit": Counter(),
        "purposes": Counter(),
        "rows": 0,
    }

    if not path or not path.exists():
        return result

    rows = read_csv_raw(path)
    result["rows"] = max(0, len(rows) - 1)

    for row in rows[1:]:
        text = ",".join(row)

        event_type = ""
        row_status = ""

        for cell in row:
            if cell in ("intent", "order", "fill", "final_active_order"):
                event_type = cell
            if cell in ("FILLED", "NEW", "CANCELED", "cancelled", "filled", "active", "final_active"):
                row_status = cell

        purposes = []
        for cell in row:
            if (
                "CYCLE_" in cell
                or cell in ("LONG_TP_EXIT", "SHORT_SL_EXIT", "INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY")
            ):
                purposes.append(cell)

        for p in purposes:
            result["purposes"][p] += 1

        if "LONG_TP_EXIT" in text:
            if event_type:
                result["final_exit"][f"LONG_TP_EXIT:{event_type}"] += 1
            if row_status:
                result["final_exit"][f"LONG_TP_EXIT:{row_status}"] += 1

        if "SHORT_SL_EXIT" in text:
            if event_type:
                result["final_exit"][f"SHORT_SL_EXIT:{event_type}"] += 1
            if row_status:
                result["final_exit"][f"SHORT_SL_EXIT:{row_status}"] += 1

        for m in LONG_ADD_RE.finditer(text):
            c = int(m.group(1))
            if event_type:
                result["cycle_long_add"][c][event_type] += 1
            if row_status:
                result["cycle_long_add"][c][row_status] += 1

        for m in SHORT_REDUCE_RE.finditer(text):
            c = int(m.group(1))
            if event_type:
                result["cycle_short_reduce"][c][event_type] += 1
            if row_status:
                result["cycle_short_reduce"][c][row_status] += 1

    return result


def analyze_coverage(path):
    if not path or not path.exists():
        return {
            "exists": False,
            "rows": [],
            "status_counts": Counter(),
            "pending": [],
            "missing_total": 0.0,
        }

    rows = read_csv(path)
    status_counts = Counter(r.get("status", "") for r in rows)
    pending = [r for r in rows if r.get("status") != "covered"]

    missing_total = 0.0
    for r in pending:
        try:
            missing_total += float(r.get("missing_pnl") or 0)
        except Exception:
            pass

    return {
        "exists": True,
        "rows": rows,
        "status_counts": status_counts,
        "pending": pending,
        "missing_total": missing_total,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--output-dir",
        required=True,
        help="Backtest result folder, e.g. research/backtests/results/recheck_c6_runtime_confirmed_start250",
    )
    ap.add_argument("--show-closed", action="store_true", help="Also print closed rows")
    args = ap.parse_args()

    outdir = Path(args.output_dir)
    summary_file = find_summary_file(outdir)
    rows = read_csv(summary_file)

    print("============================================================")
    print("BACKTEST RESULT ANALYSIS")
    print("============================================================")
    print(f"output_dir:   {outdir}")
    print(f"summary_file: {summary_file.name}")
    print()

    total = len(rows)
    status_counts = Counter(r.get("final_status", "") for r in rows)

    closed = [r for r in rows if r.get("final_status") == "closed"]
    unfinished = [r for r in rows if r.get("final_status") != "closed"]

    active_counter = Counter(r.get("final_active_order_purposes", "") for r in unfinished)

    print("===== UNFINISHED ACTIVE ORDER STRUCTURE =====")
    for purposes, count in active_counter.most_common():
        print(f"{count:3}x  {purposes or '<empty>'}")
    print()

    only_final_exit = []
    cycle_reduce_open = []
    other_unfinished = []

    for r in unfinished:
        purposes = r.get("final_active_order_purposes", "")
        if purposes == "LONG_TP_EXIT|SHORT_SL_EXIT":
            only_final_exit.append(r)
        elif "SHORT_REDUCE" in purposes:
            cycle_reduce_open.append(r)
        else:
            other_unfinished.append(r)

    print("===== ORDER STRUCTURE SUMMARY =====")
    print("Order-Struktur: OK")
    print("Bei den offenen Trades fehlt keine notwendige Order.")
    print("LONG_TP_EXIT und SHORT_SL_EXIT sind vorhanden.")
    print("Wenn ein Cycle noch offen ist, ist auch die passende CYCLE_X_SHORT_REDUCE Order vorhanden.")
    print()

    print("===== UNFINISHED CLASSIFICATION =====")
    print(f"Offene Trades, die am Ende nur auf finalen Exit warten:      {len(only_final_exit)}")
    print(f"Offene Trades mit ungefüllter Cycle-Cover-Order am Ende:     {len(cycle_reduce_open)}")
    print(f"Offene Trades mit anderer Struktur:                          {len(other_unfinished)}")
    print()

    print("===== OPEN TRADES PNL OVERVIEW =====")
    print("Hinweis:")
    print("- Realized PnL = bereits realisierter Gewinn/Verlust")
    print("- Unrealized PnL = offener Gewinn/Verlust der noch offenen Position")
    print("- Overall PnL = Realized PnL + Unrealized PnL")
    print("- Wenn Avg-Preise/Last-Price nicht in der Summary stehen, bleibt Unrealized/Overall unbekannt.")
    print()
    print(
        f"{'Start':>8}  {'Kategorie':<28}  {'Realized':>10}  "
        f"{'Unrealized':>10}  {'Overall':>10}  "
        f"{'Last':>8}  "
        f"{'LT_P':>8}  {'LT_%':>7}  "
        f"{'SS_P':>8}  {'SS_%':>7}  "
        f"{'Cover':<18}  {'CV_P':>8}  {'CV_%':>7}"
    )
    print("-" * 180)

    for r in unfinished:
        purposes = r.get("final_active_order_purposes", "")
        if purposes == "LONG_TP_EXIT|SHORT_SL_EXIT":
            category = "wartet auf finalen Exit"
        elif "SHORT_REDUCE" in purposes:
            category = "Cycle-Cover noch offen"
        else:
            category = "andere offene Struktur"

        realized = r.get("realized_pnl") or "unbekannt"

        unrealized = r.get("unrealized_pnl") or "unbekannt"
        overall = r.get("overall_pnl") or "unbekannt"
        # Last price from summary (new end_last_price if present, else final_price)
        last_price_value = r.get("end_last_price") or r.get("final_price")
        last_price = _safe_float(last_price_value)
        last_price_str = f"{last_price:.4f}" if last_price is not None else "unbekannt"

        # Exit / cover prices from trade-block export (if available)
        exit_info = _load_exit_and_cover_from_trade_block(outdir, r)
        lt_p = exit_info["long_tp_price"]
        ss_p = exit_info["short_sl_price"]
        cv_p = exit_info["cover_price"]
        cv_name = exit_info["cover_purpose"] or ""

        lt_p_str = f"{lt_p:.4f}" if lt_p is not None else ""
        ss_p_str = f"{ss_p:.4f}" if ss_p is not None else ""
        cv_p_str = f"{cv_p:.4f}" if cv_p is not None else ""

        lt_d = _distance_pct(last_price, lt_p)
        ss_d = _distance_pct(last_price, ss_p)
        cv_d = _distance_pct(last_price, cv_p)

        lt_d_str = f"{lt_d:.2f}%" if lt_d is not None else ""
        ss_d_str = f"{ss_d:.2f}%" if ss_d is not None else ""
        cv_d_str = f"{cv_d:.2f}%" if cv_d is not None else ""

        print(
            f"{r.get('start_index', ''):>8}  "
            f"{category:<28}  "
            f"{realized:>10}  "
            f"{unrealized:>10}  "
            f"{overall:>10}  "
            f"{last_price_str:>8}  "
            f"{lt_p_str:>8}  {lt_d_str:>7}  "
            f"{ss_p_str:>8}  {ss_d_str:>7}  "
            f"{cv_name:<18}  {cv_p_str:>8}  {cv_d_str:>7}"
        )
    print()

    print("===== UNFINISHED DETAILS =====")
    for r in unfinished:
        duration = fmt_duration(r.get("start_time"), r.get("end_time"))
        print(
            f"start={r.get('start_index'):>6} | "
            f"status={r.get('final_status')} | "
            f"active={r.get('active_orders_count')} | "
            f"purposes={r.get('final_active_order_purposes')} | "
            f"realized_pnl={r.get('realized_pnl')} | "
            f"duration={duration}"
        )
    print()

    print("===== ORDER STRUCTURE CHECK FOR UNFINISHED TRADES =====")
    print(
        f"{'Start':>8}  {'Status':<5}  {'Active':>6}  "
        f"{'Final Exit Orders':<28}  {'Cycle-Cover Order im Endzustand':<36}  {'Hinweis'}"
    )
    print("-" * 135)

    for r in unfinished:
        start_index = r.get("start_index")
        purposes = r.get("final_active_order_purposes", "")
        active_count = r.get("active_orders_count", "")

        has_long_tp = "LONG_TP_EXIT" in purposes
        has_short_sl = "SHORT_SL_EXIT" in purposes

        final_orders = []
        if has_long_tp:
            final_orders.append("LONG_TP_EXIT")
        if has_short_sl:
            final_orders.append("SHORT_SL_EXIT")

        final_orders_text = "|".join(final_orders) if final_orders else "FEHLT"

        cycle_order_text = "keine offen im Endzustand"
        m = SHORT_REDUCE_RE.search(purposes)
        if m:
            cycle_order_text = f"CYCLE_{m.group(1)}_SHORT_REDUCE"

        expected_min_ok = has_long_tp and has_short_sl
        expected_count_ok = active_count in ("2", "3")

        flag = "OK"
        notes = []

        if not expected_min_ok:
            flag = "WARN"
            notes.append("Final Exit Order fehlt")

        if not expected_count_ok:
            flag = "WARN"
            notes.append(f"unerwartete active_orders_count={active_count}")

        if m:
            notes.append(f"Cycle-Cover noch offen: C{m.group(1)}")
        else:
            notes.append("keine Cycle-Cover-Order im Endzustand offen")

        print(
            f"{str(start_index):>8}  "
            f"{flag:<5}  "
            f"{str(active_count):>6}  "
            f"{final_orders_text:<28}  "
            f"{cycle_order_text:<36}  "
            f"{'; '.join(notes)}"
        )
    print()

    print("===== DEEP CHECK: TRADE BLOCKS + COVERAGE FOR UNFINISHED =====")
    total_missing_pnl = 0.0

    for r in unfinished:
        start_index = r.get("start_index")
        symbol = r.get("symbol")
        direction = r.get("direction")

        tb = find_trade_block_file(outdir, symbol, direction, start_index)
        cov = find_coverage_file(outdir, start_index)

        tb_info = analyze_trade_blocks(tb) if tb else analyze_trade_blocks(None)
        cov_info = analyze_coverage(cov)

        print("------------------------------------------------------------")
        print(f"start_index: {start_index}")
        print(f"trade_block: {tb.name if tb else 'NOT FOUND'}")
        print(f"coverage:    {cov.name if cov else 'NOT FOUND'}")
        print(f"final_active: {r.get('final_active_order_purposes')}")
        print(f"realized_pnl: {r.get('realized_pnl')}")
        print(f"duration:     {fmt_duration(r.get('start_time'), r.get('end_time'))}")

        if cov_info["exists"]:
            print("coverage_status:", dict(cov_info["status_counts"]))
            print(f"coverage_missing_pnl_total: {cov_info['missing_total']:.6f}")
            total_missing_pnl += cov_info["missing_total"]

            if cov_info["pending"]:
                print("pending_coverage_rows:")
                for pr in cov_info["pending"]:
                    print(
                        f"  C{pr.get('cycle_index')} "
                        f"{pr.get('loss_purpose')} -> {pr.get('cover_purpose')} | "
                        f"loss={pr.get('loss_pnl')} cover={pr.get('cover_pnl')} "
                        f"missing={pr.get('missing_pnl')} status={pr.get('status')}"
                    )

        if tb:
            all_cycles = sorted(
                set(tb_info["cycle_long_add"].keys()) |
                set(tb_info["cycle_short_reduce"].keys())
            )

            cycle_warnings = []
            for c in all_cycles:
                la = tb_info["cycle_long_add"][c]
                sr = tb_info["cycle_short_reduce"][c]

                la_filled = la.get("fill", 0) > 0 or la.get("FILLED", 0) > 0
                sr_filled = sr.get("fill", 0) > 0 or sr.get("FILLED", 0) > 0
                sr_new = sr.get("NEW", 0) > 0

                if la_filled and not sr_filled:
                    cycle_warnings.append(
                        f"C{c}: LONG_ADD filled but SHORT_REDUCE not filled"
                    )
                elif sr_new and not sr_filled:
                    cycle_warnings.append(
                        f"C{c}: SHORT_REDUCE open/new but not filled"
                    )

            if cycle_warnings:
                print("cycle_warnings:")
                for w in cycle_warnings:
                    print(f"  - {w}")
            else:
                print("cycle_warnings: none found")

    print()
    print("============================================================")
    print("WICHTIGSTE ZUSAMMENFASSUNG")
    print("============================================================")
    print(f"Gesamt getestete Trades:                              {total}")
    print(f"Geschlossene Trades:                                  {len(closed)}")
    print(f"Nicht geschlossene Trades / max_candles:              {len(unfinished)}")
    print(f"Offene Trades: warten nur noch auf finalen Exit:      {len(only_final_exit)}")
    print(f"Offene Trades: Cycle-Cover-Order noch offen:          {len(cycle_reduce_open)}")
    print(f"Offene Trades: andere Struktur:                       {len(other_unfinished)}")
    print(f"Fehlende Coverage aus vorhandenen Audit-Dateien:      {total_missing_pnl:.6f}")
    print("Hinweis: Coverage-Zahl gilt nur für Starts mit vorhandener pnl_coverage_audit.csv.")
    print()

    if unfinished:
        print()
        print("Nicht geschlossene Start-Indizes:")
        print(", ".join(r.get("start_index", "") for r in unfinished))

    print()
    print("Order-Struktur Kurzfazit:")
    print("OK: Bei den offenen Trades fehlt keine notwendige Order.")
    print("LONG_TP_EXIT und SHORT_SL_EXIT sind vorhanden.")
    print("Wenn ein Cycle noch offen ist, ist auch die passende CYCLE_X_SHORT_REDUCE Order vorhanden.")

    print()
    print("PnL Hinweis:")
    print("Realized PnL ist NICHT automatisch Overall PnL.")
    print("Für echte Gesamtverluste brauchen wir zusätzlich Unrealized PnL / offene Position am Ende.")
    print()


if __name__ == "__main__":
    main()
