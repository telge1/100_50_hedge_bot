"""Independent zone/touch/detection derivation tests for Episode 1."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1 import (  # noqa: E402
    EXPECTED_DETECTION_ISO,
    EXPECTED_ZONE_FIRST_TOUCH_ISO,
)
from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.independent_oracle import (  # noqa: E402
    accepted_above_detection_time,
    audit_directory,
    compare_independent_payload,
    oracle_detection,
    oracle_wall_first_touch,
    oracle_zone_first_touch,
    scan_zone_visits,
)


def _zone(*, available_at: str = "2026-09-06T20:00:10Z", low: float = 100.0, high: float = 101.0) -> dict:
    return {
        "event_type": "ZONE_AVAILABLE",
        "zone_id": "pc:test",
        "persistent_cluster_id": "pc:test",
        "level_cluster_id": "cl:test",
        "zone_low": low,
        "zone_high": high,
        "zone_available_at": available_at,
        "level_type": "TPO_VAL",
    }


def _trade(*, ts: datetime, price: float, trade_id: str, recv: datetime | None = None, side: str = "buy") -> dict:
    return {
        "trade_id": trade_id,
        "trade_ts": ts.isoformat().replace("+00:00", "Z"),
        "price": price,
        "size": 1.0,
        "taker_side": side,
        "collector_received_at": None if recv is None else recv.isoformat().replace("+00:00", "Z"),
    }


def _candle(*, open_ts: datetime, low: float, high: float, close: float) -> dict:
    return {
        "open_time": open_ts.isoformat().replace("+00:00", "Z"),
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def test_01_zone_before_touch_is_ignored():
    zone = _zone()
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    trades = [
        _trade(ts=t0 + timedelta(seconds=5), price=100.5, trade_id="too_early"),
        _trade(ts=t0 + timedelta(seconds=12), price=100.5, trade_id="touch"),
    ]
    touch = oracle_zone_first_touch(zone=zone, trades=trades, candles=[], window_end=t0 + timedelta(minutes=5))
    assert touch is not None
    assert touch["trigger_record_id"] == "touch"
    assert touch["exchange_event_time"] == "2026-09-06T20:00:12Z"


def test_02_visit_walk_requires_outside_candle_before_reopen():
    zone = _zone()
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    trades = [
        _trade(ts=t0 + timedelta(seconds=12), price=100.5, trade_id="touch1"),
        _trade(ts=t0 + timedelta(seconds=20), price=102.0, trade_id="exit1"),
        _trade(ts=t0 + timedelta(seconds=25), price=100.4, trade_id="blocked_reopen"),
        _trade(ts=t0 + timedelta(minutes=2, seconds=5), price=100.4, trade_id="touch2"),
    ]
    candles = [
        _candle(open_ts=t0 + timedelta(minutes=1), low=101.5, high=102.5, close=102.0),
    ]
    visits = scan_zone_visits(zone=zone, trades=trades, candles=candles, window_end=t0 + timedelta(minutes=3))
    assert [visit["first_touch_ts"] for visit in visits] == ["2026-09-06T20:00:12Z", "2026-09-06T20:02:05Z"]


def test_03_zone_touch_can_differ_from_wall_touch():
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    trades = [
        _trade(ts=t0 + timedelta(seconds=10), price=100.5, trade_id="zone_touch"),
        _trade(ts=t0 + timedelta(seconds=20), price=101.2, trade_id="wall_touch"),
    ]
    wall = oracle_wall_first_touch(
        wall_visible_at="2026-09-06T20:00:10Z",
        trades=trades,
        wall_price=101.0,
        wall_side="ask",
    )
    assert wall is not None
    assert wall["exchange_event_time"] == "2026-09-06T20:00:20Z"


def test_04_wall_observation_can_precede_wall_touch():
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    trades = [_trade(ts=t0 + timedelta(seconds=15), price=100.9, trade_id="not_yet")]
    wall = oracle_wall_first_touch(
        wall_visible_at="2026-09-06T20:00:10Z",
        trades=trades + [_trade(ts=t0 + timedelta(seconds=30), price=101.0, trade_id="hit")],
        wall_price=101.0,
        wall_side="ask",
    )
    assert wall is not None
    assert wall["exchange_event_time"] != "2026-09-06T20:00:10Z"


def test_05_ask_wall_touch_uses_ge_rule():
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    wall = oracle_wall_first_touch(
        wall_visible_at="2026-09-06T20:00:10Z",
        trades=[
            _trade(ts=t0 + timedelta(seconds=11), price=100.99, trade_id="miss"),
            _trade(ts=t0 + timedelta(seconds=12), price=101.0, trade_id="hit"),
        ],
        wall_price=101.0,
        wall_side="ask",
    )
    assert wall is not None
    assert wall["trigger_record_id"] == "hit"


def test_06_detection_uses_earliest_two_closed_closes_above():
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    detected_at = accepted_above_detection_time(
        candles=[
            _candle(open_ts=t0, low=99.0, high=102.0, close=101.2),
            _candle(open_ts=t0 + timedelta(minutes=1), low=99.5, high=102.5, close=101.3),
            _candle(open_ts=t0 + timedelta(minutes=2), low=99.2, high=102.2, close=101.4),
        ],
        zone_high=101.0,
        zone_low=100.0,
        touch=t0 - timedelta(seconds=1),
        window_end=t0 + timedelta(minutes=10),
    )
    assert detected_at is not None
    assert detected_at.isoformat().replace("+00:00", "Z") == "2026-09-06T20:02:00Z"


def test_07_detection_rejects_when_both_lows_stay_inside_zone():
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    detected_at = accepted_above_detection_time(
        candles=[
            _candle(open_ts=t0, low=100.2, high=102.0, close=101.2),
            _candle(open_ts=t0 + timedelta(minutes=1), low=100.3, high=102.2, close=101.4),
            _candle(open_ts=t0 + timedelta(minutes=2), low=99.0, high=102.3, close=101.5),
            _candle(open_ts=t0 + timedelta(minutes=3), low=99.1, high=102.4, close=101.6),
        ],
        zone_high=101.0,
        zone_low=100.0,
        touch=t0 - timedelta(seconds=1),
        window_end=t0 + timedelta(minutes=10),
    )
    assert detected_at is not None
    assert detected_at.isoformat().replace("+00:00", "Z") == "2026-09-06T20:03:00Z"


def test_08_missing_receive_implies_event_time_availability():
    zone = _zone()
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    touch = oracle_zone_first_touch(
        zone=zone,
        trades=[_trade(ts=t0 + timedelta(seconds=12), price=100.5, trade_id="touch")],
        candles=[],
        window_end=t0 + timedelta(minutes=5),
    )
    assert touch is not None
    assert touch["receive_time_present"] is False
    assert touch["event_available_at"] == touch["exchange_event_time"]


def test_09_oracle_detection_matches_expected_episode_constants():
    zone = _zone(available_at="2026-09-06T20:18:00Z", low=79774.75, high=79775.25)
    touch = {
        "exchange_event_time": EXPECTED_ZONE_FIRST_TOUCH_ISO,
    }
    candles = [
        _candle(open_ts=datetime(2026, 9, 6, 20, 19, 0, tzinfo=timezone.utc), low=79774.0, high=79779.0, close=79776.0),
        _candle(open_ts=datetime(2026, 9, 6, 20, 20, 0, tzinfo=timezone.utc), low=79773.0, high=79780.0, close=79776.5),
    ]
    detection = oracle_detection(
        zone=zone,
        zone_touch=touch,
        candles=candles,
        window_end=datetime(2026, 9, 6, 20, 30, 0, tzinfo=timezone.utc),
    )
    assert detection is not None
    assert detection["exchange_event_time"] == EXPECTED_DETECTION_ISO


def test_10_compare_payload_matches_without_config_times(tmp_path: Path, monkeypatch):
    zone = _zone()
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    trades = [
        _trade(ts=t0 + timedelta(seconds=12), price=100.5, trade_id="touch"),
        _trade(ts=t0 + timedelta(seconds=130), price=101.0, trade_id="wall"),
    ]
    candles = [
        _candle(open_ts=t0 + timedelta(seconds=60), low=99.0, high=102.0, close=101.2),
        _candle(open_ts=t0 + timedelta(seconds=120), low=99.1, high=102.1, close=101.3),
    ]
    trades_path = tmp_path / "trades.jsonl"
    candles_path = tmp_path / "candles.jsonl"
    _write_jsonl(trades_path, trades)
    _write_jsonl(candles_path, candles)
    monkeypatch.setattr(
        "obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.independent_oracle.load_zone_from_mp_events",
        lambda: zone,
    )
    prod = {
        "episode_id": "ep:pc:test:1788724812",
        "input_paths": {"trades": str(trades_path), "candles": str(candles_path)},
        "timing": {"detection": "2026-09-06T20:03:00Z"},
        "zone_first_touch": {
            "event_type": "ZONE_FIRST_TOUCH",
            "episode_id": "ep:pc:test:1788724812",
            "exchange_event_time": "2026-09-06T20:00:12Z",
            "event_available_at": "2026-09-06T20:00:12Z",
            "trigger_record_id": "touch",
        },
        "detection": {
            "event_type": "DETECTION",
            "exchange_event_time": "2026-09-06T20:03:00Z",
            "event_available_at": "2026-09-06T20:03:00Z",
            "reaction": {"reaction_class": "ACCEPTED_ABOVE"},
        },
        "wall_observation_at_zone_touch": {
            "wall_visible_at": "2026-09-06T20:00:12Z",
            "wall_price": 101.0,
            "wall_side": "ask",
        },
        "wall_first_touch": {
            "event_type": "WALL_FIRST_TOUCH",
            "exchange_event_time": "2026-09-06T20:02:10Z",
            "event_available_at": "2026-09-06T20:02:10Z",
            "trigger_record_id": "wall",
        },
    }
    result = compare_independent_payload(prod)
    assert result["derived_false_positives"] == 0
    assert result["derived_false_negatives"] == 0
    assert result["look_ahead_violations"] == 0


def test_11_compare_payload_reports_mismatch(tmp_path: Path, monkeypatch):
    zone = _zone()
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    trades_path = tmp_path / "trades.jsonl"
    candles_path = tmp_path / "candles.jsonl"
    _write_jsonl(trades_path, [_trade(ts=t0 + timedelta(seconds=12), price=100.5, trade_id="touch")])
    _write_jsonl(
        candles_path,
        [
            _candle(open_ts=t0 + timedelta(seconds=60), low=99.0, high=102.0, close=101.2),
            _candle(open_ts=t0 + timedelta(seconds=120), low=99.0, high=102.0, close=101.3),
        ],
    )
    monkeypatch.setattr(
        "obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.independent_oracle.load_zone_from_mp_events",
        lambda: zone,
    )
    prod = {
        "episode_id": "ep:pc:test:1788724812",
        "input_paths": {"trades": str(trades_path), "candles": str(candles_path)},
        "timing": {"detection": "2026-09-06T20:03:00Z"},
        "zone_first_touch": {
            "event_type": "ZONE_FIRST_TOUCH",
            "episode_id": "ep:pc:test:1788724812",
            "exchange_event_time": "2026-09-06T20:00:12Z",
            "event_available_at": "2026-09-06T20:00:12Z",
            "trigger_record_id": "wrong",
        },
        "detection": {
            "event_type": "DETECTION",
            "exchange_event_time": "2026-09-06T20:03:00Z",
            "event_available_at": "2026-09-06T20:03:00Z",
            "reaction": {"reaction_class": "ACCEPTED_ABOVE"},
        },
    }
    result = compare_independent_payload(prod)
    assert result["derived_false_positives"] >= 1
    assert result["derived_false_negatives"] >= 1


def test_12_audit_directory_reads_independent_derivation_json(tmp_path: Path, monkeypatch):
    zone = _zone()
    t0 = datetime(2026, 9, 6, 20, 0, 0, tzinfo=timezone.utc)
    trades_path = tmp_path / "trades.jsonl"
    candles_path = tmp_path / "candles.jsonl"
    _write_jsonl(trades_path, [_trade(ts=t0 + timedelta(seconds=12), price=100.5, trade_id="touch")])
    _write_jsonl(
        candles_path,
        [
            _candle(open_ts=t0 + timedelta(seconds=60), low=99.0, high=102.0, close=101.2),
            _candle(open_ts=t0 + timedelta(seconds=120), low=99.0, high=102.0, close=101.3),
        ],
    )
    monkeypatch.setattr(
        "obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.independent_oracle.load_zone_from_mp_events",
        lambda: zone,
    )
    payload = {
        "episode_id": "ep:pc:test:1788724812",
        "input_paths": {"trades": str(trades_path), "candles": str(candles_path)},
        "timing": {"detection": "2026-09-06T20:03:00Z"},
        "zone_first_touch": {
            "event_type": "ZONE_FIRST_TOUCH",
            "episode_id": "ep:pc:test:1788724812",
            "exchange_event_time": "2026-09-06T20:00:12Z",
            "event_available_at": "2026-09-06T20:00:12Z",
            "trigger_record_id": "touch",
        },
        "detection": {
            "event_type": "DETECTION",
            "exchange_event_time": "2026-09-06T20:03:00Z",
            "event_available_at": "2026-09-06T20:03:00Z",
            "reaction": {"reaction_class": "ACCEPTED_ABOVE"},
        },
    }
    (tmp_path / "independent_derivation.json").write_text(json.dumps(payload), encoding="utf-8")
    result = audit_directory(tmp_path)
    assert result["derived_false_positives"] == 0
    assert result["derived_false_negatives"] == 0


def test_13_independent_oracle_does_not_import_production_visit_or_reaction_helpers():
    src = Path(audit_directory.__code__.co_filename).read_text(encoding="utf-8")
    assert "detect_visits_for_cluster" not in src
    assert "classify_reaction" not in src
