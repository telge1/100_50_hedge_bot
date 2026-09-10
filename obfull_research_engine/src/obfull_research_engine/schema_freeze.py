from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd

from .paths import SCHEMA_JSON, SCHEMA_SHA


def load_frozen_schema() -> dict:
    return json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))


def load_frozen_sha() -> str:
    return SCHEMA_SHA.read_text(encoding="utf-8").strip()


def frozen_column_names(schema: dict | None = None) -> list[str]:
    schema = schema or load_frozen_schema()
    return [c["name"] for c in schema["columns"]]


def compute_schema_sha(schema: dict | None = None) -> str:
    schema = schema or load_frozen_schema()
    canon = {
        "schema_version": schema["schema_version"],
        "columns": [{"name": c["name"], "dtype": c["dtype"]} for c in schema["columns"]],
    }
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_schema_freeze() -> dict:
    expected = load_frozen_sha()
    actual = compute_schema_sha()
    ok = expected == actual
    return {"ok": ok, "expected": expected, "actual": actual, "schema_version": "mb_state_1s_v1"}


def columns_match_dataframe(df) -> dict:
    return inspect_columns(df)


def inspect_columns(df: pd.DataFrame, *, schema: dict | None = None) -> dict[str, Any]:
    """Fail-closed column inventory. Order-only divergence is not a set error."""
    schema = schema or load_frozen_schema()
    expected = frozen_column_names(schema)
    actual = list(df.columns)
    seen: dict[str, int] = {}
    for name in actual:
        seen[name] = seen.get(name, 0) + 1
    duplicates = [name for name, n in seen.items() if n > 1]
    missing = [c for c in expected if c not in seen]
    extra = [c for c in actual if c not in set(expected)]
    order_mismatch = (
        not missing and not extra and not duplicates and expected != actual
    )
    type_mismatches = _type_mismatches(df, schema, expected)
    ok = not missing and not extra and not duplicates and not order_mismatch and not type_mismatches
    return {
        "ok": ok,
        "missing": missing,
        "extra": extra,
        "duplicates": duplicates,
        "order_mismatch": order_mismatch,
        "type_mismatches": type_mismatches,
        "expected": expected,
        "actual": actual,
    }


def _type_mismatches(df: pd.DataFrame, schema: dict, expected: list[str]) -> list[dict[str, str]]:
    """Gross incompatibilities only. Does not coerce. NA-induced int→float is allowed."""
    by_name = {c["name"]: c["dtype"] for c in schema["columns"]}
    out: list[dict[str, str]] = []
    for name in expected:
        if name not in df.columns:
            continue
        series = df[name]
        if not isinstance(series, pd.Series):
            out.append({"column": name, "expected_dtype": by_name[name], "actual_dtype": "duplicate_or_frame"})
            continue
        want = by_name[name]
        actual = str(series.dtype)
        if want.startswith("datetime"):
            if not pd.api.types.is_datetime64_any_dtype(series):
                out.append({"column": name, "expected_dtype": want, "actual_dtype": actual})
            continue
        if want in {"int64", "float64"}:
            if not pd.api.types.is_numeric_dtype(series) and actual != "object":
                # object can hold None mixed with numbers from sparse builder rows
                out.append({"column": name, "expected_dtype": want, "actual_dtype": actual})
            elif pd.api.types.is_bool_dtype(series):
                out.append({"column": name, "expected_dtype": want, "actual_dtype": actual})
            continue
    return out


def align_dataframe_to_frozen_schema(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Reorder to frozen mb_state_1s_v1 names. Never mutate the schema. Never astype.

    Missing / extra / duplicate / type-mismatch → fail-closed, dataframe unchanged.
    Order-only mismatch → column permutation only.
    """
    check = inspect_columns(df)
    if check["missing"] or check["extra"] or check["duplicates"] or check["type_mismatches"]:
        check = dict(check)
        check["ok"] = False
        check["aligned"] = False
        return df, check
    expected = check["expected"]
    if check["order_mismatch"]:
        df = df.loc[:, expected]
        check = inspect_columns(df)
        check["aligned"] = True
        check["ok"] = bool(check["ok"])
        return df, check
    check = dict(check)
    check["aligned"] = False
    return df, check
