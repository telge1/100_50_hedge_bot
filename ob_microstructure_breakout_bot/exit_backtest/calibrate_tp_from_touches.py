"""Phase C: calibrate TP cluster thresholds from EMA59-touch analysis events.

Mirrors the DOGE OB calibration style:
  - labeled working vs early-fail touches
  - quantile proposals per side
  - small grid search + chronological walk-forward
  - suggested rule dump (JSON + markdown), not applied live yet
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


@dataclass(frozen=True)
class TpRule:
    """When to project TP to a heavy cluster beyond near noise."""

    near_noise_max_dist_pct: float
    min_target_dist_pct: float
    max_target_dist_pct: float
    min_cluster_strength_sum: float
    min_abs_confirm_delta: float
    min_ob_ratio_long: float  # bid/ask
    max_ob_ratio_short: float  # bid/ask upper bound for shorts (ask-heavy)
    require_ob: bool = True


@dataclass(frozen=True)
class RuleScore:
    n_eligible: int
    n_projected: int
    hit_rate: float | None
    mean_exc_when_projected: float | None
    false_project_rate: float | None  # projected but never reached target
    coverage: float  # projected / eligible
    score: float


def _q(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = int(round((len(ys) - 1) * p))
    return float(ys[max(0, min(len(ys) - 1, i))])


def _round_nice(x: float) -> float:
    if x >= 100_000:
        return float(round(x / 10_000) * 10_000)
    if x >= 10_000:
        return float(round(x / 1_000) * 1_000)
    if x >= 1:
        return float(round(x, 2))
    return float(round(x, 3))


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_touches(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [t for t in data.get("touches", []) if not t.get("error")]


def label_touch(t: dict[str, Any]) -> str:
    exc = float(t.get("max_exc_vs_ema59_pct") or 0.0)
    strongest = t.get("strongest_cluster") or {}
    if exc < 0.5:
        return "failed_early"
    if strongest.get("reached") and exc >= 1.0:
        return "working"
    if exc >= 1.0:
        return "working_partial"
    return "weak"


def pick_tp_candidate(
    t: dict[str, Any],
    rule: TpRule,
) -> dict[str, Any] | None:
    """Heaviest cluster in the allowed distance band above noise floor."""
    cands = []
    for c in t.get("clusters") or []:
        dist = abs(float(c.get("dist_from_entry_pct") or 0.0))
        mass = float(c.get("strength_sum") or 0.0)
        if dist < rule.min_target_dist_pct:
            continue
        if dist > rule.max_target_dist_pct:
            continue
        if mass < rule.min_cluster_strength_sum:
            continue
        cands.append({**c, "dist_from_entry_pct": dist})
    if not cands:
        return None
    return max(cands, key=lambda c: float(c.get("strength_sum") or 0.0))


def flow_allows_projection(t: dict[str, Any], rule: TpRule) -> bool:
    side = str(t.get("side") or "long")
    abs_delta = abs(float(t.get("confirm_delta") or 0.0))
    if abs_delta < rule.min_abs_confirm_delta:
        return False
    ob = t.get("touch_ob")
    if ob is None:
        return not rule.require_ob
    ob = float(ob)
    if side == "long":
        return ob >= rule.min_ob_ratio_long
    # short: want ask-heavy book → low bid/ask
    return ob <= rule.max_ob_ratio_short


def score_rule(touches: list[dict[str, Any]], rule: TpRule) -> RuleScore:
    eligible = 0
    projected = 0
    hits = 0
    false_proj = 0
    exc_sum = 0.0
    for t in touches:
        # Eligible if there is any cluster beyond near-noise band
        has_beyond = any(
            abs(float(c.get("dist_from_entry_pct") or 0)) >= rule.min_target_dist_pct
            for c in (t.get("clusters") or [])
        )
        if not has_beyond:
            continue
        eligible += 1
        if not flow_allows_projection(t, rule):
            continue
        cand = pick_tp_candidate(t, rule)
        if cand is None:
            continue
        projected += 1
        exc_sum += float(t.get("max_exc_vs_ema59_pct") or 0.0)
        if cand.get("reached"):
            hits += 1
        else:
            false_proj += 1

    hit_rate = (hits / projected) if projected else None
    false_rate = (false_proj / projected) if projected else None
    mean_exc = (exc_sum / projected) if projected else None
    coverage = (projected / eligible) if eligible else 0.0
    # Score: prefer high hit rate with enough coverage; penalize false projects.
    if projected < 5 or hit_rate is None:
        score = -1.0
    else:
        score = float(hit_rate) * 0.7 + coverage * 0.2 - (false_rate or 0.0) * 0.1
        if projected >= 10:
            score += 0.05
    return RuleScore(
        n_eligible=eligible,
        n_projected=projected,
        hit_rate=hit_rate,
        mean_exc_when_projected=mean_exc,
        false_project_rate=false_rate,
        coverage=coverage,
        score=score,
    )


def quantile_proposal(touches: list[dict[str, Any]], side: str) -> dict[str, Any]:
    working = [t for t in touches if label_touch(t) in {"working", "working_partial"}]
    failed = [t for t in touches if label_touch(t) == "failed_early"]

    # Reversal / heaviest-reached mass among working
    rev_mass = [
        float((t.get("reversal_cluster") or {}).get("strength_sum") or 0)
        for t in working
        if t.get("reversal_cluster")
    ]
    rev_dist = [
        abs(float((t.get("reversal_cluster") or {}).get("dist_from_entry_pct") or 0))
        for t in working
        if t.get("reversal_cluster")
    ]
    heavy_mass = [
        float((t.get("heaviest_reached_cluster") or {}).get("strength_sum") or 0)
        for t in working
        if t.get("heaviest_reached_cluster")
    ]
    heavy_dist = [
        abs(float((t.get("heaviest_reached_cluster") or {}).get("dist_from_entry_pct") or 0))
        for t in working
        if t.get("heaviest_reached_cluster")
    ]
    deltas_w = [abs(float(t.get("confirm_delta") or 0)) for t in working]
    deltas_f = [abs(float(t.get("confirm_delta") or 0)) for t in failed]
    obs_w = [float(t["touch_ob"]) for t in working if t.get("touch_ob") is not None]
    obs_f = [float(t["touch_ob"]) for t in failed if t.get("touch_ob") is not None]

    # Near noise: high hit rate clusters below this dist among all touches
    near_dists = []
    for t in touches:
        for c in t.get("clusters") or []:
            if c.get("reached") and float(c.get("strength_sum") or 0) < 4:
                near_dists.append(abs(float(c.get("dist_from_entry_pct") or 0)))

    near_noise_max_dist_pct = _clamp(_q(near_dists, 0.75) or 0.8, 0.5, 1.0)
    # Target band starts at meaningful separation (~0.8%), not necessarily above near_noise.
    min_target = _clamp(_q(heavy_dist, 0.25) or 0.8, 0.8, 1.2)
    max_target = _clamp(_q(rev_dist, 0.90) or 2.5, 2.0, 3.5)
    min_mass = _clamp(_q(heavy_mass, 0.25) or 4.0, 4.0, 12.0)

    prop = {
        "side": side,
        "n_working": len(working),
        "n_failed": len(failed),
        "near_noise_max_dist_pct": _round_nice(near_noise_max_dist_pct),
        "min_target_dist_pct": _round_nice(min_target),
        "max_target_dist_pct": _round_nice(max_target),
        "min_cluster_strength_sum": _round_nice(min_mass),
        "typical_reversal_strength_sum": _round_nice(
            max(min_mass, _clamp(_q(rev_mass, 0.50) or 8.0, 4.0, 20.0))
        ),
        "min_abs_confirm_delta": _round_nice(
            _clamp(_q(deltas_w, 0.25) or 100_000, 50_000, 300_000)
        ),
        "failed_abs_confirm_delta_p75": _round_nice(_q(deltas_f, 0.75) or 0),
    }
    if side == "long":
        prop["min_ob_ratio"] = _round_nice(_clamp(_q(obs_w, 0.25) or 1.05, 1.0, 1.5))
        prop["failed_ob_p75"] = _round_nice(_q(obs_f, 0.75) or 1.0)
    else:
        # shorts: lower bid/ask is more ask-heavy
        prop["max_ob_ratio"] = _round_nice(_clamp(_q(obs_w, 0.75) or 1.0, 0.7, 1.05))
        prop["failed_ob_p25"] = _round_nice(_q(obs_f, 0.25) or 1.2)
    return prop


def _clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


def grid_for_side(prop: dict[str, Any], side: str) -> list[TpRule]:
    near = float(prop.get("near_noise_max_dist_pct") or 0.8)
    min_d_opts = sorted(
        {
            round(x, 2)
            for x in (0.8, 1.0, float(prop.get("min_target_dist_pct") or 0.8))
            if x >= 0.6
        }
    )
    max_d_opts = sorted(
        {
            round(x, 2)
            for x in (2.0, 2.5, 3.0, float(prop.get("max_target_dist_pct") or 2.5))
            if x >= 1.5
        }
    )
    mass_opts = sorted(
        {
            round(x, 2)
            for x in (
                4.0,
                6.0,
                8.0,
                float(prop.get("min_cluster_strength_sum") or 4.0),
                float(prop.get("typical_reversal_strength_sum") or 8.0),
            )
            if x >= 3.0
        }
    )
    delta_opts = sorted(
        {
            round(x, -3)
            for x in (
                100_000,
                150_000,
                200_000,
                float(prop.get("min_abs_confirm_delta") or 100_000),
            )
            if x >= 50_000
        }
    )
    if side == "long":
        ob_opts = sorted(
            {
                round(x, 2)
                for x in (1.05, 1.10, 1.20, float(prop.get("min_ob_ratio") or 1.05))
                if x >= 1.0
            }
        )
        short_ob_opts = [1.0]
    else:
        ob_opts = [1.05]
        short_ob_opts = sorted(
            {
                round(x, 2)
                for x in (0.90, 1.00, 1.05, float(prop.get("max_ob_ratio") or 1.0))
                if 0.5 <= x <= 1.1
            }
        )

    rules: list[TpRule] = []
    if side == "long":
        for min_d, max_d, mass, delta, ob in itertools.product(
            min_d_opts, max_d_opts, mass_opts, delta_opts, ob_opts
        ):
            if min_d >= max_d:
                continue
            if min_d < near * 0.75:
                continue
            rules.append(
                TpRule(
                    near_noise_max_dist_pct=near,
                    min_target_dist_pct=float(min_d),
                    max_target_dist_pct=float(max_d),
                    min_cluster_strength_sum=float(mass),
                    min_abs_confirm_delta=float(delta),
                    min_ob_ratio_long=float(ob),
                    max_ob_ratio_short=1.0,
                    require_ob=True,
                )
            )
    else:
        for min_d, max_d, mass, delta, sob in itertools.product(
            min_d_opts, max_d_opts, mass_opts, delta_opts, short_ob_opts
        ):
            if min_d >= max_d:
                continue
            rules.append(
                TpRule(
                    near_noise_max_dist_pct=near,
                    min_target_dist_pct=float(min_d),
                    max_target_dist_pct=float(max_d),
                    min_cluster_strength_sum=float(mass),
                    min_abs_confirm_delta=float(delta),
                    min_ob_ratio_long=1.05,
                    max_ob_ratio_short=float(sob),
                    require_ob=True,
                )
            )
    # Cap grid size
    if len(rules) > 400:
        step = len(rules) // 400 + 1
        rules = rules[::step]
    return rules


def walk_forward_split(
    touches: list[dict[str, Any]], train_frac: float = 0.7
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(touches, key=lambda t: _parse_ts(str(t["touch_ts"])))
    cut = max(1, int(len(ordered) * train_frac))
    return ordered[:cut], ordered[cut:]


def calibrate_side(touches: list[dict[str, Any]], side: str) -> dict[str, Any]:
    side_rows = [t for t in touches if t.get("side") == side]
    prop = quantile_proposal(side_rows, side)
    train, test = walk_forward_split(side_rows)
    rules = grid_for_side(prop, side)

    scored = []
    for rule in rules:
        tr = score_rule(train, rule)
        te = score_rule(test, rule)
        # Prefer rules with enough projections on both folds.
        if tr.n_projected < 5:
            continue
        fold_penalty = 0.0 if te.n_projected >= 3 else -0.35
        scored.append(
            {
                "rule": asdict(rule),
                "train": asdict(tr),
                "test": asdict(te),
                "combined_score": tr.score * 0.55 + te.score * 0.45 + fold_penalty,
            }
        )
    scored.sort(key=lambda x: x["combined_score"], reverse=True)

    # Quantile-derived rule as robust fallback / primary suggestion
    if side == "long":
        q_rule = TpRule(
            near_noise_max_dist_pct=float(prop["near_noise_max_dist_pct"]),
            min_target_dist_pct=float(prop["min_target_dist_pct"]),
            max_target_dist_pct=float(prop["max_target_dist_pct"]),
            min_cluster_strength_sum=float(prop["min_cluster_strength_sum"]),
            min_abs_confirm_delta=float(prop["min_abs_confirm_delta"]),
            min_ob_ratio_long=float(prop["min_ob_ratio"]),
            max_ob_ratio_short=1.0,
            require_ob=True,
        )
    else:
        q_rule = TpRule(
            near_noise_max_dist_pct=float(prop["near_noise_max_dist_pct"]),
            min_target_dist_pct=float(prop["min_target_dist_pct"]),
            max_target_dist_pct=float(prop["max_target_dist_pct"]),
            min_cluster_strength_sum=float(prop["min_cluster_strength_sum"]),
            min_abs_confirm_delta=float(prop["min_abs_confirm_delta"]),
            min_ob_ratio_long=1.05,
            max_ob_ratio_short=float(prop["max_ob_ratio"]),
            require_ob=True,
        )
    q_full = score_rule(side_rows, q_rule)
    q_train = score_rule(train, q_rule)
    q_test = score_rule(test, q_rule)
    quantile_rule_block = {
        "rule": asdict(q_rule),
        "full": asdict(q_full),
        "train": asdict(q_train),
        "test": asdict(q_test),
        "combined_score": q_train.score * 0.55 + q_test.score * 0.45,
    }

    best = scored[0] if scored else quantile_rule_block
    # If grid best is fragile on test, prefer quantile rule.
    if scored and (scored[0].get("test") or {}).get("n_projected", 0) < 3:
        best = quantile_rule_block

    # Baseline: project to heaviest cluster anywhere with dist>=0.8, mass>=4, no flow filter
    baseline = TpRule(
        near_noise_max_dist_pct=0.8,
        min_target_dist_pct=0.8,
        max_target_dist_pct=2.5,
        min_cluster_strength_sum=4.0,
        min_abs_confirm_delta=0.0,
        min_ob_ratio_long=0.0,
        max_ob_ratio_short=99.0,
        require_ob=False,
    )
    return {
        "side": side,
        "n": len(side_rows),
        "n_train": len(train),
        "n_test": len(test),
        "label_counts": {
            "working": sum(1 for t in side_rows if label_touch(t) == "working"),
            "working_partial": sum(
                1 for t in side_rows if label_touch(t) == "working_partial"
            ),
            "failed_early": sum(1 for t in side_rows if label_touch(t) == "failed_early"),
            "weak": sum(1 for t in side_rows if label_touch(t) == "weak"),
        },
        "quantile_proposal": prop,
        "quantile_rule": quantile_rule_block,
        "baseline_full": asdict(score_rule(side_rows, baseline)),
        "best_rule": best,
        "top5": scored[:5],
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# TP Cluster Threshold Calibration (Phase C)",
        "",
        f"Input: `{payload.get('input')}`",
        f"Symbol: `{payload.get('symbol')}`",
        f"n touches: **{payload.get('n_touches')}**",
        "",
        "Same idea as DOGE OB calibration Phase C: quantiles + grid + walk-forward,",
        "but for **pool-cluster TP projection** from EMA59 touch.",
        "",
    ]
    for side in ("long", "short"):
        block = (payload.get("by_side") or {}).get(side) or {}
        best = block.get("best_rule") or {}
        qrule = block.get("quantile_rule") or {}
        rule = qrule.get("rule") or best.get("rule") or {}
        train = qrule.get("train") or best.get("train") or {}
        test = qrule.get("test") or best.get("test") or {}
        full = qrule.get("full") or {}
        prop = block.get("quantile_proposal") or {}
        lines += [
            f"## {side.upper()}",
            "",
            f"- n={block.get('n')} train={block.get('n_train')} test={block.get('n_test')}",
            f"- labels: `{json.dumps(block.get('label_counts'))}`",
            "",
            "### Quantile proposal",
            "",
            "```json",
            json.dumps(prop, indent=2),
            "```",
            "",
            "### Suggested rule (quantile-primary)",
            "",
            "```json",
            json.dumps(rule, indent=2),
            "```",
            "",
            f"- full-sample: hit={full.get('hit_rate')} projected={full.get('n_projected')} "
            f"coverage={full.get('coverage')}",
            f"- train: hit={train.get('hit_rate')} projected={train.get('n_projected')} "
            f"coverage={train.get('coverage')} score={train.get('score')}",
            f"- test: hit={test.get('hit_rate')} projected={test.get('n_projected')} "
            f"coverage={test.get('coverage')} score={test.get('score')}",
            "",
            "### Suggested live wording",
            "",
        ]
        if side == "long":
            lines += [
                f"- ignore clusters closer than **{rule.get('near_noise_max_dist_pct')}%** as primary TP",
                f"- TP candidate = heaviest cluster with dist "
                f"**{rule.get('min_target_dist_pct')}–{rule.get('max_target_dist_pct')}%** "
                f"and strength_sum ≥ **{rule.get('min_cluster_strength_sum')}**",
                f"- only project if |confirm Δ| ≥ **{rule.get('min_abs_confirm_delta')}** "
                f"and OB bid/ask ≥ **{rule.get('min_ob_ratio_long')}**",
                "",
            ]
        else:
            lines += [
                f"- ignore clusters closer than **{rule.get('near_noise_max_dist_pct')}%** as primary TP",
                f"- TP candidate = heaviest cluster with dist "
                f"**{rule.get('min_target_dist_pct')}–{rule.get('max_target_dist_pct')}%** "
                f"and strength_sum ≥ **{rule.get('min_cluster_strength_sum')}**",
                f"- only project if |confirm Δ| ≥ **{rule.get('min_abs_confirm_delta')}** "
                f"and OB bid/ask ≤ **{rule.get('max_ob_ratio_short')}** (ask-heavy)",
                "",
            ]
    lines += [
        "## Status",
        "",
        "- Suggestion only — **not** wired into live exit code yet.",
        "- Next: apply rule on the 10 locked scanner longs as a sanity check,",
        "  then implement in `simulate_long` if stable.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase C: calibrate TP cluster thresholds from EMA59-touch events"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=_REPO / "results" / "ob_pool_ema59_touch_cluster_calibration.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_REPO / "results" / "ob_pool_tp_threshold_calibration_phase_c.json",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=_REPO / "results" / "ob_pool_tp_threshold_calibration_phase_c.md",
    )
    args = parser.parse_args(argv)

    touches = load_touches(args.input)
    symbol = "DOGEUSDT"
    try:
        symbol = str(json.loads(args.input.read_text()).get("symbol") or symbol)
    except Exception:
        pass

    by_side = {
        "long": calibrate_side(touches, "long"),
        "short": calibrate_side(touches, "short"),
    }
    payload = {
        "symbol": symbol,
        "input": str(args.input),
        "n_touches": len(touches),
        "by_side": by_side,
        "suggested_rules": {
            side: (
                (by_side[side].get("quantile_rule") or {}).get("rule")
                or (by_side[side].get("best_rule") or {}).get("rule")
            )
            for side in ("long", "short")
        },
        "note": (
            "Primary suggestion = quantile_rule (robust). "
            "best_rule = grid walk-forward pick; may be sparse on test."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_markdown(args.md_out, payload)

    print("=== PHASE C TP THRESHOLDS ===")
    for side in ("long", "short"):
        best = by_side[side].get("best_rule") or {}
        print(f"\n{side}:")
        print("  rule", json.dumps(best.get("rule"), indent=2))
        print(
            "  train hit",
            (best.get("train") or {}).get("hit_rate"),
            "test hit",
            (best.get("test") or {}).get("hit_rate"),
            "combined",
            best.get("combined_score"),
        )
    print(f"\nwrote {args.out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
