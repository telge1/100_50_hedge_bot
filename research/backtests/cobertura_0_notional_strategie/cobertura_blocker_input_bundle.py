"""Build versioned Cobertura input bundles from historical blocker facts.

Historical facts come from two result dirs (joined on trade_id + trigger_mode):
- historical_blocker_states_*: break + market
- historical_blocker_fill_replay_*: exact pre-signal book + open orders

Strategy assumptions live only in cobertura_start_scenarios.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from research.backtests.multicoin_price_staging_grid import atomic_write_json, atomic_write_text, write_csv
from research.regime_scanner.tem_structure_break.eval_common import csv_dicts

from .historical_blocker_state_extraction import APT_REFERENCE_TRADE_ID, parse_ts

__all__ = [
    "APT_REFERENCE_TRADE_ID",
    "SCHEMA_VERSION",
    "build_bundle",
    "apt_reference_values",
]

SCHEMA_VERSION = "1.0"
QTY_TOL = 1e-6
PCT_TOL = 1e-12

BASELINE_SCENARIO = {
    "schema_version": SCHEMA_VERSION,
    "scenario_id": "full_qty_neutralization_spread_only_v1",
    "neutralization_mode": "MATCH_SMALLER_SIDE_TO_LARGER_SIDE",
    "fill_price_model": "SIGNAL_TRADEABLE_5M_OPEN",
    "slippage_bps": 0.0,
    "fee_model": "TAKER_FEE_FROM_HISTORICAL_INPUT",
    "include_prior_realized_pnl_in_recovery_target": False,
    "include_neutralization_fee_in_spread_target": False,
    "cancel_source_strategy_orders": True,
    "inherit_source_cycle_state": False,
}

SCHEMA_DOC = {
    "schema_version": SCHEMA_VERSION,
    "description": (
        "Historical TEM blocker start facts for Cobertura research backtests. "
        "Neutralization quantities are NOT historical facts; they belong to scenarios."
    ),
    "join_key": ["trade_id", "trigger_mode"],
    "timestamp_format": "ISO-8601 with timezone offset (e.g. 2026-01-19T00:00:00+00:00 or space variant)",
    "percentage_semantics": {
        "distance_break_to_market_pct": (
            "Decimal fraction: market_price_at_signal / structure_break_level - 1. "
            "Example APT ≈ -0.02358 ≡ -2.358%."
        )
    },
    "enums": {
        "trigger_mode": ["first_break", "final_invalidation"],
        "structure_break_direction": ["bearish", "bullish", "unknown"],
        "replay_match_status": ["REPLAY_MATCH", "REPLAY_MISMATCH", "SKIPPED"],
        "fee_quality": [
            "FEES_COMPLETE",
            "FEE_RECONSTRUCTION_UNRESOLVED",
            "FEES_UNKNOWN",
        ],
        "source_quality": [
            "EXACT_FILL_LEVEL_BEFORE_SIGNAL",
            "BREAK_EVENT_UNRESOLVED",
            "STATE_UNRESOLVED",
            "NO_FILLS_BEFORE_SIGNAL",
        ],
    },
    "required_fields_ready": [
        "trade_id",
        "coin",
        "trigger.trigger_mode",
        "trigger.signal_available_ts",
        "trigger.structure_break_level",
        "trigger.structure_break_kind",
        "market.market_price_at_signal",
        "market.neutralization_fill_price",
        "pre_signal_position.long_qty",
        "pre_signal_position.short_qty",
        "quality.replay_match_status",
        "quality.ready_for_cobertura",
    ],
    "optional_fields": [
        "prior_economics.cumulative_fees",
        "trigger.confirmation_ts",
        "source_orders.orders",
    ],
}


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


def _i(v: Any, default: int | None = None) -> int | None:
    x = _f(v, None)
    if x is None:
        return default
    return int(x)


def _b(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _ts_norm(v: Any) -> str | None:
    t = parse_ts(v)
    if t is None:
        return None
    return t.isoformat()


def _ts_lt(a: Any, b: Any) -> bool | None:
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None or tb is None:
        return None
    return ta < tb


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_trade_id(trade_id: str) -> dict[str, str]:
    parts = str(trade_id).split("|")
    return {
        "coin": parts[0] if parts else "",
        "profile": parts[1] if len(parts) > 1 else "",
        "run_mode": parts[2] if len(parts) > 2 else "",
        "trade_seq": parts[3] if len(parts) > 3 else "",
    }


def infer_break_direction(kind: str | None) -> str:
    k = str(kind or "").lower()
    if "low" in k or "bearish" in k or "down" in k:
        return "bearish"
    if "high" in k or "bullish" in k or "up" in k:
        return "bullish"
    return "unknown"


def index_by_join(
    rows: Iterable[dict[str, str]],
    *,
    default_trigger: str | None = None,
) -> tuple[dict[tuple[str, str], dict[str, str]], list[dict[str, Any]]]:
    """Index rows by (trade_id, trigger_mode). Duplicates → unresolved entries."""
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        tid = r.get("trade_id") or ""
        mode = r.get("trigger_mode") or default_trigger or ""
        if not tid or not mode:
            continue
        buckets[(tid, mode)].append(r)
    out: dict[tuple[str, str], dict[str, str]] = {}
    dups: list[dict[str, Any]] = []
    for key, items in buckets.items():
        if len(items) > 1:
            dups.append(
                {
                    "trade_id": key[0],
                    "coin": items[0].get("coin"),
                    "trigger_mode": key[1],
                    "reasons": ["DUPLICATE_JOIN_KEY"],
                    "available_sources": [],
                    "missing_fields": [],
                    "quality_flags": f"n={len(items)}",
                }
            )
        else:
            out[key] = items[0]
    return out, dups


def distance_break_to_market_pct(market: float, level: float) -> float:
    return float(market) / float(level) - 1.0


def load_open_orders(
    path: Path, *, trade_id: str
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = csv_dicts(path)
    out = []
    for r in rows:
        if r.get("trade_id") != trade_id:
            continue
        if not r.get("order_id"):
            continue
        out.append(
            {
                "order_id": r.get("order_id"),
                "side": r.get("side"),
                "purpose": r.get("purpose"),
                "qty": _f(r.get("qty")),
                "price": _f(r.get("price")),
                "trigger_price": _f(r.get("trigger_price")),
                "status": r.get("status"),
                "last_event_type": r.get("last_event_type"),
                "last_event_timestamp": _ts_norm(r.get("last_event_timestamp")),
            }
        )
    # deterministic order
    out.sort(key=lambda o: (str(o.get("purpose") or ""), str(o.get("order_id") or "")))
    return out


def count_ledger_before(
    ledger_rows: list[dict[str, str]], *, trade_id: str, signal_ts: str
) -> tuple[int, int, int]:
    before = after = bad = 0
    for r in ledger_rows:
        if r.get("trade_id") != trade_id:
            continue
        ts = r.get("fill_timestamp")
        is_before = _b(r.get("before_signal"))
        lt = _ts_lt(ts, signal_ts)
        if is_before:
            before += 1
            if lt is False:
                bad += 1
        else:
            after += 1
    return before, after, bad


def evaluate_ready(record: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    """Return (ready, hard_reasons, warnings)."""
    reasons: list[str] = []
    warnings: list[str] = []
    tr = record["trigger"]
    mk = record["market"]
    pos = record["pre_signal_position"]
    q = record["quality"]
    eco = record["prior_economics"]

    if not record.get("trade_id"):
        reasons.append("MISSING_TRADE_ID")
    if not tr.get("trigger_mode"):
        reasons.append("MISSING_TRIGGER_MODE")
    if not tr.get("signal_available_ts"):
        reasons.append("MISSING_SIGNAL_AVAILABLE_TS")
    level = _f(tr.get("structure_break_level"))
    if level is None or level <= 0:
        reasons.append("MISSING_OR_NONPOSITIVE_BREAK_LEVEL")
    if not tr.get("structure_break_kind"):
        reasons.append("MISSING_BREAK_KIND")
    mpx = _f(mk.get("market_price_at_signal"))
    if mpx is None or mpx <= 0:
        reasons.append("MISSING_OR_NONPOSITIVE_MARKET_PRICE")
    nfp = _f(mk.get("neutralization_fill_price"))
    if nfp is None or nfp <= 0:
        reasons.append("MISSING_OR_NONPOSITIVE_NEUTRALIZATION_FILL")

    lq = _f(pos.get("long_qty"))
    sq = _f(pos.get("short_qty"))
    la = _f(pos.get("long_avg"))
    sa = _f(pos.get("short_avg"))
    if lq is None or lq < 0:
        reasons.append("INVALID_LONG_QTY")
    if sq is None or sq < 0:
        reasons.append("INVALID_SHORT_QTY")
    if lq is not None and lq > 0 and (la is None or la <= 0):
        reasons.append("INVALID_LONG_AVG")
    if sq is not None and sq > 0 and (sa is None or sa <= 0):
        reasons.append("INVALID_SHORT_AVG")

    last_fill = pos.get("last_fill_timestamp_before_signal")
    sig = tr.get("signal_available_ts")
    if last_fill and sig:
        if _ts_lt(last_fill, sig) is not True:
            reasons.append("LAST_FILL_NOT_STRICTLY_BEFORE_SIGNAL")
    elif lq and lq > 0:
        reasons.append("MISSING_LAST_FILL_BEFORE_SIGNAL")

    if q.get("replay_match_status") != "REPLAY_MATCH":
        reasons.append("REPLAY_NOT_MATCH")
    if int(q.get("replay_diff_count") or 0) != 0:
        reasons.append("REPLAY_DIFF_NONEZERO")
    if not _b(q.get("ready_for_neutralization_source")):
        reasons.append("NOT_READY_FOR_NEUTRALIZATION")

    if eco.get("fee_quality") == "FEE_RECONSTRUCTION_UNRESOLVED":
        warnings.append("FEE_RECONSTRUCTION_UNRESOLVED")

    # invariant: signal after break event
    evt = tr.get("structure_break_event_ts")
    if evt and sig and _ts_lt(evt, sig) is False and evt != sig:
        # allow equal? typically event < signal; equal would be weird
        if _ts_lt(sig, evt) is True:
            reasons.append("SIGNAL_BEFORE_BREAK_EVENT")

    return (len(reasons) == 0), reasons, warnings


def build_historical_record(
    *,
    trade_id: str,
    trigger_mode: str,
    break_row: dict[str, str] | None,
    market_row: dict[str, str] | None,
    pre_row: dict[str, str] | None,
    open_orders: list[dict[str, Any]],
    ledger_before: int | None,
    ledger_after: int | None,
    ledger_bad: int,
    taker_fee_rate: float,
    provenance_paths: dict[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (ready_record_or_None, unresolved_or_None)."""
    meta = parse_trade_id(trade_id)
    coin = (
        (break_row or {}).get("coin")
        or (market_row or {}).get("coin")
        or (pre_row or {}).get("coin")
        or meta["coin"]
    )
    missing: list[str] = []
    available: list[str] = []
    if break_row:
        available.append("break_events")
    else:
        missing.append("break_events")
    if market_row:
        available.append("market_prices")
    else:
        missing.append("market_prices")
    if pre_row:
        available.append("fill_replay_pre_signal")
    else:
        missing.append("fill_replay_pre_signal")

    if not break_row or not market_row or not pre_row:
        return None, {
            "trade_id": trade_id,
            "coin": coin,
            "trigger_mode": trigger_mode,
            "reasons": ["MISSING_SOURCE_RECORD"] + [f"missing:{m}" for m in missing],
            "available_sources": available,
            "missing_fields": missing,
            "quality_flags": "|".join(missing),
        }

    level = _f(break_row.get("structure_break_level"))
    market_px = _f(
        pre_row.get("market_price_at_signal")
        or market_row.get("neutralization_fill_price")
        or market_row.get("tradeable_5m_open")
    )
    fill_px = _f(
        pre_row.get("neutralization_fill_price")
        or market_row.get("neutralization_fill_price")
    )
    tradeable_open = _f(market_row.get("tradeable_5m_open") or pre_row.get("tradeable_5m_open"))
    dist = None
    if market_px is not None and level is not None and level > 0:
        dist = distance_break_to_market_pct(market_px, level)
        # cross-check stored structure_level_to_fill_pct when present
        stored = _f(market_row.get("structure_level_to_fill_pct"))
        if stored is not None and abs(stored - dist) > 1e-9:
            # keep recomputed; flag later in warnings via quality
            pass

    flags = str(pre_row.get("state_quality_flags") or "")
    fee_quality = (
        "FEE_RECONSTRUCTION_UNRESOLVED"
        if "FEE_RECONSTRUCTION_UNRESOLVED" in flags
        else (
            "FEES_COMPLETE"
            if pre_row.get("cumulative_fees_before") not in (None, "")
            else "FEES_UNKNOWN"
        )
    )

    lq = _f(pre_row.get("long_qty_before"), 0.0) or 0.0
    sq = _f(pre_row.get("short_qty_before"), 0.0) or 0.0
    net = _f(pre_row.get("net_long_qty_before"))
    if net is None:
        net = lq - sq

    fills_before = _i(pre_row.get("fills_before_signal"), 0) or 0
    fills_after = _i(pre_row.get("fills_at_or_after_signal"), 0) or 0
    if ledger_before is not None and ledger_before != fills_before:
        # hard integrity issue handled in caller checks
        pass

    order_count = _i(pre_row.get("active_order_count_at_signal"), 0) or 0
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trade_id": trade_id,
        "coin": coin,
        "profile": meta["profile"],
        "run_mode": meta["run_mode"],
        "trigger": {
            "trigger_mode": trigger_mode,
            "structure_break_event_ts": _ts_norm(break_row.get("trigger_event_timestamp")),
            "signal_available_ts": _ts_norm(
                break_row.get("signal_available_ts") or pre_row.get("signal_available_ts")
            ),
            "structure_break_level": level,
            "structure_break_kind": break_row.get("structure_break_kind"),
            "structure_break_timeframe": break_row.get("structure_break_timeframe") or "4h",
            "structure_break_direction": infer_break_direction(
                break_row.get("structure_break_kind")
            ),
            "break_cycle_id": _i(break_row.get("break_cycle_id")),
            "confirmation_ts": _ts_norm(break_row.get("confirmation_ts")) or None,
        },
        "market": {
            "tradeable_timestamp": _ts_norm(
                market_row.get("tradeable_5m_timestamp")
                or pre_row.get("tradeable_5m_timestamp")
            ),
            "market_price_at_signal": market_px,
            "tradeable_5m_open": tradeable_open,
            "neutralization_fill_price": fill_px,
            "distance_break_to_market_pct": dist,
            "taker_fee_rate": taker_fee_rate,
            "slippage_bps": _f(market_row.get("slippage_bps"), 0.0),
        },
        "pre_signal_position": {
            "last_fill_timestamp_before_signal": _ts_norm(
                pre_row.get("last_fill_timestamp_before_signal")
            ),
            "fills_before_signal": fills_before,
            "fills_at_or_after_signal": fills_after,
            "long_qty": lq,
            "long_avg": _f(pre_row.get("long_avg_before")),
            "short_qty": sq,
            "short_avg": _f(pre_row.get("short_avg_before")),
            "net_qty": net,
            "active_cycle": _i(pre_row.get("active_cycle_at_signal")),
            "open_order_count": order_count,
        },
        "prior_economics": {
            "realized_pnl": _f(pre_row.get("realized_pnl_before")),
            "unrealized_pnl_at_signal": _f(pre_row.get("unrealized_pnl_at_signal_price")),
            "total_economics_at_signal": _f(pre_row.get("total_economics_before")),
            "cumulative_fees": _f(pre_row.get("cumulative_fees_before")),
            "fee_quality": fee_quality,
        },
        "source_orders": {
            "cancel_on_cobertura_handoff": True,
            "orders": open_orders,
        },
        "quality": {
            "source_quality": pre_row.get("source_quality"),
            "replay_match_status": pre_row.get("replay_match_status"),
            "replay_diff_count": _i(pre_row.get("replay_diff_count"), 0) or 0,
            "ready_for_neutralization_source": _b(pre_row.get("ready_for_neutralization")),
            "ready_for_cobertura": False,  # set after evaluate_ready
            "warnings": [],
            "ledger_before_signal_count": ledger_before,
            "ledger_at_or_after_signal_count": ledger_after,
            "ledger_cutoff_violations": ledger_bad,
        },
        "provenance": {
            "break_source": provenance_paths["break"],
            "market_source": provenance_paths["market"],
            "position_source": provenance_paths["pre_signal"],
            "fill_ledger_source": provenance_paths["ledger"],
            "open_orders_source": provenance_paths["orders"],
        },
    }

    ready, reasons, warnings = evaluate_ready(record)
    # Extra integrity
    if abs(net - (lq - sq)) > QTY_TOL:
        ready = False
        reasons.append("NET_QTY_MISMATCH")
    if order_count != len(open_orders):
        ready = False
        reasons.append("OPEN_ORDER_COUNT_MISMATCH")
    if ledger_before is not None and ledger_before != fills_before:
        ready = False
        reasons.append("FILLS_BEFORE_LEDGER_MISMATCH")
    if ledger_bad:
        ready = False
        reasons.append("LOOKAHEAD_FILL_IN_PRESIGNAL_LEDGER")
    if dist is not None:
        stored = _f(market_row.get("structure_level_to_fill_pct"))
        if stored is not None and abs(stored - dist) > 1e-9:
            warnings.append("DISTANCE_PCT_RECOMPUTED_DIFFERS_FROM_SOURCE")

    record["quality"]["warnings"] = warnings
    record["quality"]["ready_for_cobertura"] = ready

    if not ready:
        return None, {
            "trade_id": trade_id,
            "coin": coin,
            "trigger_mode": trigger_mode,
            "reasons": reasons,
            "available_sources": available,
            "missing_fields": [],
            "quality_flags": "|".join(reasons + warnings),
        }
    return record, None


def flatten_record(rec: dict[str, Any]) -> dict[str, Any]:
    tr, mk, pos, eco, q = (
        rec["trigger"],
        rec["market"],
        rec["pre_signal_position"],
        rec["prior_economics"],
        rec["quality"],
    )
    return {
        "trade_id": rec["trade_id"],
        "coin": rec["coin"],
        "trigger_mode": tr["trigger_mode"],
        "structure_break_event_ts": tr["structure_break_event_ts"],
        "signal_available_ts": tr["signal_available_ts"],
        "structure_break_level": tr["structure_break_level"],
        "structure_break_kind": tr["structure_break_kind"],
        "structure_break_timeframe": tr["structure_break_timeframe"],
        "market_price_at_signal": mk["market_price_at_signal"],
        "neutralization_fill_price": mk["neutralization_fill_price"],
        "distance_break_to_market_pct": mk["distance_break_to_market_pct"],
        "last_fill_timestamp_before_signal": pos["last_fill_timestamp_before_signal"],
        "fills_before_signal": pos["fills_before_signal"],
        "fills_at_or_after_signal": pos["fills_at_or_after_signal"],
        "long_qty": pos["long_qty"],
        "long_avg": pos["long_avg"],
        "short_qty": pos["short_qty"],
        "short_avg": pos["short_avg"],
        "net_qty": pos["net_qty"],
        "realized_pnl": eco["realized_pnl"],
        "unrealized_pnl_at_signal": eco["unrealized_pnl_at_signal"],
        "total_economics_at_signal": eco["total_economics_at_signal"],
        "active_cycle": pos["active_cycle"],
        "open_order_count": pos["open_order_count"],
        "replay_match_status": q["replay_match_status"],
        "replay_diff_count": q["replay_diff_count"],
        "ready_for_cobertura": q["ready_for_cobertura"],
        "warnings": "|".join(q.get("warnings") or []),
    }


def git_meta(repo: Path) -> dict[str, Any]:
    def run(args: list[str]) -> str:
        try:
            return subprocess.check_output(args, cwd=str(repo), text=True).strip()
        except Exception:
            return ""

    return {
        "branch": run(["git", "branch", "--show-current"]),
        "commit": run(["git", "rev-parse", "HEAD"]),
        "dirty": bool(run(["git", "status", "--porcelain"])),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in rows]
    atomic_write_text(path, ("\n".join(lines) + ("\n" if lines else "")))


def build_bundle(
    *,
    state_dir: Path,
    fill_replay_dir: Path,
    output_dir: Path,
    trigger_mode: str = "first_break",
    taker_fee_rate: float = 0.00055,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir)
    fill_replay_dir = Path(fill_replay_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = repo_root or Path(__file__).resolve().parents[3]

    break_path = state_dir / "blocker_break_events.csv"
    market_path = state_dir / "blocker_market_prices.csv"
    hist_path = state_dir / "historical_blocker_states.csv"
    pre_path = fill_replay_dir / "blocker_pre_signal_states.csv"
    ledger_path = fill_replay_dir / "blocker_fill_ledger.csv"
    orders_path = fill_replay_dir / "blocker_open_orders_at_signal.csv"

    breaks, break_dups = index_by_join(csv_dicts(break_path))
    markets, market_dups = index_by_join(csv_dicts(market_path))
    # fill replay lacks trigger_mode → attach default
    pre_raw = csv_dicts(pre_path)
    for r in pre_raw:
        r.setdefault("trigger_mode", trigger_mode)
    pres, pre_dups = index_by_join(pre_raw, default_trigger=trigger_mode)
    hist_rows = csv_dicts(hist_path)
    for r in hist_rows:
        r.setdefault("trigger_mode", trigger_mode)
    hist_idx, hist_dups = index_by_join(hist_rows)

    ledger_rows = csv_dicts(ledger_path) if ledger_path.exists() else []

    # Universe: union of trade_ids seen in hist or pre or break for this trigger
    keys = sorted(
        {
            k
            for k in set(breaks) | set(markets) | set(pres) | set(hist_idx)
            if k[1] == trigger_mode
        },
        key=lambda x: (x[0], x[1]),
    )

    unresolved: list[dict[str, Any]] = []
    unresolved.extend(break_dups)
    unresolved.extend(market_dups)
    unresolved.extend(pre_dups)
    unresolved.extend(hist_dups)

    # Prevent mixing: reject any key that exists under another trigger_mode in sources
    other_modes = {
        k
        for k in set(breaks) | set(markets) | set(hist_idx)
        if k[1] != trigger_mode
    }
    # (informational only for first_break-only datasets)

    ready_records: list[dict[str, Any]] = []
    provenance = {
        "break": str(break_path.as_posix()),
        "market": str(market_path.as_posix()),
        "pre_signal": str(pre_path.as_posix()),
        "ledger": str(ledger_path.as_posix()),
        "orders": str(orders_path.as_posix()),
    }

    invariant_failures = 0
    warning_count = 0
    missing_break = missing_market = missing_fill = 0
    replay_mismatch = 0

    for trade_id, mode in keys:
        if mode != trigger_mode:
            continue
        br = breaks.get((trade_id, mode))
        mk = markets.get((trade_id, mode))
        pr = pres.get((trade_id, mode))
        if br is None:
            missing_break += 1
        if mk is None:
            missing_market += 1
        if pr is None:
            missing_fill += 1

        sig = (
            (br or {}).get("signal_available_ts")
            or (pr or {}).get("signal_available_ts")
            or ""
        )
        lb, la, bad = (None, None, 0)
        if sig and ledger_rows:
            lb, la, bad = count_ledger_before(
                ledger_rows, trade_id=trade_id, signal_ts=sig
            )

        orders = load_open_orders(orders_path, trade_id=trade_id)
        rec, unr = build_historical_record(
            trade_id=trade_id,
            trigger_mode=mode,
            break_row=br,
            market_row=mk,
            pre_row=pr,
            open_orders=orders,
            ledger_before=lb,
            ledger_after=la,
            ledger_bad=bad,
            taker_fee_rate=taker_fee_rate,
            provenance_paths=provenance,
        )
        if unr:
            unresolved.append(unr)
            if "REPLAY_NOT_MATCH" in unr.get("reasons", []):
                replay_mismatch += 1
            if any("MISMATCH" in r or "LOOKAHEAD" in r for r in unr.get("reasons", [])):
                invariant_failures += 1
            continue
        assert rec is not None
        warning_count += len(rec["quality"].get("warnings") or [])
        ready_records.append(rec)

    ready_records.sort(key=lambda r: (r["trade_id"], r["trigger"]["trigger_mode"]))
    unresolved.sort(key=lambda r: (r.get("trade_id") or "", r.get("trigger_mode") or ""))

    # Deduplicate unresolved by trade_id+mode+reasons
    seen_u: set[tuple[str, str, str]] = set()
    unr_dedup: list[dict[str, Any]] = []
    for u in unresolved:
        key = (u.get("trade_id") or "", u.get("trigger_mode") or "", "|".join(u.get("reasons") or []))
        if key in seen_u:
            continue
        seen_u.add(key)
        unr_dedup.append(u)
    unresolved = unr_dedup

    scenarios = [dict(BASELINE_SCENARIO)]

    # APT status
    apt = next((r for r in ready_records if r["trade_id"] == APT_REFERENCE_TRADE_ID), None)
    apt_status = "APT_BUNDLE_PASS" if apt else "APT_BUNDLE_FAIL"
    if apt:
        # regression checks
        checks = [
            abs(float(apt["trigger"]["structure_break_level"]) - 1.7639) < 1e-9,
            abs(float(apt["market"]["market_price_at_signal"]) - 1.7223) < 1e-9,
            abs(float(apt["pre_signal_position"]["long_qty"]) - 296.365) < 1e-6,
            abs(float(apt["pre_signal_position"]["short_qty"]) - 197.59699999999998) < 1e-6,
            apt["quality"]["replay_match_status"] == "REPLAY_MATCH",
            "FEE_RECONSTRUCTION_UNRESOLVED" in (apt["quality"].get("warnings") or []),
            apt["quality"]["ready_for_cobertura"] is True,
        ]
        if not all(checks):
            apt_status = "APT_BUNDLE_FAIL"
        elif apt["quality"].get("warnings"):
            apt_status = "APT_BUNDLE_PASS_WITH_WARNINGS"

    decision = "COBERTURA_BLOCKER_INPUT_BUNDLE_PASS"
    if invariant_failures or apt_status == "APT_BUNDLE_FAIL" or break_dups or market_dups or pre_dups:
        decision = "COBERTURA_BLOCKER_INPUT_BUNDLE_FAIL"
    elif warning_count or unresolved or apt_status.endswith("WARNINGS"):
        decision = "COBERTURA_BLOCKER_INPUT_BUNDLE_PASS_WITH_WARNINGS"

    # Write outputs
    write_jsonl(output_dir / "blocker_historical_states.jsonl", ready_records)
    write_csv(
        output_dir / "blocker_historical_states.csv",
        [flatten_record(r) for r in ready_records],
    )
    write_jsonl(output_dir / "cobertura_start_scenarios.jsonl", scenarios)
    write_jsonl(output_dir / "unresolved_blockers.jsonl", unresolved)
    atomic_write_json(output_dir / "schema.json", SCHEMA_DOC)

    source_files = [
        break_path,
        market_path,
        hist_path,
        pre_path,
        ledger_path,
        orders_path,
        fill_replay_dir / "replay_comparison.csv",
        fill_replay_dir / "fee_reconstruction_issues.csv",
        fill_replay_dir / "unresolved_replays.csv",
    ]
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git": git_meta(repo_root),
        "cli": {
            "state_dir": str(state_dir.as_posix()),
            "fill_replay_dir": str(fill_replay_dir.as_posix()),
            "output_dir": str(output_dir.as_posix()),
            "trigger_mode": trigger_mode,
            "taker_fee_rate": taker_fee_rate,
        },
        "sources": [
            {
                "path": str(p.as_posix()),
                "exists": p.exists(),
                "size_bytes": p.stat().st_size if p.exists() else None,
                "sha256": sha256_file(p),
            }
            for p in source_files
        ],
        "other_trigger_modes_present": sorted({k[1] for k in other_modes}),
    }
    atomic_write_json(output_dir / "source_manifest.json", manifest)

    integrity = {
        "decision": decision,
        "schema_version": SCHEMA_VERSION,
        "source_blocker_count": len(keys),
        "ready_blocker_count": len(ready_records),
        "unresolved_blocker_count": len(unresolved),
        "duplicate_join_keys": len(break_dups) + len(market_dups) + len(pre_dups) + len(hist_dups),
        "missing_break_records": missing_break,
        "missing_market_records": missing_market,
        "missing_fill_replay_records": missing_fill,
        "replay_mismatch_count": replay_mismatch,
        "invariant_failure_count": invariant_failures,
        "warning_count": warning_count,
        "apt_status": apt_status,
        "trigger_mode": trigger_mode,
    }
    atomic_write_json(output_dir / "integrity.json", integrity)

    apt_lines = []
    if apt:
        apt_lines = [
            f"- trade_id: `{apt['trade_id']}`",
            f"- break_level: `{apt['trigger']['structure_break_level']}`",
            f"- market: `{apt['market']['market_price_at_signal']}`",
            f"- long/short: `{apt['pre_signal_position']['long_qty']}` / `{apt['pre_signal_position']['short_qty']}`",
            f"- avgs: `{apt['pre_signal_position']['long_avg']}` / `{apt['pre_signal_position']['short_avg']}`",
            f"- net: `{apt['pre_signal_position']['net_qty']}`",
            f"- fills before/after: `{apt['pre_signal_position']['fills_before_signal']}` / `{apt['pre_signal_position']['fills_at_or_after_signal']}`",
            f"- open orders: `{apt['pre_signal_position']['open_order_count']}`",
            f"- warnings: `{apt['quality']['warnings']}`",
            f"- ready: `{apt['quality']['ready_for_cobertura']}`",
        ]

    report = [
        "# Cobertura Blocker Input Bundle",
        "",
        f"**Decision: `{decision}`**",
        "",
        f"APT: **{apt_status}**",
        "",
        "## Answers",
        "",
        f"1. Blockers found (join keys for `{trigger_mode}`): **{len(keys)}**",
        f"2. Startfähig (ready): **{len(ready_records)}**",
        f"3. Unresolved: **{len(unresolved)}** — "
        + ", ".join(f"`{u['trade_id']}` ({';'.join(u.get('reasons') or [])})" for u in unresolved),
        f"4. All ready are REPLAY_MATCH: **"
        f"{all(r['quality']['replay_match_status']=='REPLAY_MATCH' for r in ready_records)}**",
        f"5. Break level + market price present for all ready: **"
        f"{all(r['trigger']['structure_break_level'] and r['market']['market_price_at_signal'] for r in ready_records)}**",
        f"6. Open order counts match embedded orders: **"
        f"{all(r['pre_signal_position']['open_order_count']==len(r['source_orders']['orders']) for r in ready_records)}**",
        f"7. Warnings total: **{warning_count}** (primarily FEE_RECONSTRUCTION_UNRESOLVED)",
        "8. APT values:",
        *apt_lines,
        "9. Deterministic JSONL: sorted by trade_id/trigger_mode; `sort_keys=True`.",
        "10. Cobertura runner may load ready records from "
        "`blocker_historical_states.jsonl` + `cobertura_start_scenarios.jsonl` "
        "(no full Cobertura run in this step).",
        "",
        "## Decision",
        "",
        f"`{decision}`",
        "",
    ]
    atomic_write_text(output_dir / "REPORT.md", "\n".join(report))

    return {
        "output_dir": str(output_dir),
        "decision": decision,
        "ready": len(ready_records),
        "unresolved": len(unresolved),
        "apt_status": apt_status,
        "records": ready_records,
        "unresolved_rows": unresolved,
    }


def apt_reference_values() -> dict[str, Any]:
    return {
        "trade_id": APT_REFERENCE_TRADE_ID,
        "trigger_mode": "first_break",
        "structure_break_level": 1.7639,
        "market_price_at_signal": 1.7223,
        "tradeable_5m_open": 1.7223,
        "neutralization_fill_price": 1.7223,
        "distance_break_to_market_pct": -0.023584103407222678,
        "long_qty": 296.365,
        "long_avg": 1.864531340748192,
        "short_qty": 197.59699999999998,
        "short_avg": 1.864561269615919,
        "net_qty": 98.76800000000003,
        "realized_pnl": -11.900133102067503,
        "unrealized_pnl_at_signal": -14.041991208541187,
        "total_economics_at_signal": -25.94212431060869,
        "fills_before_signal": 9,
        "fills_at_or_after_signal": 4,
        "active_cycle": 4,
        "open_order_count": 4,
        "signal_available_ts_prefix": "2026-01-19T00:00:00",
        "structure_break_event_ts_prefix": "2026-01-18T23:55:00",
    }
