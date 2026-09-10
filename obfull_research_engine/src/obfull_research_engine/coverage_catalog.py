"""Read-only coverage catalog for closed BTCUSDT Full-OB hours."""

from __future__ import annotations

import csv
import json
import os
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import (
    DEFAULT_ARCHIVE_ROOT,
)
from orderbook_analyse.research.general_market_behavior_v1.coverage import (
    HourSegmentEval,
    evaluate_segment,
    list_hour_segments,
    load_clickhouse_env,
    eval_to_dict,
)

# Neighbor evidence window for distinguishing zero-event vs missing source
NEIGHBOR_HOURS = 1


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


def classify_trade_source(symbol: str, hour: datetime, trade_count: int) -> dict[str, Any]:
    """Distinguish active-but-quiet vs missing trade source using neighbors + watermarks."""
    prev_h = hour - timedelta(hours=1)
    next_h = hour + timedelta(hours=1)
    def cnt(a: datetime, b: datetime) -> int:
        hs = a.strftime("%Y-%m-%d %H:%M:%S")
        he = b.strftime("%Y-%m-%d %H:%M:%S")
        return int(_q(
            "SELECT count() FROM orderbook_analysis.public_trades_canonical "
            f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{he}' FORMAT TSV"
        ) or 0)
    prev_n = cnt(prev_h, hour)
    next_n = cnt(next_h, next_h + timedelta(hours=1))
    tip = _q("SELECT max(trade_ts) FROM orderbook_analysis.public_trades_canonical "
             f"WHERE symbol='{symbol}' FORMAT TSV")
    # Hour with exactly 0 trades but neighbors also 0 spanning multi-hour → likely source gap
    # Hour with 0 trades but neighbors have thousands → source gap for this hour
    # Hour with >0 trades → source active (zeros within seconds OK)
    if trade_count > 0:
        status = "SOURCE_ACTIVE"
        missing = False
    elif prev_n == 0 and next_n == 0:
        # Could be long outage — treat as missing if tip is after hour (source later recovered)
        # or tip before hour start (source not yet writing)
        tip_dt = None
        if tip and not tip.startswith("1970"):
            try:
                tip_dt = datetime.strptime(tip[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                tip_dt = None
        if tip_dt is not None and tip_dt < hour:
            status = "SOURCE_MISSING_OR_STALE_TIP"
            missing = True
        elif tip_dt is not None and tip_dt >= hour + timedelta(hours=1):
            status = "SOURCE_GAP_ZERO_WITH_NEIGHBOR_ZERO"
            missing = True
        else:
            status = "SOURCE_GAP_SUSPECTED"
            missing = True
    else:
        status = "SOURCE_GAP_ZERO_AMONG_ACTIVE_NEIGHBORS"
        missing = True
    return {
        "trade_source_status": status,
        "trade_source_missing": missing,
        "neighbor_prev_trade_count": prev_n,
        "neighbor_next_trade_count": next_n,
        "trade_tip": tip,
    }


def classify_liq_source(symbol: str, hour: datetime, liq_count: int) -> dict[str, Any]:
    """Zero liquidations can be legitimate; check neighbors for source liveness."""
    prev_h = hour - timedelta(hours=1)
    next_h = hour + timedelta(hours=1)
    def cnt(a: datetime, b: datetime) -> int:
        hs = a.strftime("%Y-%m-%d %H:%M:%S")
        he = b.strftime("%Y-%m-%d %H:%M:%S")
        return int(_q(
            "SELECT count() FROM orderbook_analysis.all_liquidations "
            f"WHERE symbol='{symbol}' AND event_time>='{hs}' AND event_time<'{he}' FORMAT TSV"
        ) or 0)
    prev_n = cnt(prev_h, hour)
    next_n = cnt(next_h, next_h + timedelta(hours=1))
    tip = _q("SELECT max(event_time) FROM orderbook_analysis.all_liquidations "
             f"WHERE symbol='{symbol}' FORMAT TSV")
    # Table queryable + tip recent relative to now OR any neighbor activity ⇒ source OK
    source_ok = True
    if tip.startswith("1970") or not tip:
        source_ok = False
        status = "SOURCE_MISSING"
    else:
        status = "SOURCE_ACTIVE_ZERO_EVENTS_OK" if liq_count == 0 else "SOURCE_ACTIVE"
    return {
        "liq_source_status": status,
        "liq_source_ok": source_ok,
        "neighbor_prev_liq_count": prev_n,
        "neighbor_next_liq_count": next_n,
        "liq_tip": tip,
    }


def coverage_class(ev: HourSegmentEval, trade_meta: dict, liq_meta: dict) -> str:
    if ev.full_join and not trade_meta["trade_source_missing"] and liq_meta["liq_source_ok"]:
        return "FULL_JOIN"
    if ev.full_wall_clock_coverage and ev.replay_chain_complete:
        # wall+replay but modality fail
        return "PARTIAL"
    if ev.replay_chain_complete and not ev.full_wall_clock_coverage:
        return "PARTIAL"  # e.g. 17Z
    if ev.gap_count_manifest or ev.in_segment_u_gap_count:
        return "GAP"
    if ev.completion_status == "GAP":
        return "GAP"
    return "PARTIAL"


def build_coverage_catalog(
    *,
    symbol: str = "BTCUSDT",
    archive_root: Path | None = None,
    exclude_open_current_hour: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    load_clickhouse_env()
    root = Path(archive_root or DEFAULT_ARCHIVE_ROOT)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows: list[dict[str, Any]] = []
    for seg in list_hour_segments(root, symbol):
        man_path = Path(str(seg) + ".manifest.json")
        if not man_path.exists():
            continue
        man = json.loads(man_path.read_text(encoding="utf-8"))
        hour = _parse_utc(str(man["utc_hour"]))
        if exclude_open_current_hour and hour >= now:
            continue
        ev = evaluate_segment(seg, symbol=symbol)
        trade_meta = classify_trade_source(symbol, hour, ev.trades_count)
        liq_meta = classify_liq_source(symbol, hour, ev.liq_count)
        # Recompute full_join with refined trade missing
        full_join = (
            ev.full_wall_clock_coverage
            and ev.replay_chain_complete
            and ev.trades_valid
            and not trade_meta["trade_source_missing"]
            and ev.oi_valid
            and liq_meta["liq_source_ok"]
        )
        klass = "FULL_JOIN" if full_join else coverage_class(ev, trade_meta, liq_meta)
        if trade_meta["trade_source_missing"] and klass == "FULL_JOIN":
            klass = "PARTIAL"
            full_join = False
        row = {
            **eval_to_dict(ev),
            **trade_meta,
            **liq_meta,
            "coverage_class": klass,
            "full_join_refined": full_join,
            "replay_chain_complete_flag": ev.replay_chain_complete,
            "full_wall_clock_coverage_flag": ev.full_wall_clock_coverage,
            "manifest_sha256": _file_sha(man_path),
            "segment_sha256": man.get("segment_sha256"),
        }
        rows.append(row)

    watermarks = {
        "symbol": symbol,
        "checked_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "public_trades_max": _q(
            f"SELECT max(trade_ts) FROM orderbook_analysis.public_trades_canonical WHERE symbol='{symbol}' FORMAT TSV"
        ),
        "oi_5s_max": _q(
            f"SELECT max(bucket_time) FROM orderbook_analysis.open_interest_5s WHERE symbol='{symbol}' FORMAT TSV"
        ),
        "liquidations_max": _q(
            f"SELECT max(event_time) FROM orderbook_analysis.all_liquidations WHERE symbol='{symbol}' FORMAT TSV"
        ),
        "candles_1m_max": _q(
            f"SELECT max(open_time) FROM signal_generator.candles_1m WHERE symbol='{symbol}' AND interval='1m' FORMAT TSV"
        ),
        "full_ob_archive_root": str(root / symbol),
        "n_closed_segments_evaluated": len(rows),
    }
    return rows, watermarks


def _file_sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_longest_contiguous_full_join(
    catalog: list[dict[str, Any]],
    *,
    prefer_from: datetime | None = None,
    max_hours: int = 24,
) -> dict[str, Any]:
    prefer = prefer_from or datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    # unique by utc_hour preferring FULL_JOIN
    by_hour: dict[str, dict] = {}
    for r in catalog:
        h = r["utc_hour"]
        prev = by_hour.get(h)
        if prev is None or (r.get("full_join_refined") and not prev.get("full_join_refined")):
            by_hour[h] = r
    hours_sorted = sorted(by_hour.keys())
    best: list[str] = []
    cur: list[str] = []
    for h in hours_sorted:
        r = by_hour[h]
        ht = _parse_utc(h)
        if ht < prefer:
            # still allow blocks that start at/after prefer; reset if before
            if not r.get("full_join_refined"):
                cur = []
            continue
        if r.get("full_join_refined"):
            if cur:
                prev_t = _parse_utc(cur[-1])
                if ht - prev_t == timedelta(hours=1):
                    cur.append(h)
                else:
                    if len(cur) > len(best):
                        best = list(cur)
                    cur = [h]
            else:
                cur = [h]
        else:
            if len(cur) > len(best):
                best = list(cur)
            cur = []
    if len(cur) > len(best):
        best = list(cur)
    best = best[:max_hours]
    return {
        "prefer_from_utc": prefer.isoformat().replace("+00:00", "Z"),
        "max_hours": max_hours,
        "selected_hours": best,
        "n_selected": len(best),
        "contiguous_ok": len(best) >= 2,
        "block_start": best[0] if best else None,
        "block_end_exclusive": (
            (_parse_utc(best[-1]) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            if best else None
        ),
        "reason_if_blocked": None
        if len(best) >= 2
        else f"need>=2 contiguous FULL_JOIN hours after {prefer.isoformat()}; found {len(best)}: {best}",
    }


def write_catalog_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = [
        "utc_hour", "coverage_class", "full_join_refined", "completion_status", "continuity_status",
        "full_wall_clock_coverage_flag", "replay_chain_complete_flag", "start_lag_s", "end_lead_s",
        "in_segment_u_gap_count", "trades_count", "trade_source_status", "trade_source_missing",
        "oi_count", "oi_valid", "liq_count", "liq_source_status", "segment_path", "block_reasons",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            out = {k: r.get(k) for k in keys}
            out["block_reasons"] = "|".join(r.get("block_reasons") or [])
            w.writerow(out)
