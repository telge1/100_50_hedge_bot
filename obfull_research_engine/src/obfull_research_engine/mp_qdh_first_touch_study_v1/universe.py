"""Freeze tradeable first-touch universe from MP batch (no outcome filter)."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import BATCH_RUN_REL


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def build_first_touch_universe(repo_root: Path | None = None) -> dict[str, Any]:
    repo = Path(repo_root or _repo_root())
    batch = repo / BATCH_RUN_REL
    events = {r["event_id"]: r for r in csv.DictReader((batch / "events_all.csv").open(encoding="utf-8"))}
    episodes = list(csv.DictReader((batch / "episodes.csv").open(encoding="utf-8")))
    windows = {r["window_id"]: r for r in csv.DictReader((batch / "batch_windows.csv").open(encoding="utf-8"))}

    included: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []

    for ep in episodes:
        eid = ep["event_id"]
        e = events.get(eid)
        if e is None:
            exclusions.append({"event_id": eid, "reason": "EVENT_NOT_IN_EVENTS_ALL"})
            continue
        if not _truthy(ep.get("is_first_touch_of_zone_version")):
            exclusions.append(
                {
                    "event_id": eid,
                    "reason": "NOT_FIRST_TOUCH",
                    "label_price_only": e.get("label_price_only"),
                }
            )
            continue
        lab = str(e.get("label_price_only") or "")
        if lab == "UNRESOLVED":
            exclusions.append({"event_id": eid, "reason": "UNRESOLVED", "label_price_only": lab})
            continue
        side = str(e.get("trade_side") or "")
        if side not in ("LONG", "SHORT"):
            exclusions.append({"event_id": eid, "reason": "NO_TRADE_SIDE", "label_price_only": lab})
            continue
        trig = e.get("trigger_ts_ns")
        tprice = e.get("trigger_price")
        if trig in (None, "", "None") or tprice in (None, "", "None"):
            exclusions.append({"event_id": eid, "reason": "MISSING_TRIGGER", "label_price_only": lab})
            continue
        # Trade-side contract check for TRUE_BREAK
        fade = str(e.get("fade_side") or "")
        brk = str(e.get("break_side") or "")
        if lab == "TRUE_BREAK" and side == fade and side != brk:
            exclusions.append(
                {
                    "event_id": eid,
                    "reason": "TRUE_BREAK_USES_FADE_SIDE",
                    "trade_side": side,
                    "fade_side": fade,
                    "break_side": brk,
                }
            )
            continue
        wid = e.get("window_id") or ep.get("window_id") or ""
        w = windows.get(wid) or {}
        row = {
            "event_id": eid,
            "window_id": wid,
            "zone_id": e.get("zone_id") or ep.get("zone_id"),
            "episode_id": ep.get("episode_id"),
            "label_price_only": lab,
            "event_role": e.get("event_role"),
            "trade_side": side,
            "trade_side_reason": e.get("trade_side_reason"),
            "fade_side": fade,
            "break_side": brk,
            "confluence_class": e.get("confluence_class"),
            "first_touch_ts_ns": e.get("first_touch_ts_ns"),
            "touch_price": e.get("touch_price"),
            "trigger_ts_ns": trig,
            "trigger_price": tprice,
            "trigger_reason": e.get("trigger_reason"),
            "is_first_touch": True,
            "selected_first_touch_zone_version": _truthy(ep.get("selected_first_touch_zone_version")),
            "replay_epoch": e.get("epoch_id") or w.get("replay_epoch") or "",
            "symbol": e.get("symbol") or "BTCUSDT",
        }
        included.append(row)

    included.sort(key=lambda r: (int(float(r["first_touch_ts_ns"])), r["event_id"]))
    ids = [r["event_id"] for r in included]
    universe_hash = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()

    excl_by = Counter(r["reason"] for r in exclusions)
    manifest = {
        "n_batch_events": len(events),
        "n_first_touch_raw": sum(1 for ep in episodes if _truthy(ep.get("is_first_touch_of_zone_version"))),
        "n_included": len(included),
        "n_excluded": len(exclusions),
        "exclusion_counts": dict(excl_by),
        "universe_hash_sha256": universe_hash,
        "labels": dict(Counter(r["label_price_only"] for r in included)),
        "trade_sides": dict(Counter(r["trade_side"] for r in included)),
        "confluence": dict(Counter(r.get("confluence_class") or "" for r in included)),
        "roles": dict(Counter(r.get("event_role") or "" for r in included)),
        "note": "Expected ~115 = 120 first-touch minus UNRESOLVED/missing side; actual is n_included.",
        "selection": "is_first_touch_of_zone_version + not UNRESOLVED + LONG/SHORT + trigger present; no MFE/QDH filter",
        "batch_run": BATCH_RUN_REL,
    }
    return {
        "included": included,
        "exclusions": exclusions,
        "manifest": manifest,
        "universe_hash": universe_hash,
        "events_by_id": {r["event_id"]: events[r["event_id"]] for r in included},
        "windows": windows,
    }


def write_universe(out_dir: Path, uni: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

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

    _csv(out_dir / "first_touch_universe.csv", uni["included"])
    _csv(out_dir / "first_touch_exclusions.csv", uni["exclusions"])
    (out_dir / "first_touch_manifest.json").write_text(
        json.dumps(uni["manifest"], indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
