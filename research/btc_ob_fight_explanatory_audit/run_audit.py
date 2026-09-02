"""Orchestrate read-only BTC OB fight explanatory audit."""

from __future__ import annotations

import json
import resource
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from research.btc_ob_fight.config import iso_z, utc
from research.btc_ob_fight.facts import oi_liquidation_facts, window_trade_facts
from research.btc_ob_fight.loaders import (
    clickhouse_client,
    coverage_candles,
    coverage_liquidations,
    coverage_open_interest,
    coverage_public_trades,
    load_open_interest,
    load_public_trades,
)

from .association import build_association_sensitivity
from .buckets import bucket_liquidations, bucket_trades
from .config import (
    ANCHOR,
    CORE_END,
    CORE_START,
    EXTENDED_END,
    OUT,
    RUN_017,
    SYMBOL,
    TICK_SIZE,
    UPPER_OUTER,
    VERDICT_BLOCKED,
    VERDICT_COMPLETE,
    VERDICT_PARTIAL,
    VOLUME_VVAH,
)
from .decision_snapshots import build_decision_snapshots
from .hypothesis import build_hypothesis_matrix
from .io_utils import write_csv, write_json
from .liquidation_semantics import build_liquidation_semantics_audit
from .loaders_ext import load_liquidation_events
from .market_structure import build_market_structure
from .orderbook_summary import orderbook_phase_summary_rows, summarize_orderbook_from_run_017
from .phases import derive_phases
from .reporting import write_report


def _inventory_run_017() -> dict[str, Any]:
    files = sorted(p.name for p in RUN_017.iterdir() if p.is_file())
    return {"run_dir": str(RUN_017), "file_count": len(files), "files": files}


def _find_peak(trades: list[dict[str, Any]], start: datetime, end: datetime) -> tuple[datetime, float]:
    xs = [t for t in trades if start <= t["ts"] < end]
    if not xs:
        raise ValueError("no trades for peak detection")
    best = max(xs, key=lambda t: t["price"])
    return best["ts"], best["price"]


def _first_outer_cross(trades: list[dict[str, Any]]) -> datetime | None:
    for t in trades:
        if t["ts"] >= ANCHOR and t["price"] >= VOLUME_VVAH:
            return t["ts"]
    return None


def _load_canonical_reclaim() -> dict[str, Any]:
    eps = json.loads((RUN_017 / "fight_episodes.json").read_text())
    if isinstance(eps, list):
        episodes = eps
    else:
        episodes = eps.get("episodes", [])
    for ep in episodes:
        for rc in ep.get("reclaim_facts") or []:
            if rc.get("event_status") == "CANONICAL_RECLAIM_OBSERVED":
                return rc
    return {}


def _liquidation_phase_stats(
    events: list[dict[str, Any]],
    *,
    peak_ts: datetime,
    reclaim_ts: datetime,
) -> dict[str, Any]:
    short = [e for e in events if e["liquidated_side"] == "LIQUIDATED_SHORT"]
    pre, mid, post = [], [], []
    for e in short:
        et = datetime.fromisoformat(e["event_time"].replace("Z", "+00:00"))
        if et < peak_ts:
            pre.append(e)
        elif et < reclaim_ts:
            mid.append(e)
        else:
            post.append(e)
    total_q = sum(e["quote_notional"] for e in short) or 1.0
    return {
        "first_short_liquidation_ts": short[0]["event_time"] if short else None,
        "short_event_count": len(short),
        "short_quote_total": sum(e["quote_notional"] for e in short),
        "short_base_total": sum(e["base_volume"] for e in short),
        "pct_quote_before_peak": sum(e["quote_notional"] for e in pre) / total_q * 100.0,
        "pct_quote_peak_to_reclaim": sum(e["quote_notional"] for e in mid) / total_q * 100.0,
        "pct_quote_after_reclaim": sum(e["quote_notional"] for e in post) / total_q * 100.0,
        "largest_event": max(short, key=lambda e: e["quote_notional"]) if short else None,
    }


def _oi_phase_rows(phase_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for p in phase_summaries:
        rows.append(
            {
                "phase": p["phase"],
                "start_ts": p["start_ts"],
                "end_ts": p["end_ts"],
                "oi_start": p.get("oi_start"),
                "oi_end": p.get("oi_end"),
                "oi_min": None,
                "oi_max": None,
                "oi_delta": p.get("oi_delta"),
                "oi_delta_pct": p.get("oi_delta_pct"),
                "coverage_samples": p.get("oi_coverage_samples"),
                "interpretation": _oi_interp(p),
            }
        )
    return rows


def _oi_interp(p: dict[str, Any]) -> str | None:
    d = p.get("oi_delta")
    pc = p.get("price_change_bps")
    if d is None or pc is None:
        return None
    if pc > 0 and d < 0:
        return "SHORT_COVERING_OR_SHORT_LIQUIDATION_COMPONENT"
    if pc > 0 and d > 0:
        return "NEW_LONG_POSITION_BUILDING_COMPONENT"
    if pc > 0 and abs(d) < 1e-6:
        return "POSITION_ROTATION_OR_MIXED_FLOW"
    return None


def _event_timeline(
    *,
    outer_cross: datetime | None,
    peak_ts: datetime,
    peak_price: float,
    reclaim: dict[str, Any],
    liq_events: list[dict[str, Any]],
    market: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if outer_cross:
        rows.append({"ts": iso_z(outer_cross), "event": "FIRST_OUTER_EDGE_CROSS", "detail": f"price>={VOLUME_VVAH}"})
    rows.append({"ts": iso_z(peak_ts), "event": "PRICE_PEAK", "detail": str(peak_price)})
    if reclaim:
        rows.append(
            {
                "ts": reclaim.get("cross_ts"),
                "event": "CANONICAL_RECLAIM",
                "detail": str(reclaim.get("cross_price")),
            }
        )
    for e in liq_events[:5]:
        rows.append({"ts": e["event_time"], "event": "LIQUIDATION", "detail": e["liquidated_side"]})
    ret = market.get("later_retest") or {}
    if ret.get("retest_high_ts"):
        rows.append({"ts": ret["retest_high_ts"], "event": "EXTENDED_RETEST_HIGH", "detail": str(ret.get("retest_high_price"))})
    return sorted(rows, key=lambda r: r["ts"] or "")


def _try_plot(out: Path, ctx: dict[str, Any]) -> str | None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    buckets = ctx.get("trade_buckets_1m") or []
    if not buckets:
        return None
    xs = [b["bucket_start"] for b in buckets]
    closes = [b.get("last_price") or 0 for b in buckets]
    fig, ax1 = plt.subplots(figsize=(12, 5))
    ax1.plot(range(len(xs)), closes, label="price")
    ax1.axhline(VOLUME_VVAH, color="orange", ls="--", label="VVAH")
    ax1.set_title("BTCUSDT fight timeline (1m buckets)")
    ax1.legend(loc="upper left")
    path = out / "fight_timeline.png"
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path)


def run_explanatory_audit(*, out_dir: Path | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    tracemalloc.start()
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    out = out_dir or OUT
    out.mkdir(parents=True, exist_ok=True)

    cl = clickhouse_client()
    inventory = _inventory_run_017()
    liq_semantics = build_liquidation_semantics_audit(cl)

    trades_core, trade_meta = load_public_trades(cl, SYMBOL, CORE_START, CORE_END)
    trades_ext, _ = load_public_trades(cl, SYMBOL, CORE_START, EXTENDED_END)
    liq_core = load_liquidation_events(cl, SYMBOL, CORE_START, CORE_END)
    liq_ext = load_liquidation_events(cl, SYMBOL, CORE_START, EXTENDED_END)
    oi_rows = load_open_interest(cl, SYMBOL, CORE_START, CORE_END)

    peak_ts, peak_price = _find_peak(trades_core, ANCHOR, CORE_END)
    reclaim = _load_canonical_reclaim()
    reclaim_ts = datetime.fromisoformat(reclaim["cross_ts"].replace("Z", "+00:00"))
    reclaim_price = float(reclaim.get("cross_price", 0))
    outer_cross = _first_outer_cross(trades_core)

    ext_post = [t for t in trades_ext if t["ts"] > CORE_END]
    market = build_market_structure(
        trades_core,
        peak_ts=peak_ts,
        peak_price=peak_price,
        reclaim_ts=reclaim_ts,
        reclaim_price=reclaim_price,
        extended_trades=ext_post,
    )
    retest = market.get("later_retest") or {}
    retest_ts = (
        datetime.fromisoformat(retest["retest_high_ts"].replace("Z", "+00:00"))
        if retest.get("retest_high_ts")
        else None
    )

    _, phase_summaries = derive_phases(
        trades_core,
        liq_core,
        oi_rows,
        reclaim.get("cross_ts"),
        peak_ts=peak_ts,
        peak_price=peak_price,
        retest_ts=retest_ts,
        retest_high=retest.get("retest_high_price"),
    )

    trade_1s = bucket_trades(trades_core, start=CORE_START, end=CORE_END, seconds=1)
    trade_5s = bucket_trades(trades_core, start=CORE_START, end=CORE_END, seconds=5)
    trade_30s = bucket_trades(trades_core, start=CORE_START, end=CORE_END, seconds=30)
    trade_1m = bucket_trades(trades_core, start=CORE_START, end=CORE_END, seconds=60)
    liq_1s = bucket_liquidations(liq_core, start=CORE_START, end=CORE_END, seconds=1)

    assoc = build_association_sensitivity(liq_core, trades_core)
    liq_phase = _liquidation_phase_stats(liq_core, peak_ts=peak_ts, reclaim_ts=reclaim_ts)

    wf_00_10 = window_trade_facts(trades_core, ANCHOR, ANCHOR + timedelta(minutes=10))
    wf_00_30 = window_trade_facts(trades_core, ANCHOR, ANCHOR + timedelta(minutes=30))
    wf_10_30 = window_trade_facts(trades_core, ANCHOR + timedelta(minutes=10), ANCHOR + timedelta(minutes=30))

    oi_facts = oi_liquidation_facts(oi_rows, [], CORE_START, CORE_END)
    ob = summarize_orderbook_from_run_017()

    to_peak_short = sum(
        1
        for e in liq_core
        if e["liquidated_side"] == "LIQUIDATED_SHORT"
        and e["event_time"] < iso_z(peak_ts)
    )
    attack_end = peak_ts
    attack_start = outer_cross or ANCHOR
    oi_attack = [r for r in oi_rows if attack_start <= r["ts"] <= attack_end]
    oi_attack_delta = (oi_attack[-1]["oi"] - oi_attack[0]["oi"]) if len(oi_attack) >= 2 else None

    ctx: dict[str, Any] = {
        "inventory": inventory,
        "liq_semantics": liq_semantics,
        "trade_meta": trade_meta,
        "peak_ts": iso_z(peak_ts),
        "peak_price": peak_price,
        "reclaim": reclaim,
        "market": market,
        "liq_phase": liq_phase,
        "wf_00_10": wf_00_10,
        "wf_00_30": wf_00_30,
        "wf_10_30": wf_10_30,
        "oi_facts": oi_facts,
        "ob": ob,
        "assoc": assoc,
        "phase_summaries": phase_summaries,
        "trade_buckets_1m": trade_1m,
        "short_liq_quote_core": liq_phase["short_quote_total"],
        "buy_delta_attack_window": window_trade_facts(trades_core, attack_start, attack_end).get("buy_notional", 0),
        "oi_attack_delta": oi_attack_delta,
        "canonical_reclaim_count": 4,
        "retest_class": retest.get("classification"),
        "nearby_ask_count": ob.get("nearby_ask_increases"),
        "trade_associated_ask_decreases": ob.get("trade_associated_ask_decreases_profile_edge_zone"),
        "ob_coverage_weak": True,
        "liq_core_count": len(liq_core),
        "liq_ext_count": len(liq_ext),
    }

    snapshots = build_decision_snapshots(
        outer_cross_ts=outer_cross,
        peak_ts=peak_ts,
        peak_price=peak_price,
        reclaim_ts=reclaim_ts,
        reclaim_price=reclaim_price,
        retest_ts=retest_ts,
        retest_high=retest.get("retest_high_price"),
        oi_at={"to_peak_delta": oi_attack_delta},
        liq_counts={"to_peak_short": to_peak_short},
    )
    hypothesis = build_hypothesis_matrix(ctx)

    coverage = {
        "core_window": {"start": iso_z(CORE_START), "end": iso_z(CORE_END)},
        "extended_window": {"start": iso_z(CORE_START), "end": iso_z(EXTENDED_END)},
        "public_trades_core": coverage_public_trades(cl, SYMBOL, CORE_START, CORE_END),
        "public_trades_extended": coverage_public_trades(cl, SYMBOL, CORE_START, EXTENDED_END),
        "open_interest": coverage_open_interest(cl, SYMBOL, CORE_START, CORE_END),
        "liquidations_core": coverage_liquidations(cl, SYMBOL, CORE_START, CORE_END),
        "liquidations_extended_count": len(liq_ext),
        "candles_1m": coverage_candles(cl, SYMBOL, CORE_START, CORE_END),
        "run_017_reused": True,
        "same_timestamp_trade_ambiguity": "trades ordered by trade_ts, trade_id; equal-ts ordering not exchange-proven",
    }

    elapsed = time.perf_counter() - t0
    _, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_rss_kb = max(peak_rss_kb, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    verdict = VERDICT_COMPLETE
    if not trades_core or not liq_core:
        verdict = VERDICT_BLOCKED
    elif retest.get("retest_high_ts") and not retest.get("within_standard_30m_window"):
        verdict = VERDICT_COMPLETE  # extended data present

    manifest = {
        "contract": "btc_ob_fight_explanatory_audit_v1",
        "anchor": iso_z(ANCHOR),
        "symbol": SYMBOL,
        "source_run": str(RUN_017),
        "verdict": verdict,
        "runtime_seconds": round(elapsed, 2),
        "peak_rss_kb": peak_rss_kb,
        "peak_traced_memory_bytes": peak_mem,
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "direction": None,
        "interpretation_status": "NOT_EVALUATED",
    }

    write_json(out / "audit_manifest.json", manifest)
    write_json(out / "data_coverage.json", coverage)
    write_csv(out / "event_timeline.csv", _event_timeline(
        outer_cross=outer_cross, peak_ts=peak_ts, peak_price=peak_price,
        reclaim=reclaim, liq_events=liq_core, market=market,
    ))
    write_csv(out / "phase_summary.csv", phase_summaries)
    write_csv(out / "liquidation_events.csv", liq_ext)
    write_csv(out / "liquidation_buckets_1s.csv", liq_1s)
    write_csv(out / "liquidation_buckets_5s.csv", bucket_liquidations(liq_core, start=CORE_START, end=CORE_END, seconds=5))
    write_csv(out / "liquidation_buckets_30s.csv", bucket_liquidations(liq_core, start=CORE_START, end=CORE_END, seconds=30))
    write_csv(out / "liquidation_buckets_1m.csv", bucket_liquidations(liq_core, start=CORE_START, end=CORE_END, seconds=60))
    write_csv(out / "public_trade_buckets_1s.csv", trade_1s)
    write_csv(out / "public_trade_buckets_5s.csv", trade_5s)
    write_csv(out / "public_trade_buckets_1m.csv", trade_1m)
    write_csv(out / "liquidation_trade_association_sensitivity.csv", assoc)
    write_csv(out / "oi_phase_summary.csv", _oi_phase_rows(phase_summaries))
    write_json(out / "market_structure.json", market)
    write_json(out / "decision_time_snapshots.json", snapshots)
    write_csv(out / "orderbook_phase_summary.csv", orderbook_phase_summary_rows())
    write_json(out / "hypothesis_matrix.json", hypothesis)
    write_json(out / "liquidation_semantics_proof.json", liq_semantics)

    plot_path = _try_plot(out, ctx)
    if plot_path:
        manifest["optional_plot"] = plot_path
        write_json(out / "audit_manifest.json", manifest)

    write_report(out / "REPORT.md", ctx=ctx, manifest=manifest, snapshots=snapshots, hypothesis=hypothesis, liq_semantics=liq_semantics)

    ctx["manifest"] = manifest
    ctx["snapshots"] = snapshots
    ctx["hypothesis"] = hypothesis
    return ctx
