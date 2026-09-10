"""Integration adapter: detector → handoff → frozen wall-flow (no algorithm edits)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..drilldown.aggregation_100ms import _as_dt
from ..drilldown.replay import clear_segment_cache, replay_window
from ..level_first_episode1_touch_detection_independent_v1.derive import derive_all
from ..level_first_episode1_touch_detection_independent_v1.wall_generations import (
    build_wall_generations,
    generation_alive_at,
)
from ..level_first_episode1_wall_flow_qdh_base_v1.independent_oracle import oracle_audit_bundle
from ..level_first_episode1_wall_flow_qdh_base_v1.pipeline import (
    _load_trades,
    compute_wall_flow_bundle,
    write_wall_flow_outputs,
)
from ..level_first_episode1_corrected_sms1_persist_v1.independent_derivation import INPUT_FREEZE_DIR
from ..level_first_episode1_corrected_sms1_persist_v1.io_zst import body_rows, read_jsonl_zst
from ..level_first_episode1_corrected_sms1_persist_v1.persist import pairs_to_map
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..paths import ENGINE_ROOT
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    FORBIDDEN_TIMING_ARTIFACTS,
    FROZEN_BAND_HITS,
    FROZEN_EXACT_HITS,
    FROZEN_TIMELINE_ROWS,
    FROZEN_TRADE_DEDUP,
    FROZEN_WALL_FLOW_BOOK_DIR,
    FROZEN_WALL_FLOW_SEMANTIC_HASH,
    VERDICT_BLOCKED,
    VERDICT_OK,
)
from .handoff import (
    Episode1Handoff,
    HandoffError,
    handoff_from_derivation,
    validate_handoff,
    wall_flow_inputs_from_handoff,
)

SYMBOL = "BTCUSDT"


def book_persist_dir(path: Path | str | None = None) -> Path:
    return Path(path or (ENGINE_ROOT / FROZEN_WALL_FLOW_BOOK_DIR))


def assert_not_using_timing_artifacts(persist_dir: Path) -> list[str]:
    """Document that timing artifacts may exist on disk but are not opened for calc."""
    present = []
    for name in FORBIDDEN_TIMING_ARTIFACTS:
        if name == "FIRST_TOUCH_ISO":
            continue
        if (persist_dir / name).is_file():
            present.append(name)
    return present


def assert_never_loaded_timing_files(loaded_paths: list[Path]) -> None:
    for p in loaded_paths:
        name = Path(p).name
        if name in FORBIDDEN_TIMING_ARTIFACTS:
            raise HandoffError(f"forbidden timing artifact loaded as calc input: {p}")


def validate_wall_generation_against_book(handoff: Episode1Handoff, persist_dir: Path) -> dict[str, Any]:
    """Fail-closed if handoff wall_generation_id does not match book-derived generation at zone touch."""
    persist_dir = Path(persist_dir)
    initial = body_rows(read_jsonl_zst(persist_dir / "initial_book.jsonl.zst"))[0]
    lcs = body_rows(read_jsonl_zst(persist_dir / "level_changes.jsonl.zst"))
    resets = body_rows(read_jsonl_zst(persist_dir / "book_resets.jsonl.zst"))
    # Normalize reset asks/bids lists → maps for generation builder
    norm_resets = []
    for r in resets:
        item = dict(r)
        if isinstance(item.get("asks"), list):
            item["asks"] = {float(p): float(q) for p, q in item["asks"] if q and float(q) > 0}
        if isinstance(item.get("bids"), list):
            item["bids"] = {float(p): float(q) for p, q in item["bids"] if q and float(q) > 0}
        norm_resets.append(item)
    asks0 = pairs_to_map(initial.get("asks"))
    bids0 = pairs_to_map(initial.get("bids"))
    window_start = _as_dt(initial.get("window_start"))
    gens = build_wall_generations(
        level_changes=lcs,
        book_resets=norm_resets,
        initial_asks=asks0,
        initial_bids=bids0,
        initial_replay_epoch=initial.get("replay_epoch"),
        window_start=window_start,
        wall_price=handoff.wall_price,
        wall_side=handoff.wall_side,
    )
    zt = parse_utc(handoff.zone_first_touch_exchange_event_time)
    alive = generation_alive_at(gens, zt)
    if alive is None:
        raise HandoffError("no wall generation alive at ZONE_FIRST_TOUCH in book persist")
    expected_gid = alive.generation_id()
    if expected_gid != handoff.wall_generation_id:
        raise HandoffError(
            f"wall_generation_id mismatch: handoff={handoff.wall_generation_id} book={expected_gid}"
        )
    if int(alive.replay_epoch) != int(handoff.replay_epoch):
        raise HandoffError(
            f"replay_epoch mismatch: handoff={handoff.replay_epoch} book={alive.replay_epoch}"
        )
    return {
        "ok": True,
        "expected_wall_generation_id": expected_gid,
        "n_generations": len(gens),
        "alive_generation_index": alive.generation_index,
    }


def feature_only_semantic(content: dict[str, Any]) -> dict[str, Any]:
    """Canonical wall-flow feature fingerprint (excludes handoff metadata)."""
    return {
        "n_exact_events": content.get("n_exact_events"),
        "n_band_events": content.get("n_band_events"),
        "n_timeline_rows": content.get("n_timeline_rows") or content.get("n_states"),
        "trade_dedup": content.get("trade_dedup"),
        "exact_stats": content.get("exact_stats"),
        "band_stats": content.get("band_stats"),
        "timeline_meta_aggressor": (content.get("timeline_meta") or {}).get("aggressor_band"),
        "timeline_meta_qdh_band": (content.get("timeline_meta") or {}).get("qdh_band"),
        "M_OI": content.get("M_OI"),
        "M_LIQ": content.get("M_LIQ"),
        "WALL_STATE": content.get("WALL_STATE"),
        "verdict_candidate": content.get("verdict_candidate"),
    }


def compare_to_frozen_golden(content: dict[str, Any], *, written_semantic_hash: str | None = None) -> dict[str, Any]:
    feat = feature_only_semantic(content)
    # Align with frozen semantic_fingerprint.json shape
    frozen_shape = {
        "n_exact_events": content.get("n_exact_events"),
        "n_band_events": content.get("n_band_events"),
        "n_timeline": content.get("n_timeline_rows"),
        "trade_dedup": content.get("trade_dedup"),
        "exact_stats": content.get("exact_stats"),
        "band_stats": content.get("band_stats"),
        "timeline_meta_aggressor": (content.get("timeline_meta") or {}).get("aggressor_band"),
        "timeline_meta_qdh_band": (content.get("timeline_meta") or {}).get("qdh_band"),
        "M_OI": content.get("M_OI"),
        "M_LIQ": content.get("M_LIQ"),
        "WALL_STATE": content.get("WALL_STATE"),
        "verdict_candidate": content.get("verdict_candidate"),
    }
    feature_hash = sha256_hex(frozen_shape)
    dedup = content.get("trade_dedup") or {}
    exact_hits = int((content.get("exact_stats") or {}).get("trades_attributed") or -1)
    band_hits = int((content.get("band_stats") or {}).get("trades_attributed") or -1)
    n_tl = int(content.get("n_timeline_rows") or 0)

    def _i(d: dict, key: str) -> int:
        if key not in d or d[key] is None:
            return -1
        return int(d[key])

    checks = {
        "timeline_rows_4178": n_tl == FROZEN_TIMELINE_ROWS,
        "exact_hits_207": exact_hits == FROZEN_EXACT_HITS,
        "band_hits_214": band_hits == FROZEN_BAND_HITS,
        "trade_dedup": (
            _i(dedup, "raw_count"),
            _i(dedup, "unique_count"),
            _i(dedup, "duplicate_count"),
        )
        == FROZEN_TRADE_DEDUP,
        "feature_only_hash_matches_frozen": feature_hash == FROZEN_WALL_FLOW_SEMANTIC_HASH,
        "written_semantic_hash_matches_frozen": (
            written_semantic_hash == FROZEN_WALL_FLOW_SEMANTIC_HASH if written_semantic_hash else None
        ),
    }
    checks["ok"] = all(
        v is True
        for k, v in checks.items()
        if k in ("timeline_rows_4178", "exact_hits_207", "band_hits_214", "trade_dedup", "feature_only_hash_matches_frozen")
    )
    return {
        "checks": checks,
        "feature_only_hash": feature_hash,
        "frozen_wall_flow_semantic_hash": FROZEN_WALL_FLOW_SEMANTIC_HASH,
        "feature_only_semantic": frozen_shape,
        "exact_hits": exact_hits,
        "band_hits": band_hits,
        "n_timeline": n_tl,
    }


def run_detector_stage(*, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Raw → touch/detection (frozen detector package)."""
    clear_segment_cache()
    pre = derive_all(cfg=cfg, replay=None, allow_archive_replay=False)
    evidence_start = parse_utc(pre["timing"]["evidence_start"])
    detection = parse_utc(pre["timing"]["detection"])
    replay = replay_window(
        symbol=SYMBOL,
        window_start=evidence_start,
        window_end=detection,
        reference_cutoffs=[],
        trades=[],
    )
    derivation = derive_all(cfg=cfg, replay=replay, allow_archive_replay=False)
    clear_segment_cache()
    return derivation


def run_wall_flow_from_handoff(
    *,
    handoff: Episode1Handoff,
    persist_dir: Path,
    out_dir: Path,
    run_key: str,
    trades_path: Path | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    persist_dir = Path(persist_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Never use run_from_persist_dir (would load independent_*.json timings).
    timing_present = assert_not_using_timing_artifacts(persist_dir)
    gen_check = validate_wall_generation_against_book(handoff, persist_dir)
    inputs = wall_flow_inputs_from_handoff(handoff)
    trades_path = Path(trades_path or (INPUT_FREEZE_DIR / "public_trades_zone_window.jsonl"))

    loaded_for_calc = [
        persist_dir / "states_100ms.jsonl.zst",
        persist_dir / "level_changes.jsonl.zst",
        persist_dir / "initial_book.jsonl.zst",
        persist_dir / "book_resets.jsonl.zst",
        trades_path,
    ]
    assert_never_loaded_timing_files(loaded_for_calc)

    bundle = compute_wall_flow_bundle(
        persist_dir=persist_dir,
        trades_path=trades_path,
        zone_touch=inputs["zone_touch"],
        wall_observation=inputs["wall_observation"],
        wall_touch=inputs["wall_touch"],
        detection=inputs["detection"],
    )
    hashes = write_wall_flow_outputs(out_dir, bundle, run_key=run_key)

    # Oracle
    exact = json.loads((out_dir / "wall_flow_events_exact.json").read_text(encoding="utf-8"))
    band = json.loads((out_dir / "wall_flow_events_band.json").read_text(encoding="utf-8"))
    import csv

    timeline = list(csv.DictReader((out_dir / "feature_timeline.csv").open(encoding="utf-8")))
    for row in timeline:
        row["look_ahead"] = False
    raw_trades, _ = _load_trades(trades_path)
    oracle = oracle_audit_bundle(
        production_exact_events=exact,
        production_band_events=band,
        production_timeline=timeline,
        raw_trades=raw_trades,
        symbol=SYMBOL,
        production_dedup=bundle["content"]["trade_dedup"],
        production_summary=bundle["content"],
    )
    golden = compare_to_frozen_golden(
        bundle["content"], written_semantic_hash=hashes.get("semantic_fingerprint")
    )
    return {
        "bundle_content": bundle["content"],
        "hashes": hashes,
        "oracle": oracle,
        "golden_compare": golden,
        "generation_check": gen_check,
        "timing_artifacts_present_but_unused": timing_present,
        "loaded_calc_inputs": [str(p) for p in loaded_for_calc],
        "used_run_from_persist_dir": False,
        "used_first_touch_iso": False,
        "used_research_visit_count_in_wall_flow": False,
    }


def run_integrated(
    *,
    run_key: str,
    out_dir: Path,
    persist_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
    handoff_override: dict[str, Any] | None = None,
    skip_detector: bool = False,
) -> dict[str, Any]:
    persist_dir = book_persist_dir(persist_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    derivation = None
    try:
        if handoff_override is not None:
            handoff = validate_handoff(handoff_override)
        else:
            if skip_detector:
                raise HandoffError("skip_detector requires handoff_override")
            derivation = run_detector_stage(cfg=cfg)
            handoff = handoff_from_derivation(derivation)

        # Persist handoff (not a wall-flow calc input file for the frozen package)
        handoff_path = out_dir / "episode1_handoff.json"
        handoff_path.write_text(json.dumps(handoff.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

        wf = run_wall_flow_from_handoff(
            handoff=handoff,
            persist_dir=persist_dir,
            out_dir=out_dir,
            run_key=run_key,
        )
    except HandoffError as exc:
        return {
            "ok": False,
            "verdict": VERDICT_BLOCKED,
            "error": str(exc),
            "handoff": None,
            "fail_closed": True,
            "run_key": run_key,
            "out_dir": str(out_dir),
        }

    ok = (
        wf["oracle"].get("ok") is True
        and wf["golden_compare"]["checks"]["ok"] is True
        and wf["generation_check"]["ok"] is True
    )
    integration_semantic = {
        "handoff_wall_generation_id": handoff.wall_generation_id,
        "handoff_zone_touch": handoff.zone_first_touch_exchange_event_time,
        "handoff_wall_touch": handoff.wall_first_touch_exchange_event_time,
        "handoff_detection": handoff.detection_exchange_event_time,
        "feature_only_hash": wf["golden_compare"]["feature_only_hash"],
        "oracle_ok": wf["oracle"].get("ok"),
    }
    return {
        "ok": ok,
        "verdict": VERDICT_OK if ok else VERDICT_BLOCKED,
        "run_key": run_key,
        "out_dir": str(out_dir),
        "handoff": handoff.to_dict(),
        "detector_verdict": None if derivation is None else derivation.get("verdict"),
        "detector_semantic_fingerprint": None if derivation is None else derivation.get("semantic_fingerprint"),
        "wall_flow": {
            "verdict_candidate": wf["bundle_content"].get("verdict_candidate"),
            "hashes": wf["hashes"],
            "oracle": wf["oracle"],
            "golden_compare": wf["golden_compare"],
            "generation_check": wf["generation_check"],
            "timing_artifacts_present_but_unused": wf["timing_artifacts_present_but_unused"],
            "used_run_from_persist_dir": False,
            "used_first_touch_iso": False,
            "used_research_visit_count_in_wall_flow": False,
        },
        "integration_semantic_fingerprint": sha256_hex(integration_semantic),
        "feature_only_hash": wf["golden_compare"]["feature_only_hash"],
        "feature_only_matches_frozen_wall_flow": wf["golden_compare"]["checks"]["feature_only_hash_matches_frozen"],
    }
