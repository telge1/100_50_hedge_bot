#!/usr/bin/env python3
"""BT-D41 causal pending short limit order lifecycle pilot (no fills)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT = ROOT / "results/phase_bt_d41_pending_lifecycle_v1"

for p in (
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
    ROOT / "tools/phase_bt_d41_pending_lifecycle_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from candidate_identity_v1 import candidate_from_signal_band  # noqa: E402
from candidate_state_v1 import DynamicCandidateStateV1  # noqa: E402
from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from pending_lifecycle_v1 import (  # noqa: E402
    CONTRACT,
    EVENT_CANCEL,
    EVENT_CREATE,
    EVENT_KEEP,
    EVENT_NO_CANDIDATE,
    EVENT_PLANNED_SCANNER,
    EVENT_POTENTIAL_FILL_UNRESOLVED,
    EVENT_REPLACE,
    PendingOrderV1,
    intrabar_potential_fill_events,
    make_pending_from_scanner,
    next_5m_close_after,
    next_5m_open_after_close,
    review_pending_at_close,
)
from pool_first_signal_v1 import detect_short_signal_pool_first  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402

REF_1535 = "2026-09-11T15:35:00Z"


def _order_id(setup_id: str, decision_utc: str) -> str:
    raw = f"{CONTRACT}|{setup_id}|{decision_utc}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _event_row(ev: Any, seq: int, pending: PendingOrderV1 | None) -> dict[str, Any]:
    return {
        "seq": seq,
        "event_type": ev.event_type,
        "decision_close_utc": ev.decision_close_utc,
        "intent_created_utc": ev.intent_created_utc,
        "earliest_eligible_execution_utc": ev.earliest_eligible_execution_utc,
        "next_review_utc": ev.next_review_utc,
        "lifecycle_state": ev.lifecycle_state,
        "pool_lower": ev.pool_lower,
        "pool_upper": ev.pool_upper,
        "pool_key": ev.pool_key,
        "origin_ids": ev.origin_ids,
        "origin_set_key": ev.origin_set_key,
        "planned_entry": ev.planned_entry,
        "planned_sl": ev.planned_sl,
        "planned_tp": ev.planned_tp,
        "selection_reason": ev.selection_reason,
        "prior_pool_key": ev.prior_pool_key,
        "prior_planned_entry": ev.prior_planned_entry,
        "prior_planned_sl": ev.prior_planned_sl,
        "new_pool_key": ev.new_pool_key,
        "cancel_reason": ev.cancel_reason,
        "detail": ev.detail,
        "active_order_id": pending.order_id if pending else None,
    }


def run_pilot(
    config: dict[str, Any],
    prices: dict[str, dict[str, float]],
    bars: list[str],
    setups_by_close: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    density = create_density_session(config)
    dynamic = DynamicCandidateStateV1()
    pool_age: dict[str, int] = {}

    pending: PendingOrderV1 | None = None
    events: list[dict[str, Any]] = []
    pending_snapshots: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    cancel_reasons: dict[str, int] = defaultdict(int)
    seq = 0
    intrabar_checked: set[tuple[str, str]] = set()
    had_pending_last_close = False

    def push(ev_obj: Any, p: PendingOrderV1 | None) -> None:
        nonlocal seq
        seq += 1
        events.append(_event_row(ev_obj, seq, p))

    for bar in bars:
        try:
            snap = density.collect_at_close(bar)
        except KeyError:
            continue
        candle = prices.get(bar)
        if not candle:
            continue

        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1

        if pending is not None and bar >= pending.earliest_eligible_execution_utc:
            key = (pending.order_id, bar)
            if key not in intrabar_checked:
                intrabar_checked.add(key)
                ohlc = prices[bar]
                for uev in intrabar_potential_fill_events(
                    pending, bar_utc=bar, ohlc=ohlc, decision_close_utc=bar
                ):
                    push(uev, pending)
                    unresolved_rows.append(
                        {
                            "order_id": pending.order_id,
                            "touch_bar_utc": bar,
                            "observed_at_decision_close_utc": bar,
                            "planned_entry": pending.planned_entry,
                            "bar_high": ohlc.get("h"),
                            "status": "POTENTIAL_FILL_UNRESOLVED",
                        }
                    )

        if pending is not None:
            review = review_pending_at_close(
                config,
                decision_close_utc=bar,
                pending=pending,
                candle=candle,
                snapshot=snap,
                pool_age_bars=pool_age,
            )
            for rev_ev in review.events:
                push(rev_ev, pending if rev_ev.event_type != EVENT_CANCEL else None)
                if rev_ev.cancel_reason:
                    cancel_reasons[rev_ev.cancel_reason] += 1
            pending = review.pending

        sig = detect_short_signal_pool_first(
            config,
            decision_close_utc=bar,
            order_active_from_utc=next_5m_open_after_close(bar),
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
            blocked_pool_keys=None,
        )
        selected = None
        if sig is not None:
            for b in snap.get("upper_bands") or []:
                if pool_key(float(b["lower_price"]), float(b["upper_price"])) == sig.pool_key:
                    selected = candidate_from_signal_band(b)
                    break

        tr = dynamic.process_bar(config, decision_utc=bar, snapshot=snap, selected=selected)
        setup = setups_by_close.get(bar)

        if tr.emit_setup and setup is not None:
            push(
                type(
                    "E",
                    (),
                    {
                        "event_type": EVENT_PLANNED_SCANNER,
                        "decision_close_utc": bar,
                        "intent_created_utc": bar,
                        "earliest_eligible_execution_utc": next_5m_open_after_close(bar),
                        "next_review_utc": next_5m_close_after(bar),
                        "lifecycle_state": "PLANNED",
                        "pool_lower": float(setup["pool_lower"]),
                        "pool_upper": float(setup["pool_upper"]),
                        "pool_key": setup.get("upper_entry_pool_id"),
                        "origin_ids": setup.get("upper_origin_ids"),
                        "origin_set_key": setup.get("origin_set_key"),
                        "planned_entry": float(setup["planned_entry"]),
                        "planned_sl": float(setup["planned_sl"]),
                        "planned_tp": float(setup["planned_tp"]) if setup.get("planned_tp") else None,
                        "selection_reason": tr.selection_reason,
                        "prior_pool_key": None,
                        "prior_planned_entry": None,
                        "prior_planned_sl": None,
                        "new_pool_key": None,
                        "cancel_reason": None,
                        "detail": f"scanner_setup_id={setup.get('setup_id')}",
                    },
                )(),
                pending,
            )

            if pending is not None:
                prior = pending
                push(
                    type(
                        "E",
                        (),
                        {
                            "event_type": EVENT_REPLACE,
                            "decision_close_utc": bar,
                            "intent_created_utc": prior.intent_created_utc,
                            "earliest_eligible_execution_utc": prior.earliest_eligible_execution_utc,
                            "next_review_utc": next_5m_close_after(bar),
                            "lifecycle_state": "REPLACED",
                            "pool_lower": float(setup["pool_lower"]),
                            "pool_upper": float(setup["pool_upper"]),
                            "pool_key": setup.get("upper_entry_pool_id"),
                            "origin_ids": setup.get("upper_origin_ids"),
                            "origin_set_key": setup.get("origin_set_key"),
                            "planned_entry": float(setup["planned_entry"]),
                            "planned_sl": float(setup["planned_sl"]),
                            "planned_tp": float(setup["planned_tp"]) if setup.get("planned_tp") else None,
                            "selection_reason": tr.selection_reason,
                            "prior_pool_key": prior.pool_key,
                            "prior_planned_entry": prior.planned_entry,
                            "prior_planned_sl": prior.planned_sl,
                            "new_pool_key": setup.get("upper_entry_pool_id"),
                            "cancel_reason": "SCANNER_EMIT_SUPERSEDES_PENDING",
                            "detail": tr.invalidation_reason,
                        },
                    )(),
                    None,
                )
                cancel_reasons["SCANNER_EMIT_SUPERSEDES_PENDING"] += 1
                pending = None

            oid = _order_id(str(setup.get("setup_id")), bar)
            pending = make_pending_from_scanner(
                order_id=oid,
                setup=setup,
                selection_reason=tr.selection_reason,
            )
            push(
                type(
                    "E",
                    (),
                    {
                        "event_type": EVENT_CREATE,
                        "decision_close_utc": bar,
                        "intent_created_utc": bar,
                        "earliest_eligible_execution_utc": pending.earliest_eligible_execution_utc,
                        "next_review_utc": next_5m_close_after(bar),
                        "lifecycle_state": "PENDING",
                        "pool_lower": pending.pool_lower,
                        "pool_upper": pending.pool_upper,
                        "pool_key": pending.pool_key,
                        "origin_ids": pending.origin_ids,
                        "origin_set_key": pending.origin_set_key,
                        "planned_entry": pending.planned_entry,
                        "planned_sl": pending.planned_sl,
                        "planned_tp": pending.planned_tp,
                        "selection_reason": tr.selection_reason,
                        "prior_pool_key": None,
                        "prior_planned_entry": None,
                        "prior_planned_sl": None,
                        "new_pool_key": None,
                        "cancel_reason": None,
                        "detail": None,
                    },
                )(),
                pending,
            )
            pending_snapshots.append(
                {
                    "order_id": oid,
                    "created_decision_close_utc": bar,
                    "scanner_setup_id": setup.get("setup_id"),
                    "lifecycle_state": pending.lifecycle_state,
                    "decision_close_utc": pending.decision_close_utc,
                    "intent_created_utc": pending.intent_created_utc,
                    "earliest_eligible_execution_utc": pending.earliest_eligible_execution_utc,
                    "pool_lower": pending.pool_lower,
                    "pool_upper": pending.pool_upper,
                    "pool_key": pending.pool_key,
                    "origin_set_key": pending.origin_set_key,
                    "planned_entry": pending.planned_entry,
                    "planned_sl": pending.planned_sl,
                    "planned_tp": pending.planned_tp,
                    "selection_reason": pending.selection_reason,
                }
            )
        elif pending is None and selected is None and had_pending_last_close:
            push(
                type(
                    "E",
                    (),
                    {
                        "event_type": EVENT_NO_CANDIDATE,
                        "decision_close_utc": bar,
                        "intent_created_utc": None,
                        "earliest_eligible_execution_utc": None,
                        "next_review_utc": next_5m_close_after(bar),
                        "lifecycle_state": "NO_CANDIDATE",
                        "pool_lower": None,
                        "pool_upper": None,
                        "pool_key": None,
                        "origin_ids": None,
                        "origin_set_key": None,
                        "planned_entry": None,
                        "planned_sl": None,
                        "planned_tp": None,
                        "selection_reason": None,
                        "prior_pool_key": None,
                        "prior_planned_entry": None,
                        "prior_planned_sl": None,
                        "new_pool_key": None,
                        "cancel_reason": None,
                        "detail": "pending_cleared_no_scanner_emit",
                    },
                )(),
                None,
            )

        had_pending_last_close = pending is not None

    return {
        "events": events,
        "pending_orders": pending_snapshots,
        "cancel_reasons": dict(cancel_reasons),
        "unresolved": unresolved_rows,
        "final_pending": pending,
    }


def build_ref_1535_timeline(events: list[dict[str, Any]], setups: list[dict[str, Any]]) -> dict[str, Any]:
    ep_setup = next(s for s in setups if s["decision_utc"] == REF_1535)
    idx = setups.index(ep_setup) + 1
    later = [s for s in setups if s["decision_utc"] >= REF_1535][:12]
    timeline = []
    for s in later:
        bar = s["decision_utc"]
        bar_events = [e for e in events if e["decision_close_utc"] == bar and e["event_type"] != EVENT_NO_CANDIDATE]
        timeline.append(
            {
                "decision_close_utc": bar,
                "setup_episode_index": setups.index(s) + 1,
                "scanner_selection_reason": s.get("selection_reason"),
                "pool_lower": s.get("pool_lower"),
                "pool_upper": s.get("pool_upper"),
                "planned_entry": s.get("planned_entry"),
                "planned_sl": s.get("planned_sl"),
                "planned_tp": s.get("planned_tp"),
                "lifecycle_events": bar_events,
            }
        )
    return {
        "reference_decision_close_utc": REF_1535,
        "reference_episode_index": idx,
        "reference_setup": {
            k: ep_setup.get(k)
            for k in (
                "setup_id",
                "decision_utc",
                "pool_lower",
                "pool_upper",
                "planned_entry",
                "planned_sl",
                "planned_tp",
                "selection_reason",
            )
        },
        "subsequent_closes": timeline,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    setups_doc = json.loads((RUN_DIR / "setups.json").read_text(encoding="utf-8"))
    setups = setups_doc["setups"]
    setups_by_close = {s["decision_utc"]: s for s in setups}

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_excl)

    result = run_pilot(config, prices, bars, setups_by_close)
    events = result["events"]
    ref_tl = build_ref_1535_timeline(events, setups)

    cancel_rows = [{"cancel_reason": k, "count": v} for k, v in sorted(result["cancel_reasons"].items())]

    counts = defaultdict(int)
    for e in events:
        counts[e["event_type"]] += 1

    summary = f"""# BT-D41 pending lifecycle summary

Contract: `{CONTRACT}`

## Event counts
{json.dumps(dict(counts), indent=2)}

## Scanner alignment
- BT-D26 PLANNED setups in run: **{len(setups)}**
- PLANNED_SCANNER events: **{counts.get(EVENT_PLANNED_SCANNER, 0)}**
- CREATE events: **{counts.get(EVENT_CREATE, 0)}**
- Pending orders snapshot rows: **{len(result['pending_orders'])}**

## Cancel reasons
{json.dumps(result['cancel_reasons'], indent=2)}

## Intrabar (no fill simulation)
- POTENTIAL_FILL_UNRESOLVED events: **{counts.get(EVENT_POTENTIAL_FILL_UNRESOLVED, 0)}**
- Unique touch intervals logged: **{len(result['unresolved'])}**

## Reference 15:35 UTC
Episode index **{ref_tl['reference_episode_index']}** — see `reference_1535_timeline.json`.

## Future interface
`FILLED` state reserved; BT-D41 does not simulate execution or retroactive cancel before unresolved touch.

## Determinism
Single forward replay; decision-close-only landscape; intent active from next 5m open.
"""
    write_csv(OUT / "lifecycle_events.csv", events)
    write_csv(OUT / "pending_orders.csv", result["pending_orders"])
    write_csv(OUT / "cancel_reasons.csv", cancel_rows)
    write_csv(OUT / "unresolved_fill_intervals.csv", result["unresolved"])
    (OUT / "reference_1535_timeline.json").write_text(json.dumps(ref_tl, indent=2), encoding="utf-8")
    (OUT / "summary.md").write_text(summary, encoding="utf-8")

    print(
        json.dumps(
            {
                "events": len(events),
                "creates": counts.get(EVENT_CREATE, 0),
                "planned_scanner": counts.get(EVENT_PLANNED_SCANNER, 0),
                "unresolved": len(result["unresolved"]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
