#!/usr/bin/env python3
"""VALIDATE_LONG_EMA20_EXTENSION_FILTER_OOS — research-only, no DB writes.

Exact methodological mirror of validate_short_ema20_extension_oos.py:
same universe/window/split/fees/bootstrap; LONG feature =
  dist_ema20_atr_long = (ema20 - entry_price) / atr
(positive = price below EMA20).
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.live.control_api import _signal_row_to_api  # noqa: E402
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.research.one_m_entry_timing.timing import (  # noqa: E402
    _prepare_1m,
    _simulate_outcome,
    _utc,
)
from signal_generator.strategy.wave_fade.adapter import bars_to_ohlcv_df  # noqa: E402
from signal_generator.strategy.wave_fade.indicators import attach_indicators  # noqa: E402
from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE  # noqa: E402
from signal_generator.timeframes import (  # noqa: E402
    aggregate_1m_to_timeframe,
    bars_from_mappings,
)

OUT = ROOT / "results" / "long_ema20_extension_oos_validation"
SHORT_SUMMARY = ROOT / "results" / "short_ema20_extension_oos_validation" / "summary.json"
# Identical lock as SHORT audit
AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
LOOKBACK_DAYS = 60
SIGNAL_TF = "15m"
FEE_PCT = float(PRIMARY_FEE)
ATR_LEN = 14
FIXED_THRESHOLDS = (1.0, 1.5, 2.0, 2.5)
TRAIN_FRAC = 0.60
SYMBOLS_FOCUS = ("APTUSDT", "DOGEUSDT")
N_BOOT = 2000
BOOT_SEED = 42

LOOKAHEAD_VIOLATIONS = 0
THRESHOLD_LEAKAGE = 0
OUTCOME_LEAKAGE = 0
TRAIN_OOS_CONTAMINATION = 0
FEATURE_NOT_AVAILABLE = 0


def _iso(ts: Any | None) -> str | None:
    if ts is None:
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = ATR_LEN) -> pd.Series:
    prev_c = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_c).abs(), (low - prev_c).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def prepare_15m(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = attach_indicators(df)
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    out["atr"] = wilder_atr(high, low, close, ATR_LEN)
    return out


def causal_idx(df: pd.DataFrame, entry_ts: datetime) -> int:
    if df.empty or "available_at" not in df.columns:
        return -1
    t = pd.Timestamp(_utc(entry_ts))
    known = df.index[df["available_at"] <= t]
    if len(known) == 0:
        return -1
    return int(known[-1])


def check_causal(available_at: Any, entry_ts: datetime) -> None:
    global LOOKAHEAD_VIOLATIONS
    if available_at is None or (isinstance(available_at, float) and np.isnan(available_at)):
        return
    if _utc(available_at) > _utc(entry_ts):
        LOOKAHEAD_VIOLATIONS += 1


def long_extension_at(
    df: pd.DataFrame, entry_ts: datetime, entry_price: float | None
) -> dict[str, Any]:
    """Causal LONG extension: (ema20 - entry_price) / atr.

    ema20/atr from last COMPLETE 15m bar with available_at <= entry_ts.
    """
    global FEATURE_NOT_AVAILABLE
    i = causal_idx(df, entry_ts)
    if i < 0:
        FEATURE_NOT_AVAILABLE += 1
        return {
            "dist_ema20_atr_long": None,
            "dist_ema20_atr_long_close_mirror": None,
            "source_candle_open": None,
            "available_at": None,
            "ema20": None,
            "atr": None,
            "bar_close": None,
        }
    row = df.iloc[i]
    check_causal(row["available_at"], entry_ts)
    close = safe_float(row["close"])
    ema20 = safe_float(row["ema20"])
    atr = safe_float(row["atr"])
    if ema20 is None or atr is None or atr <= 0 or entry_price is None or entry_price <= 0:
        FEATURE_NOT_AVAILABLE += 1
        return {
            "dist_ema20_atr_long": None,
            "dist_ema20_atr_long_close_mirror": (ema20 - close) / atr
            if ema20 is not None and close is not None and atr and atr > 0
            else None,
            "source_candle_open": _iso(row["timestamp"]),
            "available_at": _iso(row["available_at"]),
            "ema20": ema20,
            "atr": atr,
            "bar_close": close,
        }
    return {
        "dist_ema20_atr_long": (ema20 - float(entry_price)) / atr,
        "dist_ema20_atr_long_close_mirror": (ema20 - close) / atr if close is not None else None,
        "source_candle_open": _iso(row["timestamp"]),
        "available_at": _iso(row["available_at"]),
        "ema20": ema20,
        "atr": atr,
        "bar_close": close,
    }


def summarize(
    rows: list[dict[str, Any]],
    *,
    label: str,
    blocked_ids: set[str] | None = None,
) -> dict[str, Any]:
    use = [r for r in rows if blocked_ids is None or r["signal_id"] not in blocked_ids]
    wins = [r for r in use if r["result"] == "WIN"]
    losses = [r for r in use if r["result"] == "LOSS"]
    opens = [r for r in use if r["result"] == "OPEN"]
    closed = wins + losses
    pnls = [float(r["pnl"]) for r in use if r.get("pnl") is not None]
    maes = [float(r["mae"]) for r in use if r.get("mae") is not None]
    mfes = [float(r["mfe"]) for r in use if r.get("mfe") is not None]
    gp = sum(p for p in pnls if p > 0)
    gl = sum(-p for p in pnls if p < 0)
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float("inf"))
    return {
        "variant": label,
        "trades": len(use),
        "wins": len(wins),
        "losses": len(losses),
        "open": len(opens),
        "winrate": (100.0 * len(wins) / len(closed)) if closed else None,
        "net": float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0,
        "gross": float(sum(pnls)) if pnls else 0.0,
        "profit_factor": pf,
        "sl_rate": (100.0 * len(losses) / len(closed)) if closed else None,
        "tp_rate": (100.0 * len(wins) / len(closed)) if closed else None,
        "median_MAE": float(np.median(maes)) if maes else None,
        "median_MFE": float(np.median(mfes)) if mfes else None,
        "net_per_trade": (float(sum(pnls) / len(pnls)) if pnls else None),
    }


def removed_stats(rows: list[dict[str, Any]], blocked_ids: set[str]) -> dict[str, Any]:
    blocked = [r for r in rows if r["signal_id"] in blocked_ids]
    return {
        "winners_removed": sum(1 for r in blocked if r["result"] == "WIN"),
        "losers_removed": sum(1 for r in blocked if r["result"] == "LOSS"),
        "open_removed": sum(1 for r in blocked if r["result"] == "OPEN"),
        "n_blocked": len(blocked),
        "blocked_wr": (
            100.0
            * sum(1 for r in blocked if r["result"] == "WIN")
            / max(1, sum(1 for r in blocked if r["result"] in ("WIN", "LOSS")))
            if any(r["result"] in ("WIN", "LOSS") for r in blocked)
            else None
        ),
        "blocked_net": float(
            sum(float(r["pnl"]) for r in blocked if r.get("pnl") is not None)
            - FEE_PCT * sum(1 for r in blocked if r.get("pnl") is not None)
        ),
    }


def quantile_table(longs: list[dict[str, Any]], *, split: str) -> list[dict[str, Any]]:
    vals = [(safe_float(r["dist_ema20_atr_long"]), r) for r in longs]
    vals = [(v, r) for v, r in vals if v is not None]
    if len(vals) < 10:
        return []
    vs = np.array([v for v, _ in vals], dtype=float)
    edges = np.unique(np.percentile(vs, [0, 20, 40, 60, 80, 100]))
    out = []
    for qi in range(len(edges) - 1):
        lo, hi = float(edges[qi]), float(edges[qi + 1])
        if qi < len(edges) - 2:
            bucket = [r for v, r in vals if lo <= v < hi]
        else:
            bucket = [r for v, r in vals if lo <= v <= hi]
        s = summarize(bucket, label=f"{split}_Q{qi+1}")
        med_ext = float(np.median([v for v, r in vals if r in bucket])) if bucket else None
        # fix med_ext properly
        bvals = [safe_float(r["dist_ema20_atr_long"]) for r in bucket]
        bvals = [v for v in bvals if v is not None]
        out.append(
            {
                "split": split,
                "quantile": f"Q{qi+1}",
                "lo": lo,
                "hi": hi,
                "count": s["trades"],
                "median_extension": float(np.median(bvals)) if bvals else None,
                "winrate": s["winrate"],
                "net": s["net"],
                "net_per_trade": s["net_per_trade"],
                "sl_rate": s["sl_rate"],
                "median_MAE": s["median_MAE"],
            }
        )
    return out


def main() -> int:
    global LOOKAHEAD_VIOLATIONS, THRESHOLD_LEAKAGE, OUTCOME_LEAKAGE, TRAIN_OOS_CONTAMINATION
    LOOKAHEAD_VIOLATIONS = 0
    THRESHOLD_LEAKAGE = 0
    OUTCOME_LEAKAGE = 0
    TRAIN_OOS_CONTAMINATION = 0
    OUT.mkdir(parents=True, exist_ok=True)

    now = AS_OF
    start = now - timedelta(days=LOOKBACK_DAYS)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading 15m Tier-A {start.date()} → {_iso(now)} …", flush=True)
    rows, total = sig_repo.query_signals(
        start=start,
        end=now,
        tier_a=True,
        timeframe=SIGNAL_TF,
        time_field="candle_close_time",
        limit=5000,
        offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    api_rows = [r for r in api_rows if str(r.get("timeframe")) == SIGNAL_TF]
    api_rows.sort(key=lambda r: _utc(r.get("entry_time") or r.get("candle_close_time")))
    print(f"signals={len(api_rows)} (query total={total})", flush=True)

    by_sym: dict[str, list] = defaultdict(list)
    for r in api_rows:
        by_sym[str(r["symbol"]).upper()].append(r)

    pad_before = timedelta(days=10)
    pad_after = timedelta(hours=36)
    work_1m: dict[str, pd.DataFrame] = {}
    tf15: dict[str, pd.DataFrame] = {}

    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, b) or []
        work_1m[sym] = _prepare_1m(pd.DataFrame(raw)) if raw else pd.DataFrame()
        bars = bars_from_mappings(raw)
        htf = aggregate_1m_to_timeframe(bars, SIGNAL_TF, as_of=now, require_complete=True)
        tf15[sym] = prepare_15m(bars_to_ohlcv_df(htf))
        print(f"  {sym}: 1m={len(work_1m[sym])} 15m={len(tf15[sym])}", flush=True)

    details: list[dict[str, Any]] = []
    for r in api_rows:
        sym = str(r["symbol"]).upper()
        side = str(r["direction"]).upper()
        sid = str(r["signal_id"])
        signal_ts = _utc(r.get("candle_close_time") or r.get("generated_at"))
        t0 = _utc(r.get("entry_time") or signal_ts)
        try:
            entry_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            entry_px = None

        work = work_1m[sym]
        df = tf15[sym]
        entry_i = -1
        if not work.empty:
            entry_i = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))
            if entry_i >= len(work):
                entry_i = -1
        if entry_i >= 0 and entry_px is None:
            entry_px = float(work.iloc[entry_i]["open"])

        result, pnl, mae, mfe, exit_reason = "OPEN", None, None, None, None
        if entry_i >= 0 and entry_px is not None and not work.empty:
            sim = _simulate_outcome(
                side=side, entry=float(entry_px), tf=SIGNAL_TF, entry_i=entry_i, work=work, as_of=now
            )
            result = sim["result"]
            pnl = sim.get("pnl_pct")
            mae = sim.get("mae_pct")
            mfe = sim.get("mfe_pct")
            exit_reason = sim.get("exit_reason")

        feat = long_extension_at(df, t0, entry_px)
        details.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "direction": side,
                "signal_tf": SIGNAL_TF,
                "signal_ts": _iso(signal_ts),
                "entry_ts": _iso(t0),
                "entry_price": entry_px,
                **feat,
                "result": result,
                "pnl": pnl,
                "mae": mae,
                "mfe": mfe,
                "exit_reason": exit_reason,
            }
        )

    # Identical chronological split as SHORT (60% of ALL signals)
    n = len(details)
    n_train = int(n * TRAIN_FRAC)
    if n_train < 30 or (n - n_train) < 20:
        n_train = n // 2
    train = details[:n_train]
    oos = details[n_train:]
    for d in train:
        d["split"] = "TRAIN"
    for d in oos:
        d["split"] = "OOS"

    # Contamination check: overlapping signal_ids
    train_ids = {d["signal_id"] for d in train}
    oos_ids = {d["signal_id"] for d in oos}
    TRAIN_OOS_CONTAMINATION = len(train_ids & oos_ids)

    train_long = [
        d for d in train if d["direction"] == "LONG" and d.get("dist_ema20_atr_long") is not None
    ]
    if len(train_long) < 15:
        msg = {"error": "INSUFFICIENT_TRAIN_LONG", "n_train_long": len(train_long)}
        (OUT / "STOP_insufficient.json").write_text(json.dumps(msg, indent=2) + "\n")
        print("STOP", msg, flush=True)
        return 2

    train_dists = [float(d["dist_ema20_atr_long"]) for d in train_long]
    p80_train = float(np.percentile(train_dists, 80))
    threshold_source = "TRAIN_ONLY"
    # Explicit: OOS never used for threshold
    THRESHOLD_LEAKAGE = 0
    OUTCOME_LEAKAGE = 0  # threshold from feature only, never pnl/result

    oos_long_feat = [
        d for d in oos if d["direction"] == "LONG" and d.get("dist_ema20_atr_long") is not None
    ]

    meta = {
        "task": "VALIDATE_LONG_EMA20_EXTENSION_FILTER_OOS",
        "feature": "dist_ema20_atr_long = (ema20 - entry_price) / atr",
        "feature_note": "positive => price below EMA20; ema20/atr from last closed 15m bar available_at<=entry",
        "rule": "BLOCK LONG if dist_ema20_atr_long > LONG_BLOCK_THRESHOLD",
        "LONG_BLOCK_THRESHOLD": p80_train,
        "percentile": 80,
        "threshold_source": threshold_source,
        "n_total": n,
        "n_long_total": sum(1 for d in details if d["direction"] == "LONG"),
        "n_train_total": len(train),
        "n_oos_total": len(oos),
        "n_train_long_with_feature": len(train_long),
        "n_oos_long_with_feature": len(oos_long_feat),
        "train_long_dist_min": float(np.min(train_dists)),
        "train_long_dist_median": float(np.median(train_dists)),
        "train_long_dist_max": float(np.max(train_dists)),
        "as_of": _iso(now),
        "train_first_entry": train[0]["entry_ts"] if train else None,
        "train_last_entry": train[-1]["entry_ts"] if train else None,
        "oos_first_entry": oos[0]["entry_ts"] if oos else None,
        "oos_last_entry": oos[-1]["entry_ts"] if oos else None,
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "threshold_leakage": THRESHOLD_LEAKAGE,
        "outcome_leakage": OUTCOME_LEAKAGE,
        "train_oos_contamination": TRAIN_OOS_CONTAMINATION,
        "feature_not_available_count": FEATURE_NOT_AVAILABLE,
        "mirrors_short_audit": {
            "as_of": _iso(AS_OF),
            "train_frac": TRAIN_FRAC,
            "n_boot": N_BOOT,
            "boot_seed": BOOT_SEED,
            "fee_pct": FEE_PCT,
            "fixed_thresholds": list(FIXED_THRESHOLDS),
        },
    }
    (OUT / "audit_metadata.json").write_text(json.dumps(meta, indent=2) + "\n")

    def block_ids(rows: list[dict], thr: float) -> set[str]:
        return {
            d["signal_id"]
            for d in rows
            if d["direction"] == "LONG"
            and d.get("dist_ema20_atr_long") is not None
            and float(d["dist_ema20_atr_long"]) > thr
        }

    frozen_blocked = block_ids(oos, p80_train)
    for d in details:
        d["frozen_p80_threshold"] = p80_train
        d["blocked_frozen_p80"] = d["signal_id"] in frozen_blocked

    # --- OOS main (all OOS; only LONGs blocked) ---
    base_oos = summarize(oos, label="BASELINE_IMMEDIATE")
    filt_oos = summarize(oos, label="BLOCK_LONG_DIST_EMA20_ATR_GT_P80_TRAIN", blocked_ids=frozen_blocked)
    rem = removed_stats(oos, frozen_blocked)
    delta_net = filt_oos["net"] - base_oos["net"]

    oos_main = [
        {**base_oos, "delta_net": 0.0, "winners_removed": 0, "losers_removed": 0, "n_blocked": 0},
        {
            **filt_oos,
            "delta_net": delta_net,
            "winners_removed": rem["winners_removed"],
            "losers_removed": rem["losers_removed"],
            "n_blocked": rem["n_blocked"],
        },
    ]
    write_csv(OUT / "oos_main_comparison.csv", oos_main)

    # LONG-only blocked vs kept
    oos_long = [r for r in oos if r["direction"] == "LONG"]
    long_blocked_ids = {r["signal_id"] for r in oos_long if r["signal_id"] in frozen_blocked}
    long_base = summarize(oos_long, label="LONG_BASELINE")
    long_filt = summarize(oos_long, label="LONG_FILTERED", blocked_ids=long_blocked_ids)
    long_rem = removed_stats(oos_long, long_blocked_ids)
    long_blk = [r for r in oos_long if r["signal_id"] in long_blocked_ids]
    long_kept = [r for r in oos_long if r["signal_id"] not in long_blocked_ids]
    blocked_vs_kept = [
        {**long_base, "group": "LONG_BASELINE"},
        {**long_filt, "group": "LONG_FILTERED", "delta_net": long_filt["net"] - long_base["net"],
         "winners_removed": long_rem["winners_removed"], "losers_removed": long_rem["losers_removed"]},
        {**summarize(long_blk, label="LONG_BLOCKED"), "group": "BLOCKED"},
        {**summarize(long_kept, label="LONG_KEPT"), "group": "KEPT"},
    ]
    write_csv(OUT / "blocked_vs_kept.csv", blocked_vs_kept)

    # Bootstrap identical to SHORT
    rng = np.random.default_rng(BOOT_SEED)
    oos_closed = [r for r in oos if r["result"] in ("WIN", "LOSS") and r.get("pnl") is not None]
    deltas = []
    if len(oos_closed) >= 20:
        idx = np.arange(len(oos_closed))
        for _ in range(N_BOOT):
            samp = rng.choice(idx, size=len(idx), replace=True)
            base_net = 0.0
            filt_net = 0.0
            for i in samp:
                r = oos_closed[i]
                p = float(r["pnl"])
                base_net += p - FEE_PCT
                if r["signal_id"] not in frozen_blocked:
                    filt_net += p - FEE_PCT
            deltas.append(filt_net - base_net)
    delta_ci = (
        (float(np.percentile(deltas, 2.5)), float(np.percentile(deltas, 97.5))) if deltas else (None, None)
    )
    b_wins = sum(1 for r in long_blk if r["result"] == "WIN")
    b_closed = sum(1 for r in long_blk if r["result"] in ("WIN", "LOSS"))
    # wilson optional
    boot_payload = {
        "n_boot": N_BOOT,
        "seed": BOOT_SEED,
        "delta_net_point": delta_net,
        "delta_net_boot_ci_lo": delta_ci[0],
        "delta_net_boot_ci_hi": delta_ci[1],
        "ci_crosses_zero": bool(
            delta_ci[0] is not None and delta_ci[1] is not None and delta_ci[0] <= 0 <= delta_ci[1]
        ),
        "n_oos_closed_resampled": len(oos_closed),
        "blocked_closed": b_closed,
        "blocked_wins": b_wins,
    }
    (OUT / "bootstrap_results.json").write_text(json.dumps(boot_payload, indent=2) + "\n")

    # Fixed thresholds
    fixed_rows = []
    for thr in FIXED_THRESHOLDS:
        ids = block_ids(oos, thr)
        filt = summarize(oos, label=f"BLOCK_LONG_DIST_GT_{thr}", blocked_ids=ids)
        rem_f = removed_stats(oos, ids)
        fixed_rows.append(
            {
                "threshold": thr,
                "blocked_count": rem_f["n_blocked"],
                "winners_blocked": rem_f["winners_removed"],
                "losers_blocked": rem_f["losers_removed"],
                "trades_kept": filt["trades"],
                "winrate": filt["winrate"],
                "net": filt["net"],
                "profit_factor": filt["profit_factor"],
                "delta_net": filt["net"] - base_oos["net"],
                "sl_rate": filt["sl_rate"],
            }
        )
    write_csv(OUT / "fixed_thresholds.csv", fixed_rows)

    # Quantiles
    train_q = quantile_table(train_long, split="TRAIN")
    oos_q = quantile_table(oos_long_feat, split="OOS")
    write_csv(OUT / "train_quantiles.csv", train_q)
    write_csv(OUT / "oos_quantiles.csv", oos_q)

    q_pattern_ok = False
    q_pattern_flip = False
    q_monotonic = False
    if len(oos_q) >= 5:
        q1, q5 = oos_q[0], oos_q[-1]
        wrs = [r["winrate"] for r in oos_q if r.get("winrate") is not None]
        if q1.get("winrate") is not None and q5.get("winrate") is not None:
            if (q5["winrate"] or 0) + 5 < (q1["winrate"] or 0) and (q5.get("net_per_trade") or 0) < (
                q1.get("net_per_trade") or 0
            ):
                q_pattern_ok = True
            if (q5["winrate"] or 0) > (q1["winrate"] or 0) + 5:
                q_pattern_flip = True
        if len(wrs) >= 4:
            # weak monotonic: each step not increasing by >2pp toward Q5 worse
            q_monotonic = all(wrs[i] + 2.0 >= wrs[i + 1] for i in range(len(wrs) - 1))

    # OOS halves — identical mid split on OOS list
    mid = len(oos) // 2
    oos_a, oos_b = oos[:mid], oos[mid:]
    time_rows = []
    for name, part in (("OOS_A", oos_a), ("OOS_B", oos_b)):
        ids = block_ids(part, p80_train)
        b = summarize(part, label=f"{name}_BASELINE")
        f = summarize(part, label=f"{name}_FILTERED", blocked_ids=ids)
        rm = removed_stats(part, ids)
        time_rows.append(
            {
                "part": name,
                "n": len(part),
                "baseline_net": b["net"],
                "filtered_net": f["net"],
                "delta_net": f["net"] - b["net"],
                "baseline_wr": b["winrate"],
                "filtered_wr": f["winrate"],
                "winners_removed": rm["winners_removed"],
                "losers_removed": rm["losers_removed"],
                "n_blocked": rm["n_blocked"],
                "first_entry": part[0]["entry_ts"] if part else None,
                "last_entry": part[-1]["entry_ts"] if part else None,
            }
        )
    write_csv(OUT / "oos_halves.csv", time_rows)

    time_unstable = False
    if len(time_rows) == 2:
        d0, d1 = time_rows[0]["delta_net"], time_rows[1]["delta_net"]
        if d0 * d1 < 0 and (abs(d0) > 0.5 or abs(d1) > 0.5):
            time_unstable = True
        if max(d0, d1) > 1 and min(d0, d1) < -1:
            time_unstable = True

    # Symbols
    sym_rows = []
    for sym in SYMBOLS_FOCUS:
        part = [r for r in oos if r["symbol"] == sym]
        part_long = [r for r in part if r["direction"] == "LONG"]
        ids = block_ids(part, p80_train)
        b = summarize(part, label=f"{sym}_BASE")
        f = summarize(part, label=f"{sym}_FILT", blocked_ids=ids)
        rm = removed_stats(part, ids)
        lb = summarize(part_long, label=f"{sym}_LONG_BASE")
        flag = "INSUFFICIENT_SYMBOL_SAMPLE" if len(part_long) < 8 or rm["n_blocked"] < 5 else "OK"
        sym_rows.append(
            {
                "symbol": sym,
                "oos_trades": len(part),
                "oos_long": len(part_long),
                "baseline_net": b["net"],
                "filtered_net": f["net"],
                "delta_net": f["net"] - b["net"],
                "long_baseline_wr": lb["winrate"],
                "long_baseline_net": lb["net"],
                "winners_removed": rm["winners_removed"],
                "losers_removed": rm["losers_removed"],
                "n_blocked": rm["n_blocked"],
                "sample_flag": flag,
            }
        )
    write_csv(OUT / "symbol_breakdown.csv", sym_rows)

    # Blocked detail
    blocked_rows = [r for r in oos if r["signal_id"] in frozen_blocked]
    write_csv(
        OUT / "blocked_trades.csv",
        [
            {
                "signal_id": r["signal_id"],
                "symbol": r["symbol"],
                "entry_ts": r["entry_ts"],
                "dist_ema20_atr_long": r["dist_ema20_atr_long"],
                "threshold": p80_train,
                "baseline_outcome": r["result"],
                "baseline_net": (float(r["pnl"]) - FEE_PCT) if r.get("pnl") is not None else None,
                "baseline_mae": r["mae"],
                "baseline_mfe": r["mfe"],
                "blocked": True,
            }
            for r in blocked_rows
        ],
    )

    n_blocked = rem["n_blocked"]
    if n_blocked < 20:
        sample_warn = "VERY_SMALL_BLOCKED_SAMPLE"
    elif n_blocked < 50:
        sample_warn = "SMALL_BLOCKED_SAMPLE"
    else:
        sample_warn = "OK"

    blk_sum = summarize(long_blk, label="x")
    kept_sum = summarize(long_kept, label="y")
    blk_worse = False
    if long_blk and long_kept:
        if (blk_sum["winrate"] or 100) + 5 < (kept_sum["winrate"] or 0) or (
            blk_sum["net_per_trade"] or 0
        ) < (kept_sum["net_per_trade"] or 0) - 0.05:
            blk_worse = True

    # LONG primary decision (mirror SHORT logic, LONG-labeled)
    if n_blocked < 10:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "KEEP_BASELINE"
    elif time_unstable:
        primary = "LONG_EMA20_EXTENSION_FILTER_TIME_UNSTABLE"
        recommendation = "KEEP_BASELINE"
    elif q_pattern_flip and delta_net <= 0:
        primary = "LONG_EMA20_EXTENSION_FILTER_NOT_ROBUST"
        recommendation = "KEEP_BASELINE"
    elif (
        n_blocked >= 20
        and delta_net > 2
        and rem["losers_removed"] > rem["winners_removed"]
        and blk_worse
        and not time_unstable
        and (q_pattern_ok or delta_net > 4)
        and delta_ci[0] is not None
        and delta_ci[0] > 0
    ):
        primary = "LONG_EMA20_EXTENSION_FILTER_OOS_CONFIRMED"
        recommendation = "LONG_EXTENSION_FILTER_WORTH_NEXT_STAGE"
    elif (
        delta_net > 0
        and rem["losers_removed"] >= rem["winners_removed"]
        and blk_worse
        and not q_pattern_flip
        and not time_unstable
    ):
        primary = "LONG_EMA20_EXTENSION_FILTER_WEAK_BUT_PROMISING"
        recommendation = "LONG_EXTENSION_FILTER_WORTH_NEXT_STAGE"
    elif delta_net <= 0 or rem["winners_removed"] > rem["losers_removed"] + 2 or not blk_worse:
        primary = "LONG_EMA20_EXTENSION_FILTER_NOT_ROBUST"
        recommendation = "KEEP_BASELINE"
    else:
        primary = "LONG_EMA20_EXTENSION_FILTER_NOT_ROBUST"
        recommendation = "KEEP_BASELINE"

    # SHORT vs LONG comparison classification
    short = {}
    if SHORT_SUMMARY.exists():
        short = json.loads(SHORT_SUMMARY.read_text())
    short_delta = short.get("delta_net_oos")
    short_blocked_wr = (short.get("removed") or {}).get("blocked_wr")
    short_promising = short.get("primary_decision") in (
        "SHORT_EMA20_EXTENSION_FILTER_OOS_CONFIRMED",
        "SHORT_EMA20_EXTENSION_FILTER_WEAK_BUT_PROMISING",
    )
    long_promising = primary in (
        "LONG_EMA20_EXTENSION_FILTER_OOS_CONFIRMED",
        "LONG_EMA20_EXTENSION_FILTER_WEAK_BUT_PROMISING",
    )
    long_clear_fail = primary in (
        "LONG_EMA20_EXTENSION_FILTER_NOT_ROBUST",
        "LONG_EMA20_EXTENSION_FILTER_TIME_UNSTABLE",
        "INSUFFICIENT_SAMPLE",
    ) and (delta_net <= 0 or not blk_worse or q_pattern_flip)

    if short_promising and long_promising and q_pattern_ok:
        symmetry = "EMA20_EXTENSION_IS_SYMMETRIC_GENERAL_FILTER"
    elif short_promising and (not long_promising):
        symmetry = "EMA20_EXTENSION_IS_SHORT_SPECIFIC"
    elif long_promising and not short_promising:
        symmetry = "EMA20_EXTENSION_IS_LONG_SPECIFIC"
    else:
        symmetry = "EMA20_EXTENSION_EFFECT_WEAK_OR_UNSTABLE"

    # If LONG clearly fails while SHORT was weak-but-promising → SHORT_SPECIFIC
    if short_promising and long_clear_fail:
        symmetry = "EMA20_EXTENSION_IS_SHORT_SPECIFIC"

    short_oos = short.get("oos_baseline") or {}
    short_filt = short.get("oos_filtered") or {}
    short_rem = short.get("removed") or {}
    short_only = short.get("short_only") or []
    short_blk_row = next((r for r in short_only if r.get("variant") == "SHORT_BLOCKED_GROUP"), {})
    short_boot = short.get("delta_net_bootstrap_ci") or [None, None]

    comparison = [
        {
            "metric": "OOS_n_all",
            "SHORT": short.get("dataset", {}).get("oos"),
            "LONG": len(oos),
        },
        {
            "metric": "OOS_side_n",
            "SHORT": next((r.get("trades") for r in short_only if r.get("variant") == "SHORT_BASELINE"), None),
            "LONG": long_base["trades"],
        },
        {"metric": "blocked_n", "SHORT": short_rem.get("n_blocked"), "LONG": n_blocked},
        {"metric": "TRAIN_P80_ATR", "SHORT": short.get("frozen_threshold"), "LONG": p80_train},
        {"metric": "baseline_WR_all_oos", "SHORT": short_oos.get("winrate"), "LONG": base_oos["winrate"]},
        {"metric": "filtered_WR_all_oos", "SHORT": short_filt.get("winrate"), "LONG": filt_oos["winrate"]},
        {"metric": "baseline_Net_all_oos", "SHORT": short_oos.get("net"), "LONG": base_oos["net"]},
        {"metric": "filtered_Net_all_oos", "SHORT": short_filt.get("net"), "LONG": filt_oos["net"]},
        {"metric": "delta_Net", "SHORT": short_delta, "LONG": delta_net},
        {"metric": "blocked_WR", "SHORT": short_blocked_wr, "LONG": blk_sum["winrate"]},
        {"metric": "blocked_Net", "SHORT": short_blk_row.get("net"), "LONG": blk_sum["net"]},
        {
            "metric": "bootstrap_CI",
            "SHORT": str(short_boot),
            "LONG": str([delta_ci[0], delta_ci[1]]),
        },
        {"metric": "symmetry_class", "SHORT": short.get("primary_decision"), "LONG": primary},
        {"metric": "classification", "SHORT": symmetry, "LONG": symmetry},
    ]
    write_csv(OUT / "short_vs_long_comparison.csv", comparison)

    # TRAIN vs OOS consistency note
    train_q5_wr = next((r["winrate"] for r in train_q if r["quantile"] == "Q5"), None)
    train_q1_wr = next((r["winrate"] for r in train_q if r["quantile"] == "Q1"), None)
    oos_q5_wr = next((r["winrate"] for r in oos_q if r["quantile"] == "Q5"), None)
    oos_q1_wr = next((r["winrate"] for r in oos_q if r["quantile"] == "Q1"), None)
    train_had_pattern = (
        train_q1_wr is not None
        and train_q5_wr is not None
        and train_q5_wr + 5 < train_q1_wr
    )
    oos_replicates = q_pattern_ok and train_had_pattern
    oos_only_pattern = q_pattern_ok and not train_had_pattern

    payload = {
        "primary_decision": primary,
        "recommendation": recommendation,
        "symmetry_classification": symmetry,
        "sample_warning": sample_warn,
        "dataset": {
            "total_signals": n,
            "long_signals": sum(1 for d in details if d["direction"] == "LONG"),
            "train": len(train),
            "oos": len(oos),
            "n_train_long": len(train_long),
            "n_oos_long": len(oos_long),
            "train_frac": TRAIN_FRAC,
            "date_range": {
                "first": details[0]["entry_ts"] if details else None,
                "last": details[-1]["entry_ts"] if details else None,
            },
        },
        "frozen_threshold": p80_train,
        "threshold_source": threshold_source,
        "threshold_leakage": THRESHOLD_LEAKAGE,
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "outcome_leakage": OUTCOME_LEAKAGE,
        "train_oos_contamination": TRAIN_OOS_CONTAMINATION,
        "feature_available_violations": FEATURE_NOT_AVAILABLE,
        "oos_baseline": base_oos,
        "oos_filtered": filt_oos,
        "delta_net_oos": delta_net,
        "removed": rem,
        "blocked_vs_kept": blocked_vs_kept,
        "bootstrap": boot_payload,
        "fixed_thresholds": fixed_rows,
        "oos_quantiles": oos_q,
        "train_quantiles": train_q,
        "q_pattern_oos_ok": q_pattern_ok,
        "q_pattern_oos_flip": q_pattern_flip,
        "q_monotonic_decreasing_wr": q_monotonic,
        "train_had_q_pattern": train_had_pattern,
        "oos_replicates_train": oos_replicates,
        "oos_only_pattern": oos_only_pattern,
        "time_stability": time_rows,
        "time_unstable": time_unstable,
        "symbol_breakdown": sym_rows,
        "blocked_sample_size": n_blocked,
        "blocked_group_worse_than_kept": blk_worse,
        "short_vs_long": comparison,
        "as_of": _iso(now),
        "fee_pct": FEE_PCT,
    }
    (OUT / "summary.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    def fmt(v: Any, nd: int = 1) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if abs(v) == float("inf"):
                return "inf"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# VALIDATE_LONG_EMA20_EXTENSION_FILTER_OOS",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"SHORT vs LONG class: `{symmetry}`",
        f"Sample warning: `{sample_warn}` (n_blocked={n_blocked})",
        "",
        f"Feature: `dist_ema20_atr_long = (ema20 - entry_price) / atr`",
        f"threshold_source=`{threshold_source}` | P80=**{fmt(p80_train, 4)}**",
        f"threshold_leakage={THRESHOLD_LEAKAGE} | lookahead={LOOKAHEAD_VIOLATIONS} | "
        f"outcome_leakage={OUTCOME_LEAKAGE} | train_oos_contamination={TRAIN_OOS_CONTAMINATION}",
        "",
        f"Dataset: total={n} LONG={sum(1 for d in details if d['direction']=='LONG')} "
        f"TRAIN={len(train)} (LONG feat={len(train_long)}) OOS={len(oos)} (LONG={len(oos_long)})",
        "",
        "## OOS main (all TF signals; only LONGs blocked)",
        "",
        "| Variant | Trades | WR | Net | PF | SL | Med MAE |",
        "| ------- | -----: | -: | --: | -: | -: | ------: |",
        f"| BASELINE | {base_oos['trades']} | {fmt(base_oos['winrate'])} | {fmt(base_oos['net'])} | "
        f"{fmt(base_oos['profit_factor'])} | {fmt(base_oos['sl_rate'])} | {fmt(base_oos['median_MAE'], 3)} |",
        f"| FILTERED | {filt_oos['trades']} | {fmt(filt_oos['winrate'])} | {fmt(filt_oos['net'])} | "
        f"{fmt(filt_oos['profit_factor'])} | {fmt(filt_oos['sl_rate'])} | {fmt(filt_oos['median_MAE'], 3)} |",
        "",
        f"ΔNet=**{fmt(delta_net)}** | rem W/L={rem['winners_removed']}/{rem['losers_removed']} | "
        f"boot CI=[{fmt(delta_ci[0])}, {fmt(delta_ci[1])}] | crosses_zero={boot_payload['ci_crosses_zero']}",
        "",
        "## LONG blocked vs kept",
        "",
        f"- BLOCKED: n={blk_sum['trades']} WR={fmt(blk_sum['winrate'])} Net={fmt(blk_sum['net'])} "
        f"npt={fmt(blk_sum['net_per_trade'], 3)} SL={fmt(blk_sum['sl_rate'])} MAE={fmt(blk_sum['median_MAE'], 3)}",
        f"- KEPT: n={kept_sum['trades']} WR={fmt(kept_sum['winrate'])} Net={fmt(kept_sum['net'])} "
        f"npt={fmt(kept_sum['net_per_trade'], 3)} SL={fmt(kept_sum['sl_rate'])}",
        f"- blocked_worse_than_kept={blk_worse}",
        "",
        "## Fixed thresholds",
        "",
    ]
    for r in fixed_rows:
        lines.append(
            f"- >{r['threshold']}: blocked={r['blocked_count']} W/L={r['winners_blocked']}/{r['losers_blocked']} "
            f"WR={fmt(r['winrate'])} Net={fmt(r['net'])} ΔNet={fmt(r['delta_net'])}"
        )
    lines += ["", "## OOS quantiles Q1→Q5", ""]
    for r in oos_q:
        lines.append(
            f"- {r['quantile']}: n={r['count']} med_ext={fmt(r.get('median_extension'), 3)} "
            f"WR={fmt(r['winrate'])} npt={fmt(r['net_per_trade'], 3)} SL={fmt(r['sl_rate'])}"
        )
    lines += [
        "",
        f"q_pattern_ok={q_pattern_ok} flip={q_pattern_flip} monotonic_worse={q_monotonic}",
        f"train_had_pattern={train_had_pattern} oos_replicates={oos_replicates} oos_only={oos_only_pattern}",
        "",
        "## TRAIN quantiles",
        "",
    ]
    for r in train_q:
        lines.append(
            f"- {r['quantile']}: n={r['count']} WR={fmt(r['winrate'])} npt={fmt(r['net_per_trade'], 3)}"
        )
    lines += ["", "## OOS halves", ""]
    for r in time_rows:
        lines.append(
            f"- {r['part']}: ΔNet={fmt(r['delta_net'])} rem W/L={r['winners_removed']}/{r['losers_removed']} "
            f"blocked={r['n_blocked']}"
        )
    lines += ["", "## Symbols", ""]
    for r in sym_rows:
        lines.append(
            f"- {r['symbol']}: LONG={r['oos_long']} blocked={r['n_blocked']} ΔNet={fmt(r['delta_net'])} "
            f"[{r['sample_flag']}]"
        )
    lines += [
        "",
        "## SHORT vs LONG",
        "",
        "| Metric | SHORT | LONG |",
        "| ------ | ----: | ---: |",
    ]
    for r in comparison:
        if r["metric"] == "classification":
            continue
        lines.append(f"| {r['metric']} | {fmt(r['SHORT']) if not isinstance(r['SHORT'], str) else r['SHORT']} | "
                     f"{fmt(r['LONG']) if not isinstance(r['LONG'], str) else r['LONG']} |")
    lines += [
        "",
        f"**Classification: `{symmetry}`**",
        "",
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
        "## DB Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("SYMMETRY", symmetry, flush=True)
    print("REC", recommendation, flush=True)
    print("P80", p80_train, "delta", delta_net, "blocked", n_blocked, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
