"""CSV/JSON/Markdown writers for oi_price_delta_pattern audit."""

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
        ("pattern_features.csv", "features"),
        ("pattern_assignments.csv", "assignments"),
        ("forward_outcomes.csv", "outcomes"),
        ("pattern_summary.csv", "summary"),
        ("pattern_comparison.csv", "comparisons"),
        ("coin_summary.csv", "coin_summary"),
        ("direction_summary.csv", "direction_summary"),
    ]
    for fname, key in mapping:
        _write_csv(out_dir / fname, result.get(key) or [])
        files.append(fname)

    integrity = {
        "status": "ok",
        "audit": "oi_price_delta_pattern",
        "joined_rows": result.get("joined_rows"),
        "n_feature_rows": result.get("n_feature_rows"),
        "pattern_counts": result.get("pattern_counts"),
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

    # compact report
    summary = pd.DataFrame(result.get("summary") or [])
    comps = pd.DataFrame(result.get("comparisons") or [])
    primary = summary[~summary["pattern"].astype(str).str.startswith("COMBO::")] if not summary.empty else summary

    def _pat_block(pid: str) -> str:
        if primary.empty:
            return f"### {pid}\nNo rows.\n"
        g = primary[primary["pattern"] == pid]
        if g.empty:
            return f"### {pid}\nNo rows.\n"
        # focus h6 / 0.5%
        g2 = g[(g["horizon"] == 6) & (g["threshold"] == 0.005)]
        use = g2 if not g2.empty else g
        lines = [f"### {pid}", f"- rows (focus): {int(use['n'].sum()) if 'n' in use else 0}"]
        for _, r in use.iterrows():
            lines.append(
                f"- lb={int(r['lookback'])} h={int(r['horizon'])} thr={r['threshold']}: "
                f"n={int(r['n'])} coins={int(r['n_coins'])} "
                f"up%={r['up_reached_pct']:.1f} down%={r['down_reached_pct']:.1f} "
                f"up1st={r['up_first_pct']:.1f} down1st={r['down_first_pct']:.1f} "
                f"med_edge={r['median_edge']:.3f}"
            )
        return "\n".join(lines) + "\n"

    report = f"""# OI + Price + Delta Pattern Audit — Report

## 1. Datenbasis
- joined_rows: {result.get('joined_rows')}
- feature/anchor rows: {result.get('n_feature_rows')}
- symbols: {result.get('by_symbol')}
- config_hash: {result.get('config_hash')}
- DB writes: false

## 2. Feature-Semantik
See `feature_semantics.md`. Features use bars `[t-L, t)`; outcomes use `[t+1, t+H]`.

## 3. Anchor Rows
{result.get('n_feature_rows')} valid (symbol, timestamp, lookback) anchors.

## 4. Pattern-Verteilung
{json.dumps(result.get('pattern_counts'), indent=2)}

## 5–8. Primary Patterns
{_pat_block('P1')}
{_pat_block('P2')}
{_pat_block('P3')}
{_pat_block('P4')}

## 9–10. OI / Delta Zusatznutzen
See `pattern_comparison.csv` rows `oi_up_vs_not|*` and `delta_*_vs_not|*`.

## 11. BTC vs ETH vs APT
See `coin_summary.csv`.

## 12–15. Lookbacks / Horizons / Thresholds
See `pattern_summary.csv` (grouped).

## 16. Limitierungen
- 3 coins only; min sample 30 is screening-only
- No fees/entries; pattern detection only
- Tight state thresholds are fixed (not optimized)
- Subset comparisons are not independent events

## 17. Empfehlung
**{result.get('decision')}**

Rationale: {result.get('decision_rationale')}
"""
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    files.append("REPORT.md")
    return files
