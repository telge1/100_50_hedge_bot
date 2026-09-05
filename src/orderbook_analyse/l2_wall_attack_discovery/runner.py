"""Orchestrate L2 wall attack pattern discovery."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery import FORMAT_VERSION
from orderbook_analyse.l2_wall_attack_discovery.attacks import build_attack_episodes
from orderbook_analyse.l2_wall_attack_discovery.attribution import compute_size_dynamics, compute_trade_windows
from orderbook_analyse.l2_wall_attack_discovery.classify import early_classification
from orderbook_analyse.l2_wall_attack_discovery.controls import (
    compute_outcomes,
    cost_context,
    event_vs_control,
    match_controls,
)
from orderbook_analyse.l2_wall_attack_discovery.doge import doge_diagnosis
from orderbook_analyse.l2_wall_attack_discovery.features import contact_features, pre_contact_features
from orderbook_analyse.l2_wall_attack_discovery.integrity import build_causality_audit
from orderbook_analyse.l2_wall_attack_discovery.labels import label_all_horizons
from orderbook_analyse.l2_wall_attack_discovery.models import FIELD_SEMANTICS
from orderbook_analyse.l2_wall_attack_discovery.patterns import pattern_summaries
from orderbook_analyse.l2_wall_attack_discovery.trades import load_public_trades, source_integrity_rows
from orderbook_analyse.ob200_v3_raw_discovery.audit import process_segment
from orderbook_analyse.ob200_v3_raw_discovery.files import excluded_tmp_files, list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.lifecycles_v2 import build_wall_lifecycles, lifecycle_to_row
from orderbook_analyse.ob200_v3_raw_discovery.walls import extract_wall_events, wall_event_to_row

FORBIDDEN_OUTPUT_MARKERS = (
    "btc_doge_initial",
    "btc_doge_v2",
    "btc_doge_v3",
    "ob200_v3_raw_discovery",
)


def _write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        if headers:
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=headers).writeheader()
        else:
            path.write_text("", encoding="utf-8")
        return
    fields = headers or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run_wall_attack_discovery(
    *,
    raw_root: Path,
    output_dir: Path,
    start: datetime,
    end: datetime,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    sample_ms: int = 250,
    seed: int = 42,
    max_files: int | None = None,
) -> dict[str, Any]:
    out_s = str(output_dir.resolve())
    for marker in FORBIDDEN_OUTPUT_MARKERS:
        if marker in out_s and "l2_wall_attack_discovery" not in out_s:
            raise RuntimeError(f"refusing to write into protected results path containing {marker}")
    output_dir.mkdir(parents=True, exist_ok=True)

    tmp_excluded = excluded_tmp_files(raw_root, symbols)
    segments = list_closed_segments(raw_root, symbols=symbols, start=start, end=end, include_boundary_stubs=False)
    if max_files:
        segments = segments[:max_files]
    if not segments:
        raise RuntimeError("no closed segments in window")

    print(f"loading public trades for {symbols}…", flush=True)
    trades_by: dict[str, Any] = {}
    for sym in symbols:
        trades_by[sym] = load_public_trades(symbol=sym, start=start, end=end)
        print(f"  {sym} trades={len(trades_by[sym])}", flush=True)

    samples_by: dict[str, list] = {s: [] for s in symbols}
    all_events = []
    n_replay_ok = 0
    for i, ref in enumerate(segments, 1):
        print(f"replay {i}/{len(segments)} {ref.path.name}", flush=True)
        audit, samples = process_segment(
            ref, collect_samples=True, sample_ms=sample_ms, warmup_ms=60_000
        )
        if audit.replay_verdict in {
            "REPLAY_CONFIRMED",
            "REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT",
            "PARTIAL_BUT_DISCOVERY_USABLE",
        }:
            n_replay_ok += 1
            # keep samples inside analysis window
            start_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)
            kept = [s for s in samples if start_ms <= s.ts_ms < end_ms]
            samples_by[ref.symbol].extend(kept)
            all_events.extend(extract_wall_events(kept, seed=seed + i))

    for sym in symbols:
        samples_by[sym].sort(key=lambda s: s.ts_ms)

    lifecycles = build_wall_lifecycles(all_events, seed=seed)
    episodes, attack_events = build_attack_episodes(lifecycles, samples_by, trades_by, seed=seed)
    primary = [e for e in episodes if e.get("is_primary")]

    trade_window_rows: list[dict[str, Any]] = []
    dyn_rows: list[dict[str, Any]] = []
    proxy_rows: list[dict[str, Any]] = []
    pre_rows: list[dict[str, Any]] = []
    contact_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    proxies_by_attack: dict[str, list[dict[str, Any]]] = {}

    print(f"attributing {len(primary)} primary attacks…", flush=True)
    ts_index = {sym: [s.ts_ms for s in samples_by[sym]] for sym in symbols}
    trade_ts = {
        sym: ([] if trades_by[sym].empty else trades_by[sym]["ts_ms"].astype(int).tolist())
        for sym in symbols
    }
    for i, ep in enumerate(primary, 1):
        if i % 100 == 0 or i == 1:
            print(f"  episode {i}/{len(primary)}", flush=True)
        sym = ep["symbol"]
        tw = compute_trade_windows(ep, trades_by[sym], ts_list=trade_ts[sym])
        trade_window_rows.extend(tw)
        dyn, prox = compute_size_dynamics(
            ep, samples_by[sym], trades_by[sym], trade_ts_list=trade_ts[sym]
        )
        dyn_rows.extend(dyn)
        proxy_rows.extend(prox)
        proxies_by_attack[ep["attack_id"]] = prox
        pre_rows.append(pre_contact_features(ep, samples_by[sym], tw))
        contact_rows.extend(contact_features(ep, prox, tw))
        label_rows.extend(label_all_horizons(ep, samples_by[sym], prox, ts_index=ts_index[sym]))

    labels_60 = {
        r["attack_id"]: r["resolution_class"]
        for r in label_rows
        if int(r["horizon_s"]) == 60
    }
    proxies_5 = {
        aid: next((p for p in ps if int(p["horizon_s"]) == 5), {})
        for aid, ps in proxies_by_attack.items()
    }
    contact_5 = {
        r["attack_id"]: r for r in contact_rows if int(r["decision_cutoff_s"]) == 5
    }

    funnel = []
    for sym in symbols:
        for side in ("BID", "ASK"):
            ids = [e["attack_id"] for e in primary if e["symbol"] == sym and e["side"] == side]
            funnel.append(
                {
                    "symbol": sym,
                    "side": side,
                    "n_primary": len(ids),
                    **{
                        cls: sum(1 for i in ids if labels_60.get(i) == cls)
                        for cls in (
                            "DEFENDED",
                            "ABSORBED_REFILLED",
                            "PULLED_BEFORE_CONTACT",
                            "PULLED_ON_CONTACT",
                            "CLEAN_BREAK_CONTINUATION",
                            "BREAK_RECLAIM",
                            "FLOW_DIED_NO_DEFENSE",
                            "AMBIGUOUS",
                            "DATA_UNAVAILABLE",
                        )
                    },
                }
            )

    print("pattern summaries…", flush=True)
    feat_sum, bucket_sum, dvsb, avf, pvtd = pattern_summaries(primary, labels_60, proxies_5, contact_5)

    # temporal split: midpoint of window
    split_ms = int((start.timestamp() + end.timestamp()) / 2 * 1000)
    print("early classification…", flush=True)
    early_metrics, early_cm = early_classification(contact_rows, labels_60, primary, split_ms=split_ms)

    print("matched controls…", flush=True)
    controls, ctrl_q = match_controls(primary, samples_by, seed=seed, per_event=2)
    print(f"  n_controls={len(controls)}", flush=True)
    # attach control as episode-like for outcomes
    print("outcomes…", flush=True)
    event_out = compute_outcomes(primary, samples_by, is_control=False)
    print(f"  event_outcome_rows={len(event_out)}", flush=True)
    ctrl_out = compute_outcomes(controls, samples_by, is_control=True)
    print(f"  control_outcome_rows={len(ctrl_out)}", flush=True)
    ev_vs = event_vs_control(event_out + ctrl_out)
    costs = cost_context(event_out + ctrl_out, labels_60)

    print("doge diagnosis…", flush=True)
    doge_diag, doge_dist = doge_diagnosis(lifecycles, samples_by.get("DOGEUSDT", []), episodes)

    # duplicate primary check: same lifecycle + first_contact
    seen = set()
    dups = 0
    for e in primary:
        key = (e["lifecycle_id"], e.get("first_contact_at"))
        if key in seen and e.get("first_contact_at") is not None:
            dups += 1
        seen.add(key)
    causality = build_causality_audit(n_primary=len(primary), n_duplicate_attacks=dups)

    # write artifacts
    _write_csv(output_dir / "source_integrity.csv", source_integrity_rows(
        trades_by, start=start, end=end,
        n_samples={s: len(samples_by[s]) for s in symbols},
        n_segments=len(segments),
    ))
    _write_csv(output_dir / "field_semantics.csv", FIELD_SEMANTICS)
    _write_csv(output_dir / "wall_lifecycles.csv", [lifecycle_to_row(x) for x in lifecycles])
    _write_csv(output_dir / "wall_events.csv", [wall_event_to_row(e) for e in all_events])
    # contact markers retained inside attack_episodes; attack_events kept for debugging count
    _ = attack_events
    _write_csv(output_dir / "attack_episodes.csv", episodes)
    _write_csv(output_dir / "attack_trade_windows.csv", trade_window_rows)
    _write_csv(output_dir / "wall_size_dynamics.csv", dyn_rows)
    _write_csv(output_dir / "trade_attribution_proxies.csv", proxy_rows)
    _write_csv(output_dir / "attack_features_pre_contact.csv", pre_rows)
    _write_csv(output_dir / "attack_features_contact.csv", contact_rows)
    _write_csv(output_dir / "attack_resolution_labels.csv", label_rows)
    _write_csv(output_dir / "resolution_funnel.csv", funnel)
    _write_csv(output_dir / "pattern_feature_summary.csv", feat_sum)
    _write_csv(output_dir / "pattern_bucket_summary.csv", bucket_sum)
    _write_csv(output_dir / "defense_vs_break.csv", dvsb)
    _write_csv(output_dir / "absorption_vs_flow_died.csv", avf)
    _write_csv(output_dir / "pull_vs_trade_depletion.csv", pvtd)
    _write_csv(output_dir / "early_classification_metrics.csv", early_metrics)
    _write_csv(
        output_dir / "early_classification_confusion.csv",
        early_cm,
        headers=["decision_cutoff_s", "true_class", "pred_class", "count"],
    )
    _write_csv(
        output_dir / "matched_controls.csv",
        controls,
        headers=[
            "control_id",
            "matched_to_attack_id",
            "symbol",
            "side",
            "direction",
            "entry_at",
            "mid",
            "spread_bps",
            "imbalance_l10",
            "is_control",
            "match_quality",
        ],
    )
    _write_csv(
        output_dir / "control_match_quality.csv",
        ctrl_q,
        headers=["attack_id", "n_controls", "match_quality", "pool_size"],
    )
    _write_csv(output_dir / "attack_outcomes.csv", event_out + ctrl_out)
    _write_csv(output_dir / "event_vs_control.csv", ev_vs)
    _write_csv(output_dir / "cost_context.csv", costs)
    (output_dir / "doge_diagnosis.json").write_text(json.dumps(doge_diag, indent=2) + "\n", encoding="utf-8")
    _write_csv(output_dir / "doge_wall_distance_distribution.csv", doge_dist)
    (output_dir / "causality_attribution_audit.json").write_text(
        json.dumps(causality, indent=2) + "\n", encoding="utf-8"
    )

    class_counts = Counter(labels_60.values())
    manifest = {
        "format_version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_start_utc": start.isoformat().replace("+00:00", "Z"),
        "window_end_utc": end.isoformat().replace("+00:00", "Z"),
        "symbols": list(symbols),
        "sample_ms": sample_ms,
        "seed": seed,
        "closed_segments": len(segments),
        "replay_ok_segments": n_replay_ok,
        "tmp_excluded": [str(p) for p in tmp_excluded],
        "n_samples": {s: len(samples_by[s]) for s in symbols},
        "n_wall_events": len(all_events),
        "n_lifecycles": len(lifecycles),
        "n_attack_episodes": len(episodes),
        "n_primary_attacks": len(primary),
        "n_controls": len(controls),
        "resolution_60s_counts": dict(class_counts),
        "train_test_split_ms": split_ms,
        "oi_liq_used_for_selection": False,
        "required_artifacts_28": True,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    report = _report(manifest, funnel, dvsb, early_metrics, doge_diag, causality)
    (output_dir / "analysis_report.md").write_text(report, encoding="utf-8")
    return manifest


def _report(manifest, funnel, dvsb, early_metrics, doge_diag, causality) -> str:
    lines = [
        "# L2 Wall Attack Pattern Discovery V1",
        "",
        f"- Window: `{manifest['window_start_utc']}` → `{manifest['window_end_utc']}`",
        f"- Primary attacks: {manifest['n_primary_attacks']}",
        f"- Lifecycles: {manifest['n_lifecycles']}",
        f"- Resolution 60s: `{manifest['resolution_60s_counts']}`",
        "",
        "## Funnel",
        "",
    ]
    for r in funnel:
        lines.append(str(r))
    lines += ["", "## Defense vs Break", ""]
    for r in dvsb:
        lines.append(str(r))
    lines += ["", "## Early classification", ""]
    for r in early_metrics:
        lines.append(str(r))
    lines += ["", "## DOGE", "", json.dumps(doge_diag, indent=2), "", "## Causality", ""]
    for k, v in causality.items():
        lines.append(f"- {k}: {v['status']}")
    lines += [
        "",
        "## Notes",
        "",
        "- OI/liquidations were NOT used for event selection.",
        "- Labels are ex-post; features are causal cutoffs; outcomes start after cutoffs.",
        "- No PnL / TP-SL optimization.",
        "",
    ]
    return "\n".join(lines)
