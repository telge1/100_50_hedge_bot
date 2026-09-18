"""Freeze 15 first-touch winners + match 15 unique WRONG_WAY controls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from . import (
    CONTROL_CLASS,
    ENTRY_MODE_FIRST_TOUCH,
    EXPECTED_N_CONTROLS,
    EXPECTED_N_WINNERS,
    OUTCOME_CONTRACT_REL,
    OUTCOME_PARQUET_REL,
    WINNER_CLASSES,
)


class EventUniverseMismatch(RuntimeError):
    """Raised when winner count ≠ EXPECTED_N_WINNERS."""


def _atr_bucket(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "UNK"
    if x < 0.05:
        return "LO"
    if x < 0.12:
        return "MID"
    return "HI"


def _utc_hour(ts_utc: Any) -> int:
    try:
        return int(str(ts_utc).split(" ")[1].split(":")[0])
    except Exception:  # noqa: BLE001
        return -1


MATCHING_FEATURE_KEYS = (
    "label_price_only",
    "event_role",
    "trade_side",
    "confluence_class",
    "atr14_5m_pct",
    "session_utc",
    "reference_entry_ts_ns",
    "reference_entry_ts_utc",
)

FORBIDDEN_MATCH_KEYS = (
    "mfe_4h_pct",
    "mae_4h_pct",
    "mae_before_0_41_pct",
    "primary_outcome_class",
    "qdh",
    "hit_qty",
    "pull_qty",
    "refill_ratio",
    "wall_moved_with_price",
    "microprice_distance_bps",
)


def match_quality(winner: dict[str, Any], control: dict[str, Any]) -> tuple[int, list[str]]:
    """Score using only pre-outcome context. Hard-require label, edge role, trade_side."""
    reasons: list[str] = []
    if winner.get("label_price_only") != control.get("label_price_only"):
        return -1, ["LABEL_MISMATCH"]
    if winner.get("event_role") != control.get("event_role"):
        return -1, ["ROLE_MISMATCH"]
    if winner.get("trade_side") != control.get("trade_side"):
        return -1, ["SIDE_MISMATCH"]
    score = 100 + 50 + 40
    reasons.extend(["label", "role", "trade_side"])
    if winner.get("confluence_class") == control.get("confluence_class"):
        score += 20
        reasons.append("confluence")
    if _atr_bucket(winner.get("atr14_5m_pct")) == _atr_bucket(control.get("atr14_5m_pct")):
        score += 10
        reasons.append("atr")
    if winner.get("session_utc") == control.get("session_utc"):
        score += 8
        reasons.append("session")
    wh = _utc_hour(winner.get("reference_entry_ts_utc"))
    ch = _utc_hour(control.get("reference_entry_ts_utc"))
    if wh >= 0 and ch >= 0 and abs(wh - ch) <= 2:
        score += 5
        reasons.append("hour_near")
    return score, reasons


def load_outcome_frame(repo_root: Path) -> pd.DataFrame:
    path = Path(repo_root) / OUTCOME_PARQUET_REL
    return pd.read_parquet(path)


def select_winners(df: pd.DataFrame) -> list[dict[str, Any]]:
    orig = df[df["entry_mode"] == ENTRY_MODE_FIRST_TOUCH]
    w = orig[orig["primary_outcome_class"].isin(list(WINNER_CLASSES))].copy()
    w = w.sort_values(
        ["label_price_only", "event_role", "trade_side", "reference_entry_ts_ns", "event_id"]
    )
    rows = w.to_dict("records")
    if len(rows) != EXPECTED_N_WINNERS:
        raise EventUniverseMismatch(
            f"EVENT_UNIVERSE_MISMATCH: expected {EXPECTED_N_WINNERS} winners, got {len(rows)}"
        )
    return rows


def select_wrong_way_pool(df: pd.DataFrame, winner_ids: set[str]) -> list[dict[str, Any]]:
    orig = df[df["entry_mode"] == ENTRY_MODE_FIRST_TOUCH]
    c = orig[orig["primary_outcome_class"] == CONTROL_CLASS].copy()
    c = c[~c["event_id"].isin(winner_ids)]
    c = c.sort_values(
        ["label_price_only", "event_role", "trade_side", "reference_entry_ts_ns", "event_id"]
    )
    return c.to_dict("records")


def match_controls(
    winners: list[dict[str, Any]],
    controls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Greedy deterministic 1:1 WRONG_WAY matching. Returns (pairs, gap_winner_ids)."""
    winner_ids = {str(w["event_id"]) for w in winners}
    used: set[str] = set()
    pairs: list[dict[str, Any]] = []
    gaps: list[str] = []
    for i, a in enumerate(winners):
        best = None
        best_key: tuple | None = None
        best_reasons: list[str] = []
        for b in controls:
            bid = str(b["event_id"])
            if bid in used or bid in winner_ids:
                continue
            sc, reasons = match_quality(a, b)
            if sc < 0:
                continue
            try:
                atr_pen = abs(float(a.get("atr14_5m_pct") or 0) - float(b.get("atr14_5m_pct") or 0))
            except (TypeError, ValueError):
                atr_pen = 9.0
            dt = abs(int(a.get("reference_entry_ts_ns") or 0) - int(b.get("reference_entry_ts_ns") or 0))
            key = (sc, -atr_pen, -dt, bid)
            if best_key is None or key > best_key:
                best = b
                best_key = key
                best_reasons = reasons
        if best is None or best_key is None:
            gaps.append(str(a["event_id"]))
            continue
        used.add(str(best["event_id"]))
        pair_id = f"P{i + 1:02d}"
        pairs.append(
            {
                "pair_id": pair_id,
                "winner_event_id": str(a["event_id"]),
                "control_event_id": str(best["event_id"]),
                "winner_outcome": a.get("primary_outcome_class"),
                "control_outcome": best.get("primary_outcome_class"),
                "label_price_only": a.get("label_price_only"),
                "event_role": a.get("event_role"),
                "trade_side": a.get("trade_side"),
                "confluence_class_winner": a.get("confluence_class"),
                "confluence_class_control": best.get("confluence_class"),
                "match_score": int(best_key[0]),
                "shared_criteria": "|".join(best_reasons),
                "winner_reference_entry_ts_utc": a.get("reference_entry_ts_utc"),
                "control_reference_entry_ts_utc": best.get("reference_entry_ts_utc"),
                "winner_atr14_5m_pct": a.get("atr14_5m_pct"),
                "control_atr14_5m_pct": best.get("atr14_5m_pct"),
                "winner_session_utc": a.get("session_utc"),
                "control_session_utc": best.get("session_utc"),
            }
        )
    return pairs, gaps


def sha256_json(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_frozen_universe(repo_root: Path) -> dict[str, Any]:
    df = load_outcome_frame(repo_root)
    winners = select_winners(df)
    winner_ids = {str(w["event_id"]) for w in winners}
    pool = select_wrong_way_pool(df, winner_ids)
    pairs, gaps = match_controls(winners, pool)

    contract_path = Path(repo_root) / OUTCOME_CONTRACT_REL
    contract = json.loads(contract_path.read_text(encoding="utf-8")) if contract_path.exists() else {}

    event_rows: list[dict[str, Any]] = []
    for p in pairs:
        for role, eid, src in (
            ("WINNER", p["winner_event_id"], next(w for w in winners if str(w["event_id"]) == p["winner_event_id"])),
            (
                "CONTROL",
                p["control_event_id"],
                next(c for c in pool if str(c["event_id"]) == p["control_event_id"]),
            ),
        ):
            event_rows.append(
                {
                    "pair_id": p["pair_id"],
                    "case_role": role,
                    "event_id": eid,
                    "outcome_class": src.get("primary_outcome_class"),
                    "label_price_only": src.get("label_price_only"),
                    "event_role": src.get("event_role"),
                    "trade_side": src.get("trade_side"),
                    "confluence_class": src.get("confluence_class"),
                    "entry_mode": src.get("entry_mode"),
                    "reference_entry_ts_ns": src.get("reference_entry_ts_ns"),
                    "reference_entry_ts_utc": src.get("reference_entry_ts_utc"),
                    "session_utc": src.get("session_utc"),
                    "atr14_5m_pct": src.get("atr14_5m_pct"),
                    "window_id": src.get("window_id"),
                    "split": src.get("split"),
                }
            )

    # Also list unmatched winners if any
    for gid in gaps:
        src = next(w for w in winners if str(w["event_id"]) == gid)
        event_rows.append(
            {
                "pair_id": None,
                "case_role": "WINNER_UNMATCHED",
                "event_id": gid,
                "outcome_class": src.get("primary_outcome_class"),
                "label_price_only": src.get("label_price_only"),
                "event_role": src.get("event_role"),
                "trade_side": src.get("trade_side"),
                "confluence_class": src.get("confluence_class"),
                "entry_mode": src.get("entry_mode"),
                "reference_entry_ts_ns": src.get("reference_entry_ts_ns"),
                "reference_entry_ts_utc": src.get("reference_entry_ts_utc"),
                "session_utc": src.get("session_utc"),
                "atr14_5m_pct": src.get("atr14_5m_pct"),
                "window_id": src.get("window_id"),
                "split": src.get("split"),
            }
        )

    event_ids_ordered = [r["event_id"] for r in event_rows if r["case_role"] in ("WINNER", "CONTROL")]
    pairs_compact = [
        {
            "pair_id": p["pair_id"],
            "winner_event_id": p["winner_event_id"],
            "control_event_id": p["control_event_id"],
            "match_score": p["match_score"],
            "shared_criteria": p["shared_criteria"],
        }
        for p in pairs
    ]
    event_list_hash = sha256_json(event_ids_ordered)
    pair_list_hash = sha256_json(pairs_compact)
    matching_contract = {
        "hard_keys": ["label_price_only", "event_role", "trade_side"],
        "soft_keys": ["confluence_class", "atr_bucket", "session_utc", "hour_near", "time_proximity"],
        "forbidden_keys": list(FORBIDDEN_MATCH_KEYS),
        "matching_feature_keys_used": list(MATCHING_FEATURE_KEYS),
    }
    contract_hash = sha256_json(
        {
            "outcome_contract_hash": contract.get("contract_hash_sha256"),
            "matching": matching_contract,
            "winner_classes": list(WINNER_CLASSES),
            "control_class": CONTROL_CLASS,
            "entry_mode": ENTRY_MODE_FIRST_TOUCH,
            "expected_n_winners": EXPECTED_N_WINNERS,
        }
    )

    ok = (
        len(winners) == EXPECTED_N_WINNERS
        and len(pairs) == EXPECTED_N_CONTROLS
        and not gaps
        and len({p["control_event_id"] for p in pairs}) == EXPECTED_N_CONTROLS
        and not (winner_ids & {p["control_event_id"] for p in pairs})
    )

    manifest = {
        "ok": ok,
        "n_winners": len(winners),
        "n_controls_matched": len(pairs),
        "n_gaps": len(gaps),
        "gaps": gaps,
        "event_list_sha256": event_list_hash,
        "pair_list_sha256": pair_list_hash,
        "contract_hash_sha256": contract_hash,
        "outcome_contract_hash": contract.get("contract_hash_sha256"),
        "source_parquet": OUTCOME_PARQUET_REL,
        "matching_contract": matching_contract,
        "frozen": True,
        "big_clean": sum(1 for w in winners if w["primary_outcome_class"] == "BIG_CLEAN_MOVE"),
        "very_big_clean": sum(1 for w in winners if w["primary_outcome_class"] == "VERY_BIG_CLEAN_MOVE"),
    }
    return {
        "ok": ok,
        "winners": winners,
        "pairs": pairs,
        "gaps": gaps,
        "event_rows": event_rows,
        "manifest": manifest,
        "event_list_sha256": event_list_hash,
        "pair_list_sha256": pair_list_hash,
        "contract_hash_sha256": contract_hash,
    }


def write_frozen_universe(out_dir: Path, frozen: dict[str, Any]) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # CSV helpers
    import csv

    def _write(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys: list[str] = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})

    _write(out_dir / "frozen_event_universe.csv", frozen["event_rows"])
    _write(out_dir / "matched_pairs.csv", frozen["pairs"])
    (out_dir / "event_universe_manifest.json").write_text(
        json.dumps(frozen["manifest"], indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
