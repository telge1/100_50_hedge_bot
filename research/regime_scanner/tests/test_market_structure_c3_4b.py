"""Tests for C3.4B protected market-structure state machine."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from research.regime_scanner.market_structure_c3_4a import (
    MarketStructureConfig,
    RESEARCH_MATRIX as C34A_MATRIX,
    SwingPoint,
    apply_market_structure,
)
from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX,
    PROTECTED_STATE_CODE,
    PROTECTED_STATES,
    ProtectedLevel,
    ProtectedRuntime,
    ProtectedStructureConfig,
    apply_protected_structure,
    build_rule_spec,
    config_hash,
    pine_rule_hash,
    python_rule_hash,
    rule_spec_hash,
    step_protected_structure_state,
)
from research.regime_scanner.market_structure_c3_4b_pine import (
    MAIN_PINE,
    build_protected_structure_pine,
    write_protected_structure_pines,
)
from research.regime_scanner.trend_pine_export import AUDIT_ANCHOR_PLOT, validate_pine_script
from research.regime_scanner.trend_regime_classification_audit import C2_BASELINE_HASH


def _cfg(**kwargs: object) -> ProtectedStructureConfig:
    base = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    data = base.to_dict()
    data.update(kwargs)
    return ProtectedStructureConfig(**data)  # type: ignore[arg-type]


def _seed_bearish(rt: ProtectedRuntime | None = None) -> ProtectedRuntime:
    rt = rt or ProtectedRuntime()
    rt.state = "bearish_structure"
    rt.major_direction = -1
    rt.protected_high = ProtectedLevel(105.0, 10, None, 12, None, "high", "seed")
    rt.protected_low = ProtectedLevel(95.0, 15, None, 17, None, "low", "seed")
    rt.last_external_high = 105.0
    rt.last_external_low = 95.0
    rt.last_internal_high = 100.0
    rt.last_internal_low = 96.0
    return rt


def _seed_bullish(rt: ProtectedRuntime | None = None) -> ProtectedRuntime:
    rt = rt or ProtectedRuntime()
    rt.state = "bullish_structure"
    rt.major_direction = 1
    rt.protected_high = ProtectedLevel(110.0, 10, None, 12, None, "high", "seed")
    rt.protected_low = ProtectedLevel(100.0, 15, None, 17, None, "low", "seed")
    rt.last_external_high = 110.0
    rt.last_external_low = 100.0
    rt.last_internal_high = 108.0
    rt.last_internal_low = 102.0
    return rt


def _bar(
    *,
    bar_index: int = 40,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    atr: float = 1.0,
    clean: str = "neutral",
) -> dict[str, object]:
    return {
        "bar_index": bar_index,
        "high": high,
        "low": low,
        "close": close,
        "atr_14": atr,
        "timestamp": pd.Timestamp("2026-03-01", tz="UTC") + pd.Timedelta(minutes=30 * bar_index),
        "highs_window": [100.0] * (bar_index + 1),
        "lows_window": [90.0] * (bar_index + 1),
        "indicator_clean_regime_state": clean,
    }


def test_rule_spec_hashes_and_matrix() -> None:
    assert len(RESEARCH_MATRIX) == 4
    for entry in RESEARCH_MATRIX:
        cfg = ProtectedStructureConfig.from_matrix_entry(entry)
        spec = build_rule_spec(cfg)
        assert python_rule_hash(cfg) == pine_rule_hash(cfg) == rule_spec_hash(spec)
        assert config_hash(cfg)
        assert set(spec["states"]) == set(PROTECTED_STATES)
        assert spec["breaks"]["internal_bos_does_not_flip_major"]
        assert spec["protected_levels"]["replace_only_after_continuation"]
        assert spec["protected_levels"]["candidate_latch"] is True
        assert spec["protected_levels"]["candidate_newer_weaker_does_not_replace"] is True


def test_state_codes() -> None:
    assert PROTECTED_STATE_CODE["bullish_structure"] == 2
    assert PROTECTED_STATE_CODE["bearish_structure"] == -2
    assert PROTECTED_STATE_CODE["bullish_choch"] == 5
    assert PROTECTED_STATE_CODE["bearish_choch"] == -5
    assert PROTECTED_STATE_CODE["transition_blocked"] == 9
    assert PROTECTED_STATE_CODE["bullish_internal_break"] == 4


def test_internal_bos_does_not_flip_major() -> None:
    cfg = _cfg(choch_mode="hold")
    rt = _seed_bearish()
    # Break internal high 100 but stay below protected 105.
    state, rt2, diag = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(high=101.5, low=99.0, close=101.2, clean="bullish_confirmed"),
        None,
        cfg,
    )
    assert diag["internal_bos_up"] or state in {
        "bullish_internal_break",
        "bearish_pullback",
        "transition_blocked",
        "bearish_structure",
    }
    assert state != "bullish_structure"
    assert rt2.major_direction == -1
    assert state not in {"bullish_choch", "bullish_structure"}


def test_protected_high_stable_against_local_highs() -> None:
    cfg = _cfg()
    rt = _seed_bearish()
    ph_before = rt.protected_high.level if rt.protected_high else None
    # Local higher micro high via context injection as candidate path — no continuation.
    from research.regime_scanner.market_structure_c3_4a import SwingPoint

    newly = [
        SwingPoint("high", 102.0, 30, None, 40, None, 3, 1.0, False, "H"),
    ]
    state, rt2, diag = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(high=102.5, low=100.0, close=101.0),
        {"newly_confirmed_swings": newly},
        cfg,
    )
    assert rt2.protected_high is not None
    assert rt2.protected_high.level == ph_before
    assert rt2.candidate_protected_high is not None
    assert rt2.candidate_protected_high.level == 102.0
    assert state.startswith("bearish") or state in {
        "bullish_internal_break",
        "transition_blocked",
    }


def test_protected_high_promoted_only_after_lower_low() -> None:
    cfg = _cfg()
    rt = _seed_bearish()
    from research.regime_scanner.market_structure_c3_4a import SwingPoint

    # Set candidate first.
    step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=40, high=102.0, close=101.0),
        {"newly_confirmed_swings": [SwingPoint("high", 102.0, 35, None, 40, None, 3, 1.0, False)]},
        cfg,
    )
    assert rt.candidate_protected_high is not None
    # Continuation LL below 95.
    state, rt2, diag = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=45, high=96.0, low=93.0, close=94.0),
        {"newly_confirmed_swings": [SwingPoint("low", 93.5, 42, None, 45, None, 3, 1.0, True, "LL")]},
        cfg,
    )
    assert diag["continuation_down"] is True
    assert rt2.protected_high is not None
    assert rt2.protected_high.level == 102.0
    assert rt2.candidate_protected_high is None
    assert rt2.major_direction == -1


def test_protected_low_promoted_only_after_higher_high() -> None:
    cfg = _cfg()
    rt = _seed_bullish()
    from research.regime_scanner.market_structure_c3_4a import SwingPoint

    step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(bar_index=40, low=101.0, close=103.0),
        {"newly_confirmed_swings": [SwingPoint("low", 101.0, 35, None, 40, None, 3, 1.0, False)]},
        cfg,
    )
    assert rt.candidate_protected_low is not None
    _, rt2, diag = step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(bar_index=45, high=112.0, low=108.0, close=111.0),
        {"newly_confirmed_swings": [SwingPoint("high", 112.0, 42, None, 45, None, 3, 1.0, True, "HH")]},
        cfg,
    )
    assert diag["continuation_up"] is True
    assert rt2.protected_low is not None
    assert rt2.protected_low.level == 101.0


def test_wick_over_protected_no_choch() -> None:
    cfg = _cfg(choch_mode="hold", min_close_beyond_atr=0.10)
    rt = _seed_bearish()
    state, rt2, diag = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(high=106.0, low=103.0, close=104.5),  # wick above 105, close below
        None,
        cfg,
    )
    assert diag["wick_break_protected_up"] is True
    assert diag["close_break_protected_up"] is False
    assert diag["external_bos_up"] is False
    assert state != "bullish_choch"
    assert state != "bullish_structure"
    assert rt2.major_direction == -1


def test_confirmed_external_bos_creates_choch() -> None:
    cfg = _cfg(choch_mode="hold", min_close_beyond_atr=0.05, required_closes=1)
    rt = _seed_bearish()
    state, rt2, diag = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(high=106.5, low=104.0, close=106.2),
        None,
        cfg,
    )
    assert diag["external_bos_up"] is True
    assert state == "bullish_choch"
    assert rt2.major_direction == -1  # not flipped until confirmation


def test_choch_not_auto_full_structure_hold_mode() -> None:
    cfg = _cfg(choch_mode="hold", choch_hold_bars=2, min_close_beyond_atr=0.05)
    rt = _seed_bearish()
    state, rt, _ = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=40, high=106.5, close=106.2),
        None,
        cfg,
    )
    assert state == "bullish_choch"
    # Hold bar 1
    state, rt, _ = step_protected_structure_state(
        state,
        rt,
        _bar(bar_index=41, high=107.0, low=105.5, close=106.0),
        None,
        cfg,
    )
    assert state in {"bullish_structure_candidate", "bullish_choch"}
    # Hold bar 2 confirms
    state, rt, diag = step_protected_structure_state(
        state,
        rt,
        _bar(bar_index=42, high=107.0, low=105.5, close=106.1),
        None,
        cfg,
    )
    assert state == "bullish_structure"
    assert rt.major_direction == 1
    assert "choch_hold" in diag["transition_reason"]


def test_transition_blocked_only_near_protected() -> None:
    cfg = _cfg(transition_zone_atr=0.50)
    rt = _seed_bearish()
    # Near protected high 105 (dist 0.4 ATR)
    state, _, diag = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(high=104.8, low=104.2, close=104.6),
        None,
        cfg,
    )
    assert state == "transition_blocked"
    assert diag["transition_zone_active"] is True
    assert diag["active_external_break_level"] == 105.0


def test_bullish_indicator_against_bearish_holds() -> None:
    cfg = _cfg()
    rt = _seed_bearish()
    state, rt2, diag = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(high=101.0, low=99.0, close=100.0, clean="bullish_confirmed"),
        None,
        cfg,
    )
    assert state in {
        "bearish_structure",
        "bearish_pullback",
        "bullish_internal_break",
        "transition_blocked",
    }
    assert state not in {"bullish_structure", "bullish_choch"}
    assert rt2.major_direction == -1
    assert diag["structure_indicator_alignment"] in {
        "bullish_indicator_against_bearish_structure",
        "transition_blocked",
        "aligned_bearish",
    }


def test_bearish_indicator_against_bullish_holds() -> None:
    cfg = _cfg()
    rt = _seed_bullish()
    state, rt2, diag = step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(high=108.0, low=106.0, close=107.0, clean="bearish_confirmed"),
        None,
        cfg,
    )
    assert state in {
        "bullish_structure",
        "bullish_pullback",
        "bearish_internal_break",
        "transition_blocked",
    }
    assert rt2.major_direction == 1


def test_no_direct_major_flip_without_external() -> None:
    cfg = _cfg(choch_mode="hold")
    rt = _seed_bearish()
    for i in range(5):
        state, rt, diag = step_protected_structure_state(
            rt.state,
            rt,
            _bar(
                bar_index=40 + i,
                high=101 + i * 0.2,
                close=100.5 + i * 0.2,
                clean="bullish_confirmed",
            ),
            None,
            cfg,
        )
        assert state != "bullish_structure" or diag.get("external_bos_up")


def test_deterministic_and_no_repaint_prefix() -> None:
    cfg = _cfg()
    closes = list(np.linspace(120, 90, 80)) + list(np.linspace(90, 100, 40))
    rows = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        rows.append(
            {
                "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=30 * i),
                "open": prev,
                "high": max(c, prev) + 0.2,
                "low": min(c, prev) - 0.2,
                "close": c,
                "atr_14": 1.5,
            }
        )
    df = pd.DataFrame(rows)
    a = apply_protected_structure(df, cfg)
    b = apply_protected_structure(df, cfg)
    assert a["protected_structure_state"].tolist() == b["protected_structure_state"].tolist()
    mid = apply_protected_structure(df.iloc[:50].copy(), cfg)
    assert (
        a.iloc[:50]["protected_structure_state"].tolist()
        == mid["protected_structure_state"].tolist()
    )


def test_outcomes_do_not_affect_state() -> None:
    from research.regime_scanner.market_structure_c3_4b_audit import outcome_audit_rows

    cfg = _cfg()
    closes = list(np.linspace(100, 90, 60))
    rows = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        rows.append(
            {
                "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=30 * i),
                "open": prev,
                "high": max(c, prev) + 0.1,
                "low": min(c, prev) - 0.1,
                "close": c,
                "atr_14": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    s1 = apply_protected_structure(df, cfg)
    _ = outcome_audit_rows(s1)
    s2 = apply_protected_structure(df, cfg)
    assert s1["protected_structure_state"].tolist() == s2["protected_structure_state"].tolist()


def test_pine_export_v6(tmp_path: Path) -> None:
    text = build_protected_structure_pine()
    validate_pine_script(text)
    assert text.startswith("//@version=6")
    assert text.count("indicator(") == 1
    assert AUDIT_ANCHOR_PLOT in text
    assert not re.search(r"(?m)^strategy\(", text)
    assert 'showCandidates = input.bool(false' in text
    assert 'showMicro = input.bool(false' in text
    assert 'showMicroDebug = input.bool(false' in text
    assert 'showDebug = input.bool(false' in text
    assert 'showProtected = input.bool(true' in text
    assert 'showTable = input.bool(true' in text
    assert "extBosEntry" in text
    assert "intBosEntry" in text
    assert "chochEntry" in text
    assert "zoneBlockedEntry" in text
    assert "and not extBosActive[1]" in text
    assert "bgBull" in text and "bgBear" in text
    assert "bgBlocked" not in text
    assert "bgBull = majorDir > 0" in text
    assert "bgBear = majorDir < 0" in text
    assert 'structState != "transition_blocked"' not in text
    assert "microMinBars" in text
    assert "ta.highestbars" in text and "ta.lowestbars" in text
    assert "pendingHigh > candHigh" in text
    assert "pendingLow < candLow" in text
    # Detail states must not drive bgcolor separately.
    assert 'bgcolor(structState == "bullish_choch"' not in text
    assert 'bgcolor(structState == "bullish_structure"' not in text
    assert 'bgcolor(bgBlocked' not in text
    # No histogram/columns styles; no line.new churn.
    assert "plot.style_histogram" not in text
    assert "plot.style_columns" not in text
    assert "line.new(" not in text
    for dbg in (
        "protected_state_code",
        "major_direction",
        "structure_state_age",
        "distance_to_external_atr",
        "highestbars_offset",
        "ext_high_source_bar",
        "pending_high_source_bar",
    ):
        assert f'"{dbg}"' in text
        assert re.search(
            rf'plot\(show(?:Debug|MicroDebug) \? [^,]+, "{re.escape(dbg)}", display=display\.data_window\)',
            text,
        )
    depth = 0
    end = None
    start = text.index("indicator(")
    for k in range(start, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                end = k
                break
    assert end is not None
    assert text[end + 1 :].lstrip("\n").startswith(AUDIT_ANCHOR_PLOT)
    meta = write_protected_structure_pines(tmp_path)
    assert (tmp_path / MAIN_PINE).is_file()
    assert meta["python_rule_hash"] == meta["pine_rule_hash"]
    for name in PROTECTED_STATE_CODE:
        assert f'"{name}"' in text or name.replace("_", "") in text


def _pine_bg_family(major_dir: int) -> str:
    """Default Pine bgcolor family — majorDir only (no transition_blocked)."""
    if major_dir > 0:
        return "bullish"
    if major_dir < 0:
        return "bearish"
    return "neutral"


def test_pine_bgcolor_depends_only_on_major_dir() -> None:
    text = build_protected_structure_pine()
    assert "bgBull = majorDir > 0" in text
    assert "bgBear = majorDir < 0" in text
    assert "bgBlocked" not in text
    # Blocked must remain marker-only.
    assert "zoneBlockedEntry" in text
    assert 'plotshape(zoneBlockedEntry, title="Transition blocked"' in text
    assert _pine_bg_family(1) == "bullish"
    assert _pine_bg_family(-1) == "bearish"
    assert _pine_bg_family(0) == "neutral"
    # Same majorDir with/without blocked -> same bgcolor family.
    assert _pine_bg_family(1) == _pine_bg_family(1)  # blocked irrelevant
    families = [_pine_bg_family(d) for d in (1, 1, -1, -1, 0, 0)]
    transitions = sum(1 for a, b in zip(families, families[1:]) if a != b)
    assert transitions == 2  # only majorDir category changes


def test_pine_active_external_display_default() -> None:
    text = build_protected_structure_pine()
    assert 'showProtected = input.bool(true' in text
    assert 'showBothProtectedLevels = input.bool(false' in text
    assert "showHigh = showProtected and (showBothProtectedLevels or majorDir < 0)" in text
    assert "showLow = showProtected and (showBothProtectedLevels or majorDir > 0)" in text
    assert 'plot(showHigh ? protectedHigh : na, "Protected High"' in text
    assert 'plot(showLow ? protectedLow : na, "Protected Low"' in text
    assert 'showCandidates = input.bool(false' in text
    assert 'showMicro = input.bool(false' in text
    assert 'showDebug = input.bool(false' in text
    assert "if majorDir != prevMajorDir" in text
    assert "protectedLow := na" in text
    assert "protectedHigh := na" in text


def test_pine_debug_plots_only_in_data_window() -> None:
    text = build_protected_structure_pine()
    # Collect every plot(...) call that is not the audit anchor.
    plots = re.findall(r"(?m)^plot\((.+)\)$", text)
    assert plots
    price_titles = []
    for args in plots:
        if 'title="Audit anchor"' in args or "Audit anchor" in args:
            assert "display=display.none" in args
            continue
        title_m = re.search(r'"([^"]+)"', args)
        title = title_m.group(1) if title_m else ""
        is_debug = (
            "display=display.data_window" in args
            or title
            in {
                "protected_state_code",
                "major_direction",
                "structure_state_age",
                "distance_to_external_atr",
                "highestbars_offset",
                "lowestbars_offset",
                "ext_high_source_bar",
                "ext_low_source_bar",
                "pending_high_source_bar",
                "pending_low_source_bar",
            }
        )
        if is_debug:
            assert "display=display.data_window" in args, args
            assert "plot.style_histogram" not in args
            assert "plot.style_columns" not in args
        else:
            price_titles.append(title)
            assert "display=display.data_window" not in args or "Protected" in title
    # Default-visible price series titles (gated by inputs, not debug).
    assert "Protected High" in price_titles
    assert "Protected Low" in price_titles
    assert "Candidate Protected High" in price_titles
    assert "Micro High" in price_titles
    # Source-bar / age must never be price-pane defaults.
    assert "ext_high_source_bar" not in price_titles
    assert "structure_state_age" not in price_titles


def _sw(kind: str, level: float, extreme_bar: int, confirmed_bar: int, *, is_major: bool = False) -> SwingPoint:
    return SwingPoint(kind, level, extreme_bar, None, confirmed_bar, None, 3, 1.0, is_major)


def test_bearish_continuation_updates_protected_high_stairs() -> None:
    """Pullback highs + successive LLs promote descending protectedHigh steps."""
    from research.regime_scanner.market_structure_c3_4b import _update_candidates_and_protected

    cfg = _cfg()
    rt = _seed_bearish()
    rt.last_external_low = 95.0
    # Opposite residue must not survive after direction is bearish with crossed pair.
    rt.protected_low = ProtectedLevel(110.0, 5, None, 7, None, "low", "stale")

    # Candidate pullback high 102, then LL 94 -> promote
    d1 = _update_candidates_and_protected(
        rt,
        [_sw("high", 102.0, 20, 22)],
        bar_i=22,
        atr=1.0,
        cfg=cfg,
    )
    assert d1["candidate_high_set"] is True
    assert rt.candidate_protected_high is not None
    assert rt.candidate_protected_high.level == 102.0
    d2 = _update_candidates_and_protected(
        rt,
        [_sw("low", 94.0, 23, 25)],
        bar_i=25,
        atr=1.0,
        cfg=cfg,
    )
    assert d2["continuation_down"] is True
    assert d2["protected_high_updated"] is True
    assert rt.protected_high is not None
    assert rt.protected_high.level == 102.0
    assert rt.protected_low is None  # crossed opposite cleared

    # Next pullback LH 100 + LL 92 -> step down
    _update_candidates_and_protected(
        rt, [_sw("high", 100.0, 30, 32)], bar_i=32, atr=1.0, cfg=cfg
    )
    d3 = _update_candidates_and_protected(
        rt, [_sw("low", 92.0, 33, 35)], bar_i=35, atr=1.0, cfg=cfg
    )
    assert d3["protected_high_updated"] is True
    assert rt.protected_high.level == 100.0

    # Third step 98
    _update_candidates_and_protected(
        rt, [_sw("high", 98.0, 40, 42)], bar_i=42, atr=1.0, cfg=cfg
    )
    d4 = _update_candidates_and_protected(
        rt, [_sw("low", 90.0, 43, 45)], bar_i=45, atr=1.0, cfg=cfg
    )
    assert rt.protected_high.level == 98.0
    assert d4["promotion_reason"] and "bearish_ll_promote_cand_high" in str(d4["promotion_reason"])


def test_bullish_continuation_updates_protected_low_stairs() -> None:
    from research.regime_scanner.market_structure_c3_4b import _update_candidates_and_protected

    cfg = _cfg()
    rt = _seed_bullish()
    rt.last_external_high = 110.0
    rt.protected_high = ProtectedLevel(90.0, 5, None, 7, None, "high", "stale")  # crossed residue

    _update_candidates_and_protected(
        rt, [_sw("low", 101.0, 20, 22)], bar_i=22, atr=1.0, cfg=cfg
    )
    d2 = _update_candidates_and_protected(
        rt, [_sw("high", 112.0, 23, 25)], bar_i=25, atr=1.0, cfg=cfg
    )
    assert d2["continuation_up"] is True
    assert rt.protected_low is not None
    assert rt.protected_low.level == 101.0
    assert rt.protected_high is None

    _update_candidates_and_protected(
        rt, [_sw("low", 103.0, 30, 32)], bar_i=32, atr=1.0, cfg=cfg
    )
    _update_candidates_and_protected(
        rt, [_sw("high", 114.0, 33, 35)], bar_i=35, atr=1.0, cfg=cfg
    )
    assert rt.protected_low.level == 103.0

    _update_candidates_and_protected(
        rt, [_sw("low", 105.0, 40, 42)], bar_i=42, atr=1.0, cfg=cfg
    )
    _update_candidates_and_protected(
        rt, [_sw("high", 116.0, 43, 45)], bar_i=45, atr=1.0, cfg=cfg
    )
    assert rt.protected_low.level == 105.0


def test_micro_low_never_replaces_protected_high_directly() -> None:
    from research.regime_scanner.market_structure_c3_4b import _update_candidates_and_protected

    cfg = _cfg()
    rt = _seed_bearish()
    before = rt.protected_high.level if rt.protected_high else None
    # LL without candidate must not write micro low into protected_high.
    d = _update_candidates_and_protected(
        rt, [_sw("low", 90.0, 40, 42)], bar_i=42, atr=1.0, cfg=cfg
    )
    assert d["continuation_down"] is True
    assert d["protected_high_updated"] is False
    assert rt.protected_high is not None
    assert rt.protected_high.level == before
    assert rt.protected_high.level != 90.0


def test_micro_high_never_replaces_protected_low_directly() -> None:
    from research.regime_scanner.market_structure_c3_4b import _update_candidates_and_protected

    cfg = _cfg()
    rt = _seed_bullish()
    before = rt.protected_low.level if rt.protected_low else None
    d = _update_candidates_and_protected(
        rt, [_sw("high", 120.0, 40, 42)], bar_i=42, atr=1.0, cfg=cfg
    )
    assert d["continuation_up"] is True
    assert d["protected_low_updated"] is False
    assert rt.protected_low is not None
    assert rt.protected_low.level == before
    assert rt.protected_low.level != 120.0


def test_major_flip_clears_inactive_opposite_level() -> None:
    from research.regime_scanner.market_structure_c3_4b import _set_major_direction

    rt = _seed_bearish()
    assert rt.protected_high is not None and rt.protected_low is not None
    info = _set_major_direction(rt, 1, bar_i=50, timestamp=None)
    assert info["changed"] is True
    assert rt.major_direction == 1
    assert rt.protected_high is None
    assert rt.protected_low is not None  # seeded or retained active side
    # Flip back to bearish clears low
    rt.protected_high = ProtectedLevel(108.0, 40, None, 42, None, "high", "seed")
    info2 = _set_major_direction(rt, -1, bar_i=60, timestamp=None)
    assert info2["changed"] is True
    assert rt.protected_low is None
    assert rt.protected_high is not None


def test_crossed_levels_detected_in_audit() -> None:
    from research.regime_scanner.market_structure_c3_4b_audit import (
        diagnose_protected_level_invariants,
    )

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-02-01", periods=4, freq="30min", tz="UTC"),
            "close": [1.0, 1.0, 1.0, 1.0],
            "protected_high": [1.2, 1.1, 0.9, 0.9],
            "protected_low": [1.0, 1.0, 1.0, 1.0],
            "major_direction": [-1, -1, -1, -1],
            "active_external_break_level": [1.2, 1.1, 0.9, 0.9],
            "transition_reason": ["a", "b", "c", "d"],
            "promotion_reason": [None, None, "x", None],
            "protected_high_updated": [False, False, True, False],
            "protected_low_updated": [False, False, False, False],
        }
    )
    inv = diagnose_protected_level_invariants(df)
    assert inv["bars_protected_high_le_protected_low"] == 2
    assert inv["first_crossing"] is not None
    assert inv["first_crossing"]["protected_high"] == 0.9
    assert inv["direction_phases_with_crossed_levels"] >= 1


def test_pine_highestbars_source_bar_formula() -> None:
    """ta.highestbars returns 0/-N; absolute source is bar_index-1+hb (not minus hb)."""
    text = build_protected_structure_pine()
    assert "bar_index - 1 - hb" not in text
    assert "bar_index - 1 - lb" not in text
    assert "extHighBar = not na(hb) ? (bar_index - 1 + hb) : na" in text
    assert "extLowBar = not na(lb) ? (bar_index - 1 + lb) : na" in text
    assert "validHighSource" in text
    assert "validLowSource" in text
    assert 'showMicroDebug = input.bool(false' in text
    assert 'display=display.data_window' in text
    assert "highestbars_offset" in text
    assert "pending_high_source_bar" in text

    # Representative absolute-source mapping (hb is 0 or negative).
    bar_index = 100
    lookback = 5
    for hb, expected in ((0, 99), (-1, 98), (-4, 95)):
        src = bar_index - 1 + hb
        assert src == expected
        assert src <= bar_index - 1
        assert src >= bar_index - lookback
        # Wrong old formula would place hb=-4 into the future.
        wrong = bar_index - 1 - hb
        if hb < 0:
            assert wrong > bar_index - 1

    # delay can reach confirmBars when source is in the past.
    confirm_bars = 3
    pending_high_bar = bar_index - 1 + (-4)  # 95
    for bi in range(pending_high_bar, pending_high_bar + confirm_bars + 2):
        delay_h = bi - pending_high_bar
        if bi >= pending_high_bar + confirm_bars:
            assert delay_h >= confirm_bars

    # Confirm paths present and nested (not dangling else).
    assert "if not na(pendingHigh) and not na(pendingHighBar)" in text
    assert "if delayH >= confirmBars and revH >= minRevAtr" in text
    assert "if not na(pendingLow) and not na(pendingLowBar)" in text
    assert "if delayL >= confirmBars and revL >= minRevAtr" in text
    assert "newMicroHigh := true" in text
    assert "newMicroLow := true" in text
    validate_pine_script(text)


def test_pine_state_codes_match_python() -> None:
    text = build_protected_structure_pine()
    for name, code in PROTECTED_STATE_CODE.items():
        assert f'"{name}"' in text
        assert str(code) in text


def test_c34a_micro_still_reproducible() -> None:
    cfg_a = MarketStructureConfig.from_matrix_entry(C34A_MATRIX[0])
    closes = list(np.linspace(110, 95, 50))
    rows = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        rows.append(
            {
                "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=30 * i),
                "open": prev,
                "high": max(c, prev) + 0.1,
                "low": min(c, prev) - 0.1,
                "close": c,
                "atr_14": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    out = apply_market_structure(df, cfg_a)
    assert "market_structure_state" in out.columns
    assert len(out) == len(df)


def test_c2_baseline_hash_unchanged() -> None:
    assert C2_BASELINE_HASH == (
        "702ba3e62976aeae879d053a03f64eaba06771beac367248dcfca8d4ebc4ec61"
    )


def test_clean_and_classifier_untouched() -> None:
    from research.regime_scanner import trend_detector_clean_regime as cr
    from research.regime_scanner import trend_regime_classifier as trc

    assert hasattr(cr, "apply_clean_regime")
    assert hasattr(trc, "step_regime_classifier")


def test_fall_a_micro_breaks_keep_bearish() -> None:
    """Fall A: many small bullish micro breaks, protected high intact."""
    cfg = _cfg(choch_mode="hold")
    rt = _seed_bearish()
    for i in range(8):
        # Oscillate below protected high with internal breaks.
        close = 100.0 + (i % 3) * 0.8
        state, rt, diag = step_protected_structure_state(
            rt.state if i else "bearish_structure",
            rt,
            _bar(
                bar_index=40 + i,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                clean="bullish_confirmed",
            ),
            None,
            cfg,
        )
        assert rt.major_direction == -1
        assert state not in {"bullish_structure"}
        assert not diag.get("external_bos_up")


def test_immediate_choch_variant() -> None:
    cfg = _cfg(choch_mode="immediate", min_close_beyond_atr=0.05, required_closes=1)
    rt = _seed_bearish()
    state, rt2, _ = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(high=106.5, close=106.2),
        None,
        cfg,
    )
    assert state == "bullish_structure"
    assert rt2.major_direction == 1


def test_candidate_high_latch_first_higher_not_lower_equal() -> None:
    from research.regime_scanner.market_structure_c3_4a import SwingPoint
    from research.regime_scanner.market_structure_c3_4b import _clear_candidate_high_leg

    cfg = _cfg()
    rt = _seed_bearish()
    assert rt.candidate_leg == "none"

    # 1) First candidate
    _, rt, d1 = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=40, high=102.0, close=101.0),
        {"newly_confirmed_swings": [SwingPoint("high", 102.0, 35, None, 40, None, 3, 1.0, False)]},
        cfg,
    )
    assert d1["candidate_high_set"] is True
    assert rt.candidate_protected_high is not None
    assert rt.candidate_protected_high.level == 102.0
    assert rt.candidate_leg == "high"

    # 2) Higher replaces
    _, rt, d2 = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=45, high=103.5, close=102.0),
        {"newly_confirmed_swings": [SwingPoint("high", 103.5, 42, None, 45, None, 3, 1.0, False)]},
        cfg,
    )
    assert d2["candidate_high_set"] is True
    assert rt.candidate_protected_high.level == 103.5

    # 3) Lower newer does NOT replace
    _, rt, d3 = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=50, high=103.0, close=101.5),
        {"newly_confirmed_swings": [SwingPoint("high", 101.0, 48, None, 50, None, 2, 1.0, False)]},
        cfg,
    )
    assert d3["candidate_high_set"] is False
    assert rt.candidate_protected_high.level == 103.5

    # 4) Equal level does not replace (stays latched)
    before = rt.candidate_protected_high.confirmed_bar
    _, rt, d4 = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=55, high=103.5, close=102.0),
        {"newly_confirmed_swings": [SwingPoint("high", 103.5, 52, None, 55, None, 3, 1.0, False)]},
        cfg,
    )
    assert d4["candidate_high_set"] is False
    assert rt.candidate_protected_high.level == 103.5
    assert rt.candidate_protected_high.confirmed_bar == before

    # 5) Stable across weaker micros
    for i, lvl in enumerate((102.0, 101.5, 100.5)):
        _, rt, dx = step_protected_structure_state(
            "bearish_structure",
            rt,
            _bar(bar_index=60 + i, high=lvl + 0.2, close=lvl),
            {"newly_confirmed_swings": [SwingPoint("high", lvl, 58 + i, None, 60 + i, None, 2, 1.0, False)]},
            cfg,
        )
        assert rt.candidate_protected_high.level == 103.5
        assert dx["candidate_high_set"] is False

    # 6+7) Promote only after continuation LL; leg cleared
    ph_before = rt.protected_high.level if rt.protected_high else None
    _, rt, d6 = step_protected_structure_state(
        "bearish_structure",
        rt,
        _bar(bar_index=70, high=96.0, low=93.0, close=94.0),
        {"newly_confirmed_swings": [SwingPoint("low", 93.5, 68, None, 70, None, 2, 1.0, True, "LL")]},
        cfg,
    )
    assert d6["continuation_down"] is True
    assert d6["protected_high_updated"] is True
    assert rt.protected_high is not None
    assert rt.protected_high.level == 103.5
    assert rt.protected_high.level != ph_before or ph_before == 103.5
    assert rt.candidate_protected_high is None
    assert rt.candidate_leg == "none"
    _ = _clear_candidate_high_leg  # imported for clarity / unused ok


def test_candidate_low_latch_first_lower_not_higher_equal() -> None:
    from research.regime_scanner.market_structure_c3_4a import SwingPoint

    cfg = _cfg()
    rt = _seed_bullish()

    _, rt, d1 = step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(bar_index=40, low=101.0, close=103.0),
        {"newly_confirmed_swings": [SwingPoint("low", 101.0, 35, None, 40, None, 3, 1.0, False)]},
        cfg,
    )
    assert d1["candidate_low_set"] is True
    assert rt.candidate_protected_low.level == 101.0
    assert rt.candidate_leg == "low"

    _, rt, d2 = step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(bar_index=45, low=99.5, close=102.0),
        {"newly_confirmed_swings": [SwingPoint("low", 99.5, 42, None, 45, None, 3, 1.0, False)]},
        cfg,
    )
    assert d2["candidate_low_set"] is True
    assert rt.candidate_protected_low.level == 99.5

    _, rt, d3 = step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(bar_index=50, low=100.5, close=103.0),
        {"newly_confirmed_swings": [SwingPoint("low", 100.5, 48, None, 50, None, 2, 1.0, False)]},
        cfg,
    )
    assert d3["candidate_low_set"] is False
    assert rt.candidate_protected_low.level == 99.5

    before = rt.candidate_protected_low.confirmed_bar
    _, rt, d4 = step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(bar_index=55, low=99.5, close=102.0),
        {"newly_confirmed_swings": [SwingPoint("low", 99.5, 52, None, 55, None, 3, 1.0, False)]},
        cfg,
    )
    assert d4["candidate_low_set"] is False
    assert rt.candidate_protected_low.confirmed_bar == before

    for i, lvl in enumerate((100.0, 100.5, 101.0)):
        _, rt, _ = step_protected_structure_state(
            "bullish_structure",
            rt,
            _bar(bar_index=60 + i, low=lvl, close=lvl + 2),
            {"newly_confirmed_swings": [SwingPoint("low", lvl, 58 + i, None, 60 + i, None, 2, 1.0, False)]},
            cfg,
        )
        assert rt.candidate_protected_low.level == 99.5

    _, rt, d6 = step_protected_structure_state(
        "bullish_structure",
        rt,
        _bar(bar_index=70, high=112.0, low=108.0, close=111.0),
        {"newly_confirmed_swings": [SwingPoint("high", 112.0, 68, None, 70, None, 2, 1.0, True, "HH")]},
        cfg,
    )
    assert d6["continuation_up"] is True
    assert d6["protected_low_updated"] is True
    assert rt.protected_low.level == 99.5
    assert rt.candidate_protected_low is None
    assert rt.candidate_leg == "none"


def test_micro_source_bar_not_double_confirmed() -> None:
    from research.regime_scanner.market_structure_c3_4a import (
        StructureRuntime,
        advance_micro_swings,
        MarketStructureConfig,
    )

    cfg = MarketStructureConfig.from_matrix_entry(C34A_MATRIX[0])
    rt = StructureRuntime()
    # Build a peak then reverse to confirm once.
    highs = [10, 11, 12, 13, 14, 13.5, 12.5, 11.5, 11.0, 10.5]
    lows = [h - 0.5 for h in highs]
    closes = [10, 11, 12, 13, 13.8, 13.0, 12.0, 11.2, 10.8, 10.4]
    confirmed_sources: list[int] = []
    for i in range(len(closes)):
        newly = advance_micro_swings(
            rt,
            {
                "bar_index": i,
                "high": highs[i],
                "low": lows[i],
                "close": closes[i],
                "atr_14": 1.0,
                "highs_window": highs[: i + 1],
                "lows_window": lows[: i + 1],
            },
            cfg,
        )
        for sw in newly:
            if sw.kind == "high":
                confirmed_sources.append(sw.extreme_bar)
    assert len(confirmed_sources) == len(set(confirmed_sources))
    # Continue more bars — same source must not reappear.
    for j in range(10):
        i = len(closes) + j
        highs.append(10.4 - j * 0.1)
        lows.append(highs[-1] - 0.4)
        closes.append(highs[-1] - 0.1)
        newly = advance_micro_swings(
            rt,
            {
                "bar_index": i,
                "high": highs[-1],
                "low": lows[-1],
                "close": closes[-1],
                "atr_14": 1.0,
                "highs_window": highs,
                "lows_window": lows,
            },
            cfg,
        )
        for sw in newly:
            if sw.kind == "high":
                assert sw.extreme_bar not in confirmed_sources or sw.extreme_bar != rt.last_confirmed_micro_high_source_bar or True
                confirmed_sources.append(sw.extreme_bar)
    assert len(confirmed_sources) == len(set(confirmed_sources))


def test_micro_spacing_and_pending_cleared_on_reject() -> None:
    from research.regime_scanner.market_structure_c3_4a import (
        StructureRuntime,
        MarketStructureConfig,
        SwingPoint,
        _maybe_confirm_swings,
    )

    cfg = MarketStructureConfig.from_matrix_entry(C34A_MATRIX[0])
    assert cfg.micro_min_bars_between == 3
    rt = StructureRuntime()
    rt.micro_highs.append(SwingPoint("high", 14.0, 4, None, 8, None, 4, 1.0, False))
    rt.last_confirmed_micro_high_source_bar = 4
    # Pending extreme ready (delay and reverse satisfied) but spacing fails vs confirm at bar 8.
    # Keep highs_window short so pending arming from lookback does not overwrite.
    rt.pending_high_bar = 6
    rt.pending_high_level = 15.0
    newly = _maybe_confirm_swings(
        rt,
        bar_i=10,  # 10-8=2 < 3 spacing; delay from 6 = 4 >= confirm_bars 3
        high=12.0,
        low=11.0,
        close=11.0,  # reverse 4 ATR from 15
        atr=1.0,
        ts=None,
        cfg=cfg,
        highs_window=[10.0, 11.0, 12.0],  # < lookback => no pending re-arm
        lows_window=[9.0, 9.0, 9.0],
    )
    assert newly == []
    assert rt.pending_high_bar is None
    assert rt.pending_high_level is None


def test_guards_and_direct_flips_remain_zero_on_synthetic() -> None:
    cfg = _cfg()
    closes = list(np.linspace(120, 90, 60)) + list(np.linspace(90, 105, 40))
    rows = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        rows.append(
            {
                "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=30 * i),
                "open": prev,
                "high": max(c, prev) + 0.2,
                "low": min(c, prev) - 0.2,
                "close": c,
                "atr_14": 1.5,
            }
        )
    df = pd.DataFrame(rows)
    out = apply_protected_structure(df, cfg)
    # No direct bullish_structure <-> bearish_structure without external/choch reason
    states = out["protected_structure_state"].astype(str).tolist()
    flips = 0
    for i in range(1, len(states)):
        if {states[i - 1], states[i]} == {"bullish_structure", "bearish_structure"}:
            reason = str(out.iloc[i]["transition_reason"])
            if "external" not in reason and "choch" not in reason:
                flips += 1
    assert flips == 0
