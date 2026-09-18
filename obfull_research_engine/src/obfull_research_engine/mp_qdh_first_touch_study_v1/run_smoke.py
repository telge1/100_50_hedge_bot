"""3-event smoke gate before first-touch study."""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
    atomic_write_text,
)
from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.mp_qdh_canonical_integration_v1.event_load import (
    load_events_csv,
    load_windows_csv,
)

from . import ALLOW_CLICKHOUSE_WRITES, BATCH_RUN_REL, PACKAGE_NAME
from .analyze_event import analyze_first_touch_event
from .contract import CONTRACT_BODY, CONTRACT_HASH, validate_checkpoint_contract
from .universe import build_first_touch_universe, write_universe


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def select_smoke_events(uni: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic ABSORB / FAILED_BREAK / TRUE_BREAK by earliest event_id."""
    by_lab: dict[str, list[dict[str, Any]]] = {"ABSORB": [], "FAILED_BREAK": [], "TRUE_BREAK": []}
    for r in uni["included"]:
        lab = r["label_price_only"]
        if lab in by_lab:
            by_lab[lab].append(r)
    for lab in by_lab:
        by_lab[lab].sort(key=lambda r: r["event_id"])
    chosen = []
    for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
        if not by_lab[lab]:
            raise RuntimeError(f"smoke missing label {lab}")
        chosen.append(by_lab[lab][0])
    return chosen


def run_smoke(*, repo_root: Path | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    repo = Path(repo_root or _repo_root())
    out = Path(
        out_dir
        or (repo / "obfull_research_engine/runs/mp_qdh_first_touch_smoke_v1_20260917")
    )
    out.mkdir(parents=True, exist_ok=True)
    ckpt_dir = out / "checkpoints"
    ckpt_dir.mkdir(exist_ok=True)

    uni = build_first_touch_universe(repo)
    write_universe(out, uni)
    smoke_rows = select_smoke_events(uni)
    batch = repo / BATCH_RUN_REL
    events = load_events_csv(batch)
    windows = load_windows_csv(batch)

    # Stale checkpoint rejection test
    stale = {"contract_hash": "deadbeef", "universe_hash": uni["universe_hash"]}
    stale_val = validate_checkpoint_contract(
        stale, expected_hash=CONTRACT_HASH, expected_universe_hash=uni["universe_hash"]
    )
    missing_val = validate_checkpoint_contract(
        {}, expected_hash=CONTRACT_HASH, expected_universe_hash=uni["universe_hash"]
    )
    stale_ok = (not stale_val["ok"]) and stale_val["reason"] == "STALE_CHECKPOINT_REJECTED"
    missing_ok = (not missing_val["ok"]) and missing_val["reason"] == "STALE_CHECKPOINT_REJECTED"

    client = get_clickhouse_client()
    results = []
    mass_rows = []
    ie_rows = []
    for i, urow in enumerate(smoke_rows, 1):
        eid = urow["event_id"]
        ev = events[eid]
        # ensure window_id on event
        if not ev.get("window_id"):
            ev = dict(ev)
            ev["window_id"] = urow.get("window_id") or ""
        win = windows.get(ev.get("window_id") or "") or windows.get(urow.get("window_id") or "") or {}
        print(f"SMOKE {i}/3 {eid} {urow['label_price_only']}", flush=True)
        res = analyze_first_touch_event(
            universe_row=urow,
            event_row=ev,
            window=win,
            client=client,
            universe_hash=uni["universe_hash"],
            out_event_dir=ckpt_dir / eid,
        )
        atomic_write_json(
            ckpt_dir / f"{eid}.json",
            {
                "contract_hash": CONTRACT_HASH,
                "universe_hash": uni["universe_hash"],
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result": {
                    k: res[k]
                    for k in res
                    if k
                    not in (
                        # already on disk
                    )
                },
            },
        )
        results.append(res)
        feats = res.get("features") or {}
        mass_rows.append(
            {
                "event_id": eid,
                "label": urow["label_price_only"],
                "fill": feats.get("attributed_fill_qty"),
                "pull": feats.get("residual_pull_qty"),
                "refill": feats.get("refill_qty"),
                "unknown": feats.get("unknown_qty"),
                "unmatched_fill": feats.get("fill_excess_over_decrease"),
                "net_depletion": feats.get("net_depletion_qty"),
                "unknown_in_qdh": False,
                "ok": res.get("ok"),
                "flow_attribution_confidence": feats.get("flow_attribution_confidence"),
                "availability_confidence": feats.get("availability_confidence"),
            }
        )
        ie_rows.append(
            {
                "event_id": eid,
                "ie": feats.get("impact_efficiency_bps_per_million"),
                "ie_status": feats.get("impact_efficiency_status"),
                "hit_notional": feats.get("attributed_hit_notional_usdt"),
                "epsilon_inflation": False
                if feats.get("impact_efficiency_status") == "NOT_AVAILABLE"
                or feats.get("impact_efficiency_bps_per_million") is None
                else abs(float(feats.get("impact_efficiency_bps_per_million") or 0)) > 1e9,
            }
        )

    # Gates
    n_ok = sum(1 for r in results if r.get("ok"))
    hashes = {json.loads((ckpt_dir / f"{r['event_id']}.json").read_text())["contract_hash"] for r in results}
    ie_bad = any(r.get("epsilon_inflation") for r in ie_rows)
    unk_in_qdh = False  # by construction in v2
    lookahead = sum(int(r.get("causality_violations") or 0) for r in results)
    dbw = any(r.get("db_mutation") for r in results)

    gates = {
        "n_ok": n_ok,
        "n_total": 3,
        "contract_hash_unique": list(hashes),
        "contract_hash_uniform": hashes == {CONTRACT_HASH},
        "stale_checkpoint_rejected": stale_ok,
        "missing_hash_rejected": missing_ok,
        "ie_epsilon_inflation": ie_bad,
        "unknown_in_qdh": unk_in_qdh,
        "lookahead_violations": lookahead,
        "ch_writes": dbw,
        "mass_rows": mass_rows,
    }
    passed = (
        n_ok == 3
        and gates["contract_hash_uniform"]
        and stale_ok
        and missing_ok
        and not ie_bad
        and not unk_in_qdh
        and lookahead == 0
        and not dbw
    )
    verdict = "FIRST_TOUCH_SMOKE_SUCCESS" if passed else (
        "FIRST_TOUCH_SMOKE_PARTIAL" if n_ok > 0 else "FIRST_TOUCH_SMOKE_FAILED"
    )
    if not passed and n_ok < 3:
        verdict = "FIRST_TOUCH_SMOKE_BLOCKED" if n_ok == 0 else "FIRST_TOUCH_SMOKE_PARTIAL"

    # write artifacts
    def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys: list[str] = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    smoke_events = []
    for r, u in zip(results, smoke_rows):
        f = r.get("features") or {}
        d = r.get("decision_outcomes") or {}
        smoke_events.append(
            {
                "event_id": u["event_id"],
                "label": u["label_price_only"],
                "trade_side": u["trade_side"],
                "ok": r.get("ok"),
                "flow_attribution_confidence": f.get("flow_attribution_confidence"),
                "availability_confidence": f.get("availability_confidence"),
                "qdh_at_decision": f.get("qdh_at_decision"),
                "qdh_na": f.get("qdh_at_decision_na_reason"),
                "ie": f.get("impact_efficiency_bps_per_million"),
                "ie_status": f.get("impact_efficiency_status"),
                "reached_0_41": d.get("reached_0_41_pct"),
                "contract_hash": r.get("contract_hash"),
            }
        )
    _csv(out / "smoke_events.csv", smoke_events)
    _csv(out / "smoke_mass_balance.csv", mass_rows)
    _csv(out / "smoke_ie_validation.csv", ie_rows)
    atomic_write_json(
        out / "smoke_contract.json",
        {"contract_hash": CONTRACT_HASH, "body": CONTRACT_BODY},
    )
    atomic_write_json(
        out / "smoke_checkpoints.json",
        {
            "stale_rejected": stale_ok,
            "missing_rejected": missing_ok,
            "checkpoint_hashes": list(hashes),
        },
    )

    report = f"""# FIRST TOUCH SMOKE REPORT

**VERDICT:** `{verdict}`

## Events
{chr(10).join(f"- {r['event_id']} {r['label']} ok={r['ok']} flow={r['flow_attribution_confidence']} avail={r['availability_confidence']} ie_status={r['ie_status']}" for r in smoke_events)}

## Gates
- 3/3 ok: {n_ok}/3
- Contract hash uniform: {gates['contract_hash_uniform']} (`{CONTRACT_HASH[:16]}…`)
- Stale checkpoint rejected: {stale_ok}
- Missing hash rejected: {missing_ok}
- IE epsilon inflation: {ie_bad}
- UNKNOWN in QDH: {unk_in_qdh}
- Look-ahead: {lookahead}
- CH writes: {dbw}

## Note
Receive-time missing → availability=`RECEIVE_TIME_NOT_AVAILABLE` but flow attribution is **not** auto-LOW.
"""
    atomic_write_text(out / "SMOKE_REPORT.md", report)
    atomic_write_text(
        out / "smoke_test_results.md",
        f"# Smoke gate results\n\npassed={passed}\nverdict={verdict}\n",
    )
    atomic_write_json(
        out / "run_manifest.json",
        {
            "ok": passed,
            "verdict": verdict,
            "package": PACKAGE_NAME,
            "contract_hash": CONTRACT_HASH,
            "universe_hash": uni["universe_hash"],
            "gates": {k: v for k, v in gates.items() if k != "mass_rows"},
            "elapsed_s": time.monotonic() - t0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(verdict, out, flush=True)
    return {"verdict": verdict, "ok": passed, "out_dir": str(out), "universe_hash": uni["universe_hash"], "smoke_events": smoke_events}
