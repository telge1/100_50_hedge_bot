"""Read registered EMA_POOL_TREND_FLIP_V1 artifacts. No planner. No live orders."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import EXPECTED_PLANNER_COMMIT, REPO_ROOT, STATIC_VARIANT, STRATEGY_ID, enable_ema_pool_trend_flip_v1
from .schema import DECISION_NO_TRADE, is_clickhouse_candle_source, is_test_fixture_only

MISSING_MESSAGE = "EMA-Pool-Trend-Flip-Artefakt nicht verfügbar"
DEFAULT_REGISTRY = Path(__file__).resolve().parent / "research_registry.json"
BANNER_TITLE = "RESEARCH / BACKTEST ONLY"
BANNER_BODY = (
    "Source: ClickHouse 1m\n"
    "Pool calculation: closed 5m candles\n"
    "EMA calculation: closed signal-timeframe candles\n"
    "No live orders"
)


def registry_path(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("EMA_POOL_TREND_FLIP_RESEARCH_REGISTRY") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_REGISTRY


def load_registry(environ: dict | None = None) -> dict[str, Any] | None:
    path = registry_path(environ)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def artifact_dir_from_registry(reg: dict[str, Any]) -> Path | None:
    rel = str(reg.get("artifact_relpath") or "").strip()
    abs_override = str(reg.get("artifact_dir") or "").strip()
    if abs_override:
        return Path(abs_override)
    if not rel:
        return None
    return REPO_ROOT / rel


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def validate_research_artifact(run_dir: Path, registry: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if not run_dir.is_dir():
        return None, "artifact_dir_missing"
    if (run_dir / "ABORTED.json").is_file():
        return None, "aborted"
    manifest = _read_json(run_dir / "manifest.json")
    if not manifest:
        return None, "manifest_missing"
    if is_test_fixture_only(manifest):
        return None, "fixture_rejected"
    if not is_clickhouse_candle_source(manifest):
        return None, "csv_or_non_clickhouse_rejected"
    if manifest.get("complete") is not True:
        return None, "incomplete"
    if manifest.get("strategy_id") != STRATEGY_ID:
        return None, "strategy_mismatch"
    planner = manifest.get("planner") if isinstance(manifest.get("planner"), dict) else {}
    if planner.get("pin_ok") is not True:
        return None, "planner_pin_invalid"
    expected = str(registry.get("planner_version") or EXPECTED_PLANNER_COMMIT)
    if str(planner.get("commit") or "") != expected:
        return None, "planner_commit_mismatch"
    return manifest, None


def load_validated_run(environ: dict | None = None) -> tuple[dict[str, Any] | None, str | None]:
    if not enable_ema_pool_trend_flip_v1(environ):
        return None, "disabled"
    reg = load_registry(environ)
    if not reg:
        return None, "registry_missing"
    if str(reg.get("strategy_id") or "") != STRATEGY_ID:
        return None, "strategy_mismatch"
    if reg.get("research_only") is not True or reg.get("live_trading") is not False:
        return None, "not_research_only"
    run_dir = artifact_dir_from_registry(reg)
    if run_dir is None:
        return None, "artifact_path_missing"
    manifest, err = validate_research_artifact(run_dir, reg)
    if err or manifest is None:
        return None, err or "invalid"
    trades = _read_jsonl(run_dir / "trades.jsonl")
    blocked = _read_jsonl(run_dir / "blocked_signals.jsonl")
    ignored = _read_jsonl(run_dir / "ignored_duplicates.jsonl")
    summary = _read_json(run_dir / "summary.json")
    return {
        "registry": reg,
        "manifest": manifest,
        "run_dir": run_dir,
        "trades": trades,
        "blocked": blocked,
        "ignored": ignored,
        "summary": summary,
    }, None


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    text = str(ts).strip()
    if not text:
        return None
    text = text.replace(" ", "T")
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def _display_result(row: dict[str, Any]) -> str:
    if row.get("decision") in (DECISION_NO_TRADE, "BLOCKED"):
        return str(row.get("decision"))
    oc = str(row.get("outcome") or "OPEN").upper()
    if oc == "OPEN":
        return "OPEN"
    gross = row.get("gross_pnl_pct")
    if gross is None:
        return oc
    if float(gross) > 0:
        return "WIN"
    if float(gross) < 0:
        return "LOSS"
    return "FLAT"


def _map_row(row: dict[str, Any], *, registry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    hold = row.get("hold_minutes")
    duration = None
    if hold is not None:
        try:
            duration = int(float(hold) * 60)
        except (TypeError, ValueError):
            duration = None
    open_outcome = str(row.get("outcome") or "").upper() == "OPEN"
    no_trade = row.get("decision") in (DECISION_NO_TRADE, "BLOCKED")
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_ID,
        "research_mode": True,
        "research_only": True,
        "live_trading": False,
        "ema_flip_research": True,
        "pool_research": False,
        "run_id": manifest.get("run_id") or registry.get("research_run_id"),
        "signal_id": row.get("signal_id"),
        "symbol": row.get("symbol"),
        "signal_time": _iso(row.get("signal_time") or row.get("available_at")),
        "entry_time": _iso(row.get("entry_time")),
        "candle_close_time": _iso(row.get("signal_time") or row.get("entry_time")),
        "entry_price": row.get("entry_price"),
        "original_direction": row.get("original_direction"),
        "executed_direction": row.get("executed_direction"),
        "direction": row.get("executed_direction") or row.get("original_direction"),
        "trade_direction": row.get("executed_direction") or row.get("original_direction"),
        "timeframe": row.get("signal_timeframe"),
        "signal_timeframe": row.get("signal_timeframe"),
        "pool_timeframe": "5m",
        "decision": row.get("decision"),
        "entry_reason": row.get("entry_reason"),
        "no_trade_reason": row.get("no_trade_reason"),
        "ema9": row.get("ema9"),
        "ema20": row.get("ema20"),
        "ema_sep_atr": row.get("ema_sep_atr"),
        "ema_trend": row.get("ema_trend"),
        "last_confirmed_cross": row.get("last_confirmed_cross"),
        "upper_pool_bias_score": row.get("upper_pool_bias_score"),
        "lower_pool_bias_score": row.get("lower_pool_bias_score"),
        "protection_pool": row.get("protection_pool"),
        "sl_cluster": row.get("sl_cluster") or row.get("protection_pool"),
        "sl_price": None if no_trade else row.get("sl_price"),
        "sl_distance_pct": None if no_trade else row.get("sl_distance_pct"),
        "sl_too_wide": False if no_trade else bool(row.get("sl_too_wide")),
        "exit_time": None if open_outcome or no_trade else row.get("exit_time"),
        "exit_reason": row.get("exit_reason"),
        "gross_pnl_pct": None if open_outcome or no_trade else row.get("gross_pnl_pct"),
        "fees_pct": None if open_outcome or no_trade else row.get("fees_pct"),
        "net_pnl_pct": None if open_outcome or no_trade else row.get("net_pnl_pct"),
        "pnl_pct": None if open_outcome or no_trade else row.get("net_pnl_pct"),
        "hold_minutes": hold,
        "duration_seconds": duration,
        "variant": row.get("variant"),
        "ratchet_steps": row.get("ratchet_steps") or [],
        "active_upper_pools": row.get("active_upper_pools") or [],
        "active_lower_pools": row.get("active_lower_pools") or [],
        "weak_cross_candidates": row.get("weak_cross_candidates") or [],
        "confirmed_cross_events": row.get("confirmed_cross_events") or [],
        "outcome": "OPEN" if open_outcome else row.get("outcome"),
        "result": _display_result(row),
        "display_result": _display_result(row),
        "tp1_price": None,
        "tp2_price": None,
        "is_demo": False,
        "flipped": row.get("decision") == "FLIPPED",
        "aligned": row.get("decision") == "ALIGNED",
    }


def missing_response() -> dict[str, Any]:
    return {
        "success": True,
        "feed_ready": False,
        "message": MISSING_MESSAGE,
        "error": "ema_pool_trend_flip_artifact_unavailable",
        "signals": [],
        "items": [],
        "summary": None,
        "total": 0,
        "page": 1,
        "page_size": 0,
        "strategy_version": STRATEGY_ID,
        "strategy_id": STRATEGY_ID,
        "research_mode": True,
        "research_only": True,
        "live_trading": False,
        "collector_called": False,
        "banner": {"title": BANNER_TITLE, "body": BANNER_BODY},
    }


def research_signals_response(
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    direction: str | None = None,
    environ: dict | None = None,
) -> dict[str, Any]:
    payload, err = load_validated_run(environ)
    if payload is None:
        return missing_response()
    rows = list(payload["trades"]) + list(payload["blocked"])
    rows = [r for r in rows if r.get("variant") in (None, STATIC_VARIANT, "EMA_POOL_TREND_FLIP_V1_STATIC", "EMA_POOL_TREND_FLIP_V1_RATCHET") or r.get("decision") in (DECISION_NO_TRADE, "BLOCKED")]
    # Prefer showing STATIC + RATCHET trades and blocked; drop baseline/filter from the live table.
    keep = []
    for r in payload["trades"]:
        if r.get("variant") in (STATIC_VARIANT, "EMA_POOL_TREND_FLIP_V1_RATCHET"):
            keep.append(r)
    keep.extend(payload["blocked"])
    if symbol:
        want = symbol.strip().upper()
        keep = [r for r in keep if str(r.get("symbol") or "").upper() == want]
        if str(payload["registry"].get("symbol") or "").upper() not in ("", want):
            keep = []
    if timeframe:
        keep = [r for r in keep if str(r.get("signal_timeframe") or "") == timeframe]
    if direction:
        d = direction.strip().upper()
        keep = [r for r in keep if str(r.get("executed_direction") or r.get("original_direction") or "").upper() == d]
    mapped = [_map_row(r, registry=payload["registry"], manifest=payload["manifest"]) for r in keep]
    summary = payload.get("summary") or {}
    static_s = summary.get("STATIC") or {}
    return {
        "success": True,
        "feed_ready": True,
        "message": None,
        "signals": mapped,
        "items": mapped,
        "summary": {
            "signals": len(mapped),
            "wins": static_s.get("wins"),
            "losses": static_s.get("losses"),
            "open": static_s.get("open"),
            "win_rate_pct": static_s.get("win_rate_pct"),
            "gross_pnl_pct": static_s.get("gross_pnl_pct"),
            "fees_pct": static_s.get("fees_pct"),
            "net_pnl_pct": static_s.get("net_pnl_pct"),
            "total_pnl_pct": static_s.get("net_pnl_pct"),
            "profit_factor": static_s.get("profit_factor"),
            "max_drawdown_pct": static_s.get("max_drawdown_pct"),
            "sl_too_wide_count": static_s.get("sl_too_wide"),
            "strategy_version": STRATEGY_ID,
            "strategy_id": STRATEGY_ID,
        },
        "total": len(mapped),
        "page": 1,
        "page_size": len(mapped),
        "strategy_version": STRATEGY_ID,
        "strategy_id": STRATEGY_ID,
        "research_mode": True,
        "research_only": True,
        "live_trading": False,
        "collector_called": False,
        "banner": {"title": BANNER_TITLE, "body": BANNER_BODY},
        "window_label": f"{payload['registry'].get('window_start')} → {payload['registry'].get('window_end')}",
    }


def chart_payload_for_signal(signal_id: str, environ: dict | None = None) -> dict[str, Any] | None:
    payload, _err = load_validated_run(environ)
    if payload is None:
        return None
    sid = str(signal_id)
    for row in list(payload["trades"]) + list(payload["blocked"]):
        if str(row.get("signal_id") or "") == sid:
            return _map_row(row, registry=payload["registry"], manifest=payload["manifest"])
    return None


def load_research_klines(signal_id: str, environ: dict | None = None) -> dict[str, Any]:
    """ClickHouse 1m candles only. No pool/EMA/backtest compute."""
    mapped = chart_payload_for_signal(signal_id, environ)
    if mapped is None:
        return {"success": True, "candles": [], "source": "none", "message": MISSING_MESSAGE}
    from datetime import datetime, timedelta, timezone

    from pool_order_plan_v1.candles import ensure_utc
    from pool_order_plan_v1.signals import load_closed_1m

    symbol = str(mapped.get("symbol") or "")
    try:
        entry = ensure_utc(mapped.get("entry_time"))
        start = entry - timedelta(hours=12)
        end = entry + timedelta(hours=36)
        rows = load_closed_1m(symbol, start=start, end=end)
        candles = []
        for row in rows:
            dt = ensure_utc(row["open_time"])
            candles.append(
                {
                    "time": int(dt.replace(tzinfo=timezone.utc).timestamp())
                    if dt.tzinfo
                    else int(dt.replace(tzinfo=timezone.utc).timestamp()),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        return {
            "success": True,
            "candles": candles,
            "source": "clickhouse_candles_1m",
            "chart": mapped,
        }
    except Exception as exc:
        return {
            "success": True,
            "candles": [],
            "source": "clickhouse_unavailable",
            "chart": mapped,
            "message": str(exc),
        }
