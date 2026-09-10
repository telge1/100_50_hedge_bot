"""Terminal and markdown case reports (descriptive only)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import FULL_OB_STATUS, MARKET_PROFILE_STATUS


def render_terminal(
    *,
    symbol: str,
    focus_z: str,
    coverage: dict[str, Any],
    full_ob: dict[str, Any],
    fp_pre: list[dict[str, Any]],
    avr_summaries: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
    evidence_summary: dict[str, Any],
    outcomes_df: pd.DataFrame,
    interpretation: str,
    market_profile_terminal: str | None = None,
) -> str:
    ms = coverage.get("modality_status") or {}
    fp5 = next((x for x in fp_pre if x.get("fp_window_s") == 300), {})
    avr5 = next((x for x in avr_summaries if x.get("window_s") == 300), {})
    oi5 = next((x for x in oi_rows if x.get("window_s") == 300), {})
    lines = [
        "OBFULL SINGLE CASE INSPECTOR V1",
        "",
        f"Symbol: {symbol}",
        f"Focus:  {focus_z}",
        "UTC:    CONFIRMED",
        "",
        "COVERAGE",
        f"Public Trades: {ms.get('PUBLIC_TRADES', 'UNKNOWN')}",
        f"Footprint:     {ms.get('FOOTPRINT', 'UNKNOWN')}",
        f"AVR:           {ms.get('AVR', 'UNKNOWN')}",
        f"OI:            {ms.get('OI', 'UNKNOWN')}",
        f"Liquidations:  {ms.get('LIQUIDATIONS', 'UNKNOWN')}",
        f"Full-OB:       {FULL_OB_STATUS if full_ob.get('status') == FULL_OB_STATUS else full_ob.get('status')}",
        f"Overall:       {coverage.get('overall')}",
        "",
        "PRE-FOCUS 5m",
        f"Buy: {fp5.get('fp_buy_notional')}",
        f"Sell: {fp5.get('fp_sell_notional')}",
        f"Delta: {fp5.get('fp_delta_notional')}",
        f"Price: open={fp5.get('fp_open')} close={fp5.get('fp_close')} return_bps={fp5.get('fp_return_bps')}",
        f"OI: {oi5.get('oi_direction')} delta={oi5.get('oi_delta_abs')} quadrant={oi5.get('quadrant')}",
        f"AVR majority: {avr5.get('majority_state')}",
        "",
        "EARLIEST EVIDENCE",
        f"Status: {evidence_summary.get('status')}",
        f"Time: {evidence_summary.get('earliest_evidence_at')}",
        f"Type: {evidence_summary.get('earliest_type')}",
        f"Direction: {evidence_summary.get('earliest_direction')}",
        "",
        "AT FOCUS",
        f"Footprint 60s delta: {next((x.get('fp_delta_notional') for x in fp_pre if x.get('fp_window_s')==60), None)}",
        f"OI 60s: {next((x.get('oi_direction') for x in oi_rows if x.get('window_s')==60), None)}",
        f"Liquidations 60s long/short: "
        f"{next((x.get('long_count') for x in liq_rows if x.get('window_s')==60), None)}/"
        f"{next((x.get('short_count') for x in liq_rows if x.get('window_s')==60), None)}",
        f"Full-OB: {FULL_OB_STATUS}",
        "",
        "AFTER FOCUS",
    ]
    label = {5: "5s", 15: "15s", 30: "30s", 60: "60s", 300: "5m", 900: "15m", 1800: "30m"}
    if outcomes_df is not None and not outcomes_df.empty:
        for _, r in outcomes_df.sort_values("horizon_seconds").iterrows():
            h = int(r["horizon_seconds"])
            lines.append(
                f"{label.get(h, h)}:  Return {r.get('return_bps')} | "
                f"MFE {r.get('mfe_bps') if r.get('mfe_bps') is not None else r.get('neutral_mfe_bps')} | "
                f"MAE {r.get('mae_bps') if r.get('mae_bps') is not None else r.get('neutral_mae_bps')}"
            )
    if market_profile_terminal:
        lines += ["", market_profile_terminal]
    lines += [
        "",
        "INTERPRETATION",
        interpretation,
        "",
        "LIMITATION",
        "Full-OB evidence cannot be evaluated for this case.",
        f"Market Profile: {'ATTACHED' if market_profile_terminal else MARKET_PROFILE_STATUS}",
        "",
    ]
    return "\n".join(lines)


def render_case_md(
    *,
    terminal: str,
    causality: dict[str, Any],
    full_ob: dict[str, Any],
    evidence_summary: dict[str, Any],
    resources: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "# Single Case Inspector V1 — CASE REPORT",
            "",
            "```",
            terminal.rstrip(),
            "```",
            "",
            "## Causality",
            "```json",
            __import__("json").dumps(causality, indent=2, sort_keys=True, default=str),
            "```",
            "",
            "## Full-OB",
            "```json",
            __import__("json").dumps(full_ob, indent=2, sort_keys=True, default=str),
            "```",
            "",
            "## Early evidence summary",
            "```json",
            __import__("json").dumps(evidence_summary, indent=2, sort_keys=True, default=str),
            "```",
            "",
            "## Resources",
            "```json",
            __import__("json").dumps(resources, indent=2, sort_keys=True, default=str),
            "```",
            "",
        ]
    )


def descriptive_interpretation(
    *,
    evidence_summary: dict[str, Any],
    fp_pre: list[dict[str, Any]],
    avr_summaries: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    outcomes_df: pd.DataFrame,
) -> str:
    parts = []
    st = evidence_summary.get("status")
    if st == "NO_EARLY_EVIDENCE_DETECTED":
        parts.append(
            "No contract-valid early directional AVR/footprint signal was isolated before the chart focus; "
            "the focus remains a visual selection only."
        )
    else:
        parts.append(
            f"Earliest pre-focus note at {evidence_summary.get('earliest_evidence_at')}: "
            f"{evidence_summary.get('earliest_type')} ({evidence_summary.get('earliest_direction')})."
        )
    fp5 = next((x for x in fp_pre if x.get("fp_window_s") == 300), {})
    if fp5:
        parts.append(
            f"In the 5m before focus, footprint delta was {fp5.get('fp_delta_notional')} "
            f"with return_bps={fp5.get('fp_return_bps')} (descriptive, not a signal)."
        )
    avr5 = next((x for x in avr_summaries if x.get("window_s") == 300), {})
    if avr5:
        parts.append(f"AVR majority state over 5m pre-focus: {avr5.get('majority_state')}.")
    oi5 = next((x for x in oi_rows if x.get("window_s") == 300), {})
    if oi5:
        parts.append(
            f"OI 5m context: {oi5.get('oi_direction')} / quadrant {oi5.get('quadrant')} "
            "(not interpreted as long/short opening)."
        )
    if outcomes_df is not None and not outcomes_df.empty:
        r60 = outcomes_df[outcomes_df["horizon_seconds"] == 60]
        if len(r60):
            parts.append(
                f"After focus, 60s public-trade return_bps={r60.iloc[0].get('return_bps')} "
                "(computed separately from evidence; not used to retune detectors)."
            )
    parts.append("Full-OB walls/imbalance/control cannot be assessed while FULL_OB_UNAVAILABLE.")
    return " ".join(parts)
