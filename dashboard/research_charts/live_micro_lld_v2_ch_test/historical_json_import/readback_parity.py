"""Post-import JSON ↔ ClickHouse parity (read-only when CH reader supplied)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from research_charts.historical_lmv2.chart_adapter import active_pool_records
from research_charts.historical_lmv2.provenance_lookup import relative_dest_for

from ..active_snapshot.fingerprint import active_state_fingerprint
from ..active_snapshot.pool_catalog import load_active_pool_catalog
from ..active_snapshot.published_reader import PublishedActiveReader, UnpublishedSnapshotError
from ..active_snapshot.reader import ActiveSnapshotReader
from .parse import build_import_bundle, load_manifest_context
from .pilot_config import AUDIT_POOL_IDS
from ..identity import build_live_micro_lld_v2_config, config_hash
from .catalog_integrity import assert_member_catalog_id_sets, compare_catalog_rows_to_json_fields, validate_catalog_rows_in_memory
from .readback_errors import ReadbackGateFailure


class ParityMismatch:
    def __init__(self, field: str, expected: Any, actual: Any, *, context: str = "") -> None:
        self.field = field
        self.expected = expected
        self.actual = actual
        self.context = context

    def __str__(self) -> str:
        ctx = f" ({self.context})" if self.context else ""
        return f"{self.field}{ctx}: expected={self.expected!r} actual={self.actual!r}"


@dataclass
class ParityReport:
    stem: str
    ok: bool
    mismatches: list[str] = field(default_factory=list)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compare_json_to_published(
    json_path: Path,
    *,
    catalog_root: Path,
    pipeline_line_id: str,
    config_hash_value: str,
    audit_pool_ids: Sequence[str] = AUDIT_POOL_IDS,
) -> ParityReport:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    stem = json_path.stem
    manifest = load_manifest_context(catalog_root)
    pd = int(payload["pool_detail"])
    ch = config_hash(build_live_micro_lld_v2_config(pd))
    if ch != config_hash_value:
        return ParityReport(stem, False, [f"config_hash: expected {config_hash_value}, bundle {ch}"])
    bundle = build_import_bundle(
        json_path,
        catalog_root=catalog_root,
        config_hash=ch,
        manifest=manifest,
        pipeline_line_id=pipeline_line_id,
    )
    state = bundle["state"]
    dc = _utc(state.decision_close)
    mismatches: list[str] = []

    reader = PublishedActiveReader()
    try:
        pub = reader.load_published(pipeline_line_id, dc, config_hash=config_hash_value)
    except UnpublishedSnapshotError as exc:
        return ParityReport(stem, False, [str(exc)])

    if pub["snapshot_id"] != bundle["snapshot_id"]:
        mismatches.append(str(ParityMismatch("snapshot_id", bundle["snapshot_id"], pub["snapshot_id"])))
    if tuple(pub["pool_ids"]) != state.active_pool_ids:
        mismatches.append(str(ParityMismatch("active_pool_ids", state.active_pool_ids, pub["pool_ids"])))
    if pub["active_state_fingerprint"] != state.active_state_fingerprint:
        mismatches.append(
            str(
                ParityMismatch(
                    "active_state_fingerprint",
                    state.active_state_fingerprint,
                    pub["active_state_fingerprint"],
                )
            )
        )

    catalog = load_active_pool_catalog(pub["snapshot_id"])
    if len(catalog) != state.active_pool_count:
        mismatches.append(
            str(ParityMismatch("catalog_count", state.active_pool_count, len(catalog)))
        )
    try:
        assert_member_catalog_id_sets(pub["pool_ids"], [r["pool_id"] for r in catalog])
    except Exception as exc:
        mismatches.append(f"catalog/member id sets: {exc}")

    json_by_id = {r["pool_id"]: r for r in active_pool_records(payload)}
    cat_by_id = {r["pool_id"]: r for r in catalog}
    mismatches.extend(
        compare_catalog_rows_to_json_fields(bundle["catalog_rows"], json_by_id)
    )
    for pid in state.active_pool_ids:
        jr, cr = json_by_id.get(pid), cat_by_id.get(pid)
        if cr is None:
            mismatches.append(f"missing catalog row for {pid}")
            continue
        for field in (
            "side",
            "top_price",
            "bottom_price",
            "strength",
            "strength_class",
            "row_fingerprint",
        ):
            jv = jr.get(field)
            cv = cr.get(field)
            if field in ("top_price", "bottom_price", "strength"):
                if float(jv) != float(cv):
                    mismatches.append(str(ParityMismatch(field, jv, cv, context=pid)))
            elif str(jv) != str(cv):
                mismatches.append(str(ParityMismatch(field, jv, cv, context=pid)))
        jk = str(jr.get("known_at") or "")
        ck = cr.get("known_at")
        if hasattr(ck, "isoformat"):
            ck = _utc(ck).isoformat()
        if jk.replace("Z", "+00:00") != str(ck).replace("Z", "+00:00"):
            mismatches.append(str(ParityMismatch("known_at", jk, ck, context=pid)))

    rel = relative_dest_for(catalog_root, json_path)
    frozen = payload.get("frozen_input") or {}
    mismatches.extend(
        _audit_pool_detail_checks(audit_pool_ids, json_by_id, cat_by_id, stem=stem)
    )
    if str(frozen.get("snapshot_id") or "") != bundle["frozen_input_id"]:
        mismatches.append(
            str(ParityMismatch("frozen_input_id", bundle["frozen_input_id"], frozen.get("snapshot_id")))
        )
    notes = [f"provenance:relative_dest={rel}"]
    hard = [m for m in mismatches]
    return ParityReport(stem, ok=not hard, mismatches=hard + notes)


def _audit_pool_detail_checks(
    audit_ids: Sequence[str],
    json_by_id: dict,
    cat_by_id: dict,
    *,
    stem: str,
) -> list[str]:
    """
    Symmetric ACTIVE membership for fixed audit pool IDs at one decision close.

    - ACTIVE in both JSON and CH: field parity is enforced in ``compare_json_to_published``
      for all ACTIVE pools (audit IDs are a subset when ACTIVE).
    - ACTIVE in neither: PASS.
    - ACTIVE in only one side: FAIL.
    """
    out: list[str] = []
    for pid in audit_ids:
        in_json = pid in json_by_id
        in_ch = pid in cat_by_id
        if in_json and in_ch:
            continue
        if not in_json and not in_ch:
            continue
        if in_json and not in_ch:
            out.append(
                f"audit pool {pid} ACTIVE in JSON but missing in CH catalog at {stem}"
            )
        elif in_ch and not in_json:
            out.append(
                f"audit pool {pid} in CH catalog but not ACTIVE in JSON at {stem}"
            )
    return out


def compare_json_to_plan(bundle: dict[str, Any]) -> ParityReport:
    """Dry-run parity: JSON vs in-memory import plan (no ClickHouse)."""
    payload = bundle["payload"]
    state = bundle["state"]
    stem = bundle["path"].stem
    mismatches: list[str] = []
    if active_state_fingerprint(state.active_pool_ids, state.active_sides) != payload.get(
        "active_state_fingerprint"
    ):
        mismatches.append("active_state_fingerprint drift")
    if len(bundle["catalog_rows"]) != state.active_pool_count:
        mismatches.append("catalog_rows vs active_pool_count")
    try:
        validate_catalog_rows_in_memory(bundle["catalog_rows"], state)
    except Exception as exc:
        mismatches.append(f"catalog identity: {exc}")
    json_by_id = (payload.get("pools") or {})
    mismatches.extend(
        compare_catalog_rows_to_json_fields(bundle["catalog_rows"], json_by_id)
    )
    return ParityReport(stem, ok=not mismatches, mismatches=mismatches)


def run_pilot_readback(
    stems: Sequence[str],
    *,
    catalog_root: Path,
    combo_rel: str = "production/results/2026-08/15m_PD10",
    pipeline_line_id: str,
    config_hash_value: str,
    use_clickhouse: bool = False,
    fail_closed: bool | None = None,
) -> list[ParityReport]:
    if fail_closed is None:
        fail_closed = use_clickhouse
    reports: list[ParityReport] = []
    for stem in stems:
        path = catalog_root / combo_rel / f"{stem}.json"
        if use_clickhouse:
            reports.append(
                compare_json_to_published(
                    path,
                    catalog_root=catalog_root,
                    pipeline_line_id=pipeline_line_id,
                    config_hash_value=config_hash_value,
                )
            )
        else:
            manifest = load_manifest_context(catalog_root)
            pd = int(json.loads(path.read_text())["pool_detail"])
            ch = config_hash(build_live_micro_lld_v2_config(pd))
            bundle = build_import_bundle(
                path,
                catalog_root=catalog_root,
                config_hash=ch,
                manifest=manifest,
                pipeline_line_id=pipeline_line_id,
            )
            reports.append(compare_json_to_plan(bundle))
    if fail_closed:
        bad = [r for r in reports if not r.ok]
        if bad:
            raise ReadbackGateFailure(bad)
    return reports
