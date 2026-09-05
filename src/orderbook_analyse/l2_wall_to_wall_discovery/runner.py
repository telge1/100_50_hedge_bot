"""Orchestrate L2 wall-to-wall strategy discovery."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery import FORMAT_VERSION, HORIZONS_S
from orderbook_analyse.l2_wall_to_wall_discovery.exits import compute_path_and_exits
from orderbook_analyse.l2_wall_to_wall_discovery.integrity import build_causality_audit
from orderbook_analyse.l2_wall_to_wall_discovery.models import read_csv, sample_index, write_csv
from orderbook_analyse.l2_wall_to_wall_discovery.outcomes import (
    attach_oi_liq_context,
    compute_horizon_outcomes,
    cost_summary,
    event_vs_control,
    match_controls,
)
from orderbook_analyse.l2_wall_to_wall_discovery.signals import detect_breakout_signals, detect_reclaim_signals
from orderbook_analyse.l2_wall_to_wall_discovery.targets import select_target_wall, track_target
from orderbook_analyse.ob200_v3_raw_discovery.audit import process_segment
from orderbook_analyse.ob200_v3_raw_discovery.files import excluded_tmp_files, list_closed_segments

FORBIDDEN = ("l2_wall_attack_discovery", "ob200_v3_raw_discovery", "btc_doge_initial", "btc_doge_v2", "btc_doge_v3")


def run_wall_to_wall(
    *,
    attack_dir: Path,
    raw_root: Path,
    output_dir: Path,
    event_start: datetime,
    event_end: datetime,
    outcome_end: datetime,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    sample_ms: int = 250,
    seed: int = 42,
) -> dict[str, Any]:
    out_s = str(output_dir.resolve())
    if "l2_wall_to_wall_discovery" not in out_s:
        for m in FORBIDDEN:
            if m in out_s:
                raise RuntimeError(f"refusing to write into protected path ({m})")
    output_dir.mkdir(parents=True, exist_ok=True)

    episodes = [e for e in read_csv(attack_dir / "attack_episodes.csv") if e.get("is_primary") in ("True", "true", True)]
    lifecycles = read_csv(attack_dir / "wall_lifecycles.csv")
    proxies = read_csv(attack_dir / "trade_attribution_proxies.csv")
    proxy_5 = {
        r["attack_id"]: r
        for r in proxies
        if str(r.get("horizon_s")) == "5"
    }
    labels_60 = {
        r["attack_id"]: r["resolution_class"]
        for r in read_csv(attack_dir / "attack_resolution_labels.csv")
        if str(r.get("horizon_s")) == "60"
    }

    # filter episodes to event window by first_contact
    start_ms = int(event_start.timestamp() * 1000)
    end_ms = int(event_end.timestamp() * 1000)
    episodes = [
        e
        for e in episodes
        if e.get("first_contact_at") and start_ms <= int(float(e["first_contact_at"])) < end_ms
    ]
    episodes = [e for e in episodes if e.get("symbol") in symbols]

    tmp = excluded_tmp_files(raw_root, symbols)
    segments = list_closed_segments(
        raw_root, symbols=symbols, start=event_start, end=outcome_end, include_boundary_stubs=False
    )
    print(f"replay {len(segments)} segments for samples…", flush=True)
    samples_by: dict[str, list] = {s: [] for s in symbols}
    for i, ref in enumerate(segments, 1):
        print(f"  {i}/{len(segments)} {ref.path.name}", flush=True)
        audit, samples = process_segment(ref, collect_samples=True, sample_ms=sample_ms, warmup_ms=30_000)
        if audit.replay_verdict in {
            "REPLAY_CONFIRMED",
            "REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT",
            "PARTIAL_BUT_DISCOVERY_USABLE",
        }:
            samples_by[ref.symbol].extend(samples)
    for sym in symbols:
        samples_by[sym].sort(key=lambda s: s.ts_ms)
    ts_by = {sym: sample_index(samples_by[sym]) for sym in symbols}
    data_end_ms = max((samples_by[s][-1].ts_ms for s in symbols if samples_by[s]), default=end_ms)

    write_csv(
        output_dir / "input_integrity.csv",
        [
            {
                "attack_dir": str(attack_dir),
                "n_primary_episodes_in_window": len(episodes),
                "n_lifecycles": len(lifecycles),
                "n_segments_replayed": len(segments),
                "tmp_excluded": len(tmp),
                "event_start": event_start.isoformat().replace("+00:00", "Z"),
                "event_end": event_end.isoformat().replace("+00:00", "Z"),
                "outcome_end": outcome_end.isoformat().replace("+00:00", "Z"),
                "data_end_ms": data_end_ms,
                "n_samples": {s: len(samples_by[s]) for s in symbols},
            }
        ],
    )

    reclaim_rows: list[dict[str, Any]] = []
    break_rows: list[dict[str, Any]] = []
    print(f"scanning signals on {len(episodes)} episodes…", flush=True)
    for i, ep in enumerate(episodes, 1):
        if i % 200 == 0 or i == 1:
            print(f"  episode {i}/{len(episodes)}", flush=True)
        sym = ep["symbol"]
        px = proxy_5.get(ep["attack_id"])
        reclaim_rows.extend(detect_reclaim_signals(ep, samples_by[sym], ts_by[sym], px))
        break_rows.extend(detect_breakout_signals(ep, samples_by[sym], ts_by[sym], px))

    # de-dupe overlapping primary trades: keep earliest confirmation per attack+module
    def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            key = (r["attack_id"], r["module"])
            if key not in best or int(r["confirmed_at"]) < int(best[key]["confirmed_at"]):
                best[key] = r
        # also keep all variants for summary — primary entry set uses preferred variants
        return rows

    reclaim_rows = _dedupe(reclaim_rows)
    break_rows = _dedupe(break_rows)

    # Preferred entry set: strongest confirmations first
    pref_reclaim = ("R3_HOLD_3S", "R2_HOLD_1S", "R5_REFILL_RECLAIM", "R4_RETEST_HOLD", "R1_CROSS")
    pref_break = ("B2_HOLD_3S", "B5_WALL_REMOVED_CONFIRM", "B4_RETEST_FAIL", "B3_DISTANCE_CONFIRM", "B1_HOLD_1S")

    def _pick_primary(rows: list[dict[str, Any]], order: tuple[str, ...]) -> list[dict[str, Any]]:
        by_atk: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_atk[r["attack_id"]].append(r)
        picked = []
        for aid, grp in by_atk.items():
            grp_sorted = sorted(grp, key=lambda x: (order.index(x["variant"]) if x["variant"] in order else 99, x["confirmed_at"]))
            primary = dict(grp_sorted[0])
            primary["is_strategy_primary"] = True
            picked.append(primary)
        return picked

    entries = _pick_primary(reclaim_rows, pref_reclaim) + _pick_primary(break_rows, pref_break)
    # non-overlapping entries: same symbol side within 60s keep first
    entries = sorted(entries, key=lambda e: int(e["entry_at"]))
    kept = []
    last_by: dict[tuple[str, str], int] = {}
    for e in entries:
        key = (e["symbol"], e["position_side"])
        prev = last_by.get(key)
        if prev is not None and int(e["entry_at"]) - prev < 60_000:
            continue
        kept.append(e)
        last_by[key] = int(e["entry_at"])
    entries = kept

    write_csv(
        output_dir / "reclaim_signals.csv",
        reclaim_rows,
        empty_reason="no_causal_reclaim_confirmations_in_window",
    )
    write_csv(
        output_dir / "breakout_signals.csv",
        break_rows,
        empty_reason="no_causal_breakout_confirmations_in_window",
    )
    write_csv(
        output_dir / "entry_candidates.csv",
        entries,
        empty_reason="no_primary_entries_after_dedupe",
    )

    targets = []
    timelines = []
    target_res_rows = []
    paths = []
    exits_all = []
    print(f"targets/exits for {len(entries)} entries…", flush=True)
    for i, e in enumerate(entries, 1):
        if i % 100 == 0 or i == 1:
            print(f"  entry {i}/{len(entries)}", flush=True)
        sym = e["symbol"]
        tgt = select_target_wall(e, samples=samples_by[sym], ts_index=ts_by[sym], lifecycles=lifecycles)
        targets.append(tgt)
        tl, tres = track_target(e, tgt, samples_by[sym], ts_by[sym])
        # downsample timeline to reduce size: keep state changes + every ~5s
        slim = []
        last_state = None
        last_ts = -10**18
        for row in tl:
            if row["state"] != last_state or row["ts_ms"] - last_ts >= 5000:
                slim.append(row)
                last_state = row["state"]
                last_ts = row["ts_ms"]
        timelines.extend(slim)
        target_res_rows.append(tres)
        path_row, exits = compute_path_and_exits(e, tgt, tres, tl, samples_by[sym], ts_by[sym])
        path_row["ex_post_resolution_60s"] = labels_60.get(e["attack_id"])
        paths.append(path_row)
        for x in exits:
            x["module"] = e["module"]
            x["position_side"] = e["position_side"]
            x["variant"] = e["variant"]
        exits_all.extend(exits)

    write_csv(output_dir / "target_walls_at_entry.csv", targets, empty_reason="no_entries")
    write_csv(output_dir / "target_wall_timeline.csv", timelines, empty_reason="no_target_timelines")
    write_csv(output_dir / "target_resolution.csv", target_res_rows, empty_reason="no_entries")
    write_csv(output_dir / "wall_to_wall_paths.csv", paths, empty_reason="no_entries")
    write_csv(output_dir / "exit_variants.csv", exits_all, empty_reason="no_entries")

    print("outcomes…", flush=True)
    strategy_out = compute_horizon_outcomes(entries, samples_by, ts_by, data_end_ms=data_end_ms)
    write_csv(output_dir / "strategy_outcomes.csv", strategy_out)

    # summaries
    horizon_summary = []
    for module in ("WALL_HOLD_RECLAIM", "WALL_REMOVED_BREAK"):
        for pos in ("LONG", "SHORT"):
            for h in HORIZONS_S:
                vals = [
                    float(r["forward_return_bps"])
                    for r in strategy_out
                    if r["module"] == module
                    and r["position_side"] == pos
                    and r["horizon_s"] == h
                    and r.get("outcome_complete")
                    and r.get("forward_return_bps") is not None
                ]
                if not vals:
                    continue
                horizon_summary.append(
                    {
                        "module": module,
                        "position_side": pos,
                        "horizon_s": h,
                        "n": len(vals),
                        "mean_fwd_bps": sum(vals) / len(vals),
                        "median_fwd_bps": sorted(vals)[len(vals) // 2],
                    }
                )
    write_csv(output_dir / "horizon_summary.csv", horizon_summary)

    hit_summary = []
    for module in ("WALL_HOLD_RECLAIM", "WALL_REMOVED_BREAK"):
        sub = [p for p in paths if p["module"] == module]
        if not sub:
            continue
        hits = sum(1 for p in sub if str(p.get("target_reached")).lower() == "true")
        times = [int(p["time_to_target_ms"]) for p in sub if p.get("time_to_target_ms")]
        hit_summary.append(
            {
                "module": module,
                "n": len(sub),
                "target_hit_rate": hits / len(sub),
                "median_time_to_target_ms": sorted(times)[len(times) // 2] if times else None,
                "no_target_rate": sum(1 for t in targets if t["signal_id"] in {p["signal_id"] for p in sub} and t.get("no_target_wall"))
                / max(len(sub), 1),
            }
        )
    write_csv(output_dir / "target_hit_summary.csv", hit_summary)
    write_csv(
        output_dir / "wall_hop_summary.csv",
        [{"note": "single-hop discovery in V1; E4 marks continue without multi-hop retarget yet", "max_hops_implemented": 1}],
    )

    def _variant_summary(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        out = []
        for var, grp in sorted(Counter(r["variant"] for r in rows).items()):
            out.append({"kind": kind, "variant": var, "n_signals": grp})
        return out

    write_csv(output_dir / "reclaim_variant_summary.csv", _variant_summary(reclaim_rows, "reclaim"))
    write_csv(output_dir / "breakout_variant_summary.csv", _variant_summary(break_rows, "breakout"))

    # OI/liq context (optional)
    print("OI/liq context…", flush=True)
    try:
        ctx_rows, _ = attach_oi_liq_context(entries, start=event_start, end=event_end)
    except Exception as exc:  # noqa: BLE001
        ctx_rows = [{"signal_id": e["signal_id"], "oi_regime": "DATA_UNAVAILABLE", "error": str(exc)} for e in entries]
    write_csv(output_dir / "oi_liq_context.csv", ctx_rows)

    # context comparison vs 60m returns
    ctx_by = {r["signal_id"]: r for r in ctx_rows}
    out_60 = {r["signal_id"]: r for r in strategy_out if r["horizon_s"] == 3600 and r.get("outcome_complete")}
    ctx_cmp = []
    for regime in sorted({r.get("oi_regime") for r in ctx_rows}):
        ids = [sid for sid, r in ctx_by.items() if r.get("oi_regime") == regime and sid in out_60]
        vals = [float(out_60[i]["forward_return_bps"]) for i in ids if out_60[i].get("forward_return_bps") is not None]
        if not vals:
            continue
        ctx_cmp.append(
            {
                "oi_regime": regime,
                "n": len(vals),
                "mean_fwd_bps_1h": sum(vals) / len(vals),
                "median_fwd_bps_1h": sorted(vals)[len(vals) // 2],
            }
        )
    write_csv(output_dir / "context_comparison.csv", ctx_cmp)

    print("controls…", flush=True)
    controls, cq = match_controls(entries, samples_by, seed=seed)
    # control outcomes: map to fake entries
    ctrl_entries = [
        {
            "signal_id": c["control_id"],
            "module": c["module"],
            "variant": "CONTROL",
            "symbol": c["symbol"],
            "position_side": c["position_side"],
            "entry_at": c["entry_at"],
            "entry_mid": c["entry_mid"],
        }
        for c in controls
    ]
    ctrl_out = compute_horizon_outcomes(ctrl_entries, samples_by, ts_by, data_end_ms=data_end_ms)
    write_csv(output_dir / "matched_controls.csv", controls)
    ev_vs = event_vs_control(strategy_out, ctrl_out)
    write_csv(output_dir / "event_vs_control.csv", ev_vs)

    # attach module to exits already done; cost summary
    costs = cost_summary(strategy_out, exits_all)
    write_csv(output_dir / "cost_summary.csv", costs)

    audit = build_causality_audit(n_entries=len(entries))
    (output_dir / "causality_audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")

    manifest = {
        "format_version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "attack_dir": str(attack_dir),
        "event_start_utc": event_start.isoformat().replace("+00:00", "Z"),
        "event_end_utc": event_end.isoformat().replace("+00:00", "Z"),
        "outcome_end_utc": outcome_end.isoformat().replace("+00:00", "Z"),
        "symbols": list(symbols),
        "sample_ms": sample_ms,
        "seed": seed,
        "n_episodes_scanned": len(episodes),
        "n_reclaim_signals": len(reclaim_rows),
        "n_breakout_signals": len(break_rows),
        "n_entries": len(entries),
        "n_entries_reclaim": sum(1 for e in entries if e["module"] == "WALL_HOLD_RECLAIM"),
        "n_entries_break": sum(1 for e in entries if e["module"] == "WALL_REMOVED_BREAK"),
        "n_with_target": sum(1 for t in targets if not t.get("no_target_wall")),
        "n_controls": len(controls),
        "data_end_ms": data_end_ms,
        "required_artifacts_23": True,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    report = [
        "# L2 Wall-to-Wall Strategy Discovery V1",
        "",
        f"- Event window: `{manifest['event_start_utc']}` → `{manifest['event_end_utc']}`",
        f"- Outcome end: `{manifest['outcome_end_utc']}`",
        f"- Entries: {manifest['n_entries']} (reclaim={manifest['n_entries_reclaim']}, break={manifest['n_entries_break']})",
        f"- Targets visible: {manifest['n_with_target']}",
        "",
        "## Horizon summary",
        "",
    ]
    for r in horizon_summary:
        report.append(str(r))
    report += ["", "## Target hits", ""]
    for r in hit_summary:
        report.append(str(r))
    report += ["", "## Event vs control", ""]
    for r in ev_vs[:20]:
        report.append(str(r))
    report += ["", "## Causality", ""]
    for k, v in audit.items():
        if isinstance(v, dict) and "status" in v:
            report.append(f"- {k}: {v['status']}")
    (output_dir / "analysis_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return manifest
