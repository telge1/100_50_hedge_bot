"""5m bar grid integrity checks (gaps, duplicates, future leakage)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def audit_bar_series(
    bar_keys: list[str],
    *,
    as_of_utc: str,
    expected_step_minutes: int = 5,
) -> dict[str, Any]:
    as_of = _parse_utc(as_of_utc)
    issues: list[dict[str, Any]] = []
    if not bar_keys:
        return {
            "ok": False,
            "duplicate_timestamps": 0,
            "missing_interior_gaps": 0,
            "future_bars_after_as_of": 0,
            "issues": [{"code": "NO_BARS"}],
        }

    seen: set[str] = set()
    dups = 0
    for k in bar_keys:
        if k in seen:
            dups += 1
            issues.append({"code": "DUPLICATE_TS", "t": k})
        seen.add(k)

    parsed = [_parse_utc(k) for k in bar_keys]
    future = sum(1 for t in parsed if t > as_of)
    if future:
        issues.append({"code": "FUTURE_BAR", "count": future})

    step = timedelta(minutes=expected_step_minutes)
    gaps = 0
    for i in range(1, len(parsed)):
        delta = parsed[i] - parsed[i - 1]
        if delta != step:
            if delta > step:
                gaps += int(delta / step) - 1
                issues.append(
                    {
                        "code": "GAP",
                        "from": bar_keys[i - 1],
                        "to": bar_keys[i],
                        "delta_minutes": int(delta.total_seconds() // 60),
                    }
                )
            elif delta < step:
                issues.append({"code": "NON_MONOTONIC", "from": bar_keys[i - 1], "to": bar_keys[i]})

    ok = dups == 0 and future == 0 and gaps == 0
    return {
        "ok": ok,
        "duplicate_timestamps": dups,
        "missing_interior_gaps": gaps,
        "future_bars_after_as_of": future,
        "first_bar": bar_keys[0],
        "last_bar": bar_keys[-1],
        "issues": issues[:50],
        "issues_truncated": len(issues) > 50,
    }
