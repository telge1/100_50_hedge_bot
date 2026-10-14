"""Full entry-core + floor-guard signal trace on LMV2 SSOT (no trades)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from research_charts.historical_lmv2_frozen_short_paths_v1 import load_frozen_entry_module
from research_charts.historical_lmv2_frozen_short_signal_v1 import (
    _iso_z,
    _utc,
    evaluate_decision,
    load_market_and_bars,
    parse_decision_close,
)
from research_charts.historical_lmv2_ssot_floor_guard_v1 import SsotH4FloorGuard, guard_contract
from research_charts.historical_lmv2.strategy_loop import four_h_closes_from_market

PANE_FROM_DEFAULT = datetime(2026, 2, 10, tzinfo=timezone.utc)


def _decision_close_from_entry_open(entry_open: datetime | str) -> datetime:
    ot = parse_decision_close(entry_open) if isinstance(entry_open, str) else _utc(entry_open)
    return ot + timedelta(minutes=15)


def replay_guard_through(
    bars15: list[dict],
    guard: SsotH4FloorGuard,
    *,
    replay_from: datetime,
    through_moment: datetime,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for bar in bars15:
        moment = _utc(bar["close_time"])
        if moment < replay_from:
            continue
        blocked, reason = guard.advance(bar, moment)
        events.append(
            {
                "decision_close": _iso_z(moment),
                "floor_guard_blocked": blocked,
                "floor_reason": reason,
            }
        )
        if moment >= through_moment:
            break
    return events


def build_full_signal_trace(
    *,
    symbol: str,
    entry_open: str,
    pool_detail: int = 10,
    pane_from: datetime | None = None,
    client=None,
    database: str | None = None,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Entry-core + SSOT floor-guard at one documented baseline entry_open."""
    fe = load_frozen_entry_module()
    pane_from = pane_from or PANE_FROM_DEFAULT
    entry_open_dt = parse_decision_close(entry_open)
    decision_close = _decision_close_from_entry_open(entry_open_dt)

    market, bars15, load_meta = load_market_and_bars(
        symbol,
        pane_from=pane_from,
        pane_to=decision_close + timedelta(minutes=15),
        client=client,
    )
    bar = next((b for b in bars15 if _utc(b["open_time"]) == entry_open_dt), None)
    if bar is None:
        return {
            "verdict": "FAIL",
            "reason": "MISSING_15M_BAR",
            "entry_open": entry_open,
            "decision_close": _iso_z(decision_close),
        }

    guard = SsotH4FloorGuard(
        symbol,
        pane_from,
        decision_close + timedelta(days=1),
        pool_detail_4h=10,
        four_h_bar_closes=four_h_closes_from_market(market),
        client=client,
        database=database,
    )
    guard_events = replay_guard_through(
        bars15,
        guard,
        replay_from=pane_from,
        through_moment=_utc(bar["close_time"]),
    )
    last_guard = guard_events[-1] if guard_events else {"floor_guard_blocked": False, "floor_reason": None}

    core = evaluate_decision(
        symbol=symbol,
        bar=bar,
        market=market,
        pool_detail=pool_detail,
        client=client,
        database=database,
        seen_pool_ids=set(),
    )
    core_dict = core.to_dict()
    core_signal = core_dict.get("signal") is True

    pool_bounds: dict[str, Any] = {}
    if core_dict.get("entry_pool_id"):
        from research_charts.historical_lmv2_backtester_input_bridge_v1 import (
            load_historical_lmv2_backtester_input_at_close,
        )

        br = load_historical_lmv2_backtester_input_at_close(
            decision_close,
            symbol=symbol,
            timeframe="15m",
            pool_detail=pool_detail,
            client=client,
            database=database,
        )
        for m in br.active_members:
            pid = str(m.get("pool_id"))
            if pid == core_dict.get("entry_pool_id"):
                pool_bounds["entry_upper"] = {
                    "pool_id": pid,
                    "bottom": m.get("bottom"),
                    "top": m.get("top"),
                    "side": m.get("side"),
                }
            tp_id = (core_dict.get("extra") or {}).get("tp_pool_id")
            if tp_id and pid == tp_id:
                pool_bounds["tp_lower"] = {
                    "pool_id": pid,
                    "bottom": m.get("bottom"),
                    "top": m.get("top"),
                    "side": m.get("side"),
                }
        pool_bounds["pipeline_line_id"] = br.pipeline_line_id
        pool_bounds["config_hash"] = br.config_hash
        pool_bounds["publication_known_at"] = decision_close.isoformat()

    final_signal = core_signal and not last_guard.get("floor_guard_blocked")
    final_block = None
    if not core_signal:
        final_block = core_dict.get("block_reason")
    elif last_guard.get("floor_guard_blocked"):
        final_block = f"FLOOR_GUARD:{last_guard.get('floor_reason')}"

    h4_index = fe.last_closed_index(market["4h"]["bars"], _utc(bar["close_time"]))
    h4_bar = market["4h"]["bars"][h4_index] if h4_index is not None else None

    trace: dict[str, Any] = {
        "symbol": symbol,
        "entry_open": _iso_z(entry_open_dt),
        "decision_close": _iso_z(decision_close),
        "pool_detail_15m": pool_detail,
        "reference_baseline": reference,
        "load_meta": load_meta,
        "candle_15m": core_dict.get("bar"),
        "h4_trend": {
            "down_4h": core_dict.get("down_4h"),
            "h4_close_time": core_dict.get("h4_close_time"),
            "ema9": h4_bar.get("ema9") if h4_bar else None,
            "ema20": h4_bar.get("ema20") if h4_bar else None,
            "ema59": h4_bar.get("ema59") if h4_bar else None,
            "ema200": h4_bar.get("ema200") if h4_bar else None,
        },
        "lmv2_15m": {
            "snapshot_id": core_dict.get("snapshot_id"),
            "bridge_input_state_hash": core_dict.get("bridge_input_state_hash"),
            "active_member_count": core_dict.get("active_member_count"),
        },
        "entry_core": {
            "entry_setup": core_dict.get("entry_setup"),
            "entry_pool_id": core_dict.get("entry_pool_id"),
            "stop_price": core_dict.get("stop_price"),
            "tp_top": core_dict.get("tp_top"),
            "gap_pct": core_dict.get("gap_pct"),
            "next_upper_id": (core_dict.get("extra") or {}).get("next_upper_id"),
            "tp_pool_id": (core_dict.get("extra") or {}).get("tp_pool_id"),
            "signal": core_signal,
            "block_reason": core_dict.get("block_reason"),
        },
        "floor_guard": {
            "blocked_at_decision": last_guard.get("floor_guard_blocked"),
            "reason": last_guard.get("floor_reason"),
            "guard_contract": guard_contract(),
            "replay_events_count": len(guard_events),
        },
        "final": {
            "signal_with_guard": final_signal,
            "block_reason": final_block,
        },
        "pool_id_trace": {
            "reference_pool_id": (reference or {}).get("pool_id"),
            "core_entry_pool_id": core_dict.get("entry_pool_id"),
            "id_format_note": "baseline list may use lld: prefix; CH SSOT uses lldv2: — compare numeric suffix",
        },
        "pool_bounds_and_publication": pool_bounds,
        "seen_pools": {
            "seen_pool_would_skip": core_dict.get("seen_pool_would_skip"),
            "note": "trace uses empty seen_pool_ids for single-bar proof",
        },
        "entry_core_full": core_dict,
    }
    return trace


def chart_pool_parity(trace: dict[str, Any], *, pool_detail: int, client=None, database: str | None = None) -> dict[str, Any]:
    """API-level pool membership check (not browser pixels)."""
    from research_charts.historical_lmv2_backtester_input_bridge_v1 import (
        load_historical_lmv2_backtester_input_at_close,
    )

    dc = trace.get("decision_close")
    if not dc:
        return {"verdict": "SKIP", "reason": "no decision_close"}
    bridge = load_historical_lmv2_backtester_input_at_close(
        dc,
        symbol="DOGEUSDT",
        timeframe="15m",
        pool_detail=pool_detail,
        client=client,
        database=database,
    )
    ref_pid = (trace.get("reference_baseline") or {}).get("pool_id")
    core_pid = (trace.get("entry_core") or {}).get("entry_pool_id")
    members = {str(m.get("pool_id")) for m in bridge.active_members}
    suffix_match = (
        ref_pid is not None
        and core_pid is not None
        and str(ref_pid).split(":")[-1] == str(core_pid).split(":")[-1]
    )
    ref_active = ref_pid in members if ref_pid else None
    core_active = core_pid in members if core_pid else None
    if core_active and suffix_match:
        verdict = "PASS"
    elif core_active:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"
    return {
        "decision_close": dc,
        "snapshot_id": bridge.snapshot_id,
        "config_hash": bridge.config_hash,
        "pipeline_line_id": bridge.pipeline_line_id,
        "reference_pool_in_active": ref_active,
        "core_pool_in_active": core_active,
        "core_pool_matches_reference_suffix": suffix_match,
        "browser_pixel_check": "NOT_PERFORMED",
        "verdict": verdict,
        "note": "API-level ACTIVE membership only; not browser pixels",
    }
