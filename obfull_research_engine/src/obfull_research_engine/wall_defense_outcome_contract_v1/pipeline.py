"""Pipeline + contract freeze artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from ..paths import ENGINE_ROOT
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    CONTRACT_HASH,
    CONTRACT_VERSION,
    TIME_BASIS,
    VERDICT_OK,
    contract_definition,
    outcome_contract_hash,
)
from .episode1_apply import apply_episode1_mechanics_test
from .oracle import oracle_audit
from .side import SIDE_SEMANTICS_DOC


def run_outcome_contract(*, run_key: str, out_dir: Path) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    contract = contract_definition()
    chash = outcome_contract_hash()
    assert chash == CONTRACT_HASH

    ep1 = apply_episode1_mechanics_test()
    handoff = json.loads(
        (ENGINE_ROOT / "results/level_first_episode1_detection_to_wall_flow_integration_v1/BTCUSDT/d2w1_eee5d0eefaffa/episode1_handoff.json").read_text(
            encoding="utf-8"
        )
    )
    oracle = oracle_audit(
        records=ep1["records"],
        wall_side=str(handoff["wall_side"]),
        wall_price=float(handoff["wall_price"]),
        contract_hash=chash,
        expected_hash=CONTRACT_HASH,
    )

    # Label table
    from collections import Counter

    label_counts = Counter(r["label"]["label"] for r in ep1["records"])
    censor_counts = Counter(r["facts"].get("censor_reason") for r in ep1["records"] if not r["facts"]["coverage_ok"])

    # Separation proof: write outcomes in dedicated files only
    atomic_write_json(out_dir / "outcome_contract.json", {**contract, "outcome_contract_hash": chash})
    atomic_write_json(out_dir / "side_semantics.json", SIDE_SEMANTICS_DOC)
    atomic_write_json(out_dir / "episode1_outcome_facts_and_labels.json", {
        "note": "OUTCOME store only — must not be read by feature pipelines",
        "episode_id": ep1["episode_id"],
        "records": ep1["records"],
    })
    # Feature leakage check artifact (empty feature mirror)
    atomic_write_json(
        out_dir / "feature_store_placeholder.json",
        {
            "note": "Feature store must not contain outcome columns",
            "forbidden_columns": [
                "outcome_label",
                "mfe_ticks",
                "mae_ticks",
                "pnl",
                "trade_result",
            ],
            "outcome_columns_present": False,
        },
    )

    mech = ep1["mechanics_expectations"]
    ok = bool(oracle.get("ok")) and all(
        [
            mech["short_breach_outcomes_within_epoch4_exist"],
            mech["no_reclaim_within_observed_coverage"],
            mech["later_horizons_censored"],
            mech["detection_outcomes_unavailable_outside_coverage"],
            mech["no_post_epoch_same_chain_outcome"],
            mech["anchors_not_mixed"],
        ]
    )
    verdict = VERDICT_OK if ok else "WALL_DEFENSE_OUTCOME_CONTRACT_V1_BLOCKED"

    sem = {
        "schema": "wall_defense_outcome_contract_v1",
        "outcome_contract_version": CONTRACT_VERSION,
        "outcome_contract_hash": chash,
        "n_records": ep1["n_records"],
        "n_valid": ep1["n_valid"],
        "n_censored": ep1["n_censored"],
        "n_reclaim_labels_valid": ep1["n_reclaim_labels_valid"],
        "label_counts": dict(label_counts),
        "censor_counts": {str(k): v for k, v in censor_counts.items()},
        "mechanics_expectations": mech,
        "oracle_ok": oracle.get("ok"),
        "oracle_fp": oracle.get("fp"),
        "oracle_fn": oracle.get("fn"),
        "oracle_mismatch": oracle.get("mismatch"),
        "oracle_look_ahead": oracle.get("look_ahead"),
        "time_basis": TIME_BASIS,
        "verdict": verdict,
    }
    sem_hash = sha256_hex(sem)
    atomic_write_json(out_dir / "oracle_audit.json", oracle)
    atomic_write_json(out_dir / "episode1_mechanics_summary.json", {
        "expectations": mech,
        "label_counts": dict(label_counts),
        "censor_counts": {str(k): v for k, v in censor_counts.items()},
        "cluster": ep1["cluster"],
    })
    atomic_write_json(out_dir / "semantic_fingerprint.json", {**sem, "semantic_hash": sem_hash})

    # Horizon matrix for Pflichtbericht
    horizon_matrix = []
    for r in ep1["records"]:
        f = r["facts"]
        horizon_matrix.append(
            {
                "anchor_type": f["anchor_type"],
                "horizon_ms": f["horizon_ms"],
                "coverage_ok": f["coverage_ok"],
                "censor_reason": f.get("censor_reason"),
                "label": r["label"]["label"],
                "end_price_side": f.get("end_price_side"),
                "end_microprice_side": f.get("end_microprice_side"),
                "first_joint_breach_time": f.get("first_joint_breach_time"),
                "first_joint_reclaim_time": f.get("first_joint_reclaim_time"),
            }
        )

    report = {
        "verdict": verdict,
        "ok": ok,
        "cause_and_goal": {
            "cause": (
                "Before multi-episode scaling, futures must be measured and named "
                "under a frozen, outcome-blind contract so labels cannot leak into "
                "features or be optimized on Episode 1."
            ),
            "goal": (
                "Prove a causal, deterministic, versioned OutcomeFacts+OutcomeLabel "
                "contract. Episode 1 is schema/mechanics only — not a profitable pattern."
            ),
        },
        "affected_paths": [
            "src/obfull_research_engine/wall_defense_outcome_contract_v1/",
            "tests/test_wall_defense_outcome_contract_v1.py",
        ],
        "contract_definition": contract,
        "outcome_contract_hash": chash,
        "label_table": dict(label_counts),
        "censoring_table": {str(k): v for k, v in censor_counts.items()},
        "side_mirror_proof": SIDE_SEMANTICS_DOC,
        "episode1_role": "schema_mechanics_test_only",
        "episode1_horizon_matrix": horizon_matrix,
        "episode1_valid_count": ep1["n_valid"],
        "episode1_censored_count": ep1["n_censored"],
        "episode1_reclaim_labels_valid": ep1["n_reclaim_labels_valid"],
        "mechanics_expectations": mech,
        "oracle": oracle,
        "test_commands": [
            "PYTHONPATH=src:../src python -m pytest tests/test_wall_defense_outcome_contract_v1.py -q",
            "PYTHONPATH=src:../src python -m obfull_research_engine.wall_defense_outcome_contract_v1.e2e e2e --run-key <key>",
            "PYTHONPATH=src:../src python -m obfull_research_engine.wall_defense_outcome_contract_v1.e2e compare --dir-a <a> --dir-b <b> --out <cmp.json>",
        ],
        "remaining_limits": [
            "Not a multi-episode BTC run",
            "No WALL_STATE / reclaim-duration / major-wall-percentile calibration",
            "Mechanical labels are not trading classes",
            "No OI / liquidations / cross-exchange / receive-time / CH writes",
        ],
        "semantic_hash": sem_hash,
        "time_basis": TIME_BASIS,
    }
    atomic_write_json(out_dir / "pflichtbericht.json", report)
    atomic_write_text(
        out_dir / "PFLICHTBERICHT.md",
        _pflichtbericht_md(report),
    )

    result = {
        "ok": ok,
        "verdict": verdict,
        "run_key": run_key,
        "semantic_hash": sem_hash,
        "outcome_contract_hash": chash,
        "outcome_contract_version": CONTRACT_VERSION,
        "oracle": {k: oracle.get(k) for k in ("ok", "fp", "fn", "mismatch", "look_ahead")},
        "n_valid": ep1["n_valid"],
        "n_censored": ep1["n_censored"],
        "n_reclaim_labels_valid": ep1["n_reclaim_labels_valid"],
        "mechanics_expectations": mech,
        "time_basis": TIME_BASIS,
    }
    atomic_write_json(out_dir / "run_manifest.json", result)
    atomic_write_text(out_dir / "STATUS", verdict + "\n")
    return result


def _pflichtbericht_md(report: dict[str, Any]) -> str:
    lines = [
        f"# Pflichtbericht — {report['verdict']}",
        "",
        "## Ursache und Ziel",
        report["cause_and_goal"]["cause"],
        "",
        report["cause_and_goal"]["goal"],
        "",
        "## Contract-Hash",
        f"`{report['outcome_contract_hash']}`",
        "",
        "## Label-Tabelle",
        "```json",
        json.dumps(report["label_table"], indent=2, sort_keys=True),
        "```",
        "",
        "## Censoring-Tabelle",
        "```json",
        json.dumps(report["censoring_table"], indent=2, sort_keys=True),
        "```",
        "",
        "## Episode-1 Mechanik",
        f"- valid={report['episode1_valid_count']} censored={report['episode1_censored_count']} "
        f"reclaim_valid={report['episode1_reclaim_labels_valid']}",
        f"- mechanics={json.dumps(report['mechanics_expectations'], sort_keys=True)}",
        "",
        "## Oracle",
        f"`{json.dumps({k: report['oracle'].get(k) for k in ('ok','fp','fn','mismatch','look_ahead')})}`",
        "",
        "## Verbleibende Grenzen",
        *[f"- {x}" for x in report["remaining_limits"]],
        "",
    ]
    return "\n".join(lines) + "\n"
