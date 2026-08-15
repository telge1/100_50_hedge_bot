"""Backend-neutral canonicalization of liquidation_data source rows."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

# Reject taxonomy (technical vs domain).
TECHNICAL_REASONS = frozenset(
    {
        "technical_normalization_error",
        "unsupported_python_type",
        "unexpected_row_shape",
        "invalid_timestamp",
        "invalid_numeric",
        "missing_required_field",
    }
)


class NormalizationError(ValueError):
    """Technical failure while normalizing a source row."""

    def __init__(self, reason: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.field = field


def coerce_mapping(row: Any) -> dict[str, Any]:
    """Accept dict / Mapping / SQLAlchemy Row(_mapping). Reject bare tuples."""
    if row is None:
        raise NormalizationError("unexpected_row_shape", "row is None")
    if isinstance(row, Mapping):
        return {str(k): row[k] for k in row.keys()}
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return {str(k): mapping[k] for k in mapping.keys()}
    raise NormalizationError(
        "unexpected_row_shape",
        f"unsupported row type {type(row).__name__}; expected Mapping or Row._mapping",
    )


def coerce_source_timestamp(value: Any) -> datetime:
    """Normalize source timestamps to aware UTC.

    Audit rule: MySQL DATETIME from liquidation_data is UTC minute-start.
    Therefore tz-naive datetime/string values are interpreted as UTC — never
    as local server time.
    """
    if value is None:
        raise NormalizationError("missing_required_field", "timestamp is None", field="timestamp")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise NormalizationError(
                "unsupported_python_type",
                f"cannot decode timestamp bytes: {exc}",
                field="timestamp",
            ) from exc
    if isinstance(value, str):
        s = value.strip()
        if not s or s.upper() == "NULL":
            raise NormalizationError("missing_required_field", "empty timestamp", field="timestamp")
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise NormalizationError(
                "invalid_timestamp", f"unparseable timestamp {value!r}", field="timestamp"
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise NormalizationError(
        "unsupported_python_type",
        f"unsupported timestamp type {type(value).__name__}",
        field="timestamp",
    )


def coerce_decimal(value: Any, *, field: str, allow_none: bool = True) -> Decimal | None:
    """Deterministic numeric coercion. Preserves Decimal; accepts int/float/str."""
    if value is None:
        return None if allow_none else Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise NormalizationError(
            "unsupported_python_type",
            f"bool not allowed for {field}",
            field=field,
        )
    if isinstance(value, (int, float)):
        # float→Decimal via str to avoid binary float surprise in hashes when possible
        if isinstance(value, float):
            return Decimal(str(value))
        return Decimal(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        s = value.strip()
        if not s or s.upper() == "NULL":
            return None if allow_none else Decimal("0")
        try:
            return Decimal(s)
        except InvalidOperation as exc:
            raise NormalizationError(
                "invalid_numeric", f"invalid numeric for {field}: {value!r}", field=field
            ) from exc
    raise NormalizationError(
        "unsupported_python_type",
        f"unsupported numeric type {type(value).__name__} for {field}",
        field=field,
    )


def decimal_to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def normalize_symbol(symbol: Any) -> str:
    if symbol is None:
        raise NormalizationError("missing_required_field", "symbol is None", field="symbol")
    s = str(symbol).strip().upper()
    if not s or not s.isalnum():
        raise NormalizationError("invalid_symbol", f"invalid symbol: {symbol!r}", field="symbol")
    return s


REQUIRED_FIELDS = (
    "timestamp",
    "symbol",
    "open_interest",
    "open_interest_value",
    "long_liq_usd",
    "short_liq_usd",
    "buy_volume",
    "sell_volume",
    "spread",
)


def normalize_source_row(row: Any) -> dict[str, Any]:
    """Return canonical dict used by aggregation (backend-neutral).

    Numeric fields are Decimal|None; timestamp is aware UTC datetime.
    """
    m = coerce_mapping(row)
    missing = [f for f in ("timestamp", "symbol") if f not in m]
    if missing:
        raise NormalizationError(
            "missing_required_field",
            f"missing required field(s): {', '.join(missing)}",
            field=missing[0],
        )
    # Observed liquidation/volume columns may be 0; treat missing key as error only for required keys above.
    ts = coerce_source_timestamp(m.get("timestamp"))
    symbol = normalize_symbol(m.get("symbol"))
    return {
        "timestamp": ts,
        "symbol": symbol,
        "open_interest": coerce_decimal(m.get("open_interest"), field="open_interest"),
        "open_interest_value": coerce_decimal(
            m.get("open_interest_value"), field="open_interest_value"
        ),
        "long_liq_usd": coerce_decimal(m.get("long_liq_usd"), field="long_liq_usd", allow_none=True),
        "short_liq_usd": coerce_decimal(
            m.get("short_liq_usd"), field="short_liq_usd", allow_none=True
        ),
        "total_liq_usd": coerce_decimal(m.get("total_liq_usd"), field="total_liq_usd"),
        "buy_volume": coerce_decimal(m.get("buy_volume"), field="buy_volume"),
        "sell_volume": coerce_decimal(m.get("sell_volume"), field="sell_volume"),
        "spread": coerce_decimal(m.get("spread"), field="spread"),
    }


def type_name(value: Any) -> str:
    return type(value).__name__ if value is not None else "NoneType"


def safe_example(value: Any) -> str:
    """Short non-secret example for diagnostics."""
    if value is None:
        return "None"
    if isinstance(value, datetime):
        return f"datetime({value.isoformat()}, tzinfo={value.tzinfo!r})"
    if isinstance(value, Decimal):
        return f"Decimal({str(value)[:24]})"
    if isinstance(value, float):
        return f"float({value!r})"[:40]
    if isinstance(value, int) and not isinstance(value, bool):
        return f"int({value})"
    if isinstance(value, str):
        return f"str({value[:32]!r})"
    return f"{type(value).__name__}(...)"
