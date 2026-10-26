"""Explicit September 2026 immutable input binding for 4X-AO month_validate (Phase 4X-AQ).

Without JUN_SEP_SEPTEMBER_IMMUTABLE_BINDING_V1=1, behavior is unchanged (default MULTI/inputs).
With env set, only month 2026-09 uses the pinned staging snapshot; all other months use default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TOOL = Path(__file__).resolve().parent
ROOT = TOOL.parents[1]
POOLS_DASH = ROOT / "backtester/short/dashboard"
MULTI = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"

BINDING_PATH = TOOL / "september_immutable_binding_v1.json"
BINDING_ENV = "JUN_SEP_SEPTEMBER_IMMUTABLE_BINDING_V1"
SEPTEMBER_MONTH = "2026-09"


class ImmutableBindingError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ResolvedImmutableInput:
    month: str
    inputs_dir: Path
    manifest: dict[str, Any]
    binding_mode: str  # "default" | "september_pinned_v1"

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def ohlcv_content_hash(self) -> str:
        return str(self.manifest["ohlcv_content_hash"])


def binding_env_enabled() -> bool:
    return os.environ.get(BINDING_ENV, "").strip() == "1"


def load_binding_spec() -> dict[str, Any]:
    if not BINDING_PATH.is_file():
        raise ImmutableBindingError("BINDING_FILE_MISSING", f"missing {BINDING_PATH}")
    spec = json.loads(BINDING_PATH.read_text(encoding="utf-8"))
    required = (
        "binding_version",
        "applies_to_month",
        "snapshot_id",
        "ohlcv_content_hash",
        "inputs_directory",
        "range_end_exclusive",
        "expected_row_count",
    )
    missing = [k for k in required if k not in spec]
    if missing:
        raise ImmutableBindingError(
            "BINDING_SPEC_INVALID",
            f"binding spec missing keys: {missing}",
        )
    return spec


def _parse_manifest_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _resolve_default(month: str) -> ResolvedImmutableInput:
    inputs_dir = MULTI / "inputs"
    manifests = sorted(inputs_dir.glob("*.manifest.json"))
    if len(manifests) != 1:
        raise ImmutableBindingError(
            "DEFAULT_MANIFEST_AMBIGUOUS",
            f"expected exactly one manifest under {inputs_dir}, found {len(manifests)}",
            {"paths": [str(p) for p in manifests]},
        )
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    return ResolvedImmutableInput(
        month=month,
        inputs_dir=inputs_dir,
        manifest=manifest,
        binding_mode="default",
    )


def _resolve_september_pinned(month: str, spec: dict[str, Any]) -> ResolvedImmutableInput:
    if month != spec.get("applies_to_month"):
        raise ImmutableBindingError(
            "BINDING_MONTH_MISMATCH",
            f"pinned binding applies to {spec.get('applies_to_month')}, not {month}",
        )
    rel = Path(str(spec["inputs_directory"]))
    inputs_dir = (ROOT / rel).resolve() if not rel.is_absolute() else rel.resolve()
    if MULTI.resolve() in inputs_dir.parents and inputs_dir.name == "inputs":
        prod_inputs = MULTI / "inputs"
        if inputs_dir.resolve() == prod_inputs.resolve():
            raise ImmutableBindingError(
                "BINDING_REFUSES_PRODUCTION_INPUTS",
                "pinned September binding must not point at production MULTI/inputs",
            )
    sid = str(spec["snapshot_id"])
    ohlcv = str(spec["ohlcv_content_hash"])
    manifest_path = inputs_dir / f"{sid}.manifest.json"
    if not manifest_path.is_file():
        raise ImmutableBindingError(
            "BINDING_MANIFEST_MISSING",
            f"manifest not found: {manifest_path}",
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != sid:
        raise ImmutableBindingError(
            "BINDING_SNAPSHOT_ID_MISMATCH",
            "manifest snapshot_id does not match binding spec",
            {"manifest": manifest.get("snapshot_id"), "expected": sid},
        )
    if manifest.get("ohlcv_content_hash") != ohlcv:
        raise ImmutableBindingError(
            "BINDING_OHLCV_HASH_MISMATCH",
            "manifest ohlcv_content_hash does not match binding spec",
        )
    if int(manifest.get("row_count", -1)) != int(spec["expected_row_count"]):
        raise ImmutableBindingError(
            "BINDING_ROW_COUNT_MISMATCH",
            "manifest row_count does not match binding spec",
            {"manifest": manifest.get("row_count"), "expected": spec["expected_row_count"]},
        )
    if manifest.get("range_end_exclusive") != spec["range_end_exclusive"]:
        raise ImmutableBindingError(
            "BINDING_RANGE_MISMATCH",
            "manifest range_end_exclusive does not match binding spec",
        )
    parquet = inputs_dir / f"{sid}.parquet"
    if not parquet.is_file():
        raise ImmutableBindingError("BINDING_PARQUET_MISSING", f"missing {parquet}")

    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
        ohlcv_content_hash,
        require_snapshot,
    )

    rows = require_snapshot(inputs_dir, sid, ohlcv_hash=ohlcv)
    if len(rows) != int(spec["expected_row_count"]):
        raise ImmutableBindingError(
            "BINDING_COVERAGE_INCOMPLETE",
            "snapshot row count after require_snapshot mismatch",
            {"loaded": len(rows), "expected": spec["expected_row_count"]},
        )
    end_excl = _parse_manifest_ts(str(spec["range_end_exclusive"]))
    if rows[-1].open_time + timedelta(minutes=1) != end_excl:
        raise ImmutableBindingError(
            "BINDING_COVERAGE_TAIL",
            "last 1m row does not end at range_end_exclusive",
            {"last_open": str(rows[-1].open_time), "range_end_exclusive": spec["range_end_exclusive"]},
        )
    base_hash = spec.get("base_ohlcv_content_hash")
    base_rows = int(spec.get("expected_row_count", 0)) - 43200
    if base_hash and base_rows > 0 and len(rows) >= base_rows:
        if ohlcv_content_hash(rows[:base_rows]) != base_hash:
            raise ImmutableBindingError(
                "BINDING_PREFIX_INTEGRITY",
                "merged snapshot prefix ohlcv hash does not match base snapshot",
            )
    return ResolvedImmutableInput(
        month=month,
        inputs_dir=inputs_dir,
        manifest=manifest,
        binding_mode="september_pinned_v1",
    )


def resolve_immutable_input(month: str) -> ResolvedImmutableInput:
    """Fail-closed resolver: default MULTI/inputs unless September + binding env."""
    if month == SEPTEMBER_MONTH and binding_env_enabled():
        return _resolve_september_pinned(month, load_binding_spec())
    return _resolve_default(month)


def load_session_for_month(month: str):
    """Return (ReusedSnapshotSession, identity dict, resolved)."""
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
        require_snapshot,
        revision_metadata_hash,
    )
    from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession

    resolved = resolve_immutable_input(month)
    sid = resolved.snapshot_id
    ph = resolved.ohlcv_content_hash
    rows = require_snapshot(resolved.inputs_dir, sid, ohlcv_hash=ph)
    identity = {
        "snapshot_id": sid,
        "price_hash": ph,
        "revision_hash": revision_metadata_hash(rows),
        "clickhouse_candle_reads": 0,
        "immutable_binding_mode": resolved.binding_mode,
    }
    if resolved.binding_mode == "september_pinned_v1":
        spec = load_binding_spec()
        identity["immutable_binding_version"] = spec.get("binding_version")
        identity["immutable_binding_base_snapshot_id"] = spec.get("base_snapshot_id")
    session = ReusedSnapshotSession(rows, snapshot_id=sid, ohlcv_hash=ph, timeframe="5m")
    session.max_1m_bars = 500_000
    return session, identity, resolved
