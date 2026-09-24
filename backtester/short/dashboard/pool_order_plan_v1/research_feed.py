"""Read registered Pool V1 research artifacts. No planner. No collector. No live orders."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import EXPECTED_PLANNER_COMMIT, REPO_ROOT, STRATEGY_ID, enable_pool_order_plan_v1
from .metrics import strategy_stats
from .schema import (
    STATUS_NO_PLAN,
    STATUS_READY,
    is_clickhouse_candle_source,
    is_confirmed_5m_pool_run,
    is_test_fixture_only,
    last_5m_close_from_open,
    pool_pipeline_stamp,
)

MISSING_MESSAGE = "Pool-V1-Artefakt nicht verfügbar"
DEFAULT_REGISTRY = Path(__file__).resolve().parent / "research_registry.json"
BANNER_TITLE = "RESEARCH / BACKTEST ONLY"
BANNER_BODY = (
    "Source: ClickHouse 1m\n"
    "Pool calculation: closed 5m candles\n"
    "Historische, am Entry eingefrorene Pool-Snapshots.\n"
    "Keine Live-Strategie und keine Orderausführung."
)


def registry_path(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("POOL_ORDER_PLAN_RESEARCH_REGISTRY") or "").strip()
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
    if not isinstance(data, dict):
        return None
    return data


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


def validate_research_artifact(
    run_dir: Path,
    registry: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
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
    if manifest.get("productive") is not True:
        return None, "not_productive"
    planner = manifest.get("planner") if isinstance(manifest.get("planner"), dict) else {}
    if planner.get("pin_ok") is not True:
        return None, "planner_pin_invalid"
    expected = str(registry.get("planner_version") or EXPECTED_PLANNER_COMMIT)
    if str(planner.get("commit") or "") != expected:
        return None, "planner_commit_mismatch"
    sg = manifest.get("signal_generator") if isinstance(manifest.get("signal_generator"), dict) else {}
    hashes = sg.get("file_hashes") if isinstance(sg.get("file_hashes"), dict) else {}
    if not hashes:
        return None, "signal_generator_hash_missing"
    if not is_confirmed_5m_pool_run(manifest) and str(registry.get("pool_interval") or "") != "5m":
        return None, "not_5m_pool_run"
    symbol = str(registry.get("symbol") or "").upper()
    if symbol and [s.upper() for s in (manifest.get("window") or {}).get("symbols") or []] not in (
        [],
        [symbol],
    ):
        win_syms = [str(s).upper() for s in (manifest.get("window") or {}).get("symbols") or []]
        if win_syms and win_syms != [symbol]:
            return None, "symbol_mismatch"
    return manifest, None


def research_artifact_available(environ: dict | None = None) -> bool:
    if not enable_pool_order_plan_v1(environ):
        return False
    payload, _err = load_validated_run(environ)
    return payload is not None


def load_validated_run(environ: dict | None = None) -> tuple[dict[str, Any] | None, str | None]:
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
    outcomes = _read_jsonl(run_dir / "outcomes.jsonl")
    ignored = _read_jsonl(run_dir / "ignored_duplicates.jsonl")
    allowed = str(reg.get("symbol") or "").upper()
    if allowed:
        leaked = [r for r in outcomes if str(r.get("symbol") or "").upper() != allowed]
        if leaked:
            return None, "mixed_symbols"
        outcomes = [r for r in outcomes if str(r.get("symbol") or "").upper() == allowed]
    return {
        "registry": reg,
        "manifest": manifest,
        "run_dir": run_dir,
        "outcomes": outcomes,
        "ignored": ignored,
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
    if row.get("plan_status") == STATUS_NO_PLAN or not row.get("plan_status"):
        return "NO_PLAN"
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
    open_outcome = str(row.get("outcome") or "").upper() == "OPEN"
    no_plan = row.get("plan_status") == STATUS_NO_PLAN
    hold = row.get("hold_minutes")
    duration = None
    if hold is not None:
        try:
            duration = int(float(hold) * 60)
        except (TypeError, ValueError):
            duration = None
    net = None if open_outcome or no_plan else row.get("net_pnl_pct")
    gross = None if open_outcome or no_plan else row.get("gross_pnl_pct")
    fees = None if open_outcome or no_plan else row.get("fees_pct")
    sl = None if no_plan else row.get("sl_price")
    tp1 = None if no_plan else row.get("tp1_price")
    tp2 = None if no_plan else row.get("tp2_price")
    entry_time = _iso(row.get("entry_time"))
    last_open = _iso(row.get("last_5m_open"))
    last_close = _iso(row.get("last_5m_close"))
    close_derived = bool(row.get("last_5m_close_derived"))
    if last_open and not last_close:
        last_close = _iso(last_5m_close_from_open(last_open))
        close_derived = True
    pipe = pool_pipeline_stamp()
    for key, val in pipe.items():
        if registry.get(key) is not None:
            pipe[key] = registry.get(key)
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_ID,
        "research_mode": True,
        "research_only": True,
        "live_trading": False,
        "run_id": manifest.get("run_id") or registry.get("research_run_id"),
        "research_run_id": registry.get("research_run_id"),
        "symbol": row.get("symbol"),
        "window_start": registry.get("window_start"),
        "window_end": registry.get("window_end"),
        "snapshot_as_of": _iso(row.get("snapshot_as_of")) or registry.get("snapshot_as_of"),
        "signal_id": row.get("signal_id"),
        "entry_time": entry_time,
        "candle_close_time": entry_time,
        "signal_time": entry_time,
        "entry_price": row.get("entry_price"),
        "direction": row.get("direction"),
        "trade_direction": row.get("direction"),
        "timeframe": row.get("timeframe"),
        "signal_timeframe": row.get("signal_timeframe") or row.get("timeframe"),
        "pool_timeframe": "5m",
        "chart_timeframe": "separate",
        "plan_status": row.get("plan_status"),
        "no_plan_reason": row.get("no_plan_reason"),
        "initial_target_mode": row.get("initial_target_mode"),
        "entry_pool_count": row.get("entry_pool_count"),
        "sl_price": sl,
        "sl_distance_pct": None if no_plan else row.get("sl_distance_pct"),
        "sl_too_wide": False if no_plan else bool(row.get("sl_too_wide")),
        "sl_cluster": None if no_plan else row.get("sl_cluster"),
        "tp1_price": tp1,
        "tp1_size": None if no_plan else row.get("tp1_size"),
        "tp1_cluster": None if no_plan else row.get("tp1_cluster"),
        "tp2_price": tp2,
        "tp2_size": None if no_plan else row.get("tp2_size"),
        "tp2_cluster": None if no_plan else row.get("tp2_cluster"),
        "tp2_skip_reason": None if no_plan else row.get("tp2_skip_reason"),
        "tp_price": tp1,
        "outcome": "OPEN" if open_outcome else row.get("outcome"),
        "legs": [] if no_plan else (row.get("legs") or []),
        "exit_time": None if open_outcome or no_plan else row.get("exit_time"),
        "gross_pnl_pct": gross,
        "fees_pct": fees,
        "net_pnl_pct": net,
        "pnl_pct": net,
        "hold_minutes": hold,
        "duration_seconds": duration,
        "last_5m_open": last_open,
        "last_5m_close": last_close,
        "last_5m_close_derived": close_derived,
        "causal_5m_bars": row.get("causal_5m_bars"),
        "source_interval": pipe["source_interval"],
        "pool_interval": pipe["pool_interval"],
        "aggregation": pipe["aggregation"],
        "pool_engine": pipe["pool_engine"],
        "pool_lookback": pipe["pool_lookback"],
        "pool_warmup_days": pipe["pool_warmup_days"],
        "replay": pipe["replay"],
        "pool_candle_source": "clickhouse",
        "result": _display_result(row),
        "display_result": _display_result(row),
        "pool_research": True,
        "is_demo": False,
    }


def _pool_summary(rows: list[dict[str, Any]], ignored_n: int) -> dict[str, Any]:
    stats = strategy_stats(rows, kind="pool")
    ready = sum(1 for r in rows if r.get("plan_status") == STATUS_READY)
    no_plan = sum(1 for r in rows if r.get("plan_status") == STATUS_NO_PLAN)
    sl_wide = sum(1 for r in rows if r.get("sl_too_wide"))
    one_target = 0
    two_targets = 0
    for r in rows:
        if r.get("plan_status") != STATUS_READY:
            continue
        s1 = r.get("tp1_size")
        s2 = r.get("tp2_size")
        try:
            n1 = float(s1) if s1 is not None else 0.0
        except (TypeError, ValueError):
            n1 = 0.0
        try:
            n2 = float(s2) if s2 is not None else 0.0
        except (TypeError, ValueError):
            n2 = 0.0
        if n2 > 0 and abs(n1 - 0.5) < 1e-6 and abs(n2 - 0.5) < 1e-6:
            two_targets += 1
        elif abs(n1 - 1.0) < 1e-6 and n2 <= 0:
            one_target += 1
    return {
        "signals": len(rows),
        "ready": ready,
        "no_plan": no_plan,
        "wins": stats["wins"],
        "losses": stats["losses"],
        "open": stats["open"],
        "win_rate_pct": stats["win_rate_pct"],
        "gross_profit_pct": stats["gross_profit_pct"],
        "gross_loss_pct": stats["gross_loss_pct"],
        "gross_pnl_pct": stats["gross_pnl_pct"],
        "total_pnl_pct": stats["net_pnl_pct"],
        "fees_pct": stats["fees_pct"],
        "net_pnl_pct": stats["net_pnl_pct"],
        "profit_factor": stats["profit_factor"],
        "max_drawdown_pct": stats["max_drawdown_pct"],
        "sl_too_wide_count": sl_wide,
        "one_target_count": one_target,
        "two_target_count": two_targets,
        "ignored_duplicates": ignored_n,
        "pnl_basis": "net_after_fees",
        "pnl_basis_note": "Pool-V1 PnL: net after fees. Baseline PnL: gross.",
        "strategy_version": STRATEGY_ID,
        "strategy_id": STRATEGY_ID,
    }


def missing_response() -> dict[str, Any]:
    return {
        "success": True,
        "feed_ready": False,
        "message": MISSING_MESSAGE,
        "error": "pool_v1_artifact_unavailable",
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
        "pool_candle_source": None,
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
    packed, err = load_validated_run(environ)
    if packed is None:
        return missing_response()
    registry = packed["registry"]
    manifest = packed["manifest"]
    rows = [_map_row(r, registry=registry, manifest=manifest) for r in packed["outcomes"]]
    if symbol:
        want = symbol.strip().upper()
        rows = [r for r in rows if str(r.get("symbol") or "").upper() == want]
    if timeframe:
        want_tf = timeframe.strip()
        rows = [r for r in rows if str(r.get("timeframe") or "") == want_tf]
    if direction:
        want_d = direction.strip().upper()
        rows = [r for r in rows if str(r.get("direction") or "").upper() == want_d]
    filtered_src = packed["outcomes"]
    if symbol:
        want = symbol.strip().upper()
        filtered_src = [r for r in filtered_src if str(r.get("symbol") or "").upper() == want]
    if timeframe:
        want_tf = timeframe.strip()
        filtered_src = [r for r in filtered_src if str(r.get("timeframe") or "") == want_tf]
    if direction:
        want_d = direction.strip().upper()
        filtered_src = [r for r in filtered_src if str(r.get("direction") or "").upper() == want_d]
    ignored_n = len(packed["ignored"]) if not (symbol or timeframe or direction) else 0
    summary = _pool_summary(filtered_src, ignored_n)
    return {
        "success": True,
        "feed_ready": True,
        "message": None,
        "signals": rows,
        "items": rows,
        "summary": summary,
        "total": len(rows),
        "page": 1,
        "page_size": len(rows),
        "strategy_version": STRATEGY_ID,
        "strategy_id": STRATEGY_ID,
        "research_mode": True,
        "research_only": True,
        "live_trading": False,
        "run_id": manifest.get("run_id"),
        "research_run_id": registry.get("research_run_id"),
        "symbol": registry.get("symbol"),
        "window_start": registry.get("window_start"),
        "window_end": registry.get("window_end"),
        "snapshot_as_of": registry.get("snapshot_as_of"),
        "pool_candle_source": "clickhouse",
        "source_interval": "1m",
        "pool_interval": "5m",
        "aggregation": "strict_contiguous_1m_to_5m",
        "collector_called": False,
        "banner": {
            "title": BANNER_TITLE,
            "body": BANNER_BODY,
            "window_label": (
                f"{registry.get('symbol')}\n"
                f"{str(registry.get('window_start') or '').replace('T', ' ').replace('Z', ' UTC')} – "
                f"{str(registry.get('window_end') or '').replace('T', ' ').replace('Z', ' UTC')}"
            ),
            "window_start": registry.get("window_start"),
            "window_end": registry.get("window_end"),
        },
        "dashboard_signal_source": "FROZEN_BASELINE",
    }


def chart_payload_for_signal(signal_id: str, environ: dict | None = None) -> dict[str, Any] | None:
    packed, _err = load_validated_run(environ)
    if packed is None:
        return None
    for row in packed["outcomes"]:
        if str(row.get("signal_id") or "") == str(signal_id):
            mapped = _map_row(row, registry=packed["registry"], manifest=packed["manifest"])
            return {
                "signal_id": mapped["signal_id"],
                "symbol": mapped["symbol"],
                "entry_time": mapped["entry_time"],
                "entry_price": mapped["entry_price"],
                "direction": mapped["direction"],
                "sl_price": mapped["sl_price"],
                "tp1_price": mapped["tp1_price"],
                "tp1_size": mapped["tp1_size"],
                "tp2_price": mapped["tp2_price"],
                "tp2_size": mapped["tp2_size"],
                "legs": mapped["legs"],
                "sl_cluster": mapped["sl_cluster"],
                "tp1_cluster": mapped["tp1_cluster"],
                "tp2_cluster": mapped["tp2_cluster"],
                "snapshot_as_of": mapped["snapshot_as_of"],
                "last_5m_open": mapped["last_5m_open"],
                "last_5m_close": mapped["last_5m_close"],
                "last_5m_close_derived": mapped["last_5m_close_derived"],
                "causal_5m_bars": mapped["causal_5m_bars"],
                "signal_timeframe": mapped["signal_timeframe"],
                "pool_timeframe": mapped["pool_timeframe"],
                "source_interval": mapped["source_interval"],
                "pool_interval": mapped["pool_interval"],
                "aggregation": mapped["aggregation"],
                "entry_pool_count": mapped["entry_pool_count"],
                "sl_too_wide": mapped["sl_too_wide"],
                "sl_distance_pct": mapped["sl_distance_pct"],
                "exit_time": mapped["exit_time"],
                "window_start": mapped["window_start"],
                "window_end": mapped["window_end"],
                "plan_status": mapped["plan_status"],
                "outcome": mapped["outcome"],
            }
    return None


def load_research_klines(signal_id: str, environ: dict | None = None) -> dict[str, Any]:
    payload = chart_payload_for_signal(signal_id, environ)
    if payload is None:
        return {
            "success": True,
            "candles": [],
            "source": "none",
            "message": MISSING_MESSAGE,
        }
    from .candles import build_five_minute_series, ensure_utc
    from .signals import load_closed_1m

    symbol = str(payload.get("symbol") or "")
    try:
        entry = ensure_utc(payload.get("entry_time"))
        end_raw = payload.get("window_end") or payload.get("snapshot_as_of")
        end = ensure_utc(end_raw) if end_raw else entry + timedelta(hours=48)
        start = entry - timedelta(hours=6)
        rows = load_closed_1m(symbol, start=start, end=end)
        series = build_five_minute_series(symbol, rows)
        candles = []
        if not series.bars.empty:
            for _, bar in series.bars.iterrows():
                ts = bar["timestamp"]
                if hasattr(ts, "to_pydatetime"):
                    dt = ts.to_pydatetime()
                else:
                    dt = ts
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                candles.append(
                    {
                        "time": int(dt.timestamp()),
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                    }
                )
        return {
            "success": True,
            "candles": candles,
            "source": "clickhouse_candles_1m",
            "chart": payload,
        }
    except Exception as exc:
        return {
            "success": True,
            "candles": [],
            "source": "clickhouse_unavailable",
            "chart": payload,
            "message": str(exc),
        }
