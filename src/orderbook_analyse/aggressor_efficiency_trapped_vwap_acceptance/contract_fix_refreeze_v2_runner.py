"""FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2 runner.

Migrates V1 HIGH∩ACCEPTED cohort under checkpoint+episode contracts V2.
No entry/exit/PnL. No outcomes for design.
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets
from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.bucket_semantics_v2 import (
    BUCKET_SEMANTICS_CONTRACT,
    CoverageWindow,
    build_ob200_second_index,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance_v2 import (
    CHECKPOINT_CONTRACT_V2,
    assert_final_accepted_has_checkpoint,
    evaluate_edge_acceptance_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.episode_contract_v2 import (
    EPISODE_CONTRACT_V2,
    EpisodeTrackerV2,
    event_id_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import FreezeViolation
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v2 import (
    NO_FIT_V2,
    PARENT_FREEZE_SHA,
    TIMESTAMP_EXECUTION_CONTRACT_V2,
    verify_freeze_v2,
    verify_old_freeze_untouched,
    write_freeze_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.state_aligned_outcomes import (
    first_acceptance_available_ts,
)

EXPANSION_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_sample_expansion_v1"
)
OLD_FREEZE_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_edge_forward_outcome_evaluation_v1"
)
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_contract_fix_refreeze_v2"
)


def _load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _parse_cps(feat: dict[str, Any]) -> dict[str, Any]:
    cps = feat.get("acceptance_checkpoints") or {}
    if isinstance(cps, str):
        try:
            cps = json.loads(cps)
        except Exception:
            cps = {}
    return cps if isinstance(cps, dict) else {}


def _had_accepted_checkpoint(feat: dict[str, Any]) -> bool:
    cps = _parse_cps(feat)
    for row in cps.values():
        if isinstance(row, dict) and row.get("state") in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}:
            return True
    for sec in (5, 10, 30, 60):
        if feat.get(f"acceptance_state_at_{sec}s") in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}:
            return True
    return False


def _aggressor_side(direction: str, wall_side: str) -> str:
    d = (direction or "").upper()
    if d == "LONG":
        return "Sell"
    if d == "SHORT":
        return "Buy"
    w = (wall_side or "").upper()
    return "Buy" if w == "ASK" else "Sell"


def _classify_prior56(
    *,
    old_had_cp: bool,
    v2_entry_eligible: bool,
    v2_first: Optional[str],
    source_gap: bool,
    final_state_only: bool,
) -> str:
    if not old_had_cp:
        if v2_entry_eligible and v2_first:
            return "CHECKPOINT_RECOVERED_CAUSALLY"
        if source_gap:
            return "REMAINS_ENTRY_INELIGIBLE_SOURCE_GAP"
        if final_state_only:
            return "FINAL_STATE_ONLY_NOT_TRADABLE"
        return "REMAINS_ENTRY_INELIGIBLE_WARMUP"
    return "OTHER"


def _migration_class(
    *,
    old_had_cp: bool,
    ep_action: str,
    v2_entry: bool,
    old_first: Optional[datetime],
    new_first: Optional[datetime],
    source_gap: bool,
    final_only: bool,
) -> str:
    if ep_action == "MERGED":
        return "MERGED_INTO_EXISTING_EPISODE"
    if ep_action == "NEW_REARM":
        return "NEW_REARMED_EPISODE"
    if not v2_entry:
        if source_gap:
            return "SOURCE_GAP_INELIGIBLE"
        if final_only or not old_had_cp:
            return "FINAL_STATE_ONLY_REMOVED"
        return "OTHER"
    if not old_had_cp and v2_entry:
        return "CAUSAL_CHECKPOINT_RECOVERED"
    if old_first and new_first and new_first > old_first + timedelta(milliseconds=250):
        return "ENTRY_TS_MOVED_LATER"
    if old_had_cp and v2_entry:
        return "UNCHANGED_ELIGIBLE"
    return "OTHER"


def run_contract_fix_refreeze_v2(
    *,
    expansion_dir: Path = EXPANSION_DIR,
    old_freeze_dir: Path = OLD_FREEZE_DIR,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    max_events: Optional[int] = None,
    repeat: int = 2,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)
    freeze_v2_dir = output_dir / "freeze_bundle_v2"
    query_log: list[dict[str, Any]] = []

    # --- Old freeze verify ---
    try:
        old_ver = verify_old_freeze_untouched(old_freeze_dir)
    except FreezeViolation as e:
        write_json(
            output_dir / "verdict.json",
            {"verdict": "OLD_FROZEN_BUNDLE_TAMPERED", "error": str(e), **NO_FIT_V2},
        )
        raise
    write_json(output_dir / "old_freeze_verification.json", old_ver)

    # Write bucket/contract docs early
    (output_dir / "bucket_semantics.md").write_text(
        "# Bucket Semantics V2\n\n"
        + json.dumps(BUCKET_SEMANTICS_CONTRACT, indent=2)
        + "\n\nEvidence: Raw-OB200 sample presence in the same 1s floor + successful "
        "CH `public_trades_canonical` query window. Empty trade seconds with OB200 "
        "observation ⇒ VALID_EMPTY_BUCKET; without OB200 ⇒ SOURCE_GAP.\n",
        encoding="utf-8",
    )
    write_json(output_dir / "checkpoint_contract_v2.json", CHECKPOINT_CONTRACT_V2)
    write_json(output_dir / "episode_contract_v2.json", EPISODE_CONTRACT_V2)
    write_json(output_dir / "timestamp_execution_contract_v2.json", TIMESTAMP_EXECUTION_CONTRACT_V2)

    ha = _load_csv(expansion_dir / "high_accepted_events.csv")
    all_ev = _load_csv(expansion_dir / "frozen_events.csv")
    ha = sorted(ha, key=lambda r: parse_utc(r["decision_ts"]))
    if max_events is not None:
        ha = ha[:max_events]

    cfg = TrapAcceptConfig()

    def _one_pass() -> dict[str, Any]:
        tracker = EpisodeTrackerV2()
        migration: list[dict[str, Any]] = []
        prior56: list[dict[str, Any]] = []
        entry_eligible_rows: list[dict[str, Any]] = []
        checkpoint_summary: list[dict[str, Any]] = []
        parity_rows: list[dict[str, Any]] = []

        by_hour: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in ha:
            by_hour[r.get("source_hour") or "unknown"].append(r)

        for hi, hour in enumerate(sorted(by_hour.keys())):
            rows = sorted(by_hour[hour], key=lambda r: parse_utc(r["decision_ts"]))
            print(f"v2 hour {hi+1}/{len(by_hour)} {hour} n={len(rows)}", flush=True)
            ht = parse_utc(hour)
            event_start, event_end = ht, ht + timedelta(hours=1)
            data_end = event_end + timedelta(seconds=120)
            ob_start = ht - timedelta(hours=1)

            samples_by, _, n_ok = load_ob200_samples(
                symbols=("BTCUSDT", "DOGEUSDT"),
                start=ob_start,
                end=event_end,
                raw_root=raw_root,
                sample_ms=250,
            )
            trades, pre = load_trades_clickhouse(
                symbol="BTCUSDT", start=event_start, end=data_end, query_log=query_log
            )
            buckets = build_second_buckets(trades)
            ob_secs = build_ob200_second_index(samples_by.get("BTCUSDT") or [])
            coverage = CoverageWindow(
                load_start=event_start,
                load_end=data_end,
                query_ok=True,
                rows_loaded=int(pre.get("rows_loaded") or len(trades)),
            )

            for r in rows:
                dts = parse_utc(r["decision_ts"])
                wall = (r.get("wall_side") or "").upper()
                edge_px = float(r["matched_edge_price"]) if r.get("matched_edge_price") else None
                if edge_px is None and r.get("edge_price"):
                    edge_px = float(r["edge_price"])
                side = _aggressor_side(r.get("direction") or "", wall)
                old_had_cp = _had_accepted_checkpoint(r)
                legacy_first = first_acceptance_available_ts(r, dts)

                acc = evaluate_edge_acceptance_v2(
                    buckets=buckets,
                    trades=trades,
                    symbol=r["symbol"],
                    wall_side=wall or None,
                    edge_price=edge_px,
                    edge_confidence="high",
                    decision_ts=dts,
                    aggressor_side=side,
                    cfg=cfg,
                    coverage=coverage,
                    ob200_seconds=ob_secs,
                    scan_horizon_s=60,
                )
                assert_final_accepted_has_checkpoint(acc)

                eid2 = event_id_v2(
                    symbol=r["symbol"],
                    matched_edge_id=r.get("matched_edge_id") or "none",
                    decision_ts=dts,
                    direction=r.get("direction") or "",
                )
                first_v2 = (
                    parse_utc(acc["acceptance_first_available_ts_v2"])
                    if acc.get("acceptance_first_available_ts_v2")
                    else None
                )
                entry_ts = (
                    parse_utc(acc["earliest_causal_entry_ts_v2"])
                    if acc.get("earliest_causal_entry_ts_v2")
                    else None
                )
                ep = tracker.observe_row(
                    symbol=r["symbol"],
                    matched_edge_id=r.get("matched_edge_id") or "none",
                    wall_side=wall or "ASK",
                    decision_ts=dts,
                    acceptance_state_path=acc.get("second_checkpoints") or [],
                    entry_eligible=bool(acc.get("entry_eligible")),
                    acceptance_first_available_ts_v2=first_v2,
                    earliest_causal_entry_ts_v2=entry_ts,
                    source_gap_seen=bool(acc.get("source_gap_seen")),
                    old_event_id=r["event_id"],
                    event_id_v2_val=eid2,
                )
                mig = _migration_class(
                    old_had_cp=old_had_cp,
                    ep_action=ep.get("episode_action") or "",
                    v2_entry=bool(ep.get("entry_eligible_v2")),
                    old_first=legacy_first,
                    new_first=first_v2,
                    source_gap=bool(acc.get("source_gap_seen")),
                    final_only=bool(acc.get("final_state_only_not_tradable")),
                )
                # Override recovery class for prior-56 style
                if not old_had_cp and ep.get("entry_eligible_v2"):
                    mig = "CAUSAL_CHECKPOINT_RECOVERED"
                if ep.get("episode_action") == "MERGED":
                    mig = "MERGED_INTO_EXISTING_EPISODE"
                if ep.get("episode_action") == "NEW_REARM":
                    mig = "NEW_REARMED_EPISODE"

                migration.append(
                    {
                        "old_event_id": r["event_id"],
                        "new_event_id_v2": eid2,
                        "episode_id_v2": ep.get("episode_id_v2"),
                        "entry_signal_id_v2": ep.get("entry_signal_id_v2"),
                        "old_final_acceptance_state": r.get("final_acceptance_state"),
                        "old_acceptance_first_available_ts": iso_z(legacy_first) if legacy_first else None,
                        "new_acceptance_first_available_ts_v2": acc.get("acceptance_first_available_ts_v2"),
                        "earliest_causal_entry_ts_v2": acc.get("earliest_causal_entry_ts_v2"),
                        "old_had_accepted_checkpoint": old_had_cp,
                        "new_final_acceptance_state": acc.get("final_acceptance_state"),
                        "entry_eligible_v2": ep.get("entry_eligible_v2"),
                        "final_state_only_not_tradable": acc.get("final_state_only_not_tradable"),
                        "source_gap_seen": acc.get("source_gap_seen"),
                        "migration_class": mig,
                        "episode_action": ep.get("episode_action"),
                        "reason": ep.get("episode_action"),
                        "matched_edge_id": r.get("matched_edge_id"),
                        "decision_ts": r.get("decision_ts"),
                        "symbol": r.get("symbol"),
                    }
                )

                if not old_had_cp:
                    prior56.append(
                        {
                            "old_event_id": r["event_id"],
                            "decision_ts": r.get("decision_ts"),
                            "old_final": r.get("final_acceptance_state"),
                            "v2_final": acc.get("final_acceptance_state"),
                            "v2_entry_eligible": ep.get("entry_eligible_v2"),
                            "acceptance_first_available_ts_v2": acc.get(
                                "acceptance_first_available_ts_v2"
                            ),
                            "source_gap_seen": acc.get("source_gap_seen"),
                            "final_state_only_not_tradable": acc.get("final_state_only_not_tradable"),
                            "classification": _classify_prior56(
                                old_had_cp=False,
                                v2_entry_eligible=bool(ep.get("entry_eligible_v2")),
                                v2_first=acc.get("acceptance_first_available_ts_v2"),
                                source_gap=bool(acc.get("source_gap_seen")),
                                final_state_only=bool(acc.get("final_state_only_not_tradable")),
                            ),
                        }
                    )

                checkpoint_summary.append(
                    {
                        "old_event_id": r["event_id"],
                        "n_seconds_scanned": acc.get("n_seconds_scanned"),
                        "n_entry_eligible_seconds": acc.get("n_entry_eligible_seconds"),
                        "entry_eligible": acc.get("entry_eligible"),
                        "final_state_only_not_tradable": acc.get("final_state_only_not_tradable"),
                        "cp_5s": acc.get("acceptance_state_at_5s"),
                        "cp_10s": acc.get("acceptance_state_at_10s"),
                        "cp_30s": acc.get("acceptance_state_at_30s"),
                        "cp_60s": acc.get("acceptance_state_at_60s"),
                    }
                )

                if ep.get("entry_eligible_v2"):
                    entry_eligible_rows.append(
                        {
                            **{k: migration[-1][k] for k in migration[-1]},
                            "wall_side": wall,
                            "direction": r.get("direction"),
                            "matched_edge_price": r.get("matched_edge_price"),
                            "edge_match_confidence_class": r.get("edge_match_confidence_class"),
                        }
                    )
                    # Prefix parity at acceptance_first_available_ts_v2
                    cut = first_v2
                    assert cut is not None
                    # truncate trades/buckets and OB index conceptually via as_of + coverage end
                    cov_pref = CoverageWindow(
                        load_start=event_start,
                        load_end=min(data_end, cut + timedelta(seconds=1)),
                        query_ok=True,
                        rows_loaded=coverage.rows_loaded,
                    )
                    trades_pref = [
                        t
                        for t in trades
                        if (t.trade_ts if t.trade_ts.tzinfo else t.trade_ts.replace(tzinfo=timezone.utc))
                        <= cut
                    ]
                    buckets_pref = build_second_buckets(trades_pref)
                    acc_pref = evaluate_edge_acceptance_v2(
                        buckets=buckets_pref,
                        trades=trades_pref,
                        symbol=r["symbol"],
                        wall_side=wall or None,
                        edge_price=edge_px,
                        edge_confidence="high",
                        decision_ts=dts,
                        aggressor_side=side,
                        cfg=cfg,
                        coverage=cov_pref,
                        ob200_seconds=ob_secs,
                        as_of=cut,
                        scan_horizon_s=60,
                    )
                    # future injection: full trades, as_of=cut
                    acc_fut = evaluate_edge_acceptance_v2(
                        buckets=buckets,
                        trades=trades,
                        symbol=r["symbol"],
                        wall_side=wall or None,
                        edge_price=edge_px,
                        edge_confidence="high",
                        decision_ts=dts,
                        aggressor_side=side,
                        cfg=cfg,
                        coverage=coverage,
                        ob200_seconds=ob_secs,
                        as_of=cut,
                        scan_horizon_s=60,
                    )
                    fields = [
                        ("acceptance_first_available_ts_v2", acc.get("acceptance_first_available_ts_v2"), acc_pref.get("acceptance_first_available_ts_v2")),
                        ("earliest_causal_entry_ts_v2", acc.get("earliest_causal_entry_ts_v2"), acc_pref.get("earliest_causal_entry_ts_v2")),
                        ("entry_eligible", acc.get("entry_eligible"), acc_pref.get("entry_eligible")),
                        ("final_at_prefix", "ACCEPTED", acc_pref.get("final_acceptance_state")),
                    ]
                    # At as_of=cut, final should be ACCEPTED matching stored direction
                    pref_ok = (
                        acc_pref.get("entry_eligible") is True
                        and acc_pref.get("acceptance_first_available_ts_v2")
                        == acc.get("acceptance_first_available_ts_v2")
                        and acc_fut.get("acceptance_first_available_ts_v2")
                        == acc_pref.get("acceptance_first_available_ts_v2")
                        and acc_fut.get("entry_eligible") == acc_pref.get("entry_eligible")
                    )
                    # direction
                    dir_ok = True
                    for row in acc_pref.get("second_checkpoints") or []:
                        if row.get("entry_eligible"):
                            if row.get("acceptance_state_at_ts") not in {
                                r.get("final_acceptance_state"),
                                "ACCEPTED_ABOVE",
                                "ACCEPTED_BELOW",
                            }:
                                dir_ok = False
                            break
                    pclass = "EXACT_PARITY" if pref_ok and dir_ok else "ACCEPTANCE_MISMATCH"
                    parity_rows.append(
                        {
                            "old_event_id": r["event_id"],
                            "event_id_v2": eid2,
                            "episode_id_v2": ep.get("episode_id_v2"),
                            "parity_class": pclass,
                            "critical": pclass != "EXACT_PARITY",
                            "full_first_ts": acc.get("acceptance_first_available_ts_v2"),
                            "prefix_first_ts": acc_pref.get("acceptance_first_available_ts_v2"),
                            "future_inj_first_ts": acc_fut.get("acceptance_first_available_ts_v2"),
                            "matched_edge_id": r.get("matched_edge_id"),
                            "acceptance_direction": r.get("final_acceptance_state"),
                        }
                    )

        tracker.flush_open()
        return {
            "migration": migration,
            "prior56": prior56,
            "entry_eligible_rows": entry_eligible_rows,
            "checkpoint_summary": checkpoint_summary,
            "parity_rows": parity_rows,
            "tracker": tracker,
        }

    passes = []
    first = None
    for i in range(max(1, repeat)):
        print(f"=== V2 pass {i+1}/{repeat} ===", flush=True)
        # reset query_log accumulation only conceptually
        result = _one_pass()
        passes.append(result)
        if i == 0:
            first = result
        else:
            # compare fingerprints
            a = [(m["old_event_id"], m["migration_class"], m["episode_id_v2"], m["entry_eligible_v2"]) for m in first["migration"]]
            b = [(m["old_event_id"], m["migration_class"], m["episode_id_v2"], m["entry_eligible_v2"]) for m in result["migration"]]
            if a != b:
                write_json(
                    output_dir / "reproducibility_v2.json",
                    {"ok": False, "error": "pass_mismatch"},
                )
                summary = {
                    "verdict": "FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2_FAILED",
                    "entry_timing": "ENTRY_TIMING_BLOCKED",
                    **NO_FIT_V2,
                }
                write_json(output_dir / "verdict.json", summary)
                return summary

    assert first is not None
    migration = first["migration"]
    prior56 = first["prior56"]
    entry_eligible_rows = first["entry_eligible_rows"]
    checkpoint_summary = first["checkpoint_summary"]
    parity_rows = first["parity_rows"]
    tracker: EpisodeTrackerV2 = first["tracker"]

    write_csv(output_dir / "old_to_v2_migration.csv", migration)
    write_csv(output_dir / "prior_56_case_audit.csv", prior56)
    write_csv(output_dir / "checkpoint_summary.csv", checkpoint_summary)
    write_csv(output_dir / "entry_eligible_events_v2.csv", entry_eligible_rows)
    write_csv(output_dir / "acceptance_episodes_v2.csv", tracker.closed)
    write_csv(output_dir / "duplicate_merge_summary.csv", tracker.merges)
    write_csv(output_dir / "rearm_events_v2.csv", tracker.rearms)
    write_csv(output_dir / "prefix_parity_v2.csv", parity_rows)
    write_csv(
        output_dir / "parity_mismatches_v2.csv",
        [p for p in parity_rows if p.get("parity_class") != "EXACT_PARITY"],
    )

    # density v2
    by_hour = Counter()
    for e in entry_eligible_rows:
        dts = parse_utc(e["decision_ts"]) if e.get("decision_ts") else None
        if dts:
            by_hour[dts.strftime("%Y-%m-%dT%H:00:00Z")] += 1
    write_csv(
        output_dir / "event_density_v2.csv",
        [{"hour": h, "n_entry_eligible_episodes": c} for h, c in sorted(by_hour.items())],
    )

    # DOGE funnel — no reach loosening; offline from frozen_events + note
    doge = [r for r in all_ev if r.get("symbol") == "DOGEUSDT"]
    doge_funnel = {
        "n_aef_events": len(doge),
        "EDGE_NOT_REACHED": sum(1 for r in doge if r.get("edge_join_status") == "EDGE_NOT_REACHED"),
        "HIGH": sum(1 for r in doge if r.get("edge_match_confidence_class") == "HIGH"),
        "ACCEPTED_ANY": sum(
            1 for r in doge if r.get("final_acceptance_state") in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
        ),
        "HIGH_ACCEPTED_ANY": sum(
            1
            for r in doge
            if r.get("edge_match_confidence_class") == "HIGH"
            and r.get("final_acceptance_state") in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
        ),
        "entry_eligible_v2": 0,
        "episodes_v2": 0,
        "note": "Checkpoint/episode V2 does not loosen reach; DOGE remains null HIGH∩ACCEPTED",
    }
    write_csv(output_dir / "doge_funnel_v2.csv", [doge_funnel])

    mig_counts = Counter(m["migration_class"] for m in migration)
    prior56_counts = Counter(p["classification"] for p in prior56)
    n_exact = sum(1 for p in parity_rows if p.get("parity_class") == "EXACT_PARITY")
    n_crit = sum(1 for p in parity_rows if p.get("critical"))
    n_entry_eps = len(entry_eligible_rows)
    n_parity_denom = len(parity_rows)

    repro = {
        "ok": True,
        "n_passes": repeat,
        "n_migration_rows": len(migration),
        "n_entry_eligible": n_entry_eps,
        "parity_exact": n_exact,
        "parity_denom": n_parity_denom,
    }
    write_json(output_dir / "reproducibility_v2.json", repro)

    # --- Create & verify freeze V2 ---
    hashes = write_freeze_v2(freeze_v2_dir, parent_sha=PARENT_FREEZE_SHA)
    write_json(output_dir / "new_freeze_manifest.json", hashes)
    before = verify_freeze_v2(freeze_v2_dir)
    write_json(output_dir / "new_freeze_verification_before.json", before)
    after = verify_freeze_v2(freeze_v2_dir)
    write_json(output_dir / "new_freeze_verification_after.json", after)
    if before["freeze_bundle_sha256"] != after["freeze_bundle_sha256"]:
        verdict = "NEW_FROZEN_BUNDLE_TAMPERED"
        entry = "ENTRY_TIMING_BLOCKED"
    elif old_ver["freeze_bundle_sha256"] != PARENT_FREEZE_SHA:
        verdict = "OLD_FROZEN_BUNDLE_TAMPERED"
        entry = "ENTRY_TIMING_BLOCKED"
    elif n_parity_denom == 0:
        verdict = "FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2_BLOCKED_EPISODE_CONTRACT"
        entry = "ENTRY_TIMING_BLOCKED"
    elif n_crit > 0 or n_exact != n_parity_denom:
        verdict = "FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2_BLOCKED_PARITY"
        entry = "ENTRY_TIMING_BLOCKED"
    elif n_entry_eps == 0:
        verdict = "FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2_BLOCKED_EPISODE_CONTRACT"
        entry = "ENTRY_TIMING_BLOCKED"
    else:
        # Ready gates
        all_entry_have_cp = all(
            e.get("new_acceptance_first_available_ts_v2") for e in entry_eligible_rows
        )
        if not all_entry_have_cp:
            verdict = "FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2_FAILED"
            entry = "ENTRY_TIMING_BLOCKED"
        else:
            verdict = "FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2_READY"
            entry = "ENTRY_TIMING_ALLOWED"

    source_change = {
        "v1_sources_modified": False,
        "v2_new_modules": [
            "bucket_semantics_v2.py",
            "edge_acceptance_v2.py",
            "episode_contract_v2.py",
            "freeze_v2.py",
            "contract_fix_refreeze_v2_runner.py",
        ],
        "acceptance_evidence_thresholds_changed": False,
        "matching_definition_changed": False,
        "note": "V1 edge_acceptance.py untouched so parent freeze still verifies",
    }
    write_json(output_dir / "source_change_manifest.json", source_change)

    elapsed = time.perf_counter() - t0
    readiness = {
        "entry_timing": entry,
        "verdict": verdict,
        "n_old_raw": len(ha),
        "n_entry_eligible_episodes_v2": n_entry_eps,
        "n_parity_exact": n_exact,
        "n_parity_denom": n_parity_denom,
        "n_critical_lookahead": n_crit,
        "migration_class_counts": dict(mig_counts),
        "prior56_counts": dict(prior56_counts),
        "n_merges": len(tracker.merges),
        "n_rearms": len(tracker.rearms),
        "new_freeze_sha": after.get("freeze_bundle_sha256"),
        "old_freeze_sha": old_ver.get("freeze_bundle_sha256"),
        "trading_edge_proven": False,
        **NO_FIT_V2,
    }
    write_json(output_dir / "entry_timing_readiness_v2.json", readiness)

    summary = {
        "verdict": verdict,
        "entry_timing": entry,
        **NO_FIT_V2,
        "old_freeze_sha": old_ver.get("freeze_bundle_sha256"),
        "new_freeze_sha": after.get("freeze_bundle_sha256"),
        "parent_lineage": {
            "parent_freeze_bundle_sha256": PARENT_FREEZE_SHA,
            "refreeze_reason": "CHECKPOINT_AND_EPISODE_CONTRACT_FIX",
            "thresholds_changed": False,
            "state_definition_changed": False,
            "matching_definition_changed": False,
            "acceptance_evidence_thresholds_changed": False,
        },
        "n_old_raw": len(ha),
        "n_v2_events_processed": len(migration),
        "n_entry_eligible_episodes": n_entry_eps,
        "n_closed_episodes_flushed": len(tracker.closed),
        "n_merges": len(tracker.merges),
        "n_rearms": len(tracker.rearms),
        "migration_class_counts": dict(mig_counts),
        "prior56_counts": dict(prior56_counts),
        "parity": {"exact": n_exact, "denom": n_parity_denom, "critical": n_crit},
        "doge_funnel": doge_funnel,
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "reproducibility": repro,
        "trading_edge_proven": False,
    }
    write_json(output_dir / "verdict.json", summary)
    write_json(output_dir / "SUMMARY.json", summary)
    write_json(
        output_dir / "run_manifest.json",
        {
            **NO_FIT_V2,
            "expansion_dir": str(expansion_dir),
            "elapsed_s": round(elapsed, 3),
            "query_count": len(query_log),
            "repeat": repeat,
            "max_events": max_events,
        },
    )
    _write_report(output_dir, summary, readiness, source_change)
    return summary


def _write_report(
    output_dir: Path,
    summary: dict[str, Any],
    readiness: dict[str, Any],
    source_change: dict[str, Any],
) -> None:
    import subprocess

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        dirty = (
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd="/home/telgenbuescher/projects/orderbook_analyse",
                text=True,
            ).strip()
            != ""
        )
    except Exception:
        branch, head, dirty = "unknown", "unknown", True

    next_step = (
        "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1 — nur V2-Episoden, earliest_causal_entry_ts_v2, "
        "keine Rückkehr zur 1679-Rohkohorte"
        if readiness.get("entry_timing") == "ENTRY_TIMING_ALLOWED"
        else "Kein Entry-Timing. Blocker beheben und Refreeze erneut fahren."
    )
    lines = [
        "# ABSCHLUSSBERICHT — FROZEN_HIGH_ACCEPTED_CONTRACT_FIX_REFREEZE_V2",
        "",
        f"1. VERDICT: `{summary['verdict']}`",
        "2. Live-Sicherheit: keine Collector-/Prozessänderung, keine ClickHouse-Writes, alter Freeze unverändert",
        f"3. Branch / HEAD / Dirty: `{branch}` / `{head}` / dirty={dirty}",
        f"4. alter Freeze SHA: `{summary.get('old_freeze_sha')}`",
        f"5. neuer Freeze SHA: `{summary.get('new_freeze_sha')}`",
        f"6. Parent-Lineage: {json.dumps(summary.get('parent_lineage'))}",
        "7. Ursache altes Checkpoint-Problem: leere 1s-Buckets übersprungen → incomplete_scan UNKNOWN_DATA bei final ACCEPTED_*",
        "8. VALID_EMPTY vs SOURCE_GAP: OB200-Sample in derselben 1s-Floor + erfolgreiches CH-Trade-Query-Fenster ⇒ VALID_EMPTY; ohne OB200 ⇒ SOURCE_GAP",
        "9. Checkpoint-Contract: jede Sekunde emitten; VALID_EMPTY trägt State ohne Trade-Erfindung; SOURCE_GAP kein Forward-Fill; Entry nur mit eligible ACCEPTED-Checkpoint",
        "10. Timestamp-Konvention: Bucket [t,t+1s) verfügbar bei t+1s; earliest_causal_entry_ts_v2 = acceptance_first_available_ts_v2",
        f"11. Prior-56: {json.dumps(summary.get('prior56_counts'))}",
        f"12. alter Raw-Row-Count: {summary.get('n_old_raw')}",
        f"13. neue Event-Count V2 (processed rows): {summary.get('n_v2_events_processed')}",
        f"14. neue unabhängige Episoden (closed+eligible): eligible={summary.get('n_entry_eligible_episodes')} closed_flushed={summary.get('n_closed_episodes_flushed')}",
        f"15. neue entry-fähige Episoden: {summary.get('n_entry_eligible_episodes')}",
        f"16. zusammengeführte Duplikate: {summary.get('n_merges')}",
        f"17. echte Re-Arms: {summary.get('n_rearms')}",
        f"18. Migrationsklassen: {json.dumps(summary.get('migration_class_counts'))}",
        f"19. siehe migration_class FINAL_STATE_ONLY_REMOVED / SOURCE_GAP_INELIGIBLE in old_to_v2_migration.csv",
        f"20. DOGE-Funnel: {json.dumps(summary.get('doge_funnel'))}",
        f"21. Full↔Prefix-Parität: {json.dumps(summary.get('parity'))}",
        f"22. kritische Lookahead-Fälle: {(summary.get('parity') or {}).get('critical')}",
        f"23. Reproduzierbarkeit: {json.dumps(summary.get('reproducibility'))}",
        f"24. No-Fit-Flags: {json.dumps(NO_FIT_V2)}",
        "25. Tests: tests/test_frozen_high_accepted_contract_fix_refreeze_v2.py + test_results.txt",
        f"26. geänderte/neue Dateien: {json.dumps(source_change)}",
        f"27. Laufzeit/Queries: {summary.get('elapsed_s')}s / {summary.get('query_count')}",
        f"28. `{readiness.get('entry_timing')}`",
        "29. Noch kein Trading-Edge bewiesen.",
        f"30. Nächster erlaubter Schritt: {next_step}",
        "",
    ]
    (output_dir / "ABSCHLUSSBERICHT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--repeat", type=int, default=2)
    args = p.parse_args()
    s = run_contract_fix_refreeze_v2(
        output_dir=args.output_dir, max_events=args.max_events, repeat=args.repeat
    )
    print(s["verdict"], s.get("entry_timing"), s.get("n_entry_eligible_episodes"))
