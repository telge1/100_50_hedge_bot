"""Display formatting (mirrors profit-verlauf timezone, not registry exports)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

DASHBOARD_TZ = timezone(timedelta(hours=3))
TIME_LABEL_FORMAT = "%d.%m.%y, %H:%M:%S"

DISPLAY_COLUMNS = (
    "StartTime",
    "EndTime",
    "Coin",
    "Status",
    "Entry",
    "SL",
    "TP",
    "PnL",
    "Endprofit +/-",
)


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_dashboard_time_label(value: Any) -> str:
    dt = _as_utc_datetime(value)
    if dt is None:
        return ""
    return dt.astimezone(DASHBOARD_TZ).strftime(TIME_LABEL_FORMAT)


def parse_filter_input_to_utc(value: str | None) -> datetime | None:
    """Interpret datetime-local / ISO filter strings in DASHBOARD_TZ, return UTC."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        naive = datetime.fromisoformat(text)
        if naive.tzinfo is not None:
            return naive.astimezone(timezone.utc)
        local = naive.replace(tzinfo=DASHBOARD_TZ)
        return local.astimezone(timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S,%f"):
        try:
            naive = datetime.strptime(text, fmt)
            local = naive.replace(tzinfo=DASHBOARD_TZ)
            return local.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def filter_input_for_datetime_local(value: str | None) -> str:
    """Value for HTML datetime-local from query string."""
    if not value:
        return ""
    text = str(value).strip()
    if "T" in text and len(text) >= 16:
        return text[:16]
    dt = parse_filter_input_to_utc(text)
    if dt is None:
        return text
    local = dt.astimezone(DASHBOARD_TZ)
    return local.strftime("%Y-%m-%dT%H:%M")


def format_pnl_display(pnl_pct: float | None, *, is_open: bool) -> str:
    if is_open:
        return "OPEN"
    if pnl_pct is None:
        return "0.00%"
    rounded = round(float(pnl_pct), 2)
    if rounded > 0:
        return f"+{rounded:.2f}%"
    if rounded < 0:
        return f"{rounded:.2f}%"
    return "0.00%"


def format_endprofit(pnl_pct: float | None, *, is_open: bool) -> str:
    if is_open or pnl_pct is None:
        return "OPEN"
    rounded = round(float(pnl_pct), 2)
    if rounded > 0:
        return "+"
    if rounded < 0:
        return "-"
    return "0"


def pnl_tone(pnl_pct: float | None, *, is_open: bool) -> Literal["positive", "negative", "neutral"]:
    if is_open or pnl_pct is None:
        return "neutral"
    rounded = round(float(pnl_pct), 2)
    if rounded > 0:
        return "positive"
    if rounded < 0:
        return "negative"
    return "neutral"


def pnl_css_class(tone: str) -> str:
    if tone == "positive":
        return "profit-positive"
    if tone == "negative":
        return "profit-negative"
    return ""


def format_total_pnl_summary(total: float) -> str:
    rounded = round(float(total), 2)
    if rounded > 0:
        return f"+{rounded:.2f} %"
    if rounded < 0:
        return f"{rounded:.2f} %"
    return "0.00 %"


def format_winrate(winning: int, denominator: int) -> str:
    if denominator <= 0:
        return "0 %"
    rate = winning / denominator * 100.0
    return f"{round(rate, 2)} %"


def row_to_display(raw: dict[str, Any]) -> dict[str, Any]:
    tracking = str(raw.get("tracking_status") or "").upper()
    is_open = tracking == "OPEN"
    allowed = int(raw.get("allowed") or 0) == 1
    status = "ALLOWED" if allowed else "BLOCKED"
    pnl_raw = raw.get("pnl_pct")
    pnl_value = float(pnl_raw) if pnl_raw is not None and not is_open else None
    tone = pnl_tone(pnl_value, is_open=is_open)
    css = pnl_css_class(tone)

    end_time_display = "OPEN" if is_open else format_dashboard_time_label(raw.get("end_time"))

    return {
        "StartTime": format_dashboard_time_label(raw.get("decision_time")),
        "EndTime": end_time_display,
        "Coin": str(raw.get("symbol") or ""),
        "Status": status,
        "Entry": raw.get("entry_price"),
        "SL": raw.get("initial_sl"),
        "TP": raw.get("tp"),
        "PnL": format_pnl_display(pnl_value, is_open=is_open),
        "Endprofit +/-": format_endprofit(pnl_value, is_open=is_open),
        "pnl_class": css,
        "endprofit_class": css if tone != "neutral" else "",
    }
