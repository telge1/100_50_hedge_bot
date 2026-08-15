"""Recognize a finished coin-run without loading signal lists."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stoch_universe_51.jsonio import read_json

from .config import SOURCE_COMMIT, STRATEGY_VERSION

SIGNAL_TFS = ("15m", "30m", "1h", "4h")


def relative_artifact_reference(run_dir: Path, symbol: str) -> str | None:
    run_id = run_dir.name
    if not symbol or not run_id or ".." in symbol or ".." in run_id:
        return None
    if "/" in symbol or "/" in run_id or "\\" in symbol or "\\" in run_id:
        return None
    rel = f"coin_runs/{symbol}/{run_id}"
    posix = run_dir.as_posix()
    if posix.startswith("/") and not posix.endswith("/" + rel) and not posix.endswith(rel):
        return rel
    return rel


def warmup_from_per_symbol(per: dict[str, Any]) -> tuple[bool | None, dict[str, bool], str | None]:
    """Copy runner first_valid_by_timeframe.warmup_complete. Do not recompute EMA warmup."""
    src = per.get("first_valid_by_timeframe")
    if not isinstance(src, dict):
        return None, {}, "WARMUP_SCHEMA_MISSING"
    by: dict[str, bool] = {}
    for tf in SIGNAL_TFS:
        meta = src.get(tf)
        if not isinstance(meta, dict):
            return None, {}, "WARMUP_SCHEMA_INCOMPLETE"
        flag = meta.get("warmup_complete")
        if not isinstance(flag, bool):
            return None, {}, "WARMUP_SCHEMA_INVALID"
        by[tf] = flag
    overall = per.get("warmup_complete")
    if not isinstance(overall, bool):
        return None, by, "WARMUP_OVERALL_INVALID"
    if overall is True and not all(by[tf] for tf in SIGNAL_TFS):
        return False, by, "WARMUP_INCONSISTENT"
    return overall, by, None


def coin_run_is_complete(run_dir: Path, *, symbol: str, signal_start: str, signal_end_exclusive: str) -> bool:
    summary_path = run_dir / "summary.json"
    manifest_path = run_dir / "run_manifest.json"
    if not summary_path.is_file() or not manifest_path.is_file():
        return False
    try:
        summary = read_json(summary_path)
        manifest = read_json(manifest_path)
    except Exception:  # noqa: BLE001
        return False
    selected = manifest.get("selected_symbol") or (manifest.get("selected_symbols") or [None])[0]
    if selected != symbol:
        return False
    if str(manifest.get("strategy_id") or STRATEGY_VERSION) != STRATEGY_VERSION:
        return False
    start = str(manifest.get("signal_start") or "").replace("+00:00", "Z")
    end = str(manifest.get("signal_end_exclusive") or "").replace("+00:00", "Z")
    want_start = signal_start.replace("+00:00", "Z")
    want_end = signal_end_exclusive.replace("+00:00", "Z")
    if start and want_start and start[:19] != want_start[:19]:
        return False
    if end and want_end and end[:19] != want_end[:19]:
        return False
    pin = str(manifest.get("source_commit_pin") or "")
    if pin and not SOURCE_COMMIT.startswith(pin) and pin not in SOURCE_COMMIT:
        return False
    if summary.get("run_id") and manifest.get("run_id") and summary.get("run_id") != manifest.get("run_id"):
        return False
    return True


def find_complete_coin_run(coin_root: Path, *, symbol: str, signal_start: str, signal_end_exclusive: str) -> Path | None:
    if not coin_root.is_dir():
        return None
    found: list[Path] = []
    for child in sorted(coin_root.iterdir()):
        if child.is_dir() and coin_run_is_complete(
            child, symbol=symbol, signal_start=signal_start, signal_end_exclusive=signal_end_exclusive
        ):
            found.append(child)
    return found[-1] if found else None


def counts_from_run(run_dir: Path, symbol: str) -> dict[str, Any]:
    summary = read_json(run_dir / "summary.json")
    per_tf = {
        "15m": {"raw": 0, "tier_a": 0},
        "30m": {"raw": 0, "tier_a": 0},
        "1h": {"raw": 0, "tier_a": 0},
        "4h": {"raw": 0, "tier_a": 0},
    }
    warmup = None
    warmup_by_tf: dict[str, bool] = {}
    warmup_error = "WARMUP_SCHEMA_MISSING"
    multi = 0
    per_path = run_dir / "per_symbol" / f"{symbol}.json"
    if per_path.is_file():
        per = read_json(per_path)
        counts = per.get("counts_by_timeframe") or {}
        for tf, row in counts.items():
            if tf in per_tf:
                per_tf[tf] = {
                    "raw": int(row.get("raw_candidates") or 0),
                    "tier_a": int(row.get("tier_a") or 0),
                }
        warmup, warmup_by_tf, warmup_error = warmup_from_per_symbol(per)
    audit_path = run_dir / "duplicate_audit.json"
    if audit_path.is_file():
        try:
            audit = read_json(audit_path)
            multi = int(audit.get("same_entry_multi_tf_count") or 0)
        except Exception:  # noqa: BLE001
            multi = 0
    return {
        "raw_total": int(summary.get("raw_total") or 0),
        "tier_a_total": int(summary.get("tier_a_total") or 0),
        "per_timeframe": per_tf,
        "warmup_complete": warmup,
        "warmup_complete_by_tf": warmup_by_tf,
        "warmup_schema_error": warmup_error,
        "multi_tf_collision_count": multi,
        "runner_run_id": summary.get("run_id") or run_dir.name,
        "artifact_reference": relative_artifact_reference(run_dir, symbol),
    }
