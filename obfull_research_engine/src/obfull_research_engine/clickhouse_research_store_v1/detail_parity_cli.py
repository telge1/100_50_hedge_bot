"""CLI/runner: full detail parity + regressions for ClickHouse silver v1_2 pilot."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import (
    DATABASE,
    EVENTS_TABLE,
    PILOT_SEGMENT,
    PILOT_SYMBOL,
    PILOT_WINDOW_END,
    PILOT_WINDOW_START,
    PREFIX_WINDOW_END,
    PREFIX_WINDOW_START,
)
from .datetime_integrity import assert_event_receive_datetime_match_ns
from .detail_parity import (
    DEFAULT_REFERENCE_BOOK_RESETS,
    DEFAULT_REFERENCE_LEVEL_CHANGES,
    DetailParityError,
    assert_lc_epoch_consistent_with_checkpoints,
    compare_canonical_lc_lists,
    compare_checkpoints,
    load_clickhouse_checkpoints,
    load_clickhouse_level_changes,
    load_reference_checkpoints,
    load_reference_level_changes,
)
from .helpers import current_rss_bytes, get_clickhouse_client, iso_to_ns_exact, load_clickhouse_env
from .importer import run_pilot_import
from .silver_builder import compare_reference_metrics, load_bronze_window, run_silver_build
from .silver_constants import (
    CHECKPOINTS_TABLE,
    DEFAULT_REFERENCE_STATES,
    LEVEL_CHANGES_TABLE,
    METRICS_TABLE,
)
from .silver_replay import bronze_row_from_ch, dedupe_bronze_by_record_id, sort_bronze_source_order


def _stop(reason: str) -> dict[str, Any]:
    code = reason if reason.startswith("STOP_") else f"STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_{reason}"
    return {"verdict": code, "ok": False}


def run_detail_parity_and_regressions(
    *,
    client: Any | None = None,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    peak = current_rss_bytes()
    load_clickhouse_env()
    own = client is None
    if client is None:
        client = get_clickhouse_client()
    client.command("SET session_timezone = 'UTC'")

    analysis_start_ns = iso_to_ns_exact(PILOT_WINDOW_START)
    analysis_end_ns = iso_to_ns_exact(PILOT_WINDOW_END)
    bronze_start_ns = iso_to_ns_exact(PREFIX_WINDOW_START)
    bronze_end_ns = iso_to_ns_exact(PREFIX_WINDOW_END)

    report: dict[str, Any] = {
        "verdict": None,
        "ok": False,
        "level_change_parity": None,
        "checkpoint_parity": None,
        "epoch_check": None,
        "regressions": {},
        "elapsed_s": 0.0,
        "peak_rss_bytes": 0,
    }

    try:
        # --- regressions: UTC/ns ---
        bad = client.query(
            f"""
            SELECT count()
            FROM {DATABASE}.{EVENTS_TABLE} FINAL
            WHERE symbol = {{symbol:String}}
              AND event_time_ns >= {{a:UInt64}}
              AND event_time_ns < {{b:UInt64}}
              AND (
                toUnixTimestamp64Nano(event_time) != event_time_ns
                OR toUnixTimestamp64Nano(receive_time) != receive_time_ns
              )
            """,
            parameters={"symbol": PILOT_SYMBOL, "a": bronze_start_ns, "b": bronze_end_ns},
        ).result_rows[0][0]
        if int(bad) != 0:
            report.update(_stop("UTC_NS"))
            report["regressions"]["utc_ns"] = {"ok": False, "bad_rows": int(bad)}
            return report
        samples = assert_event_receive_datetime_match_ns(
            client,
            database=DATABASE,
            table=EVENTS_TABLE,
            symbol=PILOT_SYMBOL,
            start_ns=bronze_start_ns,
            end_ns=bronze_end_ns,
            sample_limit=5,
        )
        report["regressions"]["utc_ns"] = {"ok": True, "bad_rows": 0, "samples": samples["samples"]}

        # --- prefix idempotency ---
        imp = run_pilot_import(
            segment_path=PILOT_SEGMENT,
            symbol=PILOT_SYMBOL,
            window_start=PREFIX_WINDOW_START,
            window_end=PREFIX_WINDOW_END,
            client=client,
        )
        if imp.status != "SKIPPED_ALREADY_COMPLETE" or imp.rows_inserted != 0:
            report.update(_stop("IDEMPOTENCY_PREFIX"))
            report["regressions"]["prefix_idempotency"] = imp.to_dict()
            return report
        report["regressions"]["prefix_idempotency"] = {
            "ok": True,
            "status": imp.status,
            "rows_inserted": imp.rows_inserted,
            "content_hash": imp.content_hash,
        }

        # --- silver idempotency ---
        sil = run_silver_build(client=client, max_wall_clock_s=60)
        if sil.status != "SKIPPED_ALREADY_COMPLETE" or sil.rows_inserted != 0:
            report.update(_stop("IDEMPOTENCY_SILVER"))
            report["regressions"]["silver_idempotency"] = sil.to_dict()
            return report
        report["regressions"]["silver_idempotency"] = {
            "ok": True,
            "status": sil.status,
            "rows_inserted": sil.rows_inserted,
        }

        # --- gaps ---
        gap = client.query(
            f"""
            SELECT gap_count FROM {DATABASE}.ob_silver_builds_pilot_v1_2 FINAL
            WHERE status = 'COMPLETE'
            ORDER BY completed_at DESC LIMIT 1
            """
        ).result_rows
        gap_n = int(gap[0][0]) if gap else -1
        if gap_n != 0:
            report.update(_stop("GAPS"))
            report["regressions"]["gaps"] = {"ok": False, "gap_count": gap_n}
            return report
        report["regressions"]["gaps"] = {"ok": True, "gap_count": 0}

        # --- states_100ms parity ---
        metrics = client.query(
            f"""
            SELECT
              bucket_start_ms, best_bid, best_ask, mid, book_hash
            FROM {DATABASE}.{METRICS_TABLE} FINAL
            WHERE symbol = {{symbol:String}}
            ORDER BY bucket_start_ms
            """,
            parameters={"symbol": PILOT_SYMBOL},
        ).result_rows
        metric_dicts = [
            {
                "bucket_start_ms": int(r[0]),
                "best_bid": r[1],
                "best_ask": r[2],
                "mid": r[3],
                "book_hash": r[4].decode() if isinstance(r[4], (bytes, bytearray)) else str(r[4]),
            }
            for r in metrics
        ]
        parity = compare_reference_metrics(
            metrics=metric_dicts,
            reference_path=DEFAULT_REFERENCE_STATES,
            coverage_start_ns=analysis_start_ns,
            coverage_end_ns=analysis_end_ns,
        )
        if parity.get("status") != "PARITY_EXACT" or int(parity.get("compared_buckets") or 0) != 600:
            report.update(_stop("STATES_100MS"))
            report["regressions"]["states_100ms"] = parity
            return report
        report["regressions"]["states_100ms"] = {
            "ok": True,
            "status": parity["status"],
            "compared_buckets": parity["compared_buckets"],
        }

        # --- level-change detail parity ---
        ref_lcs = load_reference_level_changes(
            DEFAULT_REFERENCE_LEVEL_CHANGES,
            analysis_start_ns=analysis_start_ns,
            analysis_end_ns=analysis_end_ns,
        )
        ch_lcs, ch_meta = load_clickhouse_level_changes(
            client,
            database=DATABASE,
            table=LEVEL_CHANGES_TABLE,
            symbol=PILOT_SYMBOL,
            analysis_start_ns=analysis_start_ns,
            analysis_end_ns=analysis_end_ns,
        )
        lc_cmp = compare_canonical_lc_lists(ref_lcs, ch_lcs)
        report["level_change_parity"] = lc_cmp
        if not lc_cmp["ok"]:
            report.update(_stop("LEVEL_CHANGES"))
            return report

        # CH-only fields sanity (receive_time_ns present; ordinals positive)
        if any(int(r["receive_time_ns"]) <= 0 for r in ch_meta):
            report.update(_stop("RECEIVE_TIME_NS"))
            return report
        if any(int(r["source_record_ordinal"]) <= 0 for r in ch_meta):
            report.update(_stop("SOURCE_ORDINAL"))
            return report
        report["level_change_parity"]["ch_only_fields"] = {
            "receive_time_ns": "present_on_all_rows",
            "source_record_ordinal": "present_on_all_rows",
            "note": "Episode-1 level_changes.jsonl.zst does not carry these fields",
        }

        # --- checkpoints ---
        ref_ck = load_reference_checkpoints(
            DEFAULT_REFERENCE_BOOK_RESETS,
            bronze_start_ns=bronze_start_ns,
            analysis_end_ns=analysis_end_ns,
        )
        ch_ck = load_clickhouse_checkpoints(
            client,
            database=DATABASE,
            table=CHECKPOINTS_TABLE,
            symbol=PILOT_SYMBOL,
        )
        ck_cmp = compare_checkpoints(ref_ck, ch_ck)
        report["checkpoint_parity"] = ck_cmp
        if not ck_cmp["ok"]:
            reason = str(ck_cmp.get("reason") or "CHECKPOINTS")
            if reason.startswith("STOP_"):
                report["verdict"] = reason
                report["ok"] = False
            else:
                report.update(_stop("CHECKPOINTS"))
            return report

        epoch_chk = assert_lc_epoch_consistent_with_checkpoints(ch_meta, ch_ck)
        report["epoch_check"] = epoch_chk

        # Ensure bronze still has contiguous ordinals (regression for warmup continuity)
        raw_rows, _ = load_bronze_window(
            client,
            symbol=PILOT_SYMBOL,
            window_start=PREFIX_WINDOW_START,
            window_end=PREFIX_WINDOW_END,
        )
        records = sort_bronze_source_order(
            dedupe_bronze_by_record_id([bronze_row_from_ch(r) for r in raw_rows])
        )
        ords = [r.record_ordinal for r in records]
        if ords != sorted(ords) or (ords[-1] - ords[0] + 1) != len(ords):
            report.update(_stop("ORDINAL_GAP"))
            return report

        peak = max(peak, current_rss_bytes())
        report["ok"] = True
        report["verdict"] = "CLICKHOUSE_V1_2_FULL_DETAIL_PARITY_PROVEN"
        report["elapsed_s"] = time.monotonic() - t0
        report["peak_rss_bytes"] = peak
        return report
    except DetailParityError as exc:
        report["verdict"] = str(exc).split(":")[0]
        report["ok"] = False
        report["error"] = str(exc)
        return report
    finally:
        report["elapsed_s"] = time.monotonic() - t0
        report["peak_rss_bytes"] = max(peak, current_rss_bytes())
        if out_dir is not None:
            path = Path(out_dir)
            path.mkdir(parents=True, exist_ok=True)
            (path / "detail_parity_report.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
            )
        if own and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    out = (
        Path(__file__).resolve().parents[3]
        / "runs"
        / "clickhouse_research_store_pilot_v1"
    )
    report = run_detail_parity_and_regressions(out_dir=out)
    print(json.dumps({"verdict": report.get("verdict"), "ok": report.get("ok")}, indent=2))
    if not report.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
