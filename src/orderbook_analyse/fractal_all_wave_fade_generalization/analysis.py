"""Orchestrate frozen all-wave fade OOS / cross-symbol generalization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization import (
    APT_IS_END,
    APT_IS_RESULTS,
    APT_OOS_MIN_DAYS,
    APT_OOS_MIN_WAVES_15M,
    APT_WAVE_DIR,
    AUDIT_VERSION,
    DEFINITIONS_DOC,
    EDGE_DELAYS_BY_TF,
    FEE_PCT,
    MAIN_HORIZON_BY_TF,
    MIN_SAMPLE,
    SOURCE_AUDIT,
    TRADING_TFS,
)
from orderbook_analyse.fractal_all_wave_fade_generalization.annotate import (
    annotate_waves_df,
    load_frozen_quantile_edges,
)
from orderbook_analyse.fractal_all_wave_fade_generalization.metrics import summarize_net
from orderbook_analyse.fractal_all_wave_fade_generalization.pivot import (
    decide_pivot_utility,
    pivot_utility_summary,
)
from orderbook_analyse.fractal_all_wave_fade_generalization.waves_build import (
    build_or_load_waves,
    symbol_coverage,
)
from orderbook_analyse.fractal_failure_multitimeframe.outcomes import (
    attach_forward_with_opens,
    load_1m,
)


def _sides(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    return [
        ("COMBINED", df),
        ("LONG", df[df["side"] == "LONG"]),
        ("SHORT", df[df["side"] == "SHORT"]),
    ]


def _pass_fail(ok: bool | None) -> str:
    if ok is None:
        return "INSUFFICIENT"
    return "PASS" if ok else "FAIL"


def evaluate_hypotheses(bundle: dict[str, Any]) -> dict[str, str]:
    """Per-symbol H1..H5."""
    out: dict[str, str] = {}
    all_rows = bundle.get("all_wave_results") or []
    fail_rows = bundle.get("failure_comparison") or []
    qual = bundle.get("wave_quality_results") or []
    trend = bundle.get("trend_rsi_results") or []
    decay = bundle.get("edge_decay") or []

    # H1: majority of TFs ALL COMBINED hit>=0.52 & median_net>0 & n>=MIN
    h1_pass = h1_fail = 0
    for tf in TRADING_TFS:
        r = next(
            (
                x
                for x in all_rows
                if x.get("timeframe") == tf
                and x.get("wave_group") == "ALL"
                and x.get("side") == "COMBINED"
            ),
            None,
        )
        if not r or r.get("sample_flag") != "OK":
            continue
        if (r.get("hit_rate") or 0) >= 0.52 and (r.get("median_net") or 0) > 0:
            h1_pass += 1
        else:
            h1_fail += 1
    if h1_pass + h1_fail == 0:
        out["H1"] = "INSUFFICIENT"
    else:
        out["H1"] = "PASS" if h1_pass >= 3 and h1_pass > h1_fail else ("PASS" if h1_pass >= 3 else "FAIL")

    # H2: NON_FAILED median_net >= FAILED on majority TFs
    h2_ok = h2_bad = 0
    for tf in TRADING_TFS:
        f = next(
            (
                x
                for x in fail_rows
                if x.get("timeframe") == tf
                and x.get("wave_group") == "FAILED"
                and x.get("side") == "COMBINED"
            ),
            None,
        )
        nf = next(
            (
                x
                for x in fail_rows
                if x.get("timeframe") == tf
                and x.get("wave_group") == "NON_FAILED"
                and x.get("side") == "COMBINED"
            ),
            None,
        )
        if not f or not nf or f.get("sample_flag") != "OK" or nf.get("sample_flag") != "OK":
            continue
        if (nf.get("median_net") or -999) >= (f.get("median_net") or -999):
            h2_ok += 1
        else:
            h2_bad += 1
    if h2_ok + h2_bad == 0:
        out["H2"] = "INSUFFICIENT"
    else:
        out["H2"] = "PASS" if h2_ok >= 3 and h2_ok > h2_bad else "FAIL"

    # H3: Q4 > Q1 efficiency on majority TF x direction
    h3_ok = h3_bad = 0
    for tf in TRADING_TFS:
        for direction in ("UP", "DOWN"):
            q1 = next(
                (
                    x
                    for x in qual
                    if x.get("timeframe") == tf
                    and x.get("direction") == direction
                    and x.get("metric") == "efficiency"
                    and x.get("quantile") == "Q1"
                ),
                None,
            )
            q4 = next(
                (
                    x
                    for x in qual
                    if x.get("timeframe") == tf
                    and x.get("direction") == direction
                    and x.get("metric") == "efficiency"
                    and x.get("quantile") == "Q4"
                ),
                None,
            )
            if not q1 or not q4 or q1.get("n", 0) < MIN_SAMPLE or q4.get("n", 0) < MIN_SAMPLE:
                continue
            if (q4.get("median_net") or -999) > (q1.get("median_net") or -999):
                h3_ok += 1
            else:
                h3_bad += 1
    if h3_ok + h3_bad == 0:
        out["H3"] = "INSUFFICIENT"
    else:
        out["H3"] = "PASS" if h3_ok > h3_bad else "FAIL"

    # H4: UP+BULL > UP+BEAR and DOWN+BEAR > DOWN+BULL on majority TFs
    h4_ok = h4_bad = 0
    for tf in TRADING_TFS:
        def _get(direction: str, ctx: str) -> dict | None:
            return next(
                (
                    x
                    for x in trend
                    if x.get("timeframe") == tf
                    and x.get("direction") == direction
                    and x.get("context_type") == "ema"
                    and x.get("context") == ctx
                ),
                None,
            )

        ub, u_be = _get("UP", "EMA_BULL"), _get("UP", "EMA_BEAR")
        db, d_bu = _get("DOWN", "EMA_BEAR"), _get("DOWN", "EMA_BULL")
        if not all(
            r and r.get("n", 0) >= MIN_SAMPLE for r in (ub, u_be, db, d_bu)
        ):
            continue
        ok = (ub.get("median_net") or -999) > (u_be.get("median_net") or -999) and (
            db.get("median_net") or -999
        ) > (d_bu.get("median_net") or -999)
        if ok:
            h4_ok += 1
        else:
            h4_bad += 1
    if h4_ok + h4_bad == 0:
        out["H4"] = "INSUFFICIENT"
    else:
        out["H4"] = "PASS" if h4_ok > h4_bad else "FAIL"

    # H5: T0 median_net > next delay and T0 best among delays
    h5_ok = h5_bad = 0
    for tf in TRADING_TFS:
        delays = EDGE_DELAYS_BY_TF[tf]
        rows = [
            x
            for x in decay
            if x.get("timeframe") == tf and x.get("side") == "COMBINED"
        ]
        if not rows:
            continue
        by_d = {int(x["delay_min"]): x for x in rows if x.get("delay_min") is not None}
        if 0 not in by_d or by_d[0].get("sample_flag") != "OK":
            continue
        t0 = by_d[0].get("median_net")
        others = [by_d[d].get("median_net") for d in delays[1:] if d in by_d]
        others = [v for v in others if v is not None]
        if not others or t0 is None:
            continue
        if t0 > max(others) and t0 > (others[0] if others else -999):
            h5_ok += 1
        else:
            h5_bad += 1
    if h5_ok + h5_bad == 0:
        out["H5"] = "INSUFFICIENT"
    else:
        out["H5"] = "PASS" if h5_ok > h5_bad else "FAIL"
    return out


def decide_primary(symbol_status: dict[str, dict[str, Any]]) -> str:
    apt = symbol_status.get("APTUSDT_OOS") or {}
    doge = symbol_status.get("DOGEUSDT") or {}
    btc = symbol_status.get("BTCUSDT") or {}
    apt_insuf = apt.get("coverage_status") == "COVERAGE_INSUFFICIENT"
    doge_h1 = (doge.get("hypotheses") or {}).get("H1")
    btc_h1 = (btc.get("hypotheses") or {}).get("H1")

    if apt_insuf and doge.get("coverage_status") == "COVERAGE_INSUFFICIENT" and btc.get(
        "coverage_status"
    ) == "COVERAGE_INSUFFICIENT":
        return "STOCH_WAVE_FADE_COVERAGE_INSUFFICIENT"

    passes = [x for x in (doge_h1, btc_h1) if x == "PASS"]
    fails = [x for x in (doge_h1, btc_h1) if x == "FAIL"]
    insuf = [x for x in (doge_h1, btc_h1) if x == "INSUFFICIENT"]

    if len(passes) == 2:
        return "STOCH_WAVE_FADE_GENERALIZES"
    if len(passes) == 1 and len(fails) == 1:
        return "STOCH_WAVE_FADE_PARTIALLY_GENERALIZES"
    if len(passes) == 1 and len(insuf) == 1:
        return "STOCH_WAVE_FADE_PARTIALLY_GENERALIZES"
    if len(fails) == 2:
        # APT OOS missing -> cannot claim APT-specific from temporal OOS;
        # if DOGE+BTC fail, overall OOS fails
        return "STOCH_WAVE_FADE_OOS_FAILS"
    if apt_insuf and not passes and insuf:
        return "STOCH_WAVE_FADE_COVERAGE_INSUFFICIENT"
    if not passes and fails:
        return "STOCH_WAVE_FADE_OOS_FAILS"
    return "STOCH_WAVE_FADE_PARTIALLY_GENERALIZES"


def decide_failure_filter(symbol_status: dict[str, dict[str, Any]]) -> str:
    votes = []
    for sym, st in symbol_status.items():
        if st.get("coverage_status") == "COVERAGE_INSUFFICIENT":
            continue
        h2 = (st.get("hypotheses") or {}).get("H2")
        if h2 == "PASS":
            votes.append("hurts_or_not_needed")  # H2 PASS = NON_FAILED >= FAILED
        elif h2 == "FAIL":
            votes.append("adds")  # FAILED better than NON_FAILED
        elif h2 == "INSUFFICIENT":
            votes.append("insuf")
    if not votes or all(v == "insuf" for v in votes):
        return "FAILURE_FILTER_INSUFFICIENT"
    if votes.count("adds") > votes.count("hurts_or_not_needed"):
        return "FAILURE_FILTER_ADDS_VALUE"
    if votes.count("hurts_or_not_needed") > 0 and votes.count("adds") > 0:
        return "FAILURE_FILTER_MIXED"
    # H2 PASS means failure does not improve -> hurts or not needed; map to HURTS if
    # FAILED median clearly worse on majority symbols
    hurts = 0
    for st in symbol_status.values():
        if st.get("coverage_status") == "COVERAGE_INSUFFICIENT":
            continue
        rows = st.get("failure_comparison") or []
        for tf in TRADING_TFS:
            f = next(
                (
                    x
                    for x in rows
                    if x.get("timeframe") == tf
                    and x.get("wave_group") == "FAILED"
                    and x.get("side") == "COMBINED"
                ),
                None,
            )
            nf = next(
                (
                    x
                    for x in rows
                    if x.get("timeframe") == tf
                    and x.get("wave_group") == "NON_FAILED"
                    and x.get("side") == "COMBINED"
                ),
                None,
            )
            if f and nf and f.get("n", 0) >= MIN_SAMPLE and nf.get("n", 0) >= MIN_SAMPLE:
                if (f.get("median_net") or 0) < (nf.get("median_net") or 0):
                    hurts += 1
    if hurts >= 3:
        return "FAILURE_FILTER_HURTS_EDGE"
    return "FAILURE_FILTER_MIXED"


def run_symbol_bundle(
    *,
    label: str,
    symbol: str,
    waves_by_tf: dict[str, pd.DataFrame],
    c1: pd.DataFrame,
    quantile_edges: dict,
    entry_start: pd.Timestamp | None = None,
    entry_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    opens = c1["open"].astype(float).to_numpy()
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")

    coverage_rows = []
    all_wave_results = []
    failure_comparison = []
    direction_results = []
    endzone_results = []
    wave_quality_results = []
    trend_rsi_results = []
    previous_wave_results = []
    edge_decay = []
    pivot_rows = []

    for tf in TRADING_TFS:
        main_h = MAIN_HORIZON_BY_TF[tf]
        raw_w = waves_by_tf.get(tf, pd.DataFrame())
        ann = annotate_waves_df(raw_w, symbol=symbol, timeframe=tf, quantile_edges=quantile_edges)
        n_waves = int(len(ann))

        # coverage candle stats from TF waves timestamps if present
        if n_waves:
            t0 = ann["end_available_at"].min()
            t1 = ann["end_available_at"].max()
        else:
            t0 = t1 = None

        if n_waves == 0:
            coverage_rows.append(
                {
                    "label": label,
                    "symbol": symbol,
                    "timeframe": tf,
                    "n_waves": 0,
                    "n_fade_signals": 0,
                    "status": "COVERAGE_INSUFFICIENT",
                }
            )
            continue

        fwd = attach_forward_with_opens(
            ann,
            high=high,
            low=low,
            close=close,
            opens=opens,
            open_times=open_times,
            horizons=(main_h,),
            delay_min=0,
        )
        fwd = fwd[fwd["entry_valid"]].copy()
        if entry_start is not None:
            fwd = fwd[fwd["entry_time"] >= entry_start]
        if entry_end is not None:
            fwd = fwd[fwd["entry_time"] <= entry_end]

        coverage_rows.append(
            {
                "label": label,
                "symbol": symbol,
                "timeframe": tf,
                "wave_start": str(t0),
                "wave_end": str(t1),
                "n_waves": n_waves,
                "n_fade_signals": int(len(fwd)),
                "entry_start": str(fwd["entry_time"].min()) if len(fwd) else None,
                "entry_end": str(fwd["entry_time"].max()) if len(fwd) else None,
                "status": "OK" if len(fwd) >= MIN_SAMPLE else "COVERAGE_INSUFFICIENT",
            }
        )
        if len(fwd) < MIN_SAMPLE:
            continue

        # ALL / FAILED / NON_FAILED
        for group, mask in (
            ("ALL", pd.Series(True, index=fwd.index)),
            ("FAILED", fwd["is_failed"]),
            ("NON_FAILED", ~fwd["is_failed"]),
        ):
            sub_g = fwd[mask]
            for side_name, sub in _sides(sub_g):
                m = summarize_net(
                    sub,
                    horizon=main_h,
                    label=label,
                    symbol=symbol,
                    timeframe=tf,
                    wave_group=group,
                    side=side_name,
                )
                all_wave_results.append(m)
                failure_comparison.append(m)

        # directions
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for side_name, sub in _sides(sub_d):
                direction_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        side=side_name,
                    )
                )

        # endzone
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for zone, sub in sub_d.groupby(sub_d["stoch_zone_end"].astype(str)):
                endzone_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        stoch_zone_end=zone,
                        side="COMBINED",
                    )
                )
            # extreme paths
            for path in ("HIGH->HIGH", "LOW->LOW"):
                sub = sub_d[sub_d["stoch_path"].astype(str) == path]
                if len(sub) == 0:
                    continue
                endzone_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        stoch_path=path,
                        side="COMBINED",
                    )
                )

        # quality quantiles
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for q, sub in sub_d.groupby(sub_d["eff_quantile"].astype(str)):
                wave_quality_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        metric="efficiency",
                        quantile=q,
                        side="COMBINED",
                    )
                )
            for q, sub in sub_d.groupby(sub_d["size_quantile"].astype(str)):
                wave_quality_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        metric="size",
                        quantile=q,
                        side="COMBINED",
                    )
                )

        # RSI / EMA
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for bucket, sub in sub_d.groupby(sub_d["rsi_bucket"].astype(str)):
                trend_rsi_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        context_type="rsi",
                        context=bucket,
                        side="COMBINED",
                    )
                )
            # hypothesized vs other
            if direction == "UP":
                hyp = sub_d[sub_d["rsi_bucket"] == "gt60"]
                oth = sub_d[sub_d["rsi_bucket"] != "gt60"]
                hyp_name, oth_name = "gt60", "not_gt60"
            else:
                hyp = sub_d[sub_d["rsi_bucket"] == "lt40"]
                oth = sub_d[sub_d["rsi_bucket"] != "lt40"]
                hyp_name, oth_name = "lt40", "not_lt40"
            for name, sub in ((hyp_name, hyp), (oth_name, oth)):
                trend_rsi_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        context_type="rsi_hypothesis",
                        context=name,
                        side="COMBINED",
                    )
                )
            for ctx, sub in sub_d.groupby(sub_d["ema_context"].astype(str)):
                trend_rsi_results.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        direction=direction,
                        context_type="ema",
                        context=ctx,
                        side="COMBINED",
                    )
                )

        # previous wave
        for rel, sub in fwd.groupby(fwd["prev_rel_efficiency"].astype(str)):
            previous_wave_results.append(
                summarize_net(
                    sub,
                    horizon=main_h,
                    label=label,
                    symbol=symbol,
                    timeframe=tf,
                    prev_rel_efficiency=rel,
                    side="COMBINED",
                )
            )

        # edge decay
        for delay in EDGE_DELAYS_BY_TF[tf]:
            if delay == 0:
                delayed = fwd
            else:
                delayed = attach_forward_with_opens(
                    ann,
                    high=high,
                    low=low,
                    close=close,
                    opens=opens,
                    open_times=open_times,
                    horizons=(main_h,),
                    delay_min=delay,
                )
                delayed = delayed[delayed["entry_valid"]].copy()
                if entry_start is not None:
                    delayed = delayed[delayed["entry_time"] >= entry_start]
                if entry_end is not None:
                    delayed = delayed[delayed["entry_time"] <= entry_end]
            for side_name, sub in _sides(delayed):
                edge_decay.append(
                    summarize_net(
                        sub,
                        horizon=main_h,
                        label=label,
                        symbol=symbol,
                        timeframe=tf,
                        side=side_name,
                        delay_min=delay,
                    )
                )

        # pivot utility on ALL T0
        pivot_rows.append(
            pivot_utility_summary(
                fwd,
                high=high,
                low=low,
                close=close,
                open_times=open_times,
                symbol=symbol,
                timeframe=tf,
            )
        )

    bundle = {
        "label": label,
        "symbol": symbol,
        "coverage": coverage_rows,
        "all_wave_results": all_wave_results,
        "failure_comparison": failure_comparison,
        "direction_results": direction_results,
        "endzone_results": endzone_results,
        "wave_quality_results": wave_quality_results,
        "trend_rsi_results": trend_rsi_results,
        "previous_wave_results": previous_wave_results,
        "edge_decay": edge_decay,
        "pivot_utility": pivot_rows,
    }
    # coverage status
    ok_tfs = sum(1 for r in coverage_rows if r.get("status") == "OK")
    bundle["coverage_status"] = "OK" if ok_tfs >= 3 else "COVERAGE_INSUFFICIENT"
    bundle["hypotheses"] = (
        evaluate_hypotheses(bundle) if bundle["coverage_status"] == "OK" else {f"H{i}": "INSUFFICIENT" for i in range(1, 6)}
    )
    return bundle


def run_analysis(out_dir: Path) -> dict[str, Any]:
    print(DEFINITIONS_DOC, flush=True)
    quantile_edges = load_frozen_quantile_edges()
    print(f"[freeze] quantile edge keys={len(quantile_edges)}", flush=True)
    is_end = pd.Timestamp(APT_IS_END)
    cache_root = out_dir / "wave_cache"

    # --- APT OOS coverage check ---
    print("\n===== APTUSDT temporal OOS =====", flush=True)
    apt_cov = symbol_coverage("APTUSDT")
    apt_1m = next(c for c in apt_cov if c["timeframe"] == "1m")
    apt_max = pd.Timestamp(apt_1m["max_open"], tz="UTC")
    oos_days = (apt_max - is_end).total_seconds() / 86400.0
    print(f"[apt-oos] is_end={is_end} data_max={apt_max} oos_days={oos_days:.3f}", flush=True)

    symbol_status: dict[str, dict[str, Any]] = {}
    if oos_days < APT_OOS_MIN_DAYS:
        symbol_status["APTUSDT_OOS"] = {
            "label": "APTUSDT_OOS",
            "symbol": "APTUSDT",
            "coverage_status": "COVERAGE_INSUFFICIENT",
            "coverage_note": (
                f"No usable temporal OOS after frozen IS end {APT_IS_END}; "
                f"only {oos_days:.3f} days of newer candles (need >={APT_OOS_MIN_DAYS})."
            ),
            "coverage": [
                {
                    "label": "APTUSDT_OOS",
                    "symbol": "APTUSDT",
                    "timeframe": c["timeframe"],
                    "candle_start": c["min_open"],
                    "candle_end": c["max_open"],
                    "n_candles": c["n"],
                    "status": "COVERAGE_INSUFFICIENT",
                    "n_waves": None,
                    "n_fade_signals": None,
                }
                for c in apt_cov
            ],
            "hypotheses": {f"H{i}": "INSUFFICIENT" for i in range(1, 6)},
            "all_wave_results": [],
            "failure_comparison": [],
            "direction_results": [],
            "endzone_results": [],
            "wave_quality_results": [],
            "trend_rsi_results": [],
            "previous_wave_results": [],
            "edge_decay": [],
            "pivot_utility": [],
        }
    else:
        # Would run APT OOS if enough data existed
        waves = {tf: pd.read_csv(APT_WAVE_DIR / f"waves_{tf}.csv") for tf in TRADING_TFS}
        c1 = load_1m(symbol="APTUSDT")
        symbol_status["APTUSDT_OOS"] = run_symbol_bundle(
            label="APTUSDT_OOS",
            symbol="APTUSDT",
            waves_by_tf=waves,
            c1=c1,
            quantile_edges=quantile_edges,
            entry_start=is_end,
        )

    # --- DOGE ---
    print("\n===== DOGEUSDT =====", flush=True)
    doge_waves = build_or_load_waves("DOGEUSDT", cache_dir=cache_root / "DOGEUSDT")
    doge_1m = load_1m(symbol="DOGEUSDT")
    # enrich coverage with candle counts
    doge_bundle = run_symbol_bundle(
        label="DOGEUSDT",
        symbol="DOGEUSDT",
        waves_by_tf=doge_waves,
        c1=doge_1m,
        quantile_edges=quantile_edges,
    )
    doge_cov = symbol_coverage("DOGEUSDT")
    for row in doge_bundle["coverage"]:
        tf = row["timeframe"]
        c = next((x for x in doge_cov if x["timeframe"] == tf), None)
        if c:
            row["n_candles"] = c["n"]
            row["candle_start"] = c["min_open"]
            row["candle_end"] = c["max_open"]
        c1m = next((x for x in doge_cov if x["timeframe"] == "1m"), None)
        if c1m and tf == "5m":
            row["n_candles_1m"] = c1m["n"]
    symbol_status["DOGEUSDT"] = doge_bundle

    # --- BTC (limited by 1m coverage) ---
    print("\n===== BTCUSDT =====", flush=True)
    btc_cov = symbol_coverage("BTCUSDT")
    btc_1m_meta = next(c for c in btc_cov if c["timeframe"] == "1m")
    btc_1m_end = pd.Timestamp(btc_1m_meta["max_open"], tz="UTC")
    btc_1m_start = pd.Timestamp(btc_1m_meta["min_open"], tz="UTC")
    print(f"[btc] 1m coverage {btc_1m_start} -> {btc_1m_end}", flush=True)
    btc_waves = build_or_load_waves("BTCUSDT", cache_dir=cache_root / "BTCUSDT")
    btc_1m = load_1m(symbol="BTCUSDT")
    btc_bundle = run_symbol_bundle(
        label="BTCUSDT",
        symbol="BTCUSDT",
        waves_by_tf=btc_waves,
        c1=btc_1m,
        quantile_edges=quantile_edges,
        entry_start=btc_1m_start,
        entry_end=btc_1m_end,
    )
    for row in btc_bundle["coverage"]:
        tf = row["timeframe"]
        c = next((x for x in btc_cov if x["timeframe"] == tf), None)
        if c:
            row["n_candles"] = c["n"]
            row["candle_start"] = c["min_open"]
            row["candle_end"] = c["max_open"]
        row["note"] = f"entries restricted to 1m coverage ending {btc_1m_end.isoformat()}"
    symbol_status["BTCUSDT"] = btc_bundle

    primary = decide_primary(symbol_status)
    fail_dec = decide_failure_filter(symbol_status)
    pivot_all = []
    for st in symbol_status.values():
        pivot_all.extend(st.get("pivot_utility") or [])
    pivot_dec = decide_pivot_utility(pivot_all)

    # cross symbol matrix + hypothesis matrix
    cross_rows = []
    for tf in TRADING_TFS:
        row = {"timeframe": tf}
        pos = 0
        for lab in ("APTUSDT_OOS", "DOGEUSDT", "BTCUSDT"):
            st = symbol_status[lab]
            r = next(
                (
                    x
                    for x in (st.get("all_wave_results") or [])
                    if x.get("timeframe") == tf
                    and x.get("wave_group") == "ALL"
                    and x.get("side") == "COMBINED"
                ),
                None,
            )
            if st.get("coverage_status") == "COVERAGE_INSUFFICIENT" and lab == "APTUSDT_OOS":
                row[lab] = "INSUFFICIENT"
            elif not r:
                row[lab] = "INSUFFICIENT"
            else:
                cell = f"n={r.get('n')}/hit={r.get('hit_rate')}/net={r.get('median_net')}"
                row[lab] = cell
                if (r.get("median_net") or 0) > 0 and (r.get("hit_rate") or 0) >= 0.52:
                    pos += 1
        row["symbols_positive"] = pos
        cross_rows.append(row)

    hyp_rows = []
    for h in ("H1", "H2", "H3", "H4", "H5"):
        apt_h = (symbol_status["APTUSDT_OOS"].get("hypotheses") or {}).get(h, "INSUFFICIENT")
        doge_h = (symbol_status["DOGEUSDT"].get("hypotheses") or {}).get(h, "INSUFFICIENT")
        btc_h = (symbol_status["BTCUSDT"].get("hypotheses") or {}).get(h, "INSUFFICIENT")
        vals = [doge_h, btc_h]
        if vals.count("PASS") == 2:
            overall = "GENERALIZES"
        elif vals.count("PASS") == 1:
            overall = "MIXED"
        elif vals.count("FAIL") == 2:
            overall = "FAILS"
        elif vals.count("INSUFFICIENT") == 2:
            overall = "INSUFFICIENT"
        else:
            overall = "MIXED"
        hyp_rows.append(
            {
                "hypothesis": h,
                "APT_OOS": apt_h,
                "DOGE": doge_h,
                "BTC": btc_h,
                "overall": overall,
            }
        )

    # flatten tables
    def flat(key: str) -> list[dict]:
        rows = []
        for st in symbol_status.values():
            rows.extend(st.get(key) or [])
        return rows

    return {
        "audit_version": AUDIT_VERSION,
        "source_audit": SOURCE_AUDIT,
        "fee_pct": FEE_PCT,
        "apt_is_end": APT_IS_END,
        "definitions": DEFINITIONS_DOC,
        "primary_decision": primary,
        "failure_filter_decision": fail_dec,
        "pivot_utility_decision": pivot_dec,
        "symbol_hypotheses": {
            k: v.get("hypotheses") for k, v in symbol_status.items()
        },
        "coverage": flat("coverage")
        + [
            r
            for r in (symbol_status["APTUSDT_OOS"].get("coverage") or [])
            if r not in flat("coverage")
        ],
        "all_wave_results": flat("all_wave_results"),
        "failure_comparison": flat("failure_comparison"),
        "direction_results": flat("direction_results"),
        "endzone_results": flat("endzone_results"),
        "wave_quality_results": flat("wave_quality_results"),
        "trend_rsi_results": flat("trend_rsi_results"),
        "previous_wave_results": flat("previous_wave_results"),
        "edge_decay": flat("edge_decay"),
        "pivot_utility": flat("pivot_utility"),
        "cross_symbol_summary": cross_rows,
        "hypothesis_summary": hyp_rows,
        "symbol_status": {
            k: {
                "coverage_status": v.get("coverage_status"),
                "coverage_note": v.get("coverage_note"),
                "hypotheses": v.get("hypotheses"),
            }
            for k, v in symbol_status.items()
        },
        "main_horizons": dict(MAIN_HORIZON_BY_TF),
    }
