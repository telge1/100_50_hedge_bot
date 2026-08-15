"""Deterministic historical TEM-blocker Cobertura start-state extraction (no backtest).

Position-state semantics (from ``build_cycle_timeline`` in
``run_tem_continuous_27_blocker_root_cause.py``):

- Each cycle row stores inventory fields updated after **every** attributed fill
  (first-leg LONG_ADD and second-leg covers). Final ``long_qty`` / ``short_qty`` /
  averages are therefore the state **after the last fill of that cycle**, not
  after first-leg only and not a cycle-start snapshot.
- ``cycle_open_mtm`` / ``cycle_total_pnl`` are marked at the last-fill candle.
- ``first_leg_fill_bar`` / ``start_bar`` give the first attributed fill bar;
  ``last_fill_bar = start_bar + duration_bars - 1``.

Selection rule for inventory at ``signal_available_ts`` (tradeable bar index T):

1. Prefer the latest cycle with ``last_fill_bar < T`` **and** no later cycle with
   ``first_leg_fill_bar < T`` (exact cycle-end snapshot fully before the signal).
2. If a cycle has ``first_leg_fill_bar < T <= last_fill_bar``, fills may have
   occurred on or after the signal bar → ``POSITION_SEMANTICS_UNRESOLVED``
   (no fill-level log in root-cause artifacts; no interpolation).
3. Never take ``cycle_at_first_break`` blindly when the cycle straddles T.

Fees are not present in cycle timelines → leave empty + quality flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import FULL_HISTORY_CANDLE_LIMIT
from research.regime_scanner.tem_structure_break.monitor import candles_to_frame

APT_REFERENCE_TRADE_ID = "APTUSDT|two_early_medium|continuous|0006"
APT_REFERENCE_EXPECT = {
    "long_qty": 526.870,
    "short_qty": 199.224,
    "long_avg": 1.768355,
    "short_avg": 1.780758,
    "structure_break_level": 1.7639,
    "structure_break_kind": "protected_low_4h_close_break",
    "signal_available_ts": "2026-01-19 00:00:00+00:00",
    "entry_ts": "2026-01-17T15:00:00+00:00",
}

QTY_TOL = 1e-6

POSITION_STATE_SEMANTICS_MD = """# Position state semantics (`blocker_cycle_timelines.csv`)

## Writer

`research/backtests/run_tem_continuous_27_blocker_root_cause.py` → `build_cycle_timeline()`.

## What each cycle row means

| Field | Semantics |
|---|---|
| `start_bar` / `first_leg_fill_bar` | Absolute bar of the first attributed cycle fill (typically `CYCLE_N_LONG_ADD`) |
| `long_qty` / `short_qty` / `long_avg` / `short_avg` | Inventory **after the last fill attributed to this cycle** (first- and second-leg fills overwrite) |
| `second_leg_fills` | Count of non-first-leg fills in the cycle |
| `duration_bars` | `last_fill_bar - start_bar + 1` |
| `cycle_open_mtm` | Unrealized MTM at the **last-fill** candle close |
| `cycle_total_pnl` | Cumulative realized (through last fill) + that MTM |
| `first_leg_realized_loss` / `realized_cover_net` | Partial realized components from fill PnL fields |

This is **not** a cycle-start snapshot and **not** a first-leg-only snapshot unless `second_leg_fills == 0` and only one fill occurred.

## Fill-level availability

Root-cause artifacts do **not** retain a per-fill ledger with timestamps for the 27 blockers.
`tem_fd_resolved_timelines.json` only repeats the same cycle aggregates.

Therefore exact inventory at an arbitrary `signal_available_ts` is only proven when:

`last_fill_bar < tradeable_signal_bar`

for the latest cycle that traded before the signal, with no later cycle starting before the signal.

If `first_leg_fill_bar < tradeable_bar <= last_fill_bar`, the cycle is **active across the signal** and the state is `POSITION_SEMANTICS_UNRESOLVED` (no interpolation).

## Fees

No cumulative fee field exists in these cycle timelines. `fees_before` is left empty with flag `FEES_NOT_IN_SOURCE`.
"""


def parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime()


def _f(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int | None = None) -> int | None:
    v = _f(value, None)
    if v is None:
        return default
    return int(v)


@dataclass
class Frame5mCache:
    """Same 5m source path as eval_common.CoinFrameCache.frame_5m (no HTF build)."""

    limit: int = FULL_HISTORY_CANDLE_LIMIT
    _frames: dict[str, pd.DataFrame] | None = None

    def __post_init__(self) -> None:
        self._frames = {}

    def get(self, coin: str) -> pd.DataFrame:
        assert self._frames is not None
        if coin not in self._frames:
            candles = normalize_candles(
                coin, load_candles_for_symbol(coin, limit=self.limit)
            )
            self._frames[coin] = candles_to_frame(candles)
        return self._frames[coin]


def bar_timestamp(frame: pd.DataFrame, bar: int | None) -> str | None:
    if bar is None or bar < 0 or bar >= len(frame):
        return None
    return str(frame.iloc[int(bar)]["timestamp"])


def find_bar_index(frame: pd.DataFrame, ts: str | datetime) -> int | None:
    target = parse_ts(ts)
    if target is None:
        return None
    col = pd.to_datetime(frame["timestamp"], utc=True)
    hits = (col == pd.Timestamp(target)).to_numpy().nonzero()[0]
    if len(hits) == 0:
        idxs = (col >= pd.Timestamp(target)).to_numpy().nonzero()[0]
        if len(idxs) == 0:
            return None
        return int(idxs[0])
    return int(hits[0])


def select_causal_5m_candles(
    frame: pd.DataFrame, signal_available_ts: str
) -> dict[str, Any]:
    sig = parse_ts(signal_available_ts)
    if sig is None or frame is None or frame.empty:
        return {"ok": False, "flags": ["CANDLE_UNRESOLVED"]}
    col = pd.to_datetime(frame["timestamp"], utc=True)
    sig_ts = pd.Timestamp(sig)
    prev_idx = (col < sig_ts).to_numpy().nonzero()[0]
    trade_idx = (col >= sig_ts).to_numpy().nonzero()[0]
    if len(prev_idx) == 0 or len(trade_idx) == 0:
        return {"ok": False, "flags": ["CANDLE_UNRESOLVED"]}
    p = int(prev_idx[-1])
    t = int(trade_idx[0])
    prev_ts = parse_ts(frame.iloc[p]["timestamp"])
    trade_ts = parse_ts(frame.iloc[t]["timestamp"])
    assert prev_ts is not None and trade_ts is not None
    if prev_ts >= sig or trade_ts < sig:
        return {
            "ok": False,
            "flags": ["CANDLE_UNRESOLVED", "SAME_CANDLE_LOOKAHEAD"],
        }

    def pack(i: int, prefix: str) -> dict[str, Any]:
        row = frame.iloc[i]
        return {
            f"{prefix}_timestamp": str(row["timestamp"]),
            f"{prefix}_open": float(row["open"]),
            f"{prefix}_high": float(row["high"]),
            f"{prefix}_low": float(row["low"]),
            f"{prefix}_close": float(row["close"]),
            f"{prefix}_bar": i,
        }

    return {"ok": True, "flags": [], **pack(p, "previous_5m"), **pack(t, "tradeable_5m")}


def short_fill_price(tradeable_open: float, slippage_bps: float) -> float:
    """Additional short: negative slippage worsens fill → lower price."""
    return float(tradeable_open) * (1.0 - float(slippage_bps) / 10000.0)


def compute_neutralization(
    *,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    fill_price: float,
    taker_fee_rate: float,
) -> dict[str, Any]:
    flags: list[str] = []
    if long_qty <= 0 or short_qty < 0:
        flags.append("NEGATIVE_OR_ZERO_QTY")
    if long_avg <= 0 or short_avg <= 0 or fill_price <= 0:
        flags.append("AVG_PRICE_INVALID")

    status = "NEEDS_SHORT_FILL"
    neut_qty = 0.0
    if abs(long_qty - short_qty) <= QTY_TOL:
        status = "ALREADY_SIZE_NEUTRAL"
        neut_qty = 0.0
        new_short_qty = short_qty
        new_short_avg = short_avg
        fee = 0.0
    elif short_qty > long_qty + QTY_TOL:
        status = "SHORT_ALREADY_LARGER_THAN_LONG"
        neut_qty = 0.0
        new_short_qty = short_qty
        new_short_avg = short_avg
        fee = 0.0
        flags.append("SHORT_ALREADY_LARGER_THAN_LONG")
    else:
        neut_qty = long_qty - short_qty
        new_short_qty = short_qty + neut_qty
        new_short_avg = (
            (short_qty * short_avg + neut_qty * fill_price) / new_short_qty
            if new_short_qty > 0
            else None
        )
        fee = neut_qty * fill_price * taker_fee_rate

    spread_abs = None
    spread_pct = None
    if new_short_avg is not None and long_avg > 0:
        spread_abs = abs(long_avg - new_short_avg)
        spread_pct = spread_abs / long_avg

    net = long_qty - (new_short_qty if new_short_qty is not None else short_qty)
    return {
        "neutralization_status": status,
        "neutralization_short_qty": neut_qty,
        "new_short_qty": new_short_qty,
        "new_short_avg": new_short_avg,
        "neutralization_notional": neut_qty * fill_price,
        "neutralization_open_fee": fee,
        "post_neutralization_long_qty": long_qty,
        "post_neutralization_short_qty": new_short_qty,
        "post_neutralization_long_avg": long_avg,
        "post_neutralization_short_avg": new_short_avg,
        "post_neutralization_avg_spread_abs": spread_abs,
        "post_neutralization_avg_spread_pct_from_long": spread_pct,
        "post_neutralization_net_qty": net,
        "flags": flags,
    }


def select_break_event(
    *,
    trade_id: str,
    trigger_mode: str,
    summary: dict[str, str],
    state_events: list[dict[str, str]],
    break_episodes: list[dict[str, str]],
) -> dict[str, Any]:
    """Deterministic break/invalidation event selection."""
    flags: list[str] = []
    ambiguous: list[dict[str, Any]] = []

    if trigger_mode == "first_break":
        target_event = "BREAK_PENDING_4H"
        summary_ts = summary.get("first_break_ts") or ""
        matches = [
            e
            for e in state_events
            if e.get("trade_id") == trade_id and e.get("event") == target_event
        ]
        matches = sorted(
            matches,
            key=lambda e: (
                str(e.get("signal_available_ts") or ""),
                _i(e.get("bar"), 10**12) or 10**12,
                _i(e.get("break_cycle_id"), 10**12) or 10**12,
            ),
        )
        if not matches:
            return {
                "ok": False,
                "flags": ["BREAK_EVENT_UNRESOLVED"],
                "ambiguous": ambiguous,
            }
        exact = [
            e
            for e in matches
            if (e.get("signal_available_ts") or "") == summary_ts
            or (
                summary_ts
                and (e.get("signal_available_ts") or "").startswith(summary_ts[:19])
            )
        ]
        if len(exact) > 1:
            flags.append("MULTIPLE_MATCHING_EVENTS")
            ambiguous.append(
                {
                    "trade_id": trade_id,
                    "trigger_mode": trigger_mode,
                    "reason": "multiple_BREAK_PENDING_4H_match_summary_ts",
                    "n_matches": len(exact),
                    "selection_rule": "first_by_signal_available_ts_then_bar_then_break_cycle_id",
                }
            )
        chosen = exact[0] if exact else matches[0]
        if not exact and summary_ts:
            flags.append("SUMMARY_TS_EVENT_MISMATCH")
        if len(matches) > 1 and not exact:
            flags.append("MULTIPLE_BREAK_PENDING_EPISODES_USED_FIRST")
        eps = [
            e
            for e in break_episodes
            if e.get("trade_id") == trade_id and e.get("event") == "BREAK_PENDING"
        ]
        eps = sorted(
            eps,
            key=lambda e: (
                str(e.get("timestamp") or ""),
                _i(e.get("break_cycle_id"), 10**12) or 10**12,
            ),
        )
        episode = eps[0] if eps else None
        level = _f(chosen.get("level"))
        kind = chosen.get("kind") or (episode.get("kind") if episode else None)
        if level is None and episode is not None:
            level = _f(episode.get("level"))
        ok = level is not None and bool(chosen.get("signal_available_ts"))
        return {
            "ok": ok,
            "flags": flags + ([] if ok else ["BREAK_EVENT_UNRESOLVED"]),
            "ambiguous": ambiguous,
            "event": chosen,
            "episode": episode,
            "trigger_event_timestamp": chosen.get("timestamp"),
            "signal_available_ts": chosen.get("signal_available_ts"),
            "structure_break_level": level,
            "structure_break_kind": kind,
            "structure_break_timeframe": chosen.get("timeframe") or "4h",
            "break_cycle_id": _i(chosen.get("break_cycle_id")),
            "confirmation_ts": chosen.get("confirmation_ts") or None,
            "event_bar": _i(chosen.get("bar")),
            "selection_rule": "first_BREAK_PENDING_4H_by_signal_available_ts_then_bar",
        }

    if trigger_mode == "final_invalidation":
        matches = [
            e
            for e in state_events
            if e.get("trade_id") == trade_id
            and e.get("event") == "LONG_THESIS_INVALIDATED"
        ]
        matches = sorted(
            matches,
            key=lambda e: (
                str(e.get("signal_available_ts") or e.get("timestamp") or ""),
                _i(e.get("bar"), 10**12) or 10**12,
            ),
        )
        if not matches:
            return {
                "ok": False,
                "flags": ["BREAK_EVENT_UNRESOLVED"],
                "ambiguous": ambiguous,
            }
        if len(matches) > 1:
            flags.append("MULTIPLE_MATCHING_EVENTS")
            ambiguous.append(
                {
                    "trade_id": trade_id,
                    "trigger_mode": trigger_mode,
                    "reason": "multiple_LONG_THESIS_INVALIDATED",
                    "n_matches": len(matches),
                    "selection_rule": "first_by_signal_available_ts_then_bar",
                }
            )
        chosen = matches[0]
        sig = chosen.get("signal_available_ts") or chosen.get("timestamp")
        level = _f(chosen.get("level"))
        if level is None:
            level = _f(summary.get("invalidation_level_value"))
        kind = chosen.get("kind") or summary.get("invalidation_level_type")
        ok = level is not None and bool(sig)
        return {
            "ok": ok,
            "flags": flags + ([] if ok else ["BREAK_EVENT_UNRESOLVED"]),
            "ambiguous": ambiguous,
            "event": chosen,
            "episode": None,
            "trigger_event_timestamp": chosen.get("timestamp"),
            "signal_available_ts": sig,
            "structure_break_level": level,
            "structure_break_kind": kind,
            "structure_break_timeframe": chosen.get("timeframe") or "4h",
            "break_cycle_id": _i(chosen.get("break_cycle_id")),
            "confirmation_ts": chosen.get("confirmation_ts") or sig,
            "event_bar": _i(chosen.get("bar")),
            "selection_rule": "first_LONG_THESIS_INVALIDATED_by_signal_available_ts",
        }

    raise ValueError(f"unknown trigger_mode: {trigger_mode}")


def select_position_state(
    *,
    trade_id: str,
    cycles: list[dict[str, str]],
    signal_available_ts: str,
    frame: pd.DataFrame,
) -> dict[str, Any]:
    """Select last causally valid cycle inventory before tradeable signal bar."""
    del trade_id  # used only for call-site clarity
    flags: list[str] = []
    tradeable_bar = find_bar_index(frame, signal_available_ts)
    if tradeable_bar is None:
        return {
            "ok": False,
            "state_quality": "STATE_UNRESOLVED",
            "flags": ["CANDLE_UNRESOLVED", "POSITION_SEMANTICS_UNRESOLVED"],
            "state_selection_rule": "none",
        }

    enriched: list[dict[str, Any]] = []
    for c in cycles:
        start_bar = _i(c.get("start_bar"))
        first_leg = _i(c.get("first_leg_fill_bar"))
        dur = _i(c.get("duration_bars"), 1) or 1
        if start_bar is None:
            continue
        last_fill = start_bar + dur - 1
        fl = first_leg if first_leg is not None else start_bar
        enriched.append(
            {
                **c,
                "start_bar_i": start_bar,
                "first_leg_i": fl,
                "last_fill_i": last_fill,
                "first_leg_ts": bar_timestamp(frame, fl),
                "last_fill_ts": bar_timestamp(frame, last_fill),
            }
        )
    if not enriched:
        return {
            "ok": False,
            "state_quality": "STATE_UNRESOLVED",
            "flags": ["POSITION_SEMANTICS_UNRESOLVED"],
            "state_selection_rule": "no_cycles",
            "tradeable_bar": tradeable_bar,
        }

    exact = [c for c in enriched if c["last_fill_i"] < tradeable_bar]
    straddling = [
        c for c in enriched if c["first_leg_i"] < tradeable_bar <= c["last_fill_i"]
    ]
    exact_sorted = sorted(exact, key=lambda c: int(float(c["cycle_index"])))
    straddle_sorted = sorted(straddling, key=lambda c: int(float(c["cycle_index"])))

    if straddle_sorted:
        c = straddle_sorted[-1]
        flags.extend(
            [
                "POSITION_SEMANTICS_UNRESOLVED",
                "CYCLE_ACTIVE_ACROSS_SIGNAL",
                "NO_FILL_LEVEL_LOG",
            ]
        )
        return {
            "ok": False,
            "state_quality": "STATE_UNRESOLVED",
            "flags": flags,
            "state_selection_rule": (
                "reject_straddling_cycle_first_leg_before_signal_last_fill_on_or_after;"
                "requires_fill_level_log"
            ),
            "tradeable_bar": tradeable_bar,
            "source_cycle_index": int(float(c["cycle_index"])),
            "source_state_timestamp": c.get("last_fill_ts"),
            "first_leg_timestamp": c.get("first_leg_ts"),
            "last_fill_timestamp": c.get("last_fill_ts"),
            "candidate_long_qty": _f(c.get("long_qty")),
            "candidate_short_qty": _f(c.get("short_qty")),
            "candidate_long_avg": _f(c.get("long_avg")),
            "candidate_short_avg": _f(c.get("short_avg")),
            "candidate_cycle_total_pnl": _f(c.get("cycle_total_pnl")),
            "candidate_cycle_open_mtm": _f(c.get("cycle_open_mtm")),
            "candidate_realized_cover_net": _f(c.get("realized_cover_net")),
            "candidate_first_leg_realized_loss": _f(c.get("first_leg_realized_loss")),
            "long_qty_before": None,
            "short_qty_before": None,
            "long_avg_before": None,
            "short_avg_before": None,
            "cycle_total_pnl_before": None,
            "cycle_open_mtm_before": None,
            "realized_economics_before": None,
            "fees_before": None,
        }

    if not exact_sorted:
        return {
            "ok": False,
            "state_quality": "STATE_UNRESOLVED",
            "flags": ["POSITION_SEMANTICS_UNRESOLVED", "NO_CYCLE_BEFORE_SIGNAL"],
            "state_selection_rule": "no_cycle_with_last_fill_before_signal",
            "tradeable_bar": tradeable_bar,
        }

    c = exact_sorted[-1]
    later = [
        x
        for x in enriched
        if int(float(x["cycle_index"])) > int(float(c["cycle_index"]))
        and x["first_leg_i"] < tradeable_bar
    ]
    if later:
        return {
            "ok": False,
            "state_quality": "STATE_UNRESOLVED",
            "flags": ["POSITION_SEMANTICS_UNRESOLVED", "LATER_CYCLE_STARTED_BEFORE_SIGNAL"],
            "state_selection_rule": "later_cycle_started_before_signal",
            "tradeable_bar": tradeable_bar,
        }

    lq = _f(c.get("long_qty"))
    sq = _f(c.get("short_qty"))
    la = _f(c.get("long_avg"))
    sa = _f(c.get("short_avg"))
    if lq is None or sq is None or la is None or sa is None:
        return {
            "ok": False,
            "state_quality": "STATE_UNRESOLVED",
            "flags": ["POSITION_SEMANTICS_UNRESOLVED", "MISSING_QTY_OR_AVG"],
            "state_selection_rule": "exact_cycle_end_before_signal_but_missing_fields",
            "tradeable_bar": tradeable_bar,
            "source_cycle_index": int(float(c["cycle_index"])),
        }
    if lq <= 0 or sq < 0:
        flags.append("NEGATIVE_OR_ZERO_QTY")
    if la <= 0 or sa <= 0:
        flags.append("AVG_PRICE_INVALID")

    last_ts = parse_ts(c.get("last_fill_ts"))
    sig = parse_ts(signal_available_ts)
    if last_ts is not None and sig is not None and last_ts >= sig:
        return {
            "ok": False,
            "state_quality": "STATE_UNRESOLVED",
            "flags": flags
            + ["SIGNAL_BEFORE_STATE", "POSITION_SEMANTICS_UNRESOLVED"],
            "state_selection_rule": "last_fill_not_strictly_before_signal",
            "tradeable_bar": tradeable_bar,
        }

    total = _f(c.get("cycle_total_pnl"))
    mtm = _f(c.get("cycle_open_mtm"))
    realized = None if total is None or mtm is None else total - mtm
    flags.append("FEES_NOT_IN_SOURCE")
    flags.append("EXACT_CYCLE_END_BEFORE_SIGNAL")
    bad = {"NEGATIVE_OR_ZERO_QTY", "AVG_PRICE_INVALID"} & set(flags)
    return {
        "ok": not bad,
        "state_quality": "EXACT_CYCLE_END_BEFORE_SIGNAL" if not bad else "STATE_UNRESOLVED",
        "flags": flags,
        "state_selection_rule": (
            "latest_cycle_with_last_fill_bar_strictly_before_tradeable_signal_bar;"
            "qty_avgs_are_post_last_fill_of_that_cycle"
        ),
        "tradeable_bar": tradeable_bar,
        "source_cycle_index": int(float(c["cycle_index"])),
        "source_state_timestamp": c.get("last_fill_ts"),
        "first_leg_timestamp": c.get("first_leg_ts"),
        "last_fill_timestamp": c.get("last_fill_ts"),
        "long_qty_before": lq,
        "short_qty_before": sq,
        "long_avg_before": la,
        "short_avg_before": sa,
        "cycle_total_pnl_before": total,
        "cycle_open_mtm_before": mtm,
        "realized_economics_before": realized,
        "fees_before": None,
        "realized_cover_net": _f(c.get("realized_cover_net")),
        "first_leg_realized_loss": _f(c.get("first_leg_realized_loss")),
    }


def apt_reference_check(row: dict[str, Any]) -> dict[str, Any]:
    """Compare reconstructed APT row against known audit expectations."""
    if row.get("trade_id") != APT_REFERENCE_TRADE_ID:
        return {"status": "SKIPPED", "details": []}
    details: list[str] = []
    status = "APT_REFERENCE_PASS"

    def close(a: Any, b: float, tol: float, name: str) -> None:
        nonlocal status
        av = _f(a)
        if av is None:
            details.append(f"{name}: missing")
            status = "APT_REFERENCE_FAIL"
            return
        if abs(av - b) > tol:
            details.append(f"{name}: got {av} expected ~{b}")
            status = "APT_REFERENCE_FAIL"

    close(
        row.get("structure_break_level"),
        APT_REFERENCE_EXPECT["structure_break_level"],
        1e-6,
        "structure_break_level",
    )
    if row.get("structure_break_kind") != APT_REFERENCE_EXPECT["structure_break_kind"]:
        details.append(
            f"kind: got {row.get('structure_break_kind')} expected "
            f"{APT_REFERENCE_EXPECT['structure_break_kind']}"
        )
        status = "APT_REFERENCE_FAIL"
    sig = str(row.get("signal_available_ts") or "")
    exp_sig = APT_REFERENCE_EXPECT["signal_available_ts"]
    if not (sig.startswith(exp_sig[:19]) or exp_sig.startswith(sig[:19])):
        details.append(f"signal_available_ts: got {sig} expected {exp_sig}")
        status = "APT_REFERENCE_FAIL"

    if row.get("state_quality") == "STATE_UNRESOLVED" and "CYCLE_ACTIVE_ACROSS_SIGNAL" in str(
        row.get("state_quality_flags") or ""
    ):
        cand_lq = _f(row.get("candidate_long_qty"))
        cand_sq = _f(row.get("candidate_short_qty"))
        if (
            cand_lq is not None
            and cand_sq is not None
            and abs(cand_lq - APT_REFERENCE_EXPECT["long_qty"]) < 0.01
            and abs(cand_sq - APT_REFERENCE_EXPECT["short_qty"]) < 0.01
        ):
            details.append(
                "candidate cycle-4 end snapshot matches prior audit inventory, "
                "but cycle straddles signal_available_ts → not adopted as pre-signal state"
            )
            if status == "APT_REFERENCE_PASS":
                status = "APT_REFERENCE_WARNING"
        else:
            details.append(
                "STATE_UNRESOLVED for straddling cycle-4; "
                f"candidate inventory lq={cand_lq} sq={cand_sq}"
            )
            if status == "APT_REFERENCE_PASS":
                status = "APT_REFERENCE_WARNING"
    else:
        close(row.get("long_qty_before"), APT_REFERENCE_EXPECT["long_qty"], 0.01, "long_qty_before")
        close(row.get("short_qty_before"), APT_REFERENCE_EXPECT["short_qty"], 0.01, "short_qty_before")
        close(row.get("long_avg_before"), APT_REFERENCE_EXPECT["long_avg"], 1e-5, "long_avg_before")
        close(row.get("short_avg_before"), APT_REFERENCE_EXPECT["short_avg"], 1e-5, "short_avg_before")

    return {"status": status, "details": details}
