"""Tests for German Live Orderbook card copy (display-only)."""

from __future__ import annotations

from live_orderbook_copy import (
    absorption_headline,
    classify_money_flow_regime,
    enrich_ob_grid_for_display,
    enrich_view_display,
    level_quality_headline,
    liquidations_headline,
    money_flow_headline,
    near_price_headline,
    overall_headline,
    resistance_headline,
    support_headline,
    wall_bias_headline,
)


def test_resistance_comes_closer_down():
    out = resistance_headline(
        {
            "previous_center": 0.6140,
            "current_center": 0.6130,
            "direction": "down",
            "move_pct": -0.16,
            "strength": 59,
            "notional": 277100,
            "notional_change_pct": -25.6,
            "distance_pct": 1.08,
        }
    )
    assert out["headline"] == "WIDERSTAND KOMMT NÄHER"
    assert out["arrow"] == "↓"
    assert out["arrow_tone"] == "negative"
    assert out["distance_badge"] == "1.08% ENTFERNT"
    assert "+" not in out["distance_badge"]
    assert out["move_line"] == "0.6140 → 0.6130 → -0.16%"
    assert any(m["label"] == "Ask-Volumen" for m in out["metrics"])
    assert len(out["metrics"]) == 3
    assert all("N/A" not in str(m["value"]) for m in out["metrics"])


def test_resistance_moves_away_up():
    out = resistance_headline(
        {
            "previous_center": 0.6100,
            "current_center": 0.6200,
            "direction": "up",
            "move_pct": 1.6,
            "strength": 40,
            "notional": 1000,
            "distance_pct": 2.5,
        }
    )
    assert out["headline"] == "WIDERSTAND ENTFERNT SICH"
    assert out["arrow_tone"] == "positive"
    assert out["distance_badge"] == "2.50% ENTFERNT"


def test_resistance_missing_compare_and_undetected():
    missing = resistance_headline(
        {
            "previous_center": None,
            "current_center": 0.6160,
            "direction": "unchanged",
            "strength": 56,
            "notional": 234100,
            "notional_change_pct": None,
            "distance_pct": 1.11,
        }
    )
    assert missing["distance_badge"] == "1.11% ENTFERNT"
    assert missing["prev_price"] == "NOCH KEIN VERGLEICH"
    assert missing["move_pct_display"] == "NOCH KEIN VERGLEICH"
    assert missing["metrics"][2]["value"] == "SAMMELT DATEN"
    assert "N/A" not in missing["move_line"]

    gone = resistance_headline(None)
    assert gone["detected"] is False
    assert gone["headline"] == "WIDERSTAND NICHT MEHR SICHTBAR"
    assert gone["distance_badge"] == "ABSTAND —"
    assert gone["curr_price"] == "NICHT ERKANNT"
    assert all("N/A" not in str(m["value"]) for m in gone["metrics"])


def test_support_comes_closer_up():
    out = support_headline(
        {
            "previous_center": 0.6000,
            "current_center": 0.6010,
            "direction": "up",
            "move_pct": 0.17,
            "strength": 71,
            "notional": 419500,
        }
    )
    assert out["headline"] == "UNTERSTÜTZUNG KOMMT NÄHER"
    assert out["arrow_tone"] == "positive"


def test_support_falls_back_down():
    out = support_headline(
        {
            "previous_center": 0.6010,
            "current_center": 0.5980,
            "direction": "down",
            "move_pct": -0.5,
            "strength": 50,
            "notional": 1000,
        }
    )
    assert out["headline"] == "UNTERSTÜTZUNG FÄLLT ZURÜCK"
    assert out["arrow_tone"] == "negative"


def test_support2_detected_flag():
    undetected = support_headline(None)
    assert undetected["detected"] is False
    detected = support_headline(
        {"previous_center": 0.5, "current_center": 0.5, "direction": "unchanged", "strength": 10, "notional": 100}
    )
    assert detected["detected"] is True


def test_absorption_sell_and_buy():
    sell = absorption_headline({"kind": "SELL_ABSORPTION", "strength": 70, "reaction": "hold", "zone": "support"})
    buy = absorption_headline({"kind": "BUY_ABSORPTION", "strength": 60, "reaction": "hold", "zone": "resistance"})
    none = absorption_headline(None)
    assert sell["headline"] == "KÄUFER HALTEN DEN SUPPORT"
    assert buy["headline"] == "VERKÄUFER HALTEN DIE RESISTANCE"
    assert none["headline"] == "SAMMLE ERSTE DATEN"


def test_near_price_bid_ask_overhang():
    bid = near_price_headline({"bid_share_0_10": 62, "ask_share_0_10": 38, "bid_share_10_25": 55, "ask_share_10_25": 45})
    ask = near_price_headline({"bid_share_0_10": 40, "ask_share_0_10": 60})
    assert bid["headline"] == "MEHR KAUF-LIQUIDITÄT NAHE AM PREIS"
    assert ask["headline"] == "MEHR VERKAUFS-LIQUIDITÄT NAHE AM PREIS"
    none_near = near_price_headline({"bias": "NONE_NEAR"})
    assert none_near["headline"] == "KEINE WALLS NAHE AM PREIS"


def test_level_quality_fatigue_levels():
    assert level_quality_headline({"fatigue": "LOW", "tests": 1})["headline"] == "LEVEL IST NOCH FRISCH"
    assert level_quality_headline({"fatigue": "MEDIUM", "tests": 4, "age_display": "18m"})["headline"] == "LEVEL WURDE MEHRFACH GETESTET"
    assert level_quality_headline({"fatigue": "HIGH", "tests": 8})["headline"] == "BREAK-RISIKO STEIGT"
    assert level_quality_headline(None)["headline"] == "NOCH NICHT GENUG DATEN"


def test_liquidations_long_short():
    long_h = liquidations_headline({"buy_notional": 82400, "sell_notional": 3100})
    short_h = liquidations_headline({"buy_notional": 2000, "sell_notional": 90000})
    assert long_h["headline"] == "VIELE LONGS WERDEN HERAUSGEDRÜCKT"
    assert short_h["headline"] == "VIELE SHORTS WERDEN HERAUSGEDRÜCKT"


def test_money_flow_regimes():
    assert classify_money_flow_regime({"oi_change_pct": 0.08, "price_change_pct": -0.35, "delta_notional": -4200}) == "NEW_SHORTS"
    assert classify_money_flow_regime({"oi_change_pct": -0.1, "price_change_pct": 0.2, "delta_notional": 1000}) == "SHORT_COVERING"
    mf = money_flow_headline({"oi_change_pct": 0.08, "price_change_pct": -0.35, "delta_notional": -4200, "delta_ratio": 0.68})
    assert mf["headline"] == "NEUE SHORTS KOMMEN IN DEN MARKT"
    sc = money_flow_headline({"oi_change_pct": -0.1, "price_change_pct": 0.2, "delta_notional": 1000})
    assert sc["headline"] == "SHORTS WERDEN GESCHLOSSEN"
    # delta alone must not claim NEW_SHORTS
    assert classify_money_flow_regime({"delta_notional": -5000}) == "MIXED_FLOW"


def test_wall_bias_ask_build_bid_weak():
    wall = wall_bias_headline(
        [
            {"label": "ASK / RESISTANCE", "side": "ask", "reading": "building"},
            {"label": "BID / SUPPORT", "side": "bid", "reading": "weakening"},
        ]
    )
    assert wall["headline"] == "VERKAUFSDRUCK NIMMT ZU"


def test_contradictory_overall_forces_wait():
    display = {
        "resistance": {"headline": "WIDERSTAND KOMMT NÄHER"},
        "support": {"headline": "UNTERSTÜTZUNG KOMMT NÄHER"},
        "money_flow": {"headline": "NEUE SHORTS KOMMEN IN DEN MARKT", "regime": "NEW_SHORTS"},
        "liquidations": {"headline": "VIELE LONGS WERDEN HERAUSGEDRÜCKT"},
        "wall_bias": {"headline": "KAUFDRUCK NIMMT ZU"},
        "absorption": {"headline": "KÄUFER HALTEN DEN SUPPORT"},
        "level_quality": {"headline": "NOCH NICHT GENUG DATEN"},
    }
    overall = overall_headline({"setup": "NO_TRADE"}, display)
    assert overall["headline"] == "GEMISCHTES BILD — ABWARTEN"
    assert overall["decision"] == "ABWARTEN"
    assert len(overall["reasons_for"]) <= 3
    assert len(overall["reasons_against"]) <= 2


def test_enrich_attaches_display_and_metrics():
    view = enrich_view_display(
        {
            "report_window_seconds": 60,
            "resistance": {
                "previous_center": 0.614,
                "current_center": 0.613,
                "direction": "down",
                "move_pct": -0.16,
                "strength": 59,
                "notional": 277100,
            },
            "support": None,
            "support2": None,
            "absorption": None,
            "near_price": None,
            "level_quality": None,
            "liquidations": {"buy_notional": 82400, "sell_notional": 3100},
            "money_flow": {"oi_change_pct": 0.08, "price_change_pct": -0.35, "delta_notional": -4200},
            "wall_follow": [
                {"label": "ASK / RESISTANCE", "side": "ask", "reading": "building"},
                {"label": "BID / SUPPORT", "side": "bid", "reading": "weakening"},
            ],
        }
    )
    d = view["display"]
    assert d["resistance"]["headline"] == "WIDERSTAND KOMMT NÄHER"
    assert d["liquidations"]["headline"] == "VIELE LONGS WERDEN HERAUSGEDRÜCKT"
    assert d["money_flow"]["headline"] == "NEUE SHORTS KOMMEN IN DEN MARKT"
    assert d["wall_bias"]["headline"] == "VERKAUFSDRUCK NIMMT ZU"
    assert d["overall"]["headline"]
    assert d["overall"]["reasons_for"] is not None
    # each card has headline + metrics fields
    for key in ("resistance", "support", "absorption", "near_price", "level_quality", "liquidations", "money_flow", "wall_bias"):
        assert "headline" in d[key]
        assert "metrics" in d[key]

def test_enrich_ob_grid_adds_strongest_and_near_mid():
    grid = {
        "mid_price": 0.0693,
        "search_band_bps": {"min": 100.0, "max": 300.0},
        "bid_levels": [
            {"rank_by_distance": 1, "price": 0.0685, "distance_bps": -115.0, "policies": ["NEAREST_RELEVANT"]},
            {"rank_by_distance": 2, "price": 0.0684, "distance_bps": -130.0, "policies": ["SECOND_RELEVANT"]},
            {"rank_by_distance": 3, "price": 0.0683, "distance_bps": -145.0, "policies": []},
            {"rank_by_distance": 4, "price": 0.0682, "distance_bps": -160.0, "policies": []},
            {"rank_by_distance": 6, "price": 0.0679, "distance_bps": -210.0, "policies": ["STRONGEST_RELEVANT"]},
        ],
        "ask_levels": [
            {"rank_by_distance": 1, "price": 0.0701, "distance_bps": 110.0, "policies": ["NEAREST_RELEVANT"]},
            {"rank_by_distance": 3, "price": 0.0704, "distance_bps": 155.0, "policies": ["STRONGEST_RELEVANT"]},
        ],
        "compact_bid_levels": [
            {"rank_by_distance": 1, "price": 0.0685, "distance_bps": -115.0, "policies": ["NEAREST_RELEVANT"]},
            {"rank_by_distance": 2, "price": 0.0684, "distance_bps": -130.0, "policies": ["SECOND_RELEVANT"]},
            {"rank_by_distance": 3, "price": 0.0683, "distance_bps": -145.0, "policies": []},
            {"rank_by_distance": 4, "price": 0.0682, "distance_bps": -160.0, "policies": []},
        ],
        "compact_ask_levels": [
            {"rank_by_distance": 1, "price": 0.0701, "distance_bps": 110.0, "policies": ["NEAREST_RELEVANT"]},
            {"rank_by_distance": 3, "price": 0.0704, "distance_bps": 155.0, "policies": ["STRONGEST_RELEVANT"]},
        ],
    }
    sample = {
        "mid_price": 0.0693,
        "strongest_bid_walls": [
            {"wall_price": 0.0690, "wall_notional": 3e6, "wall_multiple": 7.0, "distance_to_mid_bps": 48.0},
            {"wall_price": 0.0680, "wall_notional": 1e6, "wall_multiple": 2.0, "distance_to_mid_bps": 150.0},
        ],
        "strongest_ask_walls": [
            {"wall_price": 0.0696, "wall_notional": 2e6, "wall_multiple": 3.0, "distance_to_mid_bps": 40.0},
        ],
    }
    out = enrich_ob_grid_for_display(grid, sample)
    assert len(out["display_bid_levels"]) == 5
    assert out["display_bid_levels"][-1]["rank_by_distance"] == 6
    # ask strongest already in compact → no duplicate
    assert len(out["display_ask_levels"]) == 2
    assert len(out["near_mid_bid_walls"]) == 1
    assert out["near_mid_bid_walls"][0]["price"] == 0.069
    assert out["near_mid_bid_walls"][0]["distance_bps"] < 0
    assert len(out["near_mid_ask_walls"]) == 1
    assert out["near_mid_ask_walls"][0]["distance_bps"] > 0
