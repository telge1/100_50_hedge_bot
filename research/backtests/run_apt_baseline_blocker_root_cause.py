"""CLI runner: baseline blocker root-cause audit (APTUSDT trade 3 default).

Research-only. Pure baseline — no freeze / recovery / S2 policies.

Example:

```bash
python -m research.backtests.run_apt_baseline_blocker_root_cause \\
  --coin APTUSDT \\
  --trade-id 3 \\
  --output-dir research/backtests/results/apt_baseline_blocker_root_cause_20260721
```
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.apt_baseline_blocker_root_cause import (
    APT_TRADE3_COIN,
    APT_TRADE3_ID,
    analyze_root_cause_markers,
    assert_output_dir_safe,
    build_cycle_snapshots,
    build_event_timeline,
    build_exit_reachability_by_cycle,
    build_exposure_growth_by_cycle,
    build_fill_replay_rows,
    build_recovery_start_state,
    check_baseline_parity,
    load_trade_start_index_from_baseline,
    pnl_reconciliation_rows,
    select_trade_from_continuous,
    selected_trade_payload,
    write_code_path_map,
    write_report,
)
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.long_add_multistart_metrics import analyze_trade
from research.backtests.run_inventory_mtm_neg1_policy_audit import FULL_HISTORY_CANDLE_LIMIT

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/apt_baseline_blocker_root_cause_20260721"


def _write_csv(path: Path, rows: list[dict[str, Any]], *, fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(fieldnames) if fieldnames else []
    seen = set(fields)
    if not fields:
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        status["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        status["dirty"] = bool(porcelain.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def run_audit(
    *,
    coin: str,
    trade_id: int,
    output_dir: Path,
    candle_limit: int = FULL_HISTORY_CANDLE_LIMIT,
    start_index: int | None = None,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    coin_upper = coin.upper()
    candles = normalize_candles(coin_upper, load_candles_for_symbol(coin_upper, limit=int(candle_limit)))

    result, meta = select_trade_from_continuous(coin=coin_upper, candles=candles, trade_id=int(trade_id))
    start_idx = int(start_index if start_index is not None else result.start_index or 0)
    if start_index is not None and start_idx != int(result.start_index or 0):
        raise ValueError(
            f"start_index override {start_idx} != continuous result start {result.start_index}"
        )

    window = candles[start_idx:]
    analysis = analyze_trade(
        result,
        variant="baseline_root_cause",
        long_add_pct=0.5,
        target_profit_usdt=0.015,
        window_candles=window,
        valid=True,
        skip_reason="ok",
    )

    snapshots = build_cycle_snapshots(result=result, candles=candles, start_index=start_idx)
    reachability = build_exit_reachability_by_cycle(
        result=result, candles=candles, start_index=start_idx, snapshots=snapshots
    )
    exposure = build_exposure_growth_by_cycle(snapshots)
    timeline = build_event_timeline(result=result, start_index=start_idx)
    pnl_rows = pnl_reconciliation_rows(result)
    replay_rows = build_fill_replay_rows(result, start_index=start_idx)

    markers = analyze_root_cause_markers(
        snapshots=snapshots,
        reachability=reachability,
        exposure_rows=exposure,
        result=result,
        candles=candles,
        start_index=start_idx,
    )
    recovery_state = build_recovery_start_state(
        coin=coin_upper,
        trade_id=int(trade_id),
        start_index=start_idx,
        snapshots=snapshots,
        markers=markers,
        replay_rows=replay_rows,
    )
    parity = check_baseline_parity(
        coin=coin_upper, trade_id=int(trade_id), result=result, analysis=analysis
    )
    selected = selected_trade_payload(
        coin=coin_upper,
        trade_id=int(trade_id),
        result=result,
        meta=meta,
        analysis=analysis,
        markers=markers,
        parity=parity,
    )

    _write_json(output_dir / "selected_trade.json", selected)
    _write_json(output_dir / "healthy_escalation_no_return.json", markers)
    _write_json(output_dir / "selected_recovery_start_state.json", recovery_state)
    _write_csv(output_dir / "event_timeline.csv", timeline)
    _write_csv(output_dir / "cycle_snapshots.csv", snapshots)
    _write_csv(output_dir / "exit_reachability_by_cycle.csv", reachability)
    _write_csv(output_dir / "exposure_growth_by_cycle.csv", exposure)
    _write_csv(output_dir / "pnl_reconciliation.csv", pnl_rows)
    write_code_path_map(output_dir / "code_path_map.md")
    write_report(
        output_dir / "REPORT.md",
        selected=selected,
        markers=markers,
        recovery_state=recovery_state,
        parity=parity,
    )
    _write_json(
        output_dir / "applied_params.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "coin": coin_upper,
            "trade_id": int(trade_id),
            "start_index": start_idx,
            "candle_limit": candle_limit,
            "baseline_only": True,
            "policies_disabled": selected.get("policy_disabled"),
            "baseline_parity": parity,
            "git": _git_status(),
        },
    )

    if not parity.get("skipped") and not parity.get("ok"):
        raise RuntimeError(f"baseline parity failed: {parity}")

    return {
        "ok": True,
        "selected": selected,
        "markers": markers,
        "parity": parity,
        "output_dir": str(output_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default=APT_TRADE3_COIN, help="Symbol (default APTUSDT)")
    parser.add_argument("--trade-id", type=int, default=APT_TRADE3_ID, help="Continuous trade number")
    parser.add_argument(
        "--output-dir",
        "--output-root",
        dest="output_dir",
        type=Path,
        default=DEFAULT_OUT,
        help="Output directory (must be empty; protected dirs refused)",
    )
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument(
        "--start-index",
        type=int,
        default=None,
        help="Optional override; must match continuous trade start if set",
    )
    args = parser.parse_args()

    payload = run_audit(
        coin=args.coin,
        trade_id=int(args.trade_id),
        output_dir=args.output_dir,
        candle_limit=int(args.candle_limit),
        start_index=args.start_index,
    )
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
