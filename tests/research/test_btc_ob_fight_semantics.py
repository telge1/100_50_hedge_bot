"""Tests for formatting and report semantics."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from research.btc_ob_fight.contracts import FORBIDDEN_REASON_CODES
from research.btc_ob_fight.formatting import (
    fmt_bps,
    fmt_duration_seconds,
    fmt_fraction_as_pct,
    fmt_oi_delta,
    fmt_pct,
    json_safe,
)
from research.btc_ob_fight.factual_reasons import derive_factual_reason_codes
from research.btc_ob_fight.reporting import build_report_md, print_console_summary
from research.btc_ob_fight.templates_de import render_german_fact, render_report_sections


def test_negative_zero_bps():
    assert fmt_bps(-0.0001) == "0.00 bps"


def test_duration_spacing():
    assert fmt_duration_seconds(158.647) == "158.647 Sekunden"


def test_oi_formatting():
    assert fmt_oi_delta(114.32999999999447) == "+114.33"
    assert fmt_pct(0.21756654304973463) == "+0.218 %"


def test_fraction_as_pct_display():
    assert fmt_fraction_as_pct(0.7013816504639208) == "70.1 %"
    assert fmt_fraction_as_pct(0.0) == "0.0 %"
    assert fmt_fraction_as_pct(1.0) == "100.0 %"
    assert fmt_fraction_as_pct(None) == "n/a"


def test_value_area_share_console_and_report(capsys):
    summary = {
        "analysis_status": "FACTS_READY_RULES_UNFROZEN",
        "schema_version": "btc_ob_fight_facts_v2_0",
        "anchor_timestamp_utc": "2026-08-31T19:00:00Z",
        "window": {"start_utc": "2026-08-31T18:30:00Z", "end_utc": "2026-08-31T19:30:00Z"},
        "symbol": "BTCUSDT",
        "data_quality": "PASS",
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "profile_facts": {
            "volume_profile_status": "COMPUTED_SEPARATELY",
            "price_at_anchor": 78984.4,
            "tpo_poc": 78565.0,
            "tpo_vah": 79140.0,
            "tpo_val": 78190.0,
            "volume_poc": 78565.0,
            "volume_vah": 79140.0,
            "volume_val": 78190.0,
            "inside_volume_value_area": True,
            "inside_tpo_value_area": True,
            "nearest_profile_levels": [{"kind": "lvn", "price": 78985.0}],
            "nearest_volume_levels": [{"kind": "lvn", "price": 78985.0}],
            "tpo_volume_level_confluence": [],
        },
        "volume_profile": {
            "status": "COMPUTED_SEPARATELY",
            "primary_volume_basis": "base_volume",
            "vpoc": 78565.0,
            "vvah": 79140.0,
            "vval": 78190.0,
            "value_area_share": 0.7013816504639208,
            "integrity": "PASS",
            "prefix_parity": "PASS",
            "oa_parity": "EXACT",
        },
        "level_events": [],
        "trade_facts": {"relative_windows": []},
        "wall_summary": {},
        "oi_liquidation_facts": {
            "oi_delta": 114.33,
            "oi_delta_pct": 0.21756654304973463,
            "oi_unit": {"display_label": "Source-Einheiten"},
            "liquidation_count": 0,
            "liquidation_summary": {"long_count": 0, "short_count": 0},
        },
    }
    manifest = {"ob_root": "/tmp", "auto_extension_enabled": False, "rules_frozen": False}
    print_console_summary(summary, run_dir=__import__("pathlib").Path("/tmp/run"), manifest=manifest)
    console = capsys.readouterr().out
    report = build_report_md(summary, [], manifest)

    assert "Value-Area-Anteil: 70.1 %" in console
    assert "Value-Area-Anteil: 70.1 %" in report
    assert "+0.701 %" not in console
    assert "+0.701 %" not in report
    assert "+0.218 %" in console
    assert fmt_pct(summary["oi_liquidation_facts"]["oi_delta_pct"]) == "+0.218 %"


def test_episode_template_no_shared_duration():
    text = render_german_fact(
        "PROFILE_LEVEL_ABOVE_EPISODE_COMPLETE",
        {
            "episode_index": 2,
            "label": "TPO-VAH",
            "level_price": 79140.0,
            "start_ts": "2026-08-31T19:12:13.63Z",
            "end_ts": "2026-08-31T19:15:00Z",
            "duration_seconds": 166.37,
            "episode_id": "tpo_vah_above_002",
        },
    )
    assert "164.942" not in text
    assert "166.370 Sekunden" in text


def test_trade_window_dedup_in_reasons():
    trade_facts = {
        "after_window": {
            "label": "after_anchor",
            "start_utc": "2026-08-31T19:00:00Z",
            "end_utc": "2026-08-31T19:30:00Z",
            "delta_notional": 1.0,
            "price_change_bps": 1.0,
        },
        "relative_windows": [
            {
                "label": "anchor_0_30m",
                "start_utc": "2026-08-31T19:00:00Z",
                "end_utc": "2026-08-31T19:30:00Z",
                "delta_notional": 1.0,
                "price_change_bps": 1.0,
            }
        ],
    }
    codes = derive_factual_reason_codes({}, [], trade_facts, [], {})
    delta_codes = [c for c in codes if "TAKER_DELTA" in c["code"]]
    assert len(delta_codes) == 1


def test_semantic_report_episode_consistency():
    level_events = [
        {
            "level_id": "tpo_vah",
            "label": "TPO-VAH",
            "price": 79140.0,
            "first_touch_ts": None,
            "episodes": [
                {
                    "episode_id": "tpo_vah_above_001",
                    "episode_index": 1,
                    "level_id": "tpo_vah",
                    "level_price": 79140.0,
                    "direction": "ABOVE",
                    "start_ts": "2026-08-31T19:08:13.573Z",
                    "end_ts": "2026-08-31T19:10:58.515Z",
                    "duration_seconds": 164.942,
                    "complete": True,
                }
            ],
        }
    ]
    reasons = derive_factual_reason_codes(
        {"inside_tpo_value_area": True, "price_at_anchor": 78984.4, "tpo_vah": 79140, "tpo_val": 78190},
        level_events,
        {},
        [],
        {},
    )
    sections = render_report_sections(
        reasons,
        {"profile_facts": {}, "wall_facts": []},
        {"heuristics": {}},
        level_events=level_events,
    )
    episode_text = " ".join(sections["episodes"])
    assert "164.942 Sekunden" in episode_text
    assert "erstmals" not in episode_text.lower() or "überschritt" in episode_text
    for c in reasons:
        assert c["code"] not in FORBIDDEN_REASON_CODES


def test_json_safe_no_nan():
    payload = json_safe({"x": float("nan")})
    assert json.dumps(payload)
