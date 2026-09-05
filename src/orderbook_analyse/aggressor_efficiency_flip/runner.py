"""F0 runner: synthetic or ClickHouse read-only discovery."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets
from orderbook_analyse.aggressor_efficiency_flip.contracts import AEFConfig
from orderbook_analyse.aggressor_efficiency_flip.episodes import discover_episodes
from orderbook_analyse.aggressor_efficiency_flip.integrity import (
    AEFCausalityError,
    assert_entry_after_final,
    assert_finite_episode,
    compare_prefix,
    prefix_snapshot,
)
from orderbook_analyse.aggressor_efficiency_flip.labels import attach_oi_class
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_flip.outcomes import compute_outcomes
from orderbook_analyse.aggressor_efficiency_flip.reporting import (
    ensure_empty_outdir,
    write_csv,
    write_json,
)
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second, iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import (
    AEFIntegrityError,
    load_oi_labels_clickhouse,
    load_trades_clickhouse,
    preflight_canonical,
    trades_from_rows,
)


def run_discovery_on_trades(
    *,
    symbol: str,
    trades: list[Trade],
    start: datetime,
    end: datetime,
    cfg: AEFConfig,
    oi_labels: Optional[dict[datetime, str]] = None,
    as_of: Optional[datetime] = None,
) -> dict[str, Any]:
    buckets = build_second_buckets(trades)
    result = discover_episodes(
        symbol=symbol,
        trades=trades,
        buckets=buckets,
        start=start,
        end=end,
        cfg=cfg,
        as_of=as_of,
        oi_labels=oi_labels or {},
    )
    for c in result["candidates"]:
        attach_oi_class(c, oi_labels or {})
        assert_finite_episode(c)
        assert_entry_after_final(c)
    result["buckets"] = buckets
    result["one_second_rows"] = [
        buckets[k].to_dict() for k in sorted(buckets.keys()) if start <= k < end
    ]
    result["outcomes"] = compute_outcomes(
        result["candidates"], buckets, data_end=as_of or end
    )
    return result


def run_prefix_parity(
    *,
    symbol: str,
    trades: list[Trade],
    start: datetime,
    end: datetime,
    cfg: AEFConfig,
) -> dict[str, Any]:
    full = run_discovery_on_trades(
        symbol=symbol, trades=trades, start=start, end=end, cfg=cfg
    )
    full_snap = prefix_snapshot(full)
    cutoffs = []
    # Sparse grid + causal stage cutoffs from first few candidates (F0 gate).
    span = max(1.0, (end - start).total_seconds())
    for frac in (0.15, 0.35, 0.55, 0.75):
        cutoffs.append(start + timedelta(seconds=span * frac))
    for c in (full.get("compressions") or [])[:3]:
        t2 = parse_utc(c["t2"]) if c.get("t2") else None
        if t2 is not None:
            cutoffs.extend(
                [
                    t2 - timedelta(seconds=6),  # during flow/post
                    t2 - timedelta(seconds=1),  # before confirm
                    t2,  # after compression confirm
                    t2 + timedelta(seconds=30),  # during counter search
                ]
            )
    for cand in (full.get("candidates") or [])[:3]:
        final = parse_utc(cand["final_decision_ts"])
        cutoffs.extend(
            [
                final - timedelta(seconds=30),
                final - timedelta(seconds=1),
                final,
                final + timedelta(seconds=1),
            ]
        )
        for key in (
            "compression_confirmed_ts",
            "counter_confirmed_ts",
            "structure_break_confirmed_ts",
            "acceptance_confirmed_ts",
        ):
            if cand.get(key):
                cutoffs.append(parse_utc(cand[key]))
    errors: list[str] = []
    details = []
    for cut in sorted(set(floor_second(c) for c in cutoffs)):
        if cut <= start or cut > end:
            continue
        pref = run_discovery_on_trades(
            symbol=symbol, trades=trades, start=start, end=end, cfg=cfg, as_of=cut
        )
        errs = compare_prefix(full_snap, prefix_snapshot(pref), cutoff=iso_z(cut) or "")
        details.append({"cutoff": iso_z(cut), "errors": errs})
        errors.extend(errs)
    return {
        "ok": not errors,
        "errors": errors,
        "details": details,
        "n_full_candidates": len(full["candidates"]),
        "n_full_compressions": len(full["compressions"]),
    }


def run_f0(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    profile: str = "unfitted_f0_diagnostic",
    trades: Optional[list[Trade]] = None,
    skip_ch: bool = False,
) -> dict[str, Any]:
    cfg = AEFConfig.from_profile(profile)
    ensure_empty_outdir(output_dir)
    query_log: list[dict[str, Any]] = []
    preflight: dict[str, Any] = {"mode": "fixture" if trades is not None or skip_ch else "clickhouse"}

    if trades is None and not skip_ch:
        preflight = preflight_canonical(
            symbol=symbol, start=start, end=end, query_log=query_log
        )
        trades, load_meta = load_trades_clickhouse(
            symbol=symbol, start=start, end=end, query_log=query_log
        )
        preflight.update(load_meta)
        oi_labels = load_oi_labels_clickhouse(
            symbol=symbol, start=start, end=end, query_log=query_log
        )
    else:
        trades = trades or []
        oi_labels = {}

    write_json(output_dir / "resolved_config.json", cfg.to_dict())
    write_json(output_dir / "source_preflight.json", preflight)

    result = run_discovery_on_trades(
        symbol=symbol,
        trades=trades,
        start=start,
        end=end,
        cfg=cfg,
        oi_labels=oi_labels,
    )
    parity = run_prefix_parity(
        symbol=symbol, trades=trades, start=start, end=end, cfg=cfg
    )
    if not parity["ok"]:
        raise AEFCausalityError(f"prefix_parity_failed:{parity['errors'][:5]}")

    write_csv(output_dir / "one_second_buckets.csv", result["one_second_rows"])
    write_csv(output_dir / "aggressor_bursts.csv", result["bursts"])
    write_csv(output_dir / "compression_events.csv", result["compressions"])
    write_csv(output_dir / "counter_initiative_events.csv", result["counters"])
    write_csv(output_dir / "state_transitions.csv", result["transitions"])
    write_csv(output_dir / "diagnostic_candidates.csv", result["candidates"])
    write_csv(output_dir / "event_timeline.csv", result["timeline"])
    write_csv(output_dir / "diagnostic_outcomes.csv", result["outcomes"])
    write_json(output_dir / "prefix_parity.json", parity)

    # funnel
    funnel = {
        "one_second_buckets": len(result["one_second_rows"]),
        "raw_bursts": len(result["bursts"]),
        "compression_evaluated": len(result["compressions"]),
        "compression_allowed": sum(1 for c in result["compressions"] if c["allowed"]),
        "same_side_veto": sum(
            1 for c in result["compressions"] if c["strong_same_side_impact_veto"]
        ),
        "delayed_continuation_veto": sum(
            1 for c in result["compressions"] if c.get("delayed_continuation_veto")
        ),
        "counter_events": len(result["counters"]),
        "counter_confirmed": sum(1 for c in result["counters"] if c["confirmed"]),
        "diagnostic_candidates": len(result["candidates"]),
    }
    integrity = {
        "prefix_parity_ok": parity["ok"],
        "candidates_finite": True,
        "funnel": funnel,
        "oi_labels": len(oi_labels),
        "ob_available": False,
        "bbo_available": False,
    }
    write_json(output_dir / "integrity_report.json", integrity)
    write_json(
        output_dir / "data_quality.json",
        {
            "trades": len(trades),
            "unique_seconds": len(result["one_second_rows"]),
            "preflight": preflight,
        },
    )
    write_json(
        output_dir / "run_manifest.json",
        {
            "symbol": str(symbol).upper(),
            "start": iso_z(start),
            "end_exclusive": iso_z(end),
            "profile": cfg.profile_name,
            "feature_version": cfg.feature_version,
            "causal_contract_version": cfg.causal_contract_version,
            "funnel": funnel,
            "prefix_parity_ok": parity["ok"],
            "status_label": cfg.status_label,
            "unfitted": True,
        },
    )
    qmd = ["# Query log — AEF F0", ""]
    for q in query_log:
        qmd.append(f"## {q.get('title')} ({q.get('ms')} ms, rows={q.get('rows')})")
        qmd.append("```sql")
        qmd.append(q.get("sql", ""))
        qmd.append("```")
        qmd.append("")
    if not query_log:
        qmd.append("No ClickHouse queries (fixture mode).")
    (output_dir / "query_log.md").write_text("\n".join(qmd), encoding="utf-8")

    return {
        "output_dir": str(output_dir),
        "funnel": funnel,
        "parity": parity,
        "n_candidates": len(result["candidates"]),
        "preflight": preflight,
        "cfg": cfg.to_dict(),
        "result": result,
    }
