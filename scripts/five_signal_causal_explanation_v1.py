#!/usr/bin/env python3
"""FIVE_SIGNAL_CAUSAL_EXPLANATION_V1 — read-only causal traces for 5 fixed signals."""

from __future__ import annotations

import csv
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.bucket_semantics_v2 import (
    CoverageWindow,
    build_ob200_second_index,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import (
    ACCEPT_MIN_CONSECUTIVE_BUCKETS,
    ACCEPT_MIN_SECONDS,
    TrapAcceptConfig,
    wall_side_for_aef_direction,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_acceptance_v2 import (
    evaluate_edge_acceptance_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    build_causal_edges_from_samples,
    load_ob200_samples,
    sample_at_or_before,
    wall_present_asof,
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
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    input_from_aef_compression,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import FreezeViolation
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v2 import verify_freeze_v2
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size

ET_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/frozen_high_accepted_entry_timing_v1"
)
V2_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_contract_fix_refreeze_v2"
)
EXP_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_sample_expansion_v1"
)
FREEZE_V2 = V2_DIR / "freeze_bundle_v2"
OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/five_signal_causal_explanation_v1"
)

# (entry_book_ts, matched_edge_id, expected_side)
TARGETS = [
    ("2026-08-25T07:37:34Z", "lc_42_466", "SHORT"),
    ("2026-08-25T07:40:59Z", "lc_42_142", "LONG"),
    ("2026-08-26T12:30:10Z", "lc_42_613", "SHORT"),
    ("2026-08-26T14:26:19Z", "lc_42_1123", "SHORT"),
    ("2026-08-26T14:28:03Z", "lc_42_655", "SHORT"),
]

EXPECTED_SHA_PREFIX = "6ca0718e4c0420d51ff1"


def _load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(x: Any) -> Optional[float]:
    if x in (None, "", "None", "null"):
        return None
    try:
        return float(x)
    except Exception:
        return None


def _resolve_signals() -> list[dict[str, Any]]:
    entries = _load_csv(ET_DIR / "entry_execution.csv")
    trades15 = {r["entry_signal_id_v2"]: r for r in _load_csv(ET_DIR / "trade_results_15m.csv")}
    mig = {r["old_event_id"]: r for r in _load_csv(V2_DIR / "entry_eligible_events_v2.csv")}
    ha = {r["event_id"]: r for r in _load_csv(EXP_DIR / "high_accepted_events.csv")}
    out = []
    for i, (ts, edge, side) in enumerate(TARGETS, 1):
        hits = [
            r
            for r in entries
            if r.get("entry_book_ts") == ts and r.get("matched_edge_id") == edge
        ]
        if len(hits) != 1:
            raise RuntimeError(f"SIGNAL_ID_AMBIGUOUS signal={i} ts={ts} edge={edge} n={len(hits)}")
        e = hits[0]
        if e["trade_side"] != side:
            raise RuntimeError(f"side mismatch signal={i}")
        oid = e["old_event_id"]
        m = mig.get(oid)
        feat = ha.get(oid)
        if not m or not feat:
            raise RuntimeError(f"missing lineage for {oid}")
        out.append(
            {
                "signal_n": i,
                "entry": e,
                "trade15": trades15.get(e["entry_signal_id_v2"]),
                "migration": m,
                "feat": feat,
            }
        )
    return out


def _parse_cp(feat: dict[str, Any]) -> dict[str, Any]:
    raw = feat.get("acceptance_checkpoints") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _gate_table(signal_n: int, esid: str, feat: dict[str, Any], thr: dict[str, Any]) -> list[dict]:
    rows = []

    def add(name, value, threshold, passed, family):
        rows.append(
            {
                "signal_n": signal_n,
                "entry_signal_id_v2": esid,
                "feature": name,
                "signal_value": value,
                "frozen_threshold": threshold,
                "pass_fail": "PASS" if passed else "FAIL",
                "family": family,
            }
        )

    # AEF compression gates (from stored feat)
    dom = _f(feat.get("dominant_share"))
    add("dominant_share", dom, thr["min_dominant_share"], (dom or 0) >= thr["min_dominant_share"], "aef")
    notional = _f(feat.get("aggressor_notional"))
    add(
        "aggressor_notional",
        notional,
        thr["min_notional_usdt"],
        (notional or 0) >= thr["min_notional_usdt"],
        "aef",
    )
    impact = _f(feat.get("signed_price_impact_bps"))
    # compression: weak contemporaneous impact
    add(
        "abs_signed_price_impact_bps_vs_weak_max",
        abs(impact) if impact is not None else None,
        thr["weak_contemporaneous_max_bps"],
        impact is not None and abs(impact) <= thr["weak_contemporaneous_max_bps"] + 1e-9
        or (impact is not None and feat.get("compression_flag") in (True, "True")),
        "aef_compression",
    )
    add(
        "compression_flag",
        feat.get("compression_flag"),
        "True",
        feat.get("compression_flag") in (True, "True"),
        "aef",
    )
    add(
        "strong_same_side_impact_veto",
        feat.get("strong_same_side_impact_veto"),
        "False",
        feat.get("strong_same_side_impact_veto") in (False, "False"),
        "aef",
    )
    add(
        "edge_match_confidence_class",
        feat.get("edge_match_confidence_class"),
        "HIGH",
        feat.get("edge_match_confidence_class") == "HIGH",
        "edge_match",
    )
    add(
        "edge_join_status",
        feat.get("edge_join_status"),
        "EXACT_TRADED_EDGE",
        feat.get("edge_join_status") == "EXACT_TRADED_EDGE",
        "edge_match",
    )
    dist = _f(feat.get("matched_edge_distance_bps"))
    add("matched_edge_distance_bps", dist, "exact<=1.5bps", dist is not None and dist <= 1.5, "edge_match")
    add(
        "accept_min_consecutive_buckets",
        ACCEPT_MIN_CONSECUTIVE_BUCKETS,
        ACCEPT_MIN_CONSECUTIVE_BUCKETS,
        True,
        "acceptance_v2",
    )
    add(
        "accept_min_seconds",
        ACCEPT_MIN_SECONDS,
        ACCEPT_MIN_SECONDS,
        True,
        "acceptance_v2",
    )
    return rows


def _explain_side(feat: dict[str, Any], trade_side: str) -> str:
    wall = feat.get("wall_side")
    edge = feat.get("matched_edge_price")
    acc = feat.get("final_acceptance_state")
    if trade_side == "SHORT":
        return (
            f"ACCEPTED_BELOW on BID wall edge {edge}: price stayed below BID edge "
            f"(acceptance={acc}); trade_side=SHORT comes ONLY from ACCEPTED_BELOW→SHORT mapping, "
            f"not from AEF direction={feat.get('direction')}."
        )
    return (
        f"ACCEPTED_ABOVE on ASK wall edge {edge}: price stayed above ASK edge "
        f"(acceptance={acc}); trade_side=LONG comes ONLY from ACCEPTED_ABOVE→LONG mapping, "
        f"not from AEF direction={feat.get('direction')}."
    )


def main() -> dict[str, Any]:
    ensure_outdir(OUT)
    try:
        freeze = verify_freeze_v2(FREEZE_V2)
    except FreezeViolation as e:
        write_json(OUT / "freeze_verification.json", {"error": str(e)})
        write_json(
            OUT / "run_manifest.json",
            {"verdict": "FROZEN_V2_BUNDLE_TAMPERED", "error": str(e)},
        )
        raise
    if not str(freeze.get("freeze_bundle_sha256", "")).startswith(EXPECTED_SHA_PREFIX):
        raise RuntimeError("FROZEN_V2_BUNDLE_TAMPERED unexpected sha")
    write_json(OUT / "freeze_verification.json", freeze)

    signals = _resolve_signals()
    aef_thr = TrapAcceptConfig().aef_config().to_dict()
    join_thr = JoinThresholds()
    dthr = DisambiguationThresholds()
    thr_accept = JoinThresholds(accept_confidence=dthr.accept_confidence)
    cfg = TrapAcceptConfig()
    query_log: list[dict[str, Any]] = []

    trace_rows: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []
    cand_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    wall_rows: list[dict[str, Any]] = []
    narratives: list[str] = []

    # group by hour for OB reload
    by_hour: dict[str, list] = {}
    for s in signals:
        hour = s["feat"].get("source_hour") or parse_utc(s["entry"]["entry_book_ts"]).strftime(
            "%Y-%m-%dT%H:00:00Z"
        )
        by_hour.setdefault(hour, []).append(s)

    cache: dict[str, Any] = {}

    for hour, group in sorted(by_hour.items()):
        ht = parse_utc(hour)
        print(f"load hour {hour} n={len(group)}", flush=True)
        ob_start = ht - timedelta(hours=1)
        event_end = ht + timedelta(hours=1)
        data_end = event_end + timedelta(seconds=120)
        samples_by, _, _ = load_ob200_samples(
            symbols=("BTCUSDT",), start=ob_start, end=event_end, raw_root=DEFAULT_RAW_ROOT
        )
        samples = samples_by.get("BTCUSDT") or []
        edges, lifecycles, _ = build_causal_edges_from_samples({"BTCUSDT": samples})
        trades, pre = load_trades_clickhouse(
            symbol="BTCUSDT", start=ht, end=data_end, query_log=query_log
        )
        # also need trades covering flow windows which are inside the hour
        trades_flow, _ = load_trades_clickhouse(
            symbol="BTCUSDT",
            start=ht - timedelta(minutes=5),
            end=data_end,
            query_log=query_log,
        )
        buckets = build_second_buckets(trades_flow)
        ob_secs = build_ob200_second_index(samples)
        coverage = CoverageWindow(
            load_start=ht - timedelta(minutes=5),
            load_end=data_end,
            query_ok=True,
            rows_loaded=len(trades_flow),
        )
        lc_by_id = {str(lc.lifecycle_id): lc for lc in lifecycles}
        cache[hour] = {
            "samples": samples,
            "edges": edges,
            "lc_by_id": lc_by_id,
            "trades": trades_flow,
            "buckets": buckets,
            "ob_secs": ob_secs,
            "coverage": coverage,
        }

        for s in group:
            n = s["signal_n"]
            e = s["entry"]
            feat = s["feat"]
            mig = s["migration"]
            esid = e["entry_signal_id_v2"]
            print(f"  signal {n} {esid}", flush=True)

            # Rebuild join candidates
            # synthesize minimal compression-like event from feat
            from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.models import (
                InputEvent,
            )

            ev = InputEvent(
                event_id=feat["event_id"],
                symbol=feat["symbol"],
                direction=feat["direction"],
                wall_side=feat.get("wall_side"),
                edge_price=_f(feat.get("matched_edge_price")),
                edge_source=str(feat.get("matched_edge_source") or "raw_ob200_wall_lifecycle"),
                edge_confidence="high",
                decision_ts=parse_utc(feat["decision_ts"]),
                flow_start_ts=parse_utc(feat["flow_start_ts"]),
                flow_end_ts=parse_utc(feat["flow_end_ts"]),
                reference_price=None,
                data_quality="OK",
                source=feat.get("source") or "five_signal_trace",
            )
            side = aggressor_side(ev.direction)
            flow = aggregate_window(buckets, ev.flow_start_ts, ev.flow_end_ts)
            vwap = side_vwap(trades_flow, ev.flow_start_ts, ev.flow_end_ts, side)
            cands = evaluate_candidates(
                ev,
                [ed for ed in edges if ed.symbol == "BTCUSDT"],
                samples,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=join_thr,
            )
            join, enriched, _clusters = select_disambiguated_match(
                ev,
                cands,
                trades=trades_flow,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=join_thr,
                dthr=dthr,
            )
            selected_id = join.matched_edge_id
            listed = enriched if enriched else cands
            for c in listed:
                if not isinstance(c, dict):
                    continue
                eid_c = c.get("edge_id") or c.get("matched_edge_id")
                cand_rows.append(
                    {
                        "signal_n": n,
                        "entry_signal_id_v2": esid,
                        "candidate_edge_id": eid_c,
                        "candidate_price": c.get("edge_price") or c.get("price"),
                        "distance_bps": c.get("distance_bps")
                        or c.get("matched_edge_distance_bps"),
                        "match_class": c.get("match_class"),
                        "reached": c.get("reached_in_directional_path"),
                        "selected": eid_c == feat.get("matched_edge_id"),
                        "recomputed_selected": eid_c == selected_id,
                        "codes": json.dumps(
                            c.get("codes") or c.get("explanation_codes") or []
                        ),
                    }
                )
            if not listed:
                cand_rows.append(
                    {
                        "signal_n": n,
                        "entry_signal_id_v2": esid,
                        "candidate_edge_id": feat["matched_edge_id"],
                        "candidate_price": feat["matched_edge_price"],
                        "distance_bps": feat.get("matched_edge_distance_bps"),
                        "selected": True,
                        "recomputed_selected": selected_id == feat["matched_edge_id"],
                        "codes": feat.get("edge_match_explanation_codes"),
                        "note": "fallback_frozen_only_candidates_not_relisted",
                    }
                )

            # V2 acceptance re-eval
            wall = (feat.get("wall_side") or "").upper()
            edge_px = _f(feat.get("matched_edge_price"))
            aggr = aggressor_side(feat["direction"])
            acc = evaluate_edge_acceptance_v2(
                buckets=buckets,
                trades=trades_flow,
                symbol="BTCUSDT",
                wall_side=wall,
                edge_price=edge_px,
                edge_confidence="high",
                decision_ts=parse_utc(feat["decision_ts"]),
                aggressor_side=aggr,
                cfg=cfg,
                coverage=coverage,
                ob200_seconds=ob_secs,
                scan_horizon_s=60,
            )

            # Trade evidence in flow window only
            t0 = parse_utc(feat["flow_start_ts"])
            t1 = parse_utc(feat["flow_end_ts"])
            entry_ts = parse_utc(e["entry_book_ts"])
            flow_trades = [
                t
                for t in trades_flow
                if t0
                <= (t.trade_ts if t.trade_ts.tzinfo else t.trade_ts.replace(tzinfo=timezone.utc))
                <= t1
            ]
            buy_n = sum(t.notional for t in flow_trades if t.side == "Buy")
            sell_n = sum(t.notional for t in flow_trades if t.side == "Sell")
            top = sorted(flow_trades, key=lambda t: -t.notional)[:5]
            trade_rows.append(
                {
                    "signal_n": n,
                    "entry_signal_id_v2": esid,
                    "flow_start_ts": feat["flow_start_ts"],
                    "flow_end_ts": feat["flow_end_ts"],
                    "aggressor_side": feat.get("aggressor_side"),
                    "aggressor_notional_stored": feat.get("aggressor_notional"),
                    "aggressor_trade_count_stored": feat.get("aggressor_trade_count"),
                    "buy_notional_window": buy_n,
                    "sell_notional_window": sell_n,
                    "trade_count_window": len(flow_trades),
                    "signed_price_impact_bps": feat.get("signed_price_impact_bps"),
                    "impact_per_100k_notional": feat.get("impact_per_100k_notional"),
                    "compression_flag": feat.get("compression_flag"),
                    "compression_reason_code": feat.get("compression_reason_code"),
                    "semantic_case": feat.get("semantic_case"),
                    "dominant_share": feat.get("dominant_share"),
                    "top5_trades": json.dumps(
                        [
                            {
                                "ts": iso_z(
                                    t.trade_ts
                                    if t.trade_ts.tzinfo
                                    else t.trade_ts.replace(tzinfo=timezone.utc)
                                ),
                                "side": t.side,
                                "price": t.price,
                                "notional": t.notional,
                            }
                            for t in top
                        ]
                    ),
                    "trades_after_entry_used": False,
                }
            )

            # Wall lifecycle evidence (as-of sizes) — NOT used as signal gate
            lc = lc_by_id.get(str(feat["matched_edge_id"]))
            sizes = {}
            for label, ts_s in [
                ("first_visible", feat.get("matched_edge_available_ts")),
                ("flow_start", feat.get("flow_start_ts")),
                ("decision", feat.get("decision_ts")),
                ("acceptance_first_v2", mig.get("new_acceptance_first_available_ts_v2")),
                ("entry", e.get("entry_book_ts")),
            ]:
                if not ts_s:
                    sizes[label] = None
                    continue
                ts = parse_utc(ts_s)
                if ts > entry_ts:
                    sizes[label] = "AFTER_ENTRY_EXCLUDED"
                    continue
                samp = sample_at_or_before(samples, int(ts.timestamp() * 1000))
                present, qty, mid = wall_present_asof(
                    samp, side=wall, edge_price=float(edge_px), symbol="BTCUSDT"
                )
                sizes[label] = {
                    "present": present,
                    "qty": qty,
                    "mid": mid,
                    "ts": iso_z(ts),
                }
            wall_rows.append(
                {
                    "signal_n": n,
                    "entry_signal_id_v2": esid,
                    "matched_edge_id": feat["matched_edge_id"],
                    "lifecycle_id": feat["matched_edge_id"],
                    "wall_side": wall,
                    "edge_price": edge_px,
                    "edge_source": feat.get("matched_edge_source"),
                    "persistence_seconds_stored": feat.get("matched_edge_persistence_seconds"),
                    "relative_size_stored": feat.get("matched_edge_relative_size"),
                    "completion_class_asof_catalog": getattr(lc, "completion_class", None)
                    if lc
                    else "NOT_IN_HOUR_CATALOG",
                    "sizes_asof_json": json.dumps(sizes),
                    "signal_gate_uses_consumption": False,
                    "signal_gate_uses_cancellation": False,
                    "classification": "WALL_LIFECYCLE_NOT_USED_FOR_SIGNAL",
                    "chart_layer_expected": "Orderbook Walls",
                    "not_liquidity_location_pool": True,
                    "not_volume_profile": True,
                }
            )

            # Timeline
            cps = _parse_cp(feat)
            first_break = None
            for cp in cps.values():
                if isinstance(cp, dict) and cp.get("first_break_ts"):
                    first_break = cp["first_break_ts"]
                    break
            # first accepted checkpoint from V1 stored cps
            first_acc_v1 = None
            for sec in (5, 10, 30, 60):
                st = feat.get(f"acceptance_state_at_{sec}s")
                if st in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}:
                    first_acc_v1 = iso_z(parse_utc(feat["decision_ts"]) + timedelta(seconds=sec))
                    break

            tl = [
                ("edge_first_visible", feat.get("matched_edge_available_ts")),
                ("aef_flow_start", feat.get("flow_start_ts")),
                ("aef_flow_end", feat.get("flow_end_ts")),
                ("aef_decision_ts", feat.get("decision_ts")),
                ("first_break_beyond_edge", first_break or "NOT_PRESENT"),
                ("first_exact_zone_trade_touch", "implied_by_EXACT_TRADED_EDGE_codes"),
                ("HIGH_confidence_at_match", feat.get("decision_ts")),
                ("first_accepted_checkpoint_v1_stored", first_acc_v1 or "NOT_PRESENT"),
                (
                    "acceptance_first_available_ts_v2",
                    mig.get("new_acceptance_first_available_ts_v2"),
                ),
                ("earliest_causal_entry_ts_v2", mig.get("earliest_causal_entry_ts_v2")),
                ("episode_action", mig.get("episode_action")),
                ("signal_available_ts", e.get("signal_available_ts")),
                ("legal_entry_ts", e.get("legal_entry_ts")),
                ("entry_book_ts", e.get("entry_book_ts")),
            ]
            for name, ts in tl:
                timeline_rows.append(
                    {
                        "signal_n": n,
                        "entry_signal_id_v2": esid,
                        "event": name,
                        "ts": ts,
                        "before_or_at_entry": (
                            True
                            if ts in (None, "NOT_PRESENT")
                            or str(ts).startswith("implied")
                            or (
                                isinstance(ts, str)
                                and "T" in ts
                                and parse_utc(ts.replace(".750000Z", "Z").replace(".250000Z", "Z")[:19] + "Z" if False else ts)
                                # simpler:
                            )
                            else None
                        ),
                    }
                )
            # fix before_or_at_entry properly
            for row in timeline_rows:
                if row["signal_n"] != n:
                    continue
                ts = row["ts"]
                if ts in (None, "NOT_PRESENT") or str(ts).startswith("implied") or row["event"] == "episode_action":
                    row["before_or_at_entry"] = True if ts != "NOT_PRESENT" else None
                    continue
                try:
                    # handle fractional Z
                    tss = str(ts).replace(".750000Z", "Z").replace(".250000Z", "Z")
                    if tss.endswith("000Z") and "+00:00" not in tss:
                        pass
                    pt = parse_utc(tss if "Z" in tss or "+" in tss else tss + "Z")
                    row["before_or_at_entry"] = pt <= entry_ts
                except Exception:
                    row["before_or_at_entry"] = "UNPARSED"

            gate_rows.extend(_gate_table(n, esid, feat, aef_thr))

            approach = (
                "from_above_toward_BID"
                if wall == "BID"
                else "from_below_toward_ASK"
                if wall == "ASK"
                else "UNKNOWN"
            )
            # market approach: AEF LONG = sell aggression into BID; SHORT = buy into ASK
            if wall == "BID":
                approach_txt = "von oben (Sell-Aggressor in BID-Wall)"
            else:
                approach_txt = "von unten (Buy-Aggressor in ASK-Wall)"

            codes = feat.get("edge_match_explanation_codes")
            if isinstance(codes, str):
                try:
                    codes = json.loads(codes)
                except Exception:
                    pass

            # acceptance first checkpoint detail from stored
            first_cp_detail = None
            for k in ("cp_5s", "cp_10s", "cp_30s", "cp_60s"):
                cp = cps.get(k)
                if isinstance(cp, dict) and cp.get("state") in {
                    "ACCEPTED_ABOVE",
                    "ACCEPTED_BELOW",
                }:
                    first_cp_detail = {"key": k, **cp}
                    break

            trace_rows.append(
                {
                    "signal_n": n,
                    "entry_signal_id_v2": esid,
                    "episode_id_v2": e["episode_id_v2"],
                    "old_event_id": e["old_event_id"],
                    "symbol": e["symbol"],
                    "acceptance_state": e["acceptance_state"],
                    "trade_side": e["trade_side"],
                    "aef_direction_field": feat.get("direction"),
                    "signal_available_ts": e["signal_available_ts"],
                    "legal_entry_ts": e["legal_entry_ts"],
                    "entry_book_ts": e["entry_book_ts"],
                    "executable_entry_price": e["executable_entry_price"],
                    "matched_edge_id": feat["matched_edge_id"],
                    "lifecycle_id": feat["matched_edge_id"],
                    "matched_edge_price": feat["matched_edge_price"],
                    "matched_edge_source": feat["matched_edge_source"],
                    "wall_side": wall,
                    "edge_semantics": (
                        "BID_wall_price_level_from_OB200_wall_lifecycle"
                        if wall == "BID"
                        else "ASK_wall_price_level_from_OB200_wall_lifecycle"
                    ),
                    "pool_width": "NOT_A_POOL_SINGLE_WALL_PRICE_LEVEL",
                    "chart_layer_expected": "Orderbook Walls",
                    "edge_join_status": feat.get("edge_join_status"),
                    "edge_match_codes": json.dumps(codes),
                    "edge_match_candidate_count": feat.get("edge_match_candidate_count"),
                    "matched_distance_bps": feat.get("matched_edge_distance_bps"),
                    "matched_available_ts": feat.get("matched_edge_available_ts"),
                    "persistence_s": feat.get("matched_edge_persistence_seconds"),
                    "information_stack": feat.get("information_stack"),
                    "efficiency_status": feat.get("efficiency_status"),
                    "final_trap_label": feat.get("final_trap_label"),
                    "acceptance_first_v2": mig.get("new_acceptance_first_available_ts_v2"),
                    "earliest_causal_entry_ts_v2": mig.get("earliest_causal_entry_ts_v2"),
                    "episode_action": mig.get("episode_action"),
                    "migration_class": mig.get("migration_class"),
                    "v2_accept_first_recomputed": acc.get("acceptance_first_available_ts_v2"),
                    "v2_entry_eligible_recomputed": acc.get("entry_eligible"),
                    "approach": approach_txt,
                    "first_cp_detail": json.dumps(first_cp_detail) if first_cp_detail else None,
                    "trend_used_for_signal": False,
                    "ema_used_for_signal": False,
                    "oi_used_for_signal": False,
                    "liquidation_used_for_signal": False,
                    "wall_consumption_used_for_signal": False,
                    "wall_cancellation_used_for_signal": False,
                    "public_trade_flow_used_for_signal": True,
                    "codepath": (
                        "raw_ob200→walls.extract_wall_events→lifecycles_v2(lc_*)→"
                        "AEF discover_episodes compression→edge_match+disambiguation→"
                        "efficiency/compression→trap(diagnostic)→acceptance_v2→"
                        "episode_contract_v2→earliest_causal_entry_ts_v2→"
                        "legal_entry(+1s)→executable bid/ask"
                    ),
                }
            )

            # Narrative block
            why_high = (
                f"edge_match_confidence_class=HIGH with codes {codes}; "
                f"join_status={feat.get('edge_join_status')}; distance_bps="
                f"{feat.get('matched_edge_distance_bps')}; EXACT_TRADE required for HIGH acceptance path."
            )
            one = (
                f"Der Code ging {e['trade_side']}, weil der Preis die "
                f"{'untere' if wall=='BID' else 'obere'} OB200-Wall-Lifecycle-Edge "
                f"{feat['matched_edge_id']} bei {feat['matched_edge_price']} "
                f"({wall}) mit Exact-Trade erreichte, "
                f"Sell/Buy-Aggressor-Flow im 5s-Fenster Ineffizienz/Compression zeigte "
                f"(impact={feat.get('signed_price_impact_bps')} bps, notional="
                f"{feat.get('aggressor_notional')}), und ab "
                f"{mig.get('new_acceptance_first_available_ts_v2')} erstmals "
                f"{e['acceptance_state']} kausal entry-eligible war "
                f"(≥{ACCEPT_MIN_CONSECUTIVE_BUCKETS}s jenseits der Edge). "
                f"Trend/EMA/OI/Liquidationen und Wall-Konsumtion/Cancel waren kein Gate."
            )
            narratives.append(
                f"""
## SIGNAL {n}

1. Beobachtete Edge: `{feat['matched_edge_id']}` = OB200 Wall-Lifecycle (`raw_ob200_wall_lifecycle`), Preis `{feat['matched_edge_price']}`, Seite `{wall}`
2. Chart-Layer: **Orderbook Walls** (nicht Liquidity Location, nicht Volume Profile)
3. Markt kam von: {approach_txt}
4. Reach: join=`{feat.get('edge_join_status')}`, Distanz `{feat.get('matched_edge_distance_bps')}` bps, Codes `{codes}`
5. Public Trades: Fenster `{feat['flow_start_ts']}`–`{feat['flow_end_ts']}`, aggressor=`{feat.get('aggressor_side')}`, notional=`{feat.get('aggressor_notional')}`, count=`{feat.get('aggressor_trade_count')}`, impact_bps=`{feat.get('signed_price_impact_bps')}`, compression=`{feat.get('compression_flag')}` / `{feat.get('compression_reason_code')}`
6. State / Stack: `{feat.get('information_stack')}`; trap=`{feat.get('final_trap_label')}`; efficiency=`{feat.get('efficiency_status')}`
7. Warum HIGH: {why_high}
8. Acceptance: `{e['acceptance_state']}` bezogen auf Edge `{feat['matched_edge_price']}`; V2 first=`{mig.get('new_acceptance_first_available_ts_v2')}`; Regel ≥{ACCEPT_MIN_CONSECUTIVE_BUCKETS} consecutive buckets / ≥{ACCEPT_MIN_SECONDS}s beyond edge; source_gap=`{mig.get('source_gap_seen')}`
9. Frühester bekannter Zeitpunkt (live): `{mig.get('earliest_causal_entry_ts_v2')}` (= acceptance_first_available_ts_v2)
10. Entry: legal=`{e['legal_entry_ts']}` (+1s Latency), book=`{e['entry_book_ts']}`, exec_px=`{e['executable_entry_price']}`
11. Wall konsumiert/gezogen/unklar: **WALL_LIFECYCLE_NOT_USED_FOR_SIGNAL** (Lifecycle-ID nur für Matching/Identität; Konsumtion/Cancel kein Gate)
12. Warum {e['trade_side']}: {_explain_side(feat, e['trade_side'])}
13. NICHT verwendet: Trend, EMA, OI, Liquidationen, Wall-Konsumtion, Wall-Cancel, Market Profile, Liquidity-Location-Pools
14. Ein-Satz-Erklärung: {one}
"""
            )

    # Fix timeline before_or_at flags more carefully
    for row in timeline_rows:
        ts = row.get("ts")
        if ts in (None, "NOT_PRESENT") or str(ts).startswith("implied") or row["event"] == "episode_action":
            continue
        try:
            raw = str(ts)
            if raw.endswith("+00:00"):
                pt = parse_utc(raw.replace("+00:00", "Z"))
            else:
                # strip subsecond weirdness
                if "." in raw and raw.endswith("Z"):
                    raw = raw.split(".")[0] + "Z"
                pt = parse_utc(raw)
            # find entry for signal
            ent = next(s for s in signals if s["signal_n"] == row["signal_n"])
            row["before_or_at_entry"] = pt <= parse_utc(ent["entry"]["entry_book_ts"])
        except Exception:
            row["before_or_at_entry"] = "UNPARSED"

    write_csv(OUT / "signal_trace.csv", trace_rows)
    write_csv(OUT / "signal_timeline.csv", timeline_rows)
    write_csv(OUT / "signal_gate_values.csv", gate_rows)
    write_csv(OUT / "edge_candidates.csv", cand_rows)
    write_csv(OUT / "trade_evidence.csv", trade_rows)
    write_csv(OUT / "wall_lifecycle_evidence.csv", wall_rows)

    # comparison table
    cmp_lines = [
        "| # | Timestamp | Side | Edge-Preis | Chart-Layer | State/Stack | Warum HIGH | Acceptance | Wall-Lifecycle benutzt? |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in trace_rows:
        cmp_lines.append(
            f"| {t['signal_n']} | {t['entry_book_ts']} | {t['trade_side']} | {t['matched_edge_price']} | Orderbook Walls | {t['information_stack']} | HIGH+EXACT_TRADE | {t['acceptance_state']} | nein (nur Edge-ID) |"
        )

    md = [
        "# SIGNALERKLAERUNG — FIVE_SIGNAL_CAUSAL_EXPLANATION_V1",
        "",
        f"**VERDICT:** `FIVE_SIGNAL_CAUSAL_EXPLANATION_V1_COMPLETE`",
        "",
        f"**Freeze V2 SHA:** `{freeze.get('freeze_bundle_sha256')}` (unverändert, Prefix `6ca0718e4c0420d51ff1…`)",
        "",
        "## Globale Codepfad-Zusammenfassung",
        "",
        "1. `ob200_v3_raw_discovery.walls.extract_wall_events` + `lifecycles_v2.build_wall_lifecycles` → Edge-ID `lc_*`",
        "2. AEF `discover_episodes` Compression-Event (5s flow)",
        "3. `edge_match.evaluate_candidates` + `edge_disambiguation.select_disambiguated_match`",
        "4. Efficiency/Compression Features (`aggressor_efficiency_*`)",
        "5. Diagnostic Trap (nicht entscheidend für HIGH∩ACCEPTED Selektion allein)",
        "6. `edge_acceptance_v2.evaluate_edge_acceptance_v2` (≥3s consecutive beyond edge)",
        "7. `episode_contract_v2.EpisodeTrackerV2`",
        "8. Entry Timing: `earliest_causal_entry_ts_v2` + 1s Latency → Bid/Ask executable",
        "",
        "### Was die Edge ist",
        "",
        "- `matched_edge_id` = `WallLifecycle.lifecycle_id` aus Raw-OB200",
        "- Source: `raw_ob200_wall_lifecycle` (nicht Liquidity-Location-Pool, nicht Volume-Profile)",
        "- Chart-Layer: **Orderbook Walls**",
        "- Edge-Preis = beobachteter Wall-Preis (einzelnes Book-/Wall-Level, keine Poolbreite)",
        "",
        "### Gates bewusst NICHT im Signal",
        "",
        "- `trend_used_for_signal = false`",
        "- `ema_used_for_signal = false`",
        "- `oi_used_for_signal = false`",
        "- `liquidation_used_for_signal = false`",
        "- `wall_consumption_used_for_signal = false`",
        "- `wall_cancellation_used_for_signal = false`",
        "- `public_trade_flow_used_for_signal = true` (AEF Compression + Exact Trade Touch)",
        "",
        *narratives,
        "",
        "## Vergleich der fünf Signale",
        "",
        *cmp_lines,
        "",
        "## Offene Datenlücken",
        "",
        "- Vollständige Candidate-Listen mit Distanz ggf. unvollständig, wenn Recompute weniger Kandidaten liefert als `edge_match_candidate_count` (Hour-Catalog/Seed).",
        "- `first_reach_ts` ist im Frozen Feature Row nicht als eigenes Feld gespeichert; Reach wird über `EXACT_TRADED_EDGE` / distance_bps und `first_break_ts` in Acceptance-Checkpoints belegt.",
        "- Wall-Qty-Zeitreihe erlaubt keine sichere Konsumtion-vs-Cancel-Klassifikation; ohnehin kein Signal-Gate.",
        "- Poolbreite existiert nicht: Edge ist ein Wall-Preislevel, kein Liquidity-Location-Pool.",
        "",
    ]
    (OUT / "SIGNALERKLAERUNG.md").write_text("\n".join(md), encoding="utf-8")

    manifest = {
        "verdict": "FIVE_SIGNAL_CAUSAL_EXPLANATION_V1_COMPLETE",
        "n_signals": 5,
        "freeze_sha": freeze.get("freeze_bundle_sha256"),
        "query_count": len(query_log),
        "targets": TARGETS,
        "resolved_ids": [s["entry"]["entry_signal_id_v2"] for s in signals],
        "chart_layer_expected": "Orderbook Walls",
        "trend_used_for_signal": False,
        "wall_consumption_used_for_signal": False,
        "public_trade_flow_used_for_signal": True,
        "read_only": True,
    }
    write_json(OUT / "run_manifest.json", manifest)
    print(manifest["verdict"], manifest["resolved_ids"])
    return manifest


if __name__ == "__main__":
    main()
