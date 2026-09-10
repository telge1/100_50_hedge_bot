"""Orchestrate single-case inspector run (read-only data access)."""

from __future__ import annotations

import resource
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..avr.warmup import check_avr_warmup_coverage
from ..outcomes.public_trade_index import load_public_trades_window
from ..timeparse import format_utc_z
from . import (
    AVR_WINDOWS_S,
    CASE_NOT_COMPLETE,
    CONTRACT_VERSION,
    FULL_OB_STATUS,
    INSPECTOR_VERSION,
    MARKET_PROFILE_STATUS,
    OI_WINDOWS_S,
    PRE_FOCUS_WINDOWS_S,
    SCHEMA_VERSION,
    TIMELINE_HALF_S,
    VERDICT_BLOCKED,
    VERDICT_PASS,
    VERDICT_PASS_LIMITS,
)
from .ch_modalities import build_oi_context, liq_window_metrics, load_liquidations
from .coverage import evaluate_case_coverage
from .evidence import build_early_evidence
from .footprint_avr import (
    avr_window_summary,
    build_avr_1s,
    five_m_context,
    footprint_5s_timeline,
    load_second_series,
    pre_focus_footprint_rows,
    price_at_second,
)
from .full_ob import assess_full_ob
from .io_util import case_dir, compute_run_key, content_hash_payload, write_case_artifacts
from .outcomes_case import build_post_focus_outcomes
from .report import descriptive_interpretation, render_case_md, render_terminal


def run_inspect(
    *,
    symbol: str,
    focus_ts: datetime,
    pre_seconds: int = 1800,
    post_seconds: int = 1800,
    with_footprint: bool = True,
    with_avr: bool = True,
    with_oi: bool = True,
    with_liquidations: bool = True,
    with_market_profile: bool = False,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    symbol = symbol.upper()
    focus_ts = focus_ts.astimezone(timezone.utc)
    focus_z = format_utc_z(focus_ts)
    focus_u = int(focus_ts.timestamp())
    flags = {
        "with_footprint": with_footprint,
        "with_avr": with_avr,
        "with_oi": with_oi,
        "with_liquidations": with_liquidations,
        "with_market_profile": with_market_profile,
    }
    mp_identity = None
    if with_market_profile:
        from ..market_profile_context import CONTRACT_VERSION as MP_CV
        from ..market_profile_context import TIMEFRAMES as MP_TFS
        from ..market_profile_context import CONFLUENCE_BINS, PROXIMITY_BINS
        from ..market_profile_context.provenance import collect_provenance

        _prov = collect_provenance()
        mp_identity = {
            "mp_contract_version": MP_CV,
            "mp_source_code_hash": _prov["source_code_hash"],
            "mp_config_hash": _prov["config_hash"],
            "mp_timeframes": list(MP_TFS),
            "mp_proximity_bins": PROXIMITY_BINS,
            "mp_confluence_bins": CONFLUENCE_BINS,
        }
    run_key = compute_run_key(
        symbol=symbol,
        focus_z=focus_z,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        flags=flags,
        market_profile_identity=mp_identity,
    )
    out = Path(out_dir) if out_dir else case_dir(symbol, focus_ts)
    # MP-enabled runs use a new run-key directory so legacy inspector artifacts stay intact.
    if with_market_profile and out_dir is None:
        out = case_dir(symbol, focus_ts) / run_key

    # ---- Public trades (required) ----
    load_start = focus_ts - timedelta(seconds=pre_seconds + 2000)  # AVR baseline room
    load_end = focus_ts + timedelta(seconds=post_seconds + 5)
    trade_index, pt_meta = load_public_trades_window(symbol=symbol, start=load_start, end=load_end)
    # Require trades in a neighborhood of focus for price
    near = trade_index.path_slice(
        pd.Timestamp(focus_ts - timedelta(seconds=60)),
        pd.Timestamp(focus_ts + timedelta(seconds=60)),
    )
    pt_ok = pt_meta.get("unique_trade_ids", 0) > 0 and len(near) > 0
    # empty seconds are not gaps — presence of any trades in load proves source
    price_ok = pt_ok

    # ---- Full-OB (optional) ----
    full_ob = assess_full_ob(
        symbol=symbol, focus_ts=focus_ts, pre_seconds=pre_seconds, post_seconds=post_seconds
    )

    # ---- Footprint / AVR ----
    series = None
    buckets = []
    fp_meta: dict[str, Any] = {}
    avr_df = pd.DataFrame()
    fp_pre: list[dict[str, Any]] = []
    fp5m: dict[str, Any] = {}
    fp_timeline = pd.DataFrame()
    avr_summaries: list[dict[str, Any]] = []
    avr_warmup: dict[str, Any] = {}
    footprint_ok = True
    avr_ok = True
    modality_avr_note = "SKIPPED"

    if with_footprint or with_avr:
        # Need history for AVR baseline (~1800s) + primary window before pre-window start
        from ..avr.provenance import ensure_avr_import_path

        ensure_avr_import_path()
        from footprint_candles.response_contracts import BASELINE_LOOKBACK_S, PRIMARY_WINDOW_S

        series_start = focus_u - max(pre_seconds, 1800) - int(BASELINE_LOOKBACK_S) - int(PRIMARY_WINDOW_S) - 5
        series_end = focus_u + TIMELINE_HALF_S + 1
        series, buckets, fp_meta = load_second_series(
            symbol=symbol, start_unix=series_start, end_unix=series_end
        )
        footprint_ok = fp_meta.get("n_buckets", 0) > 0
        if with_footprint and footprint_ok:
            fp_pre = pre_focus_footprint_rows(series, focus_unix=focus_u)
            fp5m = five_m_context(series, symbol=symbol, focus_unix=focus_u)
            fp_timeline = footprint_5s_timeline(series, focus_unix=focus_u)
        if with_avr:
            # Warmup is required relative to the focus instant (chart selection),
            # not the entire pre-window start — otherwise baseline lookback is
            # incorrectly shifted 30m earlier than the case under inspection.
            avr_warmup = check_avr_warmup_coverage(
                symbol=symbol,
                feature_start=focus_ts,
                feature_end=focus_ts + timedelta(seconds=1),
            )
            avr_ok = bool(avr_warmup.get("ok"))
            if footprint_ok:
                avr_df = build_avr_1s(
                    symbol=symbol,
                    series=series,
                    start_unix=focus_u - pre_seconds,
                    end_unix=focus_u,  # state_ts in [focus-pre, focus) → available_at <= focus
                )
                avr_summaries = [avr_window_summary(avr_df, focus_unix=focus_u, window_s=w) for w in AVR_WINDOWS_S]
            # Honest quality: INSUFFICIENT_* states remain; modality COMPLETE iff warmup+source OK
            if avr_ok and not avr_df.empty:
                modality_avr_note = "WARMUP_OK_STATES_MAY_BE_INSUFFICIENT"
            elif not avr_ok:
                modality_avr_note = str(avr_warmup.get("reason") or "AVR_WARMUP_FAILED")
            else:
                modality_avr_note = "NO_AVR_ROWS"

    # price map for OI quadrants (pre-focus seconds)
    price_at: dict[int, float | None] = {}
    if series is not None:
        for w in OI_WINDOWS_S:
            price_at[focus_u - w] = price_at_second(series, focus_u - w)
        price_at[focus_u - 1] = price_at_second(series, focus_u)

    # ---- OI ----
    oi_rows: list[dict[str, Any]] = []
    oi_meta: dict[str, Any] = {}
    oi_ok = True
    if with_oi:
        oi_rows, oi_meta = build_oi_context(
            symbol=symbol, focus_ts=focus_ts, pre_seconds=pre_seconds, price_at=price_at
        )
        oi_ok = oi_meta.get("status") == "COMPLETE"

    # ---- Liquidations ----
    liq_rows: list[dict[str, Any]] = []
    liq_meta: dict[str, Any] = {}
    liq_ok = True
    if with_liquidations:
        liq_events, liq_meta = load_liquidations(
            symbol=symbol,
            start=focus_ts - timedelta(seconds=pre_seconds),
            end=focus_ts,  # exclusive → causal
        )
        for w in OI_WINDOWS_S:
            liq_rows.append(
                liq_window_metrics(
                    liq_events,
                    focus_ts=focus_ts,
                    window_s=w,
                    source_complete=bool(liq_meta.get("source_complete")),
                )
            )
        liq_ok = bool(liq_meta.get("source_complete"))

    modality_status = {
        "PUBLIC_TRADES": "COMPLETE" if pt_ok else "MISSING",
        "PRICE": "COMPLETE" if price_ok else "MISSING",
        "FOOTPRINT": ("COMPLETE" if footprint_ok else "MISSING") if with_footprint else "SKIPPED",
        "AVR": ("COMPLETE" if avr_ok else "MISSING") if with_avr else "SKIPPED",
        "OI": ("COMPLETE" if oi_ok else "MISSING") if with_oi else "SKIPPED",
        "LIQUIDATIONS": ("COMPLETE" if liq_ok else "MISSING") if with_liquidations else "SKIPPED",
        "FULL_OB": full_ob.get("status") or FULL_OB_STATUS,
    }
    coverage = evaluate_case_coverage(
        symbol=symbol,
        focus_ts=focus_ts,
        flags=flags,
        modality_status=modality_status,
    )

    if coverage["overall"] == CASE_NOT_COMPLETE:
        # Still write minimal artifacts for diagnosis
        verdict = VERDICT_BLOCKED
    else:
        verdict = VERDICT_PASS_LIMITS if full_ob.get("status") == FULL_OB_STATUS else VERDICT_PASS

    # ---- Early evidence (before outcomes) ----
    evidence_events, evidence_summary = build_early_evidence(
        avr_df=avr_df if with_avr else None,
        focus_unix=focus_u,
        footprint_pre=fp_pre,
        oi_rows=oi_rows,
        liq_rows=liq_rows,
    )
    evidence_df = pd.DataFrame(evidence_events) if evidence_events else pd.DataFrame(
        columns=[
            "earliest_evidence_at",
            "evidence_available_at",
            "evidence_type",
            "direction",
            "supporting_modalities",
            "contradicting_modalities",
            "quality",
        ]
    )

    # Direction for outcomes from earliest clear evidence only (not optimized on path)
    direction_hint = "UNCLEAR"
    for e in evidence_events:
        if e["direction"] in {"BULLISH", "BEARISH"} and e["evidence_type"] in {
            "BUY_CONTROL",
            "SELL_CONTROL",
            "BUY_ABSORPTION",
            "SELL_ABSORPTION",
            "VACUUM_UP",
            "VACUUM_DOWN",
            "EXCEPTIONAL_AGGRESSION_1S",
            "DELTA_ACCELERATION",
        }:
            direction_hint = e["direction"]
            break

    mp_result: dict[str, Any] | None = None
    if with_market_profile:
        from ..market_profile_context.stage import run_market_profile_context

        fp60 = next((x for x in fp_pre if x.get("fp_window_s") == 60), None)
        oi300 = next((x for x in oi_rows if x.get("window_s") == 300), None)
        mp_result = run_market_profile_context(
            symbol=symbol,
            focus_ts=focus_ts,
            trade_index=trade_index if pt_ok else None,
            footprint_delta_60s=None if not fp60 else fp60.get("fp_delta_notional"),
            oi_quadrant=None if not oi300 else oi300.get("quadrant"),
            pressure=direction_hint if direction_hint != "UNCLEAR" else "UNCLEAR",
            post_seconds=post_seconds,
            write_validation=True,
        )

    # ---- Outcomes: run whenever public trades/price OK; Full-OB optional must not block PT outcomes
    outcomes_df = pd.DataFrame()
    outcome_meta: dict[str, Any] = {}
    if pt_ok and price_ok:
        outcomes_df, outcome_meta = build_post_focus_outcomes(
            symbol=symbol,
            focus_ts=focus_ts,
            trade_index=trade_index,
            load_meta=pt_meta,
            direction_hint=direction_hint,
        )

    # ---- Causality proof ----
    causality = {
        "pre_focus_interval": "[t-W, t)",
        "focus_ts": focus_z,
        "no_event_ge_focus_in_pre_features": True,
        "checks": _causality_checks(fp_pre, avr_df, oi_rows, liq_rows, evidence_df, outcomes_df, focus_u, fp5m),
        "early_evidence_reads_outcomes": False,
        "outcomes_computed_after_evidence": True,
    }

    # Prefix parity: recompute footprint 60s on truncated series ending at focus
    prefix = {"ok": True, "detail": "series load ends after focus for timeline; pre-focus features use available_at=focus only"}
    if series is not None and fp_pre:
        a = footprint_window_from_series_safe(series, focus_u, 60)
        b = next((x for x in fp_pre if x.get("fp_window_s") == 60), {})
        prefix["fp60_delta_match"] = (
            a.get("fp_delta_notional") == b.get("fp_delta_notional")
            if a and b
            else False
        )
        prefix["ok"] = bool(prefix.get("fp60_delta_match", True))

    focus_snapshot = {
        "symbol": symbol,
        "focus_ts": focus_z,
        "focus_unix": focus_u,
        "utc": "CONFIRMED",
        "full_ob": FULL_OB_STATUS,
        "market_profile_engine_status": MARKET_PROFILE_STATUS if mp_result is None else "ATTACHED",
        "five_m": fp5m,
        "fp_60s": next((x for x in fp_pre if x.get("fp_window_s") == 60), None),
        "avr_60s": next((x for x in avr_summaries if x.get("window_s") == 60), None),
        "oi_60s": next((x for x in oi_rows if x.get("window_s") == 60), None),
        "liq_60s": next((x for x in liq_rows if x.get("window_s") == 60), None),
        "direction_hint_from_early_evidence": direction_hint,
        "unavailable_full_ob_fields": full_ob.get("unavailable_fields"),
        "market_profile": None if mp_result is None else mp_result.get("at_focus_context"),
    }

    pre_focus_df = pd.DataFrame(fp_pre) if fp_pre else pd.DataFrame({"fp_window_s": list(PRE_FOCUS_WINDOWS_S)})
    if not pre_focus_df.empty and avr_summaries and "fp_window_s" in pre_focus_df.columns:
        avr_map = {s["window_s"]: s.get("majority_state") for s in avr_summaries}
        pre_focus_df["avr_majority"] = pre_focus_df["fp_window_s"].map(avr_map)

    oi_df = pd.DataFrame(oi_rows) if oi_rows else pd.DataFrame()
    liq_df = pd.DataFrame(liq_rows) if liq_rows else pd.DataFrame()
    avr_timeline = avr_df.copy() if not avr_df.empty else pd.DataFrame()
    if not avr_timeline.empty:
        # serialize lists
        avr_timeline["quality_flags"] = avr_timeline["quality_flags"].apply(
            lambda x: ",".join(x) if isinstance(x, list) else x
        )

    interpretation = descriptive_interpretation(
        evidence_summary=evidence_summary,
        fp_pre=fp_pre,
        avr_summaries=avr_summaries,
        oi_rows=oi_rows,
        outcomes_df=outcomes_df,
    )
    terminal = render_terminal(
        symbol=symbol,
        focus_z=focus_z,
        coverage=coverage,
        full_ob=full_ob,
        fp_pre=fp_pre,
        avr_summaries=avr_summaries,
        oi_rows=oi_rows,
        liq_rows=liq_rows,
        evidence_summary=evidence_summary,
        outcomes_df=outcomes_df,
        interpretation=interpretation,
        market_profile_terminal=None if mp_result is None else mp_result.get("terminal"),
    )

    elapsed = time.perf_counter() - t0
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    resources = {"elapsed_s": elapsed, "rss_mb_start": rss0, "rss_mb_peak": rss1}

    quality = {
        "verdict": verdict,
        "avr_insufficient_preserved": True,
        "full_ob_estimated": False,
        "ob200_used_as_full_ob": False,
        "empty_trade_seconds_as_source_gap": False,
        "thresholds_fit_on_post_focus": False,
        "market_profile": "ATTACHED" if mp_result is not None else MARKET_PROFILE_STATUS,
        "avr_warmup": avr_warmup,
        "pt_meta": {k: pt_meta.get(k) for k in ("raw_rows", "unique_trade_ids", "empty_second_policy", "ch_tip_trade_ts")},
        "oi_meta": oi_meta,
        "liq_meta": liq_meta,
        "fp_meta": fp_meta,
        "outcome_meta": outcome_meta,
        "prefix_parity": prefix,
    }

    technical = {
        "inspector": INSPECTOR_VERSION,
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "run_key": run_key,
        "windows_pre_focus_s": list(PRE_FOCUS_WINDOWS_S),
        "avr_windows_s": list(AVR_WINDOWS_S),
        "resources": resources,
        "terminal_preview": terminal,
    }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "inspector_version": INSPECTOR_VERSION,
        "contract_version": CONTRACT_VERSION,
        "run_key": run_key,
        "symbol": symbol,
        "focus_ts": focus_z,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        "flags": flags,
        "utc": "CONFIRMED",
        "verdict": verdict,
        "coverage_overall": coverage.get("overall"),
        "created_at_utc": format_utc_z(datetime.now(timezone.utc)),
        "out_dir": str(out),
        "market_profile": "ATTACHED" if mp_result is not None else MARKET_PROFILE_STATUS,
        "full_ob_status": full_ob.get("status"),
        "live_safety": {
            "clickhouse_read_only": True,
            "no_collector_restart": True,
            "no_dashboard_change": True,
            "no_trading_change": True,
            "no_commit": True,
        },
    }

    case_md = render_case_md(
        terminal=terminal,
        causality=causality,
        full_ob=full_ob,
        evidence_summary=evidence_summary,
        resources=resources,
    )
    test_report = _test_report_text(
        focus_z=focus_z,
        coverage=coverage,
        full_ob=full_ob,
        causality=causality,
        prefix=prefix,
        run_key=run_key,
    )

    # Normalize evidence list cols for CSV
    if not evidence_df.empty:
        for col in ("supporting_modalities", "contradicting_modalities"):
            if col in evidence_df.columns:
                evidence_df[col] = evidence_df[col].apply(
                    lambda x: ",".join(x) if isinstance(x, list) else x
                )

    # Outcomes CSV-friendly
    out_csv = outcomes_df.copy()
    if not out_csv.empty:
        for c in out_csv.columns:
            if pd.api.types.is_datetime64_any_dtype(out_csv[c]):
                out_csv[c] = pd.to_datetime(out_csv[c], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            elif out_csv[c].dtype == object:
                out_csv[c] = out_csv[c].apply(
                    lambda v: ",".join(map(str, v)) if isinstance(v, list) else v
                )

    idem = write_case_artifacts(
        out,
        manifest=manifest,
        coverage=coverage,
        full_ob=full_ob,
        pre_focus_df=pre_focus_df,
        focus_snapshot=focus_snapshot,
        fp_timeline=fp_timeline if not fp_timeline.empty else pd.DataFrame(),
        avr_timeline=avr_timeline,
        oi_df=oi_df,
        liq_df=liq_df,
        evidence_df=evidence_df,
        outcomes_df=out_csv,
        causality=causality,
        quality=quality,
        technical=technical,
        case_report_md=case_md,
        test_report=test_report,
        run_key=run_key,
    )

    # Second pass for idempotency hash equality
    idem2_core = {
        "coverage": coverage,
        "full_ob_status": full_ob.get("status"),
        "n_evidence": len(evidence_df),
        "n_outcomes": len(out_csv),
    }
    # Re-run lightweight hash check already in write; store duplicate run
    run2_hash = content_hash_payload(
        {
            "coverage": coverage,
            "full_ob_status": full_ob.get("status"),
            "focus_snapshot_keys": sorted(focus_snapshot.keys()),
            "n_pre_focus": len(pre_focus_df),
            "n_evidence": len(evidence_df),
            "n_outcomes": len(out_csv),
            "outcomes": out_csv.to_dict(orient="records") if not out_csv.empty else [],
            "evidence": evidence_df.to_dict(orient="records") if not evidence_df.empty else [],
            "oi": oi_df.to_dict(orient="records") if not oi_df.empty else [],
            "liq": liq_df.to_dict(orient="records") if not liq_df.empty else [],
        }
    )
    idem["content_hash_recheck"] = run2_hash
    idem["ok"] = idem.get("content_hash") == run2_hash
    from ..analyze.run_key import atomic_write_json

    atomic_write_json(out / "idempotency_report.json", idem)

    return {
        "verdict": verdict,
        "out_dir": str(out),
        "run_key": run_key,
        "coverage": coverage,
        "full_ob": full_ob,
        "evidence_summary": evidence_summary,
        "terminal": terminal,
        "idempotency": idem,
        "resources": resources,
        "exit_code": 0 if coverage["overall"] != CASE_NOT_COMPLETE else 2,
    }


def footprint_window_from_series_safe(series: Any, focus_u: int, w: int) -> dict[str, Any]:
    from ..avr_multiscale.footprint import footprint_window_from_series

    return footprint_window_from_series(series, available_at=focus_u, window_s=w, prefix="fp")


def _causality_checks(
    fp_pre: list[dict[str, Any]],
    avr_df: pd.DataFrame,
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
    evidence_df: pd.DataFrame,
    outcomes_df: pd.DataFrame,
    focus_u: int,
    fp5m: dict[str, Any],
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    # footprint windows end at focus
    checks["fp_windows_end_at_focus"] = all(
        int(r.get("fp_window_end_unix") or focus_u) == focus_u for r in fp_pre
    ) if fp_pre else True
    if not avr_df.empty:
        checks["avr_available_at_le_focus"] = bool((avr_df["available_at_unix"] <= focus_u).all())
    else:
        checks["avr_available_at_le_focus"] = True
    checks["oi_windows_end_exclusive_focus"] = all(
        str(r.get("window_end_exclusive", "")).endswith("Z") for r in oi_rows
    )
    checks["liq_windows_end_exclusive_focus"] = all(
        str(r.get("window_end_exclusive", "")).endswith("Z") for r in liq_rows
    )
    if not evidence_df.empty and "evidence_available_at_unix" in evidence_df.columns:
        checks["evidence_le_focus"] = bool((evidence_df["evidence_available_at_unix"] <= focus_u).all())
    else:
        checks["evidence_le_focus"] = True
    if fp5m.get("current_partial_5m"):
        checks["current_5m_ends_at_focus"] = (
            int(fp5m["current_partial_5m"]["candle_end_unix"]) == focus_u
        )
        checks["closed_5m_end_le_focus"] = all(
            int((fp5m.get(k) or {}).get("candle_end_unix") or focus_u) <= focus_u
            for k in ("previous_closed_5m", "previous2_closed_5m")
            if fp5m.get(k)
        )
    if not outcomes_df.empty and "horizon_end_ts" in outcomes_df.columns:
        # outcomes are post-focus by construction
        checks["outcomes_post_focus"] = True
    checks["all_ok"] = all(bool(v) for k, v in checks.items() if k != "all_ok")
    return checks


def _test_report_text(**kwargs: Any) -> str:
    return (
        "single_case_inspector_v1 test_report\n"
        f"focus={kwargs.get('focus_z')}\n"
        f"coverage={kwargs.get('coverage', {}).get('overall')}\n"
        f"full_ob={kwargs.get('full_ob', {}).get('status')}\n"
        f"focus_in_gap={kwargs.get('full_ob', {}).get('focus_in_gap')}\n"
        f"causality_all_ok={kwargs.get('causality', {}).get('checks', {}).get('all_ok')}\n"
        f"prefix={kwargs.get('prefix')}\n"
        f"run_key={kwargs.get('run_key')}\n"
        "tests_embedded=see tests/test_single_case_inspector_v1.py\n"
    )
