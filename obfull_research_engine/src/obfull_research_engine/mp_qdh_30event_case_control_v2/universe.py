"""Load and verify the frozen v1 30-event universe (no rematch)."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from . import (
    EXPECTED_EVENT_LIST_SHA256,
    EXPECTED_N_PAIRS,
    EXPECTED_PAIR_LIST_SHA256,
    V1_RUN_REL,
)


class EventUniverseMismatch(RuntimeError):
    pass


def sha256_json(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_and_verify_frozen_universe(repo_root: Path) -> dict[str, Any]:
    v1 = Path(repo_root) / V1_RUN_REL
    univ_path = v1 / "frozen_event_universe.csv"
    pairs_path = v1 / "matched_pairs.csv"
    man_path = v1 / "event_universe_manifest.json"
    if not univ_path.exists() or not pairs_path.exists() or not man_path.exists():
        raise EventUniverseMismatch("EVENT_UNIVERSE_MISMATCH: v1 frozen artifacts missing")

    event_rows = _read_csv(univ_path)
    pairs = _read_csv(pairs_path)
    manifest = json.loads(man_path.read_text(encoding="utf-8"))

    event_ids = [r["event_id"] for r in event_rows if r.get("case_role") in ("WINNER", "CONTROL")]
    pairs_compact = [
        {
            "pair_id": p["pair_id"],
            "winner_event_id": p["winner_event_id"],
            "control_event_id": p["control_event_id"],
            "match_score": int(float(p["match_score"])) if p.get("match_score") not in (None, "") else 0,
            "shared_criteria": p.get("shared_criteria"),
        }
        for p in pairs
    ]
    # Recompute hashes the same way as v1 universe.py
    event_hash = sha256_json(event_ids)
    pair_hash = sha256_json(pairs_compact)

    if event_hash != EXPECTED_EVENT_LIST_SHA256 or manifest.get("event_list_sha256") != EXPECTED_EVENT_LIST_SHA256:
        raise EventUniverseMismatch(
            f"EVENT_UNIVERSE_MISMATCH: event_list_sha256 got {event_hash} / manifest {manifest.get('event_list_sha256')}"
        )
    if pair_hash != EXPECTED_PAIR_LIST_SHA256 or manifest.get("pair_list_sha256") != EXPECTED_PAIR_LIST_SHA256:
        raise EventUniverseMismatch(
            f"EVENT_UNIVERSE_MISMATCH: pair_list_sha256 got {pair_hash} / manifest {manifest.get('pair_list_sha256')}"
        )
    if len(pairs) != EXPECTED_N_PAIRS:
        raise EventUniverseMismatch(f"EVENT_UNIVERSE_MISMATCH: expected {EXPECTED_N_PAIRS} pairs, got {len(pairs)}")

    winners = [r for r in event_rows if r.get("case_role") == "WINNER"]
    controls = [r for r in event_rows if r.get("case_role") == "CONTROL"]
    if len(winners) != 15 or len(controls) != 15:
        raise EventUniverseMismatch("EVENT_UNIVERSE_MISMATCH: winner/control counts")

    # Normalize pair dicts
    pair_dicts = []
    for p in pairs:
        pair_dicts.append(
            {
                "pair_id": p["pair_id"],
                "winner_event_id": p["winner_event_id"],
                "control_event_id": p["control_event_id"],
                "winner_outcome": p.get("winner_outcome"),
                "control_outcome": p.get("control_outcome"),
                "label_price_only": p.get("label_price_only"),
                "event_role": p.get("event_role"),
                "trade_side": p.get("trade_side"),
                "match_score": p.get("match_score"),
                "shared_criteria": p.get("shared_criteria"),
            }
        )

    return {
        "ok": True,
        "event_rows": event_rows,
        "pairs": pair_dicts,
        "manifest": {
            **manifest,
            "reused_from": V1_RUN_REL,
            "v2_verified_event_list_sha256": event_hash,
            "v2_verified_pair_list_sha256": pair_hash,
            "frozen_reuse": True,
        },
        "event_list_sha256": event_hash,
        "pair_list_sha256": pair_hash,
        "contract_hash_sha256": manifest.get("contract_hash_sha256"),
    }


def write_frozen_copies(out_dir: Path, frozen: dict[str, Any], repo_root: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    v1 = Path(repo_root) / V1_RUN_REL
    shutil.copy2(v1 / "frozen_event_universe.csv", out_dir / "frozen_event_universe.csv")
    shutil.copy2(v1 / "matched_pairs.csv", out_dir / "matched_pairs.csv")
    (out_dir / "event_universe_manifest.json").write_text(
        json.dumps(frozen["manifest"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
