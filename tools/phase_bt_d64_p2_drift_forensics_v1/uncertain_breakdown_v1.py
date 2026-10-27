"""Classify BT-D63 UNCERTAIN rows by cause."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def breakdown() -> list[dict[str, Any]]:
    path = ROOT / "results/phase_bt_d63_extended_timestamps_v1/causal_gate_decisions.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("P2") != "UNCERTAIN":
            continue
        reason = r.get("P2_reason") or ""
        pool = r.get("pool_validity") or ""
        cand = r.get("baseline_upper_candidate")
        cause = "ambiguous_other"
        if reason == "NO_VERIFIED_UPPER_CANDIDATE_PROXY":
            cause = "missing_upper_candidate_proxy"
        elif pool == "UNAVAILABLE":
            cause = "missing_canonical_density_collect"
        elif r.get("ema_validity") != "VALIDATED_TRP_D60":
            cause = "ema_history_not_validated"
        elif "POOL" in reason:
            cause = "missing_pool_for_r4_r5"
        out.append(
            {
                "decision_close_utc": r["decision_close_utc"],
                "window_id": r.get("window_id"),
                "evidence_class": r.get("evidence_class"),
                "P2_reason": reason,
                "pool_validity": pool,
                "baseline_upper_candidate": cand,
                "uncertain_cause": cause,
            }
        )
    return out


def stride_audit() -> list[dict[str, Any]]:
    from datetime import datetime, timedelta, timezone

    def parse(s: str):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    windows = __import__("json").loads(
        (ROOT / "results/phase_bt_d63_extended_timestamps_v1/selected_windows.json").read_text()
    )
    out = []
    for w in windows:
        if int(w.get("bar_stride") or 1) <= 1:
            continue
        s, e = parse(w["start_utc"]), parse(w["end_utc"])
        all_bars = []
        t = s
        while t <= e:
            all_bars.append(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
            t += timedelta(minutes=5)
        stride = int(w["bar_stride"])
        sampled = all_bars[::stride]
        skipped = [b for b in all_bars if b not in sampled]
        out.append(
            {
                "window_id": w["window_id"],
                "bar_stride": stride,
                "total_5m_bars": len(all_bars),
                "sampled_bars": len(sampled),
                "skipped_bars": len(skipped),
                "skipped_fraction": round(len(skipped) / max(len(all_bars), 1), 4),
            }
        )
    return out
