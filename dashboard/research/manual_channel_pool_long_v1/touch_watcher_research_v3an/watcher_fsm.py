"""Deterministic touch+watcher FSM for L1 trend support research (isolated, not production)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal

from line_geometry import LineDef, classify_candle_interval

from watcher_config import TouchWatcherConfig

TouchMode = Literal["LEGACY_020", "NEAR_003", "PHYSICAL_ONLY"]
ConfirmMode = Literal["A1", "A2", "A3", "LEGACY_C1", "B_RECLAIM"]
StateName = Literal[
    "IDLE",
    "TOUCH_DETECTED",
    "WATCHING_ABOVE",
    "WATCHING_BELOW",
    "RECLAIM_PENDING",
    "CONFIRMED",
    "ENTRY_SCHEDULED",
    "EXPIRED",
    "INVALIDATED",
]


class FSMState(str, Enum):
    IDLE = "IDLE"
    WATCHING_ABOVE = "WATCHING_ABOVE"
    WATCHING_BELOW = "WATCHING_BELOW"
    RECLAIM_PENDING = "RECLAIM_PENDING"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


@dataclass
class Transition:
    bar_idx: int
    utc: str
    from_state: str
    to_state: str
    reason: str
    o: float
    h: float
    l: float
    c: float
    line_px: float


@dataclass
class EntryCandidate:
    touch_utc: str
    touch_idx: int
    confirm_utc: str
    confirm_idx: int
    entry_utc: str
    entry_idx: int
    entry_price: float
    sl_price: float
    sl_pct: float
    scenario: str
    touch_mode: str
    watch_window: int
    notes: str = ""


@dataclass
class ReplayResult:
    config_id: str
    touch_mode: TouchMode
    confirm_mode: ConfirmMode
    watch_window: int
    transitions: list[Transition] = field(default_factory=list)
    entries: list[EntryCandidate] = field(default_factory=list)
    terminal_reason: str = ""
    config_documented: dict[str, Any] = field(default_factory=dict)


def line_px(line: LineDef, candle: dict) -> float | None:
    return line.price_at(float(candle["time"]))


def touch_on_bar(line: LineDef, candle: dict, mode: TouchMode) -> tuple[bool, str]:
    tol = {"LEGACY_020": 0.20, "NEAR_003": 0.03, "PHYSICAL_ONLY": 0.0}[mode]
    return touch_on_bar_tolerance(line, candle, tol)


def touch_on_bar_tolerance(line: LineDef, candle: dict, tolerance_pct: float) -> tuple[bool, str]:
    t_open = float(candle["time"])
    cls, lp, dist, method, touching = classify_candle_interval(
        line, t_open, float(candle["high"]), float(candle["low"])
    )
    if method == "inactive" or lp is None:
        return False, "line_inactive"
    if abs(tolerance_pct - 0.20) < 1e-9:
        ok = cls in ("TOUCH", "NEAR_0_10", "NEAR_0_20")
        return ok, f"legacy020 class={cls} dist={dist:.4f}%"
    if abs(tolerance_pct) < 1e-9:
        ok = bool(touching) or cls == "TOUCH"
        return ok, f"physical touching={touching} class={cls} dist={dist:.4f}%"
    ok = bool(touching) or dist <= float(tolerance_pct) + 1e-9
    return ok, f"tol={tolerance_pct:.4f}% dist={dist:.4f}% touching={touching}"


def bullish(c: dict) -> bool:
    return float(c["close"]) > float(c["open"])


def confirm_a1(c: dict, lp: float) -> bool:
    return bullish(c) and float(c["close"]) > lp


def confirm_a2(c: dict, lp: float, prev_close: float) -> bool:
    return float(c["close"]) > lp and float(c["close"]) > prev_close


def confirm_a3(c: dict, lp: float, prev_close: float, min_gain_pct: float) -> bool:
    if float(c["close"]) <= lp:
        return False
    rel = (float(c["close"]) - prev_close) / prev_close * 100.0
    if rel < float(min_gain_pct) - 1e-9:
        return False
    return True


def config_to_legacy_labels(cfg: TouchWatcherConfig) -> tuple[TouchMode, ConfirmMode]:
    label = cfg.legacy_touch_mode_label()
    if label in ("LEGACY_020", "NEAR_003", "PHYSICAL_ONLY"):
        touch_mode: TouchMode = label  # type: ignore[assignment]
    else:
        touch_mode = "NEAR_003"
    return touch_mode, cfg.confirmation_mode  # type: ignore[return-value]


def approach_from_above(
    candles: list[dict], bar_idx: int, line: LineDef, epsilon_pct: float = 0.05
) -> tuple[bool, str]:
    if bar_idx < 1:
        return False, "NO_PRIOR_BAR"
    prior = candles[bar_idx - 1]
    lp = line.price_at(float(prior["time"]))
    if lp is None:
        return False, "INACTIVE_PRIOR"
    pc = float(prior["close"])
    band = float(lp) * epsilon_pct / 100.0
    if pc > float(lp) + band:
        return True, "OK"
    return False, f"prior_close={pc} not_above_line={lp}"


def _log(tr: list[Transition], **kw) -> None:
    tr.append(Transition(**kw))


def replay_watcher_from_config(
    candles: list[dict],
    line: LineDef,
    cfg: TouchWatcherConfig,
    i_start: int,
    i_end: int,
    fmt_utc: Callable[[int], str],
    one_entry_only: bool = True,
) -> ReplayResult:
    touch_mode, confirm_mode = config_to_legacy_labels(cfg)
    cid = cfg.preset_id or f"{touch_mode}+{confirm_mode}+W{cfg.watch_window_bars}"
    res = ReplayResult(
        cid,
        touch_mode,
        confirm_mode,
        cfg.watch_window_bars,
        config_documented=cfg.to_documented_dict(),
    )
    tolerance_pct = cfg.touch_tolerance_pct
    confirm_mode = cfg.confirmation_mode
    watch_window = cfg.watch_window_bars
    sl_pct = cfg.sl_pct
    require_approach = cfg.require_approach_from_above
    approach_eps = cfg.approach_epsilon_pct
    min_gain = cfg.confirmation_min_gain_pct
    reclaim_bars = cfg.reclaim_confirmation_bars
    return _replay_watcher_core(
        candles,
        line,
        res=res,
        tolerance_pct=tolerance_pct,
        confirm_mode=confirm_mode,
        watch_window=watch_window,
        reclaim_bars=reclaim_bars,
        min_gain_pct=min_gain,
        i_start=i_start,
        i_end=i_end,
        fmt_utc=fmt_utc,
        sl_pct=sl_pct,
        require_approach=require_approach,
        approach_eps=approach_eps,
        one_entry_only=one_entry_only,
    )


def replay_watcher(
    candles: list[dict],
    line: LineDef,
    *,
    touch_mode: TouchMode,
    confirm_mode: ConfirmMode,
    watch_window: int,
    i_start: int,
    i_end: int,
    fmt_utc: Callable[[int], str],
    sl_pct: float = 1.20,
    require_approach: bool = True,
    one_entry_only: bool = True,
    confirmation_min_gain_pct: float = 0.20,
    reclaim_confirmation_bars: int = 2,
) -> ReplayResult:
    cid = f"{touch_mode}+{confirm_mode}+W{watch_window}"
    tol = {"LEGACY_020": 0.20, "NEAR_003": 0.03, "PHYSICAL_ONLY": 0.0}[touch_mode]
    res = ReplayResult(cid, touch_mode, confirm_mode, watch_window)
    return _replay_watcher_core(
        candles,
        line,
        res=res,
        tolerance_pct=tol,
        confirm_mode=confirm_mode,
        watch_window=watch_window,
        reclaim_bars=reclaim_confirmation_bars,
        min_gain_pct=confirmation_min_gain_pct,
        i_start=i_start,
        i_end=i_end,
        fmt_utc=fmt_utc,
        sl_pct=sl_pct,
        require_approach=require_approach,
        approach_eps=0.05,
        one_entry_only=one_entry_only,
    )


def _replay_watcher_core(
    candles: list[dict],
    line: LineDef,
    *,
    res: ReplayResult,
    tolerance_pct: float,
    confirm_mode: ConfirmMode,
    watch_window: int,
    reclaim_bars: int,
    min_gain_pct: float,
    i_start: int,
    i_end: int,
    fmt_utc: Callable[[int], str],
    sl_pct: float,
    require_approach: bool,
    approach_eps: float,
    one_entry_only: bool,
) -> ReplayResult:
    state = FSMState.IDLE
    touch_idx: int | None = None
    bars_since_touch = 0
    pending_entry_idx: int | None = None
    reclaim_streak: int = 0

    i = i_start
    while i <= i_end:
        c = candles[i]
        utc = fmt_utc(int(c["time"]))
        lp = line_px(line, c)
        if lp is None or not line.active_for_candle(float(c["time"])):
            i += 1
            continue

        prev_close = float(candles[i - 1]["close"]) if i > 0 else float(c["open"])

        if pending_entry_idx is not None and i >= pending_entry_idx:
            entry = float(c["open"])
            ci = i - 1
            cc = candles[ci]
            res.entries.append(
                EntryCandidate(
                    touch_utc=fmt_utc(int(candles[touch_idx]["time"])) if touch_idx is not None else "",
                    touch_idx=touch_idx or -1,
                    confirm_utc=fmt_utc(int(cc["time"])),
                    confirm_idx=ci,
                    entry_utc=utc,
                    entry_idx=i,
                    entry_price=entry,
                    sl_price=entry * (1.0 - sl_pct / 100.0),
                    sl_pct=sl_pct,
                    scenario=confirm_mode,
                    touch_mode=res.touch_mode,
                    watch_window=watch_window,
                )
            )
            if one_entry_only:
                res.terminal_reason = "entry_filled"
                return res
            state = FSMState.IDLE
            touch_idx = None
            pending_entry_idx = None
            i += 1
            continue

        if state == FSMState.IDLE:
            tok, detail = touch_on_bar_tolerance(line, c, tolerance_pct)
            if tok:
                if require_approach:
                    ok, ar = approach_from_above(candles, i, line, epsilon_pct=approach_eps)
                    if not ok:
                        _log(
                            res.transitions,
                            bar_idx=i,
                            utc=utc,
                            from_state="IDLE",
                            to_state="IDLE",
                            reason=f"touch_rejected_approach:{ar}",
                            o=c["open"],
                            h=c["high"],
                            l=c["low"],
                            c=c["close"],
                            line_px=lp,
                        )
                        i += 1
                        continue
                touch_idx = i
                bars_since_touch = 0
                below = float(c["close"]) < lp
                state = FSMState.WATCHING_BELOW if below else FSMState.WATCHING_ABOVE
                _log(
                    res.transitions,
                    bar_idx=i,
                    utc=utc,
                    from_state="IDLE",
                    to_state=state.value,
                    reason=f"touch:{detail}",
                    o=c["open"],
                    h=c["high"],
                    l=c["low"],
                    c=c["close"],
                    line_px=lp,
                )
            i += 1
            continue

        bars_since_touch += 1
        if bars_since_touch > watch_window:
            _log(
                res.transitions,
                bar_idx=i,
                utc=utc,
                from_state=state.value,
                to_state=FSMState.EXPIRED.value,
                reason=f"watch_window>{watch_window}",
                o=c["open"],
                h=c["high"],
                l=c["low"],
                c=c["close"],
                line_px=lp,
            )
            res.terminal_reason = "expired"
            return res

        if confirm_mode in ("A1", "A2", "A3", "LEGACY_C1") and state == FSMState.WATCHING_ABOVE:
            if float(c["close"]) < lp:
                state = FSMState.WATCHING_BELOW
                _log(
                    res.transitions,
                    bar_idx=i,
                    utc=utc,
                    from_state="WATCHING_ABOVE",
                    to_state=state.value,
                    reason="close_below_line",
                    o=c["open"],
                    h=c["high"],
                    l=c["low"],
                    c=c["close"],
                    line_px=lp,
                )
                i += 1
                continue
            ok = False
            if confirm_mode == "A1":
                ok = confirm_a1(c, lp)
            elif confirm_mode == "A2":
                ok = confirm_a2(c, lp, prev_close)
            elif confirm_mode in ("A3",):
                ok = confirm_a3(c, lp, prev_close, min_gain_pct)
            elif confirm_mode == "LEGACY_C1":
                ok = confirm_a1(c, lp)
            if ok and i + 1 <= i_end:
                pending_entry_idx = i + 1
                _log(
                    res.transitions,
                    bar_idx=i,
                    utc=utc,
                    from_state=state.value,
                    to_state="ENTRY_SCHEDULED",
                    reason=f"confirm_{confirm_mode}",
                    o=c["open"],
                    h=c["high"],
                    l=c["low"],
                    c=c["close"],
                    line_px=lp,
                )
                i += 1
                continue

        if confirm_mode == "B_RECLAIM":
            if state == FSMState.WATCHING_ABOVE and float(c["close"]) < lp:
                state = FSMState.WATCHING_BELOW
                reclaim_streak = 0
                _log(
                    res.transitions,
                    bar_idx=i,
                    utc=utc,
                    from_state="WATCHING_ABOVE",
                    to_state=state.value,
                    reason="break_close_below",
                    o=c["open"],
                    h=c["high"],
                    l=c["low"],
                    c=c["close"],
                    line_px=lp,
                )
                i += 1
                continue
            if state == FSMState.WATCHING_BELOW:
                if float(c["close"]) > lp:
                    reclaim_streak = 1
                    state = FSMState.RECLAIM_PENDING
                    _log(
                        res.transitions,
                        bar_idx=i,
                        utc=utc,
                        from_state="WATCHING_BELOW",
                        to_state=state.value,
                        reason=f"reclaim_close_{reclaim_streak}_of_{reclaim_bars}",
                        o=c["open"],
                        h=c["high"],
                        l=c["low"],
                        c=c["close"],
                        line_px=lp,
                    )
                    if reclaim_streak >= reclaim_bars and i + 1 <= i_end:
                        pending_entry_idx = i + 1
                        _log(
                            res.transitions,
                            bar_idx=i,
                            utc=utc,
                            from_state=state.value,
                            to_state="ENTRY_SCHEDULED",
                            reason=f"reclaim_confirm_{reclaim_bars}",
                            o=c["open"],
                            h=c["high"],
                            l=c["low"],
                            c=c["close"],
                            line_px=lp,
                        )
                i += 1
                continue
            if state == FSMState.RECLAIM_PENDING:
                if float(c["close"]) <= lp:
                    state = FSMState.WATCHING_BELOW
                    reclaim_streak = 0
                    _log(
                        res.transitions,
                        bar_idx=i,
                        utc=utc,
                        from_state="RECLAIM_PENDING",
                        to_state=state.value,
                        reason="reclaim_failed_close_below",
                        o=c["open"],
                        h=c["high"],
                        l=c["low"],
                        c=c["close"],
                        line_px=lp,
                    )
                elif float(c["close"]) > lp:
                    reclaim_streak += 1
                    _log(
                        res.transitions,
                        bar_idx=i,
                        utc=utc,
                        from_state=state.value,
                        to_state=state.value,
                        reason=f"reclaim_close_{reclaim_streak}_of_{reclaim_bars}",
                        o=c["open"],
                        h=c["high"],
                        l=c["low"],
                        c=c["close"],
                        line_px=lp,
                    )
                    if reclaim_streak >= reclaim_bars and i + 1 <= i_end:
                        pending_entry_idx = i + 1
                        _log(
                            res.transitions,
                            bar_idx=i,
                            utc=utc,
                            from_state=state.value,
                            to_state="ENTRY_SCHEDULED",
                            reason=f"reclaim_confirm_{reclaim_bars}",
                            o=c["open"],
                            h=c["high"],
                            l=c["low"],
                            c=c["close"],
                            line_px=lp,
                        )
                i += 1
                continue

        if state == FSMState.WATCHING_BELOW and confirm_mode != "B_RECLAIM":
            if float(c["close"]) > lp:
                state = FSMState.WATCHING_ABOVE
                _log(
                    res.transitions,
                    bar_idx=i,
                    utc=utc,
                    from_state="WATCHING_BELOW",
                    to_state=state.value,
                    reason="recovered_above_without_B",
                    o=c["open"],
                    h=c["high"],
                    l=c["low"],
                    c=c["close"],
                    line_px=lp,
                )
        i += 1

    res.terminal_reason = res.terminal_reason or "window_end_no_entry"
    return res