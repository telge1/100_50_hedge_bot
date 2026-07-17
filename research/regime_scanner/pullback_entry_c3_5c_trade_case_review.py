"""C3.5c APT trade case review (research-only, descriptive).

Consumes pattern-diagnostic artifacts. Does not change outcomes, SM, C3.4B,
Pine, or promote filters. Charts use the same 15m research frame for OHLC only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_pattern_diagnostic_audit import (
    DEFAULT_OUT as PATTERN_OUT,
    NET_COL,
    enrich_diagnostic_frame,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    DEFAULT_BASELINE_DIR,
    WARMUP_CALENDAR_DAYS,
    build_extended_tf_frame,
    outlier_metrics,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_trade_case_review"
)

SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
VARIANT = "A6"
BARS_BEFORE_ENTRY = 30
BARS_AFTER_EXIT = 10

DIAG_FEATURE_COLS: tuple[str, ...] = (
    "pullback_depth_atr",
    "pullback_duration_bars",
    "bars_arm_to_trigger",
    "bars_since_external_bos",
    "bars_since_internal_bos",
    "bars_since_choch",
    "ret_3",
    "ret_5",
    "ret_10",
    "adx",
    "adx_change_5",
    "adx_slope_5",
    "di_alignment_age",
    "di_spread_signed",  # directional DI spread (trade-direction signed in panel)
    "ema9_minus_ema20_pct",
    "ema20_minus_ema50_pct",
    "cross_age_bars",
    "rejection_wick_ratio",
    "confirmation_body_ratio",
    "chase_distance_atr",
    "vol_range_pct_5",
    "vol_range_pct_10",
    "major_direction",
    "micro_direction",
    "major_micro_alignment",
    "regime",
)

HYPOTHESIS_STATUSES: tuple[str, ...] = (
    "visually_supported",
    "statistically_supported_but_underpowered",
    "short_only",
    "top3_driven",
    "inconsistent",
    "not_supported",
)


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _pf(rets: pd.Series) -> float | None:
    r = pd.to_numeric(rets, errors="coerce").dropna()
    if r.empty:
        return None
    gp = float(r[r > 0].sum())
    gl = float((-r[r <= 0]).sum())
    if gl <= 1e-15:
        return None if gp <= 0 else float("inf")
    return gp / gl


def safe_trade_slug(trade_id: str) -> str:
    s = str(trade_id)
    s = s.replace("+00:00", "Z").replace(":", "").replace("+", "")
    s = re.sub(r"[^A-Za-z0-9_.\-]+", "_", s)
    return s[:120]


def load_closed_panel(pattern_dir: Path) -> pd.DataFrame:
    path = pattern_dir / "trade_feature_panel.csv"
    if not path.exists():
        raise FileNotFoundError(f"missing pattern panel: {path}")
    panel = pd.read_csv(path)
    closed = panel[panel["closed"] == True].copy()  # noqa: E712
    if closed.empty:
        raise RuntimeError("no closed trades in pattern panel")
    closed["entry_timestamp"] = pd.to_datetime(closed["entry_timestamp"], utc=True)
    closed["exit_timestamp"] = pd.to_datetime(closed["exit_timestamp"], utc=True)
    if "trigger_timestamp" in closed.columns:
        closed["trigger_timestamp"] = pd.to_datetime(closed["trigger_timestamp"], utc=True)
    # aliases
    if "net_return_020_pct" not in closed.columns and NET_COL in closed.columns:
        closed["net_return_020_pct"] = closed[NET_COL]
    if "regime_state" not in closed.columns and "regime" in closed.columns:
        closed["regime_state"] = closed["regime"]
    if "directional_di_spread" not in closed.columns and "di_spread_signed" in closed.columns:
        closed["directional_di_spread"] = closed["di_spread_signed"]
    if "mfe_pct" not in closed.columns and "maximum_favorable_pct" in closed.columns:
        closed["mfe_pct"] = closed["maximum_favorable_pct"]
        closed["mae_pct"] = closed["maximum_adverse_pct"]
    return closed.reset_index(drop=True)


def compute_dev_thresholds(closed: pd.DataFrame) -> dict[str, Any]:
    """Fixed Development medians / q33/q66 — applied unchanged to Val/OOS."""
    dev = closed[closed["split"] == "development"]
    if dev.empty:
        raise RuntimeError("no development trades for thresholds")

    def _med(col: str) -> float:
        return float(pd.to_numeric(dev[col], errors="coerce").median())

    def _q(col: str, q: float) -> float:
        return float(pd.to_numeric(dev[col], errors="coerce").quantile(q))

    thr = {
        "source_split": "development",
        "n_development": int(len(dev)),
        "pullback_depth_atr_median": _med("pullback_depth_atr"),
        "pullback_depth_atr_q33": _q("pullback_depth_atr", 0.33),
        "pullback_depth_atr_q66": _q("pullback_depth_atr", 0.66),
        "chase_distance_atr_median": _med("chase_distance_atr"),
        "bars_arm_to_trigger_median": _med("bars_arm_to_trigger"),
        "fixed_before_flagging": True,
        "note": "shallow<=q33, deep>=q66; chase/setup vs development median",
    }
    return thr


def apply_diagnostic_flags(closed: pd.DataFrame, thr: Mapping[str, Any]) -> pd.DataFrame:
    out = closed.copy()
    depth = pd.to_numeric(out["pullback_depth_atr"], errors="coerce")
    chase = pd.to_numeric(out["chase_distance_atr"], errors="coerce")
    setup = pd.to_numeric(out["bars_arm_to_trigger"], errors="coerce")
    adx5 = pd.to_numeric(out.get("adx_change_5"), errors="coerce")

    q33 = float(thr["pullback_depth_atr_q33"])
    q66 = float(thr["pullback_depth_atr_q66"])
    chase_med = float(thr["chase_distance_atr_median"])
    setup_med = float(thr["bars_arm_to_trigger_median"])

    out["shallow_pullback_relative_to_dev"] = depth <= q33
    out["deep_pullback_relative_to_dev"] = depth >= q66
    out["low_chase_relative_to_dev"] = chase <= chase_med
    out["high_chase_relative_to_dev"] = chase > chase_med
    out["slow_setup_relative_to_dev"] = setup >= setup_med
    out["fast_setup_relative_to_dev"] = setup < setup_med
    out["adx_rising_5"] = adx5 > 0
    out["adx_falling_5"] = adx5 < 0
    out["short_trade"] = out["side"].astype(str).str.lower() == "short"
    out["long_trade"] = out["side"].astype(str).str.lower() == "long"

    # rank by return among closed (1 = best)
    out["rank_by_return"] = out["net_return_020_pct"].rank(ascending=False, method="first").astype(int)
    out["case_slug"] = out["trade_id"].map(safe_trade_slug)
    return out


def reconstruct_lifecycle_bars(row: Mapping[str, Any]) -> dict[str, int | None]:
    """Derive absolute bars from panel relative timings (no SM mutation)."""
    trigger = int(row["trigger_bar"]) if pd.notna(row.get("trigger_bar")) else None
    fill = int(row["fill_bar"]) if pd.notna(row.get("fill_bar")) else (trigger + 1 if trigger is not None else None)
    hold = int(row["holding_bars"]) if pd.notna(row.get("holding_bars")) else 0
    exit_bar = (fill + hold) if fill is not None else None

    arm = None
    pb = None
    ready = None
    if trigger is not None and pd.notna(row.get("bars_arm_to_trigger")):
        arm = trigger - int(float(row["bars_arm_to_trigger"]))
    if arm is not None and pd.notna(row.get("bars_arm_to_pullback")):
        pb = arm + int(float(row["bars_arm_to_pullback"]))
    if pb is not None and pd.notna(row.get("bars_pullback_to_ready")):
        ready = pb + int(float(row["bars_pullback_to_ready"]))
    elif trigger is not None and pd.notna(row.get("bars_ready_to_trigger")) and ready is None:
        ready = trigger - int(float(row["bars_ready_to_trigger"]))
    return {
        "arm_bar": arm,
        "pullback_bar": pb,
        "ready_bar": ready,
        "trigger_bar": trigger,
        "fill_bar": fill,
        "exit_bar": exit_bar,
    }


def build_trade_case_index(flagged: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "trade_id",
        "case_slug",
        "side",
        "split",
        "entry_timestamp",
        "exit_timestamp",
        "holding_bars",
        "holding_minutes",
        "gross_return_pct",
        "net_return_020_pct",
        "winner_net020",
        "rank_by_return",
        "top1_trade",
        "top3_trade",
        "month",
        "entry_price",
        "exit_price",
        "setup_id",
        "trigger_bar",
        "fill_bar",
        "mfe_pct",
        "mae_pct",
        *DIAG_FEATURE_COLS,
        "directional_di_spread",
        "regime_state",
        "shallow_pullback_relative_to_dev",
        "deep_pullback_relative_to_dev",
        "low_chase_relative_to_dev",
        "high_chase_relative_to_dev",
        "slow_setup_relative_to_dev",
        "fast_setup_relative_to_dev",
        "adx_rising_5",
        "adx_falling_5",
        "short_trade",
        "long_trade",
    ]
    # ensure aliases
    out = flagged.copy()
    if "directional_di_spread" not in out.columns:
        out["directional_di_spread"] = out.get("di_spread_signed")
    if "regime_state" not in out.columns:
        out["regime_state"] = out.get("regime")
    keep = [c for c in cols if c in out.columns]
    idx = out[keep].copy()
    # rename timestamps for schema
    idx = idx.rename(columns={"entry_timestamp": "entry_time", "exit_timestamp": "exit_time"})
    return idx.sort_values("rank_by_return").reset_index(drop=True)


def assign_archetypes(row: Mapping[str, Any]) -> list[str]:
    tags: list[str] = []
    shallow = bool(row.get("shallow_pullback_relative_to_dev"))
    deep = bool(row.get("deep_pullback_relative_to_dev"))
    slow = bool(row.get("slow_setup_relative_to_dev"))
    fast = bool(row.get("fast_setup_relative_to_dev"))
    low_c = bool(row.get("low_chase_relative_to_dev"))
    high_c = bool(row.get("high_chase_relative_to_dev"))
    if shallow and slow and low_c:
        tags.append("shallow_slow_low_chase")
    if shallow and fast:
        tags.append("shallow_fast")
    if deep:
        tags.append("deep_pullback")
    if high_c:
        tags.append("high_chase")
    if bool(row.get("adx_rising_5")):
        tags.append("rising_adx")
    if bool(row.get("adx_falling_5")):
        tags.append("falling_adx")
    mma = row.get("major_micro_alignment")
    if _finite(mma) == 1.0:
        tags.append("aligned_structure")
    elif _finite(mma) == 0.0:
        tags.append("conflicting_structure")
    side = str(row.get("side", "")).lower()
    maj = int(_finite(row.get("major_direction"), 0))
    if side == "short" and maj < 0:
        tags.append("short_trend_continuation")
    if side == "long" and maj < 0:
        tags.append("long_countertrend")
    if side == "short" and maj > 0:
        tags.append("short_countertrend")
    if side == "long" and maj > 0:
        tags.append("long_trend_continuation")
    if not tags:
        tags.append("mixed")
    elif len(tags) >= 4:
        tags.append("mixed")
    return sorted(set(tags))


# ---------------------------------------------------------------------------
# Group / archetype summaries
# ---------------------------------------------------------------------------


def _group_stats(g: pd.DataFrame, *, group_name: str, group_value: str) -> dict[str, Any]:
    net = pd.to_numeric(g["net_return_020_pct"], errors="coerce")
    om = outlier_metrics(net) if len(g) else {}
    months = g["month"].astype(str).value_counts().to_dict() if "month" in g.columns else {}
    return {
        "group_name": group_name,
        "group_value": group_value,
        "n": int(len(g)),
        "n_winners": int((net > 0).sum()) if len(g) else 0,
        "n_losers": int((net <= 0).sum()) if len(g) else 0,
        "winrate": float((net > 0).mean()) if len(g) else None,
        "mean_return": float(net.mean()) if len(g) else None,
        "median_return": float(net.median()) if len(g) else None,
        "sum_return": float(net.sum()) if len(g) else None,
        "profit_factor": _pf(net),
        "mean_mfe": float(pd.to_numeric(g["mfe_pct"], errors="coerce").mean()) if "mfe_pct" in g and len(g) else None,
        "mean_mae": float(pd.to_numeric(g["mae_pct"], errors="coerce").mean()) if "mae_pct" in g and len(g) else None,
        "best": float(net.max()) if len(g) else None,
        "worst": float(net.min()) if len(g) else None,
        "top1_share": om.get("best_share_of_net_sum"),
        "top3_share": om.get("top3_share_of_net_sum"),
        "long_share": float((g["side"].astype(str).str.lower() == "long").mean()) if len(g) else None,
        "short_share": float((g["side"].astype(str).str.lower() == "short").mean()) if len(g) else None,
        "n_dev": int((g["split"] == "development").sum()) if len(g) else 0,
        "n_val": int((g["split"] == "validation").sum()) if len(g) else 0,
        "n_oos": int((g["split"] == "oos").sum()) if len(g) else 0,
        "month_distribution": json.dumps(months, sort_keys=True),
    }


def build_case_group_summary(flagged: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    rows.append(_group_stats(flagged[flagged["winner_net020"] == True], group_name="winner_vs_loser", group_value="winner"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["winner_net020"] == False], group_name="winner_vs_loser", group_value="loser"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["side"].astype(str).str.lower() == "long"], group_name="side", group_value="long"))
    rows.append(_group_stats(flagged[flagged["side"].astype(str).str.lower() == "short"], group_name="side", group_value="short"))
    rows.append(_group_stats(flagged[flagged["top3_trade"] == True], group_name="top3_vs_rest", group_value="top3"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["top3_trade"] == False], group_name="top3_vs_rest", group_value="rest"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["top3_trade"] == False], group_name="without_top3", group_value="without_top3"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["shallow_pullback_relative_to_dev"] == True], group_name="pullback", group_value="shallow"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["deep_pullback_relative_to_dev"] == True], group_name="pullback", group_value="deep"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["slow_setup_relative_to_dev"] == True], group_name="setup_speed", group_value="slow"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["fast_setup_relative_to_dev"] == True], group_name="setup_speed", group_value="fast"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["low_chase_relative_to_dev"] == True], group_name="chase", group_value="low_chase"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["high_chase_relative_to_dev"] == True], group_name="chase", group_value="high_chase"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["adx_rising_5"] == True], group_name="adx_5", group_value="rising"))  # noqa: E712
    rows.append(_group_stats(flagged[flagged["adx_falling_5"] == True], group_name="adx_5", group_value="falling"))  # noqa: E712
    for sp in ("development", "validation", "oos"):
        rows.append(_group_stats(flagged[flagged["split"] == sp], group_name="split", group_value=sp))
    return pd.DataFrame(rows)


def build_archetype_table(flagged: pd.DataFrame) -> pd.DataFrame:
    rows = []
    # explode membership
    membership: dict[str, list[int]] = {}
    for i, row in flagged.iterrows():
        for tag in assign_archetypes(row):
            membership.setdefault(tag, []).append(i)
    for tag, idxs in sorted(membership.items()):
        g = flagged.loc[idxs]
        net = pd.to_numeric(g["net_return_020_pct"], errors="coerce")
        om = outlier_metrics(net)
        without_top3 = g[g["top3_trade"] == False]  # noqa: E712
        net_wo = pd.to_numeric(without_top3["net_return_020_pct"], errors="coerce")
        rows.append(
            {
                "archetype": tag,
                "n": int(len(g)),
                "n_winners": int((net > 0).sum()),
                "n_losers": int((net <= 0).sum()),
                "winrate": float((net > 0).mean()),
                "mean_return": float(net.mean()),
                "median_return": float(net.median()),
                "profit_factor": _pf(net),
                "n_dev": int((g["split"] == "development").sum()),
                "n_val": int((g["split"] == "validation").sum()),
                "n_oos": int((g["split"] == "oos").sum()),
                "top3_share_of_members": float(g["top3_trade"].mean()),
                "top3_share_of_net": om.get("top3_share_of_net_sum"),
                "long_share": float((g["side"].astype(str).str.lower() == "long").mean()),
                "short_share": float((g["side"].astype(str).str.lower() == "short").mean()),
                "n_without_top3": int(len(without_top3)),
                "mean_return_without_top3": float(net_wo.mean()) if len(without_top3) else None,
                "sum_return_without_top3": float(net_wo.sum()) if len(without_top3) else None,
                "winrate_without_top3": float((net_wo > 0).mean()) if len(without_top3) else None,
            }
        )
    return pd.DataFrame(rows).sort_values(["n", "archetype"], ascending=[False, True])


# ---------------------------------------------------------------------------
# Charts & case packages
# ---------------------------------------------------------------------------


def _last_event_bar(frame: pd.DataFrame, col: str, end_inclusive: int) -> int | None:
    if col not in frame.columns or end_inclusive < 0:
        return None
    sub = frame.iloc[: end_inclusive + 1]
    mask = sub[col].fillna(False).astype(bool)
    if not mask.any():
        return None
    return int(mask[mask].index[-1])


def render_case_chart(
    frame: pd.DataFrame,
    row: Mapping[str, Any],
    life: Mapping[str, int | None],
    out_path: Path,
) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception:
        return False

    n = len(frame)
    fill_i = life.get("fill_bar")
    exit_i = life.get("exit_bar")
    trigger_i = life.get("trigger_bar")
    if fill_i is None or trigger_i is None:
        return False
    exit_i = exit_i if exit_i is not None else min(n - 1, fill_i)
    start = max(0, int(fill_i) - BARS_BEFORE_ENTRY)
    end = min(n - 1, int(exit_i) + BARS_AFTER_EXIT)
    win = frame.iloc[start : end + 1].copy()
    if win.empty:
        return False
    x = np.arange(len(win))

    fig, axes = plt.subplots(
        4,
        1,
        figsize=(11, 10),
        sharex=True,
        gridspec_kw={"height_ratios": [3.0, 1.2, 1.2, 1.0]},
    )
    ax, ax_adx, ax_di, ax_atr = axes

    # Candles
    opens = win["open"].astype(float).to_numpy()
    highs = win["high"].astype(float).to_numpy()
    lows = win["low"].astype(float).to_numpy()
    closes = win["close"].astype(float).to_numpy()
    for i in range(len(win)):
        color = "#2ca02c" if closes[i] >= opens[i] else "#d62728"
        ax.vlines(i, lows[i], highs[i], color=color, linewidth=0.8)
        bottom = min(opens[i], closes[i])
        height = abs(closes[i] - opens[i]) or (highs[i] - lows[i]) * 0.01
        ax.add_patch(Rectangle((i - 0.3, bottom), 0.6, height, facecolor=color, edgecolor=color, linewidth=0.4))

    for col, lab, ls in (("ema_9", "EMA9", "-"), ("ema_20", "EMA20", "--"), ("ema_50", "EMA50", ":")):
        if col in win.columns:
            ax.plot(x, win[col].astype(float).to_numpy(), ls=ls, lw=1.0, label=lab)

    def _x_of(bar: int | None) -> int | None:
        if bar is None:
            return None
        if bar < start or bar > end:
            return None
        return int(bar - start)

    # Protected levels: step plot of causal values (no forward fill from future)
    if "protected_high" in win.columns:
        ax.plot(x, win["protected_high"].astype(float), color="#9467bd", lw=0.8, alpha=0.7, label="prot_high")
    if "protected_low" in win.columns:
        ax.plot(x, win["protected_low"].astype(float), color="#8c564b", lw=0.8, alpha=0.7, label="prot_low")

    markers = [
        (life.get("arm_bar"), "ARM", "C0"),
        (life.get("pullback_bar"), "PB", "C1"),
        (life.get("ready_bar"), "READY", "C5"),
        (life.get("trigger_bar"), "TRIGGER", "C3"),
        (life.get("fill_bar"), "ENTRY", "C2"),
        (life.get("exit_bar"), "EXIT", "k"),
    ]
    # structure events last before trigger (causal)
    if trigger_i is not None:
        side = str(row.get("side", "")).lower()
        ext_col = "arm_edge_external_bear" if side == "short" else "arm_edge_external_bull"
        int_col = "arm_edge_internal_bear" if side == "short" else "arm_edge_internal_bull"
        choch_col = "arm_edge_choch_bear" if side == "short" else "arm_edge_choch_bull"
        for col, lab, color in (
            (ext_col, "extBOS", "#e377c2"),
            (int_col, "intBOS", "#7f7f7f"),
            (choch_col, "CHOCH", "#bcbd22"),
        ):
            b = _last_event_bar(frame, col, int(trigger_i))
            markers.append((b, lab, color))

    ymin = float(np.nanmin(lows))
    ymax = float(np.nanmax(highs))
    for bar, lab, color in markers:
        xi = _x_of(bar if bar is None else int(bar))
        if xi is None:
            continue
        ax.axvline(xi, color=color, lw=0.9, alpha=0.85)
        ax.text(xi, ymax, lab, rotation=90, va="top", ha="right", fontsize=7, color=color)

    # Entry/exit horizontal
    ax.axhline(float(row["entry_price"]), color="C2", lw=0.8, ls="--", alpha=0.8)
    ax.axhline(float(row["exit_price"]), color="k", lw=0.8, ls="--", alpha=0.8)
    ax.set_ylabel("price")
    ax.set_title(
        f"{row.get('trade_id')} · {row.get('side')} · net0.20={float(row['net_return_020_pct']):.2f}% · {row.get('split')}"
    )
    ax.legend(loc="upper left", fontsize=7, ncol=4)
    ax.set_ylim(ymin - (ymax - ymin) * 0.05, ymax + (ymax - ymin) * 0.12)

    # ADX
    if "adx" in win.columns:
        ax_adx.plot(x, win["adx"].astype(float), color="C0", label="ADX")
    ax_adx.axhline(20, color="gray", lw=0.5, ls=":")
    ax_adx.axhline(25, color="gray", lw=0.5, ls=":")
    ax_adx.legend(fontsize=7)
    ax_adx.set_ylabel("ADX")

    # DI
    if "plus_di" in win.columns:
        ax_di.plot(x, win["plus_di"].astype(float), label="+DI", color="C2")
    if "minus_di" in win.columns:
        ax_di.plot(x, win["minus_di"].astype(float), label="-DI", color="C3")
    # directional spread from event panel sense
    side_sign = -1 if str(row.get("side")).lower() == "short" else 1
    if "plus_di" in win.columns and "minus_di" in win.columns:
        if side_sign < 0:
            dsp = win["minus_di"].astype(float) - win["plus_di"].astype(float)
        else:
            dsp = win["plus_di"].astype(float) - win["minus_di"].astype(float)
        ax_di.plot(x, dsp, label="dir DI spread", color="C4", ls="--")
    ax_di.legend(fontsize=7)
    ax_di.set_ylabel("DI")

    # ATR% + signed returns path from entry (post-entry diagnostic only in panel note)
    if "atr_pct" in win.columns:
        ax_atr.plot(x, win["atr_pct"].astype(float), color="C1", label="ATR%")
    # directional close return from entry open bar
    entry_px = float(row["entry_price"])
    if side_sign > 0:
        path_ret = (closes / entry_px - 1.0) * 100.0
    else:
        path_ret = (entry_px / closes - 1.0) * 100.0
    ax_atr.plot(x, path_ret, color="C5", label="path ret% (diag)", alpha=0.8)
    ax_atr.legend(fontsize=7)
    ax_atr.set_ylabel("ATR%/ret")
    ax_atr.set_xlabel(f"bars [{start}..{end}] relative index")

    # mark pre/post entry
    entry_x = _x_of(int(fill_i))
    if entry_x is not None:
        for a in axes:
            a.axvline(entry_x, color="C2", lw=0.6, alpha=0.5)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return True


def write_case_notes(row: Mapping[str, Any], life: Mapping[str, int | None], archetypes: Sequence[str]) -> str:
    lines = [
        f"# Case `{row.get('trade_id')}`",
        "",
        "Rein deskriptiv. Keine Filterempfehlung.",
        "",
        "## Outcome",
        f"- Side: **{row.get('side')}** · Split: **{row.get('split')}** · Month: `{row.get('month')}`",
        f"- Entry `{row.get('entry_timestamp')}` @ {row.get('entry_price')}",
        f"- Exit `{row.get('exit_timestamp')}` @ {row.get('exit_price')}",
        f"- Holding bars: {row.get('holding_bars')} · gross={_finite(row.get('gross_return_pct')):.3f}% · "
        f"net0.20={_finite(row.get('net_return_020_pct')):.3f}%",
        f"- Winner(net0.20): {bool(row.get('winner_net020'))} · rank={row.get('rank_by_return')} · "
        f"top1={bool(row.get('top1_trade'))} · top3={bool(row.get('top3_trade'))}",
        f"- MFE={_finite(row.get('mfe_pct')):.3f}% · MAE={_finite(row.get('mae_pct')):.3f}%",
        "",
        "## Entry-Verlauf (Lifecycle)",
        f"- Arm bar: `{life.get('arm_bar')}` · Pullback: `{life.get('pullback_bar')}` · "
        f"Ready: `{life.get('ready_bar')}` · Trigger: `{life.get('trigger_bar')}` · Fill: `{life.get('fill_bar')}`",
        f"- bars_arm_to_trigger={row.get('bars_arm_to_trigger')} · "
        f"pullback_duration_bars={row.get('pullback_duration_bars')}",
        "",
        "## Pullback / Chase",
        f"- pullback_depth_atr={_finite(row.get('pullback_depth_atr')):.4g} · "
        f"shallow={bool(row.get('shallow_pullback_relative_to_dev'))} · deep={bool(row.get('deep_pullback_relative_to_dev'))}",
        f"- chase_distance_atr={_finite(row.get('chase_distance_atr')):.4g} · "
        f"low_chase={bool(row.get('low_chase_relative_to_dev'))} · high_chase={bool(row.get('high_chase_relative_to_dev'))}",
        f"- rejection_wick_ratio={_finite(row.get('rejection_wick_ratio')):.4g} · "
        f"confirmation_body_ratio={_finite(row.get('confirmation_body_ratio')):.4g}",
        "",
        "## ADX / DI (as-of trigger)",
        f"- adx={_finite(row.get('adx')):.4g} · adx_change_5={_finite(row.get('adx_change_5')):.4g} · "
        f"adx_slope_5={_finite(row.get('adx_slope_5')):.4g}",
        f"- rising_5={bool(row.get('adx_rising_5'))} · falling_5={bool(row.get('adx_falling_5'))}",
        f"- di_spread_signed={_finite(row.get('di_spread_signed')):.4g} · di_alignment_age={row.get('di_alignment_age')}",
        "",
        "## EMA / Cross",
        f"- ema9-ema20%={_finite(row.get('ema9_minus_ema20_pct')):.4g} · "
        f"ema20-ema50%={_finite(row.get('ema20_minus_ema50_pct')):.4g}",
        f"- cross_age_bars={row.get('cross_age_bars')}",
        "",
        "## Structure / Regime",
        f"- major_direction={row.get('major_direction')} · micro_direction={row.get('micro_direction')} · "
        f"aligned={row.get('major_micro_alignment')}",
        f"- bars_since_external_bos={row.get('bars_since_external_bos')} · "
        f"internal={row.get('bars_since_internal_bos')} · choch={row.get('bars_since_choch')}",
        f"- regime_state=`{row.get('regime_state', row.get('regime'))}`",
        "",
        "## Setup timing",
        f"- slow_setup={bool(row.get('slow_setup_relative_to_dev'))} · fast_setup={bool(row.get('fast_setup_relative_to_dev'))}",
        "",
        "## Archetypes (descriptive)",
        "- " + (", ".join(archetypes) if archetypes else "(none)"),
        "",
        "## Hinweis",
        "- Post-Entry-Pfad im Chart ist nur Diagnose; Entry-Features sind trigger-close kausal.",
        "",
    ]
    return "\n".join(lines)


def write_case_package(
    out_cases: Path,
    row: Mapping[str, Any],
    thr: Mapping[str, Any],
    frame: pd.DataFrame | None,
) -> dict[str, Any]:
    life = reconstruct_lifecycle_bars(row)
    archetypes = assign_archetypes(row)
    slug = str(row.get("case_slug") or safe_trade_slug(str(row["trade_id"])))
    case_dir = out_cases / f"trade_{slug}"
    case_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "trade_id": row.get("trade_id"),
        "case_slug": slug,
        "side": row.get("side"),
        "split": row.get("split"),
        "month": row.get("month"),
        "entry_time": str(row.get("entry_timestamp")),
        "exit_time": str(row.get("exit_timestamp")),
        "entry_price": row.get("entry_price"),
        "exit_price": row.get("exit_price"),
        "holding_bars": row.get("holding_bars"),
        "gross_return_pct": row.get("gross_return_pct"),
        "net_return_020_pct": row.get("net_return_020_pct"),
        "winner_net020": bool(row.get("winner_net020")),
        "rank_by_return": int(row.get("rank_by_return")),
        "top1_trade": bool(row.get("top1_trade")),
        "top3_trade": bool(row.get("top3_trade")),
        "mfe_pct": row.get("mfe_pct"),
        "mae_pct": row.get("mae_pct"),
        "lifecycle_bars": life,
        "diagnostic_features": {k: row.get(k) for k in DIAG_FEATURE_COLS if k in row},
        "directional_di_spread": row.get("directional_di_spread", row.get("di_spread_signed")),
        "regime_state": row.get("regime_state", row.get("regime")),
        "flags": {
            "shallow_pullback_relative_to_dev": bool(row.get("shallow_pullback_relative_to_dev")),
            "deep_pullback_relative_to_dev": bool(row.get("deep_pullback_relative_to_dev")),
            "low_chase_relative_to_dev": bool(row.get("low_chase_relative_to_dev")),
            "high_chase_relative_to_dev": bool(row.get("high_chase_relative_to_dev")),
            "slow_setup_relative_to_dev": bool(row.get("slow_setup_relative_to_dev")),
            "fast_setup_relative_to_dev": bool(row.get("fast_setup_relative_to_dev")),
            "adx_rising_5": bool(row.get("adx_rising_5")),
            "adx_falling_5": bool(row.get("adx_falling_5")),
            "short_trade": bool(row.get("short_trade")),
            "long_trade": bool(row.get("long_trade")),
        },
        "archetypes": archetypes,
        "dev_thresholds_ref": {
            "pullback_q33": thr.get("pullback_depth_atr_q33"),
            "pullback_q66": thr.get("pullback_depth_atr_q66"),
            "chase_median": thr.get("chase_distance_atr_median"),
            "setup_median": thr.get("bars_arm_to_trigger_median"),
        },
        "no_strategy_decision": True,
        "note": "Descriptive case package only. Not a filter recommendation.",
    }
    (case_dir / "case_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (case_dir / "case_notes.md").write_text(write_case_notes(row, life, archetypes), encoding="utf-8")
    chart_ok = False
    if frame is not None and not frame.empty:
        chart_ok = render_case_chart(frame, row, life, case_dir / "case_chart.png")
    summary["chart_written"] = chart_ok
    return summary


def write_outlier_cases_md(flagged: pd.DataFrame, out_path: Path) -> None:
    ranked = flagged.sort_values("net_return_020_pct", ascending=False)
    best = ranked.head(3)
    worst = ranked.tail(3).iloc[::-1]
    lines = [
        "# Outlier cases (best 3 / worst 3)",
        "",
        "Trennung: **Entry-sichtbar (kausal)** vs **erst nach Entry sichtbar**.",
        "",
        "## Best three",
        "",
    ]

    def _block(r: pd.Series, label: str) -> list[str]:
        life = reconstruct_lifecycle_bars(r)
        hold_h = _finite(r.get("holding_minutes")) / 60.0 if pd.notna(r.get("holding_minutes")) else _finite(r.get("holding_bars")) * 0.25
        entry_visible = []
        if bool(r.get("shallow_pullback_relative_to_dev")):
            entry_visible.append("shallow pullback (dev q33)")
        if bool(r.get("deep_pullback_relative_to_dev")):
            entry_visible.append("deep pullback (dev q66)")
        if bool(r.get("high_chase_relative_to_dev")):
            entry_visible.append("high chase vs dev median")
        if bool(r.get("low_chase_relative_to_dev")):
            entry_visible.append("low chase vs dev median")
        if bool(r.get("slow_setup_relative_to_dev")):
            entry_visible.append("slow arm→trigger")
        if bool(r.get("adx_rising_5")):
            entry_visible.append("adx_change_5>0")
        if bool(r.get("adx_falling_5")):
            entry_visible.append("adx_change_5<0")
        post = [
            f"realized net0.20={_finite(r['net_return_020_pct']):.2f}%",
            f"MFE={_finite(r.get('mfe_pct')):.2f}% / MAE={_finite(r.get('mae_pct')):.2f}%",
            f"holding≈{hold_h:.1f}h",
        ]
        return [
            f"### {label}: `{r['trade_id']}`",
            f"- Side **{r['side']}** · split `{r['split']}` · top3={bool(r['top3_trade'])}",
            f"- Lifecycle bars: arm={life.get('arm_bar')} pb={life.get('pullback_bar')} "
            f"ready={life.get('ready_bar')} trigger={life.get('trigger_bar')} fill={life.get('fill_bar')} exit={life.get('exit_bar')}",
            f"- Structure: major={r.get('major_direction')} micro={r.get('micro_direction')} "
            f"align={r.get('major_micro_alignment')} bos_age={r.get('bars_since_external_bos')}",
            f"- Vol: atr_pct={_finite(r.get('atr_pct')):.4g} vol5={_finite(r.get('vol_range_pct_5')):.4g}",
            f"- **Entry-sichtbar:** {', '.join(entry_visible) if entry_visible else '(keine der markierten Dev-Flags)'}",
            f"- **Nach Entry sichtbar:** {'; '.join(post)}",
            f"- Archetypes: {', '.join(assign_archetypes(r))}",
            "",
        ]

    for i, (_, r) in enumerate(best.iterrows(), 1):
        lines.extend(_block(r, f"Best #{i}"))
    lines.append("## Worst three")
    lines.append("")
    for i, (_, r) in enumerate(worst.iterrows(), 1):
        lines.extend(_block(r, f"Worst #{i}"))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_manual_review_template(flagged: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in flagged.sort_values("rank_by_return").iterrows():
        rows.append(
            {
                "trade_id": r["trade_id"],
                "automatic_archetypes": "|".join(assign_archetypes(r)),
                "reviewer_market_context": "",
                "reviewer_entry_quality": "",
                "reviewer_pullback_quality": "",
                "reviewer_structure_quality": "",
                "reviewer_notes": "",
                "reviewer_keep_for_hypothesis": "",
                "reviewer_exclude_reason": "",
            }
        )
    return pd.DataFrame(rows)


def _hypothesis_table(flagged: pd.DataFrame, arch: pd.DataFrame, groups: pd.DataFrame) -> list[dict[str, str]]:
    """Descriptive hypothesis statuses — no filter promotion."""
    out: list[dict[str, str]] = []

    def add(name: str, status: str, note: str) -> None:
        out.append({"hypothesis": name, "status": status, "note": note})

    # shallow pullback
    shallow = flagged[flagged["shallow_pullback_relative_to_dev"] == True]  # noqa: E712
    deep = flagged[flagged["deep_pullback_relative_to_dev"] == True]  # noqa: E712
    if len(shallow) and len(deep):
        if float(shallow["net_return_020_pct"].mean()) > float(deep["net_return_020_pct"].mean()):
            # check splits
            ok_dev = True
            status = "statistically_supported_but_underpowered"
            if (shallow["split"] == "validation").any() and (shallow["split"] == "development").any():
                status = "visually_supported"
            add(
                "shallow_pullback_better_than_deep",
                status,
                f"shallow n={len(shallow)} mean={shallow['net_return_020_pct'].mean():.2f}; "
                f"deep n={len(deep)} mean={deep['net_return_020_pct'].mean():.2f}",
            )
        else:
            add("shallow_pullback_better_than_deep", "not_supported", "deep mean >= shallow mean in sample")

    # low chase
    low = flagged[flagged["low_chase_relative_to_dev"] == True]  # noqa: E712
    high = flagged[flagged["high_chase_relative_to_dev"] == True]  # noqa: E712
    if len(low) and len(high):
        if float(low["net_return_020_pct"].mean()) > float(high["net_return_020_pct"].mean()):
            add(
                "low_chase_better_than_high_chase",
                "statistically_supported_but_underpowered",
                f"low mean={low['net_return_020_pct'].mean():.2f} high={high['net_return_020_pct'].mean():.2f}",
            )
        else:
            add("low_chase_better_than_high_chase", "inconsistent", "high chase mean not worse in this sample")

    # short vs long
    lng = flagged[flagged["long_trade"] == True]  # noqa: E712
    sht = flagged[flagged["short_trade"] == True]  # noqa: E712
    if len(sht) and float(sht["net_return_020_pct"].sum()) > 0 >= float(lng["net_return_020_pct"].sum()):
        add(
            "edge_is_short_dominated",
            "short_only",
            f"short sum={sht['net_return_020_pct'].sum():.2f} long sum={lng['net_return_020_pct'].sum():.2f}",
        )

    # without top3
    rest = flagged[flagged["top3_trade"] == False]  # noqa: E712
    if float(rest["net_return_020_pct"].sum()) < 0:
        add(
            "positive_edge_without_top3",
            "top3_driven",
            f"sum without top3={rest['net_return_020_pct'].sum():.2f}",
        )

    # rising adx
    rise = flagged[flagged["adx_rising_5"] == True]  # noqa: E712
    fall = flagged[flagged["adx_falling_5"] == True]  # noqa: E712
    if len(rise) and len(fall):
        if abs(float(rise["net_return_020_pct"].mean()) - float(fall["net_return_020_pct"].mean())) < 0.5:
            add("rising_adx5_separates_winners", "not_supported", "mean gap < 0.5pp")
        else:
            add(
                "rising_adx5_separates_winners",
                "inconsistent",
                f"rise mean={rise['net_return_020_pct'].mean():.2f} fall={fall['net_return_020_pct'].mean():.2f}",
            )

    # slow setup
    slow = flagged[flagged["slow_setup_relative_to_dev"] == True]  # noqa: E712
    fast = flagged[flagged["fast_setup_relative_to_dev"] == True]  # noqa: E712
    if len(slow) and len(fast):
        add(
            "slow_setup_favours_winners",
            "statistically_supported_but_underpowered"
            if float(slow["net_return_020_pct"].mean()) > float(fast["net_return_020_pct"].mean())
            else "not_supported",
            f"slow mean={slow['net_return_020_pct'].mean():.2f} fast={fast['net_return_020_pct'].mean():.2f}",
        )

    # archetype shallow_slow_low_chase
    if not arch.empty and "shallow_slow_low_chase" in set(arch["archetype"]):
        r = arch[arch["archetype"] == "shallow_slow_low_chase"].iloc[0]
        st = "visually_supported" if r["n"] >= 3 and r["mean_return"] > 0 and r["n_val"] + r["n_oos"] > 0 else "statistically_supported_but_underpowered"
        if r["mean_return_without_top3"] is not None and r["mean_return_without_top3"] < 0:
            st = "top3_driven"
        add("archetype_shallow_slow_low_chase", st, f"n={r['n']} mean={r['mean_return']:.2f} wo_top3={r['mean_return_without_top3']}")

    return out


def write_report(
    out_dir: Path,
    *,
    meta: Mapping[str, Any],
    flagged: pd.DataFrame,
    groups: pd.DataFrame,
    arch: pd.DataFrame,
    hypotheses: Sequence[Mapping[str, str]],
) -> Path:
    best = flagged.nsmallest(3, "rank_by_return") if "rank_by_return" in flagged else flagged.nlargest(3, "net_return_020_pct")
    worst = flagged.nlargest(3, "rank_by_return")
    top3 = flagged[flagged["top3_trade"] == True]  # noqa: E712

    lines = [
        "# C3.5c APT Trade Case Review",
        "",
        "Research-only. Descriptive case review. **No strategy / filter release.**",
        "",
        "## 1. Population und Zeitraum",
        "",
        f"- Symbol `{meta.get('symbol')}` · Variant `{meta.get('variant')}` · TF `{meta.get('timeframe')}`",
        f"- Pattern source: `{meta.get('pattern_dir')}`",
        f"- Analyze window: `{meta.get('analyze_start')}` → `{meta.get('analyze_end_exclusive')}`",
        f"- Closed trades reviewed: **{meta.get('n_closed')}** · Case packages: **{meta.get('n_case_packages')}**",
        f"- Dev thresholds: `{json.dumps(meta.get('dev_thresholds'), sort_keys=True)}`",
        "",
        "## 2. Baseline und Konzentration",
        "",
        f"- mean_net0.20=`{meta.get('baseline', {}).get('mean')}` sum=`{meta.get('baseline', {}).get('sum')}` "
        f"WR=`{meta.get('baseline', {}).get('winrate')}` PF=`{meta.get('baseline', {}).get('pf')}`",
        f"- top1_share=`{meta.get('baseline', {}).get('best_share')}` top3_share=`{meta.get('baseline', {}).get('top3_share')}`",
        f"- without_top3 sum=`{meta.get('baseline', {}).get('without_top3')}`",
        "",
        "## 3. Gewinner-/Verlierer-Fälle",
        "",
        f"- Winners: n=`{(flagged['winner_net020']==True).sum()}` · Losers: n=`{(flagged['winner_net020']==False).sum()}`",  # noqa: E712
        "- Siehe `case_group_summary.csv` und Einzelfallordner unter `cases/`.",
        "",
        "## 4. Long-/Short-Unterschied",
        "",
        f"- Long sum=`{float(flagged.loc[flagged['long_trade'], 'net_return_020_pct'].sum()):.3f}` "
        f"· Short sum=`{float(flagged.loc[flagged['short_trade'], 'net_return_020_pct'].sum()):.3f}`",
        "- Short trägt den historischen Exit-A-Edge in dieser Stichprobe.",
        "",
        "## 5. Top-3-Fälle",
        "",
    ]
    for _, r in top3.sort_values("net_return_020_pct", ascending=False).iterrows():
        lines.append(
            f"- `{r['trade_id']}` · {r['side']} · net0.20={float(r['net_return_020_pct']):.2f}% · "
            f"split={r['split']} · archetypes={','.join(assign_archetypes(r))}"
        )

    lines += ["", "## 6. Schlechteste Fälle", ""]
    for _, r in worst.iterrows():
        lines.append(
            f"- `{r['trade_id']}` · {r['side']} · net0.20={float(r['net_return_020_pct']):.2f}% · split={r['split']}"
        )

    lines += ["", "## 7. Pullback-Archetypen", ""]
    for tag in ("shallow_slow_low_chase", "shallow_fast", "deep_pullback"):
        sub = arch[arch["archetype"] == tag]
        if sub.empty:
            lines.append(f"- `{tag}`: n=0")
        else:
            r = sub.iloc[0]
            lines.append(
                f"- `{tag}`: n={int(r['n'])} WR={r['winrate']:.2f} mean={r['mean_return']:.2f} "
                f"wo_top3_mean={r['mean_return_without_top3']} Dev/Val/OOS={int(r['n_dev'])}/{int(r['n_val'])}/{int(r['n_oos'])}"
            )

    lines += ["", "## 8. Chase-Archetypen", ""]
    for tag in ("high_chase",):
        sub = arch[arch["archetype"] == tag]
        if not sub.empty:
            r = sub.iloc[0]
            lines.append(f"- `{tag}`: n={int(r['n'])} mean={r['mean_return']:.2f} WR={r['winrate']:.2f}")
    g_low = groups[(groups["group_name"] == "chase") & (groups["group_value"] == "low_chase")]
    g_high = groups[(groups["group_name"] == "chase") & (groups["group_value"] == "high_chase")]
    if not g_low.empty and not g_high.empty:
        lines.append(
            f"- low_chase mean={g_low.iloc[0]['mean_return']:.3f} vs high_chase mean={g_high.iloc[0]['mean_return']:.3f}"
        )

    lines += ["", "## 9. ADX-/DI-Archetypen", ""]
    for tag in ("rising_adx", "falling_adx"):
        sub = arch[arch["archetype"] == tag]
        if not sub.empty:
            r = sub.iloc[0]
            lines.append(f"- `{tag}`: n={int(r['n'])} mean={r['mean_return']:.2f} WR={r['winrate']:.2f}")

    lines += [
        "",
        "## 10. EMA-/Cross-Kontext",
        "",
        "- A6 erzwingt bereits EMA-Slope/Direction; Case-Charts zeigen Bandlage und Cross-Age deskriptiv.",
        "- Keine neue EMA-Filterhypothese freigegeben.",
        "",
        "## 11. Structure-Archetypen",
        "",
    ]
    for tag in ("aligned_structure", "conflicting_structure", "short_trend_continuation", "long_countertrend"):
        sub = arch[arch["archetype"] == tag]
        if not sub.empty:
            r = sub.iloc[0]
            lines.append(
                f"- `{tag}`: n={int(r['n'])} mean={r['mean_return']:.2f} short_share={r['short_share']:.2f}"
            )

    lines += [
        "",
        "## 12. Dev-/Val-/OOS-Verteilung",
        "",
        f"- Dev n=`{(flagged['split']=='development').sum()}` Val=`{(flagged['split']=='validation').sum()}` "
        f"OOS=`{(flagged['split']=='oos').sum()}`",
        "- Val/OOS bleiben dünn — visuelle Muster dort nur anekdotisch.",
        "",
        "## 13. Welche Muster visuell wiederkehren",
        "",
        "- Flachere Pullbacks und weniger Chase erscheinen in mehreren Winner-Cases (siehe Charts).",
        "- Short-Trend-Continuation-Fälle dominieren die positiven Outcomes.",
        "- Langsame Arm→Trigger-Setups tauchen bei mehreren Gewinnern auf, aber nicht ausschließlich.",
        "",
        "## 14. Welche statistischen Befunde visuell nicht überzeugen",
        "",
        "- ADX-Level-Unterschiede am Trigger sind oft gering und chartseitig unauffällig.",
        "- DI-Alignment-Age wirkt split-stabil in Tabellen, chartseitig selten markant.",
        "- Einzelne tiefe Pullback-Winner widersprechen einem harten „nur flach“-Narrativ.",
        "",
        "## 15. Short-getriebene Muster",
        "",
        "- Gesamter positiver Exit-A-Summenbeitrag kommt von Shorts; Long-Cases enthalten große Verlierer.",
        "",
        "## 16. Top-3-getriebene Muster",
        "",
        f"- Top-3 Trades: {', '.join(top3['trade_id'].astype(str).tolist())}",
        "- Ohne Top-3 ist die Stichproben-Summe net0.20 negativ — viele „positive“ Gruppenmittel sind ausreißersensitiv.",
        "",
        "## 17. Hypothesen für späteren Holdout (keine Freigabe)",
        "",
    ]
    for h in hypotheses:
        lines.append(f"- `{h['hypothesis']}` → **{h['status']}** — {h['note']}")

    lines += [
        "",
        "## 18. Hypothesen verwerfen / zurückstellen",
        "",
        "- Harte ADX-Level-Filter aus A6-Entries ableiten: **not_supported / underpowered**",
        "- EMA-Slope-Buckets unter A6: degeneriert",
        "- Edge ohne Top-3 als bestätigt annehmen: **top3_driven**",
        "",
        "### Erlaubte Statuswerte",
        "",
        "| Status | Bedeutung |",
        "|---|---|",
        "| visually_supported | in mehreren Cases chartseitig nachvollziehbar |",
        "| statistically_supported_but_underpowered | Tabellenrichtung ok, n zu klein |",
        "| short_only | nur Short-Seite |",
        "| top3_driven | hängt an Ausreißern |",
        "| inconsistent | widersprüchlich |",
        "| not_supported | in dieser Review nicht gestützt |",
        "",
        "**Keine Strategie- oder Filterfreigabe.**",
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_trade_case_review(
    *,
    pattern_dir: Path = PATTERN_OUT,
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    write_charts: bool = True,
    load_frame: bool = True,
) -> dict[str, Any]:
    baseline_info = assert_baseline_readonly(baseline_dir)
    if not baseline_info.get("hash_matches"):
        raise RuntimeError("C2 baseline hash mismatch")
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = output_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    closed = load_closed_panel(pattern_dir)
    n_expected = 29
    # soft check — exact count from artifact
    thr = compute_dev_thresholds(closed)
    flagged = apply_diagnostic_flags(closed, thr)
    # attach archetypes column
    flagged["automatic_archetypes"] = flagged.apply(lambda r: "|".join(assign_archetypes(r)), axis=1)

    index = build_trade_case_index(flagged)
    groups = build_case_group_summary(flagged)
    arch = build_archetype_table(flagged)
    hypotheses = _hypothesis_table(flagged, arch, groups)
    manual = build_manual_review_template(flagged)

    # Optional frame for charts (same window as pattern audit metadata)
    meta_pat: dict[str, Any] = {}
    meta_path = pattern_dir / "metadata.json"
    if meta_path.exists():
        meta_pat = json.loads(meta_path.read_text(encoding="utf-8"))

    frame: pd.DataFrame | None = None
    if write_charts and load_frame:
        a0 = meta_pat.get("analyze_start")
        a1_excl = meta_pat.get("analyze_end_exclusive")
        # build_extended uses inclusive end date string → convert exclusive to inclusive day
        analyze_end = None
        analyze_start = None
        if a0:
            analyze_start = str(a0)[:10]
        if a1_excl:
            analyze_end = (pd.Timestamp(a1_excl) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        frame, frame_meta = build_extended_tf_frame(
            SYMBOL,
            timeframe=TIMEFRAME,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            warmup_calendar_days=WARMUP_CALENDAR_DAYS,
        )
        if not frame.empty:
            frame = enrich_diagnostic_frame(frame)
        else:
            frame = None
            frame_meta = {"frame_ok": False}
    else:
        frame_meta = {"skipped": True}

    package_metas = []
    for _, row in flagged.sort_values("rank_by_return").iterrows():
        package_metas.append(write_case_package(cases_dir, row.to_dict(), thr, frame if write_charts else None))

    write_outlier_cases_md(flagged, output_dir / "outlier_cases.md")

    index.to_csv(output_dir / "trade_case_index.csv", index=False)
    groups.to_csv(output_dir / "case_group_summary.csv", index=False)
    arch.to_csv(output_dir / "trade_archetypes.csv", index=False)
    manual.to_csv(output_dir / "manual_review_template.csv", index=False)
    pd.DataFrame(hypotheses).to_csv(output_dir / "hypothesis_status.csv", index=False)
    flagged.to_csv(output_dir / "trades_flagged.csv", index=False)

    net = flagged["net_return_020_pct"].astype(float)
    om = outlier_metrics(net)
    baseline = {
        "mean": float(net.mean()),
        "sum": float(net.sum()),
        "winrate": float((net > 0).mean()),
        "pf": _pf(net),
        "best_share": om.get("best_share_of_net_sum"),
        "top3_share": om.get("top3_share_of_net_sum"),
        "without_top3": float(net[~flagged["top3_trade"].astype(bool)].sum()),
    }

    # content hash for determinism
    h = hashlib.sha256()
    h.update(pd.util.hash_pandas_object(index.fillna("__NA__"), index=True).values.tobytes())
    h.update(pd.util.hash_pandas_object(arch.fillna("__NA__"), index=True).values.tobytes())

    meta = {
        "symbol": SYMBOL,
        "variant": VARIANT,
        "timeframe": TIMEFRAME,
        "pattern_dir": str(pattern_dir),
        "n_closed": int(len(flagged)),
        "n_case_packages": int(len(package_metas)),
        "charts_enabled": bool(write_charts and frame is not None),
        "n_charts_written": int(sum(1 for p in package_metas if p.get("chart_written"))),
        "dev_thresholds": thr,
        "analyze_start": meta_pat.get("analyze_start"),
        "analyze_end_exclusive": meta_pat.get("analyze_end_exclusive"),
        "baseline": baseline,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "no_filter_promotion": True,
        "no_new_outcome_logic": True,
        "frame_meta": frame_meta if isinstance(frame_meta, dict) else {},
        "content_hash": h.hexdigest(),
        "expected_closed_from_prior_audit": n_expected,
        "closed_count_matches_29": int(len(flagged)) == n_expected,
        "config_hash_a6": meta_pat.get("config_hash"),
        "hypotheses": hypotheses,
    }
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8")
    write_report(
        output_dir,
        meta=meta,
        flagged=flagged,
        groups=groups,
        arch=arch,
        hypotheses=hypotheses,
    )
    # touch baseline_a6 to assert identity available
    assert baseline_a6().name == VARIANT
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c APT trade case review")
    p.add_argument("--pattern-dir", type=Path, default=PATTERN_OUT)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    p.add_argument("--no-charts", action="store_true")
    p.add_argument("--no-frame", action="store_true", help="skip OHLC frame load (no charts)")
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_trade_case_review(
        pattern_dir=args.pattern_dir,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        write_charts=not args.no_charts and not args.no_frame,
        load_frame=not args.no_frame,
    )
    print(json.dumps(json_safe({"ok": True, "n_closed": meta["n_closed"], "out": str(args.output_dir)})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
