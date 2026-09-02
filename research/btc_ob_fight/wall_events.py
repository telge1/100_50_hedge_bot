"""OB wall observation, track, and transition facts (UNFROZEN; not trade verdicts)."""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from . import HEURISTIC_CONTRACT_VERSION
from .config import (
    WALL_QTY_MEDIAN_MULT,
    WALL_SAMPLE_INTERVAL_SECONDS,
    WALL_TRADE_MATCH_FRAC,
    iso_z,
    utc,
)
from .ob_replay import extract_walls, replay_hour_at_cutoffs

WALL_FACTS_CONTRACT = "wall_facts_v1"
BTCUSDT_TICK_SIZE = Decimal("0.1")


def _dec(x: Any) -> float:
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


def price_to_tick(price: float | Decimal) -> int:
    return int((Decimal(str(price)) / BTCUSDT_TICK_SIZE).quantize(Decimal("1")))


def tick_to_price(tick: int) -> float:
    return float(Decimal(tick) * BTCUSDT_TICK_SIZE)


def sample_ob_snapshots(
    ob_root,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    *,
    interval_seconds: int = WALL_SAMPLE_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    times: list[datetime] = []
    t = utc(window_start)
    end = utc(window_end)
    while t <= end:
        times.append(t)
        t += timedelta(seconds=interval_seconds)
    by_hour: dict[datetime, list[datetime]] = {}
    for at in times:
        hour = at.replace(minute=0, second=0, microsecond=0)
        by_hour.setdefault(hour, []).append(at)
    rows: list[dict[str, Any]] = []
    sample_index = 0
    for hour in sorted(by_hour):
        snaps = replay_hour_at_cutoffs(ob_root, symbol, hour, by_hour[hour])
        for snap in snaps:
            if not snap.get("ok"):
                rows.append(
                    {
                        "sample_index": sample_index,
                        "ts": iso_z(snap.get("ts") or hour),
                        "ok": False,
                        "error": snap.get("error"),
                    }
                )
                sample_index += 1
                continue
            walls = extract_walls(snap, max_walls=10)
            mid = _dec(snap["mid"])
            top_bid = sorted([w for w in walls if w["side"] == "BID"], key=lambda w: -float(w["notional"]))[:5]
            top_ask = sorted([w for w in walls if w["side"] == "ASK"], key=lambda w: -float(w["notional"]))[:5]
            best_bid = _dec(snap["best_bid"])
            best_ask = _dec(snap["best_ask"])
            spread_bps = (best_ask - best_bid) / mid * 10000.0 if mid > 0 else 0.0
            bids = snap.get("bids") or []
            asks = snap.get("asks") or []
            rows.append(
                {
                    "sample_index": sample_index,
                    "ts": iso_z(snap["as_of_requested"]),
                    "as_of": iso_z(snap["as_of"]),
                    "mid": mid,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread_bps": spread_bps,
                    "bid_levels": snap["bid_levels"],
                    "ask_levels": snap["ask_levels"],
                    "genuine_200": snap.get("genuine_200"),
                    "top_bid_walls": [_wall_dict(w) for w in top_bid],
                    "top_ask_walls": [_wall_dict(w) for w in top_ask],
                    "bids": [(float(p), float(q)) for p, q in bids],
                    "asks": [(float(p), float(q)) for p, q in asks],
                    "ok": True,
                }
            )
            sample_index += 1
    rows.sort(key=lambda r: r.get("ts") or "")
    for i, row in enumerate(rows):
        row["sample_index"] = i
    return rows


def compute_sample_gap_stats(ob_rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok_ts = [
        datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        for r in ob_rows
        if r.get("ok") and r.get("ts")
    ]
    if len(ok_ts) < 2:
        return {"count": len(ok_ts), "p50_seconds": None, "p95_seconds": None, "max_seconds": None}
    gaps = [(ok_ts[i] - ok_ts[i - 1]).total_seconds() for i in range(1, len(ok_ts))]
    gaps_sorted = sorted(gaps)

    def pct(p: float) -> float:
        idx = min(len(gaps_sorted) - 1, max(0, int(round(p * (len(gaps_sorted) - 1)))))
        return gaps_sorted[idx]

    return {
        "book_samples_total": len(ok_ts),
        "gap_count": len(gaps),
        "p50_seconds": pct(0.50),
        "p95_seconds": pct(0.95),
        "max_seconds": max(gaps),
        "min_seconds": min(gaps),
        "mean_seconds": sum(gaps) / len(gaps),
    }


def build_wall_fact_pipeline(
    ob_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    symbol: str,
    window_end: datetime,
    trade_match_frac: float = WALL_TRADE_MATCH_FRAC,
    heuristic_contract_version: str = HEURISTIC_CONTRACT_VERSION,
) -> dict[str, Any]:
    """Full wall fact pipeline: observations → tracks → transitions → trade matches."""
    sorted_trades = sorted(trades, key=lambda t: (t["ts"], t["trade_id"]))
    observations = _build_observations(ob_rows, symbol, heuristic_contract_version)
    gap_stats = compute_sample_gap_stats(ob_rows)
    tracks, disappearance_events = _build_tracks(
        observations, ob_rows, symbol, utc(window_end), heuristic_contract_version
    )
    used_trades: set[str] = set()
    transitions, trade_matches = _build_transitions_and_matches(
        tracks,
        observations,
        sorted_trades,
        trade_match_frac=trade_match_frac,
        heuristic_contract_version=heuristic_contract_version,
        used_trades=used_trades,
    )
    disappearance_events, disappearance_matches = _classify_disappearances(
        disappearance_events,
        sorted_trades,
        trade_match_frac=trade_match_frac,
        used_trades=used_trades,
    )
    trade_matches.extend(disappearance_matches)
    transitions.extend(disappearance_events)
    transitions.sort(key=lambda t: (t.get("current_ts") or t.get("observation_ts") or "", t.get("track_id") or ""))
    refill_sequences = _detect_refill_sequences(transitions, tracks, heuristic_contract_version)
    summary = _build_wall_summary(
        observations,
        tracks,
        transitions,
        trade_matches,
        refill_sequences,
        gap_stats,
        heuristic_contract_version,
    )
    legacy = _legacy_wall_facts_from_tracks(tracks, transitions, refill_sequences, heuristic_contract_version)
    return {
        "contract_version": WALL_FACTS_CONTRACT,
        "heuristic_contract_version": heuristic_contract_version,
        "tick_size": float(BTCUSDT_TICK_SIZE),
        "observations": observations,
        "tracks": tracks,
        "transitions": transitions,
        "trade_matches": trade_matches,
        "refill_sequences": refill_sequences,
        "summary": summary,
        "sample_gap_stats": gap_stats,
        "legacy_wall_facts": legacy,
    }


def track_wall_facts(
    ob_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    symbol: str = "BTCUSDT",
    window_end: datetime | None = None,
    trade_match_frac: float = WALL_TRADE_MATCH_FRAC,
    lookback_seconds: int = WALL_SAMPLE_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper returning legacy track rows."""
    if window_end is None:
        ok_rows = [r for r in ob_rows if r.get("ok") and r.get("ts")]
        if ok_rows:
            window_end = datetime.fromisoformat(ok_rows[-1]["ts"].replace("Z", "+00:00"))
        else:
            window_end = utc(datetime.now())
    bundle = build_wall_fact_pipeline(
        ob_rows,
        trades,
        symbol=symbol,
        window_end=window_end,
        trade_match_frac=trade_match_frac,
    )
    return bundle["legacy_wall_facts"]


def _wall_dict(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "price": _dec(w["price"]),
        "qty": _dec(w["qty"]),
        "notional": _dec(w["notional"]),
        "distance_bps": _dec(w["distance_bps"]),
        "ratio": float(w["ratio"]),
        "side": w["side"],
        "local_depth_median": _dec(w["qty"]) / float(w["ratio"]) if float(w["ratio"]) > 0 else None,
    }


def _build_observations(
    ob_rows: list[dict[str, Any]],
    symbol: str,
    heuristic_contract_version: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in ob_rows:
        if not row.get("ok"):
            continue
        mid = row["mid"]
        for side_key, side_name in (("top_ask_walls", "ASK"), ("top_bid_walls", "BID")):
            for w in row.get(side_key) or []:
                price = w["price"]
                tick = price_to_tick(price)
                median = w.get("local_depth_median")
                if median is None and w.get("ratio"):
                    median = w["qty"] / w["ratio"]
                qty = w["qty"]
                ratio = qty / median if median and median > 0 else w.get("ratio")
                signed_bps = (price - mid) / mid * 10000.0 if mid else 0.0
                out.append(
                    {
                        "observation_ts": row["ts"],
                        "symbol": symbol,
                        "side": side_name,
                        "price": price,
                        "price_tick": tick,
                        "qty": qty,
                        "local_depth_median": median,
                        "qty_to_median_ratio": ratio,
                        "distance_signed_bps": signed_bps,
                        "distance_absolute_bps": abs(signed_bps),
                        "best_bid": row.get("best_bid"),
                        "best_ask": row.get("best_ask"),
                        "spread_bps": row.get("spread_bps"),
                        "sample_index": row.get("sample_index"),
                        "source_cutoff": row["ts"],
                        "heuristic_contract_version": heuristic_contract_version,
                        "mid": mid,
                    }
                )
    out.sort(key=lambda o: (o["sample_index"], o["side"], o["price_tick"]))
    return out


def _new_track_id(side: str, price_tick: int, first_ts: str) -> str:
    slug = first_ts.replace(":", "").replace("-", "").replace(".", "").replace("Z", "")
    return f"{side.lower()}_{price_tick}_{slug}_{uuid.uuid4().hex[:8]}"


def _build_tracks(
    observations: list[dict[str, Any]],
    ob_rows: list[dict[str, Any]],
    symbol: str,
    window_end: datetime,
    heuristic_contract_version: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    obs_by_sample: dict[int, list[dict[str, Any]]] = {}
    for o in observations:
        obs_by_sample.setdefault(int(o["sample_index"]), []).append(o)

    ok_samples = sorted(
        int(r["sample_index"])
        for r in ob_rows
        if r.get("ok") and r.get("sample_index") is not None
    )
    mid_by_sample = {
        int(r["sample_index"]): r["mid"]
        for r in ob_rows
        if r.get("ok") and r.get("sample_index") is not None
    }

    active: dict[tuple[str, str, int], dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    disappearance_events: list[dict[str, Any]] = []

    def finalize_track(state: dict[str, Any], final_state: str, end_ts: str, end_mid: float | None) -> None:
        obs_list = state["observations"]
        qtys = [o["qty"] for o in obs_list]
        dists = [o["distance_absolute_bps"] for o in obs_list]
        ratios = [o["qty_to_median_ratio"] for o in obs_list if o.get("qty_to_median_ratio") is not None]
        first = obs_list[0]
        last = obs_list[-1]
        first_dt = datetime.fromisoformat(first["observation_ts"].replace("Z", "+00:00"))
        last_dt = datetime.fromisoformat(last["observation_ts"].replace("Z", "+00:00"))
        if final_state == "PRICE_MOVED_THROUGH_LEVEL":
            pass
        elif final_state == "NO_LONGER_CANDIDATE" and end_mid is not None:
            side = state["side"]
            price = state["price"]
            if side == "ASK" and end_mid > price:
                final_state = "PRICE_MOVED_THROUGH_LEVEL"
            elif side == "BID" and end_mid < price:
                final_state = "PRICE_MOVED_THROUGH_LEVEL"

        track = {
            "track_id": state["track_id"],
            "symbol": symbol,
            "side": state["side"],
            "price": state["price"],
            "price_tick": state["price_tick"],
            "first_seen_ts": first["observation_ts"],
            "last_seen_ts": last["observation_ts"],
            "observation_count": len(obs_list),
            "observed_duration_seconds": (last_dt - first_dt).total_seconds(),
            "initial_qty": qtys[0],
            "last_qty": qtys[-1],
            "min_qty": min(qtys),
            "max_qty": max(qtys),
            "mean_qty": statistics.mean(qtys),
            "max_qty_to_median_ratio": max(ratios) if ratios else None,
            "min_distance_bps": min(dists) if dists else None,
            "max_distance_bps": max(dists) if dists else None,
            "final_state": final_state,
            "heuristic_contract_version": heuristic_contract_version,
            "observations": obs_list,
        }
        completed.append(track)
        if final_state in ("NO_LONGER_CANDIDATE", "PRICE_MOVED_THROUGH_LEVEL", "DATA_GAP"):
            disappearance_events.append(
                {
                    "track_id": state["track_id"],
                    "side": state["side"],
                    "price": state["price"],
                    "price_tick": state["price_tick"],
                    "transition_type": "UNMATCHED_DISAPPEARANCE",
                    "previous_ts": last["observation_ts"],
                    "current_ts": end_ts,
                    "previous_qty": last["qty"],
                    "current_qty": 0.0,
                    "qty_delta": -last["qty"],
                    "qty_added": 0.0,
                    "qty_reduced": last["qty"],
                    "final_state": final_state,
                    "trades_at_level_between_samples": 0,
                    "matching_aggressor_qty": 0.0,
                    "matching_aggressor_notional": 0.0,
                    "heuristic_contract_version": heuristic_contract_version,
                }
            )

    prev_sample: int | None = None
    for sample_index in ok_samples:
        present = obs_by_sample.get(sample_index, [])
        current_keys = {(symbol, o["side"], o["price_tick"]) for o in present}
        end_mid = mid_by_sample.get(sample_index)
        end_ts = next(r["ts"] for r in ob_rows if r.get("sample_index") == sample_index and r.get("ok"))

        if prev_sample is not None and sample_index - prev_sample > 1:
            for key, state in list(active.items()):
                finalize_track(state, "DATA_GAP", end_ts, end_mid)
                del active[key]

        for o in present:
            key = (symbol, o["side"], o["price_tick"])
            if key not in active:
                active[key] = {
                    "track_id": _new_track_id(o["side"], o["price_tick"], o["observation_ts"]),
                    "side": o["side"],
                    "price": o["price"],
                    "price_tick": o["price_tick"],
                    "observations": [o],
                }
            else:
                active[key]["observations"].append(o)

        for key, state in list(active.items()):
            if key not in current_keys:
                finalize_track(state, "NO_LONGER_CANDIDATE", end_ts, end_mid)
                del active[key]

        prev_sample = sample_index

    window_end_iso = iso_z(window_end)
    for state in active.values():
        finalize_track(state, "STILL_VISIBLE_AT_WINDOW_END", window_end_iso, None)

    completed.sort(key=lambda t: (t["side"], t["price_tick"], t["first_seen_ts"]))
    return completed, disappearance_events


def _trades_for_level(
    trades: list[dict[str, Any]],
    *,
    price_tick: int,
    side: str,
    start_ts: datetime,
    end_ts: datetime,
    used: set[str],
) -> list[dict[str, Any]]:
    target_price = tick_to_price(price_tick)
    want_side = "Buy" if side == "ASK" else "Sell"
    out = []
    for t in trades:
        if t["trade_id"] in used:
            continue
        if t["ts"] <= start_ts or t["ts"] > end_ts:
            continue
        if t["side"] != want_side:
            continue
        if price_to_tick(t["price"]) != price_tick:
            continue
        out.append(t)
    return out


def _build_transitions_and_matches(
    tracks: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    trade_match_frac: float,
    heuristic_contract_version: str,
    used_trades: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transitions: list[dict[str, Any]] = []
    trade_matches: list[dict[str, Any]] = []

    for track in tracks:
        obs_list = track.get("observations") or []
        for i in range(1, len(obs_list)):
            prev_o, cur_o = obs_list[i - 1], obs_list[i]
            prev_ts = datetime.fromisoformat(prev_o["observation_ts"].replace("Z", "+00:00"))
            cur_ts = datetime.fromisoformat(cur_o["observation_ts"].replace("Z", "+00:00"))
            prev_qty = prev_o["qty"]
            cur_qty = cur_o["qty"]
            dq = cur_qty - prev_qty
            qty_added = max(0.0, dq)
            qty_reduced = max(0.0, -dq)

            matched = _trades_for_level(
                trades,
                price_tick=track["price_tick"],
                side=track["side"],
                start_ts=prev_ts,
                end_ts=cur_ts,
                used=used_trades,
            )
            agg_qty = sum(t["size"] for t in matched)
            agg_notional = sum(t["notional"] for t in matched)
            for t in matched:
                used_trades.add(t["trade_id"])
                trade_matches.append(
                    {
                        "track_id": track["track_id"],
                        "trade_id": t["trade_id"],
                        "trade_ts": iso_z(t["ts"]),
                        "side": track["side"],
                        "aggressor_side": t["side"],
                        "price": t["price"],
                        "price_tick": price_to_tick(t["price"]),
                        "size": t["size"],
                        "notional": t["notional"],
                        "previous_ts": prev_o["observation_ts"],
                        "current_ts": cur_o["observation_ts"],
                    }
                )

            if dq > 1e-9:
                ttype = "QTY_INCREASE_OBSERVED"
            elif dq < -1e-9:
                if agg_qty >= abs(dq) * trade_match_frac:
                    ttype = "TRADE_ASSOCIATED_QTY_DECREASE"
                else:
                    ttype = "UNMATCHED_QTY_DECREASE"
            else:
                continue

            transitions.append(
                {
                    "track_id": track["track_id"],
                    "side": track["side"],
                    "price": track["price"],
                    "price_tick": track["price_tick"],
                    "transition_type": ttype,
                    "previous_ts": prev_o["observation_ts"],
                    "current_ts": cur_o["observation_ts"],
                    "previous_qty": prev_qty,
                    "current_qty": cur_qty,
                    "qty_delta": dq,
                    "qty_added": qty_added,
                    "qty_reduced": qty_reduced,
                    "trades_at_level_between_samples": len(matched),
                    "matching_aggressor_qty": agg_qty,
                    "matching_aggressor_notional": agg_notional,
                    "heuristic_contract_version": heuristic_contract_version,
                }
            )

    return transitions, trade_matches


def _classify_disappearances(
    events: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    trade_match_frac: float,
    used_trades: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for ev in events:
        prev_ts = datetime.fromisoformat(ev["previous_ts"].replace("Z", "+00:00"))
        cur_ts = datetime.fromisoformat(ev["current_ts"].replace("Z", "+00:00"))
        matched = _trades_for_level(
            trades,
            price_tick=ev["price_tick"],
            side=ev["side"],
            start_ts=prev_ts,
            end_ts=cur_ts,
            used=used_trades,
        )
        agg_qty = sum(t["size"] for t in matched)
        agg_notional = sum(t["notional"] for t in matched)
        for t in matched:
            used_trades.add(t["trade_id"])
            matches.append(
                {
                    "track_id": ev["track_id"],
                    "trade_id": t["trade_id"],
                    "trade_ts": iso_z(t["ts"]),
                    "side": ev["side"],
                    "aggressor_side": t["side"],
                    "price": t["price"],
                    "price_tick": price_to_tick(t["price"]),
                    "size": t["size"],
                    "notional": t["notional"],
                    "previous_ts": ev["previous_ts"],
                    "current_ts": ev["current_ts"],
                }
            )
        ev = dict(ev)
        ev["trades_at_level_between_samples"] = len(matched)
        ev["matching_aggressor_qty"] = agg_qty
        ev["matching_aggressor_notional"] = agg_notional
        if agg_qty >= ev["qty_reduced"] * trade_match_frac:
            ev["transition_type"] = "TRADE_ASSOCIATED_DISAPPEARANCE"
        else:
            ev["transition_type"] = "UNMATCHED_DISAPPEARANCE"
        out.append(ev)
    return out, matches


def _detect_refill_sequences(
    transitions: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
    heuristic_contract_version: str,
) -> list[dict[str, Any]]:
    decreases = [
        t
        for t in transitions
        if t["transition_type"] in ("TRADE_ASSOCIATED_QTY_DECREASE", "UNMATCHED_QTY_DECREASE")
    ]
    increases = [t for t in transitions if t["transition_type"] == "QTY_INCREASE_OBSERVED"]
    track_by_id = {t["track_id"]: t for t in tracks}
    sequences: list[dict[str, Any]] = []

    for dec in decreases:
        dec_ts = datetime.fromisoformat(dec["current_ts"].replace("Z", "+00:00"))
        for inc in increases:
            if inc["side"] != dec["side"] or inc["price_tick"] != dec["price_tick"]:
                continue
            inc_ts = datetime.fromisoformat(inc["current_ts"].replace("Z", "+00:00"))
            if inc_ts <= dec_ts:
                continue
            same_track = inc["track_id"] == dec["track_id"]
            sequences.append(
                {
                    "side": dec["side"],
                    "price": dec["price"],
                    "price_tick": dec["price_tick"],
                    "reduction_ts": dec["current_ts"],
                    "reduction_qty": dec["qty_reduced"],
                    "reduction_transition_type": dec["transition_type"],
                    "subsequent_increase_ts": inc["current_ts"],
                    "subsequent_increase_qty": inc["qty_added"],
                    "seconds_between": (inc_ts - dec_ts).total_seconds(),
                    "same_track": same_track,
                    "reduction_track_id": dec["track_id"],
                    "increase_track_id": inc["track_id"],
                    "heuristic_label": "HEURISTIC_REFILL_SEQUENCE",
                    "heuristic_contract_version": heuristic_contract_version,
                    "status": "UNFROZEN_HEURISTIC",
                }
            )
    sequences.sort(key=lambda s: (s["reduction_ts"], s["subsequent_increase_ts"]))
    return sequences


def _build_wall_summary(
    observations: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    trade_matches: list[dict[str, Any]],
    refill_sequences: list[dict[str, Any]],
    gap_stats: dict[str, Any],
    heuristic_contract_version: str,
) -> dict[str, Any]:
    ask_obs = [o for o in observations if o["side"] == "ASK"]
    bid_obs = [o for o in observations if o["side"] == "BID"]
    ask_tracks = [t for t in tracks if t["side"] == "ASK"]
    bid_tracks = [t for t in tracks if t["side"] == "BID"]

    def count_trans(ttype: str, side: str) -> int:
        return sum(1 for t in transitions if t["transition_type"] == ttype and t["side"] == side)

    def count_refill(side: str) -> int:
        return sum(1 for s in refill_sequences if s["side"] == side)

    ask_levels = {t["price_tick"] for t in ask_tracks}
    bid_levels = {t["price_tick"] for t in bid_tracks}

    top_ratio = sorted(tracks, key=lambda t: t.get("max_qty_to_median_ratio") or 0, reverse=True)[:5]
    top_duration = sorted(tracks, key=lambda t: t.get("observed_duration_seconds") or 0, reverse=True)[:5]
    track_notional: dict[str, float] = {}
    for m in trade_matches:
        track_notional[m["track_id"]] = track_notional.get(m["track_id"], 0.0) + m["notional"]
    top_trade = sorted(tracks, key=lambda t: track_notional.get(t["track_id"], 0.0), reverse=True)[:5]

    return {
        "contract_version": WALL_FACTS_CONTRACT,
        "heuristic_contract_version": heuristic_contract_version,
        "status": "UNFROZEN_HEURISTIC",
        "book_samples_total": gap_stats.get("book_samples_total"),
        "sample_gap_p50_seconds": gap_stats.get("p50_seconds"),
        "sample_gap_p95_seconds": gap_stats.get("p95_seconds"),
        "sample_gap_max_seconds": gap_stats.get("max_seconds"),
        "wall_observations_total": len(observations),
        "ask_wall_observations": len(ask_obs),
        "bid_wall_observations": len(bid_obs),
        "unique_wall_tracks": len(tracks),
        "ask_wall_tracks": len(ask_tracks),
        "bid_wall_tracks": len(bid_tracks),
        "unique_wall_price_levels": len(ask_levels | bid_levels),
        "ask_unique_wall_price_levels": len(ask_levels),
        "bid_unique_wall_price_levels": len(bid_levels),
        "qty_decreases": {
            "ask": count_trans("TRADE_ASSOCIATED_QTY_DECREASE", "ASK") + count_trans("UNMATCHED_QTY_DECREASE", "ASK"),
            "bid": count_trans("TRADE_ASSOCIATED_QTY_DECREASE", "BID") + count_trans("UNMATCHED_QTY_DECREASE", "BID"),
        },
        "trade_associated_decreases": {
            "ask": count_trans("TRADE_ASSOCIATED_QTY_DECREASE", "ASK"),
            "bid": count_trans("TRADE_ASSOCIATED_QTY_DECREASE", "BID"),
        },
        "unmatched_decreases": {
            "ask": count_trans("UNMATCHED_QTY_DECREASE", "ASK"),
            "bid": count_trans("UNMATCHED_QTY_DECREASE", "BID"),
        },
        "trade_associated_disappearances": {
            "ask": sum(
                1
                for t in transitions
                if t["transition_type"] == "TRADE_ASSOCIATED_DISAPPEARANCE" and t["side"] == "ASK"
            ),
            "bid": sum(
                1
                for t in transitions
                if t["transition_type"] == "TRADE_ASSOCIATED_DISAPPEARANCE" and t["side"] == "BID"
            ),
        },
        "unmatched_disappearances": {
            "ask": sum(
                1 for t in transitions if t["transition_type"] == "UNMATCHED_DISAPPEARANCE" and t["side"] == "ASK"
            ),
            "bid": sum(
                1 for t in transitions if t["transition_type"] == "UNMATCHED_DISAPPEARANCE" and t["side"] == "BID"
            ),
        },
        "refill_sequences_heuristic": {
            "ask": count_refill("ASK"),
            "bid": count_refill("BID"),
        },
        "tracks_visible_at_window_end": {
            "ask": sum(1 for t in ask_tracks if t["final_state"] == "STILL_VISIBLE_AT_WINDOW_END"),
            "bid": sum(1 for t in bid_tracks if t["final_state"] == "STILL_VISIBLE_AT_WINDOW_END"),
        },
        "top_by_max_qty_to_median_ratio": [
            {
                "track_id": t["track_id"],
                "side": t["side"],
                "price": t["price"],
                "max_qty_to_median_ratio": t.get("max_qty_to_median_ratio"),
            }
            for t in top_ratio
        ],
        "top_by_observed_duration_seconds": [
            {
                "track_id": t["track_id"],
                "side": t["side"],
                "price": t["price"],
                "observed_duration_seconds": t.get("observed_duration_seconds"),
            }
            for t in top_duration
        ],
        "top_by_matching_aggressor_notional": [
            {
                "track_id": t["track_id"],
                "side": t["side"],
                "price": t["price"],
                "matching_aggressor_notional": track_notional.get(t["track_id"], 0.0),
            }
            for t in top_trade
        ],
        "trade_match_count": len(trade_matches),
        "transition_count": len(transitions),
    }


def _legacy_wall_facts_from_tracks(
    tracks: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    refill_sequences: list[dict[str, Any]],
    heuristic_contract_version: str,
) -> list[dict[str, Any]]:
    """Map tracks + transitions to legacy wall_facts rows for compatibility."""
    events_by_track: dict[str, list[dict[str, Any]]] = {}
    for tr in transitions:
        if tr["transition_type"] == "QTY_INCREASE_OBSERVED":
            events_by_track.setdefault(tr["track_id"], []).append(
                {
                    "ts": tr["current_ts"],
                    "code": "HEURISTIC_WALL_REFILLED_OR_ADDED",
                    "added_qty": tr["qty_added"],
                    "reduced_qty": None,
                }
            )
        elif tr["transition_type"] == "TRADE_ASSOCIATED_QTY_DECREASE":
            events_by_track.setdefault(tr["track_id"], []).append(
                {
                    "ts": tr["current_ts"],
                    "code": "HEURISTIC_TRADE_BACKED_REDUCTION",
                    "added_qty": None,
                    "reduced_qty": tr["qty_reduced"],
                }
            )
        elif tr["transition_type"] == "UNMATCHED_QTY_DECREASE":
            events_by_track.setdefault(tr["track_id"], []).append(
                {
                    "ts": tr["current_ts"],
                    "code": "HEURISTIC_WALL_PULLED_OR_CANCELLED",
                    "added_qty": None,
                    "reduced_qty": tr["qty_reduced"],
                }
            )

    track_notional: dict[str, float] = {}
    for tr in transitions:
        track_notional[tr["track_id"]] = track_notional.get(tr["track_id"], 0.0) + tr.get("matching_aggressor_notional", 0.0)

    facts = []
    for t in tracks:
        tid = t["track_id"]
        facts.append(
            {
                "track_id": tid,
                "side": t["side"],
                "price": t["price"],
                "price_tick": t["price_tick"],
                "initial_qty": t["initial_qty"],
                "max_qty": t["max_qty"],
                "final_qty": t["last_qty"],
                "distance_bps": t.get("min_distance_bps"),
                "persistence_seconds": t.get("observed_duration_seconds"),
                "added_qty": max(0.0, t["last_qty"] - t["initial_qty"]),
                "reduced_qty": max(0.0, t["initial_qty"] - t["last_qty"]),
                "matched_trade_qty": track_notional.get(tid, 0.0) / t["price"] if t["price"] else 0.0,
                "matched_trade_notional": track_notional.get(tid, 0.0),
                "refill_count": sum(
                    1 for e in events_by_track.get(tid, []) if e["code"] == "HEURISTIC_WALL_REFILLED_OR_ADDED"
                ),
                "pull_count": sum(
                    1 for e in events_by_track.get(tid, []) if e["code"] == "HEURISTIC_WALL_PULLED_OR_CANCELLED"
                ),
                "crossed_by_price": t["final_state"] == "PRICE_MOVED_THROUGH_LEVEL",
                "first_ts": t["first_seen_ts"],
                "last_ts": t["last_seen_ts"],
                "final_state": t["final_state"],
                "observation_count": t["observation_count"],
                "heuristic_contract": "UNFROZEN_HEURISTIC",
                "heuristic_events": events_by_track.get(tid, []),
            }
        )
    facts.sort(key=lambda x: (x["side"], x["price"]))
    return facts
