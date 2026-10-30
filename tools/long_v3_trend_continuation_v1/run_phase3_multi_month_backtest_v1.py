#!/usr/bin/env python3
"""LONG V3 Phase 3 — multi-month Variant A backtest (Jun–Sep+ continuous state)."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
BASE_OUT = ROOT / "logs/research/long_v3_phase3_multimonth_v1"
REPORT_PATH = ROOT / "reports/LONG_V3_PHASE3_MULTI_MONTH_BACKTEST_V1.md"
YAML = ROOT / "configs/long_v3_trend_continuation_research_v1.yaml"
GLOBAL_LOCK = BASE_OUT / "active_run.json"

sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1"))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d60_ema_history_validation_v1"))

from density_session_adapter_v1 import FrozenBundleDensitySessionV1  # noqa: E402
from ema_research_v1 import ema_map_for_bars  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_phase2m_ledger_integrity_v1 import (  # noqa: E402
    max_consecutive_sl,
    reconstruct_trade_lifecycle,
    worst_loss_streak_sum_pct,
)
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from multi_bundle_density_session_v1 import MultiBundleDensitySessionV1  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from phase3_preflight_v1 import PHASE3_MONTH_SPECS, run_phase3_preflight, write_preflight_json  # noqa: E402
from research_runner_v2d_v1 import run_long_research_v2d  # noqa: E402

PROGRESS_EVERY_BARS = 288  # ~24h


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month_key_for_close(close_utc: str) -> str:
    return close_utc[:7]


def _variant_a_cfg(yaml_sup, max_risk) -> LongResearchConfigV2DV1:
    return LongResearchConfigV2DV1(
        support_mode="V2E_QUALIFIED",
        sl_mode="DEEPER_Q_UPPER",
        recovery_mode="NEXT_BAR",
        v2e_yaml=yaml_sup,
        max_risk_pct=max_risk,
        pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
        formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
        touch_lifecycle="LINEAGE_AWARE_V2J",
        buy_limit_semantics="FROZEN_AT_PLACE_V2J",
    )


def _bundle_pairs_from_preflight() -> list[tuple[str, Path]]:
    pairs: list[tuple[str, Path]] = []
    for spec in PHASE3_MONTH_SPECS:
        rel = spec.get("bundle_rel")
        if rel:
            pairs.append((spec["month_key"], ROOT / rel))
    return pairs


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _check_duplicate_run() -> tuple[bool, dict | None]:
    if not GLOBAL_LOCK.is_file():
        return False, None
    try:
        lock = json.loads(GLOBAL_LOCK.read_text())
    except json.JSONDecodeError:
        return False, None
    pid = lock.get("pid")
    if pid and _pid_alive(int(pid)):
        return True, lock
    return False, lock


def _trade_to_ledger_row(
    trade,
    *,
    ledger: list[dict],
    exit_ledger: list[dict],
) -> dict[str, Any]:
    lc = reconstruct_trade_lifecycle(
        ledger=ledger,
        exit_ledger=exit_ledger,
        fill_utc=trade.fill_bar_utc,
        entry_price=trade.entry_price,
        sl_price=trade.sl_price,
    )
    place_utc = None
    if lc.get("status") == "RESOLVED":
        ep = lc.get("effective_place") or {}
        place_utc = ep.get("bar_utc")
    entry = trade.entry_price
    sl = trade.sl_price
    tp = trade.tp_limit
    risk_pct = abs(entry - sl) / entry * 100.0 if entry else None
    tp_dist = abs((tp or 0) - entry) / entry * 100.0 if tp and entry else None
    return {
        "place_utc": place_utc,
        "fill_utc": trade.fill_bar_utc,
        "entry_price": entry,
        "sl_price": sl,
        "tp_price": tp,
        "exit_utc": trade.exit_bar_utc,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "gross_return_pct": trade.gross_return_pct,
        "entry_to_sl_risk_pct": round(risk_pct, 6) if risk_pct is not None else None,
        "entry_to_tp_distance_pct": round(tp_dist, 6) if tp_dist is not None else None,
        "pool_key": trade.pool_key,
        "position_id": trade.position_id,
        "lifecycle": lc,
    }


def _monthly_stats(
    *,
    month_key: str,
    trades: list,
    entry_events: list[dict],
    eval_start_close: str | None,
) -> dict[str, Any]:
    in_month = lambda u: u and u.startswith(month_key)  # noqa: E731
    month_trades = [t for t in trades if in_month(t.fill_bar_utc)]
    resolved = [t for t in month_trades if t.gross_return_pct is not None]
    wins = [t for t in resolved if (t.gross_return_pct or 0) > 0]
    losses = [t for t in resolved if (t.gross_return_pct or 0) < 0]
    tp_hits = sum(1 for t in resolved if t.exit_reason in ("FIXED_UPPER_TP", "UPPER_POOL_GAP_TP"))
    sl_hits = sum(1 for t in resolved if t.exit_reason == "STRUCTURAL_SL")
    places = [e for e in entry_events if e.get("action") == "PLACE" and in_month(e.get("bar_utc"))]
    cancels = [e for e in entry_events if e.get("action") == "CANCEL" and in_month(e.get("bar_utc"))]
    replaces = [e for e in entry_events if e.get("action") == "REPLACE" and in_month(e.get("bar_utc"))]
    unfilled = len(places) - sum(1 for e in entry_events if e.get("action") == "FILL" and in_month(e.get("bar_utc")))
    open_pos = sum(1 for t in month_trades if t.exit_bar_utc is None)
    avg_win_target = None
    avg_sl_dist = None
    if resolved:
        avg_sl_dist = round(
            sum(abs(t.entry_price - t.sl_price) / t.entry_price * 100 for t in resolved) / len(resolved),
            6,
        )
    if wins:
        avg_win_target = round(
            sum(abs((t.tp_limit or t.entry_price) - t.entry_price) / t.entry_price * 100 for t in wins)
            / len(wins),
            6,
        )
    streak = max_consecutive_sl(month_trades)
    worst_streak = worst_loss_streak_sum_pct(month_trades)
    note = ""
    if month_key == "2026-06" and eval_start_close and eval_start_close.startswith("2026-06"):
        note = f"Eval starts {eval_start_close}; first ~168h of June are warmup-only in merged timeline."
    return {
        "month_key": month_key,
        "completed_trades": len(resolved),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "hit_rate": round(len(wins) / len(resolved), 6) if resolved else None,
        "tp_exits": tp_hits,
        "sl_exits": sl_hits,
        "open_positions_end_slice": open_pos,
        "unfilled_buy_limits_estimate": max(0, unfilled),
        "cancels": len(cancels),
        "replaces": len(replaces),
        "avg_tp_target_pct": avg_win_target,
        "avg_sl_distance_pct": avg_sl_dist,
        "avg_realized_win_pct": round(sum(t.gross_return_pct for t in wins) / len(wins), 6) if wins else None,
        "avg_realized_loss_pct": round(sum(t.gross_return_pct for t in losses) / len(losses), 6)
        if losses
        else None,
        "sum_gross_trade_returns_pct": round(sum(t.gross_return_pct for t in resolved), 6) if resolved else 0.0,
        "fees_slippage": "not_modeled",
        "max_consecutive_sl": streak,
        "worst_loss_streak_sum_pct": round(worst_streak, 6),
        "note": note,
    }


def _save_replay_checkpoint(
    run_dir: Path,
    *,
    result,
    entry_events: list[dict],
    eval_start_index: int,
    eval_start_close_utc: str | None,
    merged_last_close: str,
) -> None:
    _write_json(
        run_dir / "replay_checkpoint.json",
        {
            "format": "phase3_replay_checkpoint_v1",
            "saved_utc": _utc_now(),
            "eval_start_index": eval_start_index,
            "eval_start_close_utc": eval_start_close_utc,
            "merged_last_close": merged_last_close,
            "summary": result.summary,
            "entry_events": entry_events,
            "order_event_ledger": result.order_event_ledger,
            "trades": [asdict(t) for t in result.trades],
            "blocked": result.blocked,
            "block_reason": result.block_reason,
        },
    )


def _trades_from_checkpoint(data: dict) -> list:
    from research_runner_v1 import LongTradeRecordV1

    return [LongTradeRecordV1(**row) for row in data.get("trades") or []]


def _validate_phase3_completion(
    *,
    pf,
    closes: list[str],
    trades: list,
    monthly: list[dict],
    trade_rows: list[dict],
    manifest: dict,
) -> tuple[str, list[str]]:
    """Return (status, issues). status is COMPLETED_VALIDATED or FAILED_VALIDATION."""
    issues: list[str] = []
    if manifest.get("variant_a_baseline", {}).get("buy_limit_semantics") != "FROZEN_AT_PLACE_V2J":
        issues.append("VARIANT_A_BASELINE_MISMATCH")
    if manifest.get("variant_b_executed") is True:
        issues.append("UNEXPECTED_VARIANT_B_EXECUTED")
    v_b = manifest.get("variant_a_baseline") or {}
    if v_b.get("buy_limit_semantics") == "DYNAMIC_UPPER_GROWTH_V2K":
        issues.append("VARIANT_B_CONFIG_IN_BASELINE")
    for mk in ("2026-06", "2026-07", "2026-08", "2026-09"):
        if mk not in pf.included_months:
            issues.append(f"MISSING_MONTH_{mk}")
    if not trade_rows:
        issues.append("EMPTY_TRADE_LEDGER")
    if len(monthly) < 4:
        issues.append("INCOMPLETE_MONTHLY_SUMMARY")
    if not closes:
        issues.append("NO_CLOSES")
    elif len(closes) < 2017:
        issues.append("INSUFFICIENT_WARMUP_BARS")
    open_at_end = [t for t in trades if t.gross_return_pct is None]
    if open_at_end:
        issues.append(f"OPEN_POSITIONS_AT_END:{len(open_at_end)}")
    if issues:
        return "FAILED_VALIDATION", issues
    return "COMPLETED_VALIDATED", []


def _finalize_run_outputs(
    run_dir: Path,
    run_id: str,
    *,
    result,
    pf,
    closes: list[str],
    ohlc_meta: dict,
    eval_start: int,
    eval_start_close: str | None,
) -> tuple[int, str]:
    """Write ledgers, monthly stats, report; return exit code and final state."""
    ledger = result.order_event_ledger
    exit_ledger = [e for e in ledger if e.get("phase") == "exit"]
    trade_rows = [
        _trade_to_ledger_row(t, ledger=ledger, exit_ledger=exit_ledger) for t in result.trades
    ]

    month_keys = sorted({spec["month_key"] for spec in PHASE3_MONTH_SPECS if spec.get("bundle_rel")})
    monthly = [
        _monthly_stats(
            month_key=mk,
            trades=result.trades,
            entry_events=result.entry_events,
            eval_start_close=eval_start_close if mk == "2026-06" else None,
        )
        for mk in month_keys
    ]

    _write_json(run_dir / "phase3_run_result_summary.json", result.summary)
    _write_json(run_dir / "trade_ledger.json", trade_rows)

    csv_fields = [
        "place_utc",
        "fill_utc",
        "entry_price",
        "sl_price",
        "tp_price",
        "exit_utc",
        "exit_price",
        "exit_reason",
        "gross_return_pct",
        "entry_to_sl_risk_pct",
        "entry_to_tp_distance_pct",
        "pool_key",
        "position_id",
    ]
    with (run_dir / "trade_ledger.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        w.writeheader()
        for row in trade_rows:
            w.writerow(row)

    with (run_dir / "monthly_summary.csv").open("w", newline="", encoding="utf-8") as f:
        if monthly:
            w = csv.DictWriter(f, fieldnames=list(monthly[0].keys()))
            w.writeheader()
            w.writerows(monthly)

    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    manifest.update(
        {
            "completed_utc": _utc_now(),
            "ohlc_meta": ohlc_meta,
            "eval_start_index": eval_start,
            "eval_start_close_utc": eval_start_close,
            "variant_b_executed": False,
            "variant_b_note": (
                "Phase 3 mandate is Variant A only (FROZEN_AT_PLACE_V2J). "
                "No DYNAMIC_UPPER_GROWTH_V2K run in this phase."
            ),
        }
    )
    _write_json(run_dir / "run_manifest.json", manifest)

    val_state, val_issues = _validate_phase3_completion(
        pf=pf,
        closes=closes,
        trades=result.trades,
        monthly=monthly,
        trade_rows=trade_rows,
        manifest=manifest,
    )

    report_lines = [
        "# LONG V3 Phase 3 — Multi-Month Backtest (Variant A)",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Status:** `{val_state}`",
        f"- **Completed UTC:** {_utc_now()}",
        f"- **Git HEAD:** `{pf.git_head}`",
        f"- **Eval window:** `{eval_start_close}` → `{closes[-1]}`",
        f"- **Merged closes:** {len(closes)}",
        "",
        "## Variant scope",
        "",
        "- **Variant A (FROZEN_AT_PLACE_V2J):** executed — statistics below.",
        "- **Variant B (DYNAMIC_UPPER_GROWTH_V2K):** **not executed** in Phase 3; "
        "no A/B trade delta or dynamic-repricing comparison in this run.",
        "",
        "## Month coverage",
        "",
    ]
    for aud in pf.month_audit:
        report_lines.append(
            f"- **{aud.get('month_key')}** ({aud.get('coverage_verdict')}): "
            f"{aud.get('decision_close_first')} → {aud.get('decision_close_last')}"
        )
    if val_issues:
        report_lines.extend(["", "## Validation issues", ""])
        for iss in val_issues:
            report_lines.append(f"- {iss}")
    report_lines.extend(["", "## Global summary (Variant A)", "", "```json", json.dumps(result.summary, indent=2), "```"])
    report_lines.extend(["", "## Monthly summary (Variant A)", ""])
    for m in monthly:
        report_lines.append(f"### {m['month_key']}")
        report_lines.append("```json")
        report_lines.append(json.dumps(m, indent=2))
        report_lines.append("```")
        report_lines.append("")
    report_lines.extend(
        [
            "## Variant B",
            "",
            "Not run — additional/removed trades from dynamic repricing: **N/A**.",
            "",
            "*Sum of per-trade gross returns is not account equity or drawdown; fees/slippage not modeled.*",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(report_lines), encoding="utf-8")

    return (0 if val_state == "COMPLETED_VALIDATED" else 1), val_state, val_issues, monthly, result.summary


def finalize_stats_from_checkpoint(run_dir: Path, run_id: str) -> int:
    ck_path = run_dir / "replay_checkpoint.json"
    if not ck_path.is_file():
        return 1
    data = json.loads(ck_path.read_text())
    pf = run_phase3_preflight(yaml_path=YAML)
    pairs = _bundle_pairs_from_preflight()
    session = MultiBundleDensitySessionV1(pairs)
    closes = session.decision_closes()
    trades = _trades_from_checkpoint(data)

    class _Result:
        pass

    result = _Result()
    result.trades = trades
    result.summary = data.get("summary") or {}
    result.entry_events = data.get("entry_events") or []
    result.order_event_ledger = data.get("order_event_ledger") or []
    result.blocked = data.get("blocked", False)
    result.block_reason = data.get("block_reason")

    eval_start = int(data.get("eval_start_index", pf.eval_start_index))
    eval_start_close = data.get("eval_start_close_utc") or pf.eval_start_close_utc
    ohlc_meta = {}
    if (run_dir / "run_manifest.json").is_file():
        ohlc_meta = json.loads((run_dir / "run_manifest.json").read_text()).get("ohlc_meta") or {}

    status_path = run_dir / "run_status.json"

    def status_update(**kwargs: Any) -> None:
        base = {"run_id": run_id, "updated_utc": _utc_now()}
        if status_path.is_file():
            try:
                base.update(json.loads(status_path.read_text()))
            except json.JSONDecodeError:
                pass
        base.update(kwargs)
        _write_json(status_path, base)

    try:
        code, val_state, val_issues, monthly, summary = _finalize_run_outputs(
            run_dir,
            run_id,
            result=result,
            pf=pf,
            closes=closes,
            ohlc_meta=ohlc_meta,
            eval_start=eval_start,
            eval_start_close=eval_start_close,
        )
    except Exception as exc:
        status_update(state="FAILED", error=str(exc), failed_utc=_utc_now())
        return 1

    status_update(
        state=val_state,
        validation_issues=val_issues,
        completed_utc=_utc_now(),
        trade_count=len(trades),
        summary=summary,
        report_path=str(REPORT_PATH),
    )
    return code


def worker_main(run_dir: Path, run_id: str) -> int:
    status_path = run_dir / "run_status.json"
    log_path = run_dir / "backtest.log"

    def status_update(**kwargs: Any) -> None:
        base = {
            "run_id": run_id,
            "updated_utc": _utc_now(),
        }
        if status_path.is_file():
            try:
                base.update(json.loads(status_path.read_text()))
            except json.JSONDecodeError:
                pass
        base.update(kwargs)
        _write_json(status_path, base)

    status_update(state="RUNNING", started_utc=_utc_now())

    ly = load_long_research_yaml(YAML)
    cfg = _variant_a_cfg(ly.support, ly.max_risk_pct)
    pairs = _bundle_pairs_from_preflight()
    session = MultiBundleDensitySessionV1(pairs)
    closes = session.decision_closes()
    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    emas = ema_map_for_bars(sorted(prices.keys()), prices)
    ema9, ema20, ema59, ema200 = emas[9], emas[20], emas[59], emas[200]

    pf = run_phase3_preflight(yaml_path=YAML)
    eval_start = pf.eval_start_index
    eval_start_close = pf.eval_start_close_utc

    bars_processed = 0
    current_month = _month_key_for_close(closes[eval_start] if eval_start < len(closes) else closes[0])

    def on_bar(i: int, bar: str, meta: dict) -> None:
        nonlocal bars_processed, current_month
        bars_processed = i - eval_start + 1
        mk = _month_key_for_close(bar)
        if mk != current_month:
            current_month = mk
        if bars_processed % PROGRESS_EVERY_BARS != 0 and i != len(closes) - 1:
            return
        status_update(
            state="RUNNING",
            current_month=current_month,
            last_bar_utc=bar,
            bars_processed=bars_processed,
            bars_total=len(closes) - eval_start,
            trade_count=meta.get("trade_count", 0),
            open_position=meta.get("open_position", False),
        )

    try:
        result = run_long_research_v2d(
            bars=closes,
            prices=prices,
            ema9=ema9,
            ema20=ema20,
            ema59=ema59,
            ema200=ema200,
            session=session,
            entry_config=cfg,
            exit_variant="LONG_A",
            eval_start_index=eval_start,
            eval_end_index=len(closes) - 1,
            record_state_trace=False,
            on_bar_processed=on_bar,
        )
    except Exception as exc:
        status_update(state="FAILED", error=str(exc), failed_utc=_utc_now())
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"FAILED {_utc_now()}: {exc}\n")
        return 1

    if result.blocked:
        status_update(state="FAILED", block_reason=result.block_reason, failed_utc=_utc_now())
        return 1

    _save_replay_checkpoint(
        run_dir,
        result=result,
        entry_events=result.entry_events,
        eval_start_index=eval_start,
        eval_start_close_utc=eval_start_close,
        merged_last_close=closes[-1],
    )

    try:
        code, val_state, val_issues, _monthly, summary = _finalize_run_outputs(
            run_dir,
            run_id,
            result=result,
            pf=pf,
            closes=closes,
            ohlc_meta=ohlc_meta,
            eval_start=eval_start,
            eval_start_close=eval_start_close,
        )
    except Exception as exc:
        status_update(
            state="FAILED",
            error=str(exc),
            failed_utc=_utc_now(),
            replay_checkpoint=str(run_dir / "replay_checkpoint.json"),
        )
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"FAILED finalize {_utc_now()}: {exc}\n")
        return 1

    status_update(
        state=val_state,
        validation_issues=val_issues,
        completed_utc=_utc_now(),
        trade_count=len(result.trades),
        summary=summary,
        report_path=str(REPORT_PATH),
    )
    if GLOBAL_LOCK.is_file():
        try:
            lock = json.loads(GLOBAL_LOCK.read_text())
            if lock.get("run_id") == run_id:
                GLOBAL_LOCK.unlink()
        except (json.JSONDecodeError, OSError):
            pass
    return code


def launch_worker_nohup(run_dir: Path, run_id: str) -> tuple[int, str]:
    worker_script = Path(__file__).resolve()
    log_path = run_dir / "nohup_backtest.log"
    cmd = [
        "nohup",
        sys.executable,
        str(worker_script),
        "--worker",
        "--run-id",
        run_id,
        "--run-dir",
        str(run_dir),
    ]
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(ROOT),
            start_new_session=True,
        )
    return proc.pid, " ".join(cmd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-dir", default="")
    parser.add_argument("--finalize-stats", action="store_true", help="Regenerate stats from replay_checkpoint.json")
    args = parser.parse_args()

    if args.finalize_stats:
        if not args.run_id or not args.run_dir:
            print("--finalize-stats requires --run-id and --run-dir", file=sys.stderr)
            return 2
        return finalize_stats_from_checkpoint(Path(args.run_dir), args.run_id)

    if args.worker:
        if not args.run_id or not args.run_dir:
            print("worker requires --run-id and --run-dir", file=sys.stderr)
            return 2
        return worker_main(Path(args.run_dir), args.run_id)

    pf = run_phase3_preflight(yaml_path=YAML)
    run_id = f"phase3_a_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    run_dir = BASE_OUT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_preflight_json(run_dir / "preflight.json", pf)

    if args.preflight_only:
        print(json.dumps({"verdict": pf.verdict, "ok": pf.ok, "block_reason": pf.block_reason}, indent=2))
        return 0 if pf.ok else 1

    dup, lock = _check_duplicate_run()
    if dup:
        print(json.dumps({"verdict": "BLOCKED", "reason": "DUPLICATE_RUN", "lock": lock}, indent=2))
        return 1

    if not pf.ok:
        _write_json(
            run_dir / "run_status.json",
            {
                "run_id": run_id,
                "state": "BLOCKED",
                "block_reason": pf.block_reason,
                "updated_utc": _utc_now(),
            },
        )
        print(json.dumps({"verdict": "BLOCKED", "run_id": run_id, "block_reason": pf.block_reason}, indent=2))
        return 1

    manifest = {
        "run_id": run_id,
        "started_utc": _utc_now(),
        "git_head": pf.git_head,
        "yaml_path": str(YAML),
        "variant_a_baseline": {
            "buy_limit_semantics": "FROZEN_AT_PLACE_V2J",
            "recovery_mode": "NEXT_BAR",
            "touch_lifecycle": "LINEAGE_AWARE_V2J",
            "formation_semantics": "CAUSAL_BELOW_PRICE_SEGMENT",
            "pool_age_semantics": "CAUSAL_REGION_LINEAGE_5",
            "exit_variant": "LONG_A",
        },
        "merged_first_close": pf.merged_first_close,
        "merged_last_close": pf.merged_last_close,
        "eval_start_close_utc": pf.eval_start_close_utc,
        "included_months": pf.included_months,
        "excluded_months": pf.excluded_months,
        "month_audit": pf.month_audit,
    }
    _write_json(run_dir / "run_manifest.json", manifest)
    _write_json(
        run_dir / "run_status.json",
        {
            "run_id": run_id,
            "state": "STARTING",
            "updated_utc": _utc_now(),
            "run_dir": str(run_dir),
        },
    )

    pid, cmd_str = launch_worker_nohup(run_dir, run_id)
    time.sleep(1.5)
    alive = _pid_alive(pid)

    lock_doc = {
        "run_id": run_id,
        "pid": pid,
        "started_utc": _utc_now(),
        "run_dir": str(run_dir),
        "cmd": cmd_str,
    }
    BASE_OUT.mkdir(parents=True, exist_ok=True)
    _write_json(GLOBAL_LOCK, lock_doc)

    status = {
        "run_id": run_id,
        "state": "RUNNING" if alive else "FAILED_TO_START",
        "pid": pid,
        "pid_alive": alive,
        "log_path": str(run_dir / "nohup_backtest.log"),
        "status_path": str(run_dir / "run_status.json"),
        "manifest_path": str(run_dir / "run_manifest.json"),
        "progress_cmd": f"tail -f {run_dir / 'run_status.json'}",
        "updated_utc": _utc_now(),
    }
    _write_json(run_dir / "run_status.json", status)

    print(json.dumps(status, indent=2))
    return 0 if alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
