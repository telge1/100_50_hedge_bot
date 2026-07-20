"""C3.5D protected-break post-path audit (offline, research-only).

For each fill with a first ``entry_protected_level_broken_event`` (D2 close-strict
break), measures loss-at-break vs additional adverse after break, protected
wick-retest / close-reclaim, and entry recovery — on fill horizons (h24/h48/h96),
post-break windows (post24/post48/post96), and full path. Scopes stay separate.

No D3 runtime, no exit rules, no Pine mutation of live SM, no live bot.
Does not modify D1/D2/C3.4B modules.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5d_apt_raw_audit import build_apt_d1_frame

PHASE = "C3.5D_PROTECTED_BREAK_PATH_AUDIT"
DEFAULT_APT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = DEFAULT_APT_DIR / "protected_break_path"

# Approved APT preview anchors (parity gate).
PREVIEW_N_BREAKS = 18
PREVIEW_H24_ENTRY_CLOSE_RATE = 0.0
PREVIEW_H24_PROT_CLOSE_RECLAIM_RATE = 11 / 18
PREVIEW_FULL_ENTRY_CLOSE_RATE = 1.0
PREVIEW_MEDIAN_BARS_FILL_TO_BREAK = 13.0
PREVIEW_MEDIAN_SIGNED_AT_BREAK_PCT = -2.809318688233533

LOSS_BUCKETS: tuple[tuple[str, float | None, float], ...] = (
    ("0_to_-1", -1.0, 0.0),
    ("-1_to_-2", -2.0, -1.0),
    ("-2_to_-3", -3.0, -2.0),
    ("-3_to_-5", -5.0, -3.0),
    ("worse_than_-5", None, -5.0),
)

FILL_HORIZONS: tuple[tuple[str, int], ...] = (("h24", 24), ("h48", 48), ("h96", 96))
POST_HORIZONS: tuple[tuple[str, int], ...] = (("post24", 24), ("post48", 48), ("post96", 96))
ALL_SCOPES: tuple[str, ...] = (
    "h24",
    "h48",
    "h96",
    "post24",
    "post48",
    "post96",
    "full",
)


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _safe_rate(n: int, d: int) -> float | None:
    return None if d <= 0 else float(n) / float(d)


def _median(xs: Sequence[float]) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.median(vals)) if vals else None


def _mean(xs: Sequence[float]) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.mean(vals)) if vals else None


def _quantile(xs: Sequence[float], q: float) -> float | None:
    vals = [float(v) for v in xs if v is not None and math.isfinite(float(v))]
    return float(np.quantile(vals, q)) if vals else None


def _as_bool_series(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s.fillna(False)
    return s.astype(str).str.lower().isin(["1", "true", "yes"])


def loss_bucket(signed_return_pct_at_break: float) -> str:
    """Bucket by signed return at break (more negative = worse)."""
    s = float(signed_return_pct_at_break)
    if not math.isfinite(s):
        return "unknown"
    if s > -1.0:
        return "0_to_-1"
    if s > -2.0:
        return "-1_to_-2"
    if s > -3.0:
        return "-2_to_-3"
    if s > -5.0:
        return "-3_to_-5"
    return "worse_than_-5"


def signed_return_pct(*, side: int, entry: float, close: float) -> float:
    if side > 0:
        return (close / entry - 1.0) * 100.0
    return (1.0 - close / entry) * 100.0


def dist_entry_to_protected_pct(*, side: int, entry: float, prot: float) -> float:
    if side > 0:
        return (entry - prot) / entry * 100.0
    return (prot - entry) / entry * 100.0


def mfe_mae_pct_to_break(
    path: pd.DataFrame,
    *,
    side: int,
    entry: float,
) -> tuple[float, float]:
    highs = path["high"].astype(float)
    lows = path["low"].astype(float)
    if side > 0:
        mfe = (float(highs.max()) / entry - 1.0) * 100.0
        mae = (float(lows.min()) / entry - 1.0) * 100.0
    else:
        mfe = (1.0 - float(lows.min()) / entry) * 100.0
        mae = (1.0 - float(highs.max()) / entry) * 100.0
    return float(mfe), float(mae)


def first_wick_break_bar(
    ohlc: pd.DataFrame,
    *,
    side: int,
    fill_bar: int,
    break_bar: int,
    prot: float,
) -> int | None:
    for bi in range(int(fill_bar), int(break_bar) + 1):
        if bi not in ohlc.index:
            continue
        row = ohlc.loc[bi]
        if side > 0 and float(row["low"]) < prot:
            return int(bi)
        if side < 0 and float(row["high"]) > prot:
            return int(bi)
    return None


def end_bar_for_scope(
    *,
    scope: str,
    fill_bar: int,
    break_bar: int,
    data_end: int,
) -> int:
    if scope == "full":
        return int(data_end)
    for name, n in FILL_HORIZONS:
        if scope == name:
            return int(min(data_end, fill_bar + n - 1))
    for name, n in POST_HORIZONS:
        if scope == name:
            return int(min(data_end, break_bar + n))
    raise KeyError(scope)


def analyze_post_path(
    ohlc: pd.DataFrame,
    *,
    side: int,
    entry: float,
    prot: float,
    atr: float,
    fill_bar: int,
    break_bar: int,
    end_bar: int,
    close_at_break: float,
    mae_pct_to_break: float,
) -> dict[str, Any]:
    """Post-break metrics on bars (break_bar+1 .. end_bar). Contiguous-beyond includes break bar."""
    out: dict[str, Any] = {
        "n_post_bars": 0,
        "add_adverse_pct": float("nan"),
        "add_adverse_atr": float("nan"),
        "total_max_adverse_pct": float(mae_pct_to_break),
        "bar_of_max_add_adverse": None,
        "bars_break_to_max_add_adverse": None,
        "prot_wick_retest": False,
        "prot_close_reclaim": False,
        "bars_to_prot_wick_retest": None,
        "bars_to_prot_close_reclaim": None,
        "bars_continuously_beyond": 0,
        "failed_reclaim_attempts": 0,
        "reclaim_lost_again": False,
        "entry_wick_recovery": False,
        "entry_close_recovery": False,
        "bars_to_entry_wick": None,
        "bars_to_entry_close": None,
        "max_plus_after_entry_close_pct": float("nan"),
        "final_signed_pct": float("nan"),
        # Explicit: protected reclaim is NOT full recovery.
        "protected_reclaim_is_not_full_recovery": True,
    }

    last = int(end_bar) if int(end_bar) >= int(break_bar) else int(break_bar)
    contig = 0
    for bi in range(int(break_bar), last + 1):
        if bi not in ohlc.index:
            break
        c = float(ohlc.loc[bi, "close"])
        beyond = (c < prot) if side > 0 else (c > prot)
        if beyond:
            contig += 1
        else:
            break
    out["bars_continuously_beyond"] = int(contig)

    post_idx = [bi for bi in range(int(break_bar) + 1, int(end_bar) + 1) if bi in ohlc.index]
    out["n_post_bars"] = int(len(post_idx))
    signed_break = signed_return_pct(side=side, entry=entry, close=close_at_break)
    if not post_idx:
        out["final_signed_pct"] = float(signed_break)
        return out

    highs = np.array([float(ohlc.loc[bi, "high"]) for bi in post_idx], dtype=float)
    lows = np.array([float(ohlc.loc[bi, "low"]) for bi in post_idx], dtype=float)
    closes = np.array([float(ohlc.loc[bi, "close"]) for bi in post_idx], dtype=float)

    to_break = ohlc.loc[int(fill_bar) : int(break_bar)]
    if side > 0:
        idx = int(np.argmin(lows))
        add = (float(lows[idx]) - close_at_break) / entry * 100.0
        total = (min(float(to_break["low"].min()), float(lows.min())) / entry - 1.0) * 100.0
        wick_i = np.where(highs >= prot)[0]
        crec_i = np.where(closes >= prot)[0]
        ew_i = np.where(highs >= entry)[0]
        ec_i = np.where(closes >= entry)[0]
    else:
        idx = int(np.argmax(highs))
        add = (close_at_break - float(highs[idx])) / entry * 100.0
        total = (1.0 - max(float(to_break["high"].max()), float(highs.max())) / entry) * 100.0
        wick_i = np.where(lows <= prot)[0]
        crec_i = np.where(closes <= prot)[0]
        ew_i = np.where(lows <= entry)[0]
        ec_i = np.where(closes <= entry)[0]

    atr_v = _finite(atr)
    out["add_adverse_pct"] = float(add)
    out["add_adverse_atr"] = float(add / 100.0 * entry / atr_v) if atr_v > 0 else float("nan")
    out["total_max_adverse_pct"] = float(total)
    out["bar_of_max_add_adverse"] = int(post_idx[idx])
    out["bars_break_to_max_add_adverse"] = int(post_idx[idx] - break_bar)

    if len(wick_i):
        out["prot_wick_retest"] = True
        out["bars_to_prot_wick_retest"] = int(post_idx[int(wick_i[0])] - break_bar)
    if len(crec_i):
        out["prot_close_reclaim"] = True
        out["bars_to_prot_close_reclaim"] = int(post_idx[int(crec_i[0])] - break_bar)

    state_beyond = True
    failed = 0
    seen_reclaim = False
    reclaim_lost = False
    for c in closes:
        beyond = (c < prot) if side > 0 else (c > prot)
        if state_beyond and not beyond:
            seen_reclaim = True
            state_beyond = False
        elif (not state_beyond) and beyond:
            if seen_reclaim:
                reclaim_lost = True
                failed += 1
            state_beyond = True
    out["failed_reclaim_attempts"] = int(failed)
    out["reclaim_lost_again"] = bool(reclaim_lost)

    if len(ew_i):
        out["entry_wick_recovery"] = True
        out["bars_to_entry_wick"] = int(post_idx[int(ew_i[0])] - break_bar)
    if len(ec_i):
        out["entry_close_recovery"] = True
        out["bars_to_entry_close"] = int(post_idx[int(ec_i[0])] - break_bar)
        i0 = int(ec_i[0])
        if side > 0:
            mx = (float(np.max(highs[i0:])) / entry - 1.0) * 100.0
        else:
            mx = (1.0 - float(np.min(lows[i0:])) / entry) * 100.0
        out["max_plus_after_entry_close_pct"] = float(mx)

    c_last = float(closes[-1])
    out["final_signed_pct"] = float(signed_return_pct(side=side, entry=entry, close=c_last))
    return out


def first_protected_breaks(timeline: pd.DataFrame) -> pd.DataFrame:
    tl = timeline.copy()
    if "entry_protected_level_broken_event" not in tl.columns:
        return pd.DataFrame()
    tl["entry_protected_level_broken_event"] = _as_bool_series(tl["entry_protected_level_broken_event"])
    ev = tl.loc[tl["entry_protected_level_broken_event"]].sort_values(["setup_id", "bar_index"])
    if ev.empty:
        return pd.DataFrame()
    return ev.groupby("setup_id", as_index=False).first()


def ensure_ohlc_index(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame.copy()
    if "bar_index" not in f.columns:
        f = f.reset_index(drop=True)
        f["bar_index"] = np.arange(len(f), dtype=int)
    need = {"open", "high", "low", "close"}
    missing = need - set(f.columns)
    if missing:
        raise RuntimeError(f"OHLC frame missing columns: {sorted(missing)}")
    return f.set_index("bar_index", drop=False)


def build_per_break_row(
    *,
    setup_id: int,
    direction: str,
    side: int,
    entry: float,
    prot: float,
    atr: float,
    fill_bar: int,
    break_bar: int,
    ohlc: pd.DataFrame,
    timeline_break_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if break_bar not in ohlc.index:
        raise KeyError(f"break_bar {break_bar} missing from OHLC for setup {setup_id}")
    br = ohlc.loc[int(break_bar)]
    close_b = float(br["close"])
    signed = signed_return_pct(side=side, entry=entry, close=close_b)
    atr_v = _finite(atr)
    loss_atr = (signed / 100.0 * entry / atr_v) if atr_v > 0 else float("nan")
    first_wick = first_wick_break_bar(
        ohlc, side=side, fill_bar=fill_bar, break_bar=break_bar, prot=prot
    )
    to_break = ohlc.loc[int(fill_bar) : int(break_bar)]
    mfe_pct, mae_pct = mfe_mae_pct_to_break(to_break, side=side, entry=entry)
    data_end = int(ohlc.index.max())

    rec: dict[str, Any] = {
        "setup_id": int(setup_id),
        "direction": direction,
        "side": int(side),
        "fill_bar": int(fill_bar),
        "protected_break_bar": int(break_bar),
        "bars_fill_to_break": int(break_bar - fill_bar),
        "protected_break_timestamp": str(br.get("timestamp", "")),
        "break_semantics": "close_strict",
        "wick_broke_on_or_before_close_break": first_wick is not None,
        "first_wick_break_bar": first_wick,
        "bars_wick_lead_close": (int(break_bar - first_wick) if first_wick is not None else None),
        "entry_price": float(entry),
        "entry_protected_level": float(prot),
        "frozen_atr_14": float(atr) if math.isfinite(_finite(atr)) else float("nan"),
        "signed_return_pct_at_break": float(signed),
        "loss_atr_at_break": float(loss_atr) if math.isfinite(loss_atr) else float("nan"),
        "dist_entry_to_protected_pct": float(
            dist_entry_to_protected_pct(side=side, entry=entry, prot=prot)
        ),
        "mfe_pct_to_break": float(mfe_pct),
        "mae_pct_to_break": float(mae_pct),
        "mfe_atr_to_break": (
            _finite(timeline_break_row.get("mfe_atr")) if timeline_break_row else float("nan")
        ),
        "mae_atr_to_break": (
            _finite(timeline_break_row.get("mae_atr")) if timeline_break_row else float("nan")
        ),
        "loss_bucket_at_break": loss_bucket(signed),
        "note_protected_reclaim_ne_entry_recovery": True,
    }

    for scope in ALL_SCOPES:
        end = end_bar_for_scope(
            scope=scope, fill_bar=fill_bar, break_bar=break_bar, data_end=data_end
        )
        metrics = analyze_post_path(
            ohlc,
            side=side,
            entry=entry,
            prot=prot,
            atr=atr_v,
            fill_bar=fill_bar,
            break_bar=break_bar,
            end_bar=end,
            close_at_break=close_b,
            mae_pct_to_break=mae_pct,
        )
        for k, v in metrics.items():
            rec[f"{scope}__{k}"] = v
    return rec


def build_per_fill_table(
    frame: pd.DataFrame,
    fills: pd.DataFrame,
    timeline: pd.DataFrame,
) -> pd.DataFrame:
    ohlc = ensure_ohlc_index(frame)
    first = first_protected_breaks(timeline)
    if first.empty:
        return pd.DataFrame()

    meta_cols = [
        "setup_id",
        "side",
        "direction",
        "entry_price",
        "entry_protected_level",
        "frozen_atr_14",
        "fill_bar",
    ]
    for c in meta_cols:
        if c not in fills.columns:
            raise RuntimeError(f"fills.csv missing {c}")
    first = first.merge(fills[meta_cols], on="setup_id", suffixes=("", "_fill"))

    rows: list[dict[str, Any]] = []
    for _, br in first.iterrows():
        sid = int(br["setup_id"])
        break_bar = int(br["bar_index"])
        tl_rows = timeline[(timeline["setup_id"] == sid) & (timeline["bar_index"] == break_bar)]
        tl_row = tl_rows.iloc[0].to_dict() if not tl_rows.empty else None
        rows.append(
            build_per_break_row(
                setup_id=sid,
                direction=str(br["direction"]),
                side=int(br["side"]),
                entry=float(br["entry_price"]),
                prot=float(br["entry_protected_level"]),
                atr=_finite(br["frozen_atr_14"]),
                fill_bar=int(br["fill_bar"]),
                break_bar=break_bar,
                ohlc=ohlc,
                timeline_break_row=tl_row,
            )
        )
    return pd.DataFrame(rows)


def summarize_scope(df: pd.DataFrame, *, scope: str, group: str) -> dict[str, Any]:
    pref = f"{scope}__"
    n = len(df)
    wick = df[f"{pref}prot_wick_retest"].astype(bool) if n else pd.Series(dtype=bool)
    crec = df[f"{pref}prot_close_reclaim"].astype(bool) if n else pd.Series(dtype=bool)
    ew = df[f"{pref}entry_wick_recovery"].astype(bool) if n else pd.Series(dtype=bool)
    ec = df[f"{pref}entry_close_recovery"].astype(bool) if n else pd.Series(dtype=bool)
    return {
        "scope": scope,
        "group": group,
        "n": n,
        "median_signed_return_pct_at_break": _median(df["signed_return_pct_at_break"].tolist()) if n else None,
        "median_loss_atr_at_break": _median(df["loss_atr_at_break"].tolist()) if n else None,
        "median_bars_fill_to_break": _median(df["bars_fill_to_break"].astype(float).tolist()) if n else None,
        "median_add_adverse_pct": _median(df[f"{pref}add_adverse_pct"].tolist()) if n else None,
        "p25_add_adverse_pct": _quantile(df[f"{pref}add_adverse_pct"].tolist(), 0.25) if n else None,
        "prot_wick_retest_rate": _safe_rate(int(wick.sum()), n),
        "prot_close_reclaim_rate": _safe_rate(int(crec.sum()), n),
        "median_bars_to_prot_wick_retest": _median(
            df.loc[wick, f"{pref}bars_to_prot_wick_retest"].dropna().astype(float).tolist()
        )
        if n
        else None,
        "median_bars_to_prot_close_reclaim": _median(
            df.loc[crec, f"{pref}bars_to_prot_close_reclaim"].dropna().astype(float).tolist()
        )
        if n
        else None,
        "median_bars_continuously_beyond": _median(
            df[f"{pref}bars_continuously_beyond"].astype(float).tolist()
        )
        if n
        else None,
        "reclaim_lost_again_rate": _safe_rate(int(df[f"{pref}reclaim_lost_again"].astype(bool).sum()), n)
        if n
        else None,
        "entry_wick_recovery_rate": _safe_rate(int(ew.sum()), n),
        "entry_close_recovery_rate": _safe_rate(int(ec.sum()), n),
        "median_bars_to_entry_close": _median(
            df.loc[ec, f"{pref}bars_to_entry_close"].dropna().astype(float).tolist()
        )
        if n
        else None,
        "median_max_plus_after_entry_close_pct": _median(
            df.loc[ec, f"{pref}max_plus_after_entry_close_pct"].astype(float).tolist()
        )
        if n
        else None,
        "median_final_signed_pct": _median(df[f"{pref}final_signed_pct"].tolist()) if n else None,
        "n_zero_post_bars": int((df[f"{pref}n_post_bars"] == 0).sum()) if n else 0,
        "note": "prot_close_reclaim is NOT entry recovery; scopes must not be mixed",
    }


def build_recovery_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for scope in ALL_SCOPES:
        rows.append(summarize_scope(df, scope=scope, group="all"))
        rows.append(summarize_scope(df[df["direction"] == "long"], scope=scope, group="long"))
        rows.append(summarize_scope(df[df["direction"] == "short"], scope=scope, group="short"))
    return pd.DataFrame(rows)


def build_adverse_buckets(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for scope in ALL_SCOPES:
        pref = f"{scope}__"
        for name, _lo, _hi in LOSS_BUCKETS:
            sub = df[df["loss_bucket_at_break"] == name]
            n = len(sub)
            wick = sub[f"{pref}prot_wick_retest"].astype(bool) if n else pd.Series(dtype=bool)
            crec = sub[f"{pref}prot_close_reclaim"].astype(bool) if n else pd.Series(dtype=bool)
            ec = sub[f"{pref}entry_close_recovery"].astype(bool) if n else pd.Series(dtype=bool)
            rows.append(
                {
                    "scope": scope,
                    "loss_bucket_at_break": name,
                    "n": n,
                    "share_of_breaks": _safe_rate(n, len(df)),
                    "median_add_adverse_pct": _median(sub[f"{pref}add_adverse_pct"].tolist()) if n else None,
                    "prot_wick_retest_rate": _safe_rate(int(wick.sum()), n),
                    "prot_close_reclaim_rate": _safe_rate(int(crec.sum()), n),
                    "entry_close_recovery_rate": _safe_rate(int(ec.sum()), n),
                    "median_bars_to_entry_close": _median(
                        sub.loc[ec, f"{pref}bars_to_entry_close"].dropna().astype(float).tolist()
                    )
                    if n
                    else None,
                    "median_max_plus_after_entry_close_pct": _median(
                        sub.loc[ec, f"{pref}max_plus_after_entry_close_pct"].astype(float).tolist()
                    )
                    if n
                    else None,
                }
            )
    return pd.DataFrame(rows)


def build_horizon_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    all_rows = summary[summary["group"] == "all"].copy()
    keep = [
        "scope",
        "n",
        "median_add_adverse_pct",
        "prot_wick_retest_rate",
        "prot_close_reclaim_rate",
        "entry_wick_recovery_rate",
        "entry_close_recovery_rate",
        "median_bars_to_prot_close_reclaim",
        "median_bars_to_entry_close",
        "median_final_signed_pct",
        "note",
    ]
    out = all_rows[[c for c in keep if c in all_rows.columns]].copy()
    out["horizon_axis"] = out["scope"].map(
        lambda s: "from_fill"
        if str(s).startswith("h")
        else ("from_break" if str(s).startswith("post") else "full_path")
    )
    out["bot_relevant"] = out["scope"].isin(["h24", "h48", "h96"])
    return out


def build_reclaim_events(df: pd.DataFrame) -> pd.DataFrame:
    """Long-format first events per scope (wick-retest / close-reclaim / entry recovery)."""
    rows: list[dict[str, Any]] = []
    event_specs = (
        ("protected_wick_retest", "prot_wick_retest", "bars_to_prot_wick_retest"),
        ("protected_close_reclaim", "prot_close_reclaim", "bars_to_prot_close_reclaim"),
        ("entry_wick_recovery", "entry_wick_recovery", "bars_to_entry_wick"),
        ("entry_close_recovery", "entry_close_recovery", "bars_to_entry_close"),
    )
    for _, r in df.iterrows():
        break_bar = int(r["protected_break_bar"])
        for scope in ALL_SCOPES:
            for etype, flag_col, bars_col in event_specs:
                flag = bool(r[f"{scope}__{flag_col}"])
                if not flag:
                    continue
                bars = r[f"{scope}__{bars_col}"]
                bars_i = int(bars) if pd.notna(bars) else None
                rows.append(
                    {
                        "setup_id": int(r["setup_id"]),
                        "direction": r["direction"],
                        "scope": scope,
                        "event_type": etype,
                        "protected_break_bar": break_bar,
                        "bars_since_break": bars_i,
                        "event_bar": (break_bar + bars_i) if bars_i is not None else None,
                        "signed_return_pct_at_break": float(r["signed_return_pct_at_break"]),
                        "loss_bucket_at_break": r["loss_bucket_at_break"],
                        "is_full_recovery": etype.startswith("entry_"),
                        "is_protected_only": etype.startswith("protected_"),
                        "note": (
                            "protected reclaim != entry recovery"
                            if etype.startswith("protected_")
                            else "entry recovery"
                        ),
                    }
                )
    return pd.DataFrame(rows)


def check_preview_parity(df: pd.DataFrame, *, atol: float = 1e-6) -> dict[str, Any]:
    """Gate against approved APT preview numbers."""
    n = len(df)
    h24_ec = float(df["h24__entry_close_recovery"].astype(bool).mean()) if n else float("nan")
    h24_cr = float(df["h24__prot_close_reclaim"].astype(bool).mean()) if n else float("nan")
    full_ec = float(df["full__entry_close_recovery"].astype(bool).mean()) if n else float("nan")
    med_bars = float(np.median(df["bars_fill_to_break"])) if n else float("nan")
    med_signed = float(np.median(df["signed_return_pct_at_break"])) if n else float("nan")

    checks = {
        "n_breaks": (n == PREVIEW_N_BREAKS, n, PREVIEW_N_BREAKS),
        "h24_entry_close_rate": (
            abs(h24_ec - PREVIEW_H24_ENTRY_CLOSE_RATE) <= atol,
            h24_ec,
            PREVIEW_H24_ENTRY_CLOSE_RATE,
        ),
        "h24_prot_close_reclaim_rate": (
            abs(h24_cr - PREVIEW_H24_PROT_CLOSE_RECLAIM_RATE) <= 1e-9,
            h24_cr,
            PREVIEW_H24_PROT_CLOSE_RECLAIM_RATE,
        ),
        "full_entry_close_rate": (
            abs(full_ec - PREVIEW_FULL_ENTRY_CLOSE_RATE) <= atol,
            full_ec,
            PREVIEW_FULL_ENTRY_CLOSE_RATE,
        ),
        "median_bars_fill_to_break": (
            abs(med_bars - PREVIEW_MEDIAN_BARS_FILL_TO_BREAK) <= atol,
            med_bars,
            PREVIEW_MEDIAN_BARS_FILL_TO_BREAK,
        ),
        "median_signed_return_pct_at_break": (
            abs(med_signed - PREVIEW_MEDIAN_SIGNED_AT_BREAK_PCT) <= 1e-6,
            med_signed,
            PREVIEW_MEDIAN_SIGNED_AT_BREAK_PCT,
        ),
    }
    passed = all(v[0] for v in checks.values())
    return {
        "passed": passed,
        "checks": {
            k: {"ok": ok, "got": got, "expected": exp} for k, (ok, got, exp) in checks.items()
        },
    }


def mirror_ohlc_around_entry(ohlc: pd.DataFrame, *, entry: float) -> pd.DataFrame:
    """Reflect OHLC through ``entry`` for long/short parity tests."""
    out = ohlc.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = 2.0 * entry - out[col].astype(float)
    # after reflection high/low swap roles
    hi = out["high"].copy()
    lo = out["low"].copy()
    out["high"] = np.maximum(hi, lo)
    out["low"] = np.minimum(hi, lo)
    return out


def run_audit(
    *,
    apt_dir: Path = DEFAULT_APT_DIR,
    output_dir: Path = DEFAULT_OUT,
    frame: pd.DataFrame | None = None,
    frame_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    apt_dir = Path(apt_dir)
    output_dir = Path(output_dir)
    if output_dir.resolve() == apt_dir.resolve():
        raise RuntimeError("refusing to write into apt_audit root")
    output_dir.mkdir(parents=True, exist_ok=True)

    fills = pd.read_csv(apt_dir / "fills.csv")
    timeline = pd.read_csv(apt_dir / "d2_timeline_full.csv")
    if frame is None:
        frame, _frame4h, meta = build_apt_d1_frame()
    else:
        meta = dict(frame_meta or {})

    per = build_per_fill_table(frame, fills, timeline)
    summary = build_recovery_summary(per)
    buckets = build_adverse_buckets(per)
    horizons = build_horizon_comparison(summary)
    events = build_reclaim_events(per)
    parity = check_preview_parity(per)

    per.to_csv(output_dir / "protected_break_path_per_fill.csv", index=False)
    summary.to_csv(output_dir / "protected_break_recovery_summary.csv", index=False)
    buckets.to_csv(output_dir / "protected_break_adverse_buckets.csv", index=False)
    horizons.to_csv(output_dir / "protected_break_horizon_comparison.csv", index=False)
    events.to_csv(output_dir / "protected_break_reclaim_events.csv", index=False)

    audit = {
        "phase": PHASE,
        "status": "OK" if parity["passed"] else "PARITY_MISMATCH",
        "n_fills": int(len(fills)),
        "n_protected_breaks": int(len(per)),
        "scopes": list(ALL_SCOPES),
        "definitions": {
            "break": "first entry_protected_level_broken_event (close-strict)",
            "loss_at_break": "signed_return_pct_at_break from entry close on break bar",
            "add_adverse_after_break": "further adverse from break close on post bars only",
            "prot_wick_retest": "long high>=prot / short low<=prot after break",
            "prot_close_reclaim": "long close>=prot / short close<=prot after break",
            "entry_recovery": "separate from protected reclaim",
            "h24_h48_h96": "end = fill_bar + N - 1 (from fill; bot horizons)",
            "post24_post48_post96": "end = break_bar + N (from break)",
            "full": "to data end — not mixed with bot horizons",
        },
        "preview_parity": parity,
        "summary_h24_all": summarize_scope(per, scope="h24", group="all"),
        "summary_full_all": summarize_scope(per, scope="full", group="all"),
        "summary_post24_all": summarize_scope(per, scope="post24", group="all"),
        "data_meta": {k: meta[k] for k in meta if k != "frame15_meta"} if meta else {},
        "no_d3_runtime": True,
        "no_exit_rule": True,
        "no_live_bot": True,
        "no_commit": True,
        "output_dir": str(output_dir),
    }
    (output_dir / "protected_break_path_summary.json").write_text(
        json.dumps(json_safe(audit), indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# C3.5D Protected-Break Path Audit",
                "",
                "Post-path analysis after first close-strict `entry_protected_level` break.",
                "",
                "Scopes (strictly separate):",
                "- `h24` / `h48` / `h96` — from fill (hedge-bot horizons)",
                "- `post24` / `post48` / `post96` — from break",
                "- `full` — to data end (not bot-relevant alone)",
                "",
                "Protected close-reclaim is **not** entry recovery.",
                "",
                f"- Fills: `{audit['n_fills']}`",
                f"- Protected breaks: `{audit['n_protected_breaks']}`",
                f"- Preview parity: `{parity['passed']}`",
                "",
                "No D3 runtime. No exit rule. No live bot.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return audit


def main() -> None:
    p = argparse.ArgumentParser(description="C3.5D protected-break path audit")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    audit = run_audit(apt_dir=args.apt_dir, output_dir=args.output_dir)
    print(json.dumps(json_safe(audit), indent=2))


if __name__ == "__main__":
    main()
