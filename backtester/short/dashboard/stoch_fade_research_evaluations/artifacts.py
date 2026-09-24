"""Future-evaluation artifact contract. Never rewrite completed canary folders."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from stoch_universe_51.jsonio import read_json, write_json_atomic

from .config import EXIT_POLICY, INTRABAR_POLICY, OUTCOME_ENGINE, SIGNAL_SCOPE

ALLOWED_OUTCOMES = frozenset({"WIN", "LOSS", "OPEN"})
SUCCESS_STATES = frozenset({"COMPLETED", "SKIPPED_RESUME_COMPLETE"})

SIDE_EFFECT_FLAGS: dict[str, bool] = {
    "cleanup_enabled": False,
    "live_orders_enabled": False,
    "publish_enabled": False,
    "writes_to_clickhouse": False,
    "writes_to_processing_state": False,
    "writes_to_signal_outcomes": False,
    "writes_to_signals": False,
}


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def source_counts_for_coin(src: dict[str, Any]) -> dict[str, int]:
    raw = int(src.get("raw_total") or 0)
    tier_a = int(src.get("tier_a_total") or 0)
    return {
        "source_raw_total": raw,
        "source_tier_a_total": tier_a,
        "raw_total": raw,
    }


def apply_source_counts(row: dict[str, Any], src: dict[str, Any] | None) -> dict[str, Any]:
    counts = source_counts_for_coin(src or {})
    row["source_raw_total"] = counts["source_raw_total"]
    row["source_tier_a_total"] = counts["source_tier_a_total"]
    row["raw_total"] = counts["raw_total"]
    if not row.get("tier_a_total"):
        row["tier_a_total"] = counts["source_tier_a_total"]
    row.setdefault("evaluated_tier_a_total", 0)
    row.setdefault("completed_outcomes", 0)
    row.setdefault("failed_outcomes", 0)
    return row


def empty_coin_row(symbol: str, src: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "symbol": symbol,
        "state": "PENDING",
        "raw_total": 0,
        "source_raw_total": 0,
        "source_tier_a_total": 0,
        "tier_a_total": 0,
        "evaluated_tier_a_total": 0,
        "completed_outcomes": 0,
        "failed_outcomes": 0,
        "wins": 0,
        "losses": 0,
        "open": 0,
        "error_code": None,
        "message": "",
        "artifact_reference": None,
    }
    return apply_source_counts(row, src)


def assemble_root_outcomes(directory: Path, coins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compose root outcomes from per-coin files. Source-coin order, then file order. No recompute."""
    assembled: list[dict[str, Any]] = []
    seen: set[str] = set()
    for coin in coins:
        state = str(coin.get("state") or "")
        if state and state not in SUCCESS_STATES:
            continue
        symbol = str(coin.get("symbol") or "")
        path = directory / "coin_runs" / symbol / "outcomes.jsonl"
        for row in _read_jsonl(path):
            sid = str(row.get("signal_id") or "")
            if not sid or sid in seen:
                continue
            outcome = str(row.get("outcome") or row.get("display_result") or "").upper()
            if outcome not in ALLOWED_OUTCOMES or outcome.startswith("BE") or "BE /" in outcome:
                continue
            if row.get("be_activated") or str(row.get("exit_reason") or "").upper() == "BE":
                continue
            seen.add(sid)
            assembled.append(row)
    return assembled


def combined_summary_from_rows(
    rows: list[dict[str, Any]],
    coins: list[dict[str, Any]],
) -> dict[str, Any]:
    wins = losses = opens = 0
    gross_p = gross_l = 0.0
    by_symbol: dict[str, Any] = {}
    by_tf: dict[str, Any] = {}
    by_dir: dict[str, Any] = {}
    reasons: dict[str, int] = {}
    for row in rows:
        oc = str(row.get("outcome") or row.get("display_result") or "").upper()
        symbol = str(row.get("symbol") or "")
        tf = str(row.get("timeframe") or "")
        direction = str(row.get("direction") or "")
        by_symbol.setdefault(symbol, {"signals": 0, "wins": 0, "losses": 0, "open": 0})
        by_tf.setdefault(tf, {"signals": 0, "wins": 0, "losses": 0, "open": 0})
        by_dir.setdefault(direction, {"signals": 0, "wins": 0, "losses": 0, "open": 0})
        by_symbol[symbol]["signals"] += 1
        by_tf[tf]["signals"] += 1
        by_dir[direction]["signals"] += 1
        if oc == "WIN":
            wins += 1
            by_symbol[symbol]["wins"] += 1
            by_tf[tf]["wins"] += 1
            by_dir[direction]["wins"] += 1
        elif oc == "LOSS":
            losses += 1
            by_symbol[symbol]["losses"] += 1
            by_tf[tf]["losses"] += 1
            by_dir[direction]["losses"] += 1
        else:
            opens += 1
            by_symbol[symbol]["open"] += 1
            by_tf[tf]["open"] += 1
            by_dir[direction]["open"] += 1
        reason = str(row.get("exit_reason") or oc or "")
        reasons[reason] = reasons.get(reason, 0) + 1
        pnl = row.get("pnl_pct_gross")
        if pnl is None:
            pnl = row.get("pnl_pct")
        try:
            val = float(pnl)
        except (TypeError, ValueError):
            val = 0.0
        if val > 0:
            gross_p += val
        elif val < 0:
            gross_l += val
    closed = wins + losses
    return {
        "signals": len(rows),
        "wins": wins,
        "losses": losses,
        "open": opens,
        "win_rate_pct": (wins / closed * 100.0) if closed else None,
        "win_rate_denominator": "wins+losses (OPEN excluded)",
        "gross_profit_pct": gross_p,
        "gross_loss_pct": gross_l,
        "total_pnl_pct": gross_p + gross_l,
        "exit_reason_counts": reasons,
        "by_symbol": by_symbol,
        "by_timeframe": by_tf,
        "by_direction": by_dir,
        "signal_scope": SIGNAL_SCOPE,
        "exit_policy": EXIT_POLICY,
        "outcome_engine": OUTCOME_ENGINE,
        "intrabar_policy": INTRABAR_POLICY,
        "execution_dedup_applied": False,
        "be50_activated_count": 0,
        "be50_exit_count": 0,
        "pnl_basis": "gross",
        "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        "successful_coins": [c.get("symbol") for c in coins if str(c.get("state") or "") in SUCCESS_STATES],
    }


def _coin_window_bounds(directory: Path, symbol: str) -> tuple[str | None, str | None]:
    """Start = requested/used evaluation window start. End = last closed 1m actually loaded."""
    coin_dir = directory / "coin_runs" / symbol
    window = {}
    summary = {}
    if (coin_dir / "window.json").is_file():
        try:
            window = read_json(coin_dir / "window.json")
        except Exception:  # noqa: BLE001
            window = {}
    if (coin_dir / "summary.json").is_file():
        try:
            summary = read_json(coin_dir / "summary.json")
        except Exception:  # noqa: BLE001
            summary = {}
    start = window.get("evaluation_data_start") or summary.get("evaluation_data_start")
    ident = summary.get("identity") if isinstance(summary.get("identity"), dict) else {}
    end = ident.get("candle_data_to") or window.get("evaluation_data_end") or summary.get("evaluation_data_end")
    if int(window.get("candle_rows") or summary.get("candle_rows") or 0) == 0 and not ident.get("candle_data_to"):
        if not start and not end:
            return None, None
    return (str(start) if start else None), (str(end) if end else None)


def derive_evaluation_data_bounds(
    directory: Path, coins: list[dict[str, Any]]
) -> tuple[str | None, str | None, str | None]:
    starts: list[str] = []
    ends: list[str] = []
    for coin in coins:
        if str(coin.get("state") or "") not in SUCCESS_STATES:
            continue
        start, end = _coin_window_bounds(directory, str(coin.get("symbol") or ""))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    if not starts and not ends:
        return None, None, "NO_CANDLE_WINDOWS"
    return (min(starts) if starts else None), (max(ends) if ends else None), None


def update_evaluation_manifest_bounds(
    directory: Path,
    *,
    data_start: str | None,
    data_end: str | None,
    missing_reason: str | None,
) -> None:
    path = directory / "evaluation_manifest.json"
    if not path.is_file():
        return
    manifest = read_json(path)
    if data_start:
        manifest["evaluation_data_start"] = data_start
    manifest["evaluation_data_end"] = data_end
    manifest["evaluation_data_end_status"] = missing_reason
    manifest["evaluation_data_start_semantics"] = (
        "min per-coin evaluation_data_start from window.json (first entry candle window)"
    )
    manifest["evaluation_data_end_semantics"] = (
        "max per-coin identity.candle_data_to (last closed 1m actually loaded)"
    )
    manifest["side_effect_flags"] = dict(SIDE_EFFECT_FLAGS)
    write_json_atomic(path, manifest)


def load_combined_summary(directory: Path, status: dict[str, Any] | None = None) -> dict[str, Any]:
    for name in ("combined_summary.json", "summary.json"):
        path = directory / name
        if path.is_file():
            try:
                data = read_json(path)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data:
                return data
    if status and isinstance(status.get("combined_summary"), dict):
        return dict(status["combined_summary"])
    return {}


def read_outcomes_prefer_root(directory: Path, coins: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    root = directory / "outcomes.jsonl"
    if root.is_file():
        return _read_jsonl(root)
    ordered = list(coins or [])
    if not ordered:
        coin_root = directory / "coin_runs"
        if coin_root.is_dir():
            ordered = [{"symbol": p.name} for p in sorted(coin_root.iterdir()) if p.is_dir()]
    return assemble_root_outcomes(directory, ordered)


def finalize_root_artifacts(directory: Path, coins: list[dict[str, Any]]) -> dict[str, Any]:
    rows = assemble_root_outcomes(directory, coins)
    combined = combined_summary_from_rows(rows, coins)
    write_jsonl_atomic(directory / "outcomes.jsonl", rows)
    write_json_atomic(directory / "combined_summary.json", combined)
    write_json_atomic(directory / "summary.json", combined)
    data_start, data_end, missing = derive_evaluation_data_bounds(directory, coins)
    update_evaluation_manifest_bounds(
        directory, data_start=data_start, data_end=data_end, missing_reason=missing
    )
    return combined
