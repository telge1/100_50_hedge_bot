"""Replace touch+C1 pending with V3AN watcher rows (entry only; pools via confirm bar SSOT)."""

from __future__ import annotations

from typing import Any

from line_geometry import LineDef
from support_line_selection import LINE_SELECTION_INDEPENDENT
from watcher_config import TouchWatcherConfig

from .pending_materialize import materialize_pending_row
from dataclasses import dataclass

from .watcher_scan import LineWatcherScanStats, scan_line_watcher_entries_with_stats


@dataclass
class WatcherPendingBuildStats:
    touch_episodes: int = 0
    entry_signals: int = 0
    structure_rejected: int = 0
    materialized_rows: int = 0


def build_watcher_pending(
    *,
    l1,
    candles: list[dict],
    lines: list[LineDef],
    lines_by_id: dict[str, LineDef],
    upper_line: LineDef | None,
    cfg: TouchWatcherConfig,
    line_selection_mode: str = LINE_SELECTION_INDEPENDENT,
) -> list[dict[str, Any]]:
    pending, _ = build_watcher_pending_with_stats(
        l1=l1,
        candles=candles,
        lines=lines,
        lines_by_id=lines_by_id,
        upper_line=upper_line,
        cfg=cfg,
        line_selection_mode=line_selection_mode,
    )
    return pending


def build_watcher_pending_with_stats(
    *,
    l1,
    candles: list[dict],
    lines: list[LineDef],
    lines_by_id: dict[str, LineDef],
    upper_line: LineDef | None,
    cfg: TouchWatcherConfig,
    line_selection_mode: str = LINE_SELECTION_INDEPENDENT,
) -> tuple[list[dict[str, Any]], WatcherPendingBuildStats]:
    pending: list[dict[str, Any]] = []
    seq_by_line: dict[str, int] = {}
    i_end = len(candles) - 1
    stats = WatcherPendingBuildStats()

    for line in lines:
        i_start = 0
        entries, ls = scan_line_watcher_entries_with_stats(
            candles, line, cfg, i_start, i_end, l1.fmt_utc
        )
        stats.touch_episodes += ls.touch_episodes
        stats.entry_signals += ls.entry_signals
        for ent in entries:
            seq = seq_by_line.get(line.drawing_id, 0) + 1
            seq_by_line[line.drawing_id] = seq
            te_id = f"te-{line.drawing_id}-{seq:04d}"
            struct_class, struct_reject = l1.assess_long_touch_eligibility(
                line,
                candles,
                ent.touch_idx,
                upper=upper_line,
                line_selection_mode=line_selection_mode,
            )
            confirm_idx = ent.confirm_idx
            row = materialize_pending_row(
                l1=l1,
                line=line,
                candles=candles,
                lines=lines,
                lines_by_id=lines_by_id,
                upper_line=upper_line,
                touch_idx=ent.touch_idx,
                confirm_idx=confirm_idx,
                entry_idx=ent.entry_idx,
                touch_event_id=te_id,
                line_selection_mode=line_selection_mode,
                confirm_variant=cfg.confirmation_mode,
                struct_class=struct_class,
                struct_reject=struct_reject,
            )
            row["_v3ao_watcher_preset"] = cfg.preset_id or cfg.config_id()
            row["_v3ao_touch_tolerance_pct"] = cfg.touch_tolerance_pct
            pending.append(row)
            stats.materialized_rows += 1
            if struct_reject is not None:
                stats.structure_rejected += 1

    return pending, stats
