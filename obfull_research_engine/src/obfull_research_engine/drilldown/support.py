"""Candidate support vs event trace (not profitability)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .imbalance import assess_imbalance_shift, is_imbalance_candidate


SUPPORT_RULES_VERSION = "event_drilldown_v1.support.1"


def assess_support(
    *,
    candidate_type: str,
    candidate_id: str,
    attribution_rows: list[dict[str, Any]],
    refill_rows: list[dict[str, Any]],
    wall_rows: list[dict[str, Any]],
    states_100ms: list[dict[str, Any]],
    trigger_ts: pd.Timestamp,
    causal_end: pd.Timestamp,
) -> dict[str, Any]:
    """Map event-trace evidence to support_status for the 1s candidate label."""
    trigger_ts = pd.to_datetime(trigger_ts, utc=True)
    causal_end = pd.to_datetime(causal_end, utc=True)
    # trigger bucket 100ms rows
    bucket_rows = [
        r
        for r in states_100ms
        if trigger_ts <= pd.to_datetime(r["bucket_start"], utc=True) < causal_end
    ]
    buy_n = sum(float(r["taker_buy_notional"]) for r in bucket_rows)
    sell_n = sum(float(r["taker_sell_notional"]) for r in bucket_rows)
    ask_rem = sum(float(r["ask_removed_notional"]) for r in bucket_rows)
    bid_rem = sum(float(r["bid_removed_notional"]) for r in bucket_rows)
    ask_add = sum(float(r["ask_added_notional"]) for r in bucket_rows)
    bid_add = sum(float(r["bid_added_notional"]) for r in bucket_rows)

    exec_ask = sum(
        float(r["matched_trade_notional"])
        for r in attribution_rows
        if r["side"] == "ask" and r["attribution_class"] == "EXECUTED_LIKELY"
    )
    exec_bid = sum(
        float(r["matched_trade_notional"])
        for r in attribution_rows
        if r["side"] == "bid" and r["attribution_class"] == "EXECUTED_LIKELY"
    )
    exact_refills = sum(1 for r in refill_rows if r.get("refill_type") == "EXACT_REFILL")
    reasons: list[str] = []
    contradictions: list[str] = []
    exact: list[str] = []
    proxy: list[str] = []

    ctype = candidate_type
    status = "INCONCLUSIVE"

    def finish(st: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        out = {
            "candidate_id": candidate_id,
            "candidate_type": ctype,
            "support_status": st,
            "support_reasons": reasons,
            "contradiction_reasons": contradictions,
            "exact_evidence": exact,
            "proxy_evidence": proxy,
            "ordering_limitations": ["cross_source_equal_timestamps_may_be_ambiguous"],
            "support_rules_version": SUPPORT_RULES_VERSION,
        }
        if extra:
            out.update(extra)
        return out

    if is_imbalance_candidate(ctype):
        imb = assess_imbalance_shift(
            candidate_type=ctype,
            states_100ms=states_100ms,
            trigger_ts=trigger_ts,
            causal_end=causal_end,
        )
        reasons.extend(imb.get("support_reasons") or [])
        contradictions.extend(imb.get("contradiction_reasons") or [])
        exact.extend(imb.get("exact_evidence") or [])
        proxy.extend(imb.get("proxy_evidence") or [])
        extra = {
            k: v
            for k, v in imb.items()
            if k
            not in {
                "support_status",
                "support_reasons",
                "contradiction_reasons",
                "exact_evidence",
                "proxy_evidence",
            }
        }
        return finish(str(imb["support_status"]), extra)

    if "BUY_AGGRESSION" in ctype or ctype == "BUYER_CONTROL_PROXY":
        if buy_n > sell_n and buy_n > 0:
            reasons.append("taker_buy_dominates_trigger_bucket")
            exact.append(f"taker_buy={buy_n:.2f}")
            status = "SUPPORTED_BY_EVENT_TRACE"
        elif buy_n > 0:
            reasons.append("some_buy_notional")
            status = "PARTIALLY_SUPPORTED"
        else:
            contradictions.append("no_taker_buy_in_trigger_bucket")
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        if ctype == "BUYER_CONTROL_PROXY" and ask_rem > 0:
            proxy.append("ask_liquidity_removed_with_buys")
        return finish(status)

    if "SELL_AGGRESSION" in ctype or ctype == "SELLER_CONTROL_PROXY":
        if sell_n > buy_n and sell_n > 0:
            reasons.append("taker_sell_dominates_trigger_bucket")
            exact.append(f"taker_sell={sell_n:.2f}")
            status = "SUPPORTED_BY_EVENT_TRACE"
        elif sell_n > 0:
            status = "PARTIALLY_SUPPORTED"
            reasons.append("some_sell_notional")
        else:
            contradictions.append("no_taker_sell_in_trigger_bucket")
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        return finish(status)

    if "ASK_LIQUIDITY_REMOVE" in ctype or ctype == "ASK_LIQUIDITY_VACUUM_PROXY":
        if ask_rem > 0:
            reasons.append("ask_removed_in_trigger_bucket")
            exact.append(f"ask_removed={ask_rem:.2f}")
            status = "SUPPORTED_BY_EVENT_TRACE" if ask_rem >= bid_rem else "PARTIALLY_SUPPORTED"
        else:
            contradictions.append("no_ask_removal")
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        if exec_ask > 0:
            proxy.append(f"executed_likely_ask_notional={exec_ask:.2f}")
        return finish(status)

    if "BID_LIQUIDITY_REMOVE" in ctype or ctype == "BID_LIQUIDITY_VACUUM_PROXY":
        if bid_rem > 0:
            reasons.append("bid_removed_in_trigger_bucket")
            status = "SUPPORTED_BY_EVENT_TRACE" if bid_rem >= ask_rem else "PARTIALLY_SUPPORTED"
        else:
            contradictions.append("no_bid_removal")
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        return finish(status)

    if "ASK_LIQUIDITY_ADD" in ctype:
        if ask_add > 0:
            reasons.append("ask_added")
            status = "SUPPORTED_BY_EVENT_TRACE"
        else:
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
            contradictions.append("no_ask_add")
        return finish(status)

    if "BID_LIQUIDITY_ADD" in ctype:
        if bid_add > 0:
            reasons.append("bid_added")
            status = "SUPPORTED_BY_EVENT_TRACE"
        else:
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
            contradictions.append("no_bid_add")
        return finish(status)

    if "ABSORPTION" in ctype:
        if "BUY" in ctype:
            if buy_n > 0 and ask_add >= 0:
                reasons.append("buy_aggression_with_ask_liquidity_present_or_added")
                proxy.append("absorption_proxy_only")
                status = "PARTIALLY_SUPPORTED"
            else:
                status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        else:
            if sell_n > 0:
                reasons.append("sell_aggression")
                proxy.append("absorption_proxy_only")
                status = "PARTIALLY_SUPPORTED"
            else:
                status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        return finish(status)

    if ctype == "REFILL_DEFENSE_PROXY":
        if exact_refills > 0:
            reasons.append(f"exact_refills={exact_refills}")
            status = "SUPPORTED_BY_EVENT_TRACE"
        elif any(r.get("refill_type") == "NEARBY_REFILL" for r in refill_rows):
            proxy.append("nearby_refill_only")
            status = "PARTIALLY_SUPPORTED"
        else:
            contradictions.append("no_refill_observed_in_window")
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        return finish(status)

    if "TAKER_DELTA_POSITIVE" in ctype:
        if buy_n - sell_n > 0:
            status = "SUPPORTED_BY_EVENT_TRACE"
            exact.append(f"delta={buy_n-sell_n:.2f}")
        else:
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        return finish(status)

    if "TAKER_DELTA_NEGATIVE" in ctype:
        if sell_n - buy_n > 0:
            status = "SUPPORTED_BY_EVENT_TRACE"
        else:
            status = "NOT_SUPPORTED_BY_EVENT_TRACE"
        return finish(status)

    if "PRICE_" in ctype or "HIGH_VOLUME" in ctype or "LIQUIDATION" in ctype or ctype == "UNCLEAR_HIGH_ACTIVITY":
        reasons.append("price_or_mixed_label_checked_for_activity_only")
        activity = buy_n + sell_n + ask_rem + bid_rem
        status = "PARTIALLY_SUPPORTED" if activity > 0 else "INCONCLUSIVE"
        proxy.append("no_claim_of_true_cause")
        return finish(status)

    if wall_rows:
        proxy.append(f"walls_observed={len(wall_rows)}")
    return finish("INCONCLUSIVE")
