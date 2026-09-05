"""CASE_03 BID/FROM_ABOVE causal deep audit (outcome-blind mechanical phase first)."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    CANONICAL_TRADES_TABLE,
    UNFITTED_F0_DIAGNOSTIC,
)
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from dataclasses import dataclass

from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1 import (
    ACCEPT_VARIANTS_S,
    CASE_ID as DEFAULT_CASE_ID,
    COST_RT_BPS,
    EDGE_TOL_BPS,
    EXPECTED_FREEZE_BUNDLE_SHA256,
    FLOW_WINDOWS_S,
    FORMAT_VERSION as DEFAULT_FORMAT_VERSION,
    FREEZE_DIR_REL,
    MAJOR_WALL_RANK,
    MAX_POST_S,
    PRE_S,
    SYMBOL,
    TIMEFRAMES,
)


@dataclass(frozen=True)
class BidCaseAuditSpec:
    case_id: str
    predecessor_case_id: str
    format_version: str
    results_dirname: str
    manual_review_name: str
    deep_audit_result_glob: str

    @property
    def freeze_fail(self) -> str:
        return f"{self.case_id}_FREEZE_VERIFICATION_FAILURE"

    @property
    def pool_fail(self) -> str:
        return f"{self.case_id}_FROZEN_POOL_NOT_UNAMBIGUOUS"

    @property
    def data_blocked(self) -> str:
        return f"{self.case_id}_DATA_BLOCKED"

    @property
    def prefix_fail(self) -> str:
        return f"{self.case_id}_PREFIX_PARITY_FAILURE"

    @property
    def previously_exposed(self) -> str:
        return f"{self.case_id}_PREVIOUSLY_EXPOSED"


CASE_03_SPEC = BidCaseAuditSpec(
    case_id="CASE_03",
    predecessor_case_id="CASE_02",
    format_version=DEFAULT_FORMAT_VERSION,
    results_dirname="case_03_frozen_bid_pool_causal_reaction_audit_v1",
    manual_review_name="CASE_03_MANUAL_REVIEW.md",
    deep_audit_result_glob="case_03_frozen_bid_pool_causal_reaction_audit_v1",
)

CASE_04_SPEC = BidCaseAuditSpec(
    case_id="CASE_04",
    predecessor_case_id="CASE_03",
    format_version="case_04_frozen_bid_pool_causal_reaction_audit/v1",
    results_dirname="case_04_frozen_bid_pool_causal_reaction_audit_v1",
    manual_review_name="CASE_04_MANUAL_REVIEW.md",
    deep_audit_result_glob="case_04_frozen_bid_pool_causal_reaction_audit_v1",
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    normalize_tick_price,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
    side_levels_ranked_full,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.util import notional, tick_size
from orderbook_analyse.case_03_frozen_bid_pool_causal_reaction_audit_v1.entry_contract import (
    branch_gates_to_dict,
    flatten_room_gate_for_mech,
    geom_rows_to_pool_candidates,
    prefix_room_gate_parity,
    resolve_mechanical_decision,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    RoomGateConfigError,
    load_effective_room_config,
)
from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1.freeze import (
    FreezeError,
    verify_freeze,
)
from orderbook_analyse.liquidity_pool_signal import chart_lookback_start
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    pool_row_from_engine,
    run_chart_backend_lld,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    iter_ob_1s,
)


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(dt: datetime | str) -> int:
    return int(_utc(dt).timestamp() * 1000)


def _dt_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def bps(a: float, b: float) -> float:
    if b <= 0:
        return float("nan")
    return (a - b) / b * 10000.0


def sha256_obj(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def pool_zone_bid(mid: float | None, lo: float, hi: float, tol_bps: float) -> str | None:
    """BID pool: front=upper, back=lower. FROM_ABOVE approach."""
    if mid is None or mid <= 0 or hi <= lo:
        return None
    front = hi
    back = lo
    f_lo = front * (1 - tol_bps / 10000.0)
    f_hi = front * (1 + tol_bps / 10000.0)
    b_lo = back * (1 - tol_bps / 10000.0)
    b_hi = back * (1 + tol_bps / 10000.0)
    if mid > f_hi:
        return "ABOVE_FRONT"
    if f_lo <= mid <= f_hi:
        return "AT_FRONT_EDGE"
    if mid < b_lo:
        return "BELOW_BACK"
    if b_lo <= mid <= b_hi:
        return "AT_BACK_EDGE"
    frac = (hi - mid) / (hi - lo)  # 0 at front, 1 at back
    if frac < 1 / 3:
        return "INSIDE_UPPER_THIRD"
    if frac < 2 / 3:
        return "INSIDE_MIDDLE_THIRD"
    return "INSIDE_LOWER_THIRD"


def aggressor_class(
    buy_n: float, sell_n: float, mid_chg: float | None, min_n: float, strong_bps: float
) -> str:
    if buy_n < min_n and sell_n < min_n:
        return "NO_MEANINGFUL_ATTACK"
    two = buy_n >= min_n and sell_n >= min_n
    if mid_chg is None:
        return "TWO_SIDED_CONTEST" if two else "NO_MEANINGFUL_ATTACK"
    if two and abs(mid_chg) < strong_bps * 0.5:
        return "TWO_SIDED_CONTEST"
    if sell_n >= min_n and buy_n < sell_n * 0.5:
        if mid_chg <= -strong_bps * 0.5:
            return "SELL_EFFECTIVE_BREAK_ATTACK"
        if mid_chg >= -strong_bps * 0.25:
            return "SELL_INEFFICIENT_ABSORPTION"
        return "SELL_EFFECTIVE_BREAK_ATTACK" if mid_chg < 0 else "SELL_INEFFICIENT_ABSORPTION"
    if buy_n >= min_n and sell_n < buy_n * 0.5:
        if mid_chg >= strong_bps * 0.5:
            return "BUY_COUNTER_RECLAIM"
        if mid_chg <= strong_bps * 0.25:
            return "BUY_INEFFICIENT"
        return "BUY_COUNTER_RECLAIM" if mid_chg > 0 else "BUY_INEFFICIENT"
    return "TWO_SIDED_CONTEST"


def verify_freeze_snapshot(repo: Path, label: str, *, spec: BidCaseAuditSpec) -> dict[str, Any]:
    out = {
        "label": label,
        "expected_freeze_bundle_sha256": EXPECTED_FREEZE_BUNDLE_SHA256,
        "ok": False,
    }
    try:
        res = verify_freeze(repo, repo / FREEZE_DIR_REL)
        man = json.loads((repo / FREEZE_DIR_REL / "freeze_manifest.json").read_text())
        out.update(
            {
                "ok": True,
                "freeze_bundle_sha256": res["freeze_bundle_sha256"],
                "source_manifest_sha256": man["source_manifest_sha256"],
                "matches_expected": res["freeze_bundle_sha256"] == EXPECTED_FREEZE_BUNDLE_SHA256,
            }
        )
        if not out["matches_expected"]:
            out["ok"] = False
            out["verdict"] = spec.freeze_fail
    except FreezeError as e:
        out["verdict"] = spec.freeze_fail
        out["detail"] = str(e)
    return out


def load_frozen_bid_case(repo: Path, *, spec: BidCaseAuditSpec) -> dict[str, Any]:
    frozen = json.loads((repo / FREEZE_DIR_REL / "frozen_case_sequence_v1.json").read_text())
    cases = [c for c in frozen["ordered_cases"] if c["case_id"] == spec.case_id]
    if len(cases) != 1:
        raise RuntimeError(f"{spec.pool_fail}: n={len(cases)}")
    c = cases[0]
    if c["direction"] != "BID" or c["approach"] != "FROM_ABOVE":
        raise RuntimeError(f"{spec.case_id} direction/approach mismatch")
    if c["exposure_status"] != "PROSPECTIVE_UNAUDITED":
        raise RuntimeError(f"{spec.case_id} exposure unexpected: {c['exposure_status']}")
    if frozen["next_after"].get(spec.predecessor_case_id) != spec.case_id:
        raise RuntimeError(
            f"next_after {spec.predecessor_case_id} != {spec.case_id}"
        )
    src = json.loads(
        (
            repo
            / "results/liquidity_pool_six_case_wall_trade_reaction_sample_v1/selection_manifest.json"
        ).read_text()
    )
    sc = [x for x in src["cases"] if x["case_id"] == spec.case_id]
    if len(sc) != 1:
        raise RuntimeError(f"{spec.pool_fail} source")
    sc0 = sc[0]
    if abs(float(sc0["component_lower_edge"]) - float(c["component_lower_edge"])) > 1e-6:
        raise RuntimeError(f"{spec.pool_fail} edge mismatch")
    if abs(float(sc0["component_upper_edge"]) - float(c["component_upper_edge"])) > 1e-6:
        raise RuntimeError(f"{spec.pool_fail} edge mismatch")
    members = [p for p in str(sc0["member_pool_ids"]).split("|") if p]
    if len(members) != 1:
        raise RuntimeError(f"{spec.pool_fail} members={members}")
    return {
        "freeze_case": c,
        "source_case_identity": {
            k: sc0[k]
            for k in sc0
            if not any(
                x in k.lower()
                for x in ("evidence", "verdict", "outcome", "pnl", "mfe", "mae", "return")
            )
        },
        "pool_id": members[0],
        "lower": float(c["component_lower_edge"]),
        "upper": float(c["component_upper_edge"]),
        "front_edge": float(c["component_upper_edge"]),
        "back_edge": float(c["component_lower_edge"]),
        "reference_ts": c["reference_ts"],
        "symbol": c["symbol"],
        "direction": c["direction"],
        "approach": c["approach"],
        "cluster_id": c["market_arrival_cluster_id"],
    }


def load_frozen_case_03(repo: Path) -> dict[str, Any]:
    return load_frozen_bid_case(repo, spec=CASE_03_SPEC)


def mid_at_or_before(raw_root: Path, ref: datetime) -> dict[str, Any]:
    last = None
    for bucket, gen, bb, ba, mid, _bids, _asks in iter_ob_1s(
        raw_root, ref - timedelta(seconds=30), ref
    ):
        if mid is not None:
            last = {
                "ob_timestamp": _iso(_dt_ms(bucket)),
                "ob_ms": bucket,
                "best_bid": bb,
                "best_ask": ba,
                "mid": float(mid),
                "genuine": bool(gen),
            }
    if last is None:
        return {"ok": False, "reason": "no_ob_mid"}
    last["age_to_reference_s"] = (_ms(ref) - last["ob_ms"]) / 1000.0
    last["ok"] = True
    return last


def run_audit(
    *,
    repo_root: Path,
    raw_root: Path,
    out_dir: Path,
    spec: BidCaseAuditSpec = CASE_03_SPEC,
) -> dict[str, Any]:
    t0 = datetime.now(timezone.utc)
    out_dir.mkdir(parents=True, exist_ok=True)
    query_log: list[dict[str, Any]] = []
    blindness = {
        "phase": "mechanical",
        "forbidden_reads_before_unblind": [
            "six_case_summary.csv evidence columns",
            "MANUAL_SIX_CASE_REVIEW.md",
            "forward returns / mfe / mae / pnl",
            f"any {spec.case_id} outcome verdict file",
        ],
        "files_read_mechanical": [],
        "files_read_unblind": [],
        "outcome_read_before_mechanical_persist": False,
        "case_id": spec.case_id,
        "note": "CASE_03 result does not alter thresholds or rules for later cases",
    }

    # Prior comparable deep audit?
    prior = repo_root / "results" / spec.deep_audit_result_glob
    if prior.exists() and any(prior.iterdir()) and (prior / "mechanical_verdict_pre_unblind.json").exists():
        # Re-running into same out_dir is allowed only if this is the target write; block unexpected prior exposure elsewhere
        if prior.resolve() != out_dir.resolve():
            write_json(
                out_dir / "summary.json",
                {"verdict": spec.previously_exposed, "prior_path": str(prior)},
            )
            return {"verdict": spec.previously_exposed}

    # Phase 0
    before = verify_freeze_snapshot(repo_root, "before", spec=spec)
    write_json(out_dir / "freeze_verification_before.json", before)
    if not before.get("ok"):
        write_json(
            out_dir / "summary.json",
            {"verdict": spec.freeze_fail, "before": before},
        )
        return {"verdict": spec.freeze_fail}

    try:
        effective_room = load_effective_room_config(repo_root)
    except RoomGateConfigError as exc:
        write_json(
            out_dir / "summary.json",
            {
                "verdict": "INVALID_ROOM_GATE_CONFIG",
                "detail": str(exc),
            },
        )
        return {"verdict": "INVALID_ROOM_GATE_CONFIG"}

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=repo_root, text=True
    ).strip()
    dirty_n = len(
        subprocess.check_output(["git", "status", "--short"], cwd=repo_root, text=True).splitlines()
    )

    case = load_frozen_bid_case(repo_root, spec=spec)
    blindness["files_read_mechanical"] += [
        str(repo_root / FREEZE_DIR_REL / "frozen_case_sequence_v1.json"),
        str(
            repo_root
            / "results/liquidity_pool_six_case_wall_trade_reaction_sample_v1/selection_manifest.json"
        ),
    ]
    write_json(out_dir / "frozen_case_input.json", case)

    ref = _utc(case["reference_ts"])
    lo = float(case["lower"])
    hi = float(case["upper"])
    front = float(case["front_edge"])
    back = float(case["back_edge"])
    assert front == hi and back == lo

    mid_info = mid_at_or_before(raw_root, ref)
    write_json(out_dir / "reference_mid.json", mid_info)
    if not mid_info.get("ok"):
        write_json(out_dir / "summary.json", {"verdict": spec.data_blocked, "reason": "no_ob_mid"})
        after = verify_freeze_snapshot(repo_root, "after", spec=spec)
        write_json(out_dir / "freeze_verification_after.json", after)
        return {"verdict": spec.data_blocked}

    market = float(mid_info["mid"])
    tick = tick_size(SYMBOL)
    min_n = float(UNFITTED_F0_DIAGNOSTIC["min_notional_usdt"])
    strong_bps = float(UNFITTED_F0_DIAGNOSTIC["strong_same_side_impact_bps"])

    # Pool geometry + HTF confluence as-of ref (no outcome)
    geom_rows = []
    ask_above = []
    bid_below = []
    containing = []
    for tf in TIMEFRAMES:
        start = chart_lookback_start(ref, tf)
        bundle = run_chart_backend_lld(symbol=SYMBOL, timeframe=tf, start=start, end=ref)
        for p in bundle["engine_result"].pools:
            r = pool_row_from_engine(p, cfg=bundle["config"], as_of=ref, market_price=market)
            if not r["active_as_of"]:
                continue
            row = {
                "pool_id": r["pool_id"],
                "source_timeframe": r["source_timeframe"],
                "side": r["side"],
                "lower_edge": r["lower_edge"],
                "upper_edge": r["upper_edge"],
                "available_at": r["available_at"],
                "strength": r.get("strength"),
                "contains_mid": r["lower_edge"] <= market <= r["upper_edge"],
                "is_frozen_case_pool": r["pool_id"] == case["pool_id"],
            }
            geom_rows.append(row)
            if r["side"] == "ASK" and r["lower_edge"] > market:
                ask_above.append(r)
            if r["side"] == "BID" and r["upper_edge"] < market:
                bid_below.append(r)
            if row["contains_mid"]:
                containing.append(row)
    write_csv(out_dir / "causal_pool_geometry.csv", geom_rows)
    ask_above.sort(key=lambda r: float(r["lower_edge"]))
    bid_below.sort(key=lambda r: -float(r["upper_edge"]))

    selected_pool = {
        "pool_id": case["pool_id"],
        "source_timeframe": "5m",
        "side": "BID",
        "approach": "FROM_ABOVE",
        "lower": lo,
        "upper": hi,
        "front_edge": front,
        "back_edge": back,
        "available_at": case["source_case_identity"].get("cluster_start_ts"),
        "first_causal_availability_note": "source cluster_start_ts is arrival event; pool id from member_pool_ids",
        "mid_at_reference": market,
        "mid_location": (
            "INSIDE"
            if lo <= market <= hi
            else ("ABOVE_FRONT" if market > hi else "BELOW_BACK")
        ),
        "htf_containing": containing,
        "nearest_ask_above": None
        if not ask_above
        else {
            "pool_id": ask_above[0]["pool_id"],
            "lower_edge": ask_above[0]["lower_edge"],
            "upper_edge": ask_above[0]["upper_edge"],
            "source_timeframe": ask_above[0]["source_timeframe"],
            "distance_bps": bps(float(ask_above[0]["lower_edge"]), market),
        },
        "nearest_bid_below": None
        if not bid_below
        else {
            "pool_id": bid_below[0]["pool_id"],
            "lower_edge": bid_below[0]["lower_edge"],
            "upper_edge": bid_below[0]["upper_edge"],
            "source_timeframe": bid_below[0]["source_timeframe"],
            "distance_bps": bps(market, float(bid_below[0]["upper_edge"])),
        },
    }
    write_json(out_dir / "selected_pool.json", selected_pool)

    # Load trades + OB for reaction window
    load_start = ref - timedelta(seconds=PRE_S)
    load_end = ref + timedelta(seconds=MAX_POST_S)
    trades, trade_pre = load_trades_clickhouse(
        symbol=SYMBOL, start=load_start, end=load_end + timedelta(seconds=1), query_log=query_log
    )
    end_ms = _ms(load_end)
    start_ms = _ms(load_start)
    ref_ms = _ms(ref)
    trades = [t for t in trades if _ms(t.trade_ts) <= end_ms]
    buy_1s: dict[int, float] = defaultdict(float)
    sell_1s: dict[int, float] = defaultdict(float)
    for t in trades:
        sb = (_ms(t.trade_ts) // 1000) * 1000
        if t.side == "Buy":
            buy_1s[sb] += t.notional
        else:
            sell_1s[sb] += t.notional

    grid = list(range(start_ms, end_ms + 1000, 1000))
    buy_pref = [0.0]
    sell_pref = [0.0]
    idx_of = {s: i for i, s in enumerate(grid)}
    for s in grid:
        buy_pref.append(buy_pref[-1] + buy_1s.get(s, 0.0))
        sell_pref.append(sell_pref[-1] + sell_1s.get(s, 0.0))

    ob_rows = list(iter_ob_1s(raw_root, load_start, load_end))
    mid_by: dict[int, float] = {}
    book_by: dict[int, tuple] = {}
    for bucket, gen, bb, ba, mid, bids, asks in ob_rows:
        if gen and mid is not None:
            mid_by[bucket] = float(mid)
            book_by[bucket] = (bb, ba, bids, asks)

    def mid_get(ms: int) -> float | None:
        b = (ms // 1000) * 1000
        for off in range(0, 5):
            if b + off * 1000 in mid_by:
                return mid_by[b + off * 1000]
            if b - off * 1000 in mid_by:
                return mid_by[b - off * 1000]
        return None

    def window_flow(s: int, w: int) -> dict[str, Any]:
        i = idx_of.get(s)
        if i is None:
            return {"buy_notional": 0.0, "sell_notional": 0.0, "mid_change_bps": None, "class": "NO_MEANINGFUL_ATTACK"}
        j0 = max(0, i - w + 1)
        buy_n = buy_pref[i + 1] - buy_pref[j0]
        sell_n = sell_pref[i + 1] - sell_pref[j0]
        m0 = mid_by.get(grid[j0])
        m1 = mid_by.get(s)
        chg = bps(m1, m0) if m0 and m1 else None
        return {
            "buy_notional": buy_n,
            "sell_notional": sell_n,
            "gross": buy_n + sell_n,
            "buy_share": buy_n / (buy_n + sell_n) if buy_n + sell_n > 0 else None,
            "sell_share": sell_n / (buy_n + sell_n) if buy_n + sell_n > 0 else None,
            "mid_change_bps": chg,
            "sell_eff_per_100k": ((-chg) / (sell_n / 1e5)) if chg is not None and sell_n > 0 else None,
            "buy_eff_per_100k": (chg / (buy_n / 1e5)) if chg is not None and buy_n > 0 else None,
            "class": aggressor_class(buy_n, sell_n, chg, min_n, strong_bps),
        }

    # Arrival: first mid <= front from above at/after load_start, prefer around ref
    arrival_ms = None
    prev = None
    for s in grid:
        m = mid_by.get(s)
        if m is None:
            continue
        if prev is not None and prev > front and m <= front:
            arrival_ms = s
            if s >= ref_ms - 2000:
                break
        prev = m
    if arrival_ms is None:
        # already inside at ref
        if lo <= market <= hi:
            arrival_ms = ref_ms
        else:
            write_json(
                out_dir / "summary.json",
                {"verdict": "NO_CAUSAL_POOL_REACTION", "reason": "no_front_arrival"},
            )
            after = verify_freeze_snapshot(repo_root, "after", spec=spec)
            write_json(out_dir / "freeze_verification_after.json", after)
            return {"verdict": "NO_CAUSAL_POOL_REACTION"}

    timeline = []
    agg_rows = []
    wall_hist: dict[float, dict[str, Any]] = {}
    prev_major_bids: dict[float, float] = {}
    retreat_events = []
    first_back_cross_ms = None
    first_reclaim_front_ms = None
    seen_inside = False
    local_exit_above = False

    for s in grid:
        if s < arrival_ms - PRE_S * 1000:
            continue
        mid = mid_by.get(s)
        zone = pool_zone_bid(mid, lo, hi, EDGE_TOL_BPS)
        buy_n = buy_1s.get(s, 0.0)
        sell_n = sell_1s.get(s, 0.0)
        flows = {str(w): window_flow(s, w) for w in FLOW_WINDOWS_S}
        f5 = flows["5"]
        if zone and zone.startswith("INSIDE"):
            seen_inside = True
        if mid is not None and mid < back and s >= arrival_ms:
            if first_back_cross_ms is None:
                first_back_cross_ms = s
        if mid is not None and mid > front and seen_inside and s >= arrival_ms:
            local_exit_above = True
            if first_reclaim_front_ms is None and first_back_cross_ms is None:
                first_reclaim_front_ms = s
            elif first_back_cross_ms is not None and mid > front and first_reclaim_front_ms is None:
                first_reclaim_front_ms = s

        # bid walls inside component
        book = book_by.get(s)
        major_now: dict[float, float] = {}
        if book:
            _bb, _ba, bids, _asks = book
            ranked = side_levels_ranked_full([(p, q) for p, q in bids])
            for row in ranked:
                px = normalize_tick_price(row["price"], tick)
                if not (lo - tick <= px <= hi + tick):
                    continue
                if row["full_side_rank"] > MAJOR_WALL_RANK and row["significance_class"] == "MINOR":
                    continue
                major_now[px] = row["notional"]
                h = wall_hist.setdefault(
                    px,
                    {
                        "price": px,
                        "side": "BID",
                        "first_seen_ts": _iso(_dt_ms(s)),
                        "last_seen_ts": _iso(_dt_ms(s)),
                        "max_notional": row["notional"],
                        "max_rank": row["full_side_rank"],
                        "attacked": False,
                        "trade_depletion": False,
                        "refilled": False,
                        "cancelled_before_touch": False,
                        "reappeared_lower": False,
                        "reappeared_higher": False,
                        "qty_series": [],
                    },
                )
                h["last_seen_ts"] = _iso(_dt_ms(s))
                h["max_notional"] = max(h["max_notional"], row["notional"])
                h["max_rank"] = min(h["max_rank"], row["full_side_rank"])
                h["qty_series"].append((s, row["qty"]))
                if len(h["qty_series"]) > 3:
                    h["qty_series"] = h["qty_series"][-3:]
                for t in trades:
                    if (_ms(t.trade_ts) // 1000) * 1000 != s:
                        continue
                    if t.side == "Sell" and abs(normalize_tick_price(t.price, tick) - px) < 1e-9:
                        h["attacked"] = True
                qs = h["qty_series"]
                if len(qs) >= 2 and qs[-1][0] == s:
                    prev_q, cur_q = qs[-2][1], qs[-1][1]
                    if cur_q > prev_q + 1e-12:
                        h["refilled"] = True
                    if cur_q < prev_q - 1e-12:
                        sell_at = sum(
                            t.size
                            for t in trades
                            if (_ms(t.trade_ts) // 1000) * 1000 == s
                            and t.side == "Sell"
                            and abs(normalize_tick_price(t.price, tick) - px) < 1e-9
                        )
                        red = prev_q - cur_q
                        if sell_at >= 0.85 * red and sell_at > 0:
                            h["trade_depletion"] = True
                        elif sell_at <= 0:
                            h["cancelled_before_touch"] = h["cancelled_before_touch"] or (not h["attacked"])

            for px, notion in prev_major_bids.items():
                if px not in major_now and notion >= min_n * 0.5:
                    lower = [p for p in major_now if p < px - tick]
                    higher = [p for p in major_now if p > px + tick]
                    if lower:
                        rep = max(lower)
                        followed = mid is not None and mid < px
                        retreat_events.append(
                            {
                                "disappearance_ts": _iso(_dt_ms(s)),
                                "old_wall_price": px,
                                "old_wall_attacked": bool(wall_hist.get(px, {}).get("attacked")),
                                "replacement_wall_price": rep,
                                "displacement_bps": bps(px, rep),
                                "price_followed": followed,
                                "pattern": "RETREATED_LOWER",
                            }
                        )
                        wall_hist[px]["reappeared_lower"] = True
                    if higher:
                        wall_hist[px]["reappeared_higher"] = True
        prev_major_bids = major_now

        timeline.append(
            {
                "second": _iso(_dt_ms(s)),
                "second_ms": s,
                "mid": mid,
                "pool_zone": zone,
                "buy_notional_1s": buy_n,
                "sell_notional_1s": sell_n,
                "flow_5s_buy": f5["buy_notional"],
                "flow_5s_sell": f5["sell_notional"],
                "flow_5s_mid_change_bps": f5["mid_change_bps"],
                "aggressor_class_5s": f5["class"],
                "back_crossed": first_back_cross_ms is not None and s >= first_back_cross_ms,
                "local_exit_above": local_exit_above,
            }
        )
        if s >= arrival_ms and (s - arrival_ms) % 5000 == 0:
            agg_rows.append(
                {
                    "ts": _iso(_dt_ms(s)),
                    "seconds_since_arrival": (s - arrival_ms) // 1000,
                    **{f"w{w}_{k}": flows[str(w)].get(k) for w in FLOW_WINDOWS_S for k in ("buy_notional", "sell_notional", "mid_change_bps", "class")},
                }
            )

    write_csv(out_dir / "edge_reaction_timeline.csv", timeline)
    write_csv(out_dir / "aggressor_efficiency.csv", agg_rows)

    # Acceptance / rejection variants from arrival and from back cross
    accept_rows = []
    for hold_s in ACCEPT_VARIANTS_S:
        # defense: stay above back / reclaim above front
        below_ok = False
        if first_back_cross_ms is not None:
            below_ok = True
            for hs in range(first_back_cross_ms, first_back_cross_ms + hold_s * 1000 + 1000, 1000):
                m = mid_by.get(hs)
                if m is None or m >= back:
                    below_ok = False
                    break
        reclaim_ok = False
        # rejection of breakdown: after back cross, reclaim above back and hold
        if first_back_cross_ms is not None:
            # find first reclaim above back after cross
            reclaim_ms = None
            for hs in range(first_back_cross_ms, first_back_cross_ms + 180_000, 1000):
                m = mid_by.get(hs)
                if m is not None and m >= back:
                    reclaim_ms = hs
                    break
            if reclaim_ms is not None:
                reclaim_ok = True
                for hs in range(reclaim_ms, reclaim_ms + hold_s * 1000 + 1000, 1000):
                    m = mid_by.get(hs)
                    if m is None or m < back:
                        reclaim_ok = False
                        break
        # front reclaim (defense long signal): mid > front for hold
        front_hold = False
        if first_reclaim_front_ms is not None:
            front_hold = True
            for hs in range(first_reclaim_front_ms, first_reclaim_front_ms + hold_s * 1000 + 1000, 1000):
                m = mid_by.get(hs)
                if m is None or m <= front:
                    front_hold = False
                    break
        accept_rows.append(
            {
                "hold_s": hold_s,
                "breakout_accepted_below_back": below_ok,
                "reclaim_above_back_held": reclaim_ok,
                "reclaim_above_front_held": front_hold,
                "first_back_cross_ts": _iso(_dt_ms(first_back_cross_ms)) if first_back_cross_ms else None,
                "first_front_reclaim_ts": _iso(_dt_ms(first_reclaim_front_ms)) if first_reclaim_front_ms else None,
            }
        )

    # Wall lifecycle
    wall_rows = []
    for px, h in sorted(wall_hist.items()):
        cls = "STABLE_DEFENSE"
        if h["trade_depletion"]:
            cls = "TRADE_SUPPORTED_DEPLETION"
        elif h["cancelled_before_touch"] and h.get("reappeared_lower"):
            cls = "RETREATED_LOWER"
        elif h["cancelled_before_touch"]:
            cls = "CANCEL_DOMINANT_REMOVAL"
        elif h["refilled"] and h["attacked"]:
            cls = "REFILLED"
        elif h.get("reappeared_lower"):
            cls = "REAPPEARED_LOWER"
        elif h.get("reappeared_higher"):
            cls = "REAPPEARED_HIGHER"
        elif h["attacked"] and h["cancelled_before_touch"]:
            cls = "MIXED"
        elif h["attacked"]:
            cls = "MIXED"
        wall_rows.append(
            {
                "price": px,
                "side": "BID",
                "full_side_rank_best": h["max_rank"],
                "max_notional": h["max_notional"],
                "first_seen_ts": h["first_seen_ts"],
                "last_seen_ts": h["last_seen_ts"],
                "attacked": h["attacked"],
                "trade_depletion": h["trade_depletion"],
                "refilled": h["refilled"],
                "cancelled_before_touch": h["cancelled_before_touch"],
                "reappeared_lower": h.get("reappeared_lower"),
                "reappeared_higher": h.get("reappeared_higher"),
                "lifecycle_class": cls,
                "note": "cancel_is_not_trade_depletion",
            }
        )
    write_csv(out_dir / "wall_lifecycle.csv", wall_rows)

    # Aggressor summary from timeline post-arrival
    post = [r for r in timeline if r["second_ms"] >= arrival_ms]
    sell_eff = sum(1 for r in post if r.get("aggressor_class_5s") == "SELL_EFFECTIVE_BREAK_ATTACK")
    sell_abs = sum(1 for r in post if r.get("aggressor_class_5s") == "SELL_INEFFICIENT_ABSORPTION")
    buy_rec = sum(1 for r in post if r.get("aggressor_class_5s") == "BUY_COUNTER_RECLAIM")
    two = sum(1 for r in post if r.get("aggressor_class_5s") == "TWO_SIDED_CONTEST")

    acc5 = next(r for r in accept_rows if r["hold_s"] == 5)
    acc15 = next(r for r in accept_rows if r["hold_s"] == 15)
    acc30 = next(r for r in accept_rows if r["hold_s"] == 30)

    trade_dep = any(w["lifecycle_class"] == "TRADE_SUPPORTED_DEPLETION" for w in wall_rows)
    retreat_follow = sum(1 for e in retreat_events if e.get("price_followed") and not e.get("old_wall_attacked")) >= 2

    # Window stats 5/15/30/60 from arrival
    window_stats = []
    for hold_s in ACCEPT_VARIANTS_S:
        end_w = arrival_ms + hold_s * 1000
        seg = [r for r in timeline if arrival_ms <= r["second_ms"] <= end_w and r.get("mid") is not None]
        if not seg:
            continue
        mids = [float(r["mid"]) for r in seg]
        m0 = mid_by.get(arrival_ms) or market
        m1 = mids[-1]
        inside = sum(1 for m in mids if back <= m <= front)
        window_stats.append(
            {
                "window_s": hold_s,
                "mid_change_bps": bps(m1, m0),
                "time_inside_frac": inside / len(mids),
                "max_downside_extension_bps": bps(min(mids), front),
                "max_reclaim_extension_bps": bps(max(mids), front),
                "end_mid": m1,
                "end_vs_front": "ABOVE" if m1 > front else ("AT" if abs(bps(m1, front)) <= EDGE_TOL_BPS else "BELOW"),
                "end_vs_back": "BELOW" if m1 < back else ("AT" if abs(bps(m1, back)) <= EDGE_TOL_BPS else "ABOVE"),
            }
        )

    # Room gates via entry contract v1 (config loaded once per audit)
    pool_candidates = geom_rows_to_pool_candidates(geom_rows)

    # Mechanical branches
    long_ok = (
        seen_inside
        and sell_abs >= 1
        and buy_rec >= 1
        and not (acc5["breakout_accepted_below_back"] and acc15["breakout_accepted_below_back"])
        and not retreat_follow
    )
    short_ok = (
        first_back_cross_ms is not None
        and acc5["breakout_accepted_below_back"]
        and (trade_dep or retreat_follow or sell_eff >= 1)
        and not acc30["reclaim_above_back_held"]  # no stable reclaim within 30s hold definition after reclaim start
        and sell_eff >= 1
    )
    # If 5/15 accept below but 30s reclaim into pool → contested
    short_contested = bool(
        first_back_cross_ms is not None
        and acc5["breakout_accepted_below_back"]
        and (acc30["reclaim_above_back_held"] or not acc30["breakout_accepted_below_back"])
    )

    long_first_ts = None
    short_first_ts = None
    long_entry = None
    short_entry = None
    if long_ok and first_reclaim_front_ms is not None and acc5.get("reclaim_above_front_held"):
        long_first_ts = _iso(_dt_ms(first_reclaim_front_ms + 5000))
        long_entry = mid_get(first_reclaim_front_ms + 5000)
    elif long_ok and buy_rec >= 1:
        # first buy reclaim second after arrival
        for r in post:
            if r.get("aggressor_class_5s") == "BUY_COUNTER_RECLAIM" and r.get("mid") and r["mid"] > front * 0.999:
                long_first_ts = r["second"]
                long_entry = r["mid"]
                break
    if short_ok or (first_back_cross_ms and acc5["breakout_accepted_below_back"]):
        short_first_ts = _iso(_dt_ms(first_back_cross_ms + 5000)) if first_back_cross_ms else None
        short_entry = mid_get(first_back_cross_ms + 5000) if first_back_cross_ms else None

    decision = resolve_mechanical_decision(
        seen_inside=seen_inside,
        arrival_ms=arrival_ms,
        long_ok=long_ok,
        short_ok=short_ok,
        short_contested=short_contested,
        long_entry=long_entry,
        short_entry=short_entry,
        long_first_ts=long_first_ts,
        short_first_ts=short_first_ts,
        sell_eff=sell_eff,
        buy_rec=buy_rec,
        two=two,
        pools=pool_candidates,
        effective=effective_room,
    )
    long_room = decision.long_branch.room_gate
    short_room = decision.short_branch.room_gate
    write_json(out_dir / "long_room_to_target.json", long_room)
    write_json(out_dir / "short_room_to_target.json", short_room)

    reaction = decision.reaction
    candidate_direction = decision.candidate_direction
    first_available_ts = decision.first_available_ts
    entry_price = decision.mechanical_entry_price
    room_gate = decision.room_gate
    mechanical_trade_verdict = decision.mechanical_trade_verdict
    verdict = decision.mechanical_verdict

    # Prefix parity: reconstruct with data <= first_available_ts (or arrival+60s if none)
    cut_ms = _ms(first_available_ts) if first_available_ts else arrival_ms + 60_000
    prefix_tl = [r for r in timeline if r["second_ms"] <= cut_ms]
    room_parity = prefix_room_gate_parity(
        decision=decision,
        pools=pool_candidates,
        effective=effective_room,
    )
    prefix = {
        "cut_ts": _iso(_dt_ms(cut_ms)),
        "pool_id": case["pool_id"],
        "front_edge": front,
        "back_edge": back,
        "reaction": reaction,
        "candidate_direction": candidate_direction,
        "verdict": verdict,
        "n_timeline_prefix": len(prefix_tl),
        "prefix_status": room_parity.get("prefix_status", "EXACT_PREFIX_PARITY"),
        "room_gate_parity": room_parity,
        "note": "decision fields derived only from timestamps <= cut; room gate uses same config SHA",
    }
    # Sanity: no timeline row after cut used — already filtered
    if any(r["second_ms"] > cut_ms for r in prefix_tl):
        prefix["prefix_status"] = "PREFIX_PARITY_FAILURE"
        prefix["room_gate_parity"] = {"prefix_status": "PREFIX_PARITY_FAILURE", "reason": "timeline_cut"}
        write_json(out_dir / "prefix_parity.json", prefix)
        write_json(out_dir / "summary.json", {"verdict": spec.prefix_fail, "prefix": prefix})
        after = verify_freeze_snapshot(repo_root, "after", spec=spec)
        write_json(out_dir / "freeze_verification_after.json", after)
        return {"verdict": spec.prefix_fail}
    write_json(out_dir / "prefix_parity.json", prefix)

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    mech = {
        "case_id": spec.case_id,
        "freeze_bundle_sha256": EXPECTED_FREEZE_BUNDLE_SHA256,
        "reaction": reaction,
        "first_available_ts": first_available_ts,
        "entry_price": entry_price,
        "room_gate": room_gate,
        "long_branch": {
            "eligible": long_ok,
            "first_available_ts": long_first_ts,
            "entry_price": long_entry,
            "room": long_room,
            **branch_gates_to_dict(decision.long_branch),
        },
        "short_branch": {
            "eligible": short_ok,
            "contested": short_contested,
            "first_available_ts": short_first_ts,
            "entry_price": short_entry,
            "room": short_room,
            **branch_gates_to_dict(decision.short_branch),
        },
        "acceptance_variants": accept_rows,
        "window_stats": window_stats,
        "aggressor_counts": {
            "sell_effective": sell_eff,
            "sell_inefficient_absorption": sell_abs,
            "buy_counter_reclaim": buy_rec,
            "two_sided": two,
        },
        "wall_flags": {
            "trade_supported_depletion": trade_dep,
            "bid_retreat_with_price_follow_repeated": retreat_follow,
            "n_retreat_events": len(retreat_events),
        },
        "arrival_ts": _iso(_dt_ms(arrival_ms)),
        "first_back_cross_ts": _iso(_dt_ms(first_back_cross_ms)) if first_back_cross_ms else None,
        "mechanical_verdict": verdict,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "format_version": spec.format_version,
        "entry_contract_version": "liquidity_pool_entry_contract/v1",
        "meta": {
            "head": head,
            "branch": branch,
            "dirty_n": dirty_n,
            "elapsed_s": elapsed,
            "queries": query_log,
            "trade_preflight": trade_pre,
            "n_trades": len(trades),
            "n_ob_seconds": len(ob_rows),
            "n_timeline": len(timeline),
            "table": CANONICAL_TRADES_TABLE,
        },
    }
    mech.update(flatten_room_gate_for_mech(effective_room, decision))
    mech["mechanical_payload_sha256"] = sha256_obj(
        {k: v for k, v in mech.items() if k not in ("generated_at", "mechanical_payload_sha256")}
    )
    # PERSIST PRE-UNBLIND FIRST
    write_json(out_dir / "mechanical_verdict_pre_unblind.json", mech)
    blindness["mechanical_persisted_at"] = mech["generated_at"]
    blindness["mechanical_payload_sha256"] = mech["mechanical_payload_sha256"]
    write_json(out_dir / "outcome_blindness_audit.json", blindness)

    # UNBLIND: read six_case summary evidence only for comparison
    blindness["phase"] = "unblind"
    summary_path = (
        repo_root
        / "results/liquidity_pool_six_case_wall_trade_reaction_sample_v1/six_case_summary.csv"
    )
    outcome_cmp = {
        "unblind_performed": False,
        "source": None,
        "frozen_sample_evidence_class": None,
        "mechanical_verdict": verdict,
        "agreement_note": None,
    }
    if summary_path.exists():
        blindness["files_read_unblind"].append(str(summary_path))
        with summary_path.open() as f:
            rows = list(csv.DictReader(f))
        row = next((r for r in rows if r.get("case_id") == spec.case_id), None)
        if row:
            outcome_cmp["unblind_performed"] = True
            outcome_cmp["source"] = str(summary_path)
            # only comparative fields — do not mutate mech
            outcome_cmp["frozen_sample_evidence_class"] = row.get("evidence_class") or row.get(
                "reaction_class"
            )
            outcome_cmp["frozen_sample_side"] = row.get("side")
            outcome_cmp["frozen_sample_cluster_start_ts"] = row.get("cluster_start_ts")
            outcome_cmp["agreement_note"] = (
                "Comparative only. Mechanical verdict unchanged. "
                "Six-case short-window evidence is not a deep-audit oracle."
            )
    write_json(out_dir / "outcome_comparison.json", outcome_cmp)
    write_json(out_dir / "outcome_blindness_audit.json", blindness)

    write_json(
        out_dir / "query_audit.json",
        {
            "public_trades_select": 1,
            "raw_ob_reconstruction": 1,
            "lld_packs": len(TIMEFRAMES),
            "query_log": query_log,
            "trade_preflight": trade_pre,
        },
    )

    # Manual review
    manual = [
        f"# {spec.manual_review_name.replace('.md', '')}",
        "",
        f"Mechanical verdict: **{verdict}**",
        f"Reaction: {reaction}",
        f"Reference: {case['reference_ts']} mid={market}",
        f"Pool: {case['pool_id']} [{lo}, {hi}] front={front} back={back}",
        f"Arrival: {_iso(_dt_ms(arrival_ms))}",
        f"Back cross: {_iso(_dt_ms(first_back_cross_ms)) if first_back_cross_ms else None}",
        f"first_available_ts: {first_available_ts} entry={entry_price}",
        f"candidate_direction: {candidate_direction}",
        f"long_room gate_passed: {long_room.get('gate_passed')} raw_bps={long_room.get('raw_target_distance_bps')}",
        f"short_room gate_passed: {short_room.get('gate_passed')} raw_bps={short_room.get('raw_target_distance_bps')}",
        f"room_gate_config_sha256: {effective_room.config_sha256}",
        "",
        "## Acceptance variants",
        "```json",
        json.dumps(accept_rows, indent=2),
        "```",
        "",
        "## Aggressor counts",
        json.dumps(mech["aggressor_counts"], indent=2),
        "",
        "## Prefix",
        prefix["prefix_status"],
        "",
        "Outcome comparison after unblind only — mechanical verdict not changed.",
        "No threshold/rule change from CASE_03.",
    ]
    (out_dir / spec.manual_review_name).write_text("\n".join(manual) + "\n", encoding="utf-8")

    after = verify_freeze_snapshot(repo_root, "after", spec=spec)
    write_json(out_dir / "freeze_verification_after.json", after)
    if not after.get("ok"):
        verdict = spec.freeze_fail

    summary = {
        "verdict": verdict,
        "reaction": reaction,
        "candidate_direction": candidate_direction,
        "first_available_ts": first_available_ts,
        "entry_price": entry_price,
        "mechanical_trade_verdict": mechanical_trade_verdict,
        "freeze_before_ok": before.get("ok"),
        "freeze_after_ok": after.get("ok"),
        "prefix_status": prefix["prefix_status"],
        "outcome_comparison": outcome_cmp,
        "elapsed_s": elapsed,
        "head": head,
        "branch": branch,
    }
    write_json(out_dir / "summary.json", summary)
    return summary
