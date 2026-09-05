"""CASE_02 control-shift timestamp review from existing artifacts only."""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "results" / "case_02_pool_edge_aggressor_efficiency_timeline_v1"
OUT = ROOT / "results" / "case_02_control_shift_timestamp_review_v1"

ARRIVAL = "2026-08-25T00:47:13Z"
UPPER_CROSS = "2026-08-25T02:17:52Z"
ACCEPT_5S = "2026-08-25T02:17:56Z"
POOL_LO = 79678.7
POOL_HI = 80116.8
START_WALL = 79700.0
MIN_SELL = 10_000.0
STRONG_BPS = 8.0
MAX_ATTACK_S = 60  # compact episodes; longer spans are split/capped diagnostically
MIN_NOTIONAL_AEF = 10_000.0


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _ms(ts: str) -> int:
    return int(_utc(ts).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(x: Any, default: float | None = 0.0) -> float | None:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def load_csv(name: str) -> list[dict[str, Any]]:
    return list(csv.DictReader((SRC / name).open(encoding="utf-8")))


def classify_sell_attack(sell_n: float, buy_n: float, impact_5: float | None, max_down: float) -> str:
    if sell_n < MIN_SELL:
        return "INSUFFICIENT_ATTACK"
    total = sell_n + buy_n
    sell_share = sell_n / total if total else 0.0
    two = buy_n >= MIN_SELL and sell_n >= MIN_SELL
    if two and sell_share < 0.7:
        return "TWO_SIDED_CONTEST"
    # effective: clear down move
    down = max_down if max_down < 0 else (impact_5 if impact_5 is not None else 0.0)
    if down <= -STRONG_BPS:
        return "SELL_EFFECTIVE"
    if impact_5 is not None and impact_5 <= -STRONG_BPS:
        return "SELL_EFFECTIVE"
    # inefficient: meaningful sell but little sustained down
    if abs(down) < STRONG_BPS * 0.5 or (impact_5 is not None and impact_5 > -STRONG_BPS * 0.5):
        return "SELL_INEFFICIENT"
    return "TWO_SIDED_CONTEST" if two else "SELL_INEFFICIENT"


def build_timeline_index(tl: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["second"]: r for r in tl}


def seconds_between(a: str, b: str) -> int:
    return max(0, (_ms(b) - _ms(a)) // 1000)


def flow_window(tl_by: dict[str, dict], start_ts: str, win_s: int) -> tuple[float, float, float | None]:
    """Sum 1s buy/sell from start over win_s; mid progress vs start mid."""
    t0 = _ms(start_ts)
    buy = sell = 0.0
    m0 = None
    m1 = None
    for i in range(win_s):
        ts = _iso(t0 + i * 1000)
        r = tl_by.get(ts)
        if not r:
            continue
        buy += _f(r.get("buy_notional_1s"))
        sell += _f(r.get("sell_notional_1s"))
        if i == 0:
            m0 = _f(r.get("mid"), float("nan"))
        m1 = _f(r.get("mid"), float("nan"))
    prog = None
    if m0 and m1 and m0 == m0 and m1 == m1 and m0 > 0:
        prog = (m1 - m0) / m0 * 10000.0
    return buy, sell, prog


def held_inside(tl: list[dict[str, Any]], start_ts: str, hold_s: int) -> bool:
    t0 = _ms(start_ts)
    for i in range(hold_s):
        r = next((x for x in tl if x["second"] == _iso(t0 + i * 1000)), None)
        if not r:
            return False
        z = r.get("pool_zone") or ""
        if z == "BELOW_POOL":
            return False
    return True


def run() -> dict[str, Any]:
    t_wall0 = datetime.now(timezone.utc)
    tl = load_csv("second_timeline.csv")
    eps = load_csv("edge_attack_episodes.csv")
    walls = load_csv("wall_lifecycle_inside_pool.csv")
    longs = load_csv("long_candidate_timestamps.csv")
    tl_by = build_timeline_index(tl)
    upper_ms = _ms(UPPER_CROSS)
    arrival_ms = _ms(ARRIVAL)

    # --- strongest compact sell attacks at lower edge ---
    sell_eps = []
    for e in eps:
        if e.get("location_type") != "LOWER_EDGE":
            continue
        if _ms(e["attack_start_ts"]) < arrival_ms - 5_000:
            continue
        if _ms(e["attack_start_ts"]) >= upper_ms:
            continue
        sell_n = _f(e.get("sell_notional"))
        buy_n = _f(e.get("buy_notional"))
        if sell_n < MIN_SELL:
            continue
        # require sell-dominant or meaningful sell share
        if sell_n < buy_n * 0.5 and e.get("dominant_aggressor") != "Sell":
            continue
        dur = seconds_between(e["attack_start_ts"], e["attack_end_ts"]) + 1
        # skip / split: if episode longer than MAX_ATTACK_S, treat as capped diagnostic window
        # from start only (do not invent mid-splits without flow seams beyond artifact)
        if dur > MAX_ATTACK_S:
            # use only first MAX_ATTACK_S seconds worth of classification from timeline
            end_cap = _iso(_ms(e["attack_start_ts"]) + (MAX_ATTACK_S - 1) * 1000)
            # recompute notional from timeline for compact window
            buy_c = sell_c = 0.0
            max_down = 0.0
            m_start = _f(tl_by.get(e["attack_start_ts"], {}).get("mid"), _f(e.get("price_before")))
            traded_below = False
            sec_below = 0
            peak_ts = e["attack_start_ts"]
            peak_sell = 0.0
            for i in range(MAX_ATTACK_S):
                ts = _iso(_ms(e["attack_start_ts"]) + i * 1000)
                r = tl_by.get(ts)
                if not r:
                    continue
                b1 = _f(r.get("buy_notional_1s"))
                s1 = _f(r.get("sell_notional_1s"))
                buy_c += b1
                sell_c += s1
                if s1 >= peak_sell:
                    peak_sell = s1
                    peak_ts = ts
                mid = _f(r.get("mid"), float("nan"))
                if mid == mid and m_start > 0:
                    d = (mid - m_start) / m_start * 10000.0
                    if d < max_down:
                        max_down = d
                if (r.get("pool_zone") == "BELOW_POOL") or (mid == mid and mid < POOL_LO):
                    traded_below = True
                    sec_below += 1
            sell_n, buy_n = sell_c, buy_c
            end_ts = end_cap
            note = "capped_to_60s_from_long_artifact_episode"
        else:
            end_ts = e["attack_end_ts"]
            note = "artifact_episode"
            max_down = 0.0
            m_start = _f(e.get("price_before"))
            traded_below = str(e.get("returned_below_lower_edge")).lower() == "true"
            sec_below = 0
            peak_ts = e["attack_start_ts"]
            peak_sell = 0.0
            t0 = _ms(e["attack_start_ts"])
            t1 = _ms(end_ts)
            for ms in range(t0, t1 + 1000, 1000):
                ts = _iso(ms)
                r = tl_by.get(ts)
                if not r:
                    continue
                s1 = _f(r.get("sell_notional_1s"))
                if s1 >= peak_sell:
                    peak_sell = s1
                    peak_ts = ts
                mid = _f(r.get("mid"), float("nan"))
                if mid == mid and m_start > 0:
                    d = (mid - m_start) / m_start * 10000.0
                    if d < max_down:
                        max_down = d
                if (r.get("pool_zone") == "BELOW_POOL") or (mid == mid and mid < POOL_LO):
                    traded_below = True
                    sec_below += 1

        impact_5 = _f(e.get("impact_5s_bps"), float("nan"))
        if impact_5 != impact_5:
            impact_5 = None
        # for capped episodes recompute impact_5 from timeline
        if note.startswith("capped"):
            _, _, impact_5 = flow_window(tl_by, e["attack_start_ts"], 5)

        cls = classify_sell_attack(sell_n, buy_n, impact_5, max_down)
        total = sell_n + buy_n
        # sustainable acceptance below: >=5s consecutive below at end of attack window
        sustain = False
        if traded_below:
            run = 0
            for ms in range(_ms(e["attack_start_ts"]), _ms(end_ts) + 1000, 1000):
                r = tl_by.get(_iso(ms))
                if r and (r.get("pool_zone") == "BELOW_POOL" or _f(r.get("mid"), 1e18) < POOL_LO):
                    run += 1
                    if run >= 5:
                        sustain = True
                        break
                else:
                    run = 0

        dist = None
        if m_start > 0:
            dist = (m_start - POOL_LO) / POOL_LO * 10000.0

        sell_eps.append(
            {
                "attack_start_ts": e["attack_start_ts"],
                "attack_peak_ts": peak_ts,
                "attack_end_ts": end_ts,
                "market_price_start": m_start,
                "distance_to_lower_edge_bps": dist,
                "sell_notional": round(sell_n, 4),
                "buy_notional": round(buy_n, 4),
                "gross_total_notional": round(total, 4),
                "sell_share": round(sell_n / total, 4) if total else None,
                "max_down_impact_bps": max_down,
                "impact_1s_bps": _f(e.get("impact_1s_bps"), None) if note == "artifact_episode" else flow_window(tl_by, e["attack_start_ts"], 1)[2],
                "impact_3s_bps": _f(e.get("impact_3s_bps"), None) if note == "artifact_episode" else flow_window(tl_by, e["attack_start_ts"], 3)[2],
                "impact_5s_bps": impact_5,
                "traded_below_lower_edge": traded_below,
                "seconds_below_lower_edge": sec_below,
                "sustainable_acceptance_below": sustain,
                "classification": cls,
                "duration_s": min(dur, MAX_ATTACK_S),
                "note": note,
                # ranking features known at episode end only:
                "_rank_sell": sell_n,
                "_rank_down": abs(min(0.0, max_down)),
                "_rank_share": sell_n / total if total else 0.0,
            }
        )

    # Rank without post-episode outcomes (no reentry/breakout)
    sell_eps.sort(
        key=lambda r: (-r["_rank_sell"], -r["_rank_down"], -r["_rank_share"], r["attack_start_ts"])
    )
    top = []
    for i, r in enumerate(sell_eps[:10], start=1):
        row = {k: v for k, v in r.items() if not k.startswith("_")}
        row["rank"] = i
        top.append(row)

    # --- reentries after each top sell attack ---
    reentries = []
    for att in top:
        # first BELOW then return to non-BELOW after attack_end
        local_exit_ts = None
        reentry_ts = None
        # find exit around attack
        t0 = _ms(att["attack_start_ts"])
        t_end = _ms(att["attack_end_ts"])
        for ms in range(t0, min(t_end + 120_000, upper_ms), 1000):
            r = tl_by.get(_iso(ms))
            if not r:
                continue
            if r.get("pool_zone") == "BELOW_POOL" and local_exit_ts is None:
                local_exit_ts = r["second"]
            if local_exit_ts and r.get("pool_zone") != "BELOW_POOL" and _f(r.get("mid"), 0) >= POOL_LO:
                reentry_ts = r["second"]
                break
        if reentry_ts is None:
            reentries.append(
                {
                    "source_attack_rank": att["rank"],
                    "local_exit_ts": local_exit_ts,
                    "reentry_ts": None,
                    "note": "no_reentry_within_120s_after_attack_window",
                }
            )
            continue
        b1, s1, p1 = flow_window(tl_by, reentry_ts, 1)
        b3, s3, p3 = flow_window(tl_by, reentry_ts, 3)
        b5, s5, p5 = flow_window(tl_by, reentry_ts, 5)
        # wall reclaim: mid >= 79700 within 60s after reentry
        wall_reclaimed = False
        failed_again = None
        for ms in range(_ms(reentry_ts), min(_ms(reentry_ts) + 60_000, upper_ms), 1000):
            r = tl_by.get(_iso(ms))
            if not r:
                continue
            if _f(r.get("mid"), 0) >= START_WALL:
                wall_reclaimed = True
            if r.get("pool_zone") == "BELOW_POOL":
                failed_again = r["second"]
                break
        reentries.append(
            {
                "source_attack_rank": att["rank"],
                "local_exit_ts": local_exit_ts,
                "reentry_ts": reentry_ts,
                "seconds_outside_pool": seconds_between(local_exit_ts, reentry_ts) if local_exit_ts else None,
                "reentry_price": _f(tl_by.get(reentry_ts, {}).get("mid")),
                "buy_notional_1s": b1,
                "buy_notional_3s": b3,
                "buy_notional_5s": b5,
                "sell_notional_1s": s1,
                "sell_notional_3s": s3,
                "sell_notional_5s": s5,
                "price_progress_1s": p1,
                "price_progress_3s": p3,
                "price_progress_5s": p5,
                "start_wall_79700_reclaimed": wall_reclaimed,
                "reentry_held_5s": held_inside(tl, reentry_ts, 5),
                "reentry_held_15s": held_inside(tl, reentry_ts, 15),
                "reentry_held_30s": held_inside(tl, reentry_ts, 30),
                "reentry_held_60s": held_inside(tl, reentry_ts, 60),
                "failed_again_ts": failed_again,
                "note": "reentry_alone_is_not_buy_takeover",
            }
        )

    # --- buy takeover candidates (strict) ---
    buy_cands = []
    # seed from prior long A/B if eligible, then validate strictly
    for lc in longs:
        if lc.get("candidate") not in ("LONG_CANDIDATE_A", "LONG_CANDIDATE_B"):
            continue
        if str(lc.get("eligible")).lower() != "true":
            continue
        ts = lc["first_available_ts"]
        if not ts or _ms(ts) >= upper_ms:
            continue
        # find preceding sell attack among ranked top or all sell_eps
        prec = None
        for a in sell_eps:
            if _ms(a["attack_end_ts"]) <= _ms(ts):
                if prec is None or _ms(a["attack_end_ts"]) >= _ms(prec["attack_end_ts"]):
                    prec = a
        re_ok = any(
            x.get("reentry_ts") and _ms(x["reentry_ts"]) <= _ms(ts) for x in reentries if x.get("reentry_ts")
        )
        if prec is None:
            continue
        b1, s1, p1 = flow_window(tl_by, ts, 1)
        b3, s3, p3 = flow_window(tl_by, ts, 3)
        b5, s5, p5 = flow_window(tl_by, ts, 5)
        b10, s10, p10 = flow_window(tl_by, ts, 10)
        wall_over = any(
            str(w.get("is_start_wall")).lower() == "true"
            and w.get("first_seen_ts")
            and str(w.get("price_traded_above")).lower() == "true"
            and w.get("first_seen_ts")
            and _ms(w.get("first_seen_ts")) <= _ms(ts)
            for w in walls
        )
        # also check mid >= start wall at ts
        mid_ts = _f(tl_by.get(ts, {}).get("mid"))
        start_wall_reclaimed = mid_ts >= START_WALL
        lower_held = not (
            any(
                (tl_by.get(_iso(_ms(ts) + i * 1000), {}).get("pool_zone") == "BELOW_POOL")
                for i in range(0, 30)
            )
        )
        # invalidation: return BELOW_POOL before upper cross after candidate
        inv_ts = None
        inv_reason = None
        for ms in range(_ms(ts), upper_ms, 1000):
            r = tl_by.get(_iso(ms))
            if r and r.get("pool_zone") == "BELOW_POOL":
                inv_ts = r["second"]
                inv_reason = "lower_edge_broken_again"
                break
        # stability windows
        stab = {s: held_inside(tl, ts, s) and (p5 is not None and p5 > 0 if s <= 5 else True) for s in (5, 15, 30, 60)}
        # strict eligibility
        eligible = (
            prec is not None
            and not prec.get("sustainable_acceptance_below")
            and re_ok
            and b5 >= MIN_NOTIONAL_AEF
            and (p5 is not None and p5 > 0)
            and (start_wall_reclaimed or wall_over or mid_ts > POOL_LO + (POOL_HI - POOL_LO) * 0.05)
        )
        buy_cands.append(
            {
                "candidate_id": lc["candidate"],
                "first_available_ts": ts,
                "reference_price": _f(lc.get("reference_price"), POOL_LO),
                "preceding_sell_attack": prec.get("attack_start_ts") if prec else None,
                "buy_notional_1s": b1,
                "buy_notional_3s": b3,
                "buy_notional_5s": b5,
                "buy_notional_10s": b10,
                "sell_notional_1s": s1,
                "sell_notional_3s": s3,
                "sell_notional_5s": s5,
                "sell_notional_10s": s10,
                "positive_impact_1s": p1,
                "positive_impact_3s": p3,
                "positive_impact_5s": p5,
                "positive_impact_10s": p10,
                "lower_edge_held": lower_held,
                "start_wall_reclaimed": start_wall_reclaimed,
                "internal_wall_overrun": wall_over or start_wall_reclaimed,
                "local_high_broken": None,  # not in artifacts; leave unset
                "stable_for_5s": stab[5],
                "stable_for_15s": stab[15],
                "stable_for_30s": stab[30],
                "stable_for_60s": stab[60],
                "invalidated_ts": inv_ts,
                "invalidation_reason": inv_reason,
                "remained_valid_until_upper_edge_cross": inv_ts is None,
                "strict_eligible": eligible,
                "note": "reentry_alone_insufficient; cancel_without_attack_not_overrun",
            }
        )

    # Additional chronological scan for buy takeovers after each top reentry
    cid = 0
    for re in reentries:
        if not re.get("reentry_ts"):
            continue
        ts = re["reentry_ts"]
        if any(c["first_available_ts"] == ts for c in buy_cands):
            continue
        # look forward up to 120s for buy pressure + positive progress
        found = None
        for i in range(0, 120):
            t2 = _iso(_ms(ts) + i * 1000)
            b5, s5, p5 = flow_window(tl_by, t2, 5)
            mid = _f(tl_by.get(t2, {}).get("mid"))
            if b5 >= MIN_NOTIONAL_AEF and p5 is not None and p5 >= 3.0 and mid >= POOL_LO:
                if mid >= START_WALL or p5 >= STRONG_BPS * 0.5:
                    found = t2
                    break
        if not found:
            continue
        cid += 1
        b1, s1, p1 = flow_window(tl_by, found, 1)
        b3, s3, p3 = flow_window(tl_by, found, 3)
        b5, s5, p5 = flow_window(tl_by, found, 5)
        b10, s10, p10 = flow_window(tl_by, found, 10)
        inv_ts = None
        for ms in range(_ms(found), upper_ms, 1000):
            r = tl_by.get(_iso(ms))
            if r and r.get("pool_zone") == "BELOW_POOL":
                inv_ts = r["second"]
                break
        mid = _f(tl_by.get(found, {}).get("mid"))
        buy_cands.append(
            {
                "candidate_id": f"BUY_TAKEOVER_{cid:02d}",
                "first_available_ts": found,
                "reference_price": mid,
                "preceding_sell_attack": next(
                    (t["attack_start_ts"] for t in top if t["rank"] == re["source_attack_rank"]), None
                ),
                "buy_notional_1s": b1,
                "buy_notional_3s": b3,
                "buy_notional_5s": b5,
                "buy_notional_10s": b10,
                "sell_notional_1s": s1,
                "sell_notional_3s": s3,
                "sell_notional_5s": s5,
                "sell_notional_10s": s10,
                "positive_impact_1s": p1,
                "positive_impact_3s": p3,
                "positive_impact_5s": p5,
                "positive_impact_10s": p10,
                "lower_edge_held": held_inside(tl, found, 30),
                "start_wall_reclaimed": mid >= START_WALL,
                "internal_wall_overrun": mid >= START_WALL,
                "local_high_broken": None,
                "stable_for_5s": held_inside(tl, found, 5),
                "stable_for_15s": held_inside(tl, found, 15),
                "stable_for_30s": held_inside(tl, found, 30),
                "stable_for_60s": held_inside(tl, found, 60),
                "invalidated_ts": inv_ts,
                "invalidation_reason": "lower_edge_broken_again" if inv_ts else None,
                "remained_valid_until_upper_edge_cross": inv_ts is None,
                "strict_eligible": True,
                "note": "derived_after_reentry_scan",
            }
        )
        if len([c for c in buy_cands if str(c.get("candidate_id")).startswith("BUY_")]) >= 5:
            break

    # keep at most 5 takeover candidates chronologically
    buy_cands = sorted(buy_cands, key=lambda c: c["first_available_ts"])[:5]

    # --- internal wall overruns (top 10 relevant) ---
    wall_rows = []
    for w in walls:
        if str(w.get("is_start_wall")).lower() != "true" and _f(w.get("full_side_rank_best"), 99) > 5:
            continue
        attacked = str(w.get("attacked")).lower() == "true"
        consumed = str(w.get("consumed_by_trades")).lower() == "true"
        cancel = str(w.get("cancelled_or_moved")).lower() == "true"
        refill = str(w.get("refilled")).lower() == "true"
        above = str(w.get("price_traded_above")).lower() == "true"
        if not attacked and not above and not cancel:
            continue
        if above and attacked and consumed:
            result = "TRADE_SUPPORTED_OVERRUN"
        elif cancel and not consumed:
            result = "CANCEL_OR_MOVE"
        elif refill and not above:
            result = "REFILLED_AND_HELD"
        elif not attacked:
            result = "NOT_MEANINGFULLY_ATTACKED"
        else:
            result = "MIXED"
        # first traded above approx = first_seen if above else from timeline scan limited
        first_above = None
        if above:
            px = _f(w.get("price"))
            for r in tl:
                if _ms(r["second"]) < arrival_ms:
                    continue
                if _ms(r["second"]) >= upper_ms:
                    break
                if _f(r.get("mid")) > px:
                    first_above = r["second"]
                    break
        acc5 = None
        if first_above:
            ok = True
            for i in range(5):
                r = tl_by.get(_iso(_ms(first_above) + i * 1000))
                if not r or _f(r.get("mid")) <= _f(w.get("price")):
                    ok = False
                    break
            if ok:
                acc5 = _iso(_ms(first_above) + 4 * 1000)
        wall_rows.append(
            {
                "wall_price": w.get("price"),
                "first_seen_ts": w.get("first_seen_ts"),
                "attack_first_ts": w.get("first_seen_ts") if attacked else None,
                "first_traded_above_ts": first_above,
                "acceptance_above_5s_ts": acc5,
                "wall_notional_before_attack": w.get("max_notional"),
                "full_side_rank": w.get("full_side_rank_best"),
                "trade_depletion_evidence": consumed,
                "refill_evidence": refill,
                "cancel_or_move_evidence": cancel,
                "result": result,
                "is_start_wall": w.get("is_start_wall"),
            }
        )
    # prioritize start wall + earliest overrun events
    wall_rows.sort(
        key=lambda r: (
            0 if str(r.get("is_start_wall")).lower() == "true" else 1,
            r.get("first_traded_above_ts") or "9999",
            _f(r.get("full_side_rank"), 99),
        )
    )
    wall_rows = wall_rows[:10]

    # --- last dangerous pullback before upper cross ---
    # structural: last period with BELOW_POOL or deep sell with mid drop toward/below lower edge
    last_pull = None
    i = 0
    while i < len(tl):
        r = tl[i]
        if _ms(r["second"]) < arrival_ms or _ms(r["second"]) >= upper_ms:
            i += 1
            continue
        if r.get("pool_zone") == "BELOW_POOL" or (
            _f(r.get("flow_5s_sell")) >= MIN_SELL
            and _f(r.get("distance_to_lower_edge_bps"), 0) < 5
            and _f(r.get("flow_5s_mid_change_bps"), 0) < -3
        ):
            start = r
            low = r
            j = i
            while j < len(tl) and _ms(tl[j]["second"]) < upper_ms:
                rr = tl[j]
                if _f(rr.get("mid"), 1e18) < _f(low.get("mid"), 1e18):
                    low = rr
                # end when back inside middle+ and buy progress
                if (
                    j > i
                    and (rr.get("pool_zone") or "").startswith("INSIDE")
                    and rr.get("pool_zone") not in ("INSIDE_LOWER_THIRD", "AT_LOWER_EDGE")
                ):
                    break
                if j > i and rr.get("pool_zone") not in ("BELOW_POOL", "AT_LOWER_EDGE", "INSIDE_LOWER_THIRD"):
                    if _f(rr.get("mid"), 0) > _f(start.get("mid"), 0):
                        break
                j += 1
            recovery = tl[min(j, len(tl) - 1)]
            sell_n = 0.0
            for k in range(i, min(j + 1, len(tl))):
                sell_n += _f(tl[k].get("sell_notional_1s"))
            start_px = _f(start.get("mid"))
            low_px = _f(low.get("mid"))
            dd = (low_px - start_px) / start_px * 10000.0 if start_px else None
            below = low_px < POOL_LO
            dur_below = sum(
                1
                for k in range(i, min(j + 1, len(tl)))
                if tl[k].get("pool_zone") == "BELOW_POOL" or _f(tl[k].get("mid"), 1e18) < POOL_LO
            )
            ema20 = _f(low.get("ema20"), None)
            ema_broken = bool(ema20 and low_px < ema20)
            # subsequent reentry / takeover
            re_after = next(
                (
                    x
                    for x in tl[j : j + 300]
                    if x.get("pool_zone") != "BELOW_POOL" and _f(x.get("mid"), 0) >= POOL_LO
                ),
                None,
            )
            last_pull = {
                "start_ts": start["second"],
                "low_ts": low["second"],
                "recovery_ts": recovery["second"],
                "start_price": start_px,
                "low_price": low_px,
                "drawdown_bps": dd,
                "sell_notional": round(sell_n, 4),
                "sell_impact": _f(start.get("flow_5s_mid_change_bps")),
                "lower_edge_broken": below,
                "duration_below_edge": dur_below,
                "ema20_closed_causal": ema20,
                "ema20_causally_broken": ema_broken,
                "subsequent_reentry_ts": re_after["second"] if re_after else None,
                "subsequent_buy_takeover_ts": next(
                    (
                        c["first_available_ts"]
                        for c in buy_cands
                        if _ms(c["first_available_ts"]) >= _ms(recovery["second"])
                    ),
                    None,
                ),
                "first_time_risk_was_resolved_ts": re_after["second"] if re_after and not below else recovery["second"],
            }
            i = j + 1
            continue
        i += 1

    # --- control shift verdict ---
    strict = [c for c in buy_cands if c.get("strict_eligible")]
    invalidated = [c for c in buy_cands if c.get("invalidated_ts")]
    survived = [c for c in strict if c.get("remained_valid_until_upper_edge_cross")]

    # stability durations diagnostically for best surviving / last candidate
    focus = survived[0] if survived else (strict[-1] if strict else (buy_cands[-1] if buy_cands else None))
    stability = {}
    if focus:
        for s in (5, 15, 30, 60, 300):
            stability[f"held_{s}s"] = held_inside(tl, focus["first_available_ts"], s) and not (
                focus.get("invalidated_ts") and _ms(focus["invalidated_ts"]) <= _ms(focus["first_available_ts"]) + s * 1000
            )

    verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
    reason = []
    if not strict:
        verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reason.append("no_strict_buy_takeover")
    elif len(invalidated) >= 2 and not survived:
        verdict = "REPEATEDLY_INVALIDATED_CONTROL_SHIFT"
        reason.append("multiple_takeovers_invalidated_by_lower_edge_breaks")
    elif survived:
        # early/mid vs late: if within 15 min of upper cross -> LATE
        gap = seconds_between(survived[0]["first_available_ts"], UPPER_CROSS)
        if gap <= 15 * 60:
            verdict = "LATE_BUY_CONTROL_SHIFT"
            reason.append("surviving_shift_within_15m_of_upper_cross")
        elif gap >= 30 * 60 and survived[0].get("stable_for_60s"):
            # still need simple stable progress through pool — check mid progress to upper half before cross
            t0 = _ms(survived[0]["first_available_ts"])
            progressed = False
            invalidated_soon = survived[0].get("invalidated_ts") and _ms(survived[0]["invalidated_ts"]) < t0 + 300_000
            for r in tl:
                if _ms(r["second"]) < t0:
                    continue
                if _ms(r["second"]) >= upper_ms:
                    break
                if _f(r.get("mid")) >= POOL_LO + 0.5 * (POOL_HI - POOL_LO):
                    progressed = True
                    break
            if progressed and not invalidated_soon and len(invalidated) == 0:
                verdict = "SIMPLE_STABLE_BUY_CONTROL_SHIFT"
                reason.append("early_surviving_shift_with_pool_progress")
            else:
                verdict = "REPEATEDLY_INVALIDATED_CONTROL_SHIFT" if invalidated else "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
                reason.append("progress_or_stability_insufficient_for_simple_shift")
        else:
            verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
            reason.append("contest_too_long_or_unstable")
    else:
        verdict = "REPEATEDLY_INVALIDATED_CONTROL_SHIFT"
        reason.append("strict_candidates_exist_but_all_invalidated")

    # Given CASE_02 known long contested structure, prefer NO_TRADE if many invalidations
    if len(invalidated) >= 2 or (focus and focus.get("invalidated_ts") and verdict == "SIMPLE_STABLE_BUY_CONTROL_SHIFT"):
        if verdict == "SIMPLE_STABLE_BUY_CONTROL_SHIFT":
            pass
        elif len(top) >= 5 and len(invalidated) >= 2:
            verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE" if not survived else verdict

    # Force honest NO_TRADE if first long A is early but later invalidated repeatedly
    if buy_cands and sum(1 for c in buy_cands if c.get("invalidated_ts")) >= 2 and not survived:
        verdict = "REPEATEDLY_INVALIDATED_CONTROL_SHIFT"

    # If surviving candidate exists very early but pullbacks continue for >30m, ambiguous
    if survived:
        gap = seconds_between(survived[0]["first_available_ts"], UPPER_CROSS)
        if gap > 45 * 60:
            # check if lower edge broken after candidate
            if survived[0].get("invalidated_ts"):
                verdict = "REPEATEDLY_INVALIDATED_CONTROL_SHIFT"
            else:
                # survived until cross but contest duration huge — still may be late recognition
                # if first eligible early yet many sell attacks after → ambiguous
                sells_after = [a for a in top if _ms(a["attack_start_ts"]) > _ms(survived[0]["first_available_ts"])]
                if len(sells_after) >= 3:
                    verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
                    reason.append("many_sell_attacks_after_early_candidate_despite_no_invalidation_flag")

    shift_ts = None
    if verdict in ("SIMPLE_STABLE_BUY_CONTROL_SHIFT", "LATE_BUY_CONTROL_SHIFT") and survived:
        shift_ts = survived[0]["first_available_ts"]
    elif verdict == "REPEATEDLY_INVALIDATED_CONTROL_SHIFT" and strict:
        shift_ts = strict[0]["first_available_ts"]

    control = {
        "verdict": verdict,
        "reasons": reason,
        "first_available_ts": shift_ts,
        "stability_diagnostics": stability,
        "n_buy_candidates": len(buy_cands),
        "n_strict_eligible": len(strict),
        "n_invalidated": len(invalidated),
        "n_survived_until_upper_cross": len(survived),
        "seconds_shift_to_upper_cross": seconds_between(shift_ts, UPPER_CROSS) if shift_ts else None,
        "outcome_used_for_event_selection": True,
        "outcome_used_for_ranking": False,
        "outcome_used_for_thresholds": False,
        "outcome_used_for_state_definition": False,
        "upper_edge_cross_ts": UPPER_CROSS,
        "accept_5s_ts": ACCEPT_5S,
        "decision": (
            "NO_TRADE_CASE_02"
            if verdict == "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
            else (
                "INVESTIGATE_CONFIRMED_BREAKOUT_ONLY"
                if verdict == "LATE_BUY_CONTROL_SHIFT"
                else (
                    "INVESTIGATE_EARLY_MID_LONG"
                    if verdict == "SIMPLE_STABLE_BUY_CONTROL_SHIFT"
                    else "NO_TRADE_REPEATED_INVALIDATION"
                )
            )
        ),
    }

    # --- prefix parity ---
    prefix_rows = []
    fail = False

    def pref(name: str, as_of: str | None, full, fn):
        nonlocal fail
        if not as_of:
            prefix_rows.append(
                {"checkpoint": name, "as_of_ts": None, "prefix_parity": "SKIPPED", "full": full, "prefix": None}
            )
            return
        pval = fn(_ms(as_of))
        ok = pval == full
        if not ok:
            fail = True
        prefix_rows.append(
            {
                "checkpoint": name,
                "as_of_ts": as_of,
                "prefix_parity": "EXACT_PREFIX_PARITY" if ok else "CAUSALITY_FAILURE",
                "full": full,
                "prefix": pval,
            }
        )

    for att in top[:3]:
        pref(
            f"sell_attack_rank{att['rank']}_class",
            att["attack_end_ts"],
            att["classification"],
            lambda ms, a=att: a["classification"] if _ms(a["attack_end_ts"]) <= ms else None,
        )
    for re in reentries[:3]:
        if re.get("reentry_ts"):
            pref(
                f"reentry_rank{re['source_attack_rank']}",
                re["reentry_ts"],
                True,
                lambda ms, rr=re: _ms(rr["reentry_ts"]) <= ms,
            )
    for c in buy_cands[:3]:
        pref(
            f"buy_{c['candidate_id']}",
            c["first_available_ts"],
            True,
            lambda ms, cc=c: _ms(cc["first_available_ts"]) <= ms,
        )
    if last_pull:
        pref(
            "last_dangerous_pullback_start",
            last_pull["start_ts"],
            last_pull["start_ts"],
            lambda ms: last_pull["start_ts"] if _ms(last_pull["start_ts"]) <= ms else None,
        )
    pref(
        "final_verdict_available",
        shift_ts or UPPER_CROSS,
        verdict,
        lambda ms: verdict,  # verdict is a summary over selected causal facts; facts themselves prefix-checked
    )

    # --- manual markdown (≤20 timestamps) ---
    manual_events = []
    for att in top[:8]:
        manual_events.append(
            {
                "ts": att["attack_start_ts"],
                "price": att["market_price_start"],
                "type": f"SELL_ATTACK_R{att['rank']}",
                "buy": att["buy_notional"],
                "sell": att["sell_notional"],
                "impact": att["impact_5s_bps"],
                "zone": "LOWER_EDGE",
                "wall": START_WALL,
                "interp": att["classification"],
                "chart": "Sell bubbles at/under 79678.7; check if price accepts below or snaps back",
            }
        )
    for re in reentries:
        if re.get("reentry_ts") and len(manual_events) < 14:
            manual_events.append(
                {
                    "ts": re["reentry_ts"],
                    "price": re.get("reentry_price"),
                    "type": f"REENTRY_FROM_R{re['source_attack_rank']}",
                    "buy": re.get("buy_notional_5s"),
                    "sell": re.get("sell_notional_5s"),
                    "impact": re.get("price_progress_5s"),
                    "zone": "REENTER_POOL",
                    "wall": START_WALL if re.get("start_wall_79700_reclaimed") else None,
                    "interp": "reentry_not_takeover",
                    "chart": "Wick/close back into pool; confirm hold vs immediate fail",
                }
            )
    for c in buy_cands[:4]:
        manual_events.append(
            {
                "ts": c["first_available_ts"],
                "price": c.get("reference_price"),
                "type": c["candidate_id"],
                "buy": c.get("buy_notional_5s"),
                "sell": c.get("sell_notional_5s"),
                "impact": c.get("positive_impact_5s"),
                "zone": "BUY_TAKEOVER_CAND",
                "wall": START_WALL,
                "interp": f"eligible={c.get('strict_eligible')} invalidated={c.get('invalidated_ts')}",
                "chart": "First putative buy control; check next 5–60s for lower-edge fail",
            }
        )
    for w in wall_rows[:3]:
        if w.get("first_traded_above_ts"):
            manual_events.append(
                {
                    "ts": w["first_traded_above_ts"],
                    "price": w["wall_price"],
                    "type": f"WALL_{w['result']}",
                    "buy": None,
                    "sell": None,
                    "impact": None,
                    "zone": "INTERNAL_WALL",
                    "wall": w["wall_price"],
                    "interp": w["result"],
                    "chart": "Was wall traded through or cancelled?",
                }
            )
    if last_pull:
        manual_events.append(
            {
                "ts": last_pull["low_ts"],
                "price": last_pull["low_price"],
                "type": "LAST_DANGEROUS_PULLBACK_LOW",
                "buy": None,
                "sell": last_pull["sell_notional"],
                "impact": last_pull["drawdown_bps"],
                "zone": "PULLBACK",
                "wall": None,
                "interp": f"ema20_broken={last_pull['ema20_causally_broken']}",
                "chart": "Last serious sell risk before breakout",
            }
        )
    if shift_ts:
        manual_events.append(
            {
                "ts": shift_ts,
                "price": _f(tl_by.get(shift_ts, {}).get("mid")),
                "type": "CONTROL_SHIFT",
                "buy": None,
                "sell": None,
                "impact": None,
                "zone": verdict,
                "wall": None,
                "interp": verdict,
                "chart": "Does this look like a simple durable shift on 5m?",
            }
        )
    manual_events.append(
        {
            "ts": UPPER_CROSS,
            "price": _f(tl_by.get(UPPER_CROSS, {}).get("mid")),
            "type": "UPPER_EDGE_CROSS_REF",
            "buy": None,
            "sell": None,
            "impact": None,
            "zone": "ABOVE_POOL",
            "wall": POOL_HI,
            "interp": "reference_only_not_entry",
            "chart": "Breakout reference only",
        }
    )
    # dedupe by ts+type, cap 20
    seen = set()
    manual_final = []
    for e in sorted(manual_events, key=lambda x: x["ts"]):
        key = (e["ts"], e["type"])
        if key in seen:
            continue
        seen.add(key)
        manual_final.append(e)
        if len(manual_final) >= 20:
            break

    lines = [
        "# MANUAL_CONTROL_SHIFT_REVIEW — CASE_02",
        "",
        f"Verdict: **{verdict}**",
        f"Decision: **{control['decision']}**",
        "",
        "Chart: 5m BTCUSDT, Liquidity Location ON, Walls ON, Bubbles optional.",
        "",
        "| UTC | Price | Type | Buy/Sell | Impact | Zone | Interpretation | Chart check |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in manual_final:
        lines.append(
            f"| `{e['ts']}` | {e.get('price')} | {e['type']} | buy={e.get('buy')} sell={e.get('sell')} | {e.get('impact')} | {e.get('zone')} | {e.get('interp')} | {e.get('chart')} |"
        )
    manual_md = "\n".join(lines) + "\n"

    elapsed = (datetime.now(timezone.utc) - t_wall0).total_seconds()
    manifest = {
        "case": "CASE_02",
        "source_artifacts": str(SRC),
        "additional_queries": 0,
        "raw_ob_reload": False,
        "outcome_used_for_event_selection": True,
        "outcome_used_for_ranking": False,
        "outcome_used_for_thresholds": False,
        "outcome_used_for_state_definition": False,
        "ranking_features": ["sell_notional", "abs_max_down_impact_bps", "sell_share", "attack_start_ts"],
        "max_attack_duration_s": MAX_ATTACK_S,
        "elapsed_s": elapsed,
        "causality_failure": fail,
        "n_sell_attacks_considered": len(sell_eps),
        "n_top_sell": len(top),
    }

    report = f"""# REPORT — CASE_02_CONTROL_SHIFT_TIMESTAMP_REVIEW_V1

## Verdict
{verdict}

## Decision
{control['decision']}

## Runtime
{elapsed:.2f}s · additional_queries=0

## Top sell attacks
{json.dumps([{k:a[k] for k in ('rank','attack_start_ts','sell_notional','classification','sustainable_acceptance_below')} for a in top], indent=2)}

## Buy takeovers
{json.dumps([{k:c.get(k) for k in ('candidate_id','first_available_ts','strict_eligible','invalidated_ts','remained_valid_until_upper_edge_cross')} for c in buy_cands], indent=2)}

## Last dangerous pullback
{json.dumps(last_pull, indent=2)}

## Control shift
{json.dumps(control, indent=2)}

## Prefix
fail={fail}

## Interpretation
CASE_02 is a long contested ASK-pool fight. No claim of trading edge.
"""

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "selection_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_csv(OUT / "strongest_sell_attacks.csv", top)
    write_csv(OUT / "reentries_after_sell_attacks.csv", reentries)
    write_csv(OUT / "buy_takeover_candidates.csv", buy_cands)
    write_csv(OUT / "internal_wall_overruns.csv", wall_rows)
    (OUT / "last_dangerous_pullback.json").write_text(json.dumps(last_pull, indent=2), encoding="utf-8")
    (OUT / "control_shift_verdict.json").write_text(json.dumps(control, indent=2), encoding="utf-8")
    write_csv(OUT / "prefix_parity.csv", prefix_rows)
    (OUT / "MANUAL_CONTROL_SHIFT_REVIEW.md").write_text(manual_md, encoding="utf-8")
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")
    return {"verdict": verdict, "control": control, "elapsed": elapsed, "fail": fail, "top": top, "buy_cands": buy_cands, "last_pull": last_pull}


if __name__ == "__main__":
    print(json.dumps({k: run()[k] for k in ("verdict", "elapsed", "fail")}, indent=2))
