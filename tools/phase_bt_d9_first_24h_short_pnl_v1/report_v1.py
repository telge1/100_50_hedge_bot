"""Generate PHASE_BT_D9 markdown report from result artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(out_dir: Path, report_path: Path) -> None:
    pf0 = _load(out_dir / "preflight_initial.json")
    pf1 = _load(out_dir / "preflight_final.json")
    cache = _load(out_dir / "cache_build.json")
    verify = _load(out_dir / "cache_verify.json")
    bt = _load(out_dir / "backtest_result.json")
    verdict = _load(out_dir / "final_verdict.json").get("verdict", "BT_D9_24H_PNL_INCOMPLETE")
    pnl = bt.get("pnl") or {}

    lines = [
        "# PHASE BT-D9 — First Complete 24h Depth2 Short PnL Backtest V1",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        "**Strategy label:** `EXPERIMENTAL_NOT_FINAL_APPROVED` — `pool_landscape_v1_candidate`, TP `next_eligible_band`.",
        "",
        "**Window (half-open):** DOGEUSDT `2026-09-11T12:00:00Z` → `2026-09-12T12:00:00Z` (end exclusive).",
        "",
        "> Technical completion is **not** proof of profitable strategy.",
        "",
        "## 1. Preflight",
        "",
        f"- Initial: `{pf0.get('verdict', 'n/a')}` blockers={pf0.get('blockers', [])}",
        f"- Final (post-cache): `{pf1.get('verdict', 'n/a')}`",
        "",
        "## 2. Cache integrity",
        "",
        f"- Build: {json.dumps(cache, indent=0) if cache else 'existing bundle'}",
        f"- Verify: {json.dumps(verify, indent=0) if verify else 'n/a'}",
        "",
        "## 3. Activity counts",
        "",
        f"- Signals: **{bt.get('signals', 'n/a')}**",
        f"- Orders: **{bt.get('orders', 'n/a')}**",
        f"- Trades: **{bt.get('trades', 'n/a')}**",
        "",
        "## 4. Gesamt-PnL",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
    ]
    for k, v in pnl.items():
        if k in ("equity_curve", "trade_ledger"):
            continue
        lines.append(f"| {k} | {v} |")

    lines.extend(["", "## 5. Trade ledger", "", "```json", json.dumps(pnl.get("trade_ledger", []), indent=2), "```"])
    lines.extend(["", "## 6. Equity / drawdown", "", "```json", json.dumps(pnl.get("equity_curve", []), indent=2), "```"])
    lines.extend(
        [
            "",
            "## 7. Config",
            "",
            f"- Config hash: `{bt.get('config_hash', 'n/a')}`",
            f"- Experiment: `{bt.get('experiment_dir', 'n/a')}`",
            "",
            "## 8. Runtime",
            "",
            f"- Backtest wall: **{bt.get('backtest_wall_s', 'n/a')} s**",
            "",
            "## 9. Limitations",
            "",
            "- Depth-2 exact unique-origin density via validated BT-D8 cache only.",
            "- TP cluster user rule (`dense_cluster`) not executed.",
            "- Single symbol, fixed 24h window, no optimization.",
            "",
            "## 10. Density provenance",
            "",
            "```json",
            json.dumps(bt.get("density_provenance", {}), indent=2),
            "```",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
