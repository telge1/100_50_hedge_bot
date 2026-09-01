"""A+ Pool Signal research markers for Research Charts (no execution)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BACKTESTER_SOURCE = "a_plus_pool_signal_scanner_v1"
STRATEGY_ID = "a_plus_liquidity_pool_signal_scanner_v1"
RESULTS_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "a_plus_liquidity_pool_signal_scanner_v1"
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _manifest_symbol(meta: dict[str, Any], dirname: str) -> str:
    for key in ("symbol", "Symbol"):
        v = meta.get(key)
        if v:
            return str(v).strip().upper()
    nested = meta.get("meta") if isinstance(meta.get("meta"), dict) else {}
    if nested.get("symbol"):
        return str(nested["symbol"]).strip().upper()
    # dirname convention: doge_reference_replay_… / smoke_DOGEUSDT_…
    parts = dirname.lower().replace("-", "_").split("_")
    for p in parts:
        if p.endswith("usdt") and len(p) >= 7:
            return p.upper()
    return ""


def find_latest_run_dir(symbol: str, *, results_root: Path | None = None) -> Path | None:
    """Newest results folder with confirmed_signals.jsonl for this symbol."""
    root = Path(results_root or RESULTS_ROOT)
    if not root.is_dir():
        return None
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    best: Path | None = None
    best_mtime = -1.0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        conf = d / "confirmed_signals.jsonl"
        if not conf.is_file():
            continue
        meta: dict[str, Any] = {}
        mp = d / "manifest.json"
        if mp.is_file():
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
            except json.JSONDecodeError:
                meta = {}
        run_sym = _manifest_symbol(meta, d.name)
        if run_sym and run_sym != sym:
            continue
        if not run_sym:
            # dirname often uses base coin only: doge_reference_replay_…
            base = sym.lower().removesuffix("usdt").removesuffix("usd")
            if not base or base not in d.name.lower():
                continue
        mtime = conf.stat().st_mtime
        if mtime > best_mtime:
            best = d
            best_mtime = mtime
    return best


def load_run_dir_payload(path: Path | str, *, symbol: str | None = None) -> dict[str, Any]:
    p = Path(path)
    confirmed = _read_jsonl(p / "confirmed_signals.jsonl")
    debug_rows = _read_jsonl(p / "candidates.jsonl") + _read_jsonl(p / "invalidated_candidates.jsonl")
    signal_intents = _read_jsonl(p / "signal_intents.jsonl")
    candidates = _read_jsonl(p / "candidates.jsonl")
    meta: dict[str, Any] = {}
    mp = p / "manifest.json"
    if mp.is_file():
        try:
            raw = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta = raw
        except json.JSONDecodeError:
            meta = {}
    sym = (
        str(symbol or "").strip().upper()
        or _manifest_symbol(meta, p.name)
        or str((meta.get("meta") or {}).get("symbol") or "").strip().upper()
    )
    return {
        "meta": {
            **meta,
            "symbol": sym,
            "import_path": str(p),
            "strategy_id": STRATEGY_ID,
            "source": BACKTESTER_SOURCE,
            "n_confirmed": len(confirmed),
        },
        "confirmed": confirmed,
        "debug_rows": debug_rows,
        "signal_intents": signal_intents,
        "candidates": candidates,
    }


def auto_import_latest_for_symbol(symbol: str, *, results_root: Path | None = None) -> dict[str, Any] | None:
    path = find_latest_run_dir(symbol, results_root=results_root)
    if path is None:
        return None
    return load_run_dir_payload(path, symbol=symbol)


def run_pool_signals_backtest(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Live CH scan for the given window — research only, no orders."""
    from .oa_import import ensure_oa_on_path

    ensure_oa_on_path()
    from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import (  # noqa: WPS433
        build_candles_by_tf,
        run_scanner,
        write_results,
    )

    sym = str(symbol).strip().upper()
    start_u = _utc(start).replace(tzinfo=None)
    end_u = _utc(end).replace(tzinfo=None)
    if end_u <= start_u:
        raise ValueError("end must be after start")
    # Cap runaway ranges (scanner is 1m-bar walk)
    max_days = 7
    if (end_u - start_u).total_seconds() > max_days * 86400:
        start_u = end_u - timedelta(days=max_days)
    candles = build_candles_by_tf(sym, start_u, end_u)
    if not candles:
        raise ValueError(f"no candles for {sym} in range")
    result = run_scanner(symbol=sym, candles_by_tf=candles)
    stamp = int(datetime.now(timezone.utc).timestamp())
    dest = Path(out_dir or RESULTS_ROOT) / f"dashboard_{sym.lower()}_{stamp}"
    write_results(
        result,
        out_dir=dest,
        symbol=sym,
        meta={
            "mode": "dashboard_backtester",
            "start": start_u.isoformat(),
            "end": end_u.isoformat(),
        },
    )
    payload = load_scanner_payload_from_results({**result, "symbol": sym})
    payload["meta"] = {
        **(payload.get("meta") or {}),
        "symbol": sym,
        "import_path": str(dest),
        "start": start_u.isoformat(),
        "end": end_u.isoformat(),
        "n_confirmed": len(payload.get("confirmed") or []),
    }
    return payload


def build_overlay_markers(marker_specs: list[dict[str, Any]], *, symbol: str) -> list[Any]:
    if not marker_specs:
        return []
    from .trp_import import load_trp

    trp = load_trp()
    OverlayMarker = trp["OverlayMarker"]
    OverlayLine = trp["OverlayLine"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    sym = str(symbol).upper()
    out: list[Any] = []
    for spec in marker_specs:
        kind = str(spec.get("kind") or "")
        if kind == "APS_LINE":
            out.append(
                OverlayLine(
                    overlay_id=str(spec["overlay_id"]),
                    symbol=sym,
                    kind="horizontal",
                    price=float(spec["price"]),
                    style=OverlayStyle(color=spec.get("color") or "#888888", width=1.0, opacity=0.85),
                    label_text="",  # never flood price axis with ENTRY/TP/SL tags

                    timeframe_scope="all",
                    visible=True,
                    z_order=35,
                    metadata={
                        "origin": BACKTESTER_SOURCE,
                        "strategy_id": STRATEGY_ID,
                        "kind": kind,
                        "setup_id": spec.get("setup_id"),
                        "research_note": "Research Signal – keine ausgeführte Order",
                    },
                )
            )
            continue
        meta = {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "kind": kind,
            "setup_id": spec.get("setup_id"),
            "direction": spec.get("direction"),
            "research_note": "Research Signal – keine ausgeführte Order",
            "tooltip": spec.get("tooltip"),
            # Keep payload small — nested full signal blew up overlays JSON and
            # slowed chart sync; tooltip already has the research summary.
        }
        out.append(
            OverlayMarker(
                overlay_id=str(spec["overlay_id"]),
                symbol=sym,
                timestamp=ensure_utc(spec["timestamp"]),
                price=spec.get("price"),
                position=spec.get("position") or "at_price",
                shape=spec.get("shape") or "circle",
                text=str(spec.get("text") or ""),
                size=10.0 if kind == "APS_CONFIRMED" else 8.0,
                style=OverlayStyle(color=spec.get("color") or "#888888", width=1.0),
                timeframe_scope="all",
                visible=True,
                z_order=44 if kind == "APS_CONFIRMED" else 38,
                metadata=meta,
            )
        )
    return out


def load_scanner_payload_from_results(result: dict[str, Any]) -> dict[str, Any]:
    confirmed = list(result.get("confirmed") or [])
    debug_rows = list(result.get("candidates") or []) + list(result.get("invalidated") or [])
    return {
        "meta": {"symbol": result.get("symbol"), "strategy_id": STRATEGY_ID, "source": BACKTESTER_SOURCE},
        "confirmed": confirmed,
        "debug_rows": debug_rows,
        "signal_intents": list(result.get("signal_intents") or []),
        "candidates": list(result.get("candidates") or []),
    }
