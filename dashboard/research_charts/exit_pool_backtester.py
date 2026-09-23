"""Research-charts adapter: OB exit-pool liquidity long backtest overlays."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STRATEGY_ID = "ob_exit_pool_liquidity_v1"
DISPLAY_NAME = "OB Exit Pool Liquidity V1 (Long)"
BACKTESTER_SOURCE = "exit_pool_backtester"
DRAWING_PREFIX = "exit-pool-"

_REPORTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "ob_microstructure_breakout_bot"
    / "exit_backtest"
    / "reports"
)

# Frozen baselines — do not retune; compare when collectors cover more history.
FROZEN_REPORTS: dict[str, Path] = {
    "phase1h": _REPORTS_DIR / "long_exit_phase1h_fee_filter.json",
    "phase1e": _REPORTS_DIR / "long_exit_phase1e_5m_meaningful.json",
}
DEFAULT_BASELINE = "phase1h"

# Strategy_id aliases → frozen baseline key
STRATEGY_BASELINES: dict[str, str] = {
    "ob_exit_pool_liquidity_v1": "phase1h",
    "ob_exit_pool_liquidity_v1_phase1h": "phase1h",
    "exit_pool": "phase1h",
    "exit_pool_liquidity": "phase1h",
    "ob_exit_pool_liquidity_v1_phase1e": "phase1e",
    "exit_pool_phase1e": "phase1e",
}


def resolve_baseline(strategy_id: str | None = None, baseline: str | None = None) -> str:
    if baseline and str(baseline) in FROZEN_REPORTS:
        return str(baseline)
    sid = str(strategy_id or "").strip()
    if sid in STRATEGY_BASELINES:
        return STRATEGY_BASELINES[sid]
    return DEFAULT_BASELINE


def report_path_for(baseline: str | None = None) -> Path:
    key = baseline if baseline in FROZEN_REPORTS else DEFAULT_BASELINE
    return FROZEN_REPORTS[key]


def default_report_path() -> Path:
    return report_path_for(DEFAULT_BASELINE)


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out <= 0:
        return None
    return out


def load_report(path: Path | None = None) -> dict[str, Any]:
    report = Path(path) if path else default_report_path()
    if not report.is_file():
        raise FileNotFoundError(f"exit backtest report missing: {report}")
    return json.loads(report.read_text(encoding="utf-8"))


def trade_to_position_spec(row: dict[str, Any], *, symbol: str) -> dict[str, Any] | None:
    """Map one exit_backtest trade row to a long_position drawing spec."""
    if row.get("ignored") or row.get("exit_reason") == "ignored":
        return None
    if row.get("error"):
        return None
    entry = _f(row.get("entry_price"))
    sl = _f(row.get("sl_price"))
    tp = _f(row.get("final_tp")) or _f(row.get("initial_tp")) or _f(row.get("first_pool_bottom"))
    if entry is None or sl is None or tp is None:
        return None
    start = _parse_dt(row.get("decision_ts"))
    if start is None:
        return None
    end = _parse_dt(row.get("exit_ts")) or (start + timedelta(hours=4))
    if end <= start:
        end = start + timedelta(minutes=15)
    sid = str(row.get("decision_ts") or start.isoformat())
    safe = sid.replace(":", "").replace("+", "").replace(".", "")
    return {
        "drawing_id": f"{DRAWING_PREFIX}{safe}",
        "drawing_type": "long_position",
        "symbol": symbol.upper(),
        "timeframe": "5m",
        "start": start,
        "end": end,
        "entry": entry,
        "stop": sl,
        "target": tp,
        "signal_id": sid,
        "direction": "LONG",
        "exit_reason": str(row.get("exit_reason") or ""),
        "tp_mode": str(row.get("tp_mode") or ""),
        "pnl_pct": row.get("pnl_pct"),
        "tier": str(row.get("tier") or ""),
    }


def build_position_specs(
    *,
    symbol: str,
    report_path: Path | None = None,
    baseline: str | None = None,
    strategy_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (specs, meta) for the given symbol from a frozen report."""
    key = resolve_baseline(strategy_id=strategy_id, baseline=baseline)
    path = Path(report_path) if report_path else report_path_for(key)
    payload = load_report(path)
    report_symbol = str(payload.get("symbol") or "DOGEUSDT").upper()
    sym = symbol.upper().replace("/", "")
    labels = {
        "phase1h": "phase1h mass+fee (+6.71%)",
        "phase1e": "phase1e 5m meaningful (+7.98%)",
    }
    meta = {
        "strategy_id": STRATEGY_ID,
        "display_name": DISPLAY_NAME,
        "source": BACKTESTER_SOURCE,
        "baseline": key,
        "baseline_label": labels.get(key, key),
        "report_path": str(path),
        "report_symbol": report_symbol,
        "summary": payload.get("summary") or {},
    }
    if sym != report_symbol:
        meta["message"] = f"Report ist {report_symbol}; Chart-Symbol ist {sym}"
        return [], meta
    specs: list[dict[str, Any]] = []
    for row in payload.get("trades") or []:
        spec = trade_to_position_spec(row, symbol=sym)
        if spec is not None:
            specs.append(spec)
    meta["n_trades"] = len(payload.get("trades") or [])
    meta["n_specs"] = len(specs)
    if specs:
        starts = [s["start"] for s in specs]
        ends = [s["end"] for s in specs]

        def _z(dt: datetime) -> str:
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        meta["time_span"] = {
            "start": _z(min(starts)),
            "end": _z(max(ends)),
            "focus": _z(min(starts)),
        }
    return specs, meta
