"""Phase 2C catalog identity, snapshot_id, partial catalog, readback gate."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import ActiveStateSnapshot
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
    active_state_fingerprint,
    deterministic_snapshot_id,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_integrity import (
    CatalogIdentityError,
    assert_member_catalog_id_sets,
    validate_catalog_rows_in_memory,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_write import (
    CatalogIncompleteError,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close import (
    import_historical_close,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_errors import (
    ReadbackGateFailure,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
    ParityReport,
    run_pilot_readback,
)
from research_charts.live_micro_lld_v2_ch_test.identity import ENGINE_VERSION


def test_snapshot_id_ignores_active_membership():
    dc = datetime(2026, 8, 1, tzinfo=timezone.utc)
    cls = datetime(2026, 4, 1, tzinfo=timezone.utc)
    base = {
        "symbol": "DOGEUSDT",
        "timeframe": "15m",
        "pool_detail": 10,
        "config_hash": "h",
        "candle_load_start": cls,
        "decision_close": dc,
        "prefix_candle_fingerprint": "pfx",
        "engine_version": ENGINE_VERSION,
    }
    sid_a = deterministic_snapshot_id(**base)
    fp1 = active_state_fingerprint(["a"], ["lower"])
    fp2 = active_state_fingerprint(["b"], ["upper"])
    assert sid_a == deterministic_snapshot_id(**base)
    assert fp1 != fp2
    assert fp1 not in sid_a and fp2 not in sid_a


def test_validate_catalog_rows_duplicate_pool_id():
    st = ActiveStateSnapshot(
        symbol="DOGEUSDT",
        timeframe="15m",
        pool_detail=10,
        config_hash="c",
        engine_version=ENGINE_VERSION,
        engine_input_hash="e",
        candle_load_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        history_from_close=datetime(2026, 4, 1, tzinfo=timezone.utc),
        decision_close=datetime(2026, 8, 1, tzinfo=timezone.utc),
        prefix_candle_count=1,
        prefix_candle_fingerprint="p",
        known_pool_count=1,
        active_pool_ids=("x",),
        active_sides=("lower",),
        active_upper_count=0,
        active_lower_count=1,
        active_state_fingerprint=active_state_fingerprint(["x"], ["lower"]),
    )
    rows = [
        {"pool_id": "x", "side": "lower"},
        {"pool_id": "x", "side": "lower"},
    ]
    with pytest.raises(CatalogIdentityError, match="duplicate"):
        validate_catalog_rows_in_memory(rows, st)


def test_assert_member_catalog_id_sets_mismatch():
    with pytest.raises(CatalogIdentityError):
        assert_member_catalog_id_sets(["a", "b"], ["a"])


def test_import_partial_published_catalog_stops():
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_guard import LineIdentity
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close import (
        import_historical_close,
    )
    from research_charts.tests.test_historical_import_recovery import _catalog_rows, _line, _state

    st = _state()
    with (
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.resolve_existing_publish",
            return_value={"status": "skipped_existing_publish", "publish_id": "p", "snapshot_id": "s"},
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.catalog_row_count",
            return_value=5,
        ),
        mock.patch("clickhouse_connect.get_client"),
    ):
        with pytest.raises(CatalogIncompleteError, match="partial"):
            import_historical_close(
                st,
                _catalog_rows(st),
                pipeline_line_id="line",
                test_run_id="00000000-0000-4000-8000-000000000001",
                line=_line(st),
            )


def test_readback_fail_closed_on_bad_report():
    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity.compare_json_to_published",
        return_value=ParityReport("t", False, ["mismatch"]),
    ):
        with pytest.raises(ReadbackGateFailure):
            run_pilot_readback(
                ["t"],
                catalog_root=Path("/tmp"),
                pipeline_line_id="l",
                config_hash_value="c",
                use_clickhouse=True,
            )
