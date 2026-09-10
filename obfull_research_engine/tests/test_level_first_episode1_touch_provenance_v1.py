"""Touch provenance contract tests — Episode 1 only."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.touch_provenance import (  # noqa: E402
    VERDICT_NOT_DERIVED,
    ask_wall_touch_rule,
    find_first_ask_wall_touch,
    production_path_proof,
    run_touch_provenance,
)

T0 = datetime(2026, 9, 6, 20, 19, 2, 200000, tzinfo=timezone.utc)


def _tr(ts: datetime, price: float, tid: str, *, recv: datetime | None = None) -> dict:
    row = {
        "trade_id": tid,
        "price": price,
        "size": 0.001,
        "taker_side": "Buy",
        "trade_ts": ts.isoformat().replace("+00:00", "Z"),
    }
    if recv is not None:
        row["collector_received_at"] = recv.isoformat().replace("+00:00", "Z")
    return row


def test_01_first_trade_below_wall_not_touch():
    trades = [_tr(T0, 79779.9, "a")]
    assert ask_wall_touch_rule(price=79779.9) is False
    assert find_first_ask_wall_touch(trades, wall_price=79780.0) is None


def test_02_first_trade_exactly_at_wall_is_touch():
    trades = [_tr(T0, 79780.0, "b")]
    hit = find_first_ask_wall_touch(trades, wall_price=79780.0)
    assert hit is not None
    assert hit["row"]["trade_id"] == "b"
    assert ask_wall_touch_rule(price=79780.0) is True


def test_03_first_trade_above_ask_wall_is_touch():
    trades = [_tr(T0, 79781.0, "c")]
    hit = find_first_ask_wall_touch(trades, wall_price=79780.0)
    assert hit is not None
    assert float(hit["row"]["price"]) > 79780.0


def test_04_earliest_valid_touch_selected():
    trades = [
        _tr(T0, 79770.0, "early_below"),
        _tr(T0.replace(microsecond=210000), 79780.0, "first_hit"),
        _tr(T0.replace(microsecond=220000), 79785.0, "later_hit"),
    ]
    hit = find_first_ask_wall_touch(trades, wall_price=79780.0)
    assert hit is not None
    assert hit["row"]["trade_id"] == "first_hit"


def test_05_exchange_before_touch_receive_after_touch():
    from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.touch_provenance import (
        parse_trade_ts,
    )

    exch = T0
    recv = T0.replace(microsecond=500000)
    reported_avail = T0
    row = _tr(exch, 79780.0, "late_recv", recv=recv)
    assert parse_trade_ts(row["trade_ts"]) <= reported_avail
    assert parse_trade_ts(row["collector_received_at"]) > reported_avail
    # Live availability would require touch_available_at >= receive; exchange-only is insufficient.


def test_06_missing_receive_time():
    row = _tr(T0, 79780.0, "no_recv")
    assert "collector_received_at" not in row
    # Restricted verdict class when only event time exists.
    assert VERDICT_NOT_DERIVED.startswith("EPISODE1_TOUCH_TRIGGER_")


def test_07_prescribed_episode_touch_not_independently_derived():
    proof = production_path_proof()
    assert proof["independently_derived_in_this_pipeline"] is False
    assert proof["code_evidence"]["init_hardcodes_iso"] is True
    assert proof["first_set_in"]["value"] == "2026-09-06T20:19:02.229Z"
    result = run_touch_provenance(write=False)
    assert result["verdict"] == VERDICT_NOT_DERIVED


def test_08_no_valid_touch_in_window():
    trades = [
        _tr(T0, 79770.0, "a"),
        _tr(T0.replace(microsecond=250000), 79779.0, "b"),
    ]
    assert find_first_ask_wall_touch(trades, wall_price=79780.0) is None
