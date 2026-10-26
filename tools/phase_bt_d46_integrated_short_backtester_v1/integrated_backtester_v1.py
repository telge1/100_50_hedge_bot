"""Integrated causal short backtester (BT-D46) — chronological, no PnL."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for p in (
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d41_pending_lifecycle_v1",
    ROOT / "tools/phase_bt_d45_execution_contract_v1",
    ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from candidate_identity_v1 import candidate_from_signal_band  # noqa: E402
from candidate_state_v1 import DynamicCandidateStateV1  # noqa: E402
from execution_contract_v1 import MODEL_1, model_entry_decision  # noqa: E402
from fill_chronology_v1 import _parse_utc, activation_contract_note  # noqa: E402
from pending_lifecycle_v1 import (  # noqa: E402
    EVENT_CANCEL,
    PendingOrderV1,
    make_pending_from_scanner,
    next_5m_close_after,
    next_5m_open_after_close,
    review_pending_at_close,
)
from pool_first_signal_v1 import detect_short_signal_pool_first  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402

CONTRACT = "bt_d46_integrated_short_backtester_v1"
FILL_LABEL = "SIMULATED_TOUCH_FILL_NOT_EXCHANGE_CONFIRMED"
D41_CONTRACT = "bt_d41_pending_lifecycle_v1"


@dataclass
class SimulatedPositionV1:
    position_id: str
    order_id: str
    entry_bar_utc: str
    entry_price: float
    sl_price: float
    tp_price: float
    fill_rule_id: str
    signal_close_utc: str
    pool_key: str


@dataclass
class IntegratedState:
    machine: str = "FLAT"
    pending: PendingOrderV1 | None = None
    position: SimulatedPositionV1 | None = None
    seq: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    positions_log: list[dict[str, Any]] = field(default_factory=list)
    lineage: list[dict[str, Any]] = field(default_factory=list)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def emit(self, **row: Any) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, **row})


def _order_id(setup_id: str, decision_utc: str) -> str:
    raw = f"{D41_CONTRACT}|{setup_id}|{decision_utc}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _position_id(order_id: str, entry_bar: str) -> str:
    return hashlib.sha256(f"{order_id}|{entry_bar}".encode()).hexdigest()[:16]


def _period_note(bar_close_key: str) -> str:
    close = _parse_utc(bar_close_key)
    open_ = close - timedelta(minutes=5)
    return f"5m_close_key={bar_close_key} period ({open_.strftime('%H:%M')}–{close.strftime('%H:%M')} UTC]"


def _try_exit_on_bar(
    st: IntegratedState,
    *,
    bar: str,
    config: dict[str, Any],
    prices: dict[str, dict[str, float]],
    pilot_end: str,
) -> None:
    pos = st.position
    if pos is None or bar < pos.entry_bar_utc:
        return
    ohlc = prices.get(bar)
    if not ohlc:
        return
    h, l = float(ohlc["h"]), float(ohlc["l"])
    hit_sl = h >= pos.sl_price
    hit_tp = l <= pos.tp_price
    policy = config["pnl"]["intrabar_ambiguity_policy"]
    ex_reason: str | None = None
    ex_p: float | None = None
    amb = False
    if hit_sl and hit_tp:
        if policy == "no_trade_ambiguous":
            ex_reason, ex_p, amb = "AMBIGUOUS_INTRABAR", None, True
        else:
            ex_reason, ex_p, amb = "SL", pos.sl_price, True
    elif hit_sl:
        ex_reason, ex_p = "SL", pos.sl_price
    elif hit_tp:
        ex_reason, ex_p = "TP", pos.tp_price
    else:
        return
    ex_t = bar
    bars_held = len([t for t in prices if pos.entry_bar_utc <= t <= bar])
    if amb or (ex_reason == "AMBIGUOUS_INTRABAR"):
        st.machine = "UNRESOLVED"
        st.bump("unresolved_exit")
        st.ambiguities.append(
            {
                "position_id": pos.position_id,
                "order_id": pos.order_id,
                "bar": bar,
                "reason": ex_reason,
                "phase": "exit",
            }
        )
        st.emit(
            event="EXIT_UNRESOLVED",
            decision_close_utc=bar,
            machine_state="UNRESOLVED",
            position_id=pos.position_id,
            order_id=pos.order_id,
            exit_reason=ex_reason,
        )
        st.position = None
        st.machine = "FLAT"
        return
    if ex_reason == "SL":
        st.machine = "CLOSED_SL"
        st.bump("closed_sl")
        st.emit(
            event="POSITION_CLOSED_SL",
            decision_close_utc=bar,
            machine_state="CLOSED_SL",
            position_id=pos.position_id,
            exit_price=ex_p,
        )
    elif ex_reason == "TP":
        st.machine = "CLOSED_TP"
        st.bump("closed_tp")
        st.emit(
            event="POSITION_CLOSED_TP",
            decision_close_utc=bar,
            machine_state="CLOSED_TP",
            position_id=pos.position_id,
            exit_price=ex_p,
        )
    else:
        return
    st.positions_log.append(
        {
            **pos.__dict__,
            "exit_bar": ex_t,
            "exit_price": ex_p,
            "exit_reason": ex_reason,
            "bars_held": bars_held,
            "status": st.machine,
        }
    )
    st.position = None
    st.machine = "FLAT"


def _try_fill_on_bar(
    st: IntegratedState,
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
    filled, px, rule, _note = model_entry_decision(
        MODEL_1,
        bar_utc=bar,
        active_from=pend.earliest_eligible_execution_utc,
        entry=pend.planned_entry,
        ohlc=ohlc,
    )
    if not filled or px is None:
        return False
    if pend.planned_tp is None:
        return False
    pid = _position_id(pend.order_id, bar)
    st.position = SimulatedPositionV1(
        position_id=pid,
        order_id=pend.order_id,
        entry_bar_utc=bar,
        entry_price=px,
        sl_price=pend.planned_sl,
        tp_price=float(pend.planned_tp),
        fill_rule_id=rule,
        signal_close_utc=pend.decision_close_utc,
        pool_key=pend.pool_key,
    )
    st.pending = None
    st.machine = "POSITION_SHORT_SIMULATED"
    st.bump("model_1_simulated_entries")
    st.emit(
        event="SIMULATED_FILL",
        decision_close_utc=bar,
        machine_state="POSITION_SHORT_SIMULATED",
        order_id=pend.order_id,
        position_id=pid,
        fill_label=FILL_LABEL,
        fill_rule_id=rule,
        entry_price=px,
        sl_price=pend.planned_sl,
        tp_price=pend.planned_tp,
        period_note=_period_note(bar),
    )
    return True


def run_integrated_backtest(
    config: dict[str, Any],
    prices: dict[str, dict[str, float]],
    bars: list[str],
    setups_by_close: dict[str, dict[str, Any]],
    density: Any,
    pilot_end: str,
) -> IntegratedState:
    st = IntegratedState()
    dynamic = DynamicCandidateStateV1()
    pool_age: dict[str, int] = {}
    last_parent_for_lineage: str | None = None

    for bar in bars:
        try:
            snap = density.collect_at_close(bar)
        except KeyError:
            continue
        candle = prices.get(bar)
        if not candle:
            continue

        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1

        if st.position is not None:
            _try_exit_on_bar(st, bar=bar, config=config, prices=prices, pilot_end=pilot_end)

        position_blocks_entry = st.position is not None

        if st.pending is not None and not position_blocks_entry and _try_fill_on_bar(
            st, bar=bar, prices=prices
        ):
            _try_exit_on_bar(st, bar=bar, config=config, prices=prices, pilot_end=pilot_end)

        if st.pending is not None and not position_blocks_entry:
            review = review_pending_at_close(
                config,
                decision_close_utc=bar,
                pending=st.pending,
                candle=candle,
                snapshot=snap,
                pool_age_bars=pool_age,
            )
            for rev in review.events:
                if rev.event_type == "KEEP":
                    st.bump("pending_keep")
                    st.emit(
                        event="PENDING_KEEP",
                        decision_close_utc=bar,
                        machine_state="PENDING_SHORT",
                        order_id=st.pending.order_id,
                    )
                elif rev.event_type == EVENT_CANCEL:
                    st.bump("pending_cancel")
                    st.machine = "CANCELLED"
                    st.emit(
                        event="PENDING_CANCEL",
                        decision_close_utc=bar,
                        machine_state="CANCELLED",
                        order_id=st.pending.order_id,
                        cancel_reason=rev.cancel_reason,
                    )
                    st.pending = None
                    st.machine = "FLAT"
            st.pending = review.pending
            if st.pending is not None:
                st.machine = "PENDING_SHORT"

        sig = detect_short_signal_pool_first(
            config,
            decision_close_utc=bar,
            order_active_from_utc=next_5m_open_after_close(bar),
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
            blocked_pool_keys=None,
        )
        selected = None
        if sig is not None:
            for b in snap.get("upper_bands") or []:
                if pool_key(float(b["lower_price"]), float(b["upper_price"])) == sig.pool_key:
                    selected = candidate_from_signal_band(b)
                    break

        tr = dynamic.process_bar(config, decision_utc=bar, snapshot=snap, selected=selected)
        setup = setups_by_close.get(bar)

        if tr.emit_setup and setup is not None:
            st.bump("scanner_candidate_emit")
            st.emit(
                event="SCANNER_CANDIDATE",
                decision_close_utc=bar,
                machine_state=st.machine,
                selection_reason=tr.selection_reason,
            )

            if position_blocks_entry:
                st.bump("suppressed_scanner_emits_position_open")
                st.emit(
                    event="SCANNER_SUPPRESSED",
                    decision_close_utc=bar,
                    machine_state=st.machine,
                    reason="position_open",
                )
                continue

            if st.pending is not None:
                prior = st.pending
                st.bump("pending_replace")
                st.machine = "REPLACED"
                st.emit(
                    event="PENDING_REPLACE",
                    decision_close_utc=bar,
                    machine_state="REPLACED",
                    order_id=prior.order_id,
                    new_pool_key=setup.get("upper_entry_pool_id"),
                )
                last_parent_for_lineage = prior.order_id
                st.pending = None
                st.machine = "FLAT"

            oid = _order_id(str(setup.get("setup_id")), bar)
            parent = last_parent_for_lineage
            st.pending = make_pending_from_scanner(
                order_id=oid,
                setup=setup,
                selection_reason=tr.selection_reason,
            )
            st.bump("pending_create")
            st.machine = "PENDING_SHORT"
            st.emit(
                event="PENDING_CREATE",
                decision_close_utc=bar,
                machine_state="PENDING_SHORT",
                order_id=oid,
                parent_order_id=parent,
                pending_active_from_utc=st.pending.earliest_eligible_execution_utc,
                activation_contract=activation_contract_note(),
            )
            if parent:
                st.lineage.append(
                    {
                        "child_order_id": oid,
                        "parent_order_id": parent,
                        "decision_close_utc": bar,
                        "link": "REPLACE",
                    }
                )
                last_parent_for_lineage = None

    if st.position is not None:
        st.bump("open_at_pilot_end")
        pos = st.position
        st.positions_log.append(
            {
                **pos.__dict__,
                "exit_bar": None,
                "exit_price": None,
                "exit_reason": "OPEN_AT_PILOT_END",
                "status": "POSITION_SHORT_SIMULATED",
            }
        )
        st.emit(
            event="POSITION_OPEN_AT_END",
            decision_close_utc=pilot_end,
            machine_state="POSITION_SHORT_SIMULATED",
            position_id=pos.position_id,
        )

    return st
