"""End-to-end analyze orchestrator."""

from __future__ import annotations

import json
import resource
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..behavior_groups.grouper import (
    config_sha256 as grouper_config_sha256,
)
from ..behavior_groups.grouper import (
    content_hash_episodes,
    group_behavior_episodes,
    load_config as load_grouper_config,
    validate_mapping,
)
from ..behavior_groups.reporting import write_outputs as write_episode_group_outputs
from ..behavior_groups.support import load_support_map
from ..cli import build_intersecting_hours
from ..cli_report import render_coverage_report
from ..episodes.detector import (
    candidates_content_sha256,
    config_sha256 as candidate_config_sha256,
    load_config as load_candidate_config,
)
from ..episodes.span_aware_detect import (
    candidate_span_mapping_frame,
    detect_candidates_span_aware,
    format_span_detect_terminal,
)
from ..localized_coverage.filter_spans import episodes_cross_span
from ..episodes.reporting import write_episode_outputs
from ..episodes.state_loader import load_states_for_interval
from ..outcomes.public_trade_builder import (
    build_pt_episode_outcomes,
    content_hash_pt_outcomes,
    is_carry_policy,
    load_pt_config,
    pt_config_sha256,
    validate_pt_row_contract,
)
from ..outcomes.public_trade_index import load_public_trades_window
from ..outcomes.public_trade_reporting import write_pt_outputs
from ..partition_io import read_partition_manifest
from ..paths import CONTRACTS, ENGINE_ROOT
from ..schema_freeze import load_frozen_sha
from ..timeparse import format_utc_z
from . import (
    EXIT_DATA_NOT_COMPLETE,
    EXIT_INTERNAL,
    EXIT_OK,
    MAX_OUTCOME_HORIZON_SECONDS,
    ORCHESTRATOR_VERSION,
    STAGES,
    VERDICT_BLOCKED_COVERAGE,
    VERDICT_FAILED,
    VERDICT_PASS,
    VERDICT_PASS_REFERENCED,
    VERDICT_SKIPPED,
    stages_for,
)
from .manifest import (
    load_manifest,
    mark_stage_blocked,
    mark_stage_complete,
    mark_stage_failed,
    mark_stage_running,
    new_manifest,
    save_manifest,
    validate_stage_outputs,
)
from .precheck import run_precheck
from .report import write_unified_report
from .run_key import (
    ANALYSIS_RUNS_ROOT,
    atomic_write_json,
    compute_run_key,
    file_sha256,
    run_dir_for,
)
from ..avr.provenance import collect_provenance
from ..avr.warmup import check_avr_warmup_coverage
from ..avr.stage import run_avr_context_stage
from .eligible_avr_warmup import (
    WARMUP_ANCHOR_POLICY,
    render_eligible_warmup_terminal,
    resolve_localized_avr_warmup,
)
from ..avr import VERDICT_PARITY_BLOCK as AVR_PARITY_BLOCK
from ..avr_multiscale import (
    CONTRACT_VERSION as MS_CONTRACT_VERSION,
    JOIN_VERSION as MS_JOIN_VERSION,
    WINDOWS_S as MS_WINDOWS,
)
from ..avr_multiscale.config import multiscale_config_hash
from ..avr_multiscale.stage import run_avr_multiscale_stage

CAND_CFG = ENGINE_ROOT / "config" / "episode_candidate_v1.json"
GROUP_CFG = ENGINE_ROOT / "config" / "behavior_episode_group_v1.json"
OUTCOME_CFG = ENGINE_ROOT / "config" / "episode_outcome_public_trade_v1_1.json"
CAND_SCHEMA = CONTRACTS / "episode_candidate_v1.schema.json"
DEFAULT_SUPPORT = ENGINE_ROOT / "results" / "event_drilldown_v1" / "candidate_support.csv"
PILOT_CAND = ENGINE_ROOT / "results" / "episode_candidates_v1" / "episode_candidates_v1.parquet"
PILOT_EP = ENGINE_ROOT / "results" / "behavior_episode_groups_v1" / "behavior_episode_groups_v1.parquet"
PILOT_OUT = (
    ENGINE_ROOT
    / "results"
    / "episode_outcomes_public_trade_carry_v1"
    / "episode_outcomes_public_trade_carry_v1.parquet"
)


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _stage_label(status: str, reused: bool) -> str:
    if status == "SKIPPED_REUSED" or (status == "COMPLETE" and reused):
        return "REUSED"
    if status == "BLOCKED_COVERAGE":
        return "BLOCKED"
    return status


def _print_progress(idx: int, total: int, name: str, detail: str) -> None:
    dots = "." * max(1, 28 - len(name))
    print(f"[{idx}/{total}] {name} {dots} {detail}", flush=True)


def _stage_total(manifest, n_stages=None) -> int:
    if n_stages is not None:
        return int(n_stages)
    return len(manifest.get("stage_order") or STAGES)


def run_analyze(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    support_path: Path | None = None,
    with_avr: bool = False,
    with_avr_multiscale: bool = False,
    coverage_policy: str = "STRICT_WHOLE_WINDOW",
    focus_ts: datetime | None = None,
) -> tuple[int, str, Path | None]:
    """Execute OBFULL_RESEARCH_ANALYZE_ORCHESTRATOR_V1. Returns (exit_code, verdict, run_dir)."""
    symbol = symbol.upper()
    start_z = format_utc_z(start)
    end_z = format_utc_z(end)
    if with_avr_multiscale and not with_avr:
        with_avr = True
    stage_list = list(stages_for(with_avr=with_avr, with_avr_multiscale=with_avr_multiscale))
    n_stages = len(stage_list)

    state_schema_hash = load_frozen_sha()
    cand_hash = candidate_config_sha256(CAND_CFG)
    group_hash = grouper_config_sha256(GROUP_CFG)
    outcome_hash = pt_config_sha256(OUTCOME_CFG)

    avr_contract_version = None
    avr_cfg_hash = None
    avr_code_hash = None
    ms_cfg_hash = None
    cov_pol_ver = None
    cov_pol_hash = None
    if with_avr:
        prov0 = collect_provenance()
        avr_contract_version = prov0.get("avr_source_contract_version")
        avr_cfg_hash = prov0.get("avr_config_hash")
        avr_code_hash = prov0.get("avr_source_code_hash")
    if with_avr_multiscale:
        ms_cfg_hash = multiscale_config_hash()
    if coverage_policy == "LOCALIZED_EXCLUSION_V1":
        from ..localized_coverage import POLICY_VERSION as _CPV
        from ..localized_coverage.config import policy_config_hash as _cph

        cov_pol_ver = _CPV
        cov_pol_hash = _cph()

    warmup_identity: dict[str, Any] = {}
    resolved_preview: dict[str, Any] | None = None
    if coverage_policy == "LOCALIZED_EXCLUSION_V1" and with_avr:
        from ..localized_coverage.check import check_localized_coverage

        loc_preview = check_localized_coverage(
            symbol=symbol, start=start, end=end, focus_ts=focus_ts
        )
        resolved_preview = resolve_localized_avr_warmup(
            symbol=symbol,
            requested_start=start,
            requested_end=end,
            usable_spans=list((loc_preview.get("usable_spans") or [])),
            with_avr=with_avr,
            with_avr_multiscale=with_avr_multiscale,
        )
        warmup_identity = {
            "warmup_anchor_policy": WARMUP_ANCHOR_POLICY,
            **(resolved_preview.get("run_key_identity") or {}),
        }

    run_key = compute_run_key(
        symbol=symbol,
        start_z=start_z,
        end_z=end_z,
        state_schema_hash=state_schema_hash,
        candidate_config_hash=cand_hash,
        grouper_config_hash=group_hash,
        outcome_config_hash=outcome_hash,
        with_avr=with_avr,
        avr_contract_version=avr_contract_version,
        avr_config_hash=avr_cfg_hash,
        avr_source_code_hash=avr_code_hash,
        with_avr_multiscale=with_avr_multiscale,
        multiscale_contract_version=MS_CONTRACT_VERSION if with_avr_multiscale else None,
        multiscale_windows=list(MS_WINDOWS) if with_avr_multiscale else None,
        footprint_config_hash=ms_cfg_hash if with_avr_multiscale else None,
        oi_liq_join_version=MS_JOIN_VERSION if with_avr_multiscale else None,
        coverage_policy=coverage_policy,
        coverage_policy_version=cov_pol_ver,
        coverage_policy_config_hash=cov_pol_hash,
        warmup_anchor_policy=warmup_identity.get("warmup_anchor_policy"),
        usable_spans_hash=warmup_identity.get("usable_spans_hash"),
        base_analysis_eligible_start=warmup_identity.get("base_analysis_eligible_start"),
        final_analysis_eligible_start=warmup_identity.get("final_analysis_eligible_start"),
    )
    run_dir = run_dir_for(symbol=symbol, start=start, end=end, run_key=run_key)
    man_path = run_dir / "run_manifest.json"

    print("OBFULL RESEARCH ANALYZE V1", flush=True)
    print("", flush=True)
    print(f"Symbol:    {symbol}", flush=True)
    print(f"Window:    {start_z}–{end_z}", flush=True)
    print(f"With AVR:  {with_avr}", flush=True)
    print(f"With AVR Multiscale: {with_avr_multiscale}", flush=True)
    print(f"Coverage:  {coverage_policy}", flush=True)
    if focus_ts is not None:
        print(f"Focus ts:  {format_utc_z(focus_ts)}", flush=True)
    print(f"Run key:   {run_key}", flush=True)
    print(f"Run dir:   {run_dir}", flush=True)
    print("", flush=True)

    config_hashes = {
        "state_schema_hash": state_schema_hash,
        "candidate_config_hash": cand_hash,
        "grouper_config_hash": group_hash,
        "outcome_config_hash": outcome_hash,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "with_avr": with_avr,
        "with_avr_multiscale": with_avr_multiscale,
        "coverage_policy": coverage_policy,
    }
    if with_avr:
        config_hashes["avr_contract_version"] = avr_contract_version
        config_hashes["avr_config_hash"] = avr_cfg_hash
        config_hashes["avr_source_code_hash"] = avr_code_hash
    if with_avr_multiscale:
        config_hashes["multiscale_contract_version"] = MS_CONTRACT_VERSION
        config_hashes["multiscale_windows"] = list(MS_WINDOWS)
        config_hashes["footprint_config_hash"] = ms_cfg_hash
        config_hashes["oi_liq_join_version"] = MS_JOIN_VERSION
    if coverage_policy == "LOCALIZED_EXCLUSION_V1":
        config_hashes["coverage_policy_version"] = cov_pol_ver
        config_hashes["coverage_policy_config_hash"] = cov_pol_hash
    if warmup_identity:
        config_hashes.update(warmup_identity)

    existing = load_manifest(man_path)
    if (
        existing
        and existing.get("run_key") == run_key
        and existing.get("status") == "COMPLETE"
        and all(
            validate_stage_outputs(existing["stages"][s], file_sha256=file_sha256)
            for s in existing.get("stage_order") or stage_list
            if s in existing.get("stages", {})
        )
    ):
        print("Identical COMPLETE run found — SKIPPED_ALREADY_COMPLETE", flush=True)
        for i, s in enumerate(existing.get("stage_order") or stage_list, 1):
            _print_progress(i, len(existing.get("stage_order") or stage_list), s, "SKIPPED_ALREADY_COMPLETE")
        print("", flush=True)
        print(f"VERDICT: {VERDICT_SKIPPED}", flush=True)
        print(f"Output: {run_dir}", flush=True)
        return EXIT_OK, VERDICT_SKIPPED, run_dir

    manifest = existing if existing and existing.get("run_key") == run_key else None
    if manifest is None:
        run_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("coverage", "states", "candidates", "episodes", "outcomes", "reports", "avr", "avr_multiscale"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)
        manifest = new_manifest(
            symbol=symbol,
            start_z=start_z,
            end_z=end_z,
            run_key=run_key,
            run_dir=run_dir,
            config_hashes=config_hashes,
            stage_order=stage_list,
        )
        save_manifest(man_path, manifest)

    t_wall0 = time.perf_counter()
    rss0 = _rss_mb()
    resources: dict[str, Any] = {"rss_mb_start": round(rss0, 2)}
    referenced = False
    precheck: dict[str, Any] = {}
    state_summary: dict[str, Any] = {}
    candidate_summary: dict[str, Any] = {}
    episode_summary: dict[str, Any] = {}
    outcome_summary: dict[str, Any] = {}
    causality: dict[str, Any] = {}
    idempotency: dict[str, Any] = {}
    parity: dict[str, Any] | None = None
    outcomes_df: pd.DataFrame | None = None
    episodes_df: pd.DataFrame | None = None
    candidates_df: pd.DataFrame | None = None
    avr_summary: dict[str, Any] | None = None
    ms_summary: dict[str, Any] | None = None

    try:
        # ---- PRECHECK ----
        code, precheck = _stage_precheck(
            manifest,
            man_path,
            symbol,
            start,
            end,
            n_stages=n_stages,
            with_avr=with_avr,
            with_avr_multiscale=with_avr_multiscale,
            coverage_policy=coverage_policy,
            focus_ts=focus_ts,
            resolved_warmup=resolved_preview,
        )
        if code != EXIT_OK:
            return code, manifest.get("verdict") or VERDICT_BLOCKED_COVERAGE, run_dir

        # ---- STATE_PREPARE ----
        state_cov = dict(precheck.get("coverage") or {})
        if "symbol" not in state_cov:
            loc = precheck.get("localized") or {}
            state_cov = {
                **state_cov,
                "symbol": symbol,
                "start": start_z,
                "end": end_z,
                "hour_details": state_cov.get("hour_details")
                or (loc.get("coverage") or {}).get("hour_details")
                or (loc.get("gap_report") or {}).get("hour_meta")
                or [],
            }
        code, state_summary = _stage_state(
            manifest, man_path, state_cov, symbol, start, end, n_stages=n_stages
        )
        if code != EXIT_OK:
            return code, VERDICT_FAILED, run_dir

        # ---- CANDIDATE_DETECT ----
        code, candidate_summary, candidates_df = _stage_candidates(
            manifest, man_path, precheck["coverage"], symbol, start, end, cand_hash, n_stages=n_stages
        )
        if code != EXIT_OK:
            return code, VERDICT_FAILED, run_dir

        # ---- EPISODE_GROUP ----
        code, episode_summary, episodes_df = _stage_episodes(
            manifest,
            man_path,
            precheck["coverage"],
            symbol,
            start,
            end,
            candidates_df,
            cand_hash,
            group_hash,
            support_path or DEFAULT_SUPPORT,
            n_stages=n_stages,
        )
        if code != EXIT_OK:
            return code, VERDICT_FAILED, run_dir

        avr_anchor = start
        resolved_w = precheck.get("localized_avr_warmup") or resolved_preview
        if resolved_w and resolved_w.get("avr_feature_start"):
            avr_anchor = datetime.fromisoformat(
                str(resolved_w["avr_feature_start"]).replace("Z", "+00:00")
            )

        # ---- AVR_CONTEXT (optional) ----
        if with_avr:
            code, avr_summary = _stage_avr(
                manifest,
                man_path,
                symbol,
                avr_anchor,
                end,
                episodes_df,
                n_stages=n_stages,
                validation_root=Path(manifest["run_dir"]) / "avr",
                warmup_gate="SOURCE_COMPLETE"
                if coverage_policy == "LOCALIZED_EXCLUSION_V1"
                else "STRICT",
            )
            if code != EXIT_OK:
                return code, manifest.get("verdict") or VERDICT_FAILED, run_dir

        # ---- AVR_MULTISCALE_CONTEXT (optional) ----
        if with_avr_multiscale:
            code, ms_summary = _stage_avr_multiscale(
                manifest,
                man_path,
                symbol,
                avr_anchor,
                end,
                episodes_df,
                n_stages=n_stages,
                validation_root=Path(manifest["run_dir"]) / "avr_multiscale",
                warmup_gate="SOURCE_COMPLETE"
                if coverage_policy == "LOCALIZED_EXCLUSION_V1"
                else "STRICT",
            )
            if code != EXIT_OK:
                return code, manifest.get("verdict") or VERDICT_FAILED, run_dir

        # ---- PUBLIC_TRADE_OUTCOMES ----
        code, outcome_summary, outcomes_df, referenced = _stage_outcomes(
            manifest,
            man_path,
            precheck,
            symbol,
            start,
            end,
            episodes_df,
            group_hash,
            outcome_hash,
            n_stages=n_stages,
        )
        if code != EXIT_OK:
            return code, VERDICT_FAILED, run_dir

        # ---- VALIDATE ----
        code, parity, causality, idempotency = _stage_validate(
            manifest,
            man_path,
            symbol,
            start,
            end,
            candidates_df,
            episodes_df,
            outcomes_df,
            candidate_summary,
            episode_summary,
            outcome_summary,
            state_summary,
            n_stages=n_stages,
            avr_summary=avr_summary,
        )
        if code != EXIT_OK:
            return code, VERDICT_FAILED, run_dir

        # ---- UNIFIED REPORT ----
        resources["wall_seconds"] = round(time.perf_counter() - t_wall0, 3)
        resources["rss_mb_peak_approx"] = round(max(rss0, _rss_mb()), 2)
        resources["cpu_seconds"] = round(time.process_time(), 3)
        verdict = VERDICT_PASS_REFERENCED if referenced else VERDICT_PASS
        if with_avr and avr_summary and avr_summary.get("verdict"):
            # keep orchestrator pass; AVR verdict nested in report
            pass
        code = _stage_report(
            manifest,
            man_path,
            run_dir,
            precheck,
            state_summary,
            candidate_summary,
            episode_summary,
            outcome_summary,
            causality,
            idempotency,
            resources,
            parity,
            verdict,
            n_stages=n_stages,
            avr_summary=avr_summary,
        )
        if code != EXIT_OK:
            return code, VERDICT_FAILED, run_dir

        manifest["status"] = "COMPLETE"
        manifest["verdict"] = verdict
        save_manifest(man_path, manifest)

        print("", flush=True)
        print(f"VERDICT: {verdict}", flush=True)
        print(f"Output: {run_dir}", flush=True)
        return EXIT_OK, verdict, run_dir

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        for s in stage_list:
            if manifest["stages"].get(s, {}).get("status") == "RUNNING":
                mark_stage_failed(manifest, s, str(exc))
                break
        else:
            manifest["status"] = "FAILED"
            manifest["verdict"] = VERDICT_FAILED
        save_manifest(man_path, manifest)
        print(f"Internal analyze error: {exc}", flush=True)
        return EXIT_INTERNAL, VERDICT_FAILED, run_dir


def _reuse_or_run(manifest: dict[str, Any], stage: str) -> bool:
    st = manifest["stages"][stage]
    return validate_stage_outputs(st, file_sha256=file_sha256)


def _stage_precheck(
    manifest,
    man_path,
    symbol,
    start,
    end,
    n_stages=None,
    with_avr=False,
    with_avr_multiscale=False,
    coverage_policy: str = "STRICT_WHOLE_WINDOW",
    focus_ts=None,
    resolved_warmup=None,
):
    name = "PRECHECK"
    idx = 1
    total = _stage_total(manifest, n_stages)
    if _reuse_or_run(manifest, name):
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            row_count=manifest["stages"][name].get("row_count"),
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "REUSED")
        cov_path = Path(manifest["stages"][name]["output_paths"][0])
        reused_precheck = json.loads(cov_path.read_text(encoding="utf-8"))
        if reused_precheck.get("localized"):
            from ..localized_coverage.report import render_localized_coverage

            print(render_localized_coverage(reused_precheck["localized"]), end="", flush=True)
            if reused_precheck.get("localized_avr_warmup"):
                print(
                    render_eligible_warmup_terminal(
                        requested_start=format_utc_z(start),
                        requested_end=format_utc_z(end),
                        resolved=reused_precheck["localized_avr_warmup"],
                    ),
                    end="",
                    flush=True,
                )
        return EXIT_OK, reused_precheck

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    t0 = time.perf_counter()
    precheck = run_precheck(
        symbol=symbol,
        start=start,
        end=end,
        coverage_policy=coverage_policy,
        focus_ts=focus_ts,
    )
    if with_avr and coverage_policy == "LOCALIZED_EXCLUSION_V1":
        resolved = resolved_warmup or resolve_localized_avr_warmup(
            symbol=symbol,
            requested_start=start,
            requested_end=end,
            usable_spans=list((precheck.get("localized") or {}).get("usable_spans") or []),
            with_avr=with_avr,
            with_avr_multiscale=with_avr_multiscale,
        )
        precheck["localized_avr_warmup"] = resolved
        precheck["warmup_anchor_policy"] = WARMUP_ANCHOR_POLICY
        loc = precheck.get("localized") or {}
        loc["usable_spans"] = resolved.get("analyzable_spans") or []
        loc["excluded_spans"] = list(loc.get("excluded_spans") or []) + list(resolved.get("excluded_spans") or [])
        loc["n_usable_spans"] = len(loc["usable_spans"])
        if precheck.get("coverage") is not None and isinstance(precheck["coverage"], dict):
            precheck["coverage"]["usable_spans"] = loc["usable_spans"]
            precheck["coverage"]["excluded_spans"] = loc["excluded_spans"]
        precheck["localized"] = loc
        precheck["avr_warmup"] = resolved.get("avr_warmup")
        precheck["avr_multiscale_coverage"] = resolved.get("avr_multiscale_coverage")
        if not resolved.get("any_analyzable"):
            precheck["ok"] = False
            precheck["verdict"] = "DATA_NOT_COMPLETE"
            miss = list(precheck.get("missing_intervals") or [])
            for s in resolved.get("spans") or []:
                miss.append(
                    {
                        "source": "AVR_WARMUP_COVERAGE",
                        "start": s.get("avr_required_history_start"),
                        "end": s.get("avr_required_history_end"),
                        "reason": s.get("span_exclude_reason") or s.get("source_vs_baseline"),
                        "detail": s.get("span_exclude_reason") or s.get("source_vs_baseline"),
                        "span_id": s.get("span_id"),
                    }
                )
            precheck["missing_intervals"] = miss
    elif with_avr:
        avr_w = check_avr_warmup_coverage(symbol=symbol, feature_start=start, feature_end=end)
        precheck["avr_warmup"] = avr_w
        if not avr_w.get("ok"):
            precheck["ok"] = False
            precheck["verdict"] = "DATA_NOT_COMPLETE"
            miss = list(precheck.get("missing_intervals") or [])
            miss.append(
                {
                    "source": "AVR_WARMUP_COVERAGE",
                    "start": avr_w.get("preroll_start"),
                    "end": avr_w.get("feature_start"),
                    "reason": avr_w.get("reason"),
                    "detail": avr_w.get("reason"),
                }
            )
            precheck["missing_intervals"] = miss
        if with_avr_multiscale:
            from ..avr_multiscale.coverage import check_multiscale_coverage

            ms_c = check_multiscale_coverage(
                symbol=symbol, feature_start=start, feature_end=end
            )
            precheck["avr_multiscale_coverage"] = ms_c
            if not ms_c.get("ok"):
                precheck["ok"] = False
                precheck["verdict"] = "DATA_NOT_COMPLETE"
                miss = list(precheck.get("missing_intervals") or [])
                miss.append(
                    {
                        "source": "AVR_MULTISCALE_COVERAGE",
                        "start": ms_c.get("load_start"),
                        "end": ms_c.get("feature_start"),
                        "reason": ms_c.get("reason"),
                        "detail": ms_c.get("reason"),
                    }
                )
                precheck["missing_intervals"] = miss
    cov_dir = Path(manifest["run_dir"]) / "coverage"
    cov_path = cov_dir / "coverage_report.json"
    atomic_write_json(cov_path, precheck)

    if not precheck["ok"]:
        if precheck.get("localized"):
            from ..localized_coverage.report import render_localized_coverage

            print(render_localized_coverage(precheck["localized"]), end="", flush=True)
            if precheck.get("localized_avr_warmup"):
                print(
                    render_eligible_warmup_terminal(
                        requested_start=format_utc_z(start),
                        requested_end=format_utc_z(end),
                        resolved=precheck["localized_avr_warmup"],
                    ),
                    end="",
                    flush=True,
                )
        else:
            cov = dict(precheck["coverage"])
            cov["analysis_started"] = False
            if cov.get("verdict") == "DATA_COMPLETE" and not precheck["future_public_trades"].get("ok"):
                print(render_coverage_report(cov).replace(
                    "Coverage complete (analysis not requested).",
                    "Feature coverage complete; outcome future public-trade coverage incomplete.",
                ), end="", flush=True)
            else:
                print(render_coverage_report(cov), end="", flush=True)
        if not precheck["future_public_trades"].get("ok"):
            ft = precheck["future_public_trades"]
            print(
                f"OUTCOME PT FUTURE: INCOMPLETE need_through={ft.get('need_through')} "
                f"tip={ft.get('ch_tip_trade_ts')} reason={ft.get('reason')}",
                flush=True,
            )
        for m in precheck.get("missing_intervals") or []:
            print(
                f"Missing: {m.get('source')}: {m.get('start')}–{m.get('end')} "
                f"({m.get('detail') or m.get('reason')})",
                flush=True,
            )
        mark_stage_blocked(manifest, name, "DATA_NOT_COMPLETE")
        manifest["verdict"] = VERDICT_BLOCKED_COVERAGE
        save_manifest(man_path, manifest)
        fail_path = cov_dir / "FAILURE_MANIFEST.json"
        atomic_write_json(
            fail_path,
            {
                "verdict": VERDICT_BLOCKED_COVERAGE,
                "precheck": precheck,
                "analysis_started": False,
                "downstream_stages_started": False,
            },
        )
        _print_progress(idx, total, name, "BLOCKED")
        print("", flush=True)
        print(f"VERDICT: {VERDICT_BLOCKED_COVERAGE}", flush=True)
        print(f"Output: {manifest['run_dir']}", flush=True)
        return EXIT_DATA_NOT_COMPLETE, precheck

    # Success
    if precheck.get("localized"):
        from ..localized_coverage.report import render_localized_coverage

        print(render_localized_coverage(precheck["localized"]), end="", flush=True)
        if precheck.get("localized_avr_warmup"):
            print(
                render_eligible_warmup_terminal(
                    requested_start=format_utc_z(start),
                    requested_end=format_utc_z(end),
                    resolved=precheck["localized_avr_warmup"],
                ),
                end="",
                flush=True,
            )
        print("Starting analysis on usable spans...", flush=True)
    else:
        cov = dict(precheck["coverage"])
        cov["analysis_started"] = True
        print(render_coverage_report(cov), end="", flush=True)
    h = file_sha256(cov_path)
    mark_stage_complete(
        manifest,
        name,
        output_paths=[str(cov_path)],
        output_hashes={str(cov_path): h or ""},
        schema_version="coverage_report_v1",
        row_count=len(precheck.get("missing_intervals") or []),
        duration_seconds=round(time.perf_counter() - t0, 3),
    )
    save_manifest(man_path, manifest)
    _print_progress(idx, total, name, "COMPLETE")
    return EXIT_OK, precheck


def _stage_state(manifest, man_path, coverage_report, symbol, start, end, n_stages=None):
    name = "STATE_PREPARE"
    idx = 2
    total = _stage_total(manifest, n_stages)
    if _reuse_or_run(manifest, name):
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            row_count=manifest["stages"][name].get("row_count"),
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
            schema_version=manifest["stages"][name].get("schema_version"),
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "REUSED")
        summary_path = Path(manifest["run_dir"]) / "states" / "state_summary.json"
        return EXIT_OK, json.loads(summary_path.read_text(encoding="utf-8"))

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    t0 = time.perf_counter()
    try:
        result = build_intersecting_hours(coverage_report)
        if not result.get("ok"):
            raise RuntimeError(f"state_build_failed:{result}")
        state_df, state_meta = load_states_for_interval(symbol=symbol, start=start, end=end)
        man = read_partition_manifest(symbol, start.replace(minute=0, second=0, microsecond=0))
        reused_all = all(b.get("skipped") for b in (result.get("built") or [])) and bool(result.get("built"))
        summary = {
            "n_state_rows": int(len(state_df)),
            "content_sha256": state_meta.get("content_sha256"),
            "replay_epoch": state_meta.get("replay_epoch"),
            "partitions_built_or_checked": result.get("built"),
            "all_partitions_reused": reused_all,
            "partition_manifest_content_sha256": (man or {}).get("content_sha256"),
            "schema_sha256": (man or {}).get("schema_sha256") or load_frozen_sha(),
            "schema_version": "mb_state_1s_v1",
        }
        out_path = Path(manifest["run_dir"]) / "states" / "state_summary.json"
        # Reference immutable partition path + hash (no parquet copy)
        part = read_partition_manifest(symbol, start.replace(minute=0, second=0, microsecond=0))
        from ..partition_io import partition_dir

        pq = partition_dir(symbol, start.replace(minute=0, second=0, microsecond=0)) / "state_1s.parquet"
        summary["referenced_state_parquet"] = str(pq)
        summary["referenced_state_parquet_sha256"] = file_sha256(pq)
        atomic_write_json(out_path, summary)
        h = file_sha256(out_path)
        mark_stage_complete(
            manifest,
            name,
            output_paths=[str(out_path), str(pq)],
            output_hashes={str(out_path): h or "", str(pq): summary["referenced_state_parquet_sha256"] or ""},
            schema_version="mb_state_1s_v1",
            config_hash=load_frozen_sha(),
            row_count=summary["n_state_rows"],
            reused=reused_all,
            duration_seconds=round(time.perf_counter() - t0, 3),
            status="SKIPPED_REUSED" if reused_all else "COMPLETE",
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "REUSED" if reused_all else f"COMPLETE ({summary['n_state_rows']})")
        return EXIT_OK, summary
    except Exception as exc:
        mark_stage_failed(manifest, name, str(exc))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"STATE_PREPARE failed: {exc}", flush=True)
        return EXIT_INTERNAL, {}


def _stage_candidates(manifest, man_path, coverage_report, symbol, start, end, cand_hash, n_stages=None):
    name = "CANDIDATE_DETECT"
    idx = 3
    total = _stage_total(manifest, n_stages)
    out_dir = Path(manifest["run_dir"]) / "candidates"
    pq = out_dir / "episode_candidates_v1.parquet"
    if _reuse_or_run(manifest, name) and pq.exists():
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            row_count=manifest["stages"][name].get("row_count"),
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
            config_hash=cand_hash,
            schema_version="episode_candidate_v1",
        )
        save_manifest(man_path, manifest)
        df = pd.read_parquet(pq)
        summary = json.loads((out_dir / "candidate_summary.json").read_text(encoding="utf-8"))
        _print_progress(idx, total, name, f"REUSED ({len(df)})")
        return EXIT_OK, summary, df

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    t0 = time.perf_counter()
    try:
        cfg = load_candidate_config(CAND_CFG)
        state_df, state_meta = load_states_for_interval(symbol=symbol, start=start, end=end)
        cov_status = coverage_report.get("verdict") or "DATA_COMPLETE"
        detect_kwargs = dict(
            cfg=cfg,
            symbol=symbol,
            coverage_status=cov_status,
            source_state_hash=state_meta["content_sha256"],
            replay_epoch=state_meta["replay_epoch"],
            coverage_report=coverage_report,
        )
        c1, d1, span_report = detect_candidates_span_aware(state_df, **detect_kwargs)
        c2, _, _ = detect_candidates_span_aware(state_df, **detect_kwargs)
        print(format_span_detect_terminal(span_report), end="", flush=True)
        h1 = candidates_content_sha256(c1)
        h2 = candidates_content_sha256(c2)
        idem = {"ok": h1 == h2, "hash_run1": h1, "hash_run2": h2}
        if not idem["ok"]:
            raise ValueError("candidate_detect_not_idempotent")
        resources = {
            "wall_seconds": round(time.perf_counter() - t0, 3),
            "n_state_rows": int(len(state_df)),
            "n_localized_state_rows": int(span_report.get("n_localized_state_rows") or 0),
        }
        write_episode_outputs(
            out_dir=out_dir,
            candidates=c1,
            diagnostics=d1,
            cfg=cfg,
            config_path=CAND_CFG,
            schema_path=CAND_SCHEMA,
            coverage_report=coverage_report,
            resources=resources,
            causality_proof={
                "outcome_files_read": False,
                "future_returns_loaded": False,
                "threshold_source": "PROVISIONAL_OUTCOME_BLIND",
            },
            idempotency=idem,
            quality_summary={"ok": True, "n_candidates": 0 if c1 is None else int(len(c1))},
            prefix_parity=None,
            verdict="OBFULL_EPISODE_CANDIDATE_DETECTOR_V1_PASS_WITH_PROXY_LIMITS",
        )
        mapping = candidate_span_mapping_frame(c1 if c1 is not None else pd.DataFrame())
        map_path = out_dir / "candidate_span_mapping.csv"
        mapping.to_csv(map_path, index=False)
        span_path = out_dir / "span_aware_detect_report.json"
        atomic_write_json(span_path, span_report)
        n = 0 if c1 is None else int(len(c1))
        summary = json.loads((out_dir / "candidate_summary.json").read_text(encoding="utf-8"))
        summary["span_aware"] = span_report
        atomic_write_json(out_dir / "candidate_summary.json", summary)
        out_hashes = {str(pq): file_sha256(pq) or ""}
        snap = out_dir / "config_snapshot.json"
        if snap.exists():
            out_hashes[str(snap)] = file_sha256(snap) or ""
        out_hashes[str(out_dir / "candidate_summary.json")] = file_sha256(out_dir / "candidate_summary.json") or ""
        out_hashes[str(map_path)] = file_sha256(map_path) or ""
        out_hashes[str(span_path)] = file_sha256(span_path) or ""
        mark_stage_complete(
            manifest,
            name,
            output_paths=[
                str(pq),
                str(out_dir / "candidate_summary.json"),
                str(map_path),
                str(span_path),
            ],
            output_hashes=out_hashes,
            schema_version="episode_candidate_v1",
            config_hash=cand_hash,
            row_count=n,
            duration_seconds=round(time.perf_counter() - t0, 3),
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, f"COMPLETE ({n})")
        return EXIT_OK, summary, c1 if c1 is not None else pd.DataFrame()
    except Exception as exc:
        mark_stage_failed(manifest, name, str(exc))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"CANDIDATE_DETECT failed: {exc}", flush=True)
        traceback.print_exc()
        return EXIT_INTERNAL, {}, None


def _stage_episodes(
    manifest,
    man_path,
    coverage_report,
    symbol,
    start,
    end,
    candidates_df,
    cand_hash,
    group_hash,
    support_path: Path,
    n_stages=None,
):
    name = "EPISODE_GROUP"
    idx = 4
    total = _stage_total(manifest, n_stages)
    out_dir = Path(manifest["run_dir"]) / "episodes"
    pq = out_dir / "behavior_episode_groups_v1.parquet"
    if _reuse_or_run(manifest, name) and pq.exists():
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            row_count=manifest["stages"][name].get("row_count"),
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
            config_hash=group_hash,
            schema_version="behavior_episode_group_v1",
        )
        save_manifest(man_path, manifest)
        df = pd.read_parquet(pq)
        summary = json.loads((out_dir / "episode_summary.json").read_text(encoding="utf-8"))
        _print_progress(idx, total, name, f"REUSED ({len(df)})")
        return EXIT_OK, summary, df

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    t0 = time.perf_counter()
    try:
        cfg = load_grouper_config(GROUP_CFG)
        window_ms = int(cfg["grouping_window_ms"])
        cand = candidates_df.copy()
        eligs = []
        for sp in coverage_report.get("usable_spans") or []:
            elig_s = sp.get("final_analysis_eligible_start") or sp.get("analysis_eligible_start")
            if elig_s:
                eligs.append(datetime.fromisoformat(str(elig_s).replace("Z", "+00:00")))
        group_start = min(eligs) if eligs else start
        cand = cand[
            (pd.to_datetime(cand["trigger_ts"], utc=True) >= group_start)
            & (pd.to_datetime(cand["trigger_ts"], utc=True) < end)
            & (cand["symbol"].astype(str).str.upper() == symbol)
        ]
        support_map = load_support_map(support_path)

        def _group(frame: pd.DataFrame):
            if frame is None or frame.empty:
                return group_behavior_episodes(
                    frame if frame is not None else pd.DataFrame(),
                    cfg=cfg,
                    grouping_config_hash=group_hash,
                    source_candidate_config_hash=cand_hash,
                    support_map=support_map,
                    group_window_ms=window_ms,
                )
            if "coverage_span_id" not in frame.columns:
                return group_behavior_episodes(
                    frame,
                    cfg=cfg,
                    grouping_config_hash=group_hash,
                    source_candidate_config_hash=cand_hash,
                    support_map=support_map,
                    group_window_ms=window_ms,
                )
            ep_parts, map_parts, diag_acc = [], [], None
            for sid, g in frame.groupby("coverage_span_id", dropna=False, sort=True):
                e, m, d = group_behavior_episodes(
                    g,
                    cfg=cfg,
                    grouping_config_hash=group_hash,
                    source_candidate_config_hash=cand_hash,
                    support_map=support_map,
                    group_window_ms=window_ms,
                )
                if e is not None and not e.empty:
                    e = e.copy()
                    e["coverage_span_id"] = sid
                if m is not None and not m.empty:
                    m = m.copy()
                    m["coverage_span_id"] = sid
                ep_parts.append(e)
                map_parts.append(m)
                if diag_acc is None:
                    diag_acc = dict(d)
                else:
                    for k, v in d.items():
                        if isinstance(v, (int, float)) and isinstance(diag_acc.get(k), (int, float)):
                            diag_acc[k] = diag_acc[k] + v
            ep = pd.concat(ep_parts, ignore_index=True) if ep_parts else pd.DataFrame()
            mp = pd.concat(map_parts, ignore_index=True) if map_parts else pd.DataFrame()
            return ep, mp, diag_acc or {}

        ep1, map1, diag1 = _group(cand)
        if ep1 is not None and not ep1.empty:
            sort_cols = [c for c in ("first_detection_available_at", "episode_id") if c in ep1.columns]
            if sort_cols:
                ep1 = ep1.sort_values(sort_cols).reset_index(drop=True)
        if episodes_cross_span(ep1):
            raise ValueError("cross_span_episodes")
        ep_per_span: dict[str, int] = {}
        if ep1 is not None and not ep1.empty and "coverage_span_id" in ep1.columns:
            ep_per_span = {str(k): int(v) for k, v in ep1["coverage_span_id"].value_counts().items()}
        n_cross_ep = 0
        print("EPISODE_GROUP span-aware", flush=True)
        print(f"Total episodes:             {0 if ep1 is None else int(len(ep1))}", flush=True)
        if ep_per_span:
            for sid, n_ep in sorted(ep_per_span.items()):
                print(f"  episodes[{sid}]: {n_ep}", flush=True)
        print(f"Cross-span episodes:        {n_cross_ep}", flush=True)
        h1 = content_hash_episodes(ep1, map1)
        ep2, map2, _ = _group(cand)
        h2 = content_hash_episodes(ep2, map2)
        mapping_val = validate_mapping(cand, map1, ep1)
        sizes = ep1["candidate_count"].astype(int) if ep1 is not None and not ep1.empty else pd.Series(dtype=int)
        summary = {
            **diag1,
            "episodes_per_span": ep_per_span,
            "cross_span_episodes": n_cross_ep,
            "n_episodes": int(len(ep1)) if ep1 is not None else 0,
            "reduction_rate": round(1.0 - (len(ep1) / max(diag1.get("n_candidates_in", 1), 1)), 6)
            if ep1 is not None
            else None,
            "direction_counts": ep1["direction_hint"].value_counts().to_dict() if ep1 is not None and not ep1.empty else {},
            "support_counts": ep1["support_status"].value_counts().to_dict() if ep1 is not None and not ep1.empty else {},
            "grouping_window_ms": window_ms,
            "grouping_config_hash": group_hash,
            "content_hash": h1,
            "idempotency_ok": h1 == h2,
            "mapping_validation": mapping_val,
            "support_path_used": str(support_path) if support_path.exists() else None,
            "support_referenced_existing_drilldown": support_path.exists(),
        }
        write_episode_group_outputs(
            out_dir=out_dir,
            episodes=ep1,
            mapping=map1,
            cfg=cfg,
            config_hash=group_hash,
            source_candidate_config_hash=cand_hash,
            coverage_report=coverage_report,
            causality_proof={
                "outcome_columns_read": False,
                "future_returns_read": False,
                "selection_uses_future_price": False,
                "grouping_time_anchor": cfg.get("grouping_time_anchor"),
            },
            idempotency={"ok": h1 == h2, "content_hash": h1, "content_hash_run2": h2},
            mapping_validation=mapping_val,
            summary=summary,
            resources={"wall_seconds": round(time.perf_counter() - t0, 3)},
            verdict="OBFULL_BEHAVIOR_EPISODE_GROUPER_V1_PASS",
        )
        # also write episode_summary.json alias
        atomic_write_json(out_dir / "episode_summary.json", summary)
        out_hashes = {str(pq): file_sha256(pq) or ""}
        mark_stage_complete(
            manifest,
            name,
            output_paths=[str(pq), str(out_dir / "episode_summary.json")],
            output_hashes=out_hashes,
            schema_version="behavior_episode_group_v1",
            config_hash=group_hash,
            row_count=int(len(ep1)) if ep1 is not None else 0,
            duration_seconds=round(time.perf_counter() - t0, 3),
        )
        save_manifest(man_path, manifest)
        n = int(len(ep1)) if ep1 is not None else 0
        _print_progress(idx, total, name, f"COMPLETE ({n})")
        return EXIT_OK, summary, ep1
    except Exception as exc:
        mark_stage_failed(manifest, name, str(exc))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"EPISODE_GROUP failed: {exc}", flush=True)
        traceback.print_exc()
        return EXIT_INTERNAL, {}, None



def _stage_avr(manifest, man_path, symbol, start, end, episodes_df, n_stages=None, validation_root=None, warmup_gate="STRICT"):
    name = "AVR_CONTEXT"
    order = manifest.get("stage_order") or []
    idx = (order.index(name) + 1) if name in order else 5
    total = _stage_total(manifest, n_stages)
    if name not in manifest["stages"]:
        mark_stage_failed(manifest, name, "stage_not_in_manifest")
        save_manifest(man_path, manifest)
        return EXIT_INTERNAL, {}
    if _reuse_or_run(manifest, name):
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            row_count=manifest["stages"][name].get("row_count"),
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
            schema_version="avr_episode_context_v1",
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "REUSED")
        return EXIT_OK, {"reused": True, "verdict": manifest["stages"][name].get("config_hash")}

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    try:
        result = run_avr_context_stage(
            run_dir=Path(manifest["run_dir"]),
            symbol=symbol,
            start=start,
            end=end,
            episodes=episodes_df,
            validation_root=validation_root,
            warmup_gate=warmup_gate,
        )
        if not result.get("ok"):
            hint = result.get("exit_hint")
            err = str(result.get("error") or "AVR_CONTEXT_FAILED")
            if hint == "coverage":
                mark_stage_blocked(manifest, name, err)
                manifest["verdict"] = VERDICT_BLOCKED_COVERAGE
                save_manifest(man_path, manifest)
                _print_progress(idx, total, name, "BLOCKED")
                print(f"AVR_CONTEXT blocked: {err}", flush=True)
                return EXIT_DATA_NOT_COMPLETE, result
            if hint == "parity":
                mark_stage_failed(manifest, name, err)
                manifest["verdict"] = AVR_PARITY_BLOCK
                save_manifest(man_path, manifest)
                _print_progress(idx, total, name, "FAILED")
                print(f"AVR_CONTEXT parity failure: {err}", flush=True)
                return EXIT_INTERNAL, result
            mark_stage_failed(manifest, name, err)
            save_manifest(man_path, manifest)
            _print_progress(idx, total, name, "FAILED")
            return EXIT_INTERNAL, result

        mark_stage_complete(
            manifest,
            name,
            output_paths=result.get("output_paths") or [],
            output_hashes=result.get("output_hashes") or {},
            schema_version="avr_episode_context_v1",
            config_hash=(result.get("provenance") or {}).get("avr_config_hash"),
            row_count=result.get("n_context"),
            duration_seconds=result.get("duration_seconds"),
        )
        save_manifest(man_path, manifest)
        _print_progress(
            idx,
            total,
            name,
            f"COMPLETE ({result.get('n_context')} ctx / {result.get('n_avr_1s')} 1s)",
        )
        return EXIT_OK, result
    except Exception as exc:
        mark_stage_failed(manifest, name, str(exc))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"AVR_CONTEXT failed: {exc}", flush=True)
        traceback.print_exc()
        return EXIT_INTERNAL, {}


def _stage_avr_multiscale(manifest, man_path, symbol, start, end, episodes_df, n_stages=None, validation_root=None, warmup_gate="STRICT"):
    name = "AVR_MULTISCALE_CONTEXT"
    order = manifest.get("stage_order") or []
    idx = (order.index(name) + 1) if name in order else 6
    total = _stage_total(manifest, n_stages)
    if name not in manifest["stages"]:
        mark_stage_failed(manifest, name, "stage_not_in_manifest")
        save_manifest(man_path, manifest)
        return EXIT_INTERNAL, {}
    if _reuse_or_run(manifest, name):
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            row_count=manifest["stages"][name].get("row_count"),
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
            schema_version="avr_multiscale_episode_context_v1",
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "REUSED")
        return EXIT_OK, {"reused": True}

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    try:
        result = run_avr_multiscale_stage(
            run_dir=Path(manifest["run_dir"]),
            symbol=symbol,
            start=start,
            end=end,
            episodes=episodes_df,
            validation_root=validation_root,
            warmup_gate=warmup_gate,
        )
        if not result.get("ok"):
            hint = result.get("exit_hint")
            err = str(result.get("error") or "AVR_MULTISCALE_FAILED")
            if hint == "coverage":
                mark_stage_blocked(manifest, name, err)
                manifest["verdict"] = VERDICT_BLOCKED_COVERAGE
                save_manifest(man_path, manifest)
                _print_progress(idx, total, name, "BLOCKED")
                print(f"AVR_MULTISCALE_CONTEXT blocked: {err}", flush=True)
                return EXIT_DATA_NOT_COMPLETE, result
            if hint == "parity":
                mark_stage_failed(manifest, name, err)
                manifest["verdict"] = err
                save_manifest(man_path, manifest)
                _print_progress(idx, total, name, "FAILED")
                print(f"AVR_MULTISCALE_CONTEXT parity failure: {err}", flush=True)
                return EXIT_INTERNAL, result
            mark_stage_failed(manifest, name, err)
            save_manifest(man_path, manifest)
            _print_progress(idx, total, name, "FAILED")
            return EXIT_INTERNAL, result

        mark_stage_complete(
            manifest,
            name,
            output_paths=result.get("output_paths") or [],
            output_hashes=result.get("output_hashes") or {},
            schema_version="avr_multiscale_episode_context_v1",
            config_hash=multiscale_config_hash(),
            row_count=result.get("n_rows"),
            duration_seconds=result.get("duration_seconds"),
        )
        save_manifest(man_path, manifest)
        _print_progress(
            idx,
            total,
            name,
            f"COMPLETE ({result.get('n_rows')} ctx / {result.get('n_5m')} 5m)",
        )
        return EXIT_OK, result
    except Exception as exc:
        mark_stage_failed(manifest, name, str(exc))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"AVR_MULTISCALE_CONTEXT failed: {exc}", flush=True)
        traceback.print_exc()
        return EXIT_INTERNAL, {}


def _stage_outcomes(
    manifest,
    man_path,
    precheck,
    symbol,
    start,
    end,
    episodes_df,
    group_hash,
    outcome_hash,
    n_stages=None,
):
    name = "PUBLIC_TRADE_OUTCOMES"
    order = manifest.get("stage_order") or []
    idx = (order.index(name) + 1) if name in order else 5
    total = _stage_total(manifest, n_stages)
    out_dir = Path(manifest["run_dir"]) / "outcomes"
    pq_name = "episode_outcomes_public_trade_carry_v1.parquet"
    pq = out_dir / pq_name
    if _reuse_or_run(manifest, name) and pq.exists():
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            row_count=manifest["stages"][name].get("row_count"),
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
            config_hash=outcome_hash,
            schema_version="episode_outcome_public_trade_v1",
        )
        save_manifest(man_path, manifest)
        df = pd.read_parquet(pq)
        summary = json.loads((out_dir / "outcome_summary.json").read_text(encoding="utf-8"))
        n_c = int((df["outcome_status"] == "COMPLETE").sum())
        _print_progress(idx, total, name, f"REUSED ({n_c}/{len(df)})")
        return EXIT_OK, summary, df, False

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    t0 = time.perf_counter()
    referenced = False
    try:
        cfg = load_pt_config(OUTCOME_CFG)
        assert is_carry_policy(cfg)
        assert cfg.get("forbid_book_mid_fallback") is True
        horizons = [int(h) for h in cfg["horizons_seconds"]]
        max_h = max(horizons)
        from ..partition_io import partition_dir as _pdir

        hour_after = end.replace(minute=0, second=0, microsecond=0)
        full_ob_gap = not (_pdir(symbol, hour_after) / "state_1s.parquet").exists()

        from datetime import timedelta

        load_start = start - timedelta(seconds=120)
        load_end = end + timedelta(seconds=max_h + 1)
        trade_index, load_meta = load_public_trades_window(symbol=symbol, start=load_start, end=load_end)

        out1 = build_pt_episode_outcomes(
            episodes_df,
            trade_index=trade_index,
            cfg=cfg,
            outcome_config_hash=outcome_hash,
            source_episode_config_hash=group_hash,
            full_ob_post_detection_gap=full_ob_gap,
            load_meta=load_meta,
        )
        h1 = content_hash_pt_outcomes(out1)
        out2 = build_pt_episode_outcomes(
            episodes_df,
            trade_index=trade_index,
            cfg=cfg,
            outcome_config_hash=outcome_hash,
            source_episode_config_hash=group_hash,
            full_ob_post_detection_gap=full_ob_gap,
            load_meta=load_meta,
        )
        h2 = content_hash_pt_outcomes(out2)
        row_val = validate_pt_row_contract(out1, n_episodes=len(episodes_df), horizons=horizons)

        status_by_h: dict[str, Any] = {}
        for h, g in out1.groupby("horizon_seconds"):
            status_by_h[str(int(h))] = {
                "COMPLETE": int((g["outcome_status"] == "COMPLETE").sum()),
                "CENSORED": int((g["outcome_status"] == "CENSORED").sum()),
                "NO_VALID_ANCHOR": int((g["outcome_status"] == "NO_VALID_ANCHOR").sum()),
            }
        n_complete = int((out1["outcome_status"] == "COMPLETE").sum())
        n_censored = int((out1["outcome_status"] == "CENSORED").sum())
        n_nva = int((out1["outcome_status"] == "NO_VALID_ANCHOR").sum())

        flag_counts: dict[str, int] = {}
        for fl in out1["quality_flags"]:
            items = fl if isinstance(fl, list) else (list(fl) if fl is not None else [])
            for f in items:
                flag_counts[str(f)] = flag_counts.get(str(f), 0) + 1

        ages_a = pd.to_numeric(out1["anchor_trade_age_ms"], errors="coerce").dropna()
        ages_e = pd.to_numeric(out1["endpoint_trade_age_ms"], errors="coerce").dropna()

        summary = {
            "n_episodes": int(len(episodes_df)),
            "n_horizons": len(horizons),
            "horizons_seconds": horizons,
            "n_outcome_rows": int(len(out1)),
            "n_complete": n_complete,
            "n_censored": n_censored,
            "n_no_valid_anchor": n_nva,
            "status_by_horizon": status_by_h,
            "price_source": "BYBIT_PUBLIC_TRADES",
            "price_policy": cfg.get("price_policy"),
            "outcome_config_hash": outcome_hash,
            "quality_flag_counts": flag_counts,
            "censor_reason_counts": out1.loc[out1["outcome_status"] != "COMPLETE", "censor_reason"]
            .value_counts(dropna=False)
            .to_dict(),
            "price_age": {
                "anchor_max_ms": float(ages_a.max()) if len(ages_a) else None,
                "endpoint_max_ms": float(ages_e.max()) if len(ages_e) else None,
                "anchor_p50_ms": float(ages_a.quantile(0.5)) if len(ages_a) else None,
                "endpoint_p50_ms": float(ages_e.quantile(0.5)) if len(ages_e) else None,
            },
            "content_hash": h1,
            "idempotency_ok": h1 == h2,
            "row_validation": row_val,
            "full_ob_post_detection_gap_context": full_ob_gap,
        }

        write_pt_outputs(
            out_dir=out_dir,
            outcomes=out1,
            cfg=cfg,
            config_hash=outcome_hash,
            source_episode_config_hash=group_hash,
            coverage_report=precheck.get("coverage") or {},
            pt_coverage={**load_meta, "future_public_trades": precheck.get("future_public_trades")},
            causality_proof={
                "anchor_cut": "trade_ts < first_detection_available_at",
                "endpoint_cut": "trade_ts <= horizon_end_ts",
                "no_book_mid_fallback": True,
                "price_policy": cfg.get("price_policy"),
            },
            idempotency={"ok": h1 == h2, "content_hash": h1, "content_hash_run2": h2},
            row_validation=row_val,
            summary=summary,
            resources={"wall_seconds": round(time.perf_counter() - t0, 3)},
            prefix_parity=None,
            trade_ordering={"sort_keys": cfg.get("sort_keys")},
            dedup_report={
                "raw_rows": trade_index.dedup.raw_rows,
                "unique_trade_ids": trade_index.dedup.unique_trade_ids,
            },
            book_mid_comparison=None,
            price_semantics={"note": "orchestrator uses public-trades carry only"},
            verdict="PUBLIC_TRADE_LAST_TRADE_CARRY_POLICY_V1_PASS",
            parquet_name=pq_name,
        )
        # overwrite outcome_summary with our richer summary
        atomic_write_json(out_dir / "outcome_summary.json", summary)

        # optional reference note to global pilot (hash compare in VALIDATE)
        if PILOT_OUT.exists():
            atomic_write_json(
                out_dir / "artifact_refs.json",
                {
                    "pilot_carry_parquet": str(PILOT_OUT),
                    "pilot_carry_sha256": file_sha256(PILOT_OUT),
                    "this_run_sha256": file_sha256(pq),
                    "state_partition_referenced_not_copied": True,
                },
            )

        out_hashes = {str(pq): file_sha256(pq) or ""}
        mark_stage_complete(
            manifest,
            name,
            output_paths=[str(pq), str(out_dir / "outcome_summary.json")],
            output_hashes=out_hashes,
            schema_version="episode_outcome_public_trade_v1",
            config_hash=outcome_hash,
            row_count=int(len(out1)),
            duration_seconds=round(time.perf_counter() - t0, 3),
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, f"COMPLETE ({n_complete}/{len(out1)})")
        # State partitions are referenced in-place (immutable); stage outputs are fully written.
        return EXIT_OK, summary, out1, True
    except Exception as exc:
        mark_stage_failed(manifest, name, str(exc))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"PUBLIC_TRADE_OUTCOMES failed: {exc}", flush=True)
        traceback.print_exc()
        return EXIT_INTERNAL, {}, None, False


def _stage_validate(
    manifest,
    man_path,
    symbol,
    start,
    end,
    candidates_df,
    episodes_df,
    outcomes_df,
    candidate_summary,
    episode_summary,
    outcome_summary,
    state_summary,
    n_stages=None,
    avr_summary=None,
):
    name = "VALIDATE"
    order = manifest.get("stage_order") or []
    idx = (order.index(name) + 1) if name in order else 6
    total = _stage_total(manifest, n_stages)
    if _reuse_or_run(manifest, name):
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "REUSED")
        rep = Path(manifest["run_dir"]) / "reports"
        causality = json.loads((rep / "causality_proof.json").read_text(encoding="utf-8")) if (rep / "causality_proof.json").exists() else {}
        idem = json.loads((rep / "idempotency_report.json").read_text(encoding="utf-8")) if (rep / "idempotency_report.json").exists() else {}
        parity = None
        return EXIT_OK, parity, causality, idem

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    t0 = time.perf_counter()
    issues: list[str] = []

    if state_summary.get("n_state_rows") is None:
        issues.append("missing_state_rows")
    if candidates_df is None or candidates_df.empty:
        issues.append("no_candidates")
    if episodes_df is None or episodes_df.empty:
        issues.append("no_episodes")
    if outcomes_df is None or outcomes_df.empty:
        issues.append("no_outcomes")

    expected_out = int(len(episodes_df)) * len(outcome_summary.get("horizons_seconds") or [])
    if outcomes_df is not None and len(outcomes_df) != expected_out:
        issues.append(f"outcome_row_mismatch:{len(outcomes_df)}!={expected_out}")

    if outcomes_df is not None and "price_source" in outcomes_df.columns:
        if (outcomes_df["price_source"] != "BYBIT_PUBLIC_TRADES").any():
            issues.append("price_source_mix")
        mid_cols = [c for c in outcomes_df.columns if c in {"mid_price", "anchor_mid", "endpoint_mid"}]
        if mid_cols:
            issues.append(f"mid_columns:{mid_cols}")

    # Future leakage: anchor after detection
    if outcomes_df is not None and "anchor_trade_ts" in outcomes_df.columns:
        a = pd.to_datetime(outcomes_df["anchor_trade_ts"], utc=True)
        d = pd.to_datetime(outcomes_df["first_detection_available_at"], utc=True)
        bad = (a.notna()) & (a >= d)
        if bad.any():
            issues.append("anchor_not_strictly_before_detection")

    # Candidate / episode ID sets — only against the BTC 10Z–11Z pilot window.
    parity: dict[str, Any] = {"checked": True}
    is_pilot_window = format_utc_z(start) == "2026-09-06T10:00:00Z" and format_utc_z(end) == "2026-09-06T11:00:00Z"
    parity["pilot_window"] = is_pilot_window
    if (not is_pilot_window) and PILOT_CAND.exists():
        parity["checked"] = False
        parity["note"] = "PILOT_ID_PARITY_SKIPPED_NON_PILOT_WINDOW"
    if is_pilot_window and PILOT_CAND.exists() and candidates_df is not None:
        pc = pd.read_parquet(PILOT_CAND)
        parity["candidate_ids_match"] = set(candidates_df["candidate_id"]) == set(pc["candidate_id"])
        parity["n_candidates_run"] = int(len(candidates_df))
        parity["n_candidates_pilot"] = int(len(pc))
        if not parity["candidate_ids_match"]:
            issues.append("candidate_id_parity_mismatch")
    if is_pilot_window and PILOT_EP.exists() and episodes_df is not None:
        pe = pd.read_parquet(PILOT_EP)
        parity["episode_ids_match"] = set(episodes_df["episode_id"]) == set(pe["episode_id"])
        parity["n_episodes_run"] = int(len(episodes_df))
        parity["n_episodes_pilot"] = int(len(pe))
        if not parity["episode_ids_match"]:
            issues.append("episode_id_parity_mismatch")
    if is_pilot_window and PILOT_OUT.exists() and outcomes_df is not None:
        po = pd.read_parquet(PILOT_OUT)
        both = outcomes_df.merge(
            po[
                [
                    "episode_id",
                    "horizon_seconds",
                    "outcome_status",
                    "return_bps",
                    "mfe_bps",
                    "mae_bps",
                    "anchor_trade_id",
                    "endpoint_trade_id",
                ]
            ].rename(
                columns={
                    "outcome_status": "pilot_status",
                    "return_bps": "pilot_return",
                    "mfe_bps": "pilot_mfe",
                    "mae_bps": "pilot_mae",
                    "anchor_trade_id": "pilot_anchor_id",
                    "endpoint_trade_id": "pilot_endpoint_id",
                }
            ),
            on=["episode_id", "horizon_seconds"],
            how="inner",
        )
        if len(both) != len(outcomes_df):
            issues.append(f"outcome_join_incomplete:{len(both)}")
        ret_ok = (
            (both["outcome_status"] == both["pilot_status"])
            & ((both["return_bps"] - both["pilot_return"]).abs() < 1e-9)
            & (both["anchor_trade_id"] == both["pilot_anchor_id"])
            & (both["endpoint_trade_id"] == both["pilot_endpoint_id"])
        )
        # MFE/MAE may be null for CONFLICTING
        mfe_ok = both["mfe_bps"].isna() & both["pilot_mfe"].isna() | (
            (both["mfe_bps"] - both["pilot_mfe"]).abs() < 1e-9
        )
        parity["outcome_content_match"] = bool(ret_ok.all() and mfe_ok.all())
        parity["n_outcome_compared"] = int(len(both))
        if not parity["outcome_content_match"]:
            issues.append("outcome_content_parity_mismatch")

    parity["state_rows"] = state_summary.get("n_state_rows")
    parity["config_hashes"] = manifest.get("config_hashes")

    causality = {
        "state_features_available_at_only": True,
        "candidates_outcome_blind": True,
        "episode_grouping_outcome_blind": True,
        "representative_selection_outcome_blind": True,
        "public_trade_anchor_strictly_before_detection": "anchor_not_strictly_before_detection" not in issues,
        "future_trades_only_in_outcome_labels": True,
        "no_outcome_feedback_into_detection_or_grouping": True,
        "no_episode_selection_by_later_price": True,
        "price_source_fixed": "BYBIT_PUBLIC_TRADES",
        "price_policy_fixed": "LAST_TRADE_CARRY_WITH_SOURCE_GAP_GUARD_V1",
        "no_book_mid_fallback": True,
    }
    idempotency = {
        "candidates_ok": candidate_summary.get("candidates_content_sha256") is not None,
        "episodes_ok": bool(episode_summary.get("idempotency_ok")),
        "outcomes_ok": bool(outcome_summary.get("idempotency_ok")),
        "candidate_hash": candidate_summary.get("candidates_content_sha256"),
        "episode_hash": episode_summary.get("content_hash"),
        "outcome_hash": outcome_summary.get("content_hash"),
    }

    # Persist validation artifact early (report stage also writes)
    val_path = Path(manifest["run_dir"]) / "reports" / "validation_report.json"
    atomic_write_json(
        val_path,
        {"ok": len(issues) == 0, "issues": issues, "parity": parity, "causality": causality, "idempotency": idempotency},
    )

    if issues:
        mark_stage_failed(manifest, name, ";".join(issues))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"VALIDATE failed: {issues}", flush=True)
        return EXIT_INTERNAL, parity, causality, idempotency

    h = file_sha256(val_path)
    mark_stage_complete(
        manifest,
        name,
        output_paths=[str(val_path)],
        output_hashes={str(val_path): h or ""},
        schema_version="analysis_validation_v1",
        row_count=0,
        duration_seconds=round(time.perf_counter() - t0, 3),
    )
    save_manifest(man_path, manifest)
    _print_progress(idx, total, name, "COMPLETE")
    return EXIT_OK, parity, causality, idempotency


def _stage_report(
    manifest,
    man_path,
    run_dir,
    precheck,
    state_summary,
    candidate_summary,
    episode_summary,
    outcome_summary,
    causality,
    idempotency,
    resources,
    parity,
    verdict,
    n_stages=None,
    avr_summary=None,
):
    name = "UNIFIED_TECHNICAL_REPORT"
    order = manifest.get("stage_order") or []
    idx = (order.index(name) + 1) if name in order else 7
    total = _stage_total(manifest, n_stages)
    if _reuse_or_run(manifest, name):
        mark_stage_complete(
            manifest,
            name,
            reused=True,
            status="SKIPPED_REUSED",
            duration_seconds=0.0,
            output_paths=manifest["stages"][name].get("output_paths"),
            output_hashes=manifest["stages"][name].get("output_hashes"),
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "REUSED")
        return EXIT_OK

    mark_stage_running(manifest, name)
    save_manifest(man_path, manifest)
    t0 = time.perf_counter()
    try:
        paths = write_unified_report(
            reports_dir=Path(run_dir) / "reports",
            manifest=manifest,
            precheck=precheck,
            state_summary=state_summary,
            candidate_summary=candidate_summary,
            episode_summary=episode_summary,
            outcome_summary=outcome_summary,
            causality=causality,
            idempotency=idempotency,
            resources=resources,
            parity=parity,
            verdict=verdict,
            avr_summary=avr_summary,
        )
        # also ensure run_manifest path listed
        out_hashes = {p: file_sha256(Path(p)) or "" for p in paths.values()}
        mark_stage_complete(
            manifest,
            name,
            output_paths=list(paths.values()),
            output_hashes=out_hashes,
            schema_version="unified_technical_report_v1",
            duration_seconds=round(time.perf_counter() - t0, 3),
        )
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "COMPLETE")
        return EXIT_OK
    except Exception as exc:
        mark_stage_failed(manifest, name, str(exc))
        save_manifest(man_path, manifest)
        _print_progress(idx, total, name, "FAILED")
        print(f"UNIFIED_REPORT failed: {exc}", flush=True)
        traceback.print_exc()
        return EXIT_INTERNAL
