#!/usr/bin/env python3
"""TP_FIRST vs SL_FIRST case-control on long geometry signals (entry-time features only)."""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LONG_ROOT = Path(__file__).resolve().parent
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
REPO = LONG_ROOT.parents[2]
SIGNALS_PATH = LONG_ROOT / "long_geometry_only_v1" / "long_geometry_signals.json"
OUT_DIR = LONG_ROOT / "long_geometry_case_control_v1"

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
HISTORY_WEEKS = 12
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")

NUMERIC_SKIP = {
    "symbol",
    "entry_time",
    "entry_open",
    "pool_id",
    "outcome",
    "first_hit",
    "outcome_label",
    "mode",
    "pool_open",
    "known_at",
    "bridge_lower_id",
    "tp_pool_id",
    "trend_4h",
    "exit_time",
    "ema_stack",
    "mae_pct",
    "mfe_pct",
    "pnl_pct",
}


def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT), str(LONG_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _is_tp(row: dict) -> bool:
    return row.get("outcome") == "TP_FIRST" or row.get("first_hit") == "TP"


def _mann_whitney(a: list[float], b: list[float]) -> float | None:
    if len(a) < 5 or len(b) < 5:
        return None
    try:
        from scipy.stats import mannwhitneyu

        return float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
    except Exception:
        return None


def _compare_numeric(name: str, tp_vals: list[float], sl_vals: list[float]) -> dict:
    med_tp = statistics.median(tp_vals) if tp_vals else None
    med_sl = statistics.median(sl_vals) if sl_vals else None
    mean_tp = statistics.mean(tp_vals) if tp_vals else None
    mean_sl = statistics.mean(sl_vals) if sl_vals else None
    abs_diff = None
    rel_diff = None
    if med_tp is not None and med_sl is not None:
        abs_diff = med_tp - med_sl
        if med_sl != 0:
            rel_diff = abs_diff / abs(med_sl) * 100.0
    iqr_tp = None
    iqr_sl = None
    if len(tp_vals) >= 4:
        q = statistics.quantiles(tp_vals, n=4)
        iqr_tp = q[2] - q[0]
    if len(sl_vals) >= 4:
        q = statistics.quantiles(sl_vals, n=4)
        iqr_sl = q[2] - q[0]
    pooled_iqr = None
    effect = None
    if iqr_tp is not None and iqr_sl is not None:
        pooled_iqr = (iqr_tp + iqr_sl) / 2.0
        if pooled_iqr and abs_diff is not None:
            effect = abs_diff / pooled_iqr
    return {
        "feature": name,
        "n_tp": len(tp_vals),
        "median_tp": round(med_tp, 6) if med_tp is not None else None,
        "mean_tp": round(mean_tp, 6) if mean_tp is not None else None,
        "n_sl": len(sl_vals),
        "median_sl": round(med_sl, 6) if med_sl is not None else None,
        "mean_sl": round(mean_sl, 6) if mean_sl is not None else None,
        "median_diff": round(abs_diff, 6) if abs_diff is not None else None,
        "median_rel_diff_pct": round(rel_diff, 4) if rel_diff is not None else None,
        "iqr_tp": round(iqr_tp, 6) if iqr_tp is not None else None,
        "iqr_sl": round(iqr_sl, 6) if iqr_sl is not None else None,
        "effect_size_median_iqr": round(effect, 4) if effect is not None else None,
        "mann_whitney_p": _mann_whitney(tp_vals, sl_vals),
    }


def _categorical_table(rows: list[dict], field: str) -> list[dict]:
    buckets: dict[str, dict] = {}
    for r in rows:
        v = r.get(field)
        if v is None:
            v = "null"
        elif isinstance(v, bool):
            v = str(v)
        else:
            v = str(v)
        if v not in buckets:
            buckets[v] = {"state": v, "TP": 0, "SL": 0, "count": 0}
        buckets[v]["count"] += 1
        if _is_tp(r):
            buckets[v]["TP"] += 1
        else:
            buckets[v]["SL"] += 1
    out = []
    for b in buckets.values():
        b["tp_rate"] = round(b["TP"] / b["count"], 4) if b["count"] else None
        b["feature"] = field
        out.append(b)
    return sorted(out, key=lambda x: -x["count"])


def _coin_direction(tp_med: float | None, sl_med: float | None, coin_rows: list[dict], field: str) -> str:
    if tp_med is None or sl_med is None:
        return "UNSTABLE"
    direction = 1 if tp_med > sl_med else -1
    hits = 0
    for sym in SYMBOLS:
        tp = [float(r[field]) for r in coin_rows if r["symbol"] == sym and _is_tp(r) and r.get(field) is not None]
        sl = [float(r[field]) for r in coin_rows if r["symbol"] == sym and not _is_tp(r) and r.get(field) is not None]
        if not tp or not sl:
            continue
        d = statistics.median(tp) - statistics.median(sl)
        if direction > 0 and d > 0:
            hits += 1
        if direction < 0 and d < 0:
            hits += 1
    if hits >= 3:
        return "STABLE_3COIN"
    if hits == 2:
        return "STABLE_2COIN"
    if hits == 1:
        return "COIN_SPECIFIC"
    return "UNSTABLE"


def _build_row(
    sig: dict,
    bars15: list[dict],
    pools15: list[dict],
    moment: datetime,
    entry_idx: int,
    market: dict,
    pools30: list[dict],
    pools4h: list[dict],
) -> dict:
    from case_control_features import (
        active_pools,
        candle_features,
        causal_touches_entry_pool,
        ema_feature_block,
        last_closed_index,
        mtf_pool_snapshot,
        pool_age_hours,
        prior_return_pct,
        range_pct_window,
        uppers_above_price,
    )

    entry = float(sig["entry_price"])
    pool_top = float(sig["top_price"])
    pool_bottom = float(sig["bottom_price"])
    bar = bars15[entry_idx]
    known = datetime.fromisoformat(sig["known_at"])
    width_pct = (pool_top - pool_bottom) / pool_bottom * 100.0 if pool_bottom else None
    dist_top = (entry - pool_top) / entry * 100.0
    dist_bottom = (entry - pool_bottom) / entry * 100.0
    zone_pos = (entry - pool_bottom) / (pool_top - pool_bottom) if pool_top != pool_bottom else None
    rr = (
        float(sig["tp_distance_pct"]) / float(sig["stop_distance_pct"])
        if sig.get("stop_distance_pct") and float(sig["stop_distance_pct"]) > 0
        else None
    )

    row: dict[str, Any] = {
        "symbol": sig["symbol"],
        "entry_time": sig["entry_time"],
        "outcome": sig["outcome"],
        "first_hit": sig["first_hit"],
        "entry_price": entry,
        "stop": float(sig["stop"]),
        "tp": float(sig["tp"]),
        "pool_id": sig["pool_id"],
        "mae_pct": sig.get("mae_pct"),
        "mfe_pct": sig.get("mfe_pct"),
        "pnl_pct": sig.get("pnl_pct"),
        "stop_distance_pct": sig.get("stop_distance_pct"),
        "tp_distance_pct": sig.get("tp_distance_pct"),
        "rr": round(rr, 4) if rr is not None else None,
        "bridge_gap_pct": sig.get("gap_pct"),
        "tp_gap_pct": sig.get("tp_gap_pct"),
        "entry_pool_age_h": pool_age_hours(moment, known),
        "entry_pool_width_pct": round(width_pct, 4) if width_pct is not None else None,
        "entry_dist_to_pool_top_pct": round(dist_top, 4),
        "entry_dist_to_pool_bottom_pct": round(dist_bottom, 4),
        "entry_in_pool_zone_ratio": round(zone_pos, 4) if zone_pos is not None else None,
        "entry_pool_touches_prior": causal_touches_entry_pool(bars15, entry_idx, pool_top),
        "tp_pool_age_h": None,
        "tp_pool_width_pct": None,
        "uppers_behind_tp_count": None,
    }

    tp_pool = next((p for p in active_pools(pools15, moment, "upper") if p["pool_id"] == sig["tp_pool_id"]), None)
    if tp_pool:
        row["tp_pool_age_h"] = round(pool_age_hours(moment, tp_pool["known"]), 4)
        row["tp_pool_width_pct"] = round(
            (tp_pool["top"] - tp_pool["bottom"]) / tp_pool["bottom"] * 100.0, 4
        )
        behind = [
            p
            for p in uppers_above_price(pools15, moment, float(tp_pool["bottom"]))
            if p["pool_id"] != tp_pool["pool_id"]
        ]
        row["uppers_behind_tp_count"] = len(behind)

    row.update(mtf_pool_snapshot(pools15, moment, entry, "m15"))
    row.update(mtf_pool_snapshot(pools30, moment, entry, "m30"))
    row.update(mtf_pool_snapshot(pools4h, moment, entry, "m4h"))

    idx15 = entry_idx
    idx30 = last_closed_index(market["30m"]["bars"], moment)
    idx1h = last_closed_index(market["1h"]["bars"], moment)
    idx4h = last_closed_index(market["4h"]["bars"], moment)
    row.update(ema_feature_block(bars15, idx15, "ema15m"))
    row.update(ema_feature_block(market["30m"]["bars"], idx30, "ema30m"))
    row.update(ema_feature_block(market["1h"]["bars"], idx1h, "ema1h"))
    row.update(ema_feature_block(market["4h"]["bars"], idx4h, "ema4h"))

    cf = candle_features(bar)
    for k, v in cf.items():
        row[f"entry_bar_{k}"] = round(v, 6)

    for n in (1, 3, 6, 12, 24):
        pr = prior_return_pct(bars15, idx15, n)
        row[f"prior_{n}bar_return_pct"] = round(pr, 4) if pr is not None else None
    for label, n in (("1h", 4), ("4h", 16), ("12h", 48)):
        rg = range_pct_window(bars15, idx15, n)
        row[f"prior_{label}_range_pct"] = round(rg, 4) if rg is not None else None

    return row


def _leakage_audit(sample_rows: list[dict], meta: list[dict]) -> list[dict]:
    checks = []
    for m in meta[:10]:
        checks.append(
            {
                "entry_time": m["entry_time"],
                "symbol": m["symbol"],
                "decision_time_iso": m["decision_time"],
                "pool_known_before_decision": m["pool_known_ok"],
                "pool_not_broken_at_decision": m["pool_break_ok"],
                "scanner_tip_at_entry": m["scanner_tip_ok"],
                "ema_bar_close_le_decision": m["ema_bar_ok"],
                "pass": all(
                    [
                        m["pool_known_ok"],
                        m["pool_break_ok"],
                        m["scanner_tip_ok"],
                        m["ema_bar_ok"],
                    ]
                ),
            }
        )
    return checks


def main() -> None:
    _ensure_paths()
    from dashboard.research_charts.lld_research_kernel import (
        candle_stamps,
        load_pane_candles,
        scanner_pools_for_index,
        ui_lld_config,
    )
    from case_control_features import last_closed_index
    from find_short_entry_15m_v1 import build_15m_bars, load_market

    signals = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
    if len(signals) != 169:
        raise SystemExit(f"expected 169 signals, got {len(signals)}")
    for s in signals:
        exp = "TP_FIRST" if s["first_hit"] == "TP" else "SL_FIRST"
        if s.get("outcome") != exp:
            raise SystemExit(f"outcome parity fail {s['entry_time']}")

    by_sym: dict[str, list[dict]] = {sym: [] for sym in SYMBOLS}
    for s in signals:
        by_sym[s["symbol"]].append(s)

    feature_rows: list[dict] = []
    leakage_meta: list[dict] = []

    for symbol in SYMBOLS:
        market = load_market(symbol)
        _, c15 = load_pane_candles(
            symbol, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
        )
        bars15 = build_15m_bars(c15)
        by_open = {b["open_time"]: b for b in bars15}
        lld15 = ui_lld_config("15m")
        lld30 = ui_lld_config("30m")
        lld4h = ui_lld_config("4h")
        _, c30 = load_pane_candles(
            symbol, "30m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
        )
        _, c4h = load_pane_candles(
            symbol, "4h", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
        )
        cache15: dict = {}
        cache30: dict = {}
        cache4h: dict = {}
        stamps30 = candle_stamps(c30)
        stamps4h = candle_stamps(c4h)

        def _pane_index(stamps: list, open_time: datetime) -> int:
            for i, ts in enumerate(stamps):
                if ts == open_time:
                    return i
            raise KeyError(open_time)

        for sig in sorted(by_sym[symbol], key=lambda x: x["entry_time"]):
            ot = datetime.fromisoformat(sig["entry_time"])
            bar = by_open.get(ot)
            if bar is None:
                raise SystemExit(f"missing bar {symbol} {ot}")
            moment = bar["close_time"]
            idx = bars15.index(bar)
            pools15, _, proof = scanner_pools_for_index(c15, "15m", idx, lld15, cache=cache15)
            idx30 = last_closed_index(market["30m"]["bars"], moment)
            idx4 = last_closed_index(market["4h"]["bars"], moment)
            if idx30 is None or idx4 is None:
                raise SystemExit(f"missing MTF index {symbol} {sig['entry_time']}")
            idx30_pane = _pane_index(stamps30, market["30m"]["bars"][idx30]["open_time"])
            idx4_pane = _pane_index(stamps4h, market["4h"]["bars"][idx4]["open_time"])
            pools30, _, _ = scanner_pools_for_index(c30, "30m", idx30_pane, lld30, cache=cache30)
            pools4h, _, _ = scanner_pools_for_index(c4h, "4h", idx4_pane, lld4h, cache=cache4h)

            row = _build_row(sig, bars15, pools15, moment, idx, market, pools30, pools4h)
            feature_rows.append(row)

            pool = next((p for p in pools15 if p["pool_id"] == sig["pool_id"]), None)
            h4i = last_closed_index(market["4h"]["bars"], moment)
            leakage_meta.append(
                {
                    "entry_time": sig["entry_time"],
                    "symbol": symbol,
                    "decision_time": proof.decision_time.isoformat(),
                    "pool_known_ok": pool is not None and pool["known"] <= moment,
                    "pool_break_ok": pool is not None and (pool.get("break_at") is None or pool["break_at"] > moment),
                    "scanner_tip_ok": proof.decision_time == moment and proof.entry_open == ot,
                    "ema_bar_ok": market["4h"]["bars"][h4i]["close_time"] <= moment if h4i is not None else False,
                }
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "case_control_rows.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(feature_rows[0].keys()))
        w.writeheader()
        w.writerows(feature_rows)

    tp_rows = [r for r in feature_rows if _is_tp(r)]
    sl_rows = [r for r in feature_rows if not _is_tp(r)]

    numeric_fields = []
    for k in feature_rows[0]:
        if k in NUMERIC_SKIP or k.endswith("_ema_stack"):
            continue
        v0 = feature_rows[0][k]
        if isinstance(v0, (int, float)) and not isinstance(v0, bool):
            numeric_fields.append(k)

    comparisons = []
    for field in numeric_fields:
        tp_vals = [float(r[field]) for r in tp_rows if r.get(field) is not None and not (isinstance(r[field], float) and math.isnan(r[field]))]
        sl_vals = [float(r[field]) for r in sl_rows if r.get(field) is not None and not (isinstance(r[field], float) and math.isnan(r[field]))]
        if not tp_vals or not sl_vals:
            continue
        comparisons.append(_compare_numeric(field, tp_vals, sl_vals))

    comparisons.sort(
        key=lambda x: abs(x.get("effect_size_median_iqr") or 0),
        reverse=True,
    )

    cat_fields = [
        "ema15m_close_above_ema200",
        "ema4h_close_above_ema200",
        "m15_rising_3",
        "m30_rising_3",
        "m4h_rising_3",
        "ema15m_ema_stack",
        "ema4h_ema_stack",
    ]
    categorical: list[dict] = []
    for field in cat_fields:
        if field in feature_rows[0]:
            categorical.extend(_categorical_table(feature_rows, field))

    ranking = []
    for c in comparisons:
        eff = abs(c.get("effect_size_median_iqr") or 0)
        stability = _coin_direction(c.get("median_tp"), c.get("median_sl"), feature_rows, c["feature"])
        if eff >= 0.35 and stability in ("STABLE_3COIN", "STABLE_2COIN"):
            tier = "STRONG DIFFERENCE"
        elif eff >= 0.2 or (c.get("median_rel_diff_pct") or 0) >= 15:
            tier = "MODERATE DIFFERENCE"
        else:
            tier = "WEAK / NONE"
        ranking.append({**c, "tier": tier, "cross_coin_stability": stability})

    def _bucket_rr(v: float) -> str:
        if v < 1.5:
            return "<1.5"
        if v < 2.5:
            return "1.5-2.5"
        if v < 4.0:
            return "2.5-4"
        return ">4"

    def _bucket_age(h: float) -> str:
        if h < 24:
            return "young_lt_24h"
        if h < 72:
            return "mid_24_72h"
        return "old_gt_72h"

    bucket_study = []
    for field, fn in (
        ("rr", _bucket_rr),
        ("entry_pool_age_h", _bucket_age),
        ("ema4h_dist_ema200_pct", lambda x: "below" if x < -1 else ("near" if abs(x) <= 1 else "above")),
    ):
        buckets: dict[str, dict] = {}
        for r in feature_rows:
            if r.get(field) is None:
                continue
            b = fn(float(r[field]))
            if b not in buckets:
                buckets[b] = {"bucket": b, "feature": field, "TP": 0, "SL": 0}
            if _is_tp(r):
                buckets[b]["TP"] += 1
            else:
                buckets[b]["SL"] += 1
        for b in buckets.values():
            b["count"] = b["TP"] + b["SL"]
            b["tp_rate"] = round(b["TP"] / b["count"], 4)
            bucket_study.append(b)

    ada = [r for r in feature_rows if r["symbol"] == "ADAUSDT"]
    doge = [r for r in feature_rows if r["symbol"] == "DOGEUSDT"]
    ada_vs_doge = []
    key_fields = [
        "entry_pool_age_h",
        "m15_lowers_within_2pct",
        "m15_rising_3",
        "rr",
        "tp_distance_pct",
        "ema4h_dist_ema200_pct",
        "prior_4h_range_pct",
    ]
    for field in key_fields:
        a_vals = [float(r[field]) for r in ada if isinstance(r.get(field), (int, float))]
        d_vals = [float(r[field]) for r in doge if isinstance(r.get(field), (int, float))]
        if not a_vals or not d_vals:
            continue
        ada_vs_doge.append(
            {
                "feature": field,
                "ada_median": round(statistics.median(a_vals), 4),
                "doge_median": round(statistics.median(d_vals), 4),
                "ada_tp_rate": round(sum(1 for r in ada if _is_tp(r)) / len(ada), 4),
                "doge_tp_rate": round(sum(1 for r in doge if _is_tp(r)) / len(doge), 4),
            }
        )

    coin_comparison = {}
    for sym in SYMBOLS:
        sub = [r for r in feature_rows if r["symbol"] == sym]
        tp = sum(1 for r in sub if _is_tp(r))
        sl = len(sub) - tp
        top_feats = []
        for c in comparisons[:30]:
            f = c["feature"]
            tpv = [float(r[f]) for r in sub if _is_tp(r) and r.get(f) is not None]
            slv = [float(r[f]) for r in sub if not _is_tp(r) and r.get(f) is not None]
            if tpv and slv:
                top_feats.append(
                    {
                        "feature": f,
                        "median_tp": round(statistics.median(tpv), 4),
                        "median_sl": round(statistics.median(slv), 4),
                    }
                )
        coin_comparison[sym] = {"TP": tp, "SL": sl, "top_feature_medians": top_feats[:8]}

    def _example_row(r: dict) -> dict:
        return {
            "coin": r["symbol"],
            "time": r["entry_time"],
            "entry": r["entry_price"],
            "sl": r["stop"],
            "tp": r["tp"],
            "R:R": r.get("rr"),
            "entry_lower_age_h": r.get("entry_pool_age_h"),
            "m15_lowers_2pct": r.get("m15_lowers_within_2pct"),
            "m15_rising_3": r.get("m15_rising_3"),
            "tp_upper_age_h": r.get("tp_pool_age_h"),
            "ema15m_dist_200": r.get("ema15m_dist_ema200_pct"),
            "ema1h_dist_200": r.get("ema1h_dist_ema200_pct"),
            "ema4h_dist_200": r.get("ema4h_dist_ema200_pct"),
            "pool_summary": f"lowers2%={r.get('m15_lowers_within_2pct')} rising3={r.get('m15_rising_3')}",
            "outcome": r["outcome"],
            "MAE_pct": r.get("mae_pct"),
            "MFE_pct": r.get("mfe_pct"),
        }

    tp_sorted = sorted(tp_rows, key=lambda r: float(r.get("mfe_pct") or 0), reverse=True)[:10]
    sl_sorted = sorted(sl_rows, key=lambda r: (float(r.get("mae_pct") or 0), -float(r.get("pnl_pct") or 0)), reverse=True)[:10]

    leakage = _leakage_audit(feature_rows, leakage_meta)
    leakage_pass = all(x["pass"] for x in leakage)

    strong = [r for r in ranking if r["tier"] == "STRONG DIFFERENCE"]
    moderate = [r for r in ranking if r["tier"] == "MODERATE DIFFERENCE"]

    if len(strong) >= 3:
        verdict = "LONG_CASE_CONTROL_HAS_CLEAR_SEPARATION"
    elif len(strong) + len(moderate) >= 5:
        verdict = "LONG_CASE_CONTROL_MODERATE_SEPARATION"
    else:
        verdict = "LONG_CASE_CONTROL_NO_CLEAR_SEPARATION"

    summary = {
        "dataset": {
            "n": len(feature_rows),
            "TP": len(tp_rows),
            "SL": len(sl_rows),
            "by_coin": {sym: {"TP": sum(1 for r in feature_rows if r["symbol"] == sym and _is_tp(r)), "SL": sum(1 for r in feature_rows if r["symbol"] == sym and not _is_tp(r))} for sym in SYMBOLS},
        },
        "overall_numeric_top15": comparisons[:15],
        "ranking": ranking[:40],
        "categorical_tables": categorical,
        "bucket_study": bucket_study,
        "ada_vs_doge": ada_vs_doge,
        "coin_comparison": coin_comparison,
        "examples_tp_first": [_example_row(r) for r in tp_sorted],
        "examples_sl_first": [_example_row(r) for r in sl_sorted],
        "leakage_audit": leakage,
        "leakage_audit_pass": leakage_pass,
        "candidate_features_next_research": [
            r["feature"]
            for r in ranking
            if r["tier"] in ("STRONG DIFFERENCE", "MODERATE DIFFERENCE")
            and r["cross_coin_stability"] in ("STABLE_3COIN", "STABLE_2COIN")
        ][:10],
        "final_verdict": verdict,
    }

    with (OUT_DIR / "case_control_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    with (OUT_DIR / "feature_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(comparisons[0].keys()) if comparisons else ["feature"])
        w.writeheader()
        w.writerows(comparisons)

    with (OUT_DIR / "feature_ranking.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ranking[0].keys()) if ranking else ["feature"])
        w.writeheader()
        w.writerows(ranking)

    with (OUT_DIR / "coin_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        rows_out = []
        for sym, data in coin_comparison.items():
            rows_out.append({"symbol": sym, "TP": data["TP"], "SL": data["SL"]})
        w = csv.DictWriter(f, fieldnames=["symbol", "TP", "SL"])
        w.writeheader()
        w.writerows(rows_out)

    with (OUT_DIR / "ada_vs_doge.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ada_vs_doge[0].keys()) if ada_vs_doge else ["feature"])
        w.writeheader()
        w.writerows(ada_vs_doge)

    method = """# Long geometry case-control V1

Cases: TP_FIRST (n=88). Controls: SL_FIRST (n=81). Same 169 deduped geometry signals.

Features use information at 15m entry bar close (`decision_time`) only.
MFE/MAE/PnL are outcome labels, not model inputs.

No new filters. No strategy changes.
"""
    (OUT_DIR / "method.md").write_text(method, encoding="utf-8")

    print("ROWS", len(feature_rows), "VERDICT", verdict, "LEAKAGE", leakage_pass)
    print("TOP5", [c["feature"] for c in comparisons[:5]])


if __name__ == "__main__":
    main()
