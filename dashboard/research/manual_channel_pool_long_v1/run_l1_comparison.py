#!/usr/bin/env python3
"""L1 V2 — compare pilot variants A–D on the same retrospective window."""

from __future__ import annotations

import copy
import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
from config import ALL_VARIANTS, MIN_CHANNEL_ROOM_PCT, MIN_TRADE_PROFIT_PCT

OUT_DIR = PROBE_DIR


def _parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _variant_executed_set(trades: list[dict[str, Any]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for t in trades:
        if t.get("exit_kind") not in ("NO_TRADE", ""):
            out.add((t.get("entry_utc", ""), t.get("touch_drawing_id", "")))
    return out


def _diag_rows(
    pending: list[dict[str, Any]],
    trades_a: list[dict[str, Any]],
    label: str,
    lo: datetime,
    hi: datetime,
) -> list[str]:
    lines = [f"### {label}", ""]
    by_event = {t["touch_event_id"]: t for t in trades_a}
    hits = []
    for row in pending:
        tu = _parse_utc(row.get("touch_utc", ""))
        if tu is None or not (lo <= tu <= hi):
            continue
        hits.append(row)
    if not hits:
        lines.append("_No touch/candidate rows in window._")
        return lines
    for r in sorted(hits, key=lambda x: x.get("touch_utc", "")):
        sim = by_event.get(r.get("touch_event_id", ""), {})
        sim_reject = sim.get("reject_reason") or "—"
        sim_exit = sim.get("exit_kind") or "—"
        lines.append(
            f"- `{r.get('touch_event_id')}` line `{r.get('touch_drawing_id')}` touch {r.get('touch_utc')} | "
            f"long_ok={r.get('long_trigger_allowed')} | confirm={r.get('confirm_utc') or '—'} | "
            f"entry={r.get('entry_utc') or '—'} @ {r.get('entry_price') or '—'} | "
            f"pool_gate={r.get('pool_gate_qualified_count') or '—'} | "
            f"room%={r.get('channel_room_pct') or '—'} | pot%={r.get('potential_profit_pct') or '—'} | "
            f"**variant A**: exit={sim_exit} reject={sim_reject}"
        )
    return lines


def main() -> int:
    l1._bootstrap_import_paths()
    ctx = l1.build_pilot_context()
    if ctx is None:
        return 2

    pending = ctx["pending"]
    candles = ctx["candles"]
    upper_line = ctx["upper_line"]
    touch_stats = ctx["touch_stats"]
    mod = sys.modules[l1.__name__]

    results: dict[str, dict[str, Any]] = {}
    all_trades: dict[str, list[dict[str, Any]]] = {}

    for variant in ALL_VARIANTS:
        trades = pe.execute_variant(
            variant,
            copy.deepcopy(pending),
            candles,
            upper_line=upper_line,
            l1_module=mod,
        )
        all_trades[variant.code] = trades
        results[variant.code] = pe.summarize_variant(trades, touch_stats)
        csv_path = OUT_DIR / f"l1_variant_{variant.code}_trades.csv"
        fields = list(trades[0].keys()) if trades else []
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            if trades:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(trades)

    baseline_exec = _variant_executed_set(all_trades["A"])
    blocked_by_new: list[str] = []
    for t in all_trades["A"]:
        key = (t.get("entry_utc", ""), t.get("touch_drawing_id", ""))
        if key not in baseline_exec:
            continue
        if t.get("exit_kind") in ("NO_TRADE", ""):
            continue
        for code in ("B", "C", "D"):
            match = [
                x
                for x in all_trades[code]
                if x.get("touch_event_id") == t.get("touch_event_id")
            ]
            if not match:
                continue
            m = match[0]
            if m.get("exit_kind") in ("NO_TRADE", "") and m.get("reject_reason"):
                blocked_by_new.append(
                    f"- Variant **{code}** blocked `{t.get('touch_event_id')}` "
                    f"entry {t.get('entry_utc')} (baseline {t.get('exit_kind')} "
                    f"{t.get('gross_pnl_pct')}%): `{m.get('reject_reason')}`"
                )

    md: list[str] = [
        "# L1 V2 — Variant comparison (A–D)",
        "",
        "Retrospective research pilot — not a fully causal backtest.",
        "",
        f"- Window: {l1.fmt_utc(l1.WINDOW_START)} .. {l1.fmt_utc(l1.WINDOW_END)} (exclusive)",
        f"- Filters: `MIN_CHANNEL_ROOM_PCT`={MIN_CHANNEL_ROOM_PCT:.2f} %, "
        f"`MIN_TRADE_PROFIT_PCT`={MIN_TRADE_PROFIT_PCT:.2f} % (effective TP = min(pool, main upper))",
        "",
        "## Summary by variant",
        "",
        "| Var | Label | Executed | SL | Pool TP | Channel TP | Open | Σ gross PnL % |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in ALL_VARIANTS:
        s = results[variant.code]
        md.append(
            f"| **{variant.code}** | {variant.label} | {s['executed']} | {s['sl']} | "
            f"{s['tp_pool']} | {s['tp_channel']} | {s['open_end']} | {s['pnl_sum']:.4f} |"
        )

    md.extend(
        [
            "",
            "## Per-variant detail",
            "",
            "| Var | touch events | long triggers | candidate rows | executed | blocked (new filters) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for variant in ALL_VARIANTS:
        s = results[variant.code]
        new_blocks = 0
        if variant.code != "A":
            new_blocks = sum(
                1
                for t in all_trades[variant.code]
                if t.get("reject_reason")
                in (
                    "REJECTED_INSUFFICIENT_CHANNEL_ROOM",
                    "REJECTED_MIN_PROFIT_BELOW_THRESHOLD",
                    "TARGET_ALREADY_REACHED_BEFORE_ENTRY",
                    "UNRESOLVED_UPPER_CHANNEL_AT_ENTRY",
                    "UNRESOLVED_EFFECTIVE_TP",
                )
            )
        md.append(
            f"| {variant.code} | {s['touch_events']} | {s['long_triggers_allowed']} | "
            f"{s['rows']} | {s['executed']} | {new_blocks} |"
        )

    md.extend(["", "### Reject breakdown (all candidate rows)", ""])
    for variant in ALL_VARIANTS:
        s = results[variant.code]
        if not s["rejects"]:
            continue
        md.append(f"**Variant {variant.code}**")
        for k, v in sorted(s["rejects"].items(), key=lambda x: (-x[1], x[0])):
            if k:
                md.append(f"- `{k}`: {v}")
        md.append("")

    md.extend(["", "## Blocked rejects (variant entry filters)", ""])
    for variant in ALL_VARIANTS:
        if variant.code == "A":
            continue
        s = results[variant.code]
        extra = {
            k: v
            for k, v in s["rejects"].items()
            if k
            in (
                "REJECTED_INSUFFICIENT_CHANNEL_ROOM",
                "REJECTED_MIN_PROFIT_BELOW_THRESHOLD",
                "TARGET_ALREADY_REACHED_BEFORE_ENTRY",
                "UNRESOLVED_UPPER_CHANNEL_AT_ENTRY",
                "UNRESOLVED_EFFECTIVE_TP",
            )
        }
        if extra:
            md.append(f"### Variant {variant.code}")
            for k, v in sorted(extra.items()):
                md.append(f"- `{k}`: {v}")
            md.append("")

    md.extend(["## Trades prevented vs baseline A", ""])
    if blocked_by_new:
        md.extend(blocked_by_new)
    else:
        md.append("_No baseline trades blocked by B/C/D._")

    md.extend(["", "## Reference diagnostics", ""])

    loss_rows = [
        t
        for t in all_trades["A"]
        if t.get("entry_utc") == "2026-08-28 01:55:00 UTC"
        and t.get("touch_drawing_id") == "d-a6d9a05da2f1"
    ]
    md.append("### A. 2026-08-28 01:55 UTC (`d-a6d9a05da2f1`, entry 0.08920)")
    md.append("")
    for code in ("A", "B", "C", "D"):
        sub = [
            t
            for t in all_trades[code]
            if t.get("entry_utc") == "2026-08-28 01:55:00 UTC"
        ]
        if not sub:
            md.append(f"- **{code}**: no matching row")
            continue
        t = sub[0]
        md.append(
            f"- **{code}**: exit={t.get('exit_kind')} | reject={t.get('reject_reason') or '—'} | "
            f"room%={t.get('channel_room_pct') or '—'} | pot%={t.get('potential_profit_pct') or '—'} | "
            f"pnl={t.get('gross_pnl_pct') or '—'}"
        )
    md.append("")
    room_note = "—"
    if loss_rows:
        room_note = str(loss_rows[0].get("channel_room_pct") or "—")
    md.append(
        "In this window **variant D** did not add `TARGET_ALREADY_REACHED_BEFORE_ENTRY` beyond "
        "variant B/C: the 01:55 candidate is already rejected for insufficient channel room "
        f"({room_note} % < {MIN_CHANNEL_ROOM_PCT:.2f} %)."
    )

    md.append("")
    md.extend(
        _diag_rows(
            pending,
            all_trades["A"],
            "B. 2026-08-27 12:00–14:00 UTC",
            datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc),
        )
    )
    md.append(
        "_Qualified entries in this window were not traded because the reference long "
        "from 26.08 15:10 was still open (GLOBAL_MAX_ONE_OPEN_LONG)._"
    )
    md.append("")
    md.extend(
        _diag_rows(
            pending,
            all_trades["A"],
            "B. 2026-08-27 17:00–19:00 UTC",
            datetime(2026, 8, 27, 17, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 27, 19, 0, tzinfo=timezone.utc),
        )
    )
    md.append(
        "_Same: pool gate and channel room satisfied; trades blocked while the 26.08 long remained open._"
    )

    ref = [
        t
        for t in all_trades["A"]
        if t.get("entry_utc") == "2026-08-26 15:10:00 UTC"
    ]
    md.extend(["", "### C. 2026-08-26 15:10 UTC reference long", ""])
    for code in ("A", "B", "C", "D"):
        sub = [t for t in all_trades[code] if t.get("entry_utc") == "2026-08-26 15:10:00 UTC"]
        if not sub:
            md.append(f"- **{code}**: not executed / no row")
            continue
        t = sub[0]
        md.append(
            f"- **{code}**: exit={t.get('exit_kind')} | pnl={t.get('gross_pnl_pct') or '—'} | "
            f"reject={t.get('reject_reason') or '—'} | room%={t.get('channel_room_pct') or '—'}"
        )

    md.extend(
        [
            "",
            "## Channel cycle (variant D)",
            "",
            "- `upper_reached` after any completed bar with high ≥ main upper (`d-22f8d9f720ce`).",
            "- Reset when a new allowlist touch starts and prior bar close is ≥ "
            f"{MIN_CHANNEL_ROOM_PCT:.2f} % below upper line.",
            "- Ambiguous cases: mark `UNRESOLVED_CHANNEL_CYCLE` in future if needed; "
            "current pilot uses the above deterministic rule only.",
            "",
            "## Executed trades (all variants)",
            "",
        ]
    )
    for variant in ALL_VARIANTS:
        md.append(f"### Variant {variant.code}")
        md.append("")
        executed = [t for t in all_trades[variant.code] if t.get("exit_kind") not in ("NO_TRADE", "")]
        if not executed:
            md.append("_None._")
            md.append("")
            continue
        md.append(
            "| Entry UTC | line | Entry | SL | TP | upper@entry | room % | pot % | Exit | kind | PnL % |"
        )
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for t in executed:
            md.append(
                f"| {t.get('entry_utc')} | `{t.get('touch_drawing_id')}` | {t.get('entry_price')} | "
                f"{t.get('sl_price')} | {t.get('tp_price')} | {t.get('channel_price_at_entry')} | "
                f"{t.get('channel_room_pct')} | {t.get('potential_profit_pct')} | {t.get('exit_utc')} | "
                f"{t.get('exit_kind')} | {t.get('gross_pnl_pct')} |"
            )
        md.append("")

    report = OUT_DIR / "l1_variant_compare.md"
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {report}")
    for v in ALL_VARIANTS:
        print(f"  variant {v.code}: executed={results[v.code]['executed']} pnl_sum={results[v.code]['pnl_sum']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
