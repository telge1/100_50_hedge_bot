"""Explicit LLD research kernel: pane parity, historical replay, causal backtest.

Modes P / H / C share ``compute_lld_selected`` (engine + chart selection) with an
explicit ``tip``. Windowing and decision time are chosen by each caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .service import _TF_SEC, lld_config_for_timeframe, load_candles, select_lld_pools_for_chart
from .trp_import import ensure_trp_on_path, load_trp
from .workspace_session import get_workspace

DEFAULT_HISTORY_WEEKS = 12

MODE_PANE_PARITY = "pane_parity"
MODE_HIST_REPLAY = "hist_replay"
MODE_CAUSAL = "causal"  # live scanner: UI-selected pools, pane ends at decision bar
MODE_LIVE_SCANNER = MODE_CAUSAL


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def history_limit_for_timeframe(timeframe: str, weeks: int = DEFAULT_HISTORY_WEEKS) -> int:
    """Same bar count as ``historyLoadLimitForTimeframe`` in research_charts.js."""
    step = int(_TF_SEC.get(str(timeframe), 60))
    w = max(1, int(weeks))
    return max(100, round((w * 7 * 24 * 3600) / step))


def ui_lld_config(timeframe: str):
    """Workspace LLD settings with TF-scaled ``amount`` (pane path)."""
    return lld_config_for_timeframe(get_workspace().lld_config, str(timeframe))


def resolve_tip(candles: list, tip: datetime | None) -> datetime:
    if not candles:
        raise ValueError("cannot resolve tip on empty candle series")
    if tip is not None:
        return _ensure_utc(tip)
    last = candles[-1].timestamp
    return _ensure_utc(last if last.tzinfo else last.replace(tzinfo=timezone.utc))


def load_pane_candles(
    symbol: str,
    timeframe: str,
    *,
    from_unix: int | None = None,
    to_unix: int | None = None,
    history_weeks: int = DEFAULT_HISTORY_WEEKS,
):
    """Load candles like ``loadPane`` (from/to + limit; limit does not trim when from/to set)."""
    from .service import _candles_from_packed

    limit = history_limit_for_timeframe(timeframe, history_weeks)
    packed = load_candles(
        symbol,
        timeframe,
        start=from_unix,
        end=to_unix,
        limit=limit,
    )
    candles = _candles_from_packed(packed)
    return packed, candles


def compute_lld_selected(
    candles: list,
    timeframe: str,
    config=None,
    *,
    tip: datetime | None = None,
):
    """``run_liquidity_location`` + ``select_lld_pools_for_chart`` with explicit ``tip``."""
    trp = load_trp()
    cfg = lld_config_for_timeframe(config or get_workspace().lld_config, str(timeframe))
    result = trp["run_liquidity_location"](candles, cfg)
    tip_ts = resolve_tip(
        candles,
        tip if tip is not None else getattr(result, "last_timestamp", None),
    )
    selected = select_lld_pools_for_chart(
        result.pools_all,
        int(cfg.amount),
        tip=tip_ts,
    )
    return selected, cfg, result, tip_ts


def pool_row(pool: Any) -> dict | None:
    from pool_scan.clock import available_at, break_known_at

    known = available_at(pool)
    if known is None or pool.side not in ("lower", "upper"):
        return None
    source = pool.source_timestamp
    if source is not None and source.tzinfo is None:
        source = source.replace(tzinfo=timezone.utc)
    return {
        "pool_id": pool.pool_id,
        "side": pool.side,
        "bottom": float(pool.bottom_price),
        "top": float(pool.top_price),
        "known": known,
        "source": source,
        "break_at": break_known_at(pool),
    }


def rows_from_pools(pools: Iterable) -> list[dict]:
    rows: list[dict] = []
    for pool in pools:
        row = pool_row(pool)
        if row is not None:
            rows.append(row)
    return rows


CAUSAL_LLD_WALKER_KEY = "_causal_lld_walker"


def _candles_fingerprint(candles: list) -> tuple:
    if not candles:
        return (0,)
    stamps = candle_stamps(candles)
    last = candles[-1]
    return (len(candles), stamps[0], stamps[-1], float(last.close))


def _lld_bar_deps():
    ensure_trp_on_path()
    from indicators.liquidity_location.availability import attach_availability_metadata
    from indicators.liquidity_location.models import LiquidityPool, make_pool_id
    from indicators.liquidity_location.volume_strength import (
        compute_norm_vol,
        rolling_highest,
        rolling_lowest,
    )

    return (
        LiquidityPool,
        make_pool_id,
        attach_availability_metadata,
        compute_norm_vol,
        rolling_highest,
        rolling_lowest,
    )


class _IncrementalCausalLld:
    """Forward-only LLD + chart selection; bar ``i`` matches ``candles[: i + 1]`` prefix."""

    def __init__(self, candles: list, timeframe: str, config=None) -> None:
        (
            LiquidityPool,
            make_pool_id,
            attach_availability_metadata,
            compute_norm_vol,
            rolling_highest,
            rolling_lowest,
        ) = _lld_bar_deps()
        self._LiquidityPool = LiquidityPool
        self._make_pool_id = make_pool_id
        self._attach_availability_metadata = attach_availability_metadata

        self.candles = candles
        self.timeframe = str(timeframe)
        self.fingerprint = _candles_fingerprint(candles)
        self._cfg = lld_config_for_timeframe(config or get_workspace().lld_config, self.timeframe)
        self._amount = chart_lld_amount(self.timeframe, self._cfg)
        self._stamps = candle_stamps(candles)
        n = len(candles)
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]
        _vol, _ref, self._norm_vol = compute_norm_vol(volumes)
        self._h_arr = rolling_highest(highs, int(self._cfg.highest_len))
        self._l_arr = rolling_lowest(lows, int(self._cfg.lowest_len))
        self._pools: list = []
        self._active_upper: list = []
        self._active_lower: list = []
        self._built_through = -1

    def reset_bars(self) -> None:
        """Drop pool state; keep precomputed vol/swing arrays."""
        self._pools = []
        self._active_upper = []
        self._active_lower = []
        self._built_through = -1

    def advance_to(self, index: int) -> None:
        if index < 0 or index >= len(self.candles):
            raise IndexError(f"index {index} out of range for {len(self.candles)} candles")
        if index < self._built_through:
            self.reset_bars()
        while self._built_through < index:
            self._built_through += 1
            self._process_bar(self._built_through)

    def ui_at(self, index: int) -> tuple[list[dict], datetime]:
        """Selection at ``index``; pool state matches prefix ``candles[: index + 1]`` only."""
        self.advance_to(index)
        tip = self._stamps[index]
        selected = select_lld_pools_for_chart(self._pools, self._amount, tip=tip)
        return rows_from_pools(selected), tip

    def _process_bar(self, i: int) -> None:
        candles = self.candles
        LiquidityPool = self._LiquidityPool
        make_pool_id = self._make_pool_id
        attach_availability_metadata = self._attach_availability_metadata
        symbol = candles[0].symbol
        timeframe = self.timeframe
        norm_vol = self._norm_vol
        h_arr, l_arr = self._h_arr, self._l_arr
        pools = self._pools
        active_upper = self._active_upper
        active_lower = self._active_lower

        if i >= 1:
            strength = norm_vol[i - 1]
            source = candles[i - 1]
            source_range = source.high - source.low
            half = source_range / 2.0
            h_prev, h_cur = h_arr[i - 1], h_arr[i]
            l_prev, l_cur = l_arr[i - 1], l_arr[i]

            if (
                h_prev is not None
                and h_cur is not None
                and h_prev == source.high
                and candles[i].high < h_cur
            ):
                top = source.high + half
                bottom = source.high
                pool = LiquidityPool(
                    pool_id=make_pool_id(symbol, timeframe, "upper", source.unix_seconds),
                    symbol=symbol,
                    timeframe=timeframe,
                    side="upper",
                    created_index=i,
                    created_timestamp=candles[i].timestamp,
                    source_index=i - 1,
                    source_timestamp=source.timestamp,
                    top_price=float(top),
                    bottom_price=float(bottom),
                    strength=strength,
                    active=True,
                    source_high=float(source.high),
                    source_low=float(source.low),
                    source_volume=float(source.volume),
                    metadata=attach_availability_metadata(
                        confirmation_open=candles[i].timestamp,
                        source_open=source.timestamp,
                        timeframe=timeframe,
                        half_size=float(half),
                    ),
                )
                pools.append(pool)
                active_upper.append(pool)

            if (
                l_prev is not None
                and l_cur is not None
                and l_prev == source.low
                and candles[i].low > l_cur
            ):
                top = source.low
                bottom = source.low - half
                pool = LiquidityPool(
                    pool_id=make_pool_id(symbol, timeframe, "lower", source.unix_seconds),
                    symbol=symbol,
                    timeframe=timeframe,
                    side="lower",
                    created_index=i,
                    created_timestamp=candles[i].timestamp,
                    source_index=i - 1,
                    source_timestamp=source.timestamp,
                    top_price=float(top),
                    bottom_price=float(bottom),
                    strength=strength,
                    active=True,
                    source_high=float(source.high),
                    source_low=float(source.low),
                    source_volume=float(source.volume),
                    metadata=attach_availability_metadata(
                        confirmation_open=candles[i].timestamp,
                        source_open=source.timestamp,
                        timeframe=timeframe,
                        half_size=float(half),
                    ),
                )
                pools.append(pool)
                active_lower.append(pool)

        still_upper: list = []
        for pool in active_upper:
            if candles[i].high > pool.top_price:
                pool.active = False
                pool.invalidated_timestamp = candles[i].timestamp
                pool.invalidated_index = i
            else:
                still_upper.append(pool)
        self._active_upper = still_upper

        still_lower: list = []
        for pool in active_lower:
            if candles[i].low < pool.bottom_price:
                pool.active = False
                pool.invalidated_timestamp = candles[i].timestamp
                pool.invalidated_index = i
            else:
                still_lower.append(pool)
        self._active_lower = still_lower


def chart_lld_amount(timeframe: str, config) -> int:
    """Chart ``amount`` cap (same TF scaling as ``select_lld_pools_for_chart``)."""
    from .service import scaled_lld_amount

    return scaled_lld_amount(int(config.amount), str(timeframe))


def _walker_from_cache(
    cache: dict[str, Any] | None,
    candles: list,
    timeframe: str,
    config=None,
) -> _IncrementalCausalLld:
    if cache is None:
        return _IncrementalCausalLld(candles, timeframe, config)
    fp = _candles_fingerprint(candles)
    walker = cache.get(CAUSAL_LLD_WALKER_KEY)
    if not isinstance(walker, _IncrementalCausalLld) or walker.fingerprint != fp:
        walker = _IncrementalCausalLld(candles, timeframe, config)
        cache[CAUSAL_LLD_WALKER_KEY] = walker
    return walker


def ui_selected_rows_for_prefix(
    candles: list,
    timeframe: str,
    index: int,
    config=None,
    *,
    cache: dict[str, Any] | None = None,
) -> tuple[list[dict], datetime]:
    """LLD chart selection on ``candles[:index+1]`` with tip = last prefix bar open."""
    stamps = candle_stamps(candles)
    if index < 0 or index >= len(stamps):
        raise IndexError(f"index {index} out of range for {len(stamps)} candles")
    walker = _walker_from_cache(cache, candles, timeframe, config)
    return walker.ui_at(index)


def pool_known_at(row: dict, decision_time: datetime) -> bool:
    """Pool is known and not yet broken at ``decision_time`` (break uses bar close)."""
    moment = _ensure_utc(decision_time)
    if row["known"] > moment:
        return False
    break_at = row.get("break_at")
    if break_at is not None and break_at <= moment:
        return False
    return True


def filter_rows_at_decision(rows: list[dict], decision_time: datetime) -> list[dict]:
    return [row for row in rows if pool_known_at(row, decision_time)]


def candle_stamps(candles: list) -> list[datetime]:
    return [
        c.timestamp if c.timestamp.tzinfo else c.timestamp.replace(tzinfo=timezone.utc)
        for c in candles
    ]


def causal_window_from_index(stamps: list[datetime], index: int, weeks: int) -> int:
    """Rolling history window (legacy helper; live scanner uses pane prefix slice)."""
    window_start = stamps[index] - timedelta(weeks=weeks)
    window_from = 0
    while window_from < index and stamps[window_from] < window_start:
        window_from += 1
    return window_from


def parity_diff_pool_rows(
    ref_rows: list[dict],
    scanner_rows: list[dict],
) -> dict[str, Any]:
    """Compare full pool_id and bottom/top (before strategy filters)."""
    ref_map = {r["pool_id"]: r for r in ref_rows}
    scan_map = {r["pool_id"]: r for r in scanner_rows}
    only_ref = sorted(set(ref_map) - set(scan_map))
    only_scan = sorted(set(scan_map) - set(ref_map))
    edge_mismatch: list[str] = []
    for pid in sorted(set(ref_map) & set(scan_map)):
        a, b = ref_map[pid], scan_map[pid]
        if (a["bottom"], a["top"]) != (b["bottom"], b["top"]):
            edge_mismatch.append(pid)
    return {
        "ids_match": not only_ref and not only_scan and not edge_mismatch,
        "only_ref": only_ref,
        "only_scanner": only_scan,
        "edge_mismatch": edge_mismatch,
        "ref_count": len(ref_map),
        "scanner_count": len(scan_map),
    }


def ui_selected_rows_for_pane(
    candles: list,
    timeframe: str,
    config=None,
) -> tuple[list[dict], datetime]:
    """UI chart selection: full loaded pane, tip = last bar (``lld_objects`` path)."""
    selected, _cfg, _result, tip_ts = compute_lld_selected(
        candles, timeframe, config, tip=None
    )
    return rows_from_pools(selected), tip_ts


def ui_selected_rows_at_index(
    candles: list,
    timeframe: str,
    index: int,
    config=None,
) -> tuple[list[dict], datetime, datetime]:
    """Pool universe visible at ``index`` (prefix pane, tip = bar open at ``index``)."""
    stamps = candle_stamps(candles)
    if index < 0 or index >= len(stamps):
        raise IndexError(f"index {index} out of range for {len(stamps)} candles")
    rows, tip_ts = ui_selected_rows_for_prefix(candles, timeframe, index, config)
    return rows, tip_ts, stamps[index]


@dataclass(frozen=True)
class CausalProof:
    entry_open: datetime
    decision_time: datetime
    window_from_open: datetime
    slice_len: int
    max_candle_open: datetime
    tip: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_open": self.entry_open.isoformat(),
            "decision_time": self.decision_time.isoformat(),
            "window_from_open": self.window_from_open.isoformat(),
            "slice_len": self.slice_len,
            "max_candle_open": self.max_candle_open.isoformat(),
            "tip": self.tip.isoformat(),
        }


def scanner_pools_for_index(
    candles: list,
    timeframe: str,
    index: int,
    config=None,
    *,
    cache: dict[str, Any] | None = None,
):
    """Live scanner: UI ``select_lld_pools_for_chart`` on the loaded pane; time filter per bar."""
    from pool_scan.clock import bar_close

    stamps = candle_stamps(candles)
    if index < 0 or index >= len(stamps):
        raise IndexError(f"index {index} out of range for {len(stamps)} candles")

    walker = _walker_from_cache(cache, candles, timeframe, config)
    ui_selected, tip_ts = walker.ui_at(index)

    entry_open = stamps[index]
    decision_time = bar_close(entry_open, timeframe)
    available = filter_rows_at_decision(ui_selected, decision_time)
    proof = CausalProof(
        entry_open=entry_open,
        decision_time=decision_time,
        window_from_open=stamps[0],
        slice_len=index + 1,
        max_candle_open=entry_open,
        tip=tip_ts,
    )
    return available, ui_selected, proof


def causal_pools_for_index(
    candles: list,
    timeframe: str,
    index: int,
    config=None,
    *,
    history_weeks: int = DEFAULT_HISTORY_WEEKS,
    cache: dict[int, tuple[list[dict], CausalProof]] | None = None,
):
    """Returns temporally filtered scanner rows + proof (``history_weeks`` ignored)."""
    available, _ui_selected, proof = scanner_pools_for_index(
        candles, timeframe, index, config, cache=cache
    )
    return available, proof


def parity_scanner_vs_ui_at_index(
    candles: list,
    timeframe: str,
    index: int,
    config=None,
) -> dict[str, Any]:
    """Parity: scanner pool universe vs inline UI LLD chain on the same loaded pane."""
    scanner_rows, tip_ts, _ = ui_selected_rows_at_index(candles, timeframe, index, config)
    ref_diff = parity_diff_vs_lld_objects(candles, timeframe, config)
    row_diff = parity_diff_pool_rows(scanner_rows, scanner_rows)
    return {
        "tip": tip_ts.isoformat(),
        "pane_candle_count": len(candles),
        "parity_diff_vs_lld_objects": ref_diff,
        "scanner_selected_count": len(scanner_rows),
        "ids_match": bool(ref_diff.get("ids_match")) and bool(row_diff.get("ids_match")),
    }


def pane_parity_selected(candles: list, timeframe: str, config=None):
    """Mode P: full pane array, tip = last bar open."""
    selected, cfg, result, tip_ts = compute_lld_selected(candles, timeframe, config, tip=None)
    lookahead = len(candles) > 0
    return {
        "mode": MODE_PANE_PARITY,
        "lookahead": bool(lookahead),
        "tip": tip_ts.isoformat(),
        "selected_rows": rows_from_pools(selected),
        "lld_config": {
            "highest_len": cfg.highest_len,
            "lowest_len": cfg.lowest_len,
            "amount": int(cfg.amount),
        },
        "candle_count": len(candles),
        "candle_first": candle_stamps(candles)[0].isoformat() if candles else None,
        "candle_last": candle_stamps(candles)[-1].isoformat() if candles else None,
        "pools_all_count": len(result.pools_all),
        "selected_count": len(selected),
    }


def hist_replay_report(
    candles: list,
    timeframe: str,
    decision_time: datetime,
    config=None,
):
    """Mode H: same LLD as pane P; report H1 (selected) and H2 (filtered at decision)."""
    pane = pane_parity_selected(candles, timeframe, config)
    h1_rows = pane["selected_rows"]
    h2_rows = filter_rows_at_decision(h1_rows, decision_time)
    return {
        "mode": MODE_HIST_REPLAY,
        "lookahead": pane["lookahead"],
        "tip": pane["tip"],
        "decision_time": _ensure_utc(decision_time).isoformat(),
        "h1_selected_count": len(h1_rows),
        "h2_available_at_decision_count": len(h2_rows),
        "h1_selected_rows": h1_rows,
        "h2_available_rows": h2_rows,
        "note": (
            "H2 filters pane-selected pools at decision_time; it does not re-run LLD "
            "causally. Later pane candles can affect selected membership, pool status, "
            "and overlay end times in the UI."
        ),
        "candle_count": pane["candle_count"],
        "candle_first": pane["candle_first"],
        "candle_last": pane["candle_last"],
        "lld_config": pane["lld_config"],
    }


def parity_diff_vs_lld_objects(candles: list, timeframe: str, config=None) -> dict[str, Any]:
    """Compare kernel selected IDs/edges to the inline ``lld_objects`` LLD chain."""
    selected, cfg, _result, tip_ts = compute_lld_selected(candles, timeframe, config, tip=None)
    kernel_rows = {r["pool_id"]: r for r in rows_from_pools(selected)}

    trp = load_trp()
    cfg_ref = lld_config_for_timeframe(config or get_workspace().lld_config, str(timeframe))
    result_ref = trp["run_liquidity_location"](candles, cfg_ref)
    tip_ref = getattr(result_ref, "last_timestamp", None)
    selected_ref = select_lld_pools_for_chart(
        result_ref.pools_all,
        int(cfg_ref.amount),
        tip=tip_ref,
    )
    ref_rows = {r["pool_id"]: r for r in rows_from_pools(selected_ref)}

    only_kernel = sorted(set(kernel_rows) - set(ref_rows))
    only_ref = sorted(set(ref_rows) - set(kernel_rows))
    edge_mismatch = []
    for pid in sorted(set(kernel_rows) & set(ref_rows)):
        k, r = kernel_rows[pid], ref_rows[pid]
        if (k["bottom"], k["top"]) != (r["bottom"], r["top"]):
            edge_mismatch.append(pid)

    tip_ref_iso = None
    if tip_ref is not None:
        tip_ref_iso = _ensure_utc(tip_ref).isoformat()

    return {
        "tip_kernel": tip_ts.isoformat(),
        "tip_ref": tip_ref_iso,
        "ids_match": not only_kernel and not only_ref and not edge_mismatch,
        "only_kernel": only_kernel[:20],
        "only_ref": only_ref[:20],
        "edge_mismatch": edge_mismatch[:20],
        "selected_count": len(kernel_rows),
    }


def find_pool_by_bottom(rows: list[dict], bottom: float, tol: float = 0.0003) -> list[dict]:
    return [r for r in rows if abs(r["bottom"] - bottom) <= tol]
