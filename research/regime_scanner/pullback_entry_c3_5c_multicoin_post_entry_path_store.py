"""Multicoin post-entry path checkpoint store (MySQL, additive).

Reconstructs causal checkpoints 1..4 after A6 fill for child runs under a parent label.
Does not modify signals / features / outcomes. No A6 / Pine changes.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.c35c_signal_store.path_build import (
    build_path_labels_for_panel,
    compute_checkpoints_for_signal,
    load_frame_for_symbol,
)
from research.regime_scanner.c35c_signal_store.path_schema import (
    CHECKPOINT_SEMANTICS,
    C35C_PATH_SCHEMA_VERSION,
    DEFAULT_CHECKPOINT_BARS,
    DEFAULT_PATH_VERSION,
)
from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/multicoin_post_entry_path_store_20260722")
DEFAULT_ENV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)
DEFAULT_OUTCOME_VERSION = "tp3_sl2_h192_cost020_v1"


def _symbol_from_run(run: dict[str, Any]) -> str:
    sym = run.get("symbol")
    if sym:
        return str(sym).upper()
    meta = run.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    if isinstance(meta, dict):
        label = str(meta.get("run_label") or "")
        if "__" in label:
            return label.rsplit("__", 1)[-1].upper()
        if meta.get("symbol"):
            return str(meta["symbol"]).upper()
    raise ValueError(f"cannot resolve symbol for run {run.get('run_id')}")


def process_child_run(
    store: C35cPathStore,
    run: dict[str, Any],
    *,
    path_version: str,
    checkpoints: tuple[int, ...],
    outcome_version: str,
) -> dict[str, Any]:
    run_id = str(run["run_id"])
    symbol = _symbol_from_run(run)
    t0 = time.time()
    sigs, outcomes, trig, fill = store.load_signals_bundle(run_id, outcome_version=outcome_version)
    frame, frame_meta = load_frame_for_symbol(symbol)
    cp_rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for sig in sigs:
        sid = int(sig["id"])
        rows = compute_checkpoints_for_signal(
            signal=sig,
            outcome=outcomes.get(sid),
            trigger_feat=trig.get(sid),
            fill_feat=fill.get(sid),
            frame=frame,
            path_version=path_version,
            checkpoints=checkpoints,
        )
        cp_rows.extend(rows)
        for r in rows:
            if r.get("availability") != "ok":
                missing.append(
                    {
                        "symbol": symbol,
                        "signal_id": sid,
                        "signal_key": sig.get("signal_key"),
                        "checkpoint_bar": r.get("checkpoint_bar"),
                        "availability": r.get("availability"),
                    }
                )

    panel_rows = []
    for sig in sigs:
        sid = int(sig["id"])
        oc = outcomes.get(sid) or {}
        panel_rows.append(
            {
                "signal_id": sid,
                "run_id": run_id,
                "exit_reason": oc.get("exit_reason"),
                "bars_held": oc.get("bars_held"),
                "bars_to_tp": oc.get("bars_to_tp"),
                "bars_to_sl": oc.get("bars_to_sl"),
                "net_pnl_pct": oc.get("net_pnl_pct"),
                "mfe_pct": oc.get("mfe_pct"),
                "mae_pct": oc.get("mae_pct"),
            }
        )
    ok_cp = sum(1 for r in cp_rows if r.get("availability") == "ok")
    return {
        "symbol": symbol,
        "run_id": run_id,
        "n_signals": len(sigs),
        "n_checkpoints": len(cp_rows),
        "n_checkpoints_ok": ok_cp,
        "n_missing": len(missing),
        "checkpoints": cp_rows,
        "panel_rows": panel_rows,
        "missing_rows": missing,
        "frame_meta": frame_meta,
        "elapsed_s": round(time.time() - t0, 3),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent-run-label", required=True)
    p.add_argument("--path-version", default=DEFAULT_PATH_VERSION)
    p.add_argument("--checkpoints", nargs="+", type=int, default=list(DEFAULT_CHECKPOINT_BARS))
    p.add_argument("--data-source", default="mysql", choices=["mysql"])
    p.add_argument("--regime-db-env", type=Path, default=DEFAULT_ENV)
    p.add_argument("--outcome-version", default=DEFAULT_OUTCOME_VERSION)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--persist", action="store_true")
    p.add_argument("--fail-if-existing", action="store_true")
    p.add_argument("--continue-on-symbol-error", action="store_true")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--symbols", nargs="*", default=None, help="Optional symbol filter")
    args = p.parse_args(argv)

    if args.persist and args.dry_run:
        raise SystemExit("use either --dry-run or --persist")
    if not args.persist and not args.dry_run:
        args.dry_run = True

    assert_safe_output_dir(args.output_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    load_regime_db_env_file(Path(args.regime_db_env))
    cfg = load_regime_db_config()
    store = C35cPathStore(cfg)
    store.init_schema()

    children = store.find_child_runs(args.parent_run_label)
    if args.symbols:
        want = {s.upper() for s in args.symbols}
        children = [c for c in children if _symbol_from_run(c) in want]
    if not children:
        raise SystemExit(f"no child runs for parent label {args.parent_run_label}")

    cps = tuple(sorted(set(int(x) for x in args.checkpoints)))
    inventory: list[dict[str, Any]] = []
    all_missing: list[dict[str, Any]] = []
    cp_count_rows: list[dict[str, Any]] = []
    persist_summaries: list[dict[str, Any]] = []
    dry_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    built: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    all_panel: list[dict[str, Any]] = []

    for run in children:
        symbol = _symbol_from_run(run)
        try:
            if args.persist and args.fail_if_existing:
                n_ex = store.count_path_checkpoints(
                    run_id=str(run["run_id"]), path_version=args.path_version
                )
                if n_ex:
                    already.append(
                        {
                            "symbol": symbol,
                            "run_id": str(run["run_id"]),
                            "status": "already_exists",
                            "n_checkpoints": n_ex,
                            "n_labels": store.count_path_labels(
                                run_id=str(run["run_id"]), path_version=args.path_version
                            ),
                        }
                    )
                    inventory.append(
                        {
                            "symbol": symbol,
                            "run_id": str(run["run_id"]),
                            "status": "already_exists",
                            "n_checkpoints_existing": n_ex,
                            "n_labels_existing": store.count_path_labels(
                                run_id=str(run["run_id"]), path_version=args.path_version
                            ),
                        }
                    )
                    continue

            result = process_child_run(
                store,
                run,
                path_version=args.path_version,
                checkpoints=cps,
                outcome_version=args.outcome_version,
            )
            built.append(result)
            all_panel.extend(result["panel_rows"])
            all_missing.extend(result["missing_rows"])
            for cp in cps:
                ok = sum(
                    1
                    for r in result["checkpoints"]
                    if int(r["checkpoint_bar"]) == cp and r.get("availability") == "ok"
                )
                miss = sum(
                    1
                    for r in result["checkpoints"]
                    if int(r["checkpoint_bar"]) == cp and r.get("availability") != "ok"
                )
                cp_count_rows.append(
                    {
                        "symbol": symbol,
                        "checkpoint_bar": cp,
                        "n_ok": ok,
                        "n_missing": miss,
                        "n_total": ok + miss,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            err = {
                "symbol": symbol,
                "run_id": str(run.get("run_id")),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
            errors.append(err)
            inventory.append({**err, "status": "error"})
            if not args.continue_on_symbol_error:
                raise

    # Global quantile path labels (matches prior multicoin audit semantics).
    all_labels = (
        build_path_labels_for_panel(pd.DataFrame(all_panel), path_version=args.path_version)
        if all_panel
        else []
    )
    labels_by_run: dict[str, list[dict[str, Any]]] = {}
    for lab in all_labels:
        labels_by_run.setdefault(str(lab["run_id"]), []).append(lab)

    for result in built:
        symbol = result["symbol"]
        labs = labels_by_run.get(result["run_id"], [])
        inventory.append(
            {
                "symbol": symbol,
                "run_id": result["run_id"],
                "n_signals": result["n_signals"],
                "n_checkpoints": result["n_checkpoints"],
                "n_checkpoints_ok": result["n_checkpoints_ok"],
                "n_missing": result["n_missing"],
                "n_labels": len(labs),
                "elapsed_s": result["elapsed_s"],
                "status": "built",
            }
        )
        dry_rows.append(
            {
                "symbol": symbol,
                "run_id": result["run_id"],
                "n_signals": result["n_signals"],
                "n_checkpoints": result["n_checkpoints"],
                "n_labels": len(labs),
                "would_persist": bool(args.persist),
            }
        )
        if args.persist:
            summary = store.persist_path_bundle(
                checkpoints=result["checkpoints"],
                labels=labs,
                fail_if_existing=bool(args.fail_if_existing),
            )
            summary["symbol"] = symbol
            persist_summaries.append(summary)
        else:
            persist_summaries.append(
                {
                    "symbol": symbol,
                    "run_id": result["run_id"],
                    "status": "dry_run",
                    "n_checkpoints": result["n_checkpoints"],
                    "n_labels": len(labs),
                }
            )
    persist_summaries = already + persist_summaries

    # If idempotent short-circuit emptied build outputs, re-export inventory from DB.
    if already and not built:
        run_ids = [str(a["run_id"]) for a in already]
        cps_db = store.load_checkpoints(path_version=args.path_version, run_ids=run_ids)
        labs_db = store.load_labels(path_version=args.path_version, run_ids=run_ids)
        all_labels = [
            {
                "signal_id": int(r["signal_id"]),
                "run_id": str(r["run_id"]),
                "path_version": str(r["path_version"]),
                "path_type": str(r["path_type"]),
            }
            for r in labs_db
        ]
        all_missing = [
            {
                "symbol": next(
                    (a["symbol"] for a in already if a["run_id"] == str(r["run_id"])),
                    "",
                ),
                "signal_id": int(r["signal_id"]),
                "checkpoint_bar": int(r["checkpoint_bar"]),
                "availability": r.get("availability"),
            }
            for r in cps_db
            if r.get("availability") != "ok"
        ]
        cp_count_rows = []
        for a in already:
            rows = [r for r in cps_db if str(r["run_id"]) == a["run_id"]]
            for cp in cps:
                sub = [r for r in rows if int(r["checkpoint_bar"]) == cp]
                cp_count_rows.append(
                    {
                        "symbol": a["symbol"],
                        "checkpoint_bar": cp,
                        "n_ok": sum(1 for r in sub if r.get("availability") == "ok"),
                        "n_missing": sum(1 for r in sub if r.get("availability") != "ok"),
                        "n_total": len(sub),
                    }
                )
            dry_rows.append(
                {
                    "symbol": a["symbol"],
                    "run_id": a["run_id"],
                    "n_signals": int(a.get("n_labels") or 0),
                    "n_checkpoints": int(a.get("n_checkpoints") or 0),
                    "n_labels": int(a.get("n_labels") or 0),
                    "would_persist": False,
                    "status": "already_exists",
                }
            )

    pd.DataFrame(inventory).to_csv(out_dir / "path_store_inventory.csv", index=False)
    pd.DataFrame(cp_count_rows).to_csv(out_dir / "path_checkpoint_counts.csv", index=False)
    pd.DataFrame(all_missing).to_csv(out_dir / "path_checkpoint_missing.csv", index=False)
    pd.DataFrame(
        [
            {
                "signal_id": r["signal_id"],
                "run_id": r["run_id"],
                "path_version": r["path_version"],
                "path_type": r["path_type"],
            }
            for r in all_labels
        ]
    ).to_csv(out_dir / "path_labels.csv", index=False)
    pd.DataFrame(dry_rows).to_csv(out_dir / "path_store_dry_run.csv", index=False)
    summary = {
        "parent_run_label": args.parent_run_label,
        "path_version": args.path_version,
        "schema_version": C35C_PATH_SCHEMA_VERSION,
        "checkpoints": list(cps),
        "semantics": CHECKPOINT_SEMANTICS,
        "mode": "persist" if args.persist else "dry_run",
        "n_children": len(children),
        "n_errors": len(errors),
        "persist_summaries": persist_summaries,
        "errors": errors,
    }
    (out_dir / "path_store_persist_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2), encoding="utf-8"
    )
    (out_dir / "path_store_metadata.json").write_text(
        json.dumps(
            json_safe(
                {
                    **summary,
                    "outcome_version": args.outcome_version,
                    "data_source": args.data_source,
                    "no_a6_change": True,
                    "no_pine_change": True,
                    "reuse": [
                        "pullback_entry_c3_5d_post_entry.step_post_entry",
                        "pullback_entry_c3_5c_multicoin_signal_failure_feature_audit.path_types",
                        "c35c_signal_store.build.build_15m_a6",
                    ],
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    store.close()
    print(json.dumps(json_safe(summary), indent=2))
    return 1 if errors and not args.continue_on_symbol_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
