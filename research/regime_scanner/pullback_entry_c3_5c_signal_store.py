"""CLI: APT A6 signal store — MySQL primary, dry-run default, parity-gated persist."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from research.regime_scanner.c35c_signal_store.build import (
    ANALYZE_END_EXCLUSIVE,
    ANALYZE_START,
    EXPECTED_A6_HASH,
    EXPECTED_N_FILLS,
    MySQLRequiredError,
    build_15m_a6,
    build_feature_rows,
    build_outcome_rows,
    build_signal_rows,
    check_fill_parity,
    load_symbol_5m_mysql,
    sha1_ohlcv,
    strip_internal,
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

DEFAULT_OUT = Path("research/regime_scanner/results/apt_signal_feature_store_20260722")
DEFAULT_REF = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_fill_excursion_audit/fill_excursion_panel.csv"
)
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)


def _write_schema_docs(out: Path) -> None:
    (out / "schema_analysis.md").write_text(
        "\n".join(
            [
                "# Schema Analysis — C3.5c Signal Store",
                "",
                "## Existing (reused)",
                "- `research_runs` — run metadata, hashes, git, status",
                "- `research_parameter_sets` — config dedup by parameter_hash",
                "- `research_signals` — A6 fills mapped into existing columns + metadata_json",
                "- `research_run_metrics` — counts",
                "",
                "## Existing (not used for A6 fills)",
                "- `research_trend_states` / `research_structure_events` — baseline scanner timeline",
                "",
                "## New (additive CREATE TABLE IF NOT EXISTS)",
                "- `research_signal_features` — versioned trigger/fill snapshots",
                "- `research_signal_outcomes` — versioned exit-model outcomes",
                "",
                "## research_signals mapping",
                "| Column | A6 meaning |",
                "|---|---|",
                "| timestamp | trigger close time |",
                "| entry_time | fill (next open) time |",
                "| entry_price | next-open fill price |",
                "| direction | long/short |",
                "| signal_type | c35c_a6_fill |",
                "| setup_id | A6 setup_id |",
                "| metadata_json | lifecycle / ages / opposite arm / split |",
                "",
                "## Unique keys",
                "- signals: `(run_id, signal_key)` with `signal_key=a6|{side}|{fill_ts}|{setup_id}`",
                "- features: `(signal_id, feature_version, feature_stage)`",
                "- outcomes: `(signal_id, outcome_version, exit_model, tp, sl, horizon, cost)`",
                "",
                f"Schema version: `{C35C_SIGNAL_SCHEMA_VERSION}`",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "migration_plan.md").write_text(
        "\n".join(
            [
                "# Migration Plan",
                "",
                "1. No ALTER of existing research_* tables.",
                "2. No DROP / truncate of historical data.",
                "3. Additive DDL via `C35cSignalStore.init_schema()`:",
                "   - `research_signal_features`",
                "   - `research_signal_outcomes`",
                "4. Technique: project-standard `CREATE TABLE IF NOT EXISTS` (not Alembic).",
                "5. Idempotent init; safe to re-run.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def build_run_fingerprint(
    *,
    run_label: str,
    symbol: str,
    strategy_config_hash: str,
    feature_version: str,
    outcome_version: str,
    data_source: str,
    candle_hash_5m: str,
    analyze_start: str,
    analyze_end: str,
) -> str:
    return json_hash(
        {
            "run_label": run_label,
            "scanner_name": SCANNER_NAME_A6_STORE,
            "symbol": symbol,
            "strategy_config_hash": strategy_config_hash,
            "feature_version": feature_version,
            "outcome_version": outcome_version,
            "data_source": data_source,
            "candle_hash_5m": candle_hash_5m,
            "analyze_start": analyze_start,
            "analyze_end": analyze_end,
            "schema_version": C35C_SIGNAL_SCHEMA_VERSION,
        }
    )


def run_signal_store(
    *,
    symbol: str,
    data_source: str,
    regime_db_env: Path,
    feature_version: str,
    outcome_version: str,
    run_label: str,
    output_dir: Path,
    reference_panel: Path,
    dry_run: bool = True,
    persist: bool = False,
    fail_if_existing: bool = False,
    init_schema: bool = True,
) -> dict[str, Any]:
    if data_source != "mysql":
        raise ValueError("only --data-source mysql is supported (no feather fallback)")
    if persist and dry_run:
        # persist implies not dry-run
        dry_run = False
    if not persist:
        dry_run = True

    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_schema_docs(output_dir)

    load_regime_db_env_file(regime_db_env)
    cfg = load_regime_db_config()
    store = C35cSignalStore(cfg)
    try:
        if init_schema:
            store.init_schema()

        existing = store.find_completed_run_by_label(run_label)
        if existing and fail_if_existing and persist:
            summary = {
                "ok": False,
                "status": "already_exists",
                "run_id": existing["run_id"],
                "run_label": run_label,
                "message": "completed run with this label already exists; refuse overwrite",
                "persisted": False,
            }
            # Do not overwrite a prior successful persist summary if present.
            prior = output_dir / "db_persist_summary.json"
            if prior.exists():
                try:
                    prev = json.loads(prior.read_text(encoding="utf-8"))
                    if prev.get("status") == "persisted" and prev.get("persisted"):
                        prev["idempotency_recheck"] = summary
                        prior.write_text(json.dumps(json_safe(prev), indent=2) + "\n", encoding="utf-8")
                        return summary
                except Exception:  # noqa: BLE001
                    pass
            prior.write_text(json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8")
            return summary

        t0 = time.perf_counter()
        mysql_5m, mysql_meta = load_symbol_5m_mysql(symbol)
        a0 = pd.Timestamp(ANALYZE_START)
        a1 = pd.Timestamp(ANALYZE_END_EXCLUSIVE)
        frame, fills, lives, a6_meta = build_15m_a6(mysql_5m, analyze_start=a0, analyze_end_exclusive=a1)

        ref = pd.read_csv(reference_panel)
        parity = check_fill_parity(fills, ref)
        pd.DataFrame(parity.get("rows") or [{"ok": parity["ok"]}]).to_csv(
            output_dir / "signal_parity.csv", index=False
        )
        if not parity["ok"]:
            meta = {
                "ok": False,
                "status": "parity_failed",
                "parity": {k: v for k, v in parity.items() if k != "rows"},
                "a6": a6_meta,
                "mysql_meta": mysql_meta,
                "persisted": False,
                "pine_unchanged": True,
                "a6_unchanged": True,
            }
            (output_dir / "run_metadata.json").write_text(
                json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
            )
            (output_dir / "db_persist_summary.json").write_text(
                json.dumps(json_safe({"ok": False, "status": "parity_failed", "persisted": False}), indent=2)
                + "\n",
                encoding="utf-8",
            )
            return meta

        splits = fixed_chrono_splits(a0, a1)
        signal_rows_raw = build_signal_rows(fills, lives, frame, splits=splits)
        feature_rows = build_feature_rows(frame, signal_rows_raw, feature_version=feature_version)
        outcome_rows = build_outcome_rows(frame, signal_rows_raw, outcome_version=outcome_version)
        signal_rows = strip_internal(signal_rows_raw)

        # exports
        sig_export = pd.DataFrame(
            [
                {
                    "signal_key": s["signal_key"],
                    "side": s["direction"],
                    "setup_id": s["setup_id"],
                    "trigger_timestamp": s["timestamp"],
                    "fill_timestamp": s["entry_time"],
                    "entry_price": s["entry_price"],
                    **(s.get("metadata_json") or {}),
                }
                for s in signal_rows
            ]
        )
        sig_export.to_csv(output_dir / "research_signals_export.csv", index=False)
        pd.DataFrame(feature_rows).to_csv(output_dir / "research_signal_features_export.csv", index=False)
        pd.DataFrame(outcome_rows).to_csv(output_dir / "research_signal_outcomes_export.csv", index=False)

        candle_hash = sha1_ohlcv(mysql_5m)
        g = collect_git_info()
        fp = build_run_fingerprint(
            run_label=run_label,
            symbol=symbol.upper(),
            strategy_config_hash=EXPECTED_A6_HASH,
            feature_version=feature_version,
            outcome_version=outcome_version,
            data_source="mysql",
            candle_hash_5m=candle_hash,
            analyze_start=ANALYZE_START,
            analyze_end=ANALYZE_END_EXCLUSIVE,
        )
        if existing is None:
            by_fp = store.find_run_by_fingerprint(fp)
            if by_fp and fail_if_existing and persist:
                summary = {
                    "ok": False,
                    "status": "already_exists",
                    "run_id": by_fp["run_id"],
                    "run_label": run_label,
                    "message": "completed run with identical fingerprint exists",
                }
                (output_dir / "db_persist_summary.json").write_text(
                    json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
                )
                return summary

        params = {
            "scanner_name": SCANNER_NAME_A6_STORE,
            "scanner_version": "a6_signal_store_v1",
            "strategy_name": "c35c_pullback_entry",
            "variant": "A6",
            "strategy_config_hash": EXPECTED_A6_HASH,
            "feature_version": feature_version,
            "outcome_version": outcome_version,
            "run_label": run_label,
            "symbol": symbol.upper(),
            "timeframe": "15m",
            "data_source": "mysql",
        }
        param_hash = json_hash(params)
        run_id = str(uuid.uuid4())
        duration = time.perf_counter() - t0
        metadata = {
            "run_label": run_label,
            "strategy_name": "c35c_pullback_entry",
            "variant": "A6",
            "strategy_config_hash": EXPECTED_A6_HASH,
            "feature_version": feature_version,
            "outcome_version": outcome_version,
            "schema_version": C35C_SIGNAL_SCHEMA_VERSION,
            "database_name": cfg.name,
            "symbol_set": [symbol.upper()],
            "timeframe": "15m",
            "n_fills": EXPECTED_N_FILLS,
            "parity_ok": True,
            "mysql_meta": mysql_meta,
            "a6_meta": a6_meta,
            "dry_run": dry_run,
            "persist": persist,
        }
        run_row = {
            "run_id": run_id,
            "run_fingerprint": fp,
            "parameter_set_id": None,  # filled if persist
            "exchange": "bybit",
            "symbol": symbol.upper(),
            "data_source": "mysql",
            "start_time": a0,
            "end_time": a1,
            "warmup_start": a0 - pd.Timedelta(days=2),
            "decision_time": a1,
            "started_at": pd.Timestamp.utcnow(),
            "duration_seconds": duration,
            "git_commit": g.commit,
            "git_branch": g.branch,
            "working_tree_dirty": g.working_tree_dirty,
            "candle_hash_5m": candle_hash,
            "candle_hash_15m": None,
            "candle_hash_30m": None,
            "signal_hash": json_hash(
                [{"k": s["signal_key"], "p": s["entry_price"], "t": str(s["entry_time"])} for s in signal_rows]
            ),
            "combined_output_hash": json_hash(
                {
                    "n_signals": len(signal_rows),
                    "n_features": len(feature_rows),
                    "n_outcomes": len(outcome_rows),
                    "signal_hash": "see_signal_hash",
                }
            ),
            "metadata_json": metadata,
        }

        run_meta = {
            "ok": True,
            "status": "dry_run" if dry_run else "ready_to_persist",
            "run_id": run_id,
            "run_label": run_label,
            "run_fingerprint": fp,
            "n_signals": len(signal_rows),
            "n_features": len(feature_rows),
            "n_outcomes": len(outcome_rows),
            "parity": {k: v for k, v in parity.items() if k != "rows"},
            "mysql_meta": mysql_meta,
            "a6_meta": a6_meta,
            "feature_version": feature_version,
            "outcome_version": outcome_version,
            "persisted": False,
            "pine_unchanged": True,
            "a6_unchanged": True,
            "metadata": metadata,
        }
        (output_dir / "run_metadata.json").write_text(
            json.dumps(json_safe(run_meta), indent=2) + "\n", encoding="utf-8"
        )

        if dry_run or not persist:
            summary = {
                "ok": True,
                "status": "dry_run",
                "persisted": False,
                "wrote_db": False,
                "n_signals": len(signal_rows),
                "n_features": len(feature_rows),
                "n_outcomes": len(outcome_rows),
                "run_label": run_label,
                "run_fingerprint": fp,
            }
            (output_dir / "db_persist_summary.json").write_text(
                json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
            )
            return {**run_meta, **summary}

        # persist
        if existing:
            summary = {
                "ok": False,
                "status": "already_exists",
                "persisted": False,
                "run_id": existing["run_id"],
                "message": "refusing overwrite of existing completed run_label",
            }
            (output_dir / "db_persist_summary.json").write_text(
                json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
            )
            return summary

        param_set_id = store.ensure_parameter_set(
            parameter_hash=param_hash, scanner_name=SCANNER_NAME_A6_STORE, params=params
        )
        run_row["parameter_set_id"] = param_set_id
        metrics = [
            {"metric_name": "n_signals", "metric_value": float(len(signal_rows)), "metric_text": None},
            {"metric_name": "n_features", "metric_value": float(len(feature_rows)), "metric_text": None},
            {"metric_name": "n_outcomes", "metric_value": float(len(outcome_rows)), "metric_text": None},
            {"metric_name": "n_winners", "metric_value": float(sum(1 for o in outcome_rows if o.get("is_winner"))), "metric_text": None},
        ]
        try:
            result = store.persist_bundle(
                run_row=run_row,
                signals=signal_rows,
                features=feature_rows,
                outcomes=outcome_rows,
                metrics=metrics,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                store.mark_failed(run_id, error_type=type(exc).__name__, error_message=str(exc))
            except Exception:  # noqa: BLE001
                pass
            summary = {
                "ok": False,
                "status": "persist_failed",
                "persisted": False,
                "error": f"{type(exc).__name__}: {exc}",
                "run_id": run_id,
            }
            (output_dir / "db_persist_summary.json").write_text(
                json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
            )
            raise

        summary = {
            "ok": True,
            "status": "persisted",
            "persisted": True,
            "wrote_db": True,
            "run_id": result["run_id"],
            "n_signals": result["n_signals"],
            "n_features": result["n_features"],
            "n_outcomes": result["n_outcomes"],
            "run_label": run_label,
            "run_fingerprint": fp,
        }
        run_meta["persisted"] = True
        run_meta["status"] = "persisted"
        run_meta["db"] = summary
        (output_dir / "run_metadata.json").write_text(
            json.dumps(json_safe(run_meta), indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / "db_persist_summary.json").write_text(
            json.dumps(json_safe(summary), indent=2) + "\n", encoding="utf-8"
        )
        return {**run_meta, **summary}
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c A6 APT signal feature/outcome store")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--data-source", default="mysql", choices=["mysql"])
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--feature-version", default="c35c_entry_features_v1")
    p.add_argument("--outcome-version", default="tp3_sl2_h192_cost020_v1")
    p.add_argument("--run-label", default="apt_a6_signal_store_20260722")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--reference-panel", type=Path, default=DEFAULT_REF)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--persist", action="store_true", default=False)
    p.add_argument("--fail-if-existing", action="store_true", default=False)
    p.add_argument("--no-init-schema", action="store_true", default=False)
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        meta = run_signal_store(
            symbol=args.symbol,
            data_source=args.data_source,
            regime_db_env=args.regime_db_env,
            feature_version=args.feature_version,
            outcome_version=args.outcome_version,
            run_label=args.run_label,
            output_dir=args.output_dir,
            reference_panel=args.reference_panel,
            dry_run=not args.persist,
            persist=args.persist,
            fail_if_existing=args.fail_if_existing,
            init_schema=not args.no_init_schema,
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
                    "n_signals": meta.get("n_signals"),
                    "run_id": meta.get("run_id"),
                    "out": str(args.output_dir),
                }
            )
        )
    )
    return 0 if meta.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
