#!/usr/bin/env python3
"""L1 V3AV — full historical backtest on fresh canonical drawings (READ-ONLY)."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
V3AV = Path(__file__).resolve().parent
V3AS = PROBE / "watcher_integration_v3as"
V3AO = PROBE / "watcher_integration_v3ao"
for p in (
    PROBE,
    V3AV,
    V3AS,
    V3AO,
    PROBE / "substantial_entry_gate_v3ak",
    PROBE / "realistic_pool_tp_v3ah",
    PROBE / "tp_research_v3w",
    PROBE / "support_break_exit_v3ab",
    PROBE / "support_break_exit_v3y",
    PROBE / "touch_watcher_research_v3an",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
from drawing_refresh import CANONICAL_DRAWINGS_PATH
from watcher_config import TouchWatcherConfig
from watcher_integration_v3ao.v3ap_funnel import run_full_funnel
from watcher_integration_v3ao.v3ap_shared_context import build_watcher_pending_for_cfg
from watcher_integration_v3ao.v3ar_slot_competition import run_simulation
from watcher_integration_v3as.v3as_drawings_inventory import build_drawings_inventory
from watcher_integration_v3as.v3as_live_context import load_live_research_context
from watcher_integration_v3as.v3as_metrics import enrich_trade_row, rollup_by_line, rollup_by_month, variant_summary

OUT = V3AV
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
V3AS_SNAPSHOT_GLOB = "l1_v3as_drawings_snapshot_*.json"


def _assert_equality() -> None:
    rc = subprocess.call(
        [sys.executable, str(V3AO / "run_v3ao_equality_test.py")],
        cwd=str(PROBE),
    )
    if rc != 0:
        raise SystemExit("V3AV aborted: equality test FAIL")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _load_v3as_baseline() -> dict:
    v3as_sum = V3AS / "l1_v3as_variant_summary.csv"
    v3as_trades = V3AS / "l1_v3as_all_executed_trades.csv"
    out: dict = {"available": False}
    if not v3as_sum.is_file():
        return out
    with v3as_sum.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    trades = []
    if v3as_trades.is_file():
        with v3as_trades.open(encoding="utf-8") as f:
            trades = list(csv.DictReader(f))
    old_snap = list(V3AS.glob(V3AS_SNAPSHOT_GLOB))
    old_ids = set()
    if old_snap:
        old_ids = {
            d["drawing_id"]
            for d in json.loads(old_snap[0].read_text(encoding="utf-8")).get("drawings") or []
        }
    out = {
        "available": True,
        "variant_rows": {r["variant"]: r for r in rows},
        "trade_count": len(trades),
        "old_drawing_ids": sorted(old_ids),
        "old_sha_prefix": old_snap[0].name.split("_")[-1].replace(".json", "") if old_snap else "",
    }
    return out


def _drawing_diff(old_ids: set[str], new_ids: set[str], payload: dict) -> list[dict]:
    rows: list[dict] = []
    for did in sorted(new_ids - old_ids):
        d = next(x for x in payload["drawings"] if x["drawing_id"] == did)
        rows.append(
            {
                "change": "added",
                "drawing_id": did,
                "drawing_type": d.get("drawing_type"),
                "anchor_start_ts": d.get("start_timestamp"),
                "anchor_start_price": d.get("start_price"),
                "anchor_end_ts": d.get("end_timestamp"),
                "anchor_end_price": d.get("end_price"),
            }
        )
    for did in sorted(old_ids - new_ids):
        rows.append({"change": "removed", "drawing_id": did})
    return rows


def _trade_export_row(t: dict, prow: dict | None) -> dict:
    base = enrich_trade_row(t, prow)
    pr = prow or {}
    exit_px = t.get("exit_price", "")
    return {
        "strategy_variant": t.get("variant", ""),
        "touch_drawing_id": t.get("touch_drawing_id", ""),
        "touch_event_id": t.get("touch_event_id", ""),
        "touch_utc": base.get("touch_utc", pr.get("touch_utc", "")),
        "entry_utc": t.get("entry_utc", ""),
        "entry_price": t.get("entry_price", ""),
        "sl_price": t.get("sl_price", pr.get("sl_price", "")),
        "sl_distance_pct": base.get("sl_distance_pct", ""),
        "tp_price": t.get("tp_price", pr.get("tp_price", "")),
        "tp_distance_pct": base.get("tp_distance_pct", ""),
        "tp_pool_id": pr.get("tp_pool_id", ""),
        "exit_utc": t.get("exit_utc", ""),
        "exit_price": exit_px,
        "exit_kind": t.get("exit_kind", ""),
        "gross_pnl_pct": t.get("gross_pnl_pct", ""),
        "position_minutes": t.get("position_minutes", ""),
        "confirm_variant": pr.get("confirm_variant", ""),
    }


def main() -> int:
    _assert_equality()
    OUT.mkdir(parents=True, exist_ok=True)
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]

    shared = load_live_research_context(out_dir=OUT, run_id=RUN_ID)
    payload = json.loads(Path(shared["drawings_snapshot_path"]).read_text(encoding="utf-8"))
    v3as = _load_v3as_baseline()
    new_ids = {d["drawing_id"] for d in payload.get("drawings") or []}
    diff_rows = _drawing_diff(set(v3as.get("old_drawing_ids") or []), new_ids, payload)
    _write_csv(OUT / "l1_v3av_drawings_diff_vs_v3as.csv", diff_rows)

    inv_rows, inv_summary = build_drawings_inventory(payload, shared["lines"], shared["ctx"])
    _write_csv(OUT / "l1_v3av_drawings_inventory.csv", inv_rows)
    (OUT / "l1_v3av_drawings_inventory_summary.json").write_text(
        json.dumps(inv_summary, indent=2), encoding="utf-8"
    )

    n_trend = int(inv_summary.get("trend_lines_in_backtest") or inv_summary.get("trend_lines") or 0)
    v3ak_pending = shared["v3ak_pending_post_d2"]
    v3ak_es = {
        "touch_episodes": len(shared["ctx"]["all_events"]),
        "entry_signals": sum(1 for p in v3ak_pending if p.get("_entry_idx")),
        "structure_rejected": sum(1 for p in shared["ctx"]["pending"] if p.get("long_trigger_allowed") != "yes"),
    }

    variants = [
        ("V3AK", None, v3ak_es),
        (
            "Watcher_A2",
            TouchWatcherConfig(
                touch_tolerance_pct=0.03,
                confirmation_mode="A2",
                watch_window_bars=12,
                preset_id="V3AV_A2",
            ),
            {},
        ),
        (
            "Watcher_A3",
            TouchWatcherConfig(
                touch_tolerance_pct=0.03,
                confirmation_mode="A3",
                confirmation_min_gain_pct=0.20,
                watch_window_bars=12,
                preset_id="V3AV_A3",
            ),
            {},
        ),
        (
            "Watcher_B",
            TouchWatcherConfig(
                touch_tolerance_pct=0.03,
                confirmation_mode="B_RECLAIM",
                confirmation_min_gain_pct=0.20,
                watch_window_bars=12,
                reclaim_confirmation_bars=2,
                preset_id="V3AV_B",
            ),
            {},
        ),
    ]

    summaries: list[dict] = []
    all_trades: list[dict] = []
    by_month: list[dict] = []
    by_line: list[dict] = []
    lines = shared["lines"]

    for label, cfg, default_es in variants:
        if cfg is None:
            pending = v3ak_pending
            entry_stats = v3ak_es
        else:
            pending, ws = build_watcher_pending_for_cfg(shared, cfg)
            entry_stats = {**default_es, **ws}

        funnel = run_full_funnel(
            pending,
            lines=lines,
            candles=shared["candles"],
            upper_line=shared["upper_line"],
            lines_by_id=shared["lines_by_id"],
            entry_stats=entry_stats,
        )
        sim = run_simulation(
            label=label,
            pending_ready=funnel["pending_ready"],
            candles=shared["candles"],
            lines_by_id=shared["lines_by_id"],
            upper_line=shared["upper_line"],
            mod=mod,
        )
        prow = {r["touch_event_id"]: r for r in funnel["pending_ready"]}
        enriched = [_trade_export_row({**t, "variant": label}, prow.get(t["touch_event_id"])) for t in sim.executed]
        all_trades.extend(enriched)

        sm = variant_summary(
            variant=label,
            funnel=funnel,
            slot_blocks=sim.slot_blocks,
            executed=sim.executed,
            n_valid_trend_lines=n_trend,
        )
        sm["gates_blocked_total"] = sm["d2_rejected"] + sm["cap6_rejected"] + sm["e3_rejected"]
        sm["run_id"] = RUN_ID
        sm["drawings_sha256"] = shared["drawings_sha256"]
        summaries.append(sm)
        by_month.extend([{**r, "variant": label} for r in rollup_by_month(sim.executed)])
        by_line.extend([{**r, "variant": label} for r in rollup_by_line(sim.executed)])

    _write_csv(OUT / "l1_v3av_variant_summary.csv", summaries)
    _write_csv(OUT / "l1_v3av_all_executed_trades.csv", all_trades)
    _write_csv(OUT / "l1_v3av_by_month.csv", by_month)
    _write_csv(OUT / "l1_v3av_by_line.csv", by_line)

    compare = _compare_v3as(v3as, inv_summary, summaries, all_trades, shared)
    (OUT / "l1_v3av_compare_v3as.json").write_text(json.dumps(compare, indent=2), encoding="utf-8")

    scope = {
        "run_id": RUN_ID,
        "equality_gate": "PASS",
        "drawings_ssot_path": str(CANONICAL_DRAWINGS_PATH),
        "drawings_sha256": shared["drawings_sha256"],
        "snapshot_copy": shared["drawings_snapshot_path"],
        "v3as_sha_prefix": v3as.get("old_sha_prefix", ""),
        "reproducibility": "Deterministic pipeline; same SHA + runner => same CSV",
        **{k: shared[k] for k in ("window_start_utc", "window_end_utc", "simulation_end_utc", "n_candles", "causality_mode")},
        "inventory_summary": inv_summary,
    }
    (OUT / "l1_v3av_data_scope.json").write_text(json.dumps(scope, indent=2), encoding="utf-8")

    report = _build_report(scope, summaries, compare, diff_rows, inv_rows)
    (OUT / "L1_V3AV_HISTORICAL_BACKTEST_REPORT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AV_HISTORICAL_BACKTEST_REPORT.md")
    return 0


def _compare_v3as(v3as: dict, inv_summary: dict, summaries: list[dict], trades: list[dict], shared: dict) -> dict:
    if not v3as.get("available"):
        return {"note": "V3AS baseline CSV not found"}
    old_trends = sum(1 for x in v3as.get("old_drawing_ids", []) if str(x).startswith("d-"))
    new_trends = inv_summary.get("trend_lines", 0)
    added = len([d for d in shared["lines"] if d.drawing_id not in set(v3as.get("old_drawing_ids") or [])])
    cmp_rows = {}
    for s in summaries:
        v = s["variant"]
        old = v3as["variant_rows"].get(
            v if v != "V3AK" else "V3AK",
            v3as["variant_rows"].get(v.replace("Watcher_", "Watcher_"), {}),
        )
        cmp_rows[v] = {
            "v3av_trades": s["trades_executed"],
            "v3as_trades": int(old.get("trades_executed", 0) or 0),
            "v3av_candidates": s["pending_candidates"],
            "v3as_candidates": int(old.get("pending_candidates", 0) or 0),
            "v3av_gross_sum": s["gross_pnl_pct_sum"],
            "v3as_gross_sum": float(old.get("gross_pnl_pct_sum", 0) or 0),
        }
    return {
        "v3as_drawing_count": len(v3as.get("old_drawing_ids", [])),
        "v3av_drawing_count": inv_summary.get("total_drawings_in_json"),
        "v3av_trend_lines": new_trends,
        "new_line_ids_vs_v3as": added,
        "overlap_trade_ids_with_v3as": _trade_overlap(trades, v3as),
        "by_variant": cmp_rows,
    }


def _trade_overlap(v3av_trades: list[dict], v3as: dict) -> dict:
    v3as_path = V3AS / "l1_v3as_all_executed_trades.csv"
    if not v3as_path.is_file():
        return {}
    with v3as_path.open(encoding="utf-8") as f:
        old = list(csv.DictReader(f))
    old_keys = {(r["variant"], r["touch_event_id"], r["entry_utc"]) for r in old}
    new_keys = {(r["strategy_variant"], r["touch_event_id"], r["entry_utc"]) for r in v3av_trades}
    return {
        "same_variant_entry_keys": len(old_keys & new_keys),
        "v3as_only_keys": len(old_keys - new_keys),
        "v3av_only_keys": len(new_keys - old_keys),
    }


def _build_report(scope, summaries, compare, diff_rows, inv_rows) -> str:
    lines = [
        "# L1 V3AV — Historischer Backtest (aktualisierte Trendlinien)",
        "",
        f"**Run:** `{scope['run_id']}` · Equality **PASS**",
        f"**SSOT:** `{scope['drawings_ssot_path']}`",
        f"**SHA256:** `{scope['drawings_sha256']}`",
        f"**V3AS SHA (vorher):** `{scope.get('v3as_sha_prefix', '')}`",
        f"**Modus:** `{scope.get('causality_mode', '')}`",
        "",
        "## 1. Zeichnungen vs. V3AS",
        "",
        f"| | V3AS | V3AV |",
        f"|---|---:|---:|",
        f"| JSON-Einträge | {compare.get('v3as_drawing_count', '?')} | {compare.get('v3av_drawing_count', '?')} |",
        f"| Trendlinien (Backtest) | 5 | {compare.get('v3av_trend_lines', '?')} |",
        "",
        "**Hinweis:** V3AV ersetzt die V3AS-Zeichnungsmenge vollständig (keine ID-Überlappung).",
        "Neu/entfernt: `l1_v3av_drawings_diff_vs_v3as.csv`",
        "",
        "### Trend-Anker (V3AV)",
        "",
    ]
    for r in inv_rows:
        if r.get("drawing_type") != "trend":
            continue
        lines.append(
            f"- `{r['drawing_id']}`: {r['anchor_start_utc']} @ {r['anchor_start_price']} → "
            f"{r['anchor_end_utc']} @ {r['anchor_end_price']}"
        )

    lines.extend(["", "## 2. Varianten", ""])
    lines.append(
        "| Variante | Trends | Touch-Epi | Kandidaten | Gates↓ | Slot↓ | Trades | TP/SL/SBE | Hit% | Σ gross% |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|")
    for s in summaries:
        gates = s.get("gates_blocked_total", 0)
        ex = f"{s['exits_tp']}/{s['exits_sl']}/{s['exits_sbe']}"
        lines.append(
            f"| {s['variant']} | {s['valid_trend_lines']} | {s['touch_episodes']} | {s['pending_candidates']} | "
            f"{gates} | {s['slot_blocked_gate_conform']} | {s['trades_executed']} | {ex} | {s['hit_rate_pct']} | {s['gross_pnl_pct_sum']} |"
        )

    lines.extend(["", "## 3. Vergleich V3AS", ""])
    if compare.get("by_variant"):
        lines.append("| Variante | Trades V3AS → V3AV | Kandidaten V3AS → V3AV |")
        lines.append("|---|---|---|")
        for v, c in compare["by_variant"].items():
            lines.append(
                f"| {v} | {c['v3as_trades']} → {c['v3av_trades']} | {c['v3as_candidates']} → {c['v3av_candidates']} |"
            )
    ov = compare.get("overlap_trade_ids_with_v3as", {})
    if ov:
        lines.append("")
        lines.append(
            f"- Gleiche (Variante, Event, Entry-Zeit) wie V3AS: **{ov.get('same_variant_entry_keys', 0)}** "
            f"(erwartbar **0** bei neuen Linien)"
        )
        lines.append(f"- Nur V3AV: **{ov.get('v3av_only_keys', 0)}** Trades")

    lines.extend(
        [
            "",
            "## 4. Artefakte",
            "",
            "- `l1_v3av_all_executed_trades.csv` — **Review-Liste** (TP-Pool-ID, SL/TP-%)",
            "- `l1_v3av_variant_summary.csv`",
            "- `l1_v3av_by_month.csv` / `l1_v3av_by_line.csv`",
            "- Snapshot: im Ordner `l1_v3av_drawings_snapshot_<sha>.json`",
            "",
            "**STOP** — keine Zeichnungs-/Produktionsänderungen.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
