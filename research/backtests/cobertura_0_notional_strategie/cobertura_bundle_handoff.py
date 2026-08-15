"""Research-only Handoff: bundle historical blocker → Cobertura start state.

Does not run recovery / exit / reclaim logic. Uses real Cobertura ledger seeding
and SidePosition VWAP math plus historical ``compute_neutralization``.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.emergency_lock.cost_model import fee_usdt
from research.backtests.multicoin_price_staging_grid import (
    atomic_write_json,
    atomic_write_text,
    write_csv,
)

from .config import CoberturaConfig
from .engine import CoberturaEngine
from .historical_blocker_state_extraction import APT_REFERENCE_TRADE_ID, parse_ts
from .historical_blocker_state_extraction import compute_neutralization
from .ledger import CoberturaLedger

SCHEMA_VERSION = "1.0"
ABS_TOL = 1e-9
QTY_TOL = 1e-9

APT_TRADE_ID = APT_REFERENCE_TRADE_ID
DEFAULT_SCENARIO_ID = "full_qty_neutralization_spread_only_v1"
TRIGGER_MODE = "first_break"

APT_EXPECT = {
    "structure_break_level": 1.7639,
    "market_price_at_signal": 1.7223,
    "neutralization_fill_price": 1.7223,
    "tradeable_5m_open": 1.7223,
    "distance_break_to_market_pct": -0.023584103407222723,
    "taker_fee_rate": 0.00055,
    "long_qty": 296.365,
    "long_avg": 1.864531340748192,
    "short_qty": 197.59699999999998,
    "short_avg": 1.864561269615919,
    "net_qty": 98.76800000000003,
    "fills_before_signal": 9,
    "fills_at_or_after_signal": 4,
    "active_cycle": 4,
    "open_order_count": 4,
    "realized_pnl": -11.900133102067503,
    "unrealized_pnl_at_signal": -14.041991208541187,
    "total_economics_at_signal": -25.94212431060869,
    "neutralization_qty": 98.76800000000003,
    "neutralization_notional": 170.10812640000003,
    "neutralization_fee": 0.09355946952000002,
    "post_short_avg": 1.8171506068270433,
}


class HandoffError(ValueError):
    """Hard refusal to start Cobertura from a bundle record."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return x


def _close(a: float | None, b: float | None, tol: float = ABS_TOL) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def select_bundle_record(
    records: list[dict[str, Any]],
    *,
    trade_id: str,
    trigger_mode: str = TRIGGER_MODE,
) -> dict[str, Any]:
    matches = [
        r
        for r in records
        if r.get("trade_id") == trade_id
        and (r.get("trigger") or {}).get("trigger_mode") == trigger_mode
    ]
    if not matches:
        raise HandoffError(
            f"no bundle record for trade_id={trade_id!r} trigger_mode={trigger_mode!r}"
        )
    if len(matches) > 1:
        raise HandoffError(
            f"duplicate bundle records for trade_id={trade_id!r} "
            f"trigger_mode={trigger_mode!r}: count={len(matches)}"
        )
    return deepcopy(matches[0])


def select_scenario(
    scenarios: list[dict[str, Any]],
    *,
    scenario_id: str,
) -> dict[str, Any]:
    matches = [s for s in scenarios if s.get("scenario_id") == scenario_id]
    if not matches:
        raise HandoffError(f"scenario_id not found: {scenario_id!r}")
    if len(matches) > 1:
        raise HandoffError(f"duplicate scenario_id: {scenario_id!r}")
    return deepcopy(matches[0])


def validate_bundle_record_for_handoff(
    record: dict[str, Any],
    scenario: dict[str, Any],
    *,
    require_cancel_source_orders: bool = True,
) -> list[str]:
    """Return soft warnings; raise HandoffError on hard refusal."""
    reasons: list[str] = []
    trig = record.get("trigger") or {}
    market = record.get("market") or {}
    pos = record.get("pre_signal_position") or {}
    quality = record.get("quality") or {}
    source_orders = record.get("source_orders") or {}
    orders = list(source_orders.get("orders") or [])

    if not record.get("trade_id"):
        reasons.append("MISSING_TRADE_ID")
    if trig.get("trigger_mode") != TRIGGER_MODE:
        reasons.append("TRIGGER_MODE_NOT_FIRST_BREAK")
    if quality.get("ready_for_cobertura") is not True:
        reasons.append("NOT_READY_FOR_COBERTURA")
    if quality.get("replay_match_status") != "REPLAY_MATCH":
        reasons.append("REPLAY_NOT_MATCH")
    if quality.get("replay_diff_count") is None or int(quality.get("replay_diff_count")) != 0:
        reasons.append("REPLAY_DIFF_NONEZERO")
    cutoff_v = quality.get("ledger_cutoff_violations")
    if cutoff_v is None or int(cutoff_v) > 0:
        reasons.append("LEDGER_CUTOFF_VIOLATIONS")

    level = _f(trig.get("structure_break_level"))
    if level is None or level <= 0:
        reasons.append("MISSING_OR_NONPOSITIVE_BREAK_LEVEL")
    if not trig.get("structure_break_kind"):
        reasons.append("MISSING_BREAK_KIND")
    if not trig.get("signal_available_ts"):
        reasons.append("MISSING_SIGNAL_AVAILABLE_TS")

    mkt = _f(market.get("market_price_at_signal"))
    fill = _f(market.get("neutralization_fill_price"))
    if mkt is None or mkt <= 0:
        reasons.append("MISSING_OR_NONPOSITIVE_MARKET_PRICE")
    if fill is None or fill <= 0:
        reasons.append("MISSING_OR_NONPOSITIVE_NEUTRALIZATION_FILL")

    lq = _f(pos.get("long_qty"))
    sq = _f(pos.get("short_qty"))
    la = _f(pos.get("long_avg"))
    sa = _f(pos.get("short_avg"))
    if lq is None or lq < 0 or sq is None or sq < 0:
        reasons.append("INVALID_QTY")
    if lq and lq > 0 and (la is None or la <= 0):
        reasons.append("INVALID_LONG_AVG")
    if sq and sq > 0 and (sa is None or sa <= 0):
        reasons.append("INVALID_SHORT_AVG")

    last_fill = parse_ts(pos.get("last_fill_timestamp_before_signal"))
    signal_ts = parse_ts(trig.get("signal_available_ts"))
    if last_fill is None or signal_ts is None or not (last_fill < signal_ts):
        reasons.append("LAST_FILL_NOT_STRICTLY_BEFORE_SIGNAL")

    open_count = int(pos.get("open_order_count") or 0)
    if open_count != len(orders):
        reasons.append(
            f"SOURCE_ORDER_COUNT_MISMATCH:open_order_count={open_count} orders={len(orders)}"
        )

    cancel_flag = bool(source_orders.get("cancel_on_cobertura_handoff"))
    if require_cancel_source_orders and not cancel_flag:
        reasons.append("CANCEL_ON_HANDOFF_REQUIRED")
    if scenario.get("cancel_source_strategy_orders") is not True:
        reasons.append("SCENARIO_REQUIRES_CANCEL_SOURCE_ORDERS")
    if scenario.get("inherit_source_cycle_state") is True:
        reasons.append("SCENARIO_MUST_NOT_INHERIT_SOURCE_CYCLE")
    if scenario.get("neutralization_mode") != "MATCH_SMALLER_SIDE_TO_LARGER_SIDE":
        reasons.append("UNSUPPORTED_NEUTRALIZATION_MODE")

    if reasons:
        raise HandoffError("handoff refused: " + "; ".join(reasons))

    warnings = list(quality.get("warnings") or [])
    fee_q = (record.get("prior_economics") or {}).get("fee_quality")
    if fee_q and fee_q not in warnings and fee_q != "FEES_COMPLETE":
        warnings.append(str(fee_q))
    return warnings


def cancel_source_orders(
    record: dict[str, Any],
    *,
    handoff_ts: str,
) -> dict[str, Any]:
    source_orders = record.get("source_orders") or {}
    orders = list(source_orders.get("orders") or [])
    cancelled = []
    for o in orders:
        cancelled.append(
            {
                **deepcopy(o),
                "handoff_action": "CANCELLED_ON_COBERTURA_HANDOFF",
                "cancelled_at": handoff_ts,
                "active_after_handoff": False,
            }
        )
    return {
        "cancel_on_cobertura_handoff": True,
        "source_orders_before": len(orders),
        "source_orders_after": 0,
        "active_source_order_count": 0,
        "cancelled_orders": cancelled,
        "active_order_book": [],
        "tem_order_ids_active": [],
        "notes": (
            "TEM source orders are retained for audit only and must not fill "
            "under Cobertura."
        ),
    }


def import_source_position(record: dict[str, Any]) -> CoberturaLedger:
    pos = record["pre_signal_position"]
    ledger = CoberturaLedger()
    ledger.seed_core(
        long_qty=float(pos["long_qty"]),
        long_avg=float(pos["long_avg"]),
        short_qty=float(pos["short_qty"]),
        short_avg=float(pos["short_avg"]),
    )
    return ledger


def apply_neutralization_fill(
    ledger: CoberturaLedger,
    *,
    fill_price: float,
    fee_rate: float,
    slippage_bps: float = 0.0,
) -> dict[str, Any]:
    """Match smaller side to larger using real SidePosition VWAP + fee_usdt."""
    if abs(float(slippage_bps)) > ABS_TOL:
        raise HandoffError("non-zero slippage_bps not supported in this handoff step")

    lq = float(ledger.core_long.qty)
    la = float(ledger.core_long.avg)
    sq = float(ledger.core_short.qty)
    sa = float(ledger.core_short.avg)
    expected = compute_neutralization(
        long_qty=lq,
        long_avg=la,
        short_qty=sq,
        short_avg=sa,
        fill_price=float(fill_price),
        taker_fee_rate=float(fee_rate),
    )
    if expected["neutralization_status"] != "NEEDS_SHORT_FILL":
        raise HandoffError(
            f"unexpected neutralization status: {expected['neutralization_status']}"
        )

    qty = float(expected["neutralization_short_qty"])
    if qty <= 0:
        raise HandoffError(f"neutralization qty must be positive, got {qty}")

    # Real Cobertura core-short VWAP path (not overlay add).
    ledger.core_short.open_add(qty, float(fill_price))
    fee = fee_usdt(fill_price=float(fill_price), qty=qty, fee_rate=float(fee_rate))
    ledger.cumulative_entry_fees += fee

    post_lq = float(ledger.core_long.qty)
    post_la = float(ledger.core_long.avg)
    post_sq = float(ledger.core_short.qty)
    post_sa = float(ledger.core_short.avg)
    net = post_lq - post_sq

    if not _close(post_sa, expected["new_short_avg"]):
        raise HandoffError(
            f"short avg mismatch: ledger={post_sa} expected={expected['new_short_avg']}"
        )
    if not _close(post_sq, expected["post_neutralization_short_qty"]):
        raise HandoffError("post short qty mismatch vs compute_neutralization")
    if abs(net) > QTY_TOL:
        raise HandoffError(f"post net qty not zero: {net}")
    if not _close(fee, expected["neutralization_open_fee"]):
        raise HandoffError(f"fee mismatch: {fee} vs {expected['neutralization_open_fee']}")

    return {
        "neutralization_side": "short",
        "neutralization_qty": qty,
        "neutralization_fill_price": float(fill_price),
        "neutralization_notional": qty * float(fill_price),
        "neutralization_fee": fee,
        "neutralization_fill_origin": "COBERTURA_HANDOFF",
        "neutralization_status": expected["neutralization_status"],
        "post_long_qty": post_lq,
        "post_long_avg": post_la,
        "post_short_qty": post_sq,
        "post_short_avg": post_sa,
        "post_net_qty": net,
        "compute_neutralization": expected,
    }


def assert_no_tem_cycle_inheritance(
    *,
    source_active_cycle: int | None,
    cobertura_active_cycle: Any,
    inherit_flag: bool,
) -> None:
    if inherit_flag:
        raise HandoffError("inherit_source_cycle_state=true is forbidden for this scenario")
    if cobertura_active_cycle not in (None, "", 0, "null"):
        if source_active_cycle is not None and cobertura_active_cycle == source_active_cycle:
            raise HandoffError(
                "TEM source cycle must not be inherited as Cobertura active cycle"
            )


def assert_no_regular_initial_entry(engine: CoberturaEngine) -> None:
    if engine.fills:
        raise HandoffError(
            "regular initial entry / overlay fills must not exist at handoff init"
        )
    if engine.order_events:
        raise HandoffError("order events must be empty at handoff seed")


def build_cobertura_config_after_neutralization(
    record: dict[str, Any],
    scenario: dict[str, Any],
    neut: dict[str, Any],
) -> CoberturaConfig:
    trig = record["trigger"]
    market = record["market"]
    fee_rate = float(market.get("taker_fee_rate") or 0.00055)
    cfg = CoberturaConfig(
        symbol=str(record.get("coin") or "APTUSDT"),
        timeframe="5m",
        start_timestamp=str(trig["signal_available_ts"]),
        start_price=float(market["neutralization_fill_price"]),
        start_price_source="config_start_price",
        core_long_qty=float(neut["post_long_qty"]),
        core_long_avg=float(neut["post_long_avg"]),
        core_short_qty=float(neut["post_short_qty"]),
        core_short_avg=float(neut["post_short_avg"]),
        fee_rate_open=fee_rate,
        fee_rate_close=fee_rate,
        slippage_bps_open=float(scenario.get("slippage_bps") or 0.0),
        slippage_bps_close=float(scenario.get("slippage_bps") or 0.0),
        candle_limit=1,
        tags={
            "source_strategy": "TEM",
            "source_trade_id": record["trade_id"],
            "source_active_cycle": (record.get("pre_signal_position") or {}).get(
                "active_cycle"
            ),
            "cobertura_active_cycle": None,
            "inherit_source_cycle_state": False,
            "fresh_initial_entry_required": False,
            "scenario_id": scenario.get("scenario_id"),
            "handoff": True,
        },
    )
    cfg.validate()
    return cfg


def _event(
    events: list[dict[str, Any]],
    *,
    timestamp: str,
    event_type: str,
    trade_id: str,
    scenario_id: str,
    details: dict[str, Any],
) -> None:
    events.append(
        {
            "timestamp": timestamp,
            "generated_at": _now_iso(),
            "event_type": event_type,
            "trade_id": trade_id,
            "scenario_id": scenario_id,
            "details": details,
        }
    )


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_meta() -> dict[str, Any]:
    def _run(args: list[str]) -> str | None:
        try:
            return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return None

    dirty = _run(["git", "status", "--porcelain"])
    return {
        "git_branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_commit": _run(["git", "rev-parse", "HEAD"]),
        "git_dirty": bool(dirty),
    }


def evaluate_invariants(
    *,
    record: dict[str, Any],
    scenario: dict[str, Any],
    cancel: dict[str, Any],
    before: dict[str, Any],
    neut: dict[str, Any],
    after: dict[str, Any],
    engine: CoberturaEngine,
    regular_initial_entry_created: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    trig = record["trigger"]
    market = record["market"]
    pos = record["pre_signal_position"]
    quality = record["quality"]
    eco = record.get("prior_economics") or {}

    check("trade_id", record.get("trade_id") == APT_TRADE_ID, record.get("trade_id"))
    check("trigger_mode", trig.get("trigger_mode") == TRIGGER_MODE)
    check("ready_for_cobertura", quality.get("ready_for_cobertura") is True)
    check("replay_match", quality.get("replay_match_status") == "REPLAY_MATCH")
    check(
        "replay_diff_count",
        quality.get("replay_diff_count") is not None
        and int(quality.get("replay_diff_count")) == 0,
    )
    check(
        "ledger_cutoff_violations",
        quality.get("ledger_cutoff_violations") is not None
        and int(quality.get("ledger_cutoff_violations")) == 0,
    )
    last_fill = parse_ts(pos.get("last_fill_timestamp_before_signal"))
    signal_ts = parse_ts(trig.get("signal_available_ts"))
    check(
        "last_fill_before_signal",
        last_fill is not None and signal_ts is not None and last_fill < signal_ts,
    )
    check(
        "structure_break_level",
        _close(_f(trig.get("structure_break_level")), APT_EXPECT["structure_break_level"]),
    )
    check(
        "market_price_at_signal",
        _close(_f(market.get("market_price_at_signal")), APT_EXPECT["market_price_at_signal"]),
    )
    check(
        "neutralization_fill_price",
        _close(
            _f(market.get("neutralization_fill_price")),
            APT_EXPECT["neutralization_fill_price"],
        ),
    )
    check("long_qty_before", _close(before["long_qty"], APT_EXPECT["long_qty"]))
    check("short_qty_before", _close(before["short_qty"], APT_EXPECT["short_qty"]))
    check(
        "net_qty_before",
        _close(before["net_qty"], before["long_qty"] - before["short_qty"]),
    )
    check("open_order_count", int(pos.get("open_order_count") or 0) == 4)
    check("source_orders_len", len((record.get("source_orders") or {}).get("orders") or []) == 4)
    check(
        "cancel_flag",
        (record.get("source_orders") or {}).get("cancel_on_cobertura_handoff") is True,
    )

    check("active_source_order_count", cancel.get("active_source_order_count") == 0)
    check("tem_order_ids_active_empty", cancel.get("tem_order_ids_active") == [])
    purposes_active = {
        o.get("purpose")
        for o in cancel.get("active_order_book") or []
        if o.get("active_after_handoff")
    }
    check("no_tem_purposes_active", purposes_active == set())

    check("neutralization_side", neut.get("neutralization_side") == "short")
    check(
        "neutralization_qty",
        _close(neut.get("neutralization_qty"), APT_EXPECT["neutralization_qty"]),
    )
    check(
        "neutralization_fill_price_applied",
        _close(neut.get("neutralization_fill_price"), APT_EXPECT["neutralization_fill_price"]),
    )
    check("post_long_qty", _close(after["long_qty"], APT_EXPECT["long_qty"]))
    check("post_short_qty", _close(after["short_qty"], APT_EXPECT["long_qty"]))
    check("post_net_qty", abs(float(after["net_qty"])) <= QTY_TOL)
    check("post_long_avg_unchanged", _close(after["long_avg"], APT_EXPECT["long_avg"]))
    check(
        "post_short_avg",
        _close(after["short_avg"], APT_EXPECT["post_short_avg"]),
    )
    check(
        "prior_realized_preserved",
        _close(_f(eco.get("realized_pnl")), APT_EXPECT["realized_pnl"]),
    )
    check(
        "prior_not_in_spread_target",
        scenario.get("include_prior_realized_pnl_in_recovery_target") is False,
    )
    check("no_regular_initial_entry", regular_initial_entry_created is False)
    check("engine_fills_empty", engine.fills == [])
    check(
        "cycle_not_inherited",
        engine.cfg.tags.get("cobertura_active_cycle") is None
        and engine.cfg.tags.get("inherit_source_cycle_state") is False
        and int(engine.cfg.tags.get("source_active_cycle") or 0)
        == int(pos.get("active_cycle") or 0),
    )

    decision = (
        "APT_COBERTURA_BUNDLE_HANDOFF_PASS"
        if not failures
        else "APT_COBERTURA_BUNDLE_HANDOFF_FAIL"
    )
    return {
        "decision": decision,
        "failures": failures,
        "checks": checks,
        "pass": not failures,
    }


def run_apt_bundle_handoff(
    *,
    bundle_path: Path,
    scenarios_path: Path,
    output_dir: Path,
    trade_id: str = APT_TRADE_ID,
    scenario_id: str = DEFAULT_SCENARIO_ID,
    trigger_mode: str = TRIGGER_MODE,
    cli_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle_path = Path(bundle_path)
    scenarios_path = Path(scenarios_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records = load_jsonl(bundle_path)
    scenarios = load_jsonl(scenarios_path)
    record = select_bundle_record(records, trade_id=trade_id, trigger_mode=trigger_mode)
    scenario = select_scenario(scenarios, scenario_id=scenario_id)

    events: list[dict[str, Any]] = []
    handoff_ts = str((record.get("trigger") or {}).get("signal_available_ts"))
    warnings = validate_bundle_record_for_handoff(record, scenario)

    _event(
        events,
        timestamp=handoff_ts,
        event_type="BUNDLE_RECORD_LOADED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={"trigger_mode": trigger_mode, "coin": record.get("coin")},
    )
    _event(
        events,
        timestamp=handoff_ts,
        event_type="BUNDLE_QUALITY_VALIDATED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={
            "ready_for_cobertura": True,
            "replay_match_status": (record.get("quality") or {}).get("replay_match_status"),
            "warnings": warnings,
        },
    )

    cancel = cancel_source_orders(record, handoff_ts=handoff_ts)
    _event(
        events,
        timestamp=handoff_ts,
        event_type="SOURCE_ORDERS_IDENTIFIED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={
            "count": cancel["source_orders_before"],
            "purposes": [o.get("purpose") for o in cancel["cancelled_orders"]],
        },
    )
    _event(
        events,
        timestamp=handoff_ts,
        event_type="SOURCE_ORDERS_CANCELLED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={
            "source_orders_after": 0,
            "active_source_order_count": 0,
        },
    )

    ledger = import_source_position(record)
    pos = record["pre_signal_position"]
    before = {
        "long_qty": float(ledger.core_long.qty),
        "long_avg": float(ledger.core_long.avg),
        "short_qty": float(ledger.core_short.qty),
        "short_avg": float(ledger.core_short.avg),
        "net_qty": float(ledger.net_qty()),
        "active_cycle_source": pos.get("active_cycle"),
        "last_fill_timestamp_before_signal": pos.get("last_fill_timestamp_before_signal"),
        "fills_before_signal": pos.get("fills_before_signal"),
        "fills_at_or_after_signal": pos.get("fills_at_or_after_signal"),
    }
    _event(
        events,
        timestamp=handoff_ts,
        event_type="SOURCE_POSITION_IMPORTED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details=before,
    )

    source_cycle = int(pos.get("active_cycle") or 0)
    assert_no_tem_cycle_inheritance(
        source_active_cycle=source_cycle,
        cobertura_active_cycle=None,
        inherit_flag=bool(scenario.get("inherit_source_cycle_state")),
    )
    _event(
        events,
        timestamp=handoff_ts,
        event_type="SOURCE_CYCLE_NOT_INHERITED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={
            "source_active_cycle": source_cycle,
            "cobertura_active_cycle": None,
            "inherit_source_cycle_state": False,
        },
    )
    _event(
        events,
        timestamp=handoff_ts,
        event_type="COBERTURA_HANDOFF_READY",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={
            "source_cutoff_rule": "fill_timestamp < signal_available_ts",
            "cobertura_start_timestamp": handoff_ts,
            "source_signal_bar_fills_imported": False,
            "fresh_initial_entry_required": False,
        },
    )

    market = record["market"]
    fill_px = float(market["neutralization_fill_price"])
    fee_rate = float(market.get("taker_fee_rate") or 0.00055)
    _event(
        events,
        timestamp=handoff_ts,
        event_type="NEUTRALIZATION_ORDER_CREATED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={
            "side": "short",
            "qty": float(before["long_qty"]) - float(before["short_qty"]),
            "fill_price_model": scenario.get("fill_price_model"),
            "planned_fill_price": fill_px,
        },
    )

    neut = apply_neutralization_fill(
        ledger,
        fill_price=fill_px,
        fee_rate=fee_rate,
        slippage_bps=float(scenario.get("slippage_bps") or 0.0),
    )
    _event(
        events,
        timestamp=handoff_ts,
        event_type="NEUTRALIZATION_FILL_APPLIED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={
            "neutralization_fill_origin": "COBERTURA_HANDOFF",
            "qty": neut["neutralization_qty"],
            "price": neut["neutralization_fill_price"],
            "fee": neut["neutralization_fee"],
        },
    )

    after = {
        "long_qty": float(ledger.core_long.qty),
        "long_avg": float(ledger.core_long.avg),
        "short_qty": float(ledger.core_short.qty),
        "short_avg": float(ledger.core_short.avg),
        "net_qty": float(ledger.net_qty()),
        "cumulative_entry_fees": float(ledger.cumulative_entry_fees),
    }
    _event(
        events,
        timestamp=handoff_ts,
        event_type="POSITION_NEUTRALIZED",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details=after,
    )

    cfg = build_cobertura_config_after_neutralization(record, scenario, neut)
    engine = CoberturaEngine(cfg)
    assert_no_regular_initial_entry(engine)
    regular_initial_entry_created = False

    eco = record.get("prior_economics") or {}
    prior_realized = float(eco.get("realized_pnl") or 0.0)
    spread_recovery_target = abs(float(eco.get("unrealized_pnl_at_signal") or 0.0))
    if scenario.get("include_prior_realized_pnl_in_recovery_target"):
        raise HandoffError("prior realized must not enter spread target in this step")
    if scenario.get("include_neutralization_fee_in_spread_target"):
        raise HandoffError("neutralization fee must not enter spread target in this step")

    economics_view = {
        "prior_realized_pnl": prior_realized,
        "prior_realized_deficit": abs(prior_realized),
        "spread_loss_before_neutralization": float(eco.get("unrealized_pnl_at_signal")),
        "spread_recovery_target": spread_recovery_target,
        "neutralization_fee": neut["neutralization_fee"],
        "include_prior_realized_pnl_in_recovery_target": False,
        "include_neutralization_fee_in_spread_target": False,
        "fee_quality": eco.get("fee_quality"),
        "cumulative_fees": eco.get("cumulative_fees"),
    }

    inv = evaluate_invariants(
        record=record,
        scenario=scenario,
        cancel=cancel,
        before=before,
        neut=neut,
        after=after,
        engine=engine,
        regular_initial_entry_created=regular_initial_entry_created,
    )
    if warnings and inv["pass"]:
        decision = "APT_COBERTURA_BUNDLE_HANDOFF_PASS_WITH_WARNINGS"
    else:
        decision = inv["decision"] if inv["pass"] else "APT_COBERTURA_BUNDLE_HANDOFF_FAIL"
    inv["decision"] = decision
    inv["warnings"] = warnings

    _event(
        events,
        timestamp=handoff_ts,
        event_type="HANDOFF_VALIDATION_COMPLETE",
        trade_id=trade_id,
        scenario_id=scenario_id,
        details={"decision": decision, "failures": inv["failures"], "warnings": warnings},
    )

    handoff_input = {
        "schema_version": SCHEMA_VERSION,
        "trade_id": trade_id,
        "trigger_mode": trigger_mode,
        "scenario_id": scenario_id,
        "record": record,
        "scenario": scenario,
        "causality": {
            "source_cutoff_rule": "fill_timestamp < signal_available_ts",
            "cobertura_start_timestamp": handoff_ts,
            "neutralization_fill_origin": "COBERTURA_HANDOFF",
            "source_signal_bar_fills_imported": False,
        },
    }
    state_before = {
        "trade_id": trade_id,
        "position": before,
        "trigger": record.get("trigger"),
        "market": record.get("market"),
        "prior_economics": eco,
        "source_strategy": "TEM",
        "source_active_cycle": source_cycle,
        "cobertura_active_cycle": None,
        "fresh_initial_entry_required": False,
        "initial_entry_confirmed": True,
        "initial_entry_submitted": True,
    }
    neut_out = {
        **neut,
        "trade_id": trade_id,
        "scenario_id": scenario_id,
        "timestamp": handoff_ts,
    }
    state_after = {
        "trade_id": trade_id,
        "position": after,
        "economics": economics_view,
        "source_active_cycle": source_cycle,
        "cobertura_active_cycle": None,
        "cobertura_cycle_inherited": False,
        "regular_initial_entry_created": False,
        "engine_seed_snapshot": {
            "core_long_qty": engine.ledger.core_long.qty,
            "core_long_avg": engine.ledger.core_long.avg,
            "core_short_qty": engine.ledger.core_short.qty,
            "core_short_avg": engine.ledger.core_short.avg,
            "fills_count": len(engine.fills),
            "order_events_count": len(engine.order_events),
            "state": engine.state,
        },
        "cobertura_config_tags": dict(cfg.tags),
    }

    config_snapshot = {
        "scenario": scenario,
        "cobertura_config": cfg.to_dict(),
        "research_adapter_notes": [
            "CoberturaEngine seeds qty-neutral core via seed_core; no initial entry order.",
            "Pre-neutralization unequal book is imported on CoberturaLedger only.",
            "Neutralization uses SidePosition.open_add + fee_usdt + compute_neutralization.",
            "TEM cycle is provenance only (tags.source_active_cycle).",
            "No candle recovery loop executed in this handoff step.",
        ],
    }

    summary_row = {
        "trade_id": trade_id,
        "scenario_id": scenario_id,
        "source_orders_before": cancel["source_orders_before"],
        "source_orders_after": cancel["source_orders_after"],
        "source_cycle": source_cycle,
        "cobertura_cycle_inherited": False,
        "long_qty_before": before["long_qty"],
        "short_qty_before": before["short_qty"],
        "neutralization_side": neut["neutralization_side"],
        "neutralization_qty": neut["neutralization_qty"],
        "neutralization_fill_price": neut["neutralization_fill_price"],
        "neutralization_notional": neut["neutralization_notional"],
        "neutralization_fee": neut["neutralization_fee"],
        "long_qty_after": after["long_qty"],
        "short_qty_after": after["short_qty"],
        "long_avg_after": after["long_avg"],
        "short_avg_after": after["short_avg"],
        "net_qty_after": after["net_qty"],
        "prior_realized_pnl": prior_realized,
        "include_prior_realized_pnl_in_recovery_target": False,
        "decision": decision,
        "warnings": "|".join(warnings),
    }

    rel_bundle = _rel_repo(bundle_path)
    rel_scen = _rel_repo(scenarios_path)
    manifest = {
        "created_at": _now_iso(),
        "schema_version": SCHEMA_VERSION,
        "cli_args": cli_args or {},
        **_git_meta(),
        "sources": [
            {
                "role": "blocker_historical_states",
                "path": rel_bundle,
                "sha256": _sha256(bundle_path),
                "size_bytes": bundle_path.stat().st_size if bundle_path.is_file() else None,
            },
            {
                "role": "cobertura_start_scenarios",
                "path": rel_scen,
                "sha256": _sha256(scenarios_path),
                "size_bytes": scenarios_path.stat().st_size if scenarios_path.is_file() else None,
            },
        ],
    }

    report = _build_report(
        decision=decision,
        record=record,
        scenario=scenario,
        cancel=cancel,
        before=before,
        neut=neut,
        after=after,
        economics=economics_view,
        warnings=warnings,
        inv=inv,
    )

    atomic_write_json(output_dir / "handoff_input.json", handoff_input)
    atomic_write_json(output_dir / "handoff_state_before_neutralization.json", state_before)
    atomic_write_json(output_dir / "source_order_cancellation.json", cancel)
    atomic_write_json(output_dir / "neutralization_fill.json", neut_out)
    atomic_write_json(output_dir / "handoff_state_after_neutralization.json", state_after)
    atomic_write_json(output_dir / "handoff_invariants.json", inv)
    atomic_write_json(output_dir / "config_snapshot.json", config_snapshot)
    atomic_write_json(output_dir / "source_manifest.json", manifest)
    write_csv(output_dir / "handoff_summary.csv", [summary_row])
    atomic_write_text(
        output_dir / "event_timeline.jsonl",
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events),
    )
    atomic_write_text(output_dir / "REPORT.md", report)

    return {
        "decision": decision,
        "output_dir": str(output_dir),
        "warnings": warnings,
        "summary": summary_row,
        "invariants": inv,
        "events": events,
    }


def _rel_repo(path: Path) -> str:
    try:
        root = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"], text=True
            ).strip()
        )
        return str(path.resolve().relative_to(root))
    except Exception:
        return str(path)


def _build_report(
    *,
    decision: str,
    record: dict[str, Any],
    scenario: dict[str, Any],
    cancel: dict[str, Any],
    before: dict[str, Any],
    neut: dict[str, Any],
    after: dict[str, Any],
    economics: dict[str, Any],
    warnings: list[str],
    inv: dict[str, Any],
) -> str:
    trig = record.get("trigger") or {}
    market = record.get("market") or {}
    lines = [
        "# APT Cobertura Bundle Handoff",
        "",
        f"**Decision: `{decision}`**",
        "",
        "## Answers",
        "",
        "1. Exactly one APT record loaded: **yes**",
        f"2. Bundle start-ready: **{(record.get('quality') or {}).get('ready_for_cobertura')}**",
        f"3. Break/signal/market: level=`{trig.get('structure_break_level')}`, "
        f"signal=`{trig.get('signal_available_ts')}`, "
        f"market=`{market.get('market_price_at_signal')}`",
        f"4. Exact book imported: long=`{before['long_qty']}` @ `{before['long_avg']}`; "
        f"short=`{before['short_qty']}` @ `{before['short_avg']}`",
        "5. Regular initial entry created: **no**",
        f"6. TEM source orders removed: before=`{cancel['source_orders_before']}` "
        f"after=`{cancel['source_orders_after']}`",
        "7. TEM cycle inherited as Cobertura cycle: **no** "
        f"(source_cycle=`{before.get('active_cycle_source')}`)",
        f"8. Neutralization qty: `{neut['neutralization_qty']}` ({neut['neutralization_side']})",
        f"9. Fill price: `{neut['neutralization_fill_price']}`",
        f"10. New short avg: `{after['short_avg']}`",
        f"11. Qty-neutral after: long=`{after['long_qty']}` short=`{after['short_qty']}` "
        f"net=`{after['net_qty']}`",
        f"12. Prior realized only separate: `{economics['prior_realized_pnl']}` "
        f"(include_in_spread_target=`{economics['include_prior_realized_pnl_in_recovery_target']}`)",
        f"13. Warnings: `{warnings}`",
        "14. Tests: see pytest `test_cobertura_bundle_handoff.py` + full Cobertura suite.",
        "15. Suitable for isolated Cobertura replay after handoff: **yes** "
        "(qty-neutral seeded engine; no recovery run in this step).",
        "",
        "## Scenario",
        "",
        f"- scenario_id: `{scenario.get('scenario_id')}`",
        f"- neutralization_mode: `{scenario.get('neutralization_mode')}`",
        "",
        "## Invariants",
        "",
        f"- pass: `{inv.get('pass')}`",
        f"- failures: `{inv.get('failures')}`",
        "",
        "## Decision",
        "",
        f"`{decision}`",
        "",
    ]
    return "\n".join(lines) + "\n"
