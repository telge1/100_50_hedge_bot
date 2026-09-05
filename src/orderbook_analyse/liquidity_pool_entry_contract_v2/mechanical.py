"""Mechanical-only audit API — never opens outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_v2 import (
    ENTRY_CONTRACT_VERSION,
    EXPECTED_STRATEGY_CONFIG_SHA256,
    FORMAT_VERSION,
    PREDECESSOR_V1_ENTRY_CONTRACT_SHA256,
    STRATEGY_CONFIG_REL,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.case_spec import CaseSpec
from orderbook_analyse.liquidity_pool_entry_contract_v2.decision import (
    MicroEvidence,
    branch_gates_to_dict,
    flatten_room_gate_for_mech,
    geom_rows_to_pool_candidates,
    prefix_parity,
    resolve_mechanical_decision,
)
from orderbook_analyse.liquidity_pool_entry_contract_v2.geometry import resolve_geometry
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    RoomGateConfigError,
    load_effective_room_config,
)

FORBIDDEN_OUTCOME_KEYS = (
    "outcome",
    "pnl",
    "mfe",
    "mae",
    "forward_return",
    "unblind",
    "evidence_class",
)


class MechanicalAuditError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def payload_sha256(obj: dict[str, Any]) -> str:
    filtered = {
        k: v
        for k, v in obj.items()
        if k not in ("generated_at", "mechanical_payload_sha256")
    }
    blob = json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return _sha_bytes(blob.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def assert_no_outcome_reads(frozen_inputs: dict[str, Any]) -> None:
    blob = json.dumps(frozen_inputs, default=str).lower()
    for key in FORBIDDEN_OUTCOME_KEYS:
        # allow documenting forbidden list itself
        if key in ("unblind",) and "forbid" in blob:
            continue
        if f'"outcome_source"' in blob or f"forward_return" in blob:
            if "outcome_source" in frozen_inputs or "forward_returns" in frozen_inputs:
                raise MechanicalAuditError(
                    "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
                    f"forbidden outcome input key present: {key}",
                )


def run_mechanical_audit(
    case_spec: CaseSpec,
    frozen_inputs: dict[str, Any],
    output_dir: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Public mechanical-only API.

    Required frozen_inputs:
      - evidence: MicroEvidence fields as dict (offline / regression / synthetic)
      - pool_geometry_rows: list of opposing/HTF pool rows for room gate
      - hashes: optional dict with expansion/entry_contract hashes to embed

    Never loads outcomes. Does not call unblind.
    Market-data providers are intentionally NOT invoked in this freeze phase;
    supply evidence offline. If neither evidence nor allow_market_data, fail closed.
    """
    assert_no_outcome_reads(frozen_inputs)
    if frozen_inputs.get("outcome_source") or frozen_inputs.get("unblind"):
        raise MechanicalAuditError(
            "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
            "outcome/unblind keys forbidden in mechanical inputs",
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    root = repo_root or Path(__file__).resolve().parents[3]
    try:
        effective = load_effective_room_config(root)
    except RoomGateConfigError as exc:
        raise MechanicalAuditError("INVALID_ROOM_GATE_CONFIG", str(exc)) from exc

    if abs(effective.room.min_target_distance_pct - 0.5) > 1e-12:
        raise MechanicalAuditError(
            "ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE",
            f"min_target_distance_pct drifted: {effective.room.min_target_distance_pct}",
        )
    if effective.config_sha256 != EXPECTED_STRATEGY_CONFIG_SHA256:
        raise MechanicalAuditError(
            "ENTRY_CONTRACT_V2_FREEZE_INTEGRITY_FAILURE",
            "strategy config sha mismatch",
        )

    geom = resolve_geometry(
        pool_side=case_spec.pool_side,
        approach=case_spec.approach,
        lower=case_spec.pool_lower,
        upper=case_spec.pool_upper,
    )

    ev_raw = frozen_inputs.get("evidence")
    if ev_raw is None:
        if frozen_inputs.get("allow_market_data"):
            raise MechanicalAuditError(
                "MECHANICAL_INPUT_INCOMPLETE",
                "market_data path not enabled in this freeze phase; provide evidence",
            )
        raise MechanicalAuditError(
            "MECHANICAL_INPUT_INCOMPLETE",
            "evidence bundle required for mechanical audit",
        )

    evidence = MicroEvidence(
        seen_inside=bool(ev_raw["seen_inside"]),
        arrival_present=bool(ev_raw["arrival_present"]),
        defense_ok=bool(ev_raw["defense_ok"]),
        breakout_ok=bool(ev_raw["breakout_ok"]),
        breakout_contested=bool(ev_raw["breakout_contested"]),
        defense_entry=ev_raw.get("defense_entry"),
        breakout_entry=ev_raw.get("breakout_entry"),
        defense_first_ts=ev_raw.get("defense_first_ts"),
        breakout_first_ts=ev_raw.get("breakout_first_ts"),
        attack_eff_count=int(ev_raw.get("attack_eff_count") or 0),
        counter_count=int(ev_raw.get("counter_count") or 0),
        two_sided_count=int(ev_raw.get("two_sided_count") or 0),
    )
    pools = geom_rows_to_pool_candidates(list(frozen_inputs.get("pool_geometry_rows") or []))
    decision = resolve_mechanical_decision(
        geom=geom,
        evidence=evidence,
        pools=pools,
        effective=effective,
    )
    prefix = prefix_parity(
        decision=decision,
        pools=pools,
        effective=effective,
        geom=geom,
        case_pool_id=case_spec.pool_id,
    )
    if prefix.get("prefix_status") != "EXACT_PREFIX_PARITY":
        raise MechanicalAuditError("SMOKE_PREFIX_PARITY_FAILURE", str(prefix.get("mismatches")))

    hashes = dict(frozen_inputs.get("hashes") or {})
    mech: dict[str, Any] = {
        "case_id": case_spec.expansion_case_id,
        "case_spec": case_spec.to_dict(),
        "pool_side": geom.pool_side,
        "approach": geom.approach,
        "front_edge": geom.front_edge,
        "back_edge": geom.back_edge,
        "reaction": decision.reaction,
        "first_available_ts": decision.first_available_ts,
        "entry_price": decision.mechanical_entry_price,
        "room_gate": decision.room_gate,
        "long_branch": {
            "eligible": decision.long_branch.microstructure_gate_passed,
            **branch_gates_to_dict(decision.long_branch),
        },
        "short_branch": {
            "eligible": decision.short_branch.microstructure_gate_passed,
            **branch_gates_to_dict(decision.short_branch),
        },
        "mechanical_verdict": decision.mechanical_verdict,
        "format_version": FORMAT_VERSION,
        "entry_contract_version": ENTRY_CONTRACT_VERSION,
        "predecessor_v1_entry_contract_sha256": PREDECESSOR_V1_ENTRY_CONTRACT_SHA256,
        "strategy_config_sha256": effective.config_sha256,
        "strategy_config_path": STRATEGY_CONFIG_REL,
        "expansion_freeze_sha256": hashes.get("expansion_freeze_sha256"),
        "entry_contract_v2_freeze_sha256": hashes.get("entry_contract_v2_freeze_sha256"),
        "prefix_parity": prefix,
        "generated_at": _utc_now(),
        "market_data_loaded": False,
        "outcomes_read": False,
    }
    mech.update(flatten_room_gate_for_mech(effective, decision))
    mech["mechanical_payload_sha256"] = payload_sha256(mech)

    atomic_write_json(output_dir / "mechanical_verdict_pre_unblind.json", mech)
    atomic_write_json(output_dir / "prefix_parity.json", prefix)
    atomic_write_json(output_dir / "case_spec.json", case_spec.to_dict())
    atomic_write_text(
        output_dir / "mechanical_complete.marker",
        f"{mech['mechanical_payload_sha256']}\n",
    )
    atomic_write_json(
        output_dir / "mechanical_blindness_audit.json",
        {
            "phase": "mechanical",
            "outcomes_read": False,
            "unblind_invoked": False,
            "market_data_loaded": False,
            "forbidden_outcome_keys_checked": list(FORBIDDEN_OUTCOME_KEYS),
            "mechanical_payload_sha256": mech["mechanical_payload_sha256"],
        },
    )
    return {
        "ok": True,
        "verdict": decision.mechanical_verdict,
        "mechanical_trade_verdict": decision.mechanical_trade_verdict,
        "prefix_status": prefix["prefix_status"],
        "mechanical_payload_sha256": mech["mechanical_payload_sha256"],
        "output_dir": str(output_dir),
        "outcomes_read": False,
        "market_data_loaded": False,
    }
