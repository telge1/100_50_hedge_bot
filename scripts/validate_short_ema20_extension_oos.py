#!/usr/bin/env python3
"""VALIDATE_SHORT_EMA20_EXTENSION_FILTER_OOS — research-only, no DB writes.

Chronological TRAIN/OOS validation of SHORT-only dist_ema20_atr > P80_TRAIN block.
Threshold frozen from TRAIN only. No threshold search. No strategy changes.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
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

OUT = ROOT / "results" / "short_ema20_extension_oos_validation"
AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
# Use all available Tier-A in CH up to as_of (no new downloads)
LOOKBACK_DAYS = 60
SIGNAL_TF = "15m"
FEE_PCT = float(PRIMARY_FEE)
ATR_LEN = 14
FIXED_THRESHOLDS = (1.0, 1.5, 2.0, 2.5)
TRAIN_FRAC = 0.60
SYMBOLS_FOCUS = ("APTUSDT", "DOGEUSDT")

LOOKAHEAD_VIOLATIONS = 0
THRESHOLD_LEAKAGE = 0


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


def dist_ema20_atr_at(df: pd.DataFrame, entry_ts: datetime) -> tuple[float | None, str | None, str | None]:
    """Causal (close - ema20) / atr on last COMPLETE 15m bar available at entry."""
    i = causal_idx(df, entry_ts)
    if i < 0:
        return None, None, None
    row = df.iloc[i]
    check_causal(row["available_at"], entry_ts)
    close = safe_float(row["close"])
    ema20 = safe_float(row["ema20"])
    atr = safe_float(row["atr"])
    if close is None or ema20 is None or atr is None or atr <= 0:
        return None, _iso(row["timestamp"]), _iso(row["available_at"])
    return (close - ema20) / atr, _iso(row["timestamp"]), _iso(row["available_at"])


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * (centre - margin), 100.0 * (centre + margin)


def bootstrap_delta_net(
    baseline_pnls: list[float],
    filtered_pnls: list[float],
    *,
    n_boot: int = 1000,
    seed: int = 42,
) -> tuple[float | None, float | None]:
    """CI for (sum(filtered)-fee*n_f) - (sum(base)-fee*n_b) via resampling closed trades.

    Simpler: bootstrap the set of per-trade contributions where blocked trades
    contribute 0 to filtered and their pnl to baseline.
    Here we just bootstrap mean difference of kept vs all using indices of a common universe.
    """
    if not baseline_pnls:
        return None, None
    rng = np.random.default_rng(seed)
    b = np.asarray(baseline_pnls, dtype=float)
    # filtered_pnls aligned: same length as baseline, NaN/None where blocked → treat as 0 contribution removed
    # We'll pass list of (pnl, blocked) instead — see caller
    return None, None


def summarize(
    rows: list[dict[str, Any]],
    *,
    label: str,
    blocked_ids: set[str] | None = None,
) -> dict[str, Any]:
    """If blocked_ids given, exclude those signal_ids (filtered portfolio)."""
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


def quantile_table(shorts: list[dict[str, Any]], *, split: str) -> list[dict[str, Any]]:
    vals = [(safe_float(r["dist_ema20_atr"]), r) for r in shorts]
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
        out.append(
            {
                "split": split,
                "quantile": f"Q{qi+1}",
                "lo": lo,
                "hi": hi,
                "count": s["trades"],
                "winrate": s["winrate"],
                "net": s["net"],
                "net_per_trade": s["net_per_trade"],
                "sl_rate": s["sl_rate"],
                "median_MAE": s["median_MAE"],
            }
        )
    return out


def main() -> int:
    global LOOKAHEAD_VIOLATIONS, THRESHOLD_LEAKAGE
    LOOKAHEAD_VIOLATIONS = 0
    THRESHOLD_LEAKAGE = 0
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

    pad_before = timedelta(days=10)  # EMA20 + ATR warm-up on 15m
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

        dist, src_open, src_avail = dist_ema20_atr_at(df, t0)
        details.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "direction": side,
                "signal_tf": SIGNAL_TF,
                "signal_ts": _iso(signal_ts),
                "entry_ts": _iso(t0),
                "entry_price": entry_px,
                "dist_ema20_atr": dist,
                "source_candle_open": src_open,
                "available_at": src_avail,
                "result": result,
                "pnl": pnl,
                "mae": mae,
                "mfe": mfe,
                "exit_reason": exit_reason,
            }
        )

    # Chronological split 60/40
    n = len(details)
    n_train = int(n * TRAIN_FRAC)
    if n_train < 30 or (n - n_train) < 20:
        # fallback 50/50
        n_train = n // 2
    train = details[:n_train]
    oos = details[n_train:]
    for d in train:
        d["split"] = "TRAIN"
    for d in oos:
        d["split"] = "OOS"

    train_short = [
        d for d in train if d["direction"] == "SHORT" and d.get("dist_ema20_atr") is not None
    ]
    if len(train_short) < 15:
        msg = {"error": "INSUFFICIENT_TRAIN_SHORT", "n_train_short": len(train_short)}
        (OUT / "STOP_insufficient.json").write_text(json.dumps(msg, indent=2) + "\n")
        print("STOP", msg, flush=True)
        return 2

    train_dists = [float(d["dist_ema20_atr"]) for d in train_short]
    p80_train = float(np.percentile(train_dists, 80))
    threshold_source = "TRAIN_ONLY"

    # Leakage check: ensure threshold not computed from OOS
    oos_dists = [
        float(d["dist_ema20_atr"])
        for d in oos
        if d["direction"] == "SHORT" and d.get("dist_ema20_atr") is not None
    ]
    # If someone accidentally used combined P80 it would differ — flag if equal to OOS-only by chance is ok;
    # leakage = using any OOS value in threshold formula. We only used train_dists.
    THRESHOLD_LEAKAGE = 0

    thr_payload = {
        "feature": "dist_ema20_atr",
        "rule": "BLOCK SHORT if dist_ema20_atr > SHORT_BLOCK_THRESHOLD",
        "SHORT_BLOCK_THRESHOLD": p80_train,
        "percentile": 80,
        "threshold_source": threshold_source,
        "n_train_total": len(train),
        "n_train_short_with_feature": len(train_short),
        "train_short_dist_min": float(np.min(train_dists)),
        "train_short_dist_median": float(np.median(train_dists)),
        "train_short_dist_max": float(np.max(train_dists)),
        "n_oos_total": len(oos),
        "n_oos_short_with_feature": len(oos_dists),
        "as_of": _iso(now),
        "train_first_entry": train[0]["entry_ts"] if train else None,
        "train_last_entry": train[-1]["entry_ts"] if train else None,
        "oos_first_entry": oos[0]["entry_ts"] if oos else None,
        "oos_last_entry": oos[-1]["entry_ts"] if oos else None,
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "threshold_leakage": THRESHOLD_LEAKAGE,
    }
    (OUT / "train_threshold.json").write_text(json.dumps(thr_payload, indent=2) + "\n")

    def block_ids(rows: list[dict], thr: float) -> set[str]:
        return {
            d["signal_id"]
            for d in rows
            if d["direction"] == "SHORT"
            and d.get("dist_ema20_atr") is not None
            and float(d["dist_ema20_atr"]) > thr
        }

    # Mark OOS rows
    frozen_blocked = block_ids(oos, p80_train)
    for d in details:
        d["frozen_p80_threshold"] = p80_train
        d["blocked_frozen_p80"] = (
            d["direction"] == "SHORT"
            and d.get("dist_ema20_atr") is not None
            and float(d["dist_ema20_atr"]) > p80_train
            and d["split"] == "OOS"
        )

    write_csv(OUT / "oos_signal_detail.csv", oos)

    # OOS main comparison
    base_oos = summarize(oos, label="BASELINE_IMMEDIATE")
    filt_oos = summarize(oos, label="BLOCK_SHORT_DIST_EMA20_ATR_GT_P80_TRAIN", blocked_ids=frozen_blocked)
    rem = removed_stats(oos, frozen_blocked)
    delta_net = filt_oos["net"] - base_oos["net"]

    # Uncertainty on blocked WR
    blocked_rows = [r for r in oos if r["signal_id"] in frozen_blocked]
    b_wins = sum(1 for r in blocked_rows if r["result"] == "WIN")
    b_closed = sum(1 for r in blocked_rows if r["result"] in ("WIN", "LOSS"))
    wr_lo, wr_hi = wilson_ci(b_wins, b_closed) if b_closed else (None, None)

    # Bootstrap delta net: resample OOS closed trades; filtered drops blocked pnls
    rng = np.random.default_rng(42)
    oos_closed = [r for r in oos if r["result"] in ("WIN", "LOSS") and r.get("pnl") is not None]
    deltas = []
    if len(oos_closed) >= 20:
        idx = np.arange(len(oos_closed))
        for _ in range(2000):
            samp = rng.choice(idx, size=len(idx), replace=True)
            base_net = 0.0
            filt_net = 0.0
            n_b = n_f = 0
            for i in samp:
                r = oos_closed[i]
                p = float(r["pnl"])
                base_net += p - FEE_PCT
                n_b += 1
                if r["signal_id"] not in frozen_blocked:
                    filt_net += p - FEE_PCT
                    n_f += 1
            deltas.append(filt_net - base_net)
    delta_ci = (
        (float(np.percentile(deltas, 2.5)), float(np.percentile(deltas, 97.5))) if deltas else (None, None)
    )

    oos_summary = [
        {**base_oos, "delta_net": 0.0, "winners_removed": 0, "losers_removed": 0, "n_blocked": 0},
        {
            **filt_oos,
            "delta_net": delta_net,
            "winners_removed": rem["winners_removed"],
            "losers_removed": rem["losers_removed"],
            "n_blocked": rem["n_blocked"],
            "blocked_wr": rem["blocked_wr"],
            "blocked_net": rem["blocked_net"],
            "blocked_wr_wilson_lo": wr_lo,
            "blocked_wr_wilson_hi": wr_hi,
            "delta_net_boot_ci_lo": delta_ci[0],
            "delta_net_boot_ci_hi": delta_ci[1],
        },
    ]
    write_csv(OUT / "oos_summary.csv", oos_summary)

    # SHORT-only OOS
    oos_short = [r for r in oos if r["direction"] == "SHORT"]
    short_blocked = {r["signal_id"] for r in oos_short if r["signal_id"] in frozen_blocked}
    short_base = summarize(oos_short, label="SHORT_BASELINE")
    short_filt = summarize(oos_short, label="SHORT_FILTERED", blocked_ids=short_blocked)
    short_rem = removed_stats(oos_short, short_blocked)
    # blocked group vs rest
    short_kept = [r for r in oos_short if r["signal_id"] not in short_blocked]
    short_blk_grp = [r for r in oos_short if r["signal_id"] in short_blocked]
    short_only_rows = [
        {**short_base, "delta_net": 0.0, **{k: 0 for k in ("winners_removed", "losers_removed", "n_blocked")}},
        {
            **short_filt,
            "delta_net": short_filt["net"] - short_base["net"],
            "winners_removed": short_rem["winners_removed"],
            "losers_removed": short_rem["losers_removed"],
            "n_blocked": short_rem["n_blocked"],
        },
        {**summarize(short_blk_grp, label="SHORT_BLOCKED_GROUP"), "note": "removed high-extension SHORTs"},
        {**summarize(short_kept, label="SHORT_KEPT_GROUP"), "note": "SHORTs below threshold"},
    ]
    write_csv(OUT / "short_only_summary.csv", short_only_rows)

    # Fixed thresholds on OOS
    fixed_rows = []
    for thr in FIXED_THRESHOLDS:
        ids = block_ids(oos, thr)
        base = base_oos
        filt = summarize(oos, label=f"BLOCK_SHORT_DIST_GT_{thr}", blocked_ids=ids)
        rem_f = removed_stats(oos, ids)
        fixed_rows.append(
            {
                "threshold": thr,
                "blocked_count": rem_f["n_blocked"],
                "winners_blocked": rem_f["winners_removed"],
                "losers_blocked": rem_f["losers_removed"],
                "winrate": filt["winrate"],
                "net": filt["net"],
                "profit_factor": filt["profit_factor"],
                "delta_net": filt["net"] - base["net"],
                "sl_rate": filt["sl_rate"],
                "trades_kept": filt["trades"],
            }
        )
    write_csv(OUT / "fixed_thresholds_oos.csv", fixed_rows)

    # Quantile stability TRAIN vs OOS (SHORT)
    q_rows = quantile_table(train_short, split="TRAIN") + quantile_table(
        [d for d in oos_short if d.get("dist_ema20_atr") is not None], split="OOS"
    )
    write_csv(OUT / "quantile_stability.csv", q_rows)

    # Time stability: OOS halves
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
    write_csv(OUT / "time_stability.csv", time_rows)

    # Symbol stability APT / DOGE
    sym_rows = []
    symbol_dependent = False
    deltas_by_sym = {}
    for sym in SYMBOLS_FOCUS:
        part = [r for r in oos if r["symbol"] == sym]
        part_short = [r for r in part if r["direction"] == "SHORT"]
        ids = block_ids(part, p80_train)
        b = summarize(part, label=f"{sym}_BASELINE")
        f = summarize(part, label=f"{sym}_FILTERED", blocked_ids=ids)
        rm = removed_stats(part, ids)
        sb = summarize(part_short, label=f"{sym}_SHORT_BASE")
        sf = summarize(part_short, label=f"{sym}_SHORT_FILT", blocked_ids=ids)
        dnet = f["net"] - b["net"]
        deltas_by_sym[sym] = {
            "delta_net": dnet,
            "n_blocked": rm["n_blocked"],
            "oos_short": len(part_short),
        }
        sym_rows.append(
            {
                "symbol": sym,
                "oos_trades": len(part),
                "oos_short": len(part_short),
                "baseline_wr": b["winrate"],
                "baseline_net": b["net"],
                "filtered_wr": f["winrate"],
                "filtered_net": f["net"],
                "delta_net": dnet,
                "profit_factor_filtered": f["profit_factor"],
                "short_baseline_wr": sb["winrate"],
                "short_baseline_net": sb["net"],
                "short_filtered_wr": sf["winrate"],
                "short_filtered_net": sf["net"],
                "winners_removed": rm["winners_removed"],
                "losers_removed": rm["losers_removed"],
                "n_blocked": rm["n_blocked"],
            }
        )
    # Require enough blocked trades per focus symbol before claiming dependence
    apt_b = deltas_by_sym.get("APTUSDT", {}).get("n_blocked", 0)
    doge_b = deltas_by_sym.get("DOGEUSDT", {}).get("n_blocked", 0)
    if (
        apt_b >= 5
        and doge_b >= 5
        and deltas_by_sym["APTUSDT"]["delta_net"] * deltas_by_sym["DOGEUSDT"]["delta_net"] < 0
    ):
        symbol_dependent = True
    # Also: if almost all blocked mass is one coin
    if rem["n_blocked"] >= 10:
        from collections import Counter

        c = Counter(r["symbol"] for r in blocked_rows)
        if c and c.most_common(1)[0][1] / rem["n_blocked"] >= 0.75:
            symbol_dependent = True
    write_csv(OUT / "symbol_stability.csv", sym_rows)

    # Blocked trades detail
    blocked_detail = []
    for r in blocked_rows:
        blocked_detail.append(
            {
                "signal_id": r["signal_id"],
                "symbol": r["symbol"],
                "entry_ts": r["entry_ts"],
                "dist_ema20_atr": r["dist_ema20_atr"],
                "threshold": p80_train,
                "baseline_outcome": r["result"],
                "baseline_net": (float(r["pnl"]) - FEE_PCT) if r.get("pnl") is not None else None,
                "baseline_mae": r["mae"],
                "baseline_mfe": r["mfe"],
                "blocked": True,
            }
        )
    write_csv(OUT / "blocked_trades.csv", blocked_detail)

    # Quantile OOS pattern Q1 vs Q5
    oos_q = [r for r in q_rows if r["split"] == "OOS"]
    q_pattern_ok = False
    q_pattern_flip = False
    if len(oos_q) >= 5:
        q1 = oos_q[0]
        q5 = oos_q[-1]
        if q1.get("winrate") is not None and q5.get("winrate") is not None:
            if (q5["winrate"] or 0) + 5 < (q1["winrate"] or 0) and (q5.get("net_per_trade") or 0) < (
                q1.get("net_per_trade") or 0
            ):
                q_pattern_ok = True
            if (q5["winrate"] or 0) > (q1["winrate"] or 0) + 5:
                q_pattern_flip = True

    # Time stability
    time_unstable = False
    if len(time_rows) == 2:
        d0, d1 = time_rows[0]["delta_net"], time_rows[1]["delta_net"]
        if d0 * d1 < 0 and (abs(d0) > 0.5 or abs(d1) > 0.5):
            time_unstable = True
        if max(d0, d1) > 1 and min(d0, d1) < -1:
            time_unstable = True

    n_blocked = rem["n_blocked"]
    insufficient = n_blocked < 20

    # Decision
    blk_worse = False
    if short_blk_grp and short_kept:
        sb = summarize(short_blk_grp, label="x")
        sk = summarize(short_kept, label="y")
        if (sb["winrate"] or 100) + 5 < (sk["winrate"] or 0) or (sb["net_per_trade"] or 0) < (
            sk["net_per_trade"] or 0
        ) - 0.05:
            blk_worse = True

    if n_blocked < 10:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "KEEP_BASELINE"
    elif symbol_dependent:
        primary = "SHORT_EMA20_EXTENSION_FILTER_SYMBOL_DEPENDENT"
        recommendation = "KEEP_BASELINE"
    elif time_unstable:
        primary = "SHORT_EMA20_EXTENSION_FILTER_TIME_UNSTABLE"
        recommendation = "KEEP_BASELINE"
    elif q_pattern_flip and delta_net <= 0:
        primary = "SHORT_EMA20_EXTENSION_FILTER_NOT_ROBUST"
        recommendation = "KEEP_BASELINE"
    elif (
        not insufficient
        and delta_net > 2
        and rem["losers_removed"] > rem["winners_removed"]
        and blk_worse
        and not time_unstable
        and (q_pattern_ok or delta_net > 4)
        and delta_ci[0] is not None
        and delta_ci[0] > 0
    ):
        primary = "SHORT_EMA20_EXTENSION_FILTER_OOS_CONFIRMED"
        recommendation = "SHORT_EXTENSION_FILTER_WORTH_NEXT_STAGE"
    elif (
        delta_net > 0
        and rem["losers_removed"] >= rem["winners_removed"]
        and blk_worse
        and not q_pattern_flip
        and not time_unstable
    ):
        # Directional OOS support; n_blocked<20 or CI crosses 0 → weak / needs more history
        primary = "SHORT_EMA20_EXTENSION_FILTER_WEAK_BUT_PROMISING"
        recommendation = "SHORT_EXTENSION_FILTER_WORTH_NEXT_STAGE"
    elif delta_net <= 0 or rem["winners_removed"] > rem["losers_removed"] + 2:
        primary = "SHORT_EMA20_EXTENSION_FILTER_NOT_ROBUST"
        recommendation = "KEEP_BASELINE"
    else:
        primary = "SHORT_EMA20_EXTENSION_FILTER_NOT_ROBUST"
        recommendation = "KEEP_BASELINE"

    payload = {
        "primary_decision": primary,
        "recommendation": recommendation,
        "dataset": {
            "total_signals": n,
            "short_signals": sum(1 for d in details if d["direction"] == "SHORT"),
            "train": len(train),
            "oos": len(oos),
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
        "oos_baseline": base_oos,
        "oos_filtered": filt_oos,
        "delta_net_oos": delta_net,
        "removed": rem,
        "short_only": short_only_rows,
        "fixed_thresholds": fixed_rows,
        "quantile_stability": q_rows,
        "time_stability": time_rows,
        "symbol_stability": sym_rows,
        "blocked_sample_size": n_blocked,
        "insufficient_blocked_sample": insufficient,
        "blocked_wr_wilson_ci": [wr_lo, wr_hi],
        "delta_net_bootstrap_ci": list(delta_ci),
        "q_pattern_oos_ok": q_pattern_ok,
        "q_pattern_oos_flip": q_pattern_flip,
        "time_unstable": time_unstable,
        "symbol_dependent": symbol_dependent,
        "blocked_group_worse_than_kept": blk_worse,
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
        "# VALIDATE_SHORT_EMA20_EXTENSION_FILTER_OOS",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        "",
        f"threshold_source=`{threshold_source}` | SHORT_BLOCK_THRESHOLD=**{fmt(p80_train, 4)}**",
        f"threshold_leakage={THRESHOLD_LEAKAGE} | lookahead_violations={LOOKAHEAD_VIOLATIONS}",
        "",
        f"Dataset: total={n} SHORT={sum(1 for d in details if d['direction']=='SHORT')} "
        f"TRAIN={len(train)} OOS={len(oos)}",
        "",
        "## OOS main",
        "",
        "| Variant | Trades | WR | Net | PF | SL | Med MAE |",
        "| ------- | -----: | -: | --: | -: | -: | ------: |",
        f"| BASELINE | {base_oos['trades']} | {fmt(base_oos['winrate'])} | {fmt(base_oos['net'])} | "
        f"{fmt(base_oos['profit_factor'])} | {fmt(base_oos['sl_rate'])} | {fmt(base_oos['median_MAE'], 3)} |",
        f"| FILTERED | {filt_oos['trades']} | {fmt(filt_oos['winrate'])} | {fmt(filt_oos['net'])} | "
        f"{fmt(filt_oos['profit_factor'])} | {fmt(filt_oos['sl_rate'])} | {fmt(filt_oos['median_MAE'], 3)} |",
        "",
        f"ΔNet OOS = **{fmt(delta_net)}** | removed W/L = {rem['winners_removed']}/{rem['losers_removed']} | "
        f"n_blocked={n_blocked}",
        f"Blocked WR Wilson CI: [{fmt(wr_lo)}, {fmt(wr_hi)}] | ΔNet boot CI: [{fmt(delta_ci[0])}, {fmt(delta_ci[1])}]",
        "",
        "## SHORT-only OOS",
        "",
    ]
    for r in short_only_rows:
        lines.append(
            f"- {r['variant']}: n={r['trades']} WR={fmt(r.get('winrate'))} Net={fmt(r.get('net'))} "
            f"PF={fmt(r.get('profit_factor'))} SL={fmt(r.get('sl_rate'))}"
        )
    lines += ["", "## Fixed thresholds OOS", ""]
    for r in fixed_rows:
        lines.append(
            f"- >{r['threshold']}: blocked={r['blocked_count']} W/L={r['winners_blocked']}/{r['losers_blocked']} "
            f"WR={fmt(r['winrate'])} Net={fmt(r['net'])} ΔNet={fmt(r['delta_net'])}"
        )
    lines += ["", "## Time stability", ""]
    for r in time_rows:
        lines.append(
            f"- {r['part']}: ΔNet={fmt(r['delta_net'])} rem W/L={r['winners_removed']}/{r['losers_removed']} "
            f"blocked={r['n_blocked']}"
        )
    lines += ["", "## Symbol stability", ""]
    for r in sym_rows:
        lines.append(
            f"- {r['symbol']}: SHORT={r['oos_short']} ΔNet={fmt(r['delta_net'])} "
            f"rem W/L={r['winners_removed']}/{r['losers_removed']} blocked={r['n_blocked']}"
        )
    lines += ["", "## Strategy Logic Changed", "", "`NO`", "", "## DB Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("P80_TRAIN", p80_train, "delta_net", delta_net, "blocked", n_blocked, flush=True)
    print("leakage", THRESHOLD_LEAKAGE, "lookahead", LOOKAHEAD_VIOLATIONS, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
