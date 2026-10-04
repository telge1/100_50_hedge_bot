"""Historical parity vs frozen batch oracle (tests only — may import frozen code)."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.e1r_live_scanner.config import (
    CONTEXT_STUDY_V1,
    FROZEN_CODE,
    FROZEN_ROOT,
    REPO_ROOT,
    ScannerConfig,
)
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.state import Readiness

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
SIM_FROM = datetime(2026, 4, 1, tzinfo=timezone.utc)
SIM_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
JUNE_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
JUNE_TO = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_frozen_integrity() -> tuple[bool, list[str]]:
    manifest = json.loads((FROZEN_ROOT / "FROZEN_MANIFEST.json").read_text(encoding="utf-8"))
    expected = manifest.get("copied_files_sha256") or manifest.get("source_sha256_verified_at_copy_time") or {}
    lines: list[str] = []
    ok = True
    for rel, exp in expected.items():
        if not rel.startswith("code/"):
            continue
        p = FROZEN_ROOT / rel
        if not p.is_file():
            ok = False
            lines.append(f"{rel}: MISSING")
            continue
        act = sha256_file(p)
        match = act == exp
        ok = ok and match
        lines.append(f"{rel}: {'MATCH' if match else 'FAIL'}")
    return ok, lines


def _setup_frozen_oracle() -> None:
    for p in (str(REPO_ROOT), str(CONTEXT_STUDY_V1), str(FROZEN_CODE)):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    if str(FROZEN_CODE) in sys.path:
        sys.path.remove(str(FROZEN_CODE))
    sys.path.insert(0, str(FROZEN_CODE))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def oracle_with_guard(symbol: str, report_from: datetime, report_to: datetime) -> list[dict]:
    _setup_frozen_oracle()
    import analyze_floor_guard_signal_list_v1 as fg

    old_rf, old_rt = fg.REPORT_FROM, fg.REPORT_TO
    old_pf, old_pt = fg.PANE_FROM, fg.PANE_TO
    fg.REPORT_FROM, fg.REPORT_TO = report_from, report_to
    fg.PANE_FROM, fg.PANE_TO = PANE_FROM, PANE_TO
    try:
        data = fg.collect_signals(symbol)
        wg = fg.enrich_and_drawdown(symbol, "with_guard", data["with_guard"], data["bars15"])
    finally:
        fg.REPORT_FROM, fg.REPORT_TO = old_rf, old_rt
        fg.PANE_FROM, fg.PANE_TO = old_pf, old_pt
    return wg


def oracle_e1r_decisions(
    symbol: str,
    trades: list[dict],
    sim_from: datetime,
    sim_to: datetime,
) -> dict[tuple[str, str], dict]:
    _setup_frozen_oracle()
    from analyze_crosscoin_e1_onebar_reclaim_shadow_v1 import signal_ignore, simulate_e1_e1r
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars

    _p, candles15 = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}
    sim = simulate_e1_e1r(bars15, candles15, cfg, cache, sim_from, sim_to)
    out: dict[tuple[str, str], dict] = {}
    for t in trades:
        ot = datetime.fromisoformat(t["entry_open"])
        if ot.tzinfo is None:
            ot = ot.replace(tzinfo=timezone.utc)
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        e1r = signal_ignore(dec, float(t["entry_price"]), sim, bars15, candles15, cfg, cache, "E1R")
        key = (symbol, t["entry_open"])
        out[key] = {
            "ignored": bool(e1r["ignore"]),
            "status": e1r["status"],
            "machine": sim["e1r_machine"].get(dec, "INACTIVE"),
        }
    return out


def incremental_with_guard_and_e1r(
    symbol: str,
    report_from: datetime,
    report_to: datetime,
    sim_from: datetime,
    sim_to: datetime,
) -> tuple[list[str], dict[tuple[str, str], dict]]:
    cfg = ScannerConfig(
        pane_from=PANE_FROM,
        pane_to=PANE_TO,
        e1r_sim_from=sim_from,
        report_from=report_from,
        report_to=report_to,
        live=False,
    )
    proc = SymbolProcessor(cfg)
    _st, logs = proc.replay_through(symbol, sim_to, emit_signals=True)
    if _st.readiness == Readiness.DATA_MISSING:
        raise RuntimeError(_st.data_missing_reason)
    wg_keys = sorted({t["entry_open"] for t in logs if t.get("variant") == "with_guard"})
    e1r_map: dict[tuple[str, str], dict] = {}
    for r in logs:
        if r.get("variant") != "with_guard":
            continue
        key = (r["symbol"], r["entry_open"])
        e1r_map[key] = {
            "ignored": r.get("signal_status") == "BLOCKED",
            "status": r.get("block_reason") or r.get("e1r_state"),
            "machine": r.get("e1r_state"),
        }
    return wg_keys, e1r_map


def compare_parity(
    symbol: str,
    window_from: datetime,
    window_to: datetime,
) -> dict[str, Any]:
    wg = oracle_with_guard(symbol, window_from, window_to)
    wg_oracle = sorted({t["entry_open"] for t in wg})
    e1r_oracle = oracle_e1r_decisions(symbol, wg, SIM_FROM, SIM_TO)

    wg_inc, e1r_inc = incremental_with_guard_and_e1r(symbol, window_from, window_to, SIM_FROM, SIM_TO)

    baseline_ok = wg_oracle == wg_inc
    e1r_ok = True
    mismatches: list[str] = []
    for key, o in e1r_oracle.items():
        inc = e1r_inc.get(key)
        if not inc:
            e1r_ok = False
            mismatches.append(f"missing incremental {key}")
            continue
        if o["ignored"] != inc["ignored"]:
            e1r_ok = False
            mismatches.append(f"ignore {key} oracle={o['ignored']} inc={inc['ignored']}")
    for key in e1r_inc:
        if key not in e1r_oracle:
            e1r_ok = False
            mismatches.append(f"extra incremental {key}")

    return {
        "symbol": symbol,
        "baseline_parity": baseline_ok,
        "e1r_parity": e1r_ok,
        "oracle_wg_count": len(wg_oracle),
        "incremental_wg_count": len(wg_inc),
        "mismatches": mismatches[:20],
    }
