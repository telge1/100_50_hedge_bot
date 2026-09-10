"""Read-only coverage for the bounded pilot. Fail closed. No substitute source."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..analyze.precheck import check_future_public_trade_coverage
from ..interval_coverage import SOURCE_KEYS, _q, check_interval_coverage
from ..localized_coverage.check import check_localized_coverage
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    DEFAULT_END_Z,
    DEFAULT_OUTCOME_END_Z,
    DEFAULT_START_Z,
    FROZEN_PHASE2_DIR,
    LLD_SOURCE_TIMEFRAME,
    PHASE2_RUN_KEY,
)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _count(sql: str) -> int:
    raw = (_q(sql) or "").strip()
    if not raw:
        return 0
    return int(float(raw.split()[0]))


def _tip(sql: str) -> str | None:
    raw = (_q(sql) or "").strip()
    if not raw or raw.startswith("1970"):
        return None
    return raw.replace(" ", "T") + ("Z" if not raw.endswith("Z") and "+" not in raw else "")


def existing_analysis_runs(symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    root = ENGINE_ROOT / "results" / "analysis_runs_v1" / symbol.upper()
    hits: list[dict[str, Any]] = []
    if not root.exists():
        return hits
    for manifest in root.glob("*/rk_*/run_manifest.json"):
        try:
            import json

            obj = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        cfg = obj.get("config") or obj
        s = cfg.get("start") or obj.get("start")
        e = cfg.get("end") or obj.get("end")
        if not s or not e:
            # window id in parent name
            win = manifest.parents[1].name
            if "__" in win:
                s, e = win.split("__", 1)
                s = s.replace("Z", ":00Z") if "T" in s else s
        hits.append(
            {
                "path": str(manifest.parent),
                "window_id": manifest.parents[1].name,
                "run_key": manifest.parent.name,
                "start": s,
                "end": e,
                "status": obj.get("status"),
            }
        )
    return hits


def check_pilot_coverage(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    outcome_end: datetime,
) -> dict[str, Any]:
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    outcome_end = outcome_end.astimezone(timezone.utc)

    interval = check_interval_coverage(symbol=symbol, start=start, end=outcome_end)
    localized = check_localized_coverage(symbol=symbol, start=start, end=end)
    future_pt = check_future_public_trade_coverage(
        symbol=symbol,
        feature_end=end,
        horizon_seconds=int((outcome_end - end).total_seconds()),
    )

    n_trades = _count(
        "SELECT count() FROM orderbook_analysis.public_trades_canonical "
        f"WHERE symbol='{symbol}' AND trade_ts>='{start:%Y-%m-%d %H:%M:%S}' "
        f"AND trade_ts<'{outcome_end:%Y-%m-%d %H:%M:%S}' FORMAT TSV"
    )
    n_1m = _count(
        "SELECT count() FROM signal_generator.candles_1m "
        f"WHERE symbol='{symbol}' AND interval='1m' "
        f"AND open_time>='{start:%Y-%m-%d %H:%M:%S}' "
        f"AND open_time<'{outcome_end:%Y-%m-%d %H:%M:%S}' FORMAT TSV"
    )
    n_15m = _count(
        "SELECT count() FROM signal_generator.candles_1m "
        f"WHERE symbol='{symbol}' AND interval='1m' "
        f"AND open_time>='{(start - timedelta(days=3)):%Y-%m-%d %H:%M:%S}' "
        f"AND open_time<'{end:%Y-%m-%d %H:%M:%S}' FORMAT TSV"
    )
    n_oi = _count(
        "SELECT count() FROM orderbook_analysis.open_interest_5s "
        f"WHERE symbol='{symbol}' AND bucket_time>='{(start - timedelta(seconds=1800)):%Y-%m-%d %H:%M:%S}' "
        f"AND bucket_time<'{end:%Y-%m-%d %H:%M:%S}' FORMAT TSV"
    )

    phase2 = ENGINE_ROOT / FROZEN_PHASE2_DIR
    phase2_ok = (phase2 / "manifest.json").is_file() and (phase2 / "market_profile_events.jsonl").is_file()

    expected_1m = int((outcome_end - start).total_seconds() // 60)
    missing: list[dict[str, Any]] = list(interval.get("missing_intervals") or [])
    modalities: dict[str, Any] = {
        "PUBLIC_TRADES": {
            "ok": n_trades > 0 and bool(future_pt.get("ok")),
            "n": n_trades,
            "future": future_pt,
        },
        "CANDLES_1M": {"ok": n_1m >= max(expected_1m - 1, 1), "n": n_1m, "expected": expected_1m},
        "CANDLES_FOR_LLD_15M": {"ok": n_15m > 100, "n": n_15m, "source_timeframe": LLD_SOURCE_TIMEFRAME},
        "OPEN_INTEREST": {"ok": n_oi > 0, "n": n_oi},
        "MARKET_PROFILE_PHASE2": {"ok": phase2_ok, "path": str(phase2), "run_key": PHASE2_RUN_KEY},
        "FULL_OB": {
            "interval_verdict": (interval.get("source_status") or {}).get("FULL_OB"),
            "localized_verdict": localized.get("verdict"),
            "usable_spans": localized.get("usable_spans"),
            "excluded_spans": localized.get("excluded_spans"),
        },
    }

    if not modalities["PUBLIC_TRADES"]["ok"]:
        missing.append({"source": "PUBLIC_TRADES", "start": format_utc_z(start), "end": format_utc_z(outcome_end)})
    if not modalities["CANDLES_1M"]["ok"]:
        missing.append({"source": "CANDLES_1M", "start": format_utc_z(start), "end": format_utc_z(outcome_end), "n": n_1m})
    if not modalities["CANDLES_FOR_LLD_15M"]["ok"]:
        missing.append({"source": "CANDLES_FOR_LLD", "detail": "insufficient 1m history for 15m LLD lookback"})
    if not modalities["OPEN_INTEREST"]["ok"]:
        missing.append({"source": "OPEN_INTEREST", "start": format_utc_z(start), "end": format_utc_z(end)})
    if not phase2_ok:
        missing.append({"source": "PHASE2_MATERIALIZATION", "path": str(phase2)})

    fo_status = (interval.get("source_status") or {}).get("FULL_OB")
    usable = localized.get("usable_spans") or []
    if fo_status not in {"COMPLETE"} and not usable:
        missing.append(
            {
                "source": "FULL_OB",
                "start": format_utc_z(start),
                "end": format_utc_z(end),
                "interval_status": fo_status,
                "localized_verdict": localized.get("verdict"),
            }
        )

    required_ok = all(
        modalities[k]["ok"]
        for k in ("PUBLIC_TRADES", "CANDLES_1M", "CANDLES_FOR_LLD_15M", "OPEN_INTEREST", "MARKET_PROFILE_PHASE2")
    ) and (fo_status == "COMPLETE" or bool(usable))

    analysis_runs = existing_analysis_runs(symbol, start, end)
    shards = []
    for run in analysis_runs:
        p = Path(run["path"])
        if (p / "candidates" / "episode_candidates_v1.parquet").exists():
            shards.append({**run, "has_candidates": True})

    verdict = "COVERAGE_COMPLETE" if required_ok else "PILOT_COVERAGE_NOT_COMPLETE"
    return {
        "schema": "bounded_level_first_coverage_v1",
        "symbol": symbol,
        "start": format_utc_z(start),
        "end": format_utc_z(end),
        "outcome_end": format_utc_z(outcome_end),
        "ok": required_ok,
        "verdict": verdict,
        "modalities": modalities,
        "missing_intervals": missing,
        "interval_coverage": {
            "verdict": interval.get("verdict"),
            "source_status": interval.get("source_status"),
        },
        "localized_coverage": {
            "verdict": localized.get("verdict"),
            "usable_spans": localized.get("usable_spans"),
            "excluded_spans": localized.get("excluded_spans"),
        },
        "future_public_trades": future_pt,
        "existing_analysis_runs": analysis_runs,
        "reusable_ob_candidate_shards": shards,
        "clickhouse_writes": 0,
        "no_substitute_source": True,
        "defaults": {
            "start": DEFAULT_START_Z,
            "end": DEFAULT_END_Z,
            "outcome_end": DEFAULT_OUTCOME_END_Z,
        },
        "source_keys": list(SOURCE_KEYS),
        "trade_tip": _tip(f"SELECT max(trade_ts) FROM orderbook_analysis.public_trades_canonical WHERE symbol='{symbol}' FORMAT TSV"),
    }
