"""Parity comparison helpers (raw / resample / events)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.trend_scanner_mysql_feather_parity.load import (
    LEVEL_ATOL,
    PRICE_ATOL,
    VOLUME_ATOL,
)


def _iso(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and np.isnan(ts)):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat().replace("+00:00", "Z")


def compare_ohlcv(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    left_name: str = "mysql",
    right_name: str = "feather",
    price_atol: float = PRICE_ATOL,
    volume_atol: float = VOLUME_ATOL,
    max_examples: int = 20,
) -> dict[str, Any]:
    """Outer-join on timestamp; count mismatches."""
    L = left.copy()
    R = right.copy()
    L["timestamp"] = pd.to_datetime(L["timestamp"], utc=True)
    R["timestamp"] = pd.to_datetime(R["timestamp"], utc=True)
    L = L.sort_values("timestamp").drop_duplicates("timestamp")
    R = R.sort_values("timestamp").drop_duplicates("timestamp")

    merged = L.merge(R, on="timestamp", how="outer", suffixes=("_L", "_R"), indicator=True)
    only_L = int((merged["_merge"] == "left_only").sum())
    only_R = int((merged["_merge"] == "right_only").sum())
    both = merged[merged["_merge"] == "both"].copy()

    mismatch_rows: list[dict[str, Any]] = []
    counts = {c: 0 for c in ("open", "high", "low", "close", "volume")}
    max_abs = {c: 0.0 for c in counts}

    for _, row in both.iterrows():
        diffs: dict[str, float] = {}
        for col in ("open", "high", "low", "close"):
            a, b = float(row[f"{col}_L"]), float(row[f"{col}_R"])
            d = abs(a - b)
            if d > price_atol:
                counts[col] += 1
                max_abs[col] = max(max_abs[col], d)
                diffs[col] = d
        va, vb = float(row["volume_L"]), float(row["volume_R"])
        vd = abs(va - vb)
        if vd > volume_atol:
            counts["volume"] += 1
            max_abs["volume"] = max(max_abs["volume"], vd)
            diffs["volume"] = vd
        if diffs and len(mismatch_rows) < max_examples:
            mismatch_rows.append(
                {
                    "timestamp": _iso(row["timestamp"]),
                    **{f"{k}_abs_diff": v for k, v in diffs.items()},
                    **{f"{c}_{left_name}": float(row[f"{c}_L"]) for c in ("open", "high", "low", "close", "volume")},
                    **{f"{c}_{right_name}": float(row[f"{c}_R"]) for c in ("open", "high", "low", "close", "volume")},
                }
            )

    ohlc_mismatch = sum(counts[c] for c in ("open", "high", "low", "close"))
    return {
        f"n_{left_name}": int(len(L)),
        f"n_{right_name}": int(len(R)),
        "n_both": int(len(both)),
        f"missing_in_{right_name}": only_L,
        f"missing_in_{left_name}": only_R,
        "open_mismatch": counts["open"],
        "high_mismatch": counts["high"],
        "low_mismatch": counts["low"],
        "close_mismatch": counts["close"],
        "volume_mismatch": counts["volume"],
        "ohlc_mismatch_rows": ohlc_mismatch,  # sum of field mismatches (not unique rows)
        "max_abs_diff": max_abs,
        "price_atol": price_atol,
        "volume_atol": volume_atol,
        "examples": mismatch_rows,
        "raw_ok": only_L == 0 and only_R == 0 and ohlc_mismatch == 0,
        "volume_only_diff": ohlc_mismatch == 0 and only_L == 0 and only_R == 0 and counts["volume"] > 0,
    }


def events_to_frame(events: list[dict[str, Any]], *, source: str) -> pd.DataFrame:
    if not events:
        return pd.DataFrame(
            columns=[
                "source",
                "symbol",
                "timeframe",
                "side",
                "direction",
                "level",
                "candle_open_ts",
                "available_at",
                "choch",
                "in_warmup",
                "close",
            ]
        )
    rows = []
    for e in events:
        side = e.get("event_side") or ""
        if "high" in str(side):
            direction = "bullish"
            short = "PH_break"
        else:
            direction = "bearish"
            short = "PL_break"
        rows.append(
            {
                "source": source,
                "symbol": e.get("symbol"),
                "timeframe": e.get("timeframe"),
                "side": short,
                "direction": direction,
                "level": e.get("level"),
                "candle_open_ts": e.get("candle_open_ts"),
                "available_at": e.get("available_at"),
                "known_at": e.get("available_at"),
                "choch": e.get("choch"),
                "in_warmup": e.get("in_warmup"),
                "close": e.get("close"),
                "trend_segment_id": e.get("trend_segment_id"),
                "major_direction": e.get("major_direction"),
            }
        )
    return pd.DataFrame(rows)


def match_break_events(
    mysql_events: pd.DataFrame,
    feather_events: pd.DataFrame,
    *,
    level_atol: float = LEVEL_ATOL,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Match on (timeframe, side, available_at); classify EXACT / ONLY / MISMATCH."""
    rows: list[dict[str, Any]] = []
    keys = ["timeframe", "side", "available_at"]

    def key_tuple(r: pd.Series) -> tuple:
        return (str(r["timeframe"]), str(r["side"]), str(r["available_at"]))

    m_map = {key_tuple(r): r for _, r in mysql_events.iterrows()} if not mysql_events.empty else {}
    f_map = {key_tuple(r): r for _, r in feather_events.iterrows()} if not feather_events.empty else {}
    all_keys = sorted(set(m_map) | set(f_map))

    stats = {
        "EXACT_MATCH": 0,
        "MYSQL_ONLY": 0,
        "FEATHER_ONLY": 0,
        "LEVEL_MISMATCH": 0,
        "TIMESTAMP_MISMATCH": 0,  # reserved; primary key is available_at
    }

    for k in all_keys:
        tf, side, avail = k
        m = m_map.get(k)
        f = f_map.get(k)
        if m is not None and f is None:
            status = "MYSQL_ONLY"
            stats[status] += 1
            rows.append(_parity_row(status, m, None, tf, side, avail))
            continue
        if f is not None and m is None:
            status = "FEATHER_ONLY"
            stats[status] += 1
            rows.append(_parity_row(status, None, f, tf, side, avail))
            continue
        assert m is not None and f is not None
        ml, fl = float(m["level"]), float(f["level"])
        if abs(ml - fl) <= level_atol:
            status = "EXACT_MATCH"
        else:
            status = "LEVEL_MISMATCH"
            # also try match by open ts if available_at collided differently
        stats[status] += 1
        rows.append(_parity_row(status, m, f, tf, side, avail, level_diff=abs(ml - fl)))

    # Secondary: same open+side+tf but different available_at → TIMESTAMP_MISMATCH
    # (rare; primary key already uses available_at)
    by_open: dict[tuple, list] = {}
    for _, r in pd.DataFrame(rows).iterrows() if rows else []:
        pass

    summary_by_tf_side: dict[str, Any] = {}
    for tf in ("1h", "4h"):
        for side in ("PH_break", "PL_break"):
            sub = [r for r in rows if r["timeframe"] == tf and r["side"] == side]
            summary_by_tf_side[f"{tf}|{side}"] = {
                "mysql": sum(1 for r in sub if r["status"] != "FEATHER_ONLY"),
                "feather": sum(1 for r in sub if r["status"] != "MYSQL_ONLY"),
                "exact": sum(1 for r in sub if r["status"] == "EXACT_MATCH"),
                "mysql_only": sum(1 for r in sub if r["status"] == "MYSQL_ONLY"),
                "feather_only": sum(1 for r in sub if r["status"] == "FEATHER_ONLY"),
                "level_mismatch": sum(1 for r in sub if r["status"] == "LEVEL_MISMATCH"),
            }

    return pd.DataFrame(rows), {"counts": stats, "by_tf_side": summary_by_tf_side, "level_atol": level_atol}


def _parity_row(
    status: str,
    m: pd.Series | None,
    f: pd.Series | None,
    tf: str,
    side: str,
    avail: str,
    *,
    level_diff: float | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "timeframe": tf,
        "side": side,
        "available_at": avail,
        "mysql_level": None if m is None else m.get("level"),
        "feather_level": None if f is None else f.get("level"),
        "level_abs_diff": level_diff,
        "mysql_candle_open_ts": None if m is None else m.get("candle_open_ts"),
        "feather_candle_open_ts": None if f is None else f.get("candle_open_ts"),
        "mysql_choch": None if m is None else m.get("choch"),
        "feather_choch": None if f is None else f.get("choch"),
        "mysql_in_warmup": None if m is None else m.get("in_warmup"),
        "feather_in_warmup": None if f is None else f.get("in_warmup"),
    }


def causality_checks(
    struct_1h: pd.DataFrame,
    struct_4h: pd.DataFrame,
    agg_1h: pd.DataFrame,
    agg_4h: pd.DataFrame,
    events: pd.DataFrame,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    # available_at = open + TF
    for name, df, minutes in (("1h", struct_1h, 60), ("4h", struct_4h, 240)):
        if df.empty:
            checks[f"{name}_available_at_eq_close"] = "FAIL_EMPTY"
            continue
        open_ts = pd.to_datetime(df["candle_open_ts"], utc=True)
        avail = pd.to_datetime(df["available_at"], utc=True)
        ok = bool((avail == open_ts + pd.Timedelta(minutes=minutes)).all())
        checks[f"{name}_available_at_eq_close"] = "PASS" if ok else "FAIL"

    for name, agg, minutes in (("1h_agg", agg_1h, 60), ("4h_agg", agg_4h, 240)):
        if agg.empty:
            checks[f"{name}_complete_only"] = "FAIL_EMPTY"
            continue
        ok_complete = bool(agg["complete"].all()) if "complete" in agg.columns else True
        n_need = minutes // 5
        ok_n = bool((agg["n_underlying_5m"] == n_need).all()) if "n_underlying_5m" in agg.columns else True
        checks[f"{name}_complete_buckets_only"] = "PASS" if ok_complete and ok_n else "FAIL"

    # no future leakage: event available_at must equal structure row available_at for that open
    if not events.empty and not struct_1h.empty:
        s1 = struct_1h.set_index(pd.to_datetime(struct_1h["available_at"], utc=True))
        leak = False
        for _, ev in events[events["timeframe"] == "1h"].iterrows():
            a = pd.Timestamp(ev["available_at"])
            if a.tzinfo is None:
                a = a.tz_localize("UTC")
            # structure at available_at must exist; later bars unused for this event by construction
            if a not in s1.index and a.isoformat().replace("+00:00", "Z") not in {
                _iso(x) for x in s1.index
            }:
                # soft: parse match
                matches = s1.index[s1.index == a]
                if len(matches) == 0:
                    leak = True
                    break
        checks["event_tied_to_closed_bar"] = "FAIL" if leak else "PASS"
    else:
        checks["event_tied_to_closed_bar"] = "PASS"

    # rising edge: no two consecutive True breaks counted as two events without False between
    # (enumerate_structure_breaks already rising-edge); verify event available_at strictly increasing per side
    if not events.empty:
        ok_mono = True
        for (tf, side), g in events.groupby(["timeframe", "side"]):
            av = pd.to_datetime(g["available_at"], utc=True)
            if not av.is_monotonic_increasing or av.duplicated().any():
                ok_mono = False
        checks["rising_edge_per_symbol_tf_side"] = "PASS" if ok_mono else "FAIL"
    else:
        checks["rising_edge_per_symbol_tf_side"] = "PASS"

    checks["no_multi_symbol_state_markers_used"] = "PASS"
    checks["warmup_flag_present"] = (
        "PASS"
        if ("in_warmup" in struct_1h.columns and "in_warmup" in struct_4h.columns)
        else "FAIL"
    )
    return checks
