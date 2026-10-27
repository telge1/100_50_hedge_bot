"""Opt-in isolated replay with D46-compatible TP/SL exits (post-fill same-bar allowed)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D82 = ROOT / "tools/phase_bt_d82_tp_sl_pause_accounting_v1"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
for p in (ISOLATED, D82):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from exit_semantics_audit_v1 import intrabar_exit_decision  # noqa: E402
from isolated_chronological_runner_v1 import (  # noqa: E402
    CHRONO_CONTRACT,
    IsolatedChronoStateV1,
    _next_5m_open,
    _order_id,
    _try_fill,
    run_isolated_chronological_replay,
)
from isolated_chronological_runner_v1 import (  # noqa: E402
    CONTRACT,
    RUNNER_CONTRACT,
)
from pending_lifecycle_v1 import EVENT_CANCEL, review_pending_at_close  # noqa: E402
from signal_engine_v1 import detect_short_signal  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402

from baseline_pending_v1 import make_pending_from_baseline_signal  # noqa: E402
from contracts_v1 import RegimeContextV1  # noqa: E402
from entry_gate_v1 import evaluate_entry_gate  # noqa: E402
from pure_ema_confirmation_v1 import PureEmaConfirmationGateV1  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402

RESEARCH_RUNNER_CONTRACT = "research_execution_runner_v1_d46_exit_parity"


def _gross_short_return(entry: float, exit_price: float) -> float:
    return round((entry - exit_price) / entry * 100.0, 6)


def _try_exit_d46(
    st: IsolatedChronoStateV1,
    *,
    bar: str,
    prices: dict[str, dict[str, float]],
    config: dict[str, Any],
    pause: ShortPauseControllerV1,
) -> bool:
    """D46 per-bar TP/SL/ambiguous; returns True if position closed."""
    pos = st.position
    if pos is None or bar < pos.entry_bar_utc:
        return False
    ohlc = prices.get(bar)
    if not ohlc:
        return False
    policy = str(config["pnl"]["intrabar_ambiguity_policy"])
    reason, ex_p, amb = intrabar_exit_decision(
        ohlc=ohlc,
        sl_price=pos.sl_price,
        tp_price=pos.tp_price,
        policy=policy,
    )
    if not reason:
        return False
    base = {
        "decision_close_utc": bar,
        "position_id": pos.position_id,
        "order_id": pos.order_id,
        "signal_close_utc": pos.signal_close_utc,
        "entry_price": pos.entry_price,
        "exit_price": ex_p,
        "gross_return_pct_short": _gross_short_return(pos.entry_price, ex_p) if ex_p is not None else None,
        "ambiguous_intrabar": amb,
    }
    if reason == "AMBIGUOUS_INTRABAR":
        st.modeled_positions.append({**base, "event": "SIMULATED_EXIT_AMBIGUOUS"})
    elif reason == "SL":
        pause.on_modeled_stop_loss(exit_bar_utc=bar, position_id=pos.position_id)
        st.modeled_positions.append({**base, "event": "SIMULATED_SL", "exit_price": pos.sl_price})
    elif reason == "TP":
        st.modeled_positions.append({**base, "event": "SIMULATED_TP", "exit_price": pos.tp_price})
    else:
        return False
    st.position = None
    return True


def run_research_execution_replay(
    *,
    config: dict[str, Any],
    isolated_bundle: dict[str, Any],
    prices: dict[str, dict[str, float]],
    bars: list[str],
    density_session: Any,
    pause: ShortPauseControllerV1 | None = None,
    ema_gate: PureEmaConfirmationGateV1 | None = None,
) -> dict[str, Any]:
    """Mirror isolated chronology; D46 exits (incl. post-fill same bar)."""
    pause_ctl = pause or ShortPauseControllerV1()
    st = IsolatedChronoStateV1()
    pool_age: dict[str, int] = {}
    isolated_cfg = {"isolated_strategy_v1": isolated_bundle}

    for bar in bars:
        if bar not in prices:
            continue
        try:
            snap = density_session.collect_at_close(bar)
        except KeyError as exc:
            st.coverage_incomplete.append(
                {"decision_close_utc": bar, "reason": "DENSITY_KEY_ERROR", "detail": str(exc)[:200]}
            )
            continue
        except Exception as exc:  # noqa: BLE001
            st.coverage_incomplete.append(
                {"decision_close_utc": bar, "reason": "DENSITY_COLLECT_FAILED", "detail": str(exc)[:200]}
            )
            continue
        candle = prices[bar]
        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1

        if st.position is not None:
            _try_exit_d46(st, bar=bar, prices=prices, config=config, pause=pause_ctl)

        filled_this_bar = False
        if st.pending is not None and st.position is None:
            filled_this_bar = _try_fill(st, bar=bar, prices=prices)
            if filled_this_bar and st.position is not None:
                _try_exit_d46(st, bar=bar, prices=prices, config=config, pause=pause_ctl)

        ema_at_bar = None
        if ema_gate is not None:
            ema_at_bar = ema_gate.at_close(bar)
            if filled_this_bar and ema_at_bar and not ema_at_bar.authorizes_new_pending:
                st.timing_notes.append(
                    {
                        "decision_close_utc": bar,
                        "note": "FILL_ON_BAR_BEFORE_CLOSE_EMA_WOULD_VETO",
                        "ema_decision": ema_at_bar.decision,
                    }
                )
            if st.pending is not None and st.position is None and not ema_at_bar.authorizes_new_pending:
                oid = st.pending.order_id
                st.pending_cancel_audit.append(
                    {
                        "decision_close_utc": bar,
                        "cancel_reason": "EMA_PERMISSION_LOST",
                        "ema_decision": ema_at_bar.decision,
                        "ema_reason_code": ema_at_bar.reason_code,
                        "order_id": oid,
                        "signal_close_utc": st.pending.decision_close_utc,
                    }
                )
                st.pending = None
                st.pending_cancels_ema += 1
                st.pending_lifecycle.append(
                    {
                        "event": "CANCEL",
                        "decision_close_utc": bar,
                        "cancel_reason": "EMA_PERMISSION_LOST",
                        "order_id": oid,
                    }
                )

        if st.pending is not None and st.position is None:
            review = review_pending_at_close(
                config,
                decision_close_utc=bar,
                pending=st.pending,
                candle=candle,
                snapshot=snap,
                pool_age_bars=pool_age,
            )
            for rev in review.events:
                if rev.event_type == EVENT_CANCEL:
                    st.pending_cancel_audit.append(
                        {
                            "decision_close_utc": bar,
                            "cancel_reason": rev.cancel_reason or "D41_CANCEL",
                            "order_id": st.pending.order_id if st.pending else None,
                            "detail": rev.detail,
                        }
                    )
                    st.pending = None
            st.pending = review.pending

        baseline_sig = detect_short_signal(
            config,
            decision_close_utc=bar,
            order_active_from_utc=_next_5m_open(bar),
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
        )
        if baseline_sig is not None:
            st.pool_candidates_seen += 1
            st.baseline_pool_candidates.append(
                {
                    "decision_close_utc": bar,
                    "pool_key": baseline_sig.pool_key,
                    "pool_lower": float(baseline_sig.pool_lower),
                    "pool_upper": float(baseline_sig.pool_upper),
                }
            )
            if ema_gate is not None:
                ema_shadow = ema_gate.at_close(bar)
                st.ema_shadow_at_candidates.append(
                    {
                        "decision_close_utc": bar,
                        "pool_key": baseline_sig.pool_key,
                        "ema_decision": ema_shadow.decision,
                        "ema_reason_code": ema_shadow.reason_code,
                        "ema_authorizes": ema_shadow.authorizes_new_pending,
                        "pause_state": pause_ctl.pause_state,
                    }
                )

        ctx = RegimeContextV1(decision_close_utc=bar, ohlc_to_close=candle, ema_features=None)
        decision = evaluate_entry_gate(
            context=ctx,
            pause=pause_ctl,
            baseline_signal=baseline_sig,
            snapshot=snap,
            ema_gate=ema_gate,
            isolated_strategy_config=isolated_cfg,
        )
        ema_row = decision.ema_confirmation
        st.gate_decisions.append(
            {
                "decision_close_utc": bar,
                "pool_candidate_present": decision.pool_candidate_present,
                "allow_new_short_intent": decision.allow_new_short_intent,
                "reasons": decision.reasons,
                "pause_state": decision.pause_state,
                "ema_decision": ema_row.decision if ema_row else None,
                "ema_reason_code": ema_row.reason_code if ema_row else None,
                "baseline_signal_pool_key": baseline_sig.pool_key if baseline_sig else None,
            }
        )

        if baseline_sig is not None and not decision.allow_new_short_intent:
            st.blocked_candidates.append(
                {
                    "decision_close_utc": bar,
                    "pool_key": baseline_sig.pool_key,
                    "block_reasons": decision.reasons,
                    "ema_decision": ema_row.decision if ema_row else None,
                }
            )

        if decision.allow_new_short_intent and baseline_sig is not None and st.position is None:
            st.authorized_candidates.append(
                {
                    "decision_close_utc": bar,
                    "pool_key": baseline_sig.pool_key,
                    "ema_decision": ema_row.decision if ema_row else None,
                }
            )
            if st.pending is not None:
                st.pending_cancel_audit.append(
                    {
                        "decision_close_utc": bar,
                        "cancel_reason": "REPLACE_NEW_AUTHORIZED_CANDIDATE",
                        "order_id": st.pending.order_id,
                    }
                )
                st.pending = None
            oid = _order_id(bar, str(baseline_sig.pool_key))
            pending = make_pending_from_baseline_signal(
                config=config,
                order_id=oid,
                decision_close_utc=bar,
                baseline_signal=baseline_sig,
                snapshot=snap,
            )
            if pending is not None:
                st.pending = pending
                st.pending_orders_created += 1
                st.pending_lifecycle.append(
                    {
                        "event": "CREATE",
                        "decision_close_utc": bar,
                        "order_id": oid,
                        "planned_entry": pending.planned_entry,
                        "planned_sl": pending.planned_sl,
                        "planned_tp": pending.planned_tp,
                        "pool_key": pending.pool_key,
                    }
                )

    return {
        "contract": RESEARCH_RUNNER_CONTRACT,
        "strategy_contract": CONTRACT,
        "baseline_runner_contract": CHRONO_CONTRACT,
        "runner_contract": RUNNER_CONTRACT,
        "bars_processed": len(st.gate_decisions),
        "pool_candidates_seen": st.pool_candidates_seen,
        "gate_decisions": st.gate_decisions,
        "blocked_candidates": st.blocked_candidates,
        "authorized_candidates": st.authorized_candidates,
        "pending_cancel_audit": st.pending_cancel_audit,
        "pending_orders_created": st.pending_orders_created,
        "pending_cancels_ema": st.pending_cancels_ema,
        "modeled_fills": st.modeled_fills,
        "pause_state_final": pause_ctl.pause_state,
        "pause_events": [e.__dict__ for e in pause_ctl.events],
        "position_open_at_end": st.position is not None,
        "pending_open_at_end": st.pending is not None,
        "baseline_pool_candidates": st.baseline_pool_candidates,
        "ema_shadow_at_candidates": st.ema_shadow_at_candidates,
        "pending_lifecycle": st.pending_lifecycle,
        "modeled_positions": st.modeled_positions,
        "coverage_incomplete": st.coverage_incomplete,
        "timing_notes": st.timing_notes,
    }


def summarize_trades(replay: dict[str, Any]) -> dict[str, Any]:
    modeled = list(replay.get("modeled_positions") or [])
    fills = [m for m in modeled if m.get("event") == "SIMULATED_FILL"]
    exits = [m for m in modeled if m.get("event") != "SIMULATED_FILL"]
    by_type: dict[str, int] = {}
    for e in exits:
        by_type[e.get("event", "?")] = by_type.get(e.get("event", "?"), 0) + 1
    return {
        "fills": len(fills),
        "exit_events": by_type,
        "open_at_end": replay.get("position_open_at_end"),
        "pause_final": replay.get("pause_state_final"),
    }
