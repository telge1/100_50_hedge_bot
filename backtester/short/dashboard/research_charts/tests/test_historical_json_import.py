"""Historical JSON import dry-run (no ClickHouse)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_PIPELINE_LINE_ID,
    PILOT_POOL_DETAIL,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.runner import (
    ImportMode,
    run_import_plan,
)


def test_dry_run_does_not_touch_clickhouse():
    with mock.patch("clickhouse_connect.get_client") as get_client:
        get_client.side_effect = AssertionError("ClickHouse must not be called in VALIDATE mode")
        report = run_import_plan(
            PILOT_DECISION_CLOSE_STEMS[:2],
            catalog_root=CATALOG_ROOT,
            pipeline_line_id=PILOT_PIPELINE_LINE_ID,
            mode=ImportMode.VALIDATE,
        )
    assert not report["errors"]
    assert report["summary"]["snapshot_count"] == 2
    for close in report["closes"]:
        assert close["active_pool_count"] == len(
            __import__("json").loads(
                (
                    CATALOG_ROOT
                    / "production/results/2026-08/15m_PD10"
                    / f"{close['stem']}.json"
                ).read_text()
            )["active_pool_ids"]
        )


def test_pilot_write_blocked_without_env():
    import pytest

    with pytest.raises(RuntimeError):
        run_import_plan(
            [PILOT_DECISION_CLOSE_STEMS[0]],
            catalog_root=CATALOG_ROOT,
            pipeline_line_id=PILOT_PIPELINE_LINE_ID,
            mode=ImportMode.PILOT_WRITE,
        )


def test_full_membership_catalog_row_count():
    report = run_import_plan(
        [PILOT_DECISION_CLOSE_STEMS[0]],
        catalog_root=CATALOG_ROOT,
        pipeline_line_id=PILOT_PIPELINE_LINE_ID,
        mode=ImportMode.VALIDATE,
    )
    active = report["closes"][0]["active_pool_count"]
    assert report["summary"]["member_rows"] == active
    assert report["summary"]["catalog_rows"] == active


def test_readback_plan_parity_all_pilot_closes():
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
        run_pilot_readback,
    )
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config, config_hash

    ch = config_hash(build_live_micro_lld_v2_config(PILOT_POOL_DETAIL))
    reports = run_pilot_readback(
        PILOT_DECISION_CLOSE_STEMS,
        catalog_root=CATALOG_ROOT,
        pipeline_line_id=PILOT_PIPELINE_LINE_ID,
        config_hash_value=ch,
        use_clickhouse=False,
    )
    assert len(reports) == len(PILOT_DECISION_CLOSE_STEMS)
    assert all(r.ok for r in reports)
