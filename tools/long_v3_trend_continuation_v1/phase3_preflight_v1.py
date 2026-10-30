"""LONG V3 Phase 3 multi-month preflight (read-only)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from contracts_v1 import BARS_PER_HOUR_5M, MIN_POOL_LOOKBACK_HOURS
from coverage_preflight_v1 import run_coverage_preflight
from multi_bundle_density_session_v1 import MultiBundleDensitySessionV1
from pool_history_gate_v1 import evaluate_pool_history_gate

ROOT = Path(__file__).resolve().parents[2]

PHASE3_MONTH_SPECS: list[dict[str, Any]] = [
    {
        "month_key": "2026-05",
        "label": "may",
        "bundle_dir": None,
        "expected": "BLOCKED",
    },
    {
        "month_key": "2026-06",
        "label": "june",
        "bundle_rel": "results/phase_bt_d94_june_2026_frozen_v1/cache_bundle",
        "expected": "FULL",
        "month_end_utc": "2026-06-30T23:55:00Z",
    },
    {
        "month_key": "2026-07",
        "label": "july",
        "bundle_rel": "results/phase_bt_d85_july_2026_no_pause_research_v1/cache_bundle",
        "expected": "FULL",
        "month_end_utc": "2026-07-31T23:55:00Z",
    },
    {
        "month_key": "2026-08",
        "label": "august",
        "bundle_rel": "results/phase_bt_d91_august_2026_frozen_v1/cache_bundle",
        "expected": "FULL",
        "month_end_utc": "2026-08-31T23:55:00Z",
    },
    {
        "month_key": "2026-09",
        "label": "september",
        "bundle_rel": "results/phase_bt_d92_september_2026_frozen_v1/cache_bundle",
        "expected": "PARTIAL",
        "month_end_utc": "2026-09-30T23:55:00Z",
        "published_last_close": "2026-09-30T23:40:00Z",
    },
]

REQUIRED_BARS = MIN_POOL_LOOKBACK_HOURS * BARS_PER_HOUR_5M


@dataclass
class Phase3PreflightResultV1:
    ok: bool
    verdict: str
    block_reason: str | None
    included_months: list[str] = field(default_factory=list)
    excluded_months: list[dict[str, Any]] = field(default_factory=list)
    month_audit: list[dict[str, Any]] = field(default_factory=list)
    merged_closes: int = 0
    merged_first_close: str | None = None
    merged_last_close: str | None = None
    eval_start_index: int = 0
    eval_start_close_utc: str | None = None
    timeline_gaps: list[str] = field(default_factory=list)
    git_head: str | None = None
    yaml_path: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _expected_5m_step(prev_iso: str, nxt_iso: str) -> bool:
    from ohlc_loader_v1 import _iso, _parse_utc

    return _iso(_parse_utc(prev_iso) + timedelta(minutes=5)) == nxt_iso


def run_phase3_preflight(*, yaml_path: Path) -> Phase3PreflightResultV1:
    from density_session_adapter_v1 import FrozenBundleDensitySessionV1
    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch

    if not yaml_path.is_file():
        return Phase3PreflightResultV1(
            ok=False,
            verdict="BLOCKED",
            block_reason="YAML_MISSING",
            yaml_path=str(yaml_path),
        )

    month_audit: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    bundle_pairs: list[tuple[str, Path]] = []

    for spec in PHASE3_MONTH_SPECS:
        label = spec["label"]
        if spec.get("bundle_dir") is None and spec.get("bundle_rel") is None:
            month_audit.append(
                {
                    "month_key": spec["month_key"],
                    "label": label,
                    "coverage_verdict": "BLOCKED",
                    "reason": "NO_ACTIVE_SSOT_BUNDLE",
                }
            )
            excluded.append({"month_key": spec["month_key"], "reason": "NO_ACTIVE_SSOT_BUNDLE"})
            continue

        rel = spec.get("bundle_rel")
        if not rel:
            continue
        bpath = ROOT / rel
        audit: dict[str, Any] = {
            "month_key": spec["month_key"],
            "label": label,
            "bundle_path": str(bpath),
        }
        if not bpath.is_dir():
            audit["coverage_verdict"] = "BLOCKED"
            audit["reason"] = "BUNDLE_DIR_MISSING"
            month_audit.append(audit)
            excluded.append({"month_key": spec["month_key"], "reason": "BUNDLE_DIR_MISSING"})
            continue

        sess = FrozenBundleDensitySessionV1(bpath)
        closes = sess.decision_closes()
        first_c, last_c = closes[0], closes[-1]
        audit["decision_close_first"] = first_c
        audit["decision_close_last"] = last_c
        audit["entry_count"] = len(closes)

        if spec["expected"] == "PARTIAL":
            pub_last = spec.get("published_last_close")
            if pub_last and last_c != pub_last:
                audit["published_last_mismatch"] = pub_last
            audit["coverage_verdict"] = "PARTIAL"
            audit["note"] = "Published causal SSOT ends before calendar month end (tail closes missing)"
            if pub_last and last_c == pub_last:
                audit["missing_tail"] = [
                    "2026-09-30T23:45:00Z",
                    "2026-09-30T23:50:00Z",
                    "2026-09-30T23:55:00Z",
                ]
        else:
            expected_end = spec.get("month_end_utc")
            if expected_end and last_c != expected_end:
                audit["coverage_verdict"] = "PARTIAL"
                audit["reason"] = f"EXPECTED_LAST_{expected_end}_GOT_{last_c}"
            else:
                audit["coverage_verdict"] = "FULL"

        gate_at_month_start = evaluate_pool_history_gate(close_index=0)
        audit["pool_history_at_month_start_index_0"] = asdict(gate_at_month_start)
        audit["prehistory_note"] = (
            "Month bundle starts at calendar boundary; 168h lookback uses prior bars "
            "within merged timeline (continuous run), not prior-month SSOT."
        )
        month_audit.append(audit)
        bundle_pairs.append((spec["month_key"], bpath))

    if len(bundle_pairs) < 4:
        return Phase3PreflightResultV1(
            ok=False,
            verdict="BLOCKED",
            block_reason="INSUFFICIENT_PUBLISHED_MONTH_BUNDLES",
            month_audit=month_audit,
            excluded_months=excluded,
            yaml_path=str(yaml_path),
            git_head=_git_head(),
        )

    try:
        composite = MultiBundleDensitySessionV1(bundle_pairs)
    except ValueError as e:
        return Phase3PreflightResultV1(
            ok=False,
            verdict="BLOCKED",
            block_reason=str(e),
            month_audit=month_audit,
            yaml_path=str(yaml_path),
        )

    closes = composite.decision_closes()
    gaps: list[str] = []
    for i in range(1, len(closes)):
        if not _expected_5m_step(closes[i - 1], closes[i]):
            gaps.append(f"{closes[i - 1]}->{closes[i]}")
            if len(gaps) >= 20:
                break

    pre = run_coverage_preflight(
        decision_closes=closes,
        transport="validated_frozen_bundle_composite",
        bundle_first_close=closes[0] if closes else None,
    )
    eval_start = pre.required_bars
    eval_start_close = closes[eval_start] if len(closes) > eval_start else None

    ohlc_start = _iso(_parse_utc(closes[0]) - timedelta(hours=720 + 48))
    _, prices, ohlc_meta = load_5m_ohlc_ch(ohlc_start, closes[-1])
    missing_ohlc = [c for c in closes if c not in prices][:10]

    block_reason: str | None = None
    if gaps:
        block_reason = "MERGED_TIMELINE_GAP"
    elif not pre.ok:
        block_reason = pre.reason_code
    elif missing_ohlc:
        block_reason = "OHLC_MISSING_FOR_DECISION_CLOSES"

    ok = block_reason is None
    return Phase3PreflightResultV1(
        ok=ok,
        verdict="PREFLIGHT_PASS" if ok else "BLOCKED",
        block_reason=block_reason,
        included_months=[m for m, _ in bundle_pairs],
        excluded_months=excluded,
        month_audit=month_audit,
        merged_closes=len(closes),
        merged_first_close=closes[0] if closes else None,
        merged_last_close=closes[-1] if closes else None,
        eval_start_index=eval_start,
        eval_start_close_utc=eval_start_close,
        timeline_gaps=gaps,
        git_head=_git_head(),
        yaml_path=str(yaml_path),
        detail={
            "coverage_preflight": asdict(pre),
            "ohlc_meta": ohlc_meta,
            "ohlc_missing_sample": missing_ohlc,
            "continuous_state": "single run_long_research_v2d loop; no month-boundary reset",
            "variant": "A FROZEN_AT_PLACE_V2J only",
        },
    )


def write_preflight_json(path: Path, result: Phase3PreflightResultV1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
