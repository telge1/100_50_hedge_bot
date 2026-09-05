"""Build / verify outcome-blind LP Entry Contract expansion freeze v1."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1 import (
    CASE_SEQUENCE_FREEZE_SHA256,
    ENTRY_CONTRACT_FREEZE_SHA256,
    FORBIDDEN_FIELD_SUBSTR,
    OUTCOME_LIKE_SOURCE_COLUMNS,
    RESULTS_DIR_REL,
    SAMPLING_SEED,
    SCHEMA_VERSION,
    STRATEGY_CONFIG_REL,
    STRATEGY_CONFIG_SHA256,
    TARGET_COUNT,
)
from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1.coverage import (
    DEFAULT_RAW_ROOT,
    estimate_coverage_batch,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    MAX_POST_START_S,
    PRE_START_S,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments

V2_MONITOR_DIR = "results/liquidity_pool_arrival_wall_monitor_v2"
PRIMARY_SOURCE_REL = f"{V2_MONITOR_DIR}/pool_arrivals_v2.csv"
V1_SOURCE_REL = "results/liquidity_pool_arrival_internal_wall_monitor_v1/pool_arrival_episodes.csv"
CASE_SEQUENCE_REL = "results/liquidity_pool_case_sequence_freeze_v1/frozen_case_sequence_v1.json"
SIX_CASE_SAMPLE_REL = "results/liquidity_pool_six_case_wall_trade_reaction_sample_v1"
TIMEFRAMES = ("5m", "15m", "30m", "1h")
SYMBOL_SYMBOLS = ("BTCUSDT", "DOGEUSDT")

DEEP_AUDIT_ROOTS = (
    "results/case_02_pool_edge_aggressor_efficiency_timeline_v1",
    "results/case_02_control_shift_timestamp_review_v1",
    "results/post_case_02_next_pool_causal_reaction_audit_v1",
    "results/ask_pool_022736_wall_public_trade_reaction_audit_v1",
    "results/case_03_frozen_bid_pool_causal_reaction_audit_v1",
    "results/case_04_frozen_bid_pool_causal_reaction_audit_v1",
    "results/case_05_frozen_bid_pool_entry_contract_v1_audit",
    SIX_CASE_SAMPLE_REL,
)

MANUAL_REFERENCE_CLUSTERS = (
    "090cd76d57d64cbd89a7",  # known 00:07:15 / 00:07:16 manual review
)

EMBARGO_BASE_MINUTES = 30
EPISODE_PRE_S = PRE_START_S
EPISODE_POST_S = MAX_POST_START_S
OVERLAP_SAME_SYMBOL_S = 300
EXPANSION_CASE_RE = re.compile(r"^EXP_(\\d{2})$")


class ExpansionFreezeError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if not isinstance(ts, str) or not ts.endswith("Z"):
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", f"non-UTC-Z: {ts!r}")
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in keys})


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def selection_hash(
    *,
    source_sha256: str,
    candidate_id: str,
    pool_id: str,
    reference_ts: str,
    seed: str = SAMPLING_SEED,
) -> str:
    payload = f"{source_sha256}{candidate_id}{pool_id}{reference_ts}{seed}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def embargo_policy() -> dict[str, Any]:
    episode_radius_s = max(EPISODE_PRE_S, EPISODE_POST_S)
    embargo_s = max(EMBARGO_BASE_MINUTES * 60, episode_radius_s)
    return {
        "symmetric_embargo_seconds": embargo_s,
        "symmetric_embargo_minutes": embargo_s / 60.0,
        "base_minutes": EMBARGO_BASE_MINUTES,
        "episode_pre_s": EPISODE_PRE_S,
        "episode_post_s": EPISODE_POST_S,
        "rule": "max(±30min, episode_pre_s, episode_post_s) symmetric around exposed reference_ts",
        "persisted_before_selection": True,
    }


@dataclass(frozen=True)
class SourceRecord:
    path_relative: str
    path_absolute: str
    sha256: str
    schema: str
    symbols: list[str]
    time_window: dict[str, str | None]
    candidate_count: int
    has_pool_id: bool
    has_reference_ts: bool
    has_direction: bool
    has_approach: bool
    has_first_causal_availability: bool
    outcome_fields_present: list[str]
    manual_selection_used: bool
    granularity: str
    superseded_by: str | None
    no_outcomes_flag: bool | None


def _csv_inventory(path: Path, repo_root: Path) -> SourceRecord | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = list(reader.fieldnames or [])
    if not rows:
        return None
    colset = set(cols)
    id_col = next(
        (c for c in ("pool_arrival_id", "arrival_episode_id", "candidate_id") if c in colset),
        None,
    )
    ref_col = next(
        (c for c in ("arrival_ts", "reference_ts", "cluster_start_ts") if c in colset),
        None,
    )
    side_col = next((c for c in ("side", "pool_side", "direction") if c in colset), None)
    approach_col = next(
        (c for c in ("approach_direction", "approach") if c in colset),
        None,
    )
    avail_col = next(
        (c for c in ("available_at", "pool_first_available_ts", "first_available_ts") if c in colset),
        None,
    )
    pool_col = "pool_id" if "pool_id" in colset else None
    if not all([id_col, ref_col, side_col, approach_col, pool_col]):
        return None
    forbidden = sorted(
        c
        for c in cols
        if any(s in c.lower() for s in FORBIDDEN_FIELD_SUBSTR)
        or c in OUTCOME_LIKE_SOURCE_COLUMNS
    )
    symbols = sorted({str(r.get("symbol") or "").upper() for r in rows if r.get("symbol")})
    refs = [_utc(r[ref_col]) for r in rows if r.get(ref_col)]
    tw = {
        "start": _iso(min(refs)) if refs else None,
        "end": _iso(max(refs)) if refs else None,
    }
    rel = str(path.relative_to(repo_root))
    manual = rel.endswith("selection_manifest.json") or "six_case" in rel
    superseded = None
    if rel == V1_SOURCE_REL:
        superseded = PRIMARY_SOURCE_REL
    granularity = "pool_contact" if id_col in ("pool_arrival_id", "arrival_episode_id") else "cluster"
    no_outcomes = None
    dq = path.parent / "data_quality_report.json"
    if dq.is_file():
        try:
            no_outcomes = bool(json.loads(dq.read_text(encoding="utf-8")).get("no_outcomes"))
        except Exception:
            no_outcomes = None
    return SourceRecord(
        path_relative=rel,
        path_absolute=str(path.resolve()),
        sha256=sha256_file(path),
        schema=f"csv:{','.join(cols[:8])}...",
        symbols=symbols,
        time_window=tw,
        candidate_count=len(rows),
        has_pool_id=bool(pool_col),
        has_reference_ts=bool(ref_col),
        has_direction=bool(side_col),
        has_approach=bool(approach_col),
        has_first_causal_availability=bool(avail_col),
        outcome_fields_present=forbidden,
        manual_selection_used=manual,
        granularity=granularity,
        superseded_by=superseded,
        no_outcomes_flag=no_outcomes,
    )


def _json_inventory(path: Path, repo_root: Path) -> SourceRecord | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    cases = obj.get("cases")
    if not isinstance(cases, list) or not cases:
        return None
    cols: set[str] = set()
    for c in cases:
        if isinstance(c, dict):
            cols.update(c.keys())
    if not {"side", "approach_direction", "cluster_start_ts"} <= cols:
        return None
    forbidden = sorted(
        k for k in cols if any(s in k.lower() for s in FORBIDDEN_FIELD_SUBSTR)
    )
    refs = [_utc(str(c["cluster_start_ts"])) for c in cases if c.get("cluster_start_ts")]
    rel = str(path.relative_to(repo_root))
    return SourceRecord(
        path_relative=rel,
        path_absolute=str(path.resolve()),
        sha256=sha256_file(path),
        schema="json:selection_manifest_cases",
        symbols=["BTCUSDT"],
        time_window={
            "start": _iso(min(refs)) if refs else None,
            "end": _iso(max(refs)) if refs else None,
        },
        candidate_count=len(cases),
        has_pool_id="member_pool_ids" in cols,
        has_reference_ts=True,
        has_direction=True,
        has_approach=True,
        has_first_causal_availability=False,
        outcome_fields_present=forbidden,
        manual_selection_used=True,
        granularity="cluster",
        superseded_by=None,
        no_outcomes_flag=True,
    )


def inventory_candidate_sources(repo_root: Path) -> list[dict[str, Any]]:
    hits: list[SourceRecord] = []
    results = repo_root / "results"
    candidates = [
        results / "liquidity_pool_arrival_wall_monitor_v2" / "pool_arrivals_v2.csv",
        results / "liquidity_pool_arrival_internal_wall_monitor_v1" / "pool_arrival_episodes.csv",
        results / "liquidity_pool_arrival_wall_monitor_v2" / "market_arrival_clusters.csv",
        results / "l2_wall_to_wall_discovery" / "btc_doge_v1" / "entry_candidates.csv",
        results / "liquidity_pool_six_case_wall_trade_reaction_sample_v1" / "selection_manifest.json",
    ]
    for p in candidates:
        rec = _csv_inventory(p, repo_root) if p.suffix == ".csv" else _json_inventory(p, repo_root)
        if rec is not None:
            hits.append(rec)
    return [rec.__dict__ for rec in hits]


def resolve_unique_pool_contact_source(
    repo_root: Path, inventory: list[dict[str, Any]]
) -> dict[str, Any]:
    primary_path = repo_root / PRIMARY_SOURCE_REL
    if primary_path.is_file():
        dq_path = primary_path.parent / "data_quality_report.json"
        no_outcomes = False
        if dq_path.is_file():
            try:
                no_outcomes = bool(json.loads(dq_path.read_text(encoding="utf-8")).get("no_outcomes"))
            except Exception:
                no_outcomes = False
        primary_inv = next((s for s in inventory if s["path_relative"] == PRIMARY_SOURCE_REL), None)
        if primary_inv and no_outcomes:
            return {
                "verdict": "RESOLVED",
                "source": primary_inv,
                "resolution_rule": (
                    "locked v2 pool_arrivals_v2.csv with data_quality_report.no_outcomes=true; "
                    "supersedes v1 pool_arrival_episodes.csv per monitor v2 run_manifest"
                ),
                "competing_sources_excluded": [
                    s["path_relative"]
                    for s in inventory
                    if s["granularity"] == "pool_contact"
                    and s["path_relative"] != PRIMARY_SOURCE_REL
                ],
            }

    disqualifying_outcome = (
        "outcome",
        "verdict",
        "pnl",
        "mfe",
        "mae",
        "return_pnl",
        "reaction_class",
        "evidence_class",
    )
    pool_sources = []
    for s in inventory:
        if s["granularity"] != "pool_contact" or s["manual_selection_used"]:
            continue
        bad = [c for c in s["outcome_fields_present"] if any(x in c.lower() for x in disqualifying_outcome)]
        if bad:
            continue
        pool_sources.append(s)
    active = [s for s in pool_sources if not s.get("superseded_by")]
    if len(active) == 1:
        chosen = active[0]
        return {
            "verdict": "RESOLVED",
            "source": chosen,
            "resolution_rule": "single active pre-outcome pool-contact CSV",
            "competing_sources_excluded": [
                s["path_relative"]
                for s in pool_sources
                if s["path_relative"] != chosen["path_relative"]
            ],
        }
    raise ExpansionFreezeError(
        "EXPANSION_CANDIDATE_SOURCE_NOT_UNAMBIGUOUS",
        f"active pool-contact sources={[s['path_relative'] for s in active]}",
    )


def load_source_rows(repo_root: Path, source_rel: str) -> list[dict[str, str]]:
    path = repo_root / source_rel
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_pool_contact(row: dict[str, str], source_rel: str) -> dict[str, Any]:
    if source_rel.endswith("pool_arrivals_v2.csv"):
        cid = row["pool_arrival_id"]
        ref = row["arrival_ts"]
        avail = row["available_at"]
        tf = row.get("source_timeframe") or "5m"
        lo = row.get("lower_edge")
        hi = row.get("upper_edge")
        event_family = row.get("market_arrival_cluster_id") or cid
    else:
        cid = row["arrival_episode_id"]
        ref = row["arrival_ts"]
        avail = row["available_at"]
        tf = "5m"
        lo = row.get("lower_edge")
        hi = row.get("upper_edge")
        event_family = cid
    return {
        "source_candidate_id": cid,
        "symbol": str(row["symbol"]).upper(),
        "reference_ts": ref,
        "pool_id": row["pool_id"],
        "pool_side": row["side"],
        "approach": row["approach_direction"],
        "pool_timeframe": tf,
        "pool_lower_edge": float(lo) if lo not in (None, "") else None,
        "pool_upper_edge": float(hi) if hi not in (None, "") else None,
        "pool_first_available_ts": avail,
        "event_family_id": event_family,
        "market_arrival_cluster_id": row.get("market_arrival_cluster_id"),
    }


def _raw_ob_covers_window(symbol: str, start: datetime, end: datetime, raw_root: Path) -> bool:
    segs = list_closed_segments(raw_root, symbols=(symbol,), start=start, end=end)
    covered = 0
    need = int((end - start).total_seconds()) + 1
    for seg in segs:
        if seg.is_boundary_stub:
            continue
        overlap_start = max(start, seg.start_utc)
        overlap_end = min(end, seg.end_utc)
        if overlap_end > overlap_start:
            covered += int((overlap_end - overlap_start).total_seconds())
    return covered >= max(1, int(need * 0.85))


def _lld_timeframes_available(symbol: str, ref: datetime) -> bool:
    # Pool contacts carry 5m LLD ids; audit loads 5m/15m/30m/1h via chart backend.
    # Eligibility: symbol is in v2 monitor window with closed OB archive coverage.
    return symbol in SYMBOL_SYMBOLS or symbol == "BTCUSDT"


def build_eligible_universe(
    rows: list[dict[str, str]],
    *,
    source_rel: str,
    source_sha256: str,
    raw_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_key: set[tuple[str, str]] = set()
    for row in rows:
        try:
            c = parse_pool_contact(row, source_rel)
        except (KeyError, ValueError) as exc:
            rejected.append({"source_candidate_id": row.get("pool_arrival_id"), "reason": str(exc)})
            continue
        cid = c["source_candidate_id"]
        ref = c["reference_ts"]
        pool = c["pool_id"]
        side = c["pool_side"]
        approach = c["approach"]
        reasons: list[str] = []
        if not cid:
            reasons.append("missing_candidate_id")
        if not pool:
            reasons.append("missing_pool_id")
        if side not in ("ASK", "BID"):
            reasons.append("invalid_pool_side")
        if approach not in ("FROM_BELOW", "FROM_ABOVE"):
            reasons.append("invalid_approach")
        if (pool, ref) in seen_key:
            reasons.append("duplicate_pool_id_reference_ts")
        else:
            seen_key.add((pool, ref))
        try:
            ref_dt = _utc(ref)
            avail_dt = _utc(c["pool_first_available_ts"])
            if avail_dt > ref_dt:
                reasons.append("pool_not_causally_available_at_reference_ts")
        except Exception:
            reasons.append("invalid_timestamps")
        if reasons:
            rejected.append({"source_candidate_id": cid, "reason": ";".join(reasons), **c})
            continue
        win_start = ref_dt - timedelta(seconds=PRE_START_S)
        win_end = ref_dt + timedelta(seconds=MAX_POST_START_S)
        if not _raw_ob_covers_window(c["symbol"], win_start, win_end, raw_root):
            rejected.append({"source_candidate_id": cid, "reason": "raw_ob200_coverage_insufficient", **c})
            continue
        if not _lld_timeframes_available(c["symbol"], ref_dt):
            rejected.append({"source_candidate_id": cid, "reason": "lld_pack_coverage_missing", **c})
            continue
        c["deterministic_selection_hash"] = selection_hash(
            source_sha256=source_sha256,
            candidate_id=cid,
            pool_id=pool,
            reference_ts=ref,
        )
        eligible.append(c)
    return eligible, rejected


def load_exposed_cases(repo_root: Path) -> list[dict[str, Any]]:
    exposed: list[dict[str, Any]] = []
    seq_path = repo_root / CASE_SEQUENCE_REL
    if seq_path.is_file():
        seq = json.loads(seq_path.read_text(encoding="utf-8"))
        for c in seq.get("ordered_cases", []):
            exposed.append(
                {
                    "case_id": c["case_id"],
                    "reference_ts": c["reference_ts"],
                    "market_arrival_cluster_id": c.get("market_arrival_cluster_id"),
                    "exposure_reason": "CASE_SEQUENCE_FROZEN",
                    "symbol": c.get("symbol", "BTCUSDT"),
                }
            )
    for root_rel in DEEP_AUDIT_ROOTS:
        root = repo_root / root_rel
        if not root.is_dir():
            continue
        for name in ("frozen_case_input.json", "summary.json"):
            p = root / name
            if not p.is_file():
                continue
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            ref = obj.get("reference_ts") or obj.get("frozen_sample_cluster_start_ts")
            if ref:
                exposed.append(
                    {
                        "case_id": obj.get("case_id") or root_rel,
                        "reference_ts": ref,
                        "market_arrival_cluster_id": obj.get("market_arrival_cluster_id"),
                        "exposure_reason": f"DEEP_AUDIT:{root_rel}",
                        "symbol": obj.get("symbol", "BTCUSDT"),
                    }
                )
            break
    for cluster_id in MANUAL_REFERENCE_CLUSTERS:
        exposed.append(
            {
                "case_id": "MANUAL_REFERENCE",
                "reference_ts": None,
                "market_arrival_cluster_id": cluster_id,
                "exposure_reason": "MANUAL_REFERENCE_CASE",
                "symbol": "BTCUSDT",
            }
        )
    return exposed


def apply_exposure_exclusion(
    eligible: list[dict[str, Any]],
    exposed: list[dict[str, Any]],
    embargo: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    emb = timedelta(seconds=int(embargo["symmetric_embargo_seconds"]))
    emb_windows = []
    exposed_clusters: set[str] = set()
    exposed_pool_ref: set[tuple[str, str]] = set()
    for e in exposed:
        if e.get("market_arrival_cluster_id"):
            exposed_clusters.add(str(e["market_arrival_cluster_id"]))
        if e.get("reference_ts"):
            ref = _utc(str(e["reference_ts"]))
            emb_windows.append((ref - emb, ref + emb, e))
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for c in eligible:
        reasons: list[str] = []
        cluster = c.get("market_arrival_cluster_id") or c.get("event_family_id")
        if cluster and str(cluster) in exposed_clusters:
            reasons.append("exposed_cluster")
        key = (c["pool_id"], c["reference_ts"])
        if key in exposed_pool_ref:
            reasons.append("duplicate_exposed_pool_ref")
        ref = _utc(c["reference_ts"])
        for lo, hi, e in emb_windows:
            if lo <= ref <= hi:
                reasons.append(f"embargo:{e.get('case_id', 'EXPOSED')}")
                break
        if reasons:
            excluded.append({**c, "exclusion_reason": ";".join(reasons), "exposure_status": "EXCLUDED"})
        else:
            kept.append(c)
    return kept, excluded


def stratified_select(
    eligible: list[dict[str, Any]],
    *,
    target: int = TARGET_COUNT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ask = sorted([c for c in eligible if c["pool_side"] == "ASK"], key=lambda x: x["deterministic_selection_hash"])
    bid = sorted([c for c in eligible if c["pool_side"] == "BID"], key=lambda x: x["deterministic_selection_hash"])
    primary_target = target // 2
    strata_plan = {
        "primary": {"ASK": primary_target, "BID": primary_target},
        "secondary_symbol": {"BTCUSDT": target // 2, "DOGEUSDT": target // 2},
        "tertiary_timeframe": "proportional_with_min_2_per_present_tf",
    }
    selected: list[dict[str, Any]] = []
    deviations: list[str] = []
    picks = {"ASK": ask[:primary_target], "BID": bid[:primary_target]}
    if len(picks["ASK"]) < primary_target:
        deviations.append(f"ASK stratum short: wanted {primary_target}, got {len(picks['ASK'])}")
    if len(picks["BID"]) < primary_target:
        deviations.append(f"BID stratum short: wanted {primary_target}, got {len(picks['BID'])}")
    for side in ("ASK", "BID"):
        selected.extend(picks[side])
    if len(selected) < target:
        chosen_ids = {c["source_candidate_id"] for c in selected}
        remainder = sorted(
            [c for c in eligible if c["source_candidate_id"] not in chosen_ids],
            key=lambda x: x["deterministic_selection_hash"],
        )
        for c in remainder:
            if len(selected) >= target:
                break
            selected.append(c)
            deviations.append(f"redistributed_fill:{c['source_candidate_id']}")
    selected = sorted(selected, key=lambda x: x["deterministic_selection_hash"])[:target]
    symbol_counts = Counter(c["symbol"] for c in eligible)
    if "DOGEUSDT" not in symbol_counts:
        deviations.append("secondary_symbol: DOGEUSDT absent in eligible universe — BTCUSDT only")
    tf_counts = Counter(c["pool_timeframe"] for c in eligible)
    if len(tf_counts) == 1:
        deviations.append(
            f"tertiary_timeframe: single TF {next(iter(tf_counts))} — proportional allocation trivial"
        )
    meta = {
        "strata_plan": strata_plan,
        "eligible_strata_before": {
            "ASK": len(ask),
            "BID": len(bid),
            "symbols": dict(symbol_counts),
            "timeframes": dict(tf_counts),
        },
        "selected_strata_after": {
            "ASK": sum(1 for c in selected if c["pool_side"] == "ASK"),
            "BID": sum(1 for c in selected if c["pool_side"] == "BID"),
            "symbols": dict(Counter(c["symbol"] for c in selected)),
            "timeframes": dict(Counter(c["pool_timeframe"] for c in selected)),
        },
        "deviations": deviations,
    }
    return selected, meta


def dedup_selected(
    selected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in selected:
        by_family[str(c["event_family_id"])].append(c)
    family_retained: dict[str, str] = {}
    for fam, members in sorted(by_family.items()):
        ordered = sorted(members, key=lambda x: x["deterministic_selection_hash"])
        retained = ordered[0]
        family_retained[fam] = retained["source_candidate_id"]
        ex_members = ordered[1:]
        if ex_members:
            groups.append(
                {
                    "event_family_id": fam,
                    "duplicate_group_id": f"fam_{fam[:12]}",
                    "retained_candidate": retained["source_candidate_id"],
                    "excluded_candidates": [m["source_candidate_id"] for m in ex_members],
                    "exclusion_reason": "same_event_family_pool_id",
                }
            )
            excluded.extend(
                {
                    **m,
                    "exclusion_reason": "same_event_family_first_hash_wins",
                    "retained_candidate": retained["source_candidate_id"],
                }
                for m in ex_members
            )
        kept.append(retained)
    kept_sorted = sorted(kept, key=lambda x: x["deterministic_selection_hash"])
    # temporal overlap dedup same symbol
    final: list[dict[str, Any]] = []
    for c in kept_sorted:
        ref = _utc(c["reference_ts"])
        conflict = None
        for prev in final:
            if prev["symbol"] != c["symbol"]:
                continue
            dt = abs((ref - _utc(prev["reference_ts"])).total_seconds())
            if dt <= OVERLAP_SAME_SYMBOL_S:
                conflict = prev
                break
        if conflict:
            groups.append(
                {
                    "event_family_id": c["event_family_id"],
                    "duplicate_group_id": f"overlap_{c['source_candidate_id'][:8]}",
                    "retained_candidate": conflict["source_candidate_id"],
                    "excluded_candidates": [c["source_candidate_id"]],
                    "exclusion_reason": f"same_symbol_overlap_le_{OVERLAP_SAME_SYMBOL_S}s",
                }
            )
            excluded.append(
                {
                    **c,
                    "exclusion_reason": "temporal_overlap_same_symbol",
                    "retained_candidate": conflict["source_candidate_id"],
                }
            )
        else:
            final.append(c)
    return final, groups


def _assign_expansion_ids(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(cases, key=lambda x: x["deterministic_selection_hash"])
    out = []
    for i, c in enumerate(ordered, start=1):
        out.append(
            {
                "expansion_case_id": f"EXP_{i:02d}",
                **c,
                "exposure_status": "PROSPECTIVE_UNAUDITED",
            }
        )
    return out


def _next_after(cases: list[dict[str, Any]]) -> dict[str, str | None]:
    mapping: dict[str, str | None] = {}
    for i, c in enumerate(cases):
        cid = c["expansion_case_id"]
        mapping[cid] = cases[i + 1]["expansion_case_id"] if i + 1 < len(cases) else None
    return mapping


def build_expansion_freeze(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[3]
    out_dir = root / RESULTS_DIR_REL
    out_dir.mkdir(parents=True, exist_ok=True)

    inventory = inventory_candidate_sources(root)
    resolution = resolve_unique_pool_contact_source(root, inventory)
    source = resolution["source"]
    source_rel = source["path_relative"]
    source_sha = source["sha256"]

    raw_rows = load_source_rows(root, source_rel)
    eligible_all, eligibility_rejects = build_eligible_universe(
        raw_rows,
        source_rel=source_rel,
        source_sha256=source_sha,
        raw_root=root / DEFAULT_RAW_ROOT,
    )
    if len(eligible_all) < TARGET_COUNT:
        raise ExpansionFreezeError(
            "EXPANSION_ELIGIBLE_UNIVERSE_TOO_SMALL",
            f"eligible={len(eligible_all)} need={TARGET_COUNT}",
        )

    embargo = embargo_policy()
    exposed = load_exposed_cases(root)
    eligible, exposure_excluded = apply_exposure_exclusion(eligible_all, exposed, embargo)
    if len(eligible) < TARGET_COUNT:
        raise ExpansionFreezeError(
            "EXPANSION_ELIGIBLE_UNIVERSE_TOO_SMALL",
            f"post-exposure eligible={len(eligible)} need={TARGET_COUNT}",
        )

    pre_dedup, strat_meta = stratified_select(eligible, target=TARGET_COUNT)
    if len(pre_dedup) < TARGET_COUNT:
        raise ExpansionFreezeError(
            "EXPANSION_STRATIFICATION_NOT_FEASIBLE",
            f"stratified selected={len(pre_dedup)}",
        )
    deduped, dedup_groups_raw = dedup_selected(pre_dedup)
    dedup_groups = [g for g in dedup_groups_raw if "event_family_id" in g]
    if len(deduped) < TARGET_COUNT:
        # refill deterministically from eligible not yet chosen
        chosen = {c["source_candidate_id"] for c in deduped}
        refill = sorted(
            [c for c in eligible if c["source_candidate_id"] not in chosen],
            key=lambda x: x["deterministic_selection_hash"],
        )
        for c in refill:
            if len(deduped) >= TARGET_COUNT:
                break
            deduped.append(c)
        deduped = sorted(deduped, key=lambda x: x["deterministic_selection_hash"])[:TARGET_COUNT]
        strat_meta["deviations"].append("dedup_refill_applied")
    if len(deduped) != TARGET_COUNT:
        raise ExpansionFreezeError(
            "EXPANSION_STRATIFICATION_NOT_FEASIBLE",
            f"post-dedup count={len(deduped)}",
        )

    ordered_cases = _assign_expansion_ids(deduped)
    next_after = _next_after(ordered_cases)

    frozen_payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": _utc_now(),
        "source_manifest": {
            "path_relative": source_rel,
            "sha256": source_sha,
        },
        "sampling_seed": SAMPLING_SEED,
        "eligibility_policy": {
            "required_fields": [
                "source_candidate_id",
                "symbol",
                "reference_ts",
                "pool_id",
                "pool_side",
                "approach",
                "pool_first_available_ts",
            ],
            "causal_rules": [
                "pool_first_available_ts <= reference_ts",
                "raw_ob200_coverage >= 85% audit window",
                "public_trades_canonical assumed for v2 monitor window",
                "lld_packs 5m/15m/30m/1h via chart backend",
            ],
            "forbidden_for_eligibility": [
                "acceptance",
                "micro_pass",
                "room_pass",
                "contest",
                "outcome",
                "return",
                "pnl",
                "manual_quality",
            ],
        },
        "exposure_embargo": embargo,
        "dedup_policy": {
            "same_event_family": "first deterministic hash wins",
            "same_symbol_overlap_s": OVERLAP_SAME_SYMBOL_S,
            "nested_tf_event_family": "market_arrival_cluster_id",
        },
        "stratification_policy": strat_meta["strata_plan"],
        "selected_count": len(ordered_cases),
        "ordered_cases": [
            {
                "expansion_case_id": c["expansion_case_id"],
                "source_candidate_id": c["source_candidate_id"],
                "symbol": c["symbol"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "pool_side": c["pool_side"],
                "approach": c["approach"],
                "pool_timeframe": c["pool_timeframe"],
                "pool_lower_edge": c.get("pool_lower_edge"),
                "pool_upper_edge": c.get("pool_upper_edge"),
                "pool_first_available_ts": c["pool_first_available_ts"],
                "event_family_id": c["event_family_id"],
                "exposure_status": c["exposure_status"],
                "deterministic_selection_hash": c["deterministic_selection_hash"],
            }
            for c in ordered_cases
        ],
        "next_after": next_after,
        "case_sequence_freeze_sha256": CASE_SEQUENCE_FREEZE_SHA256,
        "entry_contract_freeze_sha256": ENTRY_CONTRACT_FREEZE_SHA256,
        "strategy_config_sha256": STRATEGY_CONFIG_SHA256,
    }

    hash_payload = {k: v for k, v in frozen_payload.items() if k != "created_at_utc"}
    bundle_sha = sha256_bytes(canonical_json_bytes(hash_payload))

    write_csv(out_dir / "eligible_universe.csv", eligible)
    write_csv(out_dir / "excluded_exposed_cases.csv", exposure_excluded)
    write_csv(out_dir / "dedup_groups.csv", dedup_groups)
    write_csv(
        out_dir / "stratum_counts.csv",
        [
            {"stratum": "ASK", "eligible": strat_meta["eligible_strata_before"]["ASK"], "selected": strat_meta["selected_strata_after"]["ASK"]},
            {"stratum": "BID", "eligible": strat_meta["eligible_strata_before"]["BID"], "selected": strat_meta["selected_strata_after"]["BID"]},
            {"stratum": "symbol_BTCUSDT", "eligible": strat_meta["eligible_strata_before"]["symbols"].get("BTCUSDT", 0), "selected": strat_meta["selected_strata_after"]["symbols"].get("BTCUSDT", 0)},
            {"stratum": "symbol_DOGEUSDT", "eligible": strat_meta["eligible_strata_before"]["symbols"].get("DOGEUSDT", 0), "selected": strat_meta["selected_strata_after"]["symbols"].get("DOGEUSDT", 0),
            },
        ],
    )
    frozen_payload["expansion_freeze_bundle_sha256"] = bundle_sha
    write_json(out_dir / "frozen_expansion_cases_v1.json", frozen_payload)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN",
        "expansion_freeze_bundle_sha256": bundle_sha,
        "source_manifest_sha256": source_sha,
        "selected_count": TARGET_COUNT,
        "created_at_utc": frozen_payload["created_at_utc"],
        "files": [
            "eligible_universe.csv",
            "excluded_exposed_cases.csv",
            "dedup_groups.csv",
            "stratum_counts.csv",
            "frozen_expansion_cases_v1.json",
            "freeze_manifest.json",
            "EXPANSION_FREEZE_REPORT.md",
            "selection_audit.json",
            "outcome_blindness_audit.json",
        ],
    }
    write_json(out_dir / "freeze_manifest.json", manifest)

    coverage = estimate_coverage_batch(ordered_cases, raw_root=root / DEFAULT_RAW_ROOT)
    write_json(out_dir / "coverage_estimate.json", coverage)

    selection_audit = {
        "phase_1_source_resolution": {
            "inventory": inventory,
            "resolution": resolution,
            "outcome_columns_not_read_for_selection": True,
        },
        "phase_2_eligible_universe": {
            "size": len(eligible_all),
            "post_exposure_size": len(eligible),
            "eligibility_rejects_count": len(eligibility_rejects),
        },
        "phase_3_exposure": {
            "embargo": embargo,
            "exposed_records": exposed,
            "excluded_count": len(exposure_excluded),
        },
        "phase_4_stratification": strat_meta,
        "phase_5_dedup": {
            "groups": dedup_groups,
            "dedup_excluded_count": len([g for g in dedup_groups if g.get("excluded_candidates")]),
        },
        "phase_8_coverage_summary": coverage["summary"],
    }
    write_json(out_dir / "selection_audit.json", selection_audit)

    outcome_blindness = {
        "outcome_fields_read_for_selection": False,
        "outcome_fields_read_for_sorting": False,
        "forbidden_source_columns": list(OUTCOME_LIKE_SOURCE_COLUMNS),
        "selection_hash_inputs": [
            "source_manifest_sha256",
            "candidate_id",
            "pool_id",
            "reference_ts",
            "fixed_sampling_seed",
        ],
        "stratification_dimensions": ["pool_side", "symbol", "pool_timeframe"],
        "forbidden_stratification": [
            "micro_pass",
            "breakout_reclaim",
            "room_pass",
            "outcome",
            "profit",
        ],
        "verified_cases_have_no_outcome_fields": all(
            not any(s in k.lower() for s in FORBIDDEN_FIELD_SUBSTR)
            for c in frozen_payload["ordered_cases"]
            for k in c
        ),
    }
    write_json(out_dir / "outcome_blindness_audit.json", outcome_blindness)

    report = _expansion_report(
        root,
        frozen_payload,
        manifest,
        inventory,
        resolution,
        strat_meta,
        coverage,
        bundle_sha,
    )
    (out_dir / "EXPANSION_FREEZE_REPORT.md").write_text(report, encoding="utf-8")

    return {
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN",
        "expansion_freeze_bundle_sha256": bundle_sha,
        "out_dir": str(out_dir),
        "selected_count": len(ordered_cases),
    }


def verify_expansion_freeze(
    repo_root: Path | None = None,
    *,
    mutate: bool = False,
) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[3]
    out_dir = root / RESULTS_DIR_REL
    frozen_path = out_dir / "frozen_expansion_cases_v1.json"
    if not frozen_path.is_file():
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "missing frozen bundle")

    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    stored_sha = frozen.get("expansion_freeze_bundle_sha256")
    payload = {
        k: v
        for k, v in frozen.items()
        if k not in ("expansion_freeze_bundle_sha256", "created_at_utc")
    }
    recomputed = sha256_bytes(canonical_json_bytes(payload))
    if recomputed != stored_sha:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            f"bundle sha mismatch stored={stored_sha} recomputed={recomputed}",
        )

    source_rel = frozen["source_manifest"]["path_relative"]
    source_sha = frozen["source_manifest"]["sha256"]
    if sha256_file(root / source_rel) != source_sha:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "source manifest changed")

    if frozen["case_sequence_freeze_sha256"] != CASE_SEQUENCE_FREEZE_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "case sequence sha mismatch")
    if frozen["entry_contract_freeze_sha256"] != ENTRY_CONTRACT_FREEZE_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "entry contract sha mismatch")
    if frozen["strategy_config_sha256"] != STRATEGY_CONFIG_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "strategy config sha mismatch")
    if sha256_file(root / STRATEGY_CONFIG_REL) != STRATEGY_CONFIG_SHA256:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "strategy yaml drift")

    cases = frozen["ordered_cases"]
    if len(cases) != TARGET_COUNT:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE", f"case count {len(cases)} != {TARGET_COUNT}"
        )
    ids = [c["expansion_case_id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "duplicate expansion_case_id")

    ask = sum(1 for c in cases if c["pool_side"] == "ASK")
    bid = sum(1 for c in cases if c["pool_side"] == "BID")
    if ask != bid:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE", f"ASK/BID imbalance {ask}/{bid}"
        )

    for c in cases:
        for k in c:
            if any(s in k.lower() for s in FORBIDDEN_FIELD_SUBSTR):
                raise ExpansionFreezeError(
                    "EXPANSION_FREEZE_INTEGRITY_FAILURE", f"forbidden field {k} in case"
                )
        expected = selection_hash(
            source_sha256=source_sha,
            candidate_id=c["source_candidate_id"],
            pool_id=c["pool_id"],
            reference_ts=c["reference_ts"],
            seed=frozen["sampling_seed"],
        )
        if c["deterministic_selection_hash"] != expected:
            raise ExpansionFreezeError(
                "EXPANSION_FREEZE_INTEGRITY_FAILURE",
                f"hash mismatch {c['expansion_case_id']}",
            )

    next_after = frozen["next_after"]
    for i, c in enumerate(cases[:-1]):
        if next_after[c["expansion_case_id"]] != cases[i + 1]["expansion_case_id"]:
            raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "next_after broken")
    if next_after[cases[-1]["expansion_case_id"]] is not None:
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "terminal next_after not null")

    # no duplicate event families
    fams = [c["event_family_id"] for c in cases]
    if len(set(fams)) != len(fams):
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "duplicate event families")

    exposed = load_exposed_cases(root)
    emb = frozen["exposure_embargo"]
    eligible, _ = apply_exposure_exclusion(
        [
            {
                "source_candidate_id": c["source_candidate_id"],
                "reference_ts": c["reference_ts"],
                "pool_id": c["pool_id"],
                "market_arrival_cluster_id": c["event_family_id"],
                "event_family_id": c["event_family_id"],
            }
            for c in cases
        ],
        exposed,
        emb,
    )
    if len(eligible) != len(cases):
        raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "selected case in embargo zone")

    if mutate:
        tampered = dict(payload)
        tampered["selected_count"] = 99
        bad = sha256_bytes(canonical_json_bytes(tampered))
        if bad == stored_sha:
            raise ExpansionFreezeError("EXPANSION_FREEZE_INTEGRITY_FAILURE", "mutation test failed")
        return {"ok": True, "mutation_detected": True, "original_sha256": stored_sha, "mutated_sha256": bad}

    # reproducibility: rebuild hash from source should match if source unchanged
    rebuild = build_expansion_freeze(root)
    if rebuild["expansion_freeze_bundle_sha256"] != stored_sha:
        raise ExpansionFreezeError(
            "EXPANSION_FREEZE_INTEGRITY_FAILURE",
            "second run hash mismatch",
        )

    return {
        "ok": True,
        "verdict": "LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN",
        "expansion_freeze_bundle_sha256": stored_sha,
    }


def _expansion_report(
    root: Path,
    frozen: dict[str, Any],
    manifest: dict[str, Any],
    inventory: list[dict[str, Any]],
    resolution: dict[str, Any],
    strat_meta: dict[str, Any],
    coverage: dict[str, Any],
    bundle_sha: str,
) -> str:
    cases = frozen["ordered_cases"]
    lines = [
        "# Liquidity Pool Entry Contract Expansion Freeze v1",
        "",
        f"Generated: {frozen['created_at_utc']}",
        "",
        "## Verdict",
        "",
        "**LP_ENTRY_CONTRACT_EXPANSION_24_FROZEN**",
        "",
        "## Source",
        "",
        f"- Path: `{frozen['source_manifest']['path_relative']}`",
        f"- SHA256: `{frozen['source_manifest']['sha256']}`",
        f"- Resolution: {resolution['resolution_rule']}",
        "",
        "## Eligible universe",
        "",
        f"- Post-exposure eligible: see `eligible_universe.csv`",
        f"- Exposure exclusions: see `excluded_exposed_cases.csv`",
        "",
        "## Stratification",
        "",
        f"- Before: {json.dumps(strat_meta['eligible_strata_before'])}",
        f"- After: {json.dumps(strat_meta['selected_strata_after'])}",
        f"- Deviations: {strat_meta.get('deviations') or 'none'}",
        "",
        "## Frozen cases (24)",
        "",
    ]
    for c in cases:
        lines.append(
            f"- `{c['expansion_case_id']}` {c['pool_side']}/{c['approach']} "
            f"{c['symbol']} `{c['reference_ts']}` pool=`{c['pool_id']}` "
            f"hash=`{c['deterministic_selection_hash'][:16]}…`"
        )
    lines.extend(
        [
            "",
            "## Outcome blindness",
            "",
            "Selection uses only causal metadata + fixed seed hash. "
            "No outcome/Micro/Room columns read.",
            "",
            "## Expansion freeze bundle SHA256",
            "",
            f"`{bundle_sha}`",
            "",
            "## Coverage estimate",
            "",
            f"- Est. total runtime: {coverage['summary']['estimated_total_runtime_min']} min",
            f"- Est. per case: {coverage['summary']['estimated_runtime_per_case_s']} s",
            "",
            "## Contract hashes",
            "",
            f"- Case sequence: `{CASE_SEQUENCE_FREEZE_SHA256}`",
            f"- Entry contract: `{ENTRY_CONTRACT_FREEZE_SHA256}`",
            f"- Strategy config: `{STRATEGY_CONFIG_SHA256}`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
