"""Export bundle for EMA dual-cross multi-source runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def write_export_bundle(
    bundle: dict[str, Any],
    out_dir: str | Path,
    *,
    run_config: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, str]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    candidates = bundle.get("candidates") or []
    rejected = bundle.get("rejected_ema_crosses") or []

    pd.DataFrame(candidates).to_csv(root / "candidates.csv", index=False)
    paths["candidates.csv"] = str(root / "candidates.csv")
    (root / "candidates.json").write_text(json.dumps(candidates, indent=2, default=str) + "\n", encoding="utf-8")
    paths["candidates.json"] = str(root / "candidates.json")

    allowed = [c for c in candidates if c.get("final_verdict") == "ALLOW"]
    blocked = [c for c in candidates if c.get("final_verdict") == "BLOCK"]
    inconclusive = [c for c in candidates if c.get("final_verdict") == "INCONCLUSIVE_DATA"]
    pd.DataFrame(allowed).to_csv(root / "allowed.csv", index=False)
    pd.DataFrame(blocked).to_csv(root / "blocked.csv", index=False)
    pd.DataFrame(inconclusive).to_csv(root / "inconclusive.csv", index=False)
    paths["allowed.csv"] = str(root / "allowed.csv")
    paths["blocked.csv"] = str(root / "blocked.csv")
    paths["inconclusive.csv"] = str(root / "inconclusive.csv")

    pd.DataFrame(rejected).to_csv(root / "rejected_ema_crosses.csv", index=False)
    paths["rejected_ema_crosses.csv"] = str(root / "rejected_ema_crosses.csv")

    (root / "summary.json").write_text(json.dumps(bundle.get("summary") or {}, indent=2, default=str) + "\n", encoding="utf-8")
    paths["summary.json"] = str(root / "summary.json")
    (root / "summary.md").write_text(_summary_md(bundle), encoding="utf-8")
    paths["summary.md"] = str(root / "summary.md")
    (root / "coverage.json").write_text(json.dumps(bundle.get("coverage") or {}, indent=2, default=str) + "\n", encoding="utf-8")
    paths["coverage.json"] = str(root / "coverage.json")
    if run_config:
        (root / "run_config.json").write_text(json.dumps(run_config, indent=2, default=str) + "\n", encoding="utf-8")
        paths["run_config.json"] = str(root / "run_config.json")
    if policy:
        (root / "policy.json").write_text(json.dumps(policy, indent=2, default=str) + "\n", encoding="utf-8")
        paths["policy.json"] = str(root / "policy.json")
    return paths


def _summary_md(bundle: dict[str, Any]) -> str:
    s = bundle.get("summary") or {}
    lines = [
        "# EMA Dual Cross Multi-Source Summary",
        "",
        "No profitability claim.",
        "",
        f"- Candidates: {s.get('n_candidates', 0)}",
        f"- ALLOW: {s.get('n_allow', 0)}",
        f"- BLOCK: {s.get('n_block', 0)}",
        f"- INCONCLUSIVE: {s.get('n_inconclusive', 0)}",
        f"- Rejected EMA crosses: {s.get('n_rejected_crosses', 0)}",
        "",
    ]
    return "\n".join(lines)
