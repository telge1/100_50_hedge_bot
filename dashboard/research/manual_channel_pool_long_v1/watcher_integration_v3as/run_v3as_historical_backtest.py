#!/usr/bin/env python3
"""L1 V3AS — historical comparison on canonical research_charts drawings (READ-ONLY)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
V3AS = Path(__file__).resolve().parent
V3AO = PROBE / "watcher_integration_v3ao"
for p in (
    PROBE,
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

OUT = V3AS
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _assert_equality() -> None:
    rc = subprocess.call(
        [sys.executable, str(V3AO / "run_v3ao_equality_test.py")],
        cwd=str(PROBE),
    )
    if rc != 0:
        raise SystemExit("V3AS aborted: equality test FAIL")


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


def _variants() -> list[tuple[str, TouchWatcherConfig | None, dict]]:
    return [
        (
            "V3AK",
            None,
            {
                "touch_episodes": 0,
                "entry_signals": 0,
            },
        ),
        (
            "Watcher_A2",
            TouchWatcherConfig(
                touch_tolerance_pct=0.03,
                confirmation_mode="A2",
                watch_window_bars=12,
                preset_id="V3AS_A2",
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
                preset_id="V3AS_A3",
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
                preset_id="V3AS_B",
            ),
            {},
        ),
    ]


def main() -> int:
    _assert_equality()
    OUT.mkdir(parents=True, exist_ok=True)
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]

    shared = load_live_research_context(out_dir=OUT, run_id=RUN_ID)
    payload = json.loads(Path(shared["drawings_snapshot_path"]).read_text(encoding="utf-8"))
    inv_rows, inv_summary = build_drawings_inventory(payload, shared["lines"], shared["ctx"])
    _write_csv(OUT / "l1_v3as_drawings_inventory.csv", inv_rows)
    (OUT / "l1_v3as_drawings_inventory_summary.json").write_text(
        json.dumps(inv_summary, indent=2), encoding="utf-8"
    )

    n_valid = int(inv_summary.get("long_support_lines_for_metrics") or inv_summary.get("trend_lines_in_backtest") or 0)
    lines = shared["lines"]
    summaries: list[dict] = []
    all_trades: list[dict] = []
    by_month: list[dict] = []
    by_line: list[dict] = []

    v3ak_pending = shared["v3ak_pending_post_d2"]
    v3ak_es = {
        "touch_episodes": len(shared["ctx"]["all_events"]),
        "entry_signals": sum(1 for p in v3ak_pending if p.get("_entry_idx")),
        "structure_rejected": sum(1 for p in shared["ctx"]["pending"] if p.get("long_trigger_allowed") != "yes"),
    }

    for label, cfg, _ in _variants():
        if cfg is None:
            pending = v3ak_pending
            entry_stats = v3ak_es
        else:
            pending, ws = build_watcher_pending_for_cfg(shared, cfg)
            entry_stats = ws

        funnel = run_full_funnel(
            pending,
            lines=lines,
            candles=shared["candles"],
            upper_line=shared["upper_line"],
            lines_by_id=shared["lines_by_id"],
            entry_stats=entry_stats,
        )
        ready = funnel["pending_ready"]
        sim = run_simulation(
            label=label,
            pending_ready=ready,
            candles=shared["candles"],
            lines_by_id=shared["lines_by_id"],
            upper_line=shared["upper_line"],
            mod=mod,
        )
        prow = {r["touch_event_id"]: r for r in ready}
        enriched = []
        for t in sim.executed:
            enriched.append(enrich_trade_row({**t, "variant": label}, prow.get(t["touch_event_id"])))
        all_trades.extend(enriched)

        sm = variant_summary(
            variant=label,
            funnel=funnel,
            slot_blocks=sim.slot_blocks,
            executed=enriched,
            n_valid_trend_lines=n_valid,
        )
        sm["run_id"] = RUN_ID
        sm["drawings_sha256"] = shared["drawings_sha256"]
        summaries.append(sm)
        by_month.extend([{**r, "variant": label} for r in rollup_by_month(enriched)])
        by_line.extend([{**r, "variant": label} for r in rollup_by_line(enriched)])

    _write_csv(OUT / "l1_v3as_variant_summary.csv", summaries)
    _write_csv(OUT / "l1_v3as_all_executed_trades.csv", all_trades)
    _write_csv(OUT / "l1_v3as_by_month.csv", by_month)
    _write_csv(OUT / "l1_v3as_by_line.csv", by_line)

    scope = {
        "run_id": RUN_ID,
        "equality_gate": "PASS",
        "drawings_ssot_path": str(CANONICAL_DRAWINGS_PATH),
        "drawings_sha256": shared["drawings_sha256"],
        "snapshot_copy": shared["drawings_snapshot_path"],
        "symbol": l1.SYMBOL,
        "timeframe": l1.TIMEFRAME,
        "window_start_utc": shared["window_start_utc"],
        "window_end_utc": shared["window_end_utc"],
        "simulation_end_utc": shared["simulation_end_utc"],
        "n_candles": shared["n_candles"],
        "causality_mode": shared["causality_mode"],
        "inventory_summary": inv_summary,
    }
    (OUT / "l1_v3as_data_scope.json").write_text(json.dumps(scope, indent=2), encoding="utf-8")

    report = _build_report(scope, summaries, inv_rows, inv_summary)
    (OUT / "L1_V3AS_HISTORICAL_BACKTEST_REPORT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AS_HISTORICAL_BACKTEST_REPORT.md")
    return 0


def _build_report(scope: dict, summaries: list[dict], inv_rows: list[dict], inv_summary: dict) -> str:
    lines = [
        "# L1 V3AS — Historischer Vergleich (Research-Charts, retrospektiv)",
        "",
        f"**Run:** `{scope['run_id']}` · Equality-Gate **PASS**",
        f"**SSOT:** `{scope['drawings_ssot_path']}`",
        f"**SHA256:** `{scope['drawings_sha256']}`",
        f"**Modus:** `{scope['causality_mode']}`",
        "",
        "## 1. Drawing-Inventar",
        "",
        f"| Kennzahl | Wert |",
        f"|---|---|",
        f"| Zeichnungen in JSON | {inv_summary['total_drawings_in_json']} |",
        f"| Parsbare Linien (5m-Geometrie) | {inv_summary['parsed_line_geometry']} |",
        f"| Trendlinien | {inv_summary['trend_lines']} |",
        f"| V-Lines (nicht Touch-SSOT) | {inv_summary['vlines_unparsed_for_touch']} |",
        f"| Long-tauglich (Stichprobe) | {inv_summary['long_eligible_trends_sample']} |",
        "",
        "**Lookahead:** Manuelle Linien · nicht kausal validiert. Keine Multi-TF-Neuinterpretation.",
        "Detail: `l1_v3as_drawings_inventory.csv`",
        "",
        "### Anker (Trends)",
        "",
    ]
    for r in inv_rows:
        if r["drawing_type"] != "trend":
            continue
        lines.append(
            f"- `{r['drawing_id']}`: {r['anchor_start_utc']} @ {r['anchor_start_price']} → "
            f"{r['anchor_end_utc']} @ {r['anchor_end_price']} · eligible={r['long_support_eligible_sample']}"
        )

    lines.extend(
        [
            "",
            f"**Daten:** {scope['window_start_utc']} → Sim-Ende {scope['simulation_end_utc']} · **{scope['n_candles']}** Kerzen 5m",
            "",
            "## 2. Varianten-Vergleich",
            "",
            "| Variante | Touch-Epi | Kandidaten | D2↓ | CAP6↓ | E3↓ | Slot↓ | Trades | TP/SL/SBE | Hit% | Σ gross% | Ø Pos.min |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|",
        ]
    )
    for s in summaries:
        exits = f"{s['exits_tp']}/{s['exits_sl']}/{s['exits_sbe']}"
        lines.append(
            f"| {s['variant']} | {s['touch_episodes']} | {s['pending_candidates']} | {s['d2_rejected']} | "
            f"{s['cap6_rejected']} | {s['e3_rejected']} | {s['slot_blocked_gate_conform']} | {s['trades_executed']} | "
            f"{exits} | {s['hit_rate_pct']} | {s['gross_pnl_pct_sum']} | {s['avg_position_minutes']} |"
        )

    lines.extend(
        [
            "",
            "**Kein Gewinner-Ranking** — explorativer Retrospektiv-Vergleich.",
            "",
            "## 3. Artefakte (Chart-Review)",
            "",
            "- `l1_v3as_all_executed_trades.csv` — Trendlinien-ID, Entry/SL/TP-%, Exit, Rendite",
            "- `l1_v3as_by_month.csv` / `l1_v3as_by_line.csv`",
            "- `l1_v3as_variant_summary.csv`",
            "- `l1_v3as_data_scope.json`",
            "",
            "Downstream unverändert (D2, CAP6, E3, SL, SBE, `execute_variant_v3y`, 1-Long).",
            "",
            "**STOP**",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
