"""Export helpers for sync-tolerance research runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def write_tolerance_bundle(root: Path, payload: dict[str, Any]) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    def dump_json(name: str, obj: Any) -> None:
        p = root / name
        p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        paths[name] = str(p)

    def dump_csv(name: str, rows: list[dict[str, Any]]) -> None:
        p = root / name
        pd.DataFrame(rows).to_csv(p, index=False)
        paths[name] = str(p)

    dump_json("run_manifest.json", payload.get("manifest") or {})
    dump_csv("candidates_all.csv", payload.get("candidates_all") or [])
    dump_csv("funnel.csv", payload.get("funnel") or [])
    dump_csv("outcomes_1h_4h.csv", payload.get("outcomes_rows") or [])
    dump_csv("trades_tpsl.csv", payload.get("trades_rows") or [])
    dump_json("summary_by_mode.json", payload.get("summary_by_mode") or {})
    dump_json("rejected_reuse_audit.json", payload.get("rejected_reuse_audit") or {})
    md = payload.get("summary_md") or ""
    (root / "summary.md").write_text(md, encoding="utf-8")
    paths["summary.md"] = str(root / "summary.md")
    return paths
