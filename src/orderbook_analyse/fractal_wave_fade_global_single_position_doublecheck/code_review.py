"""Static source review for leakage / causality risk patterns."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]  # .../src/orderbook_analyse

SCAN_GLOBS = [
    "fractal_wave_fade_global_single_position_db/**/*.py",
    "fractal_wave_fade_strategy_backtest_db/**/*.py",
    "fractal_signal_confluence_db/**/*.py",
    "fractal_dynamic_cluster_upgrade_db/**/*.py",
    "fractal_parent_lower_tf_quality_db/**/*.py",
    "fractal_wave_fade_trend_filter/**/*.py",
]

PATTERNS: list[tuple[str, str]] = [
    ("shift_neg", r"shift\s*\(\s*-"),
    ("bfill", r"\bbfill\b|\bfillna\s*\(.*method\s*=\s*['\"]bfill"),
    ("merge_asof", r"merge_asof"),
    ("centered_rolling", r"rolling\([^)]*center\s*=\s*True"),
    ("iloc_plus", r"iloc\s*\[\s*[^]]*\+\s*1"),
    ("searchsorted_left_future", r"searchsorted\([^)]*side\s*=\s*['\"]left['\"]"),
]


def run_code_review() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for glob in SCAN_GLOBS:
        for path in sorted(ROOT.glob(glob)):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            rel = str(path.relative_to(ROOT.parent))
            for name, pat in PATTERNS:
                for m in re.finditer(pat, text):
                    line = text.count("\n", 0, m.start()) + 1
                    snippet = text[m.start() : m.start() + 80].replace("\n", " ")
                    # triage
                    risk = "REVIEW"
                    note = ""
                    if name == "searchsorted_left_future":
                        risk = "OK_LIKELY"
                        note = "searchsorted left often used for flush-before / index-at; verify context"
                    if name == "iloc_plus":
                        risk = "REVIEW"
                        note = "check not reading future bars outside T0 entry resolution"
                    if name in ("shift_neg", "bfill", "centered_rolling"):
                        risk = "HIGH_IF_PRESENT"
                        note = "classic lookahead risk"
                    if name == "merge_asof":
                        risk = "REVIEW"
                        note = "direction must be backward for causality"
                    findings.append(
                        {
                            "file": rel,
                            "line": line,
                            "pattern": name,
                            "risk": risk,
                            "snippet": snippet,
                            "note": note,
                        }
                    )

    # Explicit known-safe notes for entry resolution
    findings.append(
        {
            "file": "orderbook_analyse/fractal_signal_confluence_db/signals.py",
            "line": 0,
            "pattern": "entry_resolution",
            "risk": "DOCUMENTED",
            "snippet": "resolve_entries uses searchsorted(..., side='right') → first open AFTER conf",
            "note": "correct T0 semantics if confirmation is closed-bar available_at",
        }
    )
    high = [f for f in findings if f["risk"] == "HIGH_IF_PRESENT"]
    return {
        "findings": findings,
        "high_risk_count": len(high),
        "summary": (
            "No shift(-1)/bfill/centered-rolling hits in scanned packages"
            if not high
            else f"{len(high)} high-risk pattern hits — see details"
        ),
    }


def render_code_review_md(review: dict[str, Any]) -> str:
    lines = [
        "# Code review findings (lookahead / causality)",
        "",
        review["summary"],
        "",
        "| File | Line | Pattern | Risk | Note |",
        "|------|------|---------|------|------|",
    ]
    for f in review["findings"]:
        if f["pattern"] == "entry_resolution" or f["risk"] in (
            "HIGH_IF_PRESENT",
            "REVIEW",
            "DOCUMENTED",
        ):
            lines.append(
                f"| `{f['file']}` | {f['line']} | {f['pattern']} | {f['risk']} | {f['note']} |"
            )
    lines += [
        "",
        "Scanned packages: global_single_position, strategy_backtest engine, confluence, "
        "dynamic upgrade, parent waves, trend filter.",
        "",
    ]
    return "\n".join(lines)
