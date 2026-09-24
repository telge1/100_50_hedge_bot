"""EZM candidate-discovery coin runner for stoch-signale jobs.

Uses compile_candidate_discovery_v2 + continuous discovery only.
No trade compiler, no PnL, no shell args from browser input.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from stoch_universe_51.jsonio import write_json_atomic

from .config import (
    EZM_RESULT_CONTRACT_VERSION,
    EZM_RUN_INTENT,
    EZM_RUNNER_KIND,
    EZM_STRATEGY_ID,
    normalize_ezm_computation_mode,
    oa_raw_root,
    oa_root,
)
from .strategy_resolve import is_ezm_strategy

CONFIRMED_DIRECTED_STATES = frozenset(
    {
        "defense_rejection_confirmed",
        "breakout_confirmed",
        "false_breakout_confirmed",
        "possible_regime_flip",
        "full_regime_flip_confirmed",
    }
)

# OA continuous discovery reads orderbook_analysis + signal_generator.
# Stoch worker injects fade_gold_reader (signal_generator only) — override for EZM only.
_OA_CH_KEYS = (
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_HTTP_PORT",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_NATIVE_PORT",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "CLICKHOUSE_SECURE",
    "CLICKHOUSE_VERIFY",
)


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("\"'").strip()
    return out


@contextmanager
def oa_clickhouse_env(environ: dict | None = None) -> Iterator[dict[str, str]]:
    """Temporarily apply orderbook_analyse/.env ClickHouse credentials.

    Read-only SELECT path for EZM coverage/candles/OI/liq. Does not grant write
    intent; restores prior process env on exit so Frozen Fade stays on fade_gold_reader.
    """
    repo = oa_root(environ)
    file_env = _parse_dotenv(repo / ".env")
    applied: dict[str, str] = {}
    for key in _OA_CH_KEYS:
        if key in file_env and str(file_env[key]).strip() != "":
            applied[key] = str(file_env[key]).strip()
    if "CLICKHOUSE_HTTP_PORT" not in applied:
        port = applied.get("CLICKHOUSE_PORT") or file_env.get("CLICKHOUSE_PORT")
        if port:
            applied["CLICKHOUSE_HTTP_PORT"] = str(port).strip()
    missing = [
        k
        for k in ("CLICKHOUSE_HOST", "CLICKHOUSE_HTTP_PORT", "CLICKHOUSE_DATABASE", "CLICKHOUSE_USER")
        if not applied.get(k)
    ]
    if missing:
        raise RuntimeError("EZM_OA_CLICKHOUSE_ENV_INCOMPLETE:" + ",".join(missing))

    previous = {k: os.environ.get(k) for k in _OA_CH_KEYS}
    try:
        for key, value in applied.items():
            os.environ[key] = value
        yield {
            "user": applied.get("CLICKHOUSE_USER", ""),
            "database": applied.get("CLICKHOUSE_DATABASE", ""),
            "host": applied.get("CLICKHOUSE_HOST", ""),
            "http_port": applied.get("CLICKHOUSE_HTTP_PORT", ""),
        }
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _ensure_oa(environ: dict | None = None) -> Path:
    root = oa_root(environ)
    src = root / "src"
    if not src.is_dir():
        raise RuntimeError(f"ORDERBOOK_ANALYSE_SRC_MISSING:{src}")
    path = str(src)
    if path not in sys.path:
        sys.path.insert(0, path)
    return root


def compile_ezm_contract(environ: dict | None = None) -> dict[str, Any]:
    repo = _ensure_oa(environ)
    from orderbook_analyse.ema_zone_microstructure_confirmation import STRATEGY_YAML
    from orderbook_analyse.strategy_lab.decoder_v2 import load_compile_candidate_discovery_v2
    from orderbook_analyse.strategy_lab.validation import production_catalog_bundle_v2

    catalogs = production_catalog_bundle_v2()
    compiled = load_compile_candidate_discovery_v2(repo / STRATEGY_YAML, catalogs)
    return {
        "compiler": "compile_candidate_discovery_v2",
        "trade_compiler_used": False,
        "plugin_id": compiled.plugin_id,
        "strategy_hash": compiled.strategy_hash,
        "candidate_states": list(compiled.candidate_states),
        "strategy_id": EZM_STRATEGY_ID,
        "run_intent": EZM_RUN_INTENT,
    }


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_z(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def clamp_window(
    coverage: dict[str, Any],
    *,
    signal_start: str,
    signal_end_exclusive: str,
) -> tuple[datetime | None, datetime | None, str | None]:
    """Return effective [start, end) or (None, None, reason)."""
    if str(coverage.get("status") or "") != "OK":
        return None, None, str(coverage.get("incomplete_reason") or "DATA_INCOMPLETE")
    disc_start = coverage.get("discovery_start")
    disc_end = coverage.get("discovery_end")
    if not disc_start or not disc_end:
        return None, None, "MISSING_DISCOVERY_WINDOW"
    start = max(_parse_z(str(disc_start)), _parse_z(signal_start))
    end = min(_parse_z(str(disc_end)), _parse_z(signal_end_exclusive))
    if end <= start:
        return None, None, "EMPTY_CLAMPED_WINDOW"
    return start, end, None


def _missing_sources(coverage: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for src in coverage.get("sources") or []:
        if not isinstance(src, dict):
            continue
        status = str(src.get("status") or "")
        name = str(src.get("source") or "unknown")
        if status and status not in {"OK", "ok", "FULL"}:
            missing.append(f"{name}:{status}")
    reason = str(coverage.get("incomplete_reason") or "").strip()
    if reason and not missing:
        missing.append(reason)
    return missing


def candidate_is_chart_marker(row: dict[str, Any]) -> bool:
    if row.get("emit_directional_marker") is False:
        return False
    state = str(row.get("candidate_state") or "")
    direction = str(row.get("candidate_direction") or "").upper()
    if direction in {"", "NONE", "NO_TRADE", "NO_DIRECTION"}:
        return False
    if direction not in {"LONG", "SHORT"}:
        return False
    if state not in CONFIRMED_DIRECTED_STATES:
        return False
    if state in {"possible_regime_flip", "full_regime_flip_confirmed"} and not direction:
        return False
    return True


def candidate_to_signal_row(
    cand: dict[str, Any],
    *,
    strategy_hash: str,
    job_start: str,
    job_end: str,
) -> dict[str, Any] | None:
    if not candidate_is_chart_marker(cand):
        return None
    direction = str(cand.get("candidate_direction") or "").upper()
    decision_at = cand.get("decision_at")
    decision_price = cand.get("decision_price")
    if not decision_at or decision_price is None:
        return None
    episode = str(cand.get("episode_id") or "")
    symbol = str(cand.get("symbol") or "")
    signal_id = f"ezm-{symbol}-{episode}" if episode else f"ezm-{symbol}-{decision_at}"
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "timeframe": "5m",
        "direction": direction,
        "trade_direction": direction,
        "signal_type": "EZM_CANDIDATE",
        "tier_a": True,
        "entry_valid": True,
        "entry_price": decision_price,
        "entry_time": decision_at,
        "tp_price": None,
        "sl_price": None,
        "candle_open_time": decision_at,
        "candle_close_time": decision_at,
        "confirmation_available_at": decision_at,
        "generated_at": decision_at,
        "strategy_version": EZM_STRATEGY_ID,
        "strategy_id": EZM_STRATEGY_ID,
        "run_intent": EZM_RUN_INTENT,
        "signal_state": str(cand.get("candidate_state") or ""),
        "candidate_state": cand.get("candidate_state"),
        "candidate_direction": direction,
        "decision_at": decision_at,
        "decision_price": decision_price,
        "episode_id": episode,
        "zone_name": cand.get("zone_name"),
        "zone_role": cand.get("zone_role_at_watch") or cand.get("zone_role"),
        "zone_role_at_watch": cand.get("zone_role_at_watch") or cand.get("zone_role"),
        "zone_role_at_touch": cand.get("zone_role_at_touch"),
        "zone_role_at_decision": cand.get("zone_role_at_decision"),
        "post_break_role": cand.get("post_break_role"),
        "regime": cand.get("regime"),
        "mechanism": cand.get("mechanism"),
        "approach_direction": cand.get("approach_direction"),
        "reason_codes": cand.get("reason_codes"),
        "format_version": cand.get("format_version"),
        "stage_a_allows_microstructure": cand.get("stage_a_allows_microstructure"),
        "emit_directional_marker": True,
        "possible_regime_flip": cand.get("possible_regime_flip"),
        "full_regime_flip_confirmed": cand.get("full_regime_flip_confirmed"),
        "strategy_spec_hash": strategy_hash,
        "result_contract_version": EZM_RESULT_CONTRACT_VERSION,
        "plan_status": "RESEARCH_CANDIDATE_NO_TRADE",
        "outcomes_computed": False,
        "job_signal_start": job_start,
        "job_signal_end_exclusive": job_end,
        "research_note": "Research Candidate – kein ausgeführter Trade",
    }


def load_ezm_research_layers_from_run_dir(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load two-layer EZM artifacts written by ``run_ezm_coin``."""
    path = run_dir / "candidates.json"
    if not path.is_file():
        return {"ema_setup_events": [], "microstructure_confirmation_events": [], "candidates": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"ema_setup_events": [], "microstructure_confirmation_events": [], "candidates": []}
    candidates = list(data.get("candidates") or [])
    micro = list(data.get("microstructure_confirmation_events") or [])
    if not micro:
        micro = [
            c
            for c in candidates
            if str(c.get("confirmation_mode") or "") == "ema_plus_microstructure"
            or str(c.get("output_layer") or "") == "microstructure_confirmation"
        ]
    return {
        "ema_setup_events": list(data.get("ema_setup_events") or []),
        "microstructure_confirmation_events": micro,
        "candidates": candidates,
    }


def run_ezm_coin(
    *,
    symbol: str,
    signal_start: str,
    signal_end_exclusive: str,
    out_root: Path,
    environ: dict | None = None,
    contract: dict[str, Any] | None = None,
    computation_mode: str | None = None,
) -> dict[str, Any]:
    """Run one symbol; write coin_runs/<symbol>/<run_id>/ artifacts.

    Returns a coin status row (COMPLETED | DATA_INCOMPLETE | FAILED).
    """
    if not is_ezm_strategy(EZM_STRATEGY_ID):
        raise RuntimeError("EZM_STRATEGY_MISMATCH")
    stub = str((environ or {}).get("STOCH_EZM_RUNNER_STUB") or "").strip()
    if not stub:
        stub = str(os.environ.get("STOCH_EZM_RUNNER_STUB") or "").strip()

    run_id = "ezm" + uuid.uuid4().hex[:12]
    run_dir = out_root / run_id
    if run_dir.exists():
        raise RuntimeError(f"NO_OVERWRITE:{run_dir}")

    if stub:
        return _stub_coin(
            symbol=symbol,
            signal_start=signal_start,
            signal_end_exclusive=signal_end_exclusive,
            out_root=out_root,
            run_id=run_id,
            stub=stub,
            contract=contract or {},
        )

    repo = _ensure_oa(environ)
    raw_root = oa_raw_root(environ)
    from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner import (
        run_symbol,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.coverage import (
        probe_symbol_coverage,
    )
    from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.runner import (
        assert_live_safe,
    )

    assert_live_safe()
    contract = contract or compile_ezm_contract(environ)
    strategy_hash = str(contract.get("strategy_hash") or "")
    resolved_computation_mode = normalize_ezm_computation_mode(computation_mode)

    with oa_clickhouse_env(environ):
        coverage = probe_symbol_coverage(symbol=symbol, raw_root=raw_root, computation_mode=resolved_computation_mode)
        missing = _missing_sources(coverage)
        if str(coverage.get("status") or "") != "OK":
            return _write_incomplete(
                run_dir=run_dir,
                symbol=symbol,
                signal_start=signal_start,
                signal_end_exclusive=signal_end_exclusive,
                coverage=coverage,
                missing=missing,
                strategy_hash=strategy_hash,
                reason=str(coverage.get("incomplete_reason") or "DATA_INCOMPLETE"),
            )

        eff_start, eff_end, clamp_err = clamp_window(
            coverage, signal_start=signal_start, signal_end_exclusive=signal_end_exclusive
        )
        if clamp_err or eff_start is None or eff_end is None:
            return _write_incomplete(
                run_dir=run_dir,
                symbol=symbol,
                signal_start=signal_start,
                signal_end_exclusive=signal_end_exclusive,
                coverage=coverage,
                missing=missing or [clamp_err or "EMPTY_CLAMPED_WINDOW"],
                strategy_hash=strategy_hash,
                reason=clamp_err or "EMPTY_CLAMPED_WINDOW",
            )

        # Pass clamped window via coverage discovery_* (engine uses these).
        clamped_cov = dict(coverage)
        clamped_cov["discovery_start"] = _iso_z(eff_start)
        clamped_cov["discovery_end"] = _iso_z(eff_end)

        result = run_symbol(
            symbol=symbol,
            raw_root=raw_root,
            coverage=clamped_cov,
            smoke_hours=None,
            computation_mode=resolved_computation_mode,
        )
    if str(result.get("status") or "") == "DATA_INCOMPLETE":
        return _write_incomplete(
            run_dir=run_dir,
            symbol=symbol,
            signal_start=signal_start,
            signal_end_exclusive=signal_end_exclusive,
            coverage=coverage,
            missing=missing or ["DATA_INCOMPLETE"],
            strategy_hash=strategy_hash,
            reason="DATA_INCOMPLETE",
            effective_start=_iso_z(eff_start),
            effective_end=_iso_z(eff_end),
        )

    candidates = list((result.get("bundles") or {}).get("candidate_events") or [])
    ema_setup_events = list((result.get("bundles") or {}).get("ema_setup_events") or [])
    micro_events = list(
        (result.get("bundles") or {}).get("microstructure_confirmation_events") or []
    )
    if not micro_events:
        micro_events = [
            c
            for c in candidates
            if str(c.get("confirmation_mode") or "") == "ema_plus_microstructure"
            or str(c.get("output_layer") or "") == "microstructure_confirmation"
        ]
    signals: list[dict[str, Any]] = []
    for cand in candidates:
        row = candidate_to_signal_row(
            cand,
            strategy_hash=strategy_hash,
            job_start=signal_start,
            job_end=signal_end_exclusive,
        )
        if row:
            signals.append(row)

    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "per_symbol").mkdir(exist_ok=True)
    signals_path = run_dir / "signals.jsonl"
    with signals_path.open("w", encoding="utf-8") as handle:
        for row in signals:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    write_json_atomic(
        run_dir / "run_manifest.json",
        {
            "run_id": run_id,
            "selected_symbol": symbol,
            "selected_symbols": [symbol],
            "strategy_id": EZM_STRATEGY_ID,
            "run_intent": EZM_RUN_INTENT,
            "runner_kind": EZM_RUNNER_KIND,
            "result_contract_version": EZM_RESULT_CONTRACT_VERSION,
            "strategy_spec_hash": strategy_hash,
            "computation_mode": resolved_computation_mode,
            "compiler": "compile_candidate_discovery_v2",
            "trade_compiler_used": False,
            "signal_start": signal_start,
            "signal_end_exclusive": signal_end_exclusive,
            "effective_start": _iso_z(eff_start),
            "effective_end": _iso_z(eff_end),
            "coverage_status": coverage.get("status"),
            "data_basis": coverage.get("data_basis"),
            "touch_price_basis": (result.get("quality") or {}).get("touch_price_basis"),
            "orderbook_loaded": (result.get("quality") or {}).get("orderbook_loaded"),
            "oa_repo": str(repo),
        },
    )
    write_json_atomic(
        run_dir / "summary.json",
        {
            "run_id": run_id,
            "raw_total": len(candidates),
            "tier_a_total": len(signals),
            "candidate_total": len(candidates),
            "ema_setup_total": len(ema_setup_events),
            "microstructure_total": len(micro_events),
            "directed_confirmed_total": len(signals),
            "symbol": symbol,
        },
    )
    write_json_atomic(
        run_dir / "per_symbol" / f"{symbol}.json",
        {
            "symbol": symbol,
            "warmup_complete": True,
            "first_valid_by_timeframe": {
                "15m": {"warmup_complete": True},
                "30m": {"warmup_complete": True},
                "1h": {"warmup_complete": True},
                "4h": {"warmup_complete": True},
            },
            "counts_by_timeframe": {
                "5m": {
                    "raw_candidates": len(candidates),
                    "tier_a": len(signals),
                }
            },
            "missing_sources": missing,
            "effective_start": _iso_z(eff_start),
            "effective_end": _iso_z(eff_end),
        },
    )
    write_json_atomic(
        run_dir / "candidates.json",
        {
            "ema_setup_events": ema_setup_events,
            "microstructure_confirmation_events": micro_events,
            "candidates": candidates,
        },
    )
    write_json_atomic(run_dir / "coverage.json", coverage)

    return {
        "symbol": symbol,
        "state": "COMPLETED",
        "runner_run_id": run_id,
        "raw_total": len(candidates),
        "tier_a_total": len(signals),
        "error_code": None,
        "message": f"candidates={len(candidates)} markers={len(signals)}",
        "missing_sources": missing,
        "effective_start": _iso_z(eff_start),
        "effective_end": _iso_z(eff_end),
        "warmup_complete": True,
        "warmup_complete_by_tf": {tf: True for tf in ("15m", "30m", "1h", "4h")},
        "warmup_schema_error": None,
        "per_timeframe": {
            "15m": {"raw": 0, "tier_a": 0},
            "30m": {"raw": 0, "tier_a": 0},
            "1h": {"raw": 0, "tier_a": 0},
            "4h": {"raw": 0, "tier_a": 0},
            "5m": {"raw": len(candidates), "tier_a": len(signals)},
        },
        "multi_tf_collision_count": 0,
    }


def _write_incomplete(
    *,
    run_dir: Path,
    symbol: str,
    signal_start: str,
    signal_end_exclusive: str,
    coverage: dict[str, Any],
    missing: list[str],
    strategy_hash: str,
    reason: str,
    effective_start: str | None = None,
    effective_end: str | None = None,
) -> dict[str, Any]:
    run_id = run_dir.name
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json_atomic(
        run_dir / "run_manifest.json",
        {
            "run_id": run_id,
            "selected_symbol": symbol,
            "selected_symbols": [symbol],
            "strategy_id": EZM_STRATEGY_ID,
            "run_intent": EZM_RUN_INTENT,
            "runner_kind": EZM_RUNNER_KIND,
            "result_contract_version": EZM_RESULT_CONTRACT_VERSION,
            "strategy_spec_hash": strategy_hash,
            "signal_start": signal_start,
            "signal_end_exclusive": signal_end_exclusive,
            "effective_start": effective_start,
            "effective_end": effective_end,
            "coverage_status": coverage.get("status"),
            "incomplete_reason": reason,
            "missing_sources": missing,
        },
    )
    write_json_atomic(
        run_dir / "summary.json",
        {"run_id": run_id, "raw_total": 0, "tier_a_total": 0, "symbol": symbol, "status": "DATA_INCOMPLETE"},
    )
    write_json_atomic(run_dir / "coverage.json", coverage)
    (run_dir / "signals.jsonl").write_text("", encoding="utf-8")
    return {
        "symbol": symbol,
        "state": "DATA_INCOMPLETE",
        "runner_run_id": run_id,
        "raw_total": 0,
        "tier_a_total": 0,
        "error_code": "DATA_INCOMPLETE",
        "message": reason,
        "missing_sources": missing,
        "effective_start": effective_start,
        "effective_end": effective_end,
        "warmup_complete": False,
        "warmup_complete_by_tf": {},
        "warmup_schema_error": reason,
        "per_timeframe": {
            "15m": {"raw": 0, "tier_a": 0},
            "30m": {"raw": 0, "tier_a": 0},
            "1h": {"raw": 0, "tier_a": 0},
            "4h": {"raw": 0, "tier_a": 0},
        },
        "multi_tf_collision_count": 0,
    }


def _stub_coin(
    *,
    symbol: str,
    signal_start: str,
    signal_end_exclusive: str,
    out_root: Path,
    run_id: str,
    stub: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    run_dir = out_root / run_id
    if stub == "incomplete":
        return _write_incomplete(
            run_dir=run_dir,
            symbol=symbol,
            signal_start=signal_start,
            signal_end_exclusive=signal_end_exclusive,
            coverage={"status": "DATA_INCOMPLETE", "incomplete_reason": "STUB_INCOMPLETE"},
            missing=["STUB_INCOMPLETE"],
            strategy_hash=str(contract.get("strategy_hash") or "stub"),
            reason="STUB_INCOMPLETE",
        )
    if stub == "fail":
        run_dir.mkdir(parents=True, exist_ok=False)
        return {
            "symbol": symbol,
            "state": "FAILED",
            "runner_run_id": run_id,
            "raw_total": 0,
            "tier_a_total": 0,
            "error_code": "STUB_FAIL",
            "message": "STUB_FAIL",
            "missing_sources": [],
            "warmup_complete": None,
            "warmup_complete_by_tf": {},
            "warmup_schema_error": None,
            "per_timeframe": {
                "15m": {"raw": 0, "tier_a": 0},
                "30m": {"raw": 0, "tier_a": 0},
                "1h": {"raw": 0, "tier_a": 0},
                "4h": {"raw": 0, "tier_a": 0},
            },
            "multi_tf_collision_count": 0,
        }
    # success stub with one LONG + one SHORT marker
    candidates = [
        {
            "symbol": symbol,
            "episode_id": f"{symbol}-ep-long",
            "candidate_state": "defense_rejection_confirmed",
            "candidate_direction": "LONG",
            "decision_at": signal_start,
            "decision_price": 100.5,
            "zone_name": "EMA20",
            "regime": "uptrend",
            "mechanism": "BID_DEFENSE",
            "reason_codes": "STUB",
        },
        {
            "symbol": symbol,
            "episode_id": f"{symbol}-ep-short",
            "candidate_state": "breakout_confirmed",
            "candidate_direction": "SHORT",
            "decision_at": signal_start,
            "decision_price": 101.5,
            "zone_name": "EMA59",
            "regime": "downtrend",
            "mechanism": "ASK_DEFENSE",
            "reason_codes": "STUB",
        },
        {
            "symbol": symbol,
            "episode_id": f"{symbol}-ep-wait",
            "candidate_state": "wait_microstructure_confirmation",
            "candidate_direction": "",
            "decision_at": signal_start,
            "decision_price": 100.0,
        },
    ]
    strategy_hash = str(contract.get("strategy_hash") or "stubhash")
    signals = [
        row
        for cand in candidates
        if (
            row := candidate_to_signal_row(
                cand,
                strategy_hash=strategy_hash,
                job_start=signal_start,
                job_end=signal_end_exclusive,
            )
        )
    ]
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "per_symbol").mkdir(exist_ok=True)
    with (run_dir / "signals.jsonl").open("w", encoding="utf-8") as handle:
        for row in signals:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    write_json_atomic(
        run_dir / "run_manifest.json",
        {
            "run_id": run_id,
            "selected_symbol": symbol,
            "selected_symbols": [symbol],
            "strategy_id": EZM_STRATEGY_ID,
            "run_intent": EZM_RUN_INTENT,
            "runner_kind": EZM_RUNNER_KIND,
            "result_contract_version": EZM_RESULT_CONTRACT_VERSION,
            "strategy_spec_hash": strategy_hash,
            "signal_start": signal_start,
            "signal_end_exclusive": signal_end_exclusive,
            "effective_start": signal_start,
            "effective_end": signal_end_exclusive,
            "stub": True,
        },
    )
    write_json_atomic(
        run_dir / "summary.json",
        {
            "run_id": run_id,
            "raw_total": len(candidates),
            "tier_a_total": len(signals),
            "symbol": symbol,
        },
    )
    write_json_atomic(
        run_dir / "per_symbol" / f"{symbol}.json",
        {
            "symbol": symbol,
            "warmup_complete": True,
            "first_valid_by_timeframe": {
                tf: {"warmup_complete": True} for tf in ("15m", "30m", "1h", "4h")
            },
            "counts_by_timeframe": {"5m": {"raw_candidates": len(candidates), "tier_a": len(signals)}},
        },
    )
    return {
        "symbol": symbol,
        "state": "COMPLETED",
        "runner_run_id": run_id,
        "raw_total": len(candidates),
        "tier_a_total": len(signals),
        "error_code": None,
        "message": "stub ok",
        "missing_sources": [],
        "warmup_complete": True,
        "warmup_complete_by_tf": {tf: True for tf in ("15m", "30m", "1h", "4h")},
        "warmup_schema_error": None,
        "per_timeframe": {
            "15m": {"raw": 0, "tier_a": 0},
            "30m": {"raw": 0, "tier_a": 0},
            "1h": {"raw": 0, "tier_a": 0},
            "4h": {"raw": 0, "tier_a": 0},
        },
        "multi_tf_collision_count": 0,
    }
