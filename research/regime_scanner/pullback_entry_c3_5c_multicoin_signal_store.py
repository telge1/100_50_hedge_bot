"""Multicoin A6 signal store orchestrator (MySQL-only, child-run per coin).

Architecture: one parent orchestration UUID + one research_runs child per symbol
(because research_runs.symbol is singular and research_signals has no symbol column).
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from research.regime_scanner.c35c_signal_store.build import (
    EXPECTED_A6_HASH,
    MySQLRequiredError,
    build_15m_a6,
    build_feature_rows,
    build_outcome_rows,
    build_signal_rows,
    check_fill_parity,
    load_symbol_5m_mysql,
    resolve_analyze_window,
    sha1_ohlcv,
    strip_internal,
)
from research.regime_scanner.c35c_signal_store.multicoin_import import (
    DEFAULT_SYMBOLS,
    SOFT_EXPECTED_FILLS,
    run_multicoin_import,
)
from research.regime_scanner.c35c_signal_store.schema import (
    C35C_SIGNAL_SCHEMA_VERSION,
    SCANNER_NAME_A6_STORE,
)
from research.regime_scanner.c35c_signal_store.store import C35cSignalStore
from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import fixed_chrono_splits
from research.regime_scanner.research_runs.git_info import collect_git_info
from research.regime_scanner.research_runs.hashing import json_hash
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/multicoin_signal_feature_store_20260722")
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)
DEFAULT_APT_REF = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_fill_excursion_audit/fill_excursion_panel.csv"
)


def child_run_label(parent_label: str, symbol: str) -> str:
    return f"{parent_label}__{symbol.upper()}"


def process_symbol_bundle(
    symbol: str,
    *,
    feature_version: str,
    outcome_version: str,
    common_start: pd.Timestamp | None = None,
    common_end: pd.Timestamp | None = None,
    full_history: bool = True,
) -> dict[str, Any]:
    mysql_5m, mysql_meta = load_symbol_5m_mysql(symbol)
    if full_history and common_start is None:
        a0, a1 = resolve_analyze_window(mysql_5m)
    else:
        a0 = common_start or pd.Timestamp("2026-01-26T00:00:00+00:00")
        a1 = common_end or pd.Timestamp("2026-06-28T00:00:00+00:00")
        if a0.tzinfo is None:
            a0 = a0.tz_localize("UTC")
        if a1.tzinfo is None:
            a1 = a1.tz_localize("UTC")
    frame, fills, lives, a6_meta = build_15m_a6(mysql_5m, analyze_start=a0, analyze_end_exclusive=a1)
    splits = fixed_chrono_splits(a0, a1)
    signal_rows_raw = build_signal_rows(fills, lives, frame, splits=splits, symbol=symbol)
    feature_rows = build_feature_rows(frame, signal_rows_raw, feature_version=feature_version)
    outcome_rows = build_outcome_rows(frame, signal_rows_raw, outcome_version=outcome_version)
    signal_rows = strip_internal(signal_rows_raw)

    soft_exp = SOFT_EXPECTED_FILLS.get(symbol.upper())
    soft_ok = soft_exp is None or int(len(fills)) == int(soft_exp)
    apt_parity = None
    if symbol.upper() == "APTUSDT" and DEFAULT_APT_REF.exists():
        # Compare fill times/prices to reference (signal_key format may differ)
        apt_parity = check_fill_parity(fills, pd.read_csv(DEFAULT_APT_REF))

    return {
        "ok": True,
        "symbol": symbol.upper(),
        "mysql_meta": mysql_meta,
        "a6_meta": a6_meta,
        "analyze_start": a0.isoformat(),
        "analyze_end_exclusive": a1.isoformat(),
        "n_fills": len(fills),
        "n_signals": len(signal_rows),
        "n_features": len(feature_rows),
        "n_outcomes": len(outcome_rows),
        "soft_expected_fills": soft_exp,
        "soft_fill_match": soft_ok,
        "apt_parity": None if apt_parity is None else {k: v for k, v in apt_parity.items() if k != "rows"},
        "candle_hash_5m": sha1_ohlcv(mysql_5m),
        "signal_rows": signal_rows,
        "feature_rows": feature_rows,
        "outcome_rows": outcome_rows,
        "parity_ok": True if apt_parity is None else bool(apt_parity.get("ok")),
    }


def persist_child(
    store: C35cSignalStore,
    *,
    parent_id: str,
    parent_label: str,
    bundle: dict[str, Any],
    feature_version: str,
    outcome_version: str,
    data_source: str,
) -> dict[str, Any]:
    symbol = bundle["symbol"]
    label = child_run_label(parent_label, symbol)
    existing = store.find_completed_run_by_label(label)
    if existing:
        return {
            "ok": False,
            "status": "already_exists",
            "symbol": symbol,
            "run_id": existing["run_id"],
            "run_label": label,
        }

    params = {
        "scanner_name": SCANNER_NAME_A6_STORE,
        "scanner_version": "a6_multicoin_signal_store_v1",
        "strategy_name": "c35c_pullback_entry",
        "variant": "A6",
        "strategy_config_hash": EXPECTED_A6_HASH,
        "feature_version": feature_version,
        "outcome_version": outcome_version,
        "run_label": label,
        "parent_run_label": parent_label,
        "parent_run_id": parent_id,
        "symbol": symbol,
        "timeframe": "15m",
        "data_source": data_source,
    }
    param_hash = json_hash(params)
    param_set_id = store.ensure_parameter_set(
        parameter_hash=param_hash, scanner_name=SCANNER_NAME_A6_STORE, params=params
    )
    g = collect_git_info()
    run_id = str(uuid.uuid4())
    fp = json_hash(
        {
            "parent_run_id": parent_id,
            "run_label": label,
            "symbol": symbol,
            "strategy_config_hash": EXPECTED_A6_HASH,
            "feature_version": feature_version,
            "outcome_version": outcome_version,
            "candle_hash_5m": bundle["candle_hash_5m"],
            "analyze_start": bundle["analyze_start"],
            "analyze_end": bundle["analyze_end_exclusive"],
            "schema_version": C35C_SIGNAL_SCHEMA_VERSION,
        }
    )
    metadata = {
        "run_label": label,
        "parent_run_label": parent_label,
        "parent_run_id": parent_id,
        "strategy_name": "c35c_pullback_entry",
        "variant": "A6",
        "strategy_config_hash": EXPECTED_A6_HASH,
        "feature_version": feature_version,
        "outcome_version": outcome_version,
        "schema_version": C35C_SIGNAL_SCHEMA_VERSION,
        "symbol_set": [symbol],
        "n_fills": bundle["n_fills"],
        "mysql_meta": bundle["mysql_meta"],
        "a6_meta": bundle["a6_meta"],
        "soft_fill_match": bundle.get("soft_fill_match"),
    }
    run_row = {
        "run_id": run_id,
        "run_fingerprint": fp,
        "parameter_set_id": param_set_id,
        "exchange": "bybit",
        "symbol": symbol,
        "data_source": data_source,
        "start_time": pd.Timestamp(bundle["analyze_start"]),
        "end_time": pd.Timestamp(bundle["analyze_end_exclusive"]),
        "warmup_start": pd.Timestamp(bundle["analyze_start"]) - pd.Timedelta(days=2),
        "decision_time": pd.Timestamp(bundle["analyze_end_exclusive"]),
        "started_at": pd.Timestamp.utcnow(),
        "duration_seconds": None,
        "git_commit": g.commit,
        "git_branch": g.branch,
        "working_tree_dirty": g.working_tree_dirty,
        "candle_hash_5m": bundle["candle_hash_5m"],
        "candle_hash_15m": None,
        "candle_hash_30m": None,
        "signal_hash": json_hash(
            [
                {"k": s["signal_key"], "p": s["entry_price"], "t": str(s["entry_time"])}
                for s in bundle["signal_rows"]
            ]
        ),
        "combined_output_hash": json_hash(
            {
                "n_signals": bundle["n_signals"],
                "n_features": bundle["n_features"],
                "n_outcomes": bundle["n_outcomes"],
            }
        ),
        "metadata_json": metadata,
    }
    metrics = [
        {"metric_name": "n_signals", "metric_value": float(bundle["n_signals"]), "metric_text": None},
        {"metric_name": "n_features", "metric_value": float(bundle["n_features"]), "metric_text": None},
        {"metric_name": "n_outcomes", "metric_value": float(bundle["n_outcomes"]), "metric_text": None},
    ]
    result = store.persist_bundle(
        run_row=run_row,
        signals=bundle["signal_rows"],
        features=bundle["feature_rows"],
        outcomes=bundle["outcome_rows"],
        metrics=metrics,
    )
    return {
        "ok": True,
        "status": "persisted",
        "symbol": symbol,
        "run_id": result["run_id"],
        "run_label": label,
        "n_signals": result["n_signals"],
        "n_features": result["n_features"],
        "n_outcomes": result["n_outcomes"],
        "run_fingerprint": fp,
    }


def run_multicoin_signal_store(
    *,
    symbols: Sequence[str],
    regime_db_env: Path,
    feature_version: str,
    outcome_version: str,
    run_label: str,
    output_dir: Path,
    dry_run: bool = True,
    persist: bool = False,
    fail_if_existing: bool = True,
    continue_on_symbol_error: bool = True,
    full_history: bool = True,
    common_window: bool = False,
    skip_import: bool = False,
    import_only: bool = False,
) -> dict[str, Any]:
    if persist:
        dry_run = False
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Phase: import inventory / optional import
    if not skip_import:
        imp = run_multicoin_import(
            symbols=symbols,
            regime_db_env=regime_db_env,
            output_dir=output_dir,
            dry_run=True,  # always dry-run first
        )
        if not dry_run and not import_only:
            # real import for import_required
            imp = run_multicoin_import(
                symbols=symbols,
                regime_db_env=regime_db_env,
                output_dir=output_dir,
                dry_run=False,
            )
        ready = list(imp.get("ready_symbols") or [])
    else:
        load_regime_db_env_file(regime_db_env)
        ready = list(symbols)
        imp = {"skipped": True, "ready_symbols": ready}

    if import_only:
        meta = {"ok": True, "status": "import_only", "import": imp}
        (output_dir / "multicoin_run_metadata.json").write_text(
            json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
        )
        return meta

    parent_id = str(uuid.uuid4())
    g = collect_git_info()
    bundles: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    count_rows: list[dict[str, Any]] = []
    parity_rows: list[dict[str, Any]] = []
    feat_rows: list[dict[str, Any]] = []
    out_rows: list[dict[str, Any]] = []

    # optional common window pass 1: discover windows
    windows: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
    if common_window:
        for sym in ready:
            try:
                mysql_5m, _ = load_symbol_5m_mysql(sym)
                a0, a1 = resolve_analyze_window(mysql_5m)
                windows.append((sym, a0, a1))
            except Exception as exc:  # noqa: BLE001
                failed.append({"symbol": sym, "stage": "window", "error": str(exc)})
        if windows:
            cw0 = max(w[1] for w in windows)
            cw1 = min(w[2] for w in windows)
        else:
            cw0 = cw1 = None
    else:
        cw0 = cw1 = None

    for sym in ready:
        try:
            b = process_symbol_bundle(
                sym,
                feature_version=feature_version,
                outcome_version=outcome_version,
                common_start=cw0 if common_window else None,
                common_end=cw1 if common_window else None,
                full_history=full_history and not common_window,
            )
            if sym.upper() == "APTUSDT" and b.get("apt_parity") and not b["apt_parity"].get("ok"):
                failed.append({"symbol": sym, "stage": "apt_parity", "error": b["apt_parity"]})
                if not continue_on_symbol_error:
                    break
                continue
            if not b.get("parity_ok", True):
                failed.append({"symbol": sym, "stage": "parity", "error": "parity_failed"})
                if not continue_on_symbol_error:
                    break
                continue
            bundles.append(b)
            count_rows.append(
                {
                    "symbol": sym,
                    "n_fills": b["n_fills"],
                    "soft_expected_fills": b.get("soft_expected_fills"),
                    "soft_fill_match": b.get("soft_fill_match"),
                    "analyze_start": b["analyze_start"],
                    "analyze_end_exclusive": b["analyze_end_exclusive"],
                    "n_5m": b["mysql_meta"].get("n_5m"),
                }
            )
            parity_rows.append(
                {
                    "symbol": sym,
                    "parity_ok": b.get("parity_ok"),
                    "soft_fill_match": b.get("soft_fill_match"),
                    "apt_parity_ok": None if not b.get("apt_parity") else b["apt_parity"].get("ok"),
                    "n_fills": b["n_fills"],
                }
            )
            feat_rows.append({"symbol": sym, "n_features": b["n_features"], "n_trigger": b["n_signals"], "n_fill_stage": b["n_signals"]})
            out_rows.append({"symbol": sym, "n_outcomes": b["n_outcomes"]})
        except Exception as exc:  # noqa: BLE001
            failed.append({"symbol": sym, "stage": "build", "error": f"{type(exc).__name__}: {exc}"})
            if not continue_on_symbol_error:
                break

    pd.DataFrame(count_rows).to_csv(output_dir / "multicoin_signal_counts.csv", index=False)
    pd.DataFrame(parity_rows).to_csv(output_dir / "multicoin_signal_parity.csv", index=False)
    pd.DataFrame(feat_rows).to_csv(output_dir / "multicoin_feature_counts.csv", index=False)
    pd.DataFrame(out_rows).to_csv(output_dir / "multicoin_outcome_counts.csv", index=False)
    # merge failed with import failed
    fail_path = output_dir / "multicoin_failed_symbols.csv"
    if fail_path.exists() and fail_path.stat().st_size > 0:
        try:
            prev_failed = pd.read_csv(fail_path)
        except Exception:  # noqa: BLE001
            prev_failed = pd.DataFrame()
    else:
        prev_failed = pd.DataFrame()
    pd.concat([prev_failed, pd.DataFrame(failed)], ignore_index=True).to_csv(fail_path, index=False)

    # export combined CSVs for audit without DB
    all_sigs = []
    all_feats = []
    all_outs = []
    for b in bundles:
        for s in b["signal_rows"]:
            all_sigs.append(
                {
                    "symbol": b["symbol"],
                    "signal_key": s["signal_key"],
                    "side": s["direction"],
                    "setup_id": s["setup_id"],
                    "trigger_timestamp": s["timestamp"],
                    "fill_timestamp": s["entry_time"],
                    "entry_price": s["entry_price"],
                    **(s.get("metadata_json") or {}),
                }
            )
        for f in b["feature_rows"]:
            all_feats.append({"symbol": b["symbol"], **f})
        for o in b["outcome_rows"]:
            all_outs.append({"symbol": b["symbol"], **o})
    pd.DataFrame(all_sigs).to_csv(output_dir / "research_signals_export.csv", index=False)
    pd.DataFrame(all_feats).to_csv(output_dir / "research_signal_features_export.csv", index=False)
    pd.DataFrame(all_outs).to_csv(output_dir / "research_signal_outcomes_export.csv", index=False)

    expected_writes = {
        "n_child_runs": len(bundles),
        "n_signals": sum(b["n_signals"] for b in bundles),
        "n_features": sum(b["n_features"] for b in bundles),
        "n_outcomes": sum(b["n_outcomes"] for b in bundles),
    }
    run_meta = {
        "ok": True,
        "status": "dry_run" if dry_run else "ready",
        "parent_run_id": parent_id,
        "parent_run_label": run_label,
        "run_model": "child_run_per_symbol",
        "run_model_reason": "research_runs.symbol is singular; research_signals has no symbol column",
        "strategy_config_hash": EXPECTED_A6_HASH,
        "feature_version": feature_version,
        "outcome_version": outcome_version,
        "schema_version": C35C_SIGNAL_SCHEMA_VERSION,
        "full_history": full_history,
        "common_window": common_window,
        "common_start": None if cw0 is None else cw0.isoformat(),
        "common_end": None if cw1 is None else cw1.isoformat(),
        "git_commit": g.commit,
        "symbols_requested": list(symbols),
        "symbols_ready": [b["symbol"] for b in bundles],
        "symbols_failed": failed,
        "expected_writes": expected_writes,
        "import": {k: v for k, v in (imp or {}).items() if k not in {"inventory", "parity"}},
        "persisted": False,
        "pine_unchanged": True,
        "a6_unchanged": True,
        "no_filter_activated": True,
    }
    (output_dir / "multicoin_run_metadata.json").write_text(
        json.dumps(json_safe(run_meta), indent=2) + "\n", encoding="utf-8"
    )

    if dry_run or not persist:
        summary = {
            "ok": True,
            "status": "dry_run",
            "persisted": False,
            "wrote_db": False,
            **expected_writes,
            "parent_run_id": parent_id,
        }
        (output_dir / "multicoin_db_persist_summary.json").write_text(
            json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
        )
        return {**run_meta, **summary}

    # Persist children
    load_regime_db_env_file(regime_db_env)
    store = C35cSignalStore(load_regime_db_config())
    persist_results = []
    try:
        store.init_schema()
        # parent-level already_exists if all children exist
        if fail_if_existing:
            existing_children = [
                store.find_completed_run_by_label(child_run_label(run_label, b["symbol"]))
                for b in bundles
            ]
            if existing_children and all(x is not None for x in existing_children):
                summary = {
                    "ok": False,
                    "status": "already_exists",
                    "persisted": False,
                    "parent_run_label": run_label,
                    "message": "all child runs already exist",
                    "child_run_ids": [x["run_id"] for x in existing_children if x],
                }
                (output_dir / "multicoin_db_persist_summary.json").write_text(
                    json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
                )
                return summary

        for b in bundles:
            if not b.get("parity_ok", True):
                continue
            try:
                pr = persist_child(
                    store,
                    parent_id=parent_id,
                    parent_label=run_label,
                    bundle=b,
                    feature_version=feature_version,
                    outcome_version=outcome_version,
                    data_source="mysql",
                )
                persist_results.append(pr)
            except Exception as exc:  # noqa: BLE001
                persist_results.append(
                    {
                        "ok": False,
                        "symbol": b["symbol"],
                        "status": "persist_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                if not continue_on_symbol_error:
                    break
    finally:
        store.close()

    n_ok = sum(1 for r in persist_results if r.get("ok"))
    summary = {
        "ok": n_ok > 0,
        "status": "persisted" if n_ok == len(bundles) else "partial_persist",
        "persisted": n_ok > 0,
        "wrote_db": n_ok > 0,
        "parent_run_id": parent_id,
        "parent_run_label": run_label,
        "n_child_persisted": n_ok,
        "n_child_requested": len(bundles),
        "children": persist_results,
        "n_signals": sum(r.get("n_signals") or 0 for r in persist_results if r.get("ok")),
        "n_features": sum(r.get("n_features") or 0 for r in persist_results if r.get("ok")),
        "n_outcomes": sum(r.get("n_outcomes") or 0 for r in persist_results if r.get("ok")),
    }
    run_meta["persisted"] = summary["persisted"]
    run_meta["status"] = summary["status"]
    run_meta["persist_summary"] = summary
    (output_dir / "multicoin_run_metadata.json").write_text(
        json.dumps(json_safe(run_meta), indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "multicoin_db_persist_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
    )
    return {**run_meta, **summary}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Multicoin C3.5c A6 signal store (MySQL)")
    p.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    p.add_argument("--symbols-file", type=Path, default=None)
    p.add_argument("--data-source", default="mysql", choices=["mysql"])
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--feature-version", default="c35c_entry_features_v1")
    p.add_argument("--outcome-version", default="tp3_sl2_h192_cost020_v1")
    p.add_argument("--run-label", default="multicoin_a6_signal_store_20260722")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--persist", action="store_true", default=False)
    p.add_argument("--fail-if-existing", action="store_true", default=False)
    p.add_argument("--continue-on-symbol-error", action="store_true", default=True)
    p.add_argument("--full-history", action="store_true", default=True)
    p.add_argument("--common-window", action="store_true", default=False)
    p.add_argument("--skip-import", action="store_true", default=False)
    p.add_argument("--import-only", action="store_true", default=False)
    args = p.parse_args(list(argv) if argv is not None else None)

    symbols = list(args.symbols)
    if args.symbols_file is not None:
        symbols = [
            ln.strip().upper()
            for ln in args.symbols_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]

    try:
        meta = run_multicoin_signal_store(
            symbols=symbols,
            regime_db_env=args.regime_db_env,
            feature_version=args.feature_version,
            outcome_version=args.outcome_version,
            run_label=args.run_label,
            output_dir=args.output_dir,
            dry_run=not args.persist,
            persist=args.persist,
            fail_if_existing=args.fail_if_existing,
            continue_on_symbol_error=args.continue_on_symbol_error,
            full_history=args.full_history and not args.common_window,
            common_window=args.common_window,
            skip_import=args.skip_import,
            import_only=args.import_only,
        )
    except MySQLRequiredError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    print(
        json.dumps(
            json_safe(
                {
                    "ok": meta.get("ok"),
                    "status": meta.get("status"),
                    "persisted": meta.get("persisted"),
                    "n_signals": meta.get("n_signals") or meta.get("expected_writes", {}).get("n_signals"),
                    "symbols_ready": meta.get("symbols_ready"),
                    "out": str(args.output_dir),
                }
            )
        )
    )
    return 0 if meta.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
