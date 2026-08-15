"""Research-only L0/L1 long-primary baseline notional stage-TP audit.

L0: 100/50 USDT (must reproduce protected baseline exactly).
L1: 1000/500 USDT — same semantics, larger size to activate stage/partial TPs.

No freeze/recovery/S2 policies. No live config writes. No commit.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.historical_backtest import normalize_candles
from research.backtests.long_baseline_notional_stage_tp import (
    L0_LONG_NOTIONAL,
    L0_SHORT_NOTIONAL,
    L1_LONG_NOTIONAL,
    L1_SHORT_NOTIONAL,
    build_baseline_call_kwargs,
    build_blocker_comparison,
    capital_normalized_summary,
    check_l0_parity,
    classify_path_divergence,
    extract_stage_tp_attempts,
    extract_stage_tp_fills,
    freeze_guard_inactive,
    min_notional_rejection_rows,
    select_case_study_trades,
    start_parity_row,
    summarize_stage_comparison,
    summarize_variant,
    trade_row_from_result,
)
from research.backtests.long_add_multistart_metrics import safe_float
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    CONFIG_SOURCE,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
    load_baseline_coin_list,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/long_baseline_1000_500_stage_tp_audit_20260721"
PROTECTED = (
    BASELINE_DIR,
    ROOT / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720",
    ROOT / "research/backtests/results/dual_independent_long_short_s2_audit_20260721",
)


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None, "status_porcelain": ""}
    try:
        status["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        status["dirty"] = bool(porcelain.strip())
        status["status_porcelain"] = porcelain
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


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
            out = {}
            for key in fields:
                val = row.get(key)
                if isinstance(val, (dict, list)):
                    out[key] = json.dumps(val, default=str)
                else:
                    out[key] = val
            writer.writerow(out)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _resolve_exchange_rules() -> dict[str, float]:
    load = resolve_backtest_config(config_source=CONFIG_SOURCE, signal="long", symbol="BTCUSDT")
    cfg = load.config
    return {
        "min_notional_usdt": float(getattr(cfg, "min_notional_usdt", 5.0) or 5.0),
        "min_order_qty": float(getattr(cfg, "min_order_qty", 0.0) or 0.0),
        "qty_step": float(getattr(cfg, "qty_step", 0.0) or 0.0),
    }


def run_variant_for_coin(
    *,
    symbol: str,
    candles: list[Any],
    base_notional: float,
    variant: str,
    exchange_rules: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    payload = run_continuous_reentry_backtests(
        **build_baseline_call_kwargs(symbol=symbol, candles=candles, base_notional_usdt=base_notional)
    )
    results: list[BacktestResult] = list(payload.get("results") or [])
    trade_rows: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []

    long_notional = base_notional
    for result in results:
        if not freeze_guard_inactive(result):
            raise RuntimeError(f"{symbol}/{variant}: freeze guard active — baseline violation")
        row = trade_row_from_result(
            coin=symbol,
            variant=variant,
            result=result,
            candles=candles,
            long_notional=long_notional,
            long_add_pct=LONG_FILL_DISTANCE_PCT,
            target_profit_usdt=TARGET_PROFIT_USDT,
        )
        trade_rows.append(row)
        tn = int(result.trade_number or 0)
        attempts.extend(
            extract_stage_tp_attempts(
                coin=symbol,
                variant=variant,
                trade_number=tn,
                result=result,
                exchange_min_notional=exchange_rules["min_notional_usdt"],
                exchange_min_qty=exchange_rules["min_order_qty"],
                qty_step=exchange_rules["qty_step"],
            )
        )
        fills.extend(
            extract_stage_tp_fills(coin=symbol, variant=variant, trade_number=tn, result=result)
        )
    return trade_rows, attempts, fills


def write_case_studies(path: Path, picks: dict[str, dict[str, Any]]) -> None:
    lines = [
        "# Case Studies — L0 (100/50) vs L1 (1000/500)",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
    ]

    def _section(title: str, key: str) -> None:
        pick = picks.get(key)
        if not pick:
            lines.append(f"## {title}")
            lines.append("")
            lines.append("_No matching trade found._")
            lines.append("")
            return
        lines.append(f"## {title}")
        lines.append("")
        if "L0" in pick and "L1" in pick and isinstance(pick["L0"], dict) and "trade_number" in (pick["L0"] or {}):
            for label in ("L0", "L1"):
                r = pick[label]
                if not r:
                    continue
                lines.append(
                    f"- **{label}** {r.get('coin')} trade {r.get('trade_number')}: "
                    f"status={r.get('status')} max_cycle={r.get('max_cycle')} "
                    f"mtm={safe_float(r.get('mtm_pnl')):.4f} duration={r.get('duration_candles')}"
                )
        elif "L0" in pick and "cycle" in (pick.get("L0") or {}):
            lines.append(f"- L0 rejected cycle {pick['L0'].get('cycle')} on {pick['L0'].get('coin')} "
                           f"T{pick['L0'].get('trade_id')}: {pick['L0'].get('rejection_reason')}")
            lines.append(f"- L1 accepted {pick['L1'].get('actual_stage_count')} stages, "
                           f"fills={pick['L1'].get('stage_fill_count')}")
        if pick.get("path"):
            pr = pick["path"]
            lines.append(
                f"- Path: classification={pr.get('classification')} "
                f"first_divergence={pr.get('first_divergence_candle')} "
                f"L0_norm={safe_float(pr.get('L0_normalized_per_100')):.4f} "
                f"L1_norm={safe_float(pr.get('L1_normalized_per_100')):.4f}"
            )
        lines.append("")

    _section("1. L0 Stage-TP rejected / L1 accepted", "l0_reject_l1_accept")
    _section("2. Better than linear scaling (normalized)", "better_than_linear")
    _section("3. Worse than linear scaling (normalized)", "worse_than_linear")
    _section("4. APTUSDT blocker", "blocker_APTUSDT")
    _section("5. UNIUSDT blocker", "blocker_UNIUSDT")
    _section("6. BTCUSDT blocker (first trade)", "blocker_BTCUSDT")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(
    path: Path,
    *,
    l0_summary: dict[str, Any],
    l1_summary: dict[str, Any],
    l0_parity: dict[str, Any],
    stage_cmp: dict[str, Any],
    capital: list[dict[str, Any]],
    start_rows: list[dict[str, Any]],
    path_rows: list[dict[str, Any]],
    blocker_rows: list[dict[str, Any]],
) -> None:
    l0s = stage_cmp.get("L0") or {}
    l1s = stage_cmp.get("L1") or {}
    cap_l0 = next((c for c in capital if c.get("variant") == "L0"), {})
    cap_l1 = next((c for c in capital if c.get("variant") == "L1"), {})
    start_ok = sum(int(r.get("start_parity_pass") or 0) for r in start_rows)
    div_counts: dict[str, int] = {}
    for row in path_rows:
        cls = str(row.get("classification") or "unknown")
        div_counts[cls] = div_counts.get(cls, 0) + 1

    stage_answer = (
        "Ja — L0 hatte überwiegend Fallbacks/keine akzeptierten Stage-Orders; "
        "L1 aktivierte Stage-/Partial-TPs."
        if l0s.get("staged_orders_accepted", 0) == 0 and l1s.get("staged_orders_accepted", 0) > 0
        else (
            "Teilweise — L1 hat mehr akzeptierte Stage-Orders als L0."
            if l1s.get("staged_orders_accepted", 0) > l0s.get("staged_orders_accepted", 0)
            else "Nein — kein klarer Unterschied in Stage-Aktivierung."
        )
    )

    lines = [
        "# Long-Primary Baseline L0/L1 Stage-TP Audit",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- L0: Long {L0_LONG_NOTIONAL} / Short {L0_SHORT_NOTIONAL} USDT (live baseline parity)",
        f"- L1: Long {L1_LONG_NOTIONAL} / Short {L1_SHORT_NOTIONAL} USDT (10× size, same % rules)",
        f"- Coins: 27 from protected baseline manifest",
        f"- No freeze / recovery / S2 / exit-rebuild policy overrides",
        f"- Fill model: conservative, config_source=live",
        "",
        "## L0 parity",
        "",
        f"- **PASS**" if l0_parity.get("ok") else "**FAIL**",
        "",
        "| check | actual | expected | ok |",
        "|---|---:|---:|:---:|",
    ]
    for name, (actual, expected, ok) in l0_parity.get("checks", {}).items():
        lines.append(f"| {name} | {actual} | {expected} | {ok} |")

    lines.extend(
        [
            "",
            "## Stage-/Partial-TP audit",
            "",
            f"> **{stage_answer}**",
            "",
            "| metric | L0 | L1 |",
            "|---|---:|---:|",
            f"| staged_order_attempts | {l0s.get('staged_order_attempts')} | {l1s.get('staged_order_attempts')} |",
            f"| staged_orders_accepted | {l0s.get('staged_orders_accepted')} | {l1s.get('staged_orders_accepted')} |",
            f"| rejected_min_notional | {l0s.get('staged_orders_rejected_min_notional')} | {l1s.get('staged_orders_rejected_min_notional')} |",
            f"| full_qty_fallback | {l0s.get('full_qty_fallback_count')} | {l1s.get('full_qty_fallback_count')} |",
            f"| partial_tp_fills | {l0s.get('partial_tp_fills')} | {l1s.get('partial_tp_fills')} |",
            f"| cycles_with_active_partial | {l0s.get('cycles_with_active_partial_tp')} | {l1s.get('cycles_with_active_partial_tp')} |",
            f"| realized_stage_pnl | {l0s.get('realized_stage_pnl_usdt'):.4f} | {l1s.get('realized_stage_pnl_usdt'):.4f} |",
            "",
            "## Variant summaries (raw)",
            "",
            "| | L0 | L1 |",
            "|---|---:|---:|",
            f"| trades_started | {l0_summary.get('trades_started')} | {l1_summary.get('trades_started')} |",
            f"| trades_closed | {l0_summary.get('trades_closed')} | {l1_summary.get('trades_closed')} |",
            f"| open_blockers | {l0_summary.get('open_blocker_count')} | {l1_summary.get('open_blocker_count')} |",
            f"| closed_pnl_usdt | {safe_float(l0_summary.get('closed_pnl_usdt')):.4f} | {safe_float(l1_summary.get('closed_pnl_usdt')):.4f} |",
            f"| final_open_mtm_usdt | {safe_float(l0_summary.get('final_open_mtm_usdt')):.4f} | {safe_float(l1_summary.get('final_open_mtm_usdt')):.4f} |",
            f"| total_series_mtm_usdt | {safe_float(l0_summary.get('total_series_mtm_usdt')):.4f} | {safe_float(l1_summary.get('total_series_mtm_usdt')):.4f} |",
            f"| max_cycle | {l0_summary.get('maximum_cycle_reached')} | {l1_summary.get('maximum_cycle_reached')} |",
            f"| invalid_partial | {l0_summary.get('invalid_partial_cycle_count')} | {l1_summary.get('invalid_partial_cycle_count')} |",
            "",
            "## Capital normalized (per 100 USDT initial long)",
            "",
            f"| | L0 | L1 |",
            f"|---|---:|---:|",
            f"| normalized_closed_pnl | {safe_float(cap_l0.get('normalized_closed_pnl_per_100_long')):.4f} | {safe_float(cap_l1.get('normalized_closed_pnl_per_100_long')):.4f} |",
            f"| normalized_open_mtm | {safe_float(cap_l0.get('normalized_open_mtm_per_100_long')):.4f} | {safe_float(cap_l1.get('normalized_open_mtm_per_100_long')):.4f} |",
            f"| normalized_total_result | {safe_float(cap_l0.get('normalized_total_result_per_100_long')):.4f} | {safe_float(cap_l1.get('normalized_total_result_per_100_long')):.4f} |",
            f"| gross_normalized_result | {safe_float(cap_l0.get('gross_normalized_result_per_100_gross')):.4f} | {safe_float(cap_l1.get('gross_normalized_result_per_100_gross')):.4f} |",
            "",
            "## Start parity",
            "",
            f"- {start_ok}/{len(start_rows)} coins: first_entry_index L0 == L1",
            "",
            "## Path divergence classification",
            "",
        ]
    )
    for cls, count in sorted(div_counts.items()):
        lines.append(f"- `{cls}`: {count} trades")
    lines.extend(
        [
            "",
            "## Abschlussfragen",
            "",
            f"1. **Reproduziert L0 die Baseline exakt?** {'Ja' if l0_parity.get('ok') else 'Nein — siehe Parity-Tabelle'}.",
            f"2. **L1 Trades/Closed/Blocker:** {l1_summary.get('trades_started')} / {l1_summary.get('trades_closed')} / {l1_summary.get('open_blocker_count')}.",
            f"3. **L1 roh:** closed={safe_float(l1_summary.get('closed_pnl_usdt')):.4f}, open={safe_float(l1_summary.get('final_open_mtm_usdt')):.4f}, total={safe_float(l1_summary.get('total_series_mtm_usdt')):.4f}.",
            f"4. **Kapitalnormalisiert L1 vs L0 total/100:** {safe_float(cap_l1.get('normalized_total_result_per_100_long')):.4f} vs {safe_float(cap_l0.get('normalized_total_result_per_100_long')):.4f}.",
            f"5. **L0 Stage inaktiv (Min-Notional):** {l0s.get('staged_orders_rejected_min_notional')} rejections, {l0s.get('full_qty_fallback_count')} fallbacks.",
            f"6. **L1 Stage gesetzt/gefüllt:** {l1s.get('staged_orders_accepted')} accepted, {l1s.get('partial_tp_fills')} partial fills.",
            f"7. **Verbessern Teilprofite Blocker-Struktur?** Siehe `blocker_comparison.csv` — vergleiche highest_cycle, net_exposure, duration.",
            f"8. **Blocker-Anzahl/Größe:** L0={l0_summary.get('open_blocker_count')} vs L1={l1_summary.get('open_blocker_count')}; open MTM L0={safe_float(l0_summary.get('final_open_mtm_usdt')):.2f} vs L1={safe_float(l1_summary.get('final_open_mtm_usdt')):.2f}.",
            f"9. **Mehr als ×10-Skalierung?** {sum(1 for r in path_rows if r.get('classification') != 'pure_linear_scaling')} / {len(path_rows)} trades divergieren.",
            "10. **Konkrete Pfadänderungen:** siehe `path_divergence.csv` und `case_studies.md`.",
            f"11. **Neue Undercoverage:** L0={l0_summary.get('undercoverage_count')} L1={l1_summary.get('undercoverage_count')}.",
            "12. **Keine S2/Recovery/Runtime-Empfehlung** — reiner Baseline-Size-Audit.",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--coins", nargs="*", default=None)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--skip-l0-parity-abort", action="store_true")
    args = parser.parse_args()

    out: Path = args.out
    if out.resolve() in {p.resolve() for p in PROTECTED}:
        raise SystemExit(f"Refusing protected dir: {out}")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"Refusing non-empty output: {out}")
    out.mkdir(parents=True, exist_ok=True)

    coins = list(args.coins) if args.coins else load_baseline_coin_list()
    exchange_rules = _resolve_exchange_rules()

    all_l0: list[dict[str, Any]] = []
    all_l1: list[dict[str, Any]] = []
    l0_attempts_all: list[dict[str, Any]] = []
    l1_attempts_all: list[dict[str, Any]] = []
    l0_fills_all: list[dict[str, Any]] = []
    l1_fills_all: list[dict[str, Any]] = []
    start_rows: list[dict[str, Any]] = []
    coin_summary_rows: list[dict[str, Any]] = []

    print(f"[stage-tp-audit] {len(coins)} coins...", flush=True)
    for symbol in coins:
        print(f"[stage-tp-audit] {symbol}", flush=True)
        candles = normalize_candles(symbol, load_candles_for_symbol(symbol, limit=int(args.candle_limit)))

        l0_rows, l0_attempts, l0_fills = run_variant_for_coin(
            symbol=symbol,
            candles=candles,
            base_notional=L0_LONG_NOTIONAL,
            variant="L0",
            exchange_rules=exchange_rules,
        )
        l1_rows, l1_attempts, l1_fills = run_variant_for_coin(
            symbol=symbol,
            candles=candles,
            base_notional=L1_LONG_NOTIONAL,
            variant="L1",
            exchange_rules=exchange_rules,
        )

        all_l0.extend(l0_rows)
        all_l1.extend(l1_rows)
        l0_attempts_all.extend(l0_attempts)
        l1_attempts_all.extend(l1_attempts)
        l0_fills_all.extend(l0_fills)
        l1_fills_all.extend(l1_fills)

        start_rows.append(
            start_parity_row(
                coin=symbol,
                candles=candles,
                l0_first=l0_rows[0] if l0_rows else None,
                l1_first=l1_rows[0] if l1_rows else None,
            )
        )
        coin_summary_rows.append(
            {
                "coin": symbol,
                "L0_trades": len(l0_rows),
                "L1_trades": len(l1_rows),
                "L0_blocker": sum(int(r.get("is_blocker") or 0) for r in l0_rows),
                "L1_blocker": sum(int(r.get("is_blocker") or 0) for r in l1_rows),
                "L0_series_mtm": sum(safe_float(r.get("mtm_pnl")) for r in l0_rows),
                "L1_series_mtm": sum(safe_float(r.get("mtm_pnl")) for r in l1_rows),
                "start_parity_pass": start_rows[-1]["start_parity_pass"],
            }
        )

    l0_summary = summarize_variant(all_l0, variant="L0", long_notional=L0_LONG_NOTIONAL, short_notional=L0_SHORT_NOTIONAL)
    l1_summary = summarize_variant(all_l1, variant="L1", long_notional=L1_LONG_NOTIONAL, short_notional=L1_SHORT_NOTIONAL)
    l0_parity = check_l0_parity(l0_summary)
    print(f"[stage-tp-audit] L0 parity: {l0_parity['ok']}", flush=True)
    if not l0_parity.get("ok") and not args.skip_l0_parity_abort:
        _write_json(out / "l0_parity_failure.json", l0_parity)
        raise SystemExit("L0 failed baseline parity — aborting before writing full report")

    stage_cmp = summarize_stage_comparison(l0_attempts_all, l1_attempts_all, l0_fills_all, l1_fills_all)
    capital = capital_normalized_summary(l0_summary, l1_summary)

    path_rows: list[dict[str, Any]] = []
    trade_pairing: list[dict[str, Any]] = []
    blocker_rows: list[dict[str, Any]] = []
    l0_by_coin: dict[str, list[dict[str, Any]]] = {}
    l1_by_coin: dict[str, list[dict[str, Any]]] = {}
    for row in all_l0:
        l0_by_coin.setdefault(row["coin"], []).append(row)
    for row in all_l1:
        l1_by_coin.setdefault(row["coin"], []).append(row)

    for coin in coins:
        l0_coin = l0_by_coin.get(coin, [])
        l1_coin = l1_by_coin.get(coin, [])
        l0_att = [a for a in l0_attempts_all if a["coin"] == coin]
        l1_att = [a for a in l1_attempts_all if a["coin"] == coin]
        max_tn = max(
            [int(r.get("trade_number") or 0) for r in l0_coin]
            + [int(r.get("trade_number") or 0) for r in l1_coin]
            + [0]
        )
        for tn in range(1, max_tn + 1):
            l0r = next((r for r in l0_coin if int(r.get("trade_number") or 0) == tn), None)
            l1r = next((r for r in l1_coin if int(r.get("trade_number") or 0) == tn), None)
            if not l0r and not l1r:
                continue
            trade_pairing.append(
                {
                    "coin": coin,
                    "trade_number": tn,
                    "L0_present": int(l0r is not None),
                    "L1_present": int(l1r is not None),
                    "L0_status": (l0r or {}).get("status"),
                    "L1_status": (l1r or {}).get("status"),
                    "L0_mtm": (l0r or {}).get("mtm_pnl"),
                    "L1_mtm": (l1r or {}).get("mtm_pnl"),
                }
            )
            if l0r and l1r:
                path_rows.append(
                    classify_path_divergence(
                        coin=coin,
                        trade_number=tn,
                        l0_row=l0r,
                        l1_row=l1r,
                        l0_attempts=l0_att,
                        l1_attempts=l1_att,
                    )
                )

    for coin in coins:
        bc = build_blocker_comparison(
            coin=coin,
            l0_rows=l0_by_coin.get(coin, []),
            l1_rows=l1_by_coin.get(coin, []),
            l0_attempts=[a for a in l0_attempts_all if a["coin"] == coin],
            l1_attempts=[a for a in l1_attempts_all if a["coin"] == coin],
        )
        if bc:
            blocker_rows.append(bc)

    picks = select_case_study_trades(
        l0_rows=all_l0,
        l1_rows=all_l1,
        l0_attempts=l0_attempts_all,
        l1_attempts=l1_attempts_all,
        path_rows=path_rows,
    )

    def _strip_logs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k not in {"fill_log", "intent_log"}} for r in rows]

    _write_csv(out / "variant_summary.csv", [l0_summary, l1_summary])
    _write_csv(out / "coin_summary.csv", coin_summary_rows)
    _write_csv(out / "trade_pairing.csv", trade_pairing)
    _write_csv(out / "trade_details_l0.csv", _strip_logs(all_l0))
    _write_csv(out / "trade_details_l1.csv", _strip_logs(all_l1))
    _write_csv(out / "stage_tp_attempts.csv", l0_attempts_all + l1_attempts_all)
    _write_csv(out / "stage_tp_fills.csv", l0_fills_all + l1_fills_all)
    _write_csv(out / "min_notional_rejections.csv", min_notional_rejection_rows(l0_attempts_all + l1_attempts_all))
    _write_csv(out / "blocker_comparison.csv", blocker_rows)
    _write_csv(out / "path_divergence.csv", path_rows)
    _write_csv(out / "capital_normalized_summary.csv", capital)
    _write_csv(out / "start_parity.csv", start_rows)
    write_case_studies(out / "case_studies.md", picks)
    write_report(
        out / "REPORT.md",
        l0_summary=l0_summary,
        l1_summary=l1_summary,
        l0_parity=l0_parity,
        stage_cmp=stage_cmp,
        capital=capital,
        start_rows=start_rows,
        path_rows=path_rows,
        blocker_rows=blocker_rows,
    )
    _write_json(
        out / "applied_params.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "L0": {"long_notional": L0_LONG_NOTIONAL, "short_notional": L0_SHORT_NOTIONAL},
            "L1": {"long_notional": L1_LONG_NOTIONAL, "short_notional": L1_SHORT_NOTIONAL},
            "config_source": CONFIG_SOURCE,
            "fill_model": "conservative",
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "candle_limit": args.candle_limit,
            "coins": coins,
            "exchange_rules": exchange_rules,
            "policies_disabled": [
                "inventory_mtm_freeze",
                "safe_cycle_boundary",
                "recovery_reentry",
                "exit_rebuild_policy",
                "blocker_recovery",
            ],
            "l0_parity": l0_parity,
            "stage_comparison": stage_cmp,
            "git": _git_status(),
        },
    )
    print(f"[stage-tp-audit] done -> {out}", flush=True)


if __name__ == "__main__":
    main()
