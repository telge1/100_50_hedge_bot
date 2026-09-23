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

_DEFAULT_REPORT = (
    Path(__file__).resolve().parents[2]
    / "ob_microstructure_breakout_bot"
    / "exit_backtest"
    / "reports"
    / "long_exit_phase1h_fee_filter.json"
)


def default_report_path() -> Path:
    return _DEFAULT_REPORT


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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (specs, meta) for the given symbol from the latest report."""
    payload = load_report(report_path)
    report_symbol = str(payload.get("symbol") or "DOGEUSDT").upper()
    sym = symbol.upper().replace("/", "")
    meta = {
        "strategy_id": STRATEGY_ID,
        "display_name": DISPLAY_NAME,
        "source": BACKTESTER_SOURCE,
        "report_path": str(report_path or default_report_path()),
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
