"""Causal frozen compression boxes (B16/B32/B64 × Q1/Q2)."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.oi_compression_breakout.config import (
    BOX_DRIFT_MAX,
    BOX_LENGTHS,
    INNER_CLOSE_MIN_RATIO,
    INNER_ZONE_MARGIN,
    MAX_WAIT_BARS,
    QUALITY_RULES,
    box_variant_id,
)
from research.regime_scanner.oi_compression_breakout.features import contiguous_same_sequence


@dataclass
class FrozenBox:
    symbol: str
    sequence_id: Any
    box_length: int
    quality: str
    confirm_i: int
    start_i: int  # inclusive index of first box bar (= confirm_i - N)
    end_i: int  # inclusive last box bar (= confirm_i - 1)
    box_high: float
    box_low: float
    box_width: float
    box_width_atr: float
    box_drift_ratio: float
    inner_close_ratio: float
    atr_14: float
    atr_14_pctl_288: float
    oi_start: float
    oi_end: float
    confirm_bucket: str
    start_bucket: str
    end_bucket: str
    physical_id: str
    box_id: str


@dataclass
class BoxFilterDiagnostics:
    """Per-length funnel counters (raw candidates → confirmed)."""

    by_length: dict[int, dict[str, int]] = field(default_factory=dict)

    def ensure(self, length: int) -> dict[str, int]:
        if length not in self.by_length:
            self.by_length[length] = Counter(
                {
                    "raw_candidates": 0,
                    "reject_gap": 0,
                    "reject_atr": 0,
                    "reject_width": 0,
                    "pass_width": 0,
                    "reject_drift": 0,
                    "pass_drift": 0,
                    "reject_inner": 0,
                    "pass_inner": 0,
                    "reject_oi": 0,
                    "pass_all_q1": 0,
                    "pass_all_q2": 0,
                    "blocked_active": 0,
                    "confirmed_rows": 0,
                }
            )
        return self.by_length[length]

    def to_rows(self, symbol: str) -> list[dict[str, Any]]:
        rows = []
        for L, c in sorted(self.by_length.items()):
            rows.append({"symbol": symbol, "box_length": L, **dict(c)})
        return rows


def _inner_close_ratio(closes: np.ndarray, lo: float, hi: float) -> float:
    width = hi - lo
    if width <= 0 or len(closes) == 0:
        return 0.0
    inner_lo = lo + INNER_ZONE_MARGIN * width
    inner_hi = hi - INNER_ZONE_MARGIN * width
    inside = (closes >= inner_lo) & (closes <= inner_hi)
    return float(np.mean(inside))


def evaluate_box_at(
    df: pd.DataFrame,
    *,
    confirm_i: int,
    box_length: int,
    quality: str,
    diag: dict[str, int] | None = None,
    count_raw: bool = True,
) -> FrozenBox | None:
    """Evaluate box ending just before confirm_i; confirm candle excluded from bounds."""
    n = box_length
    if confirm_i < n:
        return None
    if diag is not None and count_raw:
        diag["raw_candidates"] += 1

    start_i = confirm_i - n
    end_i = confirm_i - 1
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    if not contiguous_same_sequence(seq, ts, start_i, confirm_i):
        if diag is not None:
            diag["reject_gap"] += 1
        return None

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    atr = float(df["atr_14"].iloc[confirm_i])
    if not np.isfinite(atr) or atr <= 0:
        if diag is not None:
            diag["reject_atr"] += 1
        return None

    box_high = float(np.max(highs[start_i:confirm_i]))
    box_low = float(np.min(lows[start_i:confirm_i]))
    width = box_high - box_low
    if width <= 0:
        if diag is not None:
            diag["reject_width"] += 1
        return None
    width_atr = width / atr
    max_w = QUALITY_RULES[quality]
    if width_atr > max_w:
        if diag is not None and quality == "Q1":
            diag["reject_width"] += 1
        return None
    if diag is not None and quality == "Q1":
        diag["pass_width"] += 1

    close_start = float(closes[start_i])
    close_end = float(closes[end_i])
    drift = abs(close_end - close_start) / width
    if drift > BOX_DRIFT_MAX:
        if diag is not None and quality == "Q1":
            diag["reject_drift"] += 1
        return None
    if diag is not None and quality == "Q1":
        diag["pass_drift"] += 1

    inner = _inner_close_ratio(closes[start_i:confirm_i], box_low, box_high)
    if inner < INNER_CLOSE_MIN_RATIO:
        if diag is not None and quality == "Q1":
            diag["reject_inner"] += 1
        return None
    if diag is not None and quality == "Q1":
        diag["pass_inner"] += 1

    oi = df["open_interest"].to_numpy(dtype=float)
    oi_start = float(oi[start_i])
    oi_end = float(oi[confirm_i])
    if not (np.isfinite(oi_start) and oi_start > 0 and np.isfinite(oi_end) and oi_end > 0):
        if diag is not None and quality == "Q1":
            diag["reject_oi"] += 1
        return None

    if diag is not None:
        if quality == "Q1":
            diag["pass_all_q1"] += 1
        elif quality == "Q2":
            diag["pass_all_q2"] += 1

    symbol = str(df["symbol"].iloc[confirm_i])
    seq_id = seq[confirm_i]
    confirm_bucket = str(df["bucket_start"].iloc[confirm_i])
    start_bucket = str(df["bucket_start"].iloc[start_i])
    end_bucket = str(df["bucket_start"].iloc[end_i])
    physical_id = f"{symbol}|{seq_id}|{start_bucket}|{confirm_bucket}"
    box_id = f"{physical_id}|{box_variant_id(box_length, quality)}"

    atr_p = df["atr_14_pctl_288"].iloc[confirm_i]
    return FrozenBox(
        symbol=symbol,
        sequence_id=seq_id,
        box_length=box_length,
        quality=quality,
        confirm_i=confirm_i,
        start_i=start_i,
        end_i=end_i,
        box_high=box_high,
        box_low=box_low,
        box_width=width,
        box_width_atr=width_atr,
        box_drift_ratio=drift,
        inner_close_ratio=inner,
        atr_14=atr,
        atr_14_pctl_288=float(atr_p) if pd.notna(atr_p) else float("nan"),
        oi_start=oi_start,
        oi_end=oi_end,
        confirm_bucket=confirm_bucket,
        start_bucket=start_bucket,
        end_bucket=end_bucket,
        physical_id=physical_id,
        box_id=box_id,
    )


def detect_frozen_boxes_with_early_release(
    df: pd.DataFrame,
    *,
    max_wait_bars: int = MAX_WAIT_BARS,
    scan_breakout_fn=None,
) -> tuple[list[FrozenBox], list[dict[str, Any]], BoxFilterDiagnostics]:
    """Confirm boxes causally, then resolve breakout/timeout independently.

    Critical ordering (no breakout-selection of the box population):
      1. evaluate Q1/Q2 on past bars only
      2. ``boxes.append`` immediately on confirm
      3. ``scan_breakout`` for up to ``max_wait_bars`` (must cover W48)
      4. always ``breakouts.append`` (including ``no_breakout=true``)
      5. release active slot on breakout bar / timeout / gap / dataset end

    Active key is ``box_length`` only — lengths never block each other.
    """
    from research.regime_scanner.oi_compression_breakout.breakouts import scan_breakout as _scan
    from research.regime_scanner.oi_compression_breakout.config import WAIT_WINDOWS

    if max_wait_bars < max(WAIT_WINDOWS):
        raise ValueError(
            f"max_wait_bars={max_wait_bars} < max(WAIT_WINDOWS)={max(WAIT_WINDOWS)}"
        )

    scan = scan_breakout_fn or _scan
    boxes: list[FrozenBox] = []
    breakouts: list[dict[str, Any]] = []
    diag = BoxFilterDiagnostics()
    # release_after[L] = last blocked index (inclusive)
    release_after: dict[int, int] = {L: -1 for L in BOX_LENGTHS}
    n = len(df)

    for i in range(n):
        for L in BOX_LENGTHS:
            d = diag.ensure(L)
            if i <= release_after[L]:
                d["blocked_active"] += 1
                continue

            q1 = evaluate_box_at(df, confirm_i=i, box_length=L, quality="Q1", diag=d, count_raw=True)
            q2 = evaluate_box_at(df, confirm_i=i, box_length=L, quality="Q2", diag=d, count_raw=False)
            found = [b for b in (q1, q2) if b is not None]
            if not found:
                continue

            # --- confirm population (independent of future breakout) ---
            primary = found[-1] if len(found) == 2 else found[0]
            boxes.extend(found)
            d["confirmed_rows"] += len(found)

            # --- resolve breakout / timeout / gap (always keep a row) ---
            br = scan(df, primary, max_wait=max_wait_bars)
            for b in found:
                row = dict(br)
                row["box_id"] = b.box_id
                row["physical_id"] = b.physical_id
                row["quality"] = b.quality
                row["box_length"] = b.box_length
                row["box_high"] = b.box_high
                row["box_low"] = b.box_low
                row["confirmed_before_breakout_scan"] = True
                breakouts.append(row)

            # release active slot
            if br.get("breakout_i") is not None:
                release_after[L] = int(br["breakout_i"])
            elif br.get("outcome_status") in ("gap_abort", "sequence_end"):
                # free immediately after abort bar window start
                release_after[L] = i
            else:
                # timeout or dataset_end: hold through the observed search end
                release_after[L] = min(n - 1, i + int(br.get("observed_search_bars") or max_wait_bars))

    return boxes, breakouts, diag


def detect_frozen_boxes(
    df: pd.DataFrame,
    *,
    max_wait_bars: int = MAX_WAIT_BARS,
) -> list[FrozenBox]:
    """Back-compat: return confirmed boxes only (early-release state machine)."""
    boxes, _br, _diag = detect_frozen_boxes_with_early_release(df, max_wait_bars=max_wait_bars)
    return boxes


def boxes_to_rows(boxes: list[FrozenBox]) -> list[dict[str, Any]]:
    rows = []
    for b in boxes:
        rows.append(
            {
                "box_id": b.box_id,
                "physical_id": b.physical_id,
                "symbol": b.symbol,
                "sequence_id": b.sequence_id,
                "box_length": b.box_length,
                "quality": b.quality,
                "box_variant": box_variant_id(b.box_length, b.quality),
                "confirm_i": b.confirm_i,
                "start_i": b.start_i,
                "end_i": b.end_i,
                "box_confirm_timestamp": b.confirm_bucket,
                "box_start_timestamp": b.start_bucket,
                "box_end_timestamp": b.end_bucket,
                "box_high": b.box_high,
                "box_low": b.box_low,
                "box_width": b.box_width,
                "box_width_atr": b.box_width_atr,
                "box_drift_ratio": b.box_drift_ratio,
                "inner_close_ratio": b.inner_close_ratio,
                "atr_14": b.atr_14,
                "atr_14_pctl_288": b.atr_14_pctl_288,
                "oi_start": b.oi_start,
                "oi_end": b.oi_end,
            }
        )
    return rows


def physical_phases_from_boxes(boxes: list[FrozenBox]) -> list[dict[str, Any]]:
    """Collapse quality variants sharing the same physical_id + length."""
    seen: dict[str, dict[str, Any]] = {}
    for b in boxes:
        key = f"{b.physical_id}|B{b.box_length}"
        if key in seen:
            seen[key]["qualities"].add(b.quality)
            continue
        seen[key] = {
            "physical_phase_id": key,
            "physical_id": b.physical_id,
            "symbol": b.symbol,
            "sequence_id": b.sequence_id,
            "box_length": b.box_length,
            "box_start_timestamp": b.start_bucket,
            "box_confirm_timestamp": b.confirm_bucket,
            "box_high": b.box_high,
            "box_low": b.box_low,
            "qualities": {b.quality},
        }
    rows = []
    for v in seen.values():
        quals = sorted(v.pop("qualities"))
        v["qualities"] = "|".join(quals)
        v["n_quality_variants"] = len(quals)
        rows.append(v)
    return rows
