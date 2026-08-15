"""Build post-entry path checkpoints for stored A6 signals (causal, additive).

Checkpoint semantics
--------------------
* Fill at open of bar_0 (fill candle).
* Checkpoint N (1..4) = after close of bar with bars_since_fill = N-1.
* Only candles up to that close are used (no future MFE/MAE / exit reason).
* If bars_held < N-1 → availability = not_available_due_to_prior_exit.
* Early-exit simulation (audit) exits at next open after checkpoint close.

Reuses D2 ``step_post_entry`` / ``_micro_counter_bos`` and multicoin ``path_types``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.c35c_signal_store.build import (
    build_15m_a6,
    load_symbol_5m_mysql,
    resolve_analyze_window,
)
from research.regime_scanner.c35c_signal_store.path_schema import (
    CHECKPOINT_SEMANTICS,
    DEFAULT_CHECKPOINT_BARS,
    DEFAULT_PATH_VERSION,
)
from research.regime_scanner.pullback_entry_c3_5 import _finite
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import signed_return_pct
from research.regime_scanner.pullback_entry_c3_5c_multicoin_signal_failure_feature_audit import (
    path_types,
)
from research.regime_scanner.pullback_entry_c3_5d_post_entry import (
    ContinuationPostEntryRuntime,
    FillSnapshot,
    PostEntryD2Config,
    step_post_entry,
)


def _side_sign(direction: str | int | None) -> int:
    if isinstance(direction, (int, float)) and not isinstance(direction, bool):
        return 1 if int(direction) > 0 else -1
    d = str(direction or "").lower()
    if d in ("long", "1", "bull"):
        return 1
    if d in ("short", "-1", "bear"):
        return -1
    return 0


def _micro_counter_choch(row: dict[str, Any], *, side: int) -> bool:
    """CHOCH against trade (available on prepare_research_frame; not in D2 helper)."""
    if side > 0:
        return bool(row.get("arm_edge_choch_bear")) or str(row.get("choch_side") or "") == "bear"
    if side < 0:
        return bool(row.get("arm_edge_choch_bull")) or str(row.get("choch_side") or "") == "bull"
    return False


def _ema_aligned(side: int, ema9: float | None, ema20: float | None) -> bool | None:
    if ema9 is None or ema20 is None:
        return None
    if not (math.isfinite(ema9) and math.isfinite(ema20)):
        return None
    if side > 0:
        return bool(ema9 >= ema20)
    return bool(ema9 <= ema20)


def _candle_dir(o: float, c: float) -> str:
    if c > o:
        return "bull"
    if c < o:
        return "bear"
    return "flat"


def build_fill_snapshot_from_store(
    *,
    signal: dict[str, Any],
    trigger_feat: dict[str, Any] | None,
    fill_feat: dict[str, Any] | None,
    fill_bar: int,
    fill_row: dict[str, Any],
) -> FillSnapshot:
    """Freeze levels from stored features + fill-bar structure (no A6 re-run needed)."""
    side = _side_sign(signal.get("direction"))
    trig = trigger_feat or {}
    fill = fill_feat or {}
    brk = _finite(trig.get("breakout_level") or fill.get("breakout_level"))
    atr = _finite(fill.get("atr") or trig.get("atr") or fill_row.get("atr_14"))
    if side > 0:
        prot = _finite(fill.get("protected_low") or fill_row.get("protected_low"))
        pb_low = _finite(trig.get("pullback_low") or fill.get("pullback_low"))
        pb_high = _finite(trig.get("pullback_high") or fill.get("pullback_high"))
    else:
        prot = _finite(fill.get("protected_high") or fill_row.get("protected_high"))
        pb_low = _finite(trig.get("pullback_low") or fill.get("pullback_low"))
        pb_high = _finite(trig.get("pullback_high") or fill.get("pullback_high"))
    meta = signal.get("metadata_json") or {}
    if isinstance(meta, str):
        import json

        try:
            meta = json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    return FillSnapshot(
        setup_id=int(signal.get("setup_id") or meta.get("setup_id") or 0),
        direction="long" if side > 0 else "short",
        side=side,
        arming_type=str(meta.get("arming_type") or "unknown"),
        trigger_bar=int(meta.get("trigger_bar") or max(0, fill_bar - 1)),
        trigger_timestamp=signal.get("timestamp"),
        fill_bar=fill_bar,
        fill_timestamp=signal.get("entry_time"),
        entry_price=float(signal["entry_price"]),
        setup_protected_level=prot,
        entry_protected_level=prot,
        entry_protected_side="low" if side > 0 else "high",
        frozen_breakout_level=brk,
        frozen_pullback_high=pb_high,
        frozen_pullback_low=pb_low,
        frozen_prior_swing_high=None,
        frozen_prior_swing_low=None,
        frozen_micro_swing_high=None,
        frozen_micro_swing_low=None,
        frozen_atr_14=atr,
        frozen_ltf_major_at_fill=(
            int(fill_row["major_direction"])
            if fill_row.get("major_direction") is not None
            and pd.notna(fill_row.get("major_direction"))
            else None
        ),
        frozen_htf_major_at_fill=None,
        atr_available=bool(atr is not None and atr > 0),
    )


def _row_dict(frame: pd.DataFrame, i: int) -> dict[str, Any]:
    row = frame.iloc[i]
    out = {k: row[k] for k in frame.columns}
    out["bar_index"] = int(i)
    return out


def compute_checkpoints_for_signal(
    *,
    signal: dict[str, Any],
    outcome: dict[str, Any] | None,
    trigger_feat: dict[str, Any] | None,
    fill_feat: dict[str, Any] | None,
    frame: pd.DataFrame,
    path_version: str = DEFAULT_PATH_VERSION,
    checkpoints: tuple[int, ...] = DEFAULT_CHECKPOINT_BARS,
) -> list[dict[str, Any]]:
    """Return one row per requested checkpoint (ok or not_available_*)."""
    entry_ts = pd.Timestamp(signal["entry_time"])
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("UTC")
    else:
        entry_ts = entry_ts.tz_convert("UTC")
    fts = pd.to_datetime(frame["timestamp"], utc=True)
    matches = np.where(fts == entry_ts)[0]
    if len(matches) == 0:
        # nearest fallback within 1 minute
        deltas = (fts - entry_ts).abs()
        j = int(deltas.argmin())
        if deltas.iloc[j] > pd.Timedelta(minutes=1):
            return [
                {
                    "signal_id": signal["id"],
                    "run_id": signal["run_id"],
                    "path_version": path_version,
                    "checkpoint_bar": cp,
                    "availability": "not_available_fill_bar_missing",
                    "feature_json": {"error": "fill_bar_missing", "entry_time": str(entry_ts)},
                }
                for cp in checkpoints
            ]
        fill_i = j
    else:
        fill_i = int(matches[0])

    side = _side_sign(signal.get("direction"))
    entry = float(signal["entry_price"])
    bars_held = None if outcome is None else outcome.get("bars_held")
    try:
        bars_held_i = None if bars_held is None or (isinstance(bars_held, float) and math.isnan(bars_held)) else int(bars_held)
    except (TypeError, ValueError):
        bars_held_i = None

    fill_row = _row_dict(frame, fill_i)
    snap = build_fill_snapshot_from_store(
        signal=signal,
        trigger_feat=trigger_feat,
        fill_feat=fill_feat,
        fill_bar=fill_i,
        fill_row=fill_row,
    )
    mon = ContinuationPostEntryRuntime(snap=snap)
    cfg = PostEntryD2Config(post_entry_horizon_bars=max(checkpoints) + 2)

    # running candle-seq stats across closed post-fill bars
    adverse_count = 0
    favorable_count = 0
    dir_changes = 0
    prev_dir: str | None = None
    max_high = float("-inf")
    min_low = float("inf")
    entry_lost_ever = False
    entry_reclaimed_ever = False
    entry_was_lost = False
    micro_choch_ever = False
    ema_aligned_at_fill: bool | None = None
    ema_lost_ever = False
    timeline_by_bsf: dict[int, dict[str, Any]] = {}

    max_cp = max(checkpoints)
    n = len(frame)
    for offset in range(0, max_cp):
        bi = fill_i + offset
        if bi >= n:
            break
        row = _row_dict(frame, bi)
        step = step_post_entry(mon, row, cfg=cfg)
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        max_high = max(max_high, h)
        min_low = min(min_low, l)
        cd = _candle_dir(o, c)
        fav_close = signed_return_pct(side, entry, c)
        if fav_close < 0:
            adverse_count += 1
        elif fav_close > 0:
            favorable_count += 1
        if prev_dir is not None and cd != "flat" and prev_dir != "flat" and cd != prev_dir:
            dir_changes += 1
        if cd != "flat":
            prev_dir = cd

        # entry lost / reclaim on close
        if side > 0:
            lost_now = c < entry
        else:
            lost_now = c > entry
        if lost_now:
            entry_lost_ever = True
            entry_was_lost = True
        elif entry_was_lost:
            entry_reclaimed_ever = True
            entry_was_lost = False

        if _micro_counter_choch(row, side=side):
            micro_choch_ever = True

        e9 = _finite(row.get("ema_9") or row.get("ema9"))
        e20 = _finite(row.get("ema_20") or row.get("ema20"))
        aligned = _ema_aligned(side, e9, e20)
        if offset == 0:
            ema_aligned_at_fill = aligned
        if aligned is False:
            ema_lost_ever = True

        atr = snap.frozen_atr_14
        body = abs(c - o)
        rng = h - l
        step["_extra"] = {
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "ema9": e9,
            "ema20": e20,
            "ema9_20_aligned": aligned,
            "ema9_20_lost": bool(ema_lost_ever),
            "adx": _finite(row.get("adx")),
            "di_plus": _finite(row.get("plus_di") or row.get("di_plus")),
            "di_minus": _finite(row.get("minus_di") or row.get("di_minus")),
            "checkpoint_candle_direction": cd,
            "checkpoint_body_atr": (body / atr) if atr and atr > 0 else None,
            "checkpoint_range_atr": (rng / atr) if atr and atr > 0 else None,
            "close_location_in_range": ((c - l) / rng) if rng > 0 else None,
            "adverse_candle_count": adverse_count,
            "favorable_candle_count": favorable_count,
            "direction_change_count": dir_changes,
            "max_high_so_far": max_high if max_high != float("-inf") else None,
            "min_low_so_far": min_low if min_low != float("inf") else None,
            "entry_lost": entry_lost_ever,
            "entry_reclaimed": entry_reclaimed_ever,
            "micro_counter_choch": micro_choch_ever,
            "major_structure_opposed": bool(step.get("ltf_major_alignment_is_lost")),
        }
        timeline_by_bsf[int(step["bars_since_fill"])] = step

    rows: list[dict[str, Any]] = []
    for cp in checkpoints:
        bsf = cp - 1
        if bars_held_i is not None and bars_held_i < bsf:
            rows.append(
                {
                    "signal_id": int(signal["id"]),
                    "run_id": str(signal["run_id"]),
                    "path_version": path_version,
                    "checkpoint_bar": int(cp),
                    "bars_since_fill": bsf,
                    "availability": "not_available_due_to_prior_exit",
                    "feature_json": {
                        "bars_held": bars_held_i,
                        "exit_reason": None if outcome is None else outcome.get("exit_reason"),
                        "semantics": CHECKPOINT_SEMANTICS,
                    },
                }
            )
            continue
        if fill_i + bsf >= n:
            rows.append(
                {
                    "signal_id": int(signal["id"]),
                    "run_id": str(signal["run_id"]),
                    "path_version": path_version,
                    "checkpoint_bar": int(cp),
                    "bars_since_fill": bsf,
                    "availability": "not_available_data_end",
                    "feature_json": {"fill_i": fill_i, "n": n, "semantics": CHECKPOINT_SEMANTICS},
                }
            )
            continue
        step = timeline_by_bsf.get(bsf)
        if step is None:
            rows.append(
                {
                    "signal_id": int(signal["id"]),
                    "run_id": str(signal["run_id"]),
                    "path_version": path_version,
                    "checkpoint_bar": int(cp),
                    "bars_since_fill": bsf,
                    "availability": "not_available_internal",
                    "feature_json": {"error": "missing_timeline"},
                }
            )
            continue
        extra = step.get("_extra") or {}
        mfe_pct = (float(step["mfe_price"]) / entry) * 100.0 if entry else None
        mae_pct = (float(step["mae_price"]) / entry) * 100.0 if entry else None
        # directional close return already signed in step
        dcr = float(step["signed_close_return"]) * 100.0
        close_ret = signed_return_pct(side, entry, float(extra["close"]))
        di_p = extra.get("di_plus")
        di_m = extra.get("di_minus")
        if di_p is not None and di_m is not None:
            raw_spread = float(di_p) - float(di_m)
            dir_di = raw_spread if side > 0 else -raw_spread
        else:
            dir_di = None
        no_pos_mfe = bool(mfe_pct is not None and mfe_pct <= 0)
        small_mfe = bool(mfe_pct is not None and mfe_pct < 0.25)
        deep_mae = bool(mae_pct is not None and mae_pct <= -0.50)
        exit_on_cp = bool(bars_held_i is not None and bars_held_i == bsf)
        next_i = fill_i + bsf + 1
        next_open = None
        next_ts = None
        if next_i < n:
            next_open = float(frame.iloc[next_i]["open"])
            next_ts = frame.iloc[next_i]["timestamp"]
        still_open_after_cp = bars_held_i is None or bars_held_i > bsf
        rows.append(
            {
                "signal_id": int(signal["id"]),
                "run_id": str(signal["run_id"]),
                "path_version": path_version,
                "checkpoint_bar": int(cp),
                "checkpoint_timestamp": step.get("timestamp"),
                "checkpoint_close": float(extra["close"]),
                "bars_since_fill": bsf,
                "close_return_pct": float(close_ret),
                "directional_close_return_pct": dcr,
                "mfe_so_far_pct": mfe_pct,
                "mae_so_far_pct": mae_pct,
                "mfe_so_far_atr": step.get("mfe_atr"),
                "mae_so_far_atr": step.get("mae_atr"),
                "max_high_so_far": extra.get("max_high_so_far"),
                "min_low_so_far": extra.get("min_low_so_far"),
                "entry_reclaimed": int(bool(extra.get("entry_reclaimed"))),
                "entry_lost": int(bool(extra.get("entry_lost"))),
                "breakout_level_lost": int(bool(step.get("breakout_level_is_lost"))),
                "breakout_level_reclaimed": int(bool(step.get("breakout_level_reclaimed"))),
                "protected_level_broken": int(bool(step.get("entry_protected_level_is_broken"))),
                "ema9": extra.get("ema9"),
                "ema20": extra.get("ema20"),
                "ema9_20_aligned": None if extra.get("ema9_20_aligned") is None else int(bool(extra.get("ema9_20_aligned"))),
                "ema9_20_lost": int(bool(extra.get("ema9_20_lost"))),
                "adx": extra.get("adx"),
                "di_plus": di_p,
                "di_minus": di_m,
                "directional_di_spread": dir_di,
                "micro_counter_bos": int(bool(step.get("micro_counter_bos"))),
                "micro_counter_choch": int(bool(extra.get("micro_counter_choch"))),
                "major_structure_opposed": int(bool(extra.get("major_structure_opposed"))),
                "checkpoint_candle_direction": extra.get("checkpoint_candle_direction"),
                "checkpoint_body_atr": extra.get("checkpoint_body_atr"),
                "checkpoint_range_atr": extra.get("checkpoint_range_atr"),
                "close_location_in_range": extra.get("close_location_in_range"),
                "adverse_candle_count": int(extra.get("adverse_candle_count") or 0),
                "favorable_candle_count": int(extra.get("favorable_candle_count") or 0),
                "direction_change_count": int(extra.get("direction_change_count") or 0),
                "no_positive_mfe": int(no_pos_mfe),
                "small_mfe": int(small_mfe),
                "deep_mae": int(deep_mae),
                "availability": "ok",
                "feature_json": {
                    "fill_bar": fill_i,
                    "exit_on_checkpoint_bar": exit_on_cp,
                    "still_open_after_checkpoint": still_open_after_cp,
                    "bars_held": bars_held_i,
                    "next_open_price": next_open,
                    "next_open_timestamp": None if next_ts is None else str(next_ts),
                    "breakout_level_ever_lost": bool(step.get("breakout_level_ever_lost")),
                    "micro_counter_bos_now": bool(step.get("micro_counter_bos_now")),
                    "ema_aligned_at_fill": ema_aligned_at_fill,
                    "semantics": CHECKPOINT_SEMANTICS,
                    "fav_adv_helper": "D2.step_post_entry+excursion_signed",
                },
            }
        )
    return rows


def build_path_labels_for_panel(
    panel: pd.DataFrame,
    *,
    path_version: str = DEFAULT_PATH_VERSION,
) -> list[dict[str, Any]]:
    """Reuse quantile path_types from multicoin failure audit."""
    labeled = path_types(panel)
    rows = []
    for _, r in labeled.iterrows():
        rows.append(
            {
                "signal_id": int(r["signal_id"]),
                "run_id": str(r["run_id"]),
                "path_version": path_version,
                "path_type": str(r["path_type"]),
                "path_thresholds_json": r.get("path_thresholds"),
                "label_json": {
                    "exit_reason": r.get("exit_reason"),
                    "bars_held": None if pd.isna(r.get("bars_held")) else int(r.get("bars_held")),
                    "net_pnl_pct": None if pd.isna(r.get("net_pnl_pct")) else float(r.get("net_pnl_pct")),
                    "mfe_pct": None if pd.isna(r.get("mfe_pct")) else float(r.get("mfe_pct")),
                    "mae_pct": None if pd.isna(r.get("mae_pct")) else float(r.get("mae_pct")),
                    "same_bar_loser": r.get("exit_reason") == "same_bar_conservative_sl",
                    "early_tp": bool(
                        r.get("exit_reason") == "TP"
                        and pd.notna(r.get("bars_to_tp"))
                        and int(r.get("bars_to_tp")) <= 3
                    ),
                    "early_sl": bool(
                        r.get("exit_reason") in ("SL", "same_bar_conservative_sl")
                        and pd.notna(r.get("bars_held"))
                        and int(r.get("bars_held")) <= 3
                    ),
                    "data_end": bool(r.get("exit_reason") == "data_end"),
                },
            }
        )
    return rows


def load_frame_for_symbol(
    symbol: str,
    *,
    full_history: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    mysql_5m, mysql_meta = load_symbol_5m_mysql(symbol)
    a0, a1 = resolve_analyze_window(mysql_5m)
    frame, _fills, _lives, meta = build_15m_a6(mysql_5m, analyze_start=a0, analyze_end_exclusive=a1)
    return frame, {**mysql_meta, **meta, "analyze_start": str(a0), "analyze_end_exclusive": str(a1)}
