"""Write manual 10-trade audit artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_manual_10_trade_report import DEFINITIONS_DOC


def _fmt_ts(x) -> str:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return _fmt_ts(x)
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def _pct(x: float) -> str:
    return f"{x:+.2f}%"


def write_report(
    *,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    out_dir: Path,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    df = pd.DataFrame(rows)
    p = out_dir / "manual_10_trades.csv"
    df.to_csv(p, index=False)
    paths["csv"] = p

    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary"] = p

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["definitions"] = p

    md = _render_md(rows, summary)
    p = out_dir / "manual_10_trades.md"
    p.write_text(md, encoding="utf-8")
    paths["md"] = p
    return paths


def _render_md(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Manual 10-Trade Audit Report")
    lines.append("")
    lines.append(f"- Audit: `{summary.get('audit_version')}`")
    lines.append(f"- Window: **{summary.get('window_start')}** → **{summary.get('window_end')}** ({summary.get('window_note')})")
    lines.append(f"- Trades selected: **{len(rows)}** (chronological)")
    lines.append(
        f"- Local equity start: ACTIVE={summary.get('start_active')}, "
        f"RESERVE={summary.get('start_reserve')}; "
        f"cashout={summary.get('cashout_rate')}, reimbursement coverage={summary.get('coverage_rate')}"
    )
    lines.append(
        "- Note: local equity is **sample-only** from 1000 USDT — not historical portfolio equity at these dates."
    )
    lines.append("")
    lines.append("## Control table")
    lines.append("")
    lines.append("| # | Time | Symbol | Side | Entry | TP | SL | Exit | Reason | Net % | Active After | Reserve |")
    lines.append("|---|------|--------|------|-------|----|----|------|--------|-------|--------------|---------|")
    for i, r in enumerate(rows, start=1):
        lines.append(
            "| {i} | {t} | {sym} | {side} | {ep:.6g} | {tp:.6g} | {sl:.6g} | {ex:.6g} | {reason} | {net:+.2f} | {aa:.2f} | {ra:.2f} |".format(
                i=i,
                t=_fmt_ts(r["entry_time"]),
                sym=r["symbol"],
                side=r["side"],
                ep=float(r["entry_price"]),
                tp=float(r["final_tp_price"]),
                sl=float(r["final_sl_price"]),
                ex=float(r["exit_price"]),
                reason=r["exit_reason"],
                net=float(r["net_return_pct"]),
                aa=float(r["active_after"]),
                ra=float(r["reserve_after"]),
            )
        )
    lines.append("")

    for i, r in enumerate(rows, start=1):
        lines.append("--------------------------------------------------")
        lines.append(f"TRADE {i}")
        lines.append("--------------------------------------------------")
        lines.append(f"trade_id: {r['trade_id']}")
        lines.append(f"Symbol: {r['symbol']}")
        lines.append(f"Side: {r['side']}")
        lines.append(f"Signal TF: {r['first_signal_tf']}  → highest: {r['highest_tf_reached']}")
        lines.append("")
        lines.append("Signal context:")
        lines.append(
            f"  {r['first_signal_tf']} {r['wave_direction']} Wave → {r['fade_direction']} Fade"
        )
        lines.append(
            f"  Tier {r['tier']} | {r['trend_aligned']} | {r['q_bucket']}"
            + (
                f" | DE={r['directional_efficiency']:.4f}"
                if r.get("directional_efficiency") is not None
                else ""
            )
        )
        lines.append("")
        lines.append(f"Signal:             {_fmt_ts(r['signal_time'])}")
        lines.append(f"Signal available:   {_fmt_ts(r['signal_available_at'])}")
        lines.append(f"Entry:              {_fmt_ts(r['entry_time'])}")
        lines.append(f"Entry price:        {float(r['entry_price']):.6g}")
        lines.append("")
        lines.append(
            f"Initial TP: {float(r['initial_tp_price']):.6g} ({_pct(float(r['initial_tp_pct']))})  "
            f"SL: {float(r['initial_sl_price']):.6g} ({_pct(-float(r['initial_sl_pct']))})"
        )
        lines.append(
            f"Final   TP: {float(r['final_tp_price']):.6g} ({_pct(float(r['final_tp_pct']))})  "
            f"SL: {float(r['final_sl_price']):.6g} ({_pct(-float(r['final_sl_pct']))})"
        )
        lines.append(
            f"Upgrades: {r['upgrade_count']}  sequence: {r['upgrade_sequence']}"
        )
        lines.append("")
        lines.append(f"Exit:               {_fmt_ts(r['exit_time'])}")
        lines.append(f"Exit price:         {float(r['exit_price']):.6g}")
        lines.append(f"Exit reason:        {r['exit_reason']}")
        lines.append(f"Holding:            {float(r['holding_minutes']):.1f} min")
        lines.append("")
        lines.append(f"Gross:              {_pct(float(r['gross_return_pct']))}")
        lines.append(f"Fees:               {_pct(-float(r['fee_pct']))}")
        lines.append(f"Net:                {_pct(float(r['net_return_pct']))}")
        lines.append("")
        lines.append("Local equity (sample from 1000):")
        lines.append(
            f"  before  ACTIVE={float(r['active_before']):.2f}  "
            f"RESERVE={float(r['reserve_before']):.2f}  "
            f"TOTAL={float(r['total_before']):.2f}"
        )
        lines.append(f"  raw_trade_pnl:     {float(r['raw_trade_pnl']):+.4f}")
        if float(r["raw_trade_pnl"]) > 0:
            lines.append(f"  Cashout 30%:       {float(r['cashout_amount']):.4f}")
        else:
            lines.append(f"  Loss:              {float(r['raw_trade_pnl']):+.4f}")
            lines.append(f"  Reimbursement:     {float(r['reimbursement_amount']):.4f}")
        lines.append(
            f"  after   ACTIVE={float(r['active_after']):.2f}  "
            f"RESERVE={float(r['reserve_after']):.2f}  "
            f"TOTAL={float(r['total_after']):.2f}"
        )
        if r.get("historical_note") == "HISTORICAL_FULL_PATH":
            lines.append(
                f"  HISTORICAL_FULL_PATH before: "
                f"ACTIVE={float(r['historical_active_before']):.2f}  "
                f"RESERVE={float(r['historical_reserve_before']):.2f}"
            )
        lines.append("")
        lines.append(
            f"Verification: {r['manual_audit_status']}  "
            f"(entry={r['entry_verified']} exit={r['exit_verified']} "
            f"tp_sl={r['tp_sl_verified']} upgrade={r['upgrade_verified']})"
        )
        if r.get("verify_notes"):
            lines.append(f"  notes: {r['verify_notes']}")
        lines.append("")

    inv = summary.get("accounting", {})
    lines.append("--------------------------------------------------")
    lines.append("ACCOUNTING INVARIANT CHECK")
    lines.append("--------------------------------------------------")
    lines.append(f"A) Cashout only from profit:           {inv.get('A_cashout_from_profit_only')}")
    lines.append(f"B) Reimbursement only from reserve:    {inv.get('B_reimb_from_reserve_only')}")
    lines.append(f"C) Reserve↔Active preserves total:     {inv.get('C_transfer_preserves_total')}")
    lines.append(f"D) total_after = total_before + pnl:   {inv.get('D_total_after_equals_before_plus_pnl')}")
    lines.append(f"E) Fees in net, not double-counted:    {inv.get('E_fees_in_net_not_double_counted')}")
    lines.append(f"F) Cashout not treated as loss:        {inv.get('F_cashout_not_treated_as_loss')}")
    lines.append(f"G) Reimbursement not new profit:       {inv.get('G_reimb_not_treated_as_new_profit')}")
    lines.append("")
    lines.append(f"**{summary.get('accounting_invariants')}**")
    lines.append("")
    return "\n".join(lines) + "\n"
