"""Build / verify outcome-blind liquidity-pool case-sequence freeze v1."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_case_sequence_freeze_v1 import (
    CASE_02_DEEP_AUDITS,
    CASE_06_DEEP_AUDITS,
    FORBIDDEN_FIELD_SUBSTR,
    FREEZE_SCOPE,
    REFERENCE_SOURCE_FIELD,
    SCHEMA_VERSION,
    SIX_CASE_SAMPLE,
    SOURCE_REL,
)

CASE_ID_RE = re.compile(r"^CASE_(\d{2})$")


class FreezeError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_bytes(obj: Any) -> bytes:
    """UTF-8 JSON, sort_keys=True, separators=(',', ':')."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def case_id_num(case_id: str) -> int:
    m = CASE_ID_RE.match(case_id)
    if not m:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", f"bad case_id {case_id}")
    return int(m.group(1))


def parse_utc_z(ts: str) -> datetime:
    if not isinstance(ts, str) or not ts.endswith("Z"):
        raise FreezeError("REFERENCE_TS_SEMANTICS_AMBIGUOUS", f"non-UTC-Z timestamp: {ts!r}")
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def find_unique_six_case_source(repo_root: Path) -> Path:
    """Exactly one selection_manifest containing CASE_01..CASE_06."""
    hits: list[Path] = []
    for p in (repo_root / "results").rglob("selection_manifest.json"):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        cases = obj.get("cases") if isinstance(obj, dict) else None
        if not isinstance(cases, list):
            continue
        ids = {c.get("case_id") for c in cases if isinstance(c, dict)}
        if {"CASE_01", "CASE_02", "CASE_03", "CASE_04", "CASE_05", "CASE_06"} <= ids:
            hits.append(p)
    if len(hits) == 0:
        raise FreezeError("CASE_SEQUENCE_SOURCE_NOT_UNAMBIGUOUS", "no six-case selection_manifest found")
    if len(hits) > 1:
        raise FreezeError(
            "CASE_SEQUENCE_SOURCE_NOT_UNAMBIGUOUS",
            f"multiple six-case manifests: {[str(h) for h in hits]}",
        )
    return hits[0]


def source_field_inventory(cases: list[dict[str, Any]]) -> dict[str, Any]:
    keys: set[str] = set()
    for c in cases:
        keys.update(c.keys())
    ts_fields = sorted(k for k in keys if k.endswith("_ts") or k.endswith("_ts_raw"))
    forbidden_present = sorted(
        k for k in keys if any(f in k.lower() for f in FORBIDDEN_FIELD_SUBSTR)
    )
    return {
        "all_case_fields": sorted(keys),
        "timestamp_fields": ts_fields,
        "forbidden_outcome_like_fields_in_cases": forbidden_present,
        "missing_fields_relative_to_freeze_needs": [
            x
            for x in ("symbol", "freeze_hash", "reference_ts", "exposure_status")
            if x not in keys
        ],
    }


def resolve_reference_ts_policy(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick exactly one selection-event field; reject if ambiguous across cases."""
    # Documented candidates and exclusion rules (same for every case).
    candidates = {
        "cluster_start_ts": "selection/ranking event = market arrival cluster start",
        "load_start_ts": "derived pre-window (start - 30s); not selection event",
        "causal_window_end_ts": "post-start analysis cap; forbidden for ranking in source",
        "cluster_end_ts_raw": "post-start cluster end; not selection event",
    }
    present = set()
    for c in cases:
        for k in candidates:
            if k in c:
                present.add(k)
    if REFERENCE_SOURCE_FIELD not in present:
        raise FreezeError(
            "REFERENCE_TS_SEMANTICS_AMBIGUOUS",
            f"required field {REFERENCE_SOURCE_FIELD} missing",
        )
    # Ambiguity only if cases disagree on which field holds the selection event
    # or values are non-uniformly structured. All cases share the same four fields.
    for c in cases:
        missing = [k for k in ("cluster_start_ts", "load_start_ts", "causal_window_end_ts", "cluster_end_ts_raw") if k not in c]
        if missing:
            raise FreezeError(
                "REFERENCE_TS_SEMANTICS_AMBIGUOUS",
                f"{c.get('case_id')} missing time fields {missing}",
            )
        parse_utc_z(str(c[REFERENCE_SOURCE_FIELD]))
    return {
        "source_field": REFERENCE_SOURCE_FIELD,
        "timezone": "UTC",
        "transformation": "EXACT_COPY",
        "parsing_rule": "ISO-8601 with trailing Z → UTC",
        "candidate_fields_reviewed": candidates,
        "exclusion_rules": {
            "load_start_ts": "derived window start, not selection event",
            "causal_window_end_ts": "post-start bound",
            "cluster_end_ts_raw": "post-start bound",
        },
        "selected_reason": (
            "cluster_start_ts is the selection-time market-arrival event used for "
            "ranking fields and chronological case_id assignment in the source manifest"
        ),
    }


def check_order_parity(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = sorted(cases, key=lambda c: case_id_num(str(c["case_id"])))
    by_time = sorted(cases, key=lambda c: parse_utc_z(str(c[REFERENCE_SOURCE_FIELD])))
    id_order = [c["case_id"] for c in by_id]
    time_order = [c["case_id"] for c in by_time]
    if id_order != time_order:
        raise FreezeError(
            "CASE_SEQUENCE_ORDER_CONFLICT",
            f"case_id_order={id_order} chrono_order={time_order}",
        )
    return {
        "case_id_numeric_asc": id_order,
        "chronological_by_cluster_start_ts": time_order,
        "orders_identical": True,
    }


def symbol_from_case(case: dict[str, Any]) -> str:
    mids = str(case.get("member_pool_ids") or "")
    # lld:BTCUSDT:5m:...
    parts = mids.split(":")
    if len(parts) >= 2 and parts[0] == "lld":
        return parts[1]
    raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "cannot derive symbol")


def exposure_for_case(case_id: str, repo_root: Path) -> dict[str, Any]:
    """Exposure status; does not affect ordering."""
    paths: list[str] = []
    status = "PROSPECTIVE_UNAUDITED"

    def exists(rel: str) -> bool:
        return (repo_root / rel).exists()

    if case_id == "CASE_01":
        # Explicitly pre-exposed (shared six-case short sample + prior analysis).
        if exists(SIX_CASE_SAMPLE):
            paths.append(SIX_CASE_SAMPLE)
        status = "PRE_FREEZE_EXPOSED"
    elif case_id == "CASE_02":
        for rel in CASE_02_DEEP_AUDITS:
            if exists(rel):
                paths.append(rel)
        if exists(SIX_CASE_SAMPLE):
            paths.append(SIX_CASE_SAMPLE)
        status = "PRE_FREEZE_EXPOSED"
    elif case_id == "CASE_06":
        # Comparable deep Einzelfall at same reference cluster start.
        for rel in CASE_06_DEEP_AUDITS:
            if exists(rel):
                paths.append(rel)
        if paths:
            status = "PRE_FREEZE_EXPOSED"
            if exists(SIX_CASE_SAMPLE):
                paths.append(SIX_CASE_SAMPLE)
        else:
            status = "PROSPECTIVE_UNAUDITED"
    else:
        # CASE_03..05: six-case short sample alone is NOT a comparable deep audit.
        status = "PROSPECTIVE_UNAUDITED"
        # Do not list six-case sample as deep audit path.

    return {"exposure_status": status, "existing_audit_paths": paths}


def build_frozen_sequence(repo_root: Path, *, created_at_utc: str | None = None) -> dict[str, Any]:
    source = find_unique_six_case_source(repo_root)
    expected = (repo_root / SOURCE_REL).resolve()
    if source.resolve() != expected:
        # Still unique, but path differs from locked relative path — treat as ambiguous.
        raise FreezeError(
            "CASE_SEQUENCE_SOURCE_NOT_UNAMBIGUOUS",
            f"unique hit {source} != locked {expected}",
        )
    raw = source.read_bytes()
    source_sha = sha256_bytes(raw)
    manifest = json.loads(raw.decode("utf-8"))
    cases = list(manifest["cases"])
    if len(cases) != 6:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", f"case_count={len(cases)}")

    inv = source_field_inventory(cases)
    if inv["forbidden_outcome_like_fields_in_cases"]:
        raise FreezeError(
            "CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE",
            f"source cases contain forbidden fields: {inv['forbidden_outcome_like_fields_in_cases']}",
        )

    ref_policy = resolve_reference_ts_policy(cases)
    order_info = check_order_parity(cases)
    ordered = sorted(cases, key=lambda c: case_id_num(str(c["case_id"])))

    ordered_cases = []
    for i, c in enumerate(ordered, start=1):
        cid = str(c["case_id"])
        exp = exposure_for_case(cid, repo_root)
        ordered_cases.append(
            {
                "sequence_index": i,
                "case_id": cid,
                "symbol": symbol_from_case(c),
                "direction": str(c["side"]),
                "approach": str(c["approach_direction"]),
                "reference_ts": str(c[REFERENCE_SOURCE_FIELD]),
                "market_arrival_cluster_id": str(c["market_arrival_cluster_id"]),
                "component_lower_edge": float(c["component_lower_edge"]),
                "component_upper_edge": float(c["component_upper_edge"]),
                "exposure_status": exp["exposure_status"],
                "existing_audit_paths": list(exp["existing_audit_paths"]),
            }
        )

    next_after = {
        "CASE_01": "CASE_02",
        "CASE_02": "CASE_03",
        "CASE_03": "CASE_04",
        "CASE_04": "CASE_05",
        "CASE_05": "CASE_06",
        "CASE_06": None,
    }

    frozen = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": created_at_utc or _utc_now_iso(),
        "freeze_scope": FREEZE_SCOPE,
        "source_manifest": {
            "path_relative": SOURCE_REL,
            "path_absolute": str(source.resolve()),
            "sha256": source_sha,
            "case_count": 6,
            "source_field_inventory": inv,
            "order_parity": order_info,
        },
        "selection_policy": {
            "case_membership": "SOURCE_MANIFEST_EXACT",
            "ordering": "CASE_ID_NUMERIC_ASC",
            "outcome_used_for_membership": False,
            "outcome_used_for_ordering": False,
            "outcome_used_for_reference_ts": False,
            "outcome_used_for_exposure_status": False,
        },
        "reference_ts_policy": ref_policy,
        "ordered_cases": ordered_cases,
        "next_after": next_after,
    }
    assert_no_forbidden_keys(frozen)
    return frozen


def hashable_sequence_payload(frozen: dict[str, Any]) -> dict[str, Any]:
    """Drop volatile fields before hashing the sequence document."""
    payload = json.loads(json.dumps(frozen))  # deep copy via JSON
    payload.pop("created_at_utc", None)
    payload.pop("freeze_bundle_sha256", None)
    return payload


def compute_hashes(frozen: dict[str, Any], source_sha: str) -> dict[str, str]:
    seq_payload = hashable_sequence_payload(frozen)
    frozen_sequence_sha256 = sha256_bytes(canonical_json_bytes(seq_payload))
    bundle_payload = {
        "frozen_sequence_sha256": frozen_sequence_sha256,
        "schema_version": SCHEMA_VERSION,
        "selection_policy": frozen["selection_policy"],
        "reference_ts_policy": {
            "source_field": frozen["reference_ts_policy"]["source_field"],
            "timezone": frozen["reference_ts_policy"]["timezone"],
            "transformation": frozen["reference_ts_policy"]["transformation"],
        },
        "ordered_cases": frozen["ordered_cases"],
        "next_after": frozen["next_after"],
        "source_manifest_sha256": source_sha,
        "freeze_scope": FREEZE_SCOPE,
    }
    freeze_bundle_sha256 = sha256_bytes(canonical_json_bytes(bundle_payload))
    return {
        "source_manifest_sha256": source_sha,
        "frozen_sequence_sha256": frozen_sequence_sha256,
        "freeze_bundle_sha256": freeze_bundle_sha256,
    }


def assert_no_forbidden_keys(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            # allow existing_audit_paths / exposure_status / selection policy booleans
            if k in (
                "existing_audit_paths",
                "exposure_status",
                "outcome_used_for_membership",
                "outcome_used_for_ordering",
                "outcome_used_for_reference_ts",
                "outcome_used_for_exposure_status",
                "forbidden_outcome_like_fields_in_cases",
            ):
                assert_no_forbidden_keys(v, f"{path}.{k}")
                continue
            if any(f in lk for f in FORBIDDEN_FIELD_SUBSTR):
                raise FreezeError(
                    "CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE",
                    f"forbidden key {path}.{k}",
                )
            assert_no_forbidden_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_no_forbidden_keys(v, f"{path}[{i}]")


def validate_frozen(frozen: dict[str, Any]) -> None:
    cases = frozen["ordered_cases"]
    ids = [c["case_id"] for c in cases]
    if ids != [f"CASE_{i:02d}" for i in range(1, 7)]:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", f"ids={ids}")
    if len(set(ids)) != 6:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "duplicate case_id")
    for i, c in enumerate(cases, start=1):
        if c["sequence_index"] != i:
            raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "sequence_index")
        parse_utc_z(c["reference_ts"])
    na = frozen["next_after"]
    expected = {
        "CASE_01": "CASE_02",
        "CASE_02": "CASE_03",
        "CASE_03": "CASE_04",
        "CASE_04": "CASE_05",
        "CASE_05": "CASE_06",
        "CASE_06": None,
    }
    if na != expected:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", f"next_after={na}")
    # acyclic
    seen = set()
    cur: str | None = "CASE_01"
    while cur is not None:
        if cur in seen:
            raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "cycle in next_after")
        seen.add(cur)
        cur = na[cur]
    if na["CASE_06"] is not None:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "last must be null")
    assert_no_forbidden_keys(frozen)


def build_freeze_manifest(hashes: dict[str, str], frozen: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "liquidity_pool_case_sequence_freeze_manifest/v1",
        "hash_algorithm": "sha256",
        "canonicalization": {
            "encoding": "utf-8",
            "format": "json",
            "sort_keys": True,
            "separators": [",", ":"],
            "excluded_from_sequence_hash": ["created_at_utc", "freeze_bundle_sha256"],
            "bundle_hash_includes": [
                "frozen_sequence_sha256",
                "schema_version",
                "selection_policy",
                "reference_ts_policy(source_field,timezone,transformation)",
                "ordered_cases",
                "next_after",
                "source_manifest_sha256",
                "freeze_scope",
            ],
            "bundle_hash_excludes_self": True,
        },
        "source_manifest_sha256": hashes["source_manifest_sha256"],
        "frozen_sequence_sha256": hashes["frozen_sequence_sha256"],
        "freeze_bundle_sha256": hashes["freeze_bundle_sha256"],
        "included_files": [
            "frozen_case_sequence_v1.json",
            "freeze_manifest.json",
            "FREEZE_REPORT.md",
        ],
        "frozen_case_count": len(frozen["ordered_cases"]),
        "next_after_case_02": frozen["next_after"]["CASE_02"],
        "outcome_used_for_membership": False,
        "outcome_used_for_ordering": False,
        "outcome_used_for_reference_ts": False,
    }


def write_freeze(repo_root: Path, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frozen = build_frozen_sequence(repo_root)
    validate_frozen(frozen)
    hashes = compute_hashes(frozen, frozen["source_manifest"]["sha256"])
    # Recompute twice for reproducibility check
    hashes2 = compute_hashes(frozen, frozen["source_manifest"]["sha256"])
    if hashes != hashes2:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "hash non-reproducible")

    freeze_manifest = build_freeze_manifest(hashes, frozen)
    # Attach hash only to manifest, not into sequence hash payload
    seq_path = out_dir / "frozen_case_sequence_v1.json"
    man_path = out_dir / "freeze_manifest.json"
    seq_path.write_text(json.dumps(frozen, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    man_path.write_text(json.dumps(freeze_manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    report = render_report(repo_root, frozen, freeze_manifest, hashes)
    (out_dir / "FREEZE_REPORT.md").write_text(report, encoding="utf-8")

    # Verify immediately
    verify_freeze(repo_root, out_dir)

    return {
        "verdict": "CASE_SEQUENCE_FREEZE_V1_COMPLETE",
        "freeze_bundle_sha256": hashes["freeze_bundle_sha256"],
        "next_after_case_02": frozen["next_after"]["CASE_02"],
        "out_dir": str(out_dir),
    }


def verify_freeze(repo_root: Path, out_dir: Path) -> dict[str, Any]:
    seq_path = out_dir / "frozen_case_sequence_v1.json"
    man_path = out_dir / "freeze_manifest.json"
    if not seq_path.exists() or not man_path.exists():
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "missing freeze files")

    frozen = json.loads(seq_path.read_text(encoding="utf-8"))
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    validate_frozen(frozen)

    source = repo_root / SOURCE_REL
    live_source_sha = sha256_file(source)
    if live_source_sha != frozen["source_manifest"]["sha256"]:
        raise FreezeError(
            "CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE",
            "source_manifest.json changed since freeze",
        )
    if live_source_sha != manifest["source_manifest_sha256"]:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "manifest source sha mismatch")

    hashes = compute_hashes(frozen, live_source_sha)
    if hashes["frozen_sequence_sha256"] != manifest["frozen_sequence_sha256"]:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "frozen_sequence_sha256 mismatch")
    if hashes["freeze_bundle_sha256"] != manifest["freeze_bundle_sha256"]:
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "freeze_bundle_sha256 mismatch")

    # Rebuild from source and compare identity fields (ignore created_at)
    rebuilt = build_frozen_sequence(repo_root, created_at_utc=frozen["created_at_utc"])
    validate_frozen(rebuilt)
    if hashable_sequence_payload(rebuilt) != hashable_sequence_payload(frozen):
        raise FreezeError("CASE_SEQUENCE_FREEZE_INTEGRITY_FAILURE", "rebuild mismatch")

    return {"ok": True, "freeze_bundle_sha256": hashes["freeze_bundle_sha256"]}


def render_report(
    repo_root: Path,
    frozen: dict[str, Any],
    freeze_manifest: dict[str, Any],
    hashes: dict[str, str],
) -> str:
    cases = frozen["ordered_cases"]
    lines = [
        "# FREEZE_REPORT — liquidity_pool_case_sequence_freeze_v1",
        "",
        "## 1. Verdict",
        "`CASE_SEQUENCE_FREEZE_V1_COMPLETE`",
        "",
        "## 2. HEAD note",
        "See shell `git rev-parse HEAD` at freeze time. No commit/push performed by this freeze.",
        "",
        "## 3. Live safety",
        "- No ClickHouse writes",
        "- No collector/live changes",
        "- Source `selection_manifest.json` not modified",
        "- Outputs only under `results/liquidity_pool_case_sequence_freeze_v1/`",
        "",
        "## 4. Source manifest",
        f"- relative: `{frozen['source_manifest']['path_relative']}`",
        f"- absolute: `{frozen['source_manifest']['path_absolute']}`",
        f"- sha256: `{hashes['source_manifest_sha256']}`",
        f"- case_count: {frozen['source_manifest']['case_count']}",
        f"- source fields: `{frozen['source_manifest']['source_field_inventory']['all_case_fields']}`",
        f"- timestamp fields: `{frozen['source_manifest']['source_field_inventory']['timestamp_fields']}`",
        f"- missing vs freeze needs: `{frozen['source_manifest']['source_field_inventory']['missing_fields_relative_to_freeze_needs']}`",
        f"- outcome/verdict/pnl fields in cases: `{frozen['source_manifest']['source_field_inventory']['forbidden_outcome_like_fields_in_cases']}` (must be empty)",
        "",
        "## 5. Ordering rule",
        "`CASE_ID_NUMERIC_ASC` (CASE_01 … CASE_06)",
        f"- chronology by `{REFERENCE_SOURCE_FIELD}` identical: `{frozen['source_manifest']['order_parity']['orders_identical']}`",
        "",
        "## 6. reference_ts semantics",
        f"- source_field: `{frozen['reference_ts_policy']['source_field']}`",
        f"- timezone: `{frozen['reference_ts_policy']['timezone']}`",
        f"- transformation: `{frozen['reference_ts_policy']['transformation']}`",
        f"- parsing: `{frozen['reference_ts_policy']['parsing_rule']}`",
        f"- reason: {frozen['reference_ts_policy']['selected_reason']}",
        "",
        "## 7. Ordered cases",
        "| seq | case_id | symbol | direction | approach | reference_ts | exposure |",
        "|-----|---------|--------|-----------|----------|--------------|----------|",
    ]
    for c in cases:
        lines.append(
            f"| {c['sequence_index']} | {c['case_id']} | {c['symbol']} | {c['direction']} | "
            f"{c['approach']} | {c['reference_ts']} | {c['exposure_status']} |"
        )
    lines += [
        "",
        "## 8. Exposure status detail",
    ]
    for c in cases:
        lines.append(f"- **{c['case_id']}**: `{c['exposure_status']}` paths={c['existing_audit_paths']}")
    lines += [
        "",
        "## 9. next_after",
        "```json",
        json.dumps(frozen["next_after"], indent=2),
        "```",
        "",
        "## 10. Hashes",
        f"- source_manifest_sha256: `{hashes['source_manifest_sha256']}`",
        f"- frozen_sequence_sha256: `{hashes['frozen_sequence_sha256']}`",
        f"- freeze_bundle_sha256: `{hashes['freeze_bundle_sha256']}`",
        "",
        "## 11. Integrity",
        "Verify via `scripts/verify_liquidity_pool_case_sequence_freeze_v1.py` (non-zero on any drift).",
        "",
        "## 12. Next prospective audit",
        f"`CASE_02 -> {frozen['next_after']['CASE_02']}` is now mechanically defined by this freeze.",
        "CASE_03 deep-audit is **not** started by this freeze.",
        "",
        "## 13. Output paths",
        f"- `{repo_root / 'results/liquidity_pool_case_sequence_freeze_v1/frozen_case_sequence_v1.json'}`",
        f"- `{repo_root / 'results/liquidity_pool_case_sequence_freeze_v1/freeze_manifest.json'}`",
        f"- `{repo_root / 'results/liquidity_pool_case_sequence_freeze_v1/FREEZE_REPORT.md'}`",
        "",
    ]
    return "\n".join(lines)
