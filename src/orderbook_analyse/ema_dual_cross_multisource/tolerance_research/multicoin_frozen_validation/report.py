"""Report writers for multicoin frozen validation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .aggregations import (
    equal_weight_per_coin,
    evaluate_robustness,
    half_window_split,
    leave_best_worst,
    leave_one_coin_out,
    long_short_split,
    median_coin_result,
    pnl_concentration,
    pooled_all_trades,
    results_by_coin,
    xrp_vs_coins,
)
from .constants import (
    PRIMARY_CELLS,
    PRIMARY_COST_PCT,
    PRIMARY_GROUP,
    PRIMARY_REFERENCE_CELL_ID,
    VERDICT_FAILED,
    VERDICT_INSUFFICIENT,
    VERDICT_NOT_ROBUST,
    VERDICT_ROBUST,
)


def _write_csv(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    df.to_csv(path, index=False)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def adjacent_m0_cells_similar(primary_matrix_rows: list[dict[str, Any]]) -> bool:
    """True if all four frozen M0 SUPPORTIVE cells share the same sign of net PnL (or all near zero)."""
    vals = []
    for cell in PRIMARY_CELLS:
        row = next(
            (
                r
                for r in primary_matrix_rows
                if r.get("strategy_key") == cell["cell_id"] and r.get("group") == PRIMARY_GROUP
            ),
            None,
        )
        if row is None:
            return False
        vals.append(float(row.get("net_pnl_usdt") or 0))
    if not vals:
        return False
    signs = {1 if v > 1e-9 else (-1 if v < -1e-9 else 0) for v in vals}
    signs.discard(0)
    return len(signs) <= 1


def build_reports(
    *,
    reports_dir: Path,
    trades: list[dict[str, Any]],
    coin_stats: list[dict[str, Any]],
    n_eligible: int,
    start: datetime,
    end: datetime,
    insufficient_coverage: bool,
) -> dict[str, Any]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    ref_key = PRIMARY_REFERENCE_CELL_ID
    ref_trades = [
        t
        for t in trades
        if t.get("strategy_key") == ref_key and t.get("group") == PRIMARY_GROUP and t.get("roundtrip_cost_pct") == PRIMARY_COST_PCT
    ]

    coin_rows = results_by_coin(ref_trades, strategy_key=ref_key, group=PRIMARY_GROUP)
    pooled = pooled_all_trades(ref_trades)
    ew = equal_weight_per_coin(coin_rows)
    med = median_coin_result(coin_rows)
    loo = leave_one_coin_out(coin_rows)
    lbw = leave_best_worst(coin_rows)
    conc = pnl_concentration(coin_rows)
    mid = (_utc(start) + (_utc(end) - _utc(start)) / 2).isoformat()
    halves = half_window_split(ref_trades, midpoint_iso=mid)
    ls = long_short_split(ref_trades)
    xrp = xrp_vs_coins(coin_rows)

    # Primary matrix: pooled SUPPORTIVE per frozen cell
    primary_matrix = []
    for cell in PRIMARY_CELLS:
        cell_trades = [
            t
            for t in trades
            if t.get("strategy_key") == cell["cell_id"]
            and t.get("group") == PRIMARY_GROUP
            and float(t.get("roundtrip_cost_pct") or 0) == PRIMARY_COST_PCT
        ]
        primary_matrix.append(
            {
                "strategy_key": cell["cell_id"],
                "group": PRIMARY_GROUP,
                "tp_pct": cell["tp_pct"],
                "sl_pct": cell["sl_pct"],
                "horizon": cell["horizon"],
                "is_reference": cell["is_reference"],
                **pooled_all_trades(cell_trades),
            }
        )

    secondary = []
    sec_keys = sorted({t.get("strategy_key") for t in trades if t.get("role") == "secondary"})
    for sk in sec_keys:
        st = [
            t
            for t in trades
            if t.get("strategy_key") == sk
            and t.get("group") == PRIMARY_GROUP
            and float(t.get("roundtrip_cost_pct") or 0) == PRIMARY_COST_PCT
        ]
        secondary.append({"strategy_key": sk, "group": PRIMARY_GROUP, **pooled_all_trades(st)})

    similar = adjacent_m0_cells_similar(primary_matrix)
    robust = evaluate_robustness(
        pooled=pooled,
        coin_rows=coin_rows,
        leave=lbw,
        concentration=conc,
        halves=halves,
        ls=ls,
        adjacent_cells_similar=similar,
        n_eligible=n_eligible,
    )

    if insufficient_coverage:
        verdict = VERDICT_INSUFFICIENT
    elif not coin_rows and n_eligible >= 0:
        verdict = VERDICT_FAILED if n_eligible > 0 else VERDICT_INSUFFICIENT
    elif robust.get("passed"):
        verdict = VERDICT_ROBUST
    else:
        verdict = VERDICT_NOT_ROBUST

    _write_csv(reports_dir / "results_by_coin.csv", coin_rows)
    _write_csv(reports_dir / "primary_m0_matrix.csv", primary_matrix)
    _write_csv(reports_dir / "secondary_strategies.csv", secondary)
    _write_csv(reports_dir / "pooled_results.csv", [pooled])
    _write_csv(reports_dir / "equal_weight_results.csv", [ew])
    _write_csv(reports_dir / "median_coin_results.csv", [med])
    _write_csv(reports_dir / "leave_one_coin_out.csv", loo)
    _write_csv(reports_dir / "pnl_concentration.csv", [conc])
    _write_csv(
        reports_dir / "half_window_split.csv",
        [
            {"half": "first_15d", **(halves.get("first_15d") or {})},
            {"half": "second_15d", **(halves.get("second_15d") or {})},
        ],
    )
    _write_csv(
        reports_dir / "long_short_split.csv",
        [
            {"side": "long", **(ls.get("long") or {})},
            {"side": "short", **(ls.get("short") or {})},
        ],
    )
    _write_csv(reports_dir / "xrp_vs_coins.csv", [xrp])
    if coin_stats:
        _write_csv(reports_dir / "coin_strategy_stats.csv", coin_stats)

    summary = {
        "verdict": verdict,
        "reference_cell": ref_key,
        "n_eligible_core_30d": n_eligible,
        "n_coins_in_results": len(coin_rows),
        "pooled": pooled,
        "equal_weight": ew,
        "median_coin": med,
        "robustness": robust,
        "leave_best_worst": lbw,
        "pnl_concentration": conc,
        "xrp_vs_coins": xrp,
        "half_window": {
            "strongly_contradictory": halves.get("strongly_contradictory"),
            "midpoint": halves.get("midpoint"),
        },
        "long_short": {
            "only_long_driven": ls.get("only_long_driven"),
            "only_short_driven": ls.get("only_short_driven"),
        },
        "adjacent_m0_similar": similar,
        "primary_matrix": primary_matrix,
        "secondary": secondary,
    }
    _write_json(reports_dir / "summary.json", summary)
    (reports_dir / "summary.md").write_text(_summary_md(summary), encoding="utf-8")
    return summary


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _summary_md(summary: dict[str, Any]) -> str:
    v = summary.get("verdict")
    lines = [
        "# Multi-Coin Frozen Validation",
        "",
        f"**Verdict:** `{v}`",
        "",
        f"- Reference cell: `{summary.get('reference_cell')}`",
        f"- Eligible CORE_30D coins: `{summary.get('n_eligible_core_30d')}`",
        f"- Coins in reference results: `{summary.get('n_coins_in_results')}`",
        "",
        "## Robustness",
        "",
        f"- Label: `{(summary.get('robustness') or {}).get('label')}`",
        f"- Checks: `{(summary.get('robustness') or {}).get('checks')}`",
        "",
        "## XRP vs coins",
        "",
        f"- `{summary.get('xrp_vs_coins')}`",
        "",
        "Research-SUPPORTIVE ≠ Production-ALLOW. No live activation.",
        "",
        f"**Final verdict:** `{v}`",
        "",
    ]
    return "\n".join(lines)
