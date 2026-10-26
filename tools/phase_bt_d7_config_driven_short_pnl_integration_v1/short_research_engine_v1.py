"""Config-driven short research backtester (isolated BT-D7)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bands_adapter_v1 import snapshot_to_intervals
from execution_v1 import ClosedTradeV1, simulate_short_exit
from pool_identity_v1 import pool_still_active_for_pending
from sell_limit_fill_v1 import evaluate_short_sell_limit_fill, short_entry_fill_model
from signal_engine_v1 import ShortSignalV1, detect_short_signal
from signal_snapshot_v1 import build_signal_snapshot

PHASE4D = Path(__file__).resolve().parents[2] / "phase4d_pool_landscape_backtester_v2"
BT_D6 = Path(__file__).resolve().parents[1] / "phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
for p in (str(PHASE4D), str(BT_D6)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_validate_v1 import validate_config_dict  # noqa: E402
from manifest_v1 import build_run_manifest, config_content_hash  # noqa: E402
from pnl_aggregator_v1 import SyntheticTrade, aggregate_pnl  # noqa: E402
from strategy_rules import select_short_tp_depth2, short_sl  # noqa: E402


@dataclass
class BacktestStateV1:
    signals: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    trades: list[ClosedTradeV1] = field(default_factory=list)
    suppressed: list[dict[str, Any]] = field(default_factory=list)


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_5m_open(close_utc: str) -> str:
    return _iso(_parse_utc(close_utc) + timedelta(minutes=5))


def _bars_in_range(
    prices: dict[str, dict], start: str, end: str, *, end_exclusive: bool = False
) -> list[str]:
    if end_exclusive:
        return sorted(t for t in prices if start <= t < end)
    return sorted(t for t in prices if start <= t <= end)


def run_short_research_backtest(
    config: dict[str, Any],
    density_session: Any,
    prices: dict[str, dict[str, float]],
    *,
    require_large_upper_gap: bool = True,
) -> dict[str, Any]:
    validate_config_dict(config)
    tp_mode = config["take_profit"]["selection_mode"]
    if tp_mode != "next_eligible_band":
        raise ValueError("BT-D7 v1 only executes next_eligible_band (experimental 4D amendment)")
    if config["risk"]["max_concurrent_positions"] != 1:
        raise ValueError("max_concurrent_positions must be 1")

    start, end = config["backtest"]["start_utc"], config["backtest"]["end_utc"]
    end_exclusive = bool(config["backtest"].get("end_utc_exclusive"))
    bars = _bars_in_range(prices, start, end, end_exclusive=end_exclusive)
    state = BacktestStateV1()
    pool_age: dict[str, int] = {}
    snapshots_cache: dict[str, dict[str, Any]] = {}
    pending: ShortSignalV1 | None = None
    pending_snapshot_at_signal: dict[str, Any] | None = None
    position_open_until: str | None = None
    trade_seq = 0
    fill_model = short_entry_fill_model(config)
    # Pool-first: block re-signal same pool until origin-set inactive (BT-D21).
    pool_signal_hold: dict[str, dict[str, Any]] = {}

    def _process_entry_fill(fill_t: str, fill_p: float, fill_meta: dict[str, Any]) -> None:
        nonlocal pending, pending_snapshot_at_signal, trade_seq, position_open_until
        if pending is None:
            return
        snap_sig = pending_snapshot_at_signal or snap_for_bar
        if not snap_sig:
            state.orders.append({"status": "NO_SNAPSHOT_AT_FILL", "fill_utc": fill_t, **fill_meta})
            pending = None
            pending_snapshot_at_signal = None
            return
        intervals = snapshot_to_intervals(snap_sig)
        sl = short_sl(pending.pool_upper, float(config["risk"]["sl_above_upper_top_pct"]))
        tp_iv, tp_reason = select_short_tp_depth2(
            fill_p, intervals, float(config["take_profit"]["min_distance_pct"])
        )
        state.orders.append(
            {
                "status": "FILLED",
                "fill_utc": fill_t,
                "fill_price": fill_p,
                "signal_close": pending.decision_close_utc,
                "pending_limit_fill_model": fill_model,
                **fill_meta,
            }
        )
        if not tp_iv:
            state.orders.append({"status": "NO_TP", "reason": tp_reason, "fill_utc": fill_t})
        else:
            tp_price = float(tp_iv["top"])
            ex_t, ex_p, ex_reason, amb, bars_held = simulate_short_exit(
                config,
                entry_utc=fill_t,
                entry_price=fill_p,
                sl_price=sl,
                tp_price=tp_price,
                prices=prices,
                end_utc=end,
            )
            trade_seq += 1
            if ex_p is not None and not amb:
                state.trades.append(
                    ClosedTradeV1(
                        trade_id=f"BT7_{trade_seq:04d}",
                        signal_close_utc=pending.decision_close_utc,
                        fill_utc=fill_t,
                        exit_utc=ex_t or fill_t,
                        entry_price=fill_p,
                        exit_price=ex_p,
                        exit_reason=ex_reason,
                        direction="SHORT",
                        bars_held=bars_held,
                        ambiguous_intrabar=amb,
                        sl_price=sl,
                        tp_price=tp_price,
                    )
                )
                position_open_until = ex_t
            else:
                state.orders.append({"status": ex_reason, "fill_utc": fill_t})
        pending = None
        pending_snapshot_at_signal = None

    for bar in bars:
        if position_open_until and bar < position_open_until:
            if bar.endswith(":00:00Z") or True:
                pass  # suppressed signals only evaluated at closes below

        snap_for_bar: dict[str, Any] | None = None
        if bar in snapshots_cache:
            snap_for_bar = snapshots_cache[bar]
        else:
            try:
                snap_for_bar = density_session.collect_at_close(bar)
                snapshots_cache[bar] = snap_for_bar
            except KeyError:
                snap_for_bar = None
            except Exception:
                if (config.get("density") or {}).get("density_source") == "validated_cache":
                    raise
                snap_for_bar = None

        if pending and config["short_entry"]["pending_limit_enabled"]:
            active = bar >= pending.order_active_from_utc
            if active and position_open_until is None and bar in prices:
                decision = evaluate_short_sell_limit_fill(
                    fill_model=fill_model,
                    limit_price=pending.entry_level,
                    bar_utc=bar,
                    order_active_from_utc=pending.order_active_from_utc,
                    ohlc=prices[bar],
                )
                if decision.filled and decision.fill_price is not None:
                    _process_entry_fill(
                        bar,
                        float(decision.fill_price),
                        {
                            "fill_rule_id": decision.rule_id,
                            "fill_note": decision.note,
                            "ohlc_evidence": decision.ohlc_evidence,
                        },
                    )
                elif decision.ambiguous:
                    state.orders.append(
                        {
                            "status": "AMBIGUOUS_FILL",
                            "bar": bar,
                            "signal_close": pending.decision_close_utc,
                            "fill_rule_id": decision.rule_id,
                        }
                    )
            if pending is not None and snap_for_bar and not pool_still_active_for_pending(
                config,
                pool_lo=pending.pool_lower,
                pool_hi=pending.pool_upper,
                origin_ids=pending.origin_ids,
                snapshot=snap_for_bar,
            ):
                state.orders.append(
                    {
                        "status": "CANCELLED_POOL_INACTIVE",
                        "bar": bar,
                        "signal_close": pending.decision_close_utc,
                        "pool_cancel_identity_mode": config["short_entry"].get("pool_cancel_identity_mode")
                        or "exact_band_v1",
                    }
                )
                pending = None
                pending_snapshot_at_signal = None

        if snap_for_bar is None:
            continue

        for pk, meta in list(pool_signal_hold.items()):
            if not pool_still_active_for_pending(
                config,
                pool_lo=float(meta["pool_lower"]),
                pool_hi=float(meta["pool_upper"]),
                origin_ids=meta.get("origin_ids"),
                snapshot=snap_for_bar,
            ):
                pool_signal_hold.pop(pk, None)
        for b in snap_for_bar.get("upper_bands") or []:
            pk = f"{float(b['lower_price']):.17g}|{float(b['upper_price']):.17g}"
            pool_age[pk] = pool_age.get(pk, 0) + 1

        if position_open_until and bar < position_open_until:
            state.suppressed.append({"bar": bar, "reason": "POSITION_OPEN"})
            continue
        if pending is not None:
            continue

        sig = detect_short_signal(
            config,
            decision_close_utc=bar,
            order_active_from_utc=_next_5m_open(bar),
            candle=prices[bar],
            snapshot=snap_for_bar,
            pool_age_bars=pool_age,
            require_large_upper_gap=require_large_upper_gap,
            blocked_pool_keys=set(pool_signal_hold.keys()),
        )
        if sig:
            ch = config_content_hash(config)
            snap_row = build_signal_snapshot(
                config,
                sig,
                snap_for_bar,
                config_hash=ch,
                density_provenance={
                    "pipeline_line_id": snap_for_bar.get("pipeline_line_id"),
                    "provider_config_hash": snap_for_bar.get("config_hash"),
                    "decision_close_utc": bar,
                },
            )
            state.signals.append(
                {
                    "decision_close_utc": sig.decision_close_utc,
                    "order_active_from_utc": sig.order_active_from_utc,
                    "pool_lower": sig.pool_lower,
                    "pool_upper": sig.pool_upper,
                    "entry_level": sig.entry_level,
                    "gap_min_pct_used": sig.gap_min_pct_used,
                    "experimental_selection": sig.experimental_selection,
                    "tp_mode": "next_eligible_band_EXPERIMENTAL_4D_AMENDMENT",
                    "signal_snapshot": snap_row,
                }
            )
            if config["short_entry"]["pending_limit_enabled"]:
                pending = sig
                pending_snapshot_at_signal = snap_for_bar
            pool_signal_hold[sig.pool_key] = {
                "pool_lower": sig.pool_lower,
                "pool_upper": sig.pool_upper,
                "origin_ids": sig.origin_ids,
            }
            state.orders.append({"status": "PENDING_PLACED", "signal_close": bar})

    synth = [
        SyntheticTrade(
            t.trade_id,
            config["backtest"]["symbols"][0],
            "SHORT",
            t.entry_price,
            t.exit_price,
            t.fill_utc,
            t.exit_utc,
            t.exit_reason,
            t.bars_held,
            ambiguous_intrabar=t.ambiguous_intrabar,
        )
        for t in state.trades
    ]
    pnl = aggregate_pnl(config, synth)

    manifest = build_run_manifest(
        config,
        run_kind="bt_d7_short_research",
        extra={
            "density_path": "ResearchDensityRunV1.collect_at_close",
            "strategy_freeze_status": "FREEZE_CANDIDATE_NOT_FINAL_APPROVED",
            "tp_selection_note": "next_eligible_band experimental — not dense_cluster user rule",
        },
    )
    return {
        "manifest": manifest,
        "config_hash": config_content_hash(config),
        "signals": state.signals,
        "orders": state.orders,
        "trades": [t.__dict__ for t in state.trades],
        "suppressed": state.suppressed,
        "pnl": pnl,
        "snapshots_collected": len(snapshots_cache),
    }
