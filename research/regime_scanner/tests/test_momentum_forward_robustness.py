"""Tests for momentum forward robustness analysis."""

from __future__ import annotations

import json

from research.regime_scanner.momentum_forward_audit import (
    COHORT_MOMENTUM_CONFIRMED,
    COHORT_MOMENTUM_EXPIRED,
    COHORT_MOMENTUM_INVALIDATED,
    COHORT_MOMENTUM_NOT_CONFIRMED,
)
from research.regime_scanner.momentum_forward_robustness import (
    SEGMENT_CONFIRMED_AGE0,
    SEGMENT_CONFIRMED_HIGH_AGE0,
    _age_is,
    build_monthly_cohort_summary,
    build_monthly_segment_summary,
    build_stability_summary,
    dedupe_outcome_rows,
    enrich_with_month,
    filter_segment,
    month_key_from_timestamp,
    primary_outcome_rows,
    run_robustness_from_outcomes,
    summarize_forward_metrics,
)


def _row(
    *,
    setup_id: str,
    cohort: str,
    month_ts: str,
    horizon: int = 12,
    side: str = "long",
    pattern_type: str = "higher_low",
    confidence: str | None = "medium",
    age: int | None = 0,
    mfe: float = 1.0,
    mae: float = 0.5,
    dret: float = 0.2,
    evaluable: bool = True,
    in_not_confirmed: bool = False,
    basis: str | None = None,
    regime: str = "bullish_trend",
) -> dict:
    if basis is None:
        basis = (
            "momentum_candle"
            if cohort == COHORT_MOMENTUM_CONFIRMED
            else "pa_candle"
        )
    return {
        "setup_id": setup_id,
        "side": side,
        "pattern_type": pattern_type,
        "cohort": cohort,
        "in_not_confirmed_combo": in_not_confirmed
        or cohort in {COHORT_MOMENTUM_INVALIDATED, COHORT_MOMENTUM_EXPIRED},
        "measurement_basis": basis,
        "measurement_timestamp": month_ts,
        "is_primary_basis": True,
        "horizon": horizon,
        "evaluable": evaluable,
        "reason": None if evaluable else "INSUFFICIENT_FUTURE_CANDLES",
        "directional_close_return_pct": dret if evaluable else None,
        "mfe_pct": mfe if evaluable else None,
        "mae_pct": mae if evaluable else None,
        "mfe_before_mae": True if evaluable else None,
        "momentum_confidence": confidence,
        "confirmation_age": age,
        "combined_regime": regime,
        "regime_5m": regime,
        "regime_15m": regime,
        "regime_30m": regime,
    }


def test_month_boundaries() -> None:
    assert month_key_from_timestamp("2026-01-31T23:55:00+00:00") == "2026-01"
    assert month_key_from_timestamp("2026-02-01T00:00:00+00:00") == "2026-02"
    assert month_key_from_timestamp("2026-03-01T00:00:00+00:00") == "2026-03"


def test_dedupe_same_setup_ids_across_months_concat() -> None:
    """Overlapping monthly pipelines must not double-count identical keys."""
    a = _row(
        setup_id="s1",
        cohort=COHORT_MOMENTUM_CONFIRMED,
        month_ts="2026-01-15T00:00:00+00:00",
        mfe=1.0,
    )
    b = dict(a)  # exact duplicate from bad concat
    c = _row(
        setup_id="s1",
        cohort=COHORT_MOMENTUM_CONFIRMED,
        month_ts="2026-02-15T00:00:00+00:00",
        mfe=2.0,
        horizon=12,
    )
    # Same setup_id+cohort+basis+horizon → second dropped even if month differs
    # (protects concat bugs; real continuous pipeline has unique setup ids)
    out = dedupe_outcome_rows([a, b, c])
    assert len(out) == 1
    assert out[0]["mfe_pct"] == 1.0


def test_aggregation_no_double_count_with_combo_rows() -> None:
    rows = [
        _row(
            setup_id="a",
            cohort=COHORT_MOMENTUM_INVALIDATED,
            month_ts="2026-01-10T00:00:00+00:00",
            in_not_confirmed=True,
            confidence=None,
            age=None,
        ),
        _row(
            setup_id="b",
            cohort=COHORT_MOMENTUM_EXPIRED,
            month_ts="2026-01-20T00:00:00+00:00",
            in_not_confirmed=True,
            confidence=None,
            age=None,
        ),
    ]
    primary = enrich_with_month(primary_outcome_rows(rows))
    # invalidated + expired + 2 combo copies
    assert sum(1 for r in primary if r["cohort"] == COHORT_MOMENTUM_NOT_CONFIRMED) == 2
    assert sum(1 for r in primary if r["cohort"] == COHORT_MOMENTUM_INVALIDATED) == 1
    monthly = build_monthly_cohort_summary(
        primary, months=["2026-01"], horizons=[12]
    )
    nc = next(
        r
        for r in monthly
        if r["month"] == "2026-01" and r["cohort"] == COHORT_MOMENTUM_NOT_CONFIRMED
    )
    inv = next(
        r
        for r in monthly
        if r["month"] == "2026-01" and r["cohort"] == COHORT_MOMENTUM_INVALIDATED
    )
    assert nc["n_evaluable"] == 2
    assert inv["n_evaluable"] == 1


def test_insufficient_horizon_at_data_end() -> None:
    rows = [
        _row(
            setup_id="end",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-06-27T00:00:00+00:00",
            evaluable=False,
            mfe=0.0,
            mae=0.0,
            dret=0.0,
        )
    ]
    primary = enrich_with_month(rows)
    summary = summarize_forward_metrics(
        primary,
        group_keys={"month": "2026-06", "cohort": COHORT_MOMENTUM_CONFIRMED},
    )
    assert summary["n_evaluable"] == 0
    assert summary["mfe_median"] is None
    assert summary["n_not_evaluable"] == 1


def test_empty_month_group() -> None:
    rows = [
        _row(
            setup_id="a",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-01-05T00:00:00+00:00",
        )
    ]
    primary = enrich_with_month(rows)
    monthly = build_monthly_cohort_summary(
        primary, months=["2026-01", "2026-02"], horizons=[12]
    )
    feb = next(
        r
        for r in monthly
        if r["month"] == "2026-02" and r["cohort"] == COHORT_MOMENTUM_CONFIRMED
    )
    assert feb["n_evaluable"] == 0
    assert feb["mfe_median"] is None


def test_confirmation_age_zero_is_preserved() -> None:
    """Falsy `x or fallback` must not rewrite age 0 to a fallback."""
    row = {"confirmation_age": 0}
    assert _age_is(row, 0) is True
    assert _age_is({"confirmation_age": None}, 0) is False

    primary = enrich_with_month(
        [
            _row(
                setup_id="age0",
                cohort=COHORT_MOMENTUM_CONFIRMED,
                month_ts="2026-04-01T00:00:00+00:00",
                confidence="medium",
                age=0,
                mfe=1.5,
            )
        ]
    )
    result = primary[0]
    assert result["confirmation_age"] == 0
    seg = filter_segment(primary, SEGMENT_CONFIRMED_AGE0)
    assert len(seg) == 1
    assert seg[0]["confirmation_age"] == 0


def test_high_age0_segment() -> None:
    rows = [
        _row(
            setup_id="hq",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-03-01T00:00:00+00:00",
            confidence="high",
            age=0,
            mfe=2.0,
        ),
        _row(
            setup_id="other",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-03-02T00:00:00+00:00",
            confidence="medium",
            age=1,
            mfe=0.5,
        ),
    ]
    primary = enrich_with_month(rows)
    seg = filter_segment(primary, SEGMENT_CONFIRMED_HIGH_AGE0)
    assert len(seg) == 1
    assert seg[0]["setup_id"] == "hq"
    monthly_seg = build_monthly_segment_summary(
        primary, months=["2026-03"], horizons=[12]
    )
    hq = next(
        r
        for r in monthly_seg
        if r.get("segment") == SEGMENT_CONFIRMED_HIGH_AGE0
        and r.get("month") == "2026-03"
        and r.get("horizon") == 12
    )
    assert hq["n_evaluable"] == 1
    assert abs(hq["mfe_median"] - 2.0) < 1e-12


def test_overall_matches_pooled_months_not_average_of_medians() -> None:
    """Overall aggregation pools signals; must not equal mean of monthly medians."""
    rows = [
        # Jan: one signal mfe=1
        _row(
            setup_id="j1",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-01-10T00:00:00+00:00",
            mfe=1.0,
            mae=1.0,
            dret=-0.1,
        ),
        # Feb: three signals mfe=3 → monthly median 3
        _row(
            setup_id="f1",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-02-10T00:00:00+00:00",
            mfe=3.0,
            mae=0.4,
            dret=0.1,
        ),
        _row(
            setup_id="f2",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-02-11T00:00:00+00:00",
            mfe=3.0,
            mae=0.4,
            dret=0.1,
        ),
        _row(
            setup_id="f3",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-02-12T00:00:00+00:00",
            mfe=3.0,
            mae=0.4,
            dret=0.1,
        ),
    ]
    primary = enrich_with_month(rows)
    monthly = build_monthly_cohort_summary(
        primary, months=["2026-01", "2026-02"], horizons=[12]
    )
    overall = next(
        r
        for r in monthly
        if r["month"] == "ALL" and r["cohort"] == COHORT_MOMENTUM_CONFIRMED
    )
    jan = next(
        r
        for r in monthly
        if r["month"] == "2026-01" and r["cohort"] == COHORT_MOMENTUM_CONFIRMED
    )
    feb = next(
        r
        for r in monthly
        if r["month"] == "2026-02" and r["cohort"] == COHORT_MOMENTUM_CONFIRMED
    )
    assert overall["n_evaluable"] == 4
    assert abs(overall["mfe_median"] - 3.0) < 1e-12  # pooled median of [1,3,3,3]
    mean_of_month_medians = 0.5 * (float(jan["mfe_median"]) + float(feb["mfe_median"]))
    assert abs(mean_of_month_medians - 2.0) < 1e-12
    assert abs(float(overall["mfe_median"]) - mean_of_month_medians) > 0.5


def test_stability_and_deterministic() -> None:
    rows = []
    for i, (month, conf_mae, nc_mae) in enumerate(
        [
            ("2026-01", 0.5, 1.0),
            ("2026-02", 0.6, 0.9),
            ("2026-03", 1.2, 0.8),  # oppose on MAE
        ]
    ):
        rows.append(
            _row(
                setup_id=f"c{i}",
                cohort=COHORT_MOMENTUM_CONFIRMED,
                month_ts=f"{month}-10T00:00:00+00:00",
                mfe=1.0,
                mae=conf_mae,
                dret=0.1,
                confidence="high",
                age=0,
            )
        )
        rows.append(
            _row(
                setup_id=f"n{i}",
                cohort=COHORT_MOMENTUM_INVALIDATED,
                month_ts=f"{month}-11T00:00:00+00:00",
                mfe=0.5,
                mae=nc_mae,
                dret=-0.2,
                confidence=None,
                age=None,
                in_not_confirmed=True,
            )
        )
    # Expand to all outcome shapes via robustness runner
    # Need both bases? primary_outcome_rows expects is_primary_basis already.
    # Add horizons 1 for consistency check path
    expanded = []
    for r in rows:
        for h in (1, 12):
            rr = dict(r)
            rr["horizon"] = h
            expanded.append(rr)

    p1 = run_robustness_from_outcomes(
        expanded, months=["2026-01", "2026-02", "2026-03"], horizons=(1, 12)
    )
    p2 = run_robustness_from_outcomes(
        expanded, months=["2026-01", "2026-02", "2026-03"], horizons=(1, 12)
    )
    assert json.dumps(p1["stability_summary"]["research_answers"], sort_keys=True) == json.dumps(
        p2["stability_summary"]["research_answers"], sort_keys=True
    )
    stab = p1["stability_summary"]
    assert "2026-01" in stab["months_confirmed_lower_mae"]
    assert "2026-02" in stab["months_confirmed_lower_mae"]
    assert "2026-03" in stab["months_confirmed_higher_mae"]
    assert stab["n_months_mae_win"] == 2


def test_mfe_gt_mae_and_ratio() -> None:
    rows = [
        _row(
            setup_id="a",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-04-01T00:00:00+00:00",
            mfe=2.0,
            mae=1.0,
        ),
        _row(
            setup_id="b",
            cohort=COHORT_MOMENTUM_CONFIRMED,
            month_ts="2026-04-02T00:00:00+00:00",
            mfe=0.5,
            mae=1.0,
        ),
    ]
    s = summarize_forward_metrics(rows, group_keys={"x": 1})
    assert abs(s["mfe_gt_mae_share"] - 0.5) < 1e-12
    assert s["mfe_mae_ratio_median"] is not None
