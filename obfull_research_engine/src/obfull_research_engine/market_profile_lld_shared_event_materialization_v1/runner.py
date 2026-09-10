"""Pilot materialization for the three Phase-1 audit timestamps."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..timeparse import parse_utc_z
from . import CONTRACT_NAME, CONTRACT_VERSION, PILOT_CASES
from .chart_hook_plan import HOOK_PLAN
from .hashing import file_sha256, sha256_hex
from .lld_serialize import chart_lld_generator_identity, config_hash as lld_config_hash
from .lld_serialize import materialize_lld_snapshot
from .mp_serialize import chart_mp_generator_identity, config_hash as mp_config_hash
from .mp_serialize import materialize_market_profile_case
from .persist import (
    RESULTS_ROOT,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    compute_run_key,
    load_manifest,
    mark,
    new_manifest,
    run_dir,
)
from .report import render_report


def frozen_run_config() -> dict[str, Any]:
    cases = [
        {
            "case_id": c["case_id"],
            "symbol": c["symbol"],
            "request_as_of": c["request_as_of"],
            "timeframe": c["timeframe"],
            "profile_state": c["profile_state"],
        }
        for c in PILOT_CASES
    ]
    cfg = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "symbol": "BTCUSDT",
        "cases": cases,
        "mp_config_hash": mp_config_hash(),
        "lld_config_hash": lld_config_hash(),
        "lld_source_timeframe": "15m",
        "clickhouse_writes": False,
        "parity_class": "SHARED_GENERATOR_PARITY_PROVEN",
        "historical_rendered_chart_payload_parity": False,
    }
    cfg["config_hash"] = sha256_hex(cfg)
    return cfg


def _tpo_delta(event: dict[str, Any], expected: dict[str, float]) -> dict[str, Any]:
    step = float(event.get("price_bin_size") or 0.0)
    rows = {}
    fail = False
    for key, exp in (("tpo_poc", expected["poc"]), ("tpo_vah", expected["vah"]), ("tpo_val", expected["val"])):
        got = float(event[key])
        delta = abs(got - float(exp))
        ok = delta <= max(step, 0.0)
        rows[key] = {"expected": exp, "actual": got, "delta": delta, "tolerance": step, "ok": ok}
        if not ok:
            fail = True
    return {"ok": not fail, "fields": rows, "price_bin_size": step}


def _write_complete(directory, manifest, config, mp_rows, lld_snaps, lld_zones, parity) -> None:
    atomic_write_json(directory / "config.json", config)
    atomic_write_jsonl(directory / "market_profile_events.jsonl", [r["event"] for r in mp_rows])
    atomic_write_jsonl(directory / "lld_snapshots.jsonl", [r["snapshot"] for r in lld_snaps])
    atomic_write_jsonl(directory / "lld_zone_events.jsonl", lld_zones)
    atomic_write_json(directory / "parity" / "generator_identity.json", {
        "market_profile": chart_mp_generator_identity(),
        "lld": chart_lld_generator_identity(),
        "second_algorithm": False,
    })
    atomic_write_json(directory / "parity" / "mp_regression.json", parity)
    atomic_write_json(directory / "parity" / "chart_payload_hook_plan.json", HOOK_PLAN)
    src = directory / "source_payloads"
    for row in mp_rows:
        cid = row["event"]["case_id"]
        atomic_write_json(src / f"mp_{cid}.json", {
            "raw_profile": row["raw_profile"],
            "raw_payload_hash": row["raw_payload_hash"],
            "service_meta": row["raw_service_payload_meta"],
            "naked_poc_sidecar": row["naked_poc_sidecar"],
            "bounds": row["bounds"],
        })
    for row in lld_snaps:
        cid = row["snapshot"]["case_id"]
        atomic_write_json(src / f"lld_{cid}.json", {
            "raw_pools": row["raw_pools"],
            "raw_payload_hash": row["raw_payload_hash"],
            "engine_end_as_of": row["engine_end_as_of"],
        })
    hashes = {
        "config.json": file_sha256(directory / "config.json"),
        "market_profile_events.jsonl": file_sha256(directory / "market_profile_events.jsonl"),
        "lld_snapshots.jsonl": file_sha256(directory / "lld_snapshots.jsonl"),
        "lld_zone_events.jsonl": file_sha256(directory / "lld_zone_events.jsonl"),
    }
    mark(
        manifest,
        "COMPLETE",
        output_hashes=hashes,
        verdict=parity.get("verdict"),
        shared_generator_parity="SHARED_GENERATOR_PARITY_PROVEN",
        historical_rendered_chart_payload_parity="NOT_PROVEN",
    )
    atomic_write_json(directory / "manifest.json", manifest)
    atomic_write_text(directory / "report.md", render_report(manifest, config, mp_rows, lld_snaps, lld_zones, parity))


def run_pilot(*, resume: bool = True) -> dict[str, Any]:
    config = frozen_run_config()
    key = compute_run_key(config)
    directory = run_dir("BTCUSDT", key)
    directory.mkdir(parents=True, exist_ok=True)
    man_path = directory / "manifest.json"
    existing = load_manifest(man_path)
    if resume and existing and existing.get("status") == "COMPLETE":
        return {
            "status": "COMPLETE",
            "reused": True,
            "run_key": key,
            "run_dir": str(directory),
            "manifest": existing,
            "verdict": existing.get("verdict"),
        }

    manifest = new_manifest(symbol="BTCUSDT", run_key=key, directory=directory, config=config)
    atomic_write_json(man_path, manifest)
    mp_rows: list[dict[str, Any]] = []
    lld_snaps: list[dict[str, Any]] = []
    lld_zones: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    try:
        for case in PILOT_CASES:
            as_of = parse_utc_z(case["request_as_of"], field="request_as_of")
            mp = materialize_market_profile_case(
                symbol=case["symbol"],
                request_as_of=as_of,
                timeframe=case["timeframe"],
                requested_state=case["profile_state"],
                case_id=case["case_id"],
            )
            mp_rows.append(mp)
            reg = _tpo_delta(mp["event"], case["expected_tpo"])
            reg["case_id"] = case["case_id"]
            reg["timeframe"] = case["timeframe"]
            reg["effective_profile_end"] = mp["event"]["effective_profile_end"]
            reg["natural_profile_end"] = mp["event"]["natural_profile_end"]
            reg["price_bin_size"] = mp["event"]["price_bin_size"]
            reg["tick_size"] = mp["event"]["tick_size"]
            reg["source_version"] = mp["event"]["source_version"]
            regressions.append(reg)
            lld = materialize_lld_snapshot(
                symbol=case["symbol"],
                request_as_of=as_of,
                case_id=case["case_id"],
            )
            lld_snaps.append(lld)
            lld_zones.extend(lld["zones"])

        fail = [r for r in regressions if not r["ok"]]
        if fail:
            verdict = "READY_AFTER_MATERIALIZATION_FIX"
            parity = {
                "verdict": verdict,
                "shared_generator_parity": "SHARED_GENERATOR_MISMATCH",
                "regressions": regressions,
                "failed_cases": fail,
            }
            mark(manifest, "FAILED", verdict=verdict, error="mp_regression_mismatch")
            atomic_write_json(man_path, manifest)
            atomic_write_json(directory / "parity" / "mp_regression.json", parity)
            raise RuntimeError(f"MP regression mismatch: {json.dumps(fail, default=str)}")

        parity = {
            "verdict": "READY_FOR_BOUNDED_LEVEL_EVENT_PILOT",
            "shared_generator_parity": "SHARED_GENERATOR_PARITY_PROVEN",
            "historical_rendered_chart_payload_parity": "NOT_PROVEN",
            "regressions": regressions,
        }
        _write_complete(directory, manifest, config, mp_rows, lld_snaps, lld_zones, parity)
        return {
            "status": "COMPLETE",
            "reused": False,
            "run_key": key,
            "run_dir": str(directory),
            "verdict": parity["verdict"],
            "manifest": manifest,
            "parity": parity,
        }
    except Exception as exc:  # noqa: BLE001
        if manifest.get("status") != "FAILED":
            mark(manifest, "FAILED", error=f"{type(exc).__name__}: {exc}")
            atomic_write_json(man_path, manifest)
        raise


def result_root() -> Any:
    return RESULTS_ROOT
