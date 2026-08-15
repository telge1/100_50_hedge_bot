from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_CANARY_SYMBOL,
    GENERATOR_VERSION,
    PHASE,
    SIDE_EFFECT_FLAGS,
    SOURCE_COMMIT_PIN,
    STRATEGY_ID,
    WARMUP_DAYS,
)
from .engine import parameter_hash
from .identity import (
    BE50_OUTCOME_ACTIVE,
    CANDIDATE_LIVE_STRATEGY,
    EDGES_VERSION_PIN,
    SIGNAL_TFS_PIN,
    frozen_identity,
)
from .jsonio import write_json_atomic, write_jsonl


def new_run_dir(root: Path, run_id: str) -> Path:
    path = root / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_run_artifacts(
    run_dir: Path,
    *,
    run_id: str,
    symbols: list[str],
    results: list[dict[str, Any]],
    signal_start: datetime,
    signal_end_exclusive: datetime,
    clickhouse_canary: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_signals: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    for item in results:
        sigs = list(item.get("signals") or [])
        all_signals.extend(sigs)
        coverage_rows.append(
            {
                "symbol": item["symbol"],
                "status": item["status"],
                "tier_a_count": int(item.get("tier_a_count") or 0),
                "raw_count": int(item.get("raw_count") or 0),
                "warmup_complete": bool(item.get("warmup_complete")),
                "bars_1m": int(item.get("bars_1m") or 0),
                "error": item.get("error"),
            }
        )
        skip = {"signals", "raw_candidates", "technical_duplicates"}
        write_json_atomic(
            run_dir / "per_symbol" / f"{item['symbol']}.json",
            {k: v for k, v in item.items() if k not in skip} | {"signal_count": len(sigs)},
        )
        write_jsonl(run_dir / "per_symbol" / f"{item['symbol']}_raw_candidates.jsonl", list(item.get("raw_candidates") or []))
        write_jsonl(run_dir / "per_symbol" / f"{item['symbol']}_technical_duplicates.jsonl", list(item.get("technical_duplicates") or []))

    identity = frozen_identity()
    manifest = {
        "run_id": run_id,
        "phase": PHASE,
        "strategy_id": STRATEGY_ID,
        "source_commit_pin": SOURCE_COMMIT_PIN,
        "candidate_live_strategy": CANDIDATE_LIVE_STRATEGY,
        "signal_tfs": list(SIGNAL_TFS_PIN),
        "edges_version": EDGES_VERSION_PIN,
        "be50_outcome_active": BE50_OUTCOME_ACTIVE,
        "frozen_identity": identity,
        "generator_version": GENERATOR_VERSION,
        "parameter_hash": parameter_hash(),
        "warmup_days": WARMUP_DAYS,
        "symbols": symbols,
        "selected_symbol": symbols[0] if symbols else None,
        "selected_symbols": list(symbols),
        "default_canary_symbol": DEFAULT_CANARY_SYMBOL,
        "default_canary_symbol_is_not_run_symbol": True,
        "signal_start": signal_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "signal_end_exclusive": signal_end_exclusive.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        "execution_dedup_policy": "none_phase_2a",
        "selection_status": "NOT_APPLIED_AMBIGUOUS",
        "full_51_run": False,
        "clickhouse_canary": clickhouse_canary,
        "phase": "2B" if clickhouse_canary else PHASE,
        "universe_source": extra.get("universe_source") if extra else None,
        "universe_count": extra.get("universe_count") if extra else None,
        "selected_symbols": list(symbols),
        "symbol_allowlisted": bool(extra.get("symbol_allowlisted")) if extra else True,
    }
    if extra:
        manifest.update(extra)
    write_json_atomic(run_dir / "run_manifest.json", manifest)
    write_json_atomic(run_dir / "coverage.json", {"symbols": coverage_rows})
    write_jsonl(run_dir / "signals.jsonl", all_signals)
    summary = {
        "run_id": run_id,
        "symbols_evaluated": len(results),
        "tier_a_total": sum(int(r.get("tier_a_count") or 0) for r in results),
        "raw_total": len(all_signals),
        "statuses": {row["symbol"]: row["status"] for row in coverage_rows},
    }
    write_json_atomic(run_dir / "summary.json", summary)
    return summary
