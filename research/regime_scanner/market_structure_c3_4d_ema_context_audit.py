"""Phase C3.4D — additive EMA context × C3.4B structure combined audit.

Descriptive only. Does not mutate C3.4B, C3.5c, Pine, or hedge-bot logic.
No threshold optimization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import (
    load_ohlcv_with_warmup,
    required_indicator_warmup_bars,
)
from research.regime_scanner.market_structure_c3_4d_ema_context import (
    BEARISH,
    BULLISH,
    GUARD_FORMULAS,
    STRUCTURE_IMMUTABLE_COLS,
    attach_structure_ema_relation,
    compute_c3_4d_ema_context,
    guard_decision,
    lookup_closed_htf_row,
    semantics_doc,
    structure_columns_hash,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    apply_pullback_entry,
    config_hash,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_c34b_4h_trend_audit import (
    DEFAULT_OUT as C34B_4H_AUDIT_DIR,
    build_c34b_htf_frame,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    DEFAULT_OUT as EXCURSION_DIR,
)
from research.regime_scanner.pullback_entry_c3_5c_htf_trend_alignment_audit import (
    recovery_and_risk_proxy,
)
from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    _filled_sorted,
    trades_exit_a_opposite_entry,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    DEFAULT_BASELINE_DIR,
    WARMUP_CALENDAR_DAYS,
    assign_split,
    build_extended_tf_frame,
    closed_only,
    fixed_chrono_splits,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_4d_ema_context")

SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
VARIANT = "A6"
BAR_MINUTES = 15
H4_MINUTES = 240

# Canonical heavy-MAE definition from prior C3.4B 4h audit (MAE <= -10%).
HEAVY_MAE_THRESHOLD = -10.0

GUARD_ORDER = ("G0", "G1", "G1b", "G1c")


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _pct(series: pd.Series, q: float) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return None
    return float(s.quantile(q))


def forward_close_return_pct(
    *,
    side: int,
    entry: float,
    fill_i: int,
    closes: np.ndarray,
    hours: float,
    bar_minutes: int = BAR_MINUTES,
) -> float:
    """Signed close return at horizon (causal, no future beyond available bars)."""
    n = len(closes)
    bars = max(1, int(round(hours * 60 / bar_minutes)))
    j = min(n - 1, fill_i + bars - 1)
    if j < fill_i or not math.isfinite(entry) or entry == 0:
        return float("nan")
    raw = (float(closes[j]) - entry) / entry * 100.0
    return raw if side > 0 else -raw


def build_combined_4h_frame(
    full_5m: pd.DataFrame,
    *,
    decision: pd.Timestamp,
    analyze_start: pd.Timestamp,
    analyze_end_exclusive: pd.Timestamp,
) -> tuple[pd.DataFrame, str, str]:
    """C3.4B 4h structure + additive EMA context; returns (frame, pre_hash, post_hash)."""
    struct = build_c34b_htf_frame(
        full_5m,
        "4h",
        decision=decision,
        analyze_start=analyze_start,
        analyze_end_exclusive=analyze_end_exclusive,
    )
    if struct.empty:
        return struct, "", ""
    pre = structure_columns_hash(struct)
    # EMA context on same OHLCV (structure frame already has OHLCV)
    ohlcv_cols = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in struct.columns]
    ema = compute_c3_4d_ema_context(struct[ohlcv_cols].copy())
    combined = attach_structure_ema_relation(struct, ema)
    post = structure_columns_hash(combined)
    if pre != post:
        raise RuntimeError("C3.4B structure columns mutated by C3.4D attach")
    combined["structure_major_direction"] = pd.to_numeric(
        combined["major_direction"], errors="coerce"
    ).fillna(0).astype(int)
    return combined, pre, post


def summarize_relations(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rel, g in panel.groupby("structure_ema_relation", dropna=False):
        mae = pd.to_numeric(g["mae"], errors="coerce")
        mfe = pd.to_numeric(g["mfe"], errors="coerce")
        uw = pd.to_numeric(g["underwater_duration"], errors="coerce")
        heavy = mae <= HEAVY_MAE_THRESHOLD
        rows.append(
            {
                "structure_ema_relation": rel,
                "fill_count": len(g),
                "long_count": int((g["direction"] == "long").sum()),
                "short_count": int((g["direction"] == "short").sum()),
                "median_mae": float(mae.median()) if mae.notna().any() else None,
                "p75_mae": _pct(mae, 0.75),
                "p90_mae": _pct(mae, 0.90),
                "median_mfe": float(mfe.median()) if mfe.notna().any() else None,
                "median_underwater_duration": float(uw.median()) if uw.notna().any() else None,
                "heavy_mae_count": int(heavy.sum()),
                "heavy_mae_rate": float(heavy.mean()) if len(g) else None,
            }
        )
    return pd.DataFrame(rows).sort_values("fill_count", ascending=False)


def countertrend_overlap(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    specs = [
        ("long_against_structure_bearish", (panel["direction"] == "long") & (panel["structure_major_direction"] == BEARISH)),
        ("long_against_ema_regime_bearish", (panel["direction"] == "long") & (panel["ema_regime_direction"] == BEARISH)),
        (
            "long_against_both_bearish",
            (panel["direction"] == "long")
            & (panel["structure_major_direction"] == BEARISH)
            & (panel["ema_regime_direction"] == BEARISH),
        ),
        (
            "long_against_structure_only_not_ema",
            (panel["direction"] == "long")
            & (panel["structure_major_direction"] == BEARISH)
            & (panel["ema_regime_direction"] != BEARISH),
        ),
        (
            "long_against_ema_only_not_structure",
            (panel["direction"] == "long")
            & (panel["ema_regime_direction"] == BEARISH)
            & (panel["structure_major_direction"] != BEARISH),
        ),
        ("short_against_structure_bullish", (panel["direction"] == "short") & (panel["structure_major_direction"] == BULLISH)),
        ("short_against_ema_regime_bullish", (panel["direction"] == "short") & (panel["ema_regime_direction"] == BULLISH)),
        (
            "short_against_both_bullish",
            (panel["direction"] == "short")
            & (panel["structure_major_direction"] == BULLISH)
            & (panel["ema_regime_direction"] == BULLISH),
        ),
        (
            "short_against_structure_only_not_ema",
            (panel["direction"] == "short")
            & (panel["structure_major_direction"] == BULLISH)
            & (panel["ema_regime_direction"] != BULLISH),
        ),
        (
            "short_against_ema_only_not_structure",
            (panel["direction"] == "short")
            & (panel["ema_regime_direction"] == BULLISH)
            & (panel["structure_major_direction"] != BULLISH),
        ),
    ]
    for name, mask in specs:
        sub = panel[mask]
        mae = pd.to_numeric(sub["mae"], errors="coerce")
        rows.append(
            {
                "group": name,
                "n": len(sub),
                "share_of_fills": float(len(sub) / len(panel)) if len(panel) else None,
                "median_mae": float(mae.median()) if len(sub) and mae.notna().any() else None,
                "heavy_mae_count": int((mae <= HEAVY_MAE_THRESHOLD).sum()) if len(sub) else 0,
                "fill_ids": ",".join(sub["fill_id"].astype(str).tolist()),
            }
        )
    return pd.DataFrame(rows)


def cross_vs_structure_lag(frame4h: pd.DataFrame) -> pd.DataFrame:
    """Lag from nearest prior EMA mid/regime cross to structure external BOS / major flip."""
    if frame4h.empty:
        return pd.DataFrame()
    rows = []
    ts = pd.to_datetime(frame4h["timestamp"], utc=True)
    maj = pd.to_numeric(frame4h["major_direction"], errors="coerce").fillna(0).astype(int)
    ext_up = frame4h.get("external_bos_up", pd.Series([False] * len(frame4h))).astype(bool)
    ext_dn = frame4h.get("external_bos_down", pd.Series([False] * len(frame4h))).astype(bool)
    choch = frame4h.get("choch_side", pd.Series([None] * len(frame4h)))
    c2059 = pd.to_numeric(frame4h.get("ema20_59_cross_event", 0), errors="coerce").fillna(0).astype(int)
    c59200 = pd.to_numeric(frame4h.get("ema59_200_cross_event", 0), errors="coerce").fillna(0).astype(int)

    maj_prev = maj.shift(1)
    for i in range(len(frame4h)):
        event_type = None
        if bool(ext_up.iloc[i]):
            event_type = "external_bos_up"
        elif bool(ext_dn.iloc[i]):
            event_type = "external_bos_down"
        elif isinstance(choch.iloc[i], str) and choch.iloc[i] in {"up", "down"}:
            event_type = f"choch_{choch.iloc[i]}"
        elif i > 0 and int(maj.iloc[i]) != int(maj_prev.iloc[i]) and int(maj.iloc[i]) != 0:
            event_type = "major_flip"
        if event_type is None:
            continue

        # nearest prior cross only (no future)
        prior_2059 = None
        prior_59200 = None
        for j in range(i, -1, -1):
            if prior_2059 is None and int(c2059.iloc[j]) != 0:
                prior_2059 = j
            if prior_59200 is None and int(c59200.iloc[j]) != 0:
                prior_59200 = j
            if prior_2059 is not None and prior_59200 is not None:
                break

        def _lag(j: int | None, cross_series: pd.Series) -> dict[str, Any]:
            if j is None:
                return {
                    "cross_bar_time": None,
                    "cross_event": None,
                    "lag_bars": None,
                    "lag_hours": None,
                    "cross_before_or_after_structure": "no_prior_cross",
                }
            lag = i - j
            return {
                "cross_bar_time": ts.iloc[j],
                "cross_event": int(cross_series.iloc[j]),
                "lag_bars": int(lag),
                "lag_hours": float(lag * H4_MINUTES / 60.0),
                "cross_before_or_after_structure": "before_or_same" if lag >= 0 else "after",
            }

        r2059 = _lag(prior_2059, c2059)
        r59200 = _lag(prior_59200, c59200)

        rows.append(
            {
                "structure_event_timestamp": ts.iloc[i],
                "structure_event_type": event_type,
                "structure_major_after": int(maj.iloc[i]),
                "nearest_prior_ema20_59_cross_time": r2059["cross_bar_time"],
                "nearest_prior_ema20_59_cross": r2059["cross_event"],
                "ema20_59_lag_bars": r2059["lag_bars"],
                "ema20_59_lag_hours": r2059["lag_hours"],
                "ema20_59_cross_before_or_after_structure": r2059["cross_before_or_after_structure"],
                "nearest_prior_ema59_200_cross_time": r59200["cross_bar_time"],
                "nearest_prior_ema59_200_cross": r59200["cross_event"],
                "ema59_200_lag_bars": r59200["lag_bars"],
                "ema59_200_lag_hours": r59200["lag_hours"],
                "ema59_200_cross_before_or_after_structure": r59200["cross_before_or_after_structure"],
            }
        )
    return pd.DataFrame(rows)


def summarize_guards(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    heavy_all = pd.to_numeric(panel["mae"], errors="coerce") <= HEAVY_MAE_THRESHOLD
    n_heavy = int(heavy_all.sum())
    for g in GUARD_ORDER:
        col = f"guard_{g}"
        dec = panel[col]
        allowed = panel[dec == "allow"]
        blocked = panel[dec == "block"]
        mae_a = pd.to_numeric(allowed["mae"], errors="coerce")
        mfe_a = pd.to_numeric(allowed["mfe"], errors="coerce")
        mae_b = pd.to_numeric(blocked["mae"], errors="coerce")
        blocked_heavy = int((mae_b <= HEAVY_MAE_THRESHOLD).sum()) if len(blocked) else 0
        # positive outcome proxy: Exit-A winner if available, else MFE > |MAE|
        pos = blocked.copy()
        if "exit_a_winner" in pos.columns:
            blocked_pos = int(pos["exit_a_winner"].fillna(False).astype(bool).sum())
        else:
            blocked_pos = int(
                (
                    pd.to_numeric(pos["mfe"], errors="coerce")
                    > pd.to_numeric(pos["mae"], errors="coerce").abs()
                ).sum()
            )
        formula = GUARD_FORMULAS[g]
        rows.append(
            {
                "guard_name": formula["name"],
                "guard_code": g,
                "block_long_formula": formula["block_long"],
                "block_short_formula": formula["block_short"],
                "total_fills": len(panel),
                "allowed_fills": len(allowed),
                "blocked_fills": len(blocked),
                "blocked_rate": float(len(blocked) / len(panel)) if len(panel) else None,
                "blocked_heavy_mae_count": blocked_heavy,
                "heavy_mae_coverage": float(blocked_heavy / n_heavy) if n_heavy else None,
                "blocked_positive_outcome_count": blocked_pos,
                "false_block_rate": float(blocked_pos / len(blocked)) if len(blocked) else None,
                "allowed_median_mae": float(mae_a.median()) if len(allowed) and mae_a.notna().any() else None,
                "allowed_median_mfe": float(mfe_a.median()) if len(allowed) and mfe_a.notna().any() else None,
                "blocked_median_mae": float(mae_b.median()) if len(blocked) and mae_b.notna().any() else None,
            }
        )
    return pd.DataFrame(rows)


def warmup_ready_summary(frame4h: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    rows = [
        {
            "scope": "4h_bars_all",
            "n": len(frame4h),
            "ema_context_ready_rate": float(frame4h["ema_context_ready"].mean()) if len(frame4h) else None,
            "ema200_ready_rate": float(frame4h["ema200_ready"].mean()) if len(frame4h) else None,
            "regime_neutral_rate": float((frame4h["ema_regime_direction"] == 0).mean()) if len(frame4h) else None,
            "stack_mixed_rate": float((frame4h["ema_stack_state"] == "mixed").mean()) if len(frame4h) else None,
            "stack_full_rate": float(frame4h["ema_stack_state"].isin(["bullish_full", "bearish_full"]).mean())
            if len(frame4h)
            else None,
        },
        {
            "scope": "fill_context",
            "n": len(panel),
            "ema_context_ready_rate": float(panel["ema_context_ready"].mean()) if len(panel) else None,
            "ema200_ready_rate": float(panel["ema_context_ready"].mean()) if len(panel) else None,
            "regime_neutral_rate": float((panel["ema_regime_direction"] == 0).mean()) if len(panel) else None,
            "stack_mixed_rate": float((panel["ema_stack_state"] == "mixed").mean()) if len(panel) else None,
            "stack_full_rate": float(panel["ema_stack_state"].isin(["bullish_full", "bearish_full"]).mean())
            if len(panel)
            else None,
        },
    ]
    return pd.DataFrame(rows)


def write_readme(out_dir: Path, meta: dict[str, Any]) -> Path:
    lines = [
        "# Phase C3.4D — EMA9/20/59/200 Context Audit",
        "",
        "Additive research audit. C3.4B structure major remains unchanged.",
        "",
        "## Guard formulas",
        "",
    ]
    for g, f in GUARD_FORMULAS.items():
        lines.append(f"- **{f['name']}** (`{g}`)")
        lines.append(f"  - block_long: `{f['block_long']}`")
        lines.append(f"  - block_short: `{f['block_short']}`")
    lines += [
        "",
        "## Heavy MAE",
        "",
        f"- Canonical threshold: `mae <= {HEAVY_MAE_THRESHOLD}` (same as prior C3.4B 4h severe band).",
        "",
        "## Artefacts",
        "",
        "- `fill_level_context.csv`",
        "- `structure_ema_relation_summary.csv`",
        "- `countertrend_overlap.csv`",
        "- `cross_vs_structure_lag.csv`",
        "- `guard_comparison.csv`",
        "- `warmup_ready_summary.csv`",
        "- `audit_summary.json`",
        "",
        "## Meta",
        "",
        f"- symbol: `{meta.get('symbol')}`",
        f"- n_fills: `{meta.get('n_fills')}`",
        f"- c34b_unchanged: `{meta.get('c34b_unchanged')}`",
        f"- structure_hash: `{meta.get('structure_hash')}`",
        "",
    ]
    path = out_dir / "README.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_c34d_ema_context_audit(
    *,
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    excursion_dir: Path = EXCURSION_DIR,
) -> dict[str, Any]:
    baseline_info = assert_baseline_readonly(baseline_dir)
    if not baseline_info.get("hash_matches"):
        raise RuntimeError("C2 baseline hash mismatch")
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    c34b_path = Path("research/regime_scanner/market_structure_c3_4b.py")
    c34b_src_before = hashlib.sha256(c34b_path.read_bytes()).hexdigest()

    cfg = baseline_a6()
    frame15, frame_meta = build_extended_tf_frame(
        SYMBOL, timeframe=TIMEFRAME, warmup_calendar_days=WARMUP_CALENDAR_DAYS
    )
    if frame15.empty:
        raise RuntimeError(f"empty 15m: {frame_meta}")

    a0 = pd.Timestamp(frame_meta["analyze_start"])
    a1 = pd.Timestamp(frame_meta["analyze_end_exclusive"])
    warm_bars = max(required_indicator_warmup_bars(), 400)
    full_5m, _ = load_ohlcv_with_warmup(
        SYMBOL, "5m", analyze_start=a0, analyze_end=a1, warmup_bars=warm_bars
    )
    decision = a1 + pd.Timedelta(hours=1)

    frame4h, struct_hash_pre, struct_hash_post = build_combined_4h_frame(
        full_5m, decision=decision, analyze_start=a0, analyze_end_exclusive=a1
    )
    if frame4h.empty:
        raise RuntimeError("empty 4h combined frame")
    if struct_hash_pre != struct_hash_post:
        raise RuntimeError("structure hash drift")

    _tl, entries, _lives = apply_pullback_entry(frame15, cfg, return_lifecycles=True)
    filled = _filled_sorted(frame15, entries)
    trades = trades_exit_a_opposite_entry(frame15, filled, timeframe=TIMEFRAME, variant=cfg.name)
    closed = closed_only(trades)
    if len(filled) != 55:
        raise RuntimeError(f"expected 55 fills, got {len(filled)}")

    exc_path = excursion_dir / "fill_excursion_panel.csv"
    if not exc_path.exists():
        raise RuntimeError(f"missing excursion panel {exc_path}")
    exc = pd.read_csv(exc_path)
    if len(exc) != 55:
        raise RuntimeError(f"excursion n={len(exc)}")

    # Optional prior C3.4B panel for sanity (same fills)
    prior_path = C34B_4H_AUDIT_DIR / "fill_c34b_4h_context.csv"
    prior = pd.read_csv(prior_path) if prior_path.exists() else pd.DataFrame()

    splits = fixed_chrono_splits(a0, a1)
    highs = frame15["high"].astype(float).to_numpy()
    lows = frame15["low"].astype(float).to_numpy()
    closes = frame15["close"].astype(float).to_numpy()
    n_bars = len(frame15)

    panel_rows: list[dict[str, Any]] = []
    for i, fill in enumerate(filled):
        side_name = fill["side_name"]
        side = int(fill["side"])
        fill_i = int(fill["fill_bar"])
        trigger_ts = pd.Timestamp(fill["trigger_timestamp"])
        fill_ts = pd.Timestamp(fill["fill_timestamp"])
        entry = float(fill["entry_price"])
        trigger_decision = trigger_ts + pd.Timedelta(minutes=BAR_MINUTES)
        fill_id = f"F{i:03d}_{side_name}_{fill.get('setup_id')}"

        er = exc[exc["fill_id"] == fill_id]
        if er.empty:
            er = exc[(pd.to_datetime(exc["fill_time"], utc=True) == fill_ts) & (exc["side"] == side_name)]
        if er.empty:
            raise RuntimeError(f"missing excursion {fill_id}")
        er0 = er.iloc[0]

        hit = lookup_closed_htf_row(frame4h, trigger_decision=trigger_decision)
        if not hit.get("found"):
            raise RuntimeError(f"no closed 4h context for {fill_id}")
        row4 = hit["row"]

        struct_maj = int(row4["major_direction"])
        ema_reg = int(row4["ema_regime_direction"])

        opp_bar = None
        if pd.notna(er0.get("opposite_end_bar")):
            try:
                opp_bar = int(er0["opposite_end_bar"])
            except (TypeError, ValueError):
                opp_bar = None
        risk = recovery_and_risk_proxy(
            side=side,
            entry=entry,
            fill_i=fill_i,
            highs=highs,
            lows=lows,
            closes=closes,
            n_bars=n_bars,
            opp_bar=opp_bar,
        )

        net020 = er0.get("exit_a_net_0_20")
        winner = None
        if pd.notna(er0.get("winner_net020")):
            winner = bool(er0["winner_net020"])
        elif pd.notna(net020) and bool(er0.get("exit_a_closed")):
            winner = float(net020) > 0

        row = {
            "symbol": SYMBOL,
            "fill_id": fill_id,
            "fill_timestamp": fill_ts,
            "trigger_timestamp": trigger_ts,
            "direction": side_name,
            "entry_price": entry,
            "split": assign_split(fill_ts, splits),
            "selected_4h_bar_time": hit["selected_bar_time"],
            "selected_4h_bar_close_time": hit["selected_bar_close_time"],
            "context_is_causal": True,
            "structure_major_direction": struct_maj,
            "structure_state": row4.get("protected_structure_state"),
            "structure_state_age_bars": row4.get("structure_age_bars"),
            "protected_high": row4.get("protected_high"),
            "protected_low": row4.get("protected_low"),
            "ema9": _finite(row4.get("ema9")),
            "ema20": _finite(row4.get("ema20")),
            "ema59": _finite(row4.get("ema59")),
            "ema200": _finite(row4.get("ema200")),
            "ema_micro_direction": int(row4.get("ema_micro_direction", 0)),
            "ema_mid_direction": int(row4.get("ema_mid_direction", 0)),
            "ema_regime_direction": ema_reg,
            "ema_stack_state": row4.get("ema_stack_state"),
            "ema_slope_state": row4.get("ema_slope_state"),
            "ema_band_state": row4.get("ema_band_state"),
            "ema9_slope_atr": _finite(row4.get("ema9_slope_atr")),
            "ema20_slope_atr": _finite(row4.get("ema20_slope_atr")),
            "ema59_slope_atr": _finite(row4.get("ema59_slope_atr")),
            "ema200_slope_atr": _finite(row4.get("ema200_slope_atr")),
            "price_vs_ema59_atr": _finite(row4.get("price_vs_ema59_atr")),
            "price_vs_ema200_atr": _finite(row4.get("price_vs_ema200_atr")),
            "ema9_20_cross_event": int(row4.get("ema9_20_cross_event", 0)),
            "ema20_59_cross_event": int(row4.get("ema20_59_cross_event", 0)),
            "ema59_200_cross_event": int(row4.get("ema59_200_cross_event", 0)),
            "ema_regime_age_bars": int(row4.get("ema_regime_age_bars", 0)),
            "ema_micro_age_bars": int(row4.get("ema_micro_age_bars", 0)),
            "structure_ema_relation": row4.get("structure_ema_relation"),
            "structure_ema_relation_code": row4.get("structure_ema_relation_code"),
            "ema_context_ready": bool(row4.get("ema_context_ready")),
            "mae": float(er0["maximum_adverse_excursion_pct"]),
            "mfe": float(er0["maximum_favorable_excursion_pct"]),
            "underwater_duration": er0.get("max_underwater_duration_bars"),
            "forward_return_12h": forward_close_return_pct(
                side=side, entry=entry, fill_i=fill_i, closes=closes, hours=12.0
            ),
            "forward_return_24h": forward_close_return_pct(
                side=side, entry=entry, fill_i=fill_i, closes=closes, hours=24.0
            ),
            "forward_return_48h": forward_close_return_pct(
                side=side, entry=entry, fill_i=fill_i, closes=closes, hours=48.0
            ),
            "exit_a_closed": bool(er0.get("exit_a_closed")),
            "included_in_realized_exit_a": bool(er0.get("included_in_realized_exit_a")),
            "exit_a_winner": winner,
            "net_return_020_pct": _finite(net020) if pd.notna(net020) else float("nan"),
            "top3_trade": bool(er0["top3_trade"]) if "top3_trade" in er0 and pd.notna(er0.get("top3_trade")) else False,
            "mae_to_24h": risk.get("mae_to_24h"),
            "mfe_to_24h": risk.get("mfe_to_24h"),
            "mae_to_48h": risk.get("mae_to_48h"),
            "mfe_to_48h": risk.get("mfe_to_48h"),
        }
        for g in GUARD_ORDER:
            row[f"guard_{g}"] = guard_decision(side_name, struct_maj, ema_reg, g)
            row[f"guard_{g}_name"] = GUARD_FORMULAS[g]["name"]

        # Sanity: prior C3.4B major should match if available
        if not prior.empty:
            pr = prior[prior["fill_id"] == fill_id]
            if len(pr):
                row["prior_c34b_major_match"] = int(pr.iloc[0]["major_direction_4h"]) == struct_maj

        panel_rows.append(row)

    panel = pd.DataFrame(panel_rows)

    # Guard monotonicity checks (descriptive assert for audit integrity)
    for _, r in panel.iterrows():
        b1 = r["guard_G1"] == "block"
        b1b = r["guard_G1b"] == "block"
        b1c = r["guard_G1c"] == "block"
        if b1b and not b1:
            raise RuntimeError("G1b blocked without G1 — violates AND semantics")
        if b1 and not b1c:
            raise RuntimeError("G1 blocked without G1c — violates OR semantics")

    c34b_src_after = hashlib.sha256(c34b_path.read_bytes()).hexdigest()
    if c34b_src_before != c34b_src_after:
        raise RuntimeError("C3.4B source mutated during audit")

    rel_sum = summarize_relations(panel)
    ct = countertrend_overlap(panel)
    lag = cross_vs_structure_lag(frame4h)
    guards = summarize_guards(panel)
    warm = warmup_ready_summary(frame4h, panel)

    panel.to_csv(output_dir / "fill_level_context.csv", index=False)
    rel_sum.to_csv(output_dir / "structure_ema_relation_summary.csv", index=False)
    ct.to_csv(output_dir / "countertrend_overlap.csv", index=False)
    lag.to_csv(output_dir / "cross_vs_structure_lag.csv", index=False)
    guards.to_csv(output_dir / "guard_comparison.csv", index=False)
    warm.to_csv(output_dir / "warmup_ready_summary.csv", index=False)

    # Bar dump for inspectability
    keep = [
        c
        for c in [
            "timestamp",
            "htf_close_decision",
            "close",
            "major_direction",
            "structure_major_direction",
            "ema_regime_direction",
            "ema_micro_direction",
            "ema_mid_direction",
            "ema_stack_state",
            "ema_slope_state",
            "ema_band_state",
            "structure_ema_relation",
            "ema9",
            "ema20",
            "ema59",
            "ema200",
            "ema_context_ready",
            "external_bos_up",
            "external_bos_down",
            "ema20_59_cross_event",
            "ema59_200_cross_event",
        ]
        if c in frame4h.columns
    ]
    frame4h.loc[frame4h["in_analyze_window"] == True, keep].to_csv(  # noqa: E712
        output_dir / "c34d_4h_bar_context.csv", index=False
    )

    content_hash = hashlib.sha256(
        pd.util.hash_pandas_object(
            panel[
                [
                    "fill_id",
                    "structure_major_direction",
                    "ema_regime_direction",
                    "structure_ema_relation",
                    "mae",
                ]
            ].fillna(""),
            index=False,
        ).values.tobytes()
    ).hexdigest()

    meta = {
        "symbol": SYMBOL,
        "variant": VARIANT,
        "timeframe": TIMEFRAME,
        "htf": "4h",
        "config_hash": config_hash(cfg),
        "c34b_config": "protected_medium",
        "c34b_source_hash": c34b_src_before,
        "structure_hash": struct_hash_pre,
        "n_fills": len(filled),
        "n_closed_exit_a": int(len(closed)),
        "n_4h_bars": int(len(frame4h)),
        "n_4h_analyze": int((frame4h["in_analyze_window"] == True).sum()),  # noqa: E712
        "analyze_start": frame_meta.get("analyze_start"),
        "analyze_end_exclusive": frame_meta.get("analyze_end_exclusive"),
        "heavy_mae_threshold": HEAVY_MAE_THRESHOLD,
        "semantics": semantics_doc(),
        "relation_distribution": panel["structure_ema_relation"].value_counts().to_dict(),
        "structure_major_distribution": panel["structure_major_direction"].value_counts().to_dict(),
        "ema_regime_distribution": panel["ema_regime_direction"].value_counts().to_dict(),
        "stack_distribution": panel["ema_stack_state"].value_counts().to_dict(),
        "guards": guards.to_dict(orient="records"),
        "countertrend_overlap": ct.to_dict(orient="records"),
        "warmup": warm.to_dict(orient="records"),
        "prior_c34b_major_match_rate": float(panel["prior_c34b_major_match"].mean())
        if "prior_c34b_major_match" in panel.columns
        else None,
        "c34b_unchanged": True,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "no_entry_filter_activation": True,
        "no_hedge_bot_implementation": True,
        "no_parameter_optimization": True,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "content_hash": content_hash,
        "immutable_cols_checked": list(STRUCTURE_IMMUTABLE_COLS),
    }
    (output_dir / "audit_summary.json").write_text(
        json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
    )
    write_readme(output_dir, meta)
    return meta


def main() -> None:
    p = argparse.ArgumentParser(description="C3.4D EMA context audit")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    meta = run_c34d_ema_context_audit(output_dir=args.output_dir)
    print(json.dumps(json_safe({"ok": True, "n_fills": meta["n_fills"], "content_hash": meta["content_hash"]}), indent=2))


if __name__ == "__main__":
    main()
