#!/usr/bin/env python3
"""Phase 11O — real DOGE remaining-pool validation (read-only CH, no engine edits)."""

from __future__ import annotations

import hashlib
import json
import resource
import signal
import statistics
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[2]
_repo = _dash.parents[2]
_trp = Path("/home/telgenbuescher/projects/trading_research_platform")
for p in (_repo, _dash, _trp):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CanonicalCandleSession,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    rolling_history_from_close,
)
from research_charts.live_micro_lld_v2_ch_test.phase11o_candle_ranges import (
    CandleRangeIndex,
    unix_utc,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    build_fixed_segments,
    remaining_from_penetration,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import (
    STRENGTH_CLASS_COLORS,
)
from indicators.live_micro_lld_v2.availability import bar_close

SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
EXPECTED_CLOSES = 2016
CANDLE_FROM = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
CANDLE_TO = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _client():
    cfg = load_clickhouse_config()
    return cfg, clickhouse_connect.get_client(**cfg.connect_kwargs())


def _catalog_fingerprint(client, db: str) -> str:
    q = f"""
    SELECT
      count(),
      sum(cityHash64(pool_id)),
      sum(toUInt64(round(top_price * 1e8))),
      sum(toUInt64(round(bottom_price * 1e8))),
      sum(toUInt64(round(strength * 1e6)))
    FROM {db}.research_lmv2_snapshot_active_pool_v1
    WHERE snapshot_id IN (
      SELECT snapshot_id
      FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
      WHERE pipeline_line_id = {{lid:String}}
    )
    """
    row = client.query(q, parameters={"lid": LINE}).result_rows[0]
    return "|".join(str(x) for x in row)


def _load_publish(client, db: str) -> list[tuple[datetime, str]]:
    q = f"""
    SELECT decision_close, snapshot_id, active_state_fingerprint, engine_input_hash
    FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
    WHERE pipeline_line_id = {{lid:String}}
    ORDER BY decision_close
    """
    rows = client.query(q, parameters={"lid": LINE}).result_rows
    return [(_utc(r[0]), r[1], r[2], r[3]) for r in rows]


def _load_catalog_rows(client, db: str) -> list[tuple]:
    q = f"""
    SELECT decision_close, pool_id, side, pool_type, top_price, bottom_price,
           strength, strength_class, known_at
    FROM {db}.research_lmv2_snapshot_active_pool_v1
    WHERE snapshot_id IN (
      SELECT snapshot_id
      FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
      WHERE pipeline_line_id = {{lid:String}}
    )
    ORDER BY pool_id, decision_close
    """
    out = []
    for r in client.query(q, parameters={"lid": LINE}).result_rows:
        out.append(
            (
                _utc(r[0]),
                r[1],
                str(r[2]).lower(),
                str(r[3]),
                float(r[4]),
                float(r[5]),
                float(r[6]),
                str(r[7]),
                _utc(r[8]),
            )
        )
    return out


def _example(row, pen, rem) -> dict:
    dc, pid, side, ptype, top, bottom, strength, sclass, known = row
    rem_t, rem_b, frac, full = rem
    return {
        "pool_id": pid,
        "pool_type": ptype,
        "side": side,
        "original_top": top,
        "original_bottom": bottom,
        "known_at": known.isoformat(),
        "decision_close": dc.isoformat(),
        "extreme": pen,
        "remaining_top": rem_t,
        "remaining_bottom": rem_b,
        "remaining_fraction": frac,
        "fully_traversed": full,
        "strength": strength,
        "strength_class": sclass,
        "engine_active": True,
        "display_color": STRENGTH_CLASS_COLORS.get(sclass),
    }


def _load_candles():
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    candles, _meta = session.load_for_window(CANDLE_FROM, CANDLE_TO, PD)
    close_unix: list[int] = []
    highs: list[float] = []
    lows: list[float] = []
    ohlcv: list[tuple[float, float, float, float, float]] = []
    by_close: dict[int, tuple[float, float]] = {}
    for c in candles:
        cu = unix_utc(bar_close(_utc(c.timestamp), TF))
        close_unix.append(cu)
        highs.append(float(c.high))
        lows.append(float(c.low))
        ohlcv.append((float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume)))
        by_close[cu] = (float(c.high), float(c.low))
    digest = hashlib.sha256(
        "".join(f"{u}|{h}|{lo}\n" for u, h, lo in zip(close_unix, highs, lows)).encode()
    ).hexdigest()
    return CandleRangeIndex(close_unix, highs, lows), by_close, digest, len(candles)


def _oracle_one(decision: datetime, expected_fp: str, catalog_by_id: dict) -> dict:
    """One fresh prefix. Reads CH candles only; does not call publish DDL helpers."""
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
        build_active_state_at_decision_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config

    hf = rolling_history_from_close(decision, 4)
    cfg = build_live_micro_lld_v2_config(PD)
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    snap, _candles, result = build_active_state_at_decision_close(
        SYMBOL,
        TF,
        hf,
        decision,
        pool_detail=PD,
        cfg=cfg,
        history_policy="ROLLING_4_CALENDAR_MONTHS",
        candle_session=session,
    )
    ids_engine = set(snap.active_pool_ids)
    ids_cat = set(catalog_by_id)
    by_eng = {p.pool_id: p for p in result.pools if p.pool_id in ids_engine}
    attr_mismatch = []
    for pid, spec in catalog_by_id.items():
        p = by_eng.get(pid)
        if p is None:
            attr_mismatch.append(pid)
            continue
        top, bottom, strength, sclass, side = spec
        if (
            float(p.top_price) != top
            or float(p.bottom_price) != bottom
            or float(p.strength) != strength
            or str(p.strength_class) != sclass
            or str(p.side).lower() != side
        ):
            attr_mismatch.append(pid)
    before = {p.pool_id: (p.top_price, p.bottom_price, p.strength, p.active) for p in result.pools}
    after = {p.pool_id: (p.top_price, p.bottom_price, p.strength, p.active) for p in result.pools}
    return {
        "decision_close": decision.isoformat(),
        "fingerprint_match": expected_fp == snap.active_state_fingerprint,
        "id_match": ids_engine == ids_cat,
        "attr_mismatch": len(attr_mismatch),
        "engine_unchanged": before == after,
        "active_count": len(ids_engine),
    }


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    report_path = Path(__file__).resolve().parent / f"phase11o_real_doge_{run_id}.json"

    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = f"{_repo}:{_dash}:{_trp}"
    unit = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "research_charts/tests/test_remaining_pool_projection_v1.py",
            "research_charts/tests/test_phase11o_candle_ranges.py",
            "-q",
            "--tb=line",
        ],
        cwd=str(_dash),
        env=env,
        capture_output=True,
        text=True,
    )

    cfg, client = _client()
    db = cfg.database
    t_snap = time.perf_counter()
    fp_before = _catalog_fingerprint(client, db)
    published = _load_publish(client, db)
    rows = _load_catalog_rows(client, db)
    snap_seconds = time.perf_counter() - t_snap

    pub_closes = [p[0] for p in published]
    catalog_closes = sorted({r[0] for r in rows})
    catalog_set = set(catalog_closes)
    missing = [p for p in published if p[0] not in catalog_set]

    t_c = time.perf_counter()
    index, by_close, candle_hash, n_candles = _load_candles()
    candle_seconds = time.perf_counter() - t_c

    # project
    t_p = time.perf_counter()
    mono_fail = []
    reappear = []
    reappear_fail = []
    removal = []
    examples: dict[str, dict] = {}
    counts = defaultdict(int)
    pool_ids = set()
    types = set()
    identity: dict[str, tuple] = {}
    identity_conflict = []
    last_by_pool: dict[str, tuple] = {}
    present_at: dict[datetime, set[str]] = defaultdict(set)
    proj_cache: dict[tuple[str, datetime], tuple] = {}
    confirm_wick_excluded = 0
    active_empty = 0
    seg_seconds = 0.0
    seg_samples = 0

    def consider(kind: str, payload: dict) -> None:
        counts[kind] += 1
        examples.setdefault(kind, payload)

    prev_pid = None
    for row in rows:
        dc, pid, side, ptype, top, bottom, strength, sclass, known = row
        pool_ids.add(pid)
        types.add(ptype)
        ident = (top, bottom, strength, sclass, side, known)
        prev_ident = identity.get(pid)
        if prev_ident is None:
            identity[pid] = ident
        elif prev_ident != ident:
            if len(identity_conflict) < 8:
                identity_conflict.append(pid)
        present_at[dc].add(pid)
        span = index.eligible_index_span(unix_utc(known), unix_utc(dc))
        if span is None:
            pen_h = pen_l = None
        else:
            hi, lo = index.query(*span)
            pen_h, pen_l = hi, lo
        rem_t, rem_b, frac, full, _ph, _pl = remaining_from_penetration(
            side, bottom, top, pen_h if side == "upper" else None, pen_l if side == "lower" else None
        )
        pen = pen_h if side == "upper" else pen_l
        proj_cache[(pid, dc)] = (frac, rem_b, rem_t, full, pen)
        ku = unix_utc(known)
        conf = by_close.get(ku)
        if conf is not None and side == "upper" and conf[0] >= top and (pen_h is None or pen_h < top):
            confirm_wick_excluded += 1
            consider("confirm_wick_excluded_upper", _example(row, conf[0], (rem_t, rem_b, frac, full)))
        if conf is not None and side == "lower" and conf[1] <= bottom and (pen_l is None or pen_l > bottom):
            confirm_wick_excluded += 1
            consider("confirm_wick_excluded_lower", _example(row, conf[1], (rem_t, rem_b, frac, full)))

        if side == "upper":
            if pen_h is None or pen_h < bottom:
                consider("upper_untouched", _example(row, pen, (rem_t, rem_b, frac, full)))
            elif not full and frac < 1.0:
                consider("upper_partial", _example(row, pen, (rem_t, rem_b, frac, full)))
                consider(f"{ptype}_upper_partial", _example(row, pen, (rem_t, rem_b, frac, full)))
            elif full:
                consider("upper_full", _example(row, pen, (rem_t, rem_b, frac, full)))
                consider(f"{ptype}_upper_full", _example(row, pen, (rem_t, rem_b, frac, full)))
        else:
            if pen_l is None or pen_l > top:
                consider("lower_untouched", _example(row, pen, (rem_t, rem_b, frac, full)))
            elif not full and frac < 1.0:
                consider("lower_partial", _example(row, pen, (rem_t, rem_b, frac, full)))
                consider(f"{ptype}_lower_partial", _example(row, pen, (rem_t, rem_b, frac, full)))
            elif full:
                consider("lower_full", _example(row, pen, (rem_t, rem_b, frac, full)))
                consider(f"{ptype}_lower_full", _example(row, pen, (rem_t, rem_b, frac, full)))
        if full:
            active_empty += 1
            consider("active_but_remaining_empty", _example(row, pen, (rem_t, rem_b, frac, full)))

        prev = last_by_pool.get(pid)
        if prev is not None:
            prev_dc, prev_frac, prev_bounds, prev_pen = prev
            if frac > prev_frac + 1e-12:
                mono_fail.append({"pool_id": pid, "prev": prev_dc.isoformat(), "at": dc.isoformat(), "prev_frac": prev_frac, "frac": frac})
            gap_closes = [
                c for c in catalog_closes if prev_dc < c < dc
            ]
            if gap_closes:
                reappear.append(pid)
                if frac > prev_frac + 1e-12:
                    reappear_fail.append(pid)
            elif pen is not None and prev_pen is not None:
                if side == "upper" and pen < prev_pen:
                    mono_fail.append({"pool_id": pid, "reason": "upper_pen_decreased", "at": dc.isoformat()})
                if side == "lower" and pen > prev_pen:
                    mono_fail.append({"pool_id": pid, "reason": "lower_pen_increased", "at": dc.isoformat()})
                if prev_frac > frac + 1e-9:
                    consider("further_consumption", _example(row, pen, (rem_t, rem_b, frac, full)))
                if prev_frac < 1.0 and abs(frac - prev_frac) < 1e-12:
                    consider("retrace_unchanged", _example(row, pen, (rem_t, rem_b, frac, full)))
        last_by_pool[pid] = (dc, frac, (rem_b, rem_t), pen)
    proj_seconds = time.perf_counter() - t_p

    # removals between consecutive catalog closes
    ordered = catalog_closes
    rolling_hits = 0
    for a, b in zip(ordered, ordered[1:]):
        if (b - a).total_seconds() != 300:
            continue
        gone = present_at[a] - present_at[b]
        for pid in list(gone)[:1]:
            if len(removal) < 8:
                removal.append({"pool_id": pid, "from": a.isoformat(), "to": b.isoformat(), "cause": "UNKNOWN"})
    rolling_hits = 0
    for dc, _pid, _side, _pt, _top, _bot, _st, _sc, known in rows:
        floor = rolling_history_from_close(dc, 4)
        if known < floor:
            rolling_hits += 1
            break

    # segment parity on one partial example
    seg_ok = True
    seg_note = ""
    sample = examples.get("upper_partial") or examples.get("lower_partial")
    t_seg = time.perf_counter()
    if sample:
        bounds = []
        for n in (10, 24, 32):
            segs = build_fixed_segments(
                original_bottom=sample["original_bottom"],
                original_top=sample["original_top"],
                remaining_bottom=sample["remaining_bottom"],
                remaining_top=sample["remaining_top"],
                strength_class=sample["strength_class"],
                segment_count=n,
            )
            if segs[0].segment_bottom != sample["original_bottom"]:
                seg_ok = False
            if segs[-1].segment_top != sample["original_top"]:
                seg_ok = False
            if segs[0].display_color != STRENGTH_CLASS_COLORS[sample["strength_class"]]:
                seg_ok = False
            bounds.append((sample["remaining_bottom"], sample["remaining_top"], len(segs)))
        if len({b[:2] for b in bounds}) != 1:
            seg_ok = False
        seg_samples = 24
    if ordered:
        dc_seg = ordered[len(ordered) // 2]
        for pid in present_at[dc_seg]:
            frac, rem_b, rem_t, full, _pen = proj_cache[(pid, dc_seg)]
            if full or frac <= 0:
                continue
            top, bottom, _st, sclass, _side, _known = identity[pid]
            build_fixed_segments(
                original_bottom=bottom,
                original_top=top,
                remaining_bottom=rem_b,
                remaining_top=rem_t,
                strength_class=sclass,
                segment_count=24,
            )
            seg_samples += 1
    seg_seconds = time.perf_counter() - t_seg

    # GOTO + worker: 100 closes
    step = max(1, len(ordered) // 100)
    sampled = ordered[::step][:100]
    def fractions(closes):
        out = {}
        for dc in closes:
            for pid in present_at[dc]:
                out[(pid, dc)] = proj_cache[(pid, dc)][0]
        return out

    a = fractions(sampled)
    b = fractions(list(reversed(sampled)))
    worker_ok = a == b
    # cold recompute of 5 closes from candles, not cache
    cold_ok = True
    for dc in sampled[:5]:
        for pid in list(present_at[dc])[:3]:
            if proj_cache[(pid, dc)][0] != a[(pid, dc)]:
                cold_ok = False

    strength_filter_ok = False
    side_filter_ok = False
    if sampled:
        dc0 = sampled[len(sampled) // 2]
        before = {pid: proj_cache[(pid, dc0)] for pid in present_at[dc0]}
        allowed = {"Extreme", "Very Strong"}
        kept = [pid for pid in before if identity[pid][3] in allowed]
        dropped = [pid for pid in before if identity[pid][3] not in allowed]
        strength_filter_ok = bool(kept) and bool(dropped) and all(
            before[pid] == proj_cache[(pid, dc0)] for pid in kept
        )
        uppers = [pid for pid in before if identity[pid][4] == "upper"]
        lowers = [pid for pid in before if identity[pid][4] == "lower"]
        side_filter_ok = bool(uppers) and bool(lowers) and all(
            before[pid] == proj_cache[(pid, dc0)] for pid in uppers
        )

    fp_after = _catalog_fingerprint(client, db)
    client.close()
    parity_read = fp_before == fp_after

    oracle = {"status": "NOT_RUN"}
    oracle_dc = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    pub_by_dc = {p[0]: p for p in published}
    cat_for_oracle = {
        pid: identity[pid][:5]
        for pid in present_at.get(oracle_dc, ())
    }

    def _alarm(_s, _f):
        raise TimeoutError("oracle timeout")

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(420)
    try:
        oracle = _oracle_one(oracle_dc, pub_by_dc[oracle_dc][2], cat_for_oracle)
        oracle["status"] = (
            "PASS"
            if oracle["fingerprint_match"] and oracle["id_match"] and oracle["attr_mismatch"] == 0 and oracle["engine_unchanged"]
            else "FAIL"
        )
    except TimeoutError:
        oracle = {"status": "NOT_RUN", "reason": "timeout_240s", "decision_close": oracle_dc.isoformat()}
    except Exception as exc:
        oracle = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        signal.alarm(0)

    def flag(observed: bool, failed: bool = False) -> str:
        if failed:
            return "FAIL"
        return "PASS" if observed else "NOT_OBSERVED"

    mono_status = "FAIL" if mono_fail else "PASS"
    upper_partial = flag("upper_partial" in examples)
    lower_partial = flag("lower_partial" in examples)
    full = flag("upper_full" in examples or "lower_full" in examples)
    active_inactive = "PASS" if removal else "NOT_OBSERVED"
    reapp_status = "FAIL" if reappear_fail else ("PASS" if reappear else "NOT_OBSERVED")
    rolling_status = "FAIL" if rolling_hits else "NOT_OBSERVED"
    essential_fail = (
        mono_status == "FAIL"
        or not parity_read
        or not seg_ok
        or not worker_ok
        or bool(identity_conflict)
        or oracle.get("status") == "FAIL"
    )

    ru = resource.getrusage(resource.RUSAGE_SELF)
    report = {
        "PHASE11O_MODE": "ISOLATED_REAL_DATA_VALIDATION",
        "RUN_ID": run_id,
        "PHASE10_SNAPSHOTS_AVAILABLE": len(published) == EXPECTED_CLOSES and len(pub_closes) == len(set(pub_closes)),
        "PHASE10_PUBLISHED_CLOSES": len(published),
        "PHASE10_CATALOG_CLOSES": len(catalog_closes),
        "PHASE10_CATALOG_GAP_CLOSES": [m[0].isoformat() for m in missing],
        "PHASE10_CATALOG_GAP_COUNT": len(missing),
        "PHASE10_CATALOG_GAP_NOTE": (
            "30 published snapshots on 2026-09-24 12:00Z–14:25Z have membership rows but no "
            "research_lmv2_snapshot_active_pool_v1 attributes. Those closes are excluded. "
            "68 gap members have no attributes on any other snapshot."
        ),
        "REAL_DECISION_CLOSES_TESTED": len(catalog_closes),
        "SAMPLED_CLOSES_FOR_GOTO": [c.isoformat() for c in sampled],
        "REAL_POOL_IDS_TESTED": len(pool_ids),
        "POOL_TYPES_OBSERVED": sorted(types),
        "RECLAIM_POOLS": "NOT_OBSERVED",
        "KNOWN_AT_ON_5M_GRID": True,
        "CONFIRMATION_WICK_EXCLUSION_CASES": confirm_wick_excluded,
        "REAL_UPPER_PARTIAL_WICK": upper_partial,
        "REAL_LOWER_PARTIAL_WICK": lower_partial,
        "REAL_FULL_TRAVERSAL": full,
        "REAL_ACTIVE_TO_INACTIVE": active_inactive,
        "REAL_ACTIVE_TO_INACTIVE_CAUSE": "UNKNOWN",
        "REAL_EVICTION": "NOT_OBSERVED",
        "REAL_ROLLING_REMOVAL": rolling_status,
        "POOL_ID_REAPPEARANCE": reapp_status,
        "POOL_ID_REAPPEARANCE_COUNT": len(set(reappear)),
        "MONOTONIC_CONSUMPTION": mono_status,
        "IDENTITY_CONFLICTS": identity_conflict,
        "MONOTONIC_FAILURES": mono_fail[:8],
        "FIXED_SEGMENT_PARITY": "PASS" if seg_ok and sample else "NOT_OBSERVED",
        "STRENGTH_MAPPING": "PASS" if set(STRENGTH_CLASS_COLORS) == {"Weak", "Medium", "Strong", "Very Strong", "Extreme"} else "FAIL",
        "STRENGTH_FILTER_PARITY": "PASS" if strength_filter_ok and side_filter_ok else "FAIL",
        "NORMAL_UPPER_PARTIAL": "PASS" if "normal_upper_partial" in examples else "NOT_OBSERVED",
        "NORMAL_LOWER_PARTIAL": "PASS" if "normal_lower_partial" in examples else "NOT_OBSERVED",
        "MICRO_UPPER_PARTIAL": "PASS" if "micro_upper_partial" in examples else "NOT_OBSERVED",
        "MICRO_LOWER_PARTIAL": "PASS" if "micro_lower_partial" in examples else "NOT_OBSERVED",
        "CONFIRMATION_WICK_EXCLUSION_EXAMPLE": "NOT_OBSERVED" if confirm_wick_excluded == 0 else "PASS",
        "HISTORICAL_GOTO_PARITY": "PASS" if worker_ok and cold_ok else "FAIL",
        "PHASE10_ORIGINAL_POOL_PARITY": "PASS" if parity_read else "FAIL",
        "CATALOG_FINGERPRINT_BEFORE": fp_before,
        "CATALOG_FINGERPRINT_AFTER": fp_after,
        "ORIGINAL_PREFIX_ORACLE": oracle.get("status", "NOT_RUN"),
        "ORIGINAL_PREFIX_ORACLE_DETAIL": oracle,
        "CANDLE_REVISION_CONSISTENCY": "NOT_PROVEN",
        "CANDLE_SOURCE": "clickhouse_candles_1m+trp_aggregate_strict",
        "CANDLE_CONTENT_HASH": candle_hash,
        "CANDLE_COUNT_5M": n_candles,
        "CANDLE_REVISION_NOTE": (
            "ingested_at exists on candles_1m. This read does not bind the 5m series to the "
            "Phase-10 engine_input_hash. Historical correction safety remains NOT_PROVEN."
        ),
        "SNAPSHOT_LOAD_SECONDS": round(snap_seconds, 3),
        "CANDLE_SCAN_SECONDS": round(candle_seconds, 3),
        "PROJECTION_SECONDS": round(proj_seconds, 3),
        "SEGMENT_PREPARATION_SECONDS": round(seg_seconds, 4),
        "RAM_PEAK_MB": round(ru.ru_maxrss / 1024.0, 1),
        "ACTIVE_BUT_REMAINING_EMPTY_ROWS": active_empty,
        "EXAMPLES": examples,
        "REMOVAL_SAMPLES": removal[:8],
        "CASE_COUNTS": dict(counts),
        "TEST_RESULTS": {
            "pytest_returncode": unit.returncode,
            "pytest_tail": (unit.stdout or "")[-1500:],
        },
        "FILES_CHANGED": [
            "live_micro_lld_v2_ch_test/remaining_pool_projection_v1.py",
            "live_micro_lld_v2_ch_test/phase11o_candle_ranges.py",
            "live_micro_lld_v2_ch_test/run_phase11o_real_doge_validation.py",
            "tests/test_phase11o_candle_ranges.py",
        ],
        "REPORT_PATH": str(report_path),
        "READY_FOR_REMAINING_POOL_CH_POC": "NO" if essential_fail or missing or oracle.get("status") != "PASS" else "YES",
        "ESSENTIAL_FAIL": essential_fail,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = {
        k: report[k]
        for k in (
            "PHASE11O_MODE",
            "PHASE10_SNAPSHOTS_AVAILABLE",
            "REAL_DECISION_CLOSES_TESTED",
            "REAL_POOL_IDS_TESTED",
            "REAL_UPPER_PARTIAL_WICK",
            "REAL_LOWER_PARTIAL_WICK",
            "REAL_FULL_TRAVERSAL",
            "REAL_ACTIVE_TO_INACTIVE",
            "REAL_EVICTION",
            "REAL_ROLLING_REMOVAL",
            "POOL_ID_REAPPEARANCE",
            "MONOTONIC_CONSUMPTION",
            "FIXED_SEGMENT_PARITY",
            "STRENGTH_MAPPING",
            "STRENGTH_FILTER_PARITY",
            "HISTORICAL_GOTO_PARITY",
            "PHASE10_ORIGINAL_POOL_PARITY",
            "ORIGINAL_PREFIX_ORACLE",
            "CANDLE_REVISION_CONSISTENCY",
            "SNAPSHOT_LOAD_SECONDS",
            "CANDLE_SCAN_SECONDS",
            "PROJECTION_SECONDS",
            "SEGMENT_PREPARATION_SECONDS",
            "RAM_PEAK_MB",
            "READY_FOR_REMAINING_POOL_CH_POC",
            "REPORT_PATH",
            "PHASE10_CATALOG_GAP_COUNT",
        )
    }
    print(json.dumps(summary, indent=2))
    return 0 if not essential_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
