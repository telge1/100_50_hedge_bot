"""Summary statistics with Wilson intervals and block bootstrap by episode."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from typing import Any, Iterable, Sequence

from .episodes import POLICIES, EpisodeRow, policy_event_ids
from .outcomes_ext import OutcomeExt


def _median(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return float(s[mid - 1] + s[mid]) / 2.0


def _mean(xs: Sequence[float]) -> float | None:
    return None if not xs else float(sum(xs) / len(xs))


def _std(xs: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (centre - adj) / denom, (centre + adj) / denom


def sample_size_flag(n: int) -> str:
    if n <= 0:
        return "NO_CONCLUSION"
    if n < 10:
        return "VERY_LOW_SAMPLE"
    if n < 30:
        return "LOW_SAMPLE"
    return "OK"


def block_bootstrap_mean_ci(
    blocks: Sequence[Sequence[float]],
    *,
    n_boot: int = 500,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float | None, float | None, float | None]:
    """Bootstrap CI using episode/day blocks — not individual overlapping 100ms events."""
    blocks = [list(b) for b in blocks if b]
    if not blocks:
        return None, None, None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        sample: list[float] = []
        for _b in range(len(blocks)):
            sample.extend(rng.choice(blocks))
        if sample:
            means.append(sum(sample) / len(sample))
    if not means:
        return None, None, None
    means.sort()
    lo = means[int(alpha / 2 * len(means))]
    hi = means[int((1 - alpha / 2) * len(means)) - 1]
    return _mean(means), lo, hi


def summarize_group(
    outcomes: Sequence[OutcomeExt],
    *,
    horizon_s: int = 1800,
    pair: str = "tp30_sl25",
    episode_blocks: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    subset = [o for o in outcomes if o.horizon_s == horizon_s]
    uncens = [o for o in subset if o.outcome_status == "OK" and not o.is_censored]
    decidable = []
    tp = sl = neither = amb = 0
    for o in uncens:
        v = (o.tp_sl_results or {}).get(pair, "NEITHER")
        if v == "TP":
            tp += 1
            decidable.append(o)
        elif v == "SL":
            sl += 1
            decidable.append(o)
        elif v == "AMBIGUOUS":
            amb += 1
        else:
            neither += 1
    n_dec = tp + sl
    gross = [float(o.gross_return_bps) for o in uncens if o.gross_return_bps is not None]
    net8 = [float(o.net_return_bps_8) for o in uncens if o.net_return_bps_8 is not None]
    net12 = [float(o.net_return_bps_12) for o in uncens if o.net_return_bps_12 is not None]
    mfe = [float(o.mfe_bps_gross) for o in uncens if o.mfe_bps_gross is not None]
    mae = [float(o.mae_bps_gross) for o in uncens if o.mae_bps_gross is not None]

    tp_rate_dec = (tp / n_dec) if n_dec else None
    tp_rate_all = (tp / len(uncens)) if uncens else None
    w_lo, w_hi = wilson_interval(tp, n_dec) if n_dec else (None, None)

    # profit factor on gross among decidable TP/SL only (diagnostic)
    wins = [float(o.gross_return_bps or 0) for o in decidable if (o.tp_sl_results or {}).get(pair) == "TP"]
    losses = [abs(float(o.gross_return_bps or 0)) for o in decidable if (o.tp_sl_results or {}).get(pair) == "SL"]
    pf = None
    if losses and sum(losses) > 0:
        pf = sum(wins) / sum(losses)
    elif wins and not losses:
        pf = float("inf")

    boot_mean = boot_lo = boot_hi = None
    if episode_blocks:
        boot_mean, boot_lo, boot_hi = block_bootstrap_mean_ci(list(episode_blocks.values()))

    return {
        "event_count": len(subset),
        "uncensored_count": len(uncens),
        "tp_count": tp,
        "sl_count": sl,
        "neither_count": neither,
        "ambiguous_count": amb,
        "tp_first_rate_decidable": tp_rate_dec,
        "tp_first_rate_uncensored": tp_rate_all,
        "wilson_lo": w_lo,
        "wilson_hi": w_hi,
        "median_mfe": _median(mfe),
        "median_mae": _median(mae),
        "mean_gross_return": _mean(gross),
        "median_gross_return": _median(gross),
        "mean_net_return_8bps": _mean(net8),
        "mean_net_return_12bps": _mean(net12),
        "median_net_return_8bps": _median(net8),
        "positive_net_rate_8bps": (
            sum(1 for x in net8 if x > 0) / len(net8) if net8 else None
        ),
        "std_gross": _std(gross),
        "se_gross": (_std(gross) / math.sqrt(len(gross))) if gross and _std(gross) is not None else None,
        "profit_factor_gross_tpsl": pf,
        "bootstrap_mean_gross": boot_mean,
        "bootstrap_lo": boot_lo,
        "bootstrap_hi": boot_hi,
        "bootstrap_unit": "episode_id",
        "sample_flag": sample_size_flag(len(uncens)),
        "horizon_s": horizon_s,
        "tpsl_pair": pair,
    }


def build_summaries(
    outcomes: Sequence[OutcomeExt],
    episodes: Sequence[EpisodeRow],
    *,
    horizon_s: int = 1800,
) -> dict[str, list[dict[str, Any]]]:
    ep_by_event = {e.event_id: e for e in episodes}
    out_by_id = defaultdict(list)
    for o in outcomes:
        if o.horizon_s == horizon_s and o.outcome_status == "OK" and o.gross_return_bps is not None:
            ep = ep_by_event.get(o.event_id)
            key = ep.episode_id if ep else o.event_id
            out_by_id[key].append(float(o.gross_return_bps))

    def group_key(o: OutcomeExt, kind: str) -> str:
        if kind == "confluence":
            return o.confluence_class
        if kind == "label":
            return o.label_price_only
        if kind == "trade_side":
            return o.trade_side or "NONE"
        if kind == "session":
            return o.session_utc
        if kind == "role":
            return o.event_role
        raise ValueError(kind)

    results: dict[str, list[dict[str, Any]]] = {}
    for kind, name in (
        ("confluence", "summary_by_confluence"),
        ("label", "summary_by_label"),
        ("trade_side", "summary_by_trade_side"),
        ("session", "summary_by_session"),
        ("role", "summary_by_role"),
    ):
        buckets: dict[str, list[OutcomeExt]] = defaultdict(list)
        for o in outcomes:
            buckets[group_key(o, kind)].append(o)
        rows = []
        for g, outs in sorted(buckets.items()):
            # episode blocks restricted to group
            blocks = {}
            for o in outs:
                if o.horizon_s != horizon_s or o.gross_return_bps is None:
                    continue
                ep = ep_by_event.get(o.event_id)
                key = ep.episode_id if ep else o.event_id
                blocks.setdefault(key, []).append(float(o.gross_return_bps))
            s = summarize_group(outs, horizon_s=horizon_s, episode_blocks=blocks)
            s["group"] = g
            rows.append(s)
        results[name] = rows

    # selection policies
    pol_rows = []
    for pol in POLICIES:
        ids = policy_event_ids(episodes, pol)
        outs = [o for o in outcomes if o.event_id in ids]
        blocks = {}
        for o in outs:
            if o.horizon_s != horizon_s or o.gross_return_bps is None:
                continue
            ep = ep_by_event.get(o.event_id)
            key = ep.episode_id if ep else o.event_id
            blocks.setdefault(key, []).append(float(o.gross_return_bps))
        s = summarize_group(outs, horizon_s=horizon_s, episode_blocks=blocks)
        s["group"] = pol
        pol_rows.append(s)
    results["summary_by_selection_policy"] = pol_rows

    # cost scenario summary at 1800s
    cost_rows = []
    for cost, field in ((0, "net_return_bps_0"), (8, "net_return_bps_8"), (12, "net_return_bps_12")):
        vals = [
            float(getattr(o, field))
            for o in outcomes
            if o.horizon_s == horizon_s
            and o.outcome_status == "OK"
            and getattr(o, field) is not None
        ]
        cost_rows.append(
            {
                "roundtrip_cost_bps": cost,
                "n": len(vals),
                "mean_net": _mean(vals),
                "median_net": _median(vals),
                "positive_rate": (sum(1 for v in vals if v > 0) / len(vals)) if vals else None,
                "sample_flag": sample_size_flag(len(vals)),
            }
        )
    results["cost_scenario_summary"] = cost_rows
    return results
