"""Causal isolated replay with D41 pending + BT-D65 EMA confirmation (no D46 mutation)."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for p in (
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
    ROOT / "tools/phase_bt_d41_pending_lifecycle_v1",
    ROOT / "tools/phase_bt_d45_execution_contract_v1",
    ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution_contract_v1 import MODEL_1, model_entry_decision  # noqa: E402
from pending_lifecycle_v1 import EVENT_CANCEL, review_pending_at_close  # noqa: E402
from pending_lifecycle_v1 import PendingOrderV1  # noqa: E402
from signal_engine_v1 import detect_short_signal  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402

from baseline_pending_v1 import make_pending_from_baseline_signal  # noqa: E402
from contracts_v1 import CONTRACT, RUNNER_CONTRACT, RegimeContextV1  # noqa: E402
from entry_gate_v1 import evaluate_entry_gate  # noqa: E402
from pure_ema_confirmation_v1 import PureEmaConfirmationGateV1  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402

CHRONO_CONTRACT = "isolated_chronological_runner_v1"


def _next_5m_open(close_utc: str) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(close_utc.replace("Z", "+00:00")) + timedelta(minutes=5)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _order_id(decision_utc: str, pool_key_s: str) -> str:
    raw = f"{CHRONO_CONTRACT}|{decision_utc}|{pool_key_s}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class SimulatedPositionV1:
    position_id: str
    order_id: str
    entry_bar_utc: str
    entry_price: float
    sl_price: float
    tp_price: float
    signal_close_utc: str
    pool_key: str


@dataclass
class IsolatedChronoStateV1:
    pending: PendingOrderV1 | None = None
    position: SimulatedPositionV1 | None = None
    gate_decisions: list[dict[str, Any]] = field(default_factory=list)
    blocked_candidates: list[dict[str, Any]] = field(default_factory=list)
    authorized_candidates: list[dict[str, Any]] = field(default_factory=list)
    pending_cancel_audit: list[dict[str, Any]] = field(default_factory=list)
    pending_orders_created: int = 0
    pending_cancels_ema: int = 0
    modeled_fills: int = 0
    pool_candidates_seen: int = 0


def _try_fill(
    st: IsolatedChronoStateV1,
    *,
    bar: str,
    prices: dict[str, dict[str, float]],
) -> bool:
    pend = st.pending
    if pend is None or st.position is not None:
        return False
    if bar < pend.earliest_eligible_execution_utc:
        return False
    ohlc = prices.get(bar)
    if not ohlc:
        return False
    filled, px, _rule, _note = model_entry_decision(
        MODEL_1,
        bar_utc=bar,
        active_from=pend.earliest_eligible_execution_utc,
        entry=pend.planned_entry,
        ohlc=ohlc,
    )
    if not filled or px is None or pend.planned_tp is None:
        return False
    pid = hashlib.sha256(f"{pend.order_id}|{bar}".encode()).hexdigest()[:16]
    st.position = SimulatedPositionV1(
        position_id=pid,
        order_id=pend.order_id,
        entry_bar_utc=bar,
        entry_price=px,
        sl_price=pend.planned_sl,
        tp_price=float(pend.planned_tp),
        signal_close_utc=pend.decision_close_utc,
        pool_key=pend.pool_key,
    )
    st.pending = None
    st.modeled_fills += 1
    return True


def _try_exit_sl(
    st: IsolatedChronoStateV1,
    *,
    bar: str,
    prices: dict[str, dict[str, float]],
    pause: ShortPauseControllerV1,
) -> None:
    pos = st.position
    if pos is None or bar < pos.entry_bar_utc:
        return
    ohlc = prices.get(bar)
    if not ohlc:
        return
    if float(ohlc["h"]) >= pos.sl_price:
        pause.on_modeled_stop_loss(exit_bar_utc=bar, position_id=pos.position_id)
        st.position = None


def run_isolated_chronological_replay(
    *,
    config: dict[str, Any],
    isolated_bundle: dict[str, Any],
    prices: dict[str, dict[str, float]],
    bars: list[str],
    density_session: Any,
    pause: ShortPauseControllerV1 | None = None,
    ema_gate: PureEmaConfirmationGateV1 | None = None,
) -> dict[str, Any]:
    pause_ctl = pause or ShortPauseControllerV1()
    st = IsolatedChronoStateV1()
    pool_age: dict[str, int] = {}
    isolated_cfg = {"isolated_strategy_v1": isolated_bundle}

    for bar in bars:
        if bar not in prices:
            continue
        try:
            snap = density_session.collect_at_close(bar)
        except KeyError:
            continue
        candle = prices[bar]
        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1

        if st.position is not None:
            _try_exit_sl(st, bar=bar, prices=prices, pause=pause_ctl)

        if st.pending is not None and st.position is None:
            _try_fill(st, bar=bar, prices=prices)

        ema_at_bar = None
        if ema_gate is not None:
            ema_at_bar = ema_gate.at_close(bar)
            if st.pending is not None and st.position is None and not ema_at_bar.authorizes_new_pending:
                st.pending_cancel_audit.append(
                    {
                        "decision_close_utc": bar,
                        "cancel_reason": "EMA_PERMISSION_LOST",
                        "ema_decision": ema_at_bar.decision,
                        "ema_reason_code": ema_at_bar.reason_code,
                        "order_id": st.pending.order_id,
                        "signal_close_utc": st.pending.decision_close_utc,
                    }
                )
                st.pending = None
                st.pending_cancels_ema += 1

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
                "ema_provenance": ema_row.provenance if ema_row else {},
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
                    "ema_reason_code": ema_row.reason_code if ema_row else None,
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

    return {
        "contract": CHRONO_CONTRACT,
        "strategy_contract": CONTRACT,
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
    }
