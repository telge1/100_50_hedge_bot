"""Runner: replay smoke + shadow logs (no execution)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import aggregate_timeframe, fetch_candles_1m
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from . import SCANNER_ID, SCANNER_VERSION, VERDICT_CODE_READY
from .config import DEFAULT_OUT_DIR, SMOKE_SYMBOLS, TF_CONFIRM, TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO, TF_STRUCTURE
from .markers import BACKTESTER_SOURCE, signals_to_marker_specs
from .scanner import PoolSignalScanner
from .shadow_log import ShadowEventLog


def build_candles_by_tf(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    client=None,
) -> dict[str, pd.DataFrame]:
    client = client or get_clickhouse_client()
    df1 = fetch_candles_1m(client, symbol, start, end)
    if df1.empty:
        return {}
    out = {"1m": df1}
    for tf in (TF_STRUCTURE, TF_ENTRY_POOL, TF_LIQUIDITY, TF_MACRO):
        out[tf] = aggregate_timeframe(df1, tf)
    out[TF_CONFIRM] = df1
    return out


def run_scanner(
    *,
    symbol: str,
    candles_by_tf: dict[str, pd.DataFrame],
    pool_loader: Callable[..., dict[str, list]] | None = None,
    enable_pullback: bool = True,
    enable_terminal: bool = True,
) -> dict[str, Any]:
    scanner = PoolSignalScanner(symbol=symbol)
    if pool_loader is not None:
        orig = None
        import orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.scanner as scmod

        def _wrapped_loader(candles_by_tf, *, symbol, as_of):
            raw = pool_loader(candles_by_tf, symbol=symbol, as_of=as_of)
            scanner._inject_lifecycle_records = raw
            return {
                tf: [p for p in ps if p.is_active_at(as_of)]
                for tf, ps in raw.items()
            }

        orig = scmod.load_pools_at
        scmod.load_pools_at = _wrapped_loader  # type: ignore[assignment]
        try:
            result = scanner.scan(
                candles_by_tf,
                enable_pullback=enable_pullback,
                enable_terminal=enable_terminal,
            )
        finally:
            scmod.load_pools_at = orig
        return result
    return scanner.scan(
        candles_by_tf,
        enable_pullback=enable_pullback,
        enable_terminal=enable_terminal,
    )


def write_results(
    result: dict[str, Any],
    *,
    out_dir: Path,
    symbol: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        # allow sub-run dirs
        pass
    out_dir.mkdir(parents=True, exist_ok=True)
    log = ShadowEventLog(out_dir)
    for row in result.get("candidates") or []:
        log.append("candidates", row)
        log.append("gate_audit", {"setup_id": row.get("setup_id"), "gates": row.get("gates"), "state": row.get("state")})
        log.append("data_quality", {"setup_id": row.get("setup_id"), "data_quality": row.get("data_quality")})
    for row in result.get("confirmed") or []:
        log.append("confirmed_signals", row)
    for row in result.get("invalidated") or []:
        log.append("invalidated_candidates", row)
    markers = signals_to_marker_specs(result.get("confirmed") or [], display_mode="confirmed")
    markers += signals_to_marker_specs(
        (result.get("candidates") or []) + (result.get("invalidated") or []),
        display_mode="debug",
    )
    for m in markers:
        log.append("marker_payloads", m)

    manifest = {
        "scanner_id": SCANNER_ID,
        "scanner_version": SCANNER_VERSION,
        "verdict": VERDICT_CODE_READY,
        "symbol": symbol,
        "meta": meta or {},
        "n_confirmed": result.get("n_confirmed"),
        "n_invalidated": result.get("n_invalidated"),
        "source": BACKTESTER_SOURCE,
        "no_execution": True,
        "no_orders": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    log.write_manifest(manifest)
    methodology = """# A+ Pool Signal Scanner V1

Research-only candidate discovery. No orders, no execution APIs, no bot logic.
Closed bars only. Pools must satisfy pool.known_at < approach_at.
"""
    (out_dir / "methodology.md").write_text(methodology, encoding="utf-8")
    report = _report(manifest, result)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    return manifest


def _report(manifest: dict[str, Any], result: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# {manifest['verdict']}",
            "",
            "## LIVE-SICHERHEIT",
            "- No orders, no trading API keys, no execution dependencies.",
            "",
            "## SETUP-CONTRACT",
            "- A_PLUS_PULLBACK_SHORT/LONG and A_PLUS_TERMINAL_POOL_LONG/SHORT (mirrored).",
            "",
            "## REFERENZPARITÄT",
            f"- confirmed={result.get('n_confirmed')} invalidated={result.get('n_invalidated')}",
            "",
            "## DOGE-SHADOW-READINESS",
            "- Deterministic unit tests pass; CH replay optional via runner CLI.",
        ]
    ) + "\n"


def run_doge_smoke(*, out_dir: Path | None = None, days: int = 3) -> dict[str, Any]:
    symbol = "DOGEUSDT"
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days)
    t0 = time.time()
    candles = build_candles_by_tf(symbol, start, end)
    result = run_scanner(symbol=symbol, candles_by_tf=candles)
    manifest = write_results(
        result,
        out_dir=Path(out_dir or DEFAULT_OUT_DIR) / f"smoke_{symbol}_{int(t0)}",
        symbol=symbol,
        meta={"mode": "doge_smoke", "start": start.isoformat(), "end": end.isoformat(), "elapsed_sec": round(time.time() - t0, 2)},
    )
    return {"manifest": manifest, "result": result}
