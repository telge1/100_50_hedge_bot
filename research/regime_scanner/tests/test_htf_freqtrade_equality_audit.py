"""Unit tests for HTF Freqtrade vs 5m-aggregation equality audit helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.htf_freqtrade_equality_audit import (
    ABS_TOL,
    REL_TOL,
    aggregate_from_canonical_5m,
    classify_mismatch_row,
    compare_timeframe,
    run_audit,
    values_equal_exact,
    values_within_tol,
)
from research.regime_scanner.timeframes import BARS_PER_AGGREGATE, expected_5m_opens


def _make_5m(start: str, n: int, *, base_price: float = 100.0) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    rows = []
    for i in range(n):
        ts = start_ts + pd.Timedelta(minutes=5 * i)
        o = base_price + i * 0.1
        rows.append(
            {
                "date": ts,
                "open": o,
                "high": o + 1.0,
                "low": o - 1.0,
                "close": o + 0.5,
                "volume": float(10 + i),
            }
        )
    return pd.DataFrame(rows)


def _agg_to_direct(agg: pd.DataFrame) -> pd.DataFrame:
    return agg.loc[:, ["date", "open", "high", "low", "close", "volume"]].copy()


def test_15m_aggregation_three_5m_candles() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 12)
    agg = aggregate_from_canonical_5m(candles, "15m")
    assert not agg.empty
    assert int(agg["source_5m_count"].iloc[0]) == 3
    assert all(bool(x) for x in agg["bucket_complete"])
    first = agg.iloc[0]
    assert pd.Timestamp(first["date"]) == pd.Timestamp("2026-03-01T00:00:00+00:00")
    opens = expected_5m_opens(first["date"], "15m")
    assert len(opens) == 3
    src = candles.loc[candles["date"].isin(opens)]
    assert len(src) == 3
    assert float(first["open"]) == float(src["open"].iloc[0])
    assert float(first["high"]) == float(src["high"].max())
    assert float(first["low"]) == float(src["low"].min())
    assert float(first["close"]) == float(src["close"].iloc[-1])
    assert float(first["volume"]) == float(src["volume"].sum())
    assert pd.Timestamp(first["decision_time"]) == pd.Timestamp("2026-03-01T00:15:00+00:00")


def test_30m_aggregation_six_5m_candles() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 24)
    agg = aggregate_from_canonical_5m(candles, "30m")
    first = agg.iloc[0]
    assert int(first["source_5m_count"]) == 6
    opens = expected_5m_opens(first["date"], "30m")
    assert len(opens) == 6
    src = candles.loc[candles["date"].isin(opens)]
    assert float(first["open"]) == float(src["open"].iloc[0])
    assert float(first["high"]) == float(src["high"].max())
    assert float(first["low"]) == float(src["low"].min())
    assert float(first["close"]) == float(src["close"].iloc[-1])
    assert float(first["volume"]) == float(src["volume"].sum())


def test_incomplete_buckets_excluded() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 12)
    # Drop middle bar of first 15m bucket.
    candles = candles.loc[candles["date"] != pd.Timestamp("2026-03-01T00:05:00+00:00")].reset_index(
        drop=True
    )
    agg = aggregate_from_canonical_5m(candles, "15m")
    assert pd.Timestamp("2026-03-01T00:00:00+00:00") not in set(agg["date"])


def test_utc_bucket_alignment() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 36)
    for tf in ("15m", "30m"):
        agg = aggregate_from_canonical_5m(candles, tf)
        minutes = 15 if tf == "15m" else 30
        for ts in agg["date"]:
            t = pd.Timestamp(ts)
            assert t.tz is not None
            assert str(t.tz) == "UTC"
            assert int(t.minute) % minutes == 0
            assert int(t.second) == 0


def test_exact_matches_counted() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 36)
    agg = aggregate_from_canonical_5m(candles, "15m")
    direct = _agg_to_direct(agg)
    res = compare_timeframe(timeframe="15m", candles_5m=candles, direct=direct)
    n = res.summary["counts"]["shared_timestamps"]
    assert n > 0
    assert res.summary["match_rates"]["exact_full_ohlcv_matches"] == n
    assert res.mismatches.empty


def test_float_tolerance_classification() -> None:
    a = 1.0
    b = 1.0 + 1e-13
    assert not values_equal_exact(a, b)
    assert values_within_tol(a, b, abs_tol=ABS_TOL, rel_tol=REL_TOL)
    c = 1.0 + 1e-6
    assert not values_within_tol(a, c, abs_tol=ABS_TOL, rel_tol=REL_TOL)


def test_price_mismatch_detected() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 36)
    agg = aggregate_from_canonical_5m(candles, "15m")
    direct = _agg_to_direct(agg)
    direct.loc[direct.index[0], "close"] = float(direct.iloc[0]["close"]) + 0.25
    res = compare_timeframe(timeframe="15m", candles_5m=candles, direct=direct)
    assert res.summary["match_rates"]["exact_ohlc_matches"] < res.summary["counts"]["shared_timestamps"]
    assert not res.mismatches.empty
    assert "close_mismatch" in str(res.mismatches.iloc[0]["mismatch_categories"])


def test_volume_mismatch_detected() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 36)
    agg = aggregate_from_canonical_5m(candles, "15m")
    direct = _agg_to_direct(agg)
    direct.loc[direct.index[0], "volume"] = float(direct.iloc[0]["volume"]) + 1.0
    res = compare_timeframe(timeframe="15m", candles_5m=candles, direct=direct)
    assert res.summary["match_rates"]["exact_volume_matches"] < res.summary["counts"]["shared_timestamps"]
    assert "volume_mismatch" in str(res.mismatches.iloc[0]["mismatch_categories"])


def test_direct_only_and_aggregated_only() -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 36)
    agg = aggregate_from_canonical_5m(candles, "15m")
    direct = _agg_to_direct(agg)

    # missing_in_direct: drop a middle aggregated open (keep common_start unchanged).
    drop_ts = pd.Timestamp(agg["date"].iloc[2])
    direct_missing = direct.loc[direct["date"] != drop_ts].copy()
    res_missing_direct = compare_timeframe(
        timeframe="15m", candles_5m=candles, direct=direct_missing
    )
    assert res_missing_direct.summary["counts"]["missing_in_direct"] >= 1
    assert any(
        "missing_in_direct" in c
        for c in res_missing_direct.mismatches["mismatch_categories"].astype(str)
    )

    # missing_in_aggregated / direct-only: inject a phantom open inside the window.
    phantom = pd.Timestamp(agg["date"].iloc[0]) + pd.Timedelta(minutes=5)
    row = direct.iloc[0].copy()
    row["date"] = phantom
    direct_extra = pd.concat([direct, pd.DataFrame([row])], ignore_index=True)
    res_extra = compare_timeframe(timeframe="15m", candles_5m=candles, direct=direct_extra)
    assert res_extra.summary["counts"]["missing_in_aggregated"] >= 1
    assert any(
        "missing_in_aggregated" in c
        for c in res_extra.mismatches["mismatch_categories"].astype(str)
    )

    # direct_only_after_5m_end: bar after last complete aggregated open.
    after = direct.iloc[-1:].copy()
    after["date"] = pd.Timestamp(agg["date"].iloc[-1]) + pd.Timedelta(minutes=15)
    direct_after = pd.concat([direct, after], ignore_index=True)
    res_after = compare_timeframe(timeframe="15m", candles_5m=candles, direct=direct_after)
    assert res_after.summary["counts"]["direct_only_after_5m_end"] >= 1


def test_mismatch_categories_deterministic() -> None:
    cats1 = classify_mismatch_row(
        present_direct=True,
        present_agg=True,
        price_exact={"open": False, "high": False, "low": True, "close": True},
        volume_exact=False,
        source_5m_count=3,
        expected_count=3,
    )
    cats2 = classify_mismatch_row(
        present_direct=True,
        present_agg=True,
        price_exact={"open": False, "high": False, "low": True, "close": True},
        volume_exact=False,
        source_5m_count=3,
        expected_count=3,
    )
    assert cats1 == cats2
    assert cats1[0] == "open_mismatch"
    assert "multiple_value_mismatch" in cats1
    assert "volume_mismatch" in cats1


def test_input_files_never_mutated(tmp_path: Path) -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 72)
    agg15 = aggregate_from_canonical_5m(candles, "15m")
    agg30 = aggregate_from_canonical_5m(candles, "30m")
    f5 = tmp_path / "APT_USDT_USDT-5m-futures.feather"
    f15 = tmp_path / "APT_USDT_USDT-15m-futures.feather"
    f30 = tmp_path / "APT_USDT_USDT-30m-futures.feather"
    candles.to_feather(f5)
    _agg_to_direct(agg15).to_feather(f15)
    _agg_to_direct(agg30).to_feather(f30)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (f5, f15, f30)}
    out = tmp_path / "out"
    run_audit(
        five_minute_file=f5,
        fifteen_minute_file=f15,
        thirty_minute_file=f30,
        output_dir=out,
        require_expected_hashes=False,
    )
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (f5, f15, f30)}
    assert before == after
    assert (out / "summary.json").exists()
    assert (out / "mismatches_15m.csv").exists()
    assert (out / "mismatches_30m.csv").exists()


def test_audit_output_deterministic(tmp_path: Path) -> None:
    candles = _make_5m("2026-03-01T00:00:00+00:00", 72)
    agg15 = aggregate_from_canonical_5m(candles, "15m")
    agg30 = aggregate_from_canonical_5m(candles, "30m")
    f5 = tmp_path / "5m.feather"
    f15 = tmp_path / "15m.feather"
    f30 = tmp_path / "30m.feather"
    candles.to_feather(f5)
    _agg_to_direct(agg15).to_feather(f15)
    _agg_to_direct(agg30).to_feather(f30)
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    s1 = run_audit(
        five_minute_file=f5,
        fifteen_minute_file=f15,
        thirty_minute_file=f30,
        output_dir=out1,
        require_expected_hashes=False,
    )
    s2 = run_audit(
        five_minute_file=f5,
        fifteen_minute_file=f15,
        thirty_minute_file=f30,
        output_dir=out2,
        require_expected_hashes=False,
    )
    assert s1["deterministic_hash"] == s2["deterministic_hash"]
    assert (out1 / "summary.json").read_text() == (out2 / "summary.json").read_text()


def test_bars_per_aggregate_constants() -> None:
    assert BARS_PER_AGGREGATE["15m"] == 3
    assert BARS_PER_AGGREGATE["30m"] == 6
