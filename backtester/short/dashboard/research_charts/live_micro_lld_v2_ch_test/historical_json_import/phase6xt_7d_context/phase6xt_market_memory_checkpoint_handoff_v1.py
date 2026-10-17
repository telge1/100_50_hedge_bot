#!/usr/bin/env python3
"""Phase 6XT-MARKET-MEMORY-CHECKPOINT-HANDOFF V1 — research only."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_market_memory_checkpoint_handoff_v1"
_CTX = Path(__file__).resolve().parent
_VALIDATED = (
    _REPO
    / "logs/historical_import/overnight_2026/phase6xt_90d_integrated_price_pool_memory_v1/rerun_20261016T182520Z"
)
_PAPER = _REPO / "logs/historical_import/overnight_2026/phase6xt_continuous_landscape_paper_runner_v1"
_PRIOR_BOOT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_market_memory_bootstrap_catchup_v1"
VALIDATED_RUN_ID = "20261016T182520Z"

BOOT_END = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
CATCHUP_START = datetime(2026, 8, 17, 4, 45, tzinfo=timezone.utc)
_POOL_MIN_H = 168

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
import phase6xt_90d_integrated_price_pool_memory_v1 as integrated  # noqa: E402
import phase6xt_90d_market_memory_bootstrap_catchup_v1 as boot90  # noqa: E402
import phase6xt_market_memory_price_stream_recovery_v1 as rec  # noqa: E402
from continuous_landscape_paper_runner import (  # noqa: E402
    PaperRunner,
    run_continuous_reference,
)
from market_memory_bootstrap_engine import unique_dcs  # noqa: E402
from market_memory_handoff import (  # noqa: E402
    audit_state_completeness,
    classify_validated_source,
    inventory_checkpoint_files,
    reconstruction_plan,
)
from market_memory_semantic_diff import semantic_diff  # noqa: E402
from market_memory_state import MarketMemoryState  # noqa: E402
from price_memory_stream import attach_price_processor  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson


def _bar_counter_only_mismatch(diff: dict[str, Any]) -> bool:
    diffs = diff.get("diffs") or []
    return bool(diffs) and all(d.get("path") == "price_processor.processed_bars" for d in diffs)


def _forward_paths_operational_match(forward: dict[str, Any]) -> bool:
    if forward.get("all_forward_paths_match"):
        return True
    cr = forward.get("continuous_vs_checkpoint_restart") or {}
    pr = forward.get("continuous_vs_paper_runner") or {}
    return (
        (cr.get("hash_match") or _bar_counter_only_mismatch(cr))
        and (pr.get("hash_match") or _bar_counter_only_mismatch(pr))
    )


def october_readiness(client, db: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    probes = {}
    for pd in (1, 10):
        last = None
        t = datetime(2026, 10, 1, tzinfo=timezone.utc)
        while t <= now:
            dc = causal_15m_decision_close(t)
            try:
                m = c7.load_members({}, dc, pd, client, db)
                if m:
                    last = _iso(dc)
            except Exception as e:
                probes[f"pd{pd}"] = {"error": str(e)}
                break
            t += timedelta(days=3)
        if f"pd{pd}" not in probes:
            probes[f"pd{pd}"] = {"max_dc_with_members": last}
    oct_ok = all(
        (probes.get(f"pd{pd}", {}).get("max_dc_with_members") or "") >= "2026-10-15"
        for pd in (1, 10)
    )
    return {
        "probe_utc": _iso(now),
        "pool_probes": probes,
        "october_original_publication_verified": oct_ok,
        "live_forward_pilot": "BLOCKED" if not oct_ok else "READINESS_PROBE_ONLY",
        "policy": "No synthetic October pools; no snapshot production",
    }


def short_forward_parity(
    handoff_boot_cp: dict[str, Any],
    client,
    db: str,
    spatial_idx: dict,
    bars: dict,
    foundation_verified: bool,
) -> dict[str, Any]:
    """Validated follow window: mid-catchup → boot_end (integrated Path-C segment)."""
    mid_path = _PAPER / "recovery_mid_checkpoint.json"
    if not mid_path.is_file():
        return {
            "error": "missing_recovery_mid_checkpoint",
            "foundation_verification": "FOUNDATION_NOT_VERIFIED",
        }

    mid_cp = json.loads(mid_path.read_text(encoding="utf-8"))
    mid_dc = causal_15m_decision_close(CATCHUP_START + timedelta(days=1))
    dcs_forward = [dc for dc in unique_dcs(mid_dc, BOOT_END) if dc > mid_dc]
    dcs_restart = unique_dcs(mid_dc + timedelta(minutes=15), BOOT_END)

    mem_cont = MarketMemoryState.from_checkpoint(mid_cp)
    mem_cont_end = run_continuous_reference(
        mid_dc,
        BOOT_END,
        mem_cont,
        bars,
        attach_price_processor(mem_cont),
        spatial_idx,
        c7.load_members,
        client,
        db,
    )

    mem_restart = MarketMemoryState.from_checkpoint(mid_cp)
    mem_restart_end = run_continuous_reference(
        mid_dc + timedelta(minutes=15),
        BOOT_END,
        mem_restart,
        bars,
        attach_price_processor(mem_restart),
        spatial_idx,
        c7.load_members,
        client,
        db,
    )

    runner_mem = MarketMemoryState.from_checkpoint(mid_cp)
    runner = PaperRunner(
        memory=runner_mem,
        bars_5m=bars,
        price_processor=attach_price_processor(runner_mem),
        spatial_idx=spatial_idx,
        load_members_fn=c7.load_members,
        client=client,
        db=db,
    )
    for dc in dcs_forward:
        runner.process_decision_close(dc)
    runner.flush_price()

    diff_cr = semantic_diff(mem_cont_end, mem_restart_end)
    diff_paper = semantic_diff(mem_cont_end, runner.memory)
    diff_handoff_end = semantic_diff(mem_cont_end, MarketMemoryState.from_checkpoint(handoff_boot_cp))

    active = boot90.active_parity(client, db, mem_cont_end, BOOT_END)

    fv = "FOUNDATION_VERIFIED" if foundation_verified else "FOUNDATION_NOT_VERIFIED"
    return {
        "forward_window": {"start": _iso(mid_dc), "end": _iso(BOOT_END), "decision_closes_forward": len(dcs_forward)},
        "foundation_verification": fv,
        "continuous_vs_checkpoint_restart": diff_cr,
        "continuous_vs_paper_runner": diff_paper,
        "continuous_end_vs_handoff_boot_foundation": diff_handoff_end,
        "active_ssot_parity_at_end": active,
        "end_semantic_hash": mem_cont_end.semantic_state_hash(),
        "handoff_boot_hash": handoff_boot_cp.get("semantic_state_hash"),
        "price_watermark_5m": (mem_cont_end.price_processor or {}).get("watermark_5m"),
        "watermark_utc": mem_cont_end.watermark_utc,
        "all_forward_paths_match": diff_cr.get("hash_match") and diff_paper.get("hash_match"),
        "forward_operational_match_bar_counter_only_delta": _forward_paths_operational_match(
            {
                "all_forward_paths_match": False,
                "continuous_vs_checkpoint_restart": diff_cr,
                "continuous_vs_paper_runner": diff_paper,
            }
        ),
        "handoff_boot_matches_forward_end": diff_handoff_end.get("hash_match"),
        "handoff_boot_vs_forward_note": (
            "Expected false until Path-A boot-end checkpoint exported; paper foundation uses batched price backfill."
        ),
    }


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    limitations: list[str] = []
    blockers: list[str] = []

    summary_path = _VALIDATED / "full_replay_state_summary.json"
    validated_hash = None
    if summary_path.is_file():
        validated_hash = json.loads(summary_path.read_text(encoding="utf-8")).get("semantic_state_hash")

    inventory = inventory_checkpoint_files(_REPO)
    classification = classify_validated_source(_VALIDATED, validated_hash, inventory)
    _wjson(_OUT / "checkpoint_inventory.json", {"inventory": inventory, "classification": classification})

    paper_foundation = _PAPER / "foundation_checkpoint.json"
    handoff_cp: dict[str, Any] | None = None
    handoff_provenance = "none"
    if classification["classification"] == "A":
        cand = classification.get("candidate_matching_validated_hash") or {}
        if cand.get("path"):
            handoff_cp = json.loads(Path(cand["path"]).read_text(encoding="utf-8"))
            handoff_provenance = "validated_path_a_persisted"
    elif classification["classification"] == "B":
        cand = classification.get("candidate_matching_validated_hash") or {}
        if cand.get("path"):
            handoff_cp = json.loads(Path(cand["path"]).read_text(encoding="utf-8"))
            handoff_provenance = "semantic_path_b_c_persisted"

    if handoff_cp is None and paper_foundation.is_file():
        handoff_cp = json.loads(paper_foundation.read_text(encoding="utf-8"))
        handoff_provenance = "paper_runner_foundation_not_validated_path_a"
        limitations.append("No persisted integrated Path-A checkpoint; using paper_runner foundation for load tests only.")

    completeness = audit_state_completeness(handoff_cp or {})
    _wjson(_OUT / "state_completeness_audit.json", completeness)

    hash_cmp = {
        "validated_integrated_path_a_hash": validated_hash,
        "handoff_candidate_hash": (handoff_cp or {}).get("semantic_state_hash"),
        "exact_match": validated_hash == (handoff_cp or {}).get("semantic_state_hash") if handoff_cp else False,
        "handoff_provenance": handoff_provenance,
        "classification": classification["classification"],
        "parity_artifacts": {},
    }
    for name in ("checkpoint_restart_semantic_parity.json", "multiple_restart_parity.json"):
        p = _VALIDATED / name
        if p.is_file():
            hash_cmp["parity_artifacts"][name] = json.loads(p.read_text(encoding="utf-8"))
    _wjson(_OUT / "semantic_hash_comparison.json", hash_cmp)

    foundation_verified = hash_cmp["exact_match"] and classification["classification"] in ("A", "B")

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    spatial_idx = rec.load_geometry_spatial_index()
    hist = integrated.BOOT_START - timedelta(hours=_POOL_MIN_H)
    bars = rec.load_bars(hist, BOOT_END)

    forward = {"skipped": True, "reason": "no_handoff_checkpoint"}
    if handoff_cp and completeness.get("paper_runner_loadable"):
        forward = short_forward_parity(handoff_cp, client, db, spatial_idx, bars, foundation_verified)
    _wjson(_OUT / "short_forward_parity.json", forward)

    aug = None
    if handoff_cp:
        mem = MarketMemoryState.from_checkpoint(handoff_cp)
        aug = integrated.aug_sequence_regression(mem)
    _wjson(_OUT / "august_regression.json", aug or {"skipped": True})

    october = october_readiness(client, db)
    _wjson(_OUT / "october_source_readiness.json", october)

    recon = reconstruction_plan(validated_hash)
    if classification["classification"] == "C":
        blockers.append("Validated rerun did not persist Path-A/B/C full state blob (classification C).")
        blockers.append("Operator-approved export: PHASE6XT_EXPORT_HANDOFF=1 on integrated rerun (~50m).")

    if not hash_cmp["exact_match"]:
        blockers.append("Semantic hash mismatch vs integrated Path-A; do not claim lossless handoff.")

    if not october.get("october_original_publication_verified"):
        blockers.append("October original LMV2 publication not verified; live-forward pilot remains BLOCKED.")

    (_OUT / "validated_state_source.md").write_text(
        f"""# Validated state source

- **Integrated verdict:** `INTEGRATED_90D_MEMORY_VALIDATED`
- **Run-ID:** `{VALIDATED_RUN_ID}`
- **Directory:** `{_VALIDATED}`

## Classification: **{classification["classification"]}**

{classification["definition"]}

- Rerun directory contains parity JSON and `full_replay_state_summary.json` (semantic hash).
- **No** `path_a_boot_end.checkpoint.json` or equivalent was written during the validated rerun.
- Path-A/B/C checkpoints existed **in-process only** (`meta_a.snapshots`, `mem_a.checkpoint()` at end).

## Handoff candidate used in this phase

- **Provenance:** `{handoff_provenance}`
- **Foundation verified against Path-A hash:** `{foundation_verified}`

Research only — no live start.
""",
        encoding="utf-8",
    )

    (_OUT / "handoff_contract.md").write_text(
        """# Handoff contract (MarketMemoryState checkpoint v3)

| Domain | Checkpoint keys / runtime |
|--------|---------------------------|
| PriceMemory | `price_timeline`, `price_processor` (bar_ring, watermark_5m, drops, invalidations) |
| Drop episodes | `drop_episodes` per PD |
| Origins | `origins`, `upper_origins` with bounds, strength, pool_ids, known_at via records |
| ACTIVE pools | `active_pool_ids` |
| Staircase | `spatial_steps`, `seen_origins`, `main_step_*` |
| PD isolation | PD1 and PD10 separate dicts |
| Causality | `watermark_utc`, `event_cursor`, `processed_publications` |
| Versioning | `version`=3, `schema_code_version` |

Older ACTIVE origins must not be dropped by retention in handoff load; paper runner must not filter by age.

Hash alone is not sufficient — full payload required.
""",
        encoding="utf-8",
    )

    (_OUT / "remaining_blockers.md").write_text(
        "# Remaining blockers\n\n"
        + "\n".join(f"- {b}" for b in blockers)
        + "\n\n## Reconstruction plan\n\n```json\n"
        + json.dumps(recon, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )

    forward_ok = _forward_paths_operational_match(forward)
    mechanics = (
        handoff_cp
        and completeness.get("complete_per_contract")
        and completeness.get("paper_runner_loadable")
        and forward_ok
        and (aug or {}).get("all_pass")
    )
    active_ok = (forward.get("active_ssot_parity_at_end") or {}).get("match")
    if not forward.get("skipped") and not forward_ok:
        limitations.append("Forward window restart/paper paths diverge beyond bar-counter tolerance.")

    if foundation_verified and mechanics and active_ok:
        verdict = "MARKET_MEMORY_HANDOFF_VALIDATED"
    elif mechanics and active_ok:
        verdict = "MARKET_MEMORY_HANDOFF_PARTIAL"
    else:
        verdict = "MARKET_MEMORY_HANDOFF_NOT_READY"

    report = f"""# Phase 6XT — Market Memory Checkpoint Handoff V1

**Verdict:** `{verdict}`
**Wall time:** {time.time() - t0:.1f}s

## Closure
1. Reusable validated integrated state persisted? **{classification["classification"] != "C"}** (class {classification["classification"]})
2. PaperRunner compatible load? **{completeness.get("paper_runner_loadable")}**
3. Semantic hashes match Path-A? **{hash_cmp.get("exact_match")}**
4. Short forward test? **{forward_ok}** ({forward.get("foundation_verification", "n/a")}; operational={forward.get("forward_operational_match_bar_counter_only_delta")})
5. ACTIVE pools preserved? **{active_ok}**
6. August origins/steps? **{(aug or {}).get("all_pass")}**
7. Full reconstruction required? **{classification["classification"] == "C"}** — export on next approved integrated run
8. Until live paper pilot? See `remaining_blockers.md` and `october_source_readiness.json`

STOP — no live connection.
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")
    _wjson(
        _OUT / "job_status.json",
        {"phase": "phase6xt_market_memory_checkpoint_handoff_v1", "verdict": verdict, "execution_status": "EXECUTED"},
    )
    print(verdict)


if __name__ == "__main__":
    main()
