"""Runner modes: dry-run / enrich / analyze / report-only."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from . import constants as C
from .analysis_hypotheses import run_all_hypotheses
from .checkpoint_io import (
    atomic_write_json,
    ensure_dirs,
    hash_mismatch,
    load_checkpoint,
    should_skip_complete,
    write_enrichment_checkpoint,
)
from .enrich_candidate import coverage_summary, enrich_candidate_row
from .feature_spec import FEATURE_SPECIFICATION
from .hashes import all_hashes
from .parity import ReferenceParityError, assert_parity_or_raise, check_reference_parity
from .reference_filter import is_excluded_symbol, join_candidates_trades
from .reference_inventory import EmptyFrozenReferenceError, inventory_frozen_reference, load_source_checkpoint
from .schema_mapping import SOURCE_SCHEMA_AUDIT


def _repo_root() -> Path:
    # reference_enrichment → … → src → repo root
    return Path(__file__).resolve().parents[6]


def _git_meta() -> dict[str, Any]:
    import subprocess

    repo = _repo_root()
    out: dict[str, Any] = {"repo": str(repo)}
    try:
        out["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
        ).strip()
        out["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    except Exception as e:
        out["error"] = str(e)
    return out


def resolve_path(p: str | Path) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return _repo_root() / path


def default_cfg(**kwargs) -> dict[str, Any]:
    return {
        "input_dir": C.DEFAULT_INPUT_DIR,
        "output_dir": C.DEFAULT_OUTPUT_DIR,
        "start": C.DEFAULT_START,
        "end": C.DEFAULT_END,
        "max_workers": 1,
        "limit_symbols": None,
        "symbols": None,
        "checkpoint_every": 1,
        "cli_argv": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }


def _list_input_symbols(input_dir: Path, cfg: dict) -> list[str]:
    ck = input_dir / "checkpoints"
    symbols = sorted(p.stem.upper() for p in ck.glob("*.json"))
    if cfg.get("symbols"):
        want = {s.upper() for s in cfg["symbols"]}
        symbols = [s for s in symbols if s in want]
    if cfg.get("limit_symbols"):
        symbols = symbols[: int(cfg["limit_symbols"])]
    return [s for s in symbols if not is_excluded_symbol(s)]


def run_dry_run(cfg: dict[str, Any]) -> dict[str, Any]:
    """Plan only — never opens ClickHouse / market DB."""
    input_dir = resolve_path(cfg["input_dir"])
    output_dir = resolve_path(cfg["output_dir"])
    symbols = _list_input_symbols(input_dir, cfg) if (input_dir / "checkpoints").exists() else []
    plan = {
        "mode": "dry-run",
        "code_status": C.CODE_STATUS,
        "clickhouse_queries": False,
        "clickhouse_query_count": 0,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "start": cfg["start"].isoformat() if hasattr(cfg["start"], "isoformat") else cfg["start"],
        "end": cfg["end"].isoformat() if hasattr(cfg["end"], "isoformat") else cfg["end"],
        "n_symbols": len(symbols),
        "symbols_sample": symbols[:10],
        "excluded": sorted(C.EXCLUDE_SYMBOLS),
        "hashes": all_hashes(),
        "reference": {
            "timeframe": C.REF_TIMEFRAME,
            "mode": C.REF_MODE,
            "group": C.REF_GROUP,
            "strategy_key": C.REF_STRATEGY_KEY,
            "entry_rule": C.ENTRY_RULE,
        },
        "not_available_features": list(SOURCE_SCHEMA_AUDIT["orderbook"]["not_available_columns"].keys())
        + list(SOURCE_SCHEMA_AUDIT["lld"].keys()),
    }
    ensure_dirs(output_dir)
    atomic_write_json(output_dir / "dry_run_plan.json", plan)
    atomic_write_json(output_dir / "feature_specification.json", FEATURE_SPECIFICATION)
    atomic_write_json(output_dir / "source_schema_audit.json", SOURCE_SCHEMA_AUDIT)
    return {
        "output_dir": str(output_dir),
        "verdict": C.CODE_STATUS,
        "plan": plan,
        "clickhouse_queries": False,
        "clickhouse_query_count": 0,
        "exit_code": 0,
    }


def enrich_symbol_from_frames(
    *,
    symbol: str,
    checkpoint: dict[str, Any],
    candles_5m: pd.DataFrame,
    candles_1m: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
    ob_1s: pd.DataFrame | None = None,
    oi_1m: pd.DataFrame | None = None,
    liq: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Pure enrichment for one symbol (testable without DB)."""
    if is_excluded_symbol(symbol):
        return {
            "status": "SKIPPED_EXCLUDED",
            "rows": [],
            "parity": check_reference_parity(checkpoint_candidates=[], checkpoint_trades=[], symbol=symbol),
            "candidate_ids": [],
        }

    candidates = checkpoint.get("candidates") or []
    trades_ck = checkpoint.get("trades") or []
    parity_pre = check_reference_parity(
        checkpoint_candidates=candidates,
        checkpoint_trades=trades_ck,
        enriched_rows=None,
        symbol=symbol,
    )
    assert_parity_or_raise(parity_pre)

    pairs = join_candidates_trades(candidates, trades_ck)
    if not pairs:
        return {
            "status": "SKIPPED_NO_REFERENCE_TRADES",
            "rows": [],
            "parity": parity_pre,
            "candidate_ids": [],
            "coverage": {},
        }

    rows = []
    for cand, trade in pairs:
        rows.append(
            enrich_candidate_row(
                cand,
                trade,
                candles_5m=candles_5m,
                candles_1m=candles_1m,
                trades=trades,
                ob_1s=ob_1s,
                oi_1m=oi_1m,
                liq=liq,
            )
        )

    parity_post = check_reference_parity(
        checkpoint_candidates=candidates,
        checkpoint_trades=trades_ck,
        enriched_rows=rows,
        symbol=symbol,
    )
    assert_parity_or_raise(parity_post)
    return {
        "status": "COMPLETE",
        "rows": rows,
        "parity": parity_post,
        "candidate_ids": [str(c["candidate_id"]) for c, _ in pairs],
        "coverage": coverage_summary(rows),
    }


def _empty_result(
    *,
    output_dir: Path,
    verdict: str,
    exit_code: int,
    detail: dict[str, Any],
    clickhouse_query_count: int = 0,
) -> dict[str, Any]:
    payload = {
        "verdict": verdict,
        "clickhouse_queries": clickhouse_query_count > 0,
        "clickhouse_query_count": clickhouse_query_count,
        "enriched_rows": 0,
        **detail,
    }
    atomic_write_json(output_dir / "summary.json", payload)
    return {
        "output_dir": str(output_dir),
        "verdict": verdict,
        "clickhouse_queries": clickhouse_query_count > 0,
        "clickhouse_query_count": clickhouse_query_count,
        "enriched_rows": 0,
        "exit_code": exit_code,
        "detail": detail,
        **detail,
    }


def run_enrich(cfg: dict[str, Any]) -> dict[str, Any]:
    """Enrich from market DB — only called when user passes --enrich.

    Never returns CODE_READY. Success → MULTICOIN_REFERENCE_ENRICHMENT_COMPLETE.
    """
    input_dir = resolve_path(cfg["input_dir"])
    output_dir = resolve_path(cfg["output_dir"])
    paths = ensure_dirs(output_dir)
    symbols = _list_input_symbols(input_dir, cfg)

    # 1) Frozen reference inventory + hard parity BEFORE any ClickHouse call
    try:
        inventory = inventory_frozen_reference(input_dir, symbols)
    except EmptyFrozenReferenceError as e:
        return _empty_result(
            output_dir=output_dir,
            verdict=C.STATUS_EMPTY_REFERENCE,
            exit_code=2,
            detail=e.detail,
            clickhouse_query_count=0,
        )
    except ReferenceParityError as e:
        atomic_write_json(paths["failures"] / "_reference_parity.json", {"error": C.STATUS_FAILED_PARITY, "detail": e.summary})
        return _empty_result(
            output_dir=output_dir,
            verdict=C.STATUS_FAILED_PARITY,
            exit_code=2,
            detail={
                "reference_input_path": str(input_dir),
                "parity": e.summary,
                "reference_rows_before_filter": None,
                "reference_rows_after_filter": 0,
                "unique_candidate_ids": 0,
                "symbols_total": len(symbols),
                "symbols_completed": 0,
                "symbols_resumed": 0,
                "symbols_failed": 0,
                "output_files": [],
            },
            clickhouse_query_count=0,
        )

    expected_rows = int(inventory["reference_rows_after_filter"])
    symbols_with_ref = sorted(
        {sym for sym, info in inventory["per_symbol"].items() if info.get("n_pairs", 0) > 0}
    )

    # Lazy import only after reference validation passes
    from .market_loaders import load_enrichment_market_data, open_clickhouse_client

    client = open_clickhouse_client()
    clickhouse_query_count = 0
    all_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    symbols_completed = 0
    symbols_resumed = 0
    symbols_failed = 0
    symbols_skipped = 0

    for symbol in symbols_with_ref:
        ck_out = load_checkpoint(paths["checkpoints"] / f"{symbol}.json")
        if should_skip_complete(ck_out):
            rows = ck_out.get("feature_rows") or []
            all_rows.extend(rows)
            symbols_resumed += 1
            continue

        try:
            src = load_source_checkpoint(input_dir, symbol)
            market = load_enrichment_market_data(client, symbol, cfg["start"], cfg["end"])
            # candles + trades + ob + oi + liq ≈ 5 queries per symbol
            clickhouse_query_count += 5
            result = enrich_symbol_from_frames(
                symbol=symbol,
                checkpoint=src,
                candles_5m=market["candles_5m"],
                candles_1m=market.get("candles_1m"),
                trades=market["trades"],
                ob_1s=market["ob_1s"],
                oi_1m=market.get("oi_1m"),
                liq=market.get("liq"),
            )
            if result["status"] == "SKIPPED_NO_REFERENCE_TRADES":
                symbols_skipped += 1
                write_enrichment_checkpoint(
                    paths["checkpoints"],
                    symbol=symbol,
                    status=result["status"],
                    candidate_ids=[],
                    feature_rows=[],
                    coverage_summary={},
                    parity_summary=result.get("parity") or {},
                )
                continue

            write_enrichment_checkpoint(
                paths["checkpoints"],
                symbol=symbol,
                status=result["status"],
                candidate_ids=result.get("candidate_ids") or [],
                feature_rows=result.get("rows") or [],
                coverage_summary=result.get("coverage") or {},
                parity_summary=result.get("parity") or {},
            )
            all_rows.extend(result.get("rows") or [])
            if result["status"] == "COMPLETE":
                symbols_completed += 1
            else:
                symbols_failed += 1
                failures.append({"symbol": symbol, "error": result["status"]})
        except ReferenceParityError as e:
            symbols_failed += 1
            write_enrichment_checkpoint(
                paths["checkpoints"],
                symbol=symbol,
                status=C.STATUS_FAILED_PARITY,
                candidate_ids=[],
                feature_rows=[],
                coverage_summary={},
                parity_summary=e.summary,
            )
            atomic_write_json(paths["failures"] / f"{symbol}.json", {"error": C.STATUS_FAILED_PARITY, "detail": e.summary})
            failures.append({"symbol": symbol, "error": C.STATUS_FAILED_PARITY})
            return _empty_result(
                output_dir=output_dir,
                verdict=C.STATUS_FAILED_PARITY,
                exit_code=2,
                detail={
                    "reference_input_path": str(input_dir),
                    "reference_rows_before_filter": inventory["reference_rows_before_filter"],
                    "reference_rows_after_filter": inventory["reference_rows_after_filter"],
                    "unique_candidate_ids": inventory["unique_candidate_ids"],
                    "symbols_total": len(symbols_with_ref),
                    "symbols_completed": symbols_completed,
                    "symbols_resumed": symbols_resumed,
                    "symbols_failed": symbols_failed,
                    "failures": failures,
                    "parity": e.summary,
                    "output_files": [],
                },
                clickhouse_query_count=clickhouse_query_count,
            )
        except Exception as e:
            # Do not swallow: fail the run (no silent CODE_READY with empty rows)
            symbols_failed += 1
            atomic_write_json(paths["failures"] / f"{symbol}.json", {"error": str(e), "type": type(e).__name__})
            write_enrichment_checkpoint(
                paths["checkpoints"],
                symbol=symbol,
                status="FAILED",
                candidate_ids=[],
                feature_rows=[],
                coverage_summary={},
                parity_summary={},
                extra={"error": str(e), "error_type": type(e).__name__},
            )
            failures.append({"symbol": symbol, "error": str(e), "error_type": type(e).__name__})
            return _empty_result(
                output_dir=output_dir,
                verdict=C.STATUS_ENRICHMENT_FAILED,
                exit_code=1,
                detail={
                    "reference_input_path": str(input_dir),
                    "reference_rows_before_filter": inventory["reference_rows_before_filter"],
                    "reference_rows_after_filter": inventory["reference_rows_after_filter"],
                    "unique_candidate_ids": inventory["unique_candidate_ids"],
                    "symbols_total": len(symbols_with_ref),
                    "symbols_completed": symbols_completed,
                    "symbols_resumed": symbols_resumed,
                    "symbols_failed": symbols_failed,
                    "failures": failures,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "output_files": [],
                },
                clickhouse_query_count=clickhouse_query_count,
            )

    if len(all_rows) != expected_rows:
        return _empty_result(
            output_dir=output_dir,
            verdict=C.STATUS_INCOMPLETE,
            exit_code=2,
            detail={
                "reference_input_path": str(input_dir),
                "reference_rows_before_filter": inventory["reference_rows_before_filter"],
                "reference_rows_after_filter": expected_rows,
                "unique_candidate_ids": inventory["unique_candidate_ids"],
                "symbols_total": len(symbols_with_ref),
                "symbols_completed": symbols_completed,
                "symbols_resumed": symbols_resumed,
                "symbols_failed": symbols_failed,
                "symbols_skipped": symbols_skipped,
                "enriched_rows": len(all_rows),
                "failures": failures,
                "reason": f"enriched_rows={len(all_rows)} != expected={expected_rows}",
                "output_files": [],
            },
            clickhouse_query_count=clickhouse_query_count,
        )

    output_files = _write_enrichment_exports(
        output_dir,
        all_rows,
        cfg,
        failures,
        inventory=inventory,
        stats={
            "symbols_total": len(symbols_with_ref),
            "symbols_completed": symbols_completed,
            "symbols_resumed": symbols_resumed,
            "symbols_failed": symbols_failed,
            "symbols_skipped": symbols_skipped,
            "clickhouse_query_count": clickhouse_query_count,
        },
        verdict=C.STATUS_COMPLETE,
    )
    return {
        "output_dir": str(output_dir),
        "verdict": C.STATUS_COMPLETE,
        "clickhouse_queries": clickhouse_query_count > 0 or symbols_resumed > 0,
        "clickhouse_query_count": clickhouse_query_count,
        "enriched_rows": len(all_rows),
        "reference_input_path": str(input_dir),
        "reference_rows_before_filter": inventory["reference_rows_before_filter"],
        "reference_rows_after_filter": expected_rows,
        "unique_candidate_ids": inventory["unique_candidate_ids"],
        "symbols_total": len(symbols_with_ref),
        "symbols_completed": symbols_completed,
        "symbols_resumed": symbols_resumed,
        "symbols_failed": symbols_failed,
        "output_files": output_files,
        "exit_code": 0,
    }


def _write_enrichment_exports(
    output_dir: Path,
    rows: list[dict],
    cfg: dict,
    failures: list,
    *,
    inventory: dict[str, Any],
    stats: dict[str, Any],
    verdict: str,
) -> list[str]:
    ensure_dirs(output_dir)
    written: list[str] = []
    atomic_write_json(output_dir / "feature_specification.json", FEATURE_SPECIFICATION)
    written.append("feature_specification.json")
    atomic_write_json(output_dir / "source_schema_audit.json", SOURCE_SCHEMA_AUDIT)
    written.append("source_schema_audit.json")

    inv_export = {k: v for k, v in inventory.items() if k != "pairs"}
    atomic_write_json(output_dir / "reference_parity.json", inv_export)
    written.append("reference_parity.json")

    manifest = {
        "mode": "enrich",
        "verdict": verdict,
        "hashes": all_hashes(),
        "cfg": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in cfg.items() if k != "cli_argv"},
        "cli_argv": cfg.get("cli_argv"),
        "n_rows": len(rows),
        "failures": failures,
        "reference_input_path": inventory.get("reference_input_path"),
        "reference_rows_before_filter": inventory.get("reference_rows_before_filter"),
        "reference_rows_after_filter": inventory.get("reference_rows_after_filter"),
        "unique_candidate_ids": inventory.get("unique_candidate_ids"),
        "cost_pct": C.REF_COST_PCT,
        "entry_rule": C.ENTRY_RULE,
        "window": {"start": C.DEFAULT_START.isoformat(), "end": C.DEFAULT_END.isoformat()},
        "schema_version": C.CHECKPOINT_SCHEMA_VERSION,
        "expected_reference_trades_v2": C.EXPECTED_REFERENCE_TRADES_V2,
        "git": _git_meta(),
        "started_at": cfg.get("started_at"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        **stats,
    }
    atomic_write_json(output_dir / "run_manifest.json", manifest)
    written.append("run_manifest.json")

    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(output_dir / "enriched_candidates.csv", index=False)
        written.append("enriched_candidates.csv")
        df.to_csv(output_dir / "enriched_trades.csv", index=False)
        written.append("enriched_trades.csv")
        try:
            df.to_parquet(output_dir / "enriched_trades.parquet", index=False)
            written.append("enriched_trades.parquet")
        except Exception as e:
            atomic_write_json(
                output_dir / "parquet_write_error.json",
                {"error": str(e), "note": "CSV written; parquet optional dependency failed"},
            )

        from .integrity import build_integrity_report

        integrity = build_integrity_report(
            enriched=df,
            input_dir=Path(str(inventory.get("reference_input_path") or cfg.get("input_dir"))),
            output_dir=output_dir,
        )
        atomic_write_json(output_dir / "integrity_report.json", integrity)
        written.append("integrity_report.json")

        # Coverage report
        miss_fields = []
        for col in df.columns:
            if col.startswith(C.FEATURE_PREFIX) and col.endswith("__coverage_status"):
                base = col[: -len("__coverage_status")]
                n_miss = int((df[col] != "OK").sum())
                miss_fields.append({"field": base, "n_not_ok": n_miss, "share": n_miss / len(df)})
        coverage_report = {
            "n_rows": len(df),
            "n_symbols": int(df["symbol"].nunique()) if "symbol" in df.columns else None,
            "expected_reference_trades": C.EXPECTED_REFERENCE_TRADES_V2,
            "row_count_matches_expected": len(df) == C.EXPECTED_REFERENCE_TRADES_V2,
            "integrity_ok": integrity.get("ok"),
            "missing_by_field": miss_fields,
            "mfe_mae_ok_share": float((df.get("label__mfe_mae_coverage") == "OK").mean())
            if "label__mfe_mae_coverage" in df.columns
            else None,
        }
        atomic_write_json(output_dir / "coverage_report.json", coverage_report)
        written.append("coverage_report.json")

        pd.DataFrame(miss_fields).to_csv(output_dir / "missing_features_by_field.csv", index=False)
        written.append("missing_features_by_field.csv")
        by_coin = []
        for sym, g in df.groupby("symbol"):
            n_null = 0
            for col in g.columns:
                if col.startswith(C.FEATURE_PREFIX) and not col.endswith(
                    ("__coverage_status", "__missing_reason", "__causal", "__feature_asof", "__source_table")
                ):
                    n_null += int(g[col].isna().sum())
            by_coin.append({"symbol": sym, "n_rows": len(g), "n_null_feature_cells": n_null})
        pd.DataFrame(by_coin).to_csv(output_dir / "missing_features_by_coin.csv", index=False)
        written.append("missing_features_by_coin.csv")
        cov_rows = [{"symbol": r["symbol"], "candidate_id": r.get("candidate_id")} for r in rows]
        pd.DataFrame(cov_rows).to_csv(output_dir / "enrichment_coverage.csv", index=False)
        written.append("enrichment_coverage.csv")
        # Keep enrich verdict as STATUS_COMPLETE for analyze gate; V2 final verdict set in analyze.

    summary = {
        "verdict": verdict,
        "n_rows": len(rows),
        "enriched_rows": len(rows),
        "failures": failures,
        "clickhouse_queries": stats.get("clickhouse_query_count", 0) > 0,
        "clickhouse_query_count": stats.get("clickhouse_query_count", 0),
        "reference_input_path": inventory.get("reference_input_path"),
        "reference_rows_before_filter": inventory.get("reference_rows_before_filter"),
        "reference_rows_after_filter": inventory.get("reference_rows_after_filter"),
        "unique_candidate_ids": inventory.get("unique_candidate_ids"),
        "symbols_total": stats.get("symbols_total"),
        "symbols_completed": stats.get("symbols_completed"),
        "symbols_resumed": stats.get("symbols_resumed"),
        "symbols_failed": stats.get("symbols_failed"),
        "output_files": written,
        "hashes": all_hashes(),
    }
    atomic_write_json(output_dir / "summary.json", summary)
    written.append("summary.json")
    return written


def enrichment_ready_for_analyze(output_dir: Path) -> tuple[bool, str, dict[str, Any]]:
    """Require complete, hash-compatible enrichment artifacts."""
    manifest_path = output_dir / "run_manifest.json"
    csv_path = output_dir / "enriched_candidates.csv"
    summary_path = output_dir / "summary.json"
    if not manifest_path.exists() or not csv_path.exists() or not summary_path.exists():
        return False, C.STATUS_INCOMPLETE, {"reason": "missing_manifest_or_csv_or_summary"}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if manifest.get("verdict") != C.STATUS_COMPLETE and summary.get("verdict") != C.STATUS_COMPLETE:
        return False, C.STATUS_INCOMPLETE, {"reason": "verdict_not_complete", "manifest_verdict": manifest.get("verdict")}

    cur = all_hashes()
    mh = manifest.get("hashes") or {}
    for key in ("feature_definition_hash", "reference_strategy_hash", "source_schema_hash"):
        if str(mh.get(key)) != str(cur[key]):
            return False, C.STATUS_INCOMPLETE, {"reason": "hash_mismatch", "key": key}

    df = pd.read_csv(csv_path)
    if df.empty:
        return False, C.STATUS_INCOMPLETE, {"reason": "empty_enriched_csv"}

    # Checkpoints must be COMPLETE + hash-compatible for every symbol in CSV
    symbols = sorted(df["symbol"].astype(str).str.upper().unique())
    for sym in symbols:
        rec = load_checkpoint(output_dir / "checkpoints" / f"{sym}.json")
        if not should_skip_complete(rec):
            return False, C.STATUS_INCOMPLETE, {"reason": "checkpoint_incomplete_or_hash_mismatch", "symbol": sym}

    return True, "OK", {"n_rows": len(df), "n_symbols": len(symbols)}


def run_analyze(cfg: dict[str, Any]) -> dict[str, Any]:
    """Analyze enriched CSV only — no market DB."""
    output_dir = resolve_path(cfg["output_dir"])
    ok, status, detail = enrichment_ready_for_analyze(output_dir)
    if not ok:
        payload = {
            "verdict": C.STATUS_V2_FAILED,
            "enrich_gate_status": status,
            "clickhouse_queries": False,
            "clickhouse_query_count": 0,
            **detail,
        }
        ensure_dirs(output_dir)
        atomic_write_json(output_dir / "summary.json", payload)
        atomic_write_json(output_dir / "analysis_summary.json", payload)
        return {
            "output_dir": str(output_dir),
            "verdict": C.STATUS_V2_FAILED,
            "clickhouse_queries": False,
            "clickhouse_query_count": 0,
            "exit_code": 2,
            **detail,
        }

    df = pd.read_csv(output_dir / "enriched_candidates.csv")
    results = run_all_hypotheses(df)
    pd.DataFrame(results["h1"]).to_csv(output_dir / "hypothesis_h1_orderbook.csv", index=False)
    pd.DataFrame(results["h2"]).to_csv(output_dir / "hypothesis_h2_atr_quartiles.csv", index=False)
    h3_flat = []
    for r in results["h3"]:
        h3_flat.append(
            {
                "hypothesis": r["hypothesis"],
                "feature": r["feature"],
                "cliffs_delta": r["cliffs_delta"],
                "mean_feature_profitable": r["mean_feature_profitable"],
                "mean_feature_unprofitable": r["mean_feature_unprofitable"],
                "n_profitable": r["profitable"]["n_trades"],
                "n_unprofitable": r["unprofitable"]["n_trades"],
                "missing_share": r["missing_share"],
            }
        )
    pd.DataFrame(h3_flat).to_csv(output_dir / "hypothesis_h3_ema_structure.csv", index=False)
    pd.DataFrame(results["h4"]).to_csv(output_dir / "hypothesis_h4_trade_flow.csv", index=False)
    pd.DataFrame(results["loco"]).to_csv(output_dir / "leave_one_coin_out.csv", index=False)

    from .analysis_v2_report import analysis_report_md, run_v2_analysis
    from .integrity import build_integrity_report

    input_dir = resolve_path(cfg.get("input_dir") or C.DEFAULT_INPUT_DIR)
    # Prefer path recorded on enrich manifest
    man_path = output_dir / "run_manifest.json"
    if man_path.exists():
        man = json.loads(man_path.read_text(encoding="utf-8"))
        if man.get("reference_input_path"):
            input_dir = Path(man["reference_input_path"])

    integrity = build_integrity_report(enriched=df, input_dir=input_dir, output_dir=output_dir)
    atomic_write_json(output_dir / "integrity_report.json", integrity)

    analysis = run_v2_analysis(df)
    atomic_write_json(output_dir / "analysis_summary.json", analysis["summary"])
    pd.DataFrame(analysis["coin_breakdown"]).to_csv(output_dir / "coin_breakdown.csv", index=False)
    pd.DataFrame(analysis["regime_breakdown"]).to_csv(output_dir / "regime_breakdown.csv", index=False)
    pd.DataFrame(analysis["feature_comparison"]).to_csv(output_dir / "feature_comparison.csv", index=False)
    (output_dir / "analysis_report.md").write_text(analysis_report_md(analysis), encoding="utf-8")

    if "symbol" in df.columns:
        coin_rows = []
        for sym, g in df.groupby("symbol"):
            pnl = pd.to_numeric(g["label__net_pnl_usdt"], errors="coerce")
            coin_rows.append(
                {
                    "symbol": sym,
                    "n_trades": len(g),
                    "net_pnl_usdt": float(pnl.sum()),
                    "expectancy_usdt": float(pnl.mean()),
                    "net_winrate": float((pnl > 0).mean()),
                }
            )
        pd.DataFrame(coin_rows).to_csv(output_dir / "coin_level_results.csv", index=False)

    final = C.STATUS_V2_COMPLETE if integrity.get("ok") and len(df) == C.EXPECTED_REFERENCE_TRADES_V2 else C.STATUS_V2_PARTIAL
    if len(df) == 0:
        final = C.STATUS_V2_FAILED

    # Enrich git + hashes into run_manifest (append analysis section; do not wipe enrich fields)
    manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}
    manifest["analysis"] = {
        "verdict": final,
        "n_rows": len(df),
        "integrity_ok": integrity.get("ok"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest["verdict_final"] = final
    atomic_write_json(output_dir / "run_manifest.json", manifest)

    atomic_write_json(
        output_dir / "summary.json",
        {
            "verdict": final,
            "n_rows": len(df),
            "integrity_ok": integrity.get("ok"),
            "clickhouse_queries": False,
            "clickhouse_query_count": 0,
        },
    )
    (output_dir / "summary.md").write_text(
        f"# Reference enrichment analysis\n\nverdict={final}\nn_rows={len(df)}\nintegrity_ok={integrity.get('ok')}\n",
        encoding="utf-8",
    )
    return {
        "output_dir": str(output_dir),
        "verdict": final,
        "clickhouse_queries": False,
        "clickhouse_query_count": 0,
        "enriched_rows": len(df),
        "integrity_ok": integrity.get("ok"),
        "exit_code": 0 if final == C.STATUS_V2_COMPLETE else 1,
    }


def run_report_only(cfg: dict[str, Any]) -> dict[str, Any]:
    """Aggregate existing analysis artifacts only."""
    output_dir = resolve_path(cfg["output_dir"])
    artifacts = [
        "hypothesis_h1_orderbook.csv",
        "hypothesis_h2_atr_quartiles.csv",
        "hypothesis_h3_ema_structure.csv",
        "hypothesis_h4_trade_flow.csv",
        "coin_level_results.csv",
        "leave_one_coin_out.csv",
    ]
    present = {a: (output_dir / a).exists() for a in artifacts}
    stability = []
    for a in artifacts:
        p = output_dir / a
        if p.exists():
            try:
                df = pd.read_csv(p)
                stability.append({"artifact": a, "n_rows": len(df)})
            except Exception as e:
                stability.append({"artifact": a, "error": str(e)})
    pd.DataFrame(stability).to_csv(output_dir / "hypothesis_stability.csv", index=False)
    atomic_write_json(
        output_dir / "summary.json",
        {"verdict": "REPORT_ONLY", "artifacts_present": present, "clickhouse_queries": False, "clickhouse_query_count": 0},
    )
    return {
        "output_dir": str(output_dir),
        "verdict": "REPORT_ONLY",
        "clickhouse_queries": False,
        "clickhouse_query_count": 0,
        "exit_code": 0,
    }
