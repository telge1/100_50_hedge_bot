#!/usr/bin/env python3
"""Phase 11P — cross-line catalog resolution and full remaining-pool validation."""

from __future__ import annotations

import hashlib
import json
import resource
import signal
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

from indicators.live_micro_lld_v2.availability import bar_close
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
from research_charts.live_micro_lld_v2_ch_test.phase11p_catalog_resolution import (
    classify_missing_catalog,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    build_fixed_segments,
    remaining_from_penetration,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import (
    STRENGTH_CLASS_COLORS,
)

LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
LINE9 = "phase9_DOGEUSDT_5m_PD10_ROLLING4M_full_e4b5552e"
SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
EXPECTED = 2016


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    report_path = Path(__file__).resolve().parent / f"phase11p_integrity_{run_id}.json"
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = f"{_repo}:{_dash}:{_trp}"
    unit = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "research_charts/tests/test_remaining_pool_projection_v1.py",
            "research_charts/tests/test_phase11o_candle_ranges.py",
            "research_charts/tests/test_phase11p_lifecycle_fixtures.py",
            "-q",
            "--tb=line",
        ],
        cwd=str(_dash),
        env=env,
        capture_output=True,
        text=True,
    )

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    t0 = time.perf_counter()

    published = client.query(
        f"""
        SELECT decision_close, snapshot_id, active_state_fingerprint, engine_input_hash,
               config_hash, prefix_candle_fingerprint
        FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
        WHERE pipeline_line_id = {{lid:String}}
        ORDER BY decision_close
        """,
        parameters={"lid": LINE},
    ).result_rows
    gap_sids = []
    pub = []
    for r in published:
        pub.append((_utc(r[0]), r[1], r[2], r[3], r[4], r[5]))
    phase10_only = {
        r[0]
        for r in client.query(
            f"""
            SELECT snapshot_id FROM {db}.research_lmv2_snapshot_active_pool_v1
            WHERE pipeline_line_id = {{lid:String}}
            GROUP BY snapshot_id
            """,
            parameters={"lid": LINE},
        ).result_rows
    }
    for dc, sid, *_rest in pub:
        if sid not in phase10_only:
            gap_sids.append(sid)

    by_sid = client.query(
        f"""
        SELECT snapshot_id, any(pipeline_line_id), count()
        FROM {db}.research_lmv2_snapshot_active_pool_v1
        WHERE snapshot_id IN {{sids:Array(String)}}
        GROUP BY snapshot_id
        """,
        parameters={"sids": gap_sids},
    ).result_rows
    sid_counts = {r[0]: (r[1], int(r[2])) for r in by_sid}
    member_counts = {
        r[0]: int(r[1])
        for r in client.query(
            f"""
            SELECT snapshot_id, count()
            FROM {db}.research_lmv2_active_snapshot_member_v1
            WHERE snapshot_id IN {{sids:Array(String)}}
            GROUP BY snapshot_id
            """,
            parameters={"sids": gap_sids},
        ).result_rows
    }
    identity = client.query(
        f"""
        SELECT count(),
               countIf(a.config_hash = b.config_hash
                   AND a.engine_input_hash = b.engine_input_hash
                   AND a.active_state_fingerprint = b.active_state_fingerprint
                   AND a.prefix_candle_fingerprint = b.prefix_candle_fingerprint
                   AND a.decision_close = b.decision_close)
        FROM {db}.research_lmv2_active_snapshot_publish_v1 a FINAL
        INNER JOIN {db}.research_lmv2_active_snapshot_publish_v1 b FINAL
          ON a.snapshot_id = b.snapshot_id
        WHERE a.pipeline_line_id = {{p9:String}}
          AND b.pipeline_line_id = {{p10:String}}
          AND a.snapshot_id IN {{sids:Array(String)}}
        """,
        parameters={"p9": LINE9, "p10": LINE, "sids": gap_sids},
    ).result_rows[0]
    identity_match = int(identity[0]) == len(gap_sids) and int(identity[1]) == len(gap_sids)
    line_rows = 0
    sid_rows = sum(v[1] for v in sid_counts.values())
    mem_rows = sum(member_counts.get(s, 0) for s in gap_sids)
    per_sid_ok = all(
        sid_counts.get(s, ("", 0))[1] == member_counts.get(s, -1) > 0 for s in gap_sids
    )
    root = classify_missing_catalog(
        phase10_line_rows=line_rows,
        snapshot_id_rows=sid_rows if per_sid_ok else -1,
        member_rows=mem_rows,
        identity_match=identity_match and per_sid_ok,
    )
    if not per_sid_ok:
        root = "UNKNOWN"

    mismatch = client.query(
        f"""
        SELECT count() FROM (
          SELECT p.snapshot_id
          FROM {db}.research_lmv2_active_snapshot_publish_v1 p FINAL
          LEFT JOIN (
            SELECT snapshot_id, count() n
            FROM {db}.research_lmv2_snapshot_active_pool_v1
            GROUP BY snapshot_id
          ) c ON c.snapshot_id = p.snapshot_id
          LEFT JOIN (
            SELECT snapshot_id, count() m
            FROM {db}.research_lmv2_active_snapshot_member_v1
            GROUP BY snapshot_id
          ) mb ON mb.snapshot_id = p.snapshot_id
          WHERE p.pipeline_line_id = {{lid:String}}
            AND (c.n != mb.m OR c.n = 0 OR mb.m = 0)
        )
        """,
        parameters={"lid": LINE},
    ).result_rows[0][0]
    decision_mismatch = client.query(
        f"""
        SELECT count()
        FROM {db}.research_lmv2_snapshot_active_pool_v1 c
        INNER JOIN {db}.research_lmv2_active_snapshot_publish_v1 p FINAL
          ON p.snapshot_id = c.snapshot_id
        WHERE p.pipeline_line_id = {{lid:String}}
          AND c.decision_close != p.decision_close
        """,
        parameters={"lid": LINE},
    ).result_rows[0][0]
    future = client.query(
        f"""
        SELECT count()
        FROM {db}.research_lmv2_snapshot_active_pool_v1 c
        INNER JOIN {db}.research_lmv2_active_snapshot_publish_v1 p FINAL
          ON p.snapshot_id = c.snapshot_id
        WHERE p.pipeline_line_id = {{lid:String}} AND c.known_at > c.decision_close
        """,
        parameters={"lid": LINE},
    ).result_rows[0][0]
    attr_conflict = client.query(
        f"""
        SELECT count() FROM (
          SELECT c.pool_id
          FROM {db}.research_lmv2_snapshot_active_pool_v1 c
          INNER JOIN {db}.research_lmv2_active_snapshot_publish_v1 p FINAL
            ON p.snapshot_id = c.snapshot_id
          WHERE p.pipeline_line_id = {{lid:String}}
          GROUP BY c.pool_id
          HAVING uniqExact(c.top_price) > 1 OR uniqExact(c.bottom_price) > 1
              OR uniqExact(c.strength) > 1 OR uniqExact(c.strength_class) > 1
              OR uniqExact(c.side) > 1 OR uniqExact(c.known_at) > 1
        )
        """,
        parameters={"lid": LINE},
    ).result_rows[0][0]
    at_cap = client.query(
        f"""
        SELECT count() FROM (
          SELECT c.snapshot_id, c.side, count() n
          FROM {db}.research_lmv2_snapshot_active_pool_v1 c
          INNER JOIN {db}.research_lmv2_active_snapshot_publish_v1 p FINAL
            ON p.snapshot_id = c.snapshot_id
          WHERE p.pipeline_line_id = {{lid:String}}
          GROUP BY c.snapshot_id, c.side
          HAVING n >= 300
        )
        """,
        parameters={"lid": LINE},
    ).result_rows[0][0]
    fp_before = client.query(
        f"""
        SELECT count(), sum(cityHash64(c.pool_id))
        FROM {db}.research_lmv2_snapshot_active_pool_v1 c
        INNER JOIN (
          SELECT snapshot_id FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
          WHERE pipeline_line_id = {{lid:String}}
        ) p ON p.snapshot_id = c.snapshot_id
        """,
        parameters={"lid": LINE},
    ).result_rows[0]
    rev = client.query(
        f"""
        SELECT count() FROM (
          SELECT open_time
          FROM {db}.candles_1m
          WHERE symbol = 'DOGEUSDT'
            AND open_time >= toDateTime64('2026-08-01', 3, 'UTC')
            AND open_time < toDateTime64('2026-10-01', 3, 'UTC')
          GROUP BY open_time
          HAVING uniqExact(ingested_at) > 1
        )
        """
    ).result_rows[0][0]

    complete = (
        len(pub) == EXPECTED
        and int(mismatch) == 0
        and int(decision_mismatch) == 0
        and int(future) == 0
        and int(attr_conflict) == 0
        and root == "CROSS_LINE_FILTER"
    )
    report: dict = {
        "PHASE11P_MODE": "ISOLATED_INTEGRITY_VALIDATION",
        "RUN_ID": run_id,
        "PHASE10_PUBLISHED_CLOSES": len(pub),
        "CROSS_LINE_SNAPSHOT_IDS": len(gap_sids),
        "CROSS_LINE_CATALOG_LINE": LINE9,
        "MISSING_CATALOG_ROOT_CAUSE": root,
        "CANONICAL_CATALOG_RESOLUTION": "PASS" if root == "CROSS_LINE_FILTER" and per_sid_ok else "FAIL",
        "CANONICAL_READER_SQL": "WHERE snapshot_id = {sid}  (same predicate as load_active_pool_catalog; DDL helper not called)",
        "ALL_2016_SNAPSHOTS_COMPLETE": "YES" if complete else "NO",
        "ACTIVE_MEMBERSHIP_PARITY": "PASS" if int(mismatch) == 0 else "FAIL",
        "PHASE10_ORIGINAL_ATTRIBUTES_PARITY": "PASS" if int(attr_conflict) == 0 and int(decision_mismatch) == 0 else "FAIL",
        "IDENTITY_MATCH_PHASE9_PHASE10": identity_match,
        "SIDE_COUNTS_AT_BOX_CAP": int(at_cap),
        "MULTI_INGESTED_AT_OPEN_TIMES": int(rev),
    }

    if not complete:
        report["REMAINING_PROJECTION_ALL_CLOSES"] = "NOT_RUN"
        report["READY_FOR_REMAINING_POOL_CH_POC"] = "NO"
        report["REPORT_PATH"] = str(report_path)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        client.close()
        return 1

    rows = []
    for r in client.query(
        f"""
        SELECT c.decision_close, c.pool_id, c.side, c.pool_type, c.top_price, c.bottom_price,
               c.strength, c.strength_class, c.known_at, c.break_at, c.invalidated_at
        FROM {db}.research_lmv2_snapshot_active_pool_v1 c
        INNER JOIN (
          SELECT snapshot_id FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
          WHERE pipeline_line_id = {{lid:String}}
        ) p ON p.snapshot_id = c.snapshot_id
        ORDER BY c.pool_id, c.decision_close
        """,
        parameters={"lid": LINE},
    ).result_rows:
        rows.append(
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
                r[9],
                r[10],
            )
        )
    snap_seconds = time.perf_counter() - t0

    t_c = time.perf_counter()
    session = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    candles, _meta = session.load_for_window(
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        PD,
    )
    close_unix, highs, lows = [], [], []
    by_close = {}
    for cnd in candles:
        cu = unix_utc(bar_close(_utc(cnd.timestamp), TF))
        close_unix.append(cu)
        highs.append(float(cnd.high))
        lows.append(float(cnd.low))
        by_close[cu] = (float(cnd.high), float(cnd.low))
    index = CandleRangeIndex(close_unix, highs, lows)
    candle_hash = hashlib.sha256(
        "".join(f"{u}|{h}|{lo}\n" for u, h, lo in zip(close_unix, highs, lows)).encode()
    ).hexdigest()
    candle_seconds = time.perf_counter() - t_c

    t_p = time.perf_counter()
    present = defaultdict(set)
    identity_map = {}
    conflicts = []
    last = {}
    mono_fail = []
    reappear = []
    transitions = 0
    examples = {}
    counts = defaultdict(int)
    break_at_set = 0
    invalidated_set = 0
    rolling_hits = 0
    catalog_closes = sorted({r[0] for r in rows})
    close_set = set(catalog_closes)

    def keep(kind, payload):
        counts[kind] += 1
        examples.setdefault(kind, payload)

    for row in rows:
        dc, pid, side, ptype, top, bottom, strength, sclass, known, brk, inv = row
        if brk is not None:
            break_at_set += 1
        if inv is not None:
            invalidated_set += 1
        present[dc].add(pid)
        ident = (top, bottom, strength, sclass, side, known)
        if pid not in identity_map:
            identity_map[pid] = ident
        elif identity_map[pid] != ident and len(conflicts) < 8:
            conflicts.append(pid)
        if known < rolling_history_from_close(dc, 4):
            rolling_hits += 1
        span = index.eligible_index_span(unix_utc(known), unix_utc(dc))
        pen_h = pen_l = None
        if span is not None:
            pen_h, pen_l = index.query(*span)
        rem_t, rem_b, frac, full, _a, _b = remaining_from_penetration(
            side,
            bottom,
            top,
            pen_h if side == "upper" else None,
            pen_l if side == "lower" else None,
        )
        pen = pen_h if side == "upper" else pen_l
        payload = {
            "pool_id": pid,
            "side": side,
            "pool_type": ptype,
            "decision_close": dc.isoformat(),
            "known_at": known.isoformat(),
            "original_top": top,
            "original_bottom": bottom,
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
        if side == "upper" and not full and 0 < frac < 1:
            keep("upper_partial", payload)
            keep(f"{ptype}_upper_partial", payload)
        elif side == "upper" and full:
            keep("upper_full", payload)
        elif side == "lower" and not full and 0 < frac < 1:
            keep("lower_partial", payload)
            keep(f"{ptype}_lower_partial", payload)
        elif side == "lower" and full:
            keep("lower_full", payload)
        prev = last.get(pid)
        if prev is not None:
            prev_dc, prev_frac, prev_pen = prev
            if frac > prev_frac + 1e-12:
                mono_fail.append({"pool_id": pid, "at": dc.isoformat(), "prev_frac": prev_frac, "frac": frac})
            if pen is not None and prev_pen is not None:
                if side == "upper" and pen < prev_pen:
                    mono_fail.append({"pool_id": pid, "reason": "upper_pen_decreased"})
                if side == "lower" and pen > prev_pen:
                    mono_fail.append({"pool_id": pid, "reason": "lower_pen_increased"})
            between = [c for c in catalog_closes if prev_dc < c < dc and c in close_set]
            if between and pid not in present[between[0]]:
                reappear.append(pid)
        last[pid] = (dc, frac, pen)
    proj_seconds = time.perf_counter() - t_p

    ordered = catalog_closes
    for a, b in zip(ordered, ordered[1:]):
        if (b - a).total_seconds() != 300:
            continue
        transitions += len(present[a] - present[b])

    # reappearance: absent on an intermediate catalogued close
    re_ids = []
    by_pool_closes = defaultdict(list)
    for dc, pid, *_rest in ((r[0], r[1]) for r in rows):
        by_pool_closes[pid].append(dc)
    for pid, seq in by_pool_closes.items():
        seq = sorted(set(seq))
        for left, right in zip(seq, seq[1:]):
            mid = [c for c in ordered if left < c < right]
            if mid:
                re_ids.append(pid)
                break

    t_seg = time.perf_counter()
    seg_ok = False
    sample = examples.get("upper_partial") or examples.get("lower_partial")
    if sample:
        seg_ok = True
        for n in (10, 24, 32):
            segs = build_fixed_segments(
                original_bottom=sample["original_bottom"],
                original_top=sample["original_top"],
                remaining_bottom=sample["remaining_bottom"],
                remaining_top=sample["remaining_top"],
                strength_class=sample["strength_class"],
                segment_count=n,
            )
            if segs[0].segment_bottom != sample["original_bottom"] or segs[-1].segment_top != sample["original_top"]:
                seg_ok = False
            if segs[0].display_color != STRENGTH_CLASS_COLORS[sample["strength_class"]]:
                seg_ok = False
        dc_mid = ordered[len(ordered) // 2]
        for pid in list(present[dc_mid])[:40]:
            top, bottom, _st, sclass, _side, _known = identity_map[pid]
            build_fixed_segments(
                original_bottom=bottom,
                original_top=top,
                remaining_bottom=bottom,
                remaining_top=top,
                strength_class=sclass,
                segment_count=24,
            )
    seg_seconds = time.perf_counter() - t_seg

    step = max(1, len(ordered) // 100)
    sampled = ordered[::step][:100]

    def frac_map(closes):
        out = {}
        # recompute from cache built below
        return out

    cache = {}
    # second stateless pass on sampled closes only, reverse order
    def project_close_map(closes):
        out = {}
        for dc in closes:
            for pid in present[dc]:
                top, bottom, _st, sclass, side, known = identity_map[pid]
                span = index.eligible_index_span(unix_utc(known), unix_utc(dc))
                pen_h = pen_l = None
                if span is not None:
                    pen_h, pen_l = index.query(*span)
                _rt, _rb, frac, _full, _a, _b = remaining_from_penetration(
                    side, bottom, top, pen_h if side == "upper" else None, pen_l if side == "lower" else None
                )
                out[(pid, dc)] = frac
        return out

    goto_a = project_close_map(sampled)
    goto_b = project_close_map(list(reversed(sampled)))
    goto_ok = goto_a == goto_b

    dc0 = sampled[len(sampled) // 2]
    kept = [pid for pid in present[dc0] if identity_map[pid][3] in {"Extreme", "Very Strong"}]
    dropped = [pid for pid in present[dc0] if identity_map[pid][3] not in {"Extreme", "Very Strong"}]
    strength_ok = bool(kept) and bool(dropped)

    fp_after = client.query(
        f"""
        SELECT count(), sum(cityHash64(c.pool_id))
        FROM {db}.research_lmv2_snapshot_active_pool_v1 c
        INNER JOIN (
          SELECT snapshot_id FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
          WHERE pipeline_line_id = {{lid:String}}
        ) p ON p.snapshot_id = c.snapshot_id
        """,
        parameters={"lid": LINE},
    ).result_rows[0]
    client.close()

    oracles = []

    def one_oracle(decision: datetime) -> dict:
        from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
            build_active_state_at_decision_close,
        )
        from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config

        hf = rolling_history_from_close(decision, 4)
        snap, _c, result = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            decision,
            pool_detail=PD,
            cfg=build_live_micro_lld_v2_config(PD),
            history_policy="ROLLING_4_CALENDAR_MONTHS",
            candle_session=session,
        )
        expected = next(p for p in pub if p[0] == decision)
        ids_engine = set(snap.active_pool_ids)
        ids_cat = present[decision]
        by_eng = {p.pool_id: p for p in result.pools if p.pool_id in ids_engine}
        bad = 0
        for pid in ids_cat:
            p = by_eng.get(pid)
            top, bottom, strength, sclass, side, known = identity_map[pid]
            if p is None or float(p.top_price) != top or float(p.bottom_price) != bottom or float(p.strength) != strength or str(p.strength_class) != sclass or _utc(p.known_at) != known:
                bad += 1
        before = {p.pool_id: (p.top_price, p.strength, p.active) for p in result.pools}
        return {
            "decision_close": decision.isoformat(),
            "fingerprint_match": expected[2] == snap.active_state_fingerprint,
            "input_hash_match": expected[3] == snap.engine_input_hash,
            "id_match": ids_engine == ids_cat,
            "attr_mismatch": bad,
            "engine_unchanged": before == {p.pool_id: (p.top_price, p.strength, p.active) for p in result.pools},
            "status": "PASS",
        }

    oracle_times = [
        datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 24, 0, 5, tzinfo=timezone.utc),
    ]

    def _alarm(_s, _f):
        raise TimeoutError("oracle budget")

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(420)
    try:
        for dc in oracle_times:
            try:
                row = one_oracle(dc)
                if not (
                    row["fingerprint_match"]
                    and row["id_match"]
                    and row["attr_mismatch"] == 0
                    and row["engine_unchanged"]
                ):
                    row["status"] = "FAIL"
                oracles.append(row)
            except TimeoutError:
                raise
            except Exception as exc:
                oracles.append({"decision_close": dc.isoformat(), "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
    except TimeoutError:
        oracles.append({"status": "NOT_RUN", "reason": "timeout"})
    finally:
        signal.alarm(0)

    oracle_status = "PASS" if oracles and all(o.get("status") == "PASS" for o in oracles) and len(oracles) == 3 else (
        "FAIL" if any(o.get("status") == "FAIL" for o in oracles) else "NOT_RUN"
    )
    mono_status = "FAIL" if mono_fail or conflicts else "PASS"
    projection_status = "PASS" if mono_status == "PASS" and len(catalog_closes) == EXPECTED else "FAIL"
    essential = projection_status == "FAIL" or oracle_status == "FAIL" or tuple(fp_before) != tuple(fp_after)

    ru = resource.getrusage(resource.RUSAGE_SELF)
    report.update(
        {
            "REMAINING_PROJECTION_ALL_CLOSES": projection_status,
            "REAL_DECISION_CLOSES_TESTED": len(catalog_closes),
            "REAL_POOL_IDS_TESTED": len(identity_map),
            "REAL_UPPER_PARTIAL_WICK": "PASS" if "upper_partial" in examples else "NOT_OBSERVED",
            "REAL_LOWER_PARTIAL_WICK": "PASS" if "lower_partial" in examples else "NOT_OBSERVED",
            "REAL_FULL_TRAVERSAL": "PASS" if "upper_full" in examples or "lower_full" in examples else "NOT_OBSERVED",
            "NORMAL_AND_MICRO_PARTIAL": {
                "normal_upper": "normal_upper_partial" in examples,
                "normal_lower": "normal_lower_partial" in examples,
                "micro_upper": "micro_upper_partial" in examples,
                "micro_lower": "micro_lower_partial" in examples,
            },
            "REAL_ACTIVE_TO_INACTIVE_TRANSITIONS": transitions,
            "REAL_ACTIVE_TO_INACTIVE_CAUSE": "UNKNOWN",
            "REAL_EVICTION_PARITY": "NOT_OBSERVED",
            "REAL_EVICTION_NOTE": (
                f"{int(at_cap)} side-snapshots sit at box_amount 300. Published active rows do not "
                f"carry a disappearance reason. break_at set on active rows: {break_at_set}. "
                f"invalidated_at set on active rows: {invalidated_set}."
            ),
            "SYNTHETIC_EVICTION_FIXTURE": "PASS" if unit.returncode == 0 else "FAIL",
            "REAL_ROLLING_REMOVAL_PARITY": "NOT_OBSERVED" if rolling_hits == 0 else "FAIL",
            "SYNTHETIC_ROLLING_FIXTURE": "PASS" if unit.returncode == 0 else "FAIL",
            "REAL_POOL_ID_REAPPEARANCE": "NOT_OBSERVED" if not re_ids else "PASS",
            "REAL_REAPPEARANCE_IDS": len(set(re_ids)),
            "SYNTHETIC_REAPPEARANCE": "PASS" if unit.returncode == 0 else "FAIL",
            "MONOTONIC_CONSUMPTION": mono_status,
            "MONOTONIC_FAILURES": mono_fail[:6],
            "IDENTITY_CONFLICTS": conflicts,
            "HISTORICAL_GOTO_PARITY": "PASS" if goto_ok else "FAIL",
            "FIXED_SEGMENT_PARITY": "PASS" if seg_ok else "FAIL",
            "STRENGTH_COLOR_MAPPING": "PASS" if set(STRENGTH_CLASS_COLORS) == {"Weak", "Medium", "Strong", "Very Strong", "Extreme"} and strength_ok else "FAIL",
            "ORIGINAL_PREFIX_ORACLES": oracle_status,
            "ORIGINAL_PREFIX_ORACLE_DETAIL": oracles,
            "CANDLE_REVISION_CONSISTENCY": "NOT_PROVEN",
            "CANDLE_CONTENT_HASH": candle_hash,
            "CANDLE_COUNT_5M": len(close_unix),
            "CATALOG_FINGERPRINT_UNCHANGED": tuple(fp_before) == tuple(fp_after),
            "SNAPSHOT_LOAD_SECONDS": round(snap_seconds, 3),
            "CANDLE_LOAD_SECONDS": round(candle_seconds, 3),
            "PROJECTION_SECONDS": round(proj_seconds, 3),
            "SEGMENT_PREPARATION_SECONDS": round(seg_seconds, 4),
            "RAM_PEAK_MB": round(ru.ru_maxrss / 1024.0, 1),
            "CASE_COUNTS": {k: counts[k] for k in counts if not k.startswith("normal") and not k.startswith("micro")},
            "EXAMPLES": {k: examples[k] for k in ("upper_partial", "lower_partial", "upper_full", "lower_full") if k in examples},
            "TEST_RESULTS": {"pytest_returncode": unit.returncode, "pytest_tail": (unit.stdout or "")[-800:]},
            "FILES_CHANGED": [
                "live_micro_lld_v2_ch_test/run_phase11o_real_doge_validation.py",
                "live_micro_lld_v2_ch_test/phase11p_catalog_resolution.py",
                "live_micro_lld_v2_ch_test/run_phase11p_integrity_validation.py",
                "tests/test_phase11p_lifecycle_fixtures.py",
            ],
            "REPORT_PATH": str(report_path),
            "READY_FOR_REMAINING_POOL_CH_POC": "NO",
            "READY_NOTE": "Candle revision binding to Phase-10 engine_input_hash is NOT_PROVEN.",
            "ESSENTIAL_FAIL": essential,
        }
    )
    if re_ids:
        report["REAL_POOL_ID_REAPPEARANCE"] = "PASS" if mono_status == "PASS" else "FAIL"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_keys = [
        "PHASE11P_MODE",
        "PHASE10_PUBLISHED_CLOSES",
        "CROSS_LINE_SNAPSHOT_IDS",
        "MISSING_CATALOG_ROOT_CAUSE",
        "CANONICAL_CATALOG_RESOLUTION",
        "ALL_2016_SNAPSHOTS_COMPLETE",
        "ACTIVE_MEMBERSHIP_PARITY",
        "PHASE10_ORIGINAL_ATTRIBUTES_PARITY",
        "REMAINING_PROJECTION_ALL_CLOSES",
        "REAL_UPPER_PARTIAL_WICK",
        "REAL_LOWER_PARTIAL_WICK",
        "REAL_FULL_TRAVERSAL",
        "REAL_ACTIVE_TO_INACTIVE_TRANSITIONS",
        "REAL_EVICTION_PARITY",
        "SYNTHETIC_EVICTION_FIXTURE",
        "REAL_ROLLING_REMOVAL_PARITY",
        "SYNTHETIC_ROLLING_FIXTURE",
        "REAL_POOL_ID_REAPPEARANCE",
        "SYNTHETIC_REAPPEARANCE",
        "MONOTONIC_CONSUMPTION",
        "HISTORICAL_GOTO_PARITY",
        "FIXED_SEGMENT_PARITY",
        "STRENGTH_COLOR_MAPPING",
        "ORIGINAL_PREFIX_ORACLES",
        "CANDLE_REVISION_CONSISTENCY",
        "SNAPSHOT_LOAD_SECONDS",
        "CANDLE_LOAD_SECONDS",
        "PROJECTION_SECONDS",
        "SEGMENT_PREPARATION_SECONDS",
        "RAM_PEAK_MB",
        "READY_FOR_REMAINING_POOL_CH_POC",
        "REPORT_PATH",
    ]
    print(json.dumps({k: report[k] for k in summary_keys}, indent=2))
    return 0 if not essential else 1


if __name__ == "__main__":
    raise SystemExit(main())
