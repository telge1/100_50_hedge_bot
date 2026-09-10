"""Fail-closed coverage check for arbitrary [start, end) research windows."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import (
    DEFAULT_ARCHIVE_ROOT,
)
from orderbook_analyse.research.general_market_behavior_v1.coverage import (
    evaluate_segment,
    list_hour_segments,
    load_clickhouse_env,
)
from orderbook_analyse.research.general_market_behavior_v1.semantics import OI_HARD_AGE_MS

from .coverage_catalog import classify_liq_source, classify_trade_source
from .timeparse import format_utc_z

SOURCE_KEYS = (
    "FULL_OB",
    "CHECKPOINT",
    "REPLAY_CHAIN",
    "PUBLIC_TRADES",
    "PRICE",
    "OPEN_INTEREST",
    "LIQUIDATIONS",
)

STATUS_COMPLETE = "COMPLETE"
STATUS_MISSING = "MISSING"
STATUS_NOT_CLOSED = "SOURCE_NOT_CLOSED"
STATUS_STALE = "STALE"


def _q(sql: str) -> str:
    host = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = os.environ.get("CLICKHOUSE_HTTP_PORT") or os.environ.get("CLICKHOUSE_PORT") or "8123"
    user = os.environ.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    params = {"user": user}
    if password:
        params["password"] = password
    url = f"http://{host}:{port}/?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, data=sql.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode().strip()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _hour_floor(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _iter_hours(start: datetime, end: datetime) -> list[datetime]:
    h = _hour_floor(start)
    out: list[datetime] = []
    while h < end:
        out.append(h)
        h += timedelta(hours=1)
    return out


def _covers_subinterval(ev: Any, need_start: datetime, need_end: datetime, *, tol_s: float = 2.0) -> bool:
    first = _parse_utc(ev.first_event_time)
    last = _parse_utc(ev.last_event_time)
    return first <= need_start + timedelta(seconds=tol_s) and last >= need_end - timedelta(seconds=tol_s)


def _segment_index(symbol: str, archive_root: Path) -> dict[datetime, list[dict[str, Any]]]:
    load_clickhouse_env()
    by_hour: dict[datetime, list[dict[str, Any]]] = {}
    for seg in list_hour_segments(archive_root, symbol):
        man_path = Path(str(seg) + ".manifest.json")
        if not man_path.exists():
            continue
        man = json.loads(man_path.read_text(encoding="utf-8"))
        ev = evaluate_segment(seg, symbol=symbol)
        hour = _parse_utc(str(man["utc_hour"]))
        trade_meta = classify_trade_source(symbol, hour, ev.trades_count)
        liq_meta = classify_liq_source(symbol, hour, ev.liq_count)
        by_hour.setdefault(hour, []).append(
            {
                "eval": ev,
                "manifest": man,
                "segment_path": str(seg),
                "trade_meta": trade_meta,
                "liq_meta": liq_meta,
            }
        )
    return by_hour


def _pick_best_segment(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not cands:
        return None

    def score(c: dict[str, Any]) -> tuple:
        ev = c["eval"]
        return (
            1 if ev.full_join else 0,
            1 if ev.replay_chain_complete else 0,
            1 if ev.full_wall_clock_coverage else 0,
            1 if ev.completion_status == "COMPLETE" else 0,
            -ev.in_segment_u_gap_count,
        )

    return sorted(cands, key=score, reverse=True)[0]


def _mark_missing(
    source_status: dict[str, str],
    missing: list[dict[str, str]],
    source: str,
    need_start: datetime,
    need_end: datetime,
    reason: str,
    *,
    status: str = STATUS_MISSING,
) -> None:
    if source_status.get(source) == STATUS_COMPLETE:
        source_status[source] = status
    elif status == STATUS_NOT_CLOSED:
        source_status[source] = status
    missing.append(
        {
            "source": source,
            "start": format_utc_z(need_start),
            "end": format_utc_z(need_end),
            "reason": reason,
        }
    )


def check_interval_coverage(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    archive_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return structured coverage report for [start, end)."""
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = Path(archive_root or DEFAULT_ARCHIVE_ROOT)

    source_status = {k: STATUS_COMPLETE for k in SOURCE_KEYS}
    missing_intervals: list[dict[str, str]] = []
    hour_details: list[dict[str, Any]] = []
    gap_count = 0
    resync_count = 0
    checkpoint_ids: list[str] = []
    replay_epochs = 0

    if end > now + timedelta(seconds=2):
        _mark_missing(
            source_status,
            missing_intervals,
            "FULL_OB",
            max(start, _hour_floor(now)),
            end,
            "window_extends_into_open_or_future_time",
            status=STATUS_NOT_CLOSED,
        )

    by_hour = _segment_index(symbol, root)

    for hour in _iter_hours(start, end):
        hour_end = hour + timedelta(hours=1)
        need_start = max(start, hour)
        need_end = min(end, hour_end)
        if need_start >= need_end:
            continue
        key = format_utc_z(hour)
        needs_full_hour = need_start == hour and need_end == hour_end

        if hour >= _hour_floor(now):
            _mark_missing(
                source_status,
                missing_intervals,
                "FULL_OB",
                need_start,
                need_end,
                "hour_not_closed",
                status=STATUS_NOT_CLOSED,
            )
            hour_details.append({"hour": key, "status": STATUS_NOT_CLOSED})
            continue

        best = _pick_best_segment(by_hour.get(hour) or [])
        if best is None:
            for src in ("FULL_OB", "CHECKPOINT", "REPLAY_CHAIN", "PRICE"):
                _mark_missing(
                    source_status,
                    missing_intervals,
                    src,
                    need_start,
                    need_end,
                    "no_closed_segment",
                )
            hour_details.append({"hour": key, "status": "NO_SEGMENT"})
            continue

        ev = best["eval"]
        man = best["manifest"]
        gap_count += int(ev.gap_count_manifest or 0) + int(ev.in_segment_u_gap_count or 0)
        resync_count += int(man.get("reconnect_count") or 0)
        if man.get("segment_sha256"):
            checkpoint_ids.append(str(man["segment_sha256"]))
        replay_epochs += int(man.get("checkpoint_count") or 0)

        covers = _covers_subinterval(ev, need_start, need_end)
        wall_ok = bool(ev.full_wall_clock_coverage) if needs_full_hour else covers
        replay_ok = bool(ev.replay_chain_complete) and int(ev.in_segment_u_gap_count or 0) == 0
        checkpoint_ok = replay_ok and (
            ev.completion_status == "COMPLETE" or bool(man.get("has_valid_anchor"))
        )

        # FULL_OB requires both wall/event span coverage AND a contiguous replay chain.
        if not wall_ok or not replay_ok:
            _mark_missing(
                source_status,
                missing_intervals,
                "FULL_OB",
                need_start,
                need_end,
                (
                    "wall_clock_or_event_span_incomplete"
                    if not wall_ok
                    else f"replay_incomplete u_gaps={ev.in_segment_u_gap_count}"
                ),
            )
        if not checkpoint_ok:
            _mark_missing(
                source_status,
                missing_intervals,
                "CHECKPOINT",
                need_start,
                need_end,
                f"completion={ev.completion_status} anchor={man.get('has_valid_anchor')}",
            )
        if not replay_ok:
            _mark_missing(
                source_status,
                missing_intervals,
                "REPLAY_CHAIN",
                need_start,
                need_end,
                f"u_gaps={ev.in_segment_u_gap_count} replay_ok={ev.replay_ok}",
            )
        if not (wall_ok and replay_ok and checkpoint_ok):
            _mark_missing(
                source_status,
                missing_intervals,
                "PRICE",
                need_start,
                need_end,
                "canonical_mid_requires_replayable_book",
            )

        # Public trades
        hs = need_start.strftime("%Y-%m-%d %H:%M:%S")
        he = need_end.strftime("%Y-%m-%d %H:%M:%S")
        pt_n = int(
            _q(
                "SELECT count() FROM orderbook_analysis.public_trades_canonical "
                f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{he}' FORMAT TSV"
            )
            or 0
        )
        trade_meta = best["trade_meta"]
        if needs_full_hour:
            if trade_meta["trade_source_missing"] or pt_n == 0:
                _mark_missing(
                    source_status,
                    missing_intervals,
                    "PUBLIC_TRADES",
                    need_start,
                    need_end,
                    trade_meta["trade_source_status"] if pt_n == 0 else trade_meta["trade_source_status"],
                )
        else:
            # Sub-hour: zero trades OK if source not missing for the containing hour
            if trade_meta["trade_source_missing"]:
                _mark_missing(
                    source_status,
                    missing_intervals,
                    "PUBLIC_TRADES",
                    need_start,
                    need_end,
                    trade_meta["trade_source_status"],
                )

        # OI
        oi_n = int(
            _q(
                "SELECT count() FROM orderbook_analysis.open_interest_5s "
                f"WHERE symbol='{symbol}' AND bucket_time>='{hs}' AND bucket_time<'{he}' FORMAT TSV"
            )
            or 0
        )
        lookback = (need_start - timedelta(seconds=OI_HARD_AGE_MS / 1000.0 + 5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        oi_max = _q(
            "SELECT max(source_event_time) FROM orderbook_analysis.open_interest_5s "
            f"WHERE symbol='{symbol}' AND bucket_time>='{lookback}' AND bucket_time<'{he}' FORMAT TSV"
        )
        oi_ok = oi_n > 0 and bool(oi_max) and not str(oi_max).startswith("1970")
        if needs_full_hour:
            expected = max(1, int((need_end - need_start).total_seconds() / 5) - 30)
            oi_ok = oi_ok and oi_n >= min(expected, 100) and bool(ev.oi_valid)
        if not oi_ok:
            _mark_missing(
                source_status,
                missing_intervals,
                "OPEN_INTEREST",
                need_start,
                need_end,
                f"oi_count={oi_n} oi_valid={ev.oi_valid} hard_age_ms={OI_HARD_AGE_MS}",
                status=STATUS_MISSING if oi_n == 0 else STATUS_STALE,
            )

        # Liquidations: zero events OK if source active
        liq_meta = best["liq_meta"]
        if not liq_meta["liq_source_ok"]:
            _mark_missing(
                source_status,
                missing_intervals,
                "LIQUIDATIONS",
                need_start,
                need_end,
                liq_meta["liq_source_status"],
            )

        hour_details.append(
            {
                "hour": key,
                "need_start": format_utc_z(need_start),
                "need_end": format_utc_z(need_end),
                "completion_status": ev.completion_status,
                "full_join": ev.full_join,
                "segment_path": best["segment_path"],
                "trades_in_need": pt_n,
                "oi_in_need": oi_n,
                "wall_ok": wall_ok,
                "replay_ok": replay_ok,
                "checkpoint_ok": checkpoint_ok,
            }
        )

    missing_intervals = _coalesce_missing(missing_intervals)
    complete = all(source_status[k] == STATUS_COMPLETE for k in SOURCE_KEYS)
    verdict = "DATA_COMPLETE" if complete else "DATA_NOT_COMPLETE"

    return {
        "symbol": symbol,
        "start": format_utc_z(start),
        "end": format_utc_z(end),
        "checked_at": format_utc_z(now),
        "source_status": source_status,
        "missing_intervals": missing_intervals,
        "checkpoint_identity": checkpoint_ids[:8],
        "replay_epoch": replay_epochs,
        "gap_count": gap_count,
        "resync_count": resync_count,
        "verdict": verdict,
        "hour_details": hour_details,
        "analysis_started": False,
    }


def _coalesce_missing(items: list[dict[str, str]]) -> list[dict[str, str]]:
    if not items:
        return []
    by: dict[str, list[dict[str, str]]] = {}
    for it in items:
        by.setdefault(it["source"], []).append(it)
    out: list[dict[str, str]] = []
    for source, group in by.items():
        group = sorted(group, key=lambda x: x["start"])
        cur = dict(group[0])
        for nxt in group[1:]:
            if nxt["start"] <= cur["end"]:
                if nxt["end"] > cur["end"]:
                    cur["end"] = nxt["end"]
            else:
                out.append(cur)
                cur = dict(nxt)
        out.append(cur)
    return out
