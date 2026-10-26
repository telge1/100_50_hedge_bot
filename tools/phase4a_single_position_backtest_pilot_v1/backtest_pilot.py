#!/usr/bin/env python3
"""Phase 4A — single-position LONG/SHORT backtest pilot (read-only)."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def parse_utc(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def next_open_after(close_utc: str) -> str:
    return (parse_utc(close_utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_pilot(t: str, cfg: dict) -> bool:
    return cfg["pilot_from_utc"] <= t <= cfg["pilot_to_utc"]


def lower_targets(entry: float, intervals: list[dict], min_tp: float) -> list[dict]:
    out = []
    for iv in intervals:
        if iv.get("side") != "lower":
            continue
        top = float(iv["top_price"])
        if top >= entry:
            continue
        dist_pct = (entry - top) / entry * 100.0
        out.append(
            {
                "bottom": float(iv["bottom_price"]),
                "top": top,
                "depth": iv.get("depth"),
                "distance_entry_to_cluster_top_pct": dist_pct,
                "meets_min": dist_pct >= min_tp,
            }
        )
    out.sort(key=lambda x: -x["top"])
    return out


def upper_targets(entry: float, intervals: list[dict], min_tp: float) -> list[dict]:
    out = []
    for iv in intervals:
        if iv.get("side") != "upper":
            continue
        bot = float(iv["bottom_price"])
        if bot <= entry:
            continue
        dist_pct = (bot - entry) / entry * 100.0
        out.append(
            {
                "bottom": bot,
                "top": float(iv["top_price"]),
                "depth": iv.get("depth"),
                "distance_entry_to_cluster_bottom_pct": dist_pct,
                "meets_min": dist_pct >= min_tp,
            }
        )
    out.sort(key=lambda x: x["bottom"])
    return out


def select_short_tp(entry: float, intervals: list[dict], min_tp: float) -> tuple[dict | None, str]:
    eligible = [x for x in lower_targets(entry, intervals, min_tp) if x["meets_min"]]
    if not eligible:
        return None, "TP_SELECTION_UNRESOLVED"
    best = eligible[0]
    if len(eligible) > 1 and abs(eligible[0]["top"] - eligible[1]["top"]) < 1e-10:
        return None, "AMBIGUOUS_TP"
    return best, "VERIFIED"


def select_long_tp(entry: float, intervals: list[dict], min_tp: float) -> tuple[dict | None, str]:
    eligible = [x for x in upper_targets(entry, intervals, min_tp) if x["meets_min"]]
    if not eligible:
        return None, "TP_SELECTION_UNRESOLVED"
    best = eligible[0]
    if len(eligible) > 1 and abs(eligible[0]["bottom"] - eligible[1]["bottom"]) < 1e-10:
        return None, "AMBIGUOUS_TP"
    return best, "VERIFIED"


@dataclass
class Signal:
    candidate_id: str
    direction: str
    signal_close_utc: str
    entry_open_utc: str
    signal_source: str
    pool_lower: float
    pool_upper: float
    pool_identity: str
    landscape_intervals: list[dict]
    continuous_depth2_age: int | None
    age_gate_5: bool | None
    market_state_experimental: str
    experimental_long: bool = False


@dataclass
class OpenTrade:
    trade_id: str
    candidate_id: str
    direction: str
    signal_close_utc: str
    entry_open_utc: str
    entry_price: float
    sl_price: float
    tp_price: float
    pool_lower: float
    pool_upper: float
    tp_cluster_bottom: float
    tp_cluster_top: float
    market_state: str
    scenario: str
    suppressed_during: int = 0


def load_prices(csv_path: Path) -> dict[str, dict]:
    by_t = {}
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_t[row["candle_close_utc"]] = {
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            }
    return by_t


def load_market_states(path: Path) -> dict[str, str]:
    out = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["as_of_close_utc"]] = row["market_state"]
    return out


def load_age_map(path: Path) -> dict[str, dict]:
    out = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["candidate_id"]] = row
    return out


def load_short_signals(cfg: dict) -> list[Signal]:
    phase2a = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
    ages = load_age_map(ROOT / "results" / "phase2d_5bar_pool_age_comparison_v1/gate_comparison_all_20.csv")
    states = load_market_states(
        ROOT / "results" / "phase3b_two_pool_market_structure_detector_v1/market_state_timeline.csv"
    )
    out = []
    with (phase2a / "candidates.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sig = row["signal_close_utc"]
            if not in_pilot(sig, cfg):
                continue
            prof = json.loads(
                (phase2a / "candidate_landscape_profiles" / f"{row['candidate_id']}.json").read_text()
            )
            cand = prof.get("candidate") or {}
            age_row = ages.get(row["candidate_id"], {})
            out.append(
                Signal(
                    candidate_id=row["candidate_id"],
                    direction="SHORT",
                    signal_close_utc=sig,
                    entry_open_utc=row["entry_open_utc"],
                    signal_source="PHASE2A_SHORT_SCANNER",
                    pool_lower=float(row["upper_pool_lower"]),
                    pool_upper=float(row["upper_pool_upper"]),
                    pool_identity="|".join(cand.get("upper_pool_origins") or []),
                    landscape_intervals=cand.get("landscape_intervals") or [],
                    continuous_depth2_age=int(age_row.get("continuous_depth2_zone_age_bars") or 0)
                    if age_row.get("continuous_depth2_zone_age_bars")
                    else None,
                    age_gate_5=str(age_row.get("eligible_age_gate_5bars", "")).lower() == "true",
                    market_state_experimental=states.get(sig, "UNRESOLVED"),
                )
            )
    return out


def load_experimental_long_signals(cfg: dict) -> list[Signal]:
    conf_path = (
        ROOT
        / "results/phase3b_two_pool_market_structure_detector_v1/second_lower_confirmation_events.csv"
    )
    tp_path = (
        ROOT
        / "results/phase3b_two_pool_market_structure_detector_v1/upper_tp_candidates_at_confirmation.csv"
    )
    states = load_market_states(
        ROOT / "results/phase3b_two_pool_market_structure_detector_v1/market_state_timeline.csv"
    )
    tp_by_key: dict[tuple[int, str], dict] = {}
    with tp_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("tp_rank") != "1":
                continue
            key = (int(row["episode_id"]), row["confirmation_close_utc"])
            tp_by_key[key] = row

    out = []
    with conf_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            close = row["confirmation_close_utc"]
            if not in_pilot(close, cfg):
                continue
            ep = int(row["episode_id"])
            entry_open = row["earliest_entry_open_utc"]
            tp_row = tp_by_key.get((ep, close))
            if not tp_row:
                continue
            # Minimal landscape for audit: synthetic intervals from pools
            lowers = [
                {
                    "side": "lower",
                    "bottom_price": float(row["second_lower_bottom"]),
                    "top_price": float(row["second_lower_top"]),
                    "depth": 2,
                }
            ]
            uppers = [
                {
                    "side": "upper",
                    "bottom_price": float(tp_row["bottom_price"]),
                    "top_price": float(tp_row["top_price"]),
                    "depth": int(tp_row.get("depth") or 2),
                }
            ]
            cid = f"L3B_E{ep:02d}_{close.replace(':', '').replace('-', '')}"
            out.append(
                Signal(
                    candidate_id=cid,
                    direction="LONG",
                    signal_close_utc=close,
                    entry_open_utc=entry_open,
                    signal_source="PHASE3B_LONG_CANDIDATE_EXPERIMENTAL",
                    pool_lower=float(row["second_lower_bottom"]),
                    pool_upper=float(row["second_lower_top"]),
                    pool_identity=row.get("second_pool_track_id") or "",
                    landscape_intervals=lowers + uppers,
                    continuous_depth2_age=None,
                    age_gate_5=None,
                    market_state_experimental=states.get(close, "LONG_CANDIDATE"),
                    experimental_long=True,
                )
            )
    return out


def is_watch_blocking_short(sig: Signal, blocked_rows: list[dict]) -> bool:
    for b in blocked_rows:
        if b["candidate_id"] == sig.candidate_id:
            return True
    return sig.market_state_experimental == "WATCH_REVERSAL"


def build_executable(
    sig: Signal, cfg: dict, prices: dict[str, dict]
) -> tuple[dict | None, str]:
    entry_row = prices.get(sig.entry_open_utc)
    if not entry_row:
        return None, "NO_ENTRY_BAR"
    entry = float(entry_row["o"])
    min_tp = float(cfg["min_tp_distance_pct"])
    sl_up = float(cfg["sl_above_upper_pool_pct"]) / 100.0
    sl_lo = float(cfg["sl_below_lower_pool_pct"]) / 100.0

    if sig.direction == "SHORT":
        sl = sig.pool_upper * (1 + sl_up)
        tp_cluster, tp_reason = select_short_tp(entry, sig.landscape_intervals, min_tp)
        if not tp_cluster:
            return None, tp_reason
        tp = float(tp_cluster["top"])
        return {
            "entry_price": entry,
            "sl_price": sl,
            "tp_price": tp,
            "tp_cluster_bottom": tp_cluster["bottom"],
            "tp_cluster_top": tp_cluster["top"],
            "tp_selection": tp_reason,
        }, "OK"
    sl = sig.pool_lower * (1 - sl_lo)
    if sig.experimental_long and len(sig.landscape_intervals) >= 2:
        uppers = [x for x in sig.landscape_intervals if x.get("side") == "upper"]
        if uppers:
            u = uppers[0]
            bot = float(u["bottom_price"])
            dist = (bot - entry) / entry * 100
            if dist < min_tp:
                return None, "TP_SELECTION_UNRESOLVED"
            return {
                "entry_price": entry,
                "sl_price": sl,
                "tp_price": bot,
                "tp_cluster_bottom": bot,
                "tp_cluster_top": float(u["top_price"]),
                "tp_selection": "PHASE3B_NEAREST_UPPER_RANK1",
            }, "OK"
    tp_cluster, tp_reason = select_long_tp(entry, sig.landscape_intervals, min_tp)
    if not tp_cluster:
        return None, tp_reason
    return {
        "entry_price": entry,
        "sl_price": sl,
        "tp_price": float(tp_cluster["bottom"]),
        "tp_cluster_bottom": tp_cluster["bottom"],
        "tp_cluster_top": tp_cluster["top"],
        "tp_selection": tp_reason,
    }, "OK"


def simulate_exit(
    trade: OpenTrade, prices: dict[str, dict], pilot_to: str
) -> dict[str, Any]:
    t = parse_utc(trade.entry_open_utc)
    end = parse_utc(pilot_to)
    bars = 0
    ordered_times = sorted(prices.keys())
    idx = next((i for i, k in enumerate(ordered_times) if k >= trade.entry_open_utc), 0)
    trace = []
    for k in ordered_times[idx:]:
        if k < trade.entry_open_utc:
            continue
        if k > pilot_to:
            break
        bar = prices[k]
        bars += 1
        h, l = bar["h"], bar["l"]
        hit_sl = hit_tp = False
        if trade.direction == "SHORT":
            hit_sl = h >= trade.sl_price
            hit_tp = l <= trade.tp_price
        else:
            hit_sl = l <= trade.sl_price
            hit_tp = h >= trade.tp_price
        trace.append(
            {
                "bar_utc": k,
                "high": h,
                "low": l,
                "hit_sl": hit_sl,
                "hit_tp": hit_tp,
            }
        )
        if hit_sl and hit_tp:
            return {
                "exit_utc": k,
                "exit_reason": "AMBIGUOUS_INTRABAR_EXIT",
                "exit_reason_conservative_sl_first": "SL",
                "conservative_exit_price": trade.sl_price,
                "gross_pnl_pct": None,
                "net_pnl_pct": None,
                "bars": bars,
                "trace": trace,
            }
        if hit_sl:
            exit_p = trade.sl_price
            gross = (
                (trade.entry_price - exit_p) / trade.entry_price * 100
                if trade.direction == "SHORT"
                else (exit_p - trade.entry_price) / trade.entry_price * 100
            )
            return _exit_payload(k, "SL", exit_p, gross, bars, trace, cfg_fees=0.11)
        if hit_tp:
            exit_p = trade.tp_price
            gross = (
                (trade.entry_price - exit_p) / trade.entry_price * 100
                if trade.direction == "SHORT"
                else (exit_p - trade.entry_price) / trade.entry_price * 100
            )
            return _exit_payload(k, "TP", exit_p, gross, bars, trace, cfg_fees=0.11)
    return {
        "exit_utc": "",
        "exit_reason": "POSITION_OPEN_AT_DATA_END",
        "gross_pnl_pct": None,
        "net_pnl_pct": None,
        "bars": bars,
        "trace": trace,
    }


def _exit_payload(utc, reason, exit_p, gross, bars, trace, cfg_fees: float):
    net = gross - cfg_fees
    return {
        "exit_utc": utc,
        "exit_reason": reason,
        "exit_price": exit_p,
        "gross_pnl_pct": gross,
        "net_pnl_pct": net,
        "bars": bars,
        "trace": trace,
    }


def run_scenario(
    name: str,
    signals: list[Signal],
    cfg: dict,
    prices: dict[str, dict],
    *,
    apply_3b_block: bool,
    pool_age_gate: bool,
    include_experimental_long: bool,
    blocked_rows: list[dict],
) -> dict[str, Any]:
    raw_rows = []
    suppressed = []
    unresolved = []
    executable = []
    trades = []
    trade_seq = 0
    pending_same_bar: list[Signal] = []
    active_position: OpenTrade | None = None
    active_exit_utc: str | None = None
    fees = float(cfg["roundtrip_fees_pct"])

    def position_blocks_entry(entry_open: str) -> bool:
        if not active_position:
            return False
        if active_exit_utc is None:
            return True
        if active_exit_utc == "":
            return True
        return entry_open < active_exit_utc

    def flush_pending():
        nonlocal pending_same_bar
        if len(pending_same_bar) > 1:
            for s in pending_same_bar:
                unresolved.append(
                    {
                        "candidate_id": s.candidate_id,
                        "signal_close_utc": s.signal_close_utc,
                        "reason": "AMBIGUOUS_SIGNAL",
                        "scenario": name,
                    }
                )
            pending_same_bar = []
            return
        if len(pending_same_bar) == 1:
            process_signal(pending_same_bar[0])
            pending_same_bar = []

    def process_signal(sig: Signal):
        nonlocal trade_seq, active_position, active_exit_utc
        if active_position and active_exit_utc and sig.entry_open_utc >= active_exit_utc:
            active_position = None
            active_exit_utc = None
        eligible = True
        reasons = []
        if sig.direction == "SHORT" and sig.experimental_long:
            eligible = False
            reasons.append("NOT_SHORT")
        if sig.experimental_long and not include_experimental_long:
            eligible = False
            reasons.append("EXPERIMENTAL_LONG_DISABLED")
        if pool_age_gate and sig.direction == "SHORT":
            if not sig.age_gate_5:
                eligible = False
                reasons.append("POOL_AGE_GATE_5BARS")
        if apply_3b_block and sig.direction == "SHORT" and is_watch_blocking_short(sig, blocked_rows):
            eligible = False
            reasons.append("WATCH_REVERSAL_BLOCK_EXPERIMENTAL")

        raw_rows.append(
            {
                "candidate_id": sig.candidate_id,
                "direction": sig.direction,
                "signal_close_utc": sig.signal_close_utc,
                "entry_open_utc": sig.entry_open_utc,
                "signal_source": sig.signal_source,
                "pool_identity": sig.pool_identity,
                "entry_pool_lower": sig.pool_lower,
                "entry_pool_upper": sig.pool_upper,
                "market_state_experimental": sig.market_state_experimental,
                "eligible": eligible,
                "ineligible_reason": ";".join(reasons) if reasons else "",
                "scenario": name,
            }
        )
        if not eligible:
            return
        if position_blocks_entry(sig.entry_open_utc):
            suppressed.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": "SUPPRESSED_POSITION_OPEN",
                    "open_trade_id": active_position.trade_id if active_position else "",
                    "scenario": name,
                }
            )
            if active_position:
                active_position.suppressed_during += 1
            return

        ex, status = build_executable(sig, cfg, prices)
        if not ex:
            unresolved.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": status,
                    "scenario": name,
                }
            )
            return

        trade_seq += 1
        tid = f"T{name}_{trade_seq:03d}"
        ot = OpenTrade(
            trade_id=tid,
            candidate_id=sig.candidate_id,
            direction=sig.direction,
            signal_close_utc=sig.signal_close_utc,
            entry_open_utc=sig.entry_open_utc,
            entry_price=ex["entry_price"],
            sl_price=ex["sl_price"],
            tp_price=ex["tp_price"],
            pool_lower=sig.pool_lower,
            pool_upper=sig.pool_upper,
            tp_cluster_bottom=ex["tp_cluster_bottom"],
            tp_cluster_top=ex["tp_cluster_top"],
            market_state=sig.market_state_experimental,
            scenario=name,
        )
        executable.append({**raw_rows[-1], **ex, "trade_id": tid})
        exit_info = simulate_exit(ot, prices, cfg["pilot_to_utc"])
        gross = exit_info.get("gross_pnl_pct")
        net = exit_info.get("net_pnl_pct")
        if gross is not None and net is None:
            net = gross - fees
        active_position = ot
        if exit_info.get("exit_reason") == "POSITION_OPEN_AT_DATA_END":
            active_exit_utc = ""
        else:
            active_exit_utc = exit_info.get("exit_utc") or ""
        trades.append(
            {
                "trade_id": tid,
                "candidate_id": sig.candidate_id,
                "direction": sig.direction,
                "signal_close_utc": sig.signal_close_utc,
                "entry_open_utc": sig.entry_open_utc,
                "entry_price": ex["entry_price"],
                "entry_pool_lower": sig.pool_lower,
                "entry_pool_upper": sig.pool_upper,
                "sl_price": ex["sl_price"],
                "tp_price": ex["tp_price"],
                "tp_cluster_bottom": ex["tp_cluster_bottom"],
                "tp_cluster_top": ex["tp_cluster_top"],
                "exit_utc": exit_info.get("exit_utc"),
                "exit_reason": exit_info.get("exit_reason"),
                "exit_price": exit_info.get("exit_price"),
                "gross_pnl_pct": gross,
                "fees_pct": fees if gross is not None else None,
                "exit_trace_bars": len(exit_info.get("trace") or []),
                "net_pnl_pct": net,
                "bars_in_trade": exit_info.get("bars"),
                "pool_age_bars": sig.continuous_depth2_age,
                "market_state_at_entry": sig.market_state_experimental,
                "suppressed_signals_during": ot.suppressed_during,
                "scenario": name,
                "tp_selection": ex.get("tp_selection"),
            }
        )

    all_sigs = sorted(signals, key=lambda s: (s.entry_open_utc, s.signal_close_utc, s.candidate_id))
    current_entry = None
    for sig in all_sigs:
        if current_entry != sig.entry_open_utc:
            flush_pending()
            current_entry = sig.entry_open_utc
        pending_same_bar.append(sig)
    flush_pending()

    return {
        "scenario": name,
        "raw": raw_rows,
        "suppressed": suppressed,
        "unresolved": unresolved,
        "executable": executable,
        "trades": trades,
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
