from __future__ import annotations

"""Tests for offline addon short recovery audit tooling."""

from pathlib import Path
import copy

from research.backtests.tools.addon_recovery_audit import (
    AuditEvent,
    _analyze_events_phase1,
    _apply_phase2_pnl_and_reduce_analysis,
    run_single_trade_audit,
)


def _simple_fill_row(candle_index: int, long_after: float, short_after: float) -> dict:
    return {
        "row_type": "fill",
        "candle_index": candle_index,
        "long_qty_after": long_after,
        "short_qty_after": short_after,
    }


def _base_run() -> dict:
    return {
        "symbol": "APTUSDT",
        "direction": "long",
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T01:00:00+00:00",
        "start_index": 0,
        "end_index": 10,
        "trade_number": 12,
        "trade_block_id": "backtest_long_continuous_trade_0012",
        "addon_short_recovery_activation_candle_index": 1,
        "addon_short_recovery_activation_price": 100.0,
        "addon_short_recovery_gap_at_activation": 6.0,
        "addon_short_step_fraction": 0.25,
        "allow_net_short": False,
        "addon_short_trade_count": 0,
        "addon_short_tp_count": 0,
        "addon_short_rebound_exit_count": 0,
        "addon_short_hard_stop_count": 0,
        "addon_short_events": [],
    }


def test_run_single_trade_audit_smoke(tmp_path: Path) -> None:
    """Smoke test: audit helper can run on a minimal synthetic payload."""

    # Minimal continuous_results with one run and no addon_short_events.
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    continuous = results_dir / "APTUSDT_original_hedge_5m_continuous_results.json"
    contents = """{
  "metadata": {},
  "runs": [
    {
      "symbol": "APTUSDT",
      "direction": "long",
      "start_time": "2026-01-01T00:00:00+00:00",
      "end_time": "2026-01-01T01:00:00+00:00",
      "start_index": 0,
      "end_index": 10,
      "trade_number": 12,
      "trade_block_id": "backtest_long_continuous_trade_0012",
      "addon_short_recovery_activation_candle_index": null,
      "addon_short_recovery_activation_price": null,
      "addon_short_trade_count": 0,
      "addon_short_tp_count": 0,
      "addon_short_rebound_exit_count": 0,
      "addon_short_hard_stop_count": 0,
      "addon_short_events": []
    }
  ],
  "aggregate": []
}
"""
    continuous.write_text(contents, encoding="utf-8")

    # Minimal trade_blocks JSON structure for the same trade.
    trade_blocks = (
        results_dir
        / "APTUSDT_backtest_long_continuous_trade_0012_conservative_live_trade_blocks.json"
    )
    trade_blocks.write_text(
        """{
  "metadata": {},
  "trade_blocks": []
}
""",
        encoding="utf-8",
    )

    paths = run_single_trade_audit(
        results_dir=results_dir,
        trade_block_id="backtest_long_continuous_trade_0012",
    )

    assert paths["audit_csv"].is_file()
    # In the minimal synthetic payload there are no addon_short_events, so
    # the trade-summary CSV may legitimately be missing.
    assert paths["audit_json"].is_file()
    assert paths["audit_md"].is_file()

    # Input results JSON must not be modified by the audit.
    assert continuous.read_text(encoding="utf-8") == contents


def test_entry_tp_pairing_ok() -> None:
    """Normal entry + TP are paired into a single trade."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0), _simple_fill_row(3, 10.0, 4.0)]

    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
            entry_price=101.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=3,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_TP",
            exit_price=99.0,
        ),
    ]

    enriched, trades, stats = _analyze_events_phase1(run, trade_rows, events)

    assert stats["entry_count"] == 1
    assert stats["close_count"] == 1
    assert stats["tp_count"] == 1
    assert len(trades) == 1
    trade = trades[0]
    assert trade["trade_status"] == "CLOSED_TP"
    assert trade["entry_event_sequence"] == 1
    assert trade["close_event_sequence"] == 2
    # Flags for the entry must all be True.
    entry_ev = enriched[1]
    assert entry_ev.single_addon_position_ok is True
    assert entry_ev.entry_qty_within_gap_ok is True
    assert entry_ev.combined_not_net_short_ok is True
    assert entry_ev.no_same_candle_reentry_ok is True


def test_entry_rebound_pairing_ok() -> None:
    """Normal entry + Rebound close are paired."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0), _simple_fill_row(4, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
            entry_price=101.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=4,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_REBOUND_EXIT",
            exit_price=100.5,
        ),
    ]

    _, trades, stats = _analyze_events_phase1(run, trade_rows, events)
    assert stats["entry_count"] == 1
    assert stats["close_count"] == 1
    assert stats["rebound_count"] == 1
    assert len(trades) == 1
    trade = trades[0]
    assert trade["trade_status"] == "CLOSED_REBOUND"
    assert trade["close_type"] == "REBOUND"


def test_entry_hard_stop_pairing_ok() -> None:
    """Normal entry + Hard-Stop close are paired."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0), _simple_fill_row(5, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
            entry_price=101.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=5,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_HARD_STOP",
            exit_price=103.0,
        ),
    ]

    _, trades, stats = _analyze_events_phase1(run, trade_rows, events)
    assert stats["entry_count"] == 1
    assert stats["close_count"] == 1
    assert stats["hard_stop_count"] == 1
    assert len(trades) == 1
    trade = trades[0]
    assert trade["trade_status"] == "CLOSED_HARD_STOP"
    assert trade["close_type"] == "HARD_STOP"


def test_close_without_entry_fails() -> None:
    """A close without a matching entry is flagged."""

    run = _base_run()
    trade_rows = [_simple_fill_row(3, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=3,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_TP",
            exit_price=100.0,
        ),
    ]

    enriched, trades, stats = _analyze_events_phase1(run, trade_rows, events)
    assert stats["unmatched_close_count"] == 1
    assert len(trades) == 0
    close_ev = enriched[1]
    assert close_ev.close_has_matching_entry_ok is False
    assert "close_without_matching_entry" in (close_ev.audit_error or "")


def test_second_entry_while_open_fails() -> None:
    """Second entry while an addon short is still open is flagged."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0), _simple_fill_row(2, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=1,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=2,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=1.0,
        ),
    ]

    enriched, _, _ = _analyze_events_phase1(run, trade_rows, events)
    second_entry = enriched[2]
    assert second_entry.single_addon_position_ok is False
    assert "entry_with_existing_open_addon_short" in (second_entry.audit_error or "")


def test_entry_qty_exceeds_gap_fails_and_net_short_flagged() -> None:
    """Entry quantity larger than remaining gap and causing net-short is flagged."""

    run = _base_run()
    # long=5, short=4 -> remaining gap vs normal short is 1
    trade_rows = [_simple_fill_row(2, 5.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
        ),
    ]

    enriched, _, _ = _analyze_events_phase1(run, trade_rows, events)
    ev = enriched[1]
    assert ev.entry_qty_within_gap_ok is False
    assert ev.combined_not_net_short_ok is False
    assert "entry_qty_exceeds_remaining_gap" in (ev.audit_error or "")
    assert "net_short_violation_on_entry" in (ev.audit_error or "")


def test_long_reduce_exceeds_gap_fails() -> None:
    """Long-reduce larger than remaining gap is flagged."""

    run = _base_run()
    # long=5, short=4, addon=1 -> remaining gap 0
    trade_rows = [_simple_fill_row(2, 5.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=1.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=2,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_LONG_REDUCE",
            executed_long_reduce_qty=3.0,
        ),
    ]

    enriched, _, _ = _analyze_events_phase1(run, trade_rows, events)
    reduce_ev = enriched[2]
    assert reduce_ev.long_reduce_within_gap_ok is False
    assert "long_reduce_exceeds_remaining_gap" in (reduce_ev.audit_error or "")


def test_same_candle_reentry_after_tp_fails() -> None:
    """Entry in same candle after TP is flagged."""

    run = _base_run()
    trade_rows = [_simple_fill_row(5, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=5,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=5,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_TP",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=3,
            candle_index=5,
            timestamp="t3",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=1.0,
        ),
    ]

    enriched, _, _ = _analyze_events_phase1(run, trade_rows, events)
    reentry = enriched[3]
    assert reentry.no_same_candle_reentry_ok is False
    assert "same_candle_reentry_after_close" in (reentry.audit_error or "")


def test_same_candle_reentry_after_rebound_fails() -> None:
    """Entry in same candle after Rebound close is flagged."""

    run = _base_run()
    trade_rows = [_simple_fill_row(6, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=6,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=6,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_REBOUND_EXIT",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=3,
            candle_index=6,
            timestamp="t3",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=1.0,
        ),
    ]

    enriched, _, _ = _analyze_events_phase1(run, trade_rows, events)
    reentry = enriched[3]
    assert reentry.no_same_candle_reentry_ok is False
    assert "same_candle_reentry_after_close" in (reentry.audit_error or "")


def test_same_candle_reentry_after_hard_stop_fails() -> None:
    """Entry in same candle after Hard-Stop close is flagged."""

    run = _base_run()
    trade_rows = [_simple_fill_row(7, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=7,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=7,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_HARD_STOP",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=3,
            candle_index=7,
            timestamp="t3",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=1.0,
        ),
    ]

    enriched, _, _ = _analyze_events_phase1(run, trade_rows, events)
    reentry = enriched[3]
    assert reentry.no_same_candle_reentry_ok is False
    assert "same_candle_reentry_after_close" in (reentry.audit_error or "")


def test_open_trade_marked_at_series_end() -> None:
    """Open trade at series end is marked as OPEN_AT_SERIES_END."""

    run = _base_run()
    run["end_index"] = 20
    trade_rows = [_simple_fill_row(10, 10.0, 4.0), _simple_fill_row(20, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=10,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=20,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_SERIES_END",
        ),
    ]

    _, trades, stats = _analyze_events_phase1(run, trade_rows, events)
    assert stats["open_at_end_count"] == 1
    assert len(trades) == 1
    trade = trades[0]
    assert trade["trade_status"] == "OPEN_AT_SERIES_END"


def test_event_counts_and_aggregate_comparison() -> None:
    """Event counts are reconstructed and compared with stored aggregates."""

    run = _base_run()
    # Stored aggregates for this synthetic sequence (1 entry, 1 TP).
    run["addon_short_trade_count"] = 1
    run["addon_short_tp_count"] = 1
    run["addon_short_rebound_exit_count"] = 0
    run["addon_short_hard_stop_count"] = 0

    trade_rows = [_simple_fill_row(1, 10.0, 4.0), _simple_fill_row(3, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=3,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_TP",
        ),
    ]

    _, _, stats = _analyze_events_phase1(run, trade_rows, events)
    assert stats["entry_count"] == 1
    assert stats["close_count"] == 1
    assert stats["tp_count"] == 1
    assert stats["rebound_count"] == 0
    assert stats["hard_stop_count"] == 0
    assert stats["stored_trade_count"] == 1
    assert stats["stored_tp_count"] == 1
    assert stats["stored_rebound_count"] == 0
    assert stats["stored_hard_stop_count"] == 0
    assert stats["aggregate_match_ok"] is True


def test_damaged_aggregates_detected() -> None:
    """Mismatched stored aggregates are detected via aggregate_match_ok."""

    run = _base_run()
    # Deliberately wrong aggregates.
    run["addon_short_trade_count"] = 10
    run["addon_short_tp_count"] = 5
    run["addon_short_rebound_exit_count"] = 3
    run["addon_short_hard_stop_count"] = 2

    trade_rows = [_simple_fill_row(1, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        )
    ]

    _, _, stats = _analyze_events_phase1(run, trade_rows, events)
    assert stats["aggregate_match_ok"] is False


def test_phase1_deterministic_outputs() -> None:
    """Phase 1 analysis is deterministic for identical inputs."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        )
    ]

    events_copy = copy.deepcopy(events)
    enriched1, trades1, stats1 = _analyze_events_phase1(run, trade_rows, events)
    enriched2, trades2, stats2 = _analyze_events_phase1(run, trade_rows, events_copy)

    assert [e.__dict__ for e in enriched1] == [e.__dict__ for e in enriched2]
    assert trades1 == trades2
    assert stats1 == stats2


def test_phase2_short_gross_pnl_tp() -> None:
    """Short gross PnL for TP trade is reconstructed correctly."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0), _simple_fill_row(2, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
            entry_price=100.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=3,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_TP",
            exit_price=90.0,
            addon_short_net_pnl=20.0,
        ),
    ]

    enriched, trades, stats = _analyze_events_phase1(run, trade_rows, events)
    phase2 = _apply_phase2_pnl_and_reduce_analysis(run, trade_rows, enriched, trades, stats)
    trade = trades[0]
    assert trade["gross_pnl"] == 20.0
    assert trade["expected_net_pnl"] == 20.0
    assert trade["pnl_calculation_ok"] is True
    assert phase2["addon_aggregate_checks"]["reconstructed_addon_net_realized_pnl"] == 20.0


def test_phase2_short_gross_pnl_rebound_loss() -> None:
    """Short gross PnL for Rebound loss trade is reconstructed correctly."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=1.0,
            entry_price=100.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=3,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_REBOUND_EXIT",
            exit_price=105.0,
            addon_short_net_pnl=-5.0,
        ),
    ]

    enriched, trades, stats = _analyze_events_phase1(run, trade_rows, events)
    phase2 = _apply_phase2_pnl_and_reduce_analysis(run, trade_rows, enriched, trades, stats)
    trade = trades[0]
    assert trade["gross_pnl"] == -5.0
    assert trade["expected_net_pnl"] == -5.0
    assert trade["pnl_calculation_ok"] is True
    assert phase2["addon_aggregate_checks"]["reconstructed_addon_net_realized_pnl"] == -5.0


def test_phase2_entry_and_exit_fees_zero_for_addon() -> None:
    """Addon fee rates are zero; expected fees per trade are 0."""

    run = _base_run()
    trade_rows = [_simple_fill_row(1, 10.0, 4.0)]
    events = [
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=0,
            candle_index=1,
            timestamp="t0",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="RECOVERY_ACTIVATED",
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=1,
            candle_index=2,
            timestamp="t1",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_ENTRY",
            executed_entry_qty=2.0,
            entry_price=100.0,
        ),
        AuditEvent(
            trade_block_id=run["trade_block_id"],
            event_sequence=2,
            candle_index=3,
            timestamp="t2",
            candle_open=None,
            candle_high=None,
            candle_low=None,
            candle_close=None,
            event_type="ADDON_RECOVERY_SHORT_TP",
            exit_price=90.0,
            addon_short_net_pnl=20.0,
        ),
    ]

    enriched, trades, stats = _analyze_events_phase1(run, trade_rows, events)
    phase2 = _apply_phase2_pnl_and_reduce_analysis(run, trade_rows, enriched, trades, stats)
    trade = trades[0]
    assert trade["entry_fee"] == 0.0
    assert trade["exit_fee"] == 0.0
    assert trade["total_fees"] == 0.0
    fee_model = phase2["fee_model"]
    assert fee_model["entry_fee_rate"] == 0.0
    assert fee_model["exit_fee_rate"] == 0.0

