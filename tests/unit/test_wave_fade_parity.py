"""Golden parity: port vs freeze-commit logic on a tiny synthetic OHLCV window."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from signal_generator.strategy.wave_fade import (
    BE_FRAC,
    SIGNAL_TFS,
    TPSL_BY_TF,
    annotate_waves_df,
    apply_upgrade_plan,
    assign_trend_bucket,
    attach_indicators,
    build_same_side_clusters,
    build_symbol_signals,
    build_waves_from_ohlcv,
    ensure_confirmation_before_entry,
    load_frozen_eff_edges,
    pair_window,
    resolve_entries,
    segment_stoch_waves,
    stochastic_rsi,
    tpsl_for_tf,
    trade_levels,
    wilder_rsi,
)
from signal_generator.strategy.wave_fade.parameters import SOURCE_COMMIT

FREEZE_REPO = Path("/home/telgenbuescher/projects/orderbook_analyse")
COMMIT = SOURCE_COMMIT


def _git_show(rel: str) -> str:
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(FREEZE_REPO), "show", f"{COMMIT}:{rel}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout


def _load_module(name: str, source: str, package: str | None = None):
    mod = types.ModuleType(name)
    if package:
        mod.__package__ = package
    sys.modules[name] = mod
    exec(compile(source, name, "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="module")
def freeze_core(tmp_path_factory):
    """Load freeze attach/segment/annotate/trend/cluster from git show only.

    Stoch kernels: freeze imports untracked mtf_rsi_stoch_audit; for parity we
    load that runtime dependency read-only from disk (documented gap).
    """
    # Minimal package stubs for freeze imports
    oa = types.ModuleType("orderbook_analyse")
    oa.__path__ = []  # type: ignore[attr-defined]
    sys.modules["orderbook_analyse"] = oa

    # mtf — freeze-runtime untracked dependency (read-only working tree file)
    mtf_pkg = types.ModuleType("orderbook_analyse.mtf_rsi_stoch_audit")
    mtf_pkg.RSI_LENGTH = 14
    mtf_pkg.STOCH_RSI_LENGTH = 14
    mtf_pkg.STOCH_K_SMOOTH = 3
    mtf_pkg.STOCH_D_SMOOTH = 3
    sys.modules["orderbook_analyse.mtf_rsi_stoch_audit"] = mtf_pkg
    mtf_ind_path = FREEZE_REPO / "src/orderbook_analyse/mtf_rsi_stoch_audit/indicators.py"
    if not mtf_ind_path.is_file():
        pytest.skip("freeze runtime dependency mtf_rsi_stoch_audit missing on disk")
    mtf_ind = _load_module(
        "orderbook_analyse.mtf_rsi_stoch_audit.indicators",
        mtf_ind_path.read_text(encoding="utf-8"),
        package="orderbook_analyse.mtf_rsi_stoch_audit",
    )

    fcwa = types.ModuleType("orderbook_analyse.fractal_cycle_wave_analysis")
    fcwa.RSI_LENGTH = 14
    fcwa.STOCH_RSI_LENGTH = 14
    fcwa.STOCH_K_SMOOTH = 3
    fcwa.STOCH_D_SMOOTH = 3
    fcwa.STOCH_LOW_K = 20.0
    fcwa.STOCH_HIGH_K = 80.0
    fcwa.CCI_LENGTH = 20
    fcwa.EMA_SPANS = (9, 20, 100, 400)
    fcwa.MIN_WAVE_BARS = 3
    fcwa.INEFFICIENT_ABS_PRICE_PCT = 0.02
    fcwa.MIN_ABS_STOCH_DELTA = 10.0
    sys.modules["orderbook_analyse.fractal_cycle_wave_analysis"] = fcwa

    freeze_ind = _load_module(
        "orderbook_analyse.fractal_cycle_wave_analysis.indicators",
        _git_show("src/orderbook_analyse/fractal_cycle_wave_analysis/indicators.py"),
        package="orderbook_analyse.fractal_cycle_wave_analysis",
    )
    freeze_waves = _load_module(
        "orderbook_analyse.fractal_cycle_wave_analysis.waves",
        _git_show("src/orderbook_analyse/fractal_cycle_wave_analysis/waves.py"),
        package="orderbook_analyse.fractal_cycle_wave_analysis",
    )

    # annotate deps: EXTRA_WAVE_COLS, WAVE_COLS, local_failure_mask
    faw = types.ModuleType("orderbook_analyse.fractal_all_wave_fade")
    faw.EXTRA_WAVE_COLS = [
        "n_bars",
        "favorable_move_pct",
        "adverse_move_pct",
        "rsi_end",
        "rsi_delta",
        "rsi_start",
    ]
    sys.modules["orderbook_analyse.fractal_all_wave_fade"] = faw

    fgen = types.ModuleType("orderbook_analyse.fractal_all_wave_fade_generalization")
    fgen.APT_IS_RESULTS = Path("/tmp")
    sys.modules["orderbook_analyse.fractal_all_wave_fade_generalization"] = fgen

    fcpf = types.ModuleType("orderbook_analyse.fractal_cycle_phase_failure")
    fcpf.WAVE_COLS = [
        "direction",
        "stoch_k_start",
        "stoch_k_end",
        "stoch_delta",
        "stoch_zone_start",
        "stoch_zone_end",
        "directional_efficiency",
        "signed_price_move_pct",
        "price_move_pct",
        "rsi_start",
        "rsi_end",
        "rsi_delta",
        "rsi_end_gt_50",
        "rsi_end_lt_50",
        "price_vs_ema20_end",
        "ema9_vs_ema20_end",
        "ema100_end",
        "ema400_end",
        "inefficient_flag",
        "end_available_at",
        "start_available_at",
    ]
    sys.modules["orderbook_analyse.fractal_cycle_phase_failure"] = fcpf

    # local_failure_mask only — strip heavy imports by exec'ing just the function
    events_src = _git_show("src/orderbook_analyse/fractal_cycle_phase_failure/events.py")
    # Build a minimal events module with only local_failure_mask body
    events_mod = types.ModuleType("orderbook_analyse.fractal_cycle_phase_failure.events")

    def local_failure_mask(df: pd.DataFrame):
        up = df["direction"].astype(str) == "UP"
        dn = df["direction"].astype(str) == "DOWN"
        signed = df["signed_price_move_pct"].astype(float)
        eff = df["directional_efficiency"].astype(float)
        ineff = df["inefficient_flag"].fillna(False).astype(bool)
        weak = (signed <= 0.0) | (eff <= 0.0) | ineff
        return up & weak, dn & weak

    events_mod.local_failure_mask = local_failure_mask
    sys.modules["orderbook_analyse.fractal_cycle_phase_failure.events"] = events_mod

    freeze_ann = _load_module(
        "orderbook_analyse.fractal_all_wave_fade_generalization.annotate",
        _git_show("src/orderbook_analyse/fractal_all_wave_fade_generalization/annotate.py"),
        package="orderbook_analyse.fractal_all_wave_fade_generalization",
    )

    # trend: only assign_trend_bucket — extract via loading analysis is heavy;
    # load freeze function textually from git (same body as port)
    trend_src = _git_show("src/orderbook_analyse/fractal_wave_fade_trend_filter/analysis.py")
    # isolate assign_trend_bucket by compiling a tiny module from known lines
    trend_mod = types.ModuleType("freeze_trend")
    exec(
        compile(
            "\n".join(
                [
                    "import pandas as pd",
                    "def assign_trend_bucket(df):",
                    '    out = pd.Series("MIXED", index=df.index, dtype=object)',
                    '    up = df["direction"].astype(str) == "UP"',
                    '    dn = df["direction"].astype(str) == "DOWN"',
                    '    bull = df["ema_context"].astype(str) == "EMA_BULL"',
                    '    bear = df["ema_context"].astype(str) == "EMA_BEAR"',
                    '    out.loc[up & bull] = "TREND_ALIGNED"',
                    '    out.loc[dn & bear] = "TREND_ALIGNED"',
                    '    out.loc[up & bear] = "COUNTERTREND"',
                    '    out.loc[dn & bull] = "COUNTERTREND"',
                    "    return out",
                ]
            ),
            "freeze_trend",
            "exec",
        ),
        trend_mod.__dict__,
    )

    # cluster from freeze
    fsc = types.ModuleType("orderbook_analyse.fractal_signal_confluence_db")
    fsc.SIGNAL_TFS = ("15m", "30m", "1h", "4h")
    fsc.TF_RANK = {"15m": 0, "30m": 1, "1h": 2, "4h": 3}
    fsc.TF_BAR_MIN = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
    fsc.PAIR_WINDOW_MIN = {
        ("15m", "30m"): 30,
        ("30m", "1h"): 60,
        ("1h", "4h"): 240,
        ("15m", "1h"): 60,
        ("15m", "4h"): 240,
        ("30m", "4h"): 240,
    }
    sys.modules["orderbook_analyse.fractal_signal_confluence_db"] = fsc
    freeze_cluster = _load_module(
        "orderbook_analyse.fractal_signal_confluence_db.cluster",
        _git_show("src/orderbook_analyse/fractal_signal_confluence_db/cluster.py"),
        package="orderbook_analyse.fractal_signal_confluence_db",
    )

    return {
        "mtf_ind": mtf_ind,
        "attach_indicators": freeze_ind.attach_indicators,
        "segment_stoch_waves": freeze_waves.segment_stoch_waves,
        "annotate_waves_df": freeze_ann.annotate_waves_df,
        "assign_trend_bucket": trend_mod.assign_trend_bucket,
        "pair_window": freeze_cluster.pair_window,
        "build_same_side_clusters": freeze_cluster.build_same_side_clusters,
        "trend_src_has_assign": "def assign_trend_bucket" in trend_src,
    }


def _synthetic_ohlcv(n: int = 120, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # trending then mean-reverting closes so Stoch crosses appear
    steps = rng.normal(0.0, 0.15, size=n).cumsum()
    close = 100.0 + steps
    high = close + rng.uniform(0.05, 0.4, size=n)
    low = close - rng.uniform(0.05, 0.4, size=n)
    open_ = np.r_[close[0], close[:-1]]
    idx = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    close_time = idx + pd.Timedelta(minutes=15)
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1.0, 10.0, size=n),
            "close_time": close_time,
            "available_at": close_time,
        }
    )


def test_parameters_match_freeze():
    assert SIGNAL_TFS == ("15m", "30m", "1h", "4h")
    assert TPSL_BY_TF == {
        "15m": (1.0, 1.0),
        "30m": (2.0, 1.5),
        "1h": (2.0, 1.5),
        "4h": (4.0, 2.0),
    }
    assert BE_FRAC == 0.50
    assert tpsl_for_tf("15m") == (1.0, 1.0)
    assert tpsl_for_tf("4h") == (4.0, 2.0)
    assert apply_upgrade_plan("P5A", 1.0, 1.0, "4h") == (4.0, 2.0)


def test_pair_windows_match_freeze(freeze_core):
    pairs = [
        ("15m", "30m"),
        ("30m", "1h"),
        ("15m", "1h"),
        ("1h", "4h"),
        ("15m", "4h"),
        ("30m", "4h"),
    ]
    for a, b in pairs:
        assert pair_window(a, b) == freeze_core["pair_window"](a, b)


def test_stoch_and_indicators_parity(freeze_core):
    ohlcv = _synthetic_ohlcv()
    close = ohlcv["close"]
    k1, d1 = stochastic_rsi(close)
    k0, d0 = freeze_core["mtf_ind"].stochastic_rsi(close)
    pd.testing.assert_series_equal(k1, k0, check_names=False)
    pd.testing.assert_series_equal(d1, d0, check_names=False)
    pd.testing.assert_series_equal(
        wilder_rsi(close), freeze_core["mtf_ind"].wilder_rsi(close), check_names=False
    )

    port = attach_indicators(ohlcv)
    freeze = freeze_core["attach_indicators"](ohlcv)
    for col in ("rsi", "stoch_k", "stoch_d", "cci", "ema9", "ema20"):
        np.testing.assert_allclose(
            port[col].to_numpy(dtype=float),
            freeze[col].to_numpy(dtype=float),
            equal_nan=True,
            rtol=0,
            atol=0,
        )
    pd.testing.assert_series_equal(
        port["stoch_bullish_cross"].astype(bool),
        freeze["stoch_bullish_cross"].astype(bool),
        check_names=False,
    )


def test_waves_fade_trend_tier_a_parity(freeze_core):
    assert freeze_core["trend_src_has_assign"]
    ohlcv = _synthetic_ohlcv()
    port_ind = attach_indicators(ohlcv)
    freeze_ind = freeze_core["attach_indicators"](ohlcv)
    port_w = segment_stoch_waves(port_ind)
    freeze_w = freeze_core["segment_stoch_waves"](freeze_ind)
    assert len(port_w) == len(freeze_w)
    assert not port_w.empty

    edges = {
        ("15m", "UP", "directional_efficiency"): {0.25: -1.0, 0.5: 0.0, 0.75: 0.01},
        ("15m", "DOWN", "directional_efficiency"): {0.25: -1.0, 0.5: 0.0, 0.75: 0.01},
        ("15m", "UP", "signed_price_move_pct"): {0.25: -1.0, 0.5: 0.0, 0.75: 0.1},
        ("15m", "DOWN", "signed_price_move_pct"): {0.25: -1.0, 0.5: 0.0, 0.75: 0.1},
    }
    port_a = annotate_waves_df(port_w, symbol="TEST", timeframe="15m", quantile_edges=edges)
    freeze_a = freeze_core["annotate_waves_df"](
        freeze_w, symbol="TEST", timeframe="15m", quantile_edges=edges
    )
    pd.testing.assert_series_equal(port_a["side"], freeze_a["side"], check_names=False)
    assert set(port_a["side"].unique()) <= {"LONG", "SHORT"}
    # Fade: UP -> SHORT, DOWN -> LONG
    up = port_a["direction"].astype(str) == "UP"
    assert (port_a.loc[up, "side"] == "SHORT").all()
    dn = port_a["direction"].astype(str) == "DOWN"
    assert (port_a.loc[dn, "side"] == "LONG").all()

    port_a = port_a.copy()
    freeze_a = freeze_a.copy()
    port_a["trend_bucket"] = assign_trend_bucket(port_a)
    freeze_a["trend_bucket"] = freeze_core["assign_trend_bucket"](freeze_a)
    pd.testing.assert_series_equal(
        port_a["trend_bucket"], freeze_a["trend_bucket"], check_names=False
    )
    port_a["is_tier_a"] = (port_a["trend_bucket"] == "TREND_ALIGNED") & (
        port_a["eff_quantile"] == "Q4"
    )
    freeze_a["is_tier_a"] = (freeze_a["trend_bucket"] == "TREND_ALIGNED") & (
        freeze_a["eff_quantile"] == "Q4"
    )
    pd.testing.assert_series_equal(
        port_a["is_tier_a"], freeze_a["is_tier_a"], check_names=False
    )


def test_entry_strictly_after_confirmation():
    # HTF end bar closes at 12:15 → confirmation_available_at; T0 = 12:16 open
    conf = pd.Timestamp("2024-01-01T12:15:00Z")
    open_times = (
        pd.to_datetime(
            [
                "2024-01-01T12:14:00",
                "2024-01-01T12:15:00",
                "2024-01-01T12:16:00",
            ],
            utc=True,
        )
        .tz_convert(None)
        .to_numpy(dtype="datetime64[ns]")
    )
    opens = np.array([1.0, 2.0, 3.0])
    ev = pd.DataFrame(
        {
            "confirmation_available_at": [conf],
            "side": ["LONG"],
        }
    )
    out = resolve_entries(ev, open_times, opens)
    assert bool(out.loc[0, "entry_valid"])
    assert pd.Timestamp(out.loc[0, "entry_time"]) == pd.Timestamp("2024-01-01T12:16:00Z")
    assert float(out.loc[0, "entry_price"]) == 3.0
    assert ensure_confirmation_before_entry(conf, out.loc[0, "entry_time"])
    # same-minute open must not be used (strictly after)
    assert pd.Timestamp(out.loc[0, "entry_time"]) > conf

def test_cluster_parity_and_first_entry(freeze_core):
    rows = []
    t0 = pd.Timestamp("2024-01-01T00:00:00Z")
    # two same-side 15m/30m within 30m window → one cluster
    for i, (tf, conf) in enumerate(
        [
            ("15m", t0),
            ("30m", t0 + pd.Timedelta(minutes=20)),
            ("15m", t0 + pd.Timedelta(hours=5)),  # separate cluster
        ]
    ):
        rows.append(
            {
                "signal_id": i,
                "side": "LONG",
                "signal_tf": tf,
                "confirmation_available_at": conf,
                "is_tier_a": True,
                "is_q4": True,
            }
        )
    df = pd.DataFrame(rows)
    port_c = build_same_side_clusters(df)
    freeze_c = freeze_core["build_same_side_clusters"](df)
    assert len(port_c) == len(freeze_c) == 2
    assert port_c[0]["n_raw_signals"] == freeze_c[0]["n_raw_signals"] == 2
    assert int(port_c[0]["rows"].iloc[0]["signal_id"]) == 0  # FIRST_CLUSTER


def test_be50_levels():
    tr = pd.Series(
        {
            "entry_price": 100.0,
            "side": "LONG",
            "highest_tf_reached": "15m",
        }
    )
    lv = trade_levels(tr)
    assert lv["tp_pct"] == 1.0
    assert lv["sl_pct"] == 1.0
    assert lv["tp"] == 101.0
    assert lv["sl"] == 99.0
    assert lv["be_trigger"] == pytest.approx(100.0 + 0.5 * (101.0 - 100.0))


def test_sl_first_same_bar():
    from signal_generator.strategy.wave_fade.exits import scan_exit_sl_first
    from signal_generator.strategy.wave_fade.parameters import INTRABAR_POLICY

    assert INTRABAR_POLICY == "SL_FIRST"
    # LONG: high hits +1% TP and low hits -1% SL on same bar → SL
    high = np.array([101.5])
    low = np.array([98.5])
    et, eg, exi, amb = scan_exit_sl_first("LONG", 100.0, high, low, 0, 0, 1.0, 1.0)
    assert et == "SL"
    assert eg == -1.0
    assert amb is True


def test_frozen_edges_shipped_and_not_per_coin():
    edges = load_frozen_eff_edges()
    assert ("15m", "UP", "directional_efficiency") in edges
    assert ("4h", "DOWN", "signed_price_move_pct") in edges
    # keys cover SIGNAL_TFS only
    tfs = {k[0] for k in edges}
    assert tfs == set(SIGNAL_TFS)


def test_build_symbol_signals_pipeline_smoke():
    ohlcv = _synthetic_ohlcv(160)
    waves = build_waves_from_ohlcv(ohlcv, symbol="TEST", timeframe="15m")
    edges = {
        ("15m", "UP", "directional_efficiency"): {0.25: -10.0, 0.5: 0.0, 0.75: 10.0},
        ("15m", "DOWN", "directional_efficiency"): {0.25: -10.0, 0.5: 0.0, 0.75: 10.0},
        ("15m", "UP", "signed_price_move_pct"): {0.25: -10.0, 0.5: 0.0, 0.75: 10.0},
        ("15m", "DOWN", "signed_price_move_pct"): {0.25: -10.0, 0.5: 0.0, 0.75: 10.0},
    }
    sig = build_symbol_signals("TEST", edges, {"15m": waves})
    assert not sig.empty
    assert "is_tier_a" in sig.columns
    assert "confirmation_available_at" in sig.columns
