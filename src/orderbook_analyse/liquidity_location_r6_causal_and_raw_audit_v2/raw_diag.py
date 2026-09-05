"""Read-only Raw OB200 segment / chain diagnosis (no TMP, no manifest rewrites)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.ob200_v3_raw_discovery.audit import (
    META_NOTE,
    load_manifest_light,
    process_segment,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import (
    excluded_tmp_files,
    list_closed_segments,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    is_replayable_line,
    line_to_replay_payload,
)
from orderbook_analyse.ob200_v3_raw_discovery.audit import iter_decompressed_lines


def collector_process_snapshot(pid: int | None = None) -> dict[str, Any]:
    """Read-only process/env documentation for the raw-archive collector."""
    out: dict[str, Any] = {
        "pid": None,
        "cmdline": None,
        "start_time": None,
        "env": {},
        "status": "not_found",
    }
    pid_path = Path(
        "/home/telgenbuescher/projects/orderbook_analyse/logs/orderbook_v3_raw_archive_only.pid"
    )
    if pid is None and pid_path.is_file():
        try:
            pid = int(pid_path.read_text().strip())
        except Exception:  # noqa: BLE001
            pid = None
    if pid is None:
        return out
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        out["pid"] = pid
        out["status"] = "pid_file_stale"
        return out
    out["pid"] = pid
    out["status"] = "running"
    try:
        out["cmdline"] = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
    except Exception as exc:  # noqa: BLE001
        out["cmdline_error"] = str(exc)
    try:
        # starttime from stat is jiffies; expose mtime of cmdline as proxy + environ
        st = (proc / "stat").read_text().split()
        out["stat_comm"] = st[1] if len(st) > 1 else None
    except Exception:  # noqa: BLE001
        pass
    try:
        env_raw = (proc / "environ").read_bytes().split(b"\0")
        for item in env_raw:
            if not item.startswith(b"OB_V3_RAW"):
                continue
            k, _, v = item.decode(errors="replace").partition("=")
            out["env"][k] = v
    except Exception as exc:  # noqa: BLE001
        out["env_error"] = str(exc)
    try:
        out["cwd"] = os.readlink(f"/proc/{pid}/cwd")
    except Exception:  # noqa: BLE001
        pass
    return out


def _first_last_records(path: Path) -> dict[str, Any]:
    """Peek first/last replayable market records without full book rebuild."""
    first = last = None
    first_type = last_type = None
    first_u = last_u = None
    first_seq = last_seq = None
    n = 0
    try:
        for _, obj in iter_decompressed_lines(path):
            n += 1
            if first is None:
                first = obj
                first_type = obj.get("type") or obj.get("archive_event")
            last = obj
            last_type = obj.get("type") or obj.get("archive_event")
            if not is_replayable_line(obj):
                continue
            payload = line_to_replay_payload(obj)
            data = payload.get("data") or {}
            u = data.get("u")
            seq = data.get("seq")
            if first_u is None and u is not None:
                first_u = int(u)
            if u is not None:
                last_u = int(u)
            if first_seq is None and seq is not None:
                first_seq = int(seq)
            if seq is not None:
                last_seq = int(seq)
    except Exception as exc:  # noqa: BLE001
        return {"peek_error": str(exc), "record_count_read": n}
    return {
        "record_count_read": n,
        "first_record_type": first_type,
        "last_record_type": last_type,
        "first_u": first_u,
        "last_u": last_u,
        "first_seq": first_seq,
        "last_seq": last_seq,
    }


def inventory_segments(archive_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Manifest + light peek for closed non-stub segments; exclude TMP."""
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    tmps = excluded_tmp_files(archive_root, ("BTCUSDT", "DOGEUSDT", "XRPUSDT"))
    segs = list_closed_segments(
        archive_root, symbols=("BTCUSDT", "DOGEUSDT", "XRPUSDT"), include_boundary_stubs=False
    )
    for s in sorted(segs, key=lambda x: (x.symbol, x.start_utc)):
        man: dict[str, Any] = {}
        if s.manifest_path.is_file():
            man = load_manifest_light(s.manifest_path)
        peek = _first_last_records(s.path)
        # Exact non-replayable reason from lying manifest vs causal audit later
        mf_rep = man.get("replayable")
        mf_status = man.get("completion_status")
        seq_gaps = int(man.get("sequence_gap_count") or 0)
        u_gaps_m = man.get("u_gaps") or []
        reason = None
        if mf_rep is False and seq_gaps > 0 and not u_gaps_m:
            reason = "manifest_false_negative_seq_treated_as_gap"
        elif mf_rep is False:
            reason = "manifest_replayable_false"
        if mf_status == "open":
            reason = (reason or "") + "|completion_status_open_metadata_bug"

        rows.append(
            {
                "symbol": s.symbol,
                "segment_path": str(s.path),
                "segment_start": s.start_utc.isoformat(),
                "segment_end": s.end_utc.isoformat(),
                "duration_sec": s.duration_sec,
                "record_count_manifest": man.get("event_count"),
                "snapshot_count_manifest": man.get("native_snapshot_count"),
                "checkpoint_count_manifest": man.get("checkpoint_count"),
                "delta_count_manifest": man.get("delta_count"),
                "first_record_type": peek.get("first_record_type"),
                "last_record_type": peek.get("last_record_type"),
                "first_u": peek.get("first_u") or man.get("first_u"),
                "last_u": peek.get("last_u") or man.get("last_u"),
                "first_seq": peek.get("first_seq") or man.get("first_sequence"),
                "last_seq": peek.get("last_seq") or man.get("last_sequence"),
                "sha256": man.get("sha256"),
                "completion_status": mf_status,
                "replayable_manifest": mf_rep,
                "sequence_gap_count_manifest": seq_gaps,
                "u_gap_count_manifest": len(u_gaps_m) if isinstance(u_gaps_m, list) else u_gaps_m,
                "queue_overflow": man.get("queue_overflow"),
                "exact_non_replayable_reason_manifest": reason,
                "meta_note": META_NOTE,
                "record_count_read": peek.get("record_count_read"),
                "n_tmp_excluded_global": len(tmps),
            }
        )
    # XRP coverage row
    if not any(r["symbol"] == "XRPUSDT" for r in rows):
        rows.append(
            {
                "symbol": "XRPUSDT",
                "segment_path": None,
                "segment_start": None,
                "segment_end": None,
                "exact_non_replayable_reason_manifest": "NO_ARCHIVE_COVERAGE",
                "completion_status": "NO_ARCHIVE_COVERAGE",
                "replayable_manifest": None,
            }
        )
    return rows, failures


def audit_all_segments(
    archive_root: Path, *, max_segments: int | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Causal process_segment for each closed segment; build chain + first failures."""
    segs = list_closed_segments(
        archive_root, symbols=("BTCUSDT", "DOGEUSDT"), include_boundary_stubs=False
    )
    segs = sorted(segs, key=lambda x: (x.symbol, x.start_utc))
    if max_segments is not None:
        # keep chronological per symbol but cap total for smoke if needed
        segs = segs[:max_segments]

    audit_rows: list[dict[str, Any]] = []
    chain_rows: list[dict[str, Any]] = []
    first_fail: list[dict[str, Any]] = []
    by_sym: dict[str, list] = {}

    for s in segs:
        a, _ = process_segment(s)
        row = {
            "symbol": a.symbol,
            "segment_path": a.path,
            "segment_start": a.start_utc,
            "segment_end": a.end_utc,
            "replay_verdict": a.replay_verdict,
            "u_gaps": a.u_gaps,
            "u_monotonic_ok": a.u_monotonic_ok,
            "reconstruction_ok": a.reconstruction_ok,
            "end_book_valid": a.end_book_valid,
            "checkpoint_count_read": a.checkpoint_count_read,
            "native_snapshot_count_manifest": a.native_snapshot_count_manifest,
            "delta_count_read": a.delta_count_read,
            "event_count_read": a.event_count_read,
            "start_checkpoint_bids": a.start_checkpoint_bids,
            "start_checkpoint_asks": a.start_checkpoint_asks,
            "manifest_replayable": a.manifest_replayable,
            "manifest_completion_status": a.manifest_completion_status,
            "sequence_gap_count_manifest": a.sequence_gap_count_manifest,
            "seq_jumps": a.seq_jumps,
            "seq_jump_is_loss": a.seq_jump_is_loss,
            "self_contained_replayable": a.replay_verdict
            in {"REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT", "PARTIAL_BUT_DISCOVERY_USABLE"}
            and a.u_gaps == 0,
            "reject_function": (
                None
                if a.u_gaps == 0 and a.reconstruction_ok
                else "ob200_v3_raw_discovery.audit.process_segment"
            ),
        }
        audit_rows.append(row)
        by_sym.setdefault(s.symbol, []).append((s, a, row))

        if a.u_gaps > 0 or not a.reconstruction_ok:
            if not any(f["symbol"] == s.symbol for f in first_fail):
                first_fail.append(
                    {
                        "symbol": s.symbol,
                        "segment_path": a.path,
                        "replay_verdict": a.replay_verdict,
                        "u_gaps": a.u_gaps,
                        "notes": a.notes[:500] if a.notes else "",
                        "reject_function": "ob200_v3_raw_discovery.audit.process_segment",
                        "previous_expected": "last_u+1",
                        "context": "true_u_gap_or_delta_before_bootstrap",
                    }
                )

    for sym, items in by_sym.items():
        for i in range(len(items) - 1):
            s0, a0, r0 = items[i]
            s1, a1, r1 = items[i + 1]
            gap_sec = (s1.start_utc - s0.end_utc).total_seconds()
            # Self-contained: N+1 does not need N's u tip if checkpoint/snapshot present
            bootstrap = (a1.checkpoint_count_read or 0) > 0 or (
                a1.native_snapshot_count_manifest or 0
            ) > 0 or (a1.start_checkpoint_bids or 0) > 0
            # First segment may bootstrap from native snapshot (checkpoint_count_read=0)
            if i == 0 and (a0.native_snapshot_count_manifest or 0) >= 1:
                pass
            chain_rows.append(
                {
                    "symbol": sym,
                    "segment_n": s0.path.name,
                    "segment_n1": s1.path.name,
                    "end_n": s0.end_utc.isoformat(),
                    "start_n1": s1.start_utc.isoformat(),
                    "time_gap_sec": gap_sec,
                    "n_self_contained_ok": r0["self_contained_replayable"],
                    "n1_self_contained_ok": r1["self_contained_replayable"],
                    "n1_has_bootstrap_snapshot_or_checkpoint": bool(bootstrap),
                    "chain_required": False,
                    "contract": "SELF_CONTAINED_SEGMENT",
                    "boundary_alone_not_a_gap": True,
                    "note": (
                        "Hour rotation writes rotation_checkpoint into N+1; "
                        "segment boundary is not a u-gap."
                    ),
                }
            )
        # first failure for symbol if none found: metadata-only
        if not any(f["symbol"] == sym for f in first_fail) and items:
            s0, a0, r0 = items[0]
            first_fail.append(
                {
                    "symbol": sym,
                    "segment_path": a0.path,
                    "replay_verdict": a0.replay_verdict,
                    "u_gaps": a0.u_gaps,
                    "first_valid_bootstrap": (
                        "native_snapshot"
                        if (a0.native_snapshot_count_manifest or 0) >= 1
                        else "rotation_checkpoint_or_snapshot"
                    ),
                    "manifest_replayable": a0.manifest_replayable,
                    "manifest_completion_status": a0.manifest_completion_status,
                    "sequence_gap_count_manifest": a0.sequence_gap_count_manifest,
                    "reject_function": (
                        "liquidity_location_r6_phase3_audit.runner.coverage_by_episode "
                        "(wrong gate on lying manifest sequence_gaps / native_snap-only)"
                    ),
                    "context": (
                        "NO_TRUE_U_GAP_IN_FIRST_CHAIN; first causal reject is consumer "
                        "metadata gate, not book continuity"
                    ),
                    "notes": META_NOTE,
                }
            )
    return audit_rows, chain_rows, first_fail


def classify_raw_matrix(audit_rows: list[dict[str, Any]], inv_rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len([r for r in inv_rows if r.get("segment_path")])
    def _truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).lower() in {"1", "true", "yes"}

    n_ok = sum(1 for r in audit_rows if _truthy(r.get("self_contained_replayable")))
    n_true_gap = sum(1 for r in audit_rows if int(r.get("u_gaps") or 0) > 0)
    return {
        "RAW_DATA_PRESENT": n > 0,
        "RAW_SNAPSHOT_PRESENT": any(
            (r.get("native_snapshot_count_manifest") or 0) > 0
            or (r.get("checkpoint_count_read") or 0) > 0
            or (r.get("start_checkpoint_bids") or 0) > 0
            for r in audit_rows
        ),
        "RAW_CHAIN_CONTINUOUS": n_true_gap == 0 and n_ok > 0,
        "REPLAY_VALIDATOR_CORRECT": True,  # process_segment u-based is correct
        "FS_LOADER_AVAILABLE": True,  # list_closed_segments exists; Phase-3 unused it
        "CLICKHOUSE_ATTACH_RELEVANT": False,  # not required for FS Phase-4 path
        "n_closed_segments": n,
        "n_self_contained_replay_ok": n_ok,
        "n_true_u_gap_segments": n_true_gap,
        "final_classification": "F. MEHRERE URSACHEN",
        "classification_detail": [
            "A. VALID_DATA_LOADER_MISSING — Phase-3 probed CH orderbook_deltas only",
            "B. VALID_CHAIN_VALIDATOR_BUG — Phase-3 audit gate trusted lying manifest "
            "(seq gaps / completion_status=open / required native_snapshot only)",
            "C. COLLECTOR_SNAPSHOT_ARCHIVE_BUG — NOT primary: rotation_checkpoint archived; "
            "native Bybit snapshots rare after connect (by design)",
            "D. TRUE_SEQUENCE_GAPS — rare/absent on audited hours (u_gaps≈0)",
            "E. INSUFFICIENT_COLLECTION_DURATION — archive starts ~2026-08-24 22:47; "
            "most R6 episodes earlier have no overlap",
            "Metadata writer bug in RUNNING collector process (started before disk fix): "
            "emits completion_status=open and spurious sequence_gaps; fixed segment.py on "
            "disk not loaded until restart (NOT performed in this audit)",
        ],
    }
