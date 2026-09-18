"""CLI for unified OB Forschungsengine analysis."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import RUN_PREFIX
from .analyze import analyze_episode1
from .batch_pilot import run_mp_qdh_pilot
from .contract import VERDICT_EVENT_OK, default_gates


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Unified OB Forschungsengine analysis")
    p.add_argument(
        "--mode",
        choices=("episode1", "mp-pilot"),
        default="episode1",
        help="episode1 | mp-pilot (stratified UPPER ask-wall QDH scale-out)",
    )
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--database", default=None)
    p.add_argument("--max-events", type=int, default=20)
    p.add_argument(
        "--role",
        choices=("UPPER", "LOWER"),
        default="UPPER",
        help="MP event role filter for mp-pilot (UPPER=ask, LOWER=bid)",
    )
    p.add_argument("--batch-run-dir", type=Path, default=None)
    p.add_argument(
        "--no-mp-enrichment",
        action="store_true",
        help="Disable MP HIT/PULL enrichment gate (QDH-only)",
    )
    p.add_argument(
        "--with-mp-enrichment",
        action="store_true",
        help="Enable MP HIT/PULL enrichment (heavier CH load)",
    )
    args = p.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # .../obfull_research_engine/src/obfull_research_engine/ob_forschungsengine_v1/run_cli.py
    # parents[3] = .../obfull_research_engine  (runs/)
    # parents[4] = repo root
    pkg_runs = Path(__file__).resolve().parents[3] / "runs" / "ob_forschungsengine_v1"
    repo_root = Path(__file__).resolve().parents[4]
    out = args.out_dir or (pkg_runs / f"{RUN_PREFIX}{args.mode.replace('-', '_')}_{stamp}")

    try:
        if args.mode == "episode1":
            gates = default_gates()
            if args.no_mp_enrichment or not args.with_mp_enrichment:
                gates = replace(gates, mp_hit_pull_enrichment=False)
            kwargs: dict = {"out_dir": out, "gates": gates}
            if args.database:
                kwargs["database"] = args.database
            result = analyze_episode1(**kwargs)
            slim = {
                "ok": result.get("ok"),
                "verdict": result.get("verdict"),
                "out_dir": str(out),
                "gates_active": result.get("gates_active"),
                "gates_deferred": result.get("gates_deferred"),
                "anchor_qdh": (result.get("qdh_base") or {}).get("anchor_qdh"),
                "elapsed_s": result.get("elapsed_s"),
            }
            print(json.dumps(slim, indent=2))
            return 0 if result.get("ok") and result.get("verdict") == VERDICT_EVENT_OK else 1

        if args.mode == "mp-pilot":
            kwargs = {
                "repo_root": repo_root,
                "out_dir": out,
                "max_events": int(args.max_events),
                "role": str(args.role),
                "enable_mp_enrichment": bool(args.with_mp_enrichment),
            }
            if args.batch_run_dir:
                kwargs["batch_run_dir"] = args.batch_run_dir
            if args.database:
                kwargs["database"] = args.database
            result = run_mp_qdh_pilot(**kwargs)
            print(
                json.dumps(
                    {
                        "ok": result.get("ok"),
                        "out_dir": result.get("out_dir"),
                        "manifest": {
                            k: (result.get("manifest") or {}).get(k)
                            for k in (
                                "n_selected",
                                "n_ok",
                                "n_fail",
                                "elapsed_s",
                                "verdicts",
                                "label_ok",
                            )
                        },
                    },
                    indent=2,
                )
            )
            return 0 if result.get("ok") else 1

        raise RuntimeError(f"unsupported mode {args.mode}")
    except Exception as exc:  # noqa: BLE001
        err = {"ok": False, "verdict": "STOP_OB_FORSCHUNGSENGINE", "error": str(exc)}
        out.mkdir(parents=True, exist_ok=True)
        (out / "analysis_manifest.json").write_text(
            json.dumps(err, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(err, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
