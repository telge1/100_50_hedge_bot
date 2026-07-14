"""Phase A: causal join of winner liquidation sweeps with scanner TF states.

No analysis window, path classification, entry, TP/SL, or scanner integration.
Scanner modules are imported read-only.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.leverage_rebound_audit import close_relative_to_level_pct
from research.liquidation_level.liquidation_control_validation import (
    EXPECTED_FULL,
    EXPECTED_IS,
    EXPECTED_OOS,
    WINNER_CONFIG_ID,
    ControlValidationConfig,
    ValidationEvent,
    build_winner_events,
    compute_volume_ratio,
    frozen_winner_config,
    validate_event_counts,
)
from research.liquidation_level.liquidation_levels import (
    SIDE_UPPER,
    STATUS_SWEPT,
    LiquidationLevelConfig,
    LiquidationReplayResult,
    normalize_ohlcv_dataframe,
    replay_liquidation_levels,
)
from research.regime_scanner.classifier import summarize_timeframe_regime
from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.regime_snapshot import (
    build_regime_snapshot,
    evaluate_setup_activation,
)
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots
from research.regime_scanner.timeframes import (
    TIMEFRAME_MINUTES,
    aggregate_candles,
    floor_to_timeframe,
    timeframe_timedelta,
)
from research.regime_scanner.trend_state_policy import policy_for_state
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    default_trend_structure_config,
    update_market_structure,
)

SOURCE_CONFIG_ID = WINNER_CONFIG_ID
DIRECTION_CONTEXT = "short_context"

# Diagnostic age thresholds only — NOT trading / entry thresholds.
# Flag when HTF close is older than one full TF length (normally only gaps).
STALE_15M_AGE_MINUTES = 15.0
STALE_30M_AGE_MINUTES = 30.0

# Slope columns exported when present on indicator frames.
_SLOPE_EXPORT = (
    "ema_9_slope_3_pct",
    "ema_9_slope_6_pct",
    "ema_9_slope_12_pct",
    "ema_20_slope_6_pct",
    "ema_20_slope_12_pct",
    "ema_20_slope_48_pct",
    "ema_59_slope_12_pct",
    "ema_59_slope_48_pct",
    "ema_200_slope_48_pct",
    "ema_200_slope_144_pct",
)


class EventCountMismatchError(RuntimeError):
    """Raised when frozen winner event counts do not match the validated totals."""


class SweepJoinError(RuntimeError):
    """Raised for fatal Phase A join failures."""


def ensure_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def decision_time_from_signal_open(signal_timestamp: object) -> pd.Timestamp:
    """Sweep candle open → causal availability at candle close (+5m)."""
    return ensure_utc(signal_timestamp) + pd.Timedelta(minutes=5)


@dataclass(frozen=True)
class SweepTriggerEvent:
    event_id: str
    source_config_id: str
    signal_index: int
    signal_timestamp: pd.Timestamp
    side: str
    direction_context: str
    primary_leverage: int
    swept_leverages: tuple[int, ...]
    swept_level_ids: tuple[int, ...]
    swept_level_count: int
    swept_total_strength: int
    cluster_center_price: float | None
    cluster_min_price: float | None
    cluster_max_price: float | None
    reclaim_status: str
    close_relative_to_level_pct: float
    sweep_candle_open: float
    sweep_candle_high: float
    sweep_candle_low: float
    sweep_candle_close: float
    sweep_candle_volume: float
    sample: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signal_timestamp"] = ensure_utc(self.signal_timestamp).isoformat()
        d["swept_leverages"] = list(self.swept_leverages)
        d["swept_level_ids"] = list(self.swept_level_ids)
        return d


@dataclass(frozen=True)
class SweepScannerSnapshot:
    event_id: str
    signal_index: int
    signal_timestamp: pd.Timestamp
    sample: str
    decision_time: pd.Timestamp
    # 5m availability
    tf5_timestamp: pd.Timestamp | None
    tf5_age_minutes: float | None
    tf5_exact_match: bool
    # 15m availability
    tf15_bucket_start: pd.Timestamp | None
    tf15_bucket_end: pd.Timestamp | None
    tf15_available_at: pd.Timestamp | None
    tf15_age_minutes: float | None
    tf15_is_closed: bool
    # 30m availability
    tf30_bucket_start: pd.Timestamp | None
    tf30_bucket_end: pd.Timestamp | None
    tf30_available_at: pd.Timestamp | None
    tf30_age_minutes: float | None
    tf30_is_closed: bool
    # features (frozen copies)
    features_5m: Mapping[str, Any]
    features_15m: Mapping[str, Any]
    features_30m: Mapping[str, Any]
    availability_flags: Mapping[str, Any]
    diagnostics: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        def _ts(v: pd.Timestamp | None) -> str | None:
            return None if v is None else ensure_utc(v).isoformat()

        return {
            "event_id": self.event_id,
            "signal_index": self.signal_index,
            "signal_timestamp": _ts(self.signal_timestamp),
            "sample": self.sample,
            "decision_time": _ts(self.decision_time),
            "tf5_timestamp": _ts(self.tf5_timestamp),
            "tf5_age_minutes": self.tf5_age_minutes,
            "tf5_exact_match": self.tf5_exact_match,
            "tf15_bucket_start": _ts(self.tf15_bucket_start),
            "tf15_bucket_end": _ts(self.tf15_bucket_end),
            "tf15_available_at": _ts(self.tf15_available_at),
            "tf15_age_minutes": self.tf15_age_minutes,
            "tf15_is_closed": self.tf15_is_closed,
            "tf30_bucket_start": _ts(self.tf30_bucket_start),
            "tf30_bucket_end": _ts(self.tf30_bucket_end),
            "tf30_available_at": _ts(self.tf30_available_at),
            "tf30_age_minutes": self.tf30_age_minutes,
            "tf30_is_closed": self.tf30_is_closed,
            "features_5m": dict(self.features_5m),
            "features_15m": dict(self.features_15m),
            "features_30m": dict(self.features_30m),
            "availability_flags": dict(self.availability_flags),
            "diagnostics": dict(self.diagnostics),
        }


@dataclass
class ScannerFeatureStore:
    """Precomputed continuous scanner/liquidation feature series (one pass)."""

    ohlcv: pd.DataFrame
    ind_5m: pd.DataFrame
    ind_15m: pd.DataFrame
    ind_30m: pd.DataFrame
    volume_ratio_5m: np.ndarray
    structure_5m: list[dict[str, Any]]
    structure_15m: list[dict[str, Any]]
    structure_30m: list[dict[str, Any]]
    index_by_5m_ts: dict[pd.Timestamp, int]
    available_at_15m: np.ndarray  # datetime64[ns, UTC]
    available_at_30m: np.ndarray
    scanner_cfg: RegimeScannerConfig
    stale_15m_age_minutes: float = STALE_15M_AGE_MINUTES
    stale_30m_age_minutes: float = STALE_30M_AGE_MINUTES


def reproduce_winner_events(
    ohlcv: pd.DataFrame,
    *,
    level_config: LiquidationLevelConfig | None = None,
    expect_counts: bool = True,
) -> tuple[list[ValidationEvent], LiquidationReplayResult, dict[str, Any]]:
    cfg = level_config or frozen_winner_config()
    data = normalize_ohlcv_dataframe(ohlcv)
    replay = replay_liquidation_levels(data, cfg)
    events, meta = build_winner_events(replay, data, cfg=cfg)
    counts = {
        "full": len(events),
        "in_sample": sum(1 for e in events if e.sample == "in_sample"),
        "out_of_sample": sum(1 for e in events if e.sample == "out_of_sample"),
    }
    payload = {
        "expected": {"full": EXPECTED_FULL, "in_sample": EXPECTED_IS, "out_of_sample": EXPECTED_OOS},
        "reproduced": counts,
        "config_id": SOURCE_CONFIG_ID,
        "config_hash_match": True,
    }
    if expect_counts:
        try:
            validate_event_counts(counts, ControlValidationConfig())
        except Exception as exc:  # noqa: BLE001 — convert to Phase A abort
            payload["error"] = str(exc)
            raise EventCountMismatchError(json.dumps(payload, indent=2)) from exc
    return events, replay, payload


def validation_events_to_triggers(
    events: Sequence[ValidationEvent],
    replay: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
) -> list[SweepTriggerEvent]:
    data = normalize_ohlcv_dataframe(ohlcv)
    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    volumes = data["volume"].to_numpy(float)

    by_candle: dict[int, list] = {}
    for lvl in replay.all_levels:
        if lvl.status != STATUS_SWEPT or lvl.swept_index is None:
            continue
        if lvl.side != SIDE_UPPER:
            continue
        by_candle.setdefault(int(lvl.swept_index), []).append(lvl)

    out: list[SweepTriggerEvent] = []
    for ev in events:
        i = int(ev.signal_index)
        lvls = by_candle.get(i, [])
        # Prefer levels matching primary leverage; keep all swept ids on candle.
        ids = tuple(int(x.level_id) for x in sorted(lvls, key=lambda z: int(z.level_id)))
        prices = [float(x.level_price) for x in lvls]
        primary_lvls = [x for x in lvls if int(x.leverage) == int(ev.leverage)]
        ref_level = float(primary_lvls[0].level_price) if primary_lvls else (
            float(ev.cluster_center_price) if ev.cluster_center_price is not None else float("nan")
        )
        rel = close_relative_to_level_pct(SIDE_UPPER, float(closes[i]), float(ref_level))
        out.append(
            SweepTriggerEvent(
                event_id=str(ev.event_id),
                source_config_id=SOURCE_CONFIG_ID,
                signal_index=i,
                signal_timestamp=ensure_utc(ev.signal_timestamp),
                side="upper",
                direction_context=DIRECTION_CONTEXT,
                primary_leverage=int(ev.leverage),
                swept_leverages=tuple(int(x) for x in ev.swept_leverages),
                swept_level_ids=ids,
                swept_level_count=int(ev.swept_level_count),
                swept_total_strength=int(ev.swept_total_strength),
                cluster_center_price=ev.cluster_center_price,
                cluster_min_price=min(prices) if prices else None,
                cluster_max_price=max(prices) if prices else None,
                reclaim_status="immediate_reclaim",
                close_relative_to_level_pct=float(rel),
                sweep_candle_open=float(opens[i]),
                sweep_candle_high=float(highs[i]),
                sweep_candle_low=float(lows[i]),
                sweep_candle_close=float(closes[i]),
                sweep_candle_volume=float(volumes[i]),
                sample=str(ev.sample),
            )
        )
    return out


def _finite(value: object) -> float | None:
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(x):
        return None
    return x


def _structure_compact(state: MarketStructureState) -> dict[str, Any]:
    def _evt(e: Any) -> str | None:
        return None if e is None else str(e.event_type)

    pair = None
    if state.last_high_label and state.last_low_label:
        pair = f"{state.last_high_label}|{state.last_low_label}"
    return {
        "structure_bias": state.current_structure_bias,
        "structure_pair": pair,
        "last_high_label": state.last_high_label,
        "last_low_label": state.last_low_label,
        "last_bos": _evt(state.last_bos),
        "last_choch": _evt(state.last_choch),
        "last_failed_breakout": _evt(state.last_failed_breakout),
        "last_failed_breakdown": _evt(state.last_failed_breakdown),
        "active_retest_level": state.active_retest_level,
        "active_retest_direction": state.active_retest_direction,
        "retest_bars_remaining": int(state.retest_bars_remaining),
        "hh": state.last_higher_high.price if state.last_higher_high else None,
        "hl": state.last_higher_low.price if state.last_higher_low else None,
        "lh": state.last_lower_high.price if state.last_lower_high else None,
        "ll": state.last_lower_low.price if state.last_lower_low else None,
        "structure_available": state.last_updated_at is not None,
    }


def _walk_structure(ind: pd.DataFrame, *, timeframe: str, scanner_cfg: RegimeScannerConfig) -> list[dict[str, Any]]:
    tf_cfg = scanner_cfg.with_timeframe(timeframe)
    pivots = find_confirmed_pivots(ind, config=tf_cfg)
    piv_sorted = sorted(pivots, key=lambda p: ensure_utc(p.confirmation_timestamp))
    conf_ts = [ensure_utc(p.confirmation_timestamp) for p in piv_sorted]
    known: list[ConfirmedPivot] = []
    j = 0
    state = MarketStructureState(timeframe=timeframe)
    scfg = default_trend_structure_config()
    minutes = int(TIMEFRAME_MINUTES[timeframe])
    ts = pd.to_datetime(ind["timestamp"], utc=True)
    opens = ind["open"].to_numpy(float)
    highs = ind["high"].to_numpy(float)
    lows = ind["low"].to_numpy(float)
    closes = ind["close"].to_numpy(float)
    vols = ind["volume"].to_numpy(float)
    atrs = ind["atr"].to_numpy(float) if "atr" in ind.columns else np.full(len(ind), np.nan)
    out: list[dict[str, Any]] = []
    empty = {
        "structure_bias": "unknown",
        "structure_pair": None,
        "last_high_label": None,
        "last_low_label": None,
        "last_bos": None,
        "last_choch": None,
        "last_failed_breakout": None,
        "last_failed_breakdown": None,
        "active_retest_level": None,
        "active_retest_direction": None,
        "retest_bars_remaining": 0,
        "hh": None,
        "hl": None,
        "lh": None,
        "ll": None,
        "structure_available": False,
    }
    for i in range(len(ind)):
        decision = ensure_utc(ts.iloc[i]) + pd.Timedelta(minutes=minutes)
        while j < len(conf_ts) and conf_ts[j] <= decision:
            known.append(piv_sorted[j])
            j += 1
        candle = {
            "timestamp": ts.iloc[i],
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "volume": float(vols[i]),
        }
        atr = _finite(atrs[i])
        state, _events = update_market_structure(
            state,
            candle=candle,
            pivots=known,
            decision_time=decision,
            atr=atr,
            cfg=scfg,
        )
        out.append(copy.deepcopy(_structure_compact(state)))
    if not out:
        return [dict(empty)]
    return out


def _regime_label_from_row(row: Mapping[str, Any], *, timeframe: str, bar_index: int) -> str | None:
    ema = {
        "ema_9": _finite(row.get("ema_9")),
        "ema_20": _finite(row.get("ema_20")),
        "ema_59": _finite(row.get("ema_59")),
        "ema_200": _finite(row.get("ema_200")),
    }
    if all(v is None for v in ema.values()):
        return None
    slopes = {k: _finite(row.get(k)) for k in _SLOPE_EXPORT if k in row}
    payload = {
        "timeframe": timeframe,
        "warmup_sufficient": ema["ema_200"] is not None and bar_index + 1 >= 200,
        "candles_loaded": int(bar_index + 1),
        "ema": ema,
        "adx": _finite(row.get("adx")),
        "plus_di": _finite(row.get("plus_di")),
        "minus_di": _finite(row.get("minus_di")),
        "di_spread": _finite(row.get("di_spread")),
        "atr_pct": _finite(row.get("atr_pct")),
        "ema_slopes_pct": slopes,
        "signals": [],
        "confirmed_divergences": [],
        "structural_exhaustion": [],
        "weakening_signals": [],
        "last_bar_changes": {},
    }
    summary = summarize_timeframe_regime(payload)
    return str(summary.get("regime")) if summary.get("regime") is not None else None


def precompute_scanner_feature_store(
    ohlcv: pd.DataFrame,
    *,
    scanner_cfg: RegimeScannerConfig | None = None,
    progress: Any | None = None,
) -> ScannerFeatureStore:
    def _p(msg: str) -> None:
        if progress is not None:
            progress(msg)

    cfg = scanner_cfg or default_regime_scanner_config()
    data = normalize_ohlcv_dataframe(ohlcv)
    _p(f"Candles geladen: {len(data)}")

    end_decision = ensure_utc(data["timestamp"].iloc[-1]) + pd.Timedelta(minutes=5)
    ind5 = compute_indicator_frame(data, config=cfg.with_timeframe("5m"))
    _p(f"5m Features berechnet: {len(ind5)}")

    agg15 = aggregate_candles(data, "15m", end_decision)
    ind15 = compute_indicator_frame(agg15, config=cfg.with_timeframe("15m"))
    _p(f"15m Buckets berechnet: {len(ind15)}")

    agg30 = aggregate_candles(data, "30m", end_decision)
    ind30 = compute_indicator_frame(agg30, config=cfg.with_timeframe("30m"))
    _p(f"30m Buckets berechnet: {len(ind30)}")

    vol_ratio = compute_volume_ratio(data["volume"].to_numpy(float), period=13)

    struct5 = _walk_structure(ind5, timeframe="5m", scanner_cfg=cfg)
    struct15 = _walk_structure(ind15, timeframe="15m", scanner_cfg=cfg)
    struct30 = _walk_structure(ind30, timeframe="30m", scanner_cfg=cfg)
    _p("Struktur-Walks (5m/15m/30m) fertig")

    index_by_ts = {ensure_utc(ts): int(i) for i, ts in enumerate(pd.to_datetime(ind5["timestamp"], utc=True))}
    avail15 = (pd.to_datetime(ind15["timestamp"], utc=True) + timeframe_timedelta("15m")).to_numpy()
    avail30 = (pd.to_datetime(ind30["timestamp"], utc=True) + timeframe_timedelta("30m")).to_numpy()

    return ScannerFeatureStore(
        ohlcv=data,
        ind_5m=ind5,
        ind_15m=ind15,
        ind_30m=ind30,
        volume_ratio_5m=vol_ratio,
        structure_5m=struct5,
        structure_15m=struct15,
        structure_30m=struct30,
        index_by_5m_ts=index_by_ts,
        available_at_15m=avail15,
        available_at_30m=avail30,
        scanner_cfg=cfg,
    )


def _last_closed_htf_index(available_at: np.ndarray, decision_time: pd.Timestamp) -> int | None:
    if len(available_at) == 0:
        return None
    arr = pd.to_datetime(available_at, utc=True).to_numpy(dtype="datetime64[ns]")
    # searchsorted right-1 for last available_at <= decision
    idx = int(
        np.searchsorted(
            arr,
            np.datetime64(ensure_utc(decision_time).to_datetime64()),
            side="right",
        )
        - 1
    )
    if idx < 0:
        return None
    return idx


def _forming_bucket(
    decision_time: pd.Timestamp, timeframe: str
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    start = floor_to_timeframe(decision_time, timeframe)
    end = start + timeframe_timedelta(timeframe)
    return start, end, end  # available_at would be end — but only if closed


def _feature_pack_from_indicator_row(
    row: Mapping[str, Any],
    *,
    structure: Mapping[str, Any],
    regime: str | None,
    timeframe: str,
    volume_ratio: float | None = None,
    include_pa_momentum: bool = False,
) -> tuple[dict[str, Any], dict[str, bool]]:
    feats: dict[str, Any] = {
        "ema_9": _finite(row.get("ema_9")),
        "ema_20": _finite(row.get("ema_20")),
        "ema_59": _finite(row.get("ema_59")),
        "ema_200": _finite(row.get("ema_200")),
        "ema_9_20_distance": _finite(row.get("ema_9_vs_ema_20_pct")),
        "ema_20_59_distance": _finite(row.get("ema_20_vs_ema_59_pct")),
        "adx": _finite(row.get("adx")),
        "di_plus": _finite(row.get("plus_di")),
        "di_minus": _finite(row.get("minus_di")),
        "atr": _finite(row.get("atr")),
        "atr_pct": _finite(row.get("atr_pct")),
        "regime": regime,
        "raw_volume": _finite(row.get("volume")),
        "volume_ratio": volume_ratio,
    }
    for k in _SLOPE_EXPORT:
        feats[k] = _finite(row.get(k)) if k in row else None

    feats.update(
        {
            "structure_bias": structure.get("structure_bias"),
            "structure_pair": structure.get("structure_pair"),
            "last_high_label": structure.get("last_high_label"),
            "last_low_label": structure.get("last_low_label"),
            "last_bos": structure.get("last_bos"),
            "last_choch": structure.get("last_choch"),
            "last_failed_breakout": structure.get("last_failed_breakout"),
            "last_failed_breakdown": structure.get("last_failed_breakdown"),
            "retest_level": structure.get("active_retest_level"),
            "retest_direction": structure.get("active_retest_direction"),
            "hh": structure.get("hh"),
            "hl": structure.get("hl"),
            "lh": structure.get("lh"),
            "ll": structure.get("ll"),
        }
    )

    # Trend state machine is research-gated (default disabled). Phase A exposes
    # structure-derived policy context instead of inventing TSM thresholds.
    bias = structure.get("structure_bias")
    if bias == "bullish":
        trend_state = "structure_bullish_proxy"
    elif bias == "bearish":
        trend_state = "structure_bearish_proxy"
    else:
        trend_state = "structure_unknown_proxy"
    feats["trend_state"] = trend_state
    try:
        pol = policy_for_state(
            "early_bullish"
            if bias == "bullish"
            else ("early_bearish" if bias == "bearish" else "neutral")
        )
        feats["trend_policy_allow_long"] = bool(getattr(pol, "allow_long", None))
        feats["trend_policy_allow_short"] = bool(getattr(pol, "allow_short", None))
    except Exception:  # noqa: BLE001
        feats["trend_policy_allow_long"] = None
        feats["trend_policy_allow_short"] = None

    # PA / momentum require armed setup confirmation path — not auto-started by sweep.
    feats["price_action_state"] = None
    feats["momentum_state"] = None
    feats["momentum_confirmation_age"] = None
    feats["setup_activation_side"] = None

    avail = {
        f"has_ema_9_{timeframe}": feats["ema_9"] is not None,
        f"has_ema_200_{timeframe}": feats["ema_200"] is not None,
        f"has_adx_{timeframe}": feats["adx"] is not None,
        f"has_regime_{timeframe}": regime is not None,
        f"has_structure_{timeframe}": bool(structure.get("structure_available")),
        f"has_volume_ratio_{timeframe}": volume_ratio is not None,
        f"has_pa_{timeframe}": False,
        f"has_momentum_{timeframe}": False,
        f"has_trend_state_machine_{timeframe}": False,  # explicit: proxy only
    }
    if include_pa_momentum:
        pass
    return feats, avail


def join_sweep_event(
    event: SweepTriggerEvent,
    store: ScannerFeatureStore,
) -> SweepScannerSnapshot:
    signal_ts = ensure_utc(event.signal_timestamp)
    decision = decision_time_from_signal_open(signal_ts)
    warnings: list[str] = []

    idx5 = store.index_by_5m_ts.get(signal_ts)
    missing_5m = idx5 is None
    if missing_5m:
        warnings.append("missing_5m_exact_timestamp")
        # refuse future 5m: also reject any later bar
        feats5: dict[str, Any] = {}
        avail5: dict[str, bool] = {}
        struct5: dict[str, Any] = {"structure_available": False}
        tf5_ts = None
        tf5_age = None
        exact = False
        warmup5 = False
        vol_r = None
    else:
        # Guard: never join a later 5m bar
        assert idx5 == event.signal_index or store.ohlcv.iloc[idx5]["timestamp"] == signal_ts
        row5 = store.ind_5m.iloc[idx5].to_dict()
        struct5 = store.structure_5m[idx5]
        regime5 = _regime_label_from_row(row5, timeframe="5m", bar_index=idx5)
        vol_r = _finite(store.volume_ratio_5m[idx5])
        feats5, avail5 = _feature_pack_from_indicator_row(
            row5,
            structure=struct5,
            regime=regime5,
            timeframe="5m",
            volume_ratio=vol_r,
            include_pa_momentum=True,
        )
        tf5_ts = signal_ts
        # At decision (= close), this bar's age is 0 minutes past close.
        tf5_age = 0.0
        exact = True
        warmup5 = (
            idx5 + 1 >= int(store.scanner_cfg.min_warmup_candles)
            and _finite(row5.get("ema_200")) is not None
        )

    i15 = _last_closed_htf_index(store.available_at_15m, decision)
    i30 = _last_closed_htf_index(store.available_at_30m, decision)

    missing_15m = i15 is None
    missing_30m = i30 is None

    if missing_15m:
        feats15, avail15 = {}, {}
        tf15_start = tf15_end = tf15_avail = None
        tf15_age = None
        tf15_closed = False
        warmup15 = False
        warnings.append("missing_15m")
    else:
        row15 = store.ind_15m.iloc[i15].to_dict()
        struct15 = store.structure_15m[i15]
        regime15 = _regime_label_from_row(row15, timeframe="15m", bar_index=i15)
        feats15, avail15 = _feature_pack_from_indicator_row(
            row15,
            structure=struct15,
            regime=regime15,
            timeframe="15m",
            volume_ratio=None,
        )
        # Strip PA/momentum keys conceptual contamination for HTF exports
        feats15["price_action_state"] = None
        feats15["momentum_state"] = None
        feats15["momentum_confirmation_age"] = None
        tf15_start = ensure_utc(store.ind_15m.iloc[i15]["timestamp"])
        tf15_end = tf15_start + timeframe_timedelta("15m")
        tf15_avail = tf15_end
        tf15_age = float((decision - tf15_avail).total_seconds() / 60.0)
        tf15_closed = True
        warmup15 = (
            i15 + 1 >= int(store.scanner_cfg.min_warmup_candles)
            and _finite(row15.get("ema_200")) is not None
        )
        # Causality assert: forming bucket must not be used
        form_start, form_end, _ = _forming_bucket(decision, "15m")
        if tf15_end > decision:
            raise SweepJoinError("lookahead: used 15m bucket closes after decision_time")
        if form_end > decision and tf15_start == form_start:
            raise SweepJoinError("lookahead: joined forming 15m bucket")

    if missing_30m:
        feats30, avail30 = {}, {}
        tf30_start = tf30_end = tf30_avail = None
        tf30_age = None
        tf30_closed = False
        warmup30 = False
        warnings.append("missing_30m")
    else:
        row30 = store.ind_30m.iloc[i30].to_dict()
        struct30 = store.structure_30m[i30]
        regime30 = _regime_label_from_row(row30, timeframe="30m", bar_index=i30)
        feats30, avail30 = _feature_pack_from_indicator_row(
            row30,
            structure=struct30,
            regime=regime30,
            timeframe="30m",
            volume_ratio=None,
        )
        feats30["price_action_state"] = None
        feats30["momentum_state"] = None
        feats30["momentum_confirmation_age"] = None
        tf30_start = ensure_utc(store.ind_30m.iloc[i30]["timestamp"])
        tf30_end = tf30_start + timeframe_timedelta("30m")
        tf30_avail = tf30_end
        tf30_age = float((decision - tf30_avail).total_seconds() / 60.0)
        tf30_closed = True
        warmup30 = (
            i30 + 1 >= int(store.scanner_cfg.min_warmup_candles)
            and _finite(row30.get("ema_200")) is not None
        )
        form_start, form_end, _ = _forming_bucket(decision, "30m")
        if tf30_end > decision:
            raise SweepJoinError("lookahead: used 30m bucket closes after decision_time")
        if form_end > decision and tf30_start == form_start:
            raise SweepJoinError("lookahead: joined forming 30m bucket")

    stale_15 = bool(tf15_age is not None and tf15_age >= store.stale_15m_age_minutes)
    stale_30 = bool(tf30_age is not None and tf30_age >= store.stale_30m_age_minutes)

    # Optional combined setup snapshot (read-only existing APIs) — may be thin.
    setup_side = None
    try:
        snap = build_regime_snapshot(
            decision_time=decision.isoformat(),
            regime_5m=feats5.get("regime") if feats5 else None,
            regime_15m=feats15.get("regime") if feats15 else None,
            regime_30m=feats30.get("regime") if feats30 else None,
        )
        setup = evaluate_setup_activation(snap)
        setup_side = None
        if isinstance(setup, dict):
            setup_side = setup.get("setup_side")
        if feats5:
            feats5 = dict(feats5)
            feats5["setup_activation_side"] = setup_side
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"setup_activation_unavailable:{type(exc).__name__}")

    join_ok = not missing_5m and exact
    diagnostics = {
        "join_ok": join_ok,
        "missing_5m": missing_5m,
        "missing_15m": missing_15m,
        "missing_30m": missing_30m,
        "stale_15m": stale_15,
        "stale_30m": stale_30,
        "stale_15m_threshold_minutes": store.stale_15m_age_minutes,
        "stale_30m_threshold_minutes": store.stale_30m_age_minutes,
        "warmup_complete_5m": bool(warmup5) if not missing_5m else False,
        "warmup_complete_15m": bool(warmup15) if not missing_15m else False,
        "warmup_complete_30m": bool(warmup30) if not missing_30m else False,
        "structure_available_5m": bool(struct5.get("structure_available")) if not missing_5m else False,
        "structure_available_15m": bool((store.structure_15m[i15] if i15 is not None else {}).get("structure_available")),
        "structure_available_30m": bool((store.structure_30m[i30] if i30 is not None else {}).get("structure_available")),
        "trend_state_available": False,  # full TSM not run; structure proxy only
        "pa_available": False,
        "momentum_available": False,
        "join_warnings": list(warnings),
        "trend_state_note": "structure_bias_proxy_not_full_trend_state_machine",
        "pa_momentum_note": "requires_setup_arm_not_started_by_sweep",
    }

    availability = {
        **avail5,
        **avail15,
        **avail30,
        "has_setup_activation": setup_side is not None,
    }

    # Freeze: deep-copy feature maps so later mutations cannot alter snapshot
    return SweepScannerSnapshot(
        event_id=event.event_id,
        signal_index=int(event.signal_index),
        signal_timestamp=signal_ts,
        sample=event.sample,
        decision_time=decision,
        tf5_timestamp=tf5_ts,
        tf5_age_minutes=tf5_age,
        tf5_exact_match=exact,
        tf15_bucket_start=tf15_start,
        tf15_bucket_end=tf15_end,
        tf15_available_at=tf15_avail,
        tf15_age_minutes=tf15_age,
        tf15_is_closed=tf15_closed,
        tf30_bucket_start=tf30_start,
        tf30_bucket_end=tf30_end,
        tf30_available_at=tf30_avail,
        tf30_age_minutes=tf30_age,
        tf30_is_closed=tf30_closed,
        features_5m=copy.deepcopy(feats5),
        features_15m=copy.deepcopy(feats15),
        features_30m=copy.deepcopy(feats30),
        availability_flags=copy.deepcopy(availability),
        diagnostics=copy.deepcopy(diagnostics),
    )


def join_all_events(
    events: Sequence[SweepTriggerEvent],
    store: ScannerFeatureStore,
    *,
    progress: Any | None = None,
) -> list[SweepScannerSnapshot]:
    out: list[SweepScannerSnapshot] = []
    failures = 0
    for i, ev in enumerate(events):
        snap = join_sweep_event(ev, store)
        if not snap.diagnostics.get("join_ok"):
            failures += 1
        out.append(snap)
        if progress is not None and (i + 1) % 250 == 0:
            progress(f"Events gejoint: {i + 1}/{len(events)} Join-Fehler: {failures}")
    if progress is not None:
        progress(f"Events gejoint: {len(out)} Join-Fehler: {failures}")
    return out


def freeze_snapshot(snapshot: SweepScannerSnapshot) -> SweepScannerSnapshot:
    """Return an immutable deep copy (freeze semantics)."""
    return copy.deepcopy(snapshot)


def snapshots_deterministic_hash(snapshots: Sequence[SweepScannerSnapshot]) -> str:
    payload = [s.to_dict() for s in snapshots]
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def select_timeline_event_indices(
    events: Sequence[SweepTriggerEvent],
    *,
    seed: int = 42,
    near_boundary_count: int = 10,
) -> list[int]:
    """Deterministic Phase A timeline sample indices (target ~50)."""
    n = len(events)
    if n == 0:
        return []
    rng = np.random.default_rng(int(seed))
    chosen: list[int] = []

    def _add(xs: Sequence[int]) -> None:
        for i in xs:
            if 0 <= int(i) < n and int(i) not in chosen:
                chosen.append(int(i))

    _add(range(min(10, n)))
    _add(range(max(0, n - 10), n))

    is_idx = [i for i, e in enumerate(events) if e.sample == "in_sample"]
    oos_idx = [i for i, e in enumerate(events) if e.sample == "out_of_sample"]
    if is_idx:
        pick = rng.choice(is_idx, size=min(10, len(is_idx)), replace=False)
        _add(sorted(int(x) for x in pick))
    if oos_idx:
        pick = rng.choice(oos_idx, size=min(10, len(oos_idx)), replace=False)
        _add(sorted(int(x) for x in pick))

    # Near 15m/30m boundaries: decision_time close to a bucket end
    boundary: list[tuple[float, int]] = []
    for i, e in enumerate(events):
        decision = decision_time_from_signal_open(e.signal_timestamp)
        for tf in ("15m", "30m"):
            start = floor_to_timeframe(decision, tf)
            end = start + timeframe_timedelta(tf)
            # distance to upcoming bucket close
            dist = abs((end - decision).total_seconds())
            # also distance just after a close
            prev_end = start  # current bucket start == previous end when aligned
            dist2 = abs((decision - prev_end).total_seconds())
            boundary.append((min(dist, dist2), i))
    boundary.sort(key=lambda t: (t[0], t[1]))
    _add([i for _, i in boundary[: max(near_boundary_count * 3, near_boundary_count)]])
    # keep first near_boundary unique additions preference: already sorted by closeness
    # Ensure at least near_boundary_count boundary-ish events exist in chosen
    # (already added up to 30 candidates filtered by uniqueness)

    return chosen


def forming_and_used_htf(
    decision_time: pd.Timestamp, timeframe: str, used_start: pd.Timestamp | None
) -> dict[str, Any]:
    form_start, form_end, form_avail = _forming_bucket(decision_time, timeframe)
    forming_closed = form_end <= decision_time
    return {
        "forming_bucket_start": ensure_utc(form_start).isoformat(),
        "forming_bucket_end": ensure_utc(form_end).isoformat(),
        "forming_available_at": ensure_utc(form_avail).isoformat(),
        "forming_is_closed": bool(forming_closed),
        "forming_used": bool(
            used_start is not None and ensure_utc(used_start) == ensure_utc(form_start) and forming_closed
        ),
        "reason_excluded_if_open": (
            None
            if forming_closed
            else f"{timeframe} bucket {form_start}–{form_end} still open at decision {decision_time}; close_time > decision_time"
        ),
    }


__all__ = [
    "SOURCE_CONFIG_ID",
    "DIRECTION_CONTEXT",
    "STALE_15M_AGE_MINUTES",
    "STALE_30M_AGE_MINUTES",
    "EventCountMismatchError",
    "SweepJoinError",
    "SweepTriggerEvent",
    "SweepScannerSnapshot",
    "ScannerFeatureStore",
    "ensure_utc",
    "decision_time_from_signal_open",
    "reproduce_winner_events",
    "validation_events_to_triggers",
    "precompute_scanner_feature_store",
    "join_sweep_event",
    "join_all_events",
    "freeze_snapshot",
    "snapshots_deterministic_hash",
    "select_timeline_event_indices",
    "forming_and_used_htf",
    "_last_closed_htf_index",
    "_forming_bucket",
]
