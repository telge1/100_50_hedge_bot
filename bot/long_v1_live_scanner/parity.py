"""Parity vs frozen long_geometry_ladder24_be100_v1."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bot.long_v1_live_scanner.config import FROZEN_TAG_COMMIT, LONG_FROZEN_ROOT, REPO_ROOT
from bot.long_v1_live_scanner.processing import SymbolProcessor
from bot.long_v1_live_scanner.config import ScannerConfig

JUN_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
JUN_JUL_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)


def _frozen_allowed_keys(symbol: str, report_from: datetime, report_to: datetime) -> set[tuple[str, str]]:
    """Oracle: frozen geometry + ladder24 only (no baseline TP/SL pre-filter)."""
    from bot.long_v1_live_scanner.config import ScannerConfig, context_entry15_module, ensure_runtime_paths
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, scanner_pools_for_index, ui_lld_config
    from frozen_strategies.long_geometry_ladder24_be100_v1.geometry import entry_setup_long, row_for_signal
    from frozen_strategies.long_geometry_ladder24_be100_v1.ladder import m15_lower_2_age_h, passes_ladder_filter

    ensure_runtime_paths()
    cfg = ScannerConfig(live=False)
    entry15 = context_entry15_module()
    end_unix = int(report_to.timestamp())
    packed, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(cfg.pane_from.timestamp()),
        to_unix=end_unix,
        history_weeks=entry15.HISTORY_WEEKS,
    )
    if not packed.get("strict_complete_buckets"):
        return set()
    bars15 = entry15.build_15m_bars(candles)
    by_open = {b["open_time"]: b for b in bars15}
    lld_cfg = ui_lld_config("15m")
    cache: dict = {}
    seen: set[str] = set()
    keys: set[tuple[str, str]] = set()
    for bar in bars15:
        moment = bar["close_time"]
        idx = bar["candle_index"]
        pools, _, _ = scanner_pools_for_index(candles, "15m", idx, lld_cfg, cache=cache)
        setup = entry_setup_long(pools, bar, moment)
        if setup is None:
            continue
        pool, bridge_lower, upper, gap = setup
        if pool["pool_id"] in seen:
            continue
        seen.add(pool["pool_id"])
        if moment < report_from or moment > report_to:
            continue
        source_t = pool.get("source")
        birth = by_open.get(source_t, bar) if source_t is not None else bar
        sig = row_for_signal(symbol, pool, birth, bar, bridge_lower, upper, gap)
        age_h = m15_lower_2_age_h(pools, moment, sig["entry_price"])
        if passes_ladder_filter(age_h):
            keys.add((sig["pool_id"], sig["entry_time"]))
    return keys


def compare_parity(symbol: str, report_from: datetime, report_to: datetime) -> dict:
    cfg = ScannerConfig(live=False, report_from=report_from, report_to=report_to)
    proc = SymbolProcessor(cfg)
    _st, logs = proc.replay_through(symbol, report_to, emit_signals=True, report_from=report_from, report_to=report_to)

    live_allowed = {
        (r["pool_id"], r["entry_time"])
        for r in logs
        if r.get("signal_status") == "ALLOWED" and r.get("symbol") == symbol
    }
    frozen_allowed = _frozen_allowed_keys(symbol, report_from, report_to)

    missing = frozen_allowed - live_allowed
    extra = live_allowed - frozen_allowed

    ladder_blocked = sum(1 for r in logs if r.get("signal_status") == "LADDER_BLOCKED")
    geometry = sum(1 for r in logs if r.get("signal_status") == "GEOMETRY")

    return {
        "symbol": symbol,
        "frozen_allowed": len(frozen_allowed),
        "live_allowed": len(live_allowed),
        "parity": not missing and not extra,
        "missing": sorted(missing)[:10],
        "extra": sorted(extra)[:10],
        "ladder_blocked_logged": ladder_blocked,
        "geometry_logged": geometry,
    }


def verify_frozen_integrity() -> tuple[bool, list[str]]:
    lines: list[str] = []
    proc = subprocess.run(
        ["python", "-m", "frozen_strategies.long_geometry_ladder24_be100_v1.reproduce", "--verify-hashes"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    lines.append(proc.stdout)
    lines.append(proc.stderr)
    ok_hash = "HASH_VERIFY_PASS" in proc.stdout or proc.returncode == 0

    git_st = subprocess.run(
        ["git", "status", "--short", str(LONG_FROZEN_ROOT.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    git_clean = not git_st.stdout.strip()
    lines.append(f"git_status: {git_st.stdout.strip() or 'clean'}")
    lines.append(f"expected_commit: {FROZEN_TAG_COMMIT}")
    return ok_hash and git_clean, lines
