"""Matched controls, descriptive outcomes, cost context."""

from __future__ import annotations

import bisect
import random
from collections import defaultdict
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery import COST_BPS, DECISION_CUTOFFS_S, OUTCOME_HORIZONS_S
from orderbook_analyse.l2_wall_attack_discovery.models import safe_float
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _sample_at(samples: list[SampleRow], ts_ms: int) -> SampleRow | None:
    if not samples:
        return None
    lo, hi = 0, len(samples) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= ts_ms:
            ans = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def match_controls(
    episodes: list[dict[str, Any]],
    samples_by_symbol: dict[str, list[SampleRow]],
    *,
    seed: int = 42,
    per_event: int = 2,
    max_events: int = 500,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fast matched controls: contact-core exclusion + wall-distance preference.

    Caps matched events at max_events (stratified by symbol/side) for tractability
    when attacks are dense. Deterministic; no outcome look-ahead.
    """
    rng = random.Random(seed)
    primaries = [e for e in episodes if e.get("is_primary") and e.get("first_contact_at") is not None]
    # stratify then cap
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in primaries:
        by_key[(e["symbol"], e["side"])].append(e)
    selected: list[dict[str, Any]] = []
    keys = sorted(by_key)
    if keys:
        per_bucket = max(1, max_events // max(len(keys), 1))
        for k in keys:
            bucket = by_key[k]
            if len(bucket) <= per_bucket:
                selected.extend(bucket)
            else:
                selected.extend(rng.sample(bucket, per_bucket))
    selected = selected[:max_events]

    cores: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for e in primaries:
        c = int(e["first_contact_at"])
        cores[e["symbol"]].append((c, c + 10_000))

    def _merge(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not iv:
            return []
        iv = sorted(iv)
        out = [[iv[0][0], iv[0][1]]]
        for a, b in iv[1:]:
            if a <= out[-1][1] + 1:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return [(a, b) for a, b in out]

    merged = {sym: _merge(v) for sym, v in cores.items()}

    def _in_core(sym: str, ts: int) -> bool:
        for a, b in merged.get(sym, []):
            if a <= ts <= b:
                return True
            if a > ts:
                break
        return False

    # index pools once: hour -> list of sample indices into non_core
    non_core: dict[str, list[SampleRow]] = {}
    hour_idx: dict[str, dict[int, list[int]]] = {}
    for sym, samples in samples_by_symbol.items():
        nc = [s for s in samples if (not s.warmup) and (not _in_core(sym, s.ts_ms))]
        non_core[sym] = nc
        hi: dict[int, list[int]] = defaultdict(list)
        for i, s in enumerate(nc):
            hi[(s.ts_ms // 3_600_000) % 24].append(i)
        hour_idx[sym] = hi

    controls: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    used: dict[str, set[int]] = defaultdict(set)
    cid = 0
    for e in selected:
        sym = e["symbol"]
        nc = non_core.get(sym, [])
        hour = (int(e["first_contact_at"]) // 3_600_000) % 24
        cand_i = [i for i in hour_idx.get(sym, {}).get(hour, []) if nc[i].ts_ms not in used[sym]]
        mq = "HOUR_NON_CORE"
        if len(cand_i) < per_event:
            cand_i = [i for i in range(len(nc)) if nc[i].ts_ms not in used[sym]]
            mq = "FALLBACK_NON_CORE"
        if not cand_i:
            quality.append({"attack_id": e["attack_id"], "n_controls": 0, "match_quality": "NONE", "pool_size": 0})
            continue
        picks_i = rng.sample(cand_i, k=min(per_event, len(cand_i)))
        for i in picks_i:
            s = nc[i]
            used[sym].add(s.ts_ms)
            cid += 1
            controls.append(
                {
                    "control_id": f"ctrl_wad_{seed}_{cid}",
                    "matched_to_attack_id": e["attack_id"],
                    "symbol": sym,
                    "side": e["side"],
                    "direction": e["direction"],
                    "entry_at": s.ts_ms,
                    "mid": s.mid,
                    "spread_bps": s.spread_bps,
                    "imbalance_l10": s.imbalance_l10,
                    "is_control": True,
                    "match_quality": mq,
                }
            )
        quality.append(
            {
                "attack_id": e["attack_id"],
                "n_controls": len(picks_i),
                "match_quality": mq,
                "pool_size": len(cand_i),
            }
        )
    return controls, quality



def compute_outcomes(
    episodes: list[dict[str, Any]],
    samples_by_symbol: dict[str, list[SampleRow]],
    *,
    is_control: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ts_index: dict[str, list[int]] = {
        sym: [s.ts_ms for s in samples] for sym, samples in samples_by_symbol.items()
    }
    for e in episodes:
        fc = e.get("first_contact_at") if not is_control else e.get("entry_at")
        if fc is None:
            continue
        samples = samples_by_symbol.get(e["symbol"], [])
        tss = ts_index.get(e["symbol"], [])
        side = e["side"]
        for cut in DECISION_CUTOFFS_S:
            t0 = int(fc) + cut * 1000
            s0 = _sample_at(samples, t0)
            mid0 = safe_float(s0.mid) if s0 else None
            if mid0 is None or mid0 <= 0:
                continue
            i0 = bisect.bisect_right(tss, t0)
            for h in OUTCOME_HORIZONS_S:
                t1 = t0 + h * 1000
                i1 = bisect.bisect_right(tss, t1)
                path = samples[i0:i1]
                complete = len(path) >= max(1, h // 2)
                if not path:
                    out.append(
                        {
                            "event_id": e.get("attack_id") or e.get("control_id"),
                            "is_control": is_control,
                            "decision_cutoff_s": cut,
                            "horizon_s": h,
                            "symbol": e["symbol"],
                            "side": side,
                            "forward_return_bps": None,
                            "mfe_bps": None,
                            "mae_bps": None,
                            "time_to_mfe_s": None,
                            "horizon_complete": False,
                            "semantic_role": "outcome",
                        }
                    )
                    continue
                mids = [safe_float(s.mid) for s in path]
                mids = [m for m in mids if m is not None]
                if not mids:
                    continue
                if side == "BID":
                    rets = [(mid0 - m) / mid0 * 10000 for m in mids]
                    fwd = (mid0 - mids[-1]) / mid0 * 10000
                else:
                    rets = [(m - mid0) / mid0 * 10000 for m in mids]
                    fwd = (mids[-1] - mid0) / mid0 * 10000
                out.append(
                    {
                        "event_id": e.get("attack_id") or e.get("control_id"),
                        "is_control": is_control,
                        "decision_cutoff_s": cut,
                        "horizon_s": h,
                        "symbol": e["symbol"],
                        "side": side,
                        "forward_return_bps": fwd,
                        "mfe_bps": max(rets),
                        "mae_bps": min(rets),
                        "time_to_mfe_s": None,
                        "horizon_complete": bool(complete),
                        "semantic_role": "outcome",
                    }
                )
    return out


def event_vs_control(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for cut in (0, 3, 5):
        for h in (10, 60):
            for sym in ("BTCUSDT", "DOGEUSDT"):
                for side in ("BID", "ASK"):
                    for is_c in (False, True):
                        vals = [
                            float(r["forward_return_bps"])
                            for r in outcomes
                            if r["decision_cutoff_s"] == cut
                            and r["horizon_s"] == h
                            and r["symbol"] == sym
                            and r["side"] == side
                            and r["is_control"] is is_c
                            and r.get("horizon_complete")
                            and r.get("forward_return_bps") is not None
                        ]
                        if not vals:
                            continue
                        rows.append(
                            {
                                "decision_cutoff_s": cut,
                                "horizon_s": h,
                                "symbol": sym,
                                "side": side,
                                "is_control": is_c,
                                "n": len(vals),
                                "mean_fwd_bps": sum(vals) / len(vals),
                                "median_fwd_bps": sorted(vals)[len(vals) // 2],
                            }
                        )
    return rows


def cost_context(outcomes: list[dict[str, Any]], labels_60: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for cost in COST_BPS:
        for cls in sorted(set(labels_60.values()) | {"CONTROL"}):
            vals = []
            for r in outcomes:
                if r["decision_cutoff_s"] != 3 or r["horizon_s"] != 60:
                    continue
                if not r.get("horizon_complete") or r.get("mfe_bps") is None:
                    continue
                if cls == "CONTROL":
                    if not r["is_control"]:
                        continue
                else:
                    if r["is_control"] or labels_60.get(r["event_id"]) != cls:
                        continue
                vals.append(float(r["mfe_bps"]))
            if not vals:
                continue
            rows.append(
                {
                    "resolution_class": cls,
                    "cost_bps": cost,
                    "n": len(vals),
                    "share_mfe_gt_cost": sum(1 for v in vals if v > cost) / len(vals),
                    "median_mfe_bps": sorted(vals)[len(vals) // 2],
                    "note": "descriptive cost context only; not optimized",
                }
            )
    return rows
