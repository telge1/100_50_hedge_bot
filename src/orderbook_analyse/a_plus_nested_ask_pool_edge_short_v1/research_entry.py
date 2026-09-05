"""Programmatic research entry for dashboard / CLI (no strategy logic changes).

Accepts symbol + UTC window, calls existing ``run_backtest``, writes provenance
and a dashboard-agnostic overlay payload next to engine artifacts.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .backtest import run_backtest
from .config import (
    DEFAULT_OUT_DIR,
    MAX_STOP_DISTANCE_PCT,
    ROUNDTRIP_COST_PCT_BASELINE,
    SETUP_TYPE,
    SETUP_VERSION,
    STOP_ATR_BUFFER,
    STOP_TICK_BUFFER,
)

STRATEGY_ID = "a_plus_nested_ask_pool_edge_short_v1"
WARMUP_DAYS = 3
PRIMARY_TARGET_VARIANT = "A_first_bid_near_edge"


def _utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _utc_naive(value)
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return _utc_naive(dt)


def _iso_z(dt: datetime) -> str:
    return _utc_naive(dt).isoformat() + "Z"


def _git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return out.strip()
    except Exception:  # noqa: BLE001
        return "UNAVAILABLE"


def frozen_config() -> dict[str, Any]:
    return {
        "setup_type": SETUP_TYPE,
        "setup_version": SETUP_VERSION,
        "strategy_id": STRATEGY_ID,
        "stop_tick_buffer": STOP_TICK_BUFFER,
        "stop_atr_buffer": STOP_ATR_BUFFER,
        "max_stop_distance_pct": MAX_STOP_DISTANCE_PCT,
        "roundtrip_cost_pct_baseline": ROUNDTRIP_COST_PCT_BASELINE,
        "primary_target_variant": PRIMARY_TARGET_VARIANT,
        "warmup_days": WARMUP_DAYS,
    }


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    df = pd.read_csv(path)
    if df.empty:
        return []
    return df.where(pd.notnull(df), None).to_dict(orient="records")


def _parse_edges(raw: Any) -> tuple[Any, Any]:
    if raw is None:
        return "UNAVAILABLE", "UNAVAILABLE"
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return raw[0], raw[1]
    text = str(raw)
    if text.startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list) and len(arr) >= 2:
                return arr[0], arr[1]
        except json.JSONDecodeError:
            pass
    return "UNAVAILABLE", "UNAVAILABLE"


def bar_close_to_chart_open(ts: datetime) -> datetime:
    """Engine events use bar-close labels; chart candles are indexed by open_time."""
    return _utc_naive(ts) - timedelta(minutes=1)


def _ensure_dt(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return _utc_naive(value)
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return _parse_utc(text)


def build_overlay_payload_from_run_dir(
    run_dir: Path,
    *,
    symbol: str,
    scan_start: datetime,
    scan_end: datetime,
    show_rejected: bool = False,
) -> dict[str, Any]:
    """Pure overlay specs from engine CSVs (no dashboard imports)."""
    candidates = _read_csv(run_dir / "candidates.csv")
    trades = _read_csv(run_dir / "trades.csv")
    rejected = _read_csv(run_dir / "rejected_candidates.csv") if show_rejected else []
    outcomes_flat = _read_csv(run_dir / "outcomes_flat.csv")

    primary_by_cid: dict[str, dict[str, Any]] = {}
    for row in outcomes_flat:
        if str(row.get("target_variant") or "") != PRIMARY_TARGET_VARIANT:
            continue
        cid = str(row.get("candidate_id") or "")
        if cid:
            primary_by_cid[cid] = row

    trade_by_cid = {str(t.get("candidate_id") or ""): t for t in trades}
    specs: list[dict[str, Any]] = []

    def tip_base(row: dict[str, Any], *, status: str) -> dict[str, Any]:
        c_lo, c_hi = _parse_edges(row.get("child_1m_edges"))
        if c_lo == "UNAVAILABLE":
            c_lo, c_hi = row.get("child_pool_low"), row.get("child_pool_high")
        p5_lo, p5_hi = _parse_edges(row.get("parent_5m_edges"))
        p15_lo, p15_hi = _parse_edges(row.get("parent_15m_edges"))
        return {
            "Strategy": "Nested Ask Pool Edge Short V1",
            "Status": status,
            "symbol": row.get("symbol") or symbol,
            "decision_at": row.get("decision_at") or "UNAVAILABLE",
            "order_active_at": row.get("order_active_at") or "UNAVAILABLE",
            "entry_price": row.get("entry_price") or "UNAVAILABLE",
            "child_pool_id": row.get("child_1m_id") or "UNAVAILABLE",
            "child_pool_low": c_lo if c_lo is not None else "UNAVAILABLE",
            "child_pool_high": c_hi if c_hi is not None else "UNAVAILABLE",
            "parent_5m_pool_id": row.get("parent_5m_id") or "UNAVAILABLE",
            "parent_5m_low": p5_lo,
            "parent_5m_high": p5_hi,
            "parent_15m_pool_id": row.get("parent_15m_id") or "UNAVAILABLE",
            "parent_15m_low": p15_lo,
            "parent_15m_high": p15_hi,
            "upper_gap_pct": row.get("upper_gap_pct") if row.get("upper_gap_pct") is not None else "UNAVAILABLE",
            "upper_gap_atr": row.get("upper_gap_atr") if row.get("upper_gap_atr") is not None else "UNAVAILABLE",
            "bid_pools_below": row.get("bid_pool_count_below")
            if row.get("bid_pool_count_below") is not None
            else "UNAVAILABLE",
            "stop_reference": row.get("stop_reference") if row.get("stop_reference") is not None else "UNAVAILABLE",
            "stop_loss": row.get("stop_loss") if row.get("stop_loss") is not None else "UNAVAILABLE",
            "stop_distance_pct": row.get("stop_distance_pct")
            if row.get("stop_distance_pct") is not None
            else "UNAVAILABLE",
            "primary_target": PRIMARY_TARGET_VARIANT,
            "signal_id": row.get("candidate_id") or "UNAVAILABLE",
            "child_episode_id": row.get("episode_id") or "UNAVAILABLE",
            "parent_episode_id": "UNAVAILABLE",
            "orderflow": row.get("orderflow_status") or "UNAVAILABLE",
        }

    def fmt_tooltip(d: dict[str, Any]) -> str:
        return "\n".join(f"{k}={v}" for k, v in d.items())

    for cand in candidates:
        cid = str(cand.get("candidate_id") or "")
        trade = trade_by_cid.get(cid)
        entry = cand.get("entry_price")
        try:
            entry_f = float(entry)
        except (TypeError, ValueError):
            continue
        active = _ensure_dt(cand.get("order_active_at"))
        if active is None:
            continue
        fill_status = str(cand.get("fill_status") or ("FILLED" if trade else "NO_FILL"))
        if trade:
            status = "FILLED"
            end_dt = _ensure_dt(trade.get("fill_at")) or active
        else:
            status = fill_status if fill_status not in {"FILLED"} else "NO_FILL"
            end_dt = _ensure_dt(scan_end) or active

        chart_start = bar_close_to_chart_open(active)
        chart_end = bar_close_to_chart_open(end_dt) if end_dt > active else chart_start
        tip = tip_base(cand, status=status)
        tip["research_note"] = "Research simulation — keine ausgeführten Live-Trades"
        specs.append(
            {
                "overlay_id": f"nap-limit-{cid}",
                "kind": "NAP_PENDING_LIMIT",
                "line_kind": "segment",
                "symbol": symbol,
                "start_timestamp": _iso_z(chart_start),
                "end_timestamp": _iso_z(chart_end),
                "start_price": entry_f,
                "end_price": entry_f,
                "price": entry_f,
                "color": "#e67e22",
                "text": "SHORT LIMIT",
                "label_text": "SHORT LIMIT",
                "tooltip": fmt_tooltip(tip),
                "meta": {
                    "engine_order_active_at": cand.get("order_active_at"),
                    "engine_fill_at": (trade or {}).get("fill_at") or cand.get("fill_at"),
                    "child_pool_id": cand.get("child_1m_id"),
                    "parent_5m_id": cand.get("parent_5m_id"),
                    "parent_15m_id": cand.get("parent_15m_id"),
                    "candidate_id": cid,
                },
            }
        )

    for trade in trades:
        cid = str(trade.get("candidate_id") or "")
        primary = primary_by_cid.get(cid) or {}
        try:
            entry_f = float(trade.get("entry_price") if trade.get("entry_price") is not None else trade.get("fill_price"))
        except (TypeError, ValueError):
            continue
        fill_at = _ensure_dt(trade.get("fill_at"))
        if fill_at is None:
            continue
        chart_fill = bar_close_to_chart_open(fill_at)
        tip = tip_base(trade, status="FILLED")
        tip.update(
            {
                "fill_at": trade.get("fill_at") or "UNAVAILABLE",
                "fill_price": trade.get("fill_price") if trade.get("fill_price") is not None else entry_f,
                "same_bar_ambiguous": trade.get("same_bar_ambiguous")
                if trade.get("same_bar_ambiguous") is not None
                else "UNAVAILABLE",
                "max_feature_timestamp": trade.get("max_feature_timestamp") or "UNAVAILABLE",
                "fee_model_pct": ROUNDTRIP_COST_PCT_BASELINE,
            }
        )
        specs.append(
            {
                "overlay_id": f"nap-fill-{cid}",
                "kind": "NAP_FILL",
                "symbol": symbol,
                "timestamp": _iso_z(chart_fill),
                "price": float(trade.get("fill_price") if trade.get("fill_price") is not None else entry_f),
                "shape": "arrow_down",
                "color": "#d62728",
                "text": "SHORT FILL",
                "position": "at_price",
                "tooltip": fmt_tooltip(tip),
                "meta": {
                    "engine_fill_at": trade.get("fill_at"),
                    "candidate_id": cid,
                    "child_pool_id": trade.get("child_1m_id"),
                    "parent_5m_id": trade.get("parent_5m_id"),
                    "parent_15m_id": trade.get("parent_15m_id"),
                },
            }
        )

        stop = trade.get("stop_loss")
        try:
            stop_f = float(stop)
        except (TypeError, ValueError):
            stop_f = None
        exit_at = _ensure_dt(primary.get("exit_at"))
        if exit_at is None:
            exit_at = fill_at + timedelta(minutes=240)
        chart_exit = bar_close_to_chart_open(exit_at)

        if stop_f is not None:
            specs.append(
                {
                    "overlay_id": f"nap-sl-{cid}",
                    "kind": "NAP_SL",
                    "line_kind": "segment",
                    "symbol": symbol,
                    "start_timestamp": _iso_z(chart_fill),
                    "end_timestamp": _iso_z(chart_exit),
                    "start_price": stop_f,
                    "end_price": stop_f,
                    "price": stop_f,
                    "color": "#ff6b6b",
                    "text": "SL",
                    "label_text": "SL",
                    "tooltip": fmt_tooltip({**tip, "stop_loss": stop_f}),
                    "meta": {"candidate_id": cid},
                }
            )

        try:
            tp_f = float(primary["target_price"]) if primary.get("target_price") is not None else None
        except (TypeError, ValueError):
            tp_f = None
        if tp_f is not None:
            other_variants = [
                f"{r.get('target_variant')}={r.get('target_price')}"
                for r in outcomes_flat
                if str(r.get("candidate_id")) == cid and str(r.get("target_variant")) != PRIMARY_TARGET_VARIANT
            ]
            specs.append(
                {
                    "overlay_id": f"nap-tp-{cid}",
                    "kind": "NAP_TP",
                    "line_kind": "segment",
                    "symbol": symbol,
                    "start_timestamp": _iso_z(chart_fill),
                    "end_timestamp": _iso_z(chart_exit),
                    "start_price": tp_f,
                    "end_price": tp_f,
                    "price": tp_f,
                    "color": "#51cf66",
                    "text": "TP",
                    "label_text": "TP",
                    "tooltip": fmt_tooltip(
                        {
                            **tip,
                            "primary_target_price": tp_f,
                            "other_tp_variants": "; ".join(other_variants) if other_variants else "UNAVAILABLE",
                        }
                    ),
                    "meta": {"candidate_id": cid, "target_variant": PRIMARY_TARGET_VARIANT},
                }
            )

        result = str(primary.get("result") or trade.get("primary_result") or "UNAVAILABLE")
        label_map = {
            "TP_FIRST": "TP",
            "SL_FIRST": "SL",
            "NEITHER": "TIMEOUT",
            "AMBIGUOUS": "AMBIGUOUS",
            "NO_TARGET": "TIMEOUT",
        }
        exit_label = label_map.get(result, result)
        exit_price = primary.get("exit_price")
        if exit_price is None:
            if result == "SL_FIRST" and stop_f is not None:
                exit_price = stop_f
            elif result == "TP_FIRST" and tp_f is not None:
                exit_price = tp_f
            else:
                exit_price = entry_f
        try:
            exit_px = float(exit_price)
        except (TypeError, ValueError):
            exit_px = entry_f
        exit_tip = {
            **tip,
            "outcome": result,
            "exit_at": primary.get("exit_at") or "UNAVAILABLE",
            "exit_price": exit_px,
            "gross_return": primary.get("gross_pnl_pct")
            if primary.get("gross_pnl_pct") is not None
            else trade.get("primary_gross_pnl_pct", "UNAVAILABLE"),
            "total_costs": ROUNDTRIP_COST_PCT_BASELINE,
            "net_return": primary.get("net_pnl_pct")
            if primary.get("net_pnl_pct") is not None
            else trade.get("primary_net_pnl_pct", "UNAVAILABLE"),
            "MFE": primary.get("mfe") if primary.get("mfe") is not None else trade.get("primary_mfe", "UNAVAILABLE"),
            "MAE": primary.get("mae") if primary.get("mae") is not None else trade.get("primary_mae", "UNAVAILABLE"),
            "hold_minutes": primary.get("hold_minutes")
            if primary.get("hold_minutes") is not None
            else trade.get("primary_hold_minutes", "UNAVAILABLE"),
        }
        specs.append(
            {
                "overlay_id": f"nap-exit-{cid}",
                "kind": "NAP_EXIT",
                "symbol": symbol,
                "timestamp": _iso_z(chart_exit),
                "price": exit_px,
                "shape": "circle",
                "color": "#888888" if exit_label == "TIMEOUT" else ("#51cf66" if exit_label == "TP" else "#d62728"),
                "text": exit_label,
                "position": "at_price",
                "tooltip": fmt_tooltip(exit_tip),
                "meta": {"candidate_id": cid, "outcome": result},
            }
        )

    if show_rejected:
        for i, row in enumerate(rejected):
            ts = _ensure_dt(row.get("decision_at"))
            if ts is None:
                continue
            px = row.get("entry_price") or row.get("price_at_decision")
            try:
                px_f = float(px) if px is not None else None
            except (TypeError, ValueError):
                px_f = None
            specs.append(
                {
                    "overlay_id": f"nap-rej-{i}-{row.get('child_pool_id') or i}",
                    "kind": "NAP_REJECTED",
                    "symbol": symbol,
                    "timestamp": _iso_z(bar_close_to_chart_open(ts)),
                    "price": px_f,
                    "shape": "circle",
                    "color": "#999999",
                    "text": "REJ",
                    "position": "at_price",
                    "tooltip": fmt_tooltip(
                        {
                            "Status": "REJECTED",
                            "reject_reason": row.get("reason") or "UNAVAILABLE",
                            "decision_at": row.get("decision_at"),
                            "child_pool_id": row.get("child_pool_id") or "UNAVAILABLE",
                        }
                    ),
                    "meta": {"reject_reason": row.get("reason")},
                }
            )

    return {
        "strategy_id": STRATEGY_ID,
        "setup_type": SETUP_TYPE,
        "symbol": symbol,
        "scan_start": _iso_z(scan_start),
        "scan_end": _iso_z(scan_end),
        "show_rejected": show_rejected,
        "n_specs": len(specs),
        "specs": specs,
        "time_semantics": {
            "engine_event_clock": "bar_close",
            "chart_candle_index": "open_time",
            "conversion": "chart_time = engine_bar_close - 1m",
        },
        "pool_highlight": "UNAVAILABLE_IN_CHART_OVERLAY — IDs/edges in tooltip only",
    }


def run_single_symbol_research_backtest(
    *,
    symbol: str,
    start: str | datetime,
    end: str | datetime,
    out_dir: Path | str | None = None,
    show_rejected_overlays: bool = False,
) -> dict[str, Any]:
    """Run nested engine for one symbol/window; write provenance + overlay payload."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol_required")
    if "," in sym or " " in sym:
        raise ValueError("single_symbol_required")
    scan_start = _parse_utc(start)
    scan_end = _parse_utc(end)
    if scan_end <= scan_start:
        raise ValueError("START_NOT_BEFORE_END")

    warmup_start = scan_start - timedelta(days=WARMUP_DAYS)
    root = Path(out_dir) if out_dir else Path(DEFAULT_OUT_DIR)
    root.mkdir(parents=True, exist_ok=True)

    result = run_backtest(
        symbols=[sym],
        warmup_start=warmup_start,
        scan_start=scan_start,
        scan_end=scan_end,
        out_dir=root,
    )
    run_path = Path(result["out_dir"])
    coverage = (result.get("summary") or {}).get("coverage") or {}
    cov = coverage.get(sym) or {}
    if cov.get("status") == "NO_CANDLES":
        raise ValueError(f"NO_CANDLE_COVERAGE for {sym} in [{scan_start.isoformat()}, {scan_end.isoformat()}]")
    if cov.get("status") != "OK":
        raise ValueError(f"INCOMPLETE_COVERAGE: {cov}")

    overlay = build_overlay_payload_from_run_dir(
        run_path,
        symbol=sym,
        scan_start=scan_start,
        scan_end=scan_end,
        show_rejected=show_rejected_overlays,
    )
    (run_path / "dashboard_overlay_payload.json").write_text(
        json.dumps(overlay, indent=2, default=str), encoding="utf-8"
    )

    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    provenance = {
        "run_id": result.get("run_id"),
        "strategy_id": STRATEGY_ID,
        "setup_type": SETUP_TYPE,
        "setup_version": SETUP_VERSION,
        "symbol": sym,
        "start_utc": _iso_z(scan_start),
        "end_utc": _iso_z(scan_end),
        "warmup_start_utc": _iso_z(warmup_start),
        "frozen_config": frozen_config(),
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "status": "completed",
        "source_commit": _git_head(),
        "out_dir": str(run_path),
        "summary": result.get("summary"),
        "research_note": "Research simulation — keine ausgeführten Live-Trades",
        "not_aps": True,
        "aps_results_root_forbidden": (
            "/home/telgenbuescher/projects/orderbook_analyse/results/"
            "a_plus_liquidity_pool_signal_scanner_v1"
        ),
    }
    (run_path / "dashboard_provenance.json").write_text(
        json.dumps(provenance, indent=2, default=str), encoding="utf-8"
    )

    # Enrich manifest without rewriting engine summary semantics
    man_path = run_path / "manifest.json"
    manifest: dict[str, Any] = {}
    if man_path.is_file():
        try:
            manifest = json.loads(man_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    manifest.update(
        {
            "strategy_id": STRATEGY_ID,
            "dashboard_provenance": provenance,
            "frozen_config": frozen_config(),
            "source_commit": provenance["source_commit"],
        }
    )
    man_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    return {
        "run_id": result.get("run_id"),
        "out_dir": str(run_path),
        "summary": result.get("summary"),
        "overlay": overlay,
        "provenance": provenance,
        "strategy_id": STRATEGY_ID,
        "symbol": sym,
    }
