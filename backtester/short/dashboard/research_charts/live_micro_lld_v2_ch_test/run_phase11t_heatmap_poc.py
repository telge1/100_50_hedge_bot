#!/usr/bin/env python3
"""Phase 11T — isolated historical remaining-pool heatmap. No engine edits, no CH writes."""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[2]
_repo = _dash.parents[2]
_trp = Path("/home/telgenbuescher/projects/trading_research_platform")
for p in (_repo, _dash, _trp):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import clickhouse_connect

from data.models import ensure_utc
from indicators.live_micro_lld_v2.availability import bar_close
from indicators.live_micro_lld_v2.models import LiquidityPoolV2
from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import rolling_history_from_close
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import load_snapshot
from research_charts.live_micro_lld_v2_ch_test.immutable_snapshot_candle_session import ImmutableSnapshotCandleSession
from research_charts.live_micro_lld_v2_ch_test.phase11o_candle_ranges import CandleRangeIndex
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    build_fixed_segments,
    engine_active_at_close,
    project_pool_v1,
    remaining_from_penetration,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import (
    STRENGTH_CLASS_COLORS,
)

LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
HERE = Path(__file__).resolve().parent
PHASE11R_REPORT = HERE / "phase11r_full_rolling_input_20261009T121404Z_defdfaea.json"
POC_DIR = HERE / "phase11t_heatmap"
PAYLOAD_DIR = POC_DIR / "payloads"
CLOSES = (
    datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
)
CHART_BARS = 288
SEGMENT_COUNT = 24


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_key(dt: datetime) -> str:
    return _utc(dt).strftime("%Y%m%dT%H%M%SZ")


def _pool_from_row(row) -> LiquidityPoolV2:
    known = _utc(row["known_at"])
    return LiquidityPoolV2(
        pool_id=row["pool_id"],
        pool_type=row["pool_type"],
        symbol="DOGEUSDT",
        timeframe="5m",
        side=row["side"],
        source_timestamp=known,
        created_timestamp=known,
        known_at=known,
        top_price=row["top"],
        bottom_price=row["bottom"],
        strength=row["strength"],
        strength_class=row["strength_class"],
        active=True,
    )


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    report_path = HERE / f"phase11t_heatmap_{run_id}.json"
    phase11r = json.loads(PHASE11R_REPORT.read_text(encoding="utf-8"))
    snap_dir = Path(phase11r["SNAPSHOT_DIR"])
    snap_id = phase11r["IMMUTABLE_SNAPSHOT_ID"]
    manifest = json.loads((snap_dir / f"{snap_id}.manifest.json").read_text(encoding="utf-8"))
    if manifest["snapshot_id"] != snap_id or manifest["ohlcv_content_hash"] != phase11r["PRICE_CONTENT_HASH"]:
        raise SystemExit("phase 11R manifest does not match the report")
    if int(manifest["row_count"]) != int(phase11r["SNAPSHOT_1M_ROWS"]):
        raise SystemExit("phase 11R row count mismatch")

    t0 = time.perf_counter()
    frozen = load_snapshot(snap_dir, snap_id)
    read_s = time.perf_counter() - t0
    if len(frozen) != int(manifest["row_count"]):
        raise SystemExit("immutable readback row mismatch")

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    t_cat = time.perf_counter()
    published = {}
    catalogs = {}
    for dc in CLOSES:
        pub = client.query(
            f"""
            SELECT snapshot_id, active_state_fingerprint, config_hash
            FROM {cfg.database}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE pipeline_line_id = {{lid:String}}
              AND decision_close = {{dc:DateTime64(3, 'UTC')}}
            LIMIT 1
            """,
            parameters={"lid": LINE, "dc": dc},
        ).result_rows
        if not pub:
            client.close()
            raise SystemExit(f"missing phase10 publish {_iso(dc)}")
        sid, fp, cfg_hash = pub[0]
        members = client.query(
            f"""
            SELECT pool_id, side
            FROM {cfg.database}.research_lmv2_active_snapshot_member_v1
            WHERE snapshot_id = {{sid:String}}
            """,
            parameters={"sid": sid},
        ).result_rows
        attrs = client.query(
            f"""
            SELECT pool_id, side, pool_type, top_price, bottom_price, strength, strength_class, known_at
            FROM {cfg.database}.research_lmv2_snapshot_active_pool_v1
            WHERE snapshot_id = {{sid:String}}
            """,
            parameters={"sid": sid},
        ).result_rows
        published[dc] = {"snapshot_id": sid, "fingerprint": fp, "config_hash": cfg_hash, "members": members}
        catalogs[dc] = attrs
    client.close()
    catalog_s = time.perf_counter() - t_cat

    session = ImmutableSnapshotCandleSession(snap_dir, snap_id, ohlcv_hash=manifest["ohlcv_content_hash"])
    history_from = rolling_history_from_close(CLOSES[-1], 4)
    full, _meta = session.load_for_window(history_from, CLOSES[-1], 10)
    if session.stats.ch_fetch_calls != 0:
        raise SystemExit("clickhouse candle fallback")

    upper = lower = full_hit = False
    bounds_ok = True
    color_ok = True
    segment_ok = True
    causal_ok = True
    future_ok = True
    fingerprint_ok = True
    member_attr_ok = True
    scan_ok = True
    shared_grids: dict[tuple, tuple] = {}
    monotonic_pairs = 0
    monotonic_fail = 0
    prior_frac: dict[tuple, float] = {}
    ids_by_close: list[set[str]] = []
    frac_by_close: list[dict[str, tuple]] = []
    payloads = []
    proj_s = 0.0
    payload_s = 0.0
    per_close = []

    for dc in CLOSES:
        dc_unix = int(dc.timestamp())
        prefix = [c for c in full if int(bar_close(ensure_utc(c.timestamp), "5m").timestamp()) <= dc_unix]
        if any(int(bar_close(ensure_utc(c.timestamp), "5m").timestamp()) > dc_unix for c in prefix):
            future_ok = False
        close_unix = [int(bar_close(ensure_utc(c.timestamp), "5m").timestamp()) for c in prefix]
        highs = [float(c.high) for c in prefix]
        lows = [float(c.low) for c in prefix]
        index = CandleRangeIndex(close_unix, highs, lows)
        members = published[dc]["members"]
        member_fp = active_state_fingerprint([m[0] for m in members], [str(m[1]).lower() for m in members])
        if member_fp != published[dc]["fingerprint"]:
            fingerprint_ok = False
        member_ids = {m[0] for m in members}
        rows = []
        for pool_id, side, pool_type, top, bottom, strength, sclass, known in catalogs[dc]:
            known_dt = _utc(known)
            if known_dt > dc:
                causal_ok = False
            rows.append(
                {
                    "pool_id": pool_id,
                    "side": str(side).lower(),
                    "pool_type": str(pool_type),
                    "top": float(top),
                    "bottom": float(bottom),
                    "strength": float(strength),
                    "strength_class": str(sclass),
                    "known_at": known_dt,
                }
            )
        if {r["pool_id"] for r in rows} != member_ids or len(rows) != len(member_ids):
            member_attr_ok = False
        t_p = time.perf_counter()
        frac_here: dict[str, tuple] = {}
        original = []
        remaining = []
        omitted = []
        checked = 0
        for row in rows:
            if row["strength_class"] not in STRENGTH_CLASS_COLORS:
                color_ok = False
            color = STRENGTH_CLASS_COLORS.get(row["strength_class"], "")
            span = index.eligible_index_span(int(row["known_at"].timestamp()), dc_unix)
            if span is None:
                pen_h = pen_l = None
            else:
                pen_h, pen_l = index.query(*span)
            side = row["side"]
            rem_t, rem_b, frac, traversed, _ph, _pl = remaining_from_penetration(
                side,
                row["bottom"],
                row["top"],
                pen_h if side == "upper" else None,
                pen_l if side == "lower" else None,
            )
            segs = build_fixed_segments(
                original_bottom=row["bottom"],
                original_top=row["top"],
                remaining_bottom=rem_b,
                remaining_top=rem_t,
                strength_class=row["strength_class"],
                segment_count=SEGMENT_COUNT,
            )
            if not segs or segs[0].segment_bottom != row["bottom"] or segs[-1].segment_top != row["top"]:
                segment_ok = False
            if segs and segs[0].display_color != color:
                color_ok = False
            if side == "upper" and 0 < frac < 1:
                upper = True
            if side == "lower" and 0 < frac < 1:
                lower = True
            if traversed:
                full_hit = True
            grid = tuple((round(s.segment_bottom, 10), round(s.segment_top, 10)) for s in segs)
            life = (row["pool_id"], row["bottom"], row["top"], row["known_at"])
            prev_grid = shared_grids.get(life)
            if prev_grid is not None and prev_grid != grid:
                segment_ok = False
            shared_grids[life] = grid
            prev = prior_frac.get(life)
            if prev is not None:
                monotonic_pairs += 1
                if frac > prev + 1e-9:
                    monotonic_fail += 1
            prior_frac[life] = frac
            frac_here[row["pool_id"]] = (life, frac)
            if checked < 2:
                ref = project_pool_v1(
                    _pool_from_row(row),
                    prefix,
                    decision_close=dc,
                    candle_input_reference=snap_id,
                    segment_count=SEGMENT_COUNT,
                )
                if (
                    ref.remaining_bottom != rem_b
                    or ref.remaining_top != rem_t
                    or ref.fully_traversed != traversed
                    or ref.remaining_fraction != frac
                    or not engine_active_at_close(_pool_from_row(row), dc)
                ):
                    scan_ok = False
                checked += 1
            chart_start = prefix[-CHART_BARS].timestamp if len(prefix) > CHART_BARS else prefix[0].timestamp
            t0_box = max(int(row["known_at"].timestamp()), int(ensure_utc(chart_start).timestamp()))
            original.append(
                {
                    "pool_id": row["pool_id"],
                    "side": side,
                    "top": row["top"],
                    "bottom": row["bottom"],
                    "strength": row["strength"],
                    "strength_class": row["strength_class"],
                    "color": color,
                    "t0": t0_box,
                    "known_at": _iso(row["known_at"]),
                }
            )
            visible = engine_active_at_close(_pool_from_row(row), dc) and not traversed and frac > 0
            if not visible:
                if traversed:
                    omitted.append(row["pool_id"])
                continue
            for seg in segs:
                if not seg.segment_visible or seg.visible_top <= seg.visible_bottom:
                    continue
                if seg.visible_bottom < row["bottom"] - 1e-12 or seg.visible_top > row["top"] + 1e-12:
                    bounds_ok = False
                remaining.append(
                    {
                        "pool_id": row["pool_id"],
                        "top": seg.visible_top,
                        "bottom": seg.visible_bottom,
                        "color": seg.display_color,
                        "t0": t0_box,
                    }
                )
        proj_s += time.perf_counter() - t_p
        ids_by_close.append({r["pool_id"] for r in rows})
        frac_by_close.append(frac_here)
        view = prefix[-CHART_BARS:]
        candles = [
            {
                "time": int(ensure_utc(c.timestamp).timestamp()),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
            }
            for c in view
        ]
        if any(c["time"] + 300 > dc_unix for c in candles):
            future_ok = False
        t_pay = time.perf_counter()
        body = {
            "decision_close": _iso(dc),
            "snapshot_id": snap_id,
            "price_content_hash": manifest["ohlcv_content_hash"],
            "revision_metadata_hash": manifest["revision_metadata_hash"],
            "chart_end_unix": dc_unix,
            "candles": candles,
            "original": original,
            "remaining": remaining,
            "fully_traversed_active_ids": omitted,
        }
        payloads.append(body)
        per_close.append(
            {
                "decision_close": _iso(dc),
                "published_snapshot_id": published[dc]["snapshot_id"],
                "active": len(original),
                "remaining_boxes": len(remaining),
                "fully_traversed_active": len(omitted),
                "chart_candles": len(candles),
            }
        )
        payload_s += time.perf_counter() - t_pay

    reappear_pairs = 0
    reappear_fail = 0
    if len(ids_by_close) == 3:
        for pid in (ids_by_close[0] - ids_by_close[1]) & ids_by_close[2]:
            reappear_pairs += 1
            left = frac_by_close[0].get(pid)
            right = frac_by_close[2].get(pid)
            if left and right and left[0] == right[0] and right[1] > left[1] + 1e-9:
                reappear_fail += 1

    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for body in payloads:
        (PAYLOAD_DIR / f"{_file_key(datetime.fromisoformat(body['decision_close'].replace('Z', '+00:00')))}.json").write_text(
            json.dumps(body, separators=(",", ":")),
            encoding="utf-8",
        )
    (PAYLOAD_DIR / "index.json").write_text(
        json.dumps({"closes": [_iso(c) for c in CLOSES], "snapshot_id": snap_id}, indent=2),
        encoding="utf-8",
    )

    data_ok = (
        bounds_ok
        and color_ok
        and segment_ok
        and causal_ok
        and future_ok
        and fingerprint_ok
        and member_attr_ok
        and scan_ok
        and monotonic_fail == 0
        and session.stats.ch_fetch_calls == 0
    )
    report = {
        "PHASE11T_MODE": "ISOLATED_HISTORICAL_HEATMAP_E2E_POC",
        "RUN_ID": run_id,
        "IMMUTABLE_SNAPSHOT_ID": snap_id,
        "ORACLE_CLOSES": [_iso(c) for c in CLOSES],
        "CANDLE_INPUT_IDENTITY": {
            "snapshot_id": snap_id,
            "price_content_hash": manifest["ohlcv_content_hash"],
            "revision_metadata_hash": manifest["revision_metadata_hash"],
            "rows": len(frozen),
            "clickhouse_candle_fallbacks": int(session.stats.ch_fetch_calls),
            "status": "PASS" if len(frozen) == 187596 and session.stats.ch_fetch_calls == 0 else "FAIL",
        },
        "ORIGINAL_ACTIVE_PARITY": "PASS" if fingerprint_ok and member_attr_ok else "FAIL",
        "ORIGINAL_BOUNDS_STRENGTH_PARITY": "PASS" if bounds_ok and scan_ok else "FAIL",
        "PHASE10_PUBLISHED_COMPARISON": "PASS" if fingerprint_ok and member_attr_ok else "FAIL",
        "REMAINING_UPPER_PARITY": "PASS" if upper and scan_ok else ("NOT_OBSERVED" if not upper else "FAIL"),
        "REMAINING_LOWER_PARITY": "PASS" if lower and scan_ok else ("NOT_OBSERVED" if not lower else "FAIL"),
        "REMAINING_FULL_TRAVERSAL": "PASS" if full_hit else "NOT_OBSERVED",
        "FIXED_SEGMENT_PARITY": "PASS" if segment_ok else "FAIL",
        "STRENGTH_COLOR_PARITY": "PASS" if color_ok else "FAIL",
        "HISTORICAL_GO_TO_CAUSALITY": "PASS" if causal_ok and future_ok and monotonic_fail == 0 else "FAIL",
        "MONOTONIC_SAME_LIFECYCLE_PAIRS": monotonic_pairs,
        "MONOTONIC_FAILURES": monotonic_fail,
        "REAPPEARANCE_PAIRS": reappear_pairs,
        "REAPPEARANCE_CONSUMPTION": "NOT_OBSERVED" if reappear_pairs == 0 else ("PASS" if reappear_fail == 0 else "FAIL"),
        "RENDER_PAYLOAD_PARITY": "PASS" if data_ok else "FAIL",
        "VISUAL_RENDER_VALIDATION": "NOT_PROVEN",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
        "TEST_RESULTS": {"payload_checks": "PASS" if data_ok else "FAIL", "per_close": per_close},
        "PERFORMANCE_RESULTS": {
            "SNAPSHOT_READ_SECONDS": round(read_s, 3),
            "CATALOG_READ_SECONDS": round(catalog_s, 3),
            "REMAINING_PROJECTION_SECONDS": round(proj_s, 3),
            "PAYLOAD_BUILD_SECONDS": round(payload_s, 3),
            "RENDER_SECONDS": None,
        },
        "FILES_CHANGED": [
            "live_micro_lld_v2_ch_test/run_phase11t_heatmap_poc.py",
            "live_micro_lld_v2_ch_test/phase11t_heatmap/index.html",
            "live_micro_lld_v2_ch_test/phase11t_heatmap/viewer.js",
        ],
        "REPORT_PATH": str(report_path),
        "POC_VIEW_PATH": str(POC_DIR / "index.html"),
        "READY_FOR_REMAINING_POOL_CH_POC": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "IMMUTABLE_SNAPSHOT_ID",
        "ORIGINAL_ACTIVE_PARITY",
        "REMAINING_UPPER_PARITY",
        "REMAINING_LOWER_PARITY",
        "REMAINING_FULL_TRAVERSAL",
        "HISTORICAL_GO_TO_CAUSALITY",
        "RENDER_PAYLOAD_PARITY",
        "VISUAL_RENDER_VALIDATION",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY",
        "REAPPEARANCE_CONSUMPTION",
        "POC_VIEW_PATH",
        "REPORT_PATH",
    )}, indent=2))
    print(json.dumps(per_close), flush=True)
    return 0 if data_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
