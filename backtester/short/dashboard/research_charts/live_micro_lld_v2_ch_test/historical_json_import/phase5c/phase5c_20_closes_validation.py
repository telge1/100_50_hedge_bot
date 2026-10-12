#!/usr/bin/env python3
"""Phase 5C — 20-close causal validation window (DOGEUSDT 15m PD10, read-only CH)."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_visibility import (
    LIVE_TREATS_BACKFILL_AS_SIGNAL,
    NEW_BATCH_PUBLISH_WITHOUT_SIDECAR,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    AdapterStop,
    CandleCache,
    CatalogRow,
    ClickHousePublicationSource,
    MemberRow,
    PublicationHit,
    candle_window_status,
    load_historical_remaining,
    resolve_publication,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_c.coverage import (
    base_file_status,
    decision_1m_opens,
    warmup_source_start,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_e.timeline import (
    TimelineError,
    TimelineResult,
    index_from_candles,
    observe_close,
    project_close,
    removal_classification,
)

LMV2_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = _REPO / "logs" / "historical_import" / "overnight_2026"
OUT_PATH = LOG_DIR / "phase5c_20_closes_validation.json"

SYMBOL = "DOGEUSDT"
TIMEFRAME = "15m"
POOL_DETAIL = 10
PIPELINE_LINE_ID = "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1"
PILOT_LINE_ID = "historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1"
WINDOW_START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 1, 4, 45, tzinfo=timezone.utc)
CLOSE_COUNT = 20

IMMUTABLE_SNAPSHOT_ID = "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93"
IMMUTABLE_PRICE_HASH = "d489aa686f6f648850f248d4809338abbee0d7ab6f0bda3ec5ffe3e698757f48"
IMMUTABLE_REVISION_HASH = "9a3ba3066376d27aabc81ffe01b9b08741e819ba5095bf895f8dd0125812258e"
SNAPSHOT_ROOT = LMV2_ROOT

WINDOW_INDEPENDENCE_SPOT_ISO = (
    "2026-08-01T00:00:00Z",
    "2026-08-01T02:00:00Z",
    "2026-08-01T04:45:00Z",
)


@dataclass
class LineFilteredPublicationSource:
    inner: ClickHousePublicationSource
    pipeline_line_id: str

    def find(
        self,
        *,
        symbol: str,
        timeframe: str,
        pool_detail: int,
        decision_close: datetime,
    ) -> list[PublicationHit]:
        hits = self.inner.find(
            symbol=symbol,
            timeframe=timeframe,
            pool_detail=pool_detail,
            decision_close=decision_close,
        )
        return [h for h in hits if h.pipeline_line_id == self.pipeline_line_id]

    def members(self, snapshot_id: str) -> list[MemberRow]:
        return self.inner.members(snapshot_id)

    def catalog(self, snapshot_id: str) -> list[CatalogRow]:
        return self.inner.catalog(snapshot_id)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_close(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def expected_closes() -> list[datetime]:
    out: list[datetime] = []
    cur = WINDOW_START
    while cur <= WINDOW_END:
        out.append(cur)
        cur += timedelta(minutes=15)
    if len(out) != CLOSE_COUNT:
        raise RuntimeError(f"expected {CLOSE_COUNT} closes, got {len(out)}")
    return out


def _load_immutable_manifest() -> dict[str, Any]:
    path = (
        LMV2_ROOT
        / "doge_apr_sep_2026_v1_history"
        / "inputs"
        / f"{IMMUTABLE_SNAPSHOT_ID}.manifest.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_time(manifest: dict, key: str) -> datetime:
    raw = str(manifest[key])
    if "." in raw:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _rss_mb() -> float:
    rss = 0.0
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) / 1024
                break
    except OSError:
        pass
    return rss


def _pool_map(catalog: list[dict]) -> dict[str, dict]:
    return {row["pool_id"]: row for row in catalog}


def _active_delta(prev: dict[str, dict], curr: dict[str, dict]) -> dict[str, Any]:
    prev_ids = set(prev)
    curr_ids = set(curr)
    entries = sorted(curr_ids - prev_ids)
    exits = sorted(prev_ids - curr_ids)
    changes = []
    for pid in sorted(prev_ids & curr_ids):
        a, b = prev[pid], curr[pid]
        if (
            a.get("top") != b.get("top")
            or a.get("bottom") != b.get("bottom")
            or a.get("strength") != b.get("strength")
            or a.get("strength_class") != b.get("strength_class")
            or a.get("known_at") != b.get("known_at")
        ):
            changes.append(pid)
    return {
        "entries": len(entries),
        "exits": len(exits),
        "bound_or_strength_changes": len(changes),
        "entry_pool_ids_sample": entries[:5],
        "exit_pool_ids_sample": exits[:5],
        "change_pool_ids_sample": changes[:5],
    }


def _remaining_fingerprint(rem: dict) -> str:
    rem_rows = sorted(
        (r["pool_id"], r["top"], r["bottom"], r.get("color"))
        for r in rem.get("remaining") or []
    )
    act_rows = sorted(
        (a["pool_id"], a["top"], a["bottom"], a["strength"], a["known_at"])
        for a in rem.get("active") or []
    )
    payload = {
        "snapshot_id": rem.get("published_snapshot_id"),
        "active": act_rows,
        "remaining": rem_rows,
        "traversed": sorted(rem.get("fully_traversed_active_ids") or []),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def load_catalog_lifecycle(client, database: str, snapshot_id: str) -> list[dict]:
    rows = client.query(
        f"""
        SELECT pool_id, side, top_price, bottom_price, strength, strength_class,
               known_at, break_at, confirmed_at, invalidated_at
        FROM {database}.research_lmv2_snapshot_active_pool_v1
        WHERE snapshot_id = {{sid:String}}
        ORDER BY pool_id
        """,
        parameters={"sid": snapshot_id},
    ).result_rows
    out = []
    for r in rows:
        out.append(
            {
                "pool_id": str(r[0]),
                "side": str(r[1]).lower(),
                "top": float(r[2]),
                "bottom": float(r[3]),
                "strength": float(r[4]),
                "strength_class": str(r[5]),
                "known_at": _iso(r[6]) if r[6] is not None else None,
                "break_at": _iso(r[7]) if r[7] is not None else None,
                "confirmed_at": _iso(r[8]) if r[8] is not None else None,
                "invalidated_at": _iso(r[9]) if r[9] is not None else None,
            }
        )
    return out


def verify_publications(
    client,
    database: str,
    cfg_hash: str,
    closes: list[datetime],
) -> dict[str, Any]:
    matrix = []
    status = "PASS"
    for dc in closes:
        rows = client.query(
            f"""
            SELECT pipeline_line_id, snapshot_id, active_state_fingerprint, publish_complete
            FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE symbol = {{sym:String}}
              AND timeframe = {{tf:String}}
              AND pool_detail = {{pd:UInt8}}
              AND config_hash = {{ch:String}}
              AND decision_close = {{dc:DateTime64(3, 'UTC')}}
              AND publish_complete = 1
            """,
            parameters={
                "sym": SYMBOL,
                "tf": TIMEFRAME,
                "pd": POOL_DETAIL,
                "ch": cfg_hash,
                "dc": dc,
            },
        ).result_rows
        on_line = [r for r in rows if str(r[0]) == PIPELINE_LINE_ID]
        pilot = [r for r in rows if str(r[0]) == PILOT_LINE_ID]
        row_status = "PASS"
        if len(on_line) != 1:
            row_status = "FAIL"
            status = "FAIL"
        if len(rows) > 1 and pilot:
            pass  # documented, filtered out in reads
        matrix.append(
            {
                "decision_close": _iso(dc),
                "status": row_status,
                "production_snapshot_id": str(on_line[0][1]) if len(on_line) == 1 else None,
                "active_state_fingerprint": str(on_line[0][2]) if len(on_line) == 1 else None,
                "publish_rows_total": len(rows),
                "pilot_line_rows": len(pilot),
            }
        )
    return {
        "status": status,
        "close_count": len(closes),
        "matrix": matrix,
    }


def coverage_for_close(
    manifest: dict,
    catalog: list[CatalogRow],
    decision_close: datetime,
) -> dict[str, Any]:
    rs = _manifest_time(manifest, "range_start")
    re = _manifest_time(manifest, "range_end_exclusive")
    adapter_status = candle_window_status(
        range_start=rs,
        range_end_exclusive=re,
        decision_close=decision_close,
        known_ats=[row.known_at for row in catalog],
        timeframe=TIMEFRAME,
    )
    warmup_start = warmup_source_start(decision_close, TIMEFRAME, POOL_DETAIL)
    bar_first, bar_last = decision_1m_opens(decision_close, TIMEFRAME)
    prefix_status = base_file_status(
        decision_close,
        TIMEFRAME,
        POOL_DETAIL,
        range_start=rs,
        range_end_exclusive=re,
    )
    ok = adapter_status == "READY" and prefix_status == "READY"
    return {
        "status": "PASS" if ok else "NOT_VERIFIED",
        "candle_window_status": adapter_status,
        "warmup_prefix_status": prefix_status,
        "warmup_source_start_1m_open": _iso(warmup_start),
        "decision_bar_first_1m_open": _iso(bar_first),
        "decision_bar_last_1m_open": _iso(bar_last),
        "warmup_before_window_start": warmup_start < WINDOW_START,
    }


def research_visibility_audit(client, database: str, snapshot_ids: list[str]) -> dict[str, Any]:
    """Document gates; label historical publishes as RESEARCH_HISTORICAL in report metadata only."""
    sample = snapshot_ids[:3]
    sidecar_rows = []
    for sid in sample:
        rows = client.query(
            f"""
            SELECT snapshot_id, pipeline_line_id, import_batch_id, relative_json_path, validated_at
            FROM {database}.research_lmv2_snapshot_import_provenance_v1
            WHERE snapshot_id = {{sid:String}}
              AND pipeline_line_id = {{lid:String}}
            LIMIT 1
            """,
            parameters={"sid": sid, "lid": PIPELINE_LINE_ID},
        ).result_rows
        if rows:
            r = rows[0]
            sidecar_rows.append(
                {
                    "snapshot_id": str(r[0]),
                    "import_batch_id": str(r[2]),
                    "relative_json_path": str(r[3]),
                    "validated_at": str(r[4]),
                }
            )
    enum_in_ch = client.query(
        f"""
        SELECT count()
        FROM system.columns
        WHERE database = {{db:String}}
          AND table = 'research_lmv2_active_snapshot_publish_v1'
          AND name IN ('ingestion_mode', 'visibility_time', 'visibility_mode')
        """,
        parameters={"db": database},
    ).result_rows[0][0]
    return {
        "status": "NOT_VERIFIED",
        "required_label": "RESEARCH_HISTORICAL",
        "live_treats_backfill_as_signal": LIVE_TREATS_BACKFILL_AS_SIGNAL,
        "new_batch_without_sidecar": NEW_BATCH_PUBLISH_WITHOUT_SIDECAR,
        "publish_table_visibility_columns": int(enum_in_ch),
        "sidecar_sample": sidecar_rows,
        "sidecar_present_for_sample": len(sidecar_rows) == len(sample),
        "note": (
            "Readers expose published historical state; no enforced RESEARCH_HISTORICAL enum at read time. "
            "Report marks this run as RESEARCH_HISTORICAL; UI gate still NOT_VERIFIED."
        ),
    }


def window_independence_check(
    reader: PublishedActiveReader,
    filtered: LineFilteredPublicationSource,
    cfg_hash: str,
    manifest: dict,
    spot_isos: tuple[str, ...],
) -> dict[str, Any]:
    """Isolated vs batch remaining; duplicate ACTIVE reads. No display-bounds API on 15m line."""
    results = []
    cache_batch = CandleCache(max_entries=4)
    cache_iso = CandleCache(max_entries=1)

    for iso in spot_isos:
        dc = _parse_close(iso)
        entry: dict[str, Any] = {"decision_close": iso}

        a1 = reader.load_published(PIPELINE_LINE_ID, dc, config_hash=cfg_hash)
        a2 = reader.load_published(PIPELINE_LINE_ID, dc, config_hash=cfg_hash)
        active_fp = hashlib.sha256(
            json.dumps(a1["pool_ids"], sort_keys=True).encode()
        ).hexdigest()
        entry["active_repeat_read"] = {
            "status": "PASS" if active_fp == hashlib.sha256(json.dumps(a2["pool_ids"], sort_keys=True).encode()).hexdigest() else "FAIL",
            "fingerprint_match": a1["active_state_fingerprint"] == a2["active_state_fingerprint"],
        }

        rem_batch = load_historical_remaining(
            filtered,
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            pool_detail=POOL_DETAIL,
            decision_close=dc,
            immutable_candle_snapshot_id=IMMUTABLE_SNAPSHOT_ID,
            price_content_hash=IMMUTABLE_PRICE_HASH,
            revision_metadata_hash=IMMUTABLE_REVISION_HASH,
            snapshot_root=SNAPSHOT_ROOT,
            cache=cache_batch,
        )
        rem_iso = load_historical_remaining(
            filtered,
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            pool_detail=POOL_DETAIL,
            decision_close=dc,
            immutable_candle_snapshot_id=IMMUTABLE_SNAPSHOT_ID,
            price_content_hash=IMMUTABLE_PRICE_HASH,
            revision_metadata_hash=IMMUTABLE_REVISION_HASH,
            snapshot_root=SNAPSHOT_ROOT,
            cache=cache_iso,
        )
        fp_b = _remaining_fingerprint(rem_batch)
        fp_i = _remaining_fingerprint(rem_iso)
        entry["remaining_batch_vs_isolated"] = {
            "status": "PASS" if fp_b == fp_i else "FAIL",
            "fingerprint": fp_b,
        }

        hits = filtered.find(
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            pool_detail=POOL_DETAIL,
            decision_close=dc,
        )
        cat = filtered.catalog(hits[0].snapshot_id)
        cov = coverage_for_close(manifest, cat, dc)
        entry["warmup_prefix_unchanged_by_display_window"] = {
            "status": "PASS" if cov.get("warmup_before_window_start") else "NOT_VERIFIED",
            "warmup_source_start": cov.get("warmup_source_start_1m_open"),
        }
        results.append(entry)

    display_bounds_api = {
        "status": "NOT_VERIFIED",
        "reason": (
            "PublishedActiveReader and load_historical_remaining do not accept chart display_bounds "
            "for Apr–Sep 15m PD10; phase11u_f CHART_BARS is 5m-only. Independence proven via "
            "isolated-vs-batch and duplicate-read only."
        ),
    }
    structural = all(
        r["active_repeat_read"]["status"] == "PASS"
        and r["remaining_batch_vs_isolated"]["status"] == "PASS"
        for r in results
    )
    return {
        "status": "PASS" if structural else "FAIL",
        "spot_checks": results,
        "display_bounds_comparison": display_bounds_api,
    }


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    rss0 = _rss_mb()

    cfg = build_live_micro_lld_v2_config(POOL_DETAIL)
    cfg_hash = config_hash(cfg)
    manifest = _load_immutable_manifest()
    closes = expected_closes()

    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database

    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5C",
        "title": "20-close validation — DOGEUSDT 15m PD10",
        "generated_at_utc": _utc_now(),
        "read_only": True,
        "visibility_mode": "RESEARCH_HISTORICAL",
        "scope": {
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "pool_detail": POOL_DETAIL,
            "pipeline_line_id": PIPELINE_LINE_ID,
            "window_start": _iso(WINDOW_START),
            "window_end": _iso(WINDOW_END),
            "close_count": CLOSE_COUNT,
            "immutable_candle_snapshot_id": IMMUTABLE_SNAPSHOT_ID,
        },
        "checks": {},
        "coverage_matrix": [],
        "active_timeline": {"per_close": [], "deltas_between_closes": []},
        "remaining_timeline": {"per_close": [], "deltas_between_closes": []},
        "performance": {},
        "next_step_recommendation": None,
        "summary": {},
    }

    pub = verify_publications(client, database, cfg_hash, closes)
    report["checks"]["publication_20_closes"] = pub
    if pub["status"] == "FAIL":
        report["summary"] = {"overall": "FAIL", "pass": 0, "fail": 1, "not_verified": 0}
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        client.close()
        return 1

    reader = PublishedActiveReader()
    inner = ClickHousePublicationSource(client, database)
    filtered = LineFilteredPublicationSource(inner, PIPELINE_LINE_ID)
    cache = CandleCache(max_entries=4)

    active_status = "PASS"
    remaining_status = "PASS"
    causality_violations = 0
    prev_active_map: dict[str, dict] = {}
    prev_rem_fp: str | None = None
    prev_rem_segments = 0
    prev_traversed = 0
    snapshot_ids: list[str] = []
    rem_payloads: list[dict] = []
    catalogs_by_close: list[tuple[datetime, list[CatalogRow]]] = []

    for dc in closes:
        iso = _iso(dc)
        row: dict[str, Any] = {"decision_close": iso}

        try:
            active = reader.load_published(PIPELINE_LINE_ID, dc, config_hash=cfg_hash)
            snapshot_ids.append(active["snapshot_id"])
            catalog = load_catalog_lifecycle(client, database, active["snapshot_id"])
            late = [
                p["pool_id"]
                for p in catalog
                if p.get("known_at") and _parse_close(p["known_at"]) > dc
            ]
            if late:
                causality_violations += len(late)
                active_status = "FAIL"
            curr_map = _pool_map(catalog)
            if prev_active_map:
                report["active_timeline"]["deltas_between_closes"].append(
                    {"after_close": iso, **_active_delta(prev_active_map, curr_map)}
                )
            prev_active_map = curr_map
            report["active_timeline"]["per_close"].append(
                {
                    "decision_close": iso,
                    "snapshot_id": active["snapshot_id"],
                    "active_count": len(active["pool_ids"]),
                    "active_state_fingerprint": active["active_state_fingerprint"],
                    "prefix_candle_fingerprint": active["prefix_candle_fingerprint"],
                }
            )
            row["active"] = "PASS"
        except Exception as exc:
            active_status = "FAIL"
            row["active"] = f"FAIL: {exc}"

        hits = filtered.find(
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            pool_detail=POOL_DETAIL,
            decision_close=dc,
        )
        if len(hits) != 1:
            row["publication_filter"] = "FAIL"
            remaining_status = "FAIL"
        else:
            row["publication_filter"] = "PASS"
            cat_objs = filtered.catalog(hits[0].snapshot_id)
            cov = coverage_for_close(manifest, cat_objs, dc)
            row.update(cov)
            if cov["status"] != "PASS":
                remaining_status = "NOT_VERIFIED"
                row["remaining"] = "NOT_VERIFIED"
            else:
                try:
                    resolve_publication(hits)
                    rem = load_historical_remaining(
                        filtered,
                        symbol=SYMBOL,
                        timeframe=TIMEFRAME,
                        pool_detail=POOL_DETAIL,
                        decision_close=dc,
                        immutable_candle_snapshot_id=IMMUTABLE_SNAPSHOT_ID,
                        price_content_hash=IMMUTABLE_PRICE_HASH,
                        revision_metadata_hash=IMMUTABLE_REVISION_HASH,
                        snapshot_root=SNAPSHOT_ROOT,
                        cache=cache,
                    )
                    rem_payloads.append(rem)
                    catalogs_by_close.append((dc, cat_objs))
                    fp = _remaining_fingerprint(rem)
                    traversed = len(rem.get("fully_traversed_active_ids") or [])
                    seg = rem["remaining_segment_count"]
                    if prev_rem_fp is not None:
                        report["remaining_timeline"]["deltas_between_closes"].append(
                            {
                                "after_close": iso,
                                "segment_count_delta": seg - prev_rem_segments,
                                "fully_traversed_delta": traversed - prev_traversed,
                                "fingerprint_changed": fp != prev_rem_fp,
                            }
                        )
                    prev_rem_fp = fp
                    prev_rem_segments = seg
                    prev_traversed = traversed
                    report["remaining_timeline"]["per_close"].append(
                        {
                            "decision_close": iso,
                            "snapshot_id": rem["published_snapshot_id"],
                            "active_count": rem["active_count"],
                            "remaining_segment_count": seg,
                            "fully_traversed_count": traversed,
                            "fingerprint": fp,
                        }
                    )
                    row["remaining"] = "PASS"
                except AdapterStop as exc:
                    remaining_status = "NOT_VERIFIED"
                    row["remaining"] = f"NOT_VERIFIED: {exc.status}"

        report["coverage_matrix"].append(row)

    report["checks"]["active_layer"] = {"status": active_status}
    report["checks"]["causality_known_at"] = {
        "status": "PASS" if causality_violations == 0 else "FAIL",
        "violations": causality_violations,
    }
    report["checks"]["remaining_layer"] = {"status": remaining_status}

    timeline_status = "NOT_VERIFIED"
    timeline_detail: dict[str, Any] = {}
    if len(rem_payloads) == len(closes):
        try:
            cache_key = (
                IMMUTABLE_SNAPSHOT_ID,
                IMMUTABLE_PRICE_HASH,
                IMMUTABLE_REVISION_HASH,
                SYMBOL,
                TIMEFRAME,
            )
            candles = cache._store[cache_key]["candles"]
            index = index_from_candles(candles, TIMEFRAME)
            book = TimelineResult()
            for dc, cat in catalogs_by_close:
                obs = project_close(index, cat, dc)
                observe_close(book, dc, obs)
            failures = (
                book.upper_monotonic_failures
                + book.lower_monotonic_failures
                + book.strength_failures
                + book.causality_failures
                + book.fixed_segment_failures
            )
            timeline_status = "PASS" if failures == 0 else "FAIL"
            timeline_detail = {
                "processed_closes": book.processed,
                "close_digest": book.close_digest,
                "active_to_inactive": book.active_to_inactive,
                "reappearance": book.reappearance,
                "bounds_lifecycle_changes": book.bounds_lifecycle_changes,
                "causality_failures": book.causality_failures,
                "monotonic_failures": book.upper_monotonic_failures + book.lower_monotonic_failures,
                "removal_classification": removal_classification(book),
            }
        except (TimelineError, KeyError) as exc:
            timeline_detail = {"error": str(exc)}
    else:
        timeline_detail = {
            "skipped": True,
            "reason": f"remaining READY for {len(rem_payloads)}/{len(closes)} closes",
        }
    report["checks"]["phase11u_e_timeline_20_closes"] = {
        "status": timeline_status,
        **timeline_detail,
    }

    report["checks"]["window_independence"] = window_independence_check(
        reader,
        filtered,
        cfg_hash,
        manifest,
        WINDOW_INDEPENDENCE_SPOT_ISO,
    )
    report["checks"]["research_visibility"] = research_visibility_audit(
        client, database, snapshot_ids
    )

    t1 = time.perf_counter()
    report["performance"] = {
        "elapsed_seconds": round(t1 - t0, 2),
        "rss_mb_start": round(rss0, 1),
        "rss_mb_end": round(_rss_mb(), 1),
        "candle_cache_hits": cache.hits,
        "candle_cache_loads": cache.loads,
    }

    statuses = [
        pub["status"],
        active_status,
        report["checks"]["causality_known_at"]["status"],
        remaining_status,
        timeline_status,
        report["checks"]["window_independence"]["status"],
        report["checks"]["research_visibility"]["status"],
    ]
    fail = sum(1 for s in statuses if s == "FAIL")
    nv = sum(1 for s in statuses if s == "NOT_VERIFIED")
    pass_n = sum(1 for s in statuses if s == "PASS")
    if fail:
        overall = "FAIL"
    elif nv:
        overall = "PASS_WITH_NOT_VERIFIED"
    else:
        overall = "PASS"
    report["summary"] = {
        "overall": overall,
        "pass": pass_n,
        "fail": fail,
        "not_verified": nv,
    }

    if overall in ("PASS", "PASS_WITH_NOT_VERIFIED"):
        report["next_step_recommendation"] = (
            "Wire research_visibility_mode=RESEARCH_HISTORICAL in chart/API; extend to 15m_PD5 "
            "or second week of August with same 20-close cap; optional phase11u_f-style lazy "
            "payload for 15m once display_bounds contract exists."
        )
    else:
        report["next_step_recommendation"] = "Fix FAIL checks before widening scope."

    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    client.close()
    print(json.dumps({"overall": overall, "out": str(OUT_PATH)}, indent=2))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
