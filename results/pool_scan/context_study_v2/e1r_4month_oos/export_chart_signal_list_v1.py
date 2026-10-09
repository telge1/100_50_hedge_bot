"""Export all with_guard signals (4mo OOS) for chart markers — no strategy change."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent

# Re-use funnel collector from sibling script
sys.path.insert(0, str(OUT_DIR))
from analyze_apr_may_funnel_v1 import (  # noqa: E402
    COINS,
    FOUR_MO_FROM,
    FOUR_MO_TO,
    apply_e1r,
    collect_funnel,
    _ensure_paths,
)


def main() -> None:
    _ensure_paths()
    rows: list[dict] = []
    for sym in COINS:
        print("export", sym, flush=True)
        data = collect_funnel(sym, FOUR_MO_FROM, FOUR_MO_TO)
        tagged, _eps = apply_e1r(data["with_guard"], sym, FOUR_MO_FROM, FOUR_MO_TO)
        for i, t in enumerate(sorted(tagged, key=lambda x: x["entry_open"]), start=1):
            ot = datetime.fromisoformat(t["entry_open"])
            rows.append(
                {
                    "signal_num": i,
                    "symbol": sym,
                    "entry_open": t["entry_open"],
                    "entry_unix": int(ot.timestamp()),
                    "entry_price": t.get("entry_price"),
                    "stop": t.get("stop"),
                    "tp": t.get("tp"),
                    "pool_id": t.get("pool_id"),
                    "first_hit": t.get("first_hit"),
                    "pnl_pct": t.get("pnl_pct"),
                    "exit_time": t.get("exit_time"),
                    "e1r_allowed": not t.get("ignored_E1R"),
                    "e1r_blocked": bool(t.get("ignored_E1R")),
                    "E1R_status": t.get("E1R_status"),
                    "decision_time": t.get("decision_time"),
                }
            )

    rows.sort(key=lambda r: (r["entry_open"], r["symbol"]))
    for n, r in enumerate(rows, start=1):
        r["global_num"] = n

    csv_path = OUT_DIR / "chart_signals_4mo_with_guard.csv"
    json_path = OUT_DIR / "chart_signals_4mo_with_guard.json"
    md_path = OUT_DIR / "chart_signals_4mo_with_guard.md"

    fields = [
        "global_num",
        "symbol",
        "entry_open",
        "entry_unix",
        "entry_price",
        "stop",
        "tp",
        "e1r_allowed",
        "e1r_blocked",
        "E1R_status",
        "first_hit",
        "pnl_pct",
        "exit_time",
        "pool_id",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    json_path.write_text(
        json.dumps(
            {
                "window": [FOUR_MO_FROM.isoformat(), FOUR_MO_TO.isoformat()],
                "coins": list(COINS),
                "variant": "with_guard",
                "count": len(rows),
                "e1r_allowed": sum(1 for r in rows if r["e1r_allowed"]),
                "e1r_blocked": sum(1 for r in rows if r["e1r_blocked"]),
                "signals": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    lines = [
        "# Chart-Signale Apr–Jul 2026 (`with_guard`)",
        "",
        f"**{len(rows)}** Signale | E1R erlaubt: **{sum(1 for r in rows if r['e1r_allowed'])}** | blockiert: **{sum(1 for r in rows if r['e1r_blocked'])}**",
        "",
        "CSV: `chart_signals_4mo_with_guard.csv` (Spalte `entry_unix` für TradingView-Import)",
        "",
        "| # | Coin | Entry (UTC) | Preis | SL | TP | E1R | Hit | pnl% |",
        "|---:|---|---|---:|---:|---:|---|---|---:|",
    ]
    for r in rows:
        e1r = "ok" if r["e1r_allowed"] else "BLOCK"
        ts = r["entry_open"][5:16].replace("T", " ")
        lines.append(
            f"| {r['global_num']} | {r['symbol']} | {ts} | {r['entry_price']} | {r['stop']} | {r['tp']} | {e1r} | {r.get('first_hit') or '—'} | {r.get('pnl_pct') or '—'} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print("Wrote", csv_path, json_path, md_path, "n=", len(rows))


if __name__ == "__main__":
    main()
