"""Reference parity checks — hard abort on failure."""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import constants as C
from .labels import label_parity_fields
from .reference_filter import entry_rule_ok, filter_reference_trades, is_excluded_symbol, is_reference_trade


class ReferenceParityError(Exception):
    def __init__(self, summary: dict[str, Any]):
        self.summary = summary
        super().__init__(summary.get("message", "FAILED_REFERENCE_PARITY"))


def check_reference_parity(
    *,
    checkpoint_candidates: list[dict[str, Any]],
    checkpoint_trades: list[dict[str, Any]],
    enriched_rows: list[dict[str, Any]] | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Validate frozen reference cell integrity before/after enrichment."""
    sym = (symbol or "").upper()
    if sym and is_excluded_symbol(sym):
        return {
            "parity_pass": True,
            "skipped_excluded_symbol": True,
            "symbol": sym,
            "n_checkpoint_candidates": 0,
            "n_reference_candidates": 0,
            "n_enriched_candidates": 0,
            "n_duplicate_candidate_ids": 0,
            "n_missing_features": 0,
            "n_label_mismatches": 0,
            "n_entry_mismatches": 0,
            "all_labels_unchanged": True,
            "status": "SKIPPED_EXCLUDED",
        }

    # Candidates in reference scope
    cands = [
        c
        for c in checkpoint_candidates
        if not is_excluded_symbol(str(c.get("symbol") or sym))
        and str(c.get("timeframe")) == C.REF_TIMEFRAME
        and str(c.get("mode_id")) == C.REF_MODE
        and str(c.get("core_research_verdict")) == C.REF_GROUP
    ]
    ref_trades = filter_reference_trades(checkpoint_trades, symbol=sym)
    ids = [str(t.get("candidate_id")) for t in ref_trades]
    dup = sum(1 for _, n in Counter(ids).items() if n > 1)

    entry_mismatches = 0
    for t in ref_trades:
        if not entry_rule_ok(t.get("decision_at"), t.get("entry_at")):
            entry_mismatches += 1

    # Exactly one trade per candidate among reference trades
    trade_by_cid: dict[str, list] = {}
    for t in ref_trades:
        trade_by_cid.setdefault(str(t.get("candidate_id")), []).append(t)
    multi = {k: v for k, v in trade_by_cid.items() if len(v) != 1}

    n_enriched = len(enriched_rows or [])
    n_missing_features = 0
    n_label_mismatches = 0
    if enriched_rows is not None:
        by_id = {str(t.get("candidate_id")): t for t in ref_trades}
        for row in enriched_rows:
            cid = str(row.get("feature__candidate_id") or row.get("candidate_id"))
            src = by_id.get(cid)
            if src is None:
                n_label_mismatches += 1
                continue
            for k, v in label_parity_fields(src).items():
                lk = f"{C.LABEL_PREFIX}{k}"
                if row.get(lk) != v and not (row.get(lk) is None and v is None):
                    # float tolerance for pnl
                    if k in ("gross_return_pct", "net_return_pct", "net_pnl_usdt"):
                        try:
                            if abs(float(row.get(lk)) - float(v)) < 1e-9:
                                continue
                        except (TypeError, ValueError):
                            pass
                    n_label_mismatches += 1
                    break
            # Count missing feature values (coverage not OK)
            for k, v in row.items():
                if k.startswith(C.FEATURE_PREFIX) and k.endswith("__coverage_status"):
                    if v not in ("OK",) and row.get(k.replace("__coverage_status", "")) is None:
                        # only count primary value columns' missing — avoid double counting meta
                        pass
            for k, v in row.items():
                if (
                    k.startswith(C.FEATURE_PREFIX)
                    and not k.endswith(("__coverage_status", "__missing_reason", "__causal", "__feature_asof", "__source_table"))
                    and v is None
                ):
                    n_missing_features += 1

    parity_pass = (
        dup == 0
        and entry_mismatches == 0
        and len(multi) == 0
        and n_label_mismatches == 0
        and (enriched_rows is None or n_enriched == len(ref_trades) or (n_enriched == 0 and len(ref_trades) == 0))
    )
    # When enriching, require 1:1 with reference trades
    if enriched_rows is not None and n_enriched != len(set(ids)):
        # allow skip if duplicates already failed
        if dup == 0 and len(set(ids)) != n_enriched:
            parity_pass = False

    summary = {
        "parity_pass": parity_pass,
        "symbol": sym or None,
        "n_checkpoint_candidates": len(cands),
        "n_reference_candidates": len(set(ids)),
        "n_enriched_candidates": n_enriched,
        "n_duplicate_candidate_ids": dup,
        "n_missing_features": n_missing_features,
        "n_label_mismatches": n_label_mismatches,
        "n_entry_mismatches": entry_mismatches,
        "n_multi_trade_candidate_ids": len(multi),
        "all_labels_unchanged": n_label_mismatches == 0,
        "status": "OK" if parity_pass else C.STATUS_FAILED_PARITY,
        "message": None if parity_pass else C.STATUS_FAILED_PARITY,
    }
    return summary


def assert_parity_or_raise(summary: dict[str, Any]) -> None:
    if not summary.get("parity_pass"):
        raise ReferenceParityError(summary)
