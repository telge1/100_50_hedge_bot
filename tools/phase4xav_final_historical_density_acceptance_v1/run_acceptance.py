#!/usr/bin/env python3
"""Phase 4X-AV read-only acceptance (CH + build_landscape_window)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase4xav_final_historical_density_acceptance_v1"
SPREAD = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester/short/dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

PIPELINE_5M_PD1 = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
PILOT_LOOKUP = "provenance_lookup_v1:185ddfc1ca31f9bc"
PILOT_FP_BASELINE = 18053975130951660279
EXPECTED = {
    "2026-05": 8928,
    "2026-06": 8640,
    "2026-07": 8928,
    "2026-08": 8928,
    "2026-09": 8640,
}
LANDSCAPE_ANCHORS = [
    "2026-05-15",
    "2026-06-15",
    "2026-07-15",
    "2026-08-15",
    "2026-09-10",
    "2026-09-12",
    "2026-09-15",
    "2026-09-16",
    "2026-09-17",
    "2026-09-25",
    "2026-09-30",
]
WARMUP_H = 168
VIS_H = 8


def _setup_path() -> None:
    # SPREAD last so its research_charts (5m viewer contract) wins over pools copy.
    for p in (TRP, ROOT, POOLS_DASH, SPREAD.parent, SPREAD):
        if p.is_dir():
            ps = str(p)
            if ps not in sys.path:
                sys.path.insert(0, ps)
    os.environ.setdefault("HISTORICAL_LMV2_BATCH_READER", "1")
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    start = f"{y:04d}-{m:02d}-01"
    if m == 12:
        end = f"{y + 1:04d}-01-01"
    else:
        end = f"{y:04d}-{m + 1:02d}-01"
    return start, end


def publish_integrity() -> dict:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    ch = config_hash(build_live_micro_lld_v2_config(1))
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    out: dict = {"months": {}, "total": 0, "issues": []}
    try:
        for month, exp in EXPECTED.items():
            s, e = month_bounds(month)
            rows = client.query(
                f"""
                SELECT decision_close, count() AS n
                FROM {db}.{TABLE_PUBLISH} FINAL
                WHERE pipeline_line_id = {{lid:String}}
                  AND config_hash = {{ch:String}}
                  AND publish_complete = 1
                  AND decision_close >= {{s:String}} AND decision_close < {{e:String}}
                GROUP BY decision_close
                HAVING n > 1
                ORDER BY decision_close
                LIMIT 20
                """,
                parameters={"lid": PIPELINE_5M_PD1, "ch": ch, "s": s, "e": e},
            ).result_rows
            dupes = len(rows)
            agg = client.query(
                f"""
                SELECT
                  count() AS n,
                  min(decision_close) AS first_dc,
                  max(decision_close) AS last_dc,
                  countDistinct(decision_close) AS distinct_dc
                FROM {db}.{TABLE_PUBLISH} FINAL
                WHERE pipeline_line_id = {{lid:String}}
                  AND config_hash = {{ch:String}}
                  AND publish_complete = 1
                  AND decision_close >= {{s:String}} AND decision_close < {{e:String}}
                """,
                parameters={"lid": PIPELINE_5M_PD1, "ch": ch, "s": s, "e": e},
            ).result_rows[0]
            n, first_dc, last_dc, distinct_dc = int(agg[0]), agg[1], agg[2], int(agg[3])
            missing = exp - distinct_dc if distinct_dc <= exp else 0
            out["months"][month] = {
                "expected": exp,
                "rows": n,
                "distinct_closes": distinct_dc,
                "first_close_utc": str(first_dc),
                "last_close_utc": str(last_dc),
                "duplicate_close_groups": dupes,
                "ok": distinct_dc == exp and n == exp and dupes == 0,
            }
            if not out["months"][month]["ok"]:
                out["issues"].append(f"{month}: distinct={distinct_dc} rows={n} dup_groups={dupes}")
            out["total"] += distinct_dc

        pilot_pub = client.query(
            f"""
            SELECT count() FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= '2026-09-09' AND decision_close < '2026-09-18'
            """,
            parameters={"lid": PIPELINE_5M_PD1, "ch": ch},
        ).result_rows[0][0]
        pilot_side = client.query(
            f"""
            SELECT count(), cityHash64(groupArray(tuple(
              snapshot_id, dest_sha256, frozen_price_hash, provenance_lookup_version
            )))
            FROM {db}.research_lmv2_snapshot_import_provenance_v1
            WHERE pipeline_line_id = {{lid:String}}
              AND provenance_lookup_version = {{lk:String}}
              AND relative_json_path LIKE '%/2026-09/5m_PD1/%'
            """,
            parameters={"lid": PIPELINE_5M_PD1, "lk": PILOT_LOOKUP},
        ).result_rows[0]
        out["pilot"] = {
            "publish_window": int(pilot_pub),
            "sidecar_count": int(pilot_side[0]),
            "fingerprint": int(pilot_side[1]),
            "fingerprint_ok": int(pilot_side[1]) == PILOT_FP_BASELINE,
            "ok": int(pilot_pub) == 2592 and int(pilot_side[0]) == 2592,
        }
        if not out["pilot"]["ok"]:
            out["issues"].append("pilot_counts")
        if not out["pilot"]["fingerprint_ok"]:
            out["issues"].append("pilot_fingerprint_drift")

        # sidecar vs publish for Sep sample
        sidecar_n = client.query(
            f"""
            SELECT count() FROM {db}.research_lmv2_snapshot_import_provenance_v1
            WHERE pipeline_line_id = {{lid:String}}
              AND relative_json_path LIKE '%/2026-09/5m_PD1/%'
            """,
            parameters={"lid": PIPELINE_5M_PD1},
        ).result_rows[0][0]
        out["sep_sidecar_rows"] = int(sidecar_n)
        out["ok"] = not out["issues"] and out["total"] == 44064
    finally:
        client.close()
    return out


def _window_for_anchor(day: str) -> tuple[str, str]:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    vis_from = d.replace(hour=12, minute=0, second=0)
    vis_to = vis_from + timedelta(hours=VIS_H)
    return (
        vis_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        vis_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _pool_stats(pools: list[dict]) -> dict:
    lower = upper = 0
    with_origin = 0
    for p in pools or []:
        side = (p.get("side") or "").lower()
        if side == "lower":
            lower += 1
        elif side == "upper":
            upper += 1
        if p.get("origin") or p.get("pool_id"):
            with_origin += 1
    return {"lower_pools": lower, "upper_pools": upper, "pools_with_origin_id": with_origin}


def landscape_tests() -> dict:
    from pd1_landscape_viewer_v1.historical_window_v1 import (
        LandscapeWindowError,
        WARMUP_HOURS,
        build_landscape_window,
    )

    results = []
    for day in LANDSCAPE_ANCHORS:
        vf, vt = _window_for_anchor(day)
        entry = {"anchor_day": day, "from_utc": vf, "to_utc": vt, "visible_hours": VIS_H}
        try:
            w1 = build_landscape_window(vf, vt, timeframe="5m", pool_detail=1)
            w2 = build_landscape_window(vf, vt, timeframe="5m", pool_detail=1)
            h1 = hashlib.sha256(json.dumps(w1, sort_keys=True, default=str).encode()).hexdigest()
            h2 = hashlib.sha256(json.dumps(w2, sort_keys=True, default=str).encode()).hexdigest()
            pools = w1.get("pools") or []
            stats = _pool_stats(pools)
            ctx = w1.get("window", {}).get("context_from_utc")
            prov = w1.get("provenance", {})
            entry.update(
                {
                    "ok": True,
                    "pipeline": prov.get("pipeline_line_id"),
                    "timeframe": prov.get("timeframe"),
                    "pool_detail": prov.get("pool_detail"),
                    "context_from_utc": ctx,
                    "warmup_hours_expected": WARMUP_HOURS,
                    "price_bars": len(w1.get("prices") or []),
                    "pool_segments_total": len(pools),
                    **stats,
                    "deterministic_hash_match": h1 == h2,
                    "hash": h1[:16],
                }
            )
            # warmup check
            vf_dt = datetime.strptime(vf, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ctx_dt = datetime.strptime(ctx, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            entry["context_hours"] = round((vf_dt - ctx_dt).total_seconds() / 3600, 2)
            entry["warmup_ok"] = entry["context_hours"] >= WARMUP_HOURS - 0.1
        except LandscapeWindowError as exc:
            entry.update({"ok": False, "error_code": exc.code, "error": exc.message, "details": exc.details})
        except Exception as exc:
            entry.update({"ok": False, "error_code": type(exc).__name__, "error": str(exc)[:500]})
        results.append(entry)
    return {
        "cases": results,
        "all_ok": all(c.get("ok") for c in results),
        "missing_publication": [c for c in results if c.get("error_code") == "MISSING_PUBLICATION"],
    }


def may_warmup_april_probe() -> dict:
    """Early May: is 168h context fully inside May publish or need April?"""
    from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window, LandscapeWindowError
    from research_charts.historical_pool_state_v1 import get_published_decision_closes
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash

    ch = config_hash(build_live_micro_lld_v2_config(1))
    vf, vt = _window_for_anchor("2026-05-15")
    vf_dt = datetime.strptime(vf, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    need_from = vf_dt - timedelta(hours=WARMUP_H)
    reader = PublishedActiveReader()
    pubs = sorted(
        get_published_decision_closes(reader, ch, pipeline_line_id=PIPELINE_5M_PD1),
        key=lambda d: d,
    )
    pub_dt = [d if d.tzinfo else d.replace(tzinfo=timezone.utc) for d in pubs]
    before_may = [d for d in pub_dt if d < datetime(2026, 5, 1, tzinfo=timezone.utc)]
    in_warmup = [d for d in pub_dt if need_from <= d < vf_dt]
    probe = {"warmup_need_from": need_from.isoformat(), "visible_from": vf}
    try:
        build_landscape_window(vf, vt, timeframe="5m", pool_detail=1)
        probe["window_build_ok"] = True
    except LandscapeWindowError as exc:
        probe["window_build_ok"] = False
        probe["error"] = {"code": exc.code, "message": exc.message}
    probe["april_publish_closes_before_may"] = len(before_may)
    probe["published_closes_in_warmup_band"] = len(in_warmup)
    probe["note"] = (
        "Apr publications not required for May-15 probe if window_build_ok and warmup band covered."
        if probe.get("window_build_ok")
        else "May early context may lack pre-May publications."
    )
    return probe


def landscape_15m_regression() -> dict:
    from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window

    vf, vt = "2026-09-14T21:45:00Z", "2026-09-15T05:45:00Z"
    w = build_landscape_window(vf, vt, timeframe="15m", pool_detail=1)
    prov = w.get("provenance", {})
    return {
        "ok": prov.get("timeframe") == "15m"
        and prov.get("pipeline_line_id") == "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1"
        and len(w.get("prices") or []) == 97,
        "prices": len(w.get("prices") or []),
        "pools": len(w.get("pools") or []),
        "pipeline": prov.get("pipeline_line_id"),
    }


def research_chart_static() -> dict:
    dash = SPREAD if (SPREAD / "static/js/research/research_charts.js").is_file() else POOLS_DASH
    checks = {}
    for rel in (
        "static/js/research/pd1_exact_origin_density_research_v1.js",
        "static/js/research/pd1_exact_density_trp_adapter.js",
        "static/js/research/research_charts.js",
    ):
        p = dash / rel
        checks[rel] = {"exists": p.is_file(), "path": str(p)}
    js = (dash / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8") if checks[
        "static/js/research/pd1_exact_origin_density_research_v1.js"
    ]["exists"] else ""
    checks["api_window_endpoint"] = "/live-charts/viewer/api/window" in js
    checks["warmup_168_mentioned"] = "168" in js
    checks["fail_closed_hint"] = "window load failed" in js or "prior data retained" in js
    checks["ok"] = all(
        [
            checks["static/js/research/pd1_exact_origin_density_research_v1.js"]["exists"],
            checks["api_window_endpoint"],
        ]
    )
    checks["ui_verified"] = False
    return checks


def jun_sep_progress() -> dict:
    p = ROOT / "results/phase4xao_jun_sep_nohup_rollout_v1/progress.json"
    state = json.loads(p.read_text(encoding="utf-8"))
    months = {}
    for m in ("2026-06", "2026-07", "2026-08", "2026-09"):
        mm = state["months"][m]
        months[m] = {
            "validation": mm.get("validation"),
            "publish": mm.get("publish"),
            "verified_close_count": mm.get("verified_close_count"),
        }
    return {
        "final_verdict": state.get("final_verdict"),
        "updated_at_utc": state.get("updated_at_utc"),
        "months": months,
        "ok": state.get("final_verdict") == "JUN_SEP_DENSITY_PUBLISH_COMPLETE"
        and all(months[m]["publish"] == "MONTH_PUBLISH_PASS" for m in months),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _setup_path()
    report: dict = {"generated_utc": datetime.now(timezone.utc).isoformat()}
    t0 = time.perf_counter()
    report["publish_integrity"] = publish_integrity()
    report["jun_sep_progress"] = jun_sep_progress()
    report["may_warmup"] = may_warmup_april_probe()
    report["landscape_5m"] = landscape_tests()
    report["landscape_15m"] = landscape_15m_regression()
    report["research_chart_static"] = research_chart_static()
    report["elapsed_s"] = round(time.perf_counter() - t0, 1)

    blockers = []
    if not report["publish_integrity"].get("ok"):
        blockers.append("publish_integrity")
    if not report["landscape_5m"].get("all_ok"):
        blockers.append("landscape_5m")
    if not report["landscape_15m"].get("ok"):
        blockers.append("landscape_15m")
    if not report["jun_sep_progress"].get("ok"):
        blockers.append("jun_sep_progress")
    report["blockers"] = blockers
    if blockers:
        report["verdict"] = "FINAL_ACCEPTANCE_BLOCKED"
    elif not report["research_chart_static"].get("ui_verified"):
        report["verdict"] = "FINAL_ACCEPTANCE_PASS_WITH_UI_NOT_VERIFIED"
    else:
        report["verdict"] = "FINAL_ACCEPTANCE_PASS"
    (OUT / "acceptance_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "blockers": blockers, "elapsed_s": report["elapsed_s"]}, indent=2))
    return 0 if not blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())
