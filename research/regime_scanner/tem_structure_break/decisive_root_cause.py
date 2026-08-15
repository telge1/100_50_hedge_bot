"""Root-cause notes: early v2 invalidations (development cases).

Derived only from frozen v2 artifacts + causal structure fields.
No chart timestamps hard-coded into signal logic.
"""

ROOT_CAUSE_DEV_CASES = {
    "DOTUSDT": {
        "v2_path": "frozen_1h → reclaim → rebreak_last_reclaim_level (×2) → invalidate",
        "level_type": "rebreak_last_reclaim_level",
        "level_value_role": "equals frozen entry 1h protected low",
        "why_early": (
            "v2 treats failed reclaim of the shallow entry-1h floor as terminal thesis loss. "
            "After the local rebreak the market can still form a later range / deeper support "
            "before the move that matters for inventory growth."
        ),
        "why_v2_rebreak_insufficient": (
            "Rebreak re-arms the same reclaim level immediately; it never waits for a newly "
            "confirmed post-break swing/range low."
        ),
    },
    "ATOMUSDT": {
        "v2_path": "single episode frozen_entry_protected_low_1h → no reclaim → invalidate",
        "level_type": "frozen_entry_protected_low_1h",
        "level_value_role": "entry frozen 1h PL",
        "why_early": (
            "First unreclaimed 4h close below entry-1h PL is local support loss / early warning, "
            "not necessarily the later decisive continuation break."
        ),
        "why_v2_rebreak_insufficient": "No reclaim path; invalidation is the first shallow floor loss.",
    },
    "LTCUSDT": {
        "v2_path": "single episode frozen_entry_protected_low_1h → no reclaim → invalidate",
        "level_type": "frozen_entry_protected_low_1h",
        "level_value_role": "entry frozen 1h PL",
        "why_early": (
            "Same shallow-floor pattern as ATOM: early 1h-floor loss with long remaining path "
            "to C4/C5 / explosion."
        ),
        "why_v2_rebreak_insufficient": "Terminal on first unreclaimed shallow break.",
    },
    "INJUSDT": {
        "v2_path": "single episode live protected_low_4h_close_break → no reclaim → invalidate",
        "level_type": "protected_low_4h_close_break",
        "level_value_role": "live 4h protected low near entry-era structure",
        "why_early": (
            "Relative to C5 this case is late (inv after C5). Kept as development contrast: "
            "v2 can also fire on live 4h PL without requiring post-break structure rebuild."
        ),
        "why_v2_rebreak_insufficient": "No post-break deeper-level wait; first live PL loss is terminal.",
    },
}
