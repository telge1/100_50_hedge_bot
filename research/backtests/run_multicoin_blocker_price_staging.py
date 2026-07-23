"""CLI: multi-coin baseline-blocker price-staging audit @1000/500 (research-only).

Part A — isolated original 27 blockers. APT T3 S1000 prototype parity is a hard gate.

Example:

```bash
PYTHONPATH=. python -m research.backtests.run_multicoin_blocker_price_staging \\
  --baseline-audit-dir research/backtests/results/current_baseline_multicoin_continuous_blocker_audit_20260720 \\
  --sizes 1000:500 \\
  --profiles legacy,linear4,conservative3,small_early4 \\
  --output-dir research/backtests/results/multicoin_blocker_price_staging_1000_500_20260721
```
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import (
    APT_PROTOTYPE,
    DEFAULT_BASELINE,
    DEFAULT_OUT,
    FULL_HISTORY_CANDLE_LIMIT,
    LONG_NOTIONAL,
    analyze_blocker_run,
    assert_output_dir_safe,
    check_apt_prototype_parity,
    classify_vs_legacy,
    parse_profiles,
    run_isolated_blocker,
    summarize_profile,
    write_case_markdown,
)
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.second_leg_price_staging import resolve_profile

ROOT = Path(__file__).resolve().parents[2]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
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
                out[key] = json.dumps(val, default=str) if isinstance(val, (dict, list)) else val
            writer.writerow(out)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _git() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        status["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        )
        status["dirty"] = bool(porcelain.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def safe_float_improvement(row: dict[str, Any]) -> bool:
    try:
        return float(row.get("improvement_usdt") or 0) > 1e-6
    except (TypeError, ValueError):
        return False


def write_report(
    path: Path,
    *,
    summaries: list[dict[str, Any]],
    apt_parity: dict[str, Any],
    legacy_parity: dict[str, Any],
    guards: dict[str, Any],
    n_blockers: int,
) -> None:
    lines = [
        "# Multi-coin blocker price-staging @1000/500",
        "",
        "Research-only. Profiles identical to APT T3 prototype (`only_cycles=(4,)`).",
        "Part A: isolated original baseline blockers. No live recommendation.",
        "",
        f"Population: **{n_blockers}** blockers from protected baseline audit.",
        f"Size: **{int(LONG_NOTIONAL)}/{int(LONG_NOTIONAL / 2)}** USDT.",
        "",
        "## APT control gate",
        "",
        f"- apt_parity.ok = **{apt_parity.get('ok')}**",
        f"- legacy_parity (APT M0) ok = **{legacy_parity.get('ok')}**",
        "",
        "## Profile summary (all blockers, no best-of)",
        "",
        "| profile | closed | open | +closes | Σ closed | Σ open | total | worst | improved | worsened | activated | fallback1 | invalid | undercov |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        if s.get("profile") == "legacy":
            continue
        lines.append(
            "| {profile} | {closed}/{n} | {still_open} | {closed_positive} | {sum_closed_pnl:.2f} | "
            "{sum_open_mtm:.2f} | {total_mtm:.2f} | {worst_final_mtm} | {coins_improved_vs_legacy} | "
            "{coins_worsened_vs_legacy} | {staging_activated_count} | {fallback_single_count} | "
            "{invalid_partial_sum} | {undercoverage_sum} |".format(n=n_blockers, **s)
        )
    legacy = next((s for s in summaries if s.get("profile") == "legacy"), {})
    lines.extend(
        [
            "",
            "### M0 legacy control",
            "",
            f"- closed={legacy.get('closed')} open={legacy.get('still_open')} "
            f"total_mtm={legacy.get('total_mtm')} worst={legacy.get('worst_final_mtm')}",
            "",
            "## Abschlussfragen",
            "",
            "1. **Geschlossen pro Profil?** Siehe `closed` in `profile_summary.csv`.",
            "2. **Positive Closes?** `closed_positive`.",
            "3. **Offen?** `still_open` + `blocker_classification.csv`.",
            "4. **Schlechter als Legacy?** `coins_worsened_vs_legacy` / class `still_open_worse`.",
            "5. **Stabilstes Profil?** Höchste Closes bei niedrigem `still_open_worse` + `invalid`.",
            "6. **Worst-MTM?** `worst_final_mtm` / `worst_worst_mtm`.",
            "7. **Exposure?** `avg_gross_exposure` / `max_gross_exposure` (1000/500 scale).",
            "8. **Undercoverage / Partial?** `undercoverage_sum` / `invalid_partial_sum` in guards.",
            "9. **APT reproduziert?** Siehe `apt_parity.json`.",
            "10. **Runtime-opt-in robust?** Nur wenn APT-Gate + breite Close-Rate ohne Undercoverage — "
            "**keine Live-Empfehlung** in diesem Audit.",
            "11. **Keine Live-Aktivierung.**",
            "",
            "## Guards",
            "",
            "```json",
            json.dumps(guards, indent=2, default=str),
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(
    *,
    baseline_dir: Path,
    profiles_spec: str,
    output_dir: Path,
    candle_limit: int = FULL_HISTORY_CANDLE_LIMIT,
    coins_filter: list[str] | None = None,
) -> dict[str, Any]:
    assert_output_dir_safe(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    blockers = load_baseline_blockers(baseline_dir / "blocker_trades.csv")
    if len(blockers) != 27 and not coins_filter:
        raise RuntimeError(f"expected 27 baseline blockers, got {len(blockers)}")
    if coins_filter:
        wanted = {c.upper() for c in coins_filter}
        blockers = [b for b in blockers if str(b.get("coin") or "").upper() in wanted]
    profiles = parse_profiles(profiles_spec)
    profile_names = [p.profile_name for p in profiles]
    if "legacy" not in profile_names:
        raise ValueError("profiles must include legacy (M0)")

    coins = [str(b["coin"]).upper() for b in blockers]
    print(f"[mc-staging] loading candles for {len(coins)} coins...", flush=True)
    coin_candles: dict[str, list[Any]] = {}
    for coin in coins:
        coin_candles[coin] = normalize_candles(
            coin, load_candles_for_symbol(coin, limit=int(candle_limit))
        )

    apt_row = next(
        (b for b in blockers if str(b["coin"]).upper() == "APTUSDT"),
        {"coin": "APTUSDT", "trade_number": 3, "mtm_pnl": "", "status": "open"},
    )
    apt_candles = coin_candles.get("APTUSDT")
    if apt_candles is None:
        apt_candles = normalize_candles(
            "APTUSDT", load_candles_for_symbol("APTUSDT", limit=int(candle_limit))
        )
        coin_candles["APTUSDT"] = apt_candles

    apt_by_profile: dict[str, dict[str, Any]] = {}
    print("[mc-staging] APT T3 prototype parity gate...", flush=True)
    for cfg in profiles:
        result = run_isolated_blocker(
            coin="APTUSDT",
            candles=apt_candles,
            start_index=int(APT_PROTOTYPE["start_index"]),
            staging_config=cfg,
            trade_number=int(APT_PROTOTYPE["trade_number"]),
        )
        row = analyze_blocker_run(
            coin="APTUSDT",
            trade_number=int(APT_PROTOTYPE["trade_number"]),
            start_index=int(APT_PROTOTYPE["start_index"]),
            profile=cfg.profile_name,
            result=result,
            candles=apt_candles,
            baseline_row=apt_row,
        )
        apt_by_profile[cfg.profile_name] = row
        print(
            f"  APT {cfg.profile_name}: flat={row['trade_flat']} mtm={row['final_mtm']:.4f} "
            f"apt_exit={row.get('apt_bounce_exit')} reach={row.get('apt_bounce_reaches')}",
            flush=True,
        )

    apt_parity = check_apt_prototype_parity(apt_by_profile)
    legacy_parity = {
        "ok": bool(apt_parity.get("checks", {}).get("legacy", {}).get("ok")),
        "apt_legacy": apt_parity.get("checks", {}).get("legacy"),
        "note": "M0 APTUSDT T3 @1000/500 must match apt_t3 lab legacy S1000",
    }
    _write_json(output_dir / "apt_parity.json", apt_parity)
    _write_json(output_dir / "legacy_parity.json", legacy_parity)
    if not apt_parity.get("ok"):
        _write_json(
            output_dir / "ABORT.json",
            {
                "reason": "APT prototype parity failed — audit aborted before full matrix",
                "apt_parity": apt_parity,
            },
        )
        print("ABORT: APT prototype parity failed", flush=True)
        print(json.dumps(apt_parity, indent=2, default=str), flush=True)
        return {"aborted": True, "apt_parity": apt_parity}

    per_coin: list[dict[str, Any]] = []
    stage_fill_rows: list[dict[str, Any]] = []
    exit_drop_rows: list[dict[str, Any]] = []
    exposure_rows: list[dict[str, Any]] = []
    duration_rows: list[dict[str, Any]] = []
    classification_rows: list[dict[str, Any]] = []
    legacy_by_coin: dict[str, dict[str, Any]] = {}

    for profile, row in apt_by_profile.items():
        if profile == "legacy":
            legacy_by_coin["APTUSDT"] = row

    for blocker in blockers:
        coin = str(blocker["coin"]).upper()
        trade_number = int(blocker["trade_number"])
        start_index = int(blocker["start_index"])
        candles = coin_candles[coin]

        for cfg in profiles:
            if coin == "APTUSDT" and start_index == int(APT_PROTOTYPE["start_index"]):
                row = dict(apt_by_profile[cfg.profile_name])
            else:
                print(
                    f"[mc-staging] {coin} trade={trade_number} start={start_index} "
                    f"profile={cfg.profile_name}",
                    flush=True,
                )
                result = run_isolated_blocker(
                    coin=coin,
                    candles=candles,
                    start_index=start_index,
                    staging_config=cfg,
                    trade_number=trade_number,
                )
                row = analyze_blocker_run(
                    coin=coin,
                    trade_number=trade_number,
                    start_index=start_index,
                    profile=cfg.profile_name,
                    result=result,
                    candles=candles,
                    baseline_row=blocker,
                )

            if cfg.profile_name == "legacy":
                legacy_by_coin[coin] = row
                row["improvement_usdt"] = 0.0
                row["improvement_per_100"] = 0.0
                row["classification"] = "legacy_control"
                row["avoided_blocker_duration_candles"] = 0
            else:
                legacy = legacy_by_coin.get(coin)
                if legacy is None:
                    leg_cfg = resolve_profile("legacy")
                    leg_res = run_isolated_blocker(
                        coin=coin,
                        candles=candles,
                        start_index=start_index,
                        staging_config=leg_cfg,
                        trade_number=trade_number,
                    )
                    legacy = analyze_blocker_run(
                        coin=coin,
                        trade_number=trade_number,
                        start_index=start_index,
                        profile="legacy",
                        result=leg_res,
                        candles=candles,
                        baseline_row=blocker,
                    )
                    legacy_by_coin[coin] = legacy
                cls = classify_vs_legacy(staged=row, legacy=legacy)
                row.update(cls)

            per_coin.append(row)
            stage_fill_rows.append(
                {
                    "coin": coin,
                    "profile": cfg.profile_name,
                    "planned_stages": row.get("planned_stages"),
                    "filled_stages": row.get("filled_stages"),
                    "staging_activated": row.get("staging_activated"),
                    "fallback_single_stage": row.get("fallback_single_stage"),
                    "first_stage_fill_candle": row.get("first_stage_fill_candle"),
                    "distinct_triggers": row.get("distinct_triggers"),
                }
            )
            exit_drop_rows.append(
                {
                    "coin": coin,
                    "profile": cfg.profile_name,
                    "exit_before_first_stage": row.get("exit_before_first_stage"),
                    "exit_after_first_stage": row.get("exit_after_first_stage"),
                    "strongest_exit_drop": row.get("strongest_exit_drop"),
                    "bounce_reaches_exit": row.get("bounce_reaches_exit"),
                    "apt_bounce_exit": row.get("apt_bounce_exit"),
                }
            )
            exposure_rows.append(
                {
                    "coin": coin,
                    "profile": cfg.profile_name,
                    "gross_exposure": row.get("gross_exposure"),
                    "net_exposure": row.get("net_exposure"),
                    "gross_exposure_per_100": row.get("gross_exposure_per_100"),
                }
            )
            duration_rows.append(
                {
                    "coin": coin,
                    "profile": cfg.profile_name,
                    "duration_candles": row.get("duration_candles"),
                    "trade_flat": row.get("trade_flat"),
                    "avoided_blocker_duration_candles": row.get(
                        "avoided_blocker_duration_candles"
                    ),
                }
            )
            if cfg.profile_name != "legacy":
                classification_rows.append(
                    {
                        "coin": coin,
                        "trade_number": trade_number,
                        "profile": cfg.profile_name,
                        "classification": row.get("classification"),
                        "improvement_usdt": row.get("improvement_usdt"),
                        "improvement_per_100": row.get("improvement_per_100"),
                        "legacy_final_mtm": row.get("legacy_final_mtm"),
                        "staged_final_mtm": row.get("staged_final_mtm"),
                        "staging_activated": row.get("staging_activated"),
                        "trade_flat": row.get("trade_flat"),
                    }
                )

    summaries: list[dict[str, Any]] = []
    for name in profile_names:
        rows = [r for r in per_coin if r.get("profile") == name]
        summaries.append(summarize_profile(rows, profile=name))

    guards = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git(),
        "n_blockers": len(blockers),
        "apt_parity_ok": apt_parity.get("ok"),
        "legacy_parity_ok": legacy_parity.get("ok"),
        "invalid_partial_total": sum(int(r.get("invalid_partial") or 0) for r in per_coin),
        "undercoverage_total": sum(int(r.get("undercoverage") or 0) for r in per_coin),
        "same_candle_cascade_total": sum(
            int(r.get("same_candle_cascade") or 0) for r in per_coin
        ),
        "part_b_continuous": "skipped_part_a_only",
        "notes": [
            "Profiles exact APT prototype (only_cycles=(4,))",
            "Size 1000/500 research-only — not a live recommendation",
            "Main metrics are per-profile across all blockers (no best-of pool)",
        ],
    }

    _write_csv(output_dir / "profile_summary.csv", summaries)
    _write_csv(output_dir / "per_coin_per_profile.csv", per_coin)
    _write_csv(output_dir / "blocker_classification.csv", classification_rows)
    _write_csv(output_dir / "stage_fill_summary.csv", stage_fill_rows)
    _write_csv(output_dir / "exit_drop_summary.csv", exit_drop_rows)
    _write_csv(output_dir / "exposure_summary.csv", exposure_rows)
    _write_csv(output_dir / "duration_summary.csv", duration_rows)
    _write_json(output_dir / "guards.json", guards)

    by_key = {(r["coin"], r["profile"]): r for r in per_coin}
    improved = sorted(
        [r for r in classification_rows if safe_float_improvement(r)],
        key=lambda r: float(r.get("improvement_usdt") or 0),
        reverse=True,
    )
    worst = sorted(
        [r for r in classification_rows if float(r.get("improvement_usdt") or 0) < -1e-6],
        key=lambda r: float(r.get("improvement_usdt") or 0),
    )
    for r in improved + worst:
        base = by_key.get((r["coin"], r["profile"]), {})
        r.update(
            {
                "trade_flat": base.get("trade_flat"),
                "final_mtm": base.get("final_mtm"),
                "planned_stages": base.get("planned_stages"),
                "filled_stages": base.get("filled_stages"),
                "staging_activated": base.get("staging_activated"),
            }
        )
    write_case_markdown(output_dir / "improved_cases.md", "Improved vs M0 legacy", improved)
    write_case_markdown(output_dir / "worst_cases.md", "Worse vs M0 legacy", worst)
    write_report(
        output_dir / "REPORT.md",
        summaries=summaries,
        apt_parity=apt_parity,
        legacy_parity=legacy_parity,
        guards=guards,
        n_blockers=len(blockers),
    )
    payload = {
        "output_dir": str(output_dir),
        "summaries": summaries,
        "apt_parity": apt_parity,
        "aborted": False,
    }
    _write_json(output_dir / "run_manifest.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-audit-dir", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--sizes", default="1000:500", help="Fixed research size (must be 1000:500)")
    parser.add_argument(
        "--profiles",
        default="legacy,linear4,conservative3,small_early4",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candle-limit", type=int, default=FULL_HISTORY_CANDLE_LIMIT)
    parser.add_argument("--coins", default="", help="Optional comma filter for debug")
    args = parser.parse_args(argv)

    if str(args.sizes).strip() not in ("1000:500", "1000:500.0"):
        print(f"This audit is fixed at 1000:500 (got {args.sizes})", file=sys.stderr)
        return 2

    coins_filter = [c.strip() for c in str(args.coins).split(",") if c.strip()] or None
    names = [p.strip() for p in args.profiles.split(",") if p.strip()]
    if "legacy" in names:
        names = ["legacy"] + [n for n in names if n != "legacy"]
    profiles_spec = ",".join(names)

    payload = run_audit(
        baseline_dir=args.baseline_audit_dir,
        profiles_spec=profiles_spec,
        output_dir=args.output_dir,
        candle_limit=args.candle_limit,
        coins_filter=coins_filter,
    )
    if payload.get("aborted"):
        return 1
    print(f"wrote {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
