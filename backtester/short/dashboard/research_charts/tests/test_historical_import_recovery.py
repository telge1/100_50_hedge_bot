"""Failure recovery and idempotency for historical import orchestration (mock CH)."""

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
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.faults import SimulatedCrash
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_guard import (
    LineIdentity,
    PublishConflictError,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publisher import (
    commit_publish_ledger,
    resolve_existing_publish,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.writer import PartialSnapshotError
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_write import (
    CatalogIncompleteError,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close import (
    import_historical_close,
)
from research_charts.live_micro_lld_v2_ch_test.identity import ENGINE_VERSION


def _state(**kw) -> ActiveStateSnapshot:
    dc = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    base = {
        "symbol": "DOGEUSDT",
        "timeframe": "15m",
        "pool_detail": 10,
        "config_hash": "cfg",
        "engine_version": ENGINE_VERSION,
        "engine_input_hash": "eih",
        "candle_load_start": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "history_from_close": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "decision_close": dc,
        "prefix_candle_count": 10,
        "prefix_candle_fingerprint": "pfx",
        "known_pool_count": 2,
        "active_pool_ids": ("a", "b"),
        "active_sides": ("lower", "upper"),
        "active_upper_count": 1,
        "active_lower_count": 1,
        "active_state_fingerprint": active_state_fingerprint(["a", "b"], ["lower", "upper"]),
        "history_policy": "ROLLING_4_CALENDAR_MONTHS",
    }
    base.update(kw)
    return ActiveStateSnapshot(**base)


def _line(st: ActiveStateSnapshot) -> LineIdentity:
    return LineIdentity(
        symbol=st.symbol,
        timeframe=st.timeframe,
        pool_detail=st.pool_detail,
        config_hash=st.config_hash,
        engine_version=st.engine_version,
        candle_load_start=st.candle_load_start,
        history_from_close=st.history_from_close,
        history_policy=st.history_policy,
    )


def _catalog_rows(st: ActiveStateSnapshot, sid: str = "sid") -> list[dict]:
    return [
        {
            "snapshot_id": sid,
            "pipeline_line_id": "line",
            "symbol": st.symbol,
            "timeframe": st.timeframe,
            "pool_detail": st.pool_detail,
            "decision_close": st.decision_close,
            "history_policy": st.history_policy,
            "pool_id": pid,
            "pool_type": "micro",
            "side": side,
            "top_price": 1.0,
            "bottom_price": 0.9,
            "strength": 1.0,
            "strength_class": "Medium",
            "known_at": st.decision_close,
            "break_at": None,
            "reclaim_level": None,
            "candidate_created_at": None,
            "confirmed_at": None,
            "invalidated_at": None,
            "created_index": 0,
            "row_fingerprint": f"fp-{pid}",
        }
        for pid, side in zip(st.active_pool_ids, st.active_sides)
    ]


@pytest.fixture
def mock_ch_stack():
    publish_store: dict = {}
    catalog_counts: dict[str, int] = {}

    def fake_resolve(client, db, state, *, pipeline_line_id, snapshot_id, allow_existing_publish):
        key = (pipeline_line_id, state.config_hash, state.decision_close.isoformat())
        row = publish_store.get(key)
        if not row:
            return None
        if row["snapshot_id"] != snapshot_id:
            raise PublishConflictError("snapshot_id conflict")
        if row["prefix_candle_fingerprint"] != state.prefix_candle_fingerprint:
            raise PublishConflictError("prefix conflict")
        if row["active_state_fingerprint"] != state.active_state_fingerprint:
            raise PublishConflictError("fingerprint conflict")
        return {
            "status": "skipped_existing_publish",
            "publish_id": row["publish_id"],
            "snapshot_id": row["snapshot_id"],
        }

    with (
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.resolve_existing_publish",
            side_effect=fake_resolve,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.catalog_row_count",
            side_effect=lambda sid: catalog_counts.get(sid, 0),
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.write_active_snapshot_staged",
        ) as staged,
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.write_active_pool_catalog_from_json_rows",
        ) as cat_write,
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.commit_publish_ledger",
        ) as commit,
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.historical_json_import.import_close.verify_ch_catalog_identity",
        ),
        mock.patch("clickhouse_connect.get_client"),
    ):
        staged.return_value = {"status": "written"}
        def _cat_write(rows, snapshot_id, expected_count=None, state=None):
            catalog_counts[snapshot_id] = expected_count or len(rows)
            return {"status": "written", "count": catalog_counts[snapshot_id]}

        cat_write.side_effect = _cat_write
        commit.return_value = {"status": "published", "publish_id": "p1", "snapshot_id": "sid"}
        yield {
            "staged": staged,
            "cat_write": cat_write,
            "commit": commit,
            "publish_store": publish_store,
            "catalog_counts": catalog_counts,
        }


def test_first_full_import_calls_staged_catalog_then_publish(mock_ch_stack):
    st = _state()
    rows = _catalog_rows(st)
    out = import_historical_close(
        st, rows, pipeline_line_id="line", test_run_id="00000000-0000-4000-8000-000000000001", line=_line(st)
    )
    assert mock_ch_stack["staged"].called
    assert mock_ch_stack["cat_write"].called
    assert mock_ch_stack["commit"].called
    assert out["publish"]["status"] == "published"


def test_repeat_full_import_skips_when_publish_and_catalog_complete(mock_ch_stack):
    st = _state()
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.writer import snapshot_id_for_state

    sid = snapshot_id_for_state(st)
    key = ("line", st.config_hash, st.decision_close.isoformat())
    mock_ch_stack["publish_store"][key] = {
        "publish_id": "p1",
        "snapshot_id": sid,
        "prefix_candle_fingerprint": st.prefix_candle_fingerprint,
        "active_state_fingerprint": st.active_state_fingerprint,
    }
    mock_ch_stack["catalog_counts"][sid] = st.active_pool_count
    out = import_historical_close(
        st, _catalog_rows(st, sid), pipeline_line_id="line", test_run_id="00000000-0000-4000-8000-000000000001", line=_line(st)
    )
    assert out["publish"]["status"] == "skipped_existing_publish"
    mock_ch_stack["staged"].assert_not_called()


def test_publish_without_catalog_repairs_on_retry(mock_ch_stack):
    st = _state()
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.writer import snapshot_id_for_state

    sid = snapshot_id_for_state(st)
    key = ("line", st.config_hash, st.decision_close.isoformat())
    mock_ch_stack["publish_store"][key] = {
        "publish_id": "p1",
        "snapshot_id": sid,
        "prefix_candle_fingerprint": st.prefix_candle_fingerprint,
        "active_state_fingerprint": st.active_state_fingerprint,
    }
    mock_ch_stack["catalog_counts"][sid] = 0
    import_historical_close(
        st, _catalog_rows(st, sid), pipeline_line_id="line", test_run_id="00000000-0000-4000-8000-000000000001", line=_line(st)
    )
    assert mock_ch_stack["cat_write"].called


def test_conflicting_snapshot_id_raises(mock_ch_stack):
    st = _state()
    key = ("line", st.config_hash, st.decision_close.isoformat())
    mock_ch_stack["publish_store"][key] = {
        "publish_id": "p1",
        "snapshot_id": "other-sid",
        "prefix_candle_fingerprint": st.prefix_candle_fingerprint,
        "active_state_fingerprint": st.active_state_fingerprint,
    }
    with pytest.raises(PublishConflictError):
        import_historical_close(
            st, _catalog_rows(st), pipeline_line_id="line", test_run_id="00000000-0000-4000-8000-000000000001", line=_line(st)
        )


def test_catalog_incomplete_raises_on_retry(monkeypatch):
    monkeypatch.setenv("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", "1")
    with mock.patch(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_write.clickhouse_connect.get_client"
    ) as gc:
        client = mock.MagicMock()
        gc.return_value = client
        client.query.return_value.result_rows = [[5]]
        from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_write import (
            write_active_pool_catalog_from_json_rows,
        )

        with pytest.raises(CatalogIncompleteError):
            write_active_pool_catalog_from_json_rows(
                [{"snapshot_id": "s", "pool_id": "p"}],
                snapshot_id="s",
                expected_count=10,
            )


def test_staged_writer_partial_members_raises():
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot import staged_writer

    st = _state()
    client = mock.MagicMock()
    client.query.return_value.result_rows = []
    with (
        mock.patch.object(staged_writer, "_meta_row_complete", return_value=None),
        mock.patch.object(staged_writer, "_member_count", return_value=3),
        mock.patch.object(staged_writer, "load_clickhouse_config"),
        mock.patch("clickhouse_connect.get_client", return_value=client),
        mock.patch.object(staged_writer, "ensure_active_snapshot_tables"),
    ):
        with pytest.raises(PartialSnapshotError):
            staged_writer.write_active_snapshot_staged(st, test_run_id="00000000-0000-4000-8000-000000000001")


def test_commit_publish_aborts_before_insert():
    st = _state()
    with (
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.active_snapshot.publisher.resolve_existing_publish",
            return_value=None,
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.active_snapshot.publisher.validate_snapshot_against_state",
            return_value={"validated": True},
        ),
        mock.patch(
            "research_charts.live_micro_lld_v2_ch_test.active_snapshot.publisher.insert_publish_ledger_row",
        ) as ins,
        mock.patch("clickhouse_connect.get_client") as gc,
    ):
        client = mock.MagicMock()
        gc.return_value = client
        from research_charts.live_micro_lld_v2_ch_test.active_snapshot.faults import FaultInjector

        with pytest.raises(SimulatedCrash):
            commit_publish_ledger(
                st,
                pipeline_line_id="line",
                line=_line(st),
                faults=FaultInjector(points={"before_publish_insert"}),
            )
        ins.assert_not_called()
