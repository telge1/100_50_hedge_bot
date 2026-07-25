"""Artifact writers and REPORT.md for Cobertura runs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv

from .engine import EngineResult


def _summary_row(result: EngineResult) -> dict[str, Any]:
    cfg = result.cfg
    last_econ = (
        result.total_exit_economics_timeline[-1]
        if result.total_exit_economics_timeline
        else {}
    )
    add_fills = [
        f for f in result.fill_events if f.get("kind") == "overlay_short_add"
    ]
    be_closes = [
        f for f in result.fill_events if f.get("kind") == "overlay_be_close"
    ]
    return {
        "symbol": cfg.symbol,
        "start_timestamp": cfg.start_timestamp,
        "start_price": cfg.start_price,
        "direction_mode": cfg.direction_mode,
        "add_size_pct": cfg.add_size_pct,
        "activation_move_pct": cfg.activation_move_pct,
        "first_add_move_pct": cfg.first_add_move_pct,
        "add_step_pct": cfg.add_step_pct,
        "max_add_count": cfg.max_add_count,
        "fee_rate_open": cfg.fee_rate_open,
        "fee_rate_close": cfg.fee_rate_close,
        "slippage_bps_open": cfg.slippage_bps_open,
        "slippage_bps_close": cfg.slippage_bps_close,
        "locked_spread_loss": result.locked_spread_loss,
        "final_state": result.state,
        "exit_reason": result.exit_reason,
        "recovery_rounds": result.recovery_rounds,
        "bars_processed": result.bars_processed,
        "overlay_add_fills": len(add_fills),
        "overlay_be_closes": len(be_closes),
        "realized_overlay_pnl": result.ledger.realized_overlay_pnl,
        "cumulative_entry_fees": result.ledger.cumulative_entry_fees,
        "cumulative_close_fees": result.ledger.cumulative_close_fees,
        "cumulative_slippage_costs": result.ledger.cumulative_slippage_costs,
        "final_total_exit_economics": last_econ.get("total_exit_economics"),
        "final_core_long_qty": result.ledger.core_long.qty,
        "final_core_short_qty": result.ledger.core_short.qty,
        "final_overlay_short_qty": result.ledger.overlay_short.qty,
        "final_net_qty": result.ledger.net_qty(),
    }


def _write_report_md(path: Path, result: EngineResult, summary: dict[str, Any]) -> None:
    lines = [
        "# Cobertura-0-Notional Recovery Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
        f"- symbol: `{summary['symbol']}`",
        f"- start: `{summary['start_timestamp']}` @ `{summary['start_price']}`",
        f"- locked_spread_loss: `{summary['locked_spread_loss']:.6f}`",
        f"- final_state: `{summary['final_state']}`",
        f"- exit_reason: `{summary['exit_reason']}`",
        f"- recovery_rounds: `{summary['recovery_rounds']}`",
        f"- bars_processed: `{summary['bars_processed']}`",
        f"- overlay_add_fills: `{summary['overlay_add_fills']}`",
        f"- overlay_be_closes: `{summary['overlay_be_closes']}`",
        f"- realized_overlay_pnl: `{summary['realized_overlay_pnl']}`",
        f"- cumulative_entry_fees: `{summary['cumulative_entry_fees']}`",
        f"- cumulative_close_fees: `{summary['cumulative_close_fees']}`",
        f"- final_total_exit_economics: `{summary['final_total_exit_economics']}`",
        "",
        "## Fee / BE semantics",
        "",
        "- Open/close fees booked per fill: `|price * qty| * fee_rate`.",
        "- Slippage worsens fill prices; informational slippage cost is not "
        "subtracted again from total_exit_economics.",
        "- Overlay BE solves for short-close trigger including round entry fees, "
        "exit fee, close slippage and fee_buffer.",
        "- Full exit only when total_exit_economics >= target - tolerance, "
        "including estimated remaining close fees.",
        "",
        "## Integrity",
        "",
    ]
    for key, value in result.integrity.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_run_artifacts(output_dir: Path, result: EngineResult) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = _summary_row(result)

    atomic_write_json(output_dir / "config_snapshot.json", result.cfg.to_dict())
    atomic_write_json(
        output_dir / "run_metadata.json",
        {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "start_index": result.start_index,
            "bars_processed": result.bars_processed,
            "final_state": result.state,
            "exit_reason": result.exit_reason,
            "recovery_reference_price_final": result.recovery_reference_price,
        },
    )
    write_csv(output_dir / "per_bar_trace.csv", result.per_bar_trace)
    write_csv(output_dir / "order_events.csv", result.order_events)
    write_csv(output_dir / "fill_events.csv", result.fill_events)
    write_csv(output_dir / "overlay_rounds.csv", result.overlay_rounds)
    write_csv(
        output_dir / "overlay_average_timeline.csv", result.overlay_average_timeline
    )
    write_csv(output_dir / "overlay_be_timeline.csv", result.overlay_be_timeline)
    write_csv(
        output_dir / "total_exit_economics_timeline.csv",
        result.total_exit_economics_timeline,
    )
    write_csv(output_dir / "per_run_summary.csv", [summary])
    write_csv(output_dir / "parameter_comparison.csv", [summary])
    write_csv(output_dir / "failure_reasons.csv", result.failure_reasons)
    atomic_write_json(output_dir / "integrity.json", result.integrity)
    _write_report_md(output_dir / "REPORT.md", result, summary)
    return output_dir
