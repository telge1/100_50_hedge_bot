"""Multi-batch historical import orchestration (VALIDATE / classify resume; write gated elsewhere)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from uuid import UUID
from pathlib import Path
from typing import Any, Sequence

from research_charts.trp_import import ensure_trp_on_path

from ..identity import build_live_micro_lld_v2_config, config_hash
from .batch_progress import BatchProgressReport, CloseOutcome, utc_now_iso
from .catalog_inventory import iter_stems, stem_path
from .historical_batch_config import (
    DEFAULT_BATCH_SIZE_CLOSES,
    PILOT_COMBO_FOLDER,
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_MONTH,
    PILOT_PIPELINE_LINE_ID,
    StrategyCombo,
    combo_for_folder,
)
from .parse import build_import_bundle, load_manifest_context
from .plan import build_write_plan, summarize_plans
from .runner import ImportMode, run_import_plan
from .provenance_sidecar import (
    InMemoryProvenanceRegistry,
    ProvenanceSidecarConflictError,
    build_sidecar_row_from_bundle,
    provenance_lookup_version,
)
from .validate import validate_bundle


@dataclass(frozen=True)
class BatchSpec:
    month: str
    combo_folder: str
    stems: tuple[str, ...]
    pipeline_line_id: str
    import_batch_id: str

    @property
    def batch_id(self) -> str:
        return f"{self.month}_{self.combo_folder}_{self.stems[0]}_{self.stems[-1]}"


class BatchImportError(Exception):
    """Stop batch; do not silently continue."""


def _resolve_test_run_id(import_batch_id: str, test_run_id: str | None) -> str:
    if test_run_id:
        return test_run_id
    try:
        UUID(import_batch_id)
        return import_batch_id
    except ValueError:
        return str(uuid.uuid4())


def make_batch_specs(
    catalog_root: Path,
    *,
    month: str,
    combo: StrategyCombo,
    batch_size: int = DEFAULT_BATCH_SIZE_CLOSES,
    import_batch_id: str | None = None,
) -> list[BatchSpec]:
    stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo.folder))
    bid = import_batch_id or str(uuid.uuid4())
    line = combo.pipeline_line_id()
    specs: list[BatchSpec] = []
    for i in range(0, len(stems), batch_size):
        chunk = stems[i : i + batch_size]
        specs.append(
            BatchSpec(
                month=month,
                combo_folder=combo.folder,
                stems=chunk,
                pipeline_line_id=line,
                import_batch_id=bid,
            )
        )
    return specs


def classify_close_resume(
    *,
    snapshot_id: str,
    pipeline_line_id: str,
    published_snapshot_ids: set[str],
    meta_snapshot_ids: set[str],
    pilot_snapshot_ids: set[str],
) -> str:
    """
    Read-only classification for idempotent resume (no CH writes).

    Returns one of: ``publish_skip``, ``pilot_overlap_publish_only``, ``ready``, ``meta_without_publish``.
    """
    if snapshot_id in published_snapshot_ids:
        return "publish_skip"
    if snapshot_id in pilot_snapshot_ids and snapshot_id in meta_snapshot_ids:
        return "pilot_overlap_publish_only"
    if snapshot_id in meta_snapshot_ids:
        return "meta_without_publish"
    return "ready"


def validate_batch(
    catalog_root: Path,
    spec: BatchSpec,
    *,
    test_run_id: str | None = None,
) -> dict[str, Any]:
    """VALIDATE-only: same checks as pilot dry-run, scoped to one batch."""
    ensure_trp_on_path()
    manifest = load_manifest_context(catalog_root)
    combo = combo_for_folder(spec.combo_folder)
    if combo is None:
        raise BatchImportError(f"unknown combo folder {spec.combo_folder}")
    ch = config_hash(build_live_micro_lld_v2_config(combo.pool_detail))
    errors: list[str] = []
    plans = []
    bundles = []
    snapshot_ids: list[str] = []
    provenance_rows: list[dict[str, str]] = []
    registry = InMemoryProvenanceRegistry()
    lookup_ver = provenance_lookup_version(catalog_root)
    manifest_id = manifest.catalog_id
    for stem in spec.stems:
        path = stem_path(catalog_root, spec.month, spec.combo_folder, stem)
        if not path.is_file():
            errors.append(f"missing {path}")
            continue
        try:
            bundle = build_import_bundle(
                path,
                catalog_root=catalog_root,
                config_hash=ch,
                manifest=manifest,
                pipeline_line_id=spec.pipeline_line_id,
            )
            validate_bundle(catalog_root, bundle)
            plan = build_write_plan(
                bundle,
                pipeline_line_id=spec.pipeline_line_id,
                test_run_id=_resolve_test_run_id(spec.import_batch_id, test_run_id),
            )
            bundles.append(bundle)
            plans.append(plan)
            snapshot_ids.append(bundle["snapshot_id"])
            sidecar = build_sidecar_row_from_bundle(
                bundle,
                pipeline_line_id=spec.pipeline_line_id,
                import_batch_id=spec.import_batch_id,
                catalog_manifest_id=manifest_id,
                provenance_lookup_version=lookup_ver,
                catalog_root=catalog_root,
            )
            try:
                registry.register(sidecar)
            except ProvenanceSidecarConflictError as exc:
                errors.append(f"{stem}: {exc}")
            provenance_rows.append(sidecar.as_ch_dict())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{stem}: {exc}")

    from collections import Counter

    counts = Counter(snapshot_ids)
    dup = [s for s, n in counts.items() if n > 1]
    if dup:
        errors.append(f"duplicate snapshot_id in batch: {sorted(dup)[:3]}")

    summary = summarize_plans(plans)
    return {
        "batch_id": spec.batch_id,
        "pipeline_line_id": spec.pipeline_line_id,
        "combo_folder": spec.combo_folder,
        "month": spec.month,
        "import_batch_id": spec.import_batch_id,
        "summary": summary,
        "errors": errors,
        "snapshot_ids": snapshot_ids,
        "provenance_lookup_version": lookup_ver,
        "provenance_rows_planned": len(provenance_rows),
        "closes": [
            {
                "stem": b["path"].stem,
                "snapshot_id": b["snapshot_id"],
                "active_pool_count": b["state"].active_pool_count,
                "frozen_input_id": b["frozen_input_id"],
            }
            for b in bundles
        ],
    }


def run_batch_validate_report(
    catalog_root: Path,
    spec: BatchSpec,
) -> BatchProgressReport:
    started = utc_now_iso()
    result = validate_batch(catalog_root, spec)
    status = "validated" if not result["errors"] else "failed"
    report = BatchProgressReport(
        batch_id=spec.batch_id,
        pipeline_line_id=spec.pipeline_line_id,
        combo_folder=spec.combo_folder,
        month=spec.month,
        import_batch_id=spec.import_batch_id,
        status=status,
        closes_planned=len(spec.stems),
        summary=result.get("summary") or {},
        errors=list(result.get("errors") or []),
        started_at=started,
        finished_at=utc_now_iso(),
    )
    for row in result.get("closes") or []:
        report.closes.append(
            CloseOutcome(
                stem=row["stem"],
                snapshot_id=row["snapshot_id"],
                status="validated" if status == "validated" else "failed",
            )
        )
    return report


def pilot_overlap_stems() -> set[tuple[str, str, str]]:
    return {(PILOT_MONTH, PILOT_COMBO_FOLDER, s) for s in PILOT_DECISION_CLOSE_STEMS}


def multi_combination_dry_run(
    catalog_root: Path,
    *,
    samples_per_combo: int = 2,
) -> dict[str, Any]:
    """
    Representative VALIDATE dry-run: first N closes per combo in April 2026 (no CH I/O).
    """
    from .historical_batch_config import STRATEGY_COMBOS

    ensure_trp_on_path()
    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    for combo in STRATEGY_COMBOS:
        stems = tuple(iter_stems(catalog_root, month="2026-04", combo_folder=combo.folder))[
            :samples_per_combo
        ]
        if not stems:
            errors.append(f"no stems for {combo.folder} in 2026-04")
            continue
        spec = BatchSpec(
            month="2026-04",
            combo_folder=combo.folder,
            stems=stems,
            pipeline_line_id=combo.pipeline_line_id(),
            import_batch_id=f"dryrun_{combo.folder}",
        )
        vr = validate_batch(catalog_root, spec)
        reports.append(vr)
        errors.extend(vr.get("errors") or [])

    pilot = run_import_plan(
        list(PILOT_DECISION_CLOSE_STEMS),
        catalog_root=catalog_root,
        pipeline_line_id=PILOT_PIPELINE_LINE_ID,
        mode=ImportMode.VALIDATE,
        combo_month=PILOT_MONTH,
        combo_folder=PILOT_COMBO_FOLDER,
    )
    if pilot.get("errors"):
        errors.extend(pilot["errors"])
    return {
        "samples_per_combo": samples_per_combo,
        "combo_batches": reports,
        "pilot_revalidate": {
            "summary": pilot.get("summary"),
            "errors": pilot.get("errors"),
        },
        "errors": errors,
        "ok": not errors,
    }


def run_batch_readback_gate(
    stems: Sequence[str],
    *,
    catalog_root: Path,
    combo_folder: str,
    month: str,
    pipeline_line_id: str,
    batch_spec: BatchSpec | None = None,
    require_sidecar: bool = False,
    sidecar_store: Any | None = None,
) -> dict[str, Any]:
    """
    Post-write gate: meta/member/catalog/publish parity plus optional sidecar verification.

    Legacy pilot / pre-sidecar publishes: ``require_sidecar=False``.
    New historical production batches: ``require_sidecar=True`` with ``batch_spec``.
    """
    from .readback_parity import run_pilot_readback
    from .provenance_sidecar_store import ClickHouseProvenanceSidecarStore

    combo = combo_for_folder(combo_folder)
    if combo is None:
        raise BatchImportError(f"unknown combo {combo_folder}")
    ch = config_hash(build_live_micro_lld_v2_config(combo.pool_detail))
    combo_rel = f"production/results/{month}/{combo_folder}"
    parity = run_pilot_readback(
        stems,
        catalog_root=catalog_root,
        combo_rel=combo_rel,
        pipeline_line_id=pipeline_line_id,
        config_hash_value=ch,
        use_clickhouse=True,
        fail_closed=True,
    )
    sidecar_reports: list[Any] = []
    if require_sidecar:
        if batch_spec is None:
            raise BatchImportError("batch_spec required when require_sidecar=True")
        from .sidecar_readback import verify_sidecar_readback_for_batch

        store = sidecar_store or ClickHouseProvenanceSidecarStore()
        sidecar_reports = verify_sidecar_readback_for_batch(
            catalog_root,
            batch_spec,
            store,
            stems=stems,
            fail_closed=True,
        )
    return {"parity": parity, "sidecar": sidecar_reports}


def estimate_batch_write_rows(summary: dict[str, Any]) -> dict[str, int]:
    return {
        "meta_rows": int(summary.get("meta_rows") or 0),
        "member_rows": int(summary.get("member_rows") or 0),
        "catalog_rows": int(summary.get("catalog_rows") or 0),
        "publish_rows": int(summary.get("publish_rows") or 0),
    }


def first_regulatory_batch_spec(
    catalog_root: Path,
    *,
    month: str = "2026-04",
    combo_folder: str = "4h_PD10",
    batch_size: int = DEFAULT_BATCH_SIZE_CLOSES,
    import_batch_id: str | None = None,
) -> BatchSpec:
    """First 50 closes of 4h PD10 April (no pilot overlap)."""
    combo = combo_for_folder(combo_folder)
    if combo is None:
        raise BatchImportError(f"unknown combo {combo_folder}")
    stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo_folder))[:batch_size]
    return BatchSpec(
        month=month,
        combo_folder=combo_folder,
        stems=stems,
        pipeline_line_id=combo.pipeline_line_id(),
        import_batch_id=import_batch_id or str(uuid.uuid4()),
    )


def dry_run_readback_all_combos(
    catalog_root: Path,
    *,
    samples_per_combo: int = 1,
) -> dict[str, Any]:
    """In-memory readback (``compare_json_to_plan``) per combo — no ClickHouse."""
    from .historical_batch_config import STRATEGY_COMBOS
    from .readback_parity import compare_json_to_plan

    errors: list[str] = []
    reports: list[dict[str, Any]] = []
    manifest = load_manifest_context(catalog_root)
    for combo in STRATEGY_COMBOS:
        stems = tuple(iter_stems(catalog_root, month="2026-04", combo_folder=combo.folder))[
            :samples_per_combo
        ]
        ch = config_hash(build_live_micro_lld_v2_config(combo.pool_detail))
        line = combo.pipeline_line_id()
        for stem in stems:
            path = stem_path(catalog_root, "2026-04", combo.folder, stem)
            bundle = build_import_bundle(
                path,
                catalog_root=catalog_root,
                config_hash=ch,
                manifest=manifest,
                pipeline_line_id=line,
            )
            validate_bundle(catalog_root, bundle)
            rep = compare_json_to_plan(bundle)
            reports.append({"combo": combo.folder, "stem": stem, "ok": rep.ok, "mismatches": rep.mismatches})
            if not rep.ok:
                errors.extend(rep.mismatches)
    return {"ok": not errors, "errors": errors, "reports": reports}


def estimate_full_import_storage(
    *,
    snapshot_count: int = 53802,
    pilot_snapshots_in_ch: int = 10,
    current_lmv2_bytes: int,
    current_lmv2_rows: int,
    avg_pools_per_snapshot: float = 167.0,
) -> dict[str, Any]:
    """
    Revised estimate using observed CH compression (pilot) not raw JSON size.
    """
    new_snapshots = max(0, snapshot_count - pilot_snapshots_in_ch)
    member_catalog_rows = int(new_snapshots * avg_pools_per_snapshot)
    bytes_per_row = current_lmv2_bytes / max(current_lmv2_rows, 1)
    incremental_bytes = int(member_catalog_rows * 2 * bytes_per_row * 1.15)
    sidecar_bytes = snapshot_count * 400
    backup_headroom = incremental_bytes + int(current_lmv2_bytes)
    return {
        "method": "pilot_observed_bytes_per_row",
        "bytes_per_row_observed": bytes_per_row,
        "incremental_data_bytes": incremental_bytes,
        "incremental_readable": f"{incremental_bytes / (1024**3):.2f} GiB",
        "sidecar_estimate_bytes": sidecar_bytes,
        "recommended_backup_headroom_bytes": backup_headroom,
        "pessimistic_json_linear_gib": 140.0,
        "note": "Use incremental_data_bytes + merge overhead; 140GiB linear JSON upper bound not used for go/no-go.",
    }
