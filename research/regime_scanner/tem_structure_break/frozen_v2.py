"""Frozen AAVE-v2 structure-break semantics snapshot (research-only).

This documents and freezes the monitor rule set used for multicoin generalization.
Do NOT change these values or the monitor logic during the generalization study.
"""

from __future__ import annotations

from research.regime_scanner.tem_structure_break.monitor import SIGNAL_VERSION

FROZEN_RULE_ID = "tem_structure_break_v2_multi_episode_frozen_20260723"

FROZEN_SEMANTICS: dict = {
    "rule_id": FROZEN_RULE_ID,
    "signal_version": SIGNAL_VERSION,
    "frozen": True,
    "no_coin_specific_branches": True,
    "no_optimization_during_evaluation": True,
    "reclaim_window": {
        "definition": (
            "After BREAK_PENDING on a newly closed 4h bar, confirmation/reclaim is evaluated "
            "on the next fully closed 4h bar (close_decision > pending close_decision)."
        ),
        "bars": 1,
        "timeframe": "4h",
        "reclaim_rule": "next_4h_close >= active_break_level",
        "invalidation_rule": "next_4h_close < active_break_level (reclaim failure)",
    },
    "level_priority_4h_arm": [
        "1_live_protected_low_close_break",
        "2_external_bearish_bos_or_close_break_protected_down",
        "3_rebreak_last_reclaim_level",
        "4_frozen_entry_protected_low_4h",
        "5_frozen_entry_protected_low_1h",
    ],
    "frozen_level_semantics": {
        "freeze_at": "entry_bar decision",
        "fields": [
            "protected_low_5m",
            "protected_high_5m",
            "protected_low_1h",
            "protected_low_4h",
            "major_5m_at_entry",
            "h4_major_at_entry",
        ],
        "5m_warning": "close < frozen protected_low_5m → STRUCTURE_WARNING (first only)",
        "1h_telemetry": "1h close < frozen/live 1h PL → BREAK_1H (first only; does not alone invalidate)",
        "4h_floors_post_reclaim": "frozen 4h/1h PLs remain eligible arm sources after reclaim",
    },
    "dynamic_v1lag_semantics": {
        "source": "C3.4B apply_protected_structure on completed HTF bars",
        "note": (
            "When HTF major flips bearish, live protected_low may become NaN while protected_high "
            "is tracked. Live PL close-breaks then cannot fire; rebreak/frozen floors compensate."
        ),
    },
    "rebreak_semantics": {
        "trigger": "newly closed 4h close < last_reclaim_level",
        "sets": "BREAK_PENDING with kind=rebreak_last_reclaim_level",
        "requires": "not currently BREAK_PENDING; not yet LONG_THESIS_INVALIDATED",
    },
    "state_transitions": [
        "ENTRY_* → STRUCTURE_INTACT (soft)",
        "STRUCTURE_* / AT_RISK → BREAK_PENDING (arm)",
        "BREAK_PENDING → RECLAIMED | BREAK_CONFIRMED→LONG_THESIS_INVALIDATED",
        "RECLAIMED → STRUCTURE_AT_RISK (next bar; not full thesis restore)",
        "LONG_THESIS_INVALIDATED sticky",
    ],
    "event_dedup_semantics": {
        "STRUCTURE_WARNING_5m": "first only",
        "BREAK_1H": "first only",
        "MAJOR_ALIGNMENT_LOST_5M": "first only",
        "BREAK_PENDING_4H": "once per episode (break_cycle_id increments)",
        "RECLAIMED / STRUCTURE_AT_RISK / INVALIDATED": "per episode resolution",
    },
    "macro_aware_reclaim_semantics": {
        "definition": (
            "Reclaim restores price above active_break_level only. It does NOT restore "
            "STRUCTURE_INTACT or clear ever_broken. Next state is STRUCTURE_AT_RISK."
        ),
        "macro_htf_not_auto_healed": True,
    },
    "break_episode_semantics": {
        "multiple_episodes_per_trade": True,
        "fields": ["break_cycle_id", "active_break_level", "last_reclaim_level", "ever_broken"],
        "first_star_timestamps": "sticky telemetry for first-ever events",
    },
    "sticky_invalidation_semantics": {
        "terminal_state": "LONG_THESIS_INVALIDATED",
        "no_further_arming_after_invalidation": True,
    },
    "side_support": "long primary only",
    "1d_context": "not in aggregate_complete_from_5m TF set (5m/15m/1h/4h only); entry_d1_major=null",
}


def frozen_semantics_public() -> dict:
    return dict(FROZEN_SEMANTICS)
