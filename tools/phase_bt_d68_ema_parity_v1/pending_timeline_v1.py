"""Build pool→order timeline from BT-D67 artifacts + optional gate CSV."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D67 = ROOT / "results/phase_bt_d67_canonical_ema_replay_v1"


def _read(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_timeline() -> list[dict[str, Any]]:
    candidates = _read(D67 / "baseline_pool_candidates.csv")
    ema_shadow = _read(D67 / "ema_gate_at_candidates.csv")
    lifecycle = _read(D67 / "pending_lifecycle.csv")
    ref = _read(D67 / "reference_case_audit.csv")

    ema_by_ts: dict[str, list[dict]] = {}
    for r in ema_shadow:
        ema_by_ts.setdefault(r["decision_close_utc"], []).append(r)

    life_by_ts: dict[str, list[dict]] = {}
    for r in lifecycle:
        life_by_ts.setdefault(r["decision_close_utc"], []).append(r)

    ref_by_ts = {r["decision_close_utc"]: r for r in ref}

    timeline: list[dict[str, Any]] = []
    cand_ts = {c["decision_close_utc"] for c in candidates}
    all_ts = sorted(set(cand_ts) | set(ref_by_ts) | {r["decision_close_utc"] for r in ema_shadow})

    for ts in all_ts:
        pool_rows = [c for c in candidates if c["decision_close_utc"] == ts]
        has_pool = bool(pool_rows)
        ema_rows = ema_by_ts.get(ts, [])
        research_ema = next((e for e in ema_rows if e.get("track") == "shadow_research_ready"), ema_rows[0] if ema_rows else None)
        ref_row = ref_by_ts.get(ts)
        ema_at_ts = ref_row["actual_ema"] if ref_row else (research_ema or {}).get("ema_decision")
        events = life_by_ts.get(ts, [])
        timeline.append(
            {
                "decision_close_utc": ts,
                "baseline_pool_candidate": has_pool,
                "pool_key": pool_rows[0]["pool_key"] if pool_rows else "",
                "pilot_id": pool_rows[0].get("pilot_id", "") if pool_rows else "",
                "ema_decision_at_close": ema_at_ts,
                "ema_reason_reference_audit": ref_row.get("reason_code") if ref_row else "",
                "ema_authorizes_research_track": (research_ema or {}).get("ema_authorizes"),
                "pause_state_shadow": (research_ema or {}).get("pause_state"),
                "pending_events": "|".join(e["event"] for e in events),
                "pending_create_this_close": any(e["event"] == "CREATE" for e in events),
            }
        )

    return timeline


def sep11_explanation() -> str:
    lines = [
        "# Sep-11 pilot — zero Pending explanation",
        "",
        "## Facts (BT-D67 `baseline_pool_candidates.csv`)",
        "- **Only one** baseline `detect_short_signal` candidate in 11:30–13:30 UTC: **`2026-09-11T12:00:00Z`**.",
        "- **No** pool candidate at **12:05** or **12:30** (EMA reference labels at those times are **orthogonal** to pool eligibility).",
        "",
        "## 12:00 UTC",
        "- Pool candidate: **yes**.",
        "- Pure EMA (BT-D65): **UNCERTAIN** (`FALLING_EMAS_PRICE_RECLAIM`) — blocks new Pending even in `RESEARCH_READY` track.",
        "- Default pause: `SHORT_PAUSED` (also blocks).",
        "",
        "## 12:05 UTC",
        "- Pure EMA: **SHORT_OK** (`PRICE_BELOW_FALLING_EMA59_200`) per frozen classifier on TRP OHLC.",
        "- Baseline pool signal: **no** — no entry/SL/TP candidate; EMA alone does not create orders.",
        "",
        "## 12:30 UTC",
        "- Pure EMA: **UNCERTAIN**.",
        "- Baseline pool signal: **no**.",
        "",
        "## Conclusion",
        "Zero Pending is explained by **(1) single pool candidate at 12:00 with EMA UNCERTAIN**, and **(2) no pool candidate at 12:05** despite EMA SHORT_OK. This is **not** proof that EMA blocked a full setup at 12:05.",
        "",
        "## Note on historical P2 / R6",
        "BT-D64/D62 **P2 SHORT_OK** at 16:30 used **R5/R6 pool regime**, not the pure EMA classifier. BT-D65 and BT-D67 both label 16:30 **UNCERTAIN** on EMA-only rules.",
    ]
    return "\n".join(lines)
