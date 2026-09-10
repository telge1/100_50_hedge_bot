"""Pipeline + Pflichtbericht for next-major-ask-barrier headroom."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    CLUSTER_CONTRACT,
    CONTRACT_VERSION,
    FEE_CONTRACT,
    TIME_BASIS,
    VERDICT_BLOCKED,
    VERDICT_DEPTH_INSUFFICIENT,
    VERDICT_NO_BARRIER,
    VERDICT_PROVEN,
)
from .analyze import run_episode1_analysis
from .oracle import oracle_audit


def run_headroom(*, run_key: str, out_dir: Path) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    analysis = run_episode1_analysis()
    oracle = oracle_audit(analysis)

    depth_ok = not analysis.get("depth_insufficient_any")
    any_barrier = bool(analysis.get("any_past_only_major_barrier"))
    # Depth: if barrier visible within full OB and ask_max covers barrier, depth is sufficient
    # for the research question even if required_target > barrier (that's a headroom fail, not depth fail).
    # VERDICT_DEPTH_INSUFFICIENT only if we could not see required scan depth at all.
    depth_insufficient = False
    for d in analysis["decisions"]:
        if not d.get("coverage_ok"):
            continue
        cov = d.get("ob_full_coverage") or {}
        if d.get("table_row") and cov.get("barrier_within_available_depth") is False:
            depth_insufficient = True
        if d.get("no_past_only_major_barrier") and cov.get("full_depth_coverage") is False:
            depth_insufficient = True

    if not oracle.get("ok"):
        verdict = VERDICT_BLOCKED
    elif depth_insufficient:
        verdict = VERDICT_DEPTH_INSUFFICIENT
    elif not any_barrier:
        verdict = VERDICT_NO_BARRIER
    else:
        verdict = VERDICT_PROVEN

    ok = verdict == VERDICT_PROVEN and bool(oracle.get("ok"))

    atomic_write_json(out_dir / "fee_contract.json", FEE_CONTRACT)
    atomic_write_json(out_dir / "cluster_contract.json", CLUSTER_CONTRACT)
    atomic_write_json(out_dir / "episode1_decisions.json", {"decisions": analysis["decisions"]})
    atomic_write_json(out_dir / "pflicht_table.json", {"rows": analysis["pflicht_table"]})
    atomic_write_json(out_dir / "explicit_answers.json", analysis["explicit_answers"])
    atomic_write_json(out_dir / "oracle_audit.json", oracle)

    # Compact semantic fingerprint (exclude bulky candidate lists)
    sem = {
        "schema": CONTRACT_VERSION,
        "verdict": verdict,
        "pflicht_table": analysis["pflicht_table"],
        "explicit_answers": analysis["explicit_answers"],
        "oracle_ok": oracle.get("ok"),
        "oracle_fp": oracle.get("fp"),
        "oracle_fn": oracle.get("fn"),
        "oracle_mismatch": oracle.get("mismatch"),
        "oracle_look_ahead": oracle.get("look_ahead"),
        "fee_contract": FEE_CONTRACT,
        "cluster_contract": CLUSTER_CONTRACT,
        "time_basis": TIME_BASIS,
        "n_valid_decisions": analysis["n_valid_decisions"],
        "detection_censored": analysis["detection_censored"],
    }
    sem_hash = sha256_hex(sem)
    atomic_write_json(out_dir / "semantic_fingerprint.json", {**sem, "semantic_hash": sem_hash})

    report = {
        "verdict": verdict,
        "ok": ok,
        "cause_and_goal": {
            "cause": (
                "After original ask-wall breach, a possible long needs causal visibility "
                "of the next serious ask barrier and whether remaining headroom covers "
                "0.30% net after 0.11% fees."
            ),
            "goal": (
                "Prove deterministic event-time headroom measurement to next past-only "
                "major ask barrier/cluster. No trading decision, no hold probability."
            ),
        },
        "affected_paths": [
            "src/obfull_research_engine/level_first_episode1_next_major_ask_barrier_headroom_v1/",
            "tests/test_level_first_episode1_next_major_ask_barrier_headroom_v1.py",
        ],
        "data_source": {
            "book": "results/.../wfq1_58db1918314881b6a",
            "defense_chain": "results/.../dch1_9bb0b8ff5ce8a",
            "window": "Epoch-4 continuous coverage through 2026-09-06T20:19:59.800Z",
        },
        "fee_contract": FEE_CONTRACT,
        "cluster_contract": CLUSTER_CONTRACT,
        "pflicht_table": analysis["pflicht_table"],
        "explicit_answers": analysis["explicit_answers"],
        "oracle": oracle,
        "semantic_hash": sem_hash,
        "time_basis": TIME_BASIS,
        "test_commands": [
            "PYTHONPATH=src:../src python -m pytest tests/test_level_first_episode1_next_major_ask_barrier_headroom_v1.py -q",
            "PYTHONPATH=src:../src python -m obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1.e2e e2e --run-key <key>",
        ],
        "remaining_limits": [
            "Episode-1 mechanics/headroom only — not a multi-episode edge proof",
            "Q90/Q95/Q97/Q99 are research views; none selected as trading rule",
            "No claim that any wall will hold",
            "DETECTION censored — outside Epoch-4 continuous coverage",
            "OUTCOME_ONLY path may be epoch-censored and must not affect selection",
        ],
    }
    atomic_write_json(out_dir / "pflichtbericht.json", report)
    atomic_write_text(out_dir / "PFLICHTBERICHT.md", _md(report))

    result = {
        "ok": ok,
        "verdict": verdict,
        "run_key": run_key,
        "semantic_hash": sem_hash,
        "oracle": {k: oracle.get(k) for k in ("ok", "fp", "fn", "mismatch", "look_ahead")},
        "explicit_answers": analysis["explicit_answers"],
        "n_pflicht_rows": len(analysis["pflicht_table"]),
        "time_basis": TIME_BASIS,
    }
    atomic_write_json(out_dir / "run_manifest.json", result)
    atomic_write_text(out_dir / "STATUS", verdict + "\n")
    return result


def _md(report: dict[str, Any]) -> str:
    ans = report.get("explicit_answers") or {}
    lines = [
        f"# Pflichtbericht — {report['verdict']}",
        "",
        "## Ursache und Ziel",
        report["cause_and_goal"]["cause"],
        "",
        report["cause_and_goal"]["goal"],
        "",
        "## Explicit Episode-1 answers",
        "```json",
        json.dumps(ans, indent=2, sort_keys=True),
        "```",
        "",
        "## Pflicht-Tabelle",
        "```json",
        json.dumps(report.get("pflicht_table"), indent=2),
        "```",
        "",
        "## Fee contract",
        "```json",
        json.dumps(report.get("fee_contract"), indent=2, sort_keys=True),
        "```",
        "",
        f"## Semantic hash\n`{report.get('semantic_hash')}`",
        "",
        "## Oracle",
        f"`{json.dumps({k: report['oracle'].get(k) for k in ('ok','fp','fn','mismatch','look_ahead')})}`",
        "",
    ]
    return "\n".join(lines) + "\n"
