"""DateTime64 vs *_ns integrity checks for the ClickHouse research pilot."""

from __future__ import annotations

from typing import Any


class DateTimeStorageMismatch(RuntimeError):
    """Raised when DateTime64 columns disagree with stored nanosecond columns."""


def _as_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def assert_event_receive_datetime_match_ns(
    client: Any,
    *,
    database: str,
    table: str,
    symbol: str,
    start_ns: int,
    end_ns: int,
    sample_limit: int = 5,
) -> dict[str, Any]:
    """Prove toUnixTimestamp64Nano(event_time)=event_time_ns (and receive_time).

    Exact 0 ns tolerance only. Uses event_time_ns filter — no full table scan.
    """
    client.command("SET session_timezone = 'UTC'")
    sql = f"""
    SELECT
      record_ordinal,
      message_type,
      event_time_ns,
      toUnixTimestamp64Nano(event_time) AS et_from_dt,
      receive_time_ns,
      toUnixTimestamp64Nano(receive_time) AS rt_from_dt,
      formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.%f', 'UTC') AS et_fmt_utc
    FROM {database}.{table} FINAL
    WHERE symbol = {{symbol:String}}
      AND event_time_ns >= {{start_ns:UInt64}}
      AND event_time_ns < {{end_ns:UInt64}}
    ORDER BY record_ordinal
    LIMIT {{lim:UInt32}}
    """
    rows = client.query(
        sql,
        parameters={
            "symbol": symbol.upper(),
            "start_ns": int(start_ns),
            "end_ns": int(end_ns),
            "lim": int(sample_limit),
        },
    ).result_rows
    if not rows:
        raise DateTimeStorageMismatch(
            "STOP_DATETIME_STORAGE_MISMATCH: no rows to validate in window"
        )

    samples: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    for r in rows:
        ordinal, mt, et_ns, et_dt, rt_ns, rt_dt, et_fmt = r
        et_ns, et_dt = int(et_ns), int(et_dt)
        rt_ns, rt_dt = int(rt_ns), int(rt_dt)
        et_ok = et_dt == et_ns
        rt_ok = rt_dt == rt_ns
        sample = {
            "record_ordinal": int(ordinal),
            "message_type": _as_text(mt),
            "event_time_ns": et_ns,
            "et_from_dt": et_dt,
            "et_delta_ns": et_dt - et_ns,
            "et_fmt_utc": _as_text(et_fmt),
            "et_ok": et_ok,
            "receive_time_ns": rt_ns,
            "rt_from_dt": rt_dt,
            "rt_delta_ns": rt_dt - rt_ns,
            "rt_ok": rt_ok,
        }
        samples.append(sample)
        if not et_ok or not rt_ok:
            bad.append(sample)

    if bad:
        raise DateTimeStorageMismatch(
            "STOP_DATETIME_STORAGE_MISMATCH: DateTime64 columns disagree with *_ns "
            f"on {len(bad)}/{len(samples)} sampled rows; first={bad[0]}"
        )
    return {"ok": True, "samples": samples, "checked": len(samples)}
