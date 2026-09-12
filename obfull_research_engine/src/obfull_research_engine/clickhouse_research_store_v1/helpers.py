"""Pure helpers: record_id, window filter, import_id, resource guards."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_iso_ns(value: str) -> datetime:
    """Parse ISO-8601 with optional fractional seconds into timezone-aware UTC."""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # datetime.fromisoformat handles up to microseconds; trim beyond 6 frac digits
    if "." in s:
        head, rest = s.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                frac += ch
            else:
                tz = rest[i:]
                break
        frac_us = (frac + "000000")[:6]
        s = f"{head}.{frac_us}{tz}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def datetime_to_ns(dt: datetime) -> int:
    dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1_000


def iso_to_ns_exact(value: str) -> int:
    """Parse ISO-8601 to integer nanoseconds without float conversion.

    Fractional seconds may have 1–9 digits; missing digits are zero-padded on the right.
    """
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Split timezone
    tz = "+00:00"
    for sign in ("+", "-"):
        # timezone after time portion
        idx = s.rfind(sign)
        if idx > 10:
            tz = s[idx:]
            s = s[:idx]
            break
    if "T" in s:
        date_part, time_part = s.split("T", 1)
    else:
        date_part, time_part = s, "00:00:00"
    if "." in time_part:
        hms, frac = time_part.split(".", 1)
        digits = "".join(ch for ch in frac if ch.isdigit())
    else:
        hms, digits = time_part, ""
    frac9 = (digits + "000000000")[:9]
    # Rebuild microsecond-limited datetime for the whole-second base, then add ns rem.
    base = datetime.fromisoformat(f"{date_part}T{hms}{tz}")
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base = base.astimezone(timezone.utc)
    sec = int(base.timestamp())
    return sec * 1_000_000_000 + int(frac9)


def ns_to_datetime(ns: int) -> datetime:
    sec, rem = divmod(int(ns), 1_000_000_000)
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(
        microsecond=rem // 1_000
    )


def make_record_id(*, source_segment_sha256: str, record_ordinal: int) -> str:
    material = f"{source_segment_sha256}:{int(record_ordinal)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_import_id(
    *,
    schema_version: str,
    source_segment_sha256: str,
    symbol: str,
    window_start: str,
    window_end: str,
) -> str:
    material = "|".join(
        [
            schema_version,
            source_segment_sha256,
            symbol.upper(),
            window_start,
            window_end,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def event_in_window(
    event_time_ns: int | None,
    *,
    window_start_ns: int,
    window_end_ns: int,
) -> bool:
    """Half-open [start, end). Missing event_time => not in window."""
    if event_time_ns is None:
        return False
    return window_start_ns <= int(event_time_ns) < window_end_ns


@dataclass(frozen=True)
class ResourceLimits:
    deadline_monotonic: float
    max_rss_bytes: int


def current_rss_bytes() -> int:
    # Linux: VmRSS in kB
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except OSError:
        pass
    try:
        import resource

        # ru_maxrss is KiB on Linux
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:  # noqa: BLE001
        return 0


def check_resource_limits(limits: ResourceLimits, *, now_monotonic: float | None = None) -> None:
    import time

    now = time.monotonic() if now_monotonic is None else now_monotonic
    if now > limits.deadline_monotonic:
        raise RuntimeError("STOP_RESOURCE_LIMIT: wall-clock deadline exceeded")
    rss = current_rss_bytes()
    if rss > limits.max_rss_bytes:
        raise RuntimeError(
            f"STOP_RESOURCE_LIMIT: RSS {rss} bytes exceeds {limits.max_rss_bytes}"
        )


def load_manifest(segment_path: Path) -> dict[str, Any]:
    manifest_path = Path(str(segment_path) + ".manifest.json")
    if not manifest_path.is_file():
        raise RuntimeError(f"STOP_SOURCE_MANIFEST_INVALID: missing {manifest_path}")
    try:
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"STOP_SOURCE_MANIFEST_INVALID: JSON error: {exc}") from exc
    if not isinstance(man, dict):
        raise RuntimeError("STOP_SOURCE_MANIFEST_INVALID: root not object")
    return man


def verify_manifest_against_segment(segment_path: Path, manifest: dict[str, Any]) -> str:
    expected = str(manifest.get("segment_sha256") or "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise RuntimeError("STOP_SOURCE_MANIFEST_INVALID: segment_sha256 missing/invalid")
    h = hashlib.sha256()
    with Path(segment_path).open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"STOP_SOURCE_MANIFEST_INVALID: segment SHA mismatch "
            f"manifest={expected[:12]}… actual={actual[:12]}…"
        )
    return expected


def load_clickhouse_env() -> None:
    candidates = [
        Path("/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/.env"),
        Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.env"),
        Path("/home/telgenbuescher/projects/orderbook_analyse/.env"),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break


def get_clickhouse_client() -> Any:
    import clickhouse_connect

    load_clickhouse_env()
    host = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = int(os.environ.get("CLICKHOUSE_HTTP_PORT") or os.environ.get("CLICKHOUSE_PORT") or "8123")
    user = os.environ.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        connect_timeout=10,
        send_receive_timeout=60,
        settings={"session_timezone": "UTC"},
    )
    # Defense in depth: naive strings must not be interpreted as Europe/Paris.
    try:
        client.command("SET session_timezone = 'UTC'")
    except Exception:  # noqa: BLE001
        pass
    return client


def ns_to_utc_datetime(ns: int) -> datetime:
    """Timezone-aware UTC datetime for ClickHouse DateTime64 inserts.

    clickhouse_connect treats naive datetimes / naive ISO strings as *local*
    timezone (e.g. Europe/Paris CEST = UTC+2), which silently shifts stored
    DateTime64(…, 'UTC') by -2h. Always pass tz-aware UTC objects.
    """
    sec, rem = divmod(int(ns), 1_000_000_000)
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(microsecond=rem // 1000)

