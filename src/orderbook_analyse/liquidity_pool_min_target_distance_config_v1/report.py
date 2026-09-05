"""Generate CONFIG v1 report artefacts and retrospective CASE_03/04 diagnostics."""

from __future__ import annotations

import csv
import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_min_target_distance_config_v1 import (
    CANONICAL_STRATEGY_YAML_REL,
    FORMAT_VERSION,
    STRATEGY_RESEARCH_DOC_REL,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    ConfigOwnerAmbiguousError,
    RoomGateConfigError,
    load_room_to_target_config,
    repo_root_from,
    resolve_config_yaml_path,
    validate_room_to_target_block,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.gate import (
    PoolCandidate,
    evaluate_room_to_target_gate,
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _git_head(repo_root: Path) -> str | None:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True)
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _load_pools_from_geometry_csv(path: Path) -> list[PoolCandidate]:
    pools: list[PoolCandidate] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pools.append(
                PoolCandidate(
                    pool_id=row["pool_id"],
                    source_timeframe=row["source_timeframe"],
                    side=row["side"],
                    lower_edge=float(row["lower_edge"]),
                    upper_edge=float(row["upper_edge"]),
                    available_at=row["available_at"],
                    active_as_of=True,
                )
            )
    return pools


def _case_retrospective(
    *,
    case_id: str,
    audit_dir: Path,
    config,
    exposure_class: str,
) -> dict[str, Any]:
    mech_path = audit_dir / "mechanical_verdict_pre_unblind.json"
    geom_path = audit_dir / "causal_pool_geometry.csv"
    summary_path = audit_dir / "summary.json"

    stored_verdict: dict[str, Any] = {}
    if summary_path.is_file():
        stored_verdict = json.loads(summary_path.read_text(encoding="utf-8"))

    mech = json.loads(mech_path.read_text(encoding="utf-8")) if mech_path.is_file() else {}
    pools = _load_pools_from_geometry_csv(geom_path) if geom_path.is_file() else []
    ref_ts = mech.get("arrival_ts") or stored_verdict.get("outcome_comparison", {}).get(
        "frozen_sample_cluster_start_ts"
    )

    ref_mid_path = audit_dir / "reference_mid.json"
    ref_mid = None
    if ref_mid_path.is_file():
        ref_mid = json.loads(ref_mid_path.read_text(encoding="utf-8")).get("mid")

    branches: dict[str, Any] = {}
    for branch_name, direction in (("long_branch", "LONG"), ("short_branch", "SHORT")):
        branch = mech.get(branch_name) or {}
        entry = branch.get("entry_price")
        if entry is None and direction == "LONG":
            entry = ref_mid
        if entry is None and direction == "SHORT":
            entry = mech.get("entry_price")
        gate = evaluate_room_to_target_gate(
            direction=direction,
            entry_price=float(entry) if entry is not None else float("nan"),
            pools=pools,
            config=config,
            as_of_iso=ref_ts,
        )
        branches[direction] = {
            "entry_price": entry,
            "room_gate_v1": gate,
            "stored_mechanical_verdict": mech.get("mechanical_verdict"),
            "stored_mechanical_trade_verdict": mech.get("mechanical_trade_verdict"),
            "verdict_unchanged": True,
            "note": (
                "Diagnostic only — stored CASE audit verdicts are not modified."
            ),
        }

    return {
        "case_id": case_id,
        "exposure_class": exposure_class,
        "reference_ts": ref_ts,
        "stored_summary_verdict": stored_verdict.get("verdict"),
        "stored_mechanical_trade_verdict": stored_verdict.get("mechanical_trade_verdict"),
        "branches": branches,
        "expected_distance_gate_only": {
            "CASE_03": {
                "LONG": {"approx_raw_bps": 9.15, "gate_passed": False},
                "SHORT": {
                    "approx_raw_bps": 95.0,
                    "gate_passed": True,
                    "trade_still_blocked": True,
                    "reason": "contest/NO_TRADE unchanged",
                },
            },
            "CASE_04": {
                "LONG": {"approx_raw_bps": 6.36, "gate_passed": False},
                "SHORT": {"approx_raw_bps": 9.1, "gate_passed": False},
            },
        }.get(case_id, {}),
    }


def build_report(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or repo_root_from()
    out_dir = root / "results" / "liquidity_pool_min_target_distance_config_v1"
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        yaml_path, owner_rationale = resolve_config_yaml_path(root)
    except ConfigOwnerAmbiguousError as exc:
        payload = {
            "verdict": "ROOM_GATE_CONFIG_OWNER_NOT_UNAMBIGUOUS",
            "error": str(exc),
            "generated_at": generated_at,
        }
        _write_json(out_dir / "config_validation.json", payload)
        (out_dir / "CONFIG_REPORT.md").write_text(
            f"# Room Gate Config v1\n\nVerdict: **ROOM_GATE_CONFIG_OWNER_NOT_UNAMBIGUOUS**\n\n{exc}\n",
            encoding="utf-8",
        )
        return payload

    with yaml_path.open(encoding="utf-8") as fh:
        import yaml

        raw_doc = yaml.safe_load(fh)
    block = raw_doc.get("room_to_target", {})
    validation = validate_room_to_target_block(block if isinstance(block, dict) else {})

    config_validation = {
        "valid": validation["valid"],
        "issues": validation["issues"],
        "yaml_path": str(yaml_path.relative_to(root)),
        "research_doc": STRATEGY_RESEARCH_DOC_REL,
        "generated_at": generated_at,
    }
    _write_json(out_dir / "config_validation.json", config_validation)

    if not validation["valid"]:
        payload = {
            "verdict": "MIN_TARGET_DISTANCE_CONFIG_V1_INVALID",
            "validation": config_validation,
            "generated_at": generated_at,
        }
        _write_json(out_dir / "effective_config.json", payload)
        (out_dir / "CONFIG_REPORT.md").write_text(
            "# Room Gate Config v1\n\nVerdict: **MIN_TARGET_DISTANCE_CONFIG_V1_INVALID**\n",
            encoding="utf-8",
        )
        return payload

    config = load_room_to_target_config(root, yaml_path=yaml_path)
    effective = {
        "format_version": FORMAT_VERSION,
        "verdict": "MIN_TARGET_DISTANCE_CONFIG_V1_READY",
        "config_owner": {
            "yaml_path": str(yaml_path.relative_to(root)),
            "research_doc": STRATEGY_RESEARCH_DOC_REL,
            "rationale": owner_rationale,
        },
        "room_to_target": asdict(config),
        "generated_at": generated_at,
        "git_head": _git_head(root),
    }
    _write_json(out_dir / "effective_config.json", effective)

    retrospective = {
        "diagnostic_only": True,
        "verdicts_unchanged": True,
        "temporal_validity": {
            "CASE_01": "PRE_CONFIG_EXPOSED",
            "CASE_02": "PRE_CONFIG_EXPOSED",
            "CASE_03": "PRE_CONFIG_EXPOSED",
            "CASE_04": "PRE_CONFIG_EXPOSED",
            "CASE_05": "PROSPECTIVE_UNAUDITED",
            "CASE_06": "PRE_CONFIG_EXPOSED",
        },
        "cases": [],
        "generated_at": generated_at,
    }

    case_specs = (
        ("CASE_03", "case_03_frozen_bid_pool_causal_reaction_audit_v1", "PRE_CONFIG_EXPOSED"),
        ("CASE_04", "case_04_frozen_bid_pool_causal_reaction_audit_v1", "PRE_CONFIG_EXPOSED"),
    )
    for case_id, dirname, exposure in case_specs:
        audit_dir = root / "results" / dirname
        if audit_dir.is_dir():
            retrospective["cases"].append(
                _case_retrospective(
                    case_id=case_id,
                    audit_dir=audit_dir,
                    config=config,
                    exposure_class=exposure,
                )
            )

    _write_json(out_dir / "retrospective_diagnostic_only.json", retrospective)

    report_md = _render_config_report(
        root=root,
        yaml_path=yaml_path,
        config=config,
        owner_rationale=owner_rationale,
        retrospective=retrospective,
        generated_at=generated_at,
    )
    (out_dir / "CONFIG_REPORT.md").write_text(report_md, encoding="utf-8")

    return {
        "verdict": "MIN_TARGET_DISTANCE_CONFIG_V1_READY",
        "out_dir": str(out_dir),
        "config": asdict(config),
        "retrospective": retrospective,
        "generated_at": generated_at,
    }


def _render_config_report(
    *,
    root: Path,
    yaml_path: Path,
    config,
    owner_rationale: str,
    retrospective: dict[str, Any],
    generated_at: str,
) -> str:
    yaml_text = yaml_path.read_text(encoding="utf-8")
    block_lines = yaml_text.splitlines()[yaml_text.splitlines().index("room_to_target:") :]
    lines = [
        "# Liquidity Pool Min Target Distance Config v1",
        "",
        f"Generated: {generated_at}",
        "",
        "## 1. Verdict",
        "",
        "**MIN_TARGET_DISTANCE_CONFIG_V1_READY**",
        "",
        "## 2. Config owner",
        "",
        f"- YAML: `{yaml_path.relative_to(root)}`",
        f"- Research doc: `{STRATEGY_RESEARCH_DOC_REL}`",
        f"- Rationale: {owner_rationale}",
        "",
        "## 3. YAML block (`room_to_target`)",
        "",
        "```yaml",
        *block_lines,
        "```",
        "",
        "## 4. Loaded values",
        "",
        f"- `min_target_distance_pct`: **{config.min_target_distance_pct}**",
        f"- `min_target_distance_bps`: **{config.min_target_distance_bps}**",
        "",
        "## 5. Measurement semantics",
        "",
        "- **LONG**: nearest causally available ASK pool above entry; target = **lower/front edge**;",
        "  distance = `((target_lower - entry_price) / entry_price) * 100`.",
        "- **SHORT**: nearest causally available BID pool below entry; target = **upper/front edge**;",
        "  distance = `((entry_price - target_upper) / entry_price) * 100`.",
        "",
        "## 6. Overlap / missing target",
        "",
        "- Entry inside opposing pool → `ENTRY_INSIDE_OPPOSING_POOL` (block).",
        "- HTF opposing overlap (15m/30m/1h) → `HTF_OPPOSING_POOL_OVERLAP` (block).",
        "- Missing target pool → `TARGET_NOT_OBSERVED` (block).",
        "- Future-only target → `TARGET_NOT_CAUSALLY_AVAILABLE` (block).",
        "",
        "## 7. Changed files",
        "",
        "- `strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml`",
        "- `src/orderbook_analyse/liquidity_pool_min_target_distance_config_v1/`",
        "- `scripts/run_liquidity_pool_min_target_distance_config_v1.py`",
        "- `tests/test_liquidity_pool_min_target_distance_config_v1.py`",
        "",
        "## 8. Retrospective CASE_03 / CASE_04 (diagnostic only)",
        "",
    ]

    for case in retrospective.get("cases", []):
        lines.append(f"### {case['case_id']} ({case['exposure_class']})")
        lines.append("")
        for direction in ("LONG", "SHORT"):
            branch = case["branches"].get(direction, {})
            gate = branch.get("room_gate_v1", {})
            lines.append(
                f"- **{direction}**: raw={gate.get('raw_target_distance_bps'):.4f} bps, "
                f"gate_passed={gate.get('gate_passed')}, reason={gate.get('gate_reason')}; "
                f"stored verdict unchanged: {branch.get('stored_mechanical_verdict')}"
            )
        lines.append("")

    lines.extend(
        [
            "## 9. Live safety",
            "",
            "- Read-only research module; no ClickHouse writes.",
            "- No collector/live process changes.",
            "- Gate config loaded exclusively from YAML; fail-closed on invalid config.",
            "- CASE_03/04 stored audit verdicts not rewritten.",
            "",
            "## 10. CASE_05",
            "",
            "**Not started** — no CASE_05 audit in this task.",
            "",
        ]
    )
    return "\n".join(lines)


def run_tests_and_persist(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or repo_root_from()
    out_dir = root / "results" / "liquidity_pool_min_target_distance_config_v1"
    proc = subprocess.run(
        [
            "python",
            "-m",
            "pytest",
            "tests/test_liquidity_pool_min_target_distance_config_v1.py",
            "-q",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    payload = {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "passed": proc.returncode == 0,
    }
    _write_json(out_dir / "test_results.json", payload)
    return payload
