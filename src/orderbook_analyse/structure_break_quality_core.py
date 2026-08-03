"""Generic structure-break quality analysis for fixed protected-low break events.

Reuses metric helpers and ``classify_from_metrics`` thresholds from
``apt_001_protected_low_break_deep_dive`` without changing them. APT_001 remains
the source of truth for those helpers; this module parameterizes windows and
adds multi-event causal / ablation / case-decision layers.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from orderbook_analyse.apt_001_protected_low_break_deep_dive import (
    CONFIRM_HORIZONS_S,
    FOLLOW_HORIZONS_S,
    RECLAIM_HORIZONS_S,
    aggregate_trades,
    audit_recorder_quality,
    book_metrics_at,
    buy_efficiency_bps_per_1k,
    classify_from_metrics,
    continuous_below_duration,
    ensure_utc,
    first_trade_at_or_above,
    first_trade_below,
    iso_z,
    mfe_bearish_bps,
    sample_grid,
    sell_efficiency_bps_per_1k,
)
from orderbook_analyse.dynamic_wall_detector import (
    connect_readonly,
    find_bootstrap_snapshot,
    load_events,
    load_oi_context,
    reconstruct_with_samples,
)
from orderbook_analyse.orderbook_absorption_features import load_trade_ticks
from orderbook_analyse.orderbook_replay import ReplayError

logger = logging.getLogger(__name__)

CAUSAL_HORIZONS_S = (0, 30, 60, 180, 300, 900, 1800, 3600)

OUTCOME_MAP = {
    "WEAK_BREAK_WITH_FAST_RECLAIM": "FAST_RECLAIM",
    "LIQUIDITY_SWEEP_AND_RECLAIM": "LIQUIDITY_SWEEP_RECLAIM",
    "SELL_ABSORPTION_LIMITED_FOLLOW_THROUGH": "SELL_ABSORPTION_BELOW_LEVEL",
    "EVENT_AMBIGUOUS": "AMBIGUOUS",
    "BREAK_THEN_RANGE_NO_CLEAR_ACCEPTANCE": "BREAK_THEN_RANGE_NO_CLEAR_ACCEPTANCE",
    "STRONG_BREAK_ACCEPTANCE": "STRONG_BREAK_ACCEPTANCE",
}

# Fixed a-priori event specs (must match timeline artefacts exactly).
FIXED_EVENT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "cluster_id": "BLK_APTUSDT_001",
        "symbol": "APTUSDT",
        "level_price": 0.6298,
        "break_candle_open": datetime(2026, 7, 26, 11, 45, tzinfo=timezone.utc),
        "break_known_at": datetime(2026, 7, 26, 11, 50, tzinfo=timezone.utc),
    },
    {
        "cluster_id": "BLK_APTUSDT_002",
        "symbol": "APTUSDT",
        "level_price": 0.5689,
        "break_candle_open": datetime(2026, 7, 31, 2, 25, tzinfo=timezone.utc),
        "break_known_at": datetime(2026, 7, 31, 2, 30, tzinfo=timezone.utc),
    },
    {
        "cluster_id": "BLK_APTUSDT_003",
        "symbol": "APTUSDT",
        "level_price": 0.5613,
        "break_candle_open": datetime(2026, 8, 2, 3, 50, tzinfo=timezone.utc),
        "break_known_at": datetime(2026, 8, 2, 3, 55, tzinfo=timezone.utc),
    },
    {
        "cluster_id": "BLK_DOGEUSDT_001",
        "symbol": "DOGEUSDT",
        "level_price": 0.07302,
        "break_candle_open": datetime(2026, 7, 27, 0, 55, tzinfo=timezone.utc),
        "break_known_at": datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc),
    },
    {
        "cluster_id": "BLK_DOGEUSDT_002",
        "symbol": "DOGEUSDT",
        "level_price": 0.06984,
        "break_candle_open": datetime(2026, 7, 30, 4, 55, tzinfo=timezone.utc),
        "break_known_at": datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc),
    },
)

CLEAR_CAUSAL = {
    "CONTINUATION_BIAS",
    "RECLAIM_RISK",
    "RANGE_RISK",
    "ACCEPTANCE_BIAS",
}
CLEAR_OUTCOME = {
    "STRONG_BREAK_ACCEPTANCE",
    "FAST_RECLAIM",
    "LIQUIDITY_SWEEP_RECLAIM",
    "SELL_ABSORPTION_BELOW_LEVEL",
    "BREAK_THEN_RANGE_NO_CLEAR_ACCEPTANCE",
    "WEAK_BREAK_NO_FOLLOW_THROUGH",
    "DELAYED_RECLAIM",
}


def _parse_ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        return ensure_utc(v)
    return ensure_utc(pd.Timestamp(v).to_pydatetime())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


@dataclass(frozen=True)
class BreakEvent:
    """Fixed protected-low structure-break event with APT_001-relative windows."""

    cluster_id: str
    symbol: str
    level_price: float
    break_candle_open: datetime
    break_known_at: datetime
    event_id: str
    structure_side: str = "bearish"
    level_type: str = "protected_low"
    timeframe: str = "5m"
    scanner_state: str | None = None
    external_bos: str | None = None
    choch: str | None = None
    canonical_source: str = "transition_candidates.csv T_FIRST_STRUCTURE_BREAK"
    ohlc: dict[str, float] = field(default_factory=dict)

    @property
    def break_available_at(self) -> datetime:
        return self.break_known_at

    @property
    def main_start(self) -> datetime:
        return self.break_candle_open - timedelta(minutes=10)

    @property
    def main_end(self) -> datetime:
        return self.break_known_at + timedelta(minutes=20)

    @property
    def tight_start(self) -> datetime:
        return self.break_candle_open - timedelta(minutes=2)

    @property
    def tight_end(self) -> datetime:
        return self.break_known_at + timedelta(minutes=7)

    @property
    def ctx_start(self) -> datetime:
        return self.break_known_at - timedelta(minutes=60)

    @property
    def ctx_end(self) -> datetime:
        return self.break_known_at + timedelta(minutes=100)

    @property
    def late_end(self) -> datetime:
        return self.break_known_at + timedelta(hours=6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "cluster_id": self.cluster_id,
            "symbol": self.symbol,
            "structure_side": self.structure_side,
            "level_type": self.level_type,
            "level_price": self.level_price,
            "break_candle_open": iso_z(self.break_candle_open),
            "break_available_at": iso_z(self.break_known_at),
            "break_known_at": iso_z(self.break_known_at),
            "scanner_state": self.scanner_state,
            "external_bos": self.external_bos,
            "choch": self.choch,
            "canonical_source": self.canonical_source,
            "timeframe": self.timeframe,
            "ohlc": dict(self.ohlc),
            "windows": {
                "main": [iso_z(self.main_start), iso_z(self.main_end)],
                "tight": [iso_z(self.tight_start), iso_z(self.tight_end)],
                "ctx": [iso_z(self.ctx_start), iso_z(self.ctx_end)],
                "late_end": iso_z(self.late_end),
            },
        }


def map_outcome_class(raw: str) -> str:
    return OUTCOME_MAP.get(raw, raw)


def classify_price_only(m: dict[str, Any]) -> str:
    """Price-path-only classifier (same spirit as APT_001, no flow/book)."""
    reclaim_s = m.get("seconds_to_reclaim_after_known")
    max_below = float(m.get("max_distance_below_bps") or 0)
    dur = float(m.get("seconds_continuously_below_after_first_break_in_candle") or 0)
    mfe15 = float(m.get("mfe_bearish_bps_900s_after_known") or 0)

    if reclaim_s is not None and float(reclaim_s) <= 300:
        if max_below < 25:
            return "FAST_RECLAIM"
        return "WEAK_BREAK_NO_FOLLOW_THROUGH"
    if (
        dur >= 180
        and (reclaim_s is None or float(reclaim_s) > 1800)
        and mfe15 >= 20
        and max_below >= 20
    ):
        return "STRONG_BREAK_ACCEPTANCE"
    if dur >= 60 and reclaim_s is not None and 300 < float(reclaim_s) <= 1800:
        return "BREAK_THEN_RANGE_NO_CLEAR_ACCEPTANCE"
    return "AMBIGUOUS"


def load_fixed_events(timeline_dir: Path) -> list[BreakEvent]:
    """Load and verify the five fixed PL-break events from timeline artefacts."""
    timeline_dir = Path(timeline_dir)
    tc = pd.read_csv(timeline_dir / "transition_candidates.csv")
    inv = pd.read_csv(timeline_dir / "five_regime_inventory.csv")
    tl = pd.read_csv(
        timeline_dir / "regime_timeline.csv",
        usecols=[
            "timestamp",
            "cluster_id",
            "market_open",
            "market_high",
            "market_low",
            "market_close",
            "protected_low",
            "external_bos",
            "choch",
            "trend_state",
            "structure_break_event",
        ],
    )
    tl["timestamp"] = pd.to_datetime(tl["timestamp"], utc=True)

    events: list[BreakEvent] = []
    for spec in FIXED_EVENT_SPECS:
        cid = spec["cluster_id"]
        symbol = spec["symbol"]
        level = float(spec["level_price"])
        open_ts = ensure_utc(spec["break_candle_open"])
        known_ts = ensure_utc(spec["break_known_at"])

        row = tc[(tc.cluster_id == cid) & (tc.candidate == "T_FIRST_STRUCTURE_BREAK")]
        if row.empty:
            raise ValueError(f"missing T_FIRST_STRUCTURE_BREAK for {cid}")
        known_art = _parse_ts(row.iloc[0]["timestamp"])
        if known_art != known_ts:
            raise ValueError(f"{cid}: artefact known {known_art} != expected {known_ts}")

        inv_row = inv[inv.cluster_id == cid]
        if inv_row.empty:
            raise ValueError(f"missing inventory row for {cid}")
        inv_break = _parse_ts(inv_row.iloc[0]["structure_break"])
        if inv_break != known_ts:
            raise ValueError(f"{cid}: inventory structure_break mismatch")

        mark = tl[(tl.cluster_id == cid) & (tl.timestamp == pd.Timestamp(known_ts))]
        open_row = tl[(tl.cluster_id == cid) & (tl.timestamp == pd.Timestamp(open_ts))]
        if mark.empty or open_row.empty:
            raise ValueError(f"{cid}: missing open/known rows in regime_timeline")
        pl = float(mark.iloc[0]["protected_low"])
        if abs(pl - level) > 1e-12:
            raise ValueError(f"{cid}: level mismatch artefact={pl} expected={level}")

        ohlc = {
            "open": float(open_row.iloc[0]["market_open"]),
            "high": float(open_row.iloc[0]["market_high"]),
            "low": float(open_row.iloc[0]["market_low"]),
            "close": float(open_row.iloc[0]["market_close"]),
        }
        event_id = f"{symbol}_{cid}_PL_BREAK"
        events.append(
            BreakEvent(
                cluster_id=cid,
                symbol=symbol,
                level_price=level,
                break_candle_open=open_ts,
                break_known_at=known_ts,
                event_id=event_id,
                scanner_state=str(mark.iloc[0]["trend_state"]),
                external_bos=None
                if pd.isna(mark.iloc[0]["external_bos"])
                else str(mark.iloc[0]["external_bos"]),
                choch=None if pd.isna(mark.iloc[0]["choch"]) else str(mark.iloc[0]["choch"]),
                ohlc=ohlc,
            )
        )

    if len(events) != 5:
        raise ValueError(f"expected 5 events, got {len(events)}")
    return events


def _assign_phase(
    ts: datetime,
    *,
    event: BreakEvent,
    first_5bps: datetime | None,
    first_below: datetime | None,
) -> str:
    ts = ensure_utc(ts)
    if ts < event.main_start:
        return "CTX_PRE"
    if first_5bps is None or ts < first_5bps:
        return "A_PRE_ATTACK"
    if first_below is None or ts < first_below:
        return "B_ATTACK"
    if ts < event.break_known_at:
        return "C_BREAK"
    if ts < event.break_known_at + timedelta(minutes=5):
        return "D_IMMEDIATE_POST"
    if ts < event.main_end:
        return "E_EARLY_RECLAIM_TEST"
    if ts < event.late_end:
        return "F_LATE_CONTEXT"
    return "AFTER"


def _build_acceptance(
    *,
    event: BreakEvent,
    ticks: Sequence[Any],
    first_below: datetime | None,
    first_below_any: datetime | None,
    reclaim_after_known: datetime | None,
    break_tf: dict[str, Any],
    until: datetime | None = None,
) -> dict[str, Any]:
    """Acceptance / reclaim metrics identical in spirit to APT_001."""
    level = event.level_price
    known = event.break_known_at
    hard_until = until if until is not None else event.late_end

    max_below_bps = None
    if break_tf.get("min_price") is not None:
        max_below_bps = (level - float(break_tf["min_price"])) / level * 10_000.0

    dur_below = 0.0
    if first_below is not None:
        dur_below = continuous_below_duration(
            ticks, level=level, start=first_below, until=hard_until
        )

    seconds_to_reclaim = None
    if reclaim_after_known is not None and reclaim_after_known < hard_until:
        seconds_to_reclaim = (reclaim_after_known - known).total_seconds()
    elif reclaim_after_known is not None and until is not None and reclaim_after_known >= hard_until:
        seconds_to_reclaim = None
        reclaim_after_known = None

    acceptance: dict[str, Any] = {
        "level_price": level,
        "first_trade_below_in_break_candle": iso_z(first_below),
        "first_trade_below_in_main_window": iso_z(first_below_any),
        "first_reclaim_ts": iso_z(reclaim_after_known)
        if reclaim_after_known is not None and (until is None or reclaim_after_known < hard_until)
        else None,
        "seconds_to_reclaim_after_known": seconds_to_reclaim,
        "seconds_continuously_below_after_first_break_in_candle": dur_below,
        "max_distance_below_bps": max_below_bps,
        "sell_efficiency_break_candle_bps_per_1k": break_tf.get("sell_efficiency_bps_per_1k"),
    }
    for h in RECLAIM_HORIZONS_S:
        acceptance[f"fast_reclaim_within_{h}s"] = int(
            reclaim_after_known is not None
            and (reclaim_after_known - known).total_seconds() <= h
            and (until is None or reclaim_after_known < hard_until)
        )
    for h in (30, 60, 180):
        acceptance[f"acceptance_below_{h}s"] = int(dur_below >= h)
    for h in FOLLOW_HORIZONS_S:
        h_use = h if until is None else min(h, int((hard_until - known).total_seconds()))
        if until is not None and h > int((hard_until - known).total_seconds()):
            # Only report MFE for horizons fully inside truncated window.
            acceptance[f"mfe_bearish_bps_{h}s_after_known"] = mfe_bearish_bps(
                ticks, level=level, start=known, horizon_s=h_use
            ) if h_use > 0 else None
        else:
            acceptance[f"mfe_bearish_bps_{h}s_after_known"] = mfe_bearish_bps(
                ticks, level=level, start=known, horizon_s=h
            )
    return acceptance


def causal_classify_at_horizon(
    *,
    ticks: Sequence[Any],
    event: BreakEvent,
    horizon_s: int,
    book_at_h: dict[str, Any] | None,
) -> dict[str, Any]:
    """Causal classification using only data available at known+horizon."""
    known = event.break_known_at
    level = event.level_price
    t_end = known + timedelta(seconds=horizon_s)
    reclaim_after_known = first_trade_at_or_above(ticks, level=level, after=known, until=t_end)
    mfe = mfe_bearish_bps(ticks, level=level, start=known, horizon_s=max(horizon_s, 1))
    if horizon_s == 0:
        mfe = None
    tr = aggregate_trades(ticks, start=known, end=t_end if horizon_s > 0 else known + timedelta(microseconds=1))
    sell_eff = None
    if tr["sell_volume"] and mfe is not None:
        sell_eff = sell_efficiency_bps_per_1k(mfe, float(tr["sell_volume"]))

    still_below = reclaim_after_known is None
    evidence: list[str] = []
    if mfe is not None:
        evidence.append(f"mfe={mfe:.2f}")
    evidence.append(f"sell={tr['sell_volume']}")
    evidence.append(f"buy={tr['buy_volume']}")
    if book_at_h is not None:
        evidence.append(f"imbalance_5bps={book_at_h.get('imbalance_5bps')}")

    if horizon_s == 0:
        cls, conf = "BREAK_PRINTED_BELOW_LEVEL", "medium"
        still_unknown = True
    elif reclaim_after_known is not None:
        cls, conf = "RECLAIM_RISK", "medium"
        still_unknown = False
    elif still_below and (mfe or 0) >= 20 and (tr["sell_volume"] or 0) > (tr["buy_volume"] or 0):
        if horizon_s >= 180 and (mfe or 0) >= 25:
            cls, conf = "ACCEPTANCE_BIAS", "medium"
        else:
            cls, conf = "CONTINUATION_BIAS", "medium"
        still_unknown = False
    elif still_below and (mfe or 0) < 10 and horizon_s >= 60:
        cls, conf = "RANGE_RISK", "medium"
        still_unknown = False
    else:
        cls, conf = "INCONCLUSIVE", "low"
        still_unknown = True

    return {
        "event_id": event.event_id,
        "horizon_s": horizon_s,
        "evaluation_horizon": horizon_s,
        "evaluation_ts": iso_z(t_end if horizon_s > 0 else known),
        "as_of": iso_z(t_end if horizon_s > 0 else known),
        "still_below_level": int(still_below),
        "reclaim_seen": int(reclaim_after_known is not None),
        "reclaim_ts": iso_z(reclaim_after_known),
        "mfe_bearish_bps": mfe,
        "sell_volume_since_known": tr["sell_volume"],
        "buy_volume_since_known": tr["buy_volume"],
        "net_delta_since_known": tr["net_delta"],
        "sell_efficiency_bps_per_1k": sell_eff,
        "bid_depth_5bps": None if book_at_h is None else book_at_h.get("bid_depth_5bps"),
        "ask_depth_5bps": None if book_at_h is None else book_at_h.get("ask_depth_5bps"),
        "imbalance_5bps": None if book_at_h is None else book_at_h.get("imbalance_5bps"),
        "largest_bid_wall_notional": None
        if book_at_h is None
        else book_at_h.get("largest_bid_wall_notional"),
        "classification": cls,
        "confidence": conf,
        "decisive_evidence": "; ".join(evidence),
        "still_unknown": int(still_unknown),
        "available_features": "price,tradeflow,book" if book_at_h is not None else "price,tradeflow",
    }


def _ablation_at_horizon(
    *,
    event: BreakEvent,
    ticks: Sequence[Any],
    first_below: datetime | None,
    first_below_any: datetime | None,
    break_tf: dict[str, Any],
    horizon_s: int,
    book_at_h: dict[str, Any] | None,
    causal_row: dict[str, Any],
) -> dict[str, Any]:
    known = event.break_known_at
    t_end = known + timedelta(seconds=horizon_s)
    reclaim = first_trade_at_or_above(ticks, level=event.level_price, after=known, until=t_end)
    trunc = _build_acceptance(
        event=event,
        ticks=ticks,
        first_below=first_below,
        first_below_any=first_below_any,
        reclaim_after_known=reclaim,
        break_tf=break_tf,
        until=t_end,
    )
    # At h=0 outcome classifiers are not yet informative.
    if horizon_s == 0:
        price_only = "BREAK_PRINTED_BELOW_LEVEL"
        price_tf = "AMBIGUOUS"
        price_book = "BREAK_PRINTED_BELOW_LEVEL"
        full = "BREAK_PRINTED_BELOW_LEVEL"
    else:
        price_only = classify_price_only(trunc)
        price_tf_raw, _, _ = classify_from_metrics(trunc)
        price_tf = map_outcome_class(price_tf_raw)
        # price+book: start from price_only, adjust with imbalance / wall pull proxies
        price_book = price_only
        imb = None if book_at_h is None else book_at_h.get("imbalance_5bps")
        if book_at_h is not None and causal_row["classification"] == "RECLAIM_RISK":
            price_book = "FAST_RECLAIM" if price_only in {"AMBIGUOUS", "FAST_RECLAIM"} else price_only
        elif book_at_h is not None and imb is not None and imb < -0.2 and price_only == "AMBIGUOUS":
            price_book = "STRONG_BREAK_ACCEPTANCE" if (trunc.get("mfe_bearish_bps_900s_after_known") or 0) >= 20 else price_only
        elif causal_row["classification"] in {"RANGE_RISK", "RECLAIM_RISK"} and price_only == "AMBIGUOUS":
            price_book = "BREAK_THEN_RANGE_NO_CLEAR_ACCEPTANCE"
        full_raw, _, _ = classify_from_metrics(trunc)
        full = map_outcome_class(full_raw)
        # Full prefers causal reclaim/acceptance bias when outcome still ambiguous.
        if full == "AMBIGUOUS":
            if causal_row["classification"] == "RECLAIM_RISK":
                full = "FAST_RECLAIM" if (trunc.get("seconds_to_reclaim_after_known") or 9999) <= 300 else "DELAYED_RECLAIM"
            elif causal_row["classification"] == "ACCEPTANCE_BIAS":
                full = "STRONG_BREAK_ACCEPTANCE"
            elif causal_row["classification"] == "RANGE_RISK":
                full = "BREAK_THEN_RANGE_NO_CLEAR_ACCEPTANCE"

    return {
        "event_id": event.event_id,
        "horizon_s": horizon_s,
        "price_only": price_only,
        "price_tradeflow": price_tf,
        "price_book": price_book,
        "full": full,
        "causal_class": causal_row["classification"],
    }


def _earliest_clear(rows: list[dict[str, Any]], key: str, clear_set: set[str]) -> int | None:
    for r in rows:
        if r["horizon_s"] == 0:
            continue
        val = r.get(key)
        if val in clear_set:
            return int(r["horizon_s"])
    return None


def _case_decision(
    *,
    quality_pass: bool,
    outcome: str,
    ablation_rows: list[dict[str, Any]],
    earliest_price_only: int | None,
    earliest_full: int | None,
    ob_lead: float | None,
) -> str:
    if not quality_pass or outcome == "EVENT_DATA_INVALID":
        return "EVENT_DATA_INVALID"
    if outcome == "AMBIGUOUS" and earliest_full is None:
        return "BREAK_REMAINS_AMBIGUOUS"

    reclaim_like = outcome in {
        "FAST_RECLAIM",
        "LIQUIDITY_SWEEP_RECLAIM",
        "BREAK_THEN_RANGE_NO_CLEAR_ACCEPTANCE",
        "DELAYED_RECLAIM",
        "WEAK_BREAK_NO_FOLLOW_THROUGH",
    }
    acceptance_like = outcome in {"STRONG_BREAK_ACCEPTANCE", "SELL_ABSORPTION_BELOW_LEVEL"}

    if ob_lead is not None and ob_lead > 0:
        if reclaim_like:
            return "ORDERBOOK_ADDS_EARLY_RECLAIM_WARNING"
        if acceptance_like:
            return "ORDERBOOK_ADDS_EARLY_ACCEPTANCE_CONFIRMATION"

    if (
        earliest_full is not None
        and earliest_price_only is not None
        and earliest_full > earliest_price_only
    ):
        return "ORDERBOOK_CONFIRMATION_LATE"

    if earliest_price_only is not None and (
        earliest_full is None or earliest_price_only <= earliest_full
    ):
        # Check final agreement between price_only and full at last horizon
        last = ablation_rows[-1] if ablation_rows else {}
        if last.get("price_only") == last.get("full") or last.get("price_only") == outcome:
            return "PRICE_ONLY_WAS_SUFFICIENT"

    if earliest_full is not None and earliest_full >= 300:
        return "ORDERBOOK_CONFIRMATION_LATE"

    return "BREAK_REMAINS_AMBIGUOUS"


def _render_event_charts(
    *,
    out_dir: Path,
    event: BreakEvent,
    micro_1s: list[dict[str, Any]],
    ticks: Sequence[Any],
    acceptance: dict[str, Any],
    causal_rows: list[dict[str, Any]],
    abs_rows: list[dict[str, Any]],
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    if not micro_1s:
        return paths

    level = event.level_price
    ts = pd.to_datetime([r["timestamp"] for r in micro_1s], utc=True)
    mid = [r.get("mid") for r in micro_1s]

    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.plot(ts, mid, color="black", lw=0.9, label="mid")
    ax.axhline(level, color="red", ls="--", label=f"PL {level}")
    ax.axvline(event.break_candle_open, color="orange", ls=":", label="open")
    ax.axvline(event.break_known_at, color="purple", ls=":", label="known")
    if acceptance.get("first_reclaim_ts"):
        ax.axvline(pd.Timestamp(acceptance["first_reclaim_ts"]), color="green", ls="--", label="reclaim")
    ax.set_title(f"{event.event_id} price vs protected low")
    ax.legend(fontsize=7)
    fig.autofmt_xdate()
    p = out_dir / "01_price_level.png"
    fig.tight_layout()
    fig.savefig(p, dpi=100)
    plt.close(fig)
    paths.append(str(p))

    tight = [t for t in ticks if event.tight_start <= ensure_utc(t.trade_ts) < event.tight_end]
    if tight:
        df = pd.DataFrame(
            {
                "ts": [ensure_utc(t.trade_ts) for t in tight],
                "side": [str(t.side).lower() for t in tight],
                "notional": [t.notional for t in tight],
            }
        ).set_index("ts").sort_index()
        buy = df[df.side == "buy"]["notional"].resample("5s").sum().fillna(0)
        sell = df[df.side == "sell"]["notional"].resample("5s").sum().fillna(0)
        cvd = (buy - sell).cumsum()
        fig, ax = plt.subplots(figsize=(11, 3.5))
        ax.bar(buy.index, buy.values, width=0.00005, color="green", alpha=0.5, label="buy")
        ax.bar(sell.index, -sell.values, width=0.00005, color="red", alpha=0.5, label="sell")
        ax2 = ax.twinx()
        ax2.plot(cvd.index, cvd.values, color="navy", lw=1)
        ax.axvline(event.break_known_at, color="purple", ls=":")
        ax.set_title("Aggressor flow + CVD (tight)")
        ax.legend(fontsize=7, loc="upper left")
        fig.autofmt_xdate()
        p = out_dir / "02_aggressor_flow.png"
        fig.tight_layout()
        fig.savefig(p, dpi=100)
        plt.close(fig)
        paths.append(str(p))

    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.plot(ts, [r.get("bid_depth_5bps") for r in micro_1s], label="bid 5bps")
    ax.plot(ts, [r.get("ask_depth_5bps") for r in micro_1s], label="ask 5bps")
    ax2 = ax.twinx()
    ax2.plot(ts, [r.get("imbalance_5bps") for r in micro_1s], color="gray", alpha=0.7, label="imb")
    ax.axvline(event.break_known_at, color="purple", ls=":")
    ax.set_title("Depth + imbalance")
    ax.legend(fontsize=7, loc="upper left")
    fig.autofmt_xdate()
    p = out_dir / "03_book_depth.png"
    fig.tight_layout()
    fig.savefig(p, dpi=100)
    plt.close(fig)
    paths.append(str(p))

    if abs_rows:
        ats = pd.to_datetime([r["timestamp"] for r in abs_rows], utc=True)
        fig, ax = plt.subplots(figsize=(11, 3.5))
        ax.plot(ats, [r.get("bid_wall_pull") for r in abs_rows], label="pull")
        ax.plot(ats, [r.get("bid_wall_replenish") for r in abs_rows], label="replenish")
        ax.axvline(event.break_known_at, color="purple", ls=":")
        ax.set_title("Bid wall pull / replenish")
        ax.legend(fontsize=7)
        fig.autofmt_xdate()
        p = out_dir / "04_pull_replenish.png"
        fig.tight_layout()
        fig.savefig(p, dpi=100)
        plt.close(fig)
        paths.append(str(p))

    fig, ax = plt.subplots(figsize=(11, 3.2))
    hs = [r["horizon_s"] for r in causal_rows]
    labels = [r["classification"] for r in causal_rows]
    ax.plot(hs, list(range(len(hs))), "o-")
    for i, (h, lab) in enumerate(zip(hs, labels)):
        ax.annotate(lab, (h, i), fontsize=7, xytext=(4, 0), textcoords="offset points")
    ax.set_xlabel("horizon_s")
    ax.set_title("Causal classification timeline")
    p = out_dir / "05_causal_class.png"
    fig.tight_layout()
    fig.savefig(p, dpi=100)
    plt.close(fig)
    paths.append(str(p))
    return paths


def analyze_break_event(
    event: BreakEvent,
    db: Any | None = None,
    write_charts_dir: Path | None = None,
) -> dict[str, Any]:
    """Analyze one fixed break event with APT_001-identical acceptance metrics."""
    close_db = False
    if db is None:
        db = connect_readonly()
        close_db = True

    try:
        quality = audit_recorder_quality(
            db,
            symbol=event.symbol,
            start=event.ctx_start,
            end=event.late_end,
            main_start=event.main_start,
            main_end=event.main_end,
            gap_start=event.ctx_start,
            gap_end=event.late_end,
        )
        if not quality.get("pass"):
            return {
                "event": event.to_dict(),
                "quality": quality,
                "data_valid": False,
                "outcome": "EVENT_DATA_INVALID",
                "outcome_raw": "EVENT_DATA_INVALID",
                "case_decision": "EVENT_DATA_INVALID",
                "acceptance": {},
                "causal_rows": [],
                "ablation_rows": [],
                "tradeflow_rows": [],
                "micro_1s": [],
                "abs_rows": [],
                "wall_rows": [],
                "key_events": [],
                "oi": {},
                "charts": [],
                "earliest_price_only_s": None,
                "earliest_full_s": None,
                "earliest_causal_s": None,
                "ob_lead_seconds_vs_price_only": None,
                "error": quality.get("invalid_reason"),
            }

        ticks, trade_diag = load_trade_ticks(
            db, symbol=event.symbol, start=event.ctx_start, end=event.late_end
        )
        snap_ts, snap_u, snap_seq = find_bootstrap_snapshot(
            db, symbol=event.symbol, start=event.ctx_start, end=event.main_end
        )
        events_ob = load_events(
            db,
            symbol=event.symbol,
            snapshot_ts=snap_ts,
            snapshot_u=snap_u,
            snapshot_seq=snap_seq,
            end=event.ctx_end,
        )

        grid_1s = sample_grid(event.main_start, event.main_end, 1)
        grid_5s = sample_grid(event.tight_start, event.tight_end, 5)
        grid_30s = sample_grid(event.main_start, event.main_end, 30)
        grid_1m = sample_grid(event.main_start, event.main_end, 60)
        for h in CAUSAL_HORIZONS_S:
            t = event.break_known_at + timedelta(seconds=h)
            if event.main_start <= t <= event.ctx_end:
                grid_1s.append(t)
        sample_times = sorted(set(grid_1s + grid_5s + grid_30s + grid_1m))

        try:
            _, samples = reconstruct_with_samples(
                events_ob, sample_times=sample_times, end=event.ctx_end
            )
        except ReplayError as exc:
            return {
                "event": event.to_dict(),
                "quality": {**quality, "pass": False, "invalid_reason": str(exc)},
                "data_valid": False,
                "outcome": "EVENT_DATA_INVALID",
                "outcome_raw": "EVENT_DATA_INVALID",
                "case_decision": "EVENT_DATA_INVALID",
                "acceptance": {},
                "causal_rows": [],
                "ablation_rows": [],
                "tradeflow_rows": [],
                "micro_1s": [],
                "abs_rows": [],
                "wall_rows": [],
                "key_events": [],
                "oi": {},
                "charts": [],
                "earliest_price_only_s": None,
                "earliest_full_s": None,
                "earliest_causal_s": None,
                "ob_lead_seconds_vs_price_only": None,
                "error": str(exc),
            }

        def metrics_list(grid: list[datetime]) -> list[dict[str, Any]]:
            rows = []
            for t in grid:
                book = samples.get(t)
                if book is None:
                    prior = [k for k in samples if k <= t]
                    if not prior:
                        continue
                    book = samples[max(prior)]
                rows.append(book_metrics_at(book, level=event.level_price, ts=t))
            return rows

        micro_1s = metrics_list(sorted(set(grid_1s)))

        first_below = first_trade_below(
            ticks,
            level=event.level_price,
            after=event.break_candle_open,
            until=event.break_known_at + timedelta(minutes=1),
        )
        first_below_any = first_trade_below(
            ticks, level=event.level_price, after=event.main_start, until=event.main_end
        )
        first_5bps_touch = None
        for t in ticks:
            ts = ensure_utc(t.trade_ts)
            if ts < event.main_start or ts >= event.break_known_at:
                continue
            if abs(float(t.price) - event.level_price) / event.level_price * 10_000.0 <= 5:
                first_5bps_touch = ts
                break

        reclaim_after_known = first_trade_at_or_above(
            ticks, level=event.level_price, after=event.break_known_at, until=event.late_end
        )

        for r in micro_1s:
            t = pd.Timestamp(r["timestamp"]).to_pydatetime()
            r["phase"] = _assign_phase(
                t, event=event, first_5bps=first_5bps_touch, first_below=first_below
            )

        windows = {
            "pre_attack_A": (event.main_start, first_5bps_touch or event.break_candle_open),
            "attack_B": (
                first_5bps_touch or event.break_candle_open,
                first_below or event.break_candle_open,
            ),
            "break_C": (first_below or event.break_candle_open, event.break_known_at),
            "post_D": (event.break_known_at, event.break_known_at + timedelta(minutes=5)),
            "early_E": (event.break_known_at + timedelta(minutes=5), event.main_end),
            "break_candle": (event.break_candle_open, event.break_known_at),
            "tight": (event.tight_start, event.tight_end),
            "main": (event.main_start, event.main_end),
        }
        tradeflow_rows: list[dict[str, Any]] = []
        for name, (a, b) in windows.items():
            if a is None or b is None or a >= b:
                continue
            row = aggregate_trades(ticks, start=a, end=b)
            row["window"] = name
            row["event_id"] = event.event_id
            if row["start_price"] and row["min_price"] is not None and row["sell_volume"]:
                down_bps = (row["start_price"] - row["min_price"]) / row["start_price"] * 10_000.0
                row["down_move_bps"] = down_bps
                row["sell_efficiency_bps_per_1k"] = sell_efficiency_bps_per_1k(
                    max(down_bps, 0.0), float(row["sell_volume"])
                )
            if row["start_price"] and row["max_price"] is not None and row["buy_volume"]:
                up_bps = (row["max_price"] - row["start_price"]) / row["start_price"] * 10_000.0
                row["up_move_bps"] = up_bps
                row["buy_efficiency_bps_per_1k"] = buy_efficiency_bps_per_1k(
                    max(up_bps, 0.0), float(row["buy_volume"])
                )
            tradeflow_rows.append(row)

        break_tf = next((r for r in tradeflow_rows if r["window"] == "break_candle"), {})
        acceptance = _build_acceptance(
            event=event,
            ticks=ticks,
            first_below=first_below,
            first_below_any=first_below_any,
            reclaim_after_known=reclaim_after_known,
            break_tf=break_tf,
        )

        # 1m / 5m closes
        px = pd.DataFrame(
            {
                "ts": [ensure_utc(t.trade_ts) for t in ticks],
                "price": [float(t.price) for t in ticks],
            }
        ).set_index("ts").sort_index()
        if not px.empty:
            c1 = px["price"].resample("1min", label="left", closed="left").last().dropna()
            c5 = px["price"].resample("5min", label="left", closed="left").last().dropna()
            first_1m_above = None
            for t, v in c1.items():
                if ensure_utc(t.to_pydatetime()) >= event.break_known_at and float(v) >= event.level_price:
                    first_1m_above = ensure_utc(t.to_pydatetime())
                    break
            first_5m_above = None
            for t, v in c5.items():
                if ensure_utc(t.to_pydatetime()) >= event.break_known_at and float(v) >= event.level_price:
                    first_5m_above = ensure_utc(t.to_pydatetime())
                    break
            under_1m = [
                ensure_utc(t.to_pydatetime())
                for t, v in c1.items()
                if event.break_known_at <= ensure_utc(t.to_pydatetime()) < event.main_end
                and float(v) < event.level_price
            ]
            acceptance["first_1m_close_above_level"] = iso_z(first_1m_above)
            acceptance["first_5m_close_above_level"] = iso_z(first_5m_above)
            acceptance["n_1m_closes_below_after_known_to_main_end"] = len(under_1m)
            c_next = c5.get(pd.Timestamp(event.break_known_at))
            acceptance["next_5m_close"] = (
                None
                if c_next is None or (isinstance(c_next, float) and math.isnan(c_next))
                else float(c_next)
            )
            acceptance["next_5m_close_below_level"] = int(
                acceptance["next_5m_close"] is not None
                and acceptance["next_5m_close"] < event.level_price
            )

        if reclaim_after_known is not None:
            for hold in (60, 180, 300, 900):
                end_h = reclaim_after_known + timedelta(seconds=hold)
                rebreak = first_trade_below(
                    ticks, level=event.level_price, after=reclaim_after_known, until=end_h
                )
                acceptance[f"reclaim_holds_{hold}s"] = int(rebreak is None)
                acceptance[f"rebreak_within_{hold}s_of_reclaim"] = iso_z(rebreak)

        # Absorption / wall approx on tight 1s
        abs_rows: list[dict[str, Any]] = []
        wall_rows: list[dict[str, Any]] = []
        prev = None
        for r in micro_1s:
            t = pd.Timestamp(r["timestamp"]).to_pydatetime()
            if not (event.tight_start <= ensure_utc(t) <= event.tight_end):
                prev = r
                continue
            lb = r.get("largest_bid_wall_notional") or 0.0
            pull = replenish = 0.0
            if prev is not None:
                prev_lb = prev.get("largest_bid_wall_notional") or 0.0
                if lb < prev_lb:
                    pull = prev_lb - lb
                else:
                    replenish = lb - prev_lb
            t0 = ensure_utc(t) - timedelta(seconds=1)
            tr = aggregate_trades(ticks, start=t0, end=ensure_utc(t) + timedelta(microseconds=1))
            abs_rows.append(
                {
                    "event_id": event.event_id,
                    "timestamp": r["timestamp"],
                    "phase": r.get("phase"),
                    "sell_volume_1s": tr["sell_volume"],
                    "buy_volume_1s": tr["buy_volume"],
                    "bid_depth_5bps": r.get("bid_depth_5bps"),
                    "ask_depth_5bps": r.get("ask_depth_5bps"),
                    "bid_wall_pull": pull,
                    "bid_wall_replenish": replenish,
                    "below_level": r.get("below_level"),
                    "distance_to_level_bps": r.get("distance_to_level_bps"),
                }
            )
            wall_rows.append(
                {
                    "event_id": event.event_id,
                    "timestamp": r["timestamp"],
                    "largest_bid_wall_price": r.get("largest_bid_wall_price"),
                    "largest_bid_wall_notional": r.get("largest_bid_wall_notional"),
                    "largest_ask_wall_price": r.get("largest_ask_wall_price"),
                    "largest_ask_wall_notional": r.get("largest_ask_wall_notional"),
                    "bid_pull": pull,
                    "bid_replenish": replenish,
                }
            )
            prev = r

        book_by_ts = {r["timestamp"]: r for r in micro_1s}
        causal_rows: list[dict[str, Any]] = []
        ablation_rows: list[dict[str, Any]] = []
        for h in CAUSAL_HORIZONS_S:
            as_of = event.break_known_at + timedelta(seconds=h)
            b = book_by_ts.get(iso_z(as_of))
            if b is None and h == 0:
                b = book_by_ts.get(iso_z(event.break_known_at))
            crow = causal_classify_at_horizon(
                ticks=ticks, event=event, horizon_s=h, book_at_h=b
            )
            causal_rows.append(crow)
            ablation_rows.append(
                _ablation_at_horizon(
                    event=event,
                    ticks=ticks,
                    first_below=first_below,
                    first_below_any=first_below_any,
                    break_tf=break_tf,
                    horizon_s=h,
                    book_at_h=b,
                    causal_row=crow,
                )
            )

        case_metrics = {**acceptance}
        case_raw, case_conf, case_evidence = classify_from_metrics(case_metrics)
        outcome = map_outcome_class(case_raw)

        earliest_causal = _earliest_clear(causal_rows, "classification", CLEAR_CAUSAL)
        earliest_price_only = _earliest_clear(ablation_rows, "price_only", CLEAR_OUTCOME)
        earliest_full = _earliest_clear(ablation_rows, "full", CLEAR_OUTCOME)
        ob_lead = None
        if earliest_price_only is not None and earliest_full is not None:
            ob_lead = float(earliest_price_only - earliest_full)
        elif earliest_full is not None and earliest_price_only is None:
            ob_lead = float(3600 - earliest_full)  # OB found something price_only never did

        case_decision = _case_decision(
            quality_pass=True,
            outcome=outcome,
            ablation_rows=ablation_rows,
            earliest_price_only=earliest_price_only,
            earliest_full=earliest_full,
            ob_lead=ob_lead,
        )

        key_events = [
            {"event": "break_candle_open", "timestamp": iso_z(event.break_candle_open)},
            {
                "event": "first_trade_below_level_in_break_candle",
                "timestamp": iso_z(first_below),
            },
            {"event": "break_known_at_close", "timestamp": iso_z(event.break_known_at)},
            {
                "event": "first_reclaim_trade_after_known",
                "timestamp": iso_z(reclaim_after_known),
            },
        ]

        oi = load_oi_context(db, symbol=event.symbol, start=event.main_start, end=event.main_end)

        charts: list[str] = []
        if write_charts_dir is not None:
            charts = _render_event_charts(
                out_dir=Path(write_charts_dir),
                event=event,
                micro_1s=micro_1s,
                ticks=ticks,
                acceptance=acceptance,
                causal_rows=causal_rows,
                abs_rows=abs_rows,
            )

        book_at_known = book_by_ts.get(iso_z(event.break_known_at)) or {}

        return {
            "event": event.to_dict(),
            "quality": quality,
            "data_valid": True,
            "outcome": outcome,
            "outcome_raw": case_raw,
            "outcome_confidence": case_conf,
            "outcome_evidence": case_evidence,
            "case_decision": case_decision,
            "acceptance": acceptance,
            "causal_rows": causal_rows,
            "ablation_rows": ablation_rows,
            "tradeflow_rows": tradeflow_rows,
            "micro_1s": micro_1s,
            "abs_rows": abs_rows,
            "wall_rows": wall_rows,
            "key_events": key_events,
            "oi": oi,
            "charts": charts,
            "earliest_price_only_s": earliest_price_only,
            "earliest_full_s": earliest_full,
            "earliest_causal_s": earliest_causal,
            "ob_lead_seconds_vs_price_only": ob_lead,
            "book_at_known": {
                "bid_depth_5bps": book_at_known.get("bid_depth_5bps"),
                "ask_depth_5bps": book_at_known.get("ask_depth_5bps"),
                "imbalance_5bps": book_at_known.get("imbalance_5bps"),
                "largest_bid_wall_notional": book_at_known.get("largest_bid_wall_notional"),
            },
            "trade_diag": trade_diag.to_dict() if hasattr(trade_diag, "to_dict") else {},
            "bootstrap": {
                "ts": iso_z(snap_ts),
                "update_id": snap_u,
                "cross_sequence": snap_seq,
            },
            "price_only_outcome": classify_price_only(acceptance),
        }
    finally:
        if close_db and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass


# Re-export horizons for callers / tests
__all__ = [
    "BreakEvent",
    "CAUSAL_HORIZONS_S",
    "CONFIRM_HORIZONS_S",
    "FOLLOW_HORIZONS_S",
    "RECLAIM_HORIZONS_S",
    "FIXED_EVENT_SPECS",
    "analyze_break_event",
    "classify_price_only",
    "load_fixed_events",
    "map_outcome_class",
    "causal_classify_at_horizon",
]
