"""CSV/JSON/Markdown writers for orderflow absorption audit."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd


def _write_csv(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, pd.DataFrame):
        rows.to_csv(path, index=False)
        return
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def write_reports(out_dir: Path, result: dict[str, Any]) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    cov = result.get("coverage") or {}
    _write_csv(out_dir / "joined_5m_coverage.csv", cov.get("by_symbol") or [cov])
    files.append("joined_5m_coverage.csv")

    mapping = [
        ("absorption_features.csv", "features"),
        ("absorption_assignments.csv", "assignments"),
        ("forward_outcomes.csv", "outcomes"),
        ("absorption_summary.csv", "summary"),
        ("control_comparison.csv", "comparisons"),
        ("oi_diagnostic_summary.csv", "oi_diagnostic"),
        ("coin_summary.csv", "coin_summary"),
        ("lookback_summary.csv", "lookback_summary"),
    ]
    for fname, key in mapping:
        _write_csv(out_dir / fname, result.get(key) or [])
        files.append(fname)

    integrity = {
        "status": "ok",
        "audit": "orderflow_absorption",
        "joined_rows": result.get("joined_rows"),
        "n_feature_rows": result.get("n_feature_rows"),
        "pattern_counts": result.get("pattern_counts"),
        "pattern_counts_f1": result.get("pattern_counts_f1"),
        "decision": result.get("decision"),
        "decision_rationale": result.get("decision_rationale"),
        "by_symbol": result.get("by_symbol"),
        "config_hash": result.get("config_hash"),
        "cfg": result.get("cfg"),
        "db_writes": False,
        "no_db_writes": True,
        "deterministic": True,
        "no_secrets": True,
    }
    (out_dir / "integrity.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    files.append("integrity.json")

    summary = pd.DataFrame(result.get("summary") or [])
    comps = pd.DataFrame(result.get("comparisons") or [])

    def _block(pid: str) -> str:
        if summary.empty:
            return f"### {pid}\nNo rows.\n"
        g = summary[(summary["pattern"] == pid) & (summary["flow_rule"] == "F1")]
        if g.empty:
            return f"### {pid}\nNo F1 rows.\n"
        g2 = g[(g["horizon"] == 6) & (g["threshold"] == 0.005)]
        use = g2 if not g2.empty else g
        lines = [f"### {pid} (F1 focus)"]
        for _, r in use.iterrows():
            lines.append(
                f"- lb={int(r['lookback'])} h={int(r['horizon'])} thr={r['threshold']}: "
                f"n={int(r['n'])} coins={int(r['n_coins'])} "
                f"fav1st={r['fav_first_pct']:.1f} adv1st={r['adv_first_pct']:.1f} "
                f"med_edge={r['median_edge']:.3f}"
            )
        return "\n".join(lines) + "\n"

    report = f"""# Orderflow Absorption Pattern Audit — Report

## 1. Datenbasis
- joined_rows: {result.get('joined_rows')}
- feature/anchor rows: {result.get('n_feature_rows')}
- symbols: {result.get('by_symbol')}
- config_hash: {result.get('config_hash')}
- DB writes: false

## 2. Absorption-Semantik
See `feature_semantics.md`. Strong flow + weak/counter price reaction.
Features `[t-L,t)`; outcomes `[t+1, t+H]`.

## 3. Anchor Rows
{result.get('n_feature_rows')} valid (symbol, timestamp, lookback) anchors.

## 4. Pattern-Verteilung
All flow rules: {json.dumps(result.get('pattern_counts'))}
F1+ALL: {json.dumps(result.get('pattern_counts_f1'))}

## 5–8. Patterns & Controls
{_block('A1')}
{_block('A2')}
{_block('A3')}
{_block('A4')}
{_block('C1')}
{_block('C3')}

See `control_comparison.csv` for A vs C deltas.

## 9–11. Lookbacks / Horizons / Coins
See `lookback_summary.csv` and `coin_summary.csv`.

## 12. OI-Diagnose
See `oi_diagnostic_summary.csv` (diagnostic only).

## 13. Leakage / Kausalität
- Features exclude bar t
- Outcomes start at t+1
- F3 percentile / volume median causal prior window (current excluded)
- Gaps / sequence breaks block features and outcomes

## 14. Limitierungen
- 3 coins; F1/F2/F3 fixed (not optimized)
- No fees/entries; pattern detection only
- Spread ignored (near tick)
- Subset controls are not independent

## 15. Empfehlung
**{result.get('decision')}**

Rationale: {result.get('decision_rationale')}
"""
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    files.append("REPORT.md")
    _ = comps  # kept for future report expansion
    return files
