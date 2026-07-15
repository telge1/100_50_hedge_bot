"""Read-only Feather vs MySQL candle + scanner parity audit.

Does not write to MySQL, does not modify feathers, does not change scanner logic.
Scanner HTF remains aggregated from 5m for both sources.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.candle_sources import (
    FeatherCandleSource,
    MySQLCandleSource,
    load_regime_db_env_file,
)
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.htf_freqtrade_equality_audit import (
    ABS_TOL,
    REL_TOL,
    values_equal_exact,
    values_within_tol,
)
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.mysql_candle_store.hashing import candles_export_hash, json_hash
from research.regime_scanner.point_audit import build_point_audit, json_safe
from research.regime_scanner.timeframes import ensure_utc_timestamp, timeframe_timedelta
from research.regime_scanner.trend_state_machine import (
    default_trend_state_config,
    run_trend_state_timeline,
)

RESULTS_DIR = Path("research/regime_scanner/results_mysql_feather_parity")

DECISION_TIMES = (
    "2026-06-27T12:45:00+00:00",
    "2026-06-27T12:44:59+00:00",
    "2026-03-05T17:30:00+00:00",
    "2026-03-06T14:30:00+00:00",
)

WINDOWS = {
    "march_week": ("2026-03-01T00:00:00+00:00", "2026-03-08T00:00:00+00:00"),
    "long_audit": ("2026-01-06T00:00:00+00:00", "2026-03-16T00:00:00+00:00"),
    "full_5m": ("2025-12-27T00:00:00+00:00", "2026-06-27T12:45:00+00:00"),
}

WARMUP_START = "2025-12-27T00:00:00+00:00"
ANALYSIS_START = "2026-01-06T00:00:00+00:00"


@dataclass
class DiffRow:
    section: str
    key: str
    feather: Any
    mysql: Any
    detail: str = ""


@dataclass
class ParityReport:
    ok: bool = True
    exchange: str = "bybit"
    symbol: str = "APTUSDT"
    candle_parity: dict[str, Any] = field(default_factory=dict)
    decision_time_parity: dict[str, Any] = field(default_factory=dict)
    warmup_parity: dict[str, Any] = field(default_factory=dict)
    scanner_parity: dict[str, Any] = field(default_factory=dict)
    hashes: dict[str, Any] = field(default_factory=dict)
    state_isolation: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    mysql_write_guard: dict[str, Any] = field(default_factory=dict)
    differences: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _frame_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"rows": 0, "start": None, "end": None, "export_hash": candles_export_hash(frame)}
    return {
        "rows": int(len(frame)),
        "start": ensure_utc_timestamp(frame["timestamp"].iloc[0]).isoformat(),
        "end": ensure_utc_timestamp(frame["timestamp"].iloc[-1]).isoformat(),
        "export_hash": candles_export_hash(frame),
        "dtypes": {c: str(frame[c].dtype) for c in frame.columns},
        "tz": str(getattr(frame["timestamp"].dtype, "tz", None)),
        "duplicates": int(frame["timestamp"].duplicated().sum()),
        "sorted": bool(frame["timestamp"].is_monotonic_increasing),
    }


def compare_ohlcv_frames(
    feather: pd.DataFrame,
    mysql: pd.DataFrame,
    *,
    section: str,
) -> tuple[dict[str, Any], list[DiffRow]]:
    diffs: list[DiffRow] = []
    result: dict[str, Any] = {
        "feather": _frame_summary(feather),
        "mysql": _frame_summary(mysql),
        "ok": True,
    }
    if len(feather) != len(mysql):
        diffs.append(DiffRow(section, "rows", len(feather), len(mysql)))
        result["ok"] = False
        return result, diffs

    if feather.empty:
        return result, diffs

    f = feather.reset_index(drop=True)
    m = mysql.reset_index(drop=True)
    first_diff: dict[str, Any] | None = None

    ts_f = pd.to_datetime(f["timestamp"], utc=True)
    ts_m = pd.to_datetime(m["timestamp"], utc=True)
    ts_mismatch = int((ts_f != ts_m).sum())
    ohlc_mismatch = ts_mismatch
    if ts_mismatch and first_diff is None:
        i = int((ts_f != ts_m).to_numpy().argmax())
        first_diff = {
            "index": i,
            "field": "timestamp",
            "feather": ensure_utc_timestamp(ts_f.iloc[i]).isoformat(),
            "mysql": ensure_utc_timestamp(ts_m.iloc[i]).isoformat(),
        }

    for col in ("open", "high", "low", "close"):
        a = f[col].astype("float64").to_numpy()
        b = m[col].astype("float64").to_numpy()
        bad = a != b
        n_bad = int(bad.sum())
        ohlc_mismatch += n_bad
        if n_bad and first_diff is None:
            i = int(bad.argmax())
            first_diff = {
                "index": i,
                "field": col,
                "timestamp": ensure_utc_timestamp(ts_f.iloc[i]).isoformat(),
                "feather": float(a[i]),
                "mysql": float(b[i]),
            }

    va = f["volume"].astype("float64").to_numpy()
    vb = m["volume"].astype("float64").to_numpy()
    exact_mask = va == vb
    volume_exact = int(exact_mask.sum())
    volume_within = volume_exact
    volume_outside = 0
    for i in range(len(va)):
        if exact_mask[i]:
            continue
        if values_within_tol(float(va[i]), float(vb[i]), abs_tol=ABS_TOL, rel_tol=REL_TOL):
            volume_within += 1
        else:
            volume_outside += 1
            if first_diff is None:
                first_diff = {
                    "index": i,
                    "field": "volume",
                    "timestamp": ensure_utc_timestamp(ts_f.iloc[i]).isoformat(),
                    "feather": float(va[i]),
                    "mysql": float(vb[i]),
                }

    if "close_time" in f.columns and "close_time" in m.columns:
        ct_bad = pd.to_datetime(f["close_time"], utc=True) != pd.to_datetime(m["close_time"], utc=True)
        n_ct = int(ct_bad.sum())
        ohlc_mismatch += n_ct
        if n_ct and first_diff is None:
            i = int(ct_bad.to_numpy().argmax())
            first_diff = {"index": i, "field": "close_time"}

    result["ohlc_mismatches"] = ohlc_mismatch
    result["volume_exact"] = volume_exact
    result["volume_within_tolerance"] = volume_within
    result["volume_outside_tolerance"] = volume_outside
    result["first_diff"] = first_diff
    result["hash_match"] = result["feather"]["export_hash"] == result["mysql"]["export_hash"]
    result["ok"] = ohlc_mismatch == 0 and volume_outside == 0 and result["hash_match"]
    if not result["ok"]:
        diffs.append(
            DiffRow(
                section,
                "ohlcv",
                result["feather"]["export_hash"],
                result["mysql"]["export_hash"],
                detail=json.dumps(first_diff, default=str),
            )
        )
    return result, diffs


def _count_market_candles() -> int:
    load_regime_db_env_file()
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
    from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore

    store = MySQLCandleStore(load_regime_db_config())
    try:
        return int(store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe="5m")) + int(
            store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe="15m")
        ) + int(store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe="30m"))
    finally:
        store.close()


def _count_validation_runs() -> int:
    load_regime_db_env_file()
    import pymysql
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config

    cfg = load_regime_db_config()
    conn = pymysql.connect(
        host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password, database=cfg.name
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM data_validation_runs")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def audit_candle_level(
    *,
    exchange: str,
    symbol: str,
    timeframes: list[str],
    out_dir: Path,
) -> tuple[dict[str, Any], list[DiffRow]]:
    feather_src = FeatherCandleSource()
    mysql_src = MySQLCandleSource()
    diffs: list[DiffRow] = []
    payload: dict[str, Any] = {}
    try:
        for tf in timeframes:
            f = feather_src.load_candles(exchange=exchange, symbol=symbol, timeframe=tf)
            m = mysql_src.load_candles(exchange=exchange, symbol=symbol, timeframe=tf)
            f.to_csv(out_dir / f"candles_{tf}_feather.csv", index=False)
            m.to_csv(out_dir / f"candles_{tf}_mysql.csv", index=False)
            cmp, d = compare_ohlcv_frames(f, m, section=f"candles_{tf}")
            payload[tf] = cmp
            diffs.extend(d)
    finally:
        feather_src.close()
        mysql_src.close()
    payload["ok"] = all(v.get("ok") for v in payload.values())
    return payload, diffs


def audit_decision_times(
    *,
    exchange: str,
    symbol: str,
    timeframes: list[str],
) -> tuple[dict[str, Any], list[DiffRow]]:
    feather_src = FeatherCandleSource()
    mysql_src = MySQLCandleSource()
    diffs: list[DiffRow] = []
    payload: dict[str, Any] = {}
    try:
        for decision in DECISION_TIMES:
            dec = ensure_utc_timestamp(decision)
            per_tf: dict[str, Any] = {}
            for tf in timeframes:
                f = feather_src.load_candles(
                    exchange=exchange, symbol=symbol, timeframe=tf, decision_time=dec
                )
                m = mysql_src.load_candles(
                    exchange=exchange, symbol=symbol, timeframe=tf, decision_time=dec
                )
                if not f.empty:
                    assert (f["close_time"] <= dec).all()
                if not m.empty:
                    assert (m["close_time"] <= dec).all()
                cmp, d = compare_ohlcv_frames(f, m, section=f"decision_{tf}_{decision}")
                # boundary checks for 5m
                if tf == "5m" and decision.endswith("12:45:00+00:00"):
                    if not f.empty:
                        last = ensure_utc_timestamp(f["timestamp"].iloc[-1])
                        if last != ensure_utc_timestamp("2026-06-27T12:40:00+00:00"):
                            diffs.append(
                                DiffRow(
                                    "decision_boundary",
                                    decision,
                                    last.isoformat(),
                                    "2026-06-27T12:40:00+00:00",
                                )
                            )
                            cmp["ok"] = False
                if tf == "5m" and decision.endswith("12:44:59+00:00"):
                    if not f.empty:
                        last = ensure_utc_timestamp(f["timestamp"].iloc[-1])
                        if last != ensure_utc_timestamp("2026-06-27T12:35:00+00:00"):
                            diffs.append(
                                DiffRow(
                                    "decision_boundary",
                                    decision,
                                    last.isoformat(),
                                    "2026-06-27T12:35:00+00:00",
                                )
                            )
                            cmp["ok"] = False
                per_tf[tf] = cmp
                diffs.extend(d)
            payload[decision] = per_tf
    finally:
        feather_src.close()
        mysql_src.close()
    payload["ok"] = all(
        all(v.get("ok", False) for v in per.values()) for per in payload.values() if isinstance(per, dict)
    )
    return payload, diffs


def audit_warmup(
    *,
    exchange: str,
    symbol: str,
) -> tuple[dict[str, Any], list[DiffRow]]:
    feather_src = FeatherCandleSource()
    mysql_src = MySQLCandleSource()
    diffs: list[DiffRow] = []
    try:
        start = ensure_utc_timestamp(WARMUP_START)
        end = ensure_utc_timestamp(ANALYSIS_START) - timeframe_timedelta("5m")
        f = feather_src.load_candles(
            exchange=exchange, symbol=symbol, timeframe="5m", start_time=start, end_time=end
        )
        m = mysql_src.load_candles(
            exchange=exchange, symbol=symbol, timeframe="5m", start_time=start, end_time=end
        )
        cmp, d = compare_ohlcv_frames(f, m, section="warmup_5m")
        diffs.extend(d)
        # March warmup into analysis
        march_start = ensure_utc_timestamp("2026-03-01T00:00:00+00:00")
        f2 = feather_src.load_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe="5m",
            start_time=start,
            end_time=march_start - timeframe_timedelta("5m"),
        )
        m2 = mysql_src.load_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe="5m",
            start_time=start,
            end_time=march_start - timeframe_timedelta("5m"),
        )
        cmp2, d2 = compare_ohlcv_frames(f2, m2, section="warmup_to_march")
        diffs.extend(d2)
    finally:
        feather_src.close()
        mysql_src.close()
    return {"to_analysis_start": cmp, "to_march": cmp2, "ok": cmp["ok"] and cmp2["ok"]}, diffs


def _stable_point_audit(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip path/runtime-only fields before hashing."""
    drop = {"source_feather", "elapsed_ms", "runtime_ms"}
    return {k: v for k, v in json_safe(payload).items() if k not in drop}


def _snapshots_to_rows(snaps: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for s in snaps:
        d = s.to_dict() if hasattr(s, "to_dict") else dict(s)
        # Keep deterministic state fields; drop non-deterministic if any
        rows.append(json_safe(d))
    return rows


def _events_to_rows(events: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for e in events:
        if hasattr(e, "__dict__"):
            rows.append(json_safe(dict(vars(e))))
        elif isinstance(e, dict):
            rows.append(json_safe(e))
        else:
            rows.append(json_safe({"repr": str(e)}))
    return rows


def run_scanner_window_parity(
    *,
    symbol: str,
    start: str,
    end: str,
    label: str,
    out_dir: Path,
    sample_decisions: int = 24,
    include_trend_timeline: bool = True,
) -> tuple[dict[str, Any], list[DiffRow]]:
    diffs: list[DiffRow] = []
    t0 = time.perf_counter()
    rss0 = _rss_mb()
    feather_5m = load_symbol_candles(symbol, data_source="feather")
    t_feather_load = time.perf_counter() - t0
    rss1 = _rss_mb()

    t1 = time.perf_counter()
    mysql_5m = load_symbol_candles(symbol, data_source="mysql")
    t_mysql_load = time.perf_counter() - t1
    rss2 = _rss_mb()

    cmp_input, d_input = compare_ohlcv_frames(feather_5m, mysql_5m, section=f"{label}_input_5m")
    diffs.extend(d_input)

    start_ts = ensure_utc_timestamp(start)
    end_ts = ensure_utc_timestamp(end)

    mask = (feather_5m["timestamp"] >= start_ts) & (feather_5m["timestamp"] < end_ts)
    opens = feather_5m.loc[mask, "timestamp"].tolist()
    if not opens:
        return {"ok": False, "error": "no candles in window"}, [
            DiffRow(label, "window", start, end, "empty")
        ]
    # Skip leading opens that cannot form a closed 30m bucket yet.
    min_open_for_htf = ensure_utc_timestamp(opens[0]) + timeframe_timedelta("30m")
    eligible = [ts for ts in opens if ensure_utc_timestamp(ts) >= min_open_for_htf]
    if not eligible:
        eligible = opens
    step = max(1, len(eligible) // sample_decisions)
    sample_opens = eligible[::step][:sample_decisions]
    if eligible[0] not in sample_opens:
        sample_opens = [eligible[0]] + sample_opens
    if eligible[-1] not in sample_opens:
        sample_opens = sample_opens + [eligible[-1]]

    feather_points = []
    mysql_points = []
    t2 = time.perf_counter()
    for open_ts in sample_opens:
        decision = ensure_utc_timestamp(open_ts) + timeframe_timedelta("5m")
        if decision > end_ts:
            continue
        pf = build_point_audit(
            symbol=symbol,
            decision_time=decision,
            candles=feather_5m,
            timeframes="5m,15m,30m",
        )
        pm = build_point_audit(
            symbol=symbol,
            decision_time=decision,
            candles=mysql_5m,
            timeframes="5m,15m,30m",
        )
        feather_points.append(_stable_point_audit(pf))
        mysql_points.append(_stable_point_audit(pm))
    t_points = time.perf_counter() - t2

    points_equal = feather_points == mysql_points
    if not points_equal:
        for i, (a, b) in enumerate(zip(feather_points, mysql_points)):
            if a != b:
                diffs.append(
                    DiffRow(
                        f"{label}_point_audit",
                        f"sample_{i}",
                        a.get("decision_time"),
                        b.get("decision_time"),
                        detail="first mismatched point audit",
                    )
                )
                break

    rows_f: list[dict[str, Any]] = []
    rows_m: list[dict[str, Any]] = []
    ev_f: list[dict[str, Any]] = []
    ev_m: list[dict[str, Any]] = []
    t_trend_f = 0.0
    t_trend_m = 0.0
    trend_equal = True
    events_equal = True

    if include_trend_timeline:
        scfg = default_regime_scanner_config_5m()
        # Keep enough history for indicator/structure warm-up, not the entire archive.
        warm_bars = int(getattr(scfg, "min_warmup_candles", 400) or 400) + 50
        warm_start = start_ts - pd.Timedelta(minutes=5 * warm_bars)
        archive_start = ensure_utc_timestamp(WARMUP_START)
        if warm_start < archive_start:
            warm_start = archive_start

        def _prep(frame: pd.DataFrame) -> pd.DataFrame:
            use = frame.loc[
                (frame["timestamp"] >= warm_start) & (frame["timestamp"] < end_ts)
            ].copy()
            return compute_indicator_frame(use, config=scfg)

        t3 = time.perf_counter()
        f_ind = _prep(feather_5m)
        snaps_f, _, events_f = run_trend_state_timeline(
            f_ind,
            start_decision_time=start_ts,
            end_decision_time=end_ts,
        )
        t_trend_f = time.perf_counter() - t3

        t4 = time.perf_counter()
        m_ind = _prep(mysql_5m)
        snaps_m, _, events_m = run_trend_state_timeline(
            m_ind,
            start_decision_time=start_ts,
            end_decision_time=end_ts,
        )
        t_trend_m = time.perf_counter() - t4

        rows_f = _snapshots_to_rows(snaps_f)
        rows_m = _snapshots_to_rows(snaps_m)
        ev_f = _events_to_rows(events_f)
        ev_m = _events_to_rows(events_m)

        pd.DataFrame(rows_f).to_csv(out_dir / f"trend_states_{label}_feather.csv", index=False)
        pd.DataFrame(rows_m).to_csv(out_dir / f"trend_states_{label}_mysql.csv", index=False)
        pd.DataFrame(ev_f).to_csv(out_dir / f"structure_events_{label}_feather.csv", index=False)
        pd.DataFrame(ev_m).to_csv(out_dir / f"structure_events_{label}_mysql.csv", index=False)

        trend_equal = rows_f == rows_m
        events_equal = ev_f == ev_m
        if not trend_equal:
            for i, (a, b) in enumerate(zip(rows_f, rows_m)):
                if a != b:
                    diffs.append(
                        DiffRow(
                            f"{label}_trend_state",
                            f"index_{i}",
                            a.get("state") or a.get("trend_state"),
                            b.get("state") or b.get("trend_state"),
                            detail=json.dumps(
                                {
                                    "feather_decision": a.get("decision_time"),
                                    "mysql_decision": b.get("decision_time"),
                                },
                                default=str,
                            ),
                        )
                    )
                    break
        if not events_equal:
            diffs.append(
                DiffRow(
                    f"{label}_structure_events",
                    "count_or_content",
                    len(ev_f),
                    len(ev_m),
                )
            )

    with (out_dir / f"scanner_outputs_{label}_feather.jsonl").open("w", encoding="utf-8") as fh:
        for row in feather_points:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    with (out_dir / f"scanner_outputs_{label}_mysql.jsonl").open("w", encoding="utf-8") as fh:
        for row in mysql_points:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    hashes = {
        "input_5m_feather": cmp_input["feather"]["export_hash"],
        "input_5m_mysql": cmp_input["mysql"]["export_hash"],
        "point_audits_feather": json_hash(feather_points),
        "point_audits_mysql": json_hash(mysql_points),
        "trend_states_feather": json_hash(rows_f),
        "trend_states_mysql": json_hash(rows_m),
        "structure_events_feather": json_hash(ev_f),
        "structure_events_mysql": json_hash(ev_m),
        "trend_timeline_included": include_trend_timeline,
    }
    ok = cmp_input["ok"] and points_equal and trend_equal and events_equal
    return {
        "ok": ok,
        "window": {"start": start, "end": end},
        "sample_point_audits": len(feather_points),
        "trend_snapshots": len(rows_f),
        "structure_events": len(ev_f),
        "input_parity": cmp_input,
        "point_audits_equal": points_equal,
        "trend_states_equal": trend_equal,
        "structure_events_equal": events_equal,
        "hashes": hashes,
        "performance": {
            "feather_load_s": t_feather_load,
            "mysql_load_s": t_mysql_load,
            "point_audit_s": t_points,
            "trend_feather_s": t_trend_f,
            "trend_mysql_s": t_trend_m,
            "rss_mb_after_feather": rss1,
            "rss_mb_after_mysql": rss2,
            "rss_mb_start": rss0,
        },
    }, diffs


def default_regime_scanner_config_5m():
    from research.regime_scanner.config import default_regime_scanner_config

    return default_regime_scanner_config().with_timeframe("5m")


def audit_state_isolation(symbol: str) -> tuple[dict[str, Any], list[DiffRow]]:
    diffs: list[DiffRow] = []
    decision = "2026-03-05T17:30:00+00:00"
    orders = [
        ("feather", "mysql"),
        ("mysql", "feather"),
        ("feather", "feather"),
        ("mysql", "mysql"),
    ]
    results: dict[str, Any] = {}
    hashes: list[str] = []
    for a, b in orders:
        pa = build_point_audit(
            symbol=symbol,
            decision_time=decision,
            data_source=a,
            timeframes="5m,15m,30m",
        )
        pb = build_point_audit(
            symbol=symbol,
            decision_time=decision,
            data_source=b,
            timeframes="5m,15m,30m",
        )
        ha = json_hash(_stable_point_audit(pa))
        hb = json_hash(_stable_point_audit(pb))
        key = f"{a}->{b}"
        results[key] = {"hash_a": ha, "hash_b": hb, "equal": ha == hb}
        hashes.extend([ha, hb])
        if a == b and ha != hb:
            diffs.append(DiffRow("state_isolation", key, ha, hb, "same-source mismatch"))
        if {a, b} == {"feather", "mysql"} and ha != hb:
            diffs.append(DiffRow("state_isolation", key, ha, hb, "cross-source mismatch"))
    # All four terminal hashes for feather and mysql single runs must match within source
    feather_hashes = {results["feather->feather"]["hash_a"], results["feather->mysql"]["hash_a"]}
    mysql_hashes = {results["mysql->mysql"]["hash_a"], results["mysql->feather"]["hash_a"]}
    ok = (
        len(feather_hashes) == 1
        and len(mysql_hashes) == 1
        and feather_hashes == mysql_hashes
        and all(v["equal"] for v in results.values())
    )
    return {"ok": ok, "runs": results}, diffs


def run_parity_audit(
    *,
    exchange: str = "bybit",
    symbol: str = "APTUSDT",
    timeframes: list[str] | None = None,
    output_dir: Path | None = None,
    include_full_window: bool = True,
) -> ParityReport:
    load_regime_db_env_file()
    tfs = timeframes or ["5m", "15m", "30m"]
    out_dir = Path(output_dir or RESULTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = ParityReport(exchange=exchange, symbol=symbol)

    candles_before = _count_market_candles()
    validation_before = _count_validation_runs()

    print(f"[parity] candle-level {tfs}", flush=True)
    candle_payload, d1 = audit_candle_level(
        exchange=exchange, symbol=symbol, timeframes=tfs, out_dir=out_dir
    )
    report.candle_parity = candle_payload
    report.differences.extend(asdict(x) for x in d1)

    print("[parity] decision-time", flush=True)
    dec_payload, d2 = audit_decision_times(exchange=exchange, symbol=symbol, timeframes=tfs)
    report.decision_time_parity = dec_payload
    report.differences.extend(asdict(x) for x in d2)

    print("[parity] warmup", flush=True)
    warm_payload, d3 = audit_warmup(exchange=exchange, symbol=symbol)
    report.warmup_parity = warm_payload
    report.differences.extend(asdict(x) for x in d3)

    scanner: dict[str, Any] = {}
    for label, (start, end) in WINDOWS.items():
        if label == "full_5m" and not include_full_window:
            continue
        sample = 12 if label == "full_5m" else 24
        # Full trend/structure timeline only for march_week (deterministic E2E).
        # Longer windows: input parity + sampled point audits (same scanner path).
        include_trend = label == "march_week"
        print(f"[parity] scanner window {label} trend={include_trend}", flush=True)
        payload, d = run_scanner_window_parity(
            symbol=symbol,
            start=start,
            end=end,
            label=label,
            out_dir=out_dir,
            sample_decisions=sample,
            include_trend_timeline=include_trend,
        )
        scanner[label] = payload
        report.differences.extend(asdict(x) for x in d)
        report.hashes[label] = payload.get("hashes", {})
        if label == "march_week":
            report.performance = payload.get("performance", {})
    report.scanner_parity = scanner

    print("[parity] state isolation", flush=True)
    isolation, d4 = audit_state_isolation(symbol)
    report.state_isolation = isolation
    report.differences.extend(asdict(x) for x in d4)

    candles_after = _count_market_candles()
    validation_after = _count_validation_runs()
    report.mysql_write_guard = {
        "market_candles_before": candles_before,
        "market_candles_after": candles_after,
        "validation_runs_before": validation_before,
        "validation_runs_after": validation_after,
        "unchanged": candles_before == candles_after and validation_before == validation_after,
    }

    report.ok = (
        bool(report.candle_parity.get("ok"))
        and bool(report.decision_time_parity.get("ok"))
        and bool(report.warmup_parity.get("ok"))
        and all(v.get("ok") for v in report.scanner_parity.values())
        and bool(report.state_isolation.get("ok"))
        and bool(report.mysql_write_guard.get("unchanged"))
        and not report.differences
    )
    if report.differences:
        report.ok = False
        report.errors.append(f"{len(report.differences)} difference row(s)")

    (out_dir / "parity_summary.json").write_text(
        json.dumps(json_safe(report.to_dict()), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if report.differences:
        pd.DataFrame(report.differences).to_csv(out_dir / "parity_differences.csv", index=False)
    else:
        pd.DataFrame(columns=["section", "key", "feather", "mysql", "detail"]).to_csv(
            out_dir / "parity_differences.csv", index=False
        )
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Feather vs MySQL candle/scanner parity audit")
    p.add_argument("--exchange", default="bybit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--timeframes", default="5m,15m,30m")
    p.add_argument("--output-dir", default=str(RESULTS_DIR))
    p.add_argument("--skip-full-window", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    tfs = [x.strip() for x in str(args.timeframes).split(",") if x.strip()]
    report = run_parity_audit(
        exchange=args.exchange,
        symbol=args.symbol,
        timeframes=tfs,
        output_dir=Path(args.output_dir),
        include_full_window=not args.skip_full_window,
    )
    print(json.dumps(json_safe(report.to_dict()), indent=2, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
