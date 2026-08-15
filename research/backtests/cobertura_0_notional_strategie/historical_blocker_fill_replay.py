"""Fill-level causal replay for historical TEM blockers (pre-signal book state).

Replay path (same as root-cause / continuous TEM):
  open_trades_at_end (two_early_medium blockers)
    → run_isolated_blocker → run_historical_backtest
    → BacktestResult.fills_log / order_log

Cutoff rule (strict):
  include fill iff fill_timestamp < signal_available_ts
  fills at exactly signal_available_ts are excluded from pre-signal state

No Cobertura simulation. No cycle-aggregate fallbacks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import (
    FULL_HISTORY_CANDLE_LIMIT,
    analyze_blocker_run,
    run_isolated_blocker,
)
from research.backtests.run_tem_continuous_27_blocker_root_cause import fill_log
from research.backtests.second_leg_price_staging import resolve_grid_profile

from .historical_blocker_state_extraction import (
    APT_REFERENCE_TRADE_ID,
    compute_neutralization,
    parse_ts,
)

PROFILE = "two_early_medium"
TAKER_FEE_RATE_DEFAULT = 0.00055
CYCLE_RE = re.compile(r"CYCLE_(\d+)_", re.IGNORECASE)

REPLAY_SEMANTICS_MD = """# Fill-level replay semantics

## Engine path

1. Continuous TEM blockers come from
   `staging_profiles_continuous_1000_500_20260722` (`two_early_medium`, 1000/500).
2. Root-cause and this runner isolated-replay each trade via
   `run_isolated_blocker` → `run_historical_backtest`
   (`config_source=live`, `fill_model=conservative`, profile `two_early_medium`).
3. Candle source: `load_candles_for_symbol` + `normalize_candles`, limit 50000
   (same as continuous / root-cause).

## Cutoff

`before_signal = (fill_timestamp < signal_available_ts)`

- Fills **at** `signal_available_ts` are **not** in the pre-signal book.
- No same-candle lookahead: signal-bar fills are excluded entirely when
  `include_signal_bar_fills=false` (default).
- Open orders at cutoff: last order-log state with event timestamp `< cutoff`;
  no fills applied on/after cutoff.

## Book state

Pre-signal inventory is the `*_after` fields of the last fill with
`before_signal=true`. If no such fill exists but entry is before signal,
state may be empty flat or entry-only depending on fills.

## Fees

Prefer explicit `entry_fee` + `exit_fee` on the fill log.
If missing → do **not** invent; flag `FEE_RECONSTRUCTION_UNRESOLVED`.

## Fingerprint

Full replay (no cutoff) is compared to `tem_end_blockers_27.csv`
(final qty, realized, open_mtm, total_pnl, highest_cycle, duration).
Mismatch → `REPLAY_MISMATCH` (not ready).
"""


def _f(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts_key(value: Any) -> str:
    t = parse_ts(value)
    if t is None:
        return ""
    return t.isoformat()


def fill_before_signal(fill_ts: Any, signal_ts: Any, *, strict: bool = True) -> bool:
    ft = parse_ts(fill_ts)
    st = parse_ts(signal_ts)
    if ft is None or st is None:
        return False
    if strict:
        return ft < st
    return ft <= st


def extract_cycle_index(purpose: str | None, meta: dict[str, Any] | None = None) -> int | None:
    if meta and meta.get("cycle_index") is not None:
        try:
            return int(float(meta["cycle_index"]))
        except (TypeError, ValueError):
            pass
    m = CYCLE_RE.search(str(purpose or ""))
    if m:
        return int(m.group(1))
    return None


def fee_from_fill(fill: dict[str, Any]) -> tuple[float | None, list[str]]:
    flags: list[str] = []
    entry = _f(fill.get("entry_fee"))
    exit_ = _f(fill.get("exit_fee"))
    if entry is not None or exit_ is not None:
        fee = (entry or 0.0) + (exit_ or 0.0)
        if fee < -1e-12:
            flags.append("NEGATIVE_FEE")
        return max(fee, 0.0), flags
    flags.append("FEE_RECONSTRUCTION_UNRESOLVED")
    return None, flags


@dataclass
class CandleCache:
    limit: int = FULL_HISTORY_CANDLE_LIMIT
    _data: dict[str, list[Any]] | None = None

    def __post_init__(self) -> None:
        self._data = {}

    def get(self, coin: str) -> list[Any]:
        assert self._data is not None
        if coin not in self._data:
            self._data[coin] = normalize_candles(
                coin, load_candles_for_symbol(coin, limit=self.limit)
            )
        return self._data[coin]


def run_full_isolated_replay(
    *,
    coin: str,
    start_bar: int,
    candles: list[Any],
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    cfg = resolve_grid_profile(PROFILE)
    result = run_isolated_blocker(
        coin=coin,
        candles=candles,
        start_index=int(start_bar),
        staging_config=cfg,
        trade_number=0,
    )
    analysis = analyze_blocker_run(
        coin=coin,
        trade_number=0,
        start_index=int(start_bar),
        profile=PROFILE,
        result=result,
        candles=candles,
    )
    fills = fill_log(result)
    orders = list(getattr(result, "order_log", None) or [])
    return result, analysis, fills, orders


def compare_replay_fingerprint(
    *,
    result: Any,
    analysis: dict[str, Any],
    expected: dict[str, Any],
    tol_qty: float = 1e-6,
    tol_pnl: float = 2.0,
) -> list[dict[str, Any]]:
    """Compare full replay to tem_end_blockers_27 row (root-cause fingerprint)."""
    diffs: list[dict[str, Any]] = []

    def check(name: str, actual: Any, exp: Any, tol: float) -> None:
        a = _f(actual)
        e = _f(exp)
        if a is None or e is None:
            if e is not None and a is None:
                diffs.append(
                    {"metric": name, "expected": exp, "actual": actual, "abs_diff": None}
                )
            return
        if abs(a - e) > tol:
            diffs.append(
                {"metric": name, "expected": e, "actual": a, "abs_diff": a - e}
            )

    fills = fill_log(result)
    last = fills[-1] if fills else {}
    check(
        "final_long_qty",
        last.get("long_qty_after", getattr(result, "final_long_qty", None)),
        expected.get("final_long_qty"),
        tol_qty,
    )
    check(
        "final_short_qty",
        last.get("short_qty_after", getattr(result, "final_short_qty", None)),
        expected.get("final_short_qty"),
        tol_qty,
    )
    check(
        "realized_pnl",
        getattr(result, "realized_pnl", None),
        expected.get("realized_pnl"),
        tol_pnl,
    )
    realized = _f(getattr(result, "realized_pnl", None), 0.0) or 0.0
    flat = bool(analysis.get("trade_flat"))
    open_mtm = 0.0 if flat else _f(getattr(result, "unrealized_pnl", None))
    if open_mtm is None:
        open_mtm = (_f(analysis.get("final_mtm"), 0.0) or 0.0) - realized
    total = realized + (open_mtm or 0.0)
    check("open_mtm", open_mtm, expected.get("open_mtm"), tol_pnl)
    check("total_pnl", total, expected.get("total_pnl"), tol_pnl)
    check(
        "highest_cycle",
        getattr(result, "cycles_seen", None) or analysis.get("max_cycle"),
        expected.get("highest_cycle"),
        0.0,
    )
    check(
        "duration_bars",
        getattr(result, "candles_processed", None),
        expected.get("duration_bars"),
        0.0,
    )
    return diffs


def build_fill_ledger_rows(
    *,
    trade_id: str,
    coin: str,
    start_bar: int,
    fills: list[dict[str, Any]],
    signal_available_ts: str,
    strict_before_signal: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    realized_cum = 0.0
    prev_long = 0.0
    prev_short = 0.0

    for seq, fill in enumerate(fills, start=1):
        ts = fill.get("timestamp")
        before = fill_before_signal(ts, signal_available_ts, strict=strict_before_signal)
        local = int(float(fill.get("candle_index") or 0))
        abs_bar = int(start_bar) + local
        meta = dict(fill.get("metadata_excerpt") or {})
        purpose = str(fill.get("purpose") or "")
        cycle_i = extract_cycle_index(purpose, meta)
        stage = meta.get("stage_index")
        qty = _f(fill.get("qty"), 0.0) or 0.0
        px = _f(fill.get("fill_price"), 0.0) or 0.0
        fee, fee_flags = fee_from_fill(fill)
        rp = _f(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"), 0.0) or 0.0
        realized_cum += rp
        lq = _f(fill.get("long_qty_after"), 0.0) or 0.0
        sq = _f(fill.get("short_qty_after"), 0.0) or 0.0
        la = _f(fill.get("long_avg_after"), 0.0) or 0.0
        sa = _f(fill.get("short_avg_after"), 0.0) or 0.0
        oid = str(fill.get("order_id") or f"seq:{seq}")
        if oid in seen_ids:
            violations.append(
                {
                    "trade_id": trade_id,
                    "check": "duplicate_fill_id",
                    "detail": oid,
                    "pass_fail": "FAIL",
                }
            )
        seen_ids.add(oid)
        if lq < -1e-9 or sq < -1e-9:
            violations.append(
                {
                    "trade_id": trade_id,
                    "check": "negative_qty",
                    "detail": f"seq={seq} lq={lq} sq={sq}",
                    "pass_fail": "FAIL",
                }
            )
        if (lq > 1e-12 and la <= 0) or (sq > 1e-12 and sa <= 0):
            violations.append(
                {
                    "trade_id": trade_id,
                    "check": "avg_without_qty",
                    "detail": f"seq={seq}",
                    "pass_fail": "FAIL",
                }
            )
        if fee is not None and fee < -1e-12:
            violations.append(
                {
                    "trade_id": trade_id,
                    "check": "negative_fee",
                    "detail": f"seq={seq} fee={fee}",
                    "pass_fail": "FAIL",
                }
            )

        rows.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "fill_sequence": seq,
                "fill_timestamp": ts,
                "fill_bar": abs_bar,
                "local_candle_index": local,
                "candle_open": fill.get("candle_open"),
                "candle_high": fill.get("candle_high"),
                "candle_low": fill.get("candle_low"),
                "candle_close": fill.get("candle_close"),
                "order_id": oid,
                "purpose": purpose,
                "side": fill.get("side"),
                "cycle_index": cycle_i,
                "stage_index": stage,
                "requested_qty": qty,
                "filled_qty": qty,
                "fill_price": px,
                "fee_rate": fill.get("fee_rate"),
                "fee_usdt": fee,
                "fee_flags": "|".join(fee_flags),
                "realized_pnl_delta": rp,
                "realized_pnl_cumulative": realized_cum,
                "long_qty_after": lq,
                "long_avg_after": la,
                "short_qty_after": sq,
                "short_avg_after": sa,
                "net_qty_after": lq - sq,
                "active_cycle_after": cycle_i,
                "bot_state_after": purpose,
                "active_orders_after_count": fill.get("active_orders_after_count"),
                "causal_status": "before_signal" if before else "at_or_after_signal",
                "before_signal": before,
                "long_qty_before_fill": prev_long,
                "short_qty_before_fill": prev_short,
            }
        )
        prev_long, prev_short = lq, sq

    return rows, violations


def open_orders_at_cutoff(
    *,
    trade_id: str,
    coin: str,
    order_log: list[dict[str, Any]],
    signal_available_ts: str,
) -> list[dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for ev in order_log:
        ts = ev.get("timestamp")
        if not fill_before_signal(ts, signal_available_ts, strict=True):
            continue
        oid = str(ev.get("order_id") or "")
        if not oid:
            continue
        et = str(ev.get("event_type") or "").lower()
        status = str(ev.get("status") or "").lower()
        closedish = et in ("cancelled", "canceled", "filled", "closed") or status in (
            "cancelled",
            "canceled",
            "filled",
            "closed",
        )
        if closedish:
            active.pop(oid, None)
        else:
            active[oid] = dict(ev)
        old = ev.get("replaced_old_order_id")
        if old:
            active.pop(str(old), None)

    out = []
    for oid, ev in active.items():
        out.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "signal_available_ts": signal_available_ts,
                "order_id": oid,
                "side": ev.get("side"),
                "purpose": ev.get("purpose"),
                "qty": ev.get("qty"),
                "price": ev.get("price"),
                "trigger_price": ev.get("trigger_price"),
                "status": ev.get("status") or "open",
                "last_event_type": ev.get("event_type"),
                "last_event_timestamp": ev.get("timestamp"),
            }
        )
    return out


def pre_signal_snapshot(
    *,
    trade_id: str,
    coin: str,
    signal_available_ts: str,
    trade_entry_timestamp: str | None,
    ledger: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    market: dict[str, Any],
    replay_match_status: str,
    replay_diffs: list[dict[str, Any]],
    taker_fee_rate: float,
) -> dict[str, Any]:
    flags: list[str] = []
    before = [r for r in ledger if r.get("before_signal")]
    after = [r for r in ledger if not r.get("before_signal")]

    entry_ts = parse_ts(trade_entry_timestamp)
    sig_ts = parse_ts(signal_available_ts)
    if entry_ts and sig_ts and sig_ts < entry_ts:
        flags.append("SIGNAL_BEFORE_ENTRY")

    if any("FEE_RECONSTRUCTION_UNRESOLVED" in str(r.get("fee_flags") or "") for r in before):
        flags.append("FEE_RECONSTRUCTION_UNRESOLVED")

    if replay_match_status == "REPLAY_MISMATCH":
        flags.append("REPLAY_MISMATCH")

    m_open = _f(market.get("tradeable_5m_open"))
    m_fill = _f(market.get("neutralization_fill_price"))
    market_mismatch = False
    # Cross-check: tradeable open should match stored neutralization raw when slip=0
    raw = _f(market.get("neutralization_raw_fill_price"))
    if raw is not None and m_open is not None and abs(raw - m_open) > 1e-9:
        market_mismatch = True
        flags.append("MARKET_PRICE_MISMATCH")

    if not before:
        source_quality = "NO_FILLS_BEFORE_SIGNAL"
        flags.append("NO_FILLS_BEFORE_SIGNAL")
        lq = sq = la = sa = None
        last_ts = last_bar = None
        realized = None
        fees = None
        active_cycle = None
    else:
        last = before[-1]
        source_quality = "EXACT_FILL_LEVEL_BEFORE_SIGNAL"
        lq = float(last["long_qty_after"])
        sq = float(last["short_qty_after"])
        la = float(last["long_avg_after"])
        sa = float(last["short_avg_after"])
        last_ts = last["fill_timestamp"]
        last_bar = last["fill_bar"]
        realized = float(last["realized_pnl_cumulative"])
        fee_vals = [r["fee_usdt"] for r in before if r.get("fee_usdt") is not None]
        if len(fee_vals) == len(before):
            fees = float(sum(fee_vals))
        else:
            fees = None
            flags.append("FEE_RECONSTRUCTION_UNRESOLVED")
        active_cycle = last.get("active_cycle_after")
        if not fill_before_signal(last_ts, signal_available_ts, strict=True):
            flags.append("LOOKAHEAD_FILL_IN_PRESIGNAL")
            source_quality = "STATE_UNRESOLVED"

    mark = m_fill if m_fill is not None else m_open
    unreal = None
    if (
        mark is not None
        and source_quality == "EXACT_FILL_LEVEL_BEFORE_SIGNAL"
        and lq is not None
        and sq is not None
        and la is not None
        and sa is not None
    ):
        unreal = float(lq) * (float(mark) - float(la)) + float(sq) * (
            float(sa) - float(mark)
        )

    total_before = None
    if unreal is not None and realized is not None:
        total_before = realized + unreal

    ready = (
        source_quality == "EXACT_FILL_LEVEL_BEFORE_SIGNAL"
        and replay_match_status == "REPLAY_MATCH"
        and "SIGNAL_BEFORE_ENTRY" not in flags
        and "LOOKAHEAD_FILL_IN_PRESIGNAL" not in flags
        and "REPLAY_MISMATCH" not in flags
        and not market_mismatch
    )

    neut: dict[str, Any] = {}
    if (
        source_quality == "EXACT_FILL_LEVEL_BEFORE_SIGNAL"
        and mark is not None
        and lq is not None
        and sq is not None
        and la is not None
        and sa is not None
    ):
        neut = compute_neutralization(
            long_qty=lq,
            long_avg=la,
            short_qty=sq,
            short_avg=sa,
            fill_price=float(mark),
            taker_fee_rate=taker_fee_rate,
        )
        if neut.get("neutralization_status") == "SHORT_ALREADY_LARGER_THAN_LONG":
            flags.append("SHORT_LARGER_THAN_LONG")
            ready = False
        elif neut.get("neutralization_status") == "ALREADY_SIZE_NEUTRAL":
            flags.append("ALREADY_SIZE_NEUTRAL")

    post_total = None
    if total_before is not None and neut.get("neutralization_open_fee") is not None:
        post_total = total_before - float(neut["neutralization_open_fee"])

    return {
        "trade_id": trade_id,
        "coin": coin,
        "signal_available_ts": signal_available_ts,
        "last_fill_timestamp_before_signal": last_ts,
        "last_fill_bar_before_signal": last_bar,
        "fills_before_signal": len(before),
        "fills_at_or_after_signal": len(after),
        "active_cycle_at_signal": active_cycle,
        "active_order_count_at_signal": len(open_orders),
        "long_qty_before": lq,
        "long_avg_before": la,
        "short_qty_before": sq,
        "short_avg_before": sa,
        "net_long_qty_before": (None if lq is None or sq is None else lq - sq),
        "realized_pnl_before": realized,
        "cumulative_fees_before": fees,
        "unrealized_pnl_at_signal_price": unreal,
        "total_economics_before": total_before,
        "market_price_at_signal": mark,
        "tradeable_5m_timestamp": market.get("tradeable_5m_timestamp"),
        "tradeable_5m_open": m_open,
        "neutralization_fill_price": m_fill,
        "source_quality": source_quality,
        "replay_match_status": replay_match_status,
        "replay_diff_count": len(replay_diffs),
        "state_quality_flags": "|".join(dict.fromkeys(flags)),
        "ready_for_neutralization": ready,
        "neutralization_status": neut.get("neutralization_status"),
        "neutralization_short_qty": neut.get("neutralization_short_qty"),
        "neutralization_notional": neut.get("neutralization_notional"),
        "neutralization_fee": neut.get("neutralization_open_fee"),
        "post_neutralization_short_qty": neut.get("post_neutralization_short_qty"),
        "post_neutralization_short_avg": neut.get("post_neutralization_short_avg"),
        "post_neutralization_long_qty": neut.get("post_neutralization_long_qty"),
        "post_neutralization_avg_spread_pct_from_long": neut.get(
            "post_neutralization_avg_spread_pct_from_long"
        ),
        "post_neutralization_net_qty": neut.get("post_neutralization_net_qty"),
        "post_neutralization_total_economics": post_total,
        "market_mismatch": market_mismatch,
    }


def apt_fill_replay_check(
    snapshot: dict[str, Any],
    ledger: list[dict[str, Any]],
    *,
    candidate_long: float | None,
    candidate_short: float | None,
) -> dict[str, Any]:
    if snapshot.get("trade_id") != APT_REFERENCE_TRADE_ID:
        return {"status": "SKIPPED", "details": []}
    details: list[str] = []
    status = "APT_FILL_REPLAY_PASS"
    sig = str(snapshot.get("signal_available_ts") or "")
    if not sig.startswith("2026-01-19") or "00:00:00" not in sig:
        details.append(f"unexpected signal_available_ts={sig}")
        status = "APT_FILL_REPLAY_FAIL"

    before = [r for r in ledger if r.get("before_signal")]
    after = [r for r in ledger if not r.get("before_signal")]
    details.append(f"fills_before={len(before)} fills_at_or_after={len(after)}")
    if before:
        details.append(
            f"last_before={before[-1].get('fill_timestamp')} "
            f"{before[-1].get('purpose')} "
            f"lq={before[-1].get('long_qty_after')} sq={before[-1].get('short_qty_after')}"
        )
    if after:
        details.append(
            f"first_after={after[0].get('fill_timestamp')} "
            f"{after[0].get('purpose')} "
            f"lq={after[0].get('long_qty_after')} sq={after[0].get('short_qty_after')}"
        )

    on_signal = [
        r
        for r in ledger
        if _ts_key(r.get("fill_timestamp"))[:19] == _ts_key(sig)[:19]
    ]
    if on_signal and any(r.get("before_signal") for r in on_signal):
        details.append("fill at signal incorrectly marked before_signal")
        status = "APT_FILL_REPLAY_FAIL"

    lq = _f(snapshot.get("long_qty_before"))
    sq = _f(snapshot.get("short_qty_before"))
    if (
        candidate_long is not None
        and candidate_short is not None
        and lq is not None
        and sq is not None
    ):
        if abs(lq - candidate_long) < 0.01 and abs(sq - candidate_short) < 0.01:
            details.append(
                "pre-signal state MATCHES old cycle-4 candidate "
                f"({candidate_long}/{candidate_short})"
            )
        else:
            details.append(
                "pre-signal state DIFFERS from old cycle-4 candidate: "
                f"got {lq}/{sq} vs candidate {candidate_long}/{candidate_short}"
            )
            if status == "APT_FILL_REPLAY_PASS":
                status = "APT_FILL_REPLAY_WARNING"

    if snapshot.get("source_quality") != "EXACT_FILL_LEVEL_BEFORE_SIGNAL":
        details.append(f"source_quality={snapshot.get('source_quality')}")
        status = "APT_FILL_REPLAY_FAIL"

    if snapshot.get("replay_match_status") == "REPLAY_MISMATCH":
        status = "APT_FILL_REPLAY_FAIL"
        details.append("full replay fingerprint mismatch")

    return {"status": status, "details": details}
