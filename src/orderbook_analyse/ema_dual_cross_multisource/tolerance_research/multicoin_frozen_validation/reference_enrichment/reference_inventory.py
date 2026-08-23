"""Load and validate the frozen reference cell before any market queries."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import constants as C
from .parity import ReferenceParityError, assert_parity_or_raise, check_reference_parity
from .reference_filter import (
    entry_rule_ok,
    filter_reference_trades,
    is_excluded_symbol,
    join_candidates_trades,
)


class EmptyFrozenReferenceError(Exception):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(detail.get("message", C.STATUS_EMPTY_REFERENCE))


def load_source_checkpoint(input_dir: Path, symbol: str) -> dict[str, Any]:
    path = input_dir / "checkpoints" / f"{symbol.upper()}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def inventory_frozen_reference(
    input_dir: Path,
    symbols: list[str],
) -> dict[str, Any]:
    """Scan frozen validation checkpoints and apply reference filters (no DB)."""
    trades_before = 0
    candidates_before = 0
    ref_pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    per_symbol: dict[str, dict[str, Any]] = {}
    source_incomplete: list[str] = []

    for symbol in symbols:
        if is_excluded_symbol(symbol):
            continue
        src = load_source_checkpoint(input_dir, symbol)
        cands = src.get("candidates") or []
        trades = src.get("trades") or []
        candidates_before += len(cands)
        trades_before += len(trades)

        if src.get("status") != "COMPLETE":
            source_incomplete.append(symbol)
            per_symbol[symbol] = {
                "source_status": src.get("status"),
                "n_trades_total": len(trades),
                "n_reference_trades": 0,
                "n_pairs": 0,
            }
            continue

        # Hard per-symbol parity on reference slice before any enrichment
        parity = check_reference_parity(
            checkpoint_candidates=cands,
            checkpoint_trades=trades,
            enriched_rows=None,
            symbol=symbol,
        )
        assert_parity_or_raise(parity)

        pairs = join_candidates_trades(cands, trades)
        ref_trades = filter_reference_trades(trades, symbol=symbol)
        for cand, trade in pairs:
            if not entry_rule_ok(trade.get("decision_at"), trade.get("entry_at")):
                raise ReferenceParityError(
                    {
                        "parity_pass": False,
                        "status": C.STATUS_FAILED_PARITY,
                        "message": C.STATUS_FAILED_PARITY,
                        "symbol": symbol,
                        "reason": "ENTRY_BEFORE_DECISION",
                        "candidate_id": trade.get("candidate_id"),
                    }
                )
            ref_pairs.append((symbol, cand, trade))

        per_symbol[symbol] = {
            "source_status": src.get("status"),
            "n_trades_total": len(trades),
            "n_reference_trades": len(ref_trades),
            "n_pairs": len(pairs),
            "parity": parity,
        }

    ids = [str(t.get("candidate_id")) for _, _, t in ref_pairs]
    dup = sum(1 for _, n in Counter(ids).items() if n > 1)
    if dup:
        raise ReferenceParityError(
            {
                "parity_pass": False,
                "status": C.STATUS_FAILED_PARITY,
                "message": C.STATUS_FAILED_PARITY,
                "reason": "DUPLICATE_CANDIDATE_IDS_ACROSS_SYMBOLS",
                "n_duplicate_candidate_ids": dup,
            }
        )

    detail = {
        "reference_input_path": str(input_dir),
        "filters": {
            "exclude_symbols": sorted(C.EXCLUDE_SYMBOLS),
            "timeframe": C.REF_TIMEFRAME,
            "mode": C.REF_MODE,
            "supportive_state": C.REF_GROUP,
            "policy": C.REF_STRATEGY_KEY,
            "entry_rule": C.ENTRY_RULE,
        },
        "reference_rows_before_filter": trades_before,
        "candidates_before_filter": candidates_before,
        "reference_rows_after_filter": len(ref_pairs),
        "unique_candidate_ids": len(set(ids)),
        "symbols_scanned": len(symbols),
        "symbols_with_reference": sum(1 for s, info in per_symbol.items() if info.get("n_pairs", 0) > 0),
        "source_incomplete_symbols": source_incomplete,
        "per_symbol": per_symbol,
        "candidate_ids": ids,
    }

    if len(ref_pairs) == 0:
        detail["message"] = C.STATUS_EMPTY_REFERENCE
        detail["status"] = C.STATUS_EMPTY_REFERENCE
        raise EmptyFrozenReferenceError(detail)

    detail["status"] = "OK"
    detail["message"] = None
    detail["pairs"] = ref_pairs
    return detail
