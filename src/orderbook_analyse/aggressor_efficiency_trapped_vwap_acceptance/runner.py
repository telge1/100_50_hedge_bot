"""Smoke runner — small BTC/DOGE windows only; reuses AEF discovery."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets
from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.episodes import discover_episodes
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import (
    TrapAcceptConfig,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    input_from_aef_compression,
    synthetic_event,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import (
    json_safe,
    prefix_snapshot,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.pipeline import process_event
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    try_write_parquet,
    write_csv,
    write_json,
)


DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "aggressor_efficiency_trapped_vwap_acceptance_v1"
)

# Small smoke windows (UTC)
SMOKE_WINDOWS = [
    ("DOGEUSDT", "2026-08-29T11:50:00Z", "2026-08-29T12:30:00Z"),
    ("BTCUSDT", "2026-08-29T11:50:00Z", "2026-08-29T12:30:00Z"),
]


def _build_synthetic_references(base: datetime) -> list:
    """Six semantic fixtures with known edges / gaps for reference audit."""
    T = lambda s: base + timedelta(seconds=s)
    return [
        synthetic_event(
            event_id="REF_INEFFICIENT_BUY_ASK",
            symbol="SYN",
            direction="SHORT",
            wall_side="ASK",
            edge_price=100.0,
            flow_start_ts=T(0),
            flow_end_ts=T(5),
            decision_ts=T(10),
            reference_price=100.0,
            meta={"expected": "inefficient buy at ask; may trap/reclaim"},
        ),
        synthetic_event(
            event_id="REF_EFFICIENT_BUY_BREAK",
            symbol="SYN",
            direction="SHORT",
            wall_side="ASK",
            edge_price=100.0,
            flow_start_ts=T(100),
            flow_end_ts=T(105),
            decision_ts=T(110),
            meta={"expected": "efficient buy break above ask"},
        ),
        synthetic_event(
            event_id="REF_INEFFICIENT_SELL_BID",
            symbol="SYN",
            direction="LONG",
            wall_side="BID",
            edge_price=100.0,
            flow_start_ts=T(200),
            flow_end_ts=T(205),
            decision_ts=T(210),
            meta={"expected": "inefficient sell at bid"},
        ),
        synthetic_event(
            event_id="REF_BREAK_RECLAIM",
            symbol="SYN",
            direction="SHORT",
            wall_side="ASK",
            edge_price=100.0,
            flow_start_ts=T(300),
            flow_end_ts=T(305),
            decision_ts=T(310),
            meta={"expected": "brief break then reclaim"},
        ),
        synthetic_event(
            event_id="REF_NO_EDGE",
            symbol="SYN",
            direction="LONG",
            wall_side="BID",
            edge_price=None,
            edge_source="none",
            edge_confidence="none",
            flow_start_ts=T(400),
            flow_end_ts=T(405),
            decision_ts=T(410),
            meta={"expected": "UNKNOWN_EDGE"},
        ),
        synthetic_event(
            event_id="REF_DATA_GAP",
            symbol="SYN",
            direction="LONG",
            wall_side="BID",
            edge_price=99.0,
            flow_start_ts=T(500),
            flow_end_ts=T(505),
            decision_ts=T(510),
            meta={"expected": "UNKNOWN_DATA if no trades"},
        ),
    ]


def _make_syn_trades() -> list[Trade]:
    """Deterministic synthetic trades covering reference fixtures."""
    base = parse_utc("2026-08-29T10:00:00Z")
    trades: list[Trade] = []

    def add(sec: float, side: str, px: float, n: float, tid: str, ms: int = 0) -> None:
        ts = base + timedelta(seconds=sec, milliseconds=ms)
        size = n / px if px else 0
        trades.append(Trade(trade_ts=ts, trade_id=tid, side=side, price=px, size=size, notional=n))

    # REF_INEFFICIENT_BUY_ASK: buys at 100 with no up move, then price sinks → trap
    for i in range(5):
        add(i, "Buy", 100.0, 5000, f"ib{i}")
    for i in range(10, 40):
        add(i, "Sell", 99.8, 200, f"ibs{i}")

    # REF_EFFICIENT_BUY_BREAK: buys push through 100 → hold above
    for i in range(100, 105):
        add(i, "Buy", 100.0 + (i - 100) * 0.02, 8000, f"eb{i}")
    for i in range(110, 180):
        add(i, "Buy", 100.15, 500, f"ebh{i}")

    # REF_INEFFICIENT_SELL_BID: sells at 100 flat then rise → sellers trapped
    for i in range(200, 205):
        add(i, "Sell", 100.0, 5000, f"is{i}")
    for i in range(210, 250):
        add(i, "Buy", 100.2, 300, f"isu{i}")

    # REF_BREAK_RECLAIM: brief poke above 100 then back below
    for i in range(300, 305):
        add(i, "Buy", 100.05, 3000, f"br{i}")
    add(311, "Buy", 100.12, 1000, "brk")
    for i in range(312, 340):
        add(i, "Sell", 99.95, 400, f"brr{i}")

    # REF_NO_EDGE: some sell compression flow
    for i in range(400, 405):
        add(i, "Sell", 100.0, 4000, f"ne{i}")
    for i in range(410, 440):
        add(i, "Buy", 100.0, 100, f"nex{i}")

    # REF_DATA_GAP: flow only, no post prices intentionally sparse after 510
    for i in range(500, 505):
        add(i, "Sell", 99.0, 2000, f"dg{i}")
    # gap: no trades 505-600

    return trades


def run_smoke(
    *,
    output_dir: Path = DEFAULT_OUT,
    skip_ch: bool = False,
) -> dict[str, Any]:
    t_wall0 = time.perf_counter()
    cfg = TrapAcceptConfig()
    ensure_outdir(output_dir)
    query_log: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    missing_edge: list[dict[str, Any]] = []
    prefix_audit: dict[str, Any] = {"ok": True, "checks": []}

    # --- Synthetic references first (always) ---
    syn_trades = _make_syn_trades()
    syn_buckets = build_second_buckets(syn_trades)
    syn_base = parse_utc("2026-08-29T10:00:00Z")
    for ev in _build_synthetic_references(syn_base):
        inventory.append({**ev.to_dict(), "smoke_cohort": "synthetic_reference"})
        feat, outc = process_event(
            ev,
            buckets=syn_buckets,
            trades=syn_trades,
            cfg=cfg,
            data_end=syn_base + timedelta(seconds=700),
        )
        features.append(feat)
        outcomes.append(outc)
        timelines.append(
            {
                "event_id": ev.event_id,
                "expected": (ev.meta or {}).get("expected"),
                "edge_price": ev.edge_price,
                "flow_start_ts": feat.get("flow_start_ts"),
                "flow_end_ts": feat.get("flow_end_ts"),
                "aggressor_notional": feat.get("aggressor_notional"),
                "aggressor_vwap": feat.get("trap_vwap") or feat.get("aggressor_vwap"),
                "favorable_progress_bps": feat.get("favorable_progress_bps"),
                "post_signed_return_bps": feat.get("post_signed_return_bps"),
                "final_trap_label": feat.get("final_trap_label"),
                "final_acceptance_state": feat.get("final_acceptance_state"),
                "final_research_state": feat.get("final_research_state"),
                "explanation_codes": feat.get("explanation_codes"),
            }
        )
        if feat.get("final_acceptance_state") == "UNKNOWN_EDGE":
            missing_edge.append({"event_id": ev.event_id, "reason": "no_or_low_confidence_edge"})

        # prefix parity on SYN events with edge
        if ev.edge_price is not None and ev.event_id != "REF_DATA_GAP":
            for cp in (5, 10, 30, 60):
                cut = ev.decision_ts + timedelta(seconds=cp)
                f_full, _ = process_event(ev, buckets=syn_buckets, trades=syn_trades, cfg=cfg, data_end=syn_base + timedelta(seconds=700))
                f_pref, _ = process_event(ev, buckets=syn_buckets, trades=syn_trades, cfg=cfg, as_of=cut, data_end=cut)
                a = prefix_snapshot(f_full, cp)
                b = prefix_snapshot(f_pref, cp)
                # compare decision state and trap label at cp
                ok = (a.get("decision_state") == b.get("decision_state")) and (
                    (a.get("trap_cp") or {}).get("trap_label") == (b.get("trap_cp") or {}).get("trap_label")
                    or (b.get("trap_cp") or {}).get("status") == "UNKNOWN_DATA"
                )
                # acceptance state
                acc_a = (a.get("accept_cp") or {}).get("state")
                acc_b = (b.get("accept_cp") or {}).get("state")
                if acc_b not in {None, "UNKNOWN_DATA"}:
                    ok = ok and acc_a == acc_b
                prefix_audit["checks"].append(
                    {"event_id": ev.event_id, "checkpoint_s": cp, "ok": ok, "full": a, "prefix": b}
                )
                if not ok:
                    prefix_audit["ok"] = False

    # --- CH smoke: AEF compressions on small windows ---
    ch_events = 0
    if not skip_ch:
        for symbol, start_s, end_s in SMOKE_WINDOWS:
            start, end = parse_utc(start_s), parse_utc(end_s)
            trades, meta = load_trades_clickhouse(
                symbol=symbol, start=start, end=end, query_log=query_log
            )
            buckets = build_second_buckets(trades)
            aef_cfg = cfg.aef_config()
            disc = discover_episodes(
                symbol=symbol,
                trades=trades,
                buckets=buckets,
                start=start,
                end=end,
                cfg=aef_cfg,
            )
            allowed = [c for c in disc["compressions"] if c.get("allowed")]
            # limit smoke volume
            allowed = allowed[:25]
            for row in allowed:
                ev = input_from_aef_compression(row, source=f"aef_smoke:{symbol}")
                inventory.append({**ev.to_dict(), "smoke_cohort": "aef_compression_smoke"})
                feat, outc = process_event(
                    ev, buckets=buckets, trades=trades, cfg=cfg, data_end=end
                )
                features.append(feat)
                outcomes.append(outc)
                ch_events += 1
                if feat.get("final_acceptance_state") == "UNKNOWN_EDGE":
                    missing_edge.append(
                        {
                            "event_id": ev.event_id,
                            "symbol": symbol,
                            "reason": "aef_event_without_measured_pool_edge",
                            "edge_source": ev.edge_source,
                        }
                    )
                timelines.append(
                    {
                        "event_id": ev.event_id,
                        "symbol": symbol,
                        "direction": ev.direction,
                        "edge_price": None,
                        "flow_start_ts": feat.get("flow_start_ts"),
                        "aggressor_notional": feat.get("aggressor_notional"),
                        "favorable_progress_bps": feat.get("favorable_progress_bps"),
                        "final_trap_label": feat.get("final_trap_label"),
                        "final_acceptance_state": feat.get("final_acceptance_state"),
                        "final_research_state": feat.get("final_research_state"),
                    }
                )

    # --- Summaries ---
    state_counts = Counter(f.get("final_research_state") for f in features)
    trap_counts = Counter(f.get("final_trap_label") for f in features)
    acc_counts = Counter(f.get("final_acceptance_state") for f in features)
    by_sym = Counter(f.get("symbol") for f in features)

    # comparison cohorts (descriptive, SMALL_N aware)
    def cohort(name: str, pred) -> dict[str, Any]:
        ids = {f["event_id"] for f in features if pred(f)}
        outs = [o for o in outcomes if o["event_id"] in ids and o.get("outcome_status") == "OK"]
        rets = [o.get("h300s_signed_return_bps") for o in outs if o.get("h300s_available")]
        rets = [r for r in rets if r is not None]
        n = len(rets)
        med = sorted(rets)[n // 2] if n else None
        mean = sum(rets) / n if n else None
        return {
            "cohort": name,
            "n_events": len(ids),
            "n_with_5m_outcome": n,
            "median_5m_signed_bps": med,
            "mean_5m_signed_bps": mean,
            "small_n": n < 20,
        }

    horizon_rows = [
        cohort("efficiency_compression_only", lambda f: f.get("compression_flag") is True),
        cohort(
            "efficiency_plus_trap",
            lambda f: f.get("compression_flag") is True and f.get("final_trap_label") == "TRAP_CONFIRMED",
        ),
        cohort(
            "efficiency_plus_acceptance",
            lambda f: f.get("final_acceptance_state") in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"},
        ),
        cohort(
            "efficiency_trap_acceptance",
            lambda f: f.get("final_research_state") in {"ATTACKER_WINNING", "ATTACKER_TRAPPED_REJECTION"},
        ),
        cohort("attacker_trapped_rejection", lambda f: f.get("final_research_state") == "ATTACKER_TRAPPED_REJECTION"),
        cohort("attacker_winning", lambda f: f.get("final_research_state") == "ATTACKER_WINNING"),
    ]

    symbol_side = []
    for f in features:
        symbol_side.append(
            {
                "symbol": f.get("symbol"),
                "direction": f.get("direction"),
                "wall_side": f.get("wall_side"),
                "final_research_state": f.get("final_research_state"),
                "final_trap_label": f.get("final_trap_label"),
                "final_acceptance_state": f.get("final_acceptance_state"),
            }
        )

    write_csv(output_dir / "event_inventory.csv", inventory)
    write_csv(output_dir / "features_decisions.csv", features)
    write_csv(output_dir / "forward_outcomes.csv", outcomes)
    try_write_parquet(output_dir / "features_decisions.parquet", features)
    try_write_parquet(output_dir / "forward_outcomes.parquet", outcomes)
    write_csv(
        output_dir / "combined_state_summary.csv",
        [{"state": k, "n": v} for k, v in state_counts.items()],
    )
    write_csv(output_dir / "horizon_analysis.csv", horizon_rows)
    write_csv(output_dir / "symbol_side_analysis.csv", symbol_side)
    write_csv(output_dir / "missing_edge_audit.csv", missing_edge)
    write_csv(output_dir / "reference_event_timelines.csv", timelines)
    write_json(output_dir / "prefix_parity_audit.json", prefix_audit)
    write_json(output_dir / "thresholds_used.json", cfg.to_dict())
    write_json(
        output_dir / "data_contract.json",
        {
            "package": cfg.package_id,
            "reuses": [
                "aggressor_efficiency_flip.impact.measure_dual_impact",
                "aggressor_efficiency_flip.compression.evaluate_compression",
                "aggressor_efficiency_flip.buckets.*",
                "aggressor_efficiency_flip.trade_loader.*",
                "aggressor_efficiency_flip.episodes.discover_episodes",
                "l2_wall_attack_discovery.models.tick_size",
                "wall_toxicity_audit semantics: ASK<-Buy, BID<-Sell",
            ],
            "input_events": {
                "primary_smoke": "AEF allowed compressions (no measured edge)",
                "reference": "synthetic fixtures with known edges",
            },
            "edge_policy": "Acceptance requires measured/synthetic edge; inferred wall_side alone → UNKNOWN_EDGE",
            "windows": {"flow": "5s", "post": "5s", "trap_checkpoints_s": [5, 10, 30, 60, 180]},
            "causal": True,
            "outcomes_separated": True,
        },
    )
    write_json(
        output_dir / "data_quality_audit.json",
        {
            "n_features": len(features),
            "n_outcomes": len(outcomes),
            "prefix_parity_ok": prefix_audit["ok"],
            "missing_edge_events": len(missing_edge),
            "trap_labels": dict(trap_counts),
            "acceptance_labels": dict(acc_counts),
            "symbols": dict(by_sym),
            "query_log": query_log,
            "ch_events_processed": ch_events,
        },
    )
    elapsed = time.perf_counter() - t_wall0
    summary = {
        "package": cfg.package_id,
        "n_events": len(features),
        "n_ch_events": ch_events,
        "n_synthetic": sum(1 for i in inventory if i.get("smoke_cohort") == "synthetic_reference"),
        "state_counts": dict(state_counts),
        "trap_counts": dict(trap_counts),
        "acceptance_counts": dict(acc_counts),
        "prefix_parity_ok": prefix_audit["ok"],
        "missing_edge_n": len(missing_edge),
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "small_n_warning": True,
        "smoke_windows": SMOKE_WINDOWS,
    }
    write_json(output_dir / "SUMMARY.json", summary)
    (output_dir / "commands.txt").write_text(
        "\n".join(
            [
                "cd /home/telgenbuescher/projects/orderbook_analyse",
                "PYTHONPATH=src .venv/bin/python -m pytest tests/test_aggressor_efficiency_trapped_vwap_acceptance_v1.py -q",
                "PYTHONPATH=src .venv/bin/python scripts/run_aggressor_efficiency_trapped_vwap_acceptance_v1.py --smoke",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary
