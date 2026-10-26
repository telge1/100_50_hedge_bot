"""Causal isolated replay — gates only; no D46 mutation; default zero intents."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
for p in (str(BT_D7), str(PHASE4D)):
    if p not in sys.path:
        sys.path.insert(0, p)

from signal_engine_v1 import detect_short_signal  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402

from contracts_v1 import CONTRACT, RUNNER_CONTRACT  # noqa: E402
from entry_gate_v1 import evaluate_entry_gate  # noqa: E402
from regime_classifier_v1 import RegimeClassifierV1  # noqa: E402
from contracts_v1 import RegimeContextV1  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402


def _next_5m_open(close_utc: str) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(close_utc.replace("Z", "+00:00")) + timedelta(minutes=5)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class IsolatedReplayStateV1:
    gate_decisions: list[dict[str, Any]] = field(default_factory=list)
    short_intents: list[dict[str, Any]] = field(default_factory=list)
    pending_orders_created: int = 0
    modeled_fills: int = 0


def run_isolated_research_replay(
    *,
    config: dict[str, Any],
    prices: dict[str, dict[str, float]],
    bars: list[str],
    density_session: Any,
    pause: ShortPauseControllerV1 | None = None,
    classifier: RegimeClassifierV1 | None = None,
) -> dict[str, Any]:
    """
    Research-only causal loop: evaluate baseline candidate + fail-closed gates.
    Does not call D46 and does not place pending orders in skeleton v1.
    """
    pause_ctl = pause or ShortPauseControllerV1()
    clf = classifier or RegimeClassifierV1()
    pool_age: dict[str, int] = {}
    state = IsolatedReplayStateV1()

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

        baseline_sig = detect_short_signal(
            config,
            decision_close_utc=bar,
            order_active_from_utc=_next_5m_open(bar),
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
        )
        ctx = RegimeContextV1(
            decision_close_utc=bar,
            ohlc_to_close=candle,
            ema_features=None,
        )
        decision = evaluate_entry_gate(
            context=ctx,
            pause=pause_ctl,
            baseline_signal=baseline_sig,
            snapshot=snap,
            classifier=clf,
        )
        state.gate_decisions.append(
            {
                "decision_close_utc": bar,
                "allow_new_short_intent": decision.allow_new_short_intent,
                "reasons": decision.reasons,
                "regime": decision.regime.regime,
                "regime_evidence": decision.regime.evidence,
                "eligibility_reason": decision.eligibility.reason,
                "pause_state": decision.pause_state,
                "baseline_signal_pool_key": baseline_sig.pool_key if baseline_sig else None,
                "density_provenance": decision.eligibility.candidate.density_provenance
                if decision.eligibility.candidate
                else {},
            }
        )
        if decision.allow_new_short_intent and baseline_sig is not None:
            state.short_intents.append(
                {
                    "decision_close_utc": bar,
                    "pool_key": baseline_sig.pool_key,
                    "entry_level": baseline_sig.entry_level,
                }
            )
            # Skeleton v1: still no order placement even if gate passed (future phase).

    return {
        "contract": RUNNER_CONTRACT,
        "strategy_contract": CONTRACT,
        "bars_processed": len(state.gate_decisions),
        "short_intents": state.short_intents,
        "short_intent_count": len(state.short_intents),
        "pending_orders_created": state.pending_orders_created,
        "modeled_fills": state.modeled_fills,
        "gate_decisions": state.gate_decisions,
        "pause_state_final": pause_ctl.pause_state,
        "pause_events": [e.__dict__ for e in pause_ctl.events],
    }
