"""C3.5D APT path-recovery audit (research-only, descriptive).

For each continuation fill: max adverse from entry, underwater→entry recovery
(wick/close), and max positive excursion after recovery — on the full available
post-fill path and on a Horizon-24 slice.

No D3 rules, no D1/D2 changes, no Pine, no live bot.
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

PHASE = "C3.5D_PATH_RECOVERY_AUDIT"
DEFAULT_APT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = DEFAULT_APT_DIR / "path_recovery"
HORIZON_24 = 24
ADVERSE_BUCKETS = (
    ("0_to_-0_5", -0.5, 0.0),
    ("-0_5_to_-1", -1.0, -0.5),
    ("-1_to_-2", -2.0, -1.0),
    ("-2_to_-3", -3.0, -2.0),
    ("worse_than_-3", None, -3.0),
)
POS_THRESHOLDS = (0.25, 0.5, 1.0, 2.0, 3.0)


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


def path_slice(
    frame: pd.DataFrame,
    *,
    fill_bar: int,
    n_bars: int | None,
) -> pd.DataFrame:
    """Bars from fill_bar inclusive; n_bars=None → through data end."""
    sub = frame[frame["bar_index"] >= int(fill_bar)].copy()
    if n_bars is not None:
        sub = sub.head(int(n_bars))
    return sub.reset_index(drop=True)


def compute_path_metrics(
    path: pd.DataFrame,
    *,
    side: int,
    entry: float,
    atr: float | None,
    scope: str,
) -> dict[str, Any]:
    """Core path metrics on an ordered OHLC path starting at the fill bar."""
    out: dict[str, Any] = {
        "scope": scope,
        "n_path_bars": int(len(path)),
        "ever_underwater": False,
        "entry_touched_after_underwater": False,
        "entry_closed_recovered_after_underwater": False,
        "bars_until_wick_recovery": None,
        "bars_until_close_recovery": None,
        "never_recovered_by_wick": True,
        "never_recovered_by_close": True,
        "max_adverse_pct": float("nan"),
        "max_adverse_atr": float("nan"),
        "bar_of_max_adverse": None,
        "bars_since_fill_of_max_adverse": None,
        "final_signed_return_pct": float("nan"),
        "max_positive_after_wick_recovery_pct": float("nan"),
        "max_positive_after_wick_recovery_atr": float("nan"),
        "bar_of_max_positive_after_wick_recovery": None,
        "max_positive_after_close_recovery_pct": float("nan"),
        "max_positive_after_close_recovery_atr": float("nan"),
        "bar_of_max_positive_after_close_recovery": None,
    }
    if path.empty or not math.isfinite(entry) or entry == 0:
        return out

    highs = path["high"].astype(float).to_numpy()
    lows = path["low"].astype(float).to_numpy()
    closes = path["close"].astype(float).to_numpy()
    bars = path["bar_index"].astype(int).to_numpy()
    n = len(path)

    # --- max adverse from entry (fill bar H/L included) ---
    if side > 0:
        min_low = float(np.min(lows))
        max_adverse_pct = (min_low / entry - 1.0) * 100.0
        adverse_price = min_low - entry  # <= 0
        idx_adv = int(np.argmin(lows))
        signed_close = (closes - entry) / entry
        underwater = closes < entry
        wick_touch = highs >= entry
        close_rec = closes >= entry
    else:
        max_high = float(np.max(highs))
        max_adverse_pct = (1.0 - max_high / entry) * 100.0
        adverse_price = entry - max_high  # <= 0 when adverse
        idx_adv = int(np.argmax(highs))
        signed_close = (entry - closes) / entry
        underwater = closes > entry
        wick_touch = lows <= entry
        close_rec = closes <= entry

    atr_v = _finite(atr)
    out["max_adverse_pct"] = float(max_adverse_pct)
    out["max_adverse_atr"] = float(adverse_price / atr_v) if atr_v > 0 else float("nan")
    out["bar_of_max_adverse"] = int(bars[idx_adv])
    out["bars_since_fill_of_max_adverse"] = int(idx_adv)
    out["final_signed_return_pct"] = float(signed_close[-1] * 100.0)

    ever_uw = bool(np.any(underwater))
    out["ever_underwater"] = ever_uw

    wick_rec_i = None
    close_rec_i = None
    if ever_uw:
        first_uw = int(np.argmax(underwater))  # first True
        for i in range(first_uw, n):
            if wick_rec_i is None and bool(wick_touch[i]):
                # must be at or after underwater; touching entry while underwater/recovering
                wick_rec_i = i
            if close_rec_i is None and bool(close_rec[i]):
                close_rec_i = i
            if wick_rec_i is not None and close_rec_i is not None:
                break

    if wick_rec_i is not None:
        out["entry_touched_after_underwater"] = True
        out["never_recovered_by_wick"] = False
        out["bars_until_wick_recovery"] = int(wick_rec_i)
        # max positive after wick recovery (from recovery bar onward)
        if side > 0:
            seg_h = highs[wick_rec_i:]
            mx = float(np.max(seg_h))
            pos_pct = (mx / entry - 1.0) * 100.0
            pos_atr = (mx - entry) / atr_v if atr_v > 0 else float("nan")
            j = wick_rec_i + int(np.argmax(seg_h))
        else:
            seg_l = lows[wick_rec_i:]
            mn = float(np.min(seg_l))
            pos_pct = (1.0 - mn / entry) * 100.0
            pos_atr = (entry - mn) / atr_v if atr_v > 0 else float("nan")
            j = wick_rec_i + int(np.argmin(seg_l))
        out["max_positive_after_wick_recovery_pct"] = float(pos_pct)
        out["max_positive_after_wick_recovery_atr"] = float(pos_atr) if math.isfinite(pos_atr) else float("nan")
        out["bar_of_max_positive_after_wick_recovery"] = int(bars[j])

    if close_rec_i is not None:
        out["entry_closed_recovered_after_underwater"] = True
        out["never_recovered_by_close"] = False
        out["bars_until_close_recovery"] = int(close_rec_i)
        if side > 0:
            seg_h = highs[close_rec_i:]
            mx = float(np.max(seg_h))
            pos_pct = (mx / entry - 1.0) * 100.0
            pos_atr = (mx - entry) / atr_v if atr_v > 0 else float("nan")
            j = close_rec_i + int(np.argmax(seg_h))
        else:
            seg_l = lows[close_rec_i:]
            mn = float(np.min(seg_l))
            pos_pct = (1.0 - mn / entry) * 100.0
            pos_atr = (entry - mn) / atr_v if atr_v > 0 else float("nan")
            j = close_rec_i + int(np.argmin(seg_l))
        out["max_positive_after_close_recovery_pct"] = float(pos_pct)
        out["max_positive_after_close_recovery_atr"] = float(pos_atr) if math.isfinite(pos_atr) else float("nan")
        out["bar_of_max_positive_after_close_recovery"] = int(bars[j])

    # if never underwater, recovery flags stay false / never_recovered True is a bit misleading
    if not ever_uw:
        out["never_recovered_by_wick"] = False  # N/A — did not need recovery
        out["never_recovered_by_close"] = False
        out["recovery_na_not_underwater"] = True
    else:
        out["recovery_na_not_underwater"] = False

    return out


def ltf_loss_extras(
    path: pd.DataFrame,
    *,
    side: int,
    entry: float,
    atr: float | None,
    ltf_loss_bsf: int,
) -> dict[str, Any]:
    """Metrics relative to first LTF alignment-lost bar (bars_since_fill)."""
    out: dict[str, Any] = {
        "ltf_loss_bars_since_fill": int(ltf_loss_bsf),
        "signed_return_pct_at_ltf_loss": float("nan"),
        "max_adverse_pct_before_ltf_loss": float("nan"),
        "max_adverse_pct_after_ltf_loss": float("nan"),
        "recovered_entry_after_ltf_loss_by_wick": False,
        "recovered_entry_after_ltf_loss_by_close": False,
        "max_positive_pct_after_ltf_loss": float("nan"),
        "max_positive_pct_after_recovery_post_ltf": float("nan"),
        "bars_until_wick_recovery_after_ltf_loss": None,
        "bars_until_close_recovery_after_ltf_loss": None,
    }
    if path.empty or ltf_loss_bsf < 0 or ltf_loss_bsf >= len(path):
        return out

    highs = path["high"].astype(float).to_numpy()
    lows = path["low"].astype(float).to_numpy()
    closes = path["close"].astype(float).to_numpy()
    atr_v = _finite(atr)
    i = int(ltf_loss_bsf)

    wick_i = close_i = None
    if side > 0:
        out["signed_return_pct_at_ltf_loss"] = float((closes[i] / entry - 1.0) * 100.0)
        out["max_adverse_pct_before_ltf_loss"] = float((float(np.min(lows[: i + 1])) / entry - 1.0) * 100.0)
        out["max_adverse_pct_after_ltf_loss"] = float((float(np.min(lows[i:])) / entry - 1.0) * 100.0)
        out["max_positive_pct_after_ltf_loss"] = float((float(np.max(highs[i:])) / entry - 1.0) * 100.0)
        adverse_at_loss = closes[i] < entry
        for j in range(i, len(path)):
            if wick_i is None and highs[j] >= entry:
                wick_i = j
            if close_i is None and closes[j] >= entry:
                close_i = j
    else:
        out["signed_return_pct_at_ltf_loss"] = float((1.0 - closes[i] / entry) * 100.0)
        out["max_adverse_pct_before_ltf_loss"] = float((1.0 - float(np.max(highs[: i + 1])) / entry) * 100.0)
        out["max_adverse_pct_after_ltf_loss"] = float((1.0 - float(np.max(highs[i:])) / entry) * 100.0)
        out["max_positive_pct_after_ltf_loss"] = float((1.0 - float(np.min(lows[i:])) / entry) * 100.0)
        adverse_at_loss = closes[i] > entry
        for j in range(i, len(path)):
            if wick_i is None and lows[j] <= entry:
                wick_i = j
            if close_i is None and closes[j] <= entry:
                close_i = j

    out["adverse_at_ltf_loss"] = bool(adverse_at_loss)
    # Recovery after LTF loss only if underwater (adverse close) at the loss bar.
    if adverse_at_loss:
        if wick_i is not None:
            out["recovered_entry_after_ltf_loss_by_wick"] = True
            out["bars_until_wick_recovery_after_ltf_loss"] = int(wick_i - i)
            if side > 0:
                out["max_positive_pct_after_recovery_post_ltf"] = float(
                    (float(np.max(highs[wick_i:])) / entry - 1.0) * 100.0
                )
            else:
                out["max_positive_pct_after_recovery_post_ltf"] = float(
                    (1.0 - float(np.min(lows[wick_i:])) / entry) * 100.0
                )
        if close_i is not None:
            out["recovered_entry_after_ltf_loss_by_close"] = True
            out["bars_until_close_recovery_after_ltf_loss"] = int(close_i - i)
            if side > 0:
                out["max_positive_pct_after_recovery_post_ltf"] = float(
                    (float(np.max(highs[close_i:])) / entry - 1.0) * 100.0
                )
            else:
                out["max_positive_pct_after_recovery_post_ltf"] = float(
                    (1.0 - float(np.min(lows[close_i:])) / entry) * 100.0
                )

    return out


def prefix_scope(metrics: Mapping[str, Any], scope: str) -> dict[str, Any]:
    return {f"{scope}__{k}": v for k, v in metrics.items() if k != "scope"}


def build_per_fill(
    frame: pd.DataFrame,
    fills: pd.DataFrame,
    timeline: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    # first LTF loss event per setup from D2 timeline
    ltf_first: dict[int, int] = {}
    if not timeline.empty and "ltf_major_alignment_lost_event" in timeline.columns:
        flag = timeline["ltf_major_alignment_lost_event"]
        if flag.dtype == object:
            ev_mask = flag.astype(str).str.lower().isin(["true", "1", "yes"])
        else:
            ev_mask = flag.fillna(False).astype(bool)
        ev = timeline.loc[ev_mask]
        for sid, g in ev.groupby("setup_id"):
            ltf_first[int(sid)] = int(g.sort_values("bars_since_fill").iloc[0]["bars_since_fill"])

    rows = []
    ltf_rows = []
    for _, f in fills.iterrows():
        sid = int(f["setup_id"])
        side = int(f["side"]) if pd.notna(f.get("side")) else (1 if f.get("direction") == "long" else -1)
        entry = float(f["entry_price"])
        fill_bar = int(f["fill_bar"])
        atr = _finite(f.get("frozen_atr_14"))
        full = path_slice(frame, fill_bar=fill_bar, n_bars=None)
        h24 = path_slice(frame, fill_bar=fill_bar, n_bars=HORIZON_24)
        m_full = compute_path_metrics(full, side=side, entry=entry, atr=atr, scope="full")
        m_h24 = compute_path_metrics(h24, side=side, entry=entry, atr=atr, scope="h24")
        row = {
            "setup_id": sid,
            "direction": f.get("direction"),
            "side": side,
            "fill_bar": fill_bar,
            "fill_timestamp": f.get("fill_timestamp"),
            "entry_price": entry,
            "frozen_atr_14": atr if atr > 0 else None,
            "has_ltf_major_alignment_lost": sid in ltf_first,
            "ltf_loss_bars_since_fill": ltf_first.get(sid),
        }
        row.update(prefix_scope(m_full, "full"))
        row.update(prefix_scope(m_h24, "h24"))
        rows.append(row)

        if sid in ltf_first:
            extra = ltf_loss_extras(
                full, side=side, entry=entry, atr=atr, ltf_loss_bsf=ltf_first[sid]
            )
            ltf_row = {**row, **extra}
            # also h24-scoped ltf extras on truncated path
            extra24 = ltf_loss_extras(
                h24, side=side, entry=entry, atr=atr, ltf_loss_bsf=ltf_first[sid]
            )
            for k, v in extra24.items():
                ltf_row[f"h24__{k}"] = v
            ltf_rows.append(ltf_row)

    return pd.DataFrame(rows), pd.DataFrame(ltf_rows)


def summarize_group(df: pd.DataFrame, *, label: str, scope: str) -> dict[str, Any]:
    if df.empty:
        return {"group": label, "scope": scope, "n": 0}
    pref = f"{scope}__"
    adv = df[f"{pref}max_adverse_pct"].astype(float)
    wick = df[f"{pref}entry_touched_after_underwater"].astype(bool)
    close = df[f"{pref}entry_closed_recovered_after_underwater"].astype(bool)
    # only among ever underwater for recovery rates
    uw = df[f"{pref}ever_underwater"].astype(bool)
    n_uw = int(uw.sum())
    wick_bars = df.loc[wick, f"{pref}bars_until_wick_recovery"].dropna().astype(float)
    close_bars = df.loc[close, f"{pref}bars_until_close_recovery"].dropna().astype(float)
    pos_w = df.loc[wick, f"{pref}max_positive_after_wick_recovery_pct"].astype(float)
    pos_c = df.loc[close, f"{pref}max_positive_after_close_recovery_pct"].astype(float)

    rec: dict[str, Any] = {
        "group": label,
        "scope": scope,
        "n": int(len(df)),
        "n_ever_underwater": n_uw,
        "median_max_adverse_pct": _median(adv.tolist()),
        "mean_max_adverse_pct": _mean(adv.tolist()),
        "p25_max_adverse_pct": _quantile(adv.tolist(), 0.25),
        "p75_max_adverse_pct": _quantile(adv.tolist(), 0.75),
        "p90_max_adverse_pct": _quantile(adv.tolist(), 0.90),
        "share_wick_recovery_of_all": _safe_rate(int(wick.sum()), len(df)),
        "share_close_recovery_of_all": _safe_rate(int(close.sum()), len(df)),
        "share_wick_recovery_of_underwater": _safe_rate(int(wick.sum()), n_uw),
        "share_close_recovery_of_underwater": _safe_rate(int(close.sum()), n_uw),
        "median_bars_until_wick_recovery": _median(wick_bars.tolist()),
        "median_bars_until_close_recovery": _median(close_bars.tolist()),
        "median_max_positive_after_wick_pct": _median(pos_w.tolist()),
        "median_max_positive_after_close_pct": _median(pos_c.tolist()),
    }
    for thr in POS_THRESHOLDS:
        tag = str(thr).replace(".", "_")
        rec[f"share_wick_rec_then_pos_ge_{tag}pct"] = _safe_rate(int((pos_w >= thr).sum()), int(wick.sum()))
        rec[f"share_close_rec_then_pos_ge_{tag}pct"] = _safe_rate(int((pos_c >= thr).sum()), int(close.sum()))
    return rec


def adverse_buckets(df: pd.DataFrame, *, scope: str) -> pd.DataFrame:
    pref = f"{scope}__"
    rows = []
    for name, lo, hi in ADVERSE_BUCKETS:
        adv = df[f"{pref}max_adverse_pct"].astype(float)
        if lo is None:
            mask = adv < hi
        else:
            mask = (adv < hi) & (adv >= lo)
        sub = df[mask]
        n = len(sub)
        wick = sub[f"{pref}entry_touched_after_underwater"].astype(bool)
        close = sub[f"{pref}entry_closed_recovered_after_underwater"].astype(bool)
        rows.append(
            {
                "scope": scope,
                "bucket": name,
                "bucket_lo_pct": lo,
                "bucket_hi_pct": hi,
                "n": n,
                "share_of_fills": _safe_rate(n, len(df)),
                "wick_recovery_rate": _safe_rate(int(wick.sum()), n),
                "close_recovery_rate": _safe_rate(int(close.sum()), n),
                "median_bars_until_wick_recovery": _median(
                    sub.loc[wick, f"{pref}bars_until_wick_recovery"].dropna().astype(float).tolist()
                ),
                "median_bars_until_close_recovery": _median(
                    sub.loc[close, f"{pref}bars_until_close_recovery"].dropna().astype(float).tolist()
                ),
                "median_max_positive_after_wick_pct": _median(
                    sub.loc[wick, f"{pref}max_positive_after_wick_recovery_pct"].astype(float).tolist()
                ),
                "median_max_positive_after_close_pct": _median(
                    sub.loc[close, f"{pref}max_positive_after_close_recovery_pct"].astype(float).tolist()
                ),
            }
        )
    return pd.DataFrame(rows)


def positive_excursion_table(df: pd.DataFrame, *, scope: str) -> pd.DataFrame:
    pref = f"{scope}__"
    rows = []
    for kind, rec_col, pos_col in (
        ("wick", f"{pref}entry_touched_after_underwater", f"{pref}max_positive_after_wick_recovery_pct"),
        ("close", f"{pref}entry_closed_recovered_after_underwater", f"{pref}max_positive_after_close_recovery_pct"),
    ):
        rec = df[df[rec_col].astype(bool)]
        pos = rec[pos_col].astype(float)
        row: dict[str, Any] = {
            "scope": scope,
            "recovery_type": kind,
            "n_recovered": int(len(rec)),
            "median_max_positive_pct": _median(pos.tolist()),
            "mean_max_positive_pct": _mean(pos.tolist()),
        }
        for thr in POS_THRESHOLDS:
            tag = str(thr).replace(".", "_")
            row[f"share_ge_{tag}pct"] = _safe_rate(int((pos >= thr).sum()), len(rec))
        rows.append(row)
    return pd.DataFrame(rows)


def run_audit(*, apt_dir: Path = DEFAULT_APT_DIR, output_dir: Path = DEFAULT_OUT) -> dict[str, Any]:
    apt_dir = Path(apt_dir)
    output_dir = Path(output_dir)
    if output_dir.resolve() == apt_dir.resolve():
        raise RuntimeError("refusing to write into apt_audit root")
    output_dir.mkdir(parents=True, exist_ok=True)

    fills_path = apt_dir / "fills.csv"
    tl_path = apt_dir / "d2_timeline_full.csv"
    if not fills_path.exists() or not tl_path.exists():
        raise RuntimeError(f"missing fills/timeline in {apt_dir}")
    fills = pd.read_csv(fills_path)
    timeline = pd.read_csv(tl_path)

    frame, _frame4h, meta = build_apt_d1_frame()
    per_fill, ltf_df = build_per_fill(frame, fills, timeline)

    summary_rows = []
    for scope in ("full", "h24"):
        summary_rows.append(summarize_group(per_fill, label="all", scope=scope))
        summary_rows.append(summarize_group(per_fill[per_fill["direction"] == "long"], label="long", scope=scope))
        summary_rows.append(summarize_group(per_fill[per_fill["direction"] == "short"], label="short", scope=scope))
        if not ltf_df.empty:
            summary_rows.append(summarize_group(ltf_df, label="ltf_major_alignment_lost", scope=scope))
    summary = pd.DataFrame(summary_rows)

    buckets = pd.concat(
        [adverse_buckets(per_fill, scope="full"), adverse_buckets(per_fill, scope="h24")],
        ignore_index=True,
    )
    pos_exc = pd.concat(
        [positive_excursion_table(per_fill, scope="full"), positive_excursion_table(per_fill, scope="h24")],
        ignore_index=True,
    )

    per_fill.to_csv(output_dir / "path_recovery_per_fill.csv", index=False)
    summary.to_csv(output_dir / "path_recovery_summary.csv", index=False)
    ltf_df.to_csv(output_dir / "ltf_loss_path_recovery.csv", index=False)
    buckets.to_csv(output_dir / "adverse_bucket_recovery.csv", index=False)
    pos_exc.to_csv(output_dir / "recovery_positive_excursion.csv", index=False)

    # compact JSON
    def _group(scope: str, label: str) -> dict[str, Any]:
        r = summary[(summary["scope"] == scope) & (summary["group"] == label)]
        return {} if r.empty else r.iloc[0].to_dict()

    audit = {
        "phase": PHASE,
        "n_fills": int(len(per_fill)),
        "n_ltf_loss_fills": int(len(ltf_df)),
        "data_meta": {k: meta[k] for k in meta if k != "frame15_meta"},
        "definitions": {
            "max_adverse_long": "(min_low/entry - 1)*100",
            "max_adverse_short": "(1 - max_high/entry)*100",
            "underwater": "close adverse to entry",
            "wick_recovery": "after underwater, high>=entry (long) / low<=entry (short)",
            "close_recovery": "after underwater, close>=entry (long) / close<=entry (short)",
            "full_path": "fill_bar through end of analyze window",
            "h24": "first 24 bars from fill inclusive (bars_since_fill 0..23)",
            "positive_after_recovery": "from first recovery bar onward",
        },
        "summary_full_all": _group("full", "all"),
        "summary_h24_all": _group("h24", "all"),
        "summary_full_ltf_loss": _group("full", "ltf_major_alignment_lost"),
        "summary_h24_ltf_loss": _group("h24", "ltf_major_alignment_lost"),
        "no_d3": True,
        "no_pine": True,
        "no_live_bot": True,
        "no_commit": True,
    }
    (output_dir / "path_recovery_summary.json").write_text(
        json.dumps(json_safe(audit), indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# C3.5D Path Recovery Audit",
                "",
                "Full post-fill adverse → recovery → positive excursion for APT continuation fills.",
                "Scopes: `full` (to data end) and `h24` (24 bars).",
                "",
                f"- Fills: `{audit['n_fills']}`",
                f"- With LTF alignment lost: `{audit['n_ltf_loss_fills']}`",
                "",
                "No D3 rules. No Pine. No live bot.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return audit


def main() -> None:
    p = argparse.ArgumentParser(description="C3.5D APT path recovery audit")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    audit = run_audit(apt_dir=args.apt_dir, output_dir=args.output_dir)
    print(json.dumps(json_safe(audit), indent=2))


if __name__ == "__main__":
    main()
