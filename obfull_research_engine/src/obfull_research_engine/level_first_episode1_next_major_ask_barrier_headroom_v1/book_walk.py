"""Single-pass causal book snapshots at multiple decision times (epoch-isolated)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import (
    _apply_change,
    _apply_reset,
    _as_dt,
    merge_book_events,
    normalize_book_resets,
)
from ..level_first_episode1_corrected_sms1_persist_v1.reader import replay_payload_from_tables
from ..timeparse import format_utc_z
from . import EPOCH4, EPOCH4_COVERAGE_END, EPOCH5_CHECKPOINT


def load_payload(persist_dir: Path) -> dict[str, Any]:
    return replay_payload_from_tables(Path(persist_dir))


def snapshots_at_decisions(
    payload: dict[str, Any],
    *,
    decision_times: list[str],
    require_epoch: int = EPOCH4,
    coverage_end: str = EPOCH4_COVERAGE_END,
) -> dict[str, dict[str, Any]]:
    """Walk book once; return state for each decision_time (event_time < decision).

    Decisions after coverage_end or requiring epoch bridge are marked censored.
    """
    cov_end = _as_dt(coverage_end)
    ep5 = _as_dt(EPOCH5_CHECKPOINT)
    wanted_map: dict[datetime, str] = {}
    for t in decision_times:
        wanted_map[_as_dt(t)] = format_utc_z(_as_dt(t))
    wanted = sorted(wanted_map.items(), key=lambda x: x[0])
    resets = normalize_book_resets(payload.get("book_resets"), None)
    events = merge_book_events(payload["level_changes"], resets)
    bids = dict(payload["initial_bids"])
    asks = dict(payload["initial_asks"])
    epoch = payload.get("initial_replay_epoch")
    last_et = None
    ev_i = 0
    n_ev = len(events)
    out: dict[str, dict[str, Any]] = {}

    for dt, label in wanted:
        if dt > cov_end + timedelta(milliseconds=50):
            out[label] = {
                "censored": True,
                "censor_reason": "CENSORED_BY_EPOCH_BOUNDARY",
                "decision_time": label,
            }
            continue
        if dt >= ep5:
            out[label] = {
                "censored": True,
                "censor_reason": "CENSORED_BY_EPOCH_BOUNDARY",
                "decision_time": label,
            }
            continue
        while ev_i < n_ev:
            et, _o, _k, _i, kind, pev = events[ev_i]
            if et >= dt:
                break
            if kind == "reset":
                _apply_reset(bids, asks, pev)
            else:
                _apply_change(bids, asks, pev)
            if pev.get("replay_epoch") is not None:
                epoch = int(pev["replay_epoch"])
            last_et = et
            ev_i += 1
        if require_epoch is not None and epoch is not None and int(epoch) != int(require_epoch):
            out[label] = {
                "censored": True,
                "censor_reason": "CENSORED_BY_EPOCH_BOUNDARY",
                "decision_time": label,
                "replay_epoch": int(epoch),
            }
            continue
        bb = max(bids) if bids else None
        ba = min(asks) if asks else None
        # microprice if sizes available
        micro = None
        if bb is not None and ba is not None:
            bq = float(bids.get(bb) or 0.0)
            aq = float(asks.get(ba) or 0.0)
            if bq + aq > 0:
                micro = (ba * bq + bb * aq) / (bq + aq)
        out[label] = {
            "censored": False,
            "decision_time": label,
            "bids": dict(bids),
            "asks": dict(asks),
            "best_bid": bb,
            "best_ask": ba,
            "microprice": micro,
            "replay_epoch": int(epoch) if epoch is not None else None,
            "last_event_time": format_utc_z(last_et) if last_et else None,
            "max_input_available_at": format_utc_z(last_et) if last_et else None,
            "n_ask_levels": len(asks),
            "n_bid_levels": len(bids),
            "ask_max_price": max(asks) if asks else None,
            "full_depth_coverage": bool(asks) and (max(asks) if asks else 0) >= (ba or 0) * 1.01,
        }
    return out
