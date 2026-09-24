"""Reversible Stoch dashboard signal source switch (display-only).

Does NOT mutate production GLOBAL_FROZEN_TIER_A / signal_generator.signals.
"""

from __future__ import annotations

import os

SOURCE_FROZEN_BASELINE = "FROZEN_BASELINE"
SOURCE_RESEARCH_1M_TIMING = "RESEARCH_1M_TIMING"
VALID_SOURCES = frozenset({SOURCE_FROZEN_BASELINE, SOURCE_RESEARCH_1M_TIMING})

VARIANT_WAIT_1M_EXTREME = "WAIT_1M_EXTREME"
VARIANT_WAIT_1M_EXTREME_TURN_SLOPE = "WAIT_1M_EXTREME_TURN_SLOPE"
VARIANT_WAIT_1M_EXTREME_TURN_CROSS = "WAIT_1M_EXTREME_TURN_CROSS"

# Active research display variants (Baseline is never the live dashboard source).
RESEARCH_DISPLAY_VARIANTS = (
    VARIANT_WAIT_1M_EXTREME,
    VARIANT_WAIT_1M_EXTREME_TURN_SLOPE,
    VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
)

DEFAULT_RESEARCH_DISPLAY_VARIANT = VARIANT_WAIT_1M_EXTREME_TURN_CROSS

VARIANT_LABELS = {
    VARIANT_WAIT_1M_EXTREME: "Extreme",
    VARIANT_WAIT_1M_EXTREME_TURN_SLOPE: "Turn Slope",
    VARIANT_WAIT_1M_EXTREME_TURN_CROSS: "Turn Cross",
}


def normalize_dashboard_signal_source(raw: str | None) -> str:
    v = str(raw or SOURCE_FROZEN_BASELINE).strip().upper()
    if v in ("FROZEN", "BASELINE", "PRODUCTION", "TIER_A"):
        return SOURCE_FROZEN_BASELINE
    if v in ("RESEARCH", "RESEARCH_1M", "1M_TIMING", "TIMING"):
        return SOURCE_RESEARCH_1M_TIMING
    if v not in VALID_SOURCES:
        raise ValueError(f"invalid DASHBOARD_SIGNAL_SOURCE: {raw!r}")
    return v


def get_dashboard_signal_source(environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    # Production display default: validated frozen baseline (NO_BE50).
    # RESEARCH_1M_TIMING remains available via explicit env override.
    return normalize_dashboard_signal_source(
        env.get("DASHBOARD_SIGNAL_SOURCE", SOURCE_FROZEN_BASELINE)
    )


def normalize_research_display_variant(raw: str | None) -> str:
    v = str(raw or DEFAULT_RESEARCH_DISPLAY_VARIANT).strip()
    aliases = {
        "EXTREME": VARIANT_WAIT_1M_EXTREME,
        "SLOPE": VARIANT_WAIT_1M_EXTREME_TURN_SLOPE,
        "TURN_SLOPE": VARIANT_WAIT_1M_EXTREME_TURN_SLOPE,
        "CROSS": VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
        "TURN_CROSS": VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
    }
    if v.upper() in aliases:
        return aliases[v.upper()]
    if v not in RESEARCH_DISPLAY_VARIANTS:
        raise ValueError(f"invalid research display variant: {raw!r}")
    return v


def get_default_research_display_variant(environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    return normalize_research_display_variant(
        env.get("DEFAULT_RESEARCH_DISPLAY_VARIANT", DEFAULT_RESEARCH_DISPLAY_VARIANT)
    )


def research_upstream_path() -> str:
    return "/api/research/1m_timing_signals"


def frozen_upstream_path() -> str:
    return "/api/signals"


def assert_sources_do_not_mix(source: str, rows: list[dict]) -> None:
    """Hard guard: frozen and research feeds must not mix rows."""
    src = normalize_dashboard_signal_source(source)
    if src == SOURCE_RESEARCH_1M_TIMING:
        for r in rows:
            feed = str(r.get("feed_source") or r.get("strategy_version") or "")
            if feed == "signal_generator.signals":
                raise AssertionError("RESEARCH_1M_TIMING must not mix production signal rows")
            # Production signal_ids are UUIDs; research ids are prefixed.
            sid = str(r.get("signal_id") or "")
            if sid and not sid.startswith("research1m:") and r.get("research_mode") is not True:
                # Allow empty list; otherwise require research markers
                if r.get("timing_variant") is None and r.get("1m_trigger_state") is None:
                    raise AssertionError(
                        f"non-research row leaked into RESEARCH_1M_TIMING feed: {sid}"
                    )
        return

    # FROZEN_BASELINE: reject research timing rows / pending 1m states.
    for r in rows:
        sid = str(r.get("signal_id") or "")
        feed = str(r.get("feed_source") or "")
        if sid.startswith("research1m:") or feed == SOURCE_RESEARCH_1M_TIMING:
            raise AssertionError(
                f"research row leaked into FROZEN_BASELINE feed: {sid or feed}"
            )
        if r.get("research_mode") is True:
            raise AssertionError("research_mode row leaked into FROZEN_BASELINE feed")
        state = str(r.get("1m_trigger_state") or r.get("one_m_trigger_state") or "")
        if state in (
            "WAITING_FOR_1M_EXTREME",
            "WAITING_FOR_1M_TURN",
            "NO_ENTRY_TIMEOUT",
        ):
            raise AssertionError(
                f"research 1m state leaked into FROZEN_BASELINE feed: {state}"
            )
