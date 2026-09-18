"""Batch pilot: scale QDH_base across stratified MP UPPER (ask-wall) events."""

from __future__ import annotations

import csv
import json
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
    atomic_write_text,
)
from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.mp_ob_feature_enrichment_v1.params import BATCH_RUN_REL
from obfull_research_engine.mp_wall_flow_qdh_silver_v1 import BTC_CHAIN_VERSION
from obfull_research_engine.timeparse import format_utc_z

from .analyze import analyze_mp_event
from .contract import (
    AUDIT_ID,
    PACKAGE_NAME,
    RUN_PREFIX,
    SCHEMA_VERSION,
    SILVER_DATABASE,
    default_gates,
)
from .event_spec import wall_price_from_mp_event


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _window_bounds(batch_dir: Path) -> dict[str, dict[str, Any]]:
    rows = _read_csv(batch_dir / "batch_windows.csv")
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if str(r.get("included")).lower() != "true":
            continue
        out[r["window_id"]] = {
            "start_ns": int(r["start_ns"]),
            "end_ns": int(r["end_ns"]),
            "start_ts": r.get("start_ts"),
            "end_ts": r.get("end_ts"),
            "replay_epoch": r.get("replay_epoch"),
            "chain_version": r.get("chain_version") or BTC_CHAIN_VERSION,
        }
    return out


def select_role_pilot(
    events: list[dict[str, str]],
    *,
    role: str,
    max_n: int = 20,
) -> list[dict[str, str]]:
    """Stratified events for one role (UPPER/LOWER) with touch+trigger."""
    role_u = str(role).upper()
    ready = [
        e
        for e in events
        if e.get("event_role") == role_u
        and e.get("first_touch_ts_ns") not in (None, "", "None")
        and e.get("trigger_ts_ns") not in (None, "", "None")
    ]
    by_key: dict[tuple, list[dict[str, str]]] = defaultdict(list)
    for e in ready:
        key = (
            e.get("window_id") or "",
            e.get("label_price_only") or "",
            (e.get("confluence_class") or "")[:2],
        )
        by_key[key].append(e)
    for bucket in by_key.values():
        bucket.sort(key=lambda r: int(r["first_touch_ts_ns"]))

    selected: list[dict[str, str]] = []
    keys = sorted(by_key.keys())
    while len(selected) < max_n and keys:
        progressed = False
        for k in list(keys):
            bucket = by_key[k]
            if not bucket:
                keys.remove(k)
                continue
            selected.append(bucket.pop(0))
            progressed = True
            if len(selected) >= max_n:
                break
        if not progressed:
            break
    return selected[:max_n]


def select_upper_pilot(
    events: list[dict[str, str]],
    *,
    max_n: int = 20,
) -> list[dict[str, str]]:
    """Stratified UPPER events with touch+trigger (ask-wall QDH path)."""
    return select_role_pilot(events, role="UPPER", max_n=max_n)


def _summary_row(event: dict[str, str], result: dict[str, Any]) -> dict[str, Any]:
    qdh = result.get("qdh_base") or {}
    man = qdh.get("manifest") or {}
    anchors = qdh.get("anchor_qdh") or man.get("anchor_qdh") or {}
    counts = qdh.get("counts") or man.get("counts") or {}
    side, px = wall_price_from_mp_event(event)
    reason = None
    if not result.get("ok"):
        reason = result.get("reason") or man.get("reason") or qdh.get("verdict")
    return {
        "event_id": event.get("event_id"),
        "window_id": event.get("window_id"),
        "event_role": event.get("event_role"),
        "label_price_only": event.get("label_price_only"),
        "confluence_class": event.get("confluence_class"),
        "wall_side": side,
        "wall_price": px,
        "touch_price": event.get("touch_price"),
        "first_touch_ts_ns": event.get("first_touch_ts_ns"),
        "trigger_ts_ns": event.get("trigger_ts_ns"),
        "ok": result.get("ok"),
        "verdict": result.get("verdict"),
        "reason": reason,
        "qdh_wall_touch": anchors.get("wall_touch"),
        "qdh_before_detection": anchors.get("immediately_before_episode_detection"),
        "qdh_detection": anchors.get("episode_detection"),
        "n_level_changes": counts.get("n_level_changes"),
        "n_metrics_buckets": counts.get("n_metrics_buckets"),
        "n_raw_trades": counts.get("n_raw_trades"),
        "n_timeline_rows": counts.get("n_timeline_rows"),
        "look_ahead_ok": man.get("look_ahead_ok"),
        "mass_balance_ok": man.get("mass_balance_ok"),
        "elapsed_s": result.get("elapsed_s"),
        "error": result.get("error"),
    }


def run_mp_qdh_pilot(
    *,
    repo_root: Path,
    out_dir: Path | None = None,
    batch_run_dir: Path | None = None,
    max_events: int = 20,
    role: str = "UPPER",
    database: str = SILVER_DATABASE,
    enable_mp_enrichment: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    batch_dir = Path(batch_run_dir) if batch_run_dir else (repo_root / BATCH_RUN_REL)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(out_dir) if out_dir else (
        repo_root / "obfull_research_engine" / "runs" / "ob_forschungsengine_v1" / f"{RUN_PREFIX}mp_pilot_{stamp}"
    )
    if not (repo_root / "obfull_research_engine").exists():
        out_dir = Path(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    events_dir = out_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    events = _read_csv(batch_dir / "events_all.csv")
    windows = _window_bounds(batch_dir)
    role_u = str(role).upper()
    selected = select_role_pilot(events, role=role_u, max_n=max_events)
    gates = replace(default_gates(), mp_hit_pull_enrichment=bool(enable_mp_enrichment))

    selection_meta = {
        "n_selected": len(selected),
        "max_events": max_events,
        "role_filter": role_u,
        "require_touch_and_trigger": True,
        "label_counts": dict(Counter(e.get("label_price_only") for e in selected)),
        "window_counts": dict(Counter(e.get("window_id") for e in selected)),
        "event_ids": [e["event_id"] for e in selected],
        "gates": gates.to_dict(),
        "batch_run_dir": str(batch_dir),
        "note": f"{role_u} wall QDH scale-out (ask for UPPER, bid for LOWER)",
    }
    atomic_write_json(out_dir / "pilot_selection.json", selection_meta)

    own = client is None
    client = client or get_clickhouse_client(role="ob_forschungsengine_mp_pilot")
    rows: list[dict[str, Any]] = []
    t0 = time.monotonic()
    try:
        for i, event in enumerate(selected, start=1):
            eid = event["event_id"]
            wid = event.get("window_id") or ""
            window = windows.get(wid)
            if window is None:
                row = {
                    "event_id": eid,
                    "ok": False,
                    "verdict": "STOP_OB_FORSCHUNGSENGINE",
                    "error": f"WINDOW_MISSING:{wid}",
                }
                rows.append(row)
                atomic_write_json(events_dir / eid / "analysis_manifest.json", row)
                continue
            chain = str(window.get("chain_version") or BTC_CHAIN_VERSION)
            ev_out = events_dir / eid
            print(
                f"[{i}/{len(selected)}] {eid} label={event.get('label_price_only')} "
                f"window={wid}",
                flush=True,
            )
            try:
                result = analyze_mp_event(
                    event,
                    out_dir=ev_out,
                    window=window,
                    gates=gates,
                    database=database,
                    client=client,
                    chain_version=chain,
                )
                rows.append(_summary_row(event, result))
            except Exception as exc:  # noqa: BLE001
                err = {
                    "ok": False,
                    "verdict": "STOP_OB_FORSCHUNGSENGINE",
                    "event_id": eid,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
                atomic_write_json(ev_out / "analysis_manifest.json", err)
                rows.append(
                    {
                        "event_id": eid,
                        "window_id": wid,
                        "event_role": event.get("event_role"),
                        "label_price_only": event.get("label_price_only"),
                        "ok": False,
                        "verdict": err["verdict"],
                        "error": str(exc),
                    }
                )
            print(
                f"  -> ok={rows[-1].get('ok')} verdict={rows[-1].get('verdict')} "
                f"qdh_touch={rows[-1].get('qdh_wall_touch')} elapsed={rows[-1].get('elapsed_s')}",
                flush=True,
            )
    finally:
        if own:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # summary CSV
    csv_path = out_dir / "pilot_summary.csv"
    if rows:
        keys: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in keys})

    n_ok = sum(1 for r in rows if r.get("ok"))
    n_fail = len(rows) - n_ok
    manifest = {
        "ok": n_fail == 0 and n_ok > 0,
        "package": PACKAGE_NAME,
        "audit_id": AUDIT_ID,
        "schema_version": SCHEMA_VERSION,
        "created_at": format_utc_z(datetime.now(timezone.utc)),
        "elapsed_s": round(time.monotonic() - t0, 3),
        "n_selected": len(selected),
        "n_ok": n_ok,
        "n_fail": n_fail,
        "label_ok": dict(
            Counter(r.get("label_price_only") for r in rows if r.get("ok"))
        ),
        "verdicts": dict(Counter(str(r.get("verdict")) for r in rows)),
        "selection": selection_meta,
        "out_dir": str(out_dir),
        "doge_untouched": True,
        "ch_writes": False,
    }
    atomic_write_json(out_dir / "run_manifest.json", manifest)
    report_name = "FULL_UPPER_REPORT.md" if len(selected) > 20 else "PILOT_REPORT.md"
    atomic_write_text(out_dir / report_name, _pilot_md(manifest, rows))
    # keep stable alias
    atomic_write_text(out_dir / "PILOT_REPORT.md", _pilot_md(manifest, rows))
    return {"ok": manifest["ok"], "manifest": manifest, "rows": rows, "out_dir": str(out_dir)}


def _pilot_md(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    n = int(manifest.get("n_selected") or len(rows))
    title = "MP QDH Full UPPER Report" if n > 20 else "MP QDH Pilot Report"
    lines = [
        f"# {title}",
        "",
        f"**ok:** `{manifest.get('ok')}`  |  **n_ok/n:** `{manifest.get('n_ok')}/{manifest.get('n_selected')}`",
        "",
        f"- Elapsed: {manifest.get('elapsed_s')}s",
        f"- Role filter: {manifest.get('selection', {}).get('role_filter', 'UPPER')}",
        f"- Verdicts: `{manifest.get('verdicts')}`",
        f"- Labels ok: `{manifest.get('label_ok')}`",
        "",
        "| event_id | label | qdh_wall_touch | qdh_detection | ok |",
        "|---|---|---:|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r.get('event_id')}` | {r.get('label_price_only')} | "
            f"{r.get('qdh_wall_touch')} | {r.get('qdh_detection')} | {r.get('ok')} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- QDH_base only (MP enrichment gate off for CH load).",
            "- No Signal V2 / WallState thresholds.",
            "- `qdh_wall_touch` often 0 at first touch bucket; prefer `qdh_detection`.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
