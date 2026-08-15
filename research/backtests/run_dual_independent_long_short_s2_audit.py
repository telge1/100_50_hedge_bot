"""Research-only dual independent Long-S2 + Short-S2 continuous audit (D0/D1/D2).

D0: Long-primary S2 + B1 terminal — reproduces prior S2 Kennzahlen.
D1: Short-primary S2 full continuous (no B1; no short baseline-blocker corpus).
D2: Independent parallel books — Long D0 series + Short D1 series on same candles.

No live/config/runtime edits. No commit. Refuses non-empty output overwrite.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.blocker_recovery_trigger_policy import terminal_recovery_config
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.dual_independent_long_short_s2 import (
    LONG_INITIAL_NOTIONAL_USDT,
    SHORT_INITIAL_NOTIONAL_USDT,
    S2_REFERENCE_RECOVERED,
    build_combined_equity_curve,
    build_s2_freeze_config,
    check_d0_s2_parity,
    coin_combined_summary,
    opener_classification_ok,
    select_case_study_coins,
    shared_initial_entry_row,
    summarize_side_trades,
    trade_row_from_result,
    validate_independent_reentry_offsets,
)
from research.backtests.historical_backtest import normalize_candles
from research.backtests.independent_continuous_long_short_analysis import merge_timeline
from research.backtests.inventory_mtm_freeze import InventoryMtmFreezeConfig
from research.backtests.long_add_multistart_metrics import safe_float
from research.backtests.recovery_reentry_policy import (
    baseline_blocker_trade_number_by_coin,
    load_baseline_blockers,
)
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    CONFIG_SOURCE,
    CONTINUOUS_START_INDEX,
    FILL_MODEL,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
    load_baseline_coin_list,
)

ROOT = Path(__file__).resolve().parents[2]
PRIOR_S2_DIR = ROOT / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720"
DEFAULT_OUT = ROOT / "research/backtests/results/dual_independent_long_short_s2_audit_20260721"
PROTECTED = (
    BASELINE_DIR,
    PRIOR_S2_DIR,
    ROOT / "research/backtests/results/inventory_mtm_neg1_policy_audit_20260720",
    ROOT / "research/backtests/results/blocker_recovery_trigger_and_hybrid_audit_20260720",
)


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        status["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
        status["dirty"] = bool(porcelain.strip())
        status["status_porcelain"] = porcelain
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key == "fill_log":
                continue
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


def _config_dict(cfg: InventoryMtmFreezeConfig) -> dict[str, Any]:
    return {f: getattr(cfg, f) for f in cfg.__dataclass_fields__}  # type: ignore[attr-defined]


def run_side_series(
    *,
    symbol: str,
    direction: str,
    candles: list[Any],
    freeze_config: InventoryMtmFreezeConfig,
    recovery_reentry_config: Any,
    variant_label: str,
) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "symbol": symbol,
        "direction": direction,
        "candles": candles,
        "continuous_start_index": CONTINUOUS_START_INDEX,
        "config_source": CONFIG_SOURCE,
        "fill_model": FILL_MODEL,
        "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        "target_profit_usdt": TARGET_PROFIT_USDT,
        "inventory_mtm_freeze_config": freeze_config,
        "recovery_reentry_config": recovery_reentry_config,
        "write_json": False,
        "write_csv": False,
    }
    if direction == "long":
        kwargs["long_fill_distance_pct"] = LONG_FILL_DISTANCE_PCT
    payload = run_continuous_reentry_backtests(**kwargs)
    rows = []
    for result in payload["results"]:
        rows.append(
            trade_row_from_result(
                coin=symbol,
                side=direction,
                variant=variant_label,
                result=result,
                candles=candles,
                long_add_pct=LONG_FILL_DISTANCE_PCT if direction == "long" else 0.5,
                target_profit_usdt=TARGET_PROFIT_USDT,
            )
        )
    return rows


def write_case_studies(
    path: Path,
    *,
    picks: dict[str, str | None],
    coin_rows: list[dict[str, Any]],
    long_trades: list[dict[str, Any]],
    short_trades: list[dict[str, Any]],
) -> None:
    by_coin = {r["coin"]: r for r in coin_rows}
    lines = [
        "# Dual Independent Long/Short S2 — Case Studies",
        "",
        "Independent books; no shared margin. Equity reconstructed from fill_log + candle closes.",
        "",
    ]

    def section(title: str, coin: str | None) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not coin or coin not in by_coin:
            lines.append("_Kein passender Coin gefunden._")
            lines.append("")
            return
        row = by_coin[coin]
        lines.append(f"**Coin:** `{coin}`")
        lines.append("")
        lines.append("| metric | value |")
        lines.append("|---|---:|")
        for key in (
            "long_trades",
            "short_trades",
            "long_closed_pnl",
            "short_closed_pnl",
            "long_open_mtm",
            "short_open_mtm",
            "combined_total_result",
            "long_blocker_count",
            "short_blocker_count",
            "short_contribution",
            "max_combined_drawdown",
            "final_combined_equity",
        ):
            lines.append(f"| {key} | {row.get(key)} |")
        lt = [t for t in long_trades if t["coin"] == coin]
        st = [t for t in short_trades if t["coin"] == coin]
        lines.append("")
        lines.append(
            f"Long blockers: {[t['trade_number'] for t in lt if t.get('is_blocker')]}; "
            f"Short blockers: {[t['trade_number'] for t in st if t.get('is_blocker')]}."
        )
        lines.append("")

    section("1. Long blockiert, Short profitabel", picks.get("long_blocks_short_profits"))
    section("2. Short blockiert, Long profitabel", picks.get("short_blocks_long_profits"))
    section("3. Beide blockieren / hoher kombinierter Drawdown", picks.get("both_block_or_high_dd"))
    section("4. APTUSDT", "APTUSDT")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(
    path: Path,
    *,
    d0_parity: dict[str, Any],
    d0: dict[str, Any],
    d1: dict[str, Any],
    d2_totals: dict[str, Any],
    capital: dict[str, Any],
    coin_rows: list[dict[str, Any]],
) -> None:
    both = sum(int(r.get("both_sides_block") or 0) for r in coin_rows)
    only_long = sum(int(r.get("only_long_block") or 0) for r in coin_rows)
    only_short = sum(int(r.get("only_short_block") or 0) for r in coin_rows)
    none_b = sum(int(r.get("no_blocker") or 0) for r in coin_rows)
    short_covers = sum(int(r.get("short_covers_long_blocker_mtm") or 0) for r in coin_rows)
    long_block_coins = sum(1 for r in coin_rows if int(r.get("long_blocker_count") or 0) > 0)

    lines = [
        "# Dual Independent Long/Short S2 Audit (D0/D1/D2)",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        "- Corpus: 27 baseline coins; shared first entry at candle 0 (no lookahead).",
        "- **D0**: Long-primary S2 + **B1 terminal** after recovered flat of original blocker "
        f"(parity vs prior S2 recovered={S2_REFERENCE_RECOVERED}).",
        "- **D1**: Short-primary S2 **full continuous** (no B1 — no short baseline-blocker map).",
        "- **D2**: D0 long series + D1 short series, independent books on same candles.",
        "- **Margin:** not simulated (separate wallets for aggregation only).",
        "- Research-only; no live/runtime changes; no commit.",
        "",
        "## D0 ↔ prior S2 parity",
        "",
        f"- Result: **{'PASS' if d0_parity.get('ok') else 'FAIL'}**",
        "",
        "| check | actual | expected | ok |",
        "|---|---:|---:|:---:|",
    ]
    for name, (actual, expected, ok) in (d0_parity.get("checks") or {}).items():
        lines.append(f"| {name} | {actual} | {expected} | {ok} |")

    lines.extend(
        [
            "",
            "## Variant totals",
            "",
            "| variant | series_mtm | closed_pnl | open_mtm | trades | closed | blockers | invalid_partial |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
            f"| D0 Long-S2 | {safe_float(d0.get('total_series_mtm_usdt')):.4f} | "
            f"{safe_float(d0.get('closed_pnl_usdt')):.4f} | {safe_float(d0.get('final_open_mtm_usdt')):.4f} | "
            f"{d0.get('trades_started')} | {d0.get('trades_closed')} | {d0.get('open_blocker_count')} | "
            f"{d0.get('invalid_partial_cycle_count')} |",
            f"| D1 Short-S2 | {safe_float(d1.get('total_series_mtm_usdt')):.4f} | "
            f"{safe_float(d1.get('closed_pnl_usdt')):.4f} | {safe_float(d1.get('final_open_mtm_usdt')):.4f} | "
            f"{d1.get('trades_started')} | {d1.get('trades_closed')} | {d1.get('open_blocker_count')} | "
            f"{d1.get('invalid_partial_cycle_count')} |",
            f"| D2 Combined | {safe_float(d2_totals.get('combined_total_result')):.4f} | "
            f"{safe_float(d2_totals.get('combined_closed_pnl')):.4f} | "
            f"{safe_float(d2_totals.get('combined_open_mtm')):.4f} | "
            f"{int(d0.get('trades_started') or 0)+int(d1.get('trades_started') or 0)} | "
            f"{int(d0.get('trades_closed') or 0)+int(d1.get('trades_closed') or 0)} | "
            f"{int(d0.get('open_blocker_count') or 0)+int(d1.get('open_blocker_count') or 0)} | "
            f"{int(d0.get('invalid_partial_cycle_count') or 0)+int(d1.get('invalid_partial_cycle_count') or 0)} |",
            "",
            "## Capital normalization",
            "",
            f"- Long initial notional: **{LONG_INITIAL_NOTIONAL_USDT}** USDT",
            f"- Short initial notional: **{SHORT_INITIAL_NOTIONAL_USDT}** USDT",
            f"- Combined initial: **{LONG_INITIAL_NOTIONAL_USDT + SHORT_INITIAL_NOTIONAL_USDT}** USDT",
            f"- D0 result / 100 USDT: **{safe_float(capital.get('d0_per_100')):.4f}**",
            f"- D1 result / 100 USDT: **{safe_float(capital.get('d1_per_100')):.4f}**",
            f"- D2 result / 100 USDT combined notional: **{safe_float(capital.get('d2_per_100')):.4f}**",
            "",
            "## Blocker pairing",
            "",
            f"- Both sides block: **{both}** coins",
            f"- Only long block: **{only_long}**",
            f"- Only short block: **{only_short}**",
            f"- No blocker: **{none_b}**",
            f"- Short covers long-blocker MTM (coin-level heuristic): **{short_covers}** / {long_block_coins}",
            "",
            "## Abschlussfragen",
            "",
            f"1. **Long-S2 allein (D0):** series_mtm={safe_float(d0.get('total_series_mtm_usdt')):.4f} USDT.",
            f"2. **Short-S2 allein (D1):** series_mtm={safe_float(d1.get('total_series_mtm_usdt')):.4f} USDT; "
            f"profitabel={'ja' if safe_float(d1.get('total_series_mtm_usdt')) > 0 else 'nein'}.",
            f"3. **Kombiniert (D2):** {safe_float(d2_totals.get('combined_total_result')):.4f} USDT "
            f"(Δ vs Long={safe_float(d2_totals.get('combined_delta_vs_long_only')):.4f}).",
            f"4. **Pro 100 USDT Startkapital:** D0={safe_float(capital.get('d0_per_100')):.4f}, "
            f"D1={safe_float(capital.get('d1_per_100')):.4f}, D2={safe_float(capital.get('d2_per_100')):.4f}.",
            f"5. **Trades/Blocker:** Long {d0.get('trades_started')}/{d0.get('open_blocker_count')}; "
            f"Short {d1.get('trades_started')}/{d1.get('open_blocker_count')}.",
            f"6. **Closed / Open:** Long closed={safe_float(d0.get('closed_pnl_usdt')):.4f} "
            f"open={safe_float(d0.get('final_open_mtm_usdt')):.4f}; "
            f"Short closed={safe_float(d1.get('closed_pnl_usdt')):.4f} "
            f"open={safe_float(d1.get('final_open_mtm_usdt')):.4f}.",
            f"7. **Long-Blocker durch Short kompensiert (Heuristik):** {short_covers} Coins.",
            f"8. **Coins mit Blocker auf beiden Seiten:** {both}.",
            f"9. **Robuster als Long allein?** "
            f"{'Ja auf Roh-PnL' if safe_float(d2_totals.get('combined_total_result')) > safe_float(d0.get('total_series_mtm_usdt')) else 'Nein auf Roh-PnL'}; "
            f"nach Kapitalnormalisierung D2/100={safe_float(capital.get('d2_per_100')):.4f} vs "
            f"D0/100={safe_float(capital.get('d0_per_100')):.4f}.",
            "10. **Portfolio-/Regime-Scanner als nächster Schritt?** Nur wenn nach Normalisierung "
            "und Equity-Drawdown die Short-Serie zeitlich kompensiert — siehe `combined_equity_curve.csv` "
            "/ Case Studies; nicht allein wegen höherem Roh-PnL bei mehr Kapital.",
            "11. **Keine Runtime-Empfehlung.**",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--coins", nargs="*", default=None)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument(
        "--equity-stride",
        type=int,
        default=12,
        help="Store every Nth equity row (1=all). Drawdown still uses full in-memory curve.",
    )
    args = parser.parse_args()
    out: Path = args.out
    if out.resolve() in {p.resolve() for p in PROTECTED}:
        raise SystemExit(f"Refusing protected dir: {out}")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"Refusing non-empty output: {out}")
    out.mkdir(parents=True, exist_ok=True)

    blockers = load_baseline_blockers(BASELINE_DIR / "blocker_trades.csv")
    target_map = baseline_blocker_trade_number_by_coin(blockers)
    coins = list(args.coins) if args.coins else load_baseline_coin_list()
    coins = [c for c in coins if c in target_map]
    freeze = build_s2_freeze_config()

    print(f"[dual-s2] loading {len(coins)} coins...", flush=True)
    coin_candles: dict[str, list[Any]] = {}
    for symbol in coins:
        coin_candles[symbol] = normalize_candles(
            symbol, load_candles_for_symbol(symbol, limit=int(args.candle_limit))
        )

    all_long: list[dict[str, Any]] = []
    all_short: list[dict[str, Any]] = []
    shared_entries: list[dict[str, Any]] = []
    coin_rows: list[dict[str, Any]] = []
    blocker_pairs: list[dict[str, Any]] = []
    equity_out: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []
    recovered_d0 = 0

    for symbol in coins:
        candles = coin_candles[symbol]
        target = int(target_map[symbol])
        print(f"[dual-s2] {symbol} long(D0/B1) + short(D1/continuous) target={target}", flush=True)

        long_rows = run_side_series(
            symbol=symbol,
            direction="long",
            candles=candles,
            freeze_config=freeze,
            recovery_reentry_config=terminal_recovery_config(target_blocker_trade_number=target),
            variant_label="D0",
        )
        short_rows = run_side_series(
            symbol=symbol,
            direction="short",
            candles=candles,
            freeze_config=freeze,
            recovery_reentry_config=None,
            variant_label="D1",
        )
        if any(r.get("recovered_flat_of_target_blocker") for r in long_rows):
            recovered_d0 += 1

        if len(long_rows) > 1 and not validate_independent_reentry_offsets(long_rows):
            raise RuntimeError(f"{symbol}: long reentry offset invalid")
        if len(short_rows) > 1 and not validate_independent_reentry_offsets(short_rows):
            raise RuntimeError(f"{symbol}: short reentry offset invalid")

        all_long.extend(long_rows)
        all_short.extend(short_rows)
        shared_entries.append(
            shared_initial_entry_row(
                coin=symbol,
                candles=candles,
                long_first=long_rows[0] if long_rows else None,
                short_first=short_rows[0] if short_rows else None,
            )
        )

        long_sum = summarize_side_trades(long_rows, side="long")
        short_sum = summarize_side_trades(short_rows, side="short")
        equity_rows, equity_sum = build_combined_equity_curve(
            coin=symbol, candles=candles, long_rows=long_rows, short_rows=short_rows
        )
        stride = max(1, int(args.equity_stride))
        equity_out.extend(equity_rows[::stride])
        if equity_rows and equity_rows[-1] not in equity_out:
            equity_out.append(equity_rows[-1])

        coin_row = coin_combined_summary(
            coin=symbol,
            long_summary=long_sum,
            short_summary=short_sum,
            equity_summary=equity_sum,
        )
        coin_rows.append(coin_row)
        blocker_pairs.append(
            {
                "coin": symbol,
                "long_blocker_count": coin_row["long_blocker_count"],
                "short_blocker_count": coin_row["short_blocker_count"],
                "both_sides_block": coin_row["both_sides_block"],
                "only_long_block": coin_row["only_long_block"],
                "only_short_block": coin_row["only_short_block"],
                "no_blocker": coin_row["no_blocker"],
                "short_covers_long_blocker_mtm": coin_row["short_covers_long_blocker_mtm"],
                "long_covers_short_blocker_mtm": coin_row["long_covers_short_blocker_mtm"],
                "long_open_mtm": coin_row["long_open_mtm"],
                "short_contribution": coin_row["short_contribution"],
                "max_combined_drawdown": coin_row["max_combined_drawdown"],
            }
        )
        for row in merge_timeline(
            [{k: v for k, v in r.items() if k != "fill_log"} for r in long_rows],
            [{k: v for k, v in r.items() if k != "fill_log"} for r in short_rows],
        ):
            timeline_rows.append({"coin": symbol, **row})

    d0 = summarize_side_trades(all_long, side="long")
    d1 = summarize_side_trades(all_short, side="short")
    d0["recovered_original_blockers"] = recovered_d0
    d0_parity = check_d0_s2_parity(d0)
    # Also record recovered count vs reference
    d0_parity["checks"]["recovered"] = (
        recovered_d0,
        S2_REFERENCE_RECOVERED,
        recovered_d0 == S2_REFERENCE_RECOVERED,
    )
    d0_parity["ok"] = all(c[2] for c in d0_parity["checks"].values())
    print(f"[dual-s2] D0 parity: {d0_parity['ok']}", flush=True)

    d2_totals = {
        "combined_total_result": safe_float(d0["total_series_mtm_usdt"])
        + safe_float(d1["total_series_mtm_usdt"]),
        "combined_closed_pnl": safe_float(d0["closed_pnl_usdt"]) + safe_float(d1["closed_pnl_usdt"]),
        "combined_open_mtm": safe_float(d0["final_open_mtm_usdt"])
        + safe_float(d1["final_open_mtm_usdt"]),
        "combined_delta_vs_long_only": safe_float(d1["total_series_mtm_usdt"]),
    }
    capital = {
        "long_initial_notional": LONG_INITIAL_NOTIONAL_USDT,
        "short_initial_notional": SHORT_INITIAL_NOTIONAL_USDT,
        "combined_initial_notional": LONG_INITIAL_NOTIONAL_USDT + SHORT_INITIAL_NOTIONAL_USDT,
        "d0_raw": safe_float(d0["total_series_mtm_usdt"]),
        "d1_raw": safe_float(d1["total_series_mtm_usdt"]),
        "d2_raw": safe_float(d2_totals["combined_total_result"]),
        "d0_per_100": safe_float(d0["total_series_mtm_usdt"]) / LONG_INITIAL_NOTIONAL_USDT * 100.0,
        "d1_per_100": safe_float(d1["total_series_mtm_usdt"]) / SHORT_INITIAL_NOTIONAL_USDT * 100.0,
        "d2_per_100": safe_float(d2_totals["combined_total_result"])
        / (LONG_INITIAL_NOTIONAL_USDT + SHORT_INITIAL_NOTIONAL_USDT)
        * 100.0,
        "note": (
            "Live configs: long base_notional=100, short base_notional=50. "
            "D2 deploys more capital than D0; compare per_100 columns."
        ),
    }

    variant_summary = [
        {"variant": "D0_long_s2_b1", **d0},
        {"variant": "D1_short_s2_continuous", **d1},
        {
            "variant": "D2_combined",
            "total_series_mtm_usdt": d2_totals["combined_total_result"],
            "closed_pnl_usdt": d2_totals["combined_closed_pnl"],
            "final_open_mtm_usdt": d2_totals["combined_open_mtm"],
            "combined_delta_vs_long_only": d2_totals["combined_delta_vs_long_only"],
            "trades_started": int(d0["trades_started"]) + int(d1["trades_started"]),
            "trades_closed": int(d0["trades_closed"]) + int(d1["trades_closed"]),
            "open_blocker_count": int(d0["open_blocker_count"]) + int(d1["open_blocker_count"]),
            "invalid_partial_cycle_count": int(d0["invalid_partial_cycle_count"])
            + int(d1["invalid_partial_cycle_count"]),
        },
    ]

    picks = select_case_study_coins(coin_rows)
    _write_csv(out / "variant_summary.csv", variant_summary)
    _write_csv(out / "coin_summary.csv", coin_rows)
    _write_csv(out / "long_trade_details.csv", [{k: v for k, v in r.items() if k != "fill_log"} for r in all_long])
    _write_csv(out / "short_trade_details.csv", [{k: v for k, v in r.items() if k != "fill_log"} for r in all_short])
    _write_csv(out / "combined_trade_timeline.csv", timeline_rows)
    _write_csv(out / "combined_equity_curve.csv", equity_out)
    _write_csv(out / "blocker_pairing.csv", blocker_pairs)
    _write_csv(out / "shared_initial_entries.csv", shared_entries)
    _write_csv(out / "capital_normalized_summary.csv", [capital])
    _write_csv(out / "short_opener_mapping_regression.csv", opener_classification_ok())
    write_case_studies(
        out / "case_studies.md",
        picks=picks,
        coin_rows=coin_rows,
        long_trades=all_long,
        short_trades=all_short,
    )
    write_report(
        out / "REPORT.md",
        d0_parity=d0_parity,
        d0=d0,
        d1=d1,
        d2_totals=d2_totals,
        capital=capital,
        coin_rows=coin_rows,
    )
    _write_json(
        out / "applied_params.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config_source": CONFIG_SOURCE,
            "fill_model": FILL_MODEL,
            "long_fill_distance_pct": LONG_FILL_DISTANCE_PCT,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "candle_limit": args.candle_limit,
            "coins": coins,
            "s2_freeze": _config_dict(freeze),
            "d0_recovery": "B1_terminal_after_original_blocker_flat",
            "d1_recovery": "full_continuous_no_b1",
            "d0_parity": d0_parity,
            "capital": capital,
            "git": _git_status(),
            "margin_competition_simulated": False,
        },
    )
    _write_json(
        out / "run_manifest.json",
        {
            "out": str(out),
            "n_coins": len(coins),
            "d0_parity_ok": d0_parity.get("ok"),
            "invalid_partial_long": d0.get("invalid_partial_cycle_count"),
            "invalid_partial_short": d1.get("invalid_partial_cycle_count"),
        },
    )
    print(f"[dual-s2] wrote {out}", flush=True)


if __name__ == "__main__":
    main()
