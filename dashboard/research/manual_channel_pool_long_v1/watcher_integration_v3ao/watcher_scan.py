"""Scan one support line for multiple watcher episodes (V3AO entry discovery)."""

from __future__ import annotations

from typing import Callable

from line_geometry import LineDef
from watcher_config import TouchWatcherConfig
from dataclasses import dataclass

from watcher_fsm import EntryCandidate, replay_watcher_from_config


@dataclass
class LineWatcherScanStats:
    touch_episodes: int = 0
    entry_signals: int = 0


def _count_touch_episode(transitions) -> bool:
    return any(
        t.reason.startswith("touch:")
        or t.to_state in ("WATCHING_ABOVE", "WATCHING_BELOW")
        for t in transitions
    )


def scan_line_watcher_entries(
    candles: list[dict],
    line: LineDef,
    cfg: TouchWatcherConfig,
    i_start: int,
    i_end: int,
    fmt_utc: Callable[[int], str],
) -> list[EntryCandidate]:
    entries, _ = scan_line_watcher_entries_with_stats(
        candles, line, cfg, i_start, i_end, fmt_utc
    )
    return entries


def scan_line_watcher_entries_with_stats(
    candles: list[dict],
    line: LineDef,
    cfg: TouchWatcherConfig,
    i_start: int,
    i_end: int,
    fmt_utc: Callable[[int], str],
) -> tuple[list[EntryCandidate], LineWatcherScanStats]:
    entries: list[EntryCandidate] = []
    stats = LineWatcherScanStats()
    i = i_start
    while i <= i_end:
        r = replay_watcher_from_config(
            candles,
            line,
            cfg,
            i_start=i,
            i_end=i_end,
            fmt_utc=fmt_utc,
            one_entry_only=True,
        )
        if _count_touch_episode(r.transitions):
            stats.touch_episodes += 1
        if r.entries:
            stats.entry_signals += 1
            entries.append(r.entries[0])
            i = r.entries[0].entry_idx + 1
            continue
        if r.terminal_reason == "expired" and r.transitions:
            i = r.transitions[-1].bar_idx + 1
            continue
        if r.transitions:
            i = r.transitions[-1].bar_idx + 1
            continue
        break
    return entries, stats
