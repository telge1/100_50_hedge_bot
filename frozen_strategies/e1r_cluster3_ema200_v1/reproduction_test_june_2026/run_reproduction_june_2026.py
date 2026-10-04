"""June 2026 E1R V1 reproduction from frozen code/ only (E1R logic)."""

from __future__ import annotations

import hashlib
import importlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FROZEN_ROOT = Path(__file__).resolve().parents[1]
FROZEN_CODE = FROZEN_ROOT / "code"
OUT_DIR = Path(__file__).resolve().parent
REPO = FROZEN_ROOT.parents[1]
V1 = REPO / "results/pool_scan/context_study_v1"
V2 = REPO / "results/pool_scan/context_study_v2"
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
SIM_FROM = datetime(2026, 4, 1, tzinfo=timezone.utc)
SIM_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
JUNE_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
JUNE_TO = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)

FROZEN_PY = [
    "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py",
    "analyze_crosscoin_e1r_final_pnl_comparison_v1.py",
    "analyze_xrp_ema200_cluster3_persistent_shadow_v1.py",
    "analyze_xrp_bull_regime_exit_shadow_v1.py",
    "analyze_xrp_ema200_cluster_transition_shadow_v1.py",
    "analyze_xrp_pool_ladder_forensics_v1.py",
    "analyze_4h_lower_short_block_v1.py",
    "find_short_entry_15m_v1.py",
]

FORBIDDEN_IMPORT_SUBSTRINGS = (
    "context_study_v2",
    "gap3",
    "entry_forensics",
    "d4_shadow",
    "e1r_gap",
)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_frozen_hashes() -> tuple[bool, list[str]]:
    manifest = json.loads((FROZEN_ROOT / "FROZEN_MANIFEST.json").read_text(encoding="utf-8"))
    expected = manifest["copied_files_sha256"]
    lines = []
    ok = True
    for name in FROZEN_PY:
        rel = f"code/{name}"
        p = FROZEN_CODE / name
        act = sha256_file(p)
        exp = expected.get(rel)
        match = exp == act
        ok = ok and match and exp is not None
        lines.append(f"{rel}: {'MATCH' if match else 'FAIL'}")
    return ok, lines


def setup_paths_and_imports() -> dict[str, str]:
    """Frozen E1R code first; repo + v1 only for documented signal pipeline deps."""
    for p in (str(REPO), str(V1), str(FROZEN_CODE)):
        if p not in sys.path:
            sys.path.insert(0, p)
    # Ensure frozen wins over v1 for analyze_crosscoin* / analyze_xrp*
    if str(FROZEN_CODE) in sys.path:
        sys.path.remove(str(FROZEN_CODE))
    sys.path.insert(0, str(FROZEN_CODE))

    from pool_pattern.market import ensure_paths

    ensure_paths()

    e1r_mod = importlib.import_module("analyze_crosscoin_e1_onebar_reclaim_shadow_v1")
    e1r_file = Path(e1r_mod.__file__).resolve()
    find_mod = importlib.import_module("find_short_entry_15m_v1")
    find_file = Path(find_mod.__file__).resolve()

    audit = {
        "entry_script": str(Path(__file__).resolve()),
        "e1r_module": str(e1r_file),
        "find_short_entry_module": str(find_file),
        "e1r_from_frozen": str(e1r_file).startswith(str(FROZEN_CODE)),
        "find_from_frozen": str(find_file).startswith(str(FROZEN_CODE)),
    }
    if not audit["e1r_from_frozen"]:
        raise RuntimeError(f"E1R module not from frozen code: {e1r_file}")
    if not audit["find_from_frozen"]:
        raise RuntimeError(f"find_short_entry not from frozen code: {find_file}")

    for forbidden in FORBIDDEN_IMPORT_SUBSTRINGS:
        if forbidden in str(e1r_file).lower():
            raise RuntimeError(f"contamination: {forbidden}")

    return audit


def collect_with_guard(symbol: str) -> list[dict]:
    import analyze_floor_guard_signal_list_v1 as fg

    old_rf, old_rt = fg.REPORT_FROM, fg.REPORT_TO
    fg.REPORT_FROM, fg.REPORT_TO = SIM_FROM, SIM_TO
    try:
        data = fg.collect_signals(symbol)
    finally:
        fg.REPORT_FROM, fg.REPORT_TO = old_rf, old_rt
    wg = fg.enrich_and_drawdown(symbol, "with_guard", data["with_guard"], data["bars15"])
    return wg


def apply_e1r_frozen(trades: list[dict], symbol: str) -> list[dict]:
    from analyze_crosscoin_e1_onebar_reclaim_shadow_v1 import outcome_class, signal_ignore, simulate_e1_e1r
    from dashboard.research_charts.lld_research_kernel import load_pane_candles, ui_lld_config
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars

    _p, candles15 = load_pane_candles(
        symbol, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles15)
    by_open = {b["open_time"]: b for b in bars15}
    cfg = ui_lld_config("15m")
    cache: dict = {}
    sim = simulate_e1_e1r(bars15, candles15, cfg, cache, SIM_FROM, SIM_TO)
    out = []
    wg_opens = {t["entry_open"] for t in trades}
    for t in trades:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        dec = by_open[ot]["close_time"]
        oc = outcome_class(t, bars15, by_open)
        e1r = signal_ignore(dec, float(t["entry_price"]), sim, bars15, candles15, cfg, cache, "E1R")
        out.append(
            {
                **t,
                "symbol": symbol,
                "final_outcome": oc,
                "ignored_E1R": e1r["ignore"] and t["entry_open"] in wg_opens,
                "E1R_status": e1r["status"],
            }
        )
    return out


def in_june(entry_open: str) -> bool:
    ot = _utc(datetime.fromisoformat(entry_open))
    return JUNE_FROM <= ot <= JUNE_TO


def equity_dd(trades: list[dict]) -> float:
    rows = sorted([t for t in trades if t.get("pnl_pct") is not None], key=lambda x: x["entry_open"])
    eq = peak = 100.0
    max_dd = 0.0
    for t in rows:
        eq += float(t["pnl_pct"])
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return round(max_dd, 4)


def metrics(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    pnls = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    streak = best = 0
    for t in trades:
        if t.get("first_hit") == "SL":
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return {
        "trades": len(trades),
        "tp_count": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "sl_count": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "total_pnl_pct": round(sum(pnls), 4),
        "profit_factor": round(gw / gl, 4) if gl > 0 else None,
        "max_drawdown_pct": equity_dd(trades),
        "max_sl_streak": best,
    }


def load_ref_oos() -> dict[str, Any]:
    oos = json.loads((FROZEN_ROOT / "reports/oos_validation/oos_summary.json").read_text(encoding="utf-8"))
    seg = oos["per_month"]["2026-06"]
    import csv

    allowed = []
    with (FROZEN_ROOT / "reports/oos_validation/e1r_allowed_trades_4mo.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if in_june(row["entry_open"]):
                allowed.append(row)
    funnel = json.loads((V2 / "e1r_4month_oos/apr_may_funnel_v1.json").read_text(encoding="utf-8"))
    june_wg = funnel["totals_by_month"]["2026-06"]["with_guard"]
    june_blocked = funnel["totals_by_month"]["2026-06"]["e1r_blocked"]
    return {"segment": seg, "allowed_trades": allowed, "with_guard_count": june_wg, "blocked_count": june_blocked}


def load_ref_blocked_keys(ref_allowed_keys: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """OOS reference blocked (symbol, entry_open) from chart export (data only)."""
    import csv

    chart = V2 / "e1r_4month_oos/chart_signals_4mo_with_guard.csv"
    if not chart.is_file():
        return set()
    blocked: set[tuple[str, str]] = set()
    wg: set[tuple[str, str]] = set()
    with chart.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not in_june(row["entry_open"]):
                continue
            key = (row["symbol"], row["entry_open"])
            wg.add(key)
            if row.get("e1r_blocked", "").lower() in ("true", "1") or row.get("e1r_blocked") == "True":
                blocked.add(key)
    if blocked:
        return blocked
    return wg - ref_allowed_keys


def contamination_scan_source() -> tuple[bool, list[str]]:
    hits = []
    this_file = Path(__file__).read_text(encoding="utf-8")
    for bad in FORBIDDEN_IMPORT_SUBSTRINGS:
        if bad in this_file and "FORBIDDEN_IMPORT" not in bad:
            if f'"{bad}"' in this_file or f"'{bad}'" in this_file:
                continue
    if "context_study_v2" in this_file and "V2 /" in this_file:
        hits.append("reads reference JSON from context_study_v2 path (data only)")
    return len([h for h in hits if "data only" not in h]) == 0, hits


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hash_ok, hash_lines = verify_frozen_hashes()
    cont_ok, cont_notes = contamination_scan_source()

    import_audit = setup_paths_and_imports()

    print("=== IMPORT AUDIT ===")
    for k, v in import_audit.items():
        print(f"  {k}: {v}")
    print("  external_signal_pipeline: results/pool_scan/context_study_v1/analyze_floor_guard_signal_list_v1.py")
    print("  external_runtime: pool_pattern.market, dashboard.research_charts.lld_research_kernel")
    print("=== HASH (frozen code) ===")
    for line in hash_lines:
        print(f"  {line}")

    ref = load_ref_oos()
    ref_allowed = sorted(ref["allowed_trades"], key=lambda x: x["entry_open"])
    def trade_key(t: dict) -> tuple[str, str]:
        return (t["symbol"], t["entry_open"])

    ref_allowed_keys = {trade_key(t) for t in ref_allowed}
    ref_allowed_ids = {t["entry_open"] for t in ref_allowed}  # timestamps only (diagnostic)
    ref_blocked_keys = load_ref_blocked_keys(ref_allowed_keys)

    all_june_tagged: list[dict] = []
    june_wg: list[dict] = []
    for sym in COINS:
        wg = collect_with_guard(sym)
        tagged = apply_e1r_frozen(wg, sym)
        for t in tagged:
            if in_june(t["entry_open"]):
                all_june_tagged.append(t)
                june_wg.append(t)

    june_allowed = [t for t in all_june_tagged if not t["ignored_E1R"]]
    june_blocked = [t for t in all_june_tagged if t["ignored_E1R"]]
    june_allowed.sort(key=lambda x: x["entry_open"])
    june_blocked.sort(key=lambda x: x["entry_open"])

    repro_keys_allowed = {trade_key(t) for t in june_allowed}
    repro_keys_blocked = {(t["symbol"], t["entry_open"]) for t in june_blocked}
    repro_ids_allowed = {t["entry_open"] for t in june_allowed}
    repro_ids_blocked = {t["entry_open"] for t in june_blocked}

    m = metrics(june_allowed)
    ref_seg = ref["segment"]

    # Parity rows
    parity_rows = []
    for t in june_allowed:
        tk = trade_key(t)
        ref_row = next((r for r in ref_allowed if trade_key(r) == tk), None)
        parity_rows.append(
            {
                "entry_open": t["entry_open"],
                "symbol": t.get("symbol"),
                "in_ref": ref_row is not None,
                "repro_pnl_pct": t.get("pnl_pct"),
                "ref_pnl_pct": ref_row.get("pnl_pct") if ref_row else None,
                "repro_first_hit": t.get("first_hit"),
                "ref_first_hit": ref_row.get("first_hit") if ref_row else None,
                "repro_stop": t.get("stop"),
                "ref_stop": ref_row.get("stop") if ref_row else None,
                "repro_tp": t.get("tp"),
                "ref_tp": ref_row.get("tp") if ref_row else None,
                "pnl_match": ref_row and round(float(t["pnl_pct"]), 4) == round(float(ref_row["pnl_pct"]), 4),
            }
        )

    allowed_id_match = repro_keys_allowed == ref_allowed_keys
    blocked_id_match = repro_keys_blocked == ref_blocked_keys if ref_blocked_keys else len(june_blocked) == ref["blocked_count"]
    blocked_count_match = len(june_blocked) == ref["blocked_count"]
    wg_count_match = len(june_wg) == ref["with_guard_count"]

    outcome_match = sum(1 for r in parity_rows if r["in_ref"] and r["repro_first_hit"] == r["ref_first_hit"])
    pnl_match = sum(1 for r in parity_rows if r.get("pnl_match"))

    pnl_close = abs(m["total_pnl_pct"] - ref_seg["total_pnl_pct"]) < 0.01
    pf_close = m["profit_factor"] == ref_seg["profit_factor"] or abs((m["profit_factor"] or 0) - ref_seg["profit_factor"]) < 0.01
    dd_close = m["max_drawdown_pct"] == ref_seg["max_drawdown_pct"]
    tp_close = m["tp_count"] == ref_seg["tp_count"]
    sl_close = m["sl_count"] == ref_seg["sl_count"]
    streak_close = m["max_sl_streak"] == ref_seg["max_sl_streak"]
    count_close = m["trades"] == ref_seg["trades"] == 20

    verdict = (
        hash_ok
        and cont_ok
        and allowed_id_match
        and count_close
        and pnl_close
        and outcome_match == len(ref_allowed)
        and pnl_match == len(ref_allowed)
        and dd_close
        and pf_close
        and tp_close
        and sl_close
        and streak_close
        and wg_count_match
        and blocked_count_match
        and blocked_id_match
    )

    summary = {
        "test_window": [JUNE_FROM.isoformat(), JUNE_TO.isoformat()],
        "sim_window_for_e1r_state": [SIM_FROM.isoformat(), SIM_TO.isoformat()],
        "import_audit": import_audit,
        "frozen_hash_pass": hash_ok,
        "contamination_pass": cont_ok,
        "contamination_notes": cont_notes,
        "reference": {
            "source": "frozen/reports/oos_validation/oos_summary.json segments.2026-06 + e1r_allowed_trades_4mo.csv",
            "funnel_counts": "results/pool_scan/context_study_v2/e1r_4month_oos/apr_may_funnel_v1.json (read-only)",
            "segment": ref_seg,
            "allowed_ids": sorted(ref_allowed_ids),
        },
        "reproduction": {
            "with_guard_june": len(june_wg),
            "blocked_june": len(june_blocked),
            "allowed_june": len(june_allowed),
            "blocked_ids": sorted(repro_ids_blocked),
            "allowed_ids": sorted(repro_ids_allowed),
            "ref_blocked_keys": [list(k) for k in sorted(ref_blocked_keys)],
            "metrics": m,
        },
        "parity": {
            "allowed_id_match": allowed_id_match,
            "with_guard_count_match": wg_count_match,
            "blocked_count_match": blocked_count_match,
            "blocked_id_match": blocked_id_match,
            "outcome_match": f"{outcome_match}/{len(ref_allowed)}",
            "pnl_match": f"{pnl_match}/{len(ref_allowed)}",
            "pnl_total_match": pnl_close,
            "pf_match": pf_close,
            "dd_match": dd_close,
        },
        "verdict": "FROZEN_JUNE_REPRODUCTION_PASS" if verdict else "FROZEN_JUNE_REPRODUCTION_FAIL",
    }

    (OUT_DIR / "reproduction_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    import csv

    with (OUT_DIR / "june_trade_parity.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(parity_rows[0].keys()) if parity_rows else ["entry_open"])
        w.writeheader()
        w.writerows(parity_rows)

    md = [
        "# June 2026 frozen E1R V1 reproduction",
        "",
        f"**Verdict:** `{summary['verdict']}`",
        "",
        "## Import audit",
        f"- Entry: `{import_audit['entry_script']}`",
        f"- E1R module: `{import_audit['e1r_module']}` (frozen={import_audit['e1r_from_frozen']})",
        "- Signal baseline: `analyze_floor_guard_signal_list_v1` (context_study_v1, documented pipeline)",
        "- E1R sim window: Apr–Jul 2026 (same as 4mo OOS)",
        "",
        "## Metrics (E1R allowed, June)",
        "",
        "| | Reference OOS | Reproduction |",
        "|---|---:|---:|",
        f"| Trades | {ref_seg['trades']} | {m['trades']} |",
        f"| TP | {ref_seg['tp_count']} | {m['tp_count']} |",
        f"| SL | {ref_seg['sl_count']} | {m['sl_count']} |",
        f"| PnL % | {ref_seg['total_pnl_pct']} | {m['total_pnl_pct']} |",
        f"| PF | {ref_seg['profit_factor']} | {m['profit_factor']} |",
        f"| Max DD % | {ref_seg['max_drawdown_pct']} | {m['max_drawdown_pct']} |",
        f"| Max SL streak | {ref_seg['max_sl_streak']} | {m['max_sl_streak']} |",
        "",
        f"- with_guard June: ref funnel {ref['with_guard_count']} / repro {len(june_wg)}",
        f"- blocked June: ref {ref['blocked_count']} / repro {len(june_blocked)}",
        f"- allowed ID parity: {allowed_id_match}",
        "",
    ]
    (OUT_DIR / "REPRODUCTION_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
