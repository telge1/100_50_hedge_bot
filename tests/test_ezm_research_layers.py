"""Tests for two-layer EZM research output (EMA setup vs microstructure)."""

from __future__ import annotations

from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import (
    CONFIRMATION_MODE_EMA_ONLY,
    CONFIRMATION_MODE_EMA_PLUS_MICRO,
    build_ema_setup_event,
    make_setup_id,
    microstructure_layer_fields,
)


def test_make_setup_id_stable():
    sid = make_setup_id(symbol="DOGEUSDT", zone_key="EMA20", anchor_ms=1_700_000_000_000)
    assert sid == "DOGEUSDT_EMA20_1700000000000"


def test_ema_setup_event_never_emits_direction():
    row = build_ema_setup_event(
        setup_id="DOGEUSDT_EMA20_1",
        symbol="DOGEUSDT",
        zone_key="EMA20",
        zone_event="exact_touch",
        touch_at="2026-08-25T10:00:00.000Z",
        marker_at="2026-08-25T10:00:00.000Z",
        marker_price=0.12,
    )
    assert row["confirmation_mode"] == CONFIRMATION_MODE_EMA_ONLY
    assert row["output_layer"] == "ema_setup"
    assert row["candidate_direction"] == "NONE"
    assert row["emit_directional_marker"] is False
    assert row["emit_setup_marker"] is True
    assert row["setup_id"] == "DOGEUSDT_EMA20_1"


def test_microstructure_layer_links_setup():
    fields = microstructure_layer_fields(
        setup_id="DOGEUSDT_EMA20_1",
        symbol="DOGEUSDT",
        zone_key="EMA20",
        touch_at="2026-08-25T10:00:00.000Z",
        episode_id="DOGEUSDT_ep_1",
    )
    assert fields["confirmation_mode"] == CONFIRMATION_MODE_EMA_PLUS_MICRO
    assert fields["output_layer"] == "microstructure_confirmation"
    assert fields["setup_id"] == "DOGEUSDT_EMA20_1"
