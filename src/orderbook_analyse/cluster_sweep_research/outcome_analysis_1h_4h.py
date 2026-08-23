"""1h / 4h MFE-MAE outcome analysis from 1m candles (research-only, UTC).

Horizon semantics: [entry_at, entry_at + horizon) — entry minute included, horizon end exclusive.
MFE/MAE stored as positive percent magnitudes; signed returns optional.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import pandas as pd

from .models import SetupDirection

HORIZONS_MINUTES = (60, 240)
FIRST_HIT_THRESHOLDS_PCT = (0.10, 0.20, 0.30, 0.50, 1.00)
SMALL_SAMPLE_N = 5


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def _cluster_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if str(a.get("cluster_id") or "") and str(a.get("cluster_id")) == str(b.get("cluster_id")):
        return True
    try:
        lo_a, hi_a = float(a["cluster_low"]), float(a["cluster_high"])
        lo_b, hi_b = float(b["cluster_low"]), float(b["cluster_high"])
    except (KeyError, TypeError, ValueError):
        return False
    overlap = min(hi_a, hi_b) - max(lo_a, lo_b)
    if overlap <= 0:
        return False
    span = min(hi_a - lo_a, hi_b - lo_b)
    return span > 0 and overlap / span >= 0.5


def slice_future_1m(
    candles_1m: pd.DataFrame,
    entry_at: datetime,
    horizon_minutes: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return 1m rows in [entry_at, entry_at + horizon) and coverage meta."""
    entry_u = _utc(entry_at)
    end_u = entry_u + timedelta(minutes=horizon_minutes)
    df = candles_1m.sort_values("open_time").copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    if df["open_time"].dt.tz is not None:
        df["open_time"] = df["open_time"].dt.tz_convert("UTC").dt.tz_localize(None)
    entry_naive = entry_u.replace(tzinfo=None)
    end_naive = end_u.replace(tzinfo=None)
    sl = df[(df["open_time"] >= entry_naive) & (df["open_time"] < end_naive)]
    expected = horizon_minutes
    got = len(sl)
    gaps = 0
    if got >= 2:
        diffs = sl["open_time"].diff().dt.total_seconds().div(60).dropna()
        gaps = int((diffs > 1.5).sum())
    complete = got >= int(expected * 0.95)
    meta = {
        "coverage": "COMPLETE" if complete else "INCOMPLETE_FUTURE_COVERAGE",
        "expected_minutes": expected,
        "actual_minutes": got,
        "gap_count": gaps,
        "window_start": entry_u.isoformat(),
        "window_end": end_u.isoformat(),
    }
    return sl, meta


def compute_horizon_metrics(
    chunk: pd.DataFrame,
    *,
    entry_at: datetime,
    entry_price: float,
    direction: str,
    coverage_meta: dict[str, Any],
) -> dict[str, Any]:
    """MFE/MAE/close/first-extreme for one horizon window."""
    entry_u = _utc(entry_at)
    ep = float(entry_price)
    bull = str(direction).upper() == SetupDirection.BULLISH.value
    out: dict[str, Any] = dict(coverage_meta)
    if chunk.empty or ep <= 0:
        out.update(
            {
                "status": "NO_DATA",
                "mfe_pct": None,
                "mae_pct": None,
                "close_return_pct": None,
                "first_extreme": "FLAT",
            }
        )
        return out

    highs = chunk["high"].astype(float)
    lows = chunk["low"].astype(float)
    closes = chunk["close"].astype(float)
    times = pd.to_datetime(chunk["open_time"])

    if bull:
        mfe_pct = float((highs.max() - ep) / ep * 100.0)
        mae_pct = float((ep - lows.min()) / ep * 100.0)
        mfe_idx = int(highs.values.argmax())
        mae_idx = int(lows.values.argmin())
        close_signed = float((closes.iloc[-1] - ep) / ep * 100.0)
    else:
        mfe_pct = float((ep - lows.min()) / ep * 100.0)
        mae_pct = float((highs.max() - ep) / ep * 100.0)
        mfe_idx = int(lows.values.argmin())
        mae_idx = int(highs.values.argmax())
        close_signed = float((ep - closes.iloc[-1]) / ep * 100.0)

    mfe_at = _utc(times.iloc[mfe_idx].to_pydatetime().replace(tzinfo=timezone.utc))
    mae_at = _utc(times.iloc[mae_idx].to_pydatetime().replace(tzinfo=timezone.utc))
    min_mfe = _minutes_between(entry_u, mfe_at)
    min_mae = _minutes_between(entry_u, mae_at)

    if mfe_pct <= 1e-12 and mae_pct <= 1e-12:
        first_ext = "FLAT"
    elif abs(min_mfe - min_mae) < 0.5:
        first_ext = "SAME_MINUTE"
    elif min_mfe < min_mae:
        first_ext = "MFE_FIRST"
    else:
        first_ext = "MAE_FIRST"

    out.update(
        {
            "status": "OK" if coverage_meta.get("coverage") == "COMPLETE" else "PARTIAL",
            "mfe_pct": round(mfe_pct, 6),
            "mfe_at": mfe_at.isoformat(),
            "minutes_to_mfe": round(min_mfe, 3),
            "mae_pct": round(mae_pct, 6),
            "mae_at": mae_at.isoformat(),
            "minutes_to_mae": round(min_mae, 3),
            "close_return_pct": round(close_signed, 6),
            "close_return_signed_pct": round(close_signed, 6),
            "first_extreme": first_ext,
        }
    )
    return out


def compute_first_hit_matrix(
    chunk: pd.DataFrame,
    *,
    entry_price: float,
    direction: str,
) -> dict[str, Any]:
    ep = float(entry_price)
    bull = str(direction).upper() == SetupDirection.BULLISH.value
    matrix: dict[str, Any] = {}
    if chunk.empty or ep <= 0:
        for t in FIRST_HIT_THRESHOLDS_PCT:
            matrix[f"{t:.2f}"] = "NEITHER"
        return matrix

    for thresh in FIRST_HIT_THRESHOLDS_PCT:
        frac = thresh / 100.0
        result = "NEITHER"
        for _, row in chunk.iterrows():
            hi, lo = float(row["high"]), float(row["low"])
            if bull:
                hit_t = hi >= ep * (1.0 + frac)
                hit_a = lo <= ep * (1.0 - frac)
            else:
                hit_t = lo <= ep * (1.0 - frac)
                hit_a = hi >= ep * (1.0 + frac)
            if hit_t and hit_a:
                result = "SAME_MINUTE_AMBIGUOUS"
                break
            if hit_t:
                result = "TARGET_FIRST"
                break
            if hit_a:
                result = "ADVERSE_FIRST"
                break
        matrix[f"{thresh:.2f}"] = result
    return matrix


def ema_side(ema9: float | None, ema59: float | None) -> str | None:
    if ema9 is None or ema59 is None:
        return None
    if ema9 > ema59:
        return "ABOVE"
    if ema9 < ema59:
        return "BELOW"
    return "EQUAL"


def structure_intact(direction: str, e9: float | None, e20: float | None, e59: float | None) -> bool | None:
    if None in (e9, e20, e59):
        return None
    if str(direction).upper() == SetupDirection.BULLISH.value:
        return bool(e9 > e59 and e20 > e59)
    return bool(e9 < e59 and e20 < e59)


def lookup_ema_at(
    strategy_candles: pd.DataFrame | None,
    ts: datetime,
) -> dict[str, Any]:
    if strategy_candles is None or strategy_candles.empty:
        return {}
    df = strategy_candles.sort_values("open_time").copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    t = pd.Timestamp(_utc(ts).replace(tzinfo=None))
    hit = df[df["open_time"] == t]
    if hit.empty:
        later = df[df["open_time"] <= t]
        if later.empty:
            return {}
        hit = later.tail(1)
    row = hit.iloc[0]
    keys = ("ema_9", "ema_20", "ema_59", "ema_9_slope_1", "ema_20_slope_1", "ema_59_slope_1")
    out = {k: (float(row[k]) if k in row and pd.notna(row[k]) else None) for k in keys}
    return out


def find_conservative_entry(
    event: dict[str, Any],
    strategy_candles: pd.DataFrame | None,
) -> dict[str, Any]:
    """Next open after EMA9 returns to trend side of EMA59 post-confirmation."""
    conf = _parse_ts(event.get("confirmation_at"))
    direction = str(event.get("direction") or "")
    if conf is None or strategy_candles is None or strategy_candles.empty:
        return {"status": "NO_CONSERVATIVE_ENTRY", "reason": "missing_confirmation_or_candles"}
    if "ema_9" not in strategy_candles.columns or "ema_59" not in strategy_candles.columns:
        return {"status": "NO_CONSERVATIVE_ENTRY", "reason": "missing_ema_columns"}

    df = strategy_candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    conf_naive = conf.replace(tzinfo=None)
    bull = direction.upper() == SetupDirection.BULLISH.value
    expire_raw = event.get("expire_bars") or 24
    try:
        bar_m = int(str(event.get("strategy_timeframe") or "5m").replace("m", ""))
    except ValueError:
        bar_m = 5
    expire_deadline = conf + timedelta(minutes=int(expire_raw) * bar_m)

    idxs = df.index[df["open_time"] >= conf_naive]
    for i in idxs:
        row = df.loc[i]
        t_bar = _utc(row["open_time"].to_pydatetime().replace(tzinfo=timezone.utc))
        if t_bar > expire_deadline:
            break
        e9, e59 = row.get("ema_9"), row.get("ema_59")
        if pd.isna(e9) or pd.isna(e59):
            continue
        ok = float(e9) > float(e59) if bull else float(e9) < float(e59)
        if not ok:
            continue
        if i + 1 >= len(df):
            return {"status": "NO_CONSERVATIVE_ENTRY", "reason": "no_next_open"}
        nxt = df.iloc[i + 1]
        entry_at = _utc(nxt["open_time"].to_pydatetime().replace(tzinfo=timezone.utc))
        return {
            "status": "FOUND",
            "entry_at": entry_at.isoformat(),
            "entry_price": float(nxt["open"]),
            "ema_cross_bar": t_bar.isoformat(),
            "ema_9": float(e9),
            "ema_59": float(e59),
        }
    return {"status": "NO_CONSERVATIVE_ENTRY", "reason": "no_ema9_reclaim_in_episode"}


def analyze_single_entry(
    event: dict[str, Any],
    candles_1m: pd.DataFrame,
    *,
    entry_variant: str,
    entry_at: datetime,
    entry_price: float,
    strategy_candles: pd.DataFrame | None = None,
) -> dict[str, Any]:
    direction = str(event.get("direction") or "")
    conf_ts = _parse_ts(event.get("confirmation_at"))
    ema_conf = lookup_ema_at(strategy_candles, conf_ts) if conf_ts else {}
    ema_ent = lookup_ema_at(strategy_candles, entry_at)

    row: dict[str, Any] = {
        "event_id": event.get("event_id"),
        "entry_variant": entry_variant,
        "direction": direction,
        "symbol": event.get("symbol"),
        "strategy_timeframe": event.get("strategy_timeframe"),
        "cluster_id": event.get("cluster_id"),
        "cluster_side": event.get("cluster_side"),
        "pool_count": event.get("cluster_pool_count"),
        "confirmation_type": event.get("confirmation_type"),
        "confirmation_at": event.get("confirmation_at"),
        "entry_at": entry_at.isoformat(),
        "entry_price": float(entry_price),
        "cluster_low": event.get("cluster_low"),
        "cluster_high": event.get("cluster_high"),
        "prior_touch_count": event.get("prior_touch_count"),
        "orderflow_coverage": event.get("orderflow_coverage"),
        "ema9_at_confirm": ema_conf.get("ema_9"),
        "ema20_at_confirm": ema_conf.get("ema_20"),
        "ema59_at_confirm": ema_conf.get("ema_59"),
        "ema20_structure_intact_at_confirm": structure_intact(
            direction, ema_conf.get("ema_9"), ema_conf.get("ema_20"), ema_conf.get("ema_59")
        ),
        "ema9_side_at_confirm": ema_side(ema_conf.get("ema_9"), ema_conf.get("ema_59")),
        "ema9_slope_at_confirm": ema_conf.get("ema_9_slope_1"),
        "ema20_slope_at_confirm": ema_conf.get("ema_20_slope_1"),
        "ema59_slope_at_confirm": ema_conf.get("ema_59_slope_1"),
        "ema9_at_entry": ema_ent.get("ema_9"),
        "ema20_at_entry": ema_ent.get("ema_20"),
        "ema59_at_entry": ema_ent.get("ema_59"),
        "ema20_structure_intact_at_entry": structure_intact(
            direction, ema_ent.get("ema_9"), ema_ent.get("ema_20"), ema_ent.get("ema_59")
        ),
        "ema9_side_at_entry": ema_side(ema_ent.get("ema_9"), ema_ent.get("ema_59")),
        "ema9_slope_at_entry": ema_ent.get("ema_9_slope_1"),
        "ema20_slope_at_entry": ema_ent.get("ema_20_slope_1"),
        "ema59_slope_at_entry": ema_ent.get("ema_59_slope_1"),
    }

    for minutes, label in zip(HORIZONS_MINUTES, ("1h", "4h"), strict=True):
        chunk, cov = slice_future_1m(candles_1m, entry_at, minutes)
        metrics = compute_horizon_metrics(
            chunk,
            entry_at=entry_at,
            entry_price=entry_price,
            direction=direction,
            coverage_meta=cov,
        )
        fh = compute_first_hit_matrix(chunk, entry_price=entry_price, direction=direction)
        prefix = label
        row[f"mfe_{prefix}_pct"] = metrics.get("mfe_pct")
        row[f"mfe_{prefix}_at"] = metrics.get("mfe_at")
        row[f"minutes_to_mfe_{prefix}"] = metrics.get("minutes_to_mfe")
        row[f"mae_{prefix}_pct"] = metrics.get("mae_pct")
        row[f"mae_{prefix}_at"] = metrics.get("mae_at")
        row[f"minutes_to_mae_{prefix}"] = metrics.get("minutes_to_mae")
        row[f"close_return_{prefix}_pct"] = metrics.get("close_return_pct")
        row[f"first_extreme_{prefix}"] = metrics.get("first_extreme")
        row[f"coverage_{prefix}"] = metrics.get("coverage")
        row[f"first_hit_{prefix}"] = fh

    return row


def eligible_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if str(e.get("final_status") or "") != "CONFIRMED":
            continue
        if not e.get("entry_at") or e.get("entry_price") in (None, ""):
            continue
        out.append(e)
    return out


def flag_overlap_fields(raw_outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = []
    for r in raw_outcomes:
        ent = _parse_ts(r.get("entry_at"))
        if ent is None:
            parsed.append((r, None, None))
            continue
        parsed.append((r, ent, ent + timedelta(hours=4)))

    for i, (r, ent_i, end_i) in enumerate(parsed):
        if ent_i is None:
            r["overlapping_outcome"] = False
            r["same_cluster_family"] = False
            r["previous_entry_still_in_horizon"] = False
            continue
        overlap = False
        same_fam = False
        prev_in = False
        for j, (o, ent_j, end_j) in enumerate(parsed):
            if i == j or ent_j is None:
                continue
            if ent_i < end_j and ent_j < end_i:
                overlap = True
            if _cluster_overlap(r, o):
                same_fam = True
            if j < i and ent_i < end_j:
                prev_in = True
        r["overlapping_outcome"] = overlap
        r["same_cluster_family"] = same_fam
        r["previous_entry_still_in_horizon"] = prev_in
    return raw_outcomes


def group_episodes(raw_outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic cluster-family episodes from RAW aggressive entries."""
    aggressive = [r for r in raw_outcomes if r.get("entry_variant") == "AGGRESSIVE"]
    aggressive.sort(key=lambda x: x.get("entry_at") or "")
    episodes: list[dict[str, Any]] = []
    used: set[str] = set()

    for r in aggressive:
        eid = str(r.get("event_id"))
        if eid in used:
            continue
        fam = [r]
        used.add(eid)
        ent0 = _parse_ts(r.get("entry_at"))
        for o in aggressive:
            oid = str(o.get("event_id"))
            if oid in used:
                continue
            if str(r.get("direction")) != str(o.get("direction")):
                continue
            if not _cluster_overlap(r, o):
                continue
            ent_o = _parse_ts(o.get("entry_at"))
            if ent0 and ent_o and abs((ent_o - ent0).total_seconds()) > 4 * 3600:
                continue
            fam.append(o)
            used.add(oid)
        fam.sort(key=lambda x: x.get("entry_at") or "")
        first = fam[0]
        ep_key = "|".join(
            [
                str(first.get("symbol")),
                str(first.get("direction")),
                str(first.get("cluster_id") or first.get("cluster_low")),
                str(first.get("entry_at")),
            ]
        )
        episode_id = "csep:" + hashlib.sha1(ep_key.encode()).hexdigest()[:16]
        conf_types = [str(x.get("confirmation_type") or "") for x in fam]
        strongest = max(conf_types, key=lambda c: len(c))
        episodes.append(
            {
                "episode_id": episode_id,
                "symbol": first.get("symbol"),
                "direction": first.get("direction"),
                "number_of_events": len(fam),
                "event_ids": [x.get("event_id") for x in fam],
                "cluster_ids": sorted({str(x.get("cluster_id") or "") for x in fam}),
                "first_entry_at": fam[0].get("entry_at"),
                "first_confirmation_at": min(
                    (x.get("confirmation_at") for x in fam if x.get("confirmation_at")),
                    default=None,
                ),
                "strongest_confirmation_type": strongest,
                "first_event": fam[0],
                "later_confirmations": [x.get("confirmation_at") for x in fam[1:]],
            }
        )
    return episodes


def _percentiles(vals: list[float], ps: tuple[float, ...] = (25, 75)) -> dict[str, float]:
    if not vals:
        return {}
    s = sorted(vals)
    out = {}
    for p in ps:
        k = (len(s) - 1) * p / 100.0
        f = int(k)
        c = min(f + 1, len(s) - 1)
        out[f"p{int(p)}"] = s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)
    return out


def aggregate_bucket(rows: list[dict[str, Any]], *, horizon: str) -> dict[str, Any]:
    mfe_key = f"mfe_{horizon}_pct"
    mae_key = f"mae_{horizon}_pct"
    close_key = f"close_return_{horizon}_pct"
    cov_key = f"coverage_{horizon}"
    mfe_vals = [float(r[mfe_key]) for r in rows if r.get(mfe_key) is not None]
    mae_vals = [float(r[mae_key]) for r in rows if r.get(mae_key) is not None]
    close_vals = [float(r[close_key]) for r in rows if r.get(close_key) is not None]
    n = len(rows)
    if not mfe_vals:
        return {"n": n, "SMALL_SAMPLE": n < SMALL_SAMPLE_N}
    gt = sum(
        1
        for r in rows
        if r.get(mfe_key) is not None
        and r.get(mae_key) is not None
        and float(r[mfe_key]) > float(r[mae_key])
    )
    pct = _percentiles(mfe_vals)
    pmae = _percentiles(mae_vals)
    first_hits: dict[str, dict[str, int]] = {}
    fh_key = f"first_hit_{horizon}"
    for r in rows:
        fh = r.get(fh_key) or {}
        for thresh, label in fh.items():
            first_hits.setdefault(thresh, {})
            first_hits[thresh][label] = first_hits[thresh].get(label, 0) + 1
    return {
        "n": n,
        "SMALL_SAMPLE": n < SMALL_SAMPLE_N,
        "median_mfe_pct": statistics.median(mfe_vals),
        "mean_mfe_pct": statistics.mean(mfe_vals),
        "p25_mfe_pct": pct.get("p25"),
        "p75_mfe_pct": pct.get("p75"),
        "median_mae_pct": statistics.median(mae_vals) if mae_vals else None,
        "mean_mae_pct": statistics.mean(mae_vals) if mae_vals else None,
        "p25_mae_pct": pmae.get("p25"),
        "p75_mae_pct": pmae.get("p75"),
        "median_close_return_pct": statistics.median(close_vals) if close_vals else None,
        "pct_mfe_gt_mae": gt / n if n else None,
        "coverage_complete": sum(1 for r in rows if r.get(cov_key) == "COMPLETE"),
        "coverage_incomplete": sum(1 for r in rows if r.get(cov_key) != "COMPLETE"),
        "overlapping_outcome_count": sum(1 for r in rows if r.get("overlapping_outcome")),
        "first_hit_distribution": first_hits,
    }


def build_summary(raw_outcomes: list[dict[str, Any]], episodes: list[dict[str, Any]]) -> dict[str, Any]:
    agg: dict[str, Any] = {"raw": {}, "episodes_first_entry": {}}
    aggressive = [r for r in raw_outcomes if r.get("entry_variant") == "AGGRESSIVE"]
    conservative = [r for r in raw_outcomes if r.get("entry_variant") == "CONSERVATIVE"]
    for scope, rows in (
        ("all_raw", raw_outcomes),
        ("aggressive", aggressive),
        ("conservative", conservative),
        ("bullish", [r for r in aggressive if r.get("direction") == "BULLISH"]),
        ("bearish", [r for r in aggressive if r.get("direction") == "BEARISH"]),
    ):
        agg["raw"][scope] = {
            "1h": aggregate_bucket(rows, horizon="1h"),
            "4h": aggregate_bucket(rows, horizon="4h"),
        }
    first_entries = [ep["first_event"] for ep in episodes if ep.get("first_event")]
    agg["episodes_first_entry"]["all"] = {
        "1h": aggregate_bucket(first_entries, horizon="1h"),
        "4h": aggregate_bucket(first_entries, horizon="4h"),
    }
    return agg


def analyze_events_outcomes(
    events: Sequence[dict[str, Any]],
    candles_1m: pd.DataFrame,
    *,
    symbol: str,
    strategy_timeframe: str,
    strategy_candles: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Full RAW + conservative + overlap + episode pass."""
    rows: list[dict[str, Any]] = []
    for ev in eligible_events(events):
        ev = dict(ev)
        ev.setdefault("symbol", symbol)
        ev.setdefault("strategy_timeframe", strategy_timeframe)
        entry_at = _parse_ts(ev.get("entry_at"))
        try:
            entry_px = float(ev.get("entry_price"))
        except (TypeError, ValueError):
            continue
        if entry_at is None or entry_px <= 0:
            continue
        agg_row = analyze_single_entry(
            ev,
            candles_1m,
            entry_variant="AGGRESSIVE",
            entry_at=entry_at,
            entry_price=entry_px,
            strategy_candles=strategy_candles,
        )
        rows.append(agg_row)

        cons = find_conservative_entry(ev, strategy_candles)
        if cons.get("status") == "FOUND":
            rows.append(
                analyze_single_entry(
                    ev,
                    candles_1m,
                    entry_variant="CONSERVATIVE",
                    entry_at=_parse_ts(cons["entry_at"]),
                    entry_price=float(cons["entry_price"]),
                    strategy_candles=strategy_candles,
                )
            )
        else:
            rows.append(
                {
                    "event_id": ev.get("event_id"),
                    "entry_variant": "CONSERVATIVE",
                    "direction": ev.get("direction"),
                    "status": "NO_CONSERVATIVE_ENTRY",
                    "reason": cons.get("reason"),
                }
            )

    aggressive_rows = [r for r in rows if r.get("entry_variant") == "AGGRESSIVE"]
    conservative_rows = [r for r in rows if r.get("entry_variant") == "CONSERVATIVE"]
    flag_overlap_fields(aggressive_rows)
    cons_with_mfe = [r for r in conservative_rows if r.get("mfe_1h_pct") is not None]
    if cons_with_mfe:
        flag_overlap_fields(cons_with_mfe)
    all_rows = aggressive_rows + conservative_rows
    episodes = group_episodes(aggressive_rows)
    summary = build_summary(
        [r for r in all_rows if r.get("entry_variant") in ("AGGRESSIVE", "CONSERVATIVE") and r.get("mfe_1h_pct") is not None],
        episodes,
    )
    return {
        "events_outcomes": all_rows,
        "episodes": episodes,
        "summary": summary,
        "formulas": {
            "horizon": "[entry_at, entry_at + horizon)",
            "long_mfe_pct": "(max_high - entry) / entry * 100",
            "long_mae_pct": "(entry - min_low) / entry * 100",
            "short_mfe_pct": "(entry - min_low) / entry * 100",
            "short_mae_pct": "(max_high - entry) / entry * 100",
            "profitability_claim": False,
        },
    }


def attach_outcomes_to_events(events: Sequence[dict[str, Any]], outcomes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        if o.get("entry_variant") != "AGGRESSIVE":
            continue
        by_id[str(o.get("event_id"))] = o
    merged = []
    for e in events:
        m = dict(e)
        o = by_id.get(str(e.get("event_id")))
        if o:
            m["outcomes_1h_4h"] = o
        merged.append(m)
    return merged


def write_export_bundle(
    bundle: dict[str, Any],
    out_dir: str | Path,
    *,
    run_config: dict[str, Any] | None = None,
) -> dict[str, str]:
    from pathlib import Path

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    events = bundle.get("events_outcomes") or []
    pd.DataFrame(events).to_csv(root / "events_outcomes.csv", index=False)
    paths["events_outcomes.csv"] = str(root / "events_outcomes.csv")
    (root / "events_outcomes.json").write_text(
        json.dumps(events, indent=2, default=str) + "\n", encoding="utf-8"
    )
    paths["events_outcomes.json"] = str(root / "events_outcomes.json")
    ep_rows = []
    for ep in bundle.get("episodes") or []:
        fe = ep.get("first_event") or {}
        ep_rows.append({**{k: v for k, v in ep.items() if k != "first_event"}, **fe})
    pd.DataFrame(ep_rows).to_csv(root / "episodes_outcomes.csv", index=False)
    paths["episodes_outcomes.csv"] = str(root / "episodes_outcomes.csv")
    (root / "summary.json").write_text(
        json.dumps(bundle.get("summary") or {}, indent=2, default=str) + "\n", encoding="utf-8"
    )
    paths["summary.json"] = str(root / "summary.json")
    if run_config:
        (root / "run_config.json").write_text(
            json.dumps(run_config, indent=2, default=str) + "\n", encoding="utf-8"
        )
        paths["run_config.json"] = str(root / "run_config.json")
    cov = {
        "candle_source": run_config.get("candle_source") if run_config else None,
        "n_events_analyzed": len([e for e in events if e.get("entry_variant") == "AGGRESSIVE"]),
    }
    (root / "coverage.json").write_text(json.dumps(cov, indent=2) + "\n", encoding="utf-8")
    paths["coverage.json"] = str(root / "coverage.json")
    md = _summary_markdown(bundle)
    (root / "summary.md").write_text(md, encoding="utf-8")
    paths["summary.md"] = str(root / "summary.md")
    return paths


def _summary_markdown(bundle: dict[str, Any]) -> str:
    lines = ["# Cluster Sweep 1h/4h Outcome Summary", "", "No profitability claim.", ""]
    summary = bundle.get("summary") or {}
    for scope, horizons in (summary.get("raw") or {}).items():
        lines.append(f"## RAW scope: {scope}")
        for hz, stats in horizons.items():
            lines.append(f"### {hz}")
            lines.append("```json")
            lines.append(json.dumps(stats, indent=2, default=str))
            lines.append("```")
        lines.append("")
    return "\n".join(lines)
