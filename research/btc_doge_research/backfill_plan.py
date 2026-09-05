"""Machine-readable backfill plan from modality-scoped coverage."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from typing import Any

from .contracts import sanitize_json
from .full_history_contracts import (
    FULL_HISTORY_CONTRACT_VERSION,
    IMPORTABLE_MODALITIES,
    RESULT_ROOT_SOURCE_RECOVERY,
    SEGMENT_MISSING,
    SEGMENT_AFTER_QUEUE_FULL,
    SEGMENT_CONFLICTING_PRODUCERS,
    SEGMENT_SOURCE_GAP,
)
from .modality_coverage import build_modality_coverage, coverage_summary

TARGET_TABLE = {
    "PUBLIC_TRADES": "research_public_trade_buckets_1s",
    "LIQUIDATIONS": "research_liquidation_events",
    "OPEN_INTEREST": "research_open_interest_observations",
    "CANDLES": "research_market_1m",
    "OB200": "research_ob200_snapshots_1s",
    "TPO_PROFILE": "research_tpo_profile_bins_session",
    "VOLUME_PROFILE": "research_volume_profile_bins_session",
}


def _eligibility(row: dict[str, Any]) -> tuple[str, str]:
    if row["modality"] == "CANDLES":
        return "COVERAGE_ONLY", "CANDLES_TRACKED_NOT_IMPORTED"
    status = row["status"]
    if status in (SEGMENT_MISSING, SEGMENT_AFTER_QUEUE_FULL, SEGMENT_CONFLICTING_PRODUCERS, SEGMENT_SOURCE_GAP):
        return "EXCLUDED", row.get("exclusion_reason") or status
    if status in ("READY", "PARTIAL", "ORDERING_AMBIGUOUS"):
        return "ELIGIBLE", ""
    return "EXCLUDED", status


def _dependency_status(row: dict[str, Any], segments: list[dict[str, Any]]) -> str:
    if row["modality"] not in ("TPO_PROFILE", "VOLUME_PROFILE"):
        return "NONE"
    day = row["segment_start"][:10]
    trade = next(
        (
            s for s in segments
            if s["symbol"] == row["symbol"] and s["modality"] == "PUBLIC_TRADES" and s["segment_start"].startswith(day)
        ),
        None,
    )
    if not trade:
        return "TRADES_MISSING"
    if trade["status"] not in ("READY", "ORDERING_AMBIGUOUS"):
        return "TRADES_INCOMPLETE"
    return "SATISFIED"


def build_backfill_plan(segments: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    segments = segments or build_modality_coverage()
    plan: list[dict[str, Any]] = []
    for row in segments:
        eligibility, exclusion = _eligibility(row)
        if row["modality"] in ("TPO_PROFILE", "VOLUME_PROFILE") and _dependency_status(row, segments) != "SATISFIED":
            eligibility = "EXCLUDED"
            exclusion = _dependency_status(row, segments)
        expected_bytes = int(row.get("bytes") or 0)
        if row["modality"] == "OB200" and expected_bytes == 0:
            expected_bytes = 2_000_000
        plan.append(
            sanitize_json(
                {
                    "symbol": row["symbol"],
                    "modality": row["modality"],
                    "segment_start": row["segment_start"],
                    "segment_end": row["segment_end"],
                    "producer_id": row.get("producer_id", ""),
                    "source": row.get("source", ""),
                    "source_path": row.get("source_path", ""),
                    "boundary_auxiliary_path": row.get("boundary_auxiliary_path", ""),
                    "boundary_auxiliary_fingerprint": row.get("boundary_auxiliary_fingerprint", ""),
                    "boundary_role": row.get("boundary_role", ""),
                    "source_semantics": row.get("source_semantics", ""),
                    "source_fingerprint": row.get("source_fingerprint", ""),
                    "expected_rows": row.get("expected_rows", 0),
                    "expected_bytes": expected_bytes,
                    "target_table": TARGET_TABLE.get(row["modality"], ""),
                    "contract_version": FULL_HISTORY_CONTRACT_VERSION,
                    "eligibility": eligibility,
                    "import_eligible": row["modality"] in IMPORTABLE_MODALITIES and eligibility == "ELIGIBLE",
                    "target_mode": "COVERAGE_ONLY" if row["modality"] in ("CANDLES",) else "IMPORT",
                    "exclusion_reason": exclusion,
                    "dependency_status": _dependency_status(row, segments),
                    "segment_status": row["status"],
                }
            )
        )
    return plan


def write_backfill_plan(plan: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> None:
    RESULT_ROOT_SOURCE_RECOVERY.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in plan for k in row})
    with (RESULT_ROOT_SOURCE_RECOVERY / "backfill_plan.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(plan)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": summary or coverage_summary(
            [{"status": r["segment_status"], "modality": r["modality"]} for r in plan]
        ),
        "eligible_count": sum(1 for r in plan if r.get("import_eligible")),
        "importable_count": sum(1 for r in plan if r.get("import_eligible")),
        "coverage_only_count": sum(1 for r in plan if r["eligibility"] == "COVERAGE_ONLY"),
        "excluded_count": sum(1 for r in plan if r["eligibility"] == "EXCLUDED"),
        "plan": plan,
    }
    (RESULT_ROOT_SOURCE_RECOVERY / "backfill_plan.json").write_text(
        json.dumps(sanitize_json(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run() -> dict[str, Any]:
    segments = build_modality_coverage()
    plan = build_backfill_plan(segments)
    summary = coverage_summary(segments)
    write_backfill_plan(plan, summary)
    return sanitize_json(
        {
            "eligible_segments": summary["eligible_segments"],
            "total_segments": summary["total_segments"],
            "plan_rows": len(plan),
            "by_modality": summary["by_modality"],
        }
    )


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
