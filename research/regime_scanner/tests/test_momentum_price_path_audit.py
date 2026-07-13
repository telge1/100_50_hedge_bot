"""Tests for momentum price-path / swing audit."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.momentum_forward_audit import build_signal_rows
from research.regime_scanner.momentum_price_path_audit import (
    CLASS_DROP_RECOVER_HIGHER,
    REF_SIGNAL_CLOSE,
    SwingConfig,
    analyze_adverse_threshold_recoveries,
    analyze_signal_path,
    build_legs_from_swings,
    classify_path,
    detect_swings,
    directional_adverse_pct,
    directional_favorable_pct,
    run_price_path_audit,
)


def _c(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_long_short_mirror() -> None:
    assert abs(
        directional_adverse_pct(side="long", reference=100.0, price=99.0)
        - directional_adverse_pct(side="short", reference=100.0, price=101.0)
    ) < 1e-12
    assert abs(
        directional_favorable_pct(side="long", reference=100.0, price=101.0)
        - directional_favorable_pct(side="short", reference=100.0, price=99.0)
    ) < 1e-12


def test_adverse_then_favorable() -> None:
    # Drop to 99.0 then recover to 100.5 — confirm swings with >=0.10%
    future = [
        _c("t0", 100, 100.0, 99.0, 99.2),
        _c("t1", 99.2, 100.5, 99.1, 100.4),
        _c("t2", 100.4, 100.4, 100.2, 100.3),  # pullback to confirm fav
    ]
    # pad to 96
    future += [_c(f"t{i}", 100.3, 100.3, 100.2, 100.25) for i in range(3, 96)]
    out = detect_swings(
        side="long",
        reference=100.0,
        future_candles=future,
        config=SwingConfig(swing_min_pct=0.10, max_candles=96),
    )
    assert out["evaluable"] is True
    kinds = [s.kind for s in out["swings"]]
    assert kinds[:2] == ["adverse", "favorable"]
    assert abs(out["swings"][0].price - 99.0) < 1e-12
    assert abs(out["swings"][1].price - 100.5) < 1e-9


def test_two_drops_two_recoveries_second_higher() -> None:
    future = []
    # adv1 to 99.0, fav1 to 100.3, adv2 to 99.2, fav2 to 100.8
    future.append(_c("a", 100, 100.0, 99.0, 99.1))
    future.append(_c("b", 99.1, 100.3, 99.05, 100.2))
    future.append(_c("c", 100.2, 100.25, 99.2, 99.3))  # confirms fav1, starts adv2
    future.append(_c("d", 99.3, 100.8, 99.25, 100.7))
    future.append(_c("e", 100.7, 100.7, 100.5, 100.6))  # confirm fav2
    future += [_c(f"p{i}", 100.6, 100.6, 100.5, 100.55) for i in range(91)]
    out = detect_swings(
        side="long", reference=100.0, future_candles=future, config=SwingConfig()
    )
    legs = build_legs_from_swings(out["swings"], side="long", reference=100.0)
    fav = [L for L in legs if L["kind"] == "favorable"]
    assert len(fav) >= 2
    assert fav[1]["move_from_reference_pct"] > fav[0]["move_from_reference_pct"]
    cls = classify_path(
        evaluable=True, legs=legs, mfe_96=fav[1]["move_from_reference_pct"], mae_96=1.0
    )
    assert cls == CLASS_DROP_RECOVER_HIGHER


def test_second_recovery_lower() -> None:
    future = [
        _c("a", 100, 100.0, 99.0, 99.1),
        _c("b", 99.1, 100.8, 99.05, 100.7),
        _c("c", 100.7, 100.7, 99.3, 99.4),
        _c("d", 99.4, 100.4, 99.35, 100.3),
        _c("e", 100.3, 100.3, 100.1, 100.2),
    ]
    future += [_c(f"p{i}", 100.2, 100.2, 100.1, 100.15) for i in range(91)]
    out = detect_swings(
        side="long", reference=100.0, future_candles=future, config=SwingConfig()
    )
    legs = build_legs_from_swings(out["swings"], side="long", reference=100.0)
    fav = [L for L in legs if L["kind"] == "favorable"]
    assert len(fav) >= 2
    assert fav[1]["move_from_reference_pct"] < fav[0]["move_from_reference_pct"]


def test_adverse_threshold_exact_050() -> None:
    future = [_c(f"t{i}", 100, 100.0, 99.6, 99.7) for i in range(96)]
    # first candle low exactly -0.50%
    future[0] = _c("t0", 100, 100.0, 99.5, 99.6)
    rows = analyze_adverse_threshold_recoveries(
        side="long", reference=100.0, future_candles=future, thresholds=(0.50,)
    )
    assert rows[0]["reached"] is True
    assert rows[0]["threshold_age"] == 0


def test_recovery_to_signal_and_025() -> None:
    future = [_c(f"t{i}", 99.0, 99.0, 98.5, 98.8) for i in range(96)]
    future[0] = _c("t0", 100, 100.0, 99.0, 99.1)  # -1%
    future[5] = _c("t5", 99.1, 100.0, 99.0, 99.9)  # back to signal
    future[10] = _c("t10", 99.9, 100.30, 99.8, 100.2)  # +0.30%
    rows = analyze_adverse_threshold_recoveries(
        side="long", reference=100.0, future_candles=future, thresholds=(1.00,)
    )
    assert rows[0]["reached"] is True
    assert rows[0]["returned_to_signal"] is True
    assert rows[0]["reached_025_after"] is True


def test_noise_below_swing_threshold_ignored() -> None:
    # Tiny ~0.04% wiggles should not create swings
    future = []
    for i in range(96):
        future.append(_c(f"t{i}", 100, 100.02, 99.98, 100.0))
    out = detect_swings(
        side="long",
        reference=100.0,
        future_candles=future,
        config=SwingConfig(swing_min_pct=0.10),
    )
    assert out["swings"] == []


def test_equal_highs_lows_stable() -> None:
    future = [
        _c("a", 100, 100.0, 99.0, 99.2),
        _c("b", 99.2, 99.5, 99.0, 99.1),  # equal low — stay at same extreme
        _c("c", 99.1, 100.4, 99.05, 100.3),
        _c("d", 100.3, 100.3, 100.1, 100.2),
    ]
    future += [_c(f"p{i}", 100.2, 100.2, 100.1, 100.15) for i in range(92)]
    out = detect_swings(
        side="long", reference=100.0, future_candles=future, config=SwingConfig()
    )
    adv = [s for s in out["swings"] if s.kind == "adverse"]
    assert len(adv) >= 1
    assert abs(adv[0].price - 99.0) < 1e-12
    assert adv[0].age == 0  # first extreme, not re-assigned on equal low


def test_insufficient_future() -> None:
    future = [_c("t0", 100, 101, 99, 100)]
    out = detect_swings(
        side="long",
        reference=100.0,
        future_candles=future,
        config=SwingConfig(max_candles=96),
    )
    assert out["evaluable"] is False
    assert out["reason"] == "INSUFFICIENT_FUTURE_CANDLES"


def test_confirmation_age_zero_preserved() -> None:
    base = pd.Timestamp("2026-03-01T00:00:00+00:00")
    rows = []
    for i in range(120):
        ts = base + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": 100.0,
                "high": 100.2,
                "low": 99.5,
                "close": 100.0,
                "volume": 1.0,
            }
        )
    frame = pd.DataFrame(rows)
    frame.loc[1, "low"] = 99.0
    frame.loc[2, "high"] = 100.5
    frame.loc[3, "low"] = 100.2
    pa = [
        {
            "setup_id": "a",
            "side": "long",
            "pattern_type": "higher_low",
            "structure_break_timestamp": "2026-03-01T00:00:00+00:00",
            "warnings": [],
        }
    ]
    mom = [
        {
            "setup_id": "a",
            "confirmation_timestamp": "2026-03-01T00:00:00+00:00",
            "confidence": "high",
            "candles_after_price_action_confirmation": 0,
            "confirmation_type": "break_candle",
        }
    ]
    events = [{"setup_id": "a", "event": "momentum_confirmed", "reason": None}]
    payload = run_price_path_audit(
        price_action_confirmations=pa,
        momentum_confirmations=mom,
        momentum_events=events,
        candles=frame,
    )
    primary = [
        r
        for r in payload["signal_price_paths"]
        if r["reference_mode"] == REF_SIGNAL_CLOSE
    ]
    assert primary[0]["confirmation_age"] == 0
    assert primary[0]["evaluable"] is True


def test_deterministic_swing_sequence() -> None:
    future = [
        _c("a", 100, 100.0, 99.0, 99.1),
        _c("b", 99.1, 100.4, 99.05, 100.3),
        _c("c", 100.3, 100.3, 100.1, 100.2),
    ] + [_c(f"p{i}", 100.2, 100.25, 100.1, 100.15) for i in range(93)]
    a = detect_swings(side="long", reference=100.0, future_candles=future, config=SwingConfig())
    b = detect_swings(side="long", reference=100.0, future_candles=future, config=SwingConfig())
    assert [(s.kind, s.price, s.age) for s in a["swings"]] == [
        (s.kind, s.price, s.age) for s in b["swings"]
    ]


def test_short_adverse_then_favorable() -> None:
    future = [
        _c("a", 100, 101.0, 100.0, 100.8),  # adverse rise
        _c("b", 100.8, 100.9, 99.5, 99.6),  # recovery down
        _c("c", 99.6, 99.8, 99.5, 99.7),  # small bounce to confirm
    ] + [_c(f"p{i}", 99.7, 99.8, 99.6, 99.7) for i in range(93)]
    out = detect_swings(
        side="short", reference=100.0, future_candles=future, config=SwingConfig()
    )
    assert [s.kind for s in out["swings"][:2]] == ["adverse", "favorable"]
    assert out["swings"][0].price == 101.0
    assert abs(out["swings"][1].price - 99.5) < 1e-12
