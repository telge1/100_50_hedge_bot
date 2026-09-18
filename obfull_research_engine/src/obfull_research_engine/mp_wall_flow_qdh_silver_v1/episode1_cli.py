"""CLI: Episode-1 QDH on BTC Silver via unified Forschungsengine facade."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from obfull_research_engine.ob_forschungsengine_v1.analyze import analyze_episode1
from obfull_research_engine.ob_forschungsengine_v1.contract import VERDICT_EVENT_OK, default_gates
from dataclasses import replace

from . import RUN_PREFIX


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Episode-1 wall-flow/QDH on BTC Silver v1.3")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--database", default=None)
    args = p.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(__file__).resolve().parents[3] / "runs" / "mp_wall_flow_qdh_silver_v1_episode1"
    out = args.out_dir or (root / f"{RUN_PREFIX}{stamp}")
    gates = replace(default_gates(), mp_hit_pull_enrichment=False)
    kwargs: dict = {"out_dir": out, "gates": gates}
    if args.database:
        kwargs["database"] = args.database
    try:
        result = analyze_episode1(**kwargs)
    except Exception as exc:  # noqa: BLE001
        err = {"ok": False, "verdict": "STOP_MP_SILVER_QDH_BRIDGE", "error": str(exc)}
        out.mkdir(parents=True, exist_ok=True)
        (out / "run_manifest.json").write_text(json.dumps(err, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(err, indent=2))
        return 2

    qdh = result.get("qdh_base") or {}
    print(
        json.dumps(
            {
                "ok": result.get("ok"),
                "verdict": result.get("verdict"),
                "out_dir": str(out),
                "unified_package": result.get("package"),
                "anchor_qdh": qdh.get("anchor_qdh"),
            },
            indent=2,
        )
    )
    # Keep legacy exit semantics roughly: ok + EVENT verdict.
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
