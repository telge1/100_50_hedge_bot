#!/usr/bin/env python3
"""L1 V3AR — Trade competition & slot occupancy (READ-ONLY)."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
V3AP = Path(__file__).resolve().parent
for p in (
    PROBE,
    V3AP,
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
from v3ak_context import build_context_from_snapshot, DEFAULT_SNAPSHOT, FROZEN_SHA256
from watcher_config import TouchWatcherConfig
from watcher_integration_v3ao.run_v3aq_slot_forensic import _build_ready_pending
from watcher_integration_v3ao.v3ap_shared_context import build_watcher_pending_for_cfg, load_shared_probe_context
from watcher_integration_v3ao.v3ar_slot_competition import run_simulation

OUT = V3AP
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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


def _variants():
    return [
        ("V3AK", None),
        (
            "A1_W12_003",
            TouchWatcherConfig(
                touch_tolerance_pct=0.03,
                confirmation_mode="A1",
                watch_window_bars=12,
                preset_id="V3AR_A1",
            ),
        ),
        (
            "A2_W12_003",
            TouchWatcherConfig(
                touch_tolerance_pct=0.03,
                confirmation_mode="A2",
                watch_window_bars=12,
                preset_id="V3AR_A2",
            ),
        ),
        (
            "A3_W12_003",
            TouchWatcherConfig(
                touch_tolerance_pct=0.03,
                confirmation_mode="A3",
                confirmation_min_gain_pct=0.20,
                watch_window_bars=12,
                preset_id="V3AR_A3",
            ),
        ),
    ]


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]
    shared = load_shared_probe_context()
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    v3ak_ready = _build_ready_pending(frozen["pending"], shared)

    all_exec: list[dict] = []
    all_blocks: list[dict] = []
    all_hypo: list[dict] = []
    stats_rows: list[dict] = []

    for label, cfg in _variants():
        ready = v3ak_ready if cfg is None else _build_ready_pending(
            build_watcher_pending_for_cfg(shared, cfg)[0], shared
        )
        sim = run_simulation(
            label=label,
            pending_ready=ready,
            candles=shared["candles"],
            lines_by_id=shared["lines_by_id"],
            upper_line=shared["upper_line"],
            mod=mod,
        )
        all_exec.extend(sim.executed)
        all_blocks.extend(sim.slot_blocks)
        all_hypo.extend(sim.hypotheticals)
        stats_rows.append({**sim.stats, "run_id": RUN_ID})

    _write_csv(OUT / "l1_v3ar_executed_trades.csv", all_exec)
    _write_csv(OUT / "l1_v3ar_slot_conflicts.csv", all_blocks)
    _write_csv(OUT / "l1_v3ar_hypothetical_blocked.csv", all_hypo)
    _write_csv(OUT / "l1_v3ar_variant_statistics.csv", stats_rows)

    by_line = _rollup(all_blocks, "blocked_line_id")
    by_blocker_line = _rollup(all_blocks, "blocker_line_id")
    by_month = _rollup_month(all_blocks)
    _write_csv(OUT / "l1_v3ar_blocks_by_blocked_line.csv", by_line)
    _write_csv(OUT / "l1_v3ar_blocks_by_blocker_line.csv", by_blocker_line)
    _write_csv(OUT / "l1_v3ar_blocks_by_month.csv", by_month)

    report = _build_report(stats_rows, all_blocks, all_hypo, shared)
    (OUT / "L1_V3AR_SLOT_COMPETITION_REPORT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AR_SLOT_COMPETITION_REPORT.md")
    return 0


def _rollup(blocks: list[dict], key: str) -> list[dict]:
    c: dict[str, Counter] = defaultdict(Counter)
    for b in blocks:
        c[b.get(key, "")]["blocks"] += 1
        if float(b.get("blocker_gross_pnl_pct") or 0) < 0:
            c[b.get(key, "")]["blocker_loss"] += 1
        elif float(b.get("blocker_gross_pnl_pct") or 0) > 0:
            c[b.get(key, "")]["blocker_win"] += 1
    return [{"line_id": k, **dict(v)} for k, v in sorted(c.items())]


def _rollup_month(blocks: list[dict]) -> list[dict]:
    c: Counter = Counter()
    for b in blocks:
        eu = b.get("entry_utc", "")
        ym = eu[:7] if len(eu) >= 7 else "unknown"
        c[ym] += 1
    return [{"year_month": k, "slot_blocks": v} for k, v in sorted(c.items())]


def _build_report(stats: list[dict], blocks: list[dict], hypo: list[dict], shared: dict) -> str:
    lines = [
        "# L1 V3AR — Trade-Konkurrenz & Slot-Belegung",
        "",
        f"**Run:** `{RUN_ID}` · **READ-ONLY** · Positionslimit unverändert.",
        f"Snapshot `{shared['drawings_sha256'][:16]}…` · Fenster s. `l1_v3ap_data_scope.json`.",
        "",
        "## 1. Varianten-Statistik",
        "",
        "| Variante | Executed | Slot-blocked (gate-OK) | Blocked % | Ø Pos.min | Ø Blocker.min | Σ realized % | Blocker loss/win |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in stats:
        lines.append(
            f"| {s['variant']} | {s['executed_trades']} | {s['gate_conform_blocked']} | "
            f"{s['blocked_pct_of_gate_conform']} | {s['avg_position_minutes']} | {s['avg_blocker_position_minutes']} | "
            f"{s['realized_gross_pnl_pct_sum']} | {s['blocker_was_loss_count']}/{s['blocker_was_win_count']} |"
        )

    lines.extend(
        [
            "",
            "**Hypothetisch (blockiert):** siehe `l1_v3ar_hypothetical_blocked.csv` — "
            "Spalte `hypothetical_note=DIAGNOSTIC_ONLY_NOT_IN_BACKTEST_SUM`.",
            "",
            "| Variante | Hypo-Zeilen | Unsafe-Σ hypo % (überlappend) |",
            "|---|---:|---:|",
        ]
    )
    for s in stats:
        lines.append(
            f"| {s['variant']} | {s['hypothetical_rows']} | {s['hypothetical_sum_naive_overlap_UNSAFE']} |"
        )

    lines.extend(
        [
            "",
            "_Hinweis:_ Mehrere blockierte Kandidaten können **dieselbe** offene Position blockieren; "
            "hypothetische Summen sind **nicht** gleichzeitig realisierbar.",
            "",
            "## 2. Trade-Konkurrenz (V3AQ-Kernfall 30.08.)",
            "",
            "A1/A2: Blocker **`te-d-8521dc37f7b9-0007`** (Entry 29.08 17:40, SL 30.08 21:00, −1,20 %) "
            "sperrt EA08-Früheinstiege; A3 führt diesen 8521-Trade in **eigener** Simulation **nicht** aus → anderer Slot.",
            "",
            "## 3. Nach Linie / Periode",
            "",
            "- `l1_v3ar_blocks_by_blocked_line.csv`",
            "- `l1_v3ar_blocks_by_blocker_line.csv`",
            "- `l1_v3ar_blocks_by_month.csv`",
            "",
            "## 4. Beurteilung",
            "",
            "Die Watcher-Logik erzeugt **gate-konforme** Signale, deren Ausführung durch **chronologische 1-Long-Regel** "
            "begrenzt wird — nicht durch fehlende D2/CAP6/E3.",
            "Frühere Bestätigung (A1/A2) erhöht **Kandidatendichte** und damit **Slot-Kollisionen** vs. V3AK; "
            "A3 verändert zusätzlich **welche** früheren Trades überhaupt ausgeführt werden (eigenes Pending).",
            "Keine Strategieempfehlung aus hypothetischer Einzelrendite (z. B. +0,71 % @ 09:35).",
            "",
            "## Artefakte",
            "",
            "- `l1_v3ar_executed_trades.csv`",
            "- `l1_v3ar_slot_conflicts.csv`",
            "- `l1_v3ar_hypothetical_blocked.csv`",
            "- `l1_v3ar_variant_statistics.csv`",
            "",
            "**STOP**",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
