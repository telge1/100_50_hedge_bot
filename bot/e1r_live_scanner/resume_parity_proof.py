"""One-symbol full-warmup vs resume tail proof.

This module does not start a warmup unless the caller passes a checkpoint
close or explicitly allows a full-tip warmup. It never writes into the live
checkpoint directory and is not used by startup.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.e1r_live_scanner.config import REPO_ROOT, ScannerConfig
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.resume_checkpoint import (
    CHECKPOINT_USED_FOR_STARTUP,
    e1r_resume_snapshot,
    load_resume_checkpoint,
    payload_state_hash,
    prime_e1r_walker_to_checkpoint,
    prime_frozen_walker_to_checkpoint,
    restore_symbol_state_from_checkpoint_v1,
    write_read_only_checkpoint,
)

LIVE_CHECKPOINT_DIR = REPO_ROOT / "bot/e1r_live_scanner/runtime/checkpoints"
_SIGNAL_KEYS = (
    "symbol",
    "entry_open",
    "decision_time",
    "entry_price",
    "stop",
    "tp",
    "pool_id",
    "floor_guard_blocked",
    "floor_reason",
    "e1r_state",
    "signal_status",
    "block_reason",
    "variant",
    "ema200",
    "rising_3",
    "cluster_1_top",
    "cluster_2_top",
    "cluster_3_top",
)


def refuse_live_checkpoint_dir(path: Path) -> None:
    live = LIVE_CHECKPOINT_DIR.resolve()
    resolved = path.resolve()
    if resolved == live or live in resolved.parents:
        raise ValueError("refusing to write a proof checkpoint into the live checkpoint directory")


def project_signal(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _SIGNAL_KEYS}


def _floor_view(guard: Any) -> dict[str, Any]:
    block = guard.block_until.isoformat() if guard is not None and guard.block_until is not None else None
    return {
        "block_until": block,
        "session_floor_top": None if guard is None else guard.session_floor_top,
        "session_floor_bottom": None if guard is None else guard.session_floor_bottom,
        "last_reason": None if guard is None else guard.last_reason,
    }


def capture_end_state(st: Any, signal_rows: list[dict]) -> dict[str, Any]:
    snap = e1r_resume_snapshot(st.e1r_engine)
    close = st.last_processed_15m_close
    return {
        "last_processed_15m_close": close.isoformat() if close is not None else None,
        "seen_no_guard": sorted(st.seen_no_guard),
        "seen_with_guard": sorted(st.seen_with_guard),
        "e1r_state": snap["e1r_state"],
        "e1r_machine": snap["e1r_machine"],
        "pending": snap["pending"],
        "e1r_ep": snap["e1r_ep"],
        "reclaim_events": snap["reclaim_events"],
        "e1r_snapshot_hash": payload_state_hash(snap),
        "floor": _floor_view(st.floor_guard),
        "signals": [project_signal(row) for row in signal_rows],
    }


def _first_key_diff(left: dict, right: dict) -> str | None:
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            return key
    return None


def compare_end_states(full: dict[str, Any], resume: dict[str, Any], *, bar: str | None = None) -> dict[str, Any]:
    """Exact field verdicts. Large values are not copied into the diff."""
    fields = (
        "last_processed_15m_close",
        "seen_no_guard",
        "seen_with_guard",
        "e1r_state",
        "e1r_machine",
        "pending",
        "e1r_ep",
        "reclaim_events",
        "e1r_snapshot_hash",
        "floor",
        "frozen_pool_ids",
        "frozen_rows_hash",
        "e1r_lld_pool_ids",
        "e1r_lld_rows_hash",
        "signals",
    )
    verdicts: dict[str, str] = {}
    diffs: list[dict[str, Any]] = []
    for field in fields:
        ok = full.get(field) == resume.get(field)
        verdicts[field] = "PASS" if ok else "FAIL"
        if ok:
            continue
        item: dict[str, Any] = {"field": field, "bar": bar}
        if field == "e1r_machine":
            key = _first_key_diff(full.get(field) or {}, resume.get(field) or {})
            item["first_key"] = key
            item["full"] = None if key is None else (full.get(field) or {}).get(key)
            item["resume"] = None if key is None else (resume.get(field) or {}).get(key)
        elif field in ("seen_no_guard", "seen_with_guard", "frozen_pool_ids", "e1r_lld_pool_ids"):
            full_set = set(full.get(field) or [])
            resume_set = set(resume.get(field) or [])
            item["full_only"] = sorted(full_set - resume_set)[:10]
            item["resume_only"] = sorted(resume_set - full_set)[:10]
        elif field == "signals":
            full_rows = full.get(field) or []
            resume_rows = resume.get(field) or []
            item["full_count"] = len(full_rows)
            item["resume_count"] = len(resume_rows)
            for index, (left, right) in enumerate(zip(full_rows, resume_rows)):
                if left != right:
                    item["first_row"] = index
                    item["full"] = left
                    item["resume"] = right
                    break
            else:
                if len(full_rows) != len(resume_rows):
                    item["length_mismatch"] = True
        else:
            item["full"] = full.get(field)
            item["resume"] = resume.get(field)
        diffs.append(item)
    return {"verdicts": verdicts, "pass": all(value == "PASS" for value in verdicts.values()), "diffs": diffs}


def _pool_views(st: Any, bar: dict) -> dict[str, Any]:
    from bot.e1r_live_scanner.short_pools import available_for_short_entry
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index
    from dashboard.research_charts.short_pool_oracle_v1 import rows_hash

    frozen = available_for_short_entry(
        st.symbol,
        st.candles15,
        bar["close_time"],
        frozen_lld_cache=st.frozen_lld_cache,
    )
    available, _ui, _proof = scanner_pools_for_index(
        st.candles15,
        "15m",
        bar["candle_index"],
        st.lld_cfg,
        cache=st.lld_cache,
    )
    return {
        "frozen_pool_ids": list(frozen.available_pool_ids),
        "frozen_rows_hash": frozen.available_rows_hash,
        "e1r_lld_pool_ids": sorted(row["pool_id"] for row in available),
        "e1r_lld_rows_hash": rows_hash(available),
    }


def _attach_shared_pane(target: Any, source: Any) -> None:
    target.candles15 = source.candles15
    target.bars15 = source.bars15
    target.by_open = source.by_open
    target.market = source.market
    target.lld_cfg = source.lld_cfg
    target.strict_complete_buckets = True


def run_one_symbol_resume_parity(
    symbol: str = "APTUSDT",
    *,
    tail_bars: int = 3,
    through_close: datetime | None = None,
    allow_full_warmup: bool = False,
    checkpoint_dir: Path | None = None,
) -> dict[str, Any]:
    """Compare one full warmup through T with restore-at-T_c plus a short tail.

    ``through_close`` is T_c. The tail is the next ``tail_bars`` closes.
    Omitting ``through_close`` without ``allow_full_warmup`` does not load data.
    """
    if tail_bars < 1:
        raise ValueError("tail_bars must be at least 1")
    if through_close is None and not allow_full_warmup:
        raise RuntimeError(
            "refusing to start a full-tip warmup; pass through_close or allow_full_warmup=True"
        )
    if checkpoint_dir is None:
        raise ValueError("checkpoint_dir is required and must not be the live checkpoint directory")
    refuse_live_checkpoint_dir(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    sym = str(symbol).strip().upper()
    config = ScannerConfig(
        live=False,
        write_resume_checkpoint=True,
        checkpoint_dir=checkpoint_dir,
        frozen_pool_transition_parity=False,
        frozen_pool_shadow_parity=False,
        log_path=checkpoint_dir / "signals.jsonl",
    )
    processor = SymbolProcessor(config)
    pane_started = time.perf_counter()
    reference = processor.load_state(sym)
    pane_seconds = time.perf_counter() - pane_started
    if not reference.bars15:
        return {
            "SCHEMA2_CHECKPOINT_VALID": "NO",
            "REAL_RESUME_END_STATE_PARITY": "NOT_RUN",
            "reason": reference.data_missing_reason or "no bars",
            "CHECKPOINT_USED_FOR_STARTUP": CHECKPOINT_USED_FOR_STARTUP,
        }

    if through_close is None:
        cutoff_index = len(reference.bars15) - 1 - tail_bars
        if cutoff_index < 1:
            raise ValueError("pane is shorter than the requested tail")
        checkpoint_close = reference.bars15[cutoff_index]["close_time"]
    else:
        checkpoint_close = through_close
    tail = [bar for bar in reference.bars15 if bar["close_time"] > checkpoint_close][:tail_bars]
    if len(tail) < tail_bars:
        raise ValueError(f"only {len(tail)} bars exist after {checkpoint_close.isoformat()}")

    warmup_started = time.perf_counter()
    processor.warmup(reference, through_close=checkpoint_close)
    full_warmup_seconds = time.perf_counter() - warmup_started
    write_read_only_checkpoint(reference, config)
    path = checkpoint_dir / f"{sym}.json"

    from dashboard.research_charts.lld_research_kernel import candle_stamps

    load_started = time.perf_counter()
    loaded = load_resume_checkpoint(
        path,
        expected_symbol=sym,
        pane_opens=candle_stamps(reference.candles15),
    )
    checkpoint_load_seconds = time.perf_counter() - load_started
    if not loaded.resume_ready:
        return {
            "SCHEMA2_CHECKPOINT_VALID": "NO",
            "REAL_RESUME_END_STATE_PARITY": "NOT_RUN",
            "readiness": loaded.readiness,
            "reasons": loaded.reasons,
            "resume_blockers": loaded.resume_blockers,
            "full_warmup_seconds": round(full_warmup_seconds, 3),
            "CHECKPOINT_USED_FOR_STARTUP": CHECKPOINT_USED_FOR_STARTUP,
        }

    restore_started = time.perf_counter()
    restored = restore_symbol_state_from_checkpoint_v1(loaded)
    restore_seconds = time.perf_counter() - restore_started
    resume = restored.state
    _attach_shared_pane(resume, reference)

    frozen_started = time.perf_counter()
    prime_frozen_walker_to_checkpoint(
        sym,
        resume.candles15,
        checkpoint_close,
        cache=resume.frozen_lld_cache,
    )
    frozen_seconds = time.perf_counter() - frozen_started
    e1r_started = time.perf_counter()
    prime_e1r_walker_to_checkpoint(
        resume.candles15,
        int(loaded.metadata["checkpoint_candle_index"]),
        cache=resume.lld_cache,
    )
    e1r_seconds = time.perf_counter() - e1r_started

    checkpoint_bar = next(bar for bar in reference.bars15 if bar["close_time"] == checkpoint_close)
    at_checkpoint = compare_end_states(
        {**capture_end_state(reference, []), **_pool_views(reference, checkpoint_bar)},
        {**capture_end_state(resume, []), **_pool_views(resume, checkpoint_bar)},
        bar=checkpoint_close.isoformat(),
    )

    tail_started = time.perf_counter()
    full_signals: list[dict] = []
    resume_signals: list[dict] = []
    first_bar = None
    final_compare = at_checkpoint
    for bar in tail:
        full_signals.extend(processor._process_one_bar(reference, bar, emit_signals=True))
        resume_signals.extend(processor._process_one_bar(resume, bar, emit_signals=True))
        reference.last_processed_15m_close = bar["close_time"]
        resume.last_processed_15m_close = bar["close_time"]
        final_compare = compare_end_states(
            {**capture_end_state(reference, full_signals), **_pool_views(reference, bar)},
            {**capture_end_state(resume, resume_signals), **_pool_views(resume, bar)},
            bar=bar["close_time"].isoformat(),
        )
        if not final_compare["pass"]:
            first_bar = bar["close_time"].isoformat()
            break
    tail_seconds = time.perf_counter() - tail_started
    groups = {
        "Frozen pools": ("frozen_pool_ids", "frozen_rows_hash"),
        "E1R": ("e1r_state", "e1r_machine", "pending", "e1r_ep", "e1r_snapshot_hash", "e1r_lld_pool_ids", "e1r_lld_rows_hash"),
        "seen": ("seen_no_guard", "seen_with_guard"),
        "Floor": ("floor",),
        "reclaim_events": ("reclaim_events",),
        "Signals": ("signals",),
    }
    grouped = {
        name: "PASS" if all(final_compare["verdicts"].get(field) == "PASS" for field in fields) else "FAIL"
        for name, fields in groups.items()
    }
    total = checkpoint_load_seconds + restore_seconds + frozen_seconds + e1r_seconds + tail_seconds
    return {
        "SCHEMA2_CHECKPOINT_VALID": "YES",
        "symbol": sym,
        "checkpoint_close": checkpoint_close.isoformat(),
        "tail_end": tail[-1]["close_time"].isoformat(),
        "FULL_VS_RESUME": grouped,
        "field_verdicts": final_compare["verdicts"],
        "diffs": final_compare["diffs"],
        "checkpoint_match_before_tail": at_checkpoint["pass"],
        "FIRST_DIVERGENCE_BAR": first_bar,
        "RESUME_TOTAL_SECONDS": round(total, 3),
        "checkpoint_load_seconds": round(checkpoint_load_seconds, 3),
        "pane_load_seconds": round(pane_seconds, 3),
        "state_restore_seconds": round(restore_seconds, 3),
        "frozen_prime_seconds": round(frozen_seconds, 3),
        "e1r_prime_seconds": round(e1r_seconds, 3),
        "tail_replay_seconds": round(tail_seconds, 3),
        "FULL_WARMUP_SECONDS": round(full_warmup_seconds, 3),
        "REAL_RESUME_END_STATE_PARITY": "PASS" if final_compare["pass"] else "FAIL",
        "SAFE_FOR_CONTROLLED_STARTUP_CUTOVER": "NO",
        "CHECKPOINT_USED_FOR_STARTUP": CHECKPOINT_USED_FOR_STARTUP,
    }
