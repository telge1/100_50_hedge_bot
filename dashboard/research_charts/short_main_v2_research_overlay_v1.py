"""SHORT MAIN V2 combined exit → Research Chart overlay (read-only, Phase 3 ledgers)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

CONTRACT_VERSION = "short_main_v2_dashboard_overlay_v1"
SYMBOL = "DOGEUSDT"
MIN_BOX_WIDTH_SEC = 300
FILL_DISCLAIMER = "SHORT MAIN V2 frozen research replay · SIMULATED — NOT EXCHANGE CONFIRMED"
BACKTESTER_SOURCE = "short_main_v2_combined_exit_chart_overlay_v1"


@dataclass(frozen=True)
class ShortMainV2MonthProfile:
    month_key: str
    strategy_id: str
    display_name: str
    coverage: str
    ledger_relpath: str
    bundled_filename: str
    expected_trades: int
    expected_sl: int
    expected_gap_tp: int
    expected_ema: int
    box_prefix: str
    link_prefix: str
    tp_seg_prefix: str
    workspace_snap_key: str
    backtester_source: str
    payload_kind: str


MONTH_PROFILES: dict[str, ShortMainV2MonthProfile] = {
    "june": ShortMainV2MonthProfile(
        month_key="june",
        strategy_id="short_main_v2_june_combined_exit_research",
        display_name="SHORT MAIN V2 · Juni 2026 · D107 SL + Dynamic TP · 10 Trades",
        coverage="FULL",
        ledger_relpath="results/phase_short_main_v2_combined_exit_v1/phase3_monthly/june/variant_c_trade_ledger.json",
        bundled_filename="short_main_v2_june_variant_c_trade_ledger.json",
        expected_trades=10,
        expected_sl=4,
        expected_gap_tp=4,
        expected_ema=2,
        box_prefix="btv2june-box-",
        link_prefix="btv2june-link-",
        tp_seg_prefix="btv2june-tp-",
        workspace_snap_key="short_main_v2_june_research",
        backtester_source=BACKTESTER_SOURCE,
        payload_kind="short_main_v2_combined_exit_chart_integration_v1",
    ),
    "july": ShortMainV2MonthProfile(
        month_key="july",
        strategy_id="short_main_v2_july_combined_exit_research",
        display_name="SHORT MAIN V2 · Juli 2026 · D107 SL + Dynamic TP · 8 Trades",
        coverage="FULL",
        ledger_relpath="results/phase_short_main_v2_combined_exit_v1/phase3_monthly/july/variant_c_trade_ledger.json",
        bundled_filename="short_main_v2_july_variant_c_trade_ledger.json",
        expected_trades=8,
        expected_sl=4,
        expected_gap_tp=1,
        expected_ema=3,
        box_prefix="btv2july-box-",
        link_prefix="btv2july-link-",
        tp_seg_prefix="btv2july-tp-",
        workspace_snap_key="short_main_v2_july_research",
        backtester_source=BACKTESTER_SOURCE,
        payload_kind="short_main_v2_combined_exit_chart_integration_v1",
    ),
    "august": ShortMainV2MonthProfile(
        month_key="august",
        strategy_id="short_main_v2_august_combined_exit_research",
        display_name="SHORT MAIN V2 · August 2026 · D107 SL + Dynamic TP · 3 Trades",
        coverage="FULL",
        ledger_relpath="results/phase_short_main_v2_combined_exit_v1/phase3_monthly/august/variant_c_trade_ledger.json",
        bundled_filename="short_main_v2_august_variant_c_trade_ledger.json",
        expected_trades=3,
        expected_sl=3,
        expected_gap_tp=0,
        expected_ema=0,
        box_prefix="btv2aug-box-",
        link_prefix="btv2aug-link-",
        tp_seg_prefix="btv2aug-tp-",
        workspace_snap_key="short_main_v2_august_research",
        backtester_source=BACKTESTER_SOURCE,
        payload_kind="short_main_v2_combined_exit_chart_integration_v1",
    ),
    "september": ShortMainV2MonthProfile(
        month_key="september",
        strategy_id="short_main_v2_september_partial_combined_exit_research",
        display_name="SHORT MAIN V2 · September 2026 PARTIAL · D107 SL + Dynamic TP · 6 Trades",
        coverage="PARTIAL",
        ledger_relpath="results/phase_short_main_v2_combined_exit_v1/phase3_monthly/september/variant_c_trade_ledger.json",
        bundled_filename="short_main_v2_september_variant_c_trade_ledger.json",
        expected_trades=6,
        expected_sl=4,
        expected_gap_tp=0,
        expected_ema=2,
        box_prefix="btv2sep-box-",
        link_prefix="btv2sep-link-",
        tp_seg_prefix="btv2sep-tp-",
        workspace_snap_key="short_main_v2_september_research",
        backtester_source=BACKTESTER_SOURCE,
        payload_kind="short_main_v2_combined_exit_chart_integration_v1",
    ),
}

STRATEGY_ID_TO_MONTH: dict[str, str] = {p.strategy_id: p.month_key for p in MONTH_PROFILES.values()}


def get_profile(key_or_strategy_id: str) -> ShortMainV2MonthProfile:
    raw = str(key_or_strategy_id or "").strip()
    if raw in MONTH_PROFILES:
        return MONTH_PROFILES[raw]
    if raw in STRATEGY_ID_TO_MONTH:
        return MONTH_PROFILES[STRATEGY_ID_TO_MONTH[raw]]
    raise KeyError(f"unknown SHORT MAIN V2 profile: {raw}")


def all_profiles() -> list[ShortMainV2MonthProfile]:
    return list(MONTH_PROFILES.values())


def all_drawing_prefixes() -> set[str]:
    out: set[str] = set()
    for p in MONTH_PROFILES.values():
        out.add(p.box_prefix)
        out.add(p.link_prefix)
        out.add(p.tp_seg_prefix)
    return out


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def ledger_paths(profile: ShortMainV2MonthProfile) -> tuple[Path, Path]:
    pools = resolve_pools_ob_repo() / profile.ledger_relpath
    bundled = Path(__file__).resolve().parent / "data" / profile.bundled_filename
    return pools, bundled


def load_trade_ledger(profile: ShortMainV2MonthProfile) -> tuple[list[dict[str, Any]], Path, list[str]]:
    pools_path, bundled_path = ledger_paths(profile)
    path = pools_path if pools_path.is_file() else bundled_path
    if not path.is_file():
        return [], pools_path, ["ledger_not_found"]
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], path, ["ledger_read_error"]
    if not isinstance(rows, list):
        return [], path, ["ledger_not_array"]
    return rows, path, []


def validate_ledger(profile: ShortMainV2MonthProfile, rows: list[dict[str, Any]]) -> list[str]:
    errs: list[str] = []
    if len(rows) != profile.expected_trades:
        errs.append(f"trade_count:{len(rows)}")
    sl = sum(1 for r in rows if r.get("exit_reason") == "STRUCTURAL_SL")
    gap = sum(1 for r in rows if r.get("exit_reason") == "LOWER_POOL_GAP_TP")
    ema = sum(1 for r in rows if r.get("exit_reason") == "EMA59_200_CROSS")
    amb = sum(1 for r in rows if r.get("ambiguous_intrabar"))
    if sl != profile.expected_sl:
        errs.append(f"structural_sl_count:{sl}")
    if gap != profile.expected_gap_tp:
        errs.append(f"gap_tp_count:{gap}")
    if ema != profile.expected_ema:
        errs.append(f"ema_exit_count:{ema}")
    if amb:
        errs.append(f"ambiguous_count:{amb}")
    pids: set[str] = set()
    for r in rows:
        pid = str(r.get("position_id") or "")
        if not pid or pid in pids:
            errs.append("duplicate_or_missing_position_id")
            break
        pids.add(pid)
        for k in ("signal_close_utc", "fill_time_utc", "exit_time_utc", "entry_price", "exit_price", "effective_sl_price"):
            if r.get(k) is None and k != "effective_sl_price":
                errs.append(f"missing:{k}")
                break
        else:
            if r.get("effective_sl_price") is None:
                errs.append("missing:effective_sl_price")
    return errs


def _holding_minutes(row: dict[str, Any]) -> int | None:
    try:
        a = _parse_utc(str(row["fill_time_utc"]))
        b = _parse_utc(str(row["exit_time_utc"]))
        return int((b - a).total_seconds() / 60)
    except (TypeError, ValueError):
        return None


def _trade_status(reason: str, gross: float | None) -> str:
    if reason == "LOWER_POOL_GAP_TP":
        return "EXIT_LOWER_POOL_GAP_TP"
    if reason == "EMA59_200_CROSS":
        return "EXIT_EMA59_200_CROSS"
    if reason == "STRUCTURAL_SL":
        return "EXIT_STRUCTURAL_SL"
    if reason == "AMBIGUOUS_INTRABAR":
        return "EXIT_AMBIGUOUS"
    if gross is not None and gross > 0:
        return "EXIT_TP"
    return "EXIT_SL"


def tp_limit_segments_from_audit(
    audit: list[dict[str, Any]],
    *,
    exit_bar: str,
    exit_reason: str,
) -> tuple[list[tuple[str, str, float, bool]], list[str]]:
    """Return TP limit line segments strictly from order_audit (no candle inference).

    Each segment is (start_bar, end_bar, limit_price, segment_complete).
    """
    segments: list[tuple[str, str, float, bool]] = []
    incomplete_notes: list[str] = []
    active_start: str | None = None
    active_price: float | None = None
    last_active_bar: str | None = None

    def close(end_bar: str, *, complete: bool) -> None:
        nonlocal active_start, active_price, last_active_bar
        if active_start and active_price is not None and end_bar >= active_start:
            segments.append((active_start, end_bar, active_price, complete))
        active_start = None
        active_price = None
        last_active_bar = None

    for ev in sorted(audit, key=lambda x: str(x.get("bar_utc") or "")):
        act = str(ev.get("action") or "")
        bar = str(ev.get("bar_utc") or "")
        detail = ev.get("detail") or {}
        if act == "TP_LIMIT_PLACE":
            close(bar, complete=True)
            active_start = bar
            active_price = float(detail.get("price") or detail.get("tp_limit_price") or 0)
            last_active_bar = bar
        elif act == "TP_LIMIT_REPLACE":
            new_px = float(detail.get("new") or detail.get("price") or 0)
            close(bar, complete=True)
            active_start = bar
            active_price = new_px
            last_active_bar = bar
        elif act == "TP_LIMIT_UNCHANGED":
            if active_start and active_price is not None:
                last_active_bar = bar or last_active_bar
        elif act == "TP_LIMIT_CANCEL":
            close(bar, complete=True)
        elif act in ("TP_LIMIT_FILL", "TP_GAP_AT_OPEN"):
            if active_start is None:
                incomplete_notes.append("tp_fill_without_prior_place")
                continue
            close(bar, complete=True)

    if active_start and active_price is not None:
        if exit_reason == "LOWER_POOL_GAP_TP":
            close(exit_bar, complete=True)
        else:
            end_bar = last_active_bar or active_start
            if end_bar >= active_start:
                segments.append((active_start, end_bar, active_price, False))
            incomplete_notes.append("tp_active_without_cancel_or_fill_in_audit")
    return segments, incomplete_notes


def assess_tp_limit_audit(row: dict[str, Any]) -> dict[str, Any]:
    audit = row.get("order_audit") or []
    exit_reason = str(row.get("exit_reason") or "")
    segments, notes = tp_limit_segments_from_audit(
        audit,
        exit_bar=str(row.get("exit_time_utc") or ""),
        exit_reason=exit_reason,
    )
    has_place = any(str(e.get("action") or "") == "TP_LIMIT_PLACE" for e in audit)
    complete = not notes and all(s[3] for s in segments)
    if not has_place and exit_reason != "LOWER_POOL_GAP_TP":
        status = "none"
    elif complete:
        status = "complete"
    else:
        status = "partial"
    return {
        "tp_limit_audit_status": status,
        "tp_limit_audit_incomplete": status == "partial",
        "tp_limit_audit_notes": notes,
        "tp_limit_segment_count": len(segments),
    }


def build_summary(
    profile: ShortMainV2MonthProfile,
    rows: list[dict[str, Any]],
    *,
    ledger_path: Path,
) -> dict[str, Any]:
    times: list[str] = []
    for r in rows:
        times.append(str(r["fill_time_utc"]))
        times.append(str(r["exit_time_utc"]))
    sl = sum(1 for r in rows if r.get("exit_reason") == "STRUCTURAL_SL")
    gap = sum(1 for r in rows if r.get("exit_reason") == "LOWER_POOL_GAP_TP")
    ema = sum(1 for r in rows if r.get("exit_reason") == "EMA59_200_CROSS")
    tp_partial = sum(1 for r in rows if assess_tp_limit_audit(r)["tp_limit_audit_status"] == "partial")
    return {
        "contract_version": CONTRACT_VERSION,
        "strategy_id": profile.strategy_id,
        "month_key": profile.month_key,
        "coverage": profile.coverage,
        "symbol": SYMBOL,
        "trade_count": len(rows),
        "structural_sl_count": sl,
        "gap_tp_count": gap,
        "ema_exit_count": ema,
        "tp_count": gap,
        "sl_count": sl,
        "fill_disclaimer": FILL_DISCLAIMER,
        "execution_model": "D100-A entry · D107 structural SL · gap TP + EMA59/200 exit (Phase 3)",
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "ledger_path": str(ledger_path),
        "payload_kind": "short_main_v2_combined_exit_chart_integration_v1",
        "display_name": profile.display_name,
        "tp_limit_partial_count": tp_partial,
    }


def trade_to_position_spec(profile: ShortMainV2MonthProfile, row: dict[str, Any]) -> dict[str, Any]:
    pid = str(row["position_id"])
    entry_ts = _parse_utc(str(row["fill_time_utc"]))
    exit_ts = _parse_utc(str(row["exit_time_utc"]))
    if exit_ts < entry_ts:
        exit_ts = entry_ts + timedelta(seconds=MIN_BOX_WIDTH_SEC)
    if (exit_ts - entry_ts).total_seconds() < MIN_BOX_WIDTH_SEC:
        exit_ts = entry_ts + timedelta(seconds=MIN_BOX_WIDTH_SEC)
    reason = str(row.get("exit_reason") or "")
    gross = row.get("gross_return_pct_short")
    gross_f = float(gross) if gross is not None else None
    sl = float(row["effective_sl_price"])
    tp_show = row.get("tp_limit_price_at_exit")
    entry_px = float(row["entry_price"])
    if reason == "LOWER_POOL_GAP_TP":
        target = float(tp_show) if tp_show is not None else float(row["exit_price"])
        tp_target_suppressed = False
    elif tp_show is not None:
        target = float(tp_show)
        tp_target_suppressed = False
    else:
        target = entry_px
        tp_target_suppressed = True
    audit = row.get("order_audit") or []
    ema_arm = next((e for e in audit if e.get("action") == "EMA_CROSS_ARMED"), None)
    tp_audit = assess_tp_limit_audit(row)
    return {
        "drawing_id": f"{profile.box_prefix}{pid}",
        "drawing_type": "short_position",
        "start": entry_ts,
        "end": exit_ts,
        "entry": float(row["entry_price"]),
        "stop": sl,
        "target": target,
        "metadata": {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": profile.strategy_id,
            "contract_version": CONTRACT_VERSION,
            "position_id": pid,
            "signal_close_utc": row.get("signal_close_utc"),
            "fill_time_utc": row.get("fill_time_utc"),
            "exit_time_utc": row.get("exit_time_utc"),
            "entry_price": row.get("entry_price"),
            "exit_price": row.get("exit_price"),
            "effective_sl_price": sl,
            "tp_limit_price_at_exit": tp_show,
            "exit_reason": reason,
            "exit_execution": row.get("exit_execution"),
            "gross_return_pct_short": gross_f,
            "holding_minutes": _holding_minutes(row),
            "trade_status": _trade_status(reason, gross_f),
            "ema_cross_armed": ema_arm,
            "ema_cross_from_audit_only": True,
            "tp_target_line_suppressed": tp_target_suppressed,
            **tp_audit,
            "structural_sl_meta": row.get("structural_sl_meta"),
            "pool_key": row.get("pool_key"),
            "simulated_only": True,
            "fill_disclaimer": FILL_DISCLAIMER,
            "display_mode": "short_main_v2_combined_exit_v1",
            "coverage": profile.coverage,
        },
    }


def trade_to_link_spec(profile: ShortMainV2MonthProfile, row: dict[str, Any]) -> dict[str, Any]:
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
            "origin": BACKTESTER_SOURCE,
            "strategy_id": profile.strategy_id,
            "position_id": pid,
            "link_role": "entry_to_exit",
            "exit_reason": row.get("exit_reason"),
            "gross_return_pct_short": row.get("gross_return_pct_short"),
            "line_style": "dotted",
            "width": 1,
            "opacity": 0.55,
        },
    }


def tp_segment_specs(profile: ShortMainV2MonthProfile, row: dict[str, Any]) -> list[dict[str, Any]]:
    pid = str(row["position_id"])
    audit = row.get("order_audit") or []
    exit_bar = str(row["exit_time_utc"])
    exit_reason = str(row.get("exit_reason") or "")
    tp_audit = assess_tp_limit_audit(row)
    specs: list[dict[str, Any]] = []
    segments, _notes = tp_limit_segments_from_audit(
        audit,
        exit_bar=exit_bar,
        exit_reason=exit_reason,
    )
    for i, (start_bar, end_bar, price, segment_complete) in enumerate(segments):
        try:
            t0 = _parse_utc(start_bar)
            t1 = _parse_utc(end_bar)
        except (TypeError, ValueError):
            continue
        if t1 < t0:
            t1 = t0 + timedelta(seconds=MIN_BOX_WIDTH_SEC)
        seg_color = "#38bdf8" if segment_complete else "#fbbf24"
        specs.append(
            {
                "drawing_id": f"{profile.tp_seg_prefix}{pid}-{i}",
                "drawing_type": "trend",
                "start": t0,
                "end": t1,
                "price_a": price,
                "price_b": price,
                "metadata": {
                    "origin": BACKTESTER_SOURCE,
                    "strategy_id": profile.strategy_id,
                    "position_id": pid,
                    "link_role": "active_gap_tp_limit",
                    "tp_limit_price": price,
                    "active_from_bar": start_bar,
                    "active_until_bar": end_bar,
                    "tp_segment_complete": segment_complete,
                    "tp_limit_audit_status": tp_audit["tp_limit_audit_status"],
                    "tp_limit_audit_incomplete": not segment_complete,
                    "tp_limit_audit_notes": tp_audit["tp_limit_audit_notes"],
                    "line_style": "dashed" if not segment_complete else "solid",
                    "width": 1,
                    "opacity": 0.85 if segment_complete else 0.65,
                    "color": seg_color,
                },
            }
        )
    return specs


def build_drawing_specs(
    profile: ShortMainV2MonthProfile,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sorted_rows = sorted(rows, key=lambda x: str(x["fill_time_utc"]))
    positions = [trade_to_position_spec(profile, r) for r in sorted_rows]
    links = [trade_to_link_spec(profile, r) for r in rows]
    tp_segs: list[dict[str, Any]] = []
    for r in rows:
        tp_segs.extend(tp_segment_specs(profile, r))
    return positions, links, tp_segs
