"""BT-D95: June / July / August frozen research ledgers → Research Chart overlay (read-only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

CONTRACT_VERSION = "bt_d95_three_month_dashboard_overlay_v1"
SYMBOL = "DOGEUSDT"
MIN_BOX_WIDTH_SEC = 300
FILL_DISCLAIMER = "Frozen month research replay · SIMULATED — NOT EXCHANGE CONFIRMED"


@dataclass(frozen=True)
class FrozenMonthProfile:
    key: str
    strategy_id: str
    display_name: str
    results_subdir: str
    ledger_filename: str
    bundled_filename: str
    expected_fills: int
    expected_tp: int
    expected_sl: int
    box_prefix: str
    link_prefix: str
    backtester_source: str
    payload_kind: str
    workspace_snap_key: str
    execution_model: str


PROFILES: dict[str, FrozenMonthProfile] = {
    "june": FrozenMonthProfile(
        key="june",
        strategy_id="bt_d94_june_ema_v3_no_pause_research",
        display_name="BT-D94R · Juni 2026 · EMA V3 · 34 Trades",
        results_subdir="phase_bt_d94_june_2026_frozen_v1",
        ledger_filename="trade_ledger_no_pause.json",
        bundled_filename="bt_d94_june_trade_ledger_no_pause.json",
        expected_fills=34,
        expected_tp=9,
        expected_sl=25,
        box_prefix="btd94-box-",
        link_prefix="btd94-link-",
        backtester_source="bt_d94_june_chart_overlay_v1",
        payload_kind="bt_d94_june_chart_integration_v1",
        workspace_snap_key="bt_d94_june_research",
        execution_model="BT-D94R June 2026 frozen research (no post-SL pause)",
    ),
    "july": FrozenMonthProfile(
        key="july",
        strategy_id="bt_d85_july_ema_v3_no_pause_research",
        display_name="BT-D85 · Juli 2026 · EMA V3 · 25 Trades",
        results_subdir="phase_bt_d85_july_2026_no_pause_research_v1",
        ledger_filename="trade_ledger_no_pause.json",
        bundled_filename="bt_d85_july_trade_ledger_no_pause.json",
        expected_fills=25,
        expected_tp=10,
        expected_sl=15,
        box_prefix="btd85-box-",
        link_prefix="btd85-link-",
        backtester_source="bt_d85_july_chart_overlay_v1",
        payload_kind="bt_d85_july_chart_integration_v1",
        workspace_snap_key="bt_d85_july_research",
        execution_model="BT-D83 D46 research execution (July 2026 no post-SL pause)",
    ),
    "august": FrozenMonthProfile(
        key="august",
        strategy_id="bt_d91_august_ema_v3_no_pause_research",
        display_name="BT-D91 · August 2026 · EMA V3 · 25 Trades",
        results_subdir="phase_bt_d91_august_2026_frozen_v1",
        ledger_filename="trade_ledger_no_pause.json",
        bundled_filename="bt_d91_august_trade_ledger_no_pause.json",
        expected_fills=25,
        expected_tp=4,
        expected_sl=21,
        box_prefix="btd91-box-",
        link_prefix="btd91-link-",
        backtester_source="bt_d91_august_chart_overlay_v1",
        payload_kind="bt_d91_august_chart_integration_v1",
        workspace_snap_key="bt_d91_august_research",
        execution_model="BT-D91 August 2026 frozen research (no post-SL pause)",
    ),
}

STRATEGY_ID_TO_KEY: dict[str, str] = {p.strategy_id: p.key for p in PROFILES.values()}


def get_profile(key_or_strategy_id: str) -> FrozenMonthProfile:
    raw = str(key_or_strategy_id or "").strip()
    if raw in PROFILES:
        return PROFILES[raw]
    if raw in STRATEGY_ID_TO_KEY:
        return PROFILES[STRATEGY_ID_TO_KEY[raw]]
    raise KeyError(f"unknown frozen month profile: {raw}")


def all_profiles() -> list[FrozenMonthProfile]:
    return list(PROFILES.values())


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def ledger_paths(profile: FrozenMonthProfile) -> tuple[Path, Path]:
    pools = (
        resolve_pools_ob_repo()
        / "results"
        / profile.results_subdir
        / profile.ledger_filename
    )
    bundled = Path(__file__).resolve().parent / "data" / profile.bundled_filename
    return pools, bundled


def load_trade_ledger(profile: FrozenMonthProfile) -> tuple[list[dict[str, Any]], Path, list[str]]:
    pools_path, bundled_path = ledger_paths(profile)
    path = pools_path if pools_path.is_file() else bundled_path
    errs: list[str] = []
    if not path.is_file():
        alt = pools_path.parent / "trade_ledger.json"
        if alt.is_file():
            path = alt
        else:
            return [], pools_path, ["ledger_not_found"]
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], path, ["ledger_read_error"]
    if not isinstance(rows, list):
        return [], path, ["ledger_not_array"]
    return rows, path, errs


def validate_ledger(profile: FrozenMonthProfile, rows: list[dict[str, Any]]) -> list[str]:
    errs: list[str] = []
    if len(rows) != profile.expected_fills:
        errs.append(f"fill_count:{len(rows)}")
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    amb = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_EXIT_AMBIGUOUS")
    if tp != profile.expected_tp:
        errs.append(f"tp_count:{tp}")
    if sl != profile.expected_sl:
        errs.append(f"sl_count:{sl}")
    if amb:
        errs.append(f"ambiguous_count:{amb}")
    pids = set()
    for r in rows:
        if not r.get("fill_time_utc") or not r.get("exit_time_utc"):
            errs.append("missing_fill_or_exit_time")
            break
        if r.get("entry_price") is None or r.get("exit_price") is None:
            errs.append("missing_prices")
            break
        if r.get("sl_price") is None or r.get("tp_price") is None:
            errs.append("missing_sl_or_tp")
            break
        pid = str(r.get("position_id") or "")
        if not pid or pid in pids:
            errs.append("duplicate_or_missing_position_id")
            break
        pids.add(pid)
        for ts in (r.get("fill_time_utc"), r.get("exit_time_utc")):
            try:
                _parse_utc(str(ts))
            except (TypeError, ValueError):
                errs.append("invalid_utc_timestamp")
                break
    return errs


def build_summary(
    profile: FrozenMonthProfile,
    rows: list[dict[str, Any]],
    *,
    ledger_path: Path,
) -> dict[str, Any]:
    times = []
    for r in rows:
        times.append(r["fill_time_utc"])
        times.append(r["exit_time_utc"])
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    return {
        "contract_version": CONTRACT_VERSION,
        "strategy_id": profile.strategy_id,
        "month_key": profile.key,
        "symbol": SYMBOL,
        "trade_count": len(rows),
        "tp_count": tp,
        "sl_count": sl,
        "drawing_count": len(rows) * 2,
        "marker_entry_count": len(rows),
        "marker_exit_count": len(rows),
        "fill_disclaimer": FILL_DISCLAIMER,
        "execution_model": profile.execution_model,
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "ledger_path": str(ledger_path),
        "payload_kind": profile.payload_kind,
        "display_name": profile.display_name,
    }


def trade_to_position_spec(profile: FrozenMonthProfile, row: dict[str, Any]) -> dict[str, Any]:
    pid = str(row["position_id"])
    entry_ts = _parse_utc(str(row["fill_time_utc"]))
    exit_ts = _parse_utc(str(row["exit_time_utc"]))
    if exit_ts < entry_ts:
        exit_ts = entry_ts + timedelta(seconds=MIN_BOX_WIDTH_SEC)
    if (exit_ts - entry_ts).total_seconds() < MIN_BOX_WIDTH_SEC:
        exit_ts = entry_ts + timedelta(seconds=MIN_BOX_WIDTH_SEC)
    reason = str(row.get("exit_reason") or "")
    status = "EXIT_TP" if reason == "SIMULATED_TP" else "EXIT_SL"
    return {
        "drawing_id": f"{profile.box_prefix}{pid}",
        "drawing_type": "short_position",
        "start": entry_ts,
        "end": exit_ts,
        "entry": float(row["entry_price"]),
        "stop": float(row["sl_price"]),
        "target": float(row["tp_price"]),
        "metadata": {
            "origin": profile.backtester_source,
            "strategy_id": profile.strategy_id,
            "contract_version": CONTRACT_VERSION,
            "position_id": pid,
            "order_id": row.get("order_id"),
            "pool_key": row.get("pool_key"),
            "signal_close_utc": row.get("signal_close_utc"),
            "fill_time_utc": row.get("fill_time_utc"),
            "exit_time_utc": row.get("exit_time_utc"),
            "exit_reason": reason,
            "exit_price": row.get("exit_price"),
            "gross_return_pct_short": row.get("gross_return_pct_short"),
            "trade_status": status,
            "simulated_only": True,
            "fill_disclaimer": FILL_DISCLAIMER,
            "display_mode": "short_position_with_entry_exit_times_v1",
        },
    }


def trade_to_link_spec(profile: FrozenMonthProfile, row: dict[str, Any]) -> dict[str, Any]:
    pid = str(row["position_id"])
    pos = trade_to_position_spec(profile, row)
    return {
        "drawing_id": f"{profile.link_prefix}{pid}",
        "drawing_type": "trend",
        "start": pos["start"],
        "end": pos["end"],
        "price_a": pos["entry"],
        "price_b": float(row["exit_price"]),
        "metadata": {
            "origin": profile.backtester_source,
            "strategy_id": profile.strategy_id,
            "position_id": pid,
            "link_role": "entry_to_exit",
            "line_style": "dotted",
            "width": 1,
            "opacity": 0.55,
        },
    }


def build_drawing_specs(
    profile: FrozenMonthProfile,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sorted_rows = sorted(rows, key=lambda x: x["fill_time_utc"])
    positions = [trade_to_position_spec(profile, r) for r in sorted_rows]
    links = [trade_to_link_spec(profile, r) for r in rows]
    return positions, links
