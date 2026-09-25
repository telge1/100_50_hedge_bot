"""CLI to inspect dry-run open/closed paper trades as markdown tables."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.forward_test.config import (
    OPEN_TRADES_FILE,
    SIGNALS_LOG,
    SUMMARY_FILE,
    TRADES_LOG,
)


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _unique_closed(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        key = str(
            r.get("trade_id")
            or f"{r.get('symbol')}|{r.get('entry_ts')}|{r.get('entry_price')}|{r.get('exit_ts')}"
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _fmt(v: object, digits: int = 6) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


def _fmt_pnl(v: object) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):+.3f}%"
    except (TypeError, ValueError):
        return str(v)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    align = []
    for h in headers:
        # numeric-ish columns right-aligned
        if h.upper() in {
            "ENTRY",
            "SL",
            "TP",
            "EXIT_PX",
            "PNL %",
            "PNL%",
        } or "PRICE" in h.upper():
            align.append("---:")
        else:
            align.append("---")
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(align) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_open() -> str:
    data = _load_json(OPEN_TRADES_FILE)
    rows = data.get("open_trades") or []
    parts = [
        f"## Offene Paper-Trades ({len(rows)})",
        "",
    ]
    if not rows:
        parts.append("_keine offenen Trades_")
        return "\n".join(parts)

    table_rows = []
    for r in rows:
        table_rows.append(
            [
                str(r.get("symbol") or ""),
                str(r.get("side") or "short"),
                str(r.get("entry_ts") or ""),
                _fmt(r.get("entry_price")),
                _fmt(r.get("stop_price")),
                _fmt(r.get("tp_price")),
                str(r.get("rank") or ""),
            ]
        )
    parts.append(
        _md_table(
            ["Symbol", "Side", "Entry TS", "Entry", "SL", "TP", "Rank"],
            table_rows,
        )
    )
    return "\n".join(parts)


def render_closed() -> str:
    rows = _unique_closed(_load_jsonl(TRADES_LOG))
    parts = [
        f"## Geschlossene Paper-Trades ({len(rows)})",
        "",
    ]
    if not rows:
        parts.append("_keine geschlossenen Trades_")
        return "\n".join(parts)

    table_rows = []
    pnls: list[float] = []
    for r in rows:
        pnl = r.get("pnl_pct")
        if pnl is not None:
            try:
                pnls.append(float(pnl))
            except (TypeError, ValueError):
                pass
        table_rows.append(
            [
                str(r.get("symbol") or ""),
                str(r.get("side") or "short"),
                str(r.get("entry_ts") or ""),
                _fmt(r.get("entry_price")),
                _fmt(r.get("stop_price")),
                _fmt(r.get("tp_price")),
                str(r.get("exit_reason") or "").upper(),
                _fmt(r.get("exit_price")),
                _fmt_pnl(pnl),
                str(r.get("exit_ts") or ""),
            ]
        )
    parts.append(
        _md_table(
            [
                "Symbol",
                "Side",
                "Entry TS",
                "Entry",
                "SL",
                "TP",
                "Exit",
                "Exit Px",
                "PnL %",
                "Exit TS",
            ],
            table_rows,
        )
    )

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    parts.extend(
        [
            "",
            "### Ergebnis geschlossen",
            "",
            f"- Trades: **{len(rows)}**",
            f"- Wins / Losses: **{len(wins)} / {len(losses)}**",
            f"- Summe PnL: **{(sum(pnls) if pnls else 0):+.3f}%**",
            f"- Ø PnL: **{(sum(pnls)/len(pnls) if pnls else 0):+.3f}%**",
        ]
    )
    return "\n".join(parts)


def render_summary() -> str:
    data = _load_json(SUMMARY_FILE)
    open_n = len((_load_json(OPEN_TRADES_FILE).get("open_trades") or []))
    closed = _unique_closed(_load_jsonl(TRADES_LOG))
    pnls = []
    for r in closed:
        if r.get("pnl_pct") is not None:
            try:
                pnls.append(float(r["pnl_pct"]))
            except (TypeError, ValueError):
                pass
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    parts = [
        "## Gesamt auf einen Blick",
        "",
        _md_table(
            ["Metric", "Wert"],
            [
                ["Offen", str(open_n)],
                ["Geschlossen (unique)", str(len(closed))],
                ["Wins", str(len(wins))],
                ["Losses", str(len(losses))],
                ["Summe PnL", f"{(sum(pnls) if pnls else 0):+.3f}%"],
                ["Ø PnL", f"{(sum(pnls)/len(pnls) if pnls else 0):+.3f}%"],
                ["Updated", str(data.get("updated_at") or "—")],
            ],
        ),
    ]
    return "\n".join(parts)


def render_paths() -> str:
    return "\n".join(
        [
            "## Dateipfade",
            "",
            f"- Open: `{OPEN_TRADES_FILE}`",
            f"- Closed: `{TRADES_LOG}`",
            f"- Summary: `{SUMMARY_FILE}`",
            f"- Signals: `{SIGNALS_LOG}`",
        ]
    )


def render_all() -> str:
    return "\n\n".join(
        [
            "# Dry-Run Paper Trades",
            render_summary(),
            render_open(),
            render_closed(),
            render_paths(),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run paper trades as markdown tables (open + closed + PnL)."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=("all", "open", "closed", "summary", "paths"),
        help="What to show (default: all)",
    )
    args = parser.parse_args(argv)

    if args.command == "open":
        print(render_open())
    elif args.command == "closed":
        print(render_closed())
    elif args.command == "summary":
        print(render_summary())
    elif args.command == "paths":
        print(render_paths())
    else:
        print(render_all())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
