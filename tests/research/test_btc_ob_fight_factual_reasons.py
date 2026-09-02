"""Tests for factual reason codes and German templates."""

from __future__ import annotations

from research.btc_ob_fight.contracts import FORBIDDEN_REASON_CODES
from research.btc_ob_fight.factual_reasons import derive_factual_reason_codes
from research.btc_ob_fight.templates_de import render_german_fact, render_all_german


def test_factual_reason_codes_deterministic():
    profile = {"inside_tpo_value_area": True, "price_at_anchor": 79000, "tpo_vah": 79140, "tpo_val": 78900}
    trade_facts = {
        "relative_windows": [
            {
                "label": "anchor_0_10m",
                "start_utc": "2026-08-31T19:00:00Z",
                "end_utc": "2026-08-31T19:10:00Z",
                "delta_notional": 2_760_000,
                "price_change_bps": 25.88,
            }
        ]
    }
    level_events = [
        {
            "level_id": "tpo_vah",
            "label": "TPO-VAH",
            "price": 79140,
            "first_touch_ts": None,
            "episodes": [
                {
                    "episode_id": "tpo_vah_above_001",
                    "episode_index": 1,
                    "level_id": "tpo_vah",
                    "level_price": 79140,
                    "direction": "ABOVE",
                    "start_ts": "2026-08-31T19:08:13.577Z",
                    "end_ts": "2026-08-31T19:10:58.515Z",
                    "duration_seconds": 164.938,
                    "complete": True,
                    "max_excursion_bps": 10.0,
                }
            ],
        }
    ]
    codes = derive_factual_reason_codes(profile, level_events, trade_facts, [], {})
    got = [c["code"] for c in codes]
    assert "ANCHOR_INSIDE_TPO_VALUE_AREA" in got
    assert "POSITIVE_TAKER_DELTA_OBSERVED" in got
    assert "PROFILE_LEVEL_ABOVE_EPISODE_COMPLETE" in got
    assert not any(c in FORBIDDEN_REASON_CODES for c in got)


def test_german_templates_contain_values():
    text = render_german_fact(
        "POSITIVE_TAKER_DELTA_OBSERVED",
        {"delta_notional": 2_760_000, "start_utc": "2026-08-31T19:00:00Z", "end_utc": "2026-08-31T19:10:00Z"},
    )
    assert "2.76 Mio. USD" in text


def test_forbidden_codes_never_emitted():
    codes = derive_factual_reason_codes({}, [], {"before_window": {"delta_notional": 0, "price_change_bps": 0}}, [], {})
    assert not any(c["code"] in FORBIDDEN_REASON_CODES for c in codes)
