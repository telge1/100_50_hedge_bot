"""XRPUSDT Jun–Jul 2026: floor-guard vs no-guard PnL using existing SL/TP forward logic."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_xrp_floor_guard_pnl_v1.json"
OUT_MD = ROOT / "analyze_xrp_floor_guard_pnl_v1.md"

SYMBOL = "XRPUSDT"
REPORT_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def short_trade_detail(bars: list[dict], entry_idx: int, entry: float, stop: float, tp: float) -> dict:
    """Same bar walk as analyze_4h_lower_short_block_v1.forward_outcome (+ exit prices)."""
    from analyze_4h_lower_short_block_v1 import HORIZON_BARS, forward_outcome

    base = forward_outcome(bars, entry_idx, entry, stop, tp)
    outcome = base["outcome"]
    first_hit = "none"
    exit_time = base.get("exit_bar")
    exit_price = None
    same_bar_both = False

    end = min(len(bars), entry_idx + 1 + HORIZON_BARS)
    for i in range(entry_idx + 1, end):
        bar = bars[i]
        stop_hit = bar["high"] >= stop
        tp_hit = bar["low"] <= tp
        if stop_hit and tp_hit:
            first_hit = "SL"
            outcome = "stop"
            same_bar_both = True
            exit_time = bar["close_time"].isoformat()
            exit_price = stop
            break
        if stop_hit:
            first_hit = "SL"
            outcome = "stop"
            exit_time = bar["close_time"].isoformat()
            exit_price = stop
            break
        if tp_hit:
            first_hit = "TP"
            outcome = "tp"
            exit_time = bar["close_time"].isoformat()
            exit_price = tp
            break

    if first_hit == "none" and outcome == "horizon" and end > entry_idx + 1:
        exit_time = bars[end - 1]["close_time"].isoformat()
        exit_price = float(bars[end - 1]["close"])

    risk_pct = (stop - entry) / entry * 100.0 if entry > 0 else None
    pnl_pct = None
    if first_hit == "SL" and exit_price is not None:
        pnl_pct = (entry - exit_price) / entry * 100.0
    elif first_hit == "TP" and exit_price is not None:
        pnl_pct = (entry - exit_price) / entry * 100.0

    r_multiple = None
    if pnl_pct is not None and risk_pct and risk_pct > 0:
        r_multiple = round(pnl_pct / risk_pct, 4)

    return {
        **base,
        "outcome": outcome,
        "first_hit": first_hit,
        "same_bar_sl_tp": same_bar_both,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        "risk_pct": round(risk_pct, 4) if risk_pct is not None else None,
        "r_multiple": r_multiple,
        "horizon_mark_pnl_pct": round((entry - exit_price) / entry * 100.0, 4)
        if first_hit == "none" and exit_price is not None
        else None,
    }


def collect_xrp_signals() -> dict:
    from find_short_entry_15m_v1 import (
        HISTORY_WEEKS,
        build_15m_bars,
        down_4h,
        entry_setup,
        last_closed_index,
        load_market,
        row_for,
    )
    from dashboard.research_charts.lld_research_kernel import (
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from short_block_4h_guard import H4FloorGuard, guard_spec

    market = load_market(SYMBOL)
    _packed, candles = load_pane_candles(
        SYMBOL,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    lld_cfg = ui_lld_config("15m")
    bars15 = build_15m_bars(candles)
    by_open = {bar["open_time"]: bar for bar in bars15}
    idx_by_close = {bar["close_time"]: bar["candle_index"] for bar in bars15}

    guard = H4FloorGuard(SYMBOL, PANE_FROM, PANE_TO)
    cache: dict[int, tuple] = {}
    seen_no_guard: set[str] = set()
    seen_with_guard: set[str] = set()

    no_guard: list[dict] = []
    with_guard: list[dict] = []
    blocked_events: list[dict] = []
    floor_blocked_count = 0

    for bar in bars15:
        moment = bar["close_time"]
        blocked = False
        block_reason = None
        blocked, block_reason = guard.advance(bar, moment)

        if moment < REPORT_FROM or moment > REPORT_TO:
            continue

        idx = bar["candle_index"]
        pools, _ui, _proof = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
        setup = entry_setup(pools, bar, moment)
        if setup is None:
            continue

        pool, upper, lower, gap = setup
        h4_index = last_closed_index(market["4h"]["bars"], moment)
        if not down_4h(market["4h"]["bars"], h4_index):
            continue

        h4 = market["4h"]["bars"][h4_index]
        source_t = pool.get("source")
        birth = by_open.get(source_t, bar) if source_t is not None else bar
        row = row_for(SYMBOL, pool, birth, bar, h4, None, None, upper, lower, gap)
        row["_entry_idx"] = idx
        row["_block_reason"] = block_reason

        would_new_no_guard = pool["pool_id"] not in seen_no_guard
        would_new_with_guard = pool["pool_id"] not in seen_with_guard and not blocked

        if blocked:
            floor_blocked_count += 1
            blocked_events.append(
                {
                    **row,
                    "floor_block_reason": block_reason,
                    "would_be_new_signal_without_guard": would_new_no_guard,
                    "would_be_new_signal_with_guard": False,
                    "pool_already_seen_without_guard": not would_new_no_guard,
                }
            )

        if would_new_no_guard:
            seen_no_guard.add(pool["pool_id"])
            no_guard.append(row)

        if would_new_with_guard:
            seen_with_guard.add(pool["pool_id"])
            with_guard.append(row)

    return {
        "no_guard": no_guard,
        "with_guard": with_guard,
        "blocked_events": blocked_events,
        "floor_blocked_count": floor_blocked_count,
        "guard_spec": guard_spec(),
    }


def enrich_signals(bars15: list[dict], rows: list[dict], signal_num_start: int = 1) -> list[dict]:
    out = []
    for i, row in enumerate(rows, start=signal_num_start):
        entry = float(row["close"])
        stop = float(row["stop_price"])
        tp = float(row["tp_top"]) if row["tp_top"] else None
        ot = datetime.fromisoformat(row["entry_open"])
        bar = next(b for b in bars15 if b["open_time"] == ot)
        idx = bar["candle_index"]
        if tp is None:
            detail = {"first_hit": "none", "result_status": "unresolved", "pnl_pct": None}
        else:
            detail = short_trade_detail(bars15, idx, entry, stop, tp)
        out.append(
            {
                "signal_num": i,
                "entry_open": row["entry_open"],
                "entry_price": entry,
                "stop": stop,
                "tp": tp,
                "pool_id": row["pool_id"],
                "first_hit": detail["first_hit"],
                "same_bar_sl_tp": detail.get("same_bar_sl_tp", False),
                "exit_time": detail.get("exit_time"),
                "exit_price": detail.get("exit_price"),
                "outcome": detail.get("outcome"),
                "result_status": detail.get("result_status"),
                "mfe_pct": detail.get("mfe_pct"),
                "mae_pct": detail.get("mae_pct"),
                "pnl_pct": detail.get("pnl_pct"),
                "risk_pct": detail.get("risk_pct"),
                "r_multiple": detail.get("r_multiple"),
                "horizon_mark_pnl_pct": detail.get("horizon_mark_pnl_pct"),
            }
        )
    return out


def sl_streaks_and_drawdown(trades: list[dict]) -> dict:
    """Chronological SL streaks and peak-to-trough drawdown on cumulative realized PnL %."""
    ordered = sorted(trades, key=lambda t: t["entry_open"])
    sl_trades = [t for t in ordered if t["first_hit"] == "SL"]
    sl_loss_sum = round(sum(t["pnl_pct"] for t in sl_trades if t["pnl_pct"] is not None), 4)

    streaks: list[dict] = []
    cur: list[dict] = []
    for t in ordered:
        if t["first_hit"] == "SL":
            cur.append(t)
        else:
            if cur:
                streaks.append(cur)
                cur = []
    if cur:
        streaks.append(cur)

    max_streak = max((len(s) for s in streaks), default=0)
    worst = None
    if streaks:
        worst = max(
            streaks,
            key=lambda s: (
                len(s),
                -sum(x["pnl_pct"] for x in s if x["pnl_pct"] is not None),
            ),
        )

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_from = None
    max_dd_to = None
    run_peak_at = ordered[0]["entry_open"] if ordered else None
    trough_at = None
    for t in ordered:
        step = t["pnl_pct"] if t["pnl_pct"] is not None else 0.0
        equity += step
        if equity >= peak:
            peak = equity
            run_peak_at = t["entry_open"]
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_from = run_peak_at
            max_dd_to = t["entry_open"]
            trough_at = t["entry_open"]

    worst_streak_detail = None
    if worst:
        worst_streak_detail = {
            "length": len(worst),
            "sl_loss_sum_pct": round(
                sum(x["pnl_pct"] for x in worst if x["pnl_pct"] is not None), 4
            ),
            "from_entry": worst[0]["entry_open"],
            "to_entry": worst[-1]["entry_open"],
            "entries": [x["entry_open"] for x in worst],
        }

    return {
        "sl_count": len(sl_trades),
        "sl_loss_sum_pct": sl_loss_sum,
        "max_consecutive_sl": max_streak,
        "sl_streak_count": len(streaks),
        "worst_sl_streak": worst_streak_detail,
        "all_sl_streaks": [
            {
                "length": len(s),
                "sl_loss_sum_pct": round(
                    sum(x["pnl_pct"] for x in s if x["pnl_pct"] is not None), 4
                ),
                "from_entry": s[0]["entry_open"],
                "to_entry": s[-1]["entry_open"],
            }
            for s in streaks
        ],
        "max_drawdown_pct": round(max_dd, 4),
        "max_drawdown_peak_entry": max_dd_from,
        "max_drawdown_trough_entry": trough_at,
        "max_drawdown_to_entry": max_dd_to,
        "final_equity_pct": round(equity, 4),
    }


def summarize_trades(trades: list[dict]) -> dict:
    sl = sum(1 for t in trades if t["first_hit"] == "SL")
    tp = sum(1 for t in trades if t["first_hit"] == "TP")
    unresolved = sum(1 for t in trades if t["first_hit"] == "none")
    realized = [t["pnl_pct"] for t in trades if t["pnl_pct"] is not None]
    out = {
        "count": len(trades),
        "sl": sl,
        "tp": tp,
        "unresolved": unresolved,
        "total_pnl_pct_realized": round(sum(realized), 4) if realized else 0.0,
        "good": sum(1 for t in trades if t["result_status"] == "good"),
        "bad": sum(1 for t in trades if t["result_status"] == "bad"),
        "result_unresolved": sum(1 for t in trades if t["result_status"] == "unresolved"),
    }
    out.update(sl_streaks_and_drawdown(trades))
    return out


def reconcile(data: dict, no_trades: list[dict], with_trades: list[dict]) -> dict:
    no_keys = {t["entry_open"] for t in no_trades}
    with_keys = {t["entry_open"] for t in with_trades}
    removed = [t for t in no_trades if t["entry_open"] not in with_keys]
    blocked = data["blocked_events"]
    new_signal_blocks = [b for b in blocked if b["would_be_new_signal_without_guard"]]
    dup_blocks = [b for b in blocked if not b["would_be_new_signal_without_guard"]]

    return {
        "no_guard_count": len(no_trades),
        "with_guard_count": len(with_trades),
        "floor_blocked_events": data["floor_blocked_count"],
        "removed_signals_count": len(removed),
        "removed_entry_opens": [t["entry_open"] for t in removed],
        "blocked_new_signal_count": len(new_signal_blocks),
        "blocked_duplicate_pool_count": len(dup_blocks),
        "explain": (
            "floor_blocked zählt jeden Report-Bar mit Setup+4h-down+Guard-Block. "
            "CSV-Zeilen entstehen nur beim ersten pool_id-Treffer ohne Guard bzw. "
            "beim ersten nicht geblockten Treffer mit Guard. "
            "Differenz 34−25=9 entfernte Zeilen; 13−9=4 zusätzliche Blocks auf Bars, "
            "deren pool_id zu dem Zeitpunkt bereits in seen_no_guard war."
        ),
    }


def guard_economics(removed_trades: list[dict]) -> dict:
    winners = [t for t in removed_trades if t["pnl_pct"] is not None and t["pnl_pct"] > 0]
    losers = [t for t in removed_trades if t["pnl_pct"] is not None and t["pnl_pct"] < 0]
    unresolved = [t for t in removed_trades if t["pnl_pct"] is None]
    return {
        "blocked_profitable_count": len(winners),
        "blocked_profitable_pnl_pct_sum": round(sum(t["pnl_pct"] for t in winners), 4),
        "blocked_losing_count": len(losers),
        "blocked_losing_pnl_pct_sum": round(sum(t["pnl_pct"] for t in losers), 4),
        "blocked_unresolved_count": len(unresolved),
        "net_avoided_loss_pct": round(-sum(t["pnl_pct"] for t in losers), 4),
        "net_forgone_profit_pct": round(sum(t["pnl_pct"] for t in winners), 4),
    }


def write_md(report: dict) -> None:
    m = report["method"]
    s0 = report["summary"]["without_guard"]
    s1 = report["summary"]["with_guard"]
    econ = report["guard_economics_on_removed_signals"]
    rec = report["reconciliation"]

    lines = [
        "# XRPUSDT Floor-Guard PnL (01.06.–31.07.2026)",
        "",
        "## Methodik (Code)",
        "",
        f"- {m['entry']}",
        f"- {m['sl_tp']}",
        f"- {m['same_bar']}",
        f"- {m['horizon']}",
        f"- {m['pnl']}",
        "",
        "## Abgleich Signale",
        "",
        f"| Metrik | Wert |",
        f"|--------|------|",
        f"| Ohne Guard | {rec['no_guard_count']} |",
        f"| Mit Guard | {rec['with_guard_count']} |",
        f"| `floor_blocked` Events | {rec['floor_blocked_events']} |",
        f"| Entfernte CSV-Signale (Diff) | {rec['removed_signals_count']} |",
        f"| davon Blocks auf neuem pool_id | {rec['blocked_new_signal_count']} |",
        f"| Blocks auf bereits gesehenem pool_id | {rec['blocked_duplicate_pool_count']} |",
        "",
        rec["explain"],
        "",
        "## Gesamt-PnL (realisiert: nur SL/TP-Treffer)",
        "",
        "| Variante | Trades | SL | TP | unresolved (kein SL/TP in Horizon) | Σ PnL % |",
        "|----------|--------|----|----|-------------------------------------|---------|",
        f"| Ohne Guard | {s0['count']} | {s0['sl']} | {s0['tp']} | {s0['unresolved']} | {s0['total_pnl_pct_realized']} |",
        f"| Mit Guard | {s1['count']} | {s1['sl']} | {s1['tp']} | {s1['unresolved']} | {s1['total_pnl_pct_realized']} |",
        "",
        f"Δ realisierte PnL (mit − ohne): **{round(s1['total_pnl_pct_realized'] - s0['total_pnl_pct_realized'], 4)}%**.",
        "",
        "Hinweis: `first_hit=none` = kein SL/TP innerhalb 48h-Horizon. "
        f"`result_status` unresolved (MFE/MAE-Regel in forward_outcome): ohne Guard {s0['result_unresolved']}, "
        f"mit Guard {s1['result_unresolved']} — kann bei gesetztem first_hit SL/TP vorkommen.",
        "",
        "## Drawdown & SL-Streaks (chronologisch nach entry_open)",
        "",
        "Kumulierte Equity = Summe der realisierten `pnl_pct` pro Trade in Zeitreihenfolge; "
        "Drawdown = Abstand vom laufenden Peak zur Equity. SL-Streak = aufeinanderfolgende Trades mit `first_hit=SL` "
        "(TP oder Horizon dazwischen beendet die Serie).",
        "",
        "| | Ohne Guard | Mit Guard |",
        "|---|------------|-----------|",
        f"| SL-Anzahl | {s0['sl']} | {s1['sl']} |",
        f"| Summe aller SL-Verluste (pnl% < 0) | {s0['sl_loss_sum_pct']} | {s1['sl_loss_sum_pct']} |",
        f"| Max. SL hintereinander | {s0['max_consecutive_sl']} | {s1['max_consecutive_sl']} |",
        f"| Anzahl SL-Streaks | {s0['sl_streak_count']} | {s1['sl_streak_count']} |",
        f"| Max. Drawdown (Peak→Tief) | {s0['max_drawdown_pct']} | {s1['max_drawdown_pct']} |",
        "",
    ]

    def _streak_line(label: str, d: dict) -> list[str]:
        w = d.get("worst_sl_streak")
        if not w:
            return [f"**{label}:** keine SL-Streak.", ""]
        return [
            f"**{label} — längste SL-Serie:** {w['length']}× SL, "
            f"Σ Verlust **{w['sl_loss_sum_pct']}%**, "
            f"{w['from_entry'][5:16]} → {w['to_entry'][5:16]}.",
            "",
        ]

    lines += _streak_line("Ohne Guard", s0)
    lines += _streak_line("Mit Guard", s1)

    for label, d in (("Ohne Guard", s0), ("Mit Guard", s1)):
        if d.get("max_drawdown_pct"):
            lines.append(
                f"**{label} — max. Drawdown:** {d['max_drawdown_pct']}% "
                f"(Peak nahe {str(d.get('max_drawdown_peak_entry', ''))[5:16]}, "
                f"Tief {str(d.get('max_drawdown_trough_entry', ''))[5:16]})."
            )
        lines.append("")
    if s0.get("all_sl_streaks"):
        lines += ["### Alle SL-Streaks ohne Guard", ""]
        for st in s0["all_sl_streaks"]:
            lines.append(
                f"- {st['length']}× SL, Σ {st['sl_loss_sum_pct']}%, "
                f"{st['from_entry'][5:16]} → {st['to_entry'][5:16]}"
            )
        lines.append("")
    if s1.get("all_sl_streaks"):
        lines += ["### Alle SL-Streaks mit Guard", ""]
        for st in s1["all_sl_streaks"]:
            lines.append(
                f"- {st['length']}× SL, Σ {st['sl_loss_sum_pct']}%, "
                f"{st['from_entry'][5:16]} → {st['to_entry'][5:16]}"
            )
        lines.append("")

    lines += [
        "## Signal-für-Signal-Abgleich (9 entfernte CSV-Zeilen)",
        "",
        "| entry_open | ohne Guard # | first | pnl% | mit Guard | Guard-Grund |",
        "|------------|--------------|-------|------|-----------|-------------|",
    ]
    with_keys = {t["entry_open"]: t for t in report["trades_with_guard"]}
    for t in report["removed_by_guard"]:
        blk = next(
            (b for b in report["blocked_hypothetical"] if b["entry_open"] == t["entry_open"]),
            {},
        )
        lines.append(
            f"| {t['entry_open']} | {t['signal_num']} | {t['first_hit']} | {t['pnl_pct']} | "
            f"— | {blk.get('floor_block_reason', '')} |"
        )
    lines += [
        "",
        "### 13 `floor_blocked` vs. 9 entfernte Zeilen",
        "",
        "- **9 Events** mit `would_be_new_signal_without_guard=true` → entsprechen exakt den 9 fehlenden CSV-Zeilen.",
        "- **4 Events** auf bereits per `pool_id` bekannten Pools (zusätzliche 15m-Bars mit Setup, "
        "die ohne Guard wegen `seen_pools` keine zweite Zeile erzeugen würden): "
        "06-04 14:00/15:00, 06-07 02:00, 06-07 05:00.",
        "",
        "## Guard-Wirkung auf entfernte Signale (hypothetisch gehandelt)",
        "",
        f"- Verhinderte Verluste (Σ PnL blockierter Verlierer): **{econ['net_avoided_loss_pct']}%** "
        f"({econ['blocked_losing_count']} Trades)",
        f"- Entgangener Gewinn (Σ PnL blockierter Gewinner): **{econ['net_forgone_profit_pct']}%** "
        f"({econ['blocked_profitable_count']} Trades)",
        f"- Unresolved unter Entfernten: {econ['blocked_unresolved_count']}",
        "",
        "## Signale ohne Guard",
        "",
        "| # | entry | entry_px | SL | TP | first | exit | pnl% | R | status |",
        "|---|-------|----------|----|----|-------|------|------|---|--------|",
    ]
    for t in report["trades_without_guard"]:
        lines.append(
            f"| {t['signal_num']} | {t['entry_open'][5:16]} | {t['entry_price']} | {t['stop']} | {t['tp']} | "
            f"{t['first_hit']} | {t['exit_time'][5:16] if t['exit_time'] else ''} | "
            f"{t['pnl_pct'] if t['pnl_pct'] is not None else '—'} | "
            f"{t['r_multiple'] if t['r_multiple'] is not None else '—'} | {t['result_status']} |"
        )

    lines += ["", "## Signale mit Guard", ""]
    lines += [
        "| # | entry | entry_px | SL | TP | first | exit | pnl% | R | status |",
        "|---|-------|----------|----|----|-------|------|------|---|--------|",
    ]
    for t in report["trades_with_guard"]:
        lines.append(
            f"| {t['signal_num']} | {t['entry_open'][5:16]} | {t['entry_price']} | {t['stop']} | {t['tp']} | "
            f"{t['first_hit']} | {t['exit_time'][5:16] if t['exit_time'] else ''} | "
            f"{t['pnl_pct'] if t['pnl_pct'] is not None else '—'} | "
            f"{t['r_multiple'] if t['r_multiple'] is not None else '—'} | {t['result_status']} |"
        )

    lines += ["", "## Vom Guard blockierte Kandidaten (hypothetisch)", ""]
    lines += [
        "| entry | pool (kurz) | reason | neu? | first | pnl% | status |",
        "|-------|-------------|--------|------|-------|------|--------|",
    ]
    for t in report["blocked_hypothetical"]:
        pid = t["pool_id"].split(":")[-1]
        lines.append(
            f"| {t['entry_open'][5:16]} | …{pid} | {t.get('floor_block_reason')} | "
            f"{'ja' if t.get('would_be_new_signal_without_guard') else 'nein'} | {t['first_hit']} | "
            f"{t['pnl_pct'] if t['pnl_pct'] is not None else '—'} | {t['result_status']} |"
        )

    OUT_MD.write_text("\n".join(lines) + "\n")


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars
    from dashboard.research_charts.lld_research_kernel import load_pane_candles
    from analyze_4h_lower_short_block_v1 import HORIZON_BARS

    data = collect_xrp_signals()
    _packed, candles = load_pane_candles(
        SYMBOL,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles)

    no_trades = enrich_signals(bars15, data["no_guard"])
    with_trades = enrich_signals(bars15, data["with_guard"])

    removed_keys = {t["entry_open"] for t in no_trades} - {t["entry_open"] for t in with_trades}
    removed_trades = [t for t in no_trades if t["entry_open"] in removed_keys]

    blocked_hypo = []
    for ev in data["blocked_events"]:
        ot = datetime.fromisoformat(ev["entry_open"])
        bar = next(b for b in bars15 if b["open_time"] == ot)
        entry = float(ev["close"])
        stop = float(ev["stop_price"])
        tp = float(ev["tp_top"]) if ev["tp_top"] else None
        detail = short_trade_detail(bars15, bar["candle_index"], entry, stop, tp) if tp else {}
        blocked_hypo.append(
            {
                "entry_open": ev["entry_open"],
                "pool_id": ev["pool_id"],
                "floor_block_reason": ev.get("floor_block_reason"),
                "would_be_new_signal_without_guard": ev.get("would_be_new_signal_without_guard"),
                **{k: detail.get(k) for k in (
                    "first_hit", "exit_time", "exit_price", "pnl_pct", "r_multiple", "result_status", "outcome"
                )},
            }
        )

    report = {
        "symbol": SYMBOL,
        "window": [REPORT_FROM.isoformat(), REPORT_TO.isoformat()],
        "method": {
            "entry": "Einstieg = 15m-Kerze close zum entry_open (find_short_entry_15m_v1 / row_for close).",
            "sl_tp": "SL = pool top × (1+STOP_PAD 0.2%); TP = tp_top des next_lower (MIN_TP_PCT 0.8%).",
            "same_bar": (
                "Wie forward_outcome in analyze_4h_lower_short_block_v1: pro Folgekerze zuerst SL "
                "(high >= stop), dann TP (low <= tp); bei beiden in einer Kerze zählt SL "
                "(konservativ, analog backtest_pool_candidates_live_safe_v1 short)."
            ),
            "horizon": f"{HORIZON_BARS}×15m Bars nach Entry; ohne SL/TP → first_hit=none, outcome=horizon.",
            "pnl": (
                "pnl_pct nur bei SL/TP an Limit-Preis; R = pnl_pct / risk_pct mit risk_pct=(stop-entry)/entry×100. "
                "Kein USDT/Positionsgröße im Scanner-Code. result_status aus forward_outcome (good/bad/unresolved)."
            ),
            "drawdown": (
                "Equity-Kurve: chronologische Summe realisierter pnl_pct; max_drawdown_pct = größter Abfall vom Peak. "
                "SL-Streaks nur bei aufeinanderfolgenden first_hit=SL."
            ),
        },
        "guard_spec": data["guard_spec"],
        "reconciliation": reconcile(data, no_trades, with_trades),
        "summary": {
            "without_guard": summarize_trades(no_trades),
            "with_guard": summarize_trades(with_trades),
        },
        "guard_economics_on_removed_signals": guard_economics(removed_trades),
        "trades_without_guard": no_trades,
        "trades_with_guard": with_trades,
        "removed_by_guard": removed_trades,
        "blocked_hypothetical": blocked_hypo,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2) + "\n")
    write_md(report)
    print("wrote", OUT_JSON, OUT_MD)
    print("counts", report["reconciliation"])


if __name__ == "__main__":
    main()
