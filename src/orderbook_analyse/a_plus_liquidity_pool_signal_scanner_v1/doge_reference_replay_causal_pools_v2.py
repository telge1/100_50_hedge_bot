"""DOGE reference replay with causal pool availability v2."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from . import doge_reference_replay as _replay_mod
from . import doge_reference_replay_v2 as _v2
from .config import DEFAULT_OUT_DIR, SCANNER_VERSION
from .markers import dedupe_plan_rows, signals_to_marker_specs
from .pending_plan_lifecycle_audit import (
    EXTRA_LONG_SIGNAL_ID,
    LONG_REF_SIGNAL_ID,
    classify_extra_terminal_0321,
    classify_pullback_short_reference,
    classify_terminal_long_reference,
    pending_plan_lifecycle_row,
    reference_trade_timeline_rows,
)
from .runner import build_candles_by_tf, run_scanner
from .target_causality_audit import audit_signals, signal_pool_timeline_row, target_causality_row

AUDIT_END = pd.Timestamp("2026-08-28 12:00:00").to_pydatetime()
VERDICT = "A_PLUS_DOGE_V2_REVALIDATED_WITH_CAUSAL_POOLS"
VERDICT_PARTIAL = "A_PLUS_DOGE_V2_PARTIAL_REVALIDATION"
VERDICT_BLOCKED = "A_PLUS_DOGE_V2_REVALIDATION_BLOCKED"


def run_doge_causal_pools_replay(*, out_dir: Path | None = None) -> dict[str, Any]:
    run_id = int(time.time())
    out = Path(out_dir or DEFAULT_OUT_DIR) / f"doge_reference_replay_causal_pools_v2_{run_id}"
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing to overwrite: {out}")
    out.mkdir(parents=True, exist_ok=False)

    client = get_clickhouse_client()
    candles = build_candles_by_tf("DOGEUSDT", _replay_mod.WARMUP_START, AUDIT_END, client=client)
    _replay_mod.AUDIT_END = AUDIT_END

    result = run_scanner(symbol="DOGEUSDT", candles_by_tf=candles)
    short_ref = _replay_mod.identify_pullback_short_reference(candles, symbol="DOGEUSDT")
    long_ref = _replay_mod.identify_terminal_long_reference(candles, symbol="DOGEUSDT")
    funnel = _replay_mod.audit_window_funnel(candles, symbol="DOGEUSDT")

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

    audit_rows = _v2._audit_window_plans(result, audit_start=_replay_mod.AUDIT_START, audit_end=AUDIT_END)
    target_audit = audit_signals(audit_rows)
    lifecycle_rows = [pending_plan_lifecycle_row(s) for s in audit_rows]
    for c in result.get("invalidated") or []:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        if isinstance(d, dict) and str(d.get("armed_at", "")).startswith("2026-08-28"):
            lifecycle_rows.append(pending_plan_lifecycle_row(d))

    short_parity = classify_pullback_short_reference(short_scan)
    long_parity = classify_terminal_long_reference(long_scan)
    extra_parity = classify_extra_terminal_0321(extra_0321)
    extra_parity["causal_pool_recheck"] = _classify_extra_0321_causal(extra_0321)

    reval_rows = [
        {
            "signal": "short",
            "classification": short_parity.get("classification"),
            "armed_at": None if not short_scan else short_scan.get("armed_at"),
            "filled_at": None
            if not short_scan
            else short_scan.get("hypothetical_filled_at") or short_scan.get("filled_at"),
            "entry_pool": None if not short_scan else (short_scan.get("entry_pool") or {}).get("pool_id"),
            "target_pool": None if not short_scan else (short_scan.get("target_pool") or {}).get("pool_id"),
        },
        {
            "signal": "long",
            "classification": long_parity.get("classification"),
            "armed_at": None if not long_scan else long_scan.get("armed_at"),
            "reclaim_at": None if not long_scan else long_scan.get("armed_at"),
            "target_pool": None if not long_scan else (long_scan.get("target_pool") or {}).get("pool_id"),
        },
        {
            "signal": "extra_0321",
            "classification": extra_parity.get("classification"),
            "causal_recheck": extra_parity.get("causal_pool_recheck"),
            "armed_at": None if not extra_0321 else extra_0321.get("armed_at"),
        },
    ]

    target_ok = all(r.get("causality_pass") for r in target_audit) if target_audit else True
    lifecycle_ok = all(r.get("lifecycle_pass") for r in lifecycle_rows) if lifecycle_rows else True
    short_ok = short_parity.get("classification") == "VALID_REFERENCE_SHORT"
    long_ok = long_parity.get("classification") == "VALID_REFERENCE_LONG"

    verdict = VERDICT if short_ok and long_ok and target_ok and lifecycle_ok else VERDICT_PARTIAL
    if not short_scan and not long_scan:
        verdict = VERDICT_BLOCKED

    manifest = {
        "run_id": run_id,
        "scanner_version": SCANNER_VERSION,
        "pool_time_semantics_version": "closed_confirmation_bar_v2",
        "known_at_basis": "confirmation_bar_close",
        "htf_aggregation_basis": "closed_1m_prefix_as_of",
        "forming_tip_used": False,
        "legacy_pool_timestamps": False,
        "verdict": verdict,
        "short_scanner_signal": short_scan,
        "long_scanner_signal": long_scan,
        "extra_terminal_0321": extra_parity,
        "target_causality_ok": target_ok,
        "lifecycle_ok": lifecycle_ok,
        "no_execution": True,
    }

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    _replay_mod._write_csv(out / "scanner_revalidation.csv", reval_rows)
    _replay_mod._write_csv(out / "target_pool_causality_audit.csv", target_audit)
    _replay_mod._write_csv(out / "pending_plan_pool_lifecycle_audit.csv", lifecycle_rows)
    _replay_mod._write_csv(out / "signal_pool_timeline.csv", [signal_pool_timeline_row(s) for s in audit_rows])
    _replay_mod._write_csv(out / "reference_trade_timeline.csv", reference_trade_timeline_rows(short=short_scan, long=long_scan))
    _replay_mod._write_csv(out / "candidate_funnel.csv", funnel.rows())
    _replay_mod._write_jsonl(out / "confirmed_signals.jsonl", result["confirmed"])
    _replay_mod._write_jsonl(out / "invalidated_candidates.jsonl", result["invalidated"] + result.get("superseded", []))
    markers = signals_to_marker_specs(result["confirmed"], run_id=str(run_id))
    _replay_mod._write_jsonl(out / "marker_payloads.jsonl", markers)
    (out / "methodology.md").write_text(
        "# DOGE causal pools revalidation\n\nFull scanner recompute with closed confirmation bar pool availability.\n",
        encoding="utf-8",
    )
    (out / "report.md").write_text(_report(manifest, short_parity, long_parity, extra_parity), encoding="utf-8")
    return {"manifest": manifest, "out_dir": str(out), "result": result, "verdict": verdict}


def _classify_extra_0321_causal(sig: dict[str, Any] | None) -> str:
    if not sig:
        return "NO_SIGNAL"
    armed = str(sig.get("armed_at") or "")
    if not armed.startswith("2026-08-28T03:21"):
        return "NOT_0321_SIGNAL"
    # armed 03:21 — any 1h pools used must have available_at <= 03:21
    ep = sig.get("entry_pool") or {}
    known = ep.get("available_at") or ep.get("known_at")
    if known and str(known) > armed:
        return "REMOVED_AS_POOL_LOOKAHEAD"
    return classify_extra_terminal_0321(sig).get("classification", "UNKNOWN")


def _report(manifest: dict, short_p: dict, long_p: dict, extra_p: dict) -> str:
    return "\n".join(
        [
            f"# {manifest['verdict']}",
            "",
            "## Pool semantics",
            "- known_at = confirmation_bar_close",
            "- closed 1m prefix before HTF aggregation",
            "",
            "## Short",
            json.dumps(short_p, indent=2, default=str),
            "",
            "## Long",
            json.dumps(long_p, indent=2, default=str),
            "",
            "## Extra 03:21",
            json.dumps(extra_p, indent=2, default=str),
            "",
        ]
    )


if __name__ == "__main__":
    print(json.dumps(run_doge_causal_pools_replay(), indent=2, default=str))
