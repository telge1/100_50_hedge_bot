"""P2 checkpoint write, plus an experimental P3 reader/restore.

Startup must not load these files. ``CHECKPOINT_USED_FOR_STARTUP`` stays false
and ``runner`` / ``load_state`` do not call the restore helpers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from bot.e1r_live_scanner.config import REPO_ROOT, ScannerConfig, ensure_runtime_paths

CHECKPOINT_SCHEMA_VERSION = 2
DIAGNOSTIC_SCHEMA_VERSIONS = (1, 2)
TIMEFRAME_15M = "15m"
CHECKPOINT_USED_FOR_STARTUP = False
E1R_SNAPSHOT_VERSION = 2
_E1R_EP_REQUIRED_KEYS = ("start_time", "cluster_2_pool_ids", "cluster_3_pool_ids")

RESTORED_FIELDS = (
    "last_processed_15m_close",
    "seen_no_guard",
    "seen_with_guard",
    "e1r_state",
    "e1r_machine",
    "pending",
    "e1r_ep",
    "reclaim_events",
    "floor_guard_episode",
)
MISSING_REBUILD_REQUIRED = (
    "frozen_pool_walker",
    "e1r_lld_walker_cache",
    "legacy_lld_cache",
    "floor_guard_4h_cache",
)

_REQUIRED_META = (
    "schema_version",
    "symbol",
    "timeframe",
    "pane_from",
    "checkpoint_decision_close",
    "checkpoint_candle_index",
    "frozen_config_version",
    "strict_complete_buckets",
    "state_hash",
)
_FLOOR_KEYS = (
    "block_until",
    "session_floor_top",
    "session_floor_bottom",
    "last_reason",
)


def checkpoint_path_for_symbol(config: ScannerConfig, symbol: str) -> Path:
    sym = str(symbol).strip().upper()
    return config.checkpoint_dir / f"{sym}.json"


def scanner_config_hash(config: ScannerConfig) -> str:
    payload = {
        "pane_from": config.utc(config.pane_from).isoformat(),
        "pane_to": config.utc(config.pane_to).isoformat(),
        "e1r_sim_from": config.utc(config.e1r_sim_from).isoformat(),
        "report_from": config.utc(config.report_from).isoformat(),
        "report_to": config.effective_report_to().isoformat(),
        "history_weeks": config.history_weeks,
        "live": config.live,
        "frozen_pool_transition_parity": config.frozen_pool_transition_parity,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def optional_git_sha() -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def floor_guard_snapshot(guard: Any) -> dict[str, Any]:
    block_until = getattr(guard, "block_until", None)
    return {
        "block_until": block_until.isoformat() if block_until is not None else None,
        "session_floor_top": getattr(guard, "session_floor_top", None),
        "session_floor_bottom": getattr(guard, "session_floor_bottom", None),
        "last_reason": getattr(guard, "last_reason", None),
    }


def walker_built_through(cache: dict[str, Any]) -> int | None:
    ensure_runtime_paths()
    from dashboard.research_charts.lld_research_kernel import CAUSAL_LLD_WALKER_KEY

    walker = cache.get(CAUSAL_LLD_WALKER_KEY)
    if walker is None:
        return None
    return int(getattr(walker, "_built_through", -1))


def frozen_pool_diagnostics(st: Any, decision_close: datetime) -> dict[str, Any]:
    ensure_runtime_paths()
    from bot.e1r_live_scanner.short_pools import available_for_short_entry
    from dashboard.research_charts.frozen_lld_config_v1 import FROZEN_LLD_CONFIG_VERSION
    from dashboard.research_charts.lld_research_kernel import candle_stamps

    result = available_for_short_entry(
        st.symbol,
        st.candles15,
        decision_close,
        frozen_lld_cache=st.frozen_lld_cache,
    )
    stamps = candle_stamps(st.candles15)
    return {
        "frozen_config_version": FROZEN_LLD_CONFIG_VERSION,
        "available_pool_ids": list(result.available_pool_ids),
        "available_rows_hash": result.available_rows_hash,
        "candle_index": result.candle_index,
        "frozen_walker_built_through": walker_built_through(st.frozen_lld_cache),
        "legacy_walker_built_through": walker_built_through(st.legacy_lld_cache),
        "pane_first_open": stamps[0].isoformat() if stamps else None,
        "pane_last_open": stamps[-1].isoformat() if stamps else None,
    }


def build_checkpoint_document(st: Any, config: ScannerConfig) -> dict[str, Any]:
    if st.last_processed_15m_close is None:
        raise ValueError("last_processed_15m_close is required for checkpoint")
    if not st.strict_complete_buckets:
        raise ValueError("strict_complete_buckets must be true for checkpoint")

    decision = st.last_processed_15m_close
    if decision.tzinfo is None:
        decision = decision.replace(tzinfo=timezone.utc)

    frozen_diag = frozen_pool_diagnostics(st, decision)
    idx = frozen_diag["candle_index"]

    payload: dict[str, Any] = {
        "last_processed_15m_close": decision.isoformat(),
        "seen_no_guard": sorted(st.seen_no_guard),
        "seen_with_guard": sorted(st.seen_with_guard),
        "e1r": e1r_resume_snapshot(st.e1r_engine) if st.e1r_engine is not None else None,
        "e1r_lld_diagnostic": e1r_lld_diagnostic(st, idx) if st.e1r_engine is not None else None,
        "floor_guard": floor_guard_snapshot(st.floor_guard) if st.floor_guard is not None else None,
        "frozen_pool_diagnostic": frozen_diag,
        "lld_cache_note": "not_serialized_in_p2",
    }

    meta: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "symbol": st.symbol,
        "timeframe": TIMEFRAME_15M,
        "pane_from": config.utc(config.pane_from).isoformat(),
        "checkpoint_decision_close": decision.isoformat(),
        "checkpoint_candle_index": idx,
        "frozen_config_version": frozen_diag["frozen_config_version"],
        "pane_first_open": frozen_diag["pane_first_open"],
        "pane_last_open": frozen_diag["pane_last_open"],
        "strict_complete_buckets": bool(st.strict_complete_buckets),
        "scanner_config_hash": scanner_config_hash(config),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_git_sha": optional_git_sha(),
    }

    meta["state_hash"] = payload_state_hash(payload)

    return {"metadata": meta, "payload": payload}


def atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(document, indent=2, sort_keys=True, default=str)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def payload_state_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json_data(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def e1r_resume_snapshot(engine: Any) -> dict[str, Any]:
    """Fields ``process_bar`` and ``evaluate_signal`` need after the checkpoint.

    ``snapshot()`` stays the short v1 view. This document is schema 2 only.
    """
    sim_from = getattr(engine, "sim_from", None)
    sim_to = getattr(engine, "sim_to", None)
    return _json_data(
        {
            "snapshot_version": E1R_SNAPSHOT_VERSION,
            "e1r_state": engine.e1r_state,
            "e1r_machine": {k.isoformat(): v for k, v in engine.e1r_machine.items()},
            "pending": engine.pending,
            "e1r_ep": engine.e1r_ep,
            "reclaim_events": list(engine.reclaim_events),
            "sim_from": sim_from.isoformat() if isinstance(sim_from, datetime) else None,
            "sim_to": sim_to.isoformat() if isinstance(sim_to, datetime) else None,
        }
    )


def e1r_resume_blockers(
    snap: dict[str, Any] | None,
    candle_index: int | None,
    lld_diagnostic: dict[str, Any] | None = None,
) -> list[str]:
    """Missing fields that ``process_bar`` would read on the next bar."""
    if not isinstance(snap, dict):
        return ["e1r_snapshot"]
    blockers: list[str] = []
    if snap.get("snapshot_version") != E1R_SNAPSHOT_VERSION:
        blockers.append("e1r_snapshot_version")
    state = snap.get("e1r_state")
    if state not in ("INACTIVE", "ACTIVE", "PENDING"):
        blockers.append("e1r_state")
    if not isinstance(snap.get("e1r_machine"), dict):
        blockers.append("e1r_machine")
    if "pending" not in snap:
        blockers.append("pending")
    if "e1r_ep" not in snap:
        blockers.append("e1r_ep")
    if not isinstance(snap.get("reclaim_events"), list):
        blockers.append("reclaim_events")
    if _parse_utc(snap.get("sim_from")) is None or _parse_utc(snap.get("sim_to")) is None:
        blockers.append("e1r_window")
    if state == "PENDING" and not isinstance(snap.get("pending"), dict):
        blockers.append("pending_required")
    if state in ("ACTIVE", "PENDING"):
        episode = snap.get("e1r_ep")
        if not isinstance(episode, dict):
            blockers.append("e1r_ep_required")
        else:
            for key in _E1R_EP_REQUIRED_KEYS:
                if key not in episode:
                    blockers.append(f"e1r_ep_missing_{key}")
    diagnostic = lld_diagnostic
    if not isinstance(diagnostic, dict):
        blockers.append("e1r_lld_diagnostic")
    else:
        if not isinstance(diagnostic.get("pool_ids"), list) or not isinstance(diagnostic.get("rows_hash"), str):
            blockers.append("e1r_lld_diagnostic")
        diag_index = _as_nonbool_int(diagnostic.get("candle_index"))
        if candle_index is not None and diag_index != candle_index:
            blockers.append("e1r_lld_index")
    return blockers


def e1r_lld_diagnostic(st: Any, index: int) -> dict[str, Any]:
    ensure_runtime_paths()
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index
    from dashboard.research_charts.short_pool_oracle_v1 import rows_hash

    available, _ui, _proof = scanner_pools_for_index(
        st.candles15,
        TIMEFRAME_15M,
        index,
        st.lld_cfg,
        cache=st.lld_cache,
    )
    return {
        "pool_ids": sorted(row["pool_id"] for row in available),
        "rows_hash": rows_hash(available),
        "candle_index": index,
    }


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_nonbool_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _close_15m(open_time: datetime) -> datetime:
    from pool_scan.clock import bar_close

    closed = bar_close(open_time, TIMEFRAME_15M)
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    return closed.astimezone(timezone.utc)


def resolve_checkpoint_path(path_or_symbol: str | Path, config: ScannerConfig | None = None) -> Path:
    raw = Path(path_or_symbol)
    if raw.suffix == ".json" or raw.is_file():
        return raw
    cfg = config or ScannerConfig()
    return checkpoint_path_for_symbol(cfg, str(path_or_symbol))


@dataclass
class ResumeCheckpointRead:
    """Structured read. v1 can be diagnostic-valid. Only v2 with a complete E1R state is resume_ready."""

    status: str
    reasons: list[str] = field(default_factory=list)
    path: str | None = None
    metadata: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    resume_ready: bool = False
    readiness: str = "INVALID"
    resume_blockers: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.status == "VALID"


def load_resume_checkpoint(
    path_or_symbol: str | Path,
    *,
    config: ScannerConfig | None = None,
    pane_opens: Sequence[datetime] | None = None,
    latest_closed_bar: datetime | None = None,
    expected_symbol: str | None = None,
) -> ResumeCheckpointRead:
    """Read and validate a P2 checkpoint. Does not touch scanner startup."""
    path = resolve_checkpoint_path(path_or_symbol, config)
    if expected_symbol is None and path.suffix == ".json" and not Path(path_or_symbol).suffix:
        expected_symbol = str(path_or_symbol).strip().upper()
    reasons: list[str] = []
    if not path.is_file():
        return ResumeCheckpointRead(status="INVALID", reasons=["file_missing"], path=str(path))
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ResumeCheckpointRead(status="INVALID", reasons=["json_decode"], path=str(path))
    if not isinstance(document, dict):
        return ResumeCheckpointRead(status="INVALID", reasons=["document_not_object"], path=str(path))

    metadata = document.get("metadata")
    payload = document.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        return ResumeCheckpointRead(
            status="INVALID",
            reasons=["metadata_or_payload_missing"],
            path=str(path),
        )

    for key in _REQUIRED_META:
        if key not in metadata:
            reasons.append(f"missing_{key}")

    schema = _as_nonbool_int(metadata.get("schema_version"))
    if schema not in DIAGNOSTIC_SCHEMA_VERSIONS:
        reasons.append("schema_version")

    symbol = str(metadata.get("symbol") or "").strip().upper()
    if not symbol:
        reasons.append("symbol")
    else:
        if expected_symbol is not None and symbol != str(expected_symbol).strip().upper():
            reasons.append("symbol")
        if path.stem.upper() != symbol:
            reasons.append("symbol")

    if metadata.get("timeframe") != TIMEFRAME_15M:
        reasons.append("timeframe")

    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import (
        FROZEN_LLD_CONFIG_VERSION,
        PANE_FROM_UTC,
    )

    pane_from = _parse_utc(metadata.get("pane_from"))
    if pane_from != PANE_FROM_UTC:
        reasons.append("pane_from")

    frozen_version = _as_nonbool_int(metadata.get("frozen_config_version"))
    if frozen_version != FROZEN_LLD_CONFIG_VERSION:
        reasons.append("frozen_config_version")

    if metadata.get("strict_complete_buckets") is not True:
        reasons.append("strict_complete_buckets")

    decision = _parse_utc(metadata.get("checkpoint_decision_close"))
    if decision is None:
        reasons.append("checkpoint_decision_close")

    index = _as_nonbool_int(metadata.get("checkpoint_candle_index"))
    if index is None or index < 0:
        reasons.append("checkpoint_candle_index")

    payload_decision = _parse_utc(payload.get("last_processed_15m_close"))
    if decision is not None and payload_decision != decision:
        reasons.append("decision_close_payload_mismatch")

    diag = payload.get("frozen_pool_diagnostic")
    if isinstance(diag, dict) and index is not None:
        diag_index = _as_nonbool_int(diag.get("candle_index"))
        if diag_index != index:
            reasons.append("diagnostic_index_mismatch")

    if not isinstance(payload.get("seen_no_guard"), list) or not isinstance(payload.get("seen_with_guard"), list):
        reasons.append("seen_sets")
    e1r = payload.get("e1r")
    if not isinstance(e1r, dict) or "e1r_state" not in e1r or not isinstance(e1r.get("e1r_machine"), dict):
        reasons.append("e1r_snapshot")
    floor = payload.get("floor_guard")
    if not isinstance(floor, dict) or any(key not in floor for key in _FLOOR_KEYS):
        reasons.append("floor_guard_episode")

    expected_hash = metadata.get("state_hash")
    if not isinstance(expected_hash, str) or payload_state_hash(payload) != expected_hash:
        reasons.append("state_hash")

    if pane_opens is None:
        reasons.append("pane_not_loaded")
    elif decision is not None and index is not None:
        opens = list(pane_opens)
        if not opens or index >= len(opens):
            reasons.append("checkpoint_bar_missing")
        else:
            try:
                bar_close = _close_15m(opens[index])
            except Exception as exc:  # noqa: BLE001 — clock failure is a hard invalid
                reasons.append(f"checkpoint_bar_clock:{exc}")
                bar_close = None
            if bar_close is not None and bar_close != decision:
                reasons.append("checkpoint_bar_mismatch")
            latest = latest_closed_bar
            if latest is None and opens:
                try:
                    latest = _close_15m(opens[-1])
                except Exception:  # noqa: BLE001
                    latest = None
            if latest is not None and decision > _parse_utc(latest.isoformat()):
                reasons.append("checkpoint_after_latest_closed_bar")
            if opens:
                first = opens[0]
                if first.tzinfo is None:
                    first = first.replace(tzinfo=timezone.utc)
                if first.astimezone(timezone.utc) != PANE_FROM_UTC:
                    reasons.append("pane_first_open")

    status = "INVALID" if reasons else "VALID"
    blockers: list[str] = []
    readiness = "INVALID"
    resume_ready = False
    if status == "VALID" and schema == 1:
        blockers = ["schema_v1_not_resumable"]
        readiness = "VALID_FOR_DIAGNOSTIC"
    elif status == "VALID" and schema == CHECKPOINT_SCHEMA_VERSION:
        blockers = e1r_resume_blockers(
            e1r if isinstance(e1r, dict) else None,
            index,
            payload.get("e1r_lld_diagnostic") if isinstance(payload, dict) else None,
        )
        if blockers:
            reasons = [*reasons, *blockers]
            status = "INVALID"
        else:
            readiness = "VALID_FOR_RESUME"
            resume_ready = True
    elif schema == CHECKPOINT_SCHEMA_VERSION and isinstance(e1r, dict):
        blockers = e1r_resume_blockers(
            e1r,
            index,
            payload.get("e1r_lld_diagnostic") if isinstance(payload, dict) else None,
        )
        if blockers:
            reasons = [*reasons, *blockers]
    return ResumeCheckpointRead(
        status=status,
        reasons=reasons,
        path=str(path),
        metadata=metadata,
        payload=payload,
        resume_ready=resume_ready,
        readiness=readiness,
        resume_blockers=blockers,
    )


@dataclass
class RestoreReport:
    state: Any
    restored: tuple[str, ...]
    missing_rebuild_required: tuple[str, ...]
    notes: tuple[str, ...]


def restore_symbol_state_from_checkpoint_v1(
    loaded: ResumeCheckpointRead,
    *,
    pane_to: datetime | None = None,
) -> RestoreReport:
    """Restore a schema-2 checkpoint. Refuses v1 and any incomplete E1R state."""
    if not loaded.resume_ready or loaded.metadata is None or loaded.payload is None:
        detail = ",".join(loaded.resume_blockers or loaded.reasons) or "not_resume_ready"
        raise ValueError("refuse restore of checkpoint that is not resume_ready: " + detail)

    from bot.e1r_live_scanner.e1r_v1.engine import E1REngine
    from bot.e1r_live_scanner.state import SymbolState

    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import PANE_FROM_UTC
    from short_block_4h_guard import H4FloorGuard

    meta = loaded.metadata
    payload = loaded.payload
    decision = _parse_utc(payload["last_processed_15m_close"])
    if decision is None:
        raise ValueError("checkpoint decision close is not a timestamp")
    snap = payload["e1r"]
    sim_from = _parse_utc(snap.get("sim_from"))
    sim_to = _parse_utc(snap.get("sim_to"))
    if sim_from is None or sim_to is None:
        raise ValueError("e1r window missing")
    end = pane_to or sim_to

    state = SymbolState(symbol=str(meta["symbol"]).strip().upper())
    state.last_processed_15m_close = decision
    state.seen_no_guard = set(payload["seen_no_guard"])
    state.seen_with_guard = set(payload["seen_with_guard"])
    state.strict_complete_buckets = True

    engine = E1REngine(sim_from, end)
    machine = {}
    for key, value in snap["e1r_machine"].items():
        moment = _parse_utc(key)
        if moment is None:
            raise ValueError(f"e1r machine key is not a timestamp: {key}")
        machine[moment] = value
    engine.restore_machine(machine, snap["e1r_state"], copy.deepcopy(snap.get("pending")))
    engine.e1r_ep = copy.deepcopy(snap.get("e1r_ep"))
    engine.reclaim_events = copy.deepcopy(list(snap.get("reclaim_events") or []))
    engine.sim_to = sim_to
    state.e1r_engine = engine

    floor = payload["floor_guard"]
    guard = H4FloorGuard(state.symbol, PANE_FROM_UTC, end)
    guard.block_until = _parse_utc(floor.get("block_until")) if floor.get("block_until") else None
    guard.session_floor_top = floor.get("session_floor_top")
    guard.session_floor_bottom = floor.get("session_floor_bottom")
    guard.last_reason = floor.get("last_reason")
    state.floor_guard = guard

    return RestoreReport(
        state=state,
        restored=RESTORED_FIELDS,
        missing_rebuild_required=MISSING_REBUILD_REQUIRED,
        notes=(
            "walkers are not serialized; prime them to checkpoint_index before any tail bar",
            "floor 4h cache is rebuilt on the next guard.advance",
        ),
    )


def prime_frozen_walker_to_checkpoint(
    symbol: str,
    candles: list,
    decision_close: datetime,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the frozen walker once up to the checkpoint bar. No per-bar scanner replay."""
    ensure_runtime_paths()
    from bot.e1r_live_scanner.short_pools import available_for_short_entry

    holder = cache if cache is not None else {}
    started = time.perf_counter()
    result = available_for_short_entry(
        symbol,
        candles,
        decision_close,
        frozen_lld_cache=holder,
    )
    return {
        "seconds": time.perf_counter() - started,
        "method": "available_for_short_entry_once",
        "cache": holder,
        "result": result,
        "built_through": walker_built_through(holder),
    }


def prime_e1r_walker_to_checkpoint(
    candles: list,
    checkpoint_index: int,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the E1R/workspace LLD walker once. Does not step the E1R machine."""
    ensure_runtime_paths()
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config

    holder = cache if cache is not None else {}
    cfg_started = time.perf_counter()
    cfg = ui_lld_config(TIMEFRAME_15M)
    cfg_seconds = time.perf_counter() - cfg_started
    walk_started = time.perf_counter()
    available, _ui, _proof = scanner_pools_for_index(
        candles, TIMEFRAME_15M, checkpoint_index, cfg, cache=holder
    )
    from dashboard.research_charts.short_pool_oracle_v1 import rows_hash

    pool_ids = sorted(row["pool_id"] for row in available)
    return {
        "seconds": time.perf_counter() - walk_started,
        "config_seconds": cfg_seconds,
        "method": "scanner_pools_for_index_once",
        "cache": holder,
        "config": cfg,
        "built_through": walker_built_through(holder),
        "pool_ids": pool_ids,
        "rows_hash": rows_hash(available),
    }


def compare_restored_state(
    loaded: ResumeCheckpointRead,
    report: RestoreReport,
    frozen_prime: dict[str, Any] | None = None,
    e1r_prime: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Checkpoint golden vs restored fields. Walker objects stay NOT_RESTORED."""
    payload = loaded.payload or {}
    state = report.state
    out: dict[str, str] = {}

    decision = _parse_utc(payload.get("last_processed_15m_close"))
    out["last_processed_15m_close"] = "MATCH" if state.last_processed_15m_close == decision else "MISMATCH"
    out["seen_no_guard"] = "MATCH" if set(state.seen_no_guard) == set(payload.get("seen_no_guard") or []) else "MISMATCH"
    out["seen_with_guard"] = (
        "MATCH" if set(state.seen_with_guard) == set(payload.get("seen_with_guard") or []) else "MISMATCH"
    )

    golden = payload.get("e1r") or {}
    engine = state.e1r_engine
    live = engine.snapshot() if engine is not None else {}
    machine_ok = live.get("e1r_machine") == golden.get("e1r_machine")
    state_ok = live.get("e1r_state") == golden.get("e1r_state")
    pending_ok = live.get("pending") == golden.get("pending")
    out["e1r_snapshot"] = "MATCH" if machine_ok and state_ok and pending_ok else "MISMATCH"
    if engine is None or "e1r_ep" not in golden:
        out["e1r_ep"] = "NOT_COMPARABLE"
    else:
        out["e1r_ep"] = "MATCH" if engine.e1r_ep == golden.get("e1r_ep") else "MISMATCH"
    if engine is None or not isinstance(golden.get("reclaim_events"), list):
        out["reclaim_events"] = "NOT_COMPARABLE"
    else:
        out["reclaim_events"] = "MATCH" if engine.reclaim_events == golden.get("reclaim_events") else "MISMATCH"

    floor = payload.get("floor_guard") or {}
    guard = state.floor_guard
    block = guard.block_until.isoformat() if guard is not None and guard.block_until is not None else None
    floor_ok = (
        guard is not None
        and block == floor.get("block_until")
        and guard.session_floor_top == floor.get("session_floor_top")
        and guard.session_floor_bottom == floor.get("session_floor_bottom")
        and guard.last_reason == floor.get("last_reason")
    )
    out["floor_episode"] = "MATCH" if floor_ok else "MISMATCH"

    diag = payload.get("frozen_pool_diagnostic") or {}
    meta = loaded.metadata or {}
    if frozen_prime is None:
        out["frozen_available_pool_ids"] = "NOT_COMPARABLE"
        out["frozen_rows_hash"] = "NOT_COMPARABLE"
        out["checkpoint_candle_index"] = "NOT_COMPARABLE"
    else:
        result = frozen_prime.get("result")
        ids = list(getattr(result, "available_pool_ids", []) or [])
        digest = getattr(result, "available_rows_hash", None)
        idx = getattr(result, "candle_index", None)
        out["frozen_available_pool_ids"] = "MATCH" if ids == list(diag.get("available_pool_ids") or []) else "MISMATCH"
        out["frozen_rows_hash"] = "MATCH" if digest == diag.get("available_rows_hash") else "MISMATCH"
        out["checkpoint_candle_index"] = "MATCH" if idx == meta.get("checkpoint_candle_index") else "MISMATCH"
    e1r_diag = payload.get("e1r_lld_diagnostic") or {}
    if e1r_prime is None or not e1r_diag:
        out["e1r_lld_pool_ids"] = "NOT_COMPARABLE"
        out["e1r_lld_rows_hash"] = "NOT_COMPARABLE"
    else:
        out["e1r_lld_pool_ids"] = (
            "MATCH" if list(e1r_prime.get("pool_ids") or []) == list(e1r_diag.get("pool_ids") or []) else "MISMATCH"
        )
        out["e1r_lld_rows_hash"] = "MATCH" if e1r_prime.get("rows_hash") == e1r_diag.get("rows_hash") else "MISMATCH"
    out["frozen_walker_object"] = "NOT_RESTORED"
    out["e1r_lld_cache"] = "NOT_RESTORED"
    return out


def resume_and_replay_tail_v1(
    symbol: str,
    checkpoint: ResumeCheckpointRead | str | Path,
    pane: list,
    tail_end: datetime,
    *,
    processor: Any,
    bars15: Sequence[dict],
    market: dict | None = None,
    lld_cfg: Any = None,
    pane_opens: Sequence[datetime] | None = None,
) -> dict[str, Any]:
    """Restore a resume-ready checkpoint and run only bars after its decision close.

    Not used by startup. Bars at or before the checkpoint are not passed to ``_process_one_bar``.
    """
    sym = str(symbol).strip().upper()
    if isinstance(checkpoint, ResumeCheckpointRead):
        loaded = checkpoint
    else:
        loaded = load_resume_checkpoint(
            checkpoint,
            expected_symbol=sym,
            pane_opens=pane_opens,
        )
    if loaded.metadata is None or str(loaded.metadata.get("symbol") or "").upper() != sym:
        raise ValueError("checkpoint symbol does not match")
    if not loaded.resume_ready:
        detail = ",".join(loaded.resume_blockers or loaded.reasons) or "not_resume_ready"
        raise ValueError("refuse tail replay of checkpoint that is not resume_ready: " + detail)

    report = restore_symbol_state_from_checkpoint_v1(loaded)
    state = report.state
    state.candles15 = pane
    state.bars15 = list(bars15)
    state.by_open = {bar["open_time"]: bar for bar in bars15}
    state.market = market if market is not None else {"4h": {"bars": []}, "15m": {"bars": []}}
    if lld_cfg is not None:
        state.lld_cfg = lld_cfg

    decision = state.last_processed_15m_close
    if decision is None:
        raise ValueError("restored checkpoint has no decision close")
    index = int(loaded.metadata["checkpoint_candle_index"])
    seen_no_before = set(state.seen_no_guard)
    seen_with_before = set(state.seen_with_guard)

    frozen_started = time.perf_counter()
    frozen = prime_frozen_walker_to_checkpoint(sym, pane, decision, cache=state.frozen_lld_cache)
    frozen_seconds = time.perf_counter() - frozen_started
    e1r_started = time.perf_counter()
    e1r = prime_e1r_walker_to_checkpoint(pane, index, cache=state.lld_cache)
    e1r_seconds = time.perf_counter() - e1r_started
    if state.lld_cfg is None and e1r.get("config") is not None:
        state.lld_cfg = e1r["config"]
    if seen_no_before != set(state.seen_no_guard) or seen_with_before != set(state.seen_with_guard):
        raise RuntimeError("walker prime mutated seen sets")

    end = tail_end if tail_end.tzinfo else tail_end.replace(tzinfo=timezone.utc)
    processed: list[str] = []
    tail_started = time.perf_counter()
    for bar in state.bars15:
        close = bar["close_time"]
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        if close <= decision:
            continue
        if close > end:
            break
        processor._process_one_bar(state, bar, emit_signals=False)
        state.last_processed_15m_close = close
        processed.append(close.isoformat())
    tail_seconds = time.perf_counter() - tail_started
    return {
        "symbol": sym,
        "processed_closes": processed,
        "bars_at_or_before_checkpoint_processed": 0,
        "seen_no_guard_before_tail": sorted(seen_no_before),
        "seen_with_guard_before_tail": sorted(seen_with_before),
        "state": state,
        "restore": report,
        "frozen_prime": frozen,
        "e1r_prime": e1r,
        "frozen_prime_seconds": frozen_seconds,
        "e1r_prime_seconds": e1r_seconds,
        "tail_replay_seconds": tail_seconds,
        "parity": compare_restored_state(loaded, report, frozen, e1r),
        "CHECKPOINT_USED_FOR_STARTUP": CHECKPOINT_USED_FOR_STARTUP,
    }


@dataclass
class StartupResumeResult:
    ok: bool
    fallback_reason: str | None = None
    loaded: ResumeCheckpointRead | None = None
    load_seconds: float = 0.0
    restore_seconds: float = 0.0
    prime_seconds: float = 0.0
    tail_seconds: float = 0.0
    tail_bars_processed: int = 0
    legacy_prime_seconds: float = 0.0


def _startup_invariant_ok(parity: dict[str, str]) -> bool:
    for value in parity.values():
        if value == "MISMATCH":
            return False
    return True


def _merge_restored_into_pane_state(st: Any, restored: Any, *, pane_end: datetime) -> None:
    from bot.e1r_live_scanner.live_refresh import utc

    end = utc(pane_end)
    st.last_processed_15m_close = restored.last_processed_15m_close
    st.seen_no_guard = set(restored.seen_no_guard)
    st.seen_with_guard = set(restored.seen_with_guard)
    st.e1r_engine = restored.e1r_engine
    st.floor_guard = restored.floor_guard
    if st.e1r_engine is not None:
        st.e1r_engine.sim_to = end
    if st.floor_guard is not None:
        st.floor_guard.pane_to = end
    st.lld_cache.clear()
    st.frozen_lld_cache.clear()
    st.legacy_lld_cache.clear()
    st.frozen_shadow_cache.clear()


def try_startup_resume(st: Any, processor: Any, *, config: ScannerConfig) -> StartupResumeResult:
    """Attempt schema-2 startup resume into ``st`` (pane already loaded). Does not write checkpoints."""
    from bot.e1r_live_scanner.live_refresh import utc
    from dashboard.research_charts.lld_research_kernel import candle_stamps

    sym = str(st.symbol).strip().upper()
    if not st.bars15:
        return StartupResumeResult(ok=False, fallback_reason="no_bars15")

    t_load = time.perf_counter()
    loaded = load_resume_checkpoint(
        checkpoint_path_for_symbol(config, sym),
        config=config,
        pane_opens=candle_stamps(st.candles15),
        expected_symbol=sym,
    )
    load_seconds = time.perf_counter() - t_load

    if loaded.status != "VALID":
        reason = loaded.reasons[0] if loaded.reasons else "invalid_checkpoint"
        return StartupResumeResult(ok=False, fallback_reason=reason, loaded=loaded, load_seconds=load_seconds)

    if config.require_scanner_config_hash_match and loaded.metadata is not None:
        expected_hash = scanner_config_hash(config)
        if loaded.metadata.get("scanner_config_hash") != expected_hash:
            return StartupResumeResult(
                ok=False,
                fallback_reason="scanner_config_hash",
                loaded=loaded,
                load_seconds=load_seconds,
            )

    if not loaded.resume_ready:
        reason = (loaded.resume_blockers or loaded.reasons or ["not_resume_ready"])[0]
        return StartupResumeResult(ok=False, fallback_reason=reason, loaded=loaded, load_seconds=load_seconds)

    if loaded.metadata is None or loaded.payload is None:
        return StartupResumeResult(ok=False, fallback_reason="metadata_or_payload_missing", loaded=loaded)

    pane_end = st.bars15[-1]["close_time"]
    t_restore = time.perf_counter()
    try:
        report = restore_symbol_state_from_checkpoint_v1(loaded, pane_to=utc(pane_end))
    except Exception as exc:  # noqa: BLE001
        return StartupResumeResult(
            ok=False,
            fallback_reason=f"restore_error:{type(exc).__name__}",
            loaded=loaded,
            load_seconds=load_seconds,
        )
    restore_seconds = time.perf_counter() - t_restore
    _merge_restored_into_pane_state(st, report.state, pane_end=pane_end)

    decision = st.last_processed_15m_close
    if decision is None:
        return StartupResumeResult(ok=False, fallback_reason="missing_decision_close", loaded=loaded)
    index = int(loaded.metadata["checkpoint_candle_index"])
    seen_no_before = set(st.seen_no_guard)
    seen_with_before = set(st.seen_with_guard)

    t_prime = time.perf_counter()
    try:
        frozen = prime_frozen_walker_to_checkpoint(sym, st.candles15, decision, cache=st.frozen_lld_cache)
        e1r = prime_e1r_walker_to_checkpoint(st.candles15, index, cache=st.lld_cache)
        if st.lld_cfg is None and e1r.get("config") is not None:
            st.lld_cfg = e1r["config"]
    except Exception as exc:  # noqa: BLE001
        return StartupResumeResult(
            ok=False,
            fallback_reason=f"prime_error:{type(exc).__name__}",
            loaded=loaded,
            load_seconds=load_seconds,
            restore_seconds=restore_seconds,
        )
    prime_seconds = time.perf_counter() - t_prime

    if seen_no_before != set(st.seen_no_guard) or seen_with_before != set(st.seen_with_guard):
        return StartupResumeResult(
            ok=False,
            fallback_reason="prime_mutated_seen_sets",
            loaded=loaded,
            load_seconds=load_seconds,
            restore_seconds=restore_seconds,
            prime_seconds=prime_seconds,
        )

    if config.resume_startup_invariant_check:
        parity = compare_restored_state(loaded, report, frozen, e1r)
        if not _startup_invariant_ok(parity):
            return StartupResumeResult(
                ok=False,
                fallback_reason="startup_invariant_failed",
                loaded=loaded,
                load_seconds=load_seconds,
                restore_seconds=restore_seconds,
                prime_seconds=prime_seconds,
            )

    tail_bars = bars_after_checkpoint(st.bars15, decision)
    last_bar: dict | None = None
    first_tail_close: datetime | None = None
    t_tail = time.perf_counter()
    tail_count = 0
    for bar in tail_bars:
        if bar["close_time"] > pane_end:
            break
        processor._process_one_bar(st, bar, emit_signals=False)
        st.last_processed_15m_close = bar["close_time"]
        last_bar = bar
        tail_count += 1
        if first_tail_close is None:
            first_tail_close = bar["close_time"]
    tail_seconds = time.perf_counter() - t_tail

    legacy_prime_seconds = 0.0
    prime_bar = last_bar
    if prime_bar is None:
        prime_bar = next((b for b in st.bars15 if b["close_time"] == decision), None)
    if prime_bar is not None:
        t_legacy = time.perf_counter()
        processor._prime_legacy_transition_cache(st, prime_bar)
        legacy_prime_seconds = time.perf_counter() - t_legacy

    from bot.e1r_live_scanner.state import Readiness

    st.readiness = Readiness.LIVE_READY
    st.note_ts("live_ready", st.last_processed_15m_close)

    return StartupResumeResult(
        ok=True,
        loaded=loaded,
        load_seconds=load_seconds,
        restore_seconds=restore_seconds,
        prime_seconds=prime_seconds,
        tail_seconds=tail_seconds,
        tail_bars_processed=tail_count,
        legacy_prime_seconds=legacy_prime_seconds,
    )


def bars_after_checkpoint(bars15: Sequence[dict], decision_close: datetime) -> list[dict]:
    """Tail bars after the checkpoint. This does not replay them."""
    moment = decision_close if decision_close.tzinfo else decision_close.replace(tzinfo=timezone.utc)
    tail = []
    for bar in bars15:
        close = bar["close_time"]
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        if close > moment:
            tail.append(bar)
    return tail


def measure_resume_prototype(
    symbol: str = "APTUSDT",
    checkpoint_path: str | Path | None = None,
) -> dict[str, Any]:
    """One-symbol experimental timing. Does not write checkpoints or call warmup."""
    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import PANE_FROM_UTC
    from dashboard.research_charts.lld_research_kernel import candle_stamps, load_pane_candles

    sym = str(symbol).strip().upper()
    path = Path(checkpoint_path) if checkpoint_path is not None else checkpoint_path_for_symbol(ScannerConfig(), sym)
    load_started = time.perf_counter()
    structural = load_resume_checkpoint(path, expected_symbol=sym)
    checkpoint_load_seconds = time.perf_counter() - load_started
    print(
        f"checkpoint_read status={structural.status} reasons={structural.reasons} seconds={checkpoint_load_seconds:.3f}",
        flush=True,
    )

    pane_started = time.perf_counter()
    _packed, candles = load_pane_candles(
        sym,
        TIMEFRAME_15M,
        from_unix=int(PANE_FROM_UTC.timestamp()),
        to_unix=int(datetime.now(timezone.utc).timestamp()),
        history_weeks=12,
    )
    pane_load_seconds = time.perf_counter() - pane_started
    opens = candle_stamps(candles)
    print(f"pane_loaded candles={len(candles)} seconds={pane_load_seconds:.3f}", flush=True)

    loaded = load_resume_checkpoint(path, expected_symbol=sym, pane_opens=opens)
    decision = _parse_utc((loaded.metadata or {}).get("checkpoint_decision_close"))
    index = _as_nonbool_int((loaded.metadata or {}).get("checkpoint_candle_index"))
    if not loaded.valid or decision is None or index is None:
        return {
            "symbol": sym,
            "status": loaded.status,
            "readiness": loaded.readiness,
            "resume_ready": loaded.resume_ready,
            "reasons": loaded.reasons,
            "checkpoint_load_seconds": checkpoint_load_seconds,
            "pane_load_seconds": pane_load_seconds,
            "CHECKPOINT_USED_FOR_STARTUP": CHECKPOINT_USED_FOR_STARTUP,
        }

    restore_seconds = None
    report = None
    if loaded.resume_ready:
        restore_started = time.perf_counter()
        report = restore_symbol_state_from_checkpoint_v1(loaded)
        restore_seconds = time.perf_counter() - restore_started
        print(f"restore_seconds={restore_seconds:.3f}", flush=True)
    else:
        print(f"restore_skipped resume_ready=false blockers={loaded.resume_blockers}", flush=True)

    frozen = prime_frozen_walker_to_checkpoint(sym, candles, decision, cache={})
    print(
        f"frozen_walker_seconds={frozen['seconds']:.3f} built_through={frozen['built_through']}",
        flush=True,
    )
    e1r = prime_e1r_walker_to_checkpoint(candles, index, cache={})
    print(
        f"e1r_walker_seconds={e1r['seconds']:.3f} config_seconds={e1r['config_seconds']:.3f} built_through={e1r['built_through']}",
        flush=True,
    )
    from dashboard.research_charts.lld_research_kernel import (
        CAUSAL_LLD_WALKER_KEY,
        scanner_pools_for_index,
    )
    from dashboard.research_charts.short_pool_oracle_v1 import rows_hash

    stepped_cache: dict[str, Any] = {}
    incremental_started = time.perf_counter()
    scanner_pools_for_index(candles, TIMEFRAME_15M, 0, e1r["config"], cache=stepped_cache)
    walker = stepped_cache[CAUSAL_LLD_WALKER_KEY]
    for cursor in range(1, index + 1):
        walker.advance_to(cursor)
    stepped, _ui, _proof = scanner_pools_for_index(
        candles,
        TIMEFRAME_15M,
        index,
        e1r["config"],
        cache=stepped_cache,
    )
    stepped_ids = sorted(row["pool_id"] for row in stepped)
    stepped_hash = rows_hash(stepped)
    incremental_seconds = time.perf_counter() - incremental_started
    e1r_incremental_match = stepped_ids == list(e1r["pool_ids"]) and stepped_hash == e1r["rows_hash"]
    print(
        f"e1r_incremental_seconds={incremental_seconds:.3f} match_oneshot={e1r_incremental_match}",
        flush=True,
    )
    frozen_diag = (loaded.payload or {}).get("frozen_pool_diagnostic") or {}
    frozen_ids = list(getattr(frozen["result"], "available_pool_ids", []) or [])
    frozen_hash = getattr(frozen["result"], "available_rows_hash", None)
    frozen_match = frozen_ids == list(frozen_diag.get("available_pool_ids") or []) and frozen_hash == frozen_diag.get(
        "available_rows_hash"
    )
    tail_seconds = None
    tail_processed = None
    if not loaded.resume_ready:
        print("tail_replay_skipped schema_not_resume_ready", flush=True)
    parity = None
    if report is not None:
        parity = compare_restored_state(loaded, report, frozen, e1r)
    total_parts = [
        checkpoint_load_seconds,
        frozen["seconds"],
        e1r["seconds"],
        e1r["config_seconds"],
    ]
    if restore_seconds is not None:
        total_parts.append(restore_seconds)
    summary = {
        "symbol": sym,
        "status": loaded.status,
        "readiness": loaded.readiness,
        "resume_ready": loaded.resume_ready,
        "resume_blockers": loaded.resume_blockers,
        "created_at": loaded.metadata.get("created_at"),
        "schema_version": loaded.metadata.get("schema_version"),
        "checkpoint_load_seconds": round(checkpoint_load_seconds, 3),
        "pane_load_seconds": round(pane_load_seconds, 3),
        "restore_seconds": None if restore_seconds is None else round(restore_seconds, 3),
        "frozen_walker_rebuild_seconds": round(frozen["seconds"], 3),
        "frozen_pool_parity": "MATCH" if frozen_match else "MISMATCH",
        "e1r_walker_rebuild_seconds": round(e1r["seconds"], 3),
        "e1r_config_seconds": round(e1r["config_seconds"], 3),
        "e1r_incremental_seconds": round(incremental_seconds, 3),
        "e1r_oneshot_matches_incremental": e1r_incremental_match,
        "tail_replay_seconds": tail_seconds,
        "tail_processed": tail_processed,
        "tail_skipped_reason": None if loaded.resume_ready else "schema_v1_not_resumable",
        "total_prototype_seconds_excluding_pane": round(sum(total_parts), 3),
        "tail_bars_available": max(0, len(candles) - index - 1),
        "parity": parity,
        "CHECKPOINT_USED_FOR_STARTUP": CHECKPOINT_USED_FOR_STARTUP,
        "full_warmup_reference_seconds": 1676.775,
    }
    print("PROTOTYPE_SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)
    return summary


def write_read_only_checkpoint(st: Any, config: ScannerConfig) -> Path:
    if not config.write_resume_checkpoint:
        raise RuntimeError("checkpoint write disabled")
    doc = build_checkpoint_document(st, config)
    path = checkpoint_path_for_symbol(config, st.symbol)
    atomic_write_json(path, doc)
    return path
