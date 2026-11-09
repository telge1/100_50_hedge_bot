"""Post-approval pilot write orchestration (not invoked in Masterplan Phase 2)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Sequence

from research_charts.trp_import import ensure_trp_on_path

from ..active_snapshot.publish_guard import LineIdentity
from ..identity import build_live_micro_lld_v2_config, config_hash
from .import_close import import_historical_close
from .parse import build_import_bundle, load_manifest_context, snapshot_path_for_stem
from .pilot_config import CATALOG_ROOT, PILOT_PIPELINE_LINE_ID
from .validate import validate_bundle


def execute_pilot_import(
    stems: Sequence[str],
    *,
    catalog_root: Path = CATALOG_ROOT,
    pipeline_line_id: str = PILOT_PIPELINE_LINE_ID,
    test_run_id: str | None = None,
) -> list[dict]:
    if os.environ.get("HISTORICAL_JSON_IMPORT_ALLOW_WRITE") != "1":
        raise RuntimeError("set HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1 before execute_pilot_import")
    ensure_trp_on_path()
    manifest = load_manifest_context(catalog_root)
    run_id = test_run_id or str(uuid.uuid4())
    results: list[dict] = []
    for stem in stems:
        path = snapshot_path_for_stem(
            catalog_root, month="2026-08", combo="15m_PD10", stem=str(stem)
        )
        payload = __import__("json").loads(path.read_text(encoding="utf-8"))
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
        state = bundle["state"]
        sid = bundle["snapshot_id"]
        line = LineIdentity(
            symbol=state.symbol,
            timeframe=state.timeframe,
            pool_detail=state.pool_detail,
            config_hash=state.config_hash,
            engine_version=state.engine_version,
            candle_load_start=state.candle_load_start,
            history_from_close=state.history_from_close,
            history_policy=state.history_policy,
        )
        outcome = import_historical_close(
            state,
            bundle["catalog_rows"],
            pipeline_line_id=pipeline_line_id,
            test_run_id=run_id,
            line=line,
        )
        results.append({"stem": stem, "snapshot_id": sid, **outcome})
    return results
