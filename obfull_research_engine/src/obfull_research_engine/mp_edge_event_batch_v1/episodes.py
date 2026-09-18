"""Deterministic episode / selection-policy layer (analytical; does not drop raw events)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from obfull_research_engine.mp_edge_event_study_v1.util import NS, stable_hash
from obfull_research_engine.mp_edge_event_study_v2.schema import EventV2, OutcomeV2


POLICIES = (
    "ALL_VALID_EVENTS",
    "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION",
    "COOLDOWN_5M",
    "COOLDOWN_15M",
    "COOLDOWN_30M",
    "NON_OVERLAPPING_30M_OUTCOMES",
)


@dataclass
class EpisodeRow:
    event_id: str
    window_id: str
    zone_id: str
    profile_version_ids: str
    episode_id: str
    seconds_since_previous_same_zone_event: float | None
    overlapping_outcome_count: int
    is_first_touch_of_zone_version: bool
    is_first_signal_of_episode: bool
    selected_cooldown_5m: bool
    selected_cooldown_15m: bool
    selected_cooldown_30m: bool
    selected_all_valid: bool = True
    selected_first_touch_zone_version: bool = False
    selected_non_overlapping_30m: bool = False

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def _profile_version_key(ev: EventV2) -> str:
    return "|".join(sorted(ev.active_profile_ids))


def _episode_id(window_id: str, zone_id: str, profile_version: str, first_touch_ns: int) -> str:
    return "ep_" + stable_hash([window_id, zone_id, profile_version, first_touch_ns], n=16)


def build_episode_rows(
    events: Sequence[EventV2],
    outcomes: Sequence[OutcomeV2],
    *,
    window_id: str,
) -> list[EpisodeRow]:
    """Chronological, causal selection — no future outcomes used for cooldowns."""
    ordered = sorted(events, key=lambda e: (e.first_touch_ts_ns, e.event_id))
    # outcomes overlapping: count other events whose [touch, touch+1800s] overlaps
    rows: list[EpisodeRow] = []
    last_zone_touch: dict[str, int] = {}
    last_zone_version_seen: set[str] = set()
    last_selected_5m: dict[str, int] = {}
    last_selected_15m: dict[str, int] = {}
    last_selected_30m: dict[str, int] = {}
    last_nonoverlap_end: dict[str, int] = {}

    for ev in ordered:
        zkey = ev.zone_id
        pv = _profile_version_key(ev)
        zv = f"{zkey}::{pv}"
        prev = last_zone_touch.get(zkey)
        secs = None if prev is None else (ev.first_touch_ts_ns - prev) / NS
        last_zone_touch[zkey] = ev.first_touch_ts_ns

        is_first_zv = zv not in last_zone_version_seen
        last_zone_version_seen.add(zv)

        # overlapping outcome count vs other events
        horizon = 1800 * NS
        my_end = ev.first_touch_ts_ns + horizon
        overlap = 0
        for other in ordered:
            if other.event_id == ev.event_id:
                continue
            o_end = other.first_touch_ts_ns + horizon
            if other.first_touch_ts_ns < my_end and ev.first_touch_ts_ns < o_end:
                overlap += 1

        rows.append(
            EpisodeRow(
                event_id=ev.event_id,
                window_id=window_id,
                zone_id=ev.zone_id,
                profile_version_ids=pv,
                episode_id="",  # fill second pass
                seconds_since_previous_same_zone_event=secs,
                overlapping_outcome_count=overlap,
                is_first_touch_of_zone_version=is_first_zv,
                is_first_signal_of_episode=False,
                selected_cooldown_5m=False,
                selected_cooldown_15m=False,
                selected_cooldown_30m=False,
                selected_first_touch_zone_version=is_first_zv,
            )
        )

    # second pass: episode ids + cooldowns (causal)
    last_zv_touch: dict[str, tuple[int, str]] = {}
    for row, ev in zip(rows, ordered):
        zv = f"{row.zone_id}::{row.profile_version_ids}"
        prev = last_zv_touch.get(zv)
        if prev is not None and (ev.first_touch_ts_ns - prev[0]) / NS <= 1800:
            row.episode_id = prev[1]
            row.is_first_signal_of_episode = False
        else:
            row.episode_id = _episode_id(
                window_id, row.zone_id, row.profile_version_ids, ev.first_touch_ts_ns
            )
            row.is_first_signal_of_episode = True
        last_zv_touch[zv] = (ev.first_touch_ts_ns, row.episode_id)

        # cooldowns by zone_id (chronological)
        for label, store, cd_s in (
            ("5m", last_selected_5m, 300),
            ("15m", last_selected_15m, 900),
            ("30m", last_selected_30m, 1800),
        ):
            last = store.get(row.zone_id)
            ok = last is None or (ev.first_touch_ts_ns - last) / NS >= cd_s
            if ok and ev.label_price_only != "UNRESOLVED":
                if label == "5m":
                    row.selected_cooldown_5m = True
                elif label == "15m":
                    row.selected_cooldown_15m = True
                else:
                    row.selected_cooldown_30m = True
                store[row.zone_id] = ev.first_touch_ts_ns

        # non-overlapping 30m outcomes: by trade_side globally chronological
        if ev.label_price_only != "UNRESOLVED" and ev.trade_side:
            key = ev.trade_side
            last_end = last_nonoverlap_end.get(key)
            if last_end is None or ev.first_touch_ts_ns >= last_end:
                row.selected_non_overlapping_30m = True
                # outcome horizon 1800s from trigger if present else touch
                start = ev.trigger_ts_ns or ev.first_touch_ts_ns
                last_nonoverlap_end[key] = start + 1800 * NS

    return rows


def policy_event_ids(rows: Sequence[EpisodeRow], policy: str) -> set[str]:
    if policy == "ALL_VALID_EVENTS":
        return {r.event_id for r in rows}
    if policy == "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION":
        return {r.event_id for r in rows if r.selected_first_touch_zone_version}
    if policy == "COOLDOWN_5M":
        return {r.event_id for r in rows if r.selected_cooldown_5m}
    if policy == "COOLDOWN_15M":
        return {r.event_id for r in rows if r.selected_cooldown_15m}
    if policy == "COOLDOWN_30M":
        return {r.event_id for r in rows if r.selected_cooldown_30m}
    if policy == "NON_OVERLAPPING_30M_OUTCOMES":
        return {r.event_id for r in rows if r.selected_non_overlapping_30m}
    raise ValueError(policy)
