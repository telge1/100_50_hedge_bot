"""Per-coin backtest orchestration (bounded memory; isolated failures)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..shared_strategy.market_data import load_strategy_market_data
from ..shared_strategy.market_data import prepare_tf_frames as shared_prepare_tf_frames
from ..shared_strategy.outcomes import simulate_canonical_trade
from ..shared_strategy.semantics import REQUIRE_FULL_HORIZON
from ..tpsl_pnl_engine import aggregate_strategy_stats
from .candidates import detect_modes_for_coin
from .constants import (
    CONTROL_GROUPS,
    ENTRY_RULE,
    FUNDING_STATUS,
    NOTIONAL_USDT,
    PRIMARY_CELLS,
    PRIMARY_COST_PCT,
    PRIMARY_GROUP,
    PRIMARY_MODE,
    PRIMARY_TF,
    SECONDARY_STRATEGIES,
)

GROUP_FILTERS: dict[str, Callable[[dict], bool]] = {
    "EMA_RAW": lambda c: True,
    "CORE_RESEARCH_SUPPORTIVE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_SUPPORTIVE",
    "CORE_RESEARCH_ADVERSE": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_ADVERSE",
    "CORE_RESEARCH_MIXED": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_MIXED",
    "CORE_RESEARCH_INSUFFICIENT": lambda c: c.get("core_research_verdict") == "CORE_RESEARCH_INSUFFICIENT",
    "FULL_MULTISOURCE": lambda c: c.get("coverage_segment") == "FULL_MULTISOURCE",
    "PRODUCTION_ALLOW": lambda c: c.get("production_gate_verdict") == "ALLOW",
    "PRODUCTION_BLOCK": lambda c: c.get("production_gate_verdict") == "BLOCK",
    "PRODUCTION_INCONCLUSIVE": lambda c: c.get("production_gate_verdict") == "INCONCLUSIVE_DATA",
}


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def strategy_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for cell in PRIMARY_CELLS:
        specs.append(
            {
                "strategy_key": cell["cell_id"],
                "role": "primary",
                "timeframe": PRIMARY_TF,
                "mode_id": PRIMARY_MODE,
                "group": PRIMARY_GROUP,
                "strategy_id": cell["strategy_id"],
                "tp_pct": cell["tp_pct"],
                "sl_pct": cell["sl_pct"],
                "horizon": cell["horizon"],
                "horizon_min": cell["horizon_min"],
                "is_reference": cell["is_reference"],
                "cell_id": cell["cell_id"],
            }
        )
    for sec in SECONDARY_STRATEGIES:
        specs.append(dict(sec))
    return specs


def simulate_for_candidates(
    candidates: list[dict[str, Any]],
    candles_1m: pd.DataFrame,
    *,
    coverage_class: str | None,
    cost_pct: float = PRIMARY_COST_PCT,
    groups: tuple[str, ...] = CONTROL_GROUPS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (trades, per_strategy_coin_stats) via shared canonical outcome engine."""
    del cost_pct  # frozen in simulate_canonical_trade (REF_COST_PCT)
    specs = strategy_specs()
    trades: list[dict[str, Any]] = []
    stats_rows: list[dict[str, Any]] = []

    for group in groups:
        filt = GROUP_FILTERS[group]
        for spec in specs:
            subset = [
                c
                for c in candidates
                if c.get("timeframe") == spec["timeframe"]
                and c.get("mode_id") == spec["mode_id"]
                and filt(c)
            ]
            batch = []
            for c in subset:
                paid = simulate_canonical_trade(
                    candles_1m,
                    direction=str(c["direction"]),
                    entry_at=c["entry_at"],
                    entry_price=float(c["entry_price"]),
                    tp_pct=float(spec["tp_pct"]),
                    sl_pct=float(spec["sl_pct"]),
                    horizon_min=int(spec["horizon_min"]),
                )
                rec = {
                    **paid,
                    "symbol": c.get("symbol"),
                    "candidate_id": c.get("candidate_id"),
                    "cross_episode_id": c.get("cross_episode_id"),
                    "direction": c.get("direction"),
                    "decision_at": c.get("decision_at"),
                    "timeframe": spec["timeframe"],
                    "mode_id": spec["mode_id"],
                    "group": group,
                    "strategy_key": spec["strategy_key"],
                    "strategy_id": spec["strategy_id"],
                    "role": spec["role"],
                    "horizon": spec["horizon"],
                    "is_reference": bool(spec.get("is_reference")),
                    "coverage_class": coverage_class,
                    "core_research_verdict": c.get("core_research_verdict"),
                    "production_gate_verdict": c.get("production_gate_verdict"),
                    "funding_status": FUNDING_STATUS,
                    "notional_usdt": NOTIONAL_USDT,
                    "require_full_horizon": REQUIRE_FULL_HORIZON,
                    "entry_rule": ENTRY_RULE,
                }
                batch.append(rec)
                trades.append(rec)
            st = aggregate_strategy_stats(batch)
            stats_rows.append(
                {
                    "symbol": (candidates[0].get("symbol") if candidates else None),
                    "strategy_key": spec["strategy_key"],
                    "role": spec["role"],
                    "group": group,
                    "timeframe": spec["timeframe"],
                    "mode_id": spec["mode_id"],
                    "tp_pct": spec["tp_pct"],
                    "sl_pct": spec["sl_pct"],
                    "horizon": spec["horizon"],
                    "is_reference": bool(spec.get("is_reference")),
                    "coverage_class": coverage_class,
                    "n_candidates_in_group": len(subset),
                    **st,
                }
            )
    return trades, stats_rows


def load_coin_market_data(client, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    return load_strategy_market_data(client, symbol, start, end)


def prepare_tf_frames(candles_1m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return shared_prepare_tf_frames(candles_1m, timeframes=("5m", "15m"))


def run_one_coin(
    client,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    coverage_class: str | None,
    window_report: dict[str, Any] | None = None,
    cost_pct: float = PRIMARY_COST_PCT,
    repo: Path | None = None,
    enforce_xrp_parity: bool = True,
    xrp_export_path: str | Path | None = None,
) -> dict[str, Any]:
    """Full per-coin pipeline. Caller handles checkpoint / failure isolation.

    For XRPUSDT, when ``enforce_xrp_parity`` is True, candidates are compared to the
    frozen XRP export (local CSV only) on the multicoin detection scope.
    """
    from .xrp_parity import verify_xrp_candidates_against_export

    del cost_pct
    data = load_coin_market_data(client, symbol, start, end)
    c1m = data["candles_1m"]
    df_by_tf = prepare_tf_frames(c1m)
    candidates = detect_modes_for_coin(
        df_by_tf=df_by_tf,
        candles_1m=c1m,
        symbol=symbol,
        window_start=start,
        window_end=end,
        trades_1m=data["trades"],
        ob_1m=data["ob"],
        oi_1m=data["oi"],
        liq=data["liq"],
        window_report=window_report,
    )

    parity: dict[str, Any] | None = None
    if str(symbol).upper() == "XRPUSDT" and enforce_xrp_parity:
        root = Path(repo) if repo is not None else Path(__file__).resolve().parents[5]
        parity = verify_xrp_candidates_against_export(
            candidates, repo=root, export_path=xrp_export_path
        )
        if not parity.get("ok"):
            return {
                "symbol": "XRPUSDT",
                "status": "FAILED_PARITY",
                "coverage_class": coverage_class,
                "n_candidates": len(candidates),
                "n_trades": 0,
                "candidates": candidates,
                "trades": [],
                "stats_by_strategy": [],
                "funding_status": FUNDING_STATUS,
                "entry_rule": ENTRY_RULE,
                "parity": parity,
            }

    trades, stats_rows = simulate_for_candidates(
        candidates, c1m, coverage_class=coverage_class
    )
    return {
        "symbol": symbol.upper(),
        "status": "COMPLETE",
        "coverage_class": coverage_class,
        "n_candidates": len(candidates),
        "n_trades": len(trades),
        "candidates": candidates,
        "trades": trades,
        "stats_by_strategy": stats_rows,
        "funding_status": FUNDING_STATUS,
        "entry_rule": ENTRY_RULE,
        "parity": parity,
        "market_pads": data.get("pads"),
        "require_full_horizon": REQUIRE_FULL_HORIZON,
    }
