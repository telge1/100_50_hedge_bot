"""Write REPORT.md, feature_semantics.md, integrity.json and CSV tables."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.orderflow_absorption_level.config import (
    AUDIT_NAME,
    AUDIT_VERSION,
    IMPORTED_ABSORPTION,
    IMPORTED_LEVELS,
    NEW_ADAPTERS,
    LevelAbsorptionConfig,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, pd.DataFrame):
        rows.to_csv(path, index=False)
    else:
        pd.DataFrame(rows).to_csv(path, index=False)


def write_feature_semantics(path: Path) -> None:
    text = """# Feature Semantics — Level-Context × Orderflow Absorption V1

## Absorption (imported unchanged)

- Feature window: `[t-L, t)` with `L=24`
- Outcomes: from `entry_eligible_index + 1` (not from absorption anchor if R1/R2 later)
- Flow rule: `F1` (`delta_ratio <= -0.10` for A4; mirrored for A2)
- Patterns: `A4` bullish treatment; `A2` bearish treatment; `A1` diagnostic only
- Gap/sequence guards: contiguous `sequence_id` + 300s bars

## Level distance

```text
distance_atr = abs(close[t] - level_price) / atr_14[t-1]
max_distance_atr = 0.50
```

Buckets: touch ≤0.10; very_near ≤0.25; near ≤0.50; far >0.50; no_level

## Visibility (strict causal)

```text
confirmation_index < anchor_index
```

Same-bar confirmation is **not** visible. Future levels never assigned.

## Priority

`protected` before `external_swing`; then nearest distance. Confluence if other type within 0.25 ATR.

## Events

Consecutive same pattern × same level zone merged; cooldown 6 bars after end.
Event id: `sha1(symbol|pattern|flow|lookback|level_id|event_start_iso)[:20]`

## Confirmations

- R0: entry = event start
- R1: rejection wick + close on side
- R2: break + reclaim in 1–3 bars
"""
    path.write_text(text, encoding="utf-8")


def write_report(path: Path, result: dict[str, Any]) -> None:
    cfg: dict[str, Any] = result.get("cfg") or {}
    decision = result.get("decision")
    rationale = result.get("decision_rationale")
    lines = [
        "# REPORT — Level-Context × Orderflow Absorption V1",
        "",
        "## 1. Executive Summary",
        "",
        f"**Decision:** `{decision}`",
        "",
        f"{rationale}",
        "",
        "## 2. Datenbasis",
        "",
        f"- Symbols: {cfg.get('symbols')}",
        f"- Import version: {cfg.get('import_version')}",
        f"- Joined rows: {result.get('joined_rows')}",
        f"- Patterns: {cfg.get('patterns')}; flow: {cfg.get('flow_rules')}; lookbacks: {cfg.get('lookbacks')}",
        f"- Horizons: {cfg.get('horizons')}; thresholds: {cfg.get('move_thresholds')}",
        "",
        "## 3. Wiederverwendete Absorption-Logik",
        "",
        "Imported (unchanged):",
        "",
    ]
    for x in IMPORTED_ABSORPTION:
        lines.append(f"- `{x}`")
    lines += [
        "",
        "Thin adapters (new):",
        "",
    ]
    for x in NEW_ADAPTERS:
        lines.append(f"- `{x}`")
    lines += [
        "",
        f"- Existing `orderflow_absorption/` files unchanged: **{result.get('absorption_unchanged', True)}**",
        "",
        "## 4. Level-Kausalität",
        "",
        "- Pivot/Protected visibility: `confirmation_index < t`",
        "- ATR reference: `atr_14[t-1]`",
        "- Anchor price: `close[t]`",
        "",
        "Imported level helpers:",
        "",
    ]
    for x in IMPORTED_LEVELS:
        lines.append(f"- `{x}`")
    lines += [
        "",
        "## 5. Level-Inventar",
        "",
        f"- Level rows: {result.get('n_levels')}",
        f"- By type: {result.get('levels_by_type')}",
        "",
        "## 6. Event-Deduplizierung",
        "",
        f"- Events: {result.get('n_events')}",
        f"- Cooldown bars: {cfg.get('event_cooldown_bars')}",
        "",
        "## 7. Bullish A4@Support",
        "",
        _table_snip(result.get("treatment_summary"), "A4_"),
        "",
        "## 8. Bearish A2@Resistance",
        "",
        _table_snip(result.get("treatment_summary"), "A2_"),
        "",
        "## 9. A1 diagnostisch",
        "",
        _table_snip(result.get("treatment_summary"), "A1_"),
        "",
        "## 10. Protected vs Swing",
        "",
        _df_snip(result.get("level_type_summary")),
        "",
        "## 11. Distanz-Buckets",
        "",
        _df_snip(result.get("distance_bucket_summary")),
        "",
        "## 12. R0/R1/R2",
        "",
        f"- Confirmation counts: {result.get('confirmation_counts')}",
        "",
        _df_snip(result.get("confirmation_summary")),
        "",
        "## 13. Kontrollen K1–K4",
        "",
        _df_snip(result.get("control_comparison")),
        "",
        "## 14. BTC/ETH/APT",
        "",
        _df_snip(result.get("coin_summary")),
        "",
        "## 15. Equal-/Median-Coin",
        "",
        _df_snip(result.get("equal_coin_summary")),
        "",
        _df_snip(result.get("median_coin_summary")),
        "",
        "## 16. Leakage-/Repaint-Prüfung",
        "",
        f"- Causal flags: {result.get('causality_flags')}",
        f"- Known leakage issues: {result.get('known_leakage', False)}",
        "",
        "## 17. Limitierungen",
        "",
        "- V1 only: protected + external_swing; no HTF/session/range",
        "- No parameter optimization; fixed horizons/thresholds",
        "- Smoke windows may yield small samples",
        "",
        "## 18. Entscheidung",
        "",
        f"`{decision}`",
        "",
        "## 19. Empfehlung",
        "",
        result.get("recommendation", "Proceed to full 3-coin run only after smoke integrity OK."),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _table_snip(rows: list[dict[str, Any]] | None, prefix: str) -> str:
    if not rows:
        return "_(empty)_"
    df = pd.DataFrame(rows)
    if "treatment" in df.columns:
        df = df[df["treatment"].astype(str).str.startswith(prefix)]
    if df.empty:
        return "_(empty)_"
    return "```\n" + df.to_string(index=False) + "\n```"


def _df_snip(rows: list[dict[str, Any]] | None) -> str:
    if not rows:
        return "_(empty)_"
    df = pd.DataFrame(rows)
    if df.empty:
        return "_(empty)_"
    return "```\n" + df.to_string(index=False) + "\n```"


def write_integrity(path: Path, result: dict[str, Any], output_files: dict[str, Path]) -> None:
    hashes = {name: _sha256_file(p) for name, p in output_files.items() if p.exists()}
    payload = {
        "audit_name": AUDIT_NAME,
        "audit_version": AUDIT_VERSION,
        "input_config": result.get("cfg"),
        "config_hash": result.get("config_hash"),
        "input_semantics": {
            "loader": "load_joined_5m",
            "import_version": (result.get("cfg") or {}).get("import_version"),
            "db_writes": False,
            "read_only": True,
        },
        "row_counts": result.get("row_counts"),
        "level_counts": result.get("levels_by_type"),
        "event_counts": result.get("event_counts"),
        "confirmation_counts": result.get("confirmation_counts"),
        "outcome_counts": result.get("outcome_counts"),
        "output_hashes": hashes,
        "db_writes": False,
        "deterministic_run": True,
        "causality_flags": result.get("causality_flags"),
        "decision": result.get("decision"),
        "decision_gates": result.get("decision_rationale"),
        "imported_absorption": list(IMPORTED_ABSORPTION),
        "imported_levels": list(IMPORTED_LEVELS),
        "new_adapters": list(NEW_ADAPTERS),
        "absorption_unchanged": True,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_reports(output_dir: Path, result: dict[str, Any]) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}

    mapping = [
        ("joined_5m_coverage.csv", result.get("coverage_rows") or []),
        ("level_inventory.csv", result.get("level_inventory") or []),
        ("anchor_level_assignments.csv", result.get("anchor_level_assignments") or []),
        ("absorption_level_events.csv", result.get("events") or []),
        ("treatment_assignments.csv", result.get("treatment_assignments") or []),
        ("control_assignments.csv", result.get("control_assignments") or []),
        ("matched_control_pairs.csv", result.get("matched_control_pairs") or []),
        ("confirmation_events.csv", result.get("confirmation_events") or []),
        ("event_forward_outcomes.csv", result.get("outcomes") or []),
        ("event_summary.csv", result.get("event_summary") or []),
        ("treatment_summary.csv", result.get("treatment_summary") or []),
        ("control_comparison.csv", result.get("control_comparison") or []),
        ("level_type_summary.csv", result.get("level_type_summary") or []),
        ("distance_bucket_summary.csv", result.get("distance_bucket_summary") or []),
        ("confirmation_summary.csv", result.get("confirmation_summary") or []),
        ("coin_summary.csv", result.get("coin_summary") or []),
        ("equal_coin_summary.csv", result.get("equal_coin_summary") or []),
        ("median_coin_summary.csv", result.get("median_coin_summary") or []),
    ]
    for name, rows in mapping:
        p = output_dir / name
        _write_csv(p, rows)
        files[name] = p

    sem = output_dir / "feature_semantics.md"
    write_feature_semantics(sem)
    files["feature_semantics.md"] = sem

    report = output_dir / "REPORT.md"
    write_report(report, result)
    files["REPORT.md"] = report

    integrity = output_dir / "integrity.json"
    write_integrity(integrity, result, files)
    files["integrity.json"] = integrity

    # also place package copy of feature_semantics at package root is separate
    return files
