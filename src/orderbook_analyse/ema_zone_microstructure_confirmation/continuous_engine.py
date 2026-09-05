"""Autonomous causal zone-event detection (no manual windows / centers)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.ema_zone_microstructure_confirmation.candidate_states import (
    map_primary_to_candidate,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_defaults import (
    COOLDOWN_S,
    FORMAT_VERSION,
    MAX_CONFIRMATION_DURATION_S,
    MAX_WATCH_DURATION_S,
    PROXIMITY_WATCH_MAX_PCT,
    REARM_LEAVE_HALFWIDTH_MULT,
    TIMEOUT_S,
    TRADE_WINDOWS_S,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    ZONE_ATR_FRAC,
    ZONE_MIN_TICKS,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.oi_liq import (
    liquidation_features,
    oi_features,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.directional_clearance import (
    analyze_directional_clearance,
    clearance_fields_for_emit,
    enrich_clearance_for_emit,
    expected_move_direction,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import (
    COMPUTATION_MODE_EMA_ONLY,
    COMPUTATION_MODE_EMA_PLUS_MICRO,
    build_ema_setup_event,
    ema_setup_layer_fields,
    make_setup_id,
    microstructure_layer_fields,
    normalize_computation_mode,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.proximity import (
    classify_zone_approach_event,
    classify_zone_approach_from_candle_ohlc,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
    apply_regime_gate_to_candidate,
    detect_ema200_flip_timestamps,
    evaluate_regime_gate,
    flat_block_payload,
    flat_diagnostics,
    regime_snapshot,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.stage_a import (
    DIRECTION_NONE,
    attach_direction_fields,
    freeze_role_fields,
    involves_ema200,
    is_stacked_zone,
    post_break_role_after_confirmed_breakout,
    stage_a_direction_payload,
    zone_role_from_approach,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.zones_ext import (
    approach_side,
    stacked_zone_label,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.classify import (
    classify_window,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.impact import (
    classify_flow_mechanism,
    summarize_trades,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    find_swings,
    last_closed_bar_at,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zone_replay import (
    AnalysisSample,
    is_majorish,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    EmaZone,
    zone_half_width,
    zones_overlap,
)


def _iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _asof_ms(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def _flat_metric_fields(diag: dict[str, Any]) -> dict[str, Any]:
    return {
        "flat_reason": diag.get("flat_reason") or "",
        "ema9_slope_norm": diag.get("ema9_slope_norm"),
        "ema20_slope_norm": diag.get("ema20_slope_norm"),
        "ema59_slope_norm": diag.get("ema59_slope_norm"),
        "ema_spread_pct": diag.get("ema_spread_pct"),
        "ema_cross_count": diag.get("ema_cross_count"),
        "ema_reorder_count": diag.get("ema_reorder_count"),
    }


def _make_zone(name: str, center: float, atr: float, tick: float) -> EmaZone:
    hw = zone_half_width(atr, tick=tick)
    return EmaZone(name=name, center=center, low=center - hw, high=center + hw, half_width=hw, atr=atr)


def _dist_outside(zone: EmaZone, px: float) -> float:
    if zone.low <= px <= zone.high:
        return 0.0
    if px < zone.low:
        return zone.low - px
    return px - zone.high


def _apply_rearm_tracking(
    buf: DetectorBuffers,
    *,
    zkey: str,
    zone: EmaZone,
    inside: bool,
    dist: float,
) -> None:
    """Mark zone re-armed after price leaves the active band."""
    if is_stacked_zone(zkey):
        # Merged STACKED bands use min(low)..max(high) across overlapping EMAs.
        # half_width leave distance is the full merged span / 2 and rarely met in
        # trending markets — close leaving the merged band is sufficient to rearm.
        if not inside:
            buf.last_outside[zkey] = True
        return
    if not inside and dist >= zone.half_width * REARM_LEAVE_HALFWIDTH_MULT:
        buf.last_outside[zkey] = True


def _zone_role(approach: str, regime: str | None = None) -> str:
    """V2: approach → role only. ``regime`` ignored (kept for call-site compatibility)."""
    _ = regime
    return zone_role_from_approach(approach)


def candidate_direction(state: str, zone_role: str) -> tuple[str, str]:
    """Direction only after Stage-B confirmed states × frozen Stage-A role.

    Non-directional outcomes return ``NONE`` (never empty string for V2 contract).
    """
    if zone_role in ("ambiguous", "neutral", ""):
        return DIRECTION_NONE, "ambiguous_zone_role"
    if state in (
        "no_trade",
        "data_incomplete",
        "block_flat_compression",
        "watch_zone",
        "wait_microstructure_confirmation",
        "wait_next_zone_confirmation",
    ):
        return DIRECTION_NONE, "no_direction"
    if state == "defense_rejection_confirmed":
        if zone_role == "resistance":
            return "SHORT", "ask_defense_resistance"
        if zone_role == "support":
            return "LONG", "bid_defense_support"
    if state == "breakout_confirmed":
        if zone_role == "resistance":
            return "LONG", "breakout_up_through_resistance"
        if zone_role == "support":
            return "SHORT", "breakout_down_through_support"
    if state == "false_breakout_confirmed":
        if zone_role == "resistance":
            return "SHORT", "false_breakout_reclaim_below"
        if zone_role == "support":
            return "LONG", "false_breakout_reclaim_above"
    if state in ("possible_regime_flip", "full_regime_flip_confirmed"):
        if zone_role == "resistance":
            return "LONG", "regime_flip_bullish"
        if zone_role == "support":
            return "SHORT", "regime_flip_bearish"
        return DIRECTION_NONE, "ambiguous_zone_role"
    return DIRECTION_NONE, "unclear"


@dataclass
class ActiveWatch:
    zone_key: str
    zone_name: str
    zone: EmaZone
    watch_started_ms: int
    setup_id: str
    approach: str
    regime_at_watch: str
    zone_role_at_watch: str = ""
    flat_at_watch: bool = False
    flat_diag_at_watch: dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodeState:
    episode_id: str
    sequence_id: str
    setup_id: str
    parent_episode_id: str
    zone_key: str
    zone_name: str
    zone: EmaZone
    zone_role: str
    approach: str
    regime: str
    watch_started_ms: int
    zone_role_at_watch: str = ""
    zone_role_at_touch: str = ""
    zone_role_at_decision: str = ""
    post_break_role: str = ""
    flat_at_watch: bool = False
    flat_at_touch: bool = False
    flat_at_decision: bool = False
    flat_reason: str = ""
    flat_diag: dict[str, Any] = field(default_factory=dict)
    touch_ms: int | None = None
    attack_started_ms: int | None = None
    micro_obs_started_ms: int | None = None
    closed: bool = False
    left_zone: bool = False
    state_transitions: list[dict[str, Any]] = field(default_factory=list)
    current_state: str = "watch_zone"


@dataclass
class DetectorBuffers:
    watches: dict[str, ActiveWatch] = field(default_factory=dict)
    active: dict[str, EpisodeState] = field(default_factory=dict)
    cooldown_until: dict[str, int] = field(default_factory=dict)
    last_outside: dict[str, bool] = field(default_factory=dict)
    ep_counter: int = 0
    seq_counter: int = 0


def _append_state(
    ep: EpisodeState,
    *,
    new_state: str,
    observed_at_ms: int,
    evidence_until_ms: int,
    reason_codes: list[str],
) -> None:
    decision_ms = max(observed_at_ms, evidence_until_ms)
    ep.state_transitions.append(
        {
            "episode_id": ep.episode_id,
            "observed_at": _iso_ms(observed_at_ms),
            "decision_at": _iso_ms(decision_ms),
            "evidence_available_until": _iso_ms(evidence_until_ms),
            "previous_state": ep.current_state,
            "new_state": new_state,
            "reason_codes": "|".join(reason_codes),
        }
    )
    ep.current_state = new_state


def _zones_at_sample(
    sample: AnalysisSample,
    bars: pd.DataFrame,
    tick: float,
    ema200: float | None = None,
) -> dict[str, EmaZone | None]:
    atr = sample.atr
    if atr is None or atr <= 0:
        return {"EMA20": None, "EMA59": None, "EMA200": None}
    z20 = _make_zone("EMA20", sample.ema20, atr, tick) if sample.ema20 is not None else None
    z59 = _make_zone("EMA59", sample.ema59, atr, tick) if sample.ema59 is not None else None
    z200 = _make_zone("EMA200", ema200, atr, tick) if ema200 is not None else None
    return {"EMA20": z20, "EMA59": z59, "EMA200": z200}


def _primary_zone_key(zones: dict[str, EmaZone | None], mid: float) -> tuple[str, EmaZone] | None:
    present = [(n, z) for n, z in zones.items() if z is not None]
    if not present:
        return None
    stacked = stacked_zone_label({n: z for n, z in present})
    if stacked:
        # merge overlapping into one synthetic zone spanning min low / max high
        names = stacked.split(":", 1)[-1].split("+")
        zs = [zones[n] for n in names if zones.get(n)]
        if not zs:
            return None
        low = min(z.low for z in zs)  # type: ignore[union-attr]
        high = max(z.high for z in zs)  # type: ignore[union-attr]
        center = (low + high) / 2.0
        atr = zs[0].atr  # type: ignore[union-attr]
        hw = (high - low) / 2.0
        syn = EmaZone(name="STACKED", center=center, low=low, high=high, half_width=hw, atr=atr)
        return stacked, syn
    # nearest by distance
    best_n, best_z = min(present, key=lambda item: _dist_outside(item[1], mid))
    return best_n, best_z


def _pick_wall(sample: AnalysisSample, zone_name: str, role: str):
    if "STACKED" in zone_name or zone_name.startswith("STACKED"):
        # prefer EMA20 walls then EMA59
        if role == "resistance":
            return sample.ask_in_ema20 or sample.ask_in_ema59
        return sample.bid_in_ema20 or sample.bid_in_ema59
    if zone_name == "EMA20":
        return sample.ask_in_ema20 if role == "resistance" else sample.bid_in_ema20
    if zone_name == "EMA59":
        return sample.ask_in_ema59 if role == "resistance" else sample.bid_in_ema59
    # EMA200: use nearest EMA59 wall proxy (no dedicated EMA200 wall field in AnalysisSample)
    return sample.ask_in_ema59 if role == "resistance" else sample.bid_in_ema59


def classify_from_touch(
    *,
    samples: list[AnalysisSample],
    touch_ms: int,
    zone: EmaZone,
    zone_name: str,
    zone_role: str,
    trades: pd.DataFrame,
    confirm_max_s: int = MAX_CONFIRMATION_DURATION_S,
) -> dict[str, Any]:
    """Causal forward classification from touch — no full future window lookahead."""
    end_ms = touch_ms + confirm_max_s * 1000
    window = [s for s in samples if touch_ms - 60_000 <= s.ts_ms <= end_ms]
    after = [s for s in samples if touch_ms <= s.ts_ms <= end_ms]
    if not after:
        return {
            "primary_class": "UNDETERMINED",
            "mechanism": "UNDETERMINED",
            "timeline": {},
            "wall_before": None,
            "wall_at": None,
            "wall_after": None,
            "consumed": None,
            "major_wall": False,
            "liquidity_pull": False,
        }

    wall_side = "ASK" if zone_role == "resistance" else "BID"
    pre = [s for s in window if touch_ms - 60_000 <= s.ts_ms < touch_ms]
    at = [s for s in after if s.ts_ms <= touch_ms + 5_000]
    post60 = [s for s in after if touch_ms + 55_000 <= s.ts_ms <= touch_ms + 65_000]

    wall_before = wall_at = wall_after = None
    for s in reversed(pre):
        w = _pick_wall(s, zone_name, zone_role)
        if w:
            wall_before = w
            if is_majorish(w):
                break
    for s in at:
        w = _pick_wall(s, zone_name, zone_role)
        if w:
            wall_at = w
            if is_majorish(w):
                break
    for s in post60:
        w = _pick_wall(s, zone_name, zone_role)
        if w:
            wall_after = w
            break

    buy_n = sell_n = 0.0
    trades_present = False
    if trades is not None and not trades.empty:
        sub = trades[(trades["ts_ms"] >= touch_ms) & (trades["ts_ms"] < touch_ms + 60_000)]
        if len(sub) > 0:
            trades_present = True
            st = summarize_trades(sub)
            buy_n, sell_n = st["buy_notional"], st["sell_notional"]

    wn_before = wall_before.notional if wall_before else None
    wn_after = wall_after.notional if wall_after else None
    present_after = wall_after is not None and (wn_after or 0) > 0.2 * (wn_before or 1)
    consumed = None
    if wn_before is not None:
        aggressive = buy_n if wall_side == "ASK" else sell_n
        if wn_after is not None:
            consumed = min(max(0.0, wn_before - wn_after), aggressive if trades_present else 0.0)
        elif trades_present:
            consumed = min(aggressive, wn_before)

    price_held_beyond = False
    post = [s for s in after if touch_ms + 30_000 <= s.ts_ms <= end_ms]
    if post:
        if zone_role == "resistance":
            price_held_beyond = sum(1 for s in post if s.mid > zone.high) / len(post) > 0.7
        else:
            price_held_beyond = sum(1 for s in post if s.mid < zone.low) / len(post) > 0.7

    wall_moved = False
    if wall_before and wall_after and abs(wall_before.price - wall_after.price) >= zone.half_width * 0.05:
        wall_moved = True

    mechanism = "UNDETERMINED"
    if trades_present or wall_before is not None:
        mechanism = classify_flow_mechanism(
            attack_side="BUY" if wall_side == "ASK" else "SELL",
            wall_side=wall_side,
            buy_n=buy_n,
            sell_n=sell_n,
            wall_notional_before=wn_before,
            wall_notional_after=wn_after,
            wall_present_after=present_after,
            price_held_beyond=price_held_beyond,
            consumed_estimate=consumed,
        )
    if mechanism == "LIQUIDITY_PULL":
        mechanism = "ASK_LIQUIDITY_PULL" if wall_side == "ASK" else "BID_LIQUIDITY_PULL"

    if (
        mechanism == "UNDETERMINED"
        and wall_before is not None
        and not price_held_beyond
        and after
    ):
        if zone_role == "resistance":
            frac = sum(1 for s in after if s.mid > zone.high) / len(after)
            if frac < 0.25:
                mechanism = "ASK_DEFENSE"
        else:
            frac = sum(1 for s in after if s.mid < zone.low) / len(after)
            if frac < 0.25:
                mechanism = "BID_DEFENSE"

    # Map extended pull names back for classify_window
    mech_for_tl = mechanism
    if mechanism.endswith("LIQUIDITY_PULL"):
        mech_for_tl = "LIQUIDITY_PULL"

    tl = classify_window(
        data_incomplete=False,
        incomplete_reason="",
        samples=window,
        zone=zone,
        zone_role=zone_role,
        contact_ts_ms=touch_ms,
        mechanism=mech_for_tl,
        wall_present_before_contact=wall_before is not None,
        wall_present_after_60s=present_after,
        wall_moved=wall_moved,
    )

    return {
        "primary_class": tl.primary_class,
        "mechanism": mechanism,
        "timeline": {
            "zone_touch_at": tl.zone_touch_at,
            "attack_start_at": tl.attack_start_at,
            "wall_defended_at": tl.wall_defended_at,
            "wall_absorbed_at": tl.wall_absorbed_at,
            "wall_pulled_at": _iso_ms(touch_ms) if mech_for_tl == "LIQUIDITY_PULL" else None,
            "price_breakout_at": tl.breakout_at,
            "breakout_confirmed_at": tl.breakout_confirmed_at,
            "reclaim_confirmed_at": tl.reclaim_at,
            "retest_at": tl.retest_at,
            "classification_at": tl.classification_at,
        },
        "wall_before": wall_before,
        "wall_at": wall_at,
        "wall_after": wall_after,
        "consumed": consumed,
        "major_wall": is_majorish(wall_at) or is_majorish(wall_before),
        "liquidity_pull": mech_for_tl == "LIQUIDITY_PULL",
        "buy_n": buy_n,
        "sell_n": sell_n,
        "trades_present": trades_present,
        "wall_moved": wall_moved,
    }


def process_symbol_stream(
    *,
    symbol: str,
    samples: list[AnalysisSample],
    bars: pd.DataFrame,
    trades_loader,  # callable(start_ms, end_ms) -> DataFrame
    oi: pd.DataFrame,
    liq: pd.DataFrame,
    tick: float,
    discovery_start_ms: int,
    discovery_end_ms: int,
    computation_mode: str = COMPUTATION_MODE_EMA_PLUS_MICRO,
) -> dict[str, list[dict[str, Any]]]:
    """Single-pass autonomous detector over genuine samples."""
    computation_mode = normalize_computation_mode(computation_mode)
    ema_only_computation = computation_mode == COMPUTATION_MODE_EMA_ONLY
    buf = DetectorBuffers()
    regime_rows: list[dict[str, Any]] = []
    watch_rows: list[dict[str, Any]] = []
    contact_rows: list[dict[str, Any]] = []
    episode_tl: list[dict[str, Any]] = []
    wall_rows: list[dict[str, Any]] = []
    trade_ev: list[dict[str, Any]] = []
    oi_liq_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    ema_setup_events: list[dict[str, Any]] = []
    state_funnel: list[dict[str, Any]] = []

    # Causal regime cache keyed by closed 5m bar_end ms — O(1) per sample after first
    regime_cache: dict[int, dict[str, Any]] = {}

    def regime_at(ts_ms: int) -> dict[str, Any]:
        bar_end_ms = (ts_ms // 300_000) * 300_000
        if bar_end_ms not in regime_cache:
            asof = datetime.fromtimestamp(bar_end_ms / 1000.0, tz=timezone.utc)
            regime_cache[bar_end_ms] = regime_snapshot(bars, asof)
        return regime_cache[bar_end_ms]

    last_regime_bar_end: str | None = None
    genuine = [
        s
        for s in samples
        if s.genuine
        and not s.carried_forward
        and not s.warmup
        and discovery_start_ms <= s.ts_ms < discovery_end_ms
        and s.bid_levels >= 200
        and s.ask_levels >= 200
    ]

    # regime timeline: one row per closed 5m bar in range
    closed = bars[
        (bars["bar_end"] >= pd.Timestamp(discovery_start_ms, unit="ms", tz="UTC"))
        & (bars["bar_end"] <= pd.Timestamp(discovery_end_ms, unit="ms", tz="UTC"))
    ]
    for _, brow in closed.iterrows():
        asof = pd.Timestamp(brow["bar_end"]).to_pydatetime()
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=timezone.utc)
        reg = regime_at(int(asof.timestamp() * 1000))
        if reg.get("last_bar_end") != last_regime_bar_end:
            last_regime_bar_end = reg.get("last_bar_end")
            regime_rows.append(
                {
                    "symbol": symbol,
                    "asof_utc": reg["asof_utc"],
                    "regime": reg["regime"],
                    "block_flat_compression": reg["block_flat_compression"],
                    "ema9": reg["ema9"],
                    "ema20": reg["ema20"],
                    "ema59": reg["ema59"],
                    "ema200": reg["ema200"],
                    "atr": reg["atr"],
                    "ema200_in_regime_score": False,
                    "ema200_role": reg.get("ema200_role", "sr_clearance_flip_context"),
                    "short_term_inputs": reg.get("short_term_inputs"),
                    "regime_gate_allow_directed_base": reg.get("regime_gate_allow_directed_base"),
                    "confidence": reg["confidence"],
                    "reasons": reg["reasons"],
                    "structure": reg["structure"],
                }
            )

    i = 0
    n = len(genuine)
    while i < n:
        s = genuine[i]
        reg = regime_at(s.ts_ms)
        zones = _zones_at_sample(s, bars, tick, ema200=reg.get("ema200"))
        primary = _primary_zone_key(zones, s.mid)
        if primary is None:
            i += 1
            continue
        zkey, zone = primary
        approach = approach_side(s.mid, zone)
        if s.candle_low is not None and s.candle_high is not None:
            approach_ev = classify_zone_approach_from_candle_ohlc(
                low=float(s.candle_low),
                high=float(s.candle_high),
                close=float(s.mid),
                zone_low=zone.low,
                zone_high=zone.high,
                max_pct=PROXIMITY_WATCH_MAX_PCT,
            )
            touch_px = approach_ev.get("touch_price")
            inside = zone.low <= float(s.mid) <= zone.high
            dist = _dist_outside(zone, s.mid)
        else:
            touch_px = None
            dist = _dist_outside(zone, s.mid)
            inside = dist == 0.0
            approach_ev = classify_zone_approach_event(
                inside_band=inside,
                dist_outside=dist,
                mid=s.mid,
                max_pct=PROXIMITY_WATCH_MAX_PCT,
            )
        # Paket 2C: proximity (≤0.20% of mid) ≠ exact touch (mid inside band / OHLC overlap).
        in_proximity = bool(approach_ev["in_proximity"])
        exact_touch = bool(approach_ev["exact_touch"])
        dist_pct = approach_ev.get("proximity_dist_pct")
        event_touch_price = float(touch_px) if touch_px is not None else float(s.mid)

        # expire watches
        for k in list(buf.watches.keys()):
            if s.ts_ms - buf.watches[k].watch_started_ms > MAX_WATCH_DURATION_S * 1000:
                del buf.watches[k]

        # rearm tracking
        _apply_rearm_tracking(buf, zkey=zkey, zone=zone, inside=inside, dist=dist)

        # Flat-gate at proximity watch_at (Stage A — no direction; not a touch)
        if in_proximity:
            diag_watch = flat_diagnostics(bars, _asof_ms(s.ts_ms), snap=reg)
            if diag_watch["flat"] and zkey not in buf.active and buf.cooldown_until.get(zkey, 0) <= s.ts_ms:
                buf.ep_counter += 1
                eid = f"{symbol}_ep_{buf.ep_counter}"
                setup_id = make_setup_id(symbol=symbol, zone_key=zkey, anchor_ms=s.ts_ms)
                appr_flat = approach if approach != "inside" else (
                    "from_above" if s.mid >= zone.center else "from_below"
                )
                role_flat = zone_role_from_approach(appr_flat)
                dir_fields = stage_a_direction_payload(reason="block_flat_compression")
                flat_fields = flat_block_payload(
                    flat_at_watch=True,
                    flat_at_touch=False,
                    flat_at_decision=False,
                    diag=diag_watch,
                    decisive_stage="watch_at",
                )
                if not ema_only_computation:
                    candidate_rows.append(
                        {
                            "symbol": symbol,
                            "episode_id": eid,
                            "setup_id": setup_id,
                            **ema_setup_layer_fields(
                                setup_id=setup_id,
                                symbol=symbol,
                                zone_key=zkey,
                                touch_at=MISSING,
                                episode_id=eid,
                                zone_watch_started_at=_iso_ms(s.ts_ms),
                            ),
                            "mechanism": "UNDETERMINED",
                            "primary_class": "BLOCK_FLAT",
                            "zone_name": zkey,
                            "zone_role": role_flat,
                            "approach_direction": appr_flat,
                            "zone_role_at_watch": role_flat,
                            "zone_role_at_touch": role_flat,
                            "zone_role_at_decision": role_flat,
                            "regime": reg["regime"],
                            "decision_at": _iso_ms(s.ts_ms),
                            "decision_price": s.mid,
                            "zone_watch_started_at": _iso_ms(s.ts_ms),
                            "zone_event": "proximity_watch",
                            "in_proximity": True,
                            "exact_touch": False,
                            "proximity_dist_pct": dist_pct,
                            "proximity_watch_max_pct": PROXIMITY_WATCH_MAX_PCT,
                            "major_wall_confluence": False,
                            **flat_fields,
                            **dir_fields,
                            "stage_a_allows_microstructure": False,
                            "format_version": FORMAT_VERSION,
                            "label_anchor_at": MISSING,
                            "label_anchor_price": MISSING,
                        }
                    )
                ema_setup_events.append(
                    build_ema_setup_event(
                        setup_id=setup_id,
                        symbol=symbol,
                        zone_key=zkey,
                        zone_event="proximity_watch",
                        touch_at=MISSING,
                        episode_id=eid,
                        zone_watch_started_at=_iso_ms(s.ts_ms),
                        marker_at=_iso_ms(s.ts_ms),
                        marker_price=s.mid,
                        extra={
                            "ema_setup_state": "block_flat_compression",
                            "candidate_state": "block_flat_compression",
                            "zone_role": role_flat,
                            "approach_direction": appr_flat,
                            "regime": reg["regime"],
                            "flat_at_watch": True,
                            **flat_fields,
                        },
                    )
                )
                state_funnel.append({"symbol": symbol, "state": "block_flat_compression", "count": 1})
                buf.cooldown_until[zkey] = s.ts_ms + COOLDOWN_S * 1000
                # drop any pending watch — flat at watch is decisive
                buf.watches.pop(zkey, None)
                i += 1
                continue

        # start proximity watch (approach only — never Stage B / never directional)
        if in_proximity and not exact_touch and zkey not in buf.watches and zkey not in buf.active:
            if buf.cooldown_until.get(zkey, 0) <= s.ts_ms and buf.last_outside.get(zkey, True):
                appr_w = approach if approach != "inside" else (
                    "from_above" if s.mid >= zone.center else "from_below"
                )
                role_w = zone_role_from_approach(appr_w)
                diag_w = flat_diagnostics(bars, _asof_ms(s.ts_ms), snap=reg)
                setup_id = make_setup_id(symbol=symbol, zone_key=zkey, anchor_ms=s.ts_ms)
                buf.watches[zkey] = ActiveWatch(
                    zone_key=zkey,
                    zone_name=zone.name,
                    zone=zone,
                    watch_started_ms=s.ts_ms,
                    setup_id=setup_id,
                    approach=appr_w,
                    regime_at_watch=reg["regime"],
                    zone_role_at_watch=role_w,
                    flat_at_watch=bool(diag_w["flat"]),
                    flat_diag_at_watch=diag_w,
                )
                watch_row = {
                        "symbol": symbol,
                        "setup_id": setup_id,
                        "zone_key": zkey,
                        "zone_watch_started_at": _iso_ms(s.ts_ms),
                        "zone_event": "proximity_watch",
                        "in_proximity": True,
                        "exact_touch": False,
                        "proximity_dist_pct": dist_pct,
                        "proximity_watch_max_pct": PROXIMITY_WATCH_MAX_PCT,
                        "approach_direction": appr_w,
                        "zone_role_at_watch": role_w,
                        "zone_center": zone.center,
                        "zone_low": zone.low,
                        "zone_high": zone.high,
                        "mid": s.mid,
                        "dist": dist,
                        "regime": reg["regime"],
                        "flat_at_watch": bool(diag_w["flat"]),
                        **_flat_metric_fields(diag_w),
                        # Proximity alone must never free Stage B.
                        "stage_a_allows_microstructure": False,
                        "candidate_direction": "NONE",
                        "emit_directional_marker": False,
                    }
                watch_rows.append(watch_row)
                ema_setup_events.append(
                    build_ema_setup_event(
                        setup_id=setup_id,
                        symbol=symbol,
                        zone_key=zkey,
                        zone_event="proximity_watch",
                        touch_at=MISSING,
                        zone_watch_started_at=_iso_ms(s.ts_ms),
                        marker_at=_iso_ms(s.ts_ms),
                        marker_price=s.mid,
                        extra={
                            k: v
                            for k, v in watch_row.items()
                            if k
                            not in {
                                "symbol",
                                "setup_id",
                                "zone_key",
                                "zone_event",
                                "candidate_direction",
                                "emit_directional_marker",
                            }
                        },
                    )
                )

        # exact touch (mid inside band) — only then may Stage B begin
        if exact_touch and zkey not in buf.active:
            if buf.cooldown_until.get(zkey, 0) > s.ts_ms:
                i += 1
                continue
            if not buf.last_outside.get(zkey, True) and zkey not in buf.watches:
                # still inside from previous episode without leave — skip
                i += 1
                continue
            watch = buf.watches.pop(zkey, None)
            buf.ep_counter += 1
            buf.seq_counter += 1
            eid = f"{symbol}_ep_{buf.ep_counter}"
            sid = f"{symbol}_seq_{buf.seq_counter}"
            touch_iso = _iso_ms(s.ts_ms)
            setup_id = (
                watch.setup_id
                if watch is not None
                else make_setup_id(symbol=symbol, zone_key=zkey, anchor_ms=s.ts_ms)
            )
            approach_watch = watch.approach if watch else (
                approach if approach != "inside" else (
                    "from_above" if s.mid >= zone.center else "from_below"
                )
            )
            roles = freeze_role_fields(approach_at_watch=approach_watch)
            role = roles["zone_role_at_watch"]
            flat_at_watch = bool(watch.flat_at_watch) if watch else False
            diag_at_watch = dict(watch.flat_diag_at_watch) if watch and watch.flat_diag_at_watch else {}
            diag_touch = flat_diagnostics(bars, _asof_ms(s.ts_ms), snap=reg)
            flat_at_touch = bool(diag_touch["flat"])
            ep = EpisodeState(
                episode_id=eid,
                sequence_id=sid,
                setup_id=setup_id,
                parent_episode_id="",
                zone_key=zkey,
                zone_name=zone.name,
                zone=zone,
                zone_role=role,
                approach=approach_watch,
                regime=reg["regime"],
                watch_started_ms=watch.watch_started_ms if watch else s.ts_ms,
                zone_role_at_watch=roles["zone_role_at_watch"],
                zone_role_at_touch=roles["zone_role_at_touch"],
                zone_role_at_decision=roles["zone_role_at_decision"],
                flat_at_watch=flat_at_watch,
                flat_at_touch=flat_at_touch,
                flat_reason=str(diag_touch.get("flat_reason") or diag_at_watch.get("flat_reason") or ""),
                flat_diag=diag_touch,
                touch_ms=s.ts_ms,
                attack_started_ms=s.ts_ms,
                micro_obs_started_ms=s.ts_ms,
            )
            _append_state(
                ep,
                new_state="watch_zone",
                observed_at_ms=ep.watch_started_ms,
                evidence_until_ms=ep.watch_started_ms,
                reason_codes=["ZONE_WATCH"],
            )
            contact_rows.append(
                {
                    "symbol": symbol,
                    "episode_id": eid,
                    "setup_id": setup_id,
                    "zone_key": zkey,
                    "zone_touch_at": touch_iso,
                    "touch_at": touch_iso,
                    "zone_watch_started_at": _iso_ms(ep.watch_started_ms),
                    "zone_event": "exact_touch",
                    "in_proximity": False,
                    "exact_touch": True,
                    "proximity_dist_pct": 0.0,
                    "proximity_watch_max_pct": PROXIMITY_WATCH_MAX_PCT,
                    "approach_direction": ep.approach,
                    "zone_role": role,
                    "zone_role_at_watch": ep.zone_role_at_watch,
                    "zone_role_at_touch": ep.zone_role_at_touch,
                    "zone_low": zone.low,
                    "zone_high": zone.high,
                    "mid": event_touch_price,
                    "close": s.mid,
                    "candle_low": s.candle_low,
                    "candle_high": s.candle_high,
                    "regime": reg["regime"],
                    "flat_at_watch": flat_at_watch,
                    "flat_at_touch": flat_at_touch,
                    **_flat_metric_fields(diag_touch),
                }
            )
            ema_setup_events.append(
                build_ema_setup_event(
                    setup_id=setup_id,
                    symbol=symbol,
                    zone_key=zkey,
                    zone_event="exact_touch",
                    touch_at=touch_iso,
                    episode_id=eid,
                    zone_watch_started_at=_iso_ms(ep.watch_started_ms),
                    marker_at=touch_iso,
                    marker_price=event_touch_price,
                    extra={
                        "zone_role": role,
                        "approach_direction": ep.approach,
                        "zone_role_at_watch": ep.zone_role_at_watch,
                        "zone_role_at_touch": ep.zone_role_at_touch,
                        "regime": reg["regime"],
                        "flat_at_watch": flat_at_watch,
                        "flat_at_touch": flat_at_touch,
                        "touch_price_basis": approach_ev.get("touch_price_basis") or MISSING,
                        "candle_close": s.mid,
                        "stage_a_allows_microstructure": not flat_at_touch,
                        "ema_setup_state": (
                            "block_flat_compression" if flat_at_touch else "microstructure_eligible"
                        ),
                    },
                )
            )

            if ema_only_computation:
                if flat_at_touch:
                    _append_state(
                        ep,
                        new_state="block_flat_compression",
                        observed_at_ms=s.ts_ms,
                        evidence_until_ms=s.ts_ms,
                        reason_codes=["FLAT_COMPRESSION", "ZONE_TOUCH"],
                    )
                else:
                    _append_state(
                        ep,
                        new_state="exact_touch",
                        observed_at_ms=s.ts_ms,
                        evidence_until_ms=s.ts_ms,
                        reason_codes=["ZONE_TOUCH", "COMPUTATION_EMA_ONLY"],
                    )
                ep.closed = True
                buf.cooldown_until[zkey] = s.ts_ms + COOLDOWN_S * 1000
                buf.last_outside[zkey] = False
                for tr in ep.state_transitions:
                    episode_tl.append({**tr, "symbol": symbol, "sequence_id": sid})
                    state_funnel.append({"symbol": symbol, "state": tr["new_state"], "count": 1})
                i += 1
                continue

            # Flat-gate at touch_at — decisive Stage-A block (no Stage B)
            if flat_at_touch:
                _append_state(
                    ep,
                    new_state="block_flat_compression",
                    observed_at_ms=s.ts_ms,
                    evidence_until_ms=s.ts_ms,
                    reason_codes=["FLAT_COMPRESSION", "ZONE_TOUCH"],
                )
                ep.closed = True
                buf.cooldown_until[zkey] = s.ts_ms + COOLDOWN_S * 1000
                buf.last_outside[zkey] = False
                dir_fields = stage_a_direction_payload(reason="block_flat_compression")
                flat_fields = flat_block_payload(
                    flat_at_watch=flat_at_watch,
                    flat_at_touch=True,
                    flat_at_decision=False,
                    diag=diag_touch,
                    decisive_stage="touch_at",
                )
                for tr in ep.state_transitions:
                    episode_tl.append({**tr, "symbol": symbol, "sequence_id": sid})
                    state_funnel.append({"symbol": symbol, "state": tr["new_state"], "count": 1})
                candidate_rows.append(
                    {
                        "symbol": symbol,
                        "episode_id": eid,
                        "sequence_id": sid,
                        "parent_episode_id": "",
                        "setup_id": setup_id,
                        **microstructure_layer_fields(
                            setup_id=setup_id,
                            symbol=symbol,
                            zone_key=zkey,
                            touch_at=touch_iso,
                            episode_id=eid,
                        ),
                        "reaction_state": "block_flat_compression",
                        "mechanism": "UNDETERMINED",
                        "primary_class": "BLOCK_FLAT",
                        "zone_name": zkey,
                        "zone_role": role,
                        "approach_direction": ep.approach,
                        "zone_role_at_watch": ep.zone_role_at_watch,
                        "zone_role_at_touch": ep.zone_role_at_touch,
                        "zone_role_at_decision": ep.zone_role_at_decision,
                        "regime": reg["regime"],
                        "zone_watch_started_at": _iso_ms(ep.watch_started_ms),
                        "zone_touch_at": _iso_ms(s.ts_ms),
                        "zone_event": "exact_touch",
                        "in_proximity": False,
                        "exact_touch": True,
                        "proximity_dist_pct": 0.0,
                        "proximity_watch_max_pct": PROXIMITY_WATCH_MAX_PCT,
                        "decision_at": _iso_ms(s.ts_ms),
                        "episode_closed_at": _iso_ms(s.ts_ms),
                        "decision_price": s.mid,
                        "major_wall_confluence": False,
                        **flat_fields,
                        **dir_fields,
                        "stage_a_allows_microstructure": False,
                        "format_version": FORMAT_VERSION,
                        "label_anchor_at": MISSING,
                        "label_anchor_price": MISSING,
                    }
                )
                i += 1
                continue

            _append_state(
                ep,
                new_state="wait_microstructure_confirmation",
                observed_at_ms=s.ts_ms,
                evidence_until_ms=s.ts_ms,
                reason_codes=["ZONE_TOUCH"],
            )
            buf.active[zkey] = ep
            buf.last_outside[zkey] = False

            # Load trades only for confirmation window (memory)
            t0 = s.ts_ms - 60_000
            t1 = s.ts_ms + MAX_CONFIRMATION_DURATION_S * 1000 + 5_000
            trades = trades_loader(t0, t1)
            # Need forward samples — gather until confirm end
            confirm_end = s.ts_ms + MAX_CONFIRMATION_DURATION_S * 1000
            # find index range in genuine
            j = i
            while j < n and genuine[j].ts_ms <= confirm_end:
                j += 1
            # also need a bit of pre-touch context from samples list
            local = [x for x in samples if s.ts_ms - 90_000 <= x.ts_ms <= confirm_end]
            cls = classify_from_touch(
                samples=local,
                touch_ms=s.ts_ms,
                zone=zone,
                zone_name=zkey,
                zone_role=role,
                trades=trades,
            )

            tl = cls["timeline"]
            class_at = tl.get("classification_at")
            possible_flip = False
            full_flip = False
            post_break = ""
            flat_at_decision = False
            diag_decision: dict[str, Any] = {}
            clearance_analysis: dict[str, Any] = {}
            reaction_state = MISSING
            allow_directed = True
            regime_gate: dict[str, Any] = {
                "allow_directed": True,
                "allow_stage_b": True,
                "reason_codes": [],
            }
            if class_at in (None, MISSING) or not class_at:
                # timeout path
                timeout_ms = s.ts_ms + TIMEOUT_S * 1000
                # clamp to discovery end
                timeout_ms = min(timeout_ms, discovery_end_ms - 1)
                final_state = "no_trade"
                reasons = ["TIMEOUT_NO_CONFIRMATION"]
                decision_ms = timeout_ms
                mech = cls["mechanism"]
                primary_class = "TIMEOUT"
                reaction_state = final_state
                # Still record regime / flat diagnostics at timeout.
                reg_dec = regime_at(decision_ms)
                diag_decision = flat_diagnostics(bars, _asof_ms(decision_ms), snap=reg_dec)
                flat_at_decision = bool(diag_decision["flat"])
                clearance_analysis = analyze_directional_clearance(
                    current_zone=zone,
                    current_zone_key=zkey,
                    zones=zones,
                    expected_move="NONE",
                    mid=s.mid,
                )
                regime_gate = evaluate_regime_gate(
                    regime=str(reg_dec.get("regime") or reg["regime"]),
                    block_flat_compression=flat_at_decision,
                    ema20_slope_3_atr=reg_dec.get("ema20_slope_3_atr"),
                    ema_spread_9_59_atr=reg_dec.get("ema_spread_9_59_atr"),
                    structure=reg_dec.get("structure"),
                    zone_name=zkey,
                    touched=True,
                    clearance_wait=bool(clearance_analysis.get("wait_next_zone")),
                )
                if flat_at_decision:
                    final_state = "block_flat_compression"
                    reasons = ["FLAT_COMPRESSION", "TIMEOUT_NO_CONFIRMATION"]
            else:
                # parse classification time
                decision_ms = int(datetime.fromisoformat(class_at.replace("Z", "+00:00")).timestamp() * 1000)
                mech = cls["mechanism"]
                primary_class = cls["primary_class"]
                possible_flip = False
                full_flip = False

                # Stage A re-check at decision_at: flat / compression + regime gate
                reg_dec = regime_at(decision_ms)
                diag_decision = flat_diagnostics(bars, _asof_ms(decision_ms), snap=reg_dec)
                flat_at_decision = bool(diag_decision["flat"])
                block_flat_decision = flat_at_decision

                # EMA200 regime-flip clocks (Stage A situation + Stage B confirmation)
                if involves_ema200(zkey):
                    z200 = zones.get("EMA200")
                    flip = detect_ema200_flip_timestamps(
                        bars=bars,
                        samples=local,
                        zone200_low=z200.low if z200 else None,
                        zone200_high=z200.high if z200 else None,
                        role=role,
                        mechanism=(
                            "LIQUIDITY_PULL"
                            if "LIQUIDITY_PULL" in str(mech)
                            else mech
                        ),
                        timeline=tl,
                        contact_ts_ms=s.ts_ms,
                    )
                    possible_flip = bool(flip.get("possible_regime_flip"))
                    full_flip = bool(flip.get("full_regime_flip_confirmed"))

                prelim_state, _prelim_reasons = map_primary_to_candidate(
                    data_incomplete=False,
                    block_flat=block_flat_decision,
                    wait_next_zone=False,
                    primary_class=primary_class,
                    mechanism=(
                        "LIQUIDITY_PULL"
                        if "LIQUIDITY_PULL" in str(mech)
                        else mech
                    ),
                    possible_regime_flip=possible_flip,
                    full_regime_flip=full_flip,
                    liquidity_pull_tagged="LIQUIDITY_PULL" in str(mech),
                )
                move_dir = expected_move_direction(
                    candidate_state=prelim_state,
                    zone_role=role,
                )
                clearance_analysis = analyze_directional_clearance(
                    current_zone=zone,
                    current_zone_key=zkey,
                    zones=zones,
                    expected_move=move_dir,
                    mid=s.mid,
                    samples=local,
                    decision_ms=decision_ms,
                    candidate_state=prelim_state,
                )
                wait_next = bool(clearance_analysis.get("wait_next_zone"))

                regime_gate = evaluate_regime_gate(
                    regime=str(reg_dec.get("regime") or reg["regime"]),
                    block_flat_compression=block_flat_decision,
                    ema20_slope_3_atr=reg_dec.get("ema20_slope_3_atr"),
                    ema_spread_9_59_atr=reg_dec.get("ema_spread_9_59_atr"),
                    structure=reg_dec.get("structure"),
                    zone_name=zkey,
                    touched=True,
                    clearance_wait=wait_next,
                )

                reaction_state, reasons = map_primary_to_candidate(
                    data_incomplete=False,
                    block_flat=block_flat_decision,
                    wait_next_zone=False,
                    primary_class=primary_class,
                    mechanism=(
                        "LIQUIDITY_PULL"
                        if "LIQUIDITY_PULL" in str(mech)
                        else mech
                    ),
                    possible_regime_flip=possible_flip,
                    full_regime_flip=full_flip,
                    liquidity_pull_tagged="LIQUIDITY_PULL" in str(mech),
                )
                reaction_state, reasons, clearance_analysis = enrich_clearance_for_emit(
                    reaction_state=reaction_state,
                    reasons=reasons,
                    clearance=clearance_analysis,
                )
                if block_flat_decision and "FLAT_COMPRESSION" not in reasons:
                    reasons = ["FLAT_COMPRESSION"] + list(reasons)

                final_state, reasons, allow_directed = apply_regime_gate_to_candidate(
                    final_state=reaction_state,
                    reasons=reasons,
                    gate=regime_gate,
                )

                if final_state == "breakout_confirmed":
                    pb = post_break_role_after_confirmed_breakout(role)
                    if pb:
                        post_break = pb
                        ep.post_break_role = pb

            ep.flat_at_decision = flat_at_decision
            if diag_decision:
                ep.flat_reason = str(diag_decision.get("flat_reason") or ep.flat_reason)
                ep.flat_diag = diag_decision
            ep.zone_role_at_decision = role
            episode_state = reaction_state if reaction_state not in (MISSING, "") else final_state
            _append_state(
                ep,
                new_state=episode_state,
                observed_at_ms=decision_ms,
                evidence_until_ms=decision_ms,
                reason_codes=reasons,
            )
            ep.closed = True
            episode_closed_ms = decision_ms
            buf.cooldown_until[zkey] = episode_closed_ms + COOLDOWN_S * 1000
            del buf.active[zkey]

            # label anchor: first genuine sample strictly after decision
            label_at = MISSING
            label_px = MISSING
            for x in genuine:
                if x.ts_ms > decision_ms:
                    label_at = _iso_ms(x.ts_ms)
                    label_px = x.mid
                    break

            # Direction from preserved Stage-B reaction; marker gated by clearance + regime.
            raw_direction, dir_reason = candidate_direction(
                str(reaction_state if reaction_state not in (MISSING, "") else final_state),
                role,
            )
            dir_fields = attach_direction_fields(
                candidate_state=str(
                    reaction_state if reaction_state not in (MISSING, "") else final_state
                ),
                zone_role=role,
                raw_direction=raw_direction,
                direction_reason=dir_reason,
                block_directed_marker=bool(clearance_analysis.get("block_directed_marker")),
                allow_directed=allow_directed,
            )

            # wall evidence
            wb, wa, waf = cls["wall_before"], cls["wall_at"], cls["wall_after"]
            wall_rows.append(
                {
                    "symbol": symbol,
                    "episode_id": eid,
                    "mechanism": mech,
                    "liquidity_pull_not_absorption": cls["liquidity_pull"],
                    "major_wall_confluence": cls["major_wall"],
                    "wall_price_before": wb.price if wb else MISSING,
                    "wall_notional_before": wb.notional if wb else MISSING,
                    "wall_pct_before": wb.causal_percentile if wb else MISSING,
                    "wall_price_at": wa.price if wa else MISSING,
                    "wall_notional_at": wa.notional if wa else MISSING,
                    "wall_notional_after": waf.notional if waf else MISSING,
                    "consumed_estimate": cls["consumed"] if cls["consumed"] is not None else MISSING,
                    "wall_moved": cls["wall_moved"],
                }
            )

            # trade evidence windows
            for w_s in TRADE_WINDOWS_S:
                if trades is None or trades.empty or not cls["trades_present"]:
                    trade_ev.append(
                        {
                            "symbol": symbol,
                            "episode_id": eid,
                            "window_s": w_s,
                            "trades_present": False,
                            "buy_notional": MISSING,
                            "sell_notional": MISSING,
                            "delta": MISSING,
                            "trade_count": MISSING,
                            "status": "NO_TRADES_IN_WINDOW",
                        }
                    )
                else:
                    sub = trades[
                        (trades["ts_ms"] >= s.ts_ms) & (trades["ts_ms"] < s.ts_ms + w_s * 1000)
                    ]
                    if sub.empty:
                        trade_ev.append(
                            {
                                "symbol": symbol,
                                "episode_id": eid,
                                "window_s": w_s,
                                "trades_present": False,
                                "buy_notional": MISSING,
                                "sell_notional": MISSING,
                                "delta": MISSING,
                                "trade_count": MISSING,
                                "status": "EMPTY_WINDOW",
                            }
                        )
                    else:
                        st = summarize_trades(sub)
                        trade_ev.append(
                            {
                                "symbol": symbol,
                                "episode_id": eid,
                                "window_s": w_s,
                                "trades_present": True,
                                "buy_notional": st["buy_notional"],
                                "sell_notional": st["sell_notional"],
                                "delta": st["delta"],
                                "trade_count": st["trade_count"],
                                "status": "OK",
                            }
                        )

            touch_dt = datetime.fromtimestamp(s.ts_ms / 1000.0, tz=timezone.utc)
            oi_row = oi_features(
                oi,
                window_id=eid,
                contact_at=touch_dt,
                price_before=genuine[max(0, i - 40)].mid if i > 0 else s.mid,
                price_after=s.mid,
            )
            liq_row = liquidation_features(
                liq,
                window_id=eid,
                start=touch_dt,
                end=datetime.fromtimestamp(min(confirm_end, discovery_end_ms) / 1000.0, tz=timezone.utc),
                contact_at=touch_dt,
            )
            oi_liq_rows.append({**oi_row, **{k: v for k, v in liq_row.items() if k != "window_id"}, "symbol": symbol, "episode_id": eid})

            for tr in ep.state_transitions:
                episode_tl.append({**tr, "symbol": symbol, "sequence_id": sid})
                state_funnel.append({"symbol": symbol, "state": tr["new_state"], "count": 1})

            flat_diag_out = diag_decision or diag_touch
            flat_fields_out: dict[str, Any] = {
                "flat_at_watch": flat_at_watch,
                "flat_at_touch": flat_at_touch,
                "flat_at_decision": flat_at_decision,
                **_flat_metric_fields(flat_diag_out),
            }
            if final_state == "block_flat_compression":
                dir_fields = stage_a_direction_payload(reason="block_flat_compression")
                flat_fields_out.update(
                    flat_block_payload(
                        flat_at_watch=flat_at_watch,
                        flat_at_touch=flat_at_touch,
                        flat_at_decision=flat_at_decision,
                        diag=flat_diag_out,
                        decisive_stage="decision_at",
                    )
                )
                # flat_block_payload sets candidate_state; keep final_state aligned
                final_state = "block_flat_compression"

            cand_row = {
                "symbol": symbol,
                "episode_id": eid,
                "sequence_id": sid,
                "parent_episode_id": "",
                "setup_id": setup_id,
                **microstructure_layer_fields(
                    setup_id=setup_id,
                    symbol=symbol,
                    zone_key=zkey,
                    touch_at=touch_iso,
                    episode_id=eid,
                ),
                "candidate_state": final_state,
                "reaction_state": reaction_state if reaction_state not in (MISSING, "") else final_state,
                "ema_setup_state": (
                    "block_flat_compression"
                    if final_state == "block_flat_compression"
                    else "microstructure_eligible"
                ),
                "mechanism": mech,
                "primary_class": primary_class,
                "zone_name": zkey,
                "zone_role": role,
                "approach_direction": ep.approach,
                "zone_role_at_watch": ep.zone_role_at_watch,
                "zone_role_at_touch": ep.zone_role_at_touch,
                "zone_role_at_decision": ep.zone_role_at_decision,
                "post_break_role": post_break or MISSING,
                "regime": reg["regime"],
                "zone_watch_started_at": _iso_ms(ep.watch_started_ms),
                "zone_touch_at": _iso_ms(s.ts_ms),
                "zone_event": "exact_touch",
                "in_proximity": False,
                "exact_touch": True,
                "proximity_dist_pct": 0.0,
                "proximity_watch_max_pct": PROXIMITY_WATCH_MAX_PCT,
                "microstructure_observation_started_at": _iso_ms(s.ts_ms),
                "decision_at": _iso_ms(decision_ms),
                "episode_closed_at": _iso_ms(episode_closed_ms),
                "decision_price": s.mid,
                "major_wall_confluence": cls["major_wall"],
                "reason_codes": "|".join(reasons),
                **flat_fields_out,
                **dir_fields,
                "possible_regime_flip": possible_flip,
                "full_regime_flip_confirmed": full_flip,
                "regime_gate_allow_directed": bool(regime_gate.get("allow_directed")),
                "regime_gate_allow_stage_b": bool(regime_gate.get("allow_stage_b")),
                "regime_gate_reasons": "|".join(str(x) for x in (regime_gate.get("reason_codes") or [])),
                "stage_a_allows_microstructure": final_state != "block_flat_compression",
                "format_version": FORMAT_VERSION,
                "label_anchor_at": label_at,
                "label_anchor_price": label_px,
                "timeout_at": _iso_ms(decision_ms) if final_state == "no_trade" and "TIMEOUT" in "|".join(reasons) else MISSING,
                **{f"tl_{k}": (v if v is not None else MISSING) for k, v in tl.items()},
                **clearance_fields_for_emit(clearance_analysis),
            }
            candidate_rows.append(cand_row)
            # advance i toward end of confirm to avoid re-touch spam inside band
            i = max(i + 1, j - 1)
        i += 1

    return {
        "regime_timeline": regime_rows,
        "zone_watch_events": watch_rows,
        "zone_contacts": contact_rows,
        "episode_timeline": episode_tl,
        "wall_evidence": wall_rows,
        "public_trade_evidence": trade_ev,
        "oi_liquidation_evidence": oi_liq_rows,
        "candidate_events": candidate_rows,
        "ema_setup_events": ema_setup_events,
        "microstructure_confirmation_events": [
            r for r in candidate_rows
            if str(r.get("output_layer") or "") == "microstructure_confirmation"
            or str(r.get("confirmation_mode") or "") == "ema_plus_microstructure"
        ],
        "state_funnel_raw": state_funnel,
        "format_version": FORMAT_VERSION,
    }
