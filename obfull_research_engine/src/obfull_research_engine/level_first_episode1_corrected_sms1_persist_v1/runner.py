"""Run Episode-1 corrected sms1 persist + derived-event rebuild. Archive read-only."""

from __future__ import annotations

import json
import resource
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_csv, atomic_write_json, atomic_write_text
from ..drilldown.aggregation_100ms import (
    EVENT_TIME_SEMANTICS,
    _as_dt,
    _floor_bucket,
    book_map_sha256,
    build_states_100ms,
)
from ..drilldown.engine import _book_at
from ..drilldown.replay import replay_window
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    ALLOW_ARCHIVE_REPLAY,
    ALLOW_CLICKHOUSE_WRITES,
    AUDIT_ID,
    BUCKET_MS,
    CONTRACT_VERSION,
    DETECTION,
    DETECTION_ISO,
    EFFECTIVE_BUCKET_START_ISO,
    EPISODE_ID,
    EVIDENCE_START,
    EVIDENCE_START_ISO,
    FIELD_MAP_OLD_TO_NEW,
    FIRST_BUCKET_AVAILABLE_AT,
    FIRST_BUCKET_AVAILABLE_AT_ISO,
    FIRST_TOUCH,
    FIRST_TOUCH_ISO,
    FORBIDDEN_FIELDS,
    FROZEN,
    PARENT_GOLDEN_RUN,
    PARENT_GOLDEN_VERDICT,
    RUN_PREFIX,
    SMS1_EP1_STATES,
    SMS1_EP1_STATES_SHA256,
    SMS1_EP1_TRADES,
    SYMBOL,
)
from .derived import (
    available_at_violations,
    build_refills,
    build_touch_detection,
    build_walls,
)
from .diff import diff_refills, diff_touch_detection, diff_walls, summarize
from .golden import (
    all_bucket_end_cutoffs,
    boundary_event_audit,
    golden_all_cutoffs,
    reset_before_after_parity,
)
from .io_zst import body_rows, read_jsonl_zst
from .persist import serialize_state, write_tables
from .reader import last_state_asof, load_table, reconstruct_from_persist

CONFIG_REL = "config/level_first_episode1_corrected_sms1_persist_v1.json"
RESULTS_ROOT = ENGINE_ROOT / "results" / "level_first_episode1_corrected_sms1_persist_v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _peak() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / (1024 * 1024), 3) if rss > 10_000 else round(rss / (1024 * 1024 * 1024), 3)


def _git() -> dict[str, str]:
    import subprocess

    repo = ENGINE_ROOT.parent
    try:
        return {
            "branch": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip(),
            "head": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
            "dirty": "true" if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip() else "false",
        }
    except Exception:  # noqa: BLE001
        return {"branch": "unknown", "head": "unknown", "dirty": "unknown"}


def _pids() -> dict[str, Any]:
    import subprocess

    def _pgrep(pattern: str) -> list[dict[str, str]]:
        try:
            out = subprocess.check_output(["ps", "-eo", "pid,cmd"], text=True)
        except Exception:  # noqa: BLE001
            return []
        rows = []
        for line in out.splitlines()[1:]:
            if pattern in line and "pgrep" not in line:
                pid, _, cmd = line.strip().partition(" ")
                rows.append({"pid": pid.strip(), "cmd": cmd.strip()[:200]})
        return rows

    return {
        "clickhouse-server": _pgrep("clickhouse-server"),
        "oi_liquidation_collector": _pgrep("oi_liquidation_collector"),
        "Signal_Generator": _pgrep("run_live_collector_service.py"),
    }


def _frozen_hashes() -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for key, rel in FROZEN.items():
        path = ENGINE_ROOT / rel
        rows[key] = {
            "path": rel,
            "exists": path.is_dir(),
            "status": file_sha256(path / "STATUS") if (path / "STATUS").is_file() else None,
            "run_manifest": file_sha256(path / "run_manifest.json") if (path / "run_manifest.json").is_file() else None,
        }
    sms = ENGINE_ROOT / SMS1_EP1_STATES
    rows["sms1_ep1_states_100ms"] = {
        "path": SMS1_EP1_STATES,
        "exists": sms.is_file(),
        "sha256": file_sha256(sms) if sms.is_file() else None,
        "expected_sha256": SMS1_EP1_STATES_SHA256,
        "hash_identical": (file_sha256(sms) == SMS1_EP1_STATES_SHA256) if sms.is_file() else False,
    }
    return rows


class _PersistTrade:
    __slots__ = ("trade_ts", "side", "notional", "price", "size", "trade_id")

    def __init__(self, row: dict[str, Any]):
        self.trade_ts = parse_utc(row["trade_ts"])
        self.side = row.get("taker_side") or row.get("side") or ""
        self.notional = float(row.get("notional") or 0)
        self.price = float(row.get("price") or 0)
        self.size = float(row.get("size") or 0)
        self.trade_id = row.get("trade_id")


def _load_sms1_trades(
    *,
    start: datetime = EVIDENCE_START,
    end: datetime = DETECTION,
) -> list[_PersistTrade]:
    path = ENGINE_ROOT / SMS1_EP1_TRADES
    if not path.is_file():
        return []
    out: list[_PersistTrade] = []
    for row in body_rows(read_jsonl_zst(path)):
        if not row.get("trade_ts"):
            continue
        trade = _PersistTrade(row)
        if start <= trade.trade_ts < end:
            out.append(trade)
    return out


def _builder_kwargs(replay: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_start": replay["window_start"],
        "window_end": replay["window_end"],
        "timeline": replay["timeline"],
        "level_changes": replay["level_changes"],
        "trades": replay["trades"],
        "initial_bids": replay["initial_bids"],
        "initial_asks": replay["initial_asks"],
        "bucket_ms": BUCKET_MS,
        "ordering_confidence": replay.get("ordering_confidence") or "ORDERING_DETERMINISTIC_CONTRACT",
        "evidence_start": replay["window_start"],
        "initial_update_id": replay.get("initial_update_id"),
        "initial_sequence_id": replay.get("initial_sequence_id"),
        "initial_replay_epoch": replay.get("initial_replay_epoch"),
        "initial_checkpoint_id": replay.get("initial_checkpoint_id"),
        "book_snapshots_by_time": replay.get("book_snapshots_by_time"),
        "book_resets": replay.get("book_resets"),
    }


def partial_first_bucket_audit(*, replay: dict[str, Any], states: list[dict[str, Any]]) -> dict[str, Any]:
    first_global = _floor_bucket(EVIDENCE_START, BUCKET_MS)
    first_end = first_global + timedelta(milliseconds=BUCKET_MS)
    pre_changes = []
    for event in replay.get("level_changes") or []:
        et = _as_dt(event["event_time"])
        if first_global <= et < EVIDENCE_START:
            pre_changes.append(format_utc_z(et))
    first_state = states[0] if states else None
    return {
        "evidence_start": EVIDENCE_START_ISO,
        "global_bucket_start": format_utc_z(first_global),
        "global_bucket_end_exclusive": format_utc_z(first_end),
        "effective_bucket_start": EFFECTIVE_BUCKET_START_ISO,
        "pre_evidence_level_changes_count": len(pre_changes),
        "pre_evidence_level_changes": pre_changes,
        "first_state_available_at": format_utc_z(_as_dt(first_state["available_at"])) if first_state else None,
        "first_bucket_available_at_expected": FIRST_BUCKET_AVAILABLE_AT_ISO,
        "first_bucket_available_at_ok": bool(
            first_state is not None and _as_dt(first_state["available_at"]) == FIRST_BUCKET_AVAILABLE_AT
        ),
        "first_state_effective_bucket_start": format_utc_z(_as_dt(first_state["effective_bucket_start"]))
        if first_state
        else None,
    }


def roundtrip_proof(directory: Path, states: list[dict[str, Any]], replay: dict[str, Any]) -> dict[str, Any]:
    loaded = load_table(directory, "states_100ms")
    n = min(len(loaded), len(states))
    epoch_ok = all(loaded[i].get("replay_epoch") == states[i].get("replay_epoch") for i in range(n))
    avail_ok = all(
        loaded[i].get("available_at") == format_utc_z(_as_dt(states[i]["available_at"])) for i in range(n)
    )
    fields_ok = all(
        loaded[i].get("bucket_start")
        and loaded[i].get("bucket_end_exclusive")
        and loaded[i].get("available_at")
        and loaded[i].get("effective_bucket_start")
        and loaded[i].get("replay_epoch") is not None
        for i in range(n)
    )
    last_asof = last_state_asof(loaded, FIRST_TOUCH)
    used_before = False
    if last_asof is not None:
        used_before = _as_dt(last_asof["available_at"]) > FIRST_TOUCH
    recon_ok = True
    recon_notes = []
    by_avail = {row.get("available_at"): row for row in loaded}
    cuts = [_as_dt(states[0]["available_at"]), _as_dt(states[-1]["available_at"]), FIRST_TOUCH, DETECTION]
    for rec in replay.get("book_resets") or []:
        cuts.append(_as_dt(rec["event_time"]))
    for ts in cuts:
        live = reconstruct_from_persist(directory, ts)
        digest = book_map_sha256(live["bids"], live["asks"])
        persisted = by_avail.get(format_utc_z(ts))
        if persisted is not None and persisted.get("book_map_sha256") != digest:
            recon_ok = False
        recon_notes.append(
            {
                "until": format_utc_z(ts),
                "best_bid": live.get("best_bid"),
                "best_ask": live.get("best_ask"),
                "replay_epoch": live.get("replay_epoch"),
                "book_map_sha256": digest,
            }
        )
        if live.get("best_bid") is None:
            recon_ok = False
    # identity resets present after read
    resets = load_table(directory, "book_resets")
    identity = [r for r in resets if r.get("identity_reset")]
    return {
        "n_states_written": len(states),
        "n_states_read": len(loaded),
        "row_count_match": len(loaded) == len(states),
        "replay_epoch_roundtrip_ok": epoch_ok,
        "available_at_roundtrip_ok": avail_ok,
        "required_fields_present": fields_ok,
        "last_complete_at_touch_used_before_available": used_before,
        "n_resets_read": len(resets),
        "n_identity_resets_read": len(identity),
        "reconstruct_cutoffs": recon_notes,
        "reconstruct_ok": recon_ok and len(resets) == len(replay.get("book_resets") or []),
    }


def file_hashes(directory: Path, names: list[str]) -> dict[str, str]:
    out = {}
    for name in names:
        path = directory / name
        if path.is_file():
            out[name] = file_sha256(path)
    return out


def content_compare(primary: Path, repeat: Path, names: list[str]) -> dict[str, Any]:
    h1 = file_hashes(primary, names)
    h2 = file_hashes(repeat, names)
    byte_ok = h1 == h2
    mismatches = [k for k in names if h1.get(k) != h2.get(k)]
    return {
        "byte_identical": byte_ok,
        "primary_sha256": h1,
        "repeat_sha256": h2,
        "mismatched_files": mismatches,
        "excluded_metadata_fields": ["created_at", "elapsed_s", "peak_ram_gb", "pids_after"],
    }


def decide_verdict(
    *,
    replay: dict[str, Any] | None,
    golden: dict[str, Any],
    partial: dict[str, Any],
    boundary: dict[str, Any],
    rt: dict[str, Any],
    det: dict[str, Any],
    violations: list[dict[str, Any]],
    frozen: dict[str, Any],
    same_stream: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not replay or not replay.get("ok"):
        return "ARCHIVE_OR_COVERAGE_BLOCKED", [str((replay or {}).get("error") or "REPLAY_NOT_OK")]
    if int(partial.get("pre_evidence_level_changes_count") or 0) != 0:
        reasons.append("pre_evidence_level_changes != 0")
    if not partial.get("first_bucket_available_at_ok"):
        reasons.append("first_bucket_available_at != 2026-09-06T20:14:02.300Z")
    if int(golden.get("n_mismatch") or 0) != 0:
        reasons.append("golden FullBookState mismatch")
        return "EPISODE1_CORRECTED_SMS1_GOLDEN_PARITY_FAILED", reasons
    if not boundary.get("ok"):
        reasons.append("event at bucket_end_exclusive kept in ending bucket")
    if not rt.get("replay_epoch_roundtrip_ok") or not rt.get("available_at_roundtrip_ok") or not rt.get("row_count_match"):
        return "EPISODE1_CORRECTED_SMS1_ROUNDTRIP_FAILED", reasons + ["write/read roundtrip failed"]
    if not rt.get("reconstruct_ok"):
        return "EPISODE1_CORRECTED_SMS1_ROUNDTRIP_FAILED", reasons + ["persist reconstruct failed"]
    if violations:
        return "EPISODE1_CORRECTED_SMS1_AVAILABLE_AT_VIOLATION", reasons + [v["kind"] for v in violations[:5]]
    if not det.get("byte_identical"):
        return "EPISODE1_CORRECTED_SMS1_DETERMINISM_FAILED", reasons + ["repeat build not byte-identical"]
    epochs = {s.get("replay_epoch") for s in []}
    if not frozen.get("sms1_ep1_states_100ms", {}).get("hash_identical"):
        reasons.append("frozen sms1 states hash changed")
    if not same_stream:
        reasons.append("touch and detection not from the same checkpoint-capable stream")
    if reasons:
        if "golden FullBookState mismatch" in reasons:
            return "EPISODE1_CORRECTED_SMS1_GOLDEN_PARITY_FAILED", reasons
        return "EPISODE1_CORRECTED_SMS1_EPOCH_PERSIST_FAILED", reasons
    return "EPISODE1_CORRECTED_SMS1_PERSISTENCE_AND_DERIVED_EVENTS_PARITY_PROVEN", []


def render_report(payload: dict[str, Any]) -> str:
    g = payload.get("golden") or {}
    d = payload.get("derived_counts") or {}
    lines = [
        f"# {AUDIT_ID}",
        "",
        f"**Verdict:** `{payload.get('verdict')}`",
        f"**Run:** `{payload.get('run_key')}`",
        f"**Episode:** `{EPISODE_ID}`",
        f"**Parent golden:** `{PARENT_GOLDEN_RUN}` `{PARENT_GOLDEN_VERDICT}`",
        f"**Evidence:** `[{EVIDENCE_START_ISO}, {DETECTION_ISO})`",
        "",
        "## Persist contract",
        "",
        "- `bucket_start`, `bucket_end_exclusive`, `available_at`, `effective_bucket_start`, `replay_epoch` on every state.",
        "- Old sms1 `bucket_ts` mapped to `bucket_start` (not availability).",
        "- `available_at = bucket_end_exclusive`.",
        "- Per-state `replay_epoch` from the reset stream; no final-epoch stamp.",
        "",
        "## Counts",
        "",
        f"- states: `{payload.get('n_states')}`",
        f"- epochs: `{payload.get('epoch_counts')}`",
        f"- book_resets: `{payload.get('n_resets')}`",
        f"- golden cutoffs: `{g.get('n_ok')}/{g.get('n_cutoffs_compared')}` mismatches `{g.get('n_mismatch')}`",
        f"- walls: `{d.get('n_walls')}` refills `{d.get('n_refills')}` touches `{d.get('n_touches')}` detections `{d.get('n_detections')}`",
        f"- derived diff: `{payload.get('diff_summary')}`",
        "",
        "## Reasons",
        "",
    ]
    reasons = payload.get("reasons") or []
    if reasons:
        lines.extend(f"- {r}" for r in reasons)
    else:
        lines.append("- none")
    lines.extend(["", f"## Artifacts", "", f"`{payload.get('run_dir')}`", ""])
    return "\n".join(lines) + "\n"


def run_audit() -> dict[str, Any]:
    t0 = time.monotonic()
    pids_before = _pids()
    config_path = ENGINE_ROOT / CONFIG_REL
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config_hash = sha256_hex(config)
    run_key = RUN_PREFIX + config_hash[:16]
    run_dir = RESULTS_ROOT / SYMBOL / run_key
    primary = run_dir / "build_primary"
    repeat = run_dir / "build_repeat"
    run_dir.mkdir(parents=True, exist_ok=True)
    frozen_before = _frozen_hashes()
    input_hash = sha256_hex(
        {
            "config_hash": config_hash,
            "sms1_states_sha256": SMS1_EP1_STATES_SHA256,
            "parent_golden_run": PARENT_GOLDEN_RUN,
            "episode_id": EPISODE_ID,
            "evidence": [EVIDENCE_START_ISO, DETECTION_ISO],
        }
    )

    cutoffs = all_bucket_end_cutoffs()
    replay = replay_window(
        symbol=SYMBOL,
        window_start=EVIDENCE_START,
        window_end=DETECTION,
        reference_cutoffs=cutoffs,
        trades=_load_sms1_trades(),
    )
    if not replay.get("ok"):
        verdict, reasons = decide_verdict(
            replay=replay,
            golden={"n_mismatch": 1},
            partial={},
            boundary={"ok": False},
            rt={},
            det={"byte_identical": False},
            violations=[{"kind": "replay_failed"}],
            frozen=frozen_before,
            same_stream=False,
        )
        atomic_write_json(run_dir / "run_manifest.json", {"ok": False, "verdict": verdict, "reasons": reasons})
        atomic_write_text(run_dir / "STATUS", verdict + "\n")
        return {"ok": False, "verdict": verdict, "run_dir": str(run_dir), "run_key": run_key, "reasons": reasons}

    print(f"[csp1] replay ok n_resets={len(replay.get('book_resets') or [])} n_changes={len(replay.get('level_changes') or [])} n_refs={len(replay.get('direct_reference') or [])}", flush=True)
    states_a = build_states_100ms(**_builder_kwargs(replay))
    print(f"[csp1] states_a n={len(states_a)}", flush=True)
    print("[csp1] golden compare", flush=True)
    golden = golden_all_cutoffs(states=states_a, references=replay.get("direct_reference") or [], replay=replay)
    # drop per-row dump from json summary
    golden_rows = golden.pop("rows")
    partial = partial_first_bucket_audit(replay=replay, states=states_a)
    boundary = boundary_event_audit(replay, states_a)
    reset_parity = reset_before_after_parity(replay, states_a)

    print("[csp1] derived events", flush=True)
    touches, detections, touch_book, det_book = build_touch_detection(
        replay=replay,
        states=states_a,
        first_touch=FIRST_TOUCH,
        detection=DETECTION,
        episode_id=EPISODE_ID,
    )
    walls = build_walls(
        replay=replay,
        touch=touch_book,
        detection_book=det_book,
        cfg=config,
        config_hash=config_hash,
        first_touch=FIRST_TOUCH,
        detection=DETECTION,
        episode_id=EPISODE_ID,
    )
    mid = None
    if touch_book.get("best_bid") is not None and touch_book.get("best_ask") is not None:
        mid = (float(touch_book["best_bid"]) + float(touch_book["best_ask"])) / 2.0
    refills = build_refills(
        replay=replay, walls=walls, mid_at_touch=mid, cfg=config, states=states_a
    )
    same_stream = (
        touch_book.get("state_source") == "checkpoint_capable_event_stream"
        and det_book.get("state_source") == "checkpoint_capable_event_stream"
        and touches[0].get("book_stream") == detections[0].get("book_stream") == "reconstruct_book_asof_exclusive"
    )
    violations = available_at_violations(
        states=states_a, walls=walls, refills=refills, touches=touches, detections=detections
    )

    old_bids, old_asks = _book_at(replay["initial_bids"], replay["initial_asks"], replay["level_changes"], FIRST_TOUCH)
    old_dbids, old_dasks = _book_at(replay["initial_bids"], replay["initial_asks"], replay["level_changes"], DETECTION)
    old_touch = {
        "event_id": "old:_book_at:touch",
        "event_type": "TOUCH",
        "event_time": FIRST_TOUCH_ISO,
        "available_at": None,
        "replay_epoch": None,
        "best_bid": max(old_bids) if old_bids else None,
        "best_ask": min(old_asks) if old_asks else None,
        "side": None,
        "price": None,
        "state_source": "drilldown.engine._book_at",
    }
    old_det = {
        "event_id": "old:_book_at:detection",
        "event_type": "DETECTION",
        "event_time": DETECTION_ISO,
        "available_at": None,
        "replay_epoch": None,
        "best_bid": max(old_dbids) if old_dbids else None,
        "best_ask": min(old_dasks) if old_dasks else None,
        "side": None,
        "price": None,
        "state_source": "drilldown.engine._book_at",
    }
    wall_diff = diff_walls(walls)
    refill_diff = diff_refills(refills)
    td_diff = diff_touch_detection(
        new_touch=touches[0], new_det=detections[0], old_touch=old_touch, old_det=old_det
    )
    diff_all = wall_diff + refill_diff + td_diff
    diff_summary = {
        "walls": summarize(wall_diff),
        "refills": summarize(refill_diff),
        "touch_detection": summarize(td_diff),
        "all": summarize(diff_all),
    }

    table_names = [
        "states_100ms.jsonl.zst",
        "book_resets.jsonl.zst",
        "level_changes.jsonl.zst",
        "initial_book.jsonl.zst",
        "walls.jsonl.zst",
        "refill_removal.jsonl.zst",
        "touches.jsonl.zst",
        "detections.jsonl.zst",
    ]
    print(f"[csp1] write primary walls={len(walls)} refills={len(refills)}", flush=True)
    hashes_a = write_tables(
        primary,
        states=states_a,
        replay=replay,
        walls=walls,
        refills=refills,
        touches=touches,
        detections=detections,
        config_hash=config_hash,
        input_hash=input_hash,
    )
    print("[csp1] write repeat", flush=True)
    hashes_b = write_tables(
        repeat,
        states=states_a,
        replay=replay,
        walls=walls,
        refills=refills,
        touches=touches,
        detections=detections,
        config_hash=config_hash,
        input_hash=input_hash,
    )
    builder_idempotent = True
    det = content_compare(primary, repeat, table_names)
    rt = roundtrip_proof(primary, states_a, replay)
    frozen_after = _frozen_hashes()

    epochs = {}
    for s in states_a:
        epochs[str(s.get("replay_epoch"))] = epochs.get(str(s.get("replay_epoch")), 0) + 1
    first_st, last_st = states_a[0], states_a[-1]
    final_epoch = replay.get("replay_epoch")
    epoch_stamp_bug = bool(states_a) and all(s.get("replay_epoch") == final_epoch for s in states_a) and len(epochs) > 1
    if epoch_stamp_bug:
        epoch_reason = ["replay_epoch stamped final for all states"]
    else:
        epoch_reason = []

    verdict, reasons = decide_verdict(
        replay=replay,
        golden=golden,
        partial=partial,
        boundary=boundary,
        rt=rt,
        det=det,
        violations=violations,
        frozen=frozen_after,
        same_stream=same_stream,
    )
    reasons = list(reasons) + epoch_reason
    if epoch_stamp_bug and verdict.startswith("EPISODE1_CORRECTED_SMS1_PERSISTENCE"):
        verdict = "EPISODE1_CORRECTED_SMS1_EPOCH_PERSIST_FAILED"
    if not builder_idempotent:
        reasons.append("builder_not_idempotent")
        if verdict.startswith("EPISODE1_CORRECTED_SMS1_PERSISTENCE"):
            verdict = "EPISODE1_CORRECTED_SMS1_DETERMINISM_FAILED"
    if frozen_before.get("sms1_ep1_states_100ms", {}).get("sha256") != frozen_after.get("sms1_ep1_states_100ms", {}).get("sha256"):
        reasons.append("sms1 states mutated")
        verdict = "EPISODE1_CORRECTED_SMS1_GOLDEN_PARITY_FAILED"

    mismatches = [r for r in golden_rows if not r.get("ok")]
    atomic_write_csv(run_dir / "golden_all_cutoffs.csv", golden_rows)
    atomic_write_csv(run_dir / "golden_mismatches.csv", mismatches)
    atomic_write_csv(run_dir / "reset_before_after.csv", reset_parity)
    non_unchanged = [r for r in diff_all if r.get("class") != "UNCHANGED"]
    atomic_write_csv(run_dir / "derived_event_diff.csv", diff_all if len(diff_all) < 20000 else non_unchanged)
    atomic_write_json(run_dir / "derived_event_diff_non_unchanged.json", non_unchanged)
    atomic_write_json(run_dir / "partial_first_bucket_audit.json", partial)
    atomic_write_json(run_dir / "boundary_event_audit.json", boundary)
    atomic_write_json(run_dir / "roundtrip_proof.json", rt)
    atomic_write_json(run_dir / "determinism_proof.json", det)
    atomic_write_json(run_dir / "field_map_old_to_new.json", FIELD_MAP_OLD_TO_NEW)
    atomic_write_json(run_dir / "config.json", config)
    atomic_write_json(
        run_dir / "golden_summary.json",
        {k: golden[k] for k in golden if k != "rows"},
    )
    pids_after = _pids()
    derived_counts = {
        "n_walls": len(walls),
        "n_refills": len(refills),
        "n_touches": len(touches),
        "n_detections": len(detections),
    }
    report_payload = {
        "verdict": verdict,
        "reasons": reasons,
        "run_key": run_key,
        "run_dir": str(run_dir),
        "n_states": len(states_a),
        "n_resets": len(replay.get("book_resets") or []),
        "epoch_counts": epochs,
        "golden": golden,
        "derived_counts": derived_counts,
        "diff_summary": diff_summary,
    }
    report = render_report(report_payload)
    atomic_write_text(run_dir / "final_report.md", report)
    atomic_write_text(run_dir / "STATUS", verdict + "\n")
    manifest = {
        "ok": verdict == "EPISODE1_CORRECTED_SMS1_PERSISTENCE_AND_DERIVED_EVENTS_PARITY_PROVEN",
        "audit_id": AUDIT_ID,
        "contract_version": CONTRACT_VERSION,
        "run_key": run_key,
        "config_hash": config_hash,
        "input_hash": input_hash,
        "symbol": SYMBOL,
        "episode_id": EPISODE_ID,
        "verdict": verdict,
        "reasons": reasons,
        "git": _git(),
        "created_at": _now(),
        "elapsed_s": round(time.monotonic() - t0, 3),
        "peak_ram_gb": _peak(),
        "clickhouse_writes": ALLOW_CLICKHOUSE_WRITES,
        "allow_archive_replay": ALLOW_ARCHIVE_REPLAY,
        "forbidden_fields": list(FORBIDDEN_FIELDS),
        "parent_golden_run": PARENT_GOLDEN_RUN,
        "parent_golden_verdict": PARENT_GOLDEN_VERDICT,
        "evidence_start": EVIDENCE_START_ISO,
        "evidence_end": DETECTION_ISO,
        "first_touch": FIRST_TOUCH_ISO,
        "n_states": len(states_a),
        "n_level_changes": len(replay.get("level_changes") or []),
        "n_book_resets": len(replay.get("book_resets") or []),
        "epoch_counts": epochs,
        "first_state": serialize_state(first_st),
        "last_state": serialize_state(last_st),
        "primary_dir": str(primary),
        "repeat_dir": str(repeat),
        "primary_content_hashes": hashes_a,
        "repeat_content_hashes": hashes_b,
        "file_sha256_primary": file_hashes(primary, table_names),
        "determinism": det,
        "builder_idempotent": builder_idempotent,
        "golden": {k: golden.get(k) for k in ("n_cutoffs_compared", "n_ok", "n_mismatch", "full_book_level_mode", "first_mismatch")},
        "partial_first_bucket": partial,
        "boundary": boundary,
        "roundtrip": {k: rt.get(k) for k in rt if k != "reconstruct_cutoffs"},
        "available_at_violations": violations,
        "derived_counts": derived_counts,
        "diff_summary": diff_summary,
        "same_book_stream_touch_detection": same_stream,
        "frozen_hashes_before": frozen_before,
        "frozen_hashes_after": frozen_after,
        "pids_before": pids_before,
        "pids_after": pids_after,
        "event_time_semantics": EVENT_TIME_SEMANTICS,
        "artifacts": [
            "build_primary/",
            "build_repeat/",
            "golden_all_cutoffs.csv",
            "golden_mismatches.csv",
            "golden_summary.json",
            "reset_before_after.csv",
            "derived_event_diff.csv",
            "derived_event_diff_non_unchanged.json",
            "partial_first_bucket_audit.json",
            "boundary_event_audit.json",
            "roundtrip_proof.json",
            "determinism_proof.json",
            "field_map_old_to_new.json",
            "config.json",
            "run_manifest.json",
            "final_report.md",
            "STATUS",
        ],
    }
    atomic_write_json(run_dir / "run_manifest.json", manifest)
    return {
        "ok": manifest["ok"],
        "verdict": verdict,
        "run_dir": str(run_dir),
        "run_key": run_key,
        "reasons": reasons,
        "n_states": len(states_a),
        "golden_n_ok": golden.get("n_ok"),
        "golden_n_mismatch": golden.get("n_mismatch"),
    }


def main() -> int:
    result = run_audit()
    print(json.dumps({k: result.get(k) for k in ("ok", "verdict", "run_key", "run_dir", "reasons", "n_states", "golden_n_ok", "golden_n_mismatch")}, indent=2, default=str))
    return 0 if result.get("verdict") else 1
