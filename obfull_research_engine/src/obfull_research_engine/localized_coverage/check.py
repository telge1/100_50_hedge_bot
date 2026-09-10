"""Main localized coverage check entrypoint."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import (
    DEFAULT_ARCHIVE_ROOT,
)

from ..interval_coverage import check_interval_coverage
from ..timeparse import format_utc_z
from . import POLICY_ID, POLICY_VERSION, STRICT_POLICY_ID, VERDICT_NOT_COMPLETE
from .config import policy_config_hash, policy_config_payload
from .focus import check_focus_ts
from .gap_scan import scan_window_gaps
from .oi_stale import localize_oi_stale
from .spans import build_spans


def check_localized_coverage(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    archive_root: Path | None = None,
    focus_ts: datetime | None = None,
    include_strict_shadow: bool = True,
) -> dict[str, Any]:
    """Produce LOCALIZED_EXCLUSION_V1 coverage with usable/excluded spans."""
    archive_root = Path(archive_root or DEFAULT_ARCHIVE_ROOT)
    symbol = symbol.upper()

    strict = None
    if include_strict_shadow:
        strict = check_interval_coverage(
            symbol=symbol, start=start, end=end, archive_root=archive_root
        )

    gap_report = scan_window_gaps(
        symbol=symbol, start=start, end=end, archive_root=archive_root
    )
    oi_report = localize_oi_stale(symbol=symbol, start=start, end=end)

    pt_ok = True
    liq_ok = True
    if strict:
        ss = strict.get("source_status") or {}
        pt_ok = ss.get("PUBLIC_TRADES") == "COMPLETE"
        liq_ok = ss.get("LIQUIDATIONS") == "COMPLETE"

    spans = build_spans(
        start=start,
        end=end,
        tainted_intervals=gap_report["tainted_intervals"],
        oi_stale_intervals=oi_report["stale_intervals"],
        public_trades_ok=pt_ok,
        liquidations_ok=liq_ok,
    )
    if not pt_ok or not liq_ok:
        spans["verdict"] = VERDICT_NOT_COMPLETE

    focus = None
    if focus_ts is not None:
        focus = check_focus_ts(
            focus_ts=focus_ts,
            window_start=start,
            window_end=end,
            usable_spans=spans["usable_spans"],
            excluded_spans=spans["excluded_spans"],
        )

    ok = spans["verdict"] != VERDICT_NOT_COMPLETE
    analysis_ok = ok
    if focus is not None and focus.get("status") != "ELIGIBLE":
        analysis_ok = False

    return {
        "schema_version": "localized_coverage_v1",
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "policy_config_hash": policy_config_hash(),
        "policy_config": policy_config_payload(),
        "symbol": symbol,
        "start": format_utc_z(start),
        "end": format_utc_z(end),
        "ok": analysis_ok if focus is not None else ok,
        "verdict": spans["verdict"],
        "analysis_ok": analysis_ok,
        "full_ob_summary": {
            "status": spans["verdict"],
            "n_sequence_gaps": gap_report["n_sequence_gaps"],
            "n_tainted_intervals": gap_report["n_tainted_intervals"],
            "excluded_ob_duration_seconds": spans["excluded_ob_duration_seconds"],
            "usable_duration_seconds": spans["usable_duration_seconds"],
            "n_usable_spans": spans["n_usable_spans"],
        },
        "gap_report": gap_report,
        "oi_report": oi_report,
        "usable_spans": spans["usable_spans"],
        "excluded_spans": spans["excluded_spans"],
        "span_summary": {
            k: spans[k]
            for k in (
                "window_duration_seconds",
                "usable_duration_seconds",
                "excluded_ob_duration_seconds",
                "n_usable_spans",
                "n_excluded_spans",
                "max_local_exclusion_duration_seconds",
                "verdict",
            )
        },
        "focus": focus,
        "strict_shadow": {
            "policy_id": STRICT_POLICY_ID,
            "verdict": None if strict is None else strict.get("verdict"),
            "source_status": None if strict is None else strict.get("source_status"),
            "missing_intervals": None if strict is None else strict.get("missing_intervals"),
            "gap_count": None if strict is None else strict.get("gap_count"),
        },
        "public_trades_ok": pt_ok,
        "liquidations_ok": liq_ok,
        "source_status_localized": {
            "FULL_OB": spans["verdict"],
            "PUBLIC_TRADES": "COMPLETE" if pt_ok else "MISSING",
            "LIQUIDATIONS": "COMPLETE" if liq_ok else "MISSING",
            "OPEN_INTEREST": "LOCALIZED_STALE_OPTIONAL",
            "PRICE": spans["verdict"],
            "CHECKPOINT": "RECOVERY_PER_GAP",
            "REPLAY_CHAIN": "PER_USABLE_SPAN",
        },
        # Compatibility fields for orchestrator precheck
        "coverage": {
            "verdict": spans["verdict"],
            "source_status": {
                "FULL_OB": "COMPLETE"
                if spans["verdict"] != VERDICT_NOT_COMPLETE
                else "MISSING",
                "PUBLIC_TRADES": "COMPLETE" if pt_ok else "MISSING",
                "LIQUIDATIONS": "COMPLETE" if liq_ok else "MISSING",
                "OPEN_INTEREST": "COMPLETE",  # optional localized
                "PRICE": "COMPLETE" if spans["verdict"] != VERDICT_NOT_COMPLETE else "MISSING",
                "CHECKPOINT": "COMPLETE" if spans["verdict"] != VERDICT_NOT_COMPLETE else "MISSING",
                "REPLAY_CHAIN": "COMPLETE"
                if spans["verdict"] != VERDICT_NOT_COMPLETE
                else "MISSING",
            },
            "missing_intervals": [
                {
                    "source": e.get("reason"),
                    "start": e["start"],
                    "end": e["end"],
                    "reason": e.get("reason"),
                }
                for e in spans["excluded_spans"]
                if e.get("reason") != "OI_STALE"
            ],
            "gap_count": gap_report["n_sequence_gaps"],
            "resync_count": 0,
            "hour_details": gap_report.get("hour_meta") or [],
            "usable_spans": spans["usable_spans"],
            "excluded_spans": spans["excluded_spans"],
            "coverage_policy": POLICY_ID,
        },
        "future_public_trades": {"ok": True},  # filled by precheck wrapper
        "missing_intervals": [
            {
                "source": e.get("reason"),
                "start": e["start"],
                "end": e["end"],
                "reason": e.get("reason"),
            }
            for e in spans["excluded_spans"]
            if e.get("hard_local_gap") or e.get("reason") == "TOO_SHORT_AFTER_EXCLUSION"
        ],
    }
