"""Schema validation for F3 public trade impact audit inputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.constants import (
    REQUIRED_INPUT_FILES,
    WINDOW_COMPARISONS,
)


def _prefix_fields(prefix: str) -> tuple[str, ...]:
    return (
        f"{prefix}_aggressive_notional",
        f"{prefix}_impact_per_notional",
        f"{prefix}_trades_present",
    )


def required_impact_compression_columns() -> tuple[str, ...]:
    cols: list[str] = ["cluster_id", "direction", "data_abort"]
    for _pair, first_prefix, last_prefix in WINDOW_COMPARISONS:
        cols.extend(_prefix_fields(first_prefix))
        cols.extend(_prefix_fields(last_prefix))
    return tuple(cols)


@dataclass(frozen=True)
class SchemaCheckResult:
    ok: bool
    missing_fields: tuple[str, ...]
    present_columns: tuple[str, ...]
    missing_files: tuple[str, ...]


def check_input_schema(input_dir: Path) -> SchemaCheckResult:
    missing_files = tuple(
        name for name in REQUIRED_INPUT_FILES if not (input_dir / name).is_file()
    )
    if missing_files:
        return SchemaCheckResult(
            ok=False,
            missing_fields=(),
            present_columns=(),
            missing_files=missing_files,
        )

    impact_path = input_dir / "impact_compression_metrics.csv"
    frame = pd.read_csv(impact_path, nrows=0)
    present = tuple(frame.columns.astype(str).tolist())
    present_set = set(present)
    required = required_impact_compression_columns()
    missing_fields = tuple(col for col in required if col not in present_set)
    ok = not missing_fields and not missing_files
    return SchemaCheckResult(
        ok=ok,
        missing_fields=missing_fields,
        present_columns=present,
        missing_files=missing_files,
    )
