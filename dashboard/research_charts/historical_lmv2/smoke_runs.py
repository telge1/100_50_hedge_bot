"""Registered read-only V2 backtest report runs (no client filesystem paths)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ARTIFACTS = Path(__file__).resolve().parent / "artifacts"

FROZEN_JULY_2026 = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"


@dataclass(frozen=True)
class SmokeRunSpec:
    run_id: str
    label: str
    warning: str
    report_path: Path
    catalog_id: str
    frozen_input_id: str
    symbol: str
    timeframe: str
    pool_detail: int
    variant_keys: tuple[str, ...]
    report_from: str
    report_to: str


REGISTERED_RUNS: dict[str, SmokeRunSpec] = {
    "july_2026_pd10_smoke": SmokeRunSpec(
        run_id="july_2026_pd10_smoke",
        label="Juli 2026 Smoke (PD10, DOGE)",
        warning="SMOKE / STATE_WARMUP_NOT_PROVEN",
        report_path=_ARTIFACTS / "july_smoke_report.json",
        catalog_id="doge_multi_month_2026",
        frozen_input_id=FROZEN_JULY_2026,
        symbol="DOGEUSDT",
        timeframe="15m",
        pool_detail=10,
        variant_keys=("15m_pd10_guard_off", "15m_pd10_guard_on"),
        report_from="2026-07-07T00:00:00+00:00",
        report_to="2026-07-09T23:59:59+00:00",
    ),
}


def list_runs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in REGISTERED_RUNS.values():
        out.append(
            {
                "run_id": spec.run_id,
                "label": spec.label,
                "warning": spec.warning,
                "symbol": spec.symbol,
                "timeframe": spec.timeframe,
                "pool_detail": spec.pool_detail,
                "variants": [
                    {"key": k, "guard_on": k.endswith("guard_on")}
                    for k in spec.variant_keys
                ],
                "report_from": spec.report_from,
                "report_to": spec.report_to,
                "frozen_input_id": spec.frozen_input_id,
                "catalog_id": spec.catalog_id,
            }
        )
    return out


def get_run(run_id: str) -> SmokeRunSpec:
    key = str(run_id or "").strip()
    spec = REGISTERED_RUNS.get(key)
    if spec is None:
        raise KeyError(f"unknown run_id {run_id!r}")
    if not spec.report_path.is_file():
        raise FileNotFoundError(f"report missing for run {run_id}")
    return spec


def _parse_ts(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def decision_close_utc(entry_open: str) -> str:
    """Strategy pool load uses 15m bar close (= open + 15m)."""
    dc = _parse_ts(entry_open) + timedelta(minutes=15)
    return dc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _trade_row(raw: dict[str, Any], spec: SmokeRunSpec, variant_key: str) -> dict[str, Any]:
    entry_open = str(raw.get("entry_open") or "")
    dc = decision_close_utc(entry_open) if entry_open else ""
    exit_time = raw.get("exit_time")
    first_hit = raw.get("first_hit") or raw.get("outcome") or ""
    return {
        "signal_num": raw.get("signal_num"),
        "symbol": spec.symbol,
        "direction": "SHORT",
        "variant_key": variant_key,
        "pool_detail": spec.pool_detail,
        "guard_on": variant_key.endswith("guard_on"),
        "entry_open": entry_open,
        "decision_close": dc,
        "entry_price": raw.get("entry_price"),
        "stop": raw.get("stop"),
        "tp": raw.get("tp"),
        "pool_id": raw.get("pool_id"),
        "bridge_upper_id": "NOT_TRACED",
        "tp_lower_id": "NOT_TRACED",
        "exit_time": exit_time,
        "exit_reason": first_hit,
        "exit_price": raw.get("exit_price"),
        "pnl_pct": raw.get("pnl_pct"),
        "r_multiple": raw.get("r_multiple"),
        "catalog_id": spec.catalog_id,
        "frozen_input_id": spec.frozen_input_id,
        "timeframe": spec.timeframe,
    }


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    tp = sum(1 for t in trades if t.get("exit_reason") == "TP")
    sl = sum(1 for t in trades if t.get("exit_reason") == "SL")
    horizon = sum(1 for t in trades if t.get("exit_reason") == "none")
    pnl_vals = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    pnl_sum = round(sum(pnl_vals), 4) if pnl_vals else None
    return {
        "trades": n,
        "tp_exits": tp,
        "sl_exits": sl,
        "horizon_exits": horizon,
        "pnl_sum_pct": pnl_sum,
    }


def paginated_results(
    run_id: str,
    *,
    variant_key: str,
    page: int = 0,
    page_size: int = 50,
) -> dict[str, Any]:
    spec = get_run(run_id)
    if variant_key not in spec.variant_keys:
        raise KeyError(f"variant {variant_key!r} not in run {run_id}")

    payload = json.loads(spec.report_path.read_text(encoding="utf-8"))
    block = (payload.get("variants") or {}).get(variant_key) or {}
    raw_trades = list(block.get("trades") or [])
    rows = [_trade_row(t, spec, variant_key) for t in raw_trades]
    rows.sort(key=lambda r: (r.get("decision_close") or "", r.get("signal_num") or 0))

    total = len(rows)
    size = max(1, min(int(page_size), 500))
    pages = (total + size - 1) // size if total else 0
    page_n = max(0, min(int(page), pages - 1)) if pages else 0
    start = page_n * size
    page_rows = rows[start : start + size]

    trade_summary = block.get("trade_summary") or _summary(rows)
    funnel = block.get("funnel") or {}

    return {
        "success": True,
        "run_id": spec.run_id,
        "label": spec.label,
        "warning": spec.warning,
        "state_warmup": payload.get("STATE_WARMUP_VALIDATION"),
        "variant_key": variant_key,
        "guard_on": variant_key.endswith("guard_on"),
        "symbol": spec.symbol,
        "timeframe": spec.timeframe,
        "pool_detail": spec.pool_detail,
        "catalog_id": spec.catalog_id,
        "frozen_input_id": spec.frozen_input_id,
        "report_from": spec.report_from,
        "report_to": spec.report_to,
        "funnel": funnel,
        "summary": trade_summary,
        "trades": page_rows,
        "pagination": {
            "page": page_n,
            "page_size": size,
            "total_filtered": total,
            "total_pages": pages,
            "has_prev": page_n > 0,
            "has_next": page_n + 1 < pages,
        },
    }


def config_has_run(pool_detail: int, guard_on: bool) -> str | None:
    """Return run_id if a registered report exists for this config, else None."""
    vkey = f"15m_pd{int(pool_detail)}_guard_{'on' if guard_on else 'off'}"
    for spec in REGISTERED_RUNS.values():
        if vkey in spec.variant_keys and spec.report_path.is_file():
            return spec.run_id
    return None
