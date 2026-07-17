"""Pine v6 diagnostic export for C3.5 pullback-entry state machine (research-only).

Mirrors Python C3.5 gates for variants A0 / A1 / A6 as closely as possible.
Structure arming edges reuse the C3.4B medium protected-structure Pine rules
(black-box SoT for arming); C3.4B Python is not modified.

A9 MTF uses closed HTF bars via request.security(..., lookahead=barmerge.lookahead_off).
HTF gate is an EMA-bias proxy of Python's C3.4B HTF major_direction — documented in
pine_parity_summary.json.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX as C34B_MATRIX,
    ProtectedStructureConfig,
    config_hash as c34b_config_hash,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    RESEARCH_VARIANTS,
    PullbackEntryConfig,
    apply_pullback_entry,
    config_hash as c35_config_hash,
)
from research.regime_scanner.trend_pine_export import (
    build_pine_header,
    validate_pine_script,
)

MAIN_PINE = "indicator_pullback_entry_c3_5.pine"
DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine")
_C34B = ProtectedStructureConfig.from_matrix_entry(C34B_MATRIX[0])


def _variant_by_name(name: str) -> PullbackEntryConfig:
    for cfg in RESEARCH_VARIANTS:
        if cfg.name == name:
            return cfg
    raise KeyError(name)


def _pine_body_path() -> Path:
    return Path(__file__).resolve().parent / "_pine_c35_body.pinefrag"


def build_pullback_entry_pine(*, title: str | None = None) -> str:
    a6 = _variant_by_name("A6")
    cfg_h = c35_config_hash(a6)
    c34b_h = c34b_config_hash(_C34B)
    lines = [
        *build_pine_header(title or "C3.5 Pullback Entry Diagnose"),
        "// RESEARCH ONLY — C3.5 pullback entry state machine visualization.",
        "// SoT: research/regime_scanner/pullback_entry_c3_5.py",
        "// Structure arming edges mirror C3.4B medium Pine rules (not a free rewrite).",
        f"// c35_primary_config_hash={cfg_h}",
        f"// c34b_arming_config_hash={c34b_h}",
        "// Labels: rising-edge state transitions only. Trigger bar != fill bar (next open).",
        "// A9 HTF: closed bars via request.security(..., lookahead=barmerge.lookahead_off);",
        "// A9 HTF gate is EMA-bias proxy (see pine_parity_summary.json).",
        "",
        f"lookback = {_C34B.lookback}",
        f"confirmBars = {_C34B.confirm_bars}",
        f"minRevAtr = {_C34B.min_reversal_atr}",
        f"microMinBars = {_C34B.micro_min_bars_between}",
        f"zoneAtr = {_C34B.transition_zone_atr}",
        f"minBeyond = {_C34B.min_close_beyond_atr}",
        f"reqCloses = {_C34B.required_closes}",
        f'chochMode = "{_C34B.choch_mode}"',
        f"chochHoldBars = {_C34B.choch_hold_bars}",
        f"retestTol = {_C34B.retest_tolerance_atr}",
        "",
        f"maxAgeDefault = {a6.max_age_bars}",
        f"adxMinDefault = {a6.adx_min}",
        f"maxEntryDistEmaAtr = {a6.max_entry_dist_ema_atr}",
        f"maxMoveSinceArmAtr = {a6.max_move_since_arm_atr}",
        f"maxBreakoutCandleAtr = {a6.max_breakout_candle_atr}",
        "",
        'variant = input.string("A6", "Variant", options=["A0", "A1", "A6", "A9"])',
        'armingMode = input.string("external_bos", "Arming type", options=["external_bos", "internal_bos", "choch", "major_dir_change", "structure_plus_protected"])',
        'showArmingLabels = input.bool(true, "Show arming labels")',
        'showPullbackLabels = input.bool(true, "Show pullback labels")',
        'showReadyLabels = input.bool(true, "Show ready labels")',
        'showEntryLabels = input.bool(true, "Show trigger/entry labels")',
        'showInvalidationLabels = input.bool(false, "Show invalidation labels")',
        'showBreakoutLevel = input.bool(true, "Show frozen breakout level")',
        'showEmaZone = input.bool(true, "Show EMA 9/20 zone")',
        'showEma50 = input.bool(false, "Show EMA 50")',
        'showTable = input.bool(true, "Show state table")',
        'showDebug = input.bool(false, "Show debug (data window)")',
        'useFocusWindow = input.bool(false, "Limit labels to focus window")',
        'focusStart = input.time(timestamp("2026-02-01 00:00 +0000"), "Focus start (UTC)")',
        'focusEnd = input.time(timestamp("2026-04-30 00:00 +0000"), "Focus end (UTC)")',
        "",
        'isA0 = variant == "A0"',
        'isA6 = variant == "A6"',
        'isA9 = variant == "A9"',
        "usePullbackPath = not isA0",
        "requireLH = isA6 or isA9",
        "requireEmaDir = isA6 or isA9",
        "requireEmaSlope = isA6 or isA9",
        "requireAdxDi = isA6 or isA9",
        "requireAtrAntiChase = isA6 or isA9",
        "useMtfGates = isA9",
        "maxAgeBars = maxAgeDefault",
        "inFocus = not useFocusWindow or (time >= focusStart and time <= focusEnd)",
        "",
    ]
    body = _pine_body_path().read_text(encoding="utf-8")
    text = "\n".join(lines) + "\n" + body
    if not text.endswith("\n"):
        text += "\n"
    validate_pine_script(text)
    if re.search(r"(?m)^strategy\(", text):
        raise ValueError("must not contain strategy(")
    if "line.new(" in text:
        raise ValueError("must not spam line.new")
    if "lookahead_on" in text:
        raise ValueError("lookahead_on forbidden")
    if "request.security(" in text and "lookahead=barmerge.lookahead_off" not in text:
        raise ValueError("MTF request.security must use lookahead_off")
    return text


def write_pullback_entry_pine(output_dir: Path | None = None) -> dict[str, Any]:
    output_dir = Path(output_dir or DEFAULT_OUT)
    output_dir.mkdir(parents=True, exist_ok=True)
    text = build_pullback_entry_pine()
    path = output_dir / MAIN_PINE
    path.write_text(text, encoding="utf-8")
    return {
        "path": str(path),
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "bytes": len(text.encode()),
        "main_pine": MAIN_PINE,
    }


def export_pine_expected_event_labels(
    frame: pd.DataFrame,
    *,
    variants: Sequence[str] = ("A0", "A1", "A6", "A9"),
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name in variants:
        cfg = _variant_by_name(name)
        timeline, _entries = apply_pullback_entry(frame, cfg)
        prev_side = 0
        for _, r in timeline.iterrows():
            ev = str(r.get("events") or "")
            ts = r.get("timestamp")
            bi = int(r.get("bar_index") or 0)
            side = int(r.get("entry_side") or 0)
            effective_side = side if side != 0 else prev_side
            st = str(r.get("entry_state") or "IDLE")
            if "short_armed" in ev or "long_armed" in ev:
                rows.append(
                    {
                        "timestamp": ts,
                        "bar_index": bi,
                        "direction": "short" if "short_armed" in ev else "long",
                        "event_type": "ARM",
                        "variant": name,
                        "price": r.get("armed_price"),
                        "breakout_level": None,
                        "state": st,
                        "reason": r.get("arming_type"),
                    }
                )
            if "short_pullback" in ev or "long_pullback" in ev:
                rows.append(
                    {
                        "timestamp": ts,
                        "bar_index": bi,
                        "direction": "short" if "short_pullback" in ev else "long",
                        "event_type": "PULLBACK",
                        "variant": name,
                        "price": None,
                        "breakout_level": None,
                        "state": st,
                        "reason": None,
                    }
                )
            if "short_ready" in ev or "long_ready" in ev:
                rows.append(
                    {
                        "timestamp": ts,
                        "bar_index": bi,
                        "direction": "short" if "short_ready" in ev else "long",
                        "event_type": "READY",
                        "variant": name,
                        "price": None,
                        "breakout_level": r.get("breakout_level"),
                        "state": st,
                        "reason": None,
                    }
                )
            if "invalidated:" in ev or "direct_reject:" in ev:
                direction = "short" if effective_side < 0 else ("long" if effective_side > 0 else "")
                rows.append(
                    {
                        "timestamp": ts,
                        "bar_index": bi,
                        "direction": direction,
                        "event_type": "INVALIDATED",
                        "variant": name,
                        "price": None,
                        "breakout_level": None,
                        "state": "IDLE",
                        "reason": ev,
                    }
                )
            if "break_rejected:" in ev:
                rows.append(
                    {
                        "timestamp": ts,
                        "bar_index": bi,
                        "direction": "short" if effective_side < 0 else ("long" if effective_side > 0 else ""),
                        "event_type": "REJECT",
                        "variant": name,
                        "price": None,
                        "breakout_level": r.get("breakout_level"),
                        "state": st,
                        "reason": ev,
                    }
                )
            if r.get("entry_signal"):
                direction = "short" if int(r.get("entry_side") or 0) < 0 else "long"
                rows.append(
                    {
                        "timestamp": ts,
                        "bar_index": bi,
                        "direction": direction,
                        "event_type": "TRIGGER",
                        "variant": name,
                        "price": r.get("entry_price"),
                        "breakout_level": r.get("breakout_level"),
                        "state": st,
                        "reason": r.get("entry_reason"),
                    }
                )
                fill_i = bi + 1
                if fill_i < len(frame):
                    fr = frame.iloc[fill_i]
                    rows.append(
                        {
                            "timestamp": fr.get("timestamp"),
                            "bar_index": fill_i,
                            "direction": direction,
                            "event_type": "FILL",
                            "variant": name,
                            "price": float(fr["open"]),
                            "breakout_level": r.get("breakout_level"),
                            "state": "FILL",
                            "reason": "next_open",
                        }
                    )
            if side != 0:
                prev_side = side
            elif "invalidated:" in ev or "direct_reject:" in ev or "reset_after_entry" in ev:
                prev_side = 0
    return pd.DataFrame(rows)


def write_pine_parity_artifacts(frame: pd.DataFrame, output_dir: Path | None = None) -> dict[str, Any]:
    output_dir = Path(output_dir or DEFAULT_OUT)
    output_dir.mkdir(parents=True, exist_ok=True)
    pine_meta = write_pullback_entry_pine(output_dir)
    labels = export_pine_expected_event_labels(frame)
    labels_path = output_dir / "pine_expected_event_labels.csv"
    labels.to_csv(labels_path, index=False)
    counts = {}
    reject_counts = {}
    for variant in ("A0", "A1", "A6", "A9"):
        sub = labels[labels["variant"] == variant] if not labels.empty else labels
        counts[variant] = {
            k: int((sub["event_type"] == k).sum()) if not sub.empty else 0
            for k in ("ARM", "PULLBACK", "READY", "TRIGGER", "FILL", "INVALIDATED", "REJECT")
        }
        reject_counts[variant] = counts[variant]["REJECT"]
    parity = {
        "symbol": "APTUSDT",
        "timeframe": "5m",
        "analyze_window": "2026-02-01 → 2026-04-30",
        "note": (
            "Static rule parity: Pine embeds C3.4B-medium arming + C3.5 SM for A0/A1/A6. "
            "A9 HTF gate uses closed-bar EMA9/20 bias via request.security(..., [1], lookahead_off), "
            "not full C3.4B HTF major_direction / protected_structure_state. "
            "No TradingView bar-for-bar runner available; pine_expected_event_labels.csv is the "
            "Python-authoritative visual checklist."
        ),
        "python_event_counts": counts,
        "pine_expected_labels_rows": int(len(labels)),
        "fill_rule": "TRIGGER on signal bar close; FILL at next bar open",
        "frozen_breakout": True,
        "lookahead": False,
        "variants_exact_target": ["A0", "A1", "A6"],
        "variants_documented_gap": ["A9"],
        "deviations": {
            "A9_htf": "EMA-bias proxy vs Python C3.4B HTF major_direction — event counts for A9 may diverge in TV",
            "structure_arming": "Pine uses embedded C3.4B medium rules; should match Python apply_protected_structure medium",
            "bar_for_bar_tv_runner": None,
        },
        "reject_counts": reject_counts,
    }
    (output_dir / "pine_parity_summary.json").write_text(json.dumps(json_safe(parity), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {"pine": pine_meta, "labels_csv": str(labels_path), "parity_summary": str(output_dir / "pine_parity_summary.json"), "c34b_arming_config_hash": c34b_config_hash(_C34B), "c35_a6_config_hash": c35_config_hash(_variant_by_name("A6"))}
    (output_dir / "pine_export_manifest.json").write_text(json.dumps(json_safe(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
