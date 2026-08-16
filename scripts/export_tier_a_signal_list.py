#!/usr/bin/env python3
"""Read-only export of Tier-A signals + Frozen BE50 / No-BE counterfactual outcomes.

Usage:
  .venv/bin/python scripts/export_tier_a_signal_list.py
  .venv/bin/python scripts/export_tier_a_signal_list.py --hours 168 --also-all

Writes under results/full_signal_export/ (CSV + JSON + summary.md).
Does not mutate signals, outcomes, strategy, or dashboard.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.outcomes import SignalOutcomeRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.pipeline.outcome_eval import (  # noqa: E402
    RESULT_OPEN,
    display_result_for,
)
from signal_generator.pipeline.trade_plan import parse_trade_plan_from_metadata  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("export_tier_a")

OUT_DIR = ROOT / "results" / "full_signal_export"

# Column order for CSV (exact export schema)
COLUMNS = [
    # Identity
    "signal_id",
    "symbol",
    "timeframe",
    "direction",
    "signal_type",
    "strategy_version",
    "generator_version",
    # Timing
    "candle_open_time",
    "candle_close_time",
    "generated_at",
    "entry_time",
    # Trade plan
    "signal_price",
    "entry_price",
    "tp_pct",
    "sl_pct",
    "tp_price",
    "sl_price",
    "be_trigger_price",
    "break_even_price",
    # Context
    "stoch_k",
    "stoch_d",
    "wave_state",
    "tier_a",
    "tier_a_context",
    "trend_15m",
    "trend_30m",
    "trend_1h",
    "trend_4h",
    "rank_score",
    "selected",
    # Frozen outcome
    "frozen_result",
    "display_result",
    "be50_activated",
    "be50_activated_at",
    "exit_time",
    "exit_price",
    "exit_reason",
    "pnl_pct",
    "duration_seconds",
    # No-BE counterfactual
    "counterfactual_no_be_result",
    "counterfactual_no_be_exit_time",
    "counterfactual_no_be_exit_price",
    "counterfactual_no_be_exit_reason",
    "counterfactual_no_be_pnl_pct",
    "counterfactual_no_be_duration_seconds",
    # Evaluation metadata
    "evaluated_at",
    "last_evaluated_open_time",
]


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.endswith("+00:00"):
            return s.replace("+00:00", "Z")
        return s
    if hasattr(v, "isoformat"):
        t = v
        if getattr(t, "tzinfo", None) is None:
            # ClickHouse DateTime often naive UTC
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(v)


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_tier_a(
    signals: SignalRepository,
    *,
    start: datetime,
    end: datetime,
    page_size: int = 2000,
) -> list[dict[str, Any]]:
    """Paginate all Tier-A rows in [start, end) by candle_close_time."""
    rows: list[dict[str, Any]] = []
    offset = 0
    total = None
    while True:
        page, total = signals.query_signals(
            start=start,
            end=end,
            tier_a=True,
            time_field="candle_close_time",
            limit=page_size,
            offset=offset,
        )
        rows.extend(page)
        offset += len(page)
        logger.info("fetched %s / %s tier_a signals", offset, total)
        if not page or offset >= total:
            break
    return rows


def _row_from_signal_and_outcome(
    signal: dict[str, Any],
    outcome: Any | None,
) -> dict[str, Any]:
    plan = parse_trade_plan_from_metadata(signal.get("metadata"))
    signal_price = _f(signal.get("signal_price"))
    entry_price = _f(plan.get("entry_price"))
    if entry_price is None and signal_price is not None and signal_price > 0:
        entry_price = signal_price

    if outcome is not None:
        frozen = str(outcome.result or RESULT_OPEN)
        cf = outcome.counterfactual_no_be_result
        # For frozen BE without CF yet, preserve OPEN mapping
        if frozen == "BE" and not cf:
            cf = RESULT_OPEN
        display = outcome.display_result or display_result_for(frozen, cf)
        out_fields = {
            "frozen_result": frozen,
            "display_result": display,
            "be50_activated": bool(outcome.be50_activated),
            "be50_activated_at": _iso(outcome.be50_activated_at),
            "exit_time": _iso(outcome.exit_time),
            "exit_price": _f(outcome.exit_price),
            "exit_reason": outcome.exit_reason,
            "pnl_pct": _f(outcome.pnl_pct),
            "duration_seconds": outcome.duration_seconds,
            "counterfactual_no_be_result": cf,
            "counterfactual_no_be_exit_time": _iso(outcome.counterfactual_no_be_exit_time),
            "counterfactual_no_be_exit_price": _f(outcome.counterfactual_no_be_exit_price),
            "counterfactual_no_be_exit_reason": outcome.counterfactual_no_be_exit_reason,
            "counterfactual_no_be_pnl_pct": _f(outcome.counterfactual_no_be_pnl_pct),
            "counterfactual_no_be_duration_seconds": outcome.counterfactual_no_be_duration_seconds,
            "evaluated_at": _iso(outcome.evaluated_at),
            "last_evaluated_open_time": _iso(outcome.last_evaluated_open_time),
        }
    else:
        out_fields = {
            "frozen_result": RESULT_OPEN,
            "display_result": RESULT_OPEN,
            "be50_activated": False,
            "be50_activated_at": None,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_pct": None,
            "duration_seconds": None,
            "counterfactual_no_be_result": None,
            "counterfactual_no_be_exit_time": None,
            "counterfactual_no_be_exit_price": None,
            "counterfactual_no_be_exit_reason": None,
            "counterfactual_no_be_pnl_pct": None,
            "counterfactual_no_be_duration_seconds": None,
            "evaluated_at": None,
            "last_evaluated_open_time": None,
        }

    return {
        "signal_id": str(signal.get("signal_id")),
        "symbol": signal.get("symbol"),
        "timeframe": signal.get("timeframe"),
        "direction": str(signal.get("direction") or "").upper(),
        "signal_type": signal.get("signal_type"),
        "strategy_version": signal.get("strategy_version"),
        "generator_version": signal.get("generator_version"),
        "candle_open_time": _iso(signal.get("candle_open_time")),
        "candle_close_time": _iso(signal.get("candle_close_time")),
        "generated_at": _iso(signal.get("generated_at")),
        "entry_time": _iso(plan.get("entry_time")),
        "signal_price": signal_price,
        "entry_price": entry_price,
        "tp_pct": _f(plan.get("tp_pct")),
        "sl_pct": _f(plan.get("sl_pct")),
        "tp_price": _f(plan.get("tp_price")),
        "sl_price": _f(plan.get("sl_price")),
        "be_trigger_price": _f(plan.get("be_trigger_price")),
        "break_even_price": _f(plan.get("break_even_price")),
        "stoch_k": _f(signal.get("stoch_k")),
        "stoch_d": _f(signal.get("stoch_d")),
        "wave_state": signal.get("wave_state"),
        "tier_a": bool(signal.get("tier_a")),
        "tier_a_context": signal.get("tier_a_context") or "",
        "trend_15m": signal.get("trend_15m"),
        "trend_30m": signal.get("trend_30m"),
        "trend_1h": signal.get("trend_1h"),
        "trend_4h": signal.get("trend_4h"),
        "rank_score": _f(signal.get("rank_score")),
        "selected": bool(signal.get("selected")),
        **out_fields,
    }


def _sort_key(row: dict[str, Any]) -> tuple:
    et = row.get("entry_time") or row.get("candle_close_time") or ""
    return (et, str(row.get("symbol") or ""), str(row.get("timeframe") or ""))


def build_export_rows(
    signal_rows: list[dict[str, Any]],
    outcomes: SignalOutcomeRepository,
) -> list[dict[str, Any]]:
    # Dedup by signal_id (FINAL query should already be unique; belt-and-suspenders)
    by_id: dict[str, dict[str, Any]] = {}
    for r in signal_rows:
        sid = str(r.get("signal_id"))
        by_id[sid] = r
    unique_signals = list(by_id.values())

    views = outcomes.get_trade_outcomes_by_signal_ids([s["signal_id"] for s in unique_signals])
    rows = [
        _row_from_signal_and_outcome(s, views.get(str(s["signal_id"])))
        for s in unique_signals
    ]
    rows.sort(key=_sort_key)
    return rows


def quality_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [r["signal_id"] for r in rows]
    uniq = set(ids)
    display_counts = Counter(r.get("display_result") or "OPEN" for r in rows)
    frozen_counts = Counter(r.get("frozen_result") or "OPEN" for r in rows)
    null_entry = sum(1 for r in rows if r.get("entry_price") is None)
    zero_entry = sum(
        1 for r in rows if r.get("entry_price") is not None and float(r["entry_price"]) == 0.0
    )
    null_tp = sum(1 for r in rows if r.get("tp_price") is None)
    null_sl = sum(1 for r in rows if r.get("sl_price") is None)
    unresolved = sum(
        1
        for r in rows
        if (r.get("display_result") or "") in ("OPEN", "BE / OPEN")
        or r.get("frozen_result") == "OPEN"
    )
    missing_outcome = sum(
        1 for r in rows if r.get("evaluated_at") is None and r.get("frozen_result") == "OPEN"
    )
    return {
        "row_count": len(rows),
        "unique_signal_ids": len(uniq),
        "duplicate_signal_ids": len(ids) - len(uniq),
        "null_entry_price": null_entry,
        "zero_entry_price": zero_entry,
        "null_tp_price": null_tp,
        "null_sl_price": null_sl,
        "display_result_counts": dict(display_counts),
        "frozen_result_counts": dict(frozen_counts),
        "unresolved_display_or_open": unresolved,
        "missing_trade_outcome_eval": missing_outcome,
    }


def pnl_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    frozen_pnls: list[float] = []
    no_be_pnls: list[float] = []
    for r in rows:
        fr = r.get("frozen_result")
        if fr == "OPEN":
            continue
        fp = r.get("pnl_pct")
        frozen_pnls.append(float(fp) if fp is not None else 0.0)
        if fr == "BE":
            cf = r.get("counterfactual_no_be_result")
            if cf in ("WIN", "LOSS"):
                no_be_pnls.append(float(r.get("counterfactual_no_be_pnl_pct") or 0.0))
            # BE/OPEN → 0 contribution until resolved
            else:
                no_be_pnls.append(0.0)
        else:
            no_be_pnls.append(float(fp) if fp is not None else 0.0)

    frozen_total = float(sum(frozen_pnls)) if frozen_pnls else 0.0
    no_be_total = float(sum(no_be_pnls)) if no_be_pnls else 0.0
    return {
        "frozen_total_pnl": frozen_total,
        "frozen_mean_pnl": float(np.mean(frozen_pnls)) if frozen_pnls else None,
        "frozen_median_pnl": float(np.median(frozen_pnls)) if frozen_pnls else None,
        "no_be_total_pnl": no_be_total,
        "difference_no_be_minus_frozen": no_be_total - frozen_total,
        "n_pnl_rows": len(frozen_pnls),
    }


def _bucket_table(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[str(r.get(key) or "")].append(r)
    out = []
    for k in sorted(groups):
        g = groups[k]
        dc = Counter(r.get("display_result") or "OPEN" for r in g)
        ps = pnl_stats(g)
        out.append(
            {
                key: k,
                "signals": len(g),
                "WIN": dc.get("WIN", 0),
                "LOSS": dc.get("LOSS", 0),
                "BE_WIN": dc.get("BE / WIN", 0),
                "BE_LOSS": dc.get("BE / LOSS", 0),
                "BE_OPEN": dc.get("BE / OPEN", 0),
                "OPEN": dc.get("OPEN", 0),
                "frozen_pnl": ps["frozen_total_pnl"],
                "no_be_pnl": ps["no_be_total_pnl"],
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in COLUMNS})


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def render_summary(
    *,
    window_label: str,
    start: datetime,
    end: datetime,
    rows: list[dict[str, Any]],
    quality: dict[str, Any],
    pnl: dict[str, Any],
    by_tf: list[dict[str, Any]],
    by_sym: list[dict[str, Any]],
) -> str:
    dc = quality["display_result_counts"]
    lines = [
        f"# Tier-A Signal Export — {window_label}",
        "",
        "## Window",
        "",
        f"- Start (UTC): `{_iso(start)}`",
        f"- End (UTC): `{_iso(end)}` (half-open `[start, end)` on `candle_close_time`)",
        f"- Rows: **{quality['row_count']}**",
        f"- Unique `signal_id`: **{quality['unique_signal_ids']}**",
        f"- Duplicates: **{quality['duplicate_signal_ids']}**",
        "",
        "## Result Counts (`display_result`)",
        "",
        "| Result | Count |",
        "| ------ | ----: |",
        f"| WIN | {dc.get('WIN', 0)} |",
        f"| LOSS | {dc.get('LOSS', 0)} |",
        f"| BE / WIN | {dc.get('BE / WIN', 0)} |",
        f"| BE / LOSS | {dc.get('BE / LOSS', 0)} |",
        f"| BE / OPEN | {dc.get('BE / OPEN', 0)} |",
        f"| OPEN | {dc.get('OPEN', 0)} |",
        "",
        "## Frozen PnL",
        "",
        f"- total: `{pnl['frozen_total_pnl']}`",
        f"- mean: `{pnl['frozen_mean_pnl']}`",
        f"- median: `{pnl['frozen_median_pnl']}`",
        "",
        "## Counterfactual No-BE",
        "",
        f"- total no-BE pnl: `{pnl['no_be_total_pnl']}`",
        f"- difference vs Frozen (No-BE − Frozen): `{pnl['difference_no_be_minus_frozen']}`",
        "",
        "## By timeframe",
        "",
        "| TF | Signals | WIN | LOSS | BE/WIN | BE/LOSS | BE/OPEN | OPEN | Frozen PnL | No-BE PnL |",
        "| -- | ------: | --: | ---: | -----: | ------: | ------: | ---: | ---------: | --------: |",
    ]
    for r in by_tf:
        lines.append(
            f"| {r['timeframe']} | {r['signals']} | {r['WIN']} | {r['LOSS']} | "
            f"{r['BE_WIN']} | {r['BE_LOSS']} | {r['BE_OPEN']} | {r['OPEN']} | "
            f"{r['frozen_pnl']} | {r['no_be_pnl']} |"
        )
    lines += [
        "",
        "## By symbol",
        "",
        "| Symbol | Signals | WIN | LOSS | BE/WIN | BE/LOSS | BE/OPEN | OPEN | Frozen PnL | No-BE PnL |",
        "| ------ | ------: | --: | ---: | -----: | ------: | ------: | ---: | ---------: | --------: |",
    ]
    for r in by_sym:
        lines.append(
            f"| {r['symbol']} | {r['signals']} | {r['WIN']} | {r['LOSS']} | "
            f"{r['BE_WIN']} | {r['BE_LOSS']} | {r['BE_OPEN']} | {r['OPEN']} | "
            f"{r['frozen_pnl']} | {r['no_be_pnl']} |"
        )
    lines += [
        "",
        "## Data Quality",
        "",
        f"- null entry_price: {quality['null_entry_price']}",
        f"- zero entry_price: {quality['zero_entry_price']}",
        f"- null tp_price: {quality['null_tp_price']}",
        f"- null sl_price: {quality['null_sl_price']}",
        f"- unresolved (OPEN or BE / OPEN): {quality['unresolved_display_or_open']}",
        "",
        "_Descriptive only. No strategy change._",
        "",
    ]
    return "\n".join(lines)


def export_window(
    *,
    label: str,
    prefix: str,
    start: datetime,
    end: datetime,
    signals: SignalRepository,
    outcomes: SignalOutcomeRepository,
) -> dict[str, Any]:
    raw = _fetch_tier_a(signals, start=start, end=end)
    rows = build_export_rows(raw, outcomes)
    quality = quality_report(rows)
    pnl = pnl_stats(rows)
    by_tf = _bucket_table(rows, "timeframe")
    by_sym = _bucket_table(rows, "symbol")

    csv_path = OUT_DIR / f"{prefix}.csv"
    json_path = OUT_DIR / f"{prefix}.json"
    jsonl_path = OUT_DIR / f"{prefix}.jsonl"
    write_csv(csv_path, rows)
    write_json(json_path, rows)
    write_jsonl(jsonl_path, rows)

    return {
        "label": label,
        "prefix": prefix,
        "start": _iso(start),
        "end": _iso(end),
        "files": [str(csv_path.relative_to(ROOT)), str(json_path.relative_to(ROOT)), str(jsonl_path.relative_to(ROOT))],
        "quality": quality,
        "pnl": pnl,
        "by_timeframe": by_tf,
        "by_symbol": by_sym,
        "rows": rows,
        "summary_md": render_summary(
            window_label=label,
            start=start,
            end=end,
            rows=rows,
            quality=quality,
            pnl=pnl,
            by_tf=by_tf,
            by_sym=by_sym,
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--also-all", action="store_true", default=True)
    ap.add_argument("--no-all", action="store_true", help="Skip all-history export")
    args = ap.parse_args()
    also_all = bool(args.also_all) and not bool(args.no_all)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = setup_clickhouse(settings=get_clickhouse_settings())
    signals = SignalRepository(client)
    outcomes = SignalOutcomeRepository(client)

    end = datetime.now(timezone.utc).replace(microsecond=0)
    start_168 = end - timedelta(hours=max(1, args.hours))

    exports: list[dict[str, Any]] = []
    exp168 = export_window(
        label=f"{args.hours}h",
        prefix="tier_a_signals_168h",
        start=start_168,
        end=end,
        signals=signals,
        outcomes=outcomes,
    )
    exports.append(exp168)

    if also_all:
        # Earliest Tier-A candle_close_time → all history
        r = client.query(
            f"""
            SELECT min(candle_close_time) AS mn
            FROM {client.database}.signals FINAL
            WHERE tier_a = 1
            """
        )
        mn = r.result_rows[0][0] if r.result_rows else None
        if mn is None:
            logger.warning("no tier_a signals for all-history export")
        else:
            if getattr(mn, "tzinfo", None) is None:
                start_all = mn.replace(tzinfo=timezone.utc)
            else:
                start_all = mn.astimezone(timezone.utc)
            # tiny cushion so min row is included in half-open window
            start_all = start_all - timedelta(seconds=1)
            exp_all = export_window(
                label="all",
                prefix="tier_a_signals_all",
                start=start_all,
                end=end,
                signals=signals,
                outcomes=outcomes,
            )
            exports.append(exp_all)

    # Combined summary.md (168h first, then all)
    summary_parts = [e["summary_md"] for e in exports]
    (OUT_DIR / "summary.md").write_text("\n---\n\n".join(summary_parts), encoding="utf-8")

    meta = {
        "task": "EXPORT_FULL_TIER_A_SIGNAL_LIST",
        "exported_at": _iso(datetime.now(timezone.utc)),
        "read_only": True,
        "strategy_logic_changed": False,
        "filter": "tier_a = true",
        "outcome_horizon": "TRADE",
        "sort": ["entry_time ASC", "symbol", "timeframe"],
        "exports": [
            {
                "label": e["label"],
                "start": e["start"],
                "end": e["end"],
                "files": e["files"],
                "quality": e["quality"],
                "pnl": e["pnl"],
            }
            for e in exports
        ],
    }
    (OUT_DIR / "run_metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Console Abschlussbericht (168h)
    q = exp168["quality"]
    p = exp168["pnl"]
    dc = q["display_result_counts"]
    print("=== FULL_TIER_A_SIGNAL_EXPORT ===")
    print("window", exp168["start"], "→", exp168["end"])
    print("rows", q["row_count"], "unique", q["unique_signal_ids"], "dupes", q["duplicate_signal_ids"])
    print("results", dict(dc))
    print("frozen_total", p["frozen_total_pnl"], "no_be_total", p["no_be_total_pnl"], "diff", p["difference_no_be_minus_frozen"])
    print("out", OUT_DIR)
    for e in exports:
        print(" ", e["prefix"], e["quality"]["row_count"], "rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
