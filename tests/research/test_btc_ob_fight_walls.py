"""Tests for OB wall observation/track/transition pipeline."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from research.btc_ob_fight.contracts import FORBIDDEN_REASON_CODES
from research.btc_ob_fight.formatting import json_safe
from research.btc_ob_fight.templates_de import render_report_sections
from research.btc_ob_fight.wall_events import (
    BTCUSDT_TICK_SIZE,
    build_wall_fact_pipeline,
    compute_sample_gap_stats,
    price_to_tick,
    sample_ob_snapshots,
    tick_to_price,
)


def _ts(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"


def _sample_row(idx: int, base: datetime, mid: float, ask_wall: dict | None, bid_wall: dict | None) -> dict:
    asks = [ask_wall] if ask_wall else []
    bids = [bid_wall] if bid_wall else []
    return {
        "sample_index": idx,
        "ts": _ts(base, idx),
        "ok": True,
        "mid": mid,
        "best_bid": mid - 1,
        "best_ask": mid + 1,
        "spread_bps": 2.0,
        "top_ask_walls": asks,
        "top_bid_walls": bids,
    }


def _wall(side: str, price: float, qty: float, ratio: float = 4.0) -> dict:
    return {
        "side": side,
        "price": price,
        "qty": qty,
        "notional": price * qty,
        "distance_bps": 10.0,
        "ratio": ratio,
        "local_depth_median": qty / ratio,
    }


def test_samples_not_equal_tracks():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), _wall("BID", 78900.0, 8.0)),
        _sample_row(1, base, 79000.0, _wall("ASK", 79100.0, 9.0), _wall("BID", 78900.0, 8.0)),
        _sample_row(2, base, 79000.0, _wall("ASK", 79150.0, 7.0), _wall("BID", 78900.0, 8.0)),
    ]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    s = bundle["summary"]
    assert s["book_samples_total"] == 3
    assert s["wall_observations_total"] == 6
    assert s["unique_wall_tracks"] == 3
    assert s["wall_observations_total"] != s["unique_wall_tracks"]


def test_same_level_consecutive_samples_same_track():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", 79100.0, 9.0), None),
    ]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    ask_tracks = [t for t in bundle["tracks"] if t["side"] == "ASK"]
    assert len(ask_tracks) == 1
    assert ask_tracks[0]["observation_count"] == 2


def test_reappeared_level_new_track():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), None),
        _sample_row(1, base, 79000.0, None, None),
        _sample_row(2, base, 79000.0, _wall("ASK", 79100.0, 8.0), None),
    ]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    ask_tracks = [t for t in bundle["tracks"] if t["side"] == "ASK"]
    assert len(ask_tracks) == 2


def test_ask_matches_buy_bid_matches_sell():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    price = 79100.0
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", price, 5.0), None),
    ]
    t0 = base
    trades = [
        {"ts": t0 + timedelta(seconds=1), "trade_id": "b1", "side": "Buy", "price": price, "size": 3.0, "notional": price * 3},
        {"ts": t0 + timedelta(seconds=1), "trade_id": "s1", "side": "Sell", "price": price, "size": 99.0, "notional": price * 99},
    ]
    bundle = build_wall_fact_pipeline(rows, trades, symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    matches = bundle["trade_matches"]
    assert len(matches) == 1
    assert matches[0]["aggressor_side"] == "Buy"


def test_trades_outside_interval_not_matched():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    price = 79100.0
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", price, 5.0), None),
    ]
    trades = [
        {"ts": base - timedelta(seconds=5), "trade_id": "old", "side": "Buy", "price": price, "size": 5.0, "notional": 1.0},
        {"ts": base + timedelta(seconds=120), "trade_id": "new", "side": "Buy", "price": price, "size": 5.0, "notional": 1.0},
    ]
    bundle = build_wall_fact_pipeline(rows, trades, symbol="BTCUSDT", window_end=base + timedelta(minutes=5))
    assert bundle["trade_matches"] == []


def test_trade_not_double_assigned():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    price = 79100.0
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", price, 7.0), None),
        _sample_row(2, base, 79000.0, _wall("ASK", price, 4.0), None),
    ]
    trades = [
        {"ts": base + timedelta(seconds=1), "trade_id": "t1", "side": "Buy", "price": price, "size": 2.0, "notional": 1.0},
    ]
    bundle = build_wall_fact_pipeline(rows, trades, symbol="BTCUSDT", window_end=base + timedelta(minutes=5))
    assert len(bundle["trade_matches"]) == 1


def test_tick_exact_price_matching():
    tick = price_to_tick(79100.05)
    assert tick_to_price(tick) == 79100.0
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", 79100.0, 8.0), None),
    ]
    trades = [
        {"ts": base + timedelta(seconds=1), "trade_id": "near", "side": "Buy", "price": 79100.05, "size": 2.0, "notional": 1.0},
    ]
    bundle = build_wall_fact_pipeline(rows, trades, symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    assert len(bundle["trade_matches"]) == 1


def test_qty_increase_and_decrease_transitions():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    price = 79100.0
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", price, 7.0), None),
        _sample_row(2, base, 79000.0, _wall("ASK", price, 9.0), None),
    ]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    types = [t["transition_type"] for t in bundle["transitions"]]
    assert "UNMATCHED_QTY_DECREASE" in types
    assert "QTY_INCREASE_OBSERVED" in types


def test_trade_associated_and_unmatched_decrease():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    price = 79100.0
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", price, 5.0), None),
    ]
    trades = [
        {"ts": base + timedelta(seconds=1), "trade_id": "t1", "side": "Buy", "price": price, "size": 4.0, "notional": 1.0},
    ]
    bundle = build_wall_fact_pipeline(rows, trades, symbol="BTCUSDT", window_end=base + timedelta(minutes=1), trade_match_frac=0.3)
    assert any(t["transition_type"] == "TRADE_ASSOCIATED_QTY_DECREASE" for t in bundle["transitions"])

    rows2 = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", price, 5.0), None),
    ]
    bundle2 = build_wall_fact_pipeline(rows2, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    assert any(t["transition_type"] == "UNMATCHED_QTY_DECREASE" for t in bundle2["transitions"])


def test_disappearance_transitions():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    price = 79100.0
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, None, None),
    ]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    assert any(t["transition_type"] == "UNMATCHED_DISAPPEARANCE" for t in bundle["transitions"])


def test_refill_sequence():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    price = 79100.0
    rows = [
        _sample_row(0, base, 79000.0, _wall("ASK", price, 10.0), None),
        _sample_row(1, base, 79000.0, _wall("ASK", price, 6.0), None),
        _sample_row(2, base, 79000.0, _wall("ASK", price, 9.0), None),
    ]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    assert len(bundle["refill_sequences"]) >= 1
    assert bundle["refill_sequences"][0]["heuristic_label"] == "HEURISTIC_REFILL_SEQUENCE"


def test_track_open_at_window_end():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [_sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), None)]
    end = base + timedelta(minutes=1)
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=end)
    assert bundle["tracks"][0]["final_state"] == "STILL_VISIBLE_AT_WINDOW_END"


def test_data_gap_ends_track():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [
        {**_sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), None), "sample_index": 0},
        {**_sample_row(5, base, 79000.0, _wall("ASK", 79100.0, 8.0), None), "sample_index": 5},
    ]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=10))
    assert any(t["final_state"] == "DATA_GAP" for t in bundle["tracks"])


def test_sample_gap_stats():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [
        {"ok": True, "ts": _ts(base, 0), "sample_index": 0},
        {"ok": True, "ts": _ts(base, 1), "sample_index": 1},
        {"ok": True, "ts": _ts(base, 3), "sample_index": 2},
    ]
    stats = compute_sample_gap_stats(rows)
    assert stats["p50_seconds"] == 1.0
    assert stats["max_seconds"] == 2.0


def test_json_safe_no_nan():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [_sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), None)]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    text = json.dumps(json_safe(bundle))
    assert "NaN" not in text and "Infinity" not in text


def test_report_wall_labels():
    summary = {
        "wall_summary": {
            "book_samples_total": 10,
            "sample_gap_p50_seconds": 1,
            "sample_gap_p95_seconds": 1,
            "sample_gap_max_seconds": 1,
            "ask_wall_observations": 20,
            "bid_wall_observations": 18,
            "ask_wall_tracks": 5,
            "bid_wall_tracks": 4,
            "ask_unique_wall_price_levels": 5,
            "bid_unique_wall_price_levels": 4,
            "qty_decreases": {"ask": 1, "bid": 2},
            "trade_associated_decreases": {"ask": 1, "bid": 0},
            "unmatched_decreases": {"ask": 0, "bid": 2},
            "trade_associated_disappearances": {"ask": 0, "bid": 0},
            "unmatched_disappearances": {"ask": 1, "bid": 0},
            "refill_sequences_heuristic": {"ask": 0, "bid": 1},
            "tracks_visible_at_window_end": {"ask": 2, "bid": 1},
            "status": "UNFROZEN_HEURISTIC",
            "heuristic_contract_version": "wall_heuristics_v1",
        }
    }
    sections = render_report_sections([], summary, {"heuristics": {"btcusdt_tick_size": 0.1}})
    wall_text = "\n".join(sections["walls"])
    assert "Wall-Kandidaten" not in wall_text
    assert "Book-Samples" in wall_text
    assert "Wall-Beobachtungen" in wall_text


def test_level_micro_episodes_aggregated_in_report():
    level_events = [
        {
            "level_id": "tpo_vah",
            "label": "TPO-VAH",
            "price": 79140.0,
            "anchor_state": {"initial_side_at_anchor": "BELOW", "final_side_at_window_end": "ABOVE"},
            "episodes": [
                {
                    "direction": "ABOVE",
                    "complete": True,
                    "duration_seconds": 100.0,
                    "start_ts": "2026-08-31T19:08:00Z",
                    "end_ts": "2026-08-31T19:09:40Z",
                },
                {
                    "direction": "ABOVE",
                    "complete": True,
                    "duration_seconds": 0.5,
                    "start_ts": "2026-08-31T19:10:00Z",
                    "end_ts": "2026-08-31T19:10:00.5Z",
                },
            ],
        }
    ]
    sections = render_report_sections([], {}, {"heuristics": {"report_micro_episode_seconds": 1.0}}, level_events=level_events)
    text = "\n".join(sections["episodes"])
    assert "micro_flicker" in text
    assert "Top-1 ABOVE" in text


def test_public_trade_window_not_duplicated():
    reasons = [
        {"code": "POSITIVE_TAKER_DELTA_OBSERVED", "fields": {"start_utc": "2026-08-31T19:00:00Z", "end_utc": "2026-08-31T19:10:00Z", "delta_notional": 1e6}},
        {"code": "PRICE_MOVED_UP_IN_WINDOW", "fields": {"start_utc": "2026-08-31T19:00:00Z", "end_utc": "2026-08-31T19:10:00Z", "price_change_bps": 10.0}},
    ]
    lines = render_report_sections(reasons, {}, {})["trade_windows"]
    assert len(lines) == 1
    assert "Delta" in lines[0] and "Preisänderung" in lines[0]


def test_no_forbidden_codes_in_wall_pipeline():
    base = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    rows = [_sample_row(0, base, 79000.0, _wall("ASK", 79100.0, 10.0), None)]
    bundle = build_wall_fact_pipeline(rows, [], symbol="BTCUSDT", window_end=base + timedelta(minutes=1))
    blob = json.dumps(bundle)
    for code in FORBIDDEN_REASON_CODES:
        assert code not in blob


def test_btc_tick_size_constant():
    assert float(BTCUSDT_TICK_SIZE) == 0.1


def test_sampling_smoke_1s_vs_30s(monkeypatch):
    from research.btc_ob_fight.config import resolve_ob_root

    ob = resolve_ob_root()
    if ob is None:
        return
    ws = datetime(2026, 8, 31, 19, 7, tzinfo=timezone.utc)
    we = datetime(2026, 8, 31, 19, 16, tzinfo=timezone.utc)
    rows_30 = sample_ob_snapshots(ob, "BTCUSDT", ws, we, interval_seconds=30)
    rows_1 = sample_ob_snapshots(ob, "BTCUSDT", ws, we, interval_seconds=1)
    assert len(rows_1) > len(rows_30)
