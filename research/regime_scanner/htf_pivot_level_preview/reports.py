"""Write CSV / Pine / docs / integrity for HTF pivot level preview."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.htf_pivot_level_preview.config import (
    AUDIT_NAME,
    AUDIT_VERSION,
    HTF_PIVOT_SPECS,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_REPLACEMENT,
)


EXPECTED_COLUMNS = [
    "symbol",
    "source_type",
    "timeframe",
    "side",
    "level_price",
    "pivot_timestamp",
    "confirmation_timestamp",
    "visible_from_timestamp",
    "invalidated_at",
    "invalidation_reason",
    "replacement_level_id",
    "active",
    "touch_count",
    "first_touch_timestamp",
    "level_id",
    "sequence_id",
    "repaint_safe",
]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_levels_csv(path: Path, levels: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(levels)
    for c in EXPECTED_COLUMNS:
        if c not in df.columns:
            df[c] = None
    if df.empty:
        pd.DataFrame(columns=EXPECTED_COLUMNS).to_csv(path, index=False)
    else:
        df[EXPECTED_COLUMNS].to_csv(path, index=False)


def write_feature_semantics(path: Path) -> None:
    path.write_text(
        f"""# Feature Semantics — HTF Pivot Level Preview ({AUDIT_VERSION})

## Role

Visual validation only. Python scanner is source of truth. Pine embeds the same levels.

## HTF-only review pines

Embedded families: **4h / 12h / 1D pivots only**.

Not embedded: external_swing, protected.

Selection rule: all HTF levels sorted by `(visible_from_timestamp ASC, level_id ASC)`.
No truncation that drops HTF levels below TradingView line limit (500).

## Lifecycle modes

### replacement (`close_break_or_replacement`)

New confirmed pivot of same `(source×tf×side)` ends the previous active level at the
new level's `visible_from`. Close-break may end earlier.

### persistent (`close_break_only`)

New pivots do **not** replace prior levels. Each level stays active until close-break
or data end. Diagnostic comparison only.

## Touch markers

- `first_touch_timestamp` = first 5m bar close at/after `visible_from` that wick-touches
- Pine `T` marker uses `firstTouchArr` only
- No `T` when `first_touch_timestamp` is missing
- Touches before `visible_from` are forbidden

## Arrays (identical length)

seqArr, priceArr, sideArr, srcArr, activeArr, touchArr, invReasonArr,
visArr, pivotArr, invArr, firstTouchArr, idArr, tfArr, labelArr
""",
        encoding="utf-8",
    )


def write_visual_review(path: Path, result: dict[str, Any]) -> None:
    modes = result.get("modes") or {}
    path.write_text(
        f"""# VISUAL_REVIEW — HTF-only Pivot Levels (dual lifecycle)

Audit: `{AUDIT_NAME}` / `{AUDIT_VERSION}`

## Files

For each coin × lifecycle:

- `htf_pivot_<SYMBOL>_replacement.pine`
- `htf_pivot_<SYMBOL>_persistent.pine`
- `level_preview_expected_<SYMBOL>_replacement.csv`
- `level_preview_expected_<SYMBOL>_persistent.csv`

## Charts

- APTUSDT / BTCUSDT / ETHUSDT on **1h**, UTC
- Open **replacement** and **persistent** pines separately (do not mix)

## Checklist

1. Beginnt die Linie erst bei Bestätigung (`C` / visible_from)?
2. Ist `P` am Pivot-Open, Linie aber erst ab visible_from?
3. Stimmen Preis und TF (4h solid / 12h dashed / 1D dotted)?
4. Endet die Linie beim richtigen Break (`X`) bzw. Replacement (`R`)?
5. Bleiben historische Segmente erhalten?
6. Keine rückwirkenden Änderungen beim Replay?
7. Support/Resistance Seiten korrekt?
8. Persistent: mehrere gleichzeitige Levels derselben TF×Side möglich?
9. Replacement: jeweils nur ein aktives Level je TF×Side?
10. `T` nur am first_touch, nie am visible_from; ohne Touch kein T?
11. Pine `nLevels` == CSV-Zeilenanzahl für denselben Coin×Mode?
12. Keine src=4/5 (External/Protected) in den Arrays?

## Summaries

```json
{json.dumps({k: v.get("summaries") for k, v in modes.items()}, indent=2, default=str)}
```
""",
        encoding="utf-8",
    )


def write_dual_lifecycle_reports(output_dir: Path, result: dict[str, Any]) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}

    cov = output_dir / "joined_5m_coverage.csv"
    pd.DataFrame(result.get("coverage") or []).to_csv(cov, index=False)
    files["joined_5m_coverage.csv"] = cov

    modes = result.get("modes") or {}
    for lifecycle, payload in modes.items():
        mode_dir = output_dir / f"mode_{lifecycle}"
        mode_dir.mkdir(parents=True, exist_ok=True)
        pines = payload.get("pines") or {}
        levels = payload.get("levels") or []
        # combined csv
        combined = mode_dir / f"level_preview_expected_{lifecycle}.csv"
        _write_levels_csv(combined, levels)
        files[str(combined.relative_to(output_dir))] = combined
        # per-symbol csv + pine
        by_sym: dict[str, list[dict[str, Any]]] = {}
        for row in levels:
            by_sym.setdefault(str(row["symbol"]), []).append(row)
        for sym, text in pines.items():
            pine_name = f"htf_pivot_{sym}_{lifecycle}.pine"
            pine_path = output_dir / pine_name
            pine_path.write_text(text, encoding="utf-8")
            files[pine_name] = pine_path
            # also copy under mode dir
            (mode_dir / pine_name).write_text(text, encoding="utf-8")
            csv_name = f"level_preview_expected_{sym}_{lifecycle}.csv"
            csv_path = output_dir / csv_name
            _write_levels_csv(csv_path, by_sym.get(sym, []))
            files[csv_name] = csv_path
            _write_levels_csv(mode_dir / csv_name, by_sym.get(sym, []))

        summary_path = mode_dir / "level_summary.json"
        summary_path.write_text(
            json.dumps(payload.get("summaries") or {}, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        files[str(summary_path.relative_to(output_dir))] = summary_path

    sem = output_dir / "feature_semantics.md"
    write_feature_semantics(sem)
    files["feature_semantics.md"] = sem
    write_feature_semantics(Path(__file__).resolve().parent / "feature_semantics.md")

    vis = output_dir / "VISUAL_REVIEW.md"
    write_visual_review(vis, result)
    files["VISUAL_REVIEW.md"] = vis

    hashes = {k: _sha(v) for k, v in files.items() if v.exists()}
    integrity = {
        "audit_name": AUDIT_NAME,
        "audit_version": AUDIT_VERSION,
        "joined_rows": result.get("joined_rows"),
        "lifecycles": [LIFECYCLE_REPLACEMENT, LIFECYCLE_PERSISTENT],
        "mode_summaries": {k: v.get("summaries") for k, v in modes.items()},
        "mode_config_hashes": {k: v.get("config_hash") for k, v in modes.items()},
        "causality_flags": result.get("causality_flags"),
        "db_writes": False,
        "deterministic_run": True,
        "htf_only": True,
        "embed_all_htf_levels": True,
        "output_hashes": hashes,
        "htf_pivot_specs": HTF_PIVOT_SPECS,
    }
    integ = output_dir / "integrity.json"
    integ.write_text(json.dumps(integrity, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    files["integrity.json"] = integ
    return files


def write_reports(output_dir: Path, result: dict[str, Any]) -> dict[str, Path]:
    """Single-lifecycle report (legacy entry). Prefer write_dual_lifecycle_reports for review."""
    if "modes" in result:
        return write_dual_lifecycle_reports(output_dir, result)

    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    levels = result.get("levels") or []
    lifecycle = str(result.get("lifecycle_mode") or "replacement")
    csv_path = output_dir / f"level_preview_expected_{lifecycle}.csv"
    _write_levels_csv(csv_path, levels)
    files[csv_path.name] = csv_path

    cov = output_dir / "joined_5m_coverage.csv"
    pd.DataFrame(result.get("coverage") or []).to_csv(cov, index=False)
    files["joined_5m_coverage.csv"] = cov

    summary_path = output_dir / "level_summary.json"
    summary_path.write_text(
        json.dumps(result.get("summaries") or {}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    files["level_summary.json"] = summary_path

    for sym, text in (result.get("pines") or {}).items():
        p = output_dir / f"htf_pivot_{sym}_{lifecycle}.pine"
        p.write_text(text, encoding="utf-8")
        files[p.name] = p

    sem = output_dir / "feature_semantics.md"
    write_feature_semantics(sem)
    files["feature_semantics.md"] = sem

    dual_like = {
        "modes": {
            lifecycle: {
                "summaries": result.get("summaries"),
            }
        },
        "causality_flags": result.get("causality_flags"),
    }
    vis = output_dir / "VISUAL_REVIEW.md"
    write_visual_review(vis, dual_like)
    files["VISUAL_REVIEW.md"] = vis

    hashes = {k: _sha(v) for k, v in files.items() if v.exists()}
    integrity = {
        "audit_name": AUDIT_NAME,
        "audit_version": AUDIT_VERSION,
        "config": result.get("cfg"),
        "config_hash": result.get("config_hash"),
        "lifecycle_mode": lifecycle,
        "joined_rows": result.get("joined_rows"),
        "summaries": result.get("summaries"),
        "causality_flags": result.get("causality_flags"),
        "db_writes": False,
        "output_hashes": hashes,
        "htf_pivot_specs": HTF_PIVOT_SPECS,
    }
    integ = output_dir / "integrity.json"
    integ.write_text(json.dumps(integrity, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    files["integrity.json"] = integ
    return files
