"""Prefix-parity integrity audit for HIGH∩ACCEPTED expansion cohort.

Read-only w.r.t. freeze bundle. Outcomes never used for matching/thresholds/
state/sample selection/parity resolution.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import (
    aggregate_window,
    build_second_buckets,
    side_vwap,
)
from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance import (
    evaluate_edge_acceptance,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    build_causal_edges_from_samples,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_disambiguation import (
    DisambiguationThresholds,
    select_disambiguated_match,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import (
    JoinThresholds,
    apply_join_to_event,
    evaluate_candidates,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import (
    FreezeViolation,
    verify_freeze,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.models import InputEvent
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.pipeline import process_event
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_runner import (
    FrozenBundleTampered,
    _copy_freeze_manifests,
    _verify_or_tamper,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.state_aligned_outcomes import (
    first_acceptance_available_ts,
)

EXPANSION_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_sample_expansion_v1"
)
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_prefix_parity_audit_v1"
)
PRIOR_FREEZE = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_edge_forward_outcome_evaluation_v1"
)

# Pre-declared technical tolerance only (not derived from observed deltas).
TIMESTAMP_TOLERANCE_MS = 250

NO_FIT = {
    "outcome_used_for_matching": False,
    "outcome_used_for_thresholds": False,
    "outcome_used_for_state_definition": False,
    "outcome_used_for_sample_selection": False,
    "outcome_used_for_parity_resolution": False,
}

ACCEPTED = {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
DIAG_GAP_S = 60  # diagnostic only — not a fitted / frozen cooldown


def _parse_cps(feat: dict[str, Any]) -> dict[str, Any]:
    cps = feat.get("acceptance_checkpoints") or {}
    if isinstance(cps, str):
        try:
            cps = json.loads(cps)
        except Exception:
            cps = {}
    return cps if isinstance(cps, dict) else {}


def accepted_state_first_ts_from_checkpoints(feat: dict[str, Any], decision_ts: datetime) -> Optional[datetime]:
    cps = _parse_cps(feat)
    best: Optional[datetime] = None
    for key, row in cps.items():
        if not isinstance(row, dict):
            continue
        st = row.get("state")
        if st not in ACCEPTED:
            continue
        ts_s = row.get("checkpoint_ts")
        if ts_s:
            ts = parse_utc(ts_s) if isinstance(ts_s, str) else decision_ts
        else:
            try:
                sec = int(str(key).replace("cp_", "").replace("s", ""))
            except ValueError:
                continue
            ts = decision_ts + timedelta(seconds=sec)
        if best is None or ts < best:
            best = ts
    if best is None:
        for sec in (5, 10, 30, 60):
            st = feat.get(f"acceptance_state_at_{sec}s")
            if st in ACCEPTED:
                return decision_ts + timedelta(seconds=sec)
    return best


def _load_csv(path: Path) -> list[dict[str, Any]]:
    import csv

    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _event_from_row(row: dict[str, Any]) -> InputEvent:
    return InputEvent(
        event_id=row["event_id"],
        symbol=row["symbol"],
        direction=row["direction"],
        wall_side=row.get("wall_side") or None,
        edge_price=None,
        edge_source="none",
        edge_confidence="none",
        flow_start_ts=parse_utc(row["flow_start_ts"]),
        flow_end_ts=parse_utc(row["flow_end_ts"]),
        decision_ts=parse_utc(row["decision_ts"]),
        reference_price=float(row["reference_price"]) if row.get("reference_price") else None,
        data_quality=row.get("data_quality") or "OK",
        source=row.get("source") or "parity_audit",
        meta={},
    )


def _px_eq(a: Any, b: Any, tol: float = 1e-9) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def classify_parity(
    *,
    stored: dict[str, Any],
    feat_full: dict[str, Any],
    feat_pref: dict[str, Any],
    feat_pre: dict[str, Any],
    feat_future_inj: dict[str, Any],
    join_edge_id: Optional[str],
    join_edge_px: Any,
    lock_ts: Optional[datetime],
) -> dict[str, Any]:
    """Classify Full vs Prefix parity. Outcomes never consulted."""
    critical = False
    field = None
    fv = pv = None
    pclass = "EXACT_PARITY"
    reason = "ok"

    # Future injection must not change prefix classification
    if feat_future_inj.get("final_acceptance_state") != feat_pref.get("final_acceptance_state"):
        pclass = "OTHER"
        field = "future_trade_injection_acceptance"
        fv = feat_pref.get("final_acceptance_state")
        pv = feat_future_inj.get("final_acceptance_state")
        critical = True
        reason = "future_trades_changed_prefix_acceptance"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if feat_future_inj.get("matched_edge_id") != feat_pref.get("matched_edge_id"):
        pclass = "EDGE_MISMATCH"
        field = "future_injection_edge"
        fv = feat_pref.get("matched_edge_id")
        pv = feat_future_inj.get("matched_edge_id")
        critical = True
        reason = "future_data_changed_edge"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    # Join recompute vs stored
    if join_edge_id != stored.get("matched_edge_id") and not _px_eq(
        join_edge_px, stored.get("matched_edge_price")
    ):
        pclass = "EDGE_MISMATCH"
        field = "matched_edge_id"
        fv, pv = stored.get("matched_edge_id"), join_edge_id
        critical = True
        reason = "recomputed_edge_differs_from_stored"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if feat_full.get("edge_match_confidence_class") != stored.get("edge_match_confidence_class"):
        pclass = "CONFIDENCE_MISMATCH"
        field = "edge_match_confidence_class"
        fv = stored.get("edge_match_confidence_class")
        pv = feat_full.get("edge_match_confidence_class")
        critical = True
        reason = "recomputed_confidence_differs"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if feat_pref.get("matched_edge_id") != feat_full.get("matched_edge_id"):
        pclass = "EDGE_MISMATCH"
        field = "matched_edge_id_prefix_vs_full"
        fv = feat_full.get("matched_edge_id")
        pv = feat_pref.get("matched_edge_id")
        critical = True
        reason = "prefix_edge_differs_from_full"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if feat_pref.get("edge_match_confidence_class") != feat_full.get("edge_match_confidence_class"):
        pclass = "CONFIDENCE_MISMATCH"
        field = "confidence_prefix_vs_full"
        fv = feat_full.get("edge_match_confidence_class")
        pv = feat_pref.get("edge_match_confidence_class")
        critical = True
        reason = "prefix_confidence_differs"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    stored_acc = stored.get("final_acceptance_state")
    full_acc = feat_full.get("final_acceptance_state")
    pref_acc = feat_pref.get("final_acceptance_state")
    pre_acc = feat_pre.get("final_acceptance_state")

    if lock_ts is None:
        pclass = "INSUFFICIENT_PREFIX_WARMUP"
        field = "first_accepted_lock_ts"
        fv, pv = stored_acc, None
        critical = True
        reason = "cannot_locate_accepted_lock_in_prefix_scan"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if stored_acc not in ACCEPTED:
        pclass = "ACCEPTANCE_MISMATCH"
        field = "final_acceptance_state"
        fv, pv = stored_acc, pref_acc
        critical = True
        reason = "stored_not_accepted"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if full_acc != stored_acc:
        pclass = "ACCEPTANCE_MISMATCH"
        field = "final_acceptance_full_vs_stored"
        fv, pv = stored_acc, full_acc
        critical = True
        reason = "full_window_acceptance_differs_from_stored"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if pref_acc != stored_acc:
        if pref_acc in ACCEPTED and pref_acc != stored_acc:
            pclass = "DIRECTION_MISMATCH"
            field = "final_acceptance_state"
            fv, pv = stored_acc, pref_acc
            critical = True
            reason = "prefix_accepted_other_direction"
            return _pack(pclass, field, fv, pv, critical, reason, lock_ts)
        pclass = "ACCEPTANCE_MISMATCH"
        field = "final_acceptance_state"
        fv, pv = stored_acc, pref_acc
        critical = True
        reason = "prefix_cannot_reproduce_acceptance_at_lock_ts"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    # Acceptance must not be locked before lock_ts (1ms earlier)
    if pre_acc in ACCEPTED:
        # Informational: lock detected at closed bucket; 1ms pre-cut may still see same
        # closed-second state depending on as_of inclusivity. Only critical if pre is
        # a *different* accepted direction.
        if pre_acc != stored_acc:
            pclass = "DIRECTION_MISMATCH"
            field = "pre_lock_acceptance"
            fv, pv = stored_acc, pre_acc
            critical = True
            reason = "pre_lock_wrong_direction"
            return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if feat_full.get("event_id") != stored.get("event_id"):
        pclass = "EVENT_ID_MISMATCH"
        field = "event_id"
        fv, pv = stored.get("event_id"), feat_full.get("event_id")
        critical = True
        reason = "event_id_changed"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    # Trap/research state: only compare at same as_of (prefix vs future-injection).
    # Do NOT compare unbounded full-horizon trap vs lock-prefix — longer horizon ≠ lookahead.
    if feat_future_inj.get("final_trap_label") != feat_pref.get("final_trap_label"):
        pclass = "STATE_MISMATCH"
        field = "final_trap_label"
        fv = feat_pref.get("final_trap_label")
        pv = feat_future_inj.get("final_trap_label")
        critical = True
        reason = "future_changed_trap_at_same_as_of"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    if feat_future_inj.get("final_research_state") != feat_pref.get("final_research_state"):
        pclass = "STATE_MISMATCH"
        field = "final_research_state"
        fv = feat_pref.get("final_research_state")
        pv = feat_future_inj.get("final_research_state")
        critical = True
        reason = "future_changed_research_state_at_same_as_of"
        return _pack(pclass, field, fv, pv, critical, reason, lock_ts)

    return _pack(pclass, field, fv, pv, critical, reason, lock_ts)


def _pack(pclass, field, fv, pv, critical, reason, lock_ts) -> dict[str, Any]:
    return {
        "parity_class": pclass,
        "first_mismatch_field": field,
        "full_value": fv,
        "prefix_value": pv,
        "critical": critical,
        "reason": reason,
        "accepted_lock_ts": iso_z(lock_ts) if lock_ts else None,
        "lookahead_relevant": critical,
        "repairable_without_definition_change": False,
    }


def _fingerprint_parity(rows: list[dict[str, Any]]) -> str:
    lines = []
    for r in sorted(rows, key=lambda x: x.get("event_id") or ""):
        lines.append(f"{r.get('event_id')}|{r.get('parity_class')}|{r.get('critical')}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _run_event_reconstructions(
    *,
    ha: list[dict[str, Any]],
    raw_root: Path,
    query_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cfg = TrapAcceptConfig()
    thr = JoinThresholds()
    dthr = DisambiguationThresholds()
    thr_accept = JoinThresholds(accept_confidence=dthr.accept_confidence)

    by_src_hour: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ha:
        by_src_hour[r.get("source_hour") or "unknown"].append(r)

    parity_rows: list[dict[str, Any]] = []
    hours = sorted(by_src_hour.keys())

    for hi, hour in enumerate(hours):
        print(f"parity hour {hi+1}/{len(hours)} {hour} n={len(by_src_hour[hour])}", flush=True)
        ht = parse_utc(hour)
        event_start, event_end = ht, ht + timedelta(hours=1)
        ob_start = ht - timedelta(hours=1)
        data_end = event_end + timedelta(seconds=3600)
        samples_by, _, n_ok = load_ob200_samples(
            symbols=("BTCUSDT", "DOGEUSDT"),
            start=ob_start,
            end=event_end,
            raw_root=raw_root,
            sample_ms=250,
        )
        edges, _, _ = build_causal_edges_from_samples(samples_by)
        if n_ok == 0:
            for r in by_src_hour[hour]:
                parity_rows.append(
                    {
                        "event_id": r["event_id"],
                        "symbol": r["symbol"],
                        "source_hour": hour,
                        "parity_class": "SOURCE_GAP",
                        "critical": True,
                        "reason": "no_ob200_segments",
                        "first_mismatch_field": "ob200",
                        "full_value": None,
                        "prefix_value": None,
                        "lookahead_relevant": True,
                        "repairable_without_definition_change": False,
                        "decision_ts": r.get("decision_ts"),
                        "delta_ms": None,
                    }
                )
            continue

        trades_cache: dict[str, Any] = {}
        buckets_cache: dict[str, Any] = {}
        for sym in sorted({r["symbol"] for r in by_src_hour[hour]}):
            trades, _ = load_trades_clickhouse(
                symbol=sym, start=event_start, end=data_end, query_log=query_log
            )
            trades_cache[sym] = trades
            buckets_cache[sym] = build_second_buckets(trades)

        for r in by_src_hour[hour]:
            sym = r["symbol"]
            trades = trades_cache.get(sym) or []
            buckets = buckets_cache.get(sym) or {}
            samples = samples_by.get(sym) or []
            sym_edges = [e for e in edges if e.symbol == sym]
            dts = parse_utc(r["decision_ts"])
            legacy_first = first_acceptance_available_ts(r, dts)
            stored_cp_first = accepted_state_first_ts_from_checkpoints(r, dts)

            ev = _event_from_row(r)
            side = aggressor_side(ev.direction)
            flow = aggregate_window(buckets, ev.flow_start_ts, ev.flow_end_ts)
            vwap = side_vwap(trades, ev.flow_start_ts, ev.flow_end_ts, side)
            cands = evaluate_candidates(
                ev,
                sym_edges,
                samples,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=thr,
            )
            join, _, _ = select_disambiguated_match(
                ev,
                cands,
                trades=trades,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=thr,
                dthr=dthr,
            )
            ev2 = apply_join_to_event(deepcopy(ev), join, thr_accept)

            feat_full, _ = process_event(
                ev2, buckets=buckets, trades=trades, cfg=cfg, data_end=data_end
            )
            # Causal lock via frozen evaluate_edge_acceptance as_of scan (no source edit).
            lock_ts = None
            if feat_full.get("final_acceptance_state") in ACCEPTED and ev2.edge_price is not None:
                side = aggressor_side(ev2.direction)
                for sec in range(1, 61):
                    cut = dts + timedelta(seconds=sec)
                    acc_cut = evaluate_edge_acceptance(
                        buckets=buckets,
                        trades=trades,
                        symbol=sym,
                        wall_side=ev2.wall_side,
                        edge_price=ev2.edge_price,
                        edge_confidence=ev2.edge_confidence or "high",
                        decision_ts=dts,
                        aggressor_side=side or "Buy",
                        cfg=cfg,
                        as_of=cut,
                    )
                    if acc_cut.get("final_acceptance_state") in ACCEPTED:
                        lock_ts = cut
                        break
            if lock_ts is None and stored_cp_first is not None:
                lock_ts = stored_cp_first

            if lock_ts is None:
                out = {
                    "event_id": r["event_id"],
                    "symbol": sym,
                    "source_hour": hour,
                    "parity_class": "INSUFFICIENT_PREFIX_WARMUP",
                    "critical": True,
                    "reason": "no_accepted_lock_ts",
                    "first_mismatch_field": "first_accepted_lock_ts",
                    "full_value": r.get("final_acceptance_state"),
                    "prefix_value": None,
                    "decision_ts": r.get("decision_ts"),
                    "full_decision_ts": feat_full.get("decision_ts"),
                    "prefix_decision_ts": None,
                    "delta_ms": None,
                    "legacy_first_ts": iso_z(legacy_first) if legacy_first else None,
                    "stored_checkpoint_accepted_first": iso_z(stored_cp_first) if stored_cp_first else None,
                    "lookahead_relevant": True,
                    "repairable_without_definition_change": False,
                    "stored_acceptance": r.get("final_acceptance_state"),
                    "full_acceptance": feat_full.get("final_acceptance_state"),
                    "prefix_acceptance": None,
                }
                parity_rows.append(out)
                continue

            # Prefix: truncated trades/buckets at lock; future injection = full trades + as_of
            from datetime import timezone as _tz

            trades_trunc = []
            for t in trades:
                ts = t.trade_ts
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
                if ts <= lock_ts:
                    trades_trunc.append(t)
            buckets_trunc = build_second_buckets(trades_trunc)
            feat_pref, _ = process_event(
                ev2,
                buckets=buckets_trunc,
                trades=trades_trunc,
                cfg=cfg,
                as_of=lock_ts,
                data_end=lock_ts,
            )
            feat_future, _ = process_event(
                ev2, buckets=buckets, trades=trades, cfg=cfg, as_of=lock_ts, data_end=lock_ts
            )

            pre_cut = lock_ts - timedelta(milliseconds=1)
            feat_pre, _ = process_event(
                ev2,
                buckets=buckets_trunc,
                trades=trades_trunc,
                cfg=cfg,
                as_of=pre_cut,
                data_end=pre_cut,
            )

            cls = classify_parity(
                stored=r,
                feat_full=feat_full,
                feat_pref=feat_pref,
                feat_pre=feat_pre,
                feat_future_inj=feat_future,
                join_edge_id=join.matched_edge_id,
                join_edge_px=join.matched_edge_price,
                lock_ts=lock_ts,
            )

            delta_ms = None
            if legacy_first and lock_ts:
                delta_ms = round((lock_ts - legacy_first).total_seconds() * 1000.0, 3)

            parity_rows.append(
                {
                    "event_id": r["event_id"],
                    "symbol": sym,
                    "source_hour": hour,
                    "decision_ts": r.get("decision_ts"),
                    "full_decision_ts": feat_full.get("decision_ts"),
                    "prefix_decision_ts": feat_pref.get("decision_ts"),
                    "delta_ms": delta_ms,
                    "legacy_first_ts": iso_z(legacy_first) if legacy_first else None,
                    "stored_checkpoint_accepted_first": iso_z(stored_cp_first) if stored_cp_first else None,
                    "earliest_causal_entry_ts": iso_z(lock_ts),
                    "stored_acceptance": r.get("final_acceptance_state"),
                    "full_acceptance": feat_full.get("final_acceptance_state"),
                    "prefix_acceptance": feat_pref.get("final_acceptance_state"),
                    "pre_acceptance": feat_pre.get("final_acceptance_state"),
                    "matched_edge_id_stored": r.get("matched_edge_id"),
                    "matched_edge_id_full": feat_full.get("matched_edge_id"),
                    "matched_edge_id_prefix": feat_pref.get("matched_edge_id"),
                    "confidence_stored": r.get("edge_match_confidence_class"),
                    "confidence_full": feat_full.get("edge_match_confidence_class"),
                    **cls,
                }
            )
    return parity_rows


def run_prefix_parity_audit(
    *,
    expansion_dir: Path = EXPANSION_DIR,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    max_events: Optional[int] = None,
    repeat: int = 2,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)
    corrected_dir = output_dir / "corrected_recompute"
    ensure_outdir(corrected_dir)

    freeze_verify_dir = PRIOR_FREEZE
    if (expansion_dir / "freeze_bundle" / "frozen_hashes.json").is_file():
        # Verify against original freeze source of truth
        freeze_verify_dir = PRIOR_FREEZE

    try:
        before = _verify_or_tamper(freeze_verify_dir, "before")
    except FrozenBundleTampered:
        raise
    write_json(output_dir / "freeze_verification_before.json", before)
    _copy_freeze_manifests(freeze_verify_dir, output_dir / "freeze_bundle")

    # --- A. Prior metric semantics ---
    dq = json.loads((expansion_dir / "data_quality_report.json").read_text())
    prior_prefix = dq.get("prefix_parity") or {}
    prior_metric = {
        "metric_name_reported": "prefix_ts_frac_ok / frac_ok",
        "actual_technical_meaning": (
            "NOT Full-vs-Prefix event parity on the 1679 cohort. "
            "Fraction of a SAMPLE (first 20 HIGH∩ACCEPTED) for which "
            "first_acceptance_available_ts could be recovered from checkpoints "
            "(any state ∉ {UNKNOWN_EDGE, UNKNOWN_DATA}) or flat acceptance_state_at_*. "
            "Gate: frac_ok >= 0.80. Mode: acceptance_ts_consistency_ge_80pct(_recomputed)."
        ),
        "source_file": "sample_expansion_runner.py::_prefix_parity_checks",
        "numerator": prior_prefix.get("n_ok"),
        "denominator": prior_prefix.get("n_checked"),
        "frac_ok": prior_prefix.get("frac_ok"),
        "denominator_is_primary_cohort_1679": False,
        "checks_full_window_vs_prefix_reconstruction": False,
        "checks_outcome_horizon_coverage": False,
        "correct_rename": "acceptance_first_ts_recoverability_sample_frac",
        "root_cause_of_non_1_0": (
            "edge_acceptance skipped checkpoint emission when the 1s trade bucket at "
            "checkpoint wall-clock was empty, filling UNKNOWN_DATA via incomplete_scan "
            "while final_acceptance_state could still be ACCEPTED_*. "
            "A causal fix requires editing frozen source edge_acceptance.py "
            "(hashed in freeze_bundle) → FREEZE_CHANGE_REQUIRED. No fix applied in this audit."
        ),
        "freeze_includes_source_hashes": True,
        "corrective_edit_blocked_by_freeze": True,
        "mode": prior_prefix.get("mode"),
        "gate_threshold": 0.80,
        "timestamp_tolerance_ms_declared": None,
    }
    write_json(output_dir / "prior_metric_semantics.json", prior_metric)

    ha = _load_csv(expansion_dir / "high_accepted_events.csv")
    all_ev = _load_csv(expansion_dir / "frozen_events.csv")
    if max_events is not None:
        ha = ha[:max_events]

    inventory = []
    for r in ha:
        dts = parse_utc(r["decision_ts"])
        legacy_ts = first_acceptance_available_ts(r, dts)
        acc_ts = accepted_state_first_ts_from_checkpoints(r, dts)
        inventory.append(
            {
                "event_id": r["event_id"],
                "symbol": r["symbol"],
                "source_hour": r.get("source_hour"),
                "utc_day": r.get("utc_day"),
                "direction": r.get("direction"),
                "final_acceptance_state": r.get("final_acceptance_state"),
                "matched_edge_id": r.get("matched_edge_id"),
                "matched_edge_price": r.get("matched_edge_price"),
                "decision_ts": r.get("decision_ts"),
                "legacy_acceptance_first_available_ts": iso_z(legacy_ts) if legacy_ts else None,
                "accepted_state_first_ts_from_stored_checkpoints": iso_z(acc_ts) if acc_ts else None,
                "legacy_equals_accepted_first": (
                    iso_z(legacy_ts) == iso_z(acc_ts) if legacy_ts and acc_ts else False
                ),
                "has_accepted_first_in_stored_checkpoints": acc_ts is not None,
            }
        )
    write_csv(output_dir / "primary_cohort_inventory.csv", inventory)

    n_missing_stored_cp = sum(1 for x in inventory if not x["has_accepted_first_in_stored_checkpoints"])
    n_legacy_diff = sum(
        1
        for x in inventory
        if x["has_accepted_first_in_stored_checkpoints"]
        and x["legacy_acceptance_first_available_ts"]
        != x["accepted_state_first_ts_from_stored_checkpoints"]
    )

    # --- Density / duplicates ---
    by_hour = Counter(r.get("source_hour") for r in ha)
    write_csv(
        output_dir / "event_density_by_hour.csv",
        [{"source_hour": h, "n_raw_accepted": c} for h, c in sorted(by_hour.items())],
    )
    by_minute: Counter = Counter()
    for r in ha:
        dts = parse_utc(r["decision_ts"])
        by_minute[dts.strftime("%Y-%m-%dT%H:%M:00Z")] += 1
    write_csv(
        output_dir / "event_density_by_minute.csv",
        [{"minute": m, "n_raw_accepted": c} for m, c in sorted(by_minute.items())],
    )

    groups: dict[tuple, list] = defaultdict(list)
    for r in ha:
        key = (r["symbol"], r.get("matched_edge_id"), r.get("final_acceptance_state"))
        groups[key].append(parse_utc(r["decision_ts"]))
    dup_rows = []
    n_episodes = 0
    for key, tss in groups.items():
        tss = sorted(tss)
        if not tss:
            continue
        episodes = 1
        for a, b in zip(tss, tss[1:]):
            gap = (b - a).total_seconds()
            dup_rows.append(
                {
                    "symbol": key[0],
                    "matched_edge_id": key[1],
                    "acceptance_state": key[2],
                    "gap_s": gap,
                    "within_diag_60s": gap < DIAG_GAP_S,
                }
            )
            if gap >= DIAG_GAP_S:
                episodes += 1
        n_episodes += episodes
    write_csv(output_dir / "duplicate_event_audit.csv", dup_rows)
    indep = {
        "raw_accepted_event_rows": len(ha),
        "unique_event_ids": len({r["event_id"] for r in ha}),
        "unique_edge_ids": len({r.get("matched_edge_id") for r in ha}),
        "diagnostic_independent_episodes_gap_ge_60s": n_episodes,
        "diagnostic_gap_s": DIAG_GAP_S,
        "diagnostic_gap_is_frozen_contract_rule": False,
        "frozen_acceptance_episode_cooldown_exists": False,
        "note": (
            "60s gap is diagnostic only. Freeze has no acceptance-episode cooldown. "
            "AEF discovery cooldown is separate and does not dedupe acceptance rows."
        ),
        "n_gaps_lt_60s": sum(1 for d in dup_rows if d["within_diag_60s"]),
        "event_id_uniqueness_ok": len(ha) == len({r["event_id"] for r in ha}),
        "episode_contract_unresolved": True,
    }
    write_json(output_dir / "independent_episode_summary.json", indep)
    write_csv(output_dir / "independent_episode_summary.csv", [indep])

    # --- DOGE funnel ---
    doge = [r for r in all_ev if r.get("symbol") == "DOGEUSDT"]
    doge_funnel = {
        "n_aef_events": len(doge),
        "EDGE_NOT_REACHED": sum(1 for r in doge if r.get("edge_join_status") == "EDGE_NOT_REACHED"),
        "MULTIPLE_EDGE_AMBIGUOUS": sum(
            1 for r in doge if r.get("edge_join_status") == "MULTIPLE_EDGE_AMBIGUOUS"
        ),
        "EXACT_TRADED_EDGE": sum(1 for r in doge if r.get("edge_join_status") == "EXACT_TRADED_EDGE"),
        "reached_any": sum(
            1
            for r in doge
            if r.get("edge_join_status")
            not in {None, "EDGE_NOT_REACHED", "NO_EDGE_CANDIDATE", "UNKNOWN"}
        ),
        "HIGH": sum(1 for r in doge if r.get("edge_match_confidence_class") == "HIGH"),
        "MEDIUM": sum(1 for r in doge if r.get("edge_match_confidence_class") == "MEDIUM"),
        "LOW": sum(1 for r in doge if r.get("edge_match_confidence_class") == "LOW"),
        "NONE": sum(1 for r in doge if r.get("edge_match_confidence_class") == "NONE"),
        "ACCEPTED_ABOVE": sum(1 for r in doge if r.get("final_acceptance_state") == "ACCEPTED_ABOVE"),
        "ACCEPTED_BELOW": sum(1 for r in doge if r.get("final_acceptance_state") == "ACCEPTED_BELOW"),
        "HIGH_ACCEPTED_ANY": sum(
            1
            for r in doge
            if r.get("edge_match_confidence_class") == "HIGH"
            and r.get("final_acceptance_state") in ACCEPTED
        ),
        "UNKNOWN_EDGE": sum(1 for r in doge if r.get("final_acceptance_state") == "UNKNOWN_EDGE"),
        "null_cause": (
            "Almost all DOGE AEF events are EDGE_NOT_REACHED under frozen reach/side rules; "
            "no HIGH∩ACCEPTED. Not a DOGE-specific threshold; coverage/reach, not BTC constant."
        ),
    }
    write_csv(output_dir / "doge_funnel.csv", [doge_funnel])
    excl = Counter(r.get("edge_join_status") for r in doge)
    write_csv(
        output_dir / "doge_exclusion_reasons.csv",
        [
            {
                "reason": k,
                "n": v,
                "note": "frozen join status; no DOGE-specific threshold introduced",
            }
            for k, v in excl.items()
        ],
    )

    # --- B/C reconstructions (repeat for reproducibility) ---
    query_log: list[dict[str, Any]] = []
    fingerprints = []
    parity_rows: list[dict[str, Any]] = []
    for run_i in range(max(1, repeat)):
        print(f"=== parity reconstruction pass {run_i+1}/{repeat} ===", flush=True)
        # Fresh query_log per pass only on first; second pass reuses same logic
        qlog: list[dict[str, Any]] = []
        rows = _run_event_reconstructions(ha=ha, raw_root=raw_root, query_log=qlog)
        fp = _fingerprint_parity(rows)
        fingerprints.append(fp)
        if run_i == 0:
            parity_rows = rows
            query_log = qlog
        else:
            if fp != fingerprints[0]:
                write_json(
                    output_dir / "reproducibility_check.json",
                    {"ok": False, "fingerprints": fingerprints, "error": "pass_mismatch"},
                )
                raise RuntimeError("parity audit not reproducible across passes")

    class_counts = Counter(r.get("parity_class") for r in parity_rows)
    mismatch_rows = [r for r in parity_rows if r.get("parity_class") != "EXACT_PARITY"]
    critical_rows = [r for r in parity_rows if r.get("critical")]
    field_counts = Counter(
        r.get("first_mismatch_field") for r in mismatch_rows if r.get("first_mismatch_field")
    )

    write_csv(output_dir / "event_prefix_parity.csv", parity_rows)
    write_csv(corrected_dir / "event_prefix_parity.csv", parity_rows)
    write_csv(output_dir / "parity_mismatches.csv", mismatch_rows)
    write_csv(
        output_dir / "parity_class_summary.csv",
        [{"parity_class": k, "n": v} for k, v in sorted(class_counts.items())],
    )
    write_csv(
        output_dir / "field_mismatch_summary.csv",
        [{"field": k, "n": v} for k, v in sorted(field_counts.items())],
    )
    write_csv(output_dir / "critical_lookahead_cases.csv", critical_rows)

    n_exact = class_counts.get("EXACT_PARITY", 0)
    n_tol = class_counts.get("TIMESTAMP_TOLERANCE_ONLY", 0)
    n_crit = len(critical_rows)
    n_checked = len(parity_rows)

    after = _verify_or_tamper(freeze_verify_dir, "after")
    write_json(output_dir / "freeze_verification_after.json", after)
    if after.get("freeze_bundle_sha256") != before.get("freeze_bundle_sha256"):
        raise FrozenBundleTampered("freeze changed during audit")

    repro = {
        "ok": len(set(fingerprints)) == 1 and n_checked == len(ha),
        "n_passes": repeat,
        "fingerprints": fingerprints,
        "n_events": n_checked,
        "class_counts": dict(class_counts),
    }
    write_json(output_dir / "reproducibility_check.json", repro)

    # Earliest causal entry distribution
    entry_ts_list = [
        r.get("earliest_causal_entry_ts")
        for r in parity_rows
        if r.get("earliest_causal_entry_ts")
    ]

    # Verdict: checkpoint recoverability bug is freeze-hashed source defect;
    # episode contract missing; either alone blocks entry timing.
    parity_clean = n_crit == 0 and n_exact + n_tol == n_checked and n_checked == len(ha)
    freeze_change_needed = True  # frozen edge_acceptance empty-bucket checkpoint bug (hashed source)

    if before.get("freeze_bundle_sha256") != after.get("freeze_bundle_sha256"):
        verdict = "FROZEN_BUNDLE_TAMPERED"
        entry = "ENTRY_TIMING_BLOCKED"
    elif not parity_clean:
        verdict = "FROZEN_HIGH_ACCEPTED_PREFIX_PARITY_AUDIT_V1_BLOCKED"
        entry = "ENTRY_TIMING_BLOCKED"
    elif freeze_change_needed:
        verdict = "FROZEN_HIGH_ACCEPTED_PREFIX_PARITY_AUDIT_V1_FREEZE_CHANGE_REQUIRED"
        entry = "ENTRY_TIMING_BLOCKED"
    else:
        verdict = "FROZEN_HIGH_ACCEPTED_PREFIX_PARITY_AUDIT_V1_PASS"
        entry = "ENTRY_TIMING_ALLOWED"

    readiness = {
        "entry_timing": entry,
        "verdict": verdict,
        "n_checked": n_checked,
        "n_exact_parity": n_exact,
        "n_timestamp_tolerance_only": n_tol,
        "n_critical": n_crit,
        "n_missing_accepted_in_stored_checkpoints": n_missing_stored_cp,
        "n_legacy_vs_accepted_checkpoint_diff": n_legacy_diff,
        "prior_frac_ok_was_not_full_parity": True,
        "raw_rows": len(ha),
        "diagnostic_episodes": n_episodes,
        "earliest_causal_entry_ts_n": len(entry_ts_list),
        "minimal_invasive_correction": (
            "none applied — empty-bucket checkpoint emission fix would change "
            "frozen source hash of edge_acceptance.py → FREEZE_CHANGE_REQUIRED. "
            "Audit uses as_of second-scan for earliest ACCEPTED lock without editing freeze modules."
        ),
        **NO_FIT,
    }
    write_json(output_dir / "entry_timing_readiness.json", readiness)

    elapsed = time.perf_counter() - t0
    summary = {
        "verdict": verdict,
        "entry_timing": entry,
        **NO_FIT,
        "freeze_bundle_sha256_before": before.get("freeze_bundle_sha256"),
        "freeze_bundle_sha256_after": after.get("freeze_bundle_sha256"),
        "prior_metric": prior_metric,
        "n_primary_raw": len(ha),
        "n_checked": n_checked,
        "parity_class_counts": dict(class_counts),
        "n_exact_parity": n_exact,
        "n_timestamp_tolerance_only": n_tol,
        "n_critical_lookahead": n_crit,
        "n_missing_accepted_in_stored_checkpoints": n_missing_stored_cp,
        "n_legacy_metric_differs_from_accepted_checkpoint": n_legacy_diff,
        "independent_episode_summary": indep,
        "doge_funnel": doge_funnel,
        "minimal_invasive_corrections": readiness["minimal_invasive_correction"],
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "reproducibility": repro,
        "timestamp_tolerance_ms": TIMESTAMP_TOLERANCE_MS,
    }
    write_json(output_dir / "verdict.json", summary)
    write_json(output_dir / "SUMMARY.json", summary)
    write_json(
        output_dir / "run_manifest.json",
        {
            **NO_FIT,
            "expansion_dir": str(expansion_dir),
            "hours_audited": sorted(by_hour.keys()),
            "elapsed_s": round(elapsed, 3),
            "query_count": len(query_log),
            "repeat": repeat,
            "max_events": max_events,
        },
    )

    _write_abschlussbericht(output_dir, summary, readiness, indep, doge_funnel, class_counts)
    return summary


def _write_abschlussbericht(
    output_dir: Path,
    summary: dict[str, Any],
    readiness: dict[str, Any],
    indep: dict[str, Any],
    doge_funnel: dict[str, Any],
    class_counts: Counter,
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

    other = {k: v for k, v in class_counts.items() if k not in {"EXACT_PARITY", "TIMESTAMP_TOLERANCE_ONLY"}}
    lines = [
        "# ABSCHLUSSBERICHT — FROZEN_HIGH_ACCEPTED_PREFIX_PARITY_AUDIT_V1",
        "",
        f"1. VERDICT: `{summary['verdict']}`",
        "2. Live-Sicherheit: keine Collector-/Prozessänderung, keine ClickHouse-Writes, read-only Freeze-Verify",
        f"3. Branch / HEAD / Dirty: `{branch}` / `{head}` / dirty={dirty}",
        f"4. Freeze SHA vor/nach: `{summary.get('freeze_bundle_sha256_before')}` / `{summary.get('freeze_bundle_sha256_after')}`",
        "5. Bedeutung der bisherigen 0.90-Metrik: "
        + str((summary.get("prior_metric") or {}).get("actual_technical_meaning")),
        f"6. Bisheriger Zähler/Nenner: n_ok={ (summary.get('prior_metric') or {}).get('numerator') } / "
        f"n_checked={ (summary.get('prior_metric') or {}).get('denominator') } "
        f"(Sample 20, nicht 1679; keine Full↔Prefix-Eventparität)",
        f"7. Geprüfte Rohereignisse: {summary.get('n_primary_raw')}",
        f"8. Unabhängige Episoden (diagnostisch gap≥60s, nicht eingefroren): "
        f"{indep.get('diagnostic_independent_episodes_gap_ge_60s')} "
        f"(raw rows={indep.get('raw_accepted_event_rows')})",
        f"9. EXACT_PARITY: {summary.get('n_exact_parity')}",
        f"10. TIMESTAMP_TOLERANCE_ONLY: {summary.get('n_timestamp_tolerance_only')}",
        f"11. Übrige Paritätsklassen: {json.dumps(other, sort_keys=True)}",
        f"12. Kritische Lookahead-Fälle: {summary.get('n_critical_lookahead')}",
        "13. Frühester kausaler Entry-Zeitpunkt: `first_accepted_lock_ts` "
        "(Scan-Lock ACCEPTED_*; siehe event_prefix_parity.csv Spalte earliest_causal_entry_ts)",
        f"14. Event-Dichte/Duplikate: unique_edges={indep.get('unique_edge_ids')}, "
        f"gaps<60s={indep.get('n_gaps_lt_60s')}, "
        f"episode_contract_unresolved={indep.get('episode_contract_unresolved')}",
        f"15. DOGE-Funnel: {json.dumps(doge_funnel, sort_keys=True)}",
        f"16. Minimal-invasive Korrekturen: {summary.get('minimal_invasive_corrections')}",
        f"17. No-Fit-Flags: {json.dumps(NO_FIT)}",
        f"18. Reproduzierbarkeit: {json.dumps(summary.get('reproducibility'))}",
        "19. Tests: tests/test_frozen_high_accepted_prefix_parity_audit_v1.py",
        f"20. Entscheidung: `{readiness.get('entry_timing')}`",
        "",
        "## Nächster Schritt",
        (
            "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1"
            if readiness.get("entry_timing") == "ENTRY_TIMING_ALLOWED"
            else "Kein Entry-Backtest. Zuerst Fix-/Refreeze-Plan (Episode-Contract und/oder Paritätsblocker)."
        ),
        "",
    ]
    (output_dir / "ABSCHLUSSBERICHT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--expansion-dir", type=Path, default=EXPANSION_DIR)
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--repeat", type=int, default=2)
    args = p.parse_args()
    s = run_prefix_parity_audit(
        expansion_dir=args.expansion_dir,
        output_dir=args.output_dir,
        max_events=args.max_events,
        repeat=args.repeat,
    )
    print(s["verdict"], s.get("entry_timing"), s.get("n_exact_parity"), "/", s.get("n_checked"))
