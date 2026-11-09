#!/usr/bin/env python3
"""Phase 5B — minimal causal pilot: DOGEUSDT 15m PD10 @ 2026-08-01 (read-only CH)."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
)

LMV2_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = _REPO / "logs" / "historical_import" / "overnight_2026"
OUT_PATH = LOG_DIR / "phase5b_pilot_aug01_2026.json"

SYMBOL = "DOGEUSDT"
TIMEFRAME = "15m"
POOL_DETAIL = 10
PIPELINE_LINE_ID = "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1"
PRIMARY_CLOSES_ISO = ("2026-08-01T00:00:00Z", "2026-08-01T00:15:00Z")

# Apr–Sep frozen immutable 1m input (parquet present under catalog inputs/)
IMMUTABLE_SNAPSHOT_ID = "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93"
IMMUTABLE_PRICE_HASH = "d489aa686f6f648850f248d4809338abbee0d7ab6f0bda3ec5ffe3e698757f48"
IMMUTABLE_REVISION_HASH = "9a3ba3066376d27aabc81ffe01b9b08741e819ba5095bf895f8dd0125812258e"
SNAPSHOT_ROOT = LMV2_ROOT


@dataclass
class LineFilteredPublicationSource:
    """Restrict publication hits to one pipeline_line_id (no silent fallback)."""

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
        filtered = [h for h in hits if h.pipeline_line_id == self.pipeline_line_id]
        return filtered

    def members(self, snapshot_id: str) -> list[MemberRow]:
        return self.inner.members(snapshot_id)

    def catalog(self, snapshot_id: str) -> list[CatalogRow]:
        return self.inner.catalog(snapshot_id)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_close(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def verify_exact_closes_on_line(
    client,
    database: str,
    cfg_hash: str,
) -> dict[str, Any]:
    primary = [_parse_close(s) for s in PRIMARY_CLOSES_ISO]
    out: dict[str, Any] = {
        "status": "PASS",
        "pipeline_line_id": PIPELINE_LINE_ID,
        "config_hash": cfg_hash,
        "primary_closes": list(PRIMARY_CLOSES_ISO),
        "per_close": [],
        "other_lines_at_same_closes": [],
        "stop_before_substitute": False,
    }
    for dc in primary:
        rows = client.query(
            f"""
            SELECT pipeline_line_id, snapshot_id, publish_complete
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
        other = [
            {"pipeline_line_id": str(r[0]), "snapshot_id": str(r[1])}
            for r in rows
            if str(r[0]) != PIPELINE_LINE_ID
        ]
        entry = {
            "decision_close": _iso(dc),
            "published_on_target_line": len(on_line) == 1,
            "snapshot_id": str(on_line[0][1]) if len(on_line) == 1 else None,
            "competing_publish_rows": len(rows),
        }
        out["per_close"].append(entry)
        if other:
            out["other_lines_at_same_closes"].append(
                {"decision_close": _iso(dc), "lines": other}
            )
        if len(on_line) != 1:
            out["status"] = "FAIL"
            out["stop_before_substitute"] = True

    if out["stop_before_substitute"]:
        missing = [e["decision_close"] for e in out["per_close"] if not e["published_on_target_line"]]
        nearest: dict[str, Any] = {}
        for miss_iso in missing:
            dc = _parse_close(miss_iso)
            near = client.query(
                f"""
                SELECT decision_close,
                       abs(toInt64(decision_close) - toInt64({{dc:DateTime64(3, 'UTC')}})) AS dist
                FROM {database}.research_lmv2_active_snapshot_publish_v1 FINAL
                WHERE pipeline_line_id = {{lid:String}}
                  AND config_hash = {{ch:String}}
                  AND publish_complete = 1
                ORDER BY dist ASC
                LIMIT 3
                """,
                parameters={"lid": PIPELINE_LINE_ID, "ch": cfg_hash, "dc": dc},
            ).result_rows
            nearest[miss_iso] = [_iso(r[0]) for r in near]
        out["nearest_published_closes_on_line"] = nearest
        out["note"] = (
            "Exact primary closes missing on production line; nearest documented; "
            "no substitute chosen (manual approval required)."
        )
    return out


def load_catalog_lifecycle(client, database: str, snapshot_id: str) -> list[dict]:
    rows = client.query(
        f"""
        SELECT pool_id, side, top_price, bottom_price, strength, strength_class,
               known_at, break_at, confirmed_at, invalidated_at, pipeline_line_id
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
                "catalog_pipeline_line_id": str(r[10]) if r[10] else None,
            }
        )
    return out


def causality_audit(catalog: list[dict], decision_close: datetime) -> dict[str, Any]:
    dc = decision_close
    late_known = []
    for row in catalog:
        ka = row.get("known_at")
        if ka is None:
            continue
        if isinstance(ka, datetime):
            known = ka if ka.tzinfo else ka.replace(tzinfo=timezone.utc)
            known = known.astimezone(timezone.utc)
        else:
            known = _parse_close(str(ka))
        if known > dc:
            late_known.append(row["pool_id"])
    return {
        "status": "PASS" if not late_known else "FAIL",
        "known_at_after_decision_close_count": len(late_known),
        "pool_ids_sample": late_known[:20],
        "rule": "catalog.known_at must not exceed decision_close for causal ACTIVE display",
    }


def candle_coverage_audit(
    manifest: dict,
    catalog: list[CatalogRow],
    decision_close: datetime,
) -> dict[str, Any]:
    rs = _manifest_time(manifest, "range_start")
    re = _manifest_time(manifest, "range_end_exclusive")
    known_ats = [row.known_at for row in catalog]
    adapter_status = candle_window_status(
        range_start=rs,
        range_end_exclusive=re,
        decision_close=decision_close,
        known_ats=known_ats,
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
    return {
        "status": "PASS" if adapter_status == "READY" and prefix_status == "READY" else "NOT_VERIFIED",
        "immutable_snapshot_id": IMMUTABLE_SNAPSHOT_ID,
        "manifest_range_start": _iso(rs),
        "manifest_range_end_exclusive": _iso(re),
        "warmup_source_start_1m_open": _iso(warmup_start),
        "decision_bar_first_1m_open": _iso(bar_first),
        "decision_bar_last_1m_open": _iso(bar_last),
        "candle_window_status": adapter_status,
        "warmup_prefix_base_file_status": prefix_status,
        "note": "All 1m opens in [warmup_start, decision_bar_last] must exist in immutable parquet (verified on load).",
    }


def run_phase11u_e_pair(
    source: LineFilteredPublicationSource,
    cache: CandleCache,
    closes: list[datetime],
) -> dict[str, Any]:
    if len(closes) < 2:
        return {"status": "NOT_VERIFIED", "detail": "need at least two closes"}
    try:
        payloads = []
        for dc in closes:
            payloads.append(
                load_historical_remaining(
                    source,
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
            )
        cache_key = (
            IMMUTABLE_SNAPSHOT_ID,
            IMMUTABLE_PRICE_HASH,
            IMMUTABLE_REVISION_HASH,
            SYMBOL,
            TIMEFRAME,
        )
        candles = cache._store[cache_key]["candles"]
        index = index_from_candles(candles, TIMEFRAME)
        result = TimelineResult()
        for dc, payload in zip(closes, payloads):
            cat = source.catalog(payload["published_snapshot_id"])
            observations = project_close(index, cat, dc)
            observe_close(result, dc, observations)
        failures = (
            result.upper_monotonic_failures
            + result.lower_monotonic_failures
            + result.strength_failures
            + result.causality_failures
        )
        return {
            "status": "PASS" if failures == 0 else "FAIL",
            "processed_closes": result.processed,
            "causality_failures": result.causality_failures,
            "monotonic_failures": result.upper_monotonic_failures + result.lower_monotonic_failures,
            "close_digest": result.close_digest,
        }
    except (AdapterStop, TimelineError, KeyError) as exc:
        return {"status": "NOT_VERIFIED", "detail": str(exc)}


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = build_live_micro_lld_v2_config(POOL_DETAIL)
    cfg_hash = config_hash(cfg)
    manifest = _load_immutable_manifest()

    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    database = ch_cfg.database

    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5B",
        "title": "DOGEUSDT 15m PD10 causal pilot — 2026-08-01",
        "generated_at_utc": _utc_now(),
        "read_only": True,
        "scope": {
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "pool_detail": POOL_DETAIL,
            "pipeline_line_id": PIPELINE_LINE_ID,
            "primary_decision_closes": list(PRIMARY_CLOSES_ISO),
        },
        "checks": {},
        "per_close": [],
        "live_visibility": {
            "status": "NOT_VERIFIED",
            "rule": LIVE_TREATS_BACKFILL_AS_SIGNAL,
            "note": (
                "CH publish proves frozen historical ACTIVE at decision_close; "
                "no ingestion_mode / visibility_time in CH proves retroactive live availability."
            ),
        },
        "phase_5c_proposal": None,
        "summary": {"overall": "PENDING", "pass": 0, "fail": 0, "not_verified": 0},
    }

    close_check = verify_exact_closes_on_line(client, database, cfg_hash)
    report["checks"]["exact_closes_on_production_line"] = close_check

    if close_check.get("stop_before_substitute"):
        report["phase_5c_proposal"] = (
            "Resolve missing exact decision closes on production line (re-import gap or "
            "approve substitute closes) before Remaining/timeline extension."
        )
        report["summary"]["overall"] = "FAIL"
        report["summary"]["fail"] = 1
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        client.close()
        print(json.dumps({"overall": report["summary"]["overall"], "out": str(OUT_PATH)}, indent=2))
        return 1

    reader = PublishedActiveReader()
    inner = ClickHousePublicationSource(client, database)
    filtered = LineFilteredPublicationSource(inner, PIPELINE_LINE_ID)
    cache = CandleCache(max_entries=2)

    active_layer_status = "PASS"
    remaining_layer_status = "PASS"
    identity_status = "PASS"
    ch_consistency_status = "PASS"

    closes_dt = [_parse_close(s) for s in PRIMARY_CLOSES_ISO]

    for dc in closes_dt:
        close_report: dict[str, Any] = {"decision_close": _iso(dc), "steps": {}}

        # STEP 1 — ACTIVE
        try:
            active = reader.load_published(PIPELINE_LINE_ID, dc, config_hash=cfg_hash)
            catalog_rows = load_catalog_lifecycle(client, database, active["snapshot_id"])
            close_report["steps"]["active"] = {
                "status": "PASS",
                "snapshot_id": active["snapshot_id"],
                "publish_id": active.get("publish_id"),
                "active_pool_count": len(active["pool_ids"]),
                "active_state_fingerprint": active["active_state_fingerprint"],
                "prefix_candle_fingerprint": active["prefix_candle_fingerprint"],
                "engine_input_hash": active.get("engine_input_hash"),
                "engine_version": active.get("engine_version"),
                "pool_ids_sample": active["pool_ids"][:5],
            }
        except Exception as exc:
            active_layer_status = "FAIL"
            close_report["steps"]["active"] = {"status": "FAIL", "error": str(exc)}
            catalog_rows = []
            active = {}

        # Ambiguity on unfiltered CH find
        all_hits = inner.find(
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            pool_detail=POOL_DETAIL,
            decision_close=dc,
        )
        filtered_hits = filtered.find(
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            pool_detail=POOL_DETAIL,
            decision_close=dc,
        )
        close_report["steps"]["publication_line_filter"] = {
            "status": "PASS" if len(filtered_hits) == 1 else "FAIL",
            "unfiltered_hit_count": len(all_hits),
            "unfiltered_line_ids": sorted({h.pipeline_line_id for h in all_hits}),
            "filtered_hit_count": len(filtered_hits),
        }
        if len(filtered_hits) != 1:
            active_layer_status = "FAIL"

        # STEP 2 — causality
        if catalog_rows:
            causality = causality_audit(catalog_rows, dc)
            close_report["steps"]["causality"] = causality
            if causality["status"] == "FAIL":
                active_layer_status = "FAIL"
        else:
            close_report["steps"]["causality"] = {"status": "NOT_VERIFIED"}

        # STEP 3 — remaining coverage then projection
        if filtered_hits:
            cat_objs = filtered.catalog(filtered_hits[0].snapshot_id)
            cov = candle_coverage_audit(manifest, cat_objs, dc)
            close_report["steps"]["immutable_candle_coverage"] = cov
            if cov["status"] != "PASS":
                remaining_layer_status = "NOT_VERIFIED"
                close_report["steps"]["remaining"] = {
                    "status": "NOT_VERIFIED",
                    "detail": "coverage gate failed; no load_historical_remaining run",
                }
            else:
                try:
                    resolve_publication(filtered_hits)
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
                    active_ids = {p["pool_id"] for p in rem["active"]}
                    member_ids = {m.pool_id for m in filtered.members(rem["published_snapshot_id"])}
                    id_ok = active_ids == member_ids
                    close_report["steps"]["remaining"] = {
                        "status": "PASS",
                        "published_snapshot_id": rem["published_snapshot_id"],
                        "pipeline_line_ids": rem["pipeline_line_ids"],
                        "active_count": rem["active_count"],
                        "remaining_segment_count": rem["remaining_segment_count"],
                        "fully_traversed_active_ids_count": len(rem["fully_traversed_active_ids"]),
                        "identity_active_set_matches_members": id_ok,
                    }
                    if not id_ok:
                        identity_status = "FAIL"
                except AdapterStop as exc:
                    remaining_layer_status = "NOT_VERIFIED"
                    close_report["steps"]["remaining"] = {
                        "status": "NOT_VERIFIED",
                        "adapter_status": exc.status,
                        "detail": str(exc),
                    }
        else:
            close_report["steps"]["immutable_candle_coverage"] = {"status": "NOT_VERIFIED"}
            close_report["steps"]["remaining"] = {"status": "NOT_VERIFIED"}

        # STEP 5 — CH read consistency (duplicate member count)
        if active.get("snapshot_id"):
            cnt = client.query(
                f"""
                SELECT count() FROM {database}.research_lmv2_active_snapshot_member_v1
                WHERE snapshot_id = {{sid:String}}
                """,
                parameters={"sid": active["snapshot_id"]},
            ).result_rows[0][0]
            close_report["steps"]["ch_member_count_repeat"] = {
                "status": "PASS" if int(cnt) == len(active.get("pool_ids", [])) else "FAIL",
                "member_rows": int(cnt),
                "published_reader_pool_ids": len(active.get("pool_ids", [])),
            }
            if close_report["steps"]["ch_member_count_repeat"]["status"] == "FAIL":
                ch_consistency_status = "FAIL"

        report["per_close"].append(close_report)

    timeline_check = run_phase11u_e_pair(filtered, cache, closes_dt)
    report["checks"]["phase11u_e_two_close_smoke"] = timeline_check

    report["checks"]["active_layer"] = {"status": active_layer_status}
    report["checks"]["remaining_layer"] = {"status": remaining_layer_status}
    report["checks"]["layer_identity"] = {"status": identity_status}
    report["checks"]["ch_read_consistency"] = {"status": ch_consistency_status}

    statuses = [
        close_check["status"],
        active_layer_status,
        remaining_layer_status,
        identity_status,
        ch_consistency_status,
        timeline_check.get("status", "NOT_VERIFIED"),
        report["live_visibility"]["status"],
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
        report["phase_5c_proposal"] = (
            "Extend pilot to a bounded July–Aug window on the same pipeline_line_id "
            "(≤20 closes) with phase11u_e timeline + optional chart slice; keep immutable "
            "84c22453 candle input and strict line filter; wire visibility_mode for research UI."
        )
    else:
        report["phase_5c_proposal"] = "Remediate FAIL checks before any timeline build."

    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    client.close()
    print(json.dumps({"overall": overall, "out": str(OUT_PATH)}, indent=2))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
