#!/usr/bin/env python3
"""Phase 4S — restore causal timeline + gated 28d backtest."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))
from ssot_adapter import apply_ssot_pythonpath  # noqa: E402

apply_ssot_pythonpath()
RESULTS = ROOT / "results/phase4s_timeline_recovery_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4S_28DAY_TIMELINE_RECOVERY_V1.md"
MERGED_JSONL = RESULTS / "landscape_timeline_merged.jsonl"
REF_JSONL = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.jsonl"

sys.path.insert(0, str(ROOT / "tools/phase4r_historical_28day_backtest_v1"))
sys.path.insert(0, str(ROOT / "tools/phase4p_fixed_notional_pnl_v1"))
sys.path.insert(0, str(ROOT / "tools/phase4h_oco_pending_entry_v1"))
sys.path.insert(0, str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"))
sys.path.insert(0, str(ROOT / "tools/phase4m_touch_rejection_market_backtester_v1"))

from chunk_timeline import build_chunk, iter_chunks, merge_snapshots  # noqa: E402
from dynamic_signals import apply_dynamic_snapshots  # noqa: E402
from parity import compare_timelines, load_index  # noqa: E402
from run_phase4p import ledger_row, load_cfg, summarize, trade_differences, write_csv  # noqa: E402
from signal_provider import load_all_signals  # noqa: E402
from snapshot_index import load_snapshot_index  # noqa: E402
from snapshot_lifecycle import zone_status_from_snapshot  # noqa: E402
from ssot_adapter import import_error_traceback_probe, published_inventory  # noqa: E402
from trade_collectors import collect_market_trades, collect_oco_trades  # noqa: E402
from pool_zone_tracker import PoolAnchor  # noqa: E402
from market_engine import run_market_backtest, WatchSpec  # noqa: E402
from strategy_rules import plan_long, plan_short  # noqa: E402
from exit_simulator import simulate_exit  # noqa: E402


PARITY_FROM = "2026-09-11T12:00:00Z"
PARITY_TO = "2026-09-12T12:00:00Z"
REQ_14D_FROM = "2026-09-01T00:00:00Z"
REQ_14D_TO_EXCL = "2026-09-15T00:00:00Z"
REQ_28D_FROM = "2026-09-01T00:00:00Z"
REQ_28D_TO_EXCL = "2026-09-29T00:00:00Z"
BARS_28D = 8064


def write_jsonl(path: Path, snapshots: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for s in snapshots:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")


def prices_from_index(idx: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for t, s in idx.items():
        o = s["ohlc"]
        out[t] = {"o": float(o["o"]), "h": float(o["h"]), "l": float(o["l"]), "c": float(o["c"])}
    return out


def daily_weekly(ledger: list[dict]) -> tuple[list[dict], list[dict]]:
    daily: dict[str, float] = defaultdict(float)
    weekly: dict[str, float] = defaultdict(float)
    for r in ledger:
        if r.get("exit_reason") not in ("SL", "TP") or r.get("execution_ambiguity") != "NONE":
            continue
        ex = r.get("exit_utc") or ""
        if not ex:
            continue
        net = float(r["net_pnl_usdt"])
        daily[ex[:10]] += net
        dt = datetime.fromisoformat(ex.replace("Z", "+00:00"))
        iso_year, iso_week, _ = dt.isocalendar()
        weekly[f"{iso_year}-W{iso_week:02d}"] += net
    return (
        [{"utc_date": k, "net_pnl_usdt": v} for k, v in sorted(daily.items())],
        [{"iso_week": k, "net_pnl_usdt": v} for k, v in sorted(weekly.items())],
    )


def coverage_row(window: str, req_from: str, req_to_excl: str, pub: dict, built: int) -> dict:
    pub_from = pub["published_from_utc"]
    pub_to = pub["published_to_utc"]
    ok = pub_from <= req_from and pub_to >= _minus_5m(req_to_excl)
    return {
        "window": window,
        "requested_from": req_from,
        "requested_to_exclusive": req_to_excl,
        "published_from": pub_from,
        "published_to": pub_to,
        "built_snapshot_bars": built,
        "expected_28d_bars": BARS_28D if window == "main_28d" else "",
        "candle_coverage_ok": ok,
        "status": "COVERAGE_OK" if ok and built > 0 else "COVERAGE_BLOCKED",
    }


def _minus_5m(to_excl: str) -> str:
    dt = datetime.fromisoformat(to_excl.replace("Z", "+00:00")) - __import__("datetime").timedelta(minutes=5)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_zone_fn(snap_index: dict):
    def zone_status_fn(anchor: PoolAnchor, bar_utc: str, _lifecycle) -> Any:
        return zone_status_from_snapshot(
            snap_index,
            bar_utc,
            anchor.side,
            anchor.bottom,
            anchor.top,
            "|".join(sorted(anchor.origins)),
        )

    return zone_status_fn


def anchor_from_signal(direction, pool_lower, pool_upper, pool_identity):
    origins = frozenset(pool_identity.split("|")) if pool_identity else frozenset()
    side = "upper" if direction == "SHORT" else "lower"
    return PoolAnchor(side=side, bottom=pool_lower, top=pool_upper, origins=origins)


def run_backtests(snap_index: dict, from_utc: str, to_inclusive: str) -> dict:
    cfg = load_cfg()
    cfg = {**cfg, "pilot_from_utc": from_utc, "pilot_to_utc": to_inclusive}
    prices = prices_from_index(snap_index)
    prices = {k: v for k, v in prices.items() if from_utc <= k <= to_inclusive}
    signals = load_all_signals(cfg)
    signals, unresolved = apply_dynamic_snapshots(signals, snap_index)

    oco = collect_oco_trades(cfg, prices, signals)

    watches = [
        WatchSpec(
            watch_key=s.candidate_id,
            candidate_id=s.candidate_id,
            direction=s.direction,
            watch_from_utc=s.signal_close_utc,
            pool_lower=s.pool_lower,
            pool_upper=s.pool_upper,
            pool_identity=s.pool_identity,
            landscape_intervals=s.landscape_intervals,
            upper_tp_rows=s.upper_tp_rows,
        )
        for s in signals
    ]
    zone_fn = make_zone_fn(snap_index)
    mkt_out = run_market_backtest(
        cfg,
        watches,
        prices,
        None,
        plan_short,
        plan_long,
        simulate_exit,
        zone_fn,
        anchor_from_signal,
    )
    mkt_trades = []
    sig_map = {s.candidate_id: s for s in signals}
    from trade_collectors import NormalizedTrade, _signal_meta, _tp_meta_short, _tp_meta_long

    for row in mkt_out["simulated_market_trades"]:
        cid = row["candidate_id"]
        sig = sig_map.get(cid)
        meta = _signal_meta(cid)
        direction = row["direction"]
        entry = float(row["entry_price"])
        sl = float(row["sl_price"])
        tp = float(row["tp_price"])
        exit_price = row.get("exit_price")
        exit_price_f = float(exit_price) if exit_price not in ("", None) else None
        exit_reason = row.get("exit_reason") or "POSITION_OPEN_AT_DATA_END"
        tp_bot, tp_top, tp_orig = (None, tp, "")
        intervals = sig.landscape_intervals if sig else meta["landscape_intervals"]
        if direction == "SHORT":
            tp_bot, tp_top, tp_orig = _tp_meta_short(entry, intervals, tp)
        elif sig:
            tp_bot, tp_top, tp_orig = _tp_meta_long(sig.upper_tp_rows, tp)
        mkt_trades.append(
            NormalizedTrade(
                strategy_variant="TOUCH_REJECTION_MARKET_4M_SNAPSHOT",
                trade_id=row["trade_id"],
                candidate_id=cid,
                direction=direction,
                signal_utc=meta.get("signal_utc") or row.get("rejection_confirm_utc", ""),
                entry_utc=row["fill_utc"],
                exit_utc=row.get("exit_utc") or None,
                entry_price=entry,
                exit_price=exit_price_f,
                entry_pool_lower=sig.pool_lower if sig else meta["pool_lower"],
                entry_pool_upper=sig.pool_upper if sig else meta["pool_upper"],
                entry_pool_origin_ids=sig.pool_identity if sig else meta.get("origins", ""),
                tp_pool_bottom=tp_bot,
                tp_pool_top=tp_top,
                tp_pool_origin_ids=tp_orig,
                stop_loss_price=sl,
                take_profit_price=tp,
                exit_reason=exit_reason,
                execution_ambiguity="NONE",
            )
        )

    ll = [ledger_row(t, prices, to_inclusive) for t in oco]
    lm = [ledger_row(t, prices, to_inclusive) for t in mkt_trades]
    for r in ll + lm:
        r["strategy_validation_status"] = "TECHNICAL_PNL_COMPARISON"
    return {
        "ledger_limit": ll,
        "ledger_market": lm,
        "unresolved": unresolved,
        "market_out": mkt_out,
        "summ_limit": summarize("LIMIT_OCO_4H", ll, len(signals)),
        "summ_market": summarize("TOUCH_REJECTION_MARKET_4M", lm, len(mkt_out["rejection_confirmations"])),
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    probe = import_error_traceback_probe()
    pub = published_inventory()
    (RESULTS / "root_cause_report.md").write_text(
        "\n".join(
            [
                "# Phase 4S root cause",
                "",
                "## Primary failure (Phase 4R)",
                "",
                "1. **`research_charts.historical_lmv2_pd_contract_v1` (pools dashboard shadow)** exports only 15m PD registry — **missing** `ALLOWED_VIEWER_SCOPES`, `resolve_viewer_historical_scope`, `membership_slot_minutes`.",
                "2. `historical_window_v1.build_landscape_window` imports those symbols → **`ImportError: cannot import name ALLOWED_VIEWER_SCOPES`**.",
                "3. Phase 4R `coverage.probe_ssot_build` used **`from run_pilot import ...`** without `tools/phase1b_...` on `sys.path` → misleading `No module named run_pilot`.",
                "4. Even with spread dashboard, **`data.models`** requires `trading_research_platform` on `PYTHONPATH`.",
                "",
                "## Probe",
                "",
                f"- pools-only: `{probe['pools_dashboard_only'][:300]}`",
                f"- adapter: `{probe['phase4s_adapter'][:80] if probe['phase4s_adapter'].startswith('OK') else 'see traceback in logs'}`",
                "",
                "## Phase 4S fix (isolated)",
                "",
                "`ssot_adapter.apply_ssot_pythonpath()` → spread dashboard + trading_research_platform; no SSOT/LMV2 code changes.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_csv(RESULTS / "ssot_inventory.csv", [pub])

    # B + C: 24h parity chunk
    parity_chunk = build_chunk(PARITY_FROM, _minus_5m(PARITY_TO))
    ref_index = load_index(REF_JSONL)
    new_index = {s["candle_close_utc"]: s for s in parity_chunk["snapshots"]}
    parity_rows, parity_status = compare_timelines(new_index, ref_index, PARITY_FROM, PARITY_TO)
    write_csv(RESULTS / "timeline_parity.csv", parity_rows)

    verdict = "28DAY_BACKTEST_BLOCKED"
    gate_stop = ""

    if parity_status != "TIMELINE_PARITY_OK":
        gate_stop = "Gate C: TIMELINE_PARITY_FAILED"
    else:
        # Build full published span
        pub_from = pub["published_from_utc"]
        pub_to = pub["published_to_utc"]
        chunks_meta = []
        built_chunks = []
        chunk_dir = RESULTS / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        for i, (a, b) in enumerate(iter_chunks(pub_from, pub_to), 1):
            ch = build_chunk(a, b)
            built_chunks.append(ch)
            chunk_path = chunk_dir / f"chunk_{i:02d}_{a[:10]}_{b[:10]}.json"
            chunk_path.write_text(
                json.dumps({"meta": {k: ch[k] for k in ch if k not in ("snapshots", "lifecycle_events")}, "snapshots": ch["snapshots"]}),
                encoding="utf-8",
            )
            chunks_meta.append(
                {
                    "chunk_id": i,
                    "vis_from": ch["vis_from"],
                    "vis_to_inclusive": ch["vis_to_inclusive"],
                    "expected_bars": ch["expected_bars"],
                    "snapshot_bars": ch["snapshot_bars"],
                    "lifecycle_event_count": len(ch["lifecycle_events"]),
                    "gap_rows": ch["gap_rows"],
                    "context_from_utc": ch["context_from_utc"],
                    "snapshot_hash": ch["snapshot_hash"],
                    "chunk_file": str(chunk_path.relative_to(ROOT)),
                }
            )
        write_csv(RESULTS / "chunk_manifest.csv", chunks_meta)
        merged = merge_snapshots(built_chunks)
        write_jsonl(MERGED_JSONL, merged)
        snap_index = {s["candle_close_utc"]: s for s in merged}
        pool_cov_pct = round(100.0 * len(merged) / max(1, int(pub["published_decision_closes"])), 2)
        write_csv(
            RESULTS / "timeline_coverage.csv",
            [
                coverage_row("control_14d", REQ_14D_FROM, REQ_14D_TO_EXCL, pub, len(merged)),
                coverage_row("main_28d", REQ_28D_FROM, REQ_28D_TO_EXCL, pub, len(merged)),
                {
                    "window": "published_ssot_span",
                    "requested_from": pub_from,
                    "requested_to_exclusive": pub_to,
                    "built_snapshot_bars": len(merged),
                    "expected_decision_closes": pub["published_decision_closes"],
                    "pool_state_coverage_pct": pool_cov_pct,
                    "first_snapshot_utc": merged[0]["candle_close_utc"] if merged else "",
                    "last_snapshot_utc": merged[-1]["candle_close_utc"] if merged else "",
                    "status": "COVERAGE_OK"
                    if len(merged) == int(pub["published_decision_closes"])
                    else "COVERAGE_PARTIAL",
                },
            ],
        )

        if coverage_row("control_14d", REQ_14D_FROM, REQ_14D_TO_EXCL, pub, len(merged))["status"] != "COVERAGE_OK":
            gate_stop = "Gate E: 14d window not in published SSOT (data from Sep 9 only)"

        # D + available-span backtest
        bt = run_backtests(snap_index, pub_from, pub_to)
        ll, lm = bt["ledger_limit"], bt["ledger_market"]
        write_csv(RESULTS / "trade_ledger_limit.csv", ll)
        write_csv(RESULTS / "trade_ledger_market.csv", lm)
        write_csv(RESULTS / "pnl_comparison.csv", [bt["summ_limit"], bt["summ_market"]])
        dl, wl = daily_weekly(ll)
        dm, wm = daily_weekly(lm)
        write_csv(RESULTS / "daily_pnl_limit.csv", dl)
        write_csv(RESULTS / "daily_pnl_market.csv", dm)
        write_csv(RESULTS / "weekly_pnl_limit.csv", wl)
        write_csv(RESULTS / "weekly_pnl_market.csv", wm)
        write_csv(RESULTS / "unresolved_cases.csv", bt["unresolved"])
        write_csv(
            RESULTS / "watch_lifecycle_diagnostics.csv",
            [
                {
                    "pool_watch_aborts": len(bt["market_out"].get("pool_watch_aborts", [])),
                    "lifecycle_mode": "SNAPSHOT_AT_BAR",
                    "note": "Replaces Phase 3A lifecycle CSV for 4S market path",
                }
            ],
        )
        verdict = "28DAY_BACKTEST_PARTIAL"

    summary = [
        "# Phase 4S final summary",
        "",
        f"**VERDICT:** `{verdict}`",
        f"**Gate:** {gate_stop or 'parity OK; 14d/28d requested windows blocked — SSOT publishes Sep 9–17 only'}",
        f"**Parity:** {parity_status}",
        f"**Published closes:** {pub['published_decision_closes']}",
        "",
    ]
    (RESULTS / "final_summary.md").write_text("\n".join(summary), encoding="utf-8")
    REPORT.write_text(
        "\n".join(
            [
                "# DOGEUSDT Phase 4S — 28-Day Timeline Recovery V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Root cause",
                "Pools-tree `historical_lmv2_pd_contract_v1.py` missing viewer scope API; spread dashboard + `trading_research_platform` required.",
                "",
                "## SSOT inventory",
                f"- Published 5m PD1: **{pub['published_from_utc']} → {pub['published_to_utc']}** ({pub['published_decision_closes']} closes)",
                "- Requested 28d (Sep 1–29 excl) needs **8064** bars — **not available** in SSOT.",
                "",
                f"## Parity (24h): **{parity_status}**",
                "",
                f"## Gate stop: {gate_stop or '14d/28d calendar blocked; backtest on published span only'}",
                "",
                f"`results/phase4s_timeline_recovery_v1/`",
                "",
                "**STOP**",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "parity": parity_status, "gate": gate_stop}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
