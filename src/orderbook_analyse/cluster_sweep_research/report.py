"""Markdown / CSV / JSON report writers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .cluster_adapter import LLD_AUDIT, CausalVerdict
from .models import SetupDirection, SweepEvent


def write_reports(
    out_dir: Path,
    events: Sequence[SweepEvent],
    *,
    coverage: dict[str, Any],
    lld_verdict: CausalVerdict,
    symbol: str,
    timeframe: str,
    window: dict[str, str],
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "window": window,
        "lld_verdict": lld_verdict.value,
        "lld_audit": LLD_AUDIT,
        "n_events": len(events),
        "n_bullish": sum(1 for e in events if e.setup_direction == SetupDirection.BULLISH),
        "n_bearish": sum(1 for e in events if e.setup_direction == SetupDirection.BEARISH),
        "events": [e.to_dict() for e in events],
        "coverage": coverage,
    }
    p_json = out_dir / "events.json"
    p_json.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    paths["events.json"] = str(p_json)

    rows = []
    for e in events:
        rows.append(
            {
                "event_id": e.event_id,
                "direction": e.setup_direction.value,
                "cluster_id": e.cluster.cluster_id,
                "cluster_side": e.cluster.side,
                "pool_count": e.cluster.pool_count,
                "t_entry_touch": e.t_entry,
                "t_earliest_entry": e.t_earliest_entry,
                "states": "|".join(s.value for s in e.states),
                "confirm_close_in_cluster": (e.confirmations.get("CLOSE_BACK_IN_CLUSTER") or {}).get("fired"),
                "confirm_ema59": (e.confirmations.get("CLOSE_RECLAIM_EMA59") or {}).get("fired"),
                "confirm_combo": (e.confirmations.get("CLUSTER_AND_EMA_RECLAIM") or {}).get("fired"),
            }
        )
    p_csv = out_dir / "events_summary.csv"
    pd.DataFrame(rows).to_csv(p_csv, index=False)
    paths["events_summary.csv"] = str(p_csv)

    p_cov = out_dir / "coverage.json"
    p_cov.write_text(json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8")
    paths["coverage.json"] = str(p_cov)

    conf_counts = {}
    for e in events:
        for k, v in e.confirmations.items():
            if v.get("fired"):
                conf_counts[k] = conf_counts.get(k, 0) + 1

    md = [
        f"# Cluster Sweep Research — {symbol} {timeframe}\n",
        f"- LLD verdict: **{lld_verdict.value}**\n",
        f"- Events: {len(events)} (bullish={payload['n_bullish']}, bearish={payload['n_bearish']})\n",
        f"- Confirmation counts: `{conf_counts}`\n",
        "- No profitability claim on this smoke sample.\n",
        "\n## LLD reuse\n\n",
        f"- Engine: `{LLD_AUDIT['engine_file']}`\n",
        f"- Clusters: `{LLD_AUDIT['cluster_file']}`\n",
        f"- Causal: {LLD_AUDIT['causal']} / Repaint: {LLD_AUDIT['repaint']}\n",
        f"- Chart number meaning: {LLD_AUDIT['chart_number']}\n",
    ]
    p_md = out_dir / "ANALYSIS_SUMMARY.md"
    p_md.write_text("".join(md), encoding="utf-8")
    paths["ANALYSIS_SUMMARY.md"] = str(p_md)
    return paths
