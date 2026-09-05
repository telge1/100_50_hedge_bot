"""DOGEUSDT reference replay V2 vs ClickHouse + chart LLD pipeline."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from .config import DEFAULT_OUT_DIR, ENTRY_FRACTION_FROM_LOWER, SCANNER_VERSION
from . import doge_reference_replay as _replay_mod
from .pool_selection import pullback_limit_price
from .runner import build_candles_by_tf, run_scanner

from .pending_plan_lifecycle_audit import (
    EXTRA_LONG_SIGNAL_ID,
    LONG_REF_SIGNAL_ID,
    SHORT_REF_SIGNAL_ID,
    classify_extra_terminal_0321,
    classify_pullback_short_reference,
    classify_terminal_long_reference,
    pending_plan_lifecycle_row,
    reference_trade_timeline_rows,
)
from .target_causality_audit import audit_signals, signal_pool_timeline_row, target_causality_row

WARMUP_START = _replay_mod.WARMUP_START
pool_from_engine_type = _replay_mod.pool_from_engine_type

AUDIT_END_V2 = pd.Timestamp("2026-08-28 12:00:00").to_pydatetime()

VERDICT_LIFECYCLE = "A_PLUS_DOGE_V2_PENDING_PLAN_LIFECYCLE_CONFIRMED"
VERDICT_CAUSAL_TARGET = "A_PLUS_DOGE_V2_CAUSAL_TARGET_POOL_CONFIRMED"
VERDICT_CAUSAL_READY = "A_PLUS_DOGE_V2_CAUSAL_SIGNAL_INTENT_READY"
VERDICT_REFERENCE_REJECTED = "REFERENCE_SIGNAL_REJECTED_NO_CAUSAL_TARGET"
VERDICT_VALIDATED = "A_PLUS_DOGE_REFERENCE_REPLAY_V2_VALIDATED"
VERDICT_SHORT_ONLY = "A_PLUS_DOGE_SHORT_VALIDATED_LONG_RR_BLOCKED"
VERDICT_MISMATCH = "A_PLUS_DOGE_REFERENCE_CONTRACT_MISMATCH_V2"


def run_doge_reference_replay_v2(*, out_dir: Path | None = None) -> dict[str, Any]:
    run_id = int(time.time())
    out = Path(out_dir or DEFAULT_OUT_DIR) / f"doge_reference_replay_v2_{run_id}"
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing to overwrite: {out}")
    out.mkdir(parents=True, exist_ok=False)

    client = get_clickhouse_client()
    candles = build_candles_by_tf("DOGEUSDT", WARMUP_START, AUDIT_END_V2, client=client)
    candles_ref = {tf: df.copy() for tf, df in candles.items()}

    _replay_mod.AUDIT_END = AUDIT_END_V2
    short_ref = _replay_mod.identify_pullback_short_reference(candles_ref, symbol="DOGEUSDT")
    long_ref = _replay_mod.identify_terminal_long_reference(candles_ref, symbol="DOGEUSDT")

    result = run_scanner(symbol="DOGEUSDT", candles_by_tf=candles)
    funnel = _replay_mod.audit_window_funnel(candles_ref, symbol="DOGEUSDT")

    audit_confirmed = [
        c for c in result["confirmed"] if c.get("signal_at") and c["signal_at"][:10] == "2026-08-28"
    ]
    short_scan = next(
        (
            c
            for c in audit_confirmed
            if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"
            and (c.get("entry_pool") or {}).get("pool_id") == "lld:DOGEUSDT:15m:upper:1787886900"
        ),
        next((c for c in audit_confirmed if c["setup_type"] == "A_PLUS_PULLBACK_SHORT"), None),
    )
    long_scan = next(
        (c for c in audit_confirmed if (c.get("signal_id") or c.get("setup_id")) == LONG_REF_SIGNAL_ID),
        None,
    )
    if long_scan is None:
        long_scan = next(
            (
                c
                for c in reversed(audit_confirmed)
                if c["setup_type"] == "A_PLUS_TERMINAL_POOL_LONG"
                and c.get("entry_pool", {}).get("side") == "BID"
                and str(c.get("armed_at", "")).startswith("2026-08-28T10:")
            ),
            None,
        )
    extra_0321 = next(
        (
            c
            for c in result["confirmed"]
            if (c.get("signal_id") or c.get("setup_id")) == EXTRA_LONG_SIGNAL_ID
            or str(c.get("armed_at", "")).startswith("2026-08-28T03:21")
        ),
        None,
    )
    long_rr_blocked = [
        c
        for c in result.get("candidates", [])
        if c.get("setup_type") == "A_PLUS_TERMINAL_POOL_LONG"
        and "RECLAIM_VALID_BUT_RR_BLOCKED" in (c.get("reason_codes") or [])
        and str(c.get("signal_at", "")).startswith("2026-08-28")
    ]

    sample_times = pd.date_range(_replay_mod.AUDIT_START, AUDIT_END_V2, freq="15min").tolist()
    parity = _replay_mod.build_pool_parity_rows(candles_ref, symbol="DOGEUSDT", sample_times=[t.to_pydatetime() for t in sample_times])
    parity_bad = [r for r in parity if (r["in_chart"] != r["in_scanner"]) or (r["in_chart"] and r["in_scanner"] and not r["parity_ok"])]

    ref_pool = "lld:DOGEUSDT:15m:upper:1787886900"
    pool_sel = [r for r in result.get("pool_selection_audit", []) if ref_pool in str(r.get("pool_id", ""))]

    short_events = result.get("pullback_limit_events") or []
    short_armed_ev = next((e for e in short_events if e.get("event") == "LIMIT_INTENT_ARMED" and ref_pool in str(e.get("pool_id", ""))), None)
    short_fill_ev = next((e for e in short_events if e.get("event") == "HYPOTHETICAL_FILLED" and ref_pool in str(e.get("pool_id", ""))), None)
    causal_ok = True
    if short_scan:
        armed_at = short_scan.get("armed_at")
        filled_at = short_scan.get("hypothetical_filled_at") or short_scan.get("filled_at")
        if not armed_at or not filled_at or armed_at >= filled_at:
            causal_ok = False
        if short_scan.get("signal_at") != armed_at:
            causal_ok = False
    if short_armed_ev and short_fill_ev:
        if short_armed_ev.get("at", "") >= short_fill_ev.get("at", ""):
            causal_ok = False

    audit_rows = _audit_window_plans(result, audit_start=_replay_mod.AUDIT_START, audit_end=AUDIT_END_V2)
    target_audit = audit_signals(audit_rows)
    timeline_rows = [signal_pool_timeline_row(s) for s in audit_rows]
    lifecycle_rows = [pending_plan_lifecycle_row(s) for s in audit_rows]
    # Also include invalidated/ambiguous pullbacks from audit window
    for c in result.get("invalidated") or []:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        if not isinstance(d, dict):
            continue
        armed = str(d.get("armed_at") or "")
        if armed.startswith("2026-08-28") and d.get("setup_type", "").startswith("A_PLUS_PULLBACK"):
            lifecycle_rows.append(pending_plan_lifecycle_row(d))
    target_causality_ok = all(r.get("causality_pass") for r in target_audit) if target_audit else True
    short_target_ok = _reference_target_ok(short_scan)
    long_target_ok = _reference_target_ok(long_scan)
    short_parity = classify_pullback_short_reference(short_scan)
    long_parity = classify_terminal_long_reference(long_scan)
    extra_parity = classify_extra_terminal_0321(extra_0321)
    ref_timeline = reference_trade_timeline_rows(short=short_scan, long=long_scan)
    lifecycle_ok = all(r.get("lifecycle_pass") for r in lifecycle_rows) if lifecycle_rows else True

    contract_v2 = {
        "scanner_version": SCANNER_VERSION,
        "entry_fraction_from_lower": ENTRY_FRACTION_FROM_LOWER,
        "pullback_model": "limit_intent_armed_then_hypothetical_fill",
        "terminal_model": "ladder_sweep_with_reclaim_reset",
        "one_order_per_episode": True,
        "target_pool_causality": "known_at_le_armed_at_and_valid_at_arm",
        "target_freeze_at_arm": True,
        "pending_plan_lifecycle": "invalidate_if_entry_or_target_gone_before_fill",
        "same_bar_policy": "AMBIGUOUS_INTRABAR_no_assumed_order",
    }

    verdict = VERDICT_MISMATCH
    if parity_bad:
        verdict = "A_PLUS_DOGE_POOL_PARITY_BLOCKED"
    elif (
        short_parity.get("classification") == "VALID_REFERENCE_SHORT"
        and long_parity.get("classification") == "VALID_REFERENCE_LONG"
        and target_causality_ok
        and lifecycle_ok
        and causal_ok
        and short_target_ok
        and long_target_ok
    ):
        verdict = VERDICT_LIFECYCLE
    elif short_scan and long_scan and causal_ok and target_causality_ok and short_target_ok and long_target_ok:
        verdict = VERDICT_CAUSAL_TARGET
    elif short_scan and not long_scan:
        verdict = VERDICT_SHORT_ONLY if short_target_ok else VERDICT_MISMATCH
    elif short_scan and long_scan and causal_ok and target_causality_ok:
        verdict = VERDICT_CAUSAL_READY
    elif short_scan and long_scan:
        verdict = VERDICT_VALIDATED
    elif short_scan and not long_scan:
        verdict = VERDICT_SHORT_ONLY

    ladder_audit = result.get("ladder_audit") or {}

    manifest = {
        "run_id": run_id,
        "symbol": "DOGEUSDT",
        "audit_start": _replay_mod._iso(_replay_mod.AUDIT_START),
        "audit_end": _replay_mod._iso(AUDIT_END_V2),
        "verdict": verdict,
        "parity_mismatches": len(parity_bad),
        "n_confirmed_audit_window": len(audit_confirmed),
        "short_scanner_signal": short_scan,
        "long_scanner_signal": long_scan,
        "long_rr_blocked": long_rr_blocked[:3],
        "short_reference_manual": short_ref,
        "long_reference_manual": long_ref,
        "reference_pool_selected": bool(pool_sel),
        "causal_intent_ok": causal_ok,
        "target_causality_ok": target_causality_ok,
        "short_target_causality_ok": short_target_ok,
        "long_target_causality_ok": long_target_ok,
        "n_audit_plans": len(audit_rows),
        "target_causality_failures": [r for r in target_audit if not r.get("causality_pass")],
        "lifecycle_ok": lifecycle_ok,
        "short_reference_parity": short_parity,
        "long_reference_parity": long_parity,
        "extra_terminal_0321": extra_parity,
        "ladder_audit": ladder_audit,
        "n_signal_intents": len(result.get("signal_intents") or []),
        "no_execution": True,
    }

    (out / "contract_v2.json").write_text(json.dumps(contract_v2, indent=2), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    _replay_mod._write_csv(out / "pool_selection_audit.csv", result.get("pool_selection_audit", []))
    _replay_mod._write_csv(out / "pullback_limit_events.csv", result.get("pullback_limit_events", []))
    _replay_mod._write_csv(out / "terminal_ladder_events.csv", result.get("terminal_ladder_events", []))
    _replay_mod._write_csv(out / "reaction_state_resets.csv", result.get("reaction_state_resets", []))
    _replay_mod._write_csv(out / "candidate_funnel.csv", funnel.rows())
    _replay_mod._write_csv(out / "one_order_contract_audit.csv", _one_order_audit(result))
    _replay_mod._write_csv(out / "entry_sl_tp_audit.csv", [_replay_mod.entry_sl_tp_audit(c) for c in result["confirmed"]])
    _replay_mod._write_csv(out / "target_pool_causality_audit.csv", target_audit)
    _replay_mod._write_csv(out / "signal_pool_timeline.csv", timeline_rows)
    _replay_mod._write_csv(out / "pending_plan_pool_lifecycle_audit.csv", lifecycle_rows)
    _replay_mod._write_csv(out / "reference_trade_timeline.csv", ref_timeline)
    _replay_mod._write_jsonl(out / "confirmed_signals.jsonl", result["confirmed"])
    _replay_mod._write_jsonl(out / "signal_intents.jsonl", result.get("signal_intents") or [])
    _replay_mod._write_jsonl(out / "lifecycle_events.jsonl", result.get("lifecycle_events") or [])
    _replay_mod._write_jsonl(out / "invalidated_candidates.jsonl", result["invalidated"] + result.get("superseded", []))

    if short_ref.get("found") and short_ref.get("pool_edges"):
        pe = short_ref["pool_edges"]
        pr = pool_from_engine_type(
            {
                **short_ref,
                "pool_id": short_ref.get("entry_pool_id", ref_pool),
                "lower_edge": pe["lower"],
                "upper_edge": pe["upper"],
                "midpoint": (pe["lower"] + pe["upper"]) / 2,
                "side": "ASK",
                "timeframe": "15m",
                "known_at": short_ref["15m_known_at"],
                "source_timestamp": short_ref["15m_known_at"],
                "component_count": 1,
            }
        )
        manifest["expected_short_limit"] = pullback_limit_price(pr, direction="SHORT")

    (out / "report.md").write_text(
        _report_v2(manifest, result, funnel, target_audit, short_parity, long_parity, extra_parity),
        encoding="utf-8",
    )
    return {"manifest": manifest, "result": result, "out_dir": str(out), "funnel": funnel}


def _audit_window_plans(result: dict[str, Any], *, audit_start: datetime, audit_end: datetime) -> list[dict[str, Any]]:
    """Collect unique plans visible in audit window (full confirmed rows preferred)."""
    from .markers import dedupe_plan_rows

    confirmed_ids = {
        str(c.get("signal_id") or c.get("setup_id") or "")
        for c in (result.get("confirmed") or [])
    }
    rows: list[dict[str, Any]] = list(result.get("confirmed") or [])
    rows.extend(
        c
        for c in (result.get("candidates") or [])
        if str(c.get("state") or "") in {"LIMIT_INTENT_ARMED", "CONFIRMED", "HYPOTHETICAL_FILLED"}
        and str(c.get("signal_id") or c.get("setup_id") or "") not in confirmed_ids
    )
    rows.extend(
        r
        for r in (result.get("signal_intents") or [])
        if str(r.get("signal_id") or r.get("setup_id") or "") not in confirmed_ids
        and str(r.get("state") or "") == "LIMIT_INTENT_ARMED"
    )
    deduped = dedupe_plan_rows([r for r in rows if isinstance(r, dict)])
    out: list[dict[str, Any]] = []
    start_s = audit_start.isoformat()[:19]
    end_s = audit_end.isoformat()[:19]
    for row in deduped:
        armed = str(row.get("armed_at") or row.get("signal_at") or "")
        if armed[:19] >= start_s and armed[:19] <= end_s:
            out.append(row)
    return out


def _reference_target_ok(signal: dict[str, Any] | None) -> bool:
    if not signal:
        return False
    return bool(target_causality_row(signal).get("causality_pass"))


def _one_order_audit(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in result.get("confirmed", []):
        rows.append(
            {
                "setup_id": c.get("setup_id"),
                "setup_type": c.get("setup_type"),
                "entry_pool_id": c.get("entry_pool", {}).get("pool_id"),
                "entries": 1,
                "filled_once": c.get("filled_once", True),
                "has_sl": c.get("stop_price") is not None,
                "has_tp": c.get("target_price") is not None,
            }
        )
    return rows


def _report_v2(
    manifest: dict[str, Any],
    result: dict[str, Any],
    funnel: Any,
    target_audit: list[dict[str, Any]],
    short_parity: dict[str, Any],
    long_parity: dict[str, Any],
    extra_parity: dict[str, Any],
) -> str:
    short = manifest.get("short_scanner_signal") or {}
    long = manifest.get("long_scanner_signal") or {}
    short_t = target_causality_row(short) if short else {}
    long_t = target_causality_row(long) if long else {}
    ladder_audit = manifest.get("ladder_audit") or result.get("ladder_audit") or {}
    return "\n".join(
        [
            f"# {manifest['verdict']}",
            "",
            "## CONTRACT V2",
            json.dumps(
                {
                    "entry_fraction": ENTRY_FRACTION_FROM_LOWER,
                    "target_freeze_at_arm": True,
                    "pending_plan_lifecycle": True,
                    "same_bar_policy": "AMBIGUOUS_INTRABAR",
                },
                indent=2,
            ),
            "",
            "## MANUAL REFERENCE TIMES",
            json.dumps(
                {
                    "short_visible_approx": "2026-08-28T06:30:00",
                    "short_armed": "2026-08-28T04:15:00",
                    "short_fill": "2026-08-28T06:35:00",
                    "long_sweep_approx": "2026-08-28T10:00:00",
                    "long_reclaim": "2026-08-28T10:27:00",
                },
                indent=2,
            ),
            "",
            "## REFERENCE PARITY",
            json.dumps(
                {"short": short_parity, "long": long_parity, "extra_0321": extra_parity},
                indent=2,
                default=str,
            ),
            "",
            "## TARGET CAUSALITY",
            json.dumps(
                {
                    "target_causality_ok": manifest.get("target_causality_ok"),
                    "lifecycle_ok": manifest.get("lifecycle_ok"),
                    "n_audit_plans": manifest.get("n_audit_plans"),
                    "failures": len(manifest.get("target_causality_failures") or []),
                },
                indent=2,
            ),
            "",
            "## PULLBACK SHORT (REFERENCE)",
            json.dumps(
                {
                    "signal_id": short.get("signal_id"),
                    "armed_at": short.get("armed_at"),
                    "hypothetical_filled_at": short.get("hypothetical_filled_at"),
                    "target_pool_id": short_t.get("target_pool_id"),
                    "target_pool_known_at": short_t.get("target_pool_known_at"),
                    "causality_pass": short_t.get("causality_pass"),
                    "classification": short_parity.get("classification"),
                },
                indent=2,
            ),
            "",
            "## TERMINAL LONG (REFERENCE)",
            json.dumps(
                {
                    "signal_id": long.get("signal_id"),
                    "armed_at": long.get("armed_at"),
                    "approach_at": long.get("approach_at"),
                    "sweep_low": long.get("sweep_low"),
                    "target_pool_id": long_t.get("target_pool_id"),
                    "target_pool_known_at": long_t.get("target_pool_known_at"),
                    "classification": long_parity.get("classification"),
                },
                indent=2,
            ),
            "",
            "## EXTRA TERMINAL 03:21",
            json.dumps(extra_parity, indent=2, default=str),
            "",
            "## LADDER AUDIT",
            json.dumps(ladder_audit, indent=2),
            "",
            "## FUNNEL",
            json.dumps(funnel.rows(), indent=2),
        ]
    ) + "\n"
