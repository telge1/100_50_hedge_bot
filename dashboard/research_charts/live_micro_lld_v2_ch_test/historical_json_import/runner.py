"""Dry-run and gated pilot import orchestration."""

from __future__ import annotations

import enum
import uuid
from pathlib import Path
from typing import Any, Sequence

from research_charts.trp_import import ensure_trp_on_path

from ..identity import build_live_micro_lld_v2_config, config_hash
from .parse import build_import_bundle, load_manifest_context, snapshot_path_for_stem
from .plan import build_write_plan, summarize_plans
from .validate import validate_bundle


class ImportMode(enum.Enum):
    VALIDATE = "validate"
    PILOT_WRITE = "pilot_write"


_PILOT_WRITE_ENV = "HISTORICAL_JSON_IMPORT_ALLOW_WRITE"


def run_import_plan(
    stems: Sequence[str],
    *,
    catalog_root: Path,
    pipeline_line_id: str,
    mode: ImportMode = ImportMode.VALIDATE,
    test_run_id: str | None = None,
    combo_month: str = "2026-08",
    combo_folder: str = "15m_PD10",
) -> dict[str, Any]:
    """
    VALIDATE: filesystem + in-memory plans only (no ClickHouse).
    PILOT_WRITE: requires env HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1 and explicit mode (not run in Phase 2).
    """
    if mode == ImportMode.PILOT_WRITE:
        import os

        if os.environ.get(_PILOT_WRITE_ENV) != "1":
            raise RuntimeError(
                f"pilot write blocked: set {_PILOT_WRITE_ENV}=1 and obtain explicit user approval"
            )
        return _run_pilot_write(
            stems,
            catalog_root=catalog_root,
            pipeline_line_id=pipeline_line_id,
            test_run_id=test_run_id or str(uuid.uuid4()),
            combo_month=combo_month,
            combo_folder=combo_folder,
        )

    ensure_trp_on_path()
    manifest = load_manifest_context(catalog_root)
    plans: list[Any] = []
    bundles: list[dict[str, Any]] = []
    errors: list[str] = []

    for stem in stems:
        path = snapshot_path_for_stem(
            catalog_root,
            month=combo_month,
            combo=combo_folder,
            stem=str(stem),
        )
        if not path.is_file():
            errors.append(f"missing snapshot file {path}")
            continue
        try:
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
            pd = int(payload["pool_detail"])
            ch = config_hash(build_live_micro_lld_v2_config(pd))
            bundle = build_import_bundle(
                path,
                catalog_root=catalog_root,
                config_hash=ch,
                manifest=manifest,
                pipeline_line_id=pipeline_line_id,
            )
            validate_bundle(catalog_root, bundle)
            plan = build_write_plan(
                bundle,
                pipeline_line_id=pipeline_line_id,
                test_run_id=test_run_id or str(uuid.uuid4()),
            )
            bundles.append(bundle)
            plans.append(plan)
        except Exception as exc:  # noqa: BLE001 — collect per-close failures for dry-run report
            errors.append(f"{stem}: {exc}")

    summary = summarize_plans(plans)
    return {
        "mode": mode.value,
        "pipeline_line_id": pipeline_line_id,
        "catalog_root": str(catalog_root),
        "summary": summary,
        "errors": errors,
        "closes": [
            {
                "stem": b["path"].stem,
                "decision_close": b["state"].decision_close.isoformat(),
                "snapshot_id": b["snapshot_id"],
                "active_pool_count": b["state"].active_pool_count,
                "frozen_input_id": b["frozen_input_id"],
                "relative_dest": b["relative_dest"],
            }
            for b in bundles
        ],
        "provenance_gaps": _provenance_schema_gaps(),
    }


def _provenance_schema_gaps() -> list[str]:
    return [
        "CH meta/member/catalog/publish tables do not store per-file dest_sha256; "
        "provenance is carried only in import plan metadata until a future schema extension.",
    ]


def _run_pilot_write(
    stems: Sequence[str],
    *,
    catalog_root: Path,
    pipeline_line_id: str,
    test_run_id: str,
    combo_month: str,
    combo_folder: str,
) -> dict[str, Any]:
    plan_report = run_import_plan(
        stems,
        catalog_root=catalog_root,
        pipeline_line_id=pipeline_line_id,
        mode=ImportMode.VALIDATE,
        test_run_id=test_run_id,
        combo_month=combo_month,
        combo_folder=combo_folder,
    )
    if plan_report["errors"]:
        raise RuntimeError(f"validate failed before write: {plan_report['errors']}")

    raise RuntimeError(
        "pilot_write path is reserved for post-approval execution "
        "(staged meta/member, json catalog insert, publish_validated_snapshot)"
    )
