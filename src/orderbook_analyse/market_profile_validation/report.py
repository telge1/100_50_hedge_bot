"""Aggregate events into per-hypothesis verdicts and write the report."""

from __future__ import annotations

import statistics as st
from pathlib import Path
from typing import Any, Sequence

from . import FORMAT_VERSION, H1_BROKE, H1_REJECTED, H2_CONTINUED, H2_REVERSED
from .contracts import RateEstimate, ValidationConfig
from .runner import event_date_key, event_symbol_key
from .stats import difference_bootstrap, estimate_binary_rate, estimate_rate

# A difference smaller than this is treated as no effect even if the interval
# happens to exclude zero — it would not survive costs.
MIN_MEANINGFUL_DIFF = 0.03

_GROUP_ORDER = ("ALL", "BALANCE", "TREND", "DOUBLE_DISTRIBUTION", "UNCLEAR")


def shape_group(kind: str) -> str:
    return "TREND" if kind in ("TREND_UP", "TREND_DOWN") else str(kind)


def _groups(events: Sequence) -> dict[str, list]:
    out: dict[str, list] = {"ALL": list(events)}
    for e in events:
        out.setdefault(shape_group(e.ref_shape_kind), []).append(e)
        out.setdefault(f"raw:{e.ref_shape_kind}", []).append(e)
    return out


def _excursion_summary(events: Sequence) -> dict[str, Any]:
    if not events:
        return {"n": 0}
    mfe = [float(e.mfe_frac) for e in events]
    mae = [float(e.mae_frac) for e in events]
    med_mae = st.median(mae)
    return {
        "n": len(events),
        "mfe_median": st.median(mfe),
        "mae_median": med_mae,
        "mfe_over_mae_median": (st.median(mfe) / med_mae) if med_mae > 0 else None,
    }


def _economics(events: Sequence, rate: float, cost_bps: float = 0.0) -> dict[str, Any]:
    """Is the measured hit rate enough to pay for the risk it takes?

    Reward/risk comes from the barrier geometry, so the breakeven hit rate is
    known without any assumption. `edge` below zero means the setup loses
    money even when the level "works" at the measured frequency.

    Costs matter more than they look here. A stop set at a fraction of a daily
    range is small in absolute terms, so a few basis points of round-trip fee
    can eat a large share of the risk unit — which is what the expectancy
    columns expose.
    """
    if not events:
        return {"n": 0}
    rr = [float(e.reward_risk) for e in events if e.reward_risk > 0]
    if not rr:
        return {"n": 0}
    med_rr = st.median(rr)
    breakeven = 1.0 / (1.0 + med_rr)

    # Cost expressed in units of the risk taken, per event, then pooled.
    cost_r_vals: list[float] = []
    for e in events:
        risk = abs(float(e.stop_price) - float(e.level_price))
        if risk > 0:
            cost_r_vals.append((cost_bps / 10_000.0) * float(e.level_price) / risk)
    med_cost_r = st.median(cost_r_vals) if cost_r_vals else 0.0

    gross = rate * med_rr - (1.0 - rate)
    net = gross - med_cost_r
    breakeven_net = (1.0 + med_cost_r) / (1.0 + med_rr)

    return {
        "n": len(rr),
        "reward_risk_median": med_rr,
        "breakeven_rate": breakeven,
        "observed_rate": rate,
        "edge": rate - breakeven,
        "cost_bps": cost_bps,
        "cost_in_risk_units_median": med_cost_r,
        "expectancy_r_gross": gross,
        "expectancy_r_net": net,
        "breakeven_rate_with_costs": breakeven_net,
        "edge_net": rate - breakeven_net,
    }


def _verdict(
    point: float, sym_lo: float, sym_hi: float, date_lo: float, date_hi: float
) -> str:
    """Conservative: both cluster views must agree and the effect must matter."""
    if sym_lo > 0 and date_lo > 0 and point >= MIN_MEANINGFUL_DIFF:
        return "SUPPORTED"
    if sym_hi < 0 and date_hi < 0 and point <= -MIN_MEANINGFUL_DIFF:
        return "CONTRADICTED"
    return "INCONCLUSIVE"


def _compare(a: Sequence, b: Sequence, *, flag, iters: int, seed: int) -> dict[str, Any]:
    p_sym, s_lo, s_hi = difference_bootstrap(
        a, b, flag=flag, cluster_key=event_symbol_key, iters=iters, seed=seed
    )
    p_date, d_lo, d_hi = difference_bootstrap(
        a, b, flag=flag, cluster_key=event_date_key, iters=iters, seed=seed + 7
    )
    return {
        "difference": p_sym,
        "difference_date_view": p_date,
        "cluster_symbol_ci": [s_lo, s_hi],
        "cluster_date_ci": [d_lo, d_hi],
        "verdict": _verdict(p_sym, s_lo, s_hi, d_lo, d_hi),
        "n_a": len(a),
        "n_b": len(b),
    }


def _race_block(
    events: Sequence,
    *,
    success: str,
    failure: str,
    group_a: str,
    group_b: str,
    iters: int,
    seed: int,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """Rates per shape group, economics, and the A-minus-B comparison."""
    rates: dict[str, RateEstimate] = {}
    for name, evs in _groups(events).items():
        rates[name] = estimate_rate(
            name,
            evs,
            success_outcome=success,
            failure_outcome=failure,
            symbol_key=event_symbol_key,
            date_key=event_date_key,
            iters=iters,
            seed=seed,
        )

    def resolved(kind: str) -> list:
        return [
            e
            for e in events
            if shape_group(e.ref_shape_kind) == kind and e.outcome in (success, failure)
        ]

    a, b = resolved(group_a), resolved(group_b)
    return {
        "rates": {k: v.to_dict() for k, v in rates.items()},
        "economics": {
            k: _economics(v, rates[k].rate, cost_bps)
            for k, v in _groups(events).items()
            if not k.startswith("raw:")
        },
        "excursion": {
            k: _excursion_summary(v)
            for k, v in _groups(events).items()
            if not k.startswith("raw:")
        },
        "comparison": _compare(
            a, b, flag=lambda e: e.outcome == success, iters=iters, seed=seed + 11
        ),
    }


def aggregate(
    touch_events: Sequence, revisit_events: Sequence, cfg: ValidationConfig
) -> dict[str, Any]:
    iters, seed = cfg.bootstrap_iters, cfg.seed

    h1_all = [e for e in touch_events if e.hypothesis == "H1"]
    h2_all = [e for e in touch_events if e.hypothesis == "H2"]
    h1_primary = f"margin_{cfg.edge_margin_frac:.2f}"
    h2_primary = f"unit_{cfg.poc_unit_frac:.2f}"

    h1_variants = {
        v: _race_block(
            [e for e in h1_all if e.variant == v],
            success=H1_REJECTED,
            failure=H1_BROKE,
            group_a="BALANCE",
            group_b="TREND",
            iters=iters,
            seed=seed,
            cost_bps=cfg.cost_bps,
        )
        for v in sorted({e.variant for e in h1_all})
    }
    h2_variants = {
        v: _race_block(
            [e for e in h2_all if e.variant == v],
            success=H2_CONTINUED,
            failure=H2_REVERSED,
            group_a="TREND",
            group_b="BALANCE",
            iters=iters,
            seed=seed + 3,
            cost_bps=cfg.cost_bps,
        )
        for v in sorted({e.variant for e in h2_all})
    }

    h3_horizons: dict[str, Any] = {}
    for hname, flag in (
        ("60m", lambda x: bool(x.revisited_60m)),
        ("240m", lambda x: bool(x.revisited_240m)),
        ("full_window", lambda x: bool(x.revisited)),
    ):
        rates = {
            name: estimate_binary_rate(
                name,
                evs,
                flag=flag,
                symbol_key=event_symbol_key,
                date_key=event_date_key,
                iters=iters,
                seed=seed + 5,
            ).to_dict()
            for name, evs in _groups(revisit_events).items()
        }
        bal = [e for e in revisit_events if shape_group(e.ref_shape_kind) == "BALANCE"]
        trd = [e for e in revisit_events if shape_group(e.ref_shape_kind) == "TREND"]
        h3_horizons[hname] = {
            "rates": rates,
            "comparison": _compare(bal, trd, flag=flag, iters=iters, seed=seed + 17),
        }

    h3_control = h3_distance_control(revisit_events, iters=iters, seed=seed + 23)
    h3_raw = h3_horizons["240m"]["comparison"]["verdict"]
    # A raw difference that dissolves under the distance control is not
    # evidence for the classification, so it must not be reported as such.
    if h3_raw == "SUPPORTED" and h3_control.get("verdict") == "EXPLAINED_BY_DISTANCE":
        h3_final = "CONFOUNDED_BY_DISTANCE"
    else:
        h3_final = h3_raw

    return {
        "format": FORMAT_VERSION,
        "config": cfg.to_dict(),
        "counts": {
            "touch_events": len(touch_events),
            "h1_events_primary": sum(1 for e in h1_all if e.variant == h1_primary),
            "h2_events_primary": sum(1 for e in h2_all if e.variant == h2_primary),
            "h3_windows": len(revisit_events),
        },
        "shape_mix": _shape_mix(revisit_events),
        "h1": {
            "claim": "value-area edges reject more often after a BALANCE window than after a TREND window",
            "comparison_label": "BALANCE minus TREND",
            "primary_variant": h1_primary,
            "variants": h1_variants,
        },
        "h2": {
            "claim": "after touching the reference POC, continuation in the reference direction is more likely for TREND windows than for BALANCE windows",
            "comparison_label": "TREND minus BALANCE",
            "primary_variant": h2_primary,
            "variants": h2_variants,
        },
        "h3": {
            "claim": "the reference POC is revisited sooner after a BALANCE window than after a TREND window",
            "comparison_label": "BALANCE minus TREND",
            "primary_variant": "240m",
            "variants": h3_horizons,
            "distance_control": h3_control,
            "verdict_before_control": h3_raw,
            "verdict_after_control": h3_final,
        },
    }


def h3_distance_control(
    revisit_events: Sequence, *, iters: int, seed: int
) -> dict[str, Any]:
    """Separate a magnet effect from plain geometry.

    A balance window closes near its own POC and a trend window closes far
    from it, so balance starts the next window much closer to the level it is
    supposed to be drawn back to. That alone would produce a higher revisit
    rate without any magnet at work.

    The control stratifies by how far the POC sits from the next window's open
    (in units of the reference range) and re-runs BALANCE minus TREND inside
    each stratum. If the effect is geometry, it collapses once distance is
    held roughly constant; if it survives, the class is telling us something
    the distance does not.
    """
    items = [e for e in revisit_events if shape_group(e.ref_shape_kind) in ("BALANCE", "TREND")]
    if len(items) < 40:
        return {"strata": [], "note": "too few BALANCE/TREND windows to stratify"}

    dists = sorted(abs(float(e.poc_distance_frac)) for e in items)

    def q(p: float) -> float:
        return dists[min(len(dists) - 1, int(p * (len(dists) - 1)))]

    cuts = [q(0.25), q(0.50), q(0.75)]
    edges = [(-1.0, cuts[0]), (cuts[0], cuts[1]), (cuts[1], cuts[2]), (cuts[2], float("inf"))]

    strata: list[dict[str, Any]] = []
    for lo, hi in edges:
        bucket = [e for e in items if lo < abs(float(e.poc_distance_frac)) <= hi]
        bal = [e for e in bucket if shape_group(e.ref_shape_kind) == "BALANCE"]
        trd = [e for e in bucket if shape_group(e.ref_shape_kind) == "TREND"]
        if len(bal) < 5 or len(trd) < 5:
            continue
        comp = _compare(
            bal,
            trd,
            flag=lambda e: bool(e.revisited_240m),
            iters=iters,
            seed=seed,
        )
        strata.append(
            {
                "distance_low": None if lo < 0 else lo,
                "distance_high": None if hi == float("inf") else hi,
                "n_balance": len(bal),
                "n_trend": len(trd),
                "rate_balance": sum(1 for e in bal if e.revisited_240m) / len(bal),
                "rate_trend": sum(1 for e in trd if e.revisited_240m) / len(trd),
                **comp,
            }
        )

    # Also show how different the starting distances actually are, since that
    # is the size of the confound being controlled for.
    def med_dist(kind: str) -> float | None:
        vals = [
            abs(float(e.poc_distance_frac))
            for e in items
            if shape_group(e.ref_shape_kind) == kind
        ]
        return st.median(vals) if vals else None

    surviving = [s for s in strata if s["verdict"] == "SUPPORTED"]
    return {
        "median_distance_balance": med_dist("BALANCE"),
        "median_distance_trend": med_dist("TREND"),
        "strata": strata,
        "strata_supported": len(surviving),
        "strata_total": len(strata),
        "verdict": (
            "SURVIVES_DISTANCE_CONTROL"
            if len(surviving) >= max(2, (len(strata) + 1) // 2)
            else "EXPLAINED_BY_DISTANCE"
            if strata
            else "UNTESTED"
        ),
    }


def _shape_mix(revisit_events: Sequence) -> dict[str, Any]:
    total = len(revisit_events)
    counts: dict[str, int] = {}
    for e in revisit_events:
        counts[e.ref_shape_kind] = counts.get(e.ref_shape_kind, 0) + 1
    return {
        "total_windows": total,
        "counts": counts,
        "shares": {k: (v / total if total else 0.0) for k, v in counts.items()},
    }


def _rate_row(name: str, r: dict[str, Any]) -> str:
    return (
        f"| {name} | {r['successes']}/{r['trials']} | {r['rate']:.3f} "
        f"| {r['wilson_low']:.3f}–{r['wilson_high']:.3f} "
        f"| {r['cluster_symbol_low']:.3f}–{r['cluster_symbol_high']:.3f} "
        f"| {r['cluster_date_low']:.3f}–{r['cluster_date_high']:.3f} "
        f"| {r['timeouts']} | {r['ambiguous']} |"
    )


def write_markdown(path: Path, results: dict[str, Any]) -> None:
    cfg = results["config"]
    lines: list[str] = []
    a = lines.append

    a("# Market Profile Validation V1")
    a("")
    a("Does the balance/trend classification predict anything? Each hypothesis")
    a("is set up so that a negative answer is a possible and visible outcome.")
    a("")
    a(f"- format: `{results['format']}`")
    a(f"- symbols: **{len(cfg['symbols'])}**")
    a(f"- range: `{cfg['start']}` → `{cfg['end']}` (end exclusive, UTC)")
    a(f"- anchor: `{cfg['anchor_mode']}`, reference window scored on the next window")
    a(f"- edge margin grid: {', '.join(f'{x:.2f}' for x in cfg['edge_margin_grid'])} x reference range")
    a(f"- POC barrier grid: ±{', '.join(f'{x:.2f}' for x in cfg['poc_unit_grid'])} x reference range")
    a(f"- trade scan dedupe (FINAL): `{cfg['use_final']}`")
    a(f"- bootstrap: {cfg['bootstrap_iters']} iterations, seed {cfg['seed']}")
    c = results["counts"]
    a(
        f"- events at the primary setting: H1 {c['h1_events_primary']}, "
        f"H2 {c['h2_events_primary']}, H3 {c['h3_windows']} windows"
    )
    a("")

    mix = results["shape_mix"]
    a("## Classification mix")
    a("")
    a(f"{mix['total_windows']} reference windows:")
    a("")
    a("| class | windows | share |")
    a("|---|---|---|")
    for k, v in sorted(mix["counts"].items(), key=lambda kv: -kv[1]):
        a(f"| {k} | {v} | {mix['shares'][k]:.1%} |")
    a("")

    a("## Verdicts at the primary setting")
    a("")
    a("| hypothesis | comparison | difference | symbol-clustered CI | date-clustered CI | verdict |")
    a("|---|---|---|---|---|---|")
    for key in ("h1", "h2", "h3"):
        block = results[key]
        comp = block["variants"][block["primary_variant"]]["comparison"]
        verdict = block.get("verdict_after_control", comp["verdict"])
        a(
            f"| {key.upper()} | {block['comparison_label']} | {comp['difference']:+.3f} "
            f"| {comp['cluster_symbol_ci'][0]:+.3f} … {comp['cluster_symbol_ci'][1]:+.3f} "
            f"| {comp['cluster_date_ci'][0]:+.3f} … {comp['cluster_date_ci'][1]:+.3f} "
            f"| **{verdict}** |"
        )
    a("")
    if results["h3"].get("verdict_after_control") == "CONFOUNDED_BY_DISTANCE":
        a("H3 shows a large raw difference that does not survive the distance")
        a("control below, so it is reported as confounded rather than supported.")
        a("")
    a("A verdict is `SUPPORTED` only when both cluster views exclude zero and the")
    a(f"effect is at least {MIN_MEANINGFUL_DIFF:.2f}. The Wilson columns further down assume")
    a("independent events, which correlated perpetuals are not; the clustered")
    a("intervals are the ones to act on.")
    a("")

    for key, title, note in (
        (
            "h1",
            "H1 — value-area edges",
            "Price reaches the reference VAH from below (or VAL from above). "
            "Success = returned to the reference POC before exceeding the edge "
            "by the margin.",
        ),
        (
            "h2",
            "H2 — POC as a way station",
            "Price reaches the reference POC. Success = moved one barrier unit "
            "in the reference window's direction before moving one against it. "
            "The null here is 0.500, since the barriers are symmetric.",
        ),
    ):
        block = results[key]
        primary = block["primary_variant"]
        a(f"## {title}")
        a("")
        a(f"_{block['claim']}_")
        a("")
        a(note)
        a("")
        a("### Sensitivity across barrier widths")
        a("")
        a("| setting | group | rate | reward/risk | breakeven | edge | net R/trade | difference | verdict |")
        a("|---|---|---|---|---|---|---|---|---|")
        for v, vb in block["variants"].items():
            comp = vb["comparison"]
            for g in ("ALL", "BALANCE", "TREND"):
                r = vb["rates"].get(g)
                ec = vb["economics"].get(g, {})
                if not r or r["trials"] == 0:
                    continue
                mark = " (primary)" if v == primary and g == "ALL" else ""
                diff = f"{comp['difference']:+.3f}" if g == "ALL" else ""
                verd = comp["verdict"] if g == "ALL" else ""
                a(
                    f"| `{v}`{mark} | {g} | {r['rate']:.3f} "
                    f"| {ec.get('reward_risk_median', float('nan')):.2f} "
                    f"| {ec.get('breakeven_rate', float('nan')):.3f} "
                    f"| {ec.get('edge', float('nan')):+.3f} "
                    f"| {ec.get('expectancy_r_net', float('nan')):+.3f} "
                    f"| {diff} | {verd} |"
                )
        a("")
        a("`edge` is the observed rate minus the rate this barrier geometry needs")
        a("to break even, ignoring costs. `net R/trade` is the expectancy in units")
        a(f"of the risk taken after {cfg['cost_bps']:.1f} bps of round-trip cost. Because the")
        a("stop is a fraction of a daily range, that fee is a large share of the")
        a("risk unit, so a positive `edge` can still come with a negative net R —")
        a("which is the number that decides whether a setup is worth trading.")
        a("")
        vb = block["variants"][primary]
        a(f"### Detail at `{primary}`")
        a("")
        a("| group | successes | rate | Wilson CI | by symbol | by date | timeout | ambiguous |")
        a("|---|---|---|---|---|---|---|---|")
        for g in _GROUP_ORDER:
            r = vb["rates"].get(g)
            if r and r["trials"] > 0:
                a(_rate_row(g, r))
        for name, r in sorted(vb["rates"].items()):
            if name.startswith("raw:TREND_") and r["trials"] > 0:
                a(_rate_row(name.replace("raw:", ""), r))
        a("")
        a("Excursion from the level, as a fraction of the reference range:")
        a("")
        a("| group | n | median favorable | median adverse | ratio |")
        a("|---|---|---|---|---|")
        for g in _GROUP_ORDER:
            ex = vb["excursion"].get(g)
            if ex and ex.get("n"):
                ratio = ex.get("mfe_over_mae_median")
                a(
                    f"| {g} | {ex['n']} | {ex['mfe_median']:.3f} | {ex['mae_median']:.3f} "
                    f"| {'n/a' if ratio is None else f'{ratio:.2f}'} |"
                )
        a("")
        a("The excursion table is parameter-free: it says how far price actually")
        a("travelled either way from the level, independent of any stop choice.")
        a("")

    h3 = results["h3"]
    a("## H3 — POC as a magnet")
    a("")
    a(f"_{h3['claim']}_")
    a("")
    a("Success = the reference POC was touched within the horizon. Over a full")
    a("following window the POC is touched almost every time regardless of")
    a("class, so the short horizons carry the information.")
    a("")
    a("| horizon | group | rate | by symbol | by date | difference | raw verdict |")
    a("|---|---|---|---|---|---|---|")
    for hname, hb in h3["variants"].items():
        comp = hb["comparison"]
        for g in ("ALL", "BALANCE", "TREND"):
            r = hb["rates"].get(g)
            if not r or r["trials"] == 0:
                continue
            diff = f"{comp['difference']:+.3f}" if g == "ALL" else ""
            verd = comp["verdict"] if g == "ALL" else ""
            a(
                f"| `{hname}` | {g} | {r['rate']:.3f} "
                f"| {r['cluster_symbol_low']:.3f}–{r['cluster_symbol_high']:.3f} "
                f"| {r['cluster_date_low']:.3f}–{r['cluster_date_high']:.3f} "
                f"| {diff} | {verd} |"
            )
    a("")
    a("The `raw verdict` column ignores the confound checked next.")
    a("")

    dc = h3["distance_control"]
    a("### Is this a magnet, or just a shorter distance?")
    a("")
    a("A balance window closes near its own POC; a trend window closes far from")
    a("it. Balance therefore starts the next window closer to the level it is")
    a("supposed to return to, which would raise the revisit rate on its own.")
    a("")
    if dc.get("median_distance_balance") is not None:
        a(
            f"Median distance from the next window's open to the POC: "
            f"**{dc['median_distance_balance']:.3f}** of the reference range after "
            f"BALANCE vs **{dc['median_distance_trend']:.3f}** after TREND."
        )
        a("")
    if dc.get("strata"):
        a("Same comparison at 240m, inside strata of that distance:")
        a("")
        a("| distance band | n balance | n trend | rate balance | rate trend | difference | symbol CI | verdict |")
        a("|---|---|---|---|---|---|---|---|")
        for s in dc["strata"]:
            lo = "0" if s["distance_low"] is None else f"{s['distance_low']:.3f}"
            hi = "inf" if s["distance_high"] is None else f"{s['distance_high']:.3f}"
            a(
                f"| {lo}–{hi} | {s['n_balance']} | {s['n_trend']} "
                f"| {s['rate_balance']:.3f} | {s['rate_trend']:.3f} "
                f"| {s['difference']:+.3f} "
                f"| {s['cluster_symbol_ci'][0]:+.3f} … {s['cluster_symbol_ci'][1]:+.3f} "
                f"| {s['verdict']} |"
            )
        a("")
        a(
            f"**{dc['verdict']}** — the effect holds in "
            f"{dc['strata_supported']} of {dc['strata_total']} distance bands."
        )
        a("")
        a("If it holds only in the bands where balance already starts closer, the")
        a("finding is geometry rather than a property of the classification.")
    else:
        a(f"Not tested: {dc.get('note', 'insufficient data')}.")
    a("")

    a("## Method notes")
    a("")
    a("The reference window's profile and class use only trades inside that")
    a("window. Every outcome is measured on 1m candles that open at or after the")
    a("window closed, and distances are normalised by the reference window's own")
    a("range, which is known at its close.")
    a("")
    a("The barrier race begins on the bar after the touch. The touch bar's own")
    a("extremes already reached the level and could straddle a barrier, so")
    a("including it would let the outcome leak into its own trigger.")
    a("")
    a("Bars that reach both barriers are reported as `ambiguous` rather than")
    a("ordered by guesswork. `worst_case_rate` in the JSON shows the result if")
    a("every one of them is counted as a failure.")
    a("")
    a("`MarketProfile.naked_poc` is forward-looking by construction and is never")
    a("used as an input here.")
    a("")
    a("Each level is tested on its first touch only, so one window contributes at")
    a("most one event per level and repeated pokes at the same level cannot stack")
    a("the count.")
    a("")
    a("The barrier grids mean several settings are examined per hypothesis. A")
    a("single setting reaching `SUPPORTED` while its neighbours do not is what")
    a("multiple comparisons produce on their own, so treat a verdict as real only")
    a("when the direction is stable across the grid.")
    a("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
