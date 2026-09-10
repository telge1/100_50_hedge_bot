"""Headroom schema keys prepared for later trading-rule work — not activated."""

from __future__ import annotations

from typing import Any

# Keys only — no 0.41% rule, no executable-entry trading activation.
HEADROOM_SCHEMA_KEYS = (
    "executable_best_bid",
    "executable_best_ask",
    "wall_side",
    "decision_time",
    "replay_epoch",
    "wall_candidates_reference",
    "defense_chain_id",
    "book_checkpoint_id",
    "source_manifest_hash",
)

HEADROOM_RULE_ACTIVATED = False
HEADROOM_RULE_NOTE = (
    "Headroom schema keys are emitted as placeholders only. "
    "No trading rule (including any 0.41% rule) is activated in this package."
)


def empty_headroom_placeholders(
    *,
    wall_side: str | None = None,
    decision_time: str | None = None,
    replay_epoch: int | None = None,
    source_manifest_hash: str | None = None,
    wall_candidates_reference: str | None = None,
    defense_chain_id: str | None = None,
    book_checkpoint_id: str | None = None,
    executable_best_bid: float | None = None,
    executable_best_ask: float | None = None,
) -> dict[str, Any]:
    return {
        "executable_best_bid": executable_best_bid,
        "executable_best_ask": executable_best_ask,
        "wall_side": wall_side,
        "decision_time": decision_time,
        "replay_epoch": replay_epoch,
        "wall_candidates_reference": wall_candidates_reference,
        "defense_chain_id": defense_chain_id,
        "book_checkpoint_id": book_checkpoint_id,
        "source_manifest_hash": source_manifest_hash,
        "headroom_rule_activated": HEADROOM_RULE_ACTIVATED,
        "headroom_rule_note": HEADROOM_RULE_NOTE,
    }
