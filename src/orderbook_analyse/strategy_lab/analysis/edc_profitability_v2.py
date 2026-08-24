"""EDC profitability diagnosis (P2E1).

Joins P2D4 trades to existing reference enrichment and produces descriptive
causal diagnostics only. No ClickHouse, no ML, no threshold optimization.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

_FORMAT_VERSION = "edc_profitability_analysis/v1"
_NOTIONAL = Decimal("1000")
_COST_SCENARIOS = (
    Decimal("0"),
    Decimal("0.055"),
    Decimal("0.11"),
    Decimal("0.15"),
    Decimal("0.20"),
)
_META_SUFFIXES = (
    "__coverage_status",
    "__missing_reason",
    "__causal",
    "__feature_asof",
    "__source_table",
)
_IDENTITY_FEATURE_BASES = frozenset(
    {
        "feature__symbol",
        "feature__candidate_id",
        "feature__cross_episode_id",
        "feature__direction",
        "feature__decision_at",
        "feature__entry_at",
    }
)
_STRATEGY_CONSTANT_BASES = frozenset(
    {
        "feature__tp_pct",
        "feature__sl_pct",
        "feature__reward_risk_gross",
        "feature__net_tp_pct",
        "feature__net_sl_pct",
    }
)
_GROUP_A = "PREDICTOR_CAUSAL"
_GROUP_B = "IDENTITY_CONTEXT"
_GROUP_C = "OUTCOME_FUTURE"
_GROUP_U = "UNRESOLVED_AVAILABILITY"

_SIDE_MAP = {
    "long": "BULLISH",
    "short": "BEARISH",
    "LONG": "BULLISH",
    "SHORT": "BEARISH",
    "BULLISH": "BULLISH",
    "BEARISH": "BEARISH",
}


class StrategyProfitabilityError(ValueError):
    """Deterministic profitability-analysis rejection."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EdcProfitabilityAnalysisV2:
    """Summary of a completed P2E1 diagnosis write."""

    strategy_hash: str
    trade_count: int
    coin_count: int
    predictor_causal_total: int
    predictor_causal_analyzable: int
    predictor_causal_numeric_analyzable: int
    outcome_future_features: int
    unresolved_features: int
    output_dir: Path
    verdict: str


def analyze_edc_profitability_v2(
    *,
    trades_path: Path,
    coin_summary_path: Path,
    enrichment_path: Path,
    output_dir: Path,
) -> EdcProfitabilityAnalysisV2:
    """Load inputs, enforce 1:1 parity, write deterministic diagnosis artifacts."""
    if not isinstance(trades_path, Path):
        raise StrategyProfitabilityError("trades_path must be pathlib.Path")
    if not isinstance(coin_summary_path, Path):
        raise StrategyProfitabilityError("coin_summary_path must be pathlib.Path")
    if not isinstance(enrichment_path, Path):
        raise StrategyProfitabilityError("enrichment_path must be pathlib.Path")
    if not isinstance(output_dir, Path):
        raise StrategyProfitabilityError("output_dir must be pathlib.Path")
    for path, label in (
        (trades_path, "trades"),
        (coin_summary_path, "coin_summary"),
        (enrichment_path, "enrichment"),
    ):
        if not path.exists():
            raise StrategyProfitabilityError(f"{label} path not found: {path}")

    trades_bytes = trades_path.read_bytes()
    coins_bytes = coin_summary_path.read_bytes()
    enrichment_file = _resolve_enrichment_csv(enrichment_path)
    enrichment_bytes = enrichment_file.read_bytes()

    trades = _read_csv_dicts(trades_bytes)
    coins = _read_csv_dicts(coins_bytes)
    enrichment = _read_csv_dicts(enrichment_bytes)
    joined = _join_trades_enrichment(trades, enrichment)
    audit = _feature_availability_audit(enrichment)
    census = _feature_census(enrichment, audit)
    analyzable = [a for a in audit if a["group"] == _GROUP_A and a["usable"] == "yes"]
    numeric_analyzable = [a for a in analyzable if a["value_kind"] == "numeric"]

    trade_cmp = _trade_feature_comparison(joined, analyzable)
    quantiles = _feature_quantiles(joined, numeric_analyzable)
    coin_rows = _coin_analysis(joined, coins, numeric_analyzable)
    cost_rows = _cost_scenarios(coins)
    stability = _stability_analysis(joined, numeric_analyzable)
    hypotheses = _select_exploratory_hypotheses(joined, trade_cmp, stability)
    findings = _build_findings(
        joined, coins, stability, cost_rows, census, hypotheses
    )
    report = _build_report(
        joined, coins, stability, cost_rows, census, hypotheses
    )

    strategy_hash = trades[0]["strategy_hash"] if trades else ""
    for t in trades:
        if t["strategy_hash"] != strategy_hash:
            raise StrategyProfitabilityError("inconsistent strategy_hash in trades")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "analysis_format_version": _FORMAT_VERSION,
        "strategy_hash": strategy_hash,
        "notional_usdt": str(_NOTIONAL),
        "join_key": ["symbol", "source_event_id"],
        "enrichment_id_field": "candidate_id",
        "inputs": {
            "trades_path": str(trades_path),
            "trades_sha256": _sha256(trades_bytes),
            "coin_summary_path": str(coin_summary_path),
            "coin_summary_sha256": _sha256(coins_bytes),
            "enrichment_path": str(enrichment_file),
            "enrichment_sha256": _sha256(enrichment_bytes),
        },
        "join_stats": {
            "trades": len(trades),
            "enrichment_rows": len(enrichment),
            "matched": len(joined),
            "missing_trades": 0,
            "extra_enrichment": 0,
            "side_mismatches": 0,
            "decision_time_mismatches": 0,
            "duplicates": 0,
        },
        "window": {
            "note": "Inherited from P2D4/enrichment inputs; not re-queried",
        },
        "coins": len(coins),
        "trades": len(trades),
        "feature_census": census,
        "verdict": "P2E1_EDC_PROFITABILITY_DIAGNOSIS_COMPLETE",
    }

    _atomic_write_text(
        output_dir / "analysis_manifest.json",
        json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
    )
    _write_csv(
        output_dir / "feature_availability.csv",
        [
            "column",
            "group",
            "value_kind",
            "n_unique",
            "source_table",
            "availability",
            "rationale",
            "missing_rate",
            "usable",
        ],
        audit,
    )
    _write_csv(
        output_dir / "trade_feature_comparison.csv",
        list(trade_cmp[0].keys()) if trade_cmp else [
            "feature",
            "scope",
            "n_winner",
            "n_loser",
            "missing_winner",
            "missing_loser",
            "missing_rate",
            "winner_median",
            "loser_median",
            "winner_q25",
            "winner_q75",
            "loser_q25",
            "loser_q75",
            "median_diff",
            "effect_direction",
            "effect_strength",
        ],
        trade_cmp,
    )
    _write_csv(
        output_dir / "feature_quantiles.csv",
        list(quantiles[0].keys()) if quantiles else [
            "feature",
            "quartile",
            "n_trades",
            "n_coins",
            "win_rate",
            "gross_pnl_usdt",
            "costs_usdt",
            "net_pnl_usdt",
            "net_per_trade_usdt",
        ],
        quantiles,
    )
    _write_csv(
        output_dir / "coin_analysis.csv",
        list(coin_rows[0].keys()) if coin_rows else ["symbol"],
        coin_rows,
    )
    _write_csv(
        output_dir / "cost_scenarios.csv",
        list(cost_rows[0].keys()) if cost_rows else [
            "scope",
            "symbol",
            "cost_pct",
            "gross_pnl_usdt",
            "trade_count",
            "scenario_net_usdt",
            "profitable",
        ],
        cost_rows,
    )
    _write_csv(
        output_dir / "stability_analysis.csv",
        list(stability[0].keys()) if stability else [
            "feature",
            "assessment",
            "pooled_direction",
            "long_direction",
            "short_direction",
            "coins_same_direction",
            "coins_opposite_direction",
            "leave_one_coin_flips",
            "notes",
        ],
        stability,
    )
    _atomic_write_text(
        output_dir / "findings.json",
        json.dumps(findings, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
    )
    _atomic_write_text(output_dir / "report.md", report)

    # Inputs must remain byte-identical.
    if trades_path.read_bytes() != trades_bytes:
        raise StrategyProfitabilityError("trades input mutated")
    if coin_summary_path.read_bytes() != coins_bytes:
        raise StrategyProfitabilityError("coin_summary input mutated")
    if enrichment_file.read_bytes() != enrichment_bytes:
        raise StrategyProfitabilityError("enrichment input mutated")

    return EdcProfitabilityAnalysisV2(
        strategy_hash=strategy_hash,
        trade_count=len(trades),
        coin_count=len(coins),
        predictor_causal_total=int(census["predictor_causal_total"]),
        predictor_causal_analyzable=int(census["predictor_causal_analyzable"]),
        predictor_causal_numeric_analyzable=int(
            census["predictor_causal_numeric_analyzable"]
        ),
        outcome_future_features=int(census["outcome_future"]),
        unresolved_features=int(census["unresolved_availability"]),
        output_dir=output_dir,
        verdict="P2E1_EDC_PROFITABILITY_DIAGNOSIS_COMPLETE",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P2E1 EDC profitability diagnosis (no ClickHouse, no ML)."
    )
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--coin-summary", type=Path, required=True)
    parser.add_argument("--enrichment", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_arg_parser().parse_args(argv)
        result = analyze_edc_profitability_v2(
            trades_path=args.trades,
            coin_summary_path=args.coin_summary,
            enrichment_path=args.enrichment,
            output_dir=args.output_dir,
        )
        print(result.verdict)
        print(
            f"trades={result.trade_count} coins={result.coin_count} "
            f"predictor_causal={result.predictor_causal_total} "
            f"analyzable={result.predictor_causal_analyzable} "
            f"numeric_analyzable={result.predictor_causal_numeric_analyzable} "
            f"unresolved={result.unresolved_features} "
            f"output_dir={result.output_dir}"
        )
        return 0
    except StrategyProfitabilityError as exc:
        msg = str(exc)
        if "parity" in msg.lower() or "join" in msg.lower() or "mismatch" in msg.lower():
            print("P2E1_EDC_PROFITABILITY_ANALYSIS_BLOCKED_BY_PARITY", file=sys.stderr)
        else:
            print("P2E1_EDC_PROFITABILITY_ANALYSIS_BLOCKED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print("P2E1_EDC_PROFITABILITY_ANALYSIS_BLOCKED", file=sys.stderr)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def wilson_interval(
    wins: int, n: int, *, z: Decimal = Decimal("1.96")
) -> tuple[Decimal | None, Decimal | None]:
    """Wilson score interval for a binomial proportion (local, no deps)."""
    if type(wins) is not int or type(n) is not int:
        raise TypeError("wins and n must be int")
    if wins < 0 or n < 0 or wins > n:
        raise ValueError("invalid wins/n")
    if n == 0:
        return None, None
    z2 = z * z
    phat = Decimal(wins) / Decimal(n)
    denom = Decimal(1) + z2 / Decimal(n)
    center = phat + z2 / (Decimal(2) * Decimal(n))
    # Use float only inside sqrt for the Wilson radical; convert back immediately.
    rad = (
        phat * (Decimal(1) - phat) / Decimal(n) + z2 / (Decimal(4) * Decimal(n) * Decimal(n))
    )
    margin = z * Decimal(str(math.sqrt(float(rad))))
    low = (center - margin) / denom
    high = (center + margin) / denom
    return low, high


def scenario_net_usdt(
    *,
    gross_pnl_usdt: Decimal,
    trade_count: int,
    notional_usdt: Decimal,
    cost_pct: Decimal,
) -> Decimal:
    """scenario_net = gross − trade_count × notional × cost_pct / 100."""
    if type(gross_pnl_usdt) is not Decimal:
        raise TypeError("gross_pnl_usdt must be Decimal")
    if type(notional_usdt) is not Decimal:
        raise TypeError("notional_usdt must be Decimal")
    if type(cost_pct) is not Decimal:
        raise TypeError("cost_pct must be Decimal")
    if type(trade_count) is not int or trade_count < 0:
        raise ValueError("trade_count must be int >= 0")
    if cost_pct < 0:
        raise ValueError("cost_pct must be >= 0")
    costs = Decimal(trade_count) * notional_usdt * cost_pct / Decimal("100")
    return gross_pnl_usdt - costs


def sample_size_bucket(trade_count: int) -> str:
    if trade_count < 5:
        return "VERY_SMALL"
    if trade_count < 10:
        return "SMALL"
    if trade_count < 20:
        return "MEDIUM"
    return "LARGER"


def _resolve_enrichment_csv(path: Path) -> Path:
    if path.is_file():
        return path
    candidate = path / "enriched_trades.csv"
    if candidate.is_file():
        return candidate
    raise StrategyProfitabilityError(
        f"enrichment file not found (expected enriched_trades.csv under {path})"
    )


def _read_csv_dicts(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StrategyProfitabilityError(f"invalid datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        raise StrategyProfitabilityError("datetime must be timezone-aware UTC")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise StrategyProfitabilityError("datetime must be UTC")
    return parsed


def _norm_side(value: str) -> str:
    key = value.strip()
    if key not in _SIDE_MAP:
        raise StrategyProfitabilityError(f"unknown side/direction: {value!r}")
    return _SIDE_MAP[key]


def _join_trades_enrichment(
    trades: Sequence[Mapping[str, str]],
    enrichment: Sequence[Mapping[str, str]],
) -> list[dict[str, object]]:
    if not trades:
        raise StrategyProfitabilityError("parity join failed: trades empty")
    if "source_event_id" not in trades[0]:
        raise StrategyProfitabilityError("trades missing source_event_id")
    if not enrichment or "candidate_id" not in enrichment[0]:
        raise StrategyProfitabilityError(
            "parity join failed: enrichment missing candidate_id "
            "(must be same legacy ID as source_event_id)"
        )

    trade_keys = [(t["symbol"], t["source_event_id"]) for t in trades]
    enr_keys = [(r["symbol"], r["candidate_id"]) for r in enrichment]
    if len(trade_keys) != len(set(trade_keys)):
        raise StrategyProfitabilityError("parity join failed: duplicate trade IDs")
    if len(enr_keys) != len(set(enr_keys)):
        raise StrategyProfitabilityError("parity join failed: duplicate enrichment IDs")
    trade_set = set(trade_keys)
    enr_set = set(enr_keys)
    missing = trade_set - enr_set
    extra = enr_set - trade_set
    if missing or extra or len(trades) != len(enrichment):
        raise StrategyProfitabilityError(
            "parity join failed: "
            f"trades={len(trades)} enrichment={len(enrichment)} "
            f"missing={len(missing)} extra={len(extra)}"
        )

    enr_by = {(r["symbol"], r["candidate_id"]): r for r in enrichment}
    joined: list[dict[str, object]] = []
    for t in trades:
        key = (t["symbol"], t["source_event_id"])
        r = enr_by[key]
        if _norm_side(t["side"]) != _norm_side(str(r.get("feature__direction", ""))):
            raise StrategyProfitabilityError(
                f"parity join side mismatch for {key}: "
                f"trade={t['side']!r} enrichment={r.get('feature__direction')!r}"
            )
        td = _parse_utc(t["decision_time"])
        ed = _parse_utc(str(r["feature__decision_at"]))
        if td != ed:
            raise StrategyProfitabilityError(
                f"parity join decision_time mismatch for {key}: "
                f"trade={t['decision_time']!r} enrichment={r['feature__decision_at']!r}"
            )
        net = Decimal(t["net_pnl_usdt"])
        if net > 0:
            label = "winner"
        elif net < 0:
            label = "loser"
        else:
            label = "zero"
        row: dict[str, object] = {
            "symbol": t["symbol"],
            "source_event_id": t["source_event_id"],
            "side": t["side"],
            "decision_time": t["decision_time"],
            "entry_time": t["entry_time"],
            "gross_pnl_usdt": Decimal(t["gross_pnl_usdt"]),
            "costs_usdt": Decimal(t["costs_usdt"]),
            "net_pnl_usdt": net,
            "label": label,
            "enrichment": r,
        }
        joined.append(row)
    joined.sort(key=lambda x: (str(x["symbol"]), str(x["decision_time"]), str(x["source_event_id"])))
    return joined


def _base_feature_columns(enrichment_cols: Sequence[str]) -> list[str]:
    bases: list[str] = []
    for col in enrichment_cols:
        if not col.startswith("feature__"):
            continue
        if any(col.endswith(suf) for suf in _META_SUFFIXES):
            continue
        bases.append(col)
    return bases


def _feature_availability_audit(
    enrichment: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    if not enrichment:
        return []
    cols = list(enrichment[0].keys())
    n = len(enrichment)
    rows: list[dict[str, str]] = []

    # Explicit identity/outcome columns outside feature__ namespace.
    for col in ("candidate_id", "symbol", "setup_id"):
        if col in cols:
            profile = _column_value_profile(enrichment, col)
            rows.append(
                {
                    "column": col,
                    "group": _GROUP_B,
                    "value_kind": profile["value_kind"],
                    "n_unique": str(profile["n_unique"]),
                    "source_table": "enrichment_identity",
                    "availability": "row_identity",
                    "rationale": "Join/identity context; not a market predictor",
                    "missing_rate": str(profile["missing_rate"]),
                    "usable": "no",
                }
            )
    for col in cols:
        if col.startswith("label__"):
            profile = _column_value_profile(enrichment, col)
            rows.append(
                {
                    "column": col,
                    "group": _GROUP_C,
                    "value_kind": profile["value_kind"],
                    "n_unique": str(profile["n_unique"]),
                    "source_table": "enrichment_labels",
                    "availability": "post_outcome",
                    "rationale": "Outcome/label field; forbidden as predictor",
                    "missing_rate": str(profile["missing_rate"]),
                    "usable": "no",
                }
            )

    for base in _base_feature_columns(cols):
        profile = _column_value_profile(enrichment, base)
        missing_rate = profile["missing_rate"]
        causal_raw = str(enrichment[0].get(f"{base}__causal", "")).strip()
        cov = str(enrichment[0].get(f"{base}__coverage_status", "")).strip()
        src = str(enrichment[0].get(f"{base}__source_table", "")).strip() or "unknown"
        asof = str(enrichment[0].get(f"{base}__feature_asof", "")).strip()

        if base in _IDENTITY_FEATURE_BASES:
            group = _GROUP_B
            rationale = "Identity/context feature exported by enrichment"
            usable = "no"
        elif base in _STRATEGY_CONSTANT_BASES:
            group = _GROUP_B
            rationale = "Strategy constant / reference-cost parameter, not a market state predictor"
            usable = "no"
        elif causal_raw == "False" or cov == "CAUSALITY_UNPROVEN":
            group = _GROUP_U
            rationale = "Enrichment marks feature as non-causal or causality unproven"
            usable = "no"
        elif causal_raw != "True":
            group = _GROUP_U
            rationale = f"Unresolved causal flag {causal_raw!r}; excluded"
            usable = "no"
        else:
            group = _GROUP_A
            if missing_rate >= Decimal("1") or profile["value_kind"] == "empty":
                rationale = "Causal feature but fully missing; excluded from diagnosis"
                usable = "no"
            elif profile["value_kind"] == "constant":
                rationale = "Causal feature but constant across non-missing rows; excluded"
                usable = "no"
            elif profile["value_kind"] == "categorical":
                rationale = (
                    "Enrichment causal=True categorical predictor "
                    f"(asof={asof or 'n/a'})"
                )
                usable = "yes"
            else:
                rationale = (
                    "Enrichment causal=True; value uses only data with timestamp<=decision_at "
                    f"(asof={asof or 'n/a'})"
                )
                usable = "yes"

        rows.append(
            {
                "column": base,
                "group": group,
                "value_kind": profile["value_kind"],
                "n_unique": str(profile["n_unique"]),
                "source_table": src,
                "availability": asof or cov or "unknown",
                "rationale": rationale,
                "missing_rate": str(missing_rate),
                "usable": usable,
            }
        )

    rows.sort(key=lambda r: (r["group"], r["column"]))
    return rows


def _column_value_profile(
    rows: Sequence[Mapping[str, str]], col: str
) -> dict[str, object]:
    empty = 0
    numeric: list[Decimal] = []
    categorical: list[str] = []
    for r in rows:
        raw = str(r.get(col, "")).strip()
        if raw in ("", "None", "null", "nan", "NaN"):
            empty += 1
            continue
        # Do not coerce True/False/bool-like labels via int; only strict decimals.
        lower = raw.lower()
        if lower in ("true", "false"):
            categorical.append(lower)
            continue
        num = _parse_number(raw)
        if num is None:
            categorical.append(raw)
        else:
            numeric.append(num)
    n = len(rows)
    missing_rate = Decimal(empty) / Decimal(n) if n else Decimal("1")
    if empty == n:
        return {"value_kind": "empty", "n_unique": 0, "missing_rate": missing_rate}
    if categorical and not numeric:
        return {
            "value_kind": "categorical",
            "n_unique": len(set(categorical)),
            "missing_rate": missing_rate,
        }
    if categorical and numeric:
        # Mixed string/number: treat as unresolved for predictor use via categorical path only
        # if strings dominate; otherwise keep numeric non-string values only.
        if len(categorical) >= len(numeric):
            return {
                "value_kind": "categorical",
                "n_unique": len(set(categorical) | {str(v) for v in numeric}),
                "missing_rate": missing_rate,
            }
        # Prefer numeric view when decimals dominate; strings counted as missing for nunique.
    uniq = set(numeric)
    if len(uniq) <= 1:
        return {
            "value_kind": "constant",
            "n_unique": len(uniq),
            "missing_rate": missing_rate,
        }
    return {
        "value_kind": "numeric",
        "n_unique": len(uniq),
        "missing_rate": missing_rate,
    }


def _feature_census(
    enrichment: Sequence[Mapping[str, str]],
    audit: Sequence[Mapping[str, str]],
) -> dict[str, int]:
    input_columns = len(enrichment[0].keys()) if enrichment else 0
    predictors = [a for a in audit if a["group"] == _GROUP_A]
    analyzable = [a for a in predictors if a["usable"] == "yes"]
    return {
        "enrichment_input_columns": input_columns,
        "audited_semantic_columns": len(audit),
        "predictor_causal_total": len(predictors),
        "predictor_causal_analyzable": len(analyzable),
        "predictor_causal_numeric_analyzable": sum(
            1 for a in analyzable if a["value_kind"] == "numeric"
        ),
        "predictor_causal_categorical_analyzable": sum(
            1 for a in analyzable if a["value_kind"] == "categorical"
        ),
        "predictor_causal_excluded_missing": sum(
            1
            for a in predictors
            if a["usable"] == "no"
            and a["value_kind"] != "constant"
            and (
                a["value_kind"] == "empty"
                or Decimal(a["missing_rate"]) >= Decimal("1")
            )
        ),
        "predictor_causal_excluded_constant": sum(
            1 for a in predictors if a["value_kind"] == "constant"
        ),
        "identity_context": sum(1 for a in audit if a["group"] == _GROUP_B),
        "outcome_future": sum(1 for a in audit if a["group"] == _GROUP_C),
        "unresolved_availability": sum(1 for a in audit if a["group"] == _GROUP_U),
    }


def _parse_number(raw: object) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text in ("", "None", "null", "nan", "NaN"):
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (1 - (pos - lo)) + ordered[hi] * (pos - lo)


def _effect_strength(winner: Sequence[float], loser: Sequence[float]) -> str:
    if not winner or not loser:
        return ""
    # Simple robust scale: |median_diff| / (MAD pooled + eps)
    all_v = list(winner) + list(loser)
    med = median(all_v)
    mad = median([abs(v - med) for v in all_v]) or 1e-12
    strength = abs(median(winner) - median(loser)) / mad
    return f"{strength:.6f}"


def _trade_feature_comparison(
    joined: Sequence[Mapping[str, object]],
    allowed: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    scopes: list[tuple[str, Sequence[Mapping[str, object]]]] = [
        ("pooled", joined),
        ("long", [r for r in joined if r["side"] == "long"]),
        ("short", [r for r in joined if r["side"] == "short"]),
    ]
    for feat_meta in allowed:
        feat = feat_meta["column"]
        for scope_name, subset in scopes:
            winners = [
                r for r in subset if r["label"] == "winner"
            ]
            losers = [r for r in subset if r["label"] == "loser"]
            w_vals: list[float] = []
            l_vals: list[float] = []
            miss_w = miss_l = 0
            for r in winners:
                enr = r["enrichment"]  # type: ignore[index]
                assert isinstance(enr, Mapping)
                val = _parse_number(enr.get(feat))
                if val is None:
                    miss_w += 1
                else:
                    w_vals.append(float(val))
            for r in losers:
                enr = r["enrichment"]  # type: ignore[index]
                assert isinstance(enr, Mapping)
                val = _parse_number(enr.get(feat))
                if val is None:
                    miss_l += 1
                else:
                    l_vals.append(float(val))
            n_avail = len(w_vals) + len(l_vals)
            n_miss = miss_w + miss_l
            miss_rate = (
                str(Decimal(n_miss) / Decimal(n_avail + n_miss))
                if (n_avail + n_miss)
                else "1"
            )
            w_med = median(w_vals) if w_vals else None
            l_med = median(l_vals) if l_vals else None
            if w_med is None or l_med is None:
                direction = "INSUFFICIENT_DATA"
                diff = ""
            else:
                diff_v = w_med - l_med
                diff = f"{diff_v:.10g}"
                if diff_v > 0:
                    direction = "winner_higher"
                elif diff_v < 0:
                    direction = "winner_lower"
                else:
                    direction = "equal"
            out.append(
                {
                    "feature": feat,
                    "scope": scope_name,
                    "n_winner": str(len(winners)),
                    "n_loser": str(len(losers)),
                    "missing_winner": str(miss_w),
                    "missing_loser": str(miss_l),
                    "missing_rate": miss_rate,
                    "winner_median": "" if w_med is None else f"{w_med:.10g}",
                    "loser_median": "" if l_med is None else f"{l_med:.10g}",
                    "winner_q25": ""
                    if not w_vals
                    else f"{_quantile(w_vals, 0.25):.10g}",
                    "winner_q75": ""
                    if not w_vals
                    else f"{_quantile(w_vals, 0.75):.10g}",
                    "loser_q25": ""
                    if not l_vals
                    else f"{_quantile(l_vals, 0.25):.10g}",
                    "loser_q75": ""
                    if not l_vals
                    else f"{_quantile(l_vals, 0.75):.10g}",
                    "median_diff": diff,
                    "effect_direction": direction,
                    "effect_strength": _effect_strength(w_vals, l_vals)
                    if w_vals and l_vals
                    else "",
                }
            )
    # categorical existing_*_verdict features among allowed
    for feat_meta in allowed:
        feat = feat_meta["column"]
        sample_vals = []
        for r in joined[:20]:
            enr = r["enrichment"]  # type: ignore[index]
            assert isinstance(enr, Mapping)
            raw = str(enr.get(feat, "")).strip()
            if raw and _parse_number(raw) is None:
                sample_vals.append(raw)
        if len(sample_vals) < 5:
            continue
        # treat as categorical
        cats: dict[str, list[Mapping[str, object]]] = {}
        for r in joined:
            enr = r["enrichment"]  # type: ignore[index]
            assert isinstance(enr, Mapping)
            raw = str(enr.get(feat, "")).strip() or "MISSING"
            if _parse_number(raw) is not None:
                cats = {}
                break
            cats.setdefault(raw, []).append(r)
        if not cats:
            continue
        for cat, rows in sorted(cats.items()):
            wins = sum(1 for r in rows if r["label"] == "winner")
            losses = sum(1 for r in rows if r["label"] == "loser")
            n = len(rows)
            gross = sum((r["gross_pnl_usdt"] for r in rows), Decimal("0"))  # type: ignore[arg-type]
            costs = sum((r["costs_usdt"] for r in rows), Decimal("0"))  # type: ignore[arg-type]
            net = sum((r["net_pnl_usdt"] for r in rows), Decimal("0"))  # type: ignore[arg-type]
            coins = len({str(r["symbol"]) for r in rows})
            wr = str(Decimal(wins) / Decimal(wins + losses)) if (wins + losses) else ""
            out.append(
                {
                    "feature": f"{feat}::{cat}",
                    "scope": "categorical_pooled",
                    "n_winner": str(wins),
                    "n_loser": str(losses),
                    "missing_winner": "0",
                    "missing_loser": "0",
                    "missing_rate": "0",
                    "winner_median": "",
                    "loser_median": "",
                    "winner_q25": "",
                    "winner_q75": "",
                    "loser_q25": "",
                    "loser_q75": "",
                    "median_diff": "",
                    "effect_direction": (
                        "SMALL_SAMPLE" if n < 5 else f"win_rate={wr};coins={coins};net={net}"
                    ),
                    "effect_strength": str(gross),
                }
            )
    out.sort(key=lambda r: (r["feature"], r["scope"]))
    return out


def _feature_quantiles(
    joined: Sequence[Mapping[str, object]],
    allowed: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for feat_meta in allowed:
        feat = feat_meta["column"]
        pairs: list[tuple[float, Mapping[str, object]]] = []
        for r in joined:
            enr = r["enrichment"]  # type: ignore[index]
            assert isinstance(enr, Mapping)
            val = _parse_number(enr.get(feat))
            if val is None:
                continue
            pairs.append((float(val), r))
        unique_vals = {v for v, _ in pairs}
        if len(pairs) < 20 or len(unique_vals) < 4:
            continue
        xs = sorted(v for v, _ in pairs)
        edges = [
            _quantile(xs, 0.0),
            _quantile(xs, 0.25),
            _quantile(xs, 0.5),
            _quantile(xs, 0.75),
            _quantile(xs, 1.0),
        ]
        assert all(e is not None for e in edges)
        buckets: list[list[Mapping[str, object]]] = [[], [], [], []]
        for v, r in pairs:
            if v <= edges[1]:  # type: ignore[operator]
                buckets[0].append(r)
            elif v <= edges[2]:  # type: ignore[operator]
                buckets[1].append(r)
            elif v <= edges[3]:  # type: ignore[operator]
                buckets[2].append(r)
            else:
                buckets[3].append(r)
        for i, rows in enumerate(buckets, start=1):
            if not rows:
                continue
            wins = sum(1 for r in rows if r["label"] == "winner")
            decided = sum(1 for r in rows if r["label"] in ("winner", "loser"))
            gross = sum((r["gross_pnl_usdt"] for r in rows), Decimal("0"))  # type: ignore[arg-type]
            costs = sum((r["costs_usdt"] for r in rows), Decimal("0"))  # type: ignore[arg-type]
            net = sum((r["net_pnl_usdt"] for r in rows), Decimal("0"))  # type: ignore[arg-type]
            n = len(rows)
            out.append(
                {
                    "feature": feat,
                    "quartile": str(i),
                    "n_trades": str(n),
                    "n_coins": str(len({str(r["symbol"]) for r in rows})),
                    "win_rate": str(Decimal(wins) / Decimal(decided)) if decided else "",
                    "gross_pnl_usdt": str(gross),
                    "costs_usdt": str(costs),
                    "net_pnl_usdt": str(net),
                    "net_per_trade_usdt": str(net / Decimal(n)),
                }
            )
    out.sort(key=lambda r: (r["feature"], r["quartile"]))
    return out


def _coin_analysis(
    joined: Sequence[Mapping[str, object]],
    coins: Sequence[Mapping[str, str]],
    allowed: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    by_sym: dict[str, list[Mapping[str, object]]] = {}
    for r in joined:
        by_sym.setdefault(str(r["symbol"]), []).append(r)
    # pick a few key regime features if present
    key_feats = [
        a["column"]
        for a in allowed
        if a["column"]
        in {
            "feature__atr14_pct",
            "feature__ema59_slope_atr",
            "feature__ob_imbalance_directional",
            "feature__directional_flow_1m",
            "feature__realized_volatility_1h",
        }
    ]
    out: list[dict[str, str]] = []
    for c in coins:
        sym = c["symbol"]
        rows = by_sym.get(sym, [])
        wins = sum(1 for r in rows if r["label"] == "winner")
        losses = sum(1 for r in rows if r["label"] == "loser")
        zeros = sum(1 for r in rows if r["label"] == "zero")
        n = len(rows)
        decided = wins + losses
        low, high = wilson_interval(wins, decided) if decided else (None, None)
        gross = Decimal(c["gross_pnl_usdt"]) if c.get("gross_pnl_usdt") not in ("", None) else Decimal("0")
        costs = Decimal(c["costs_usdt"]) if c.get("costs_usdt") not in ("", None) else Decimal("0")
        net = Decimal(c["net_pnl_usdt"]) if c.get("net_pnl_usdt") not in ("", None) else Decimal("0")
        long_rows = [r for r in rows if r["side"] == "long"]
        short_rows = [r for r in rows if r["side"] == "short"]
        row: dict[str, str] = {
            "symbol": sym,
            "status": c.get("status", ""),
            "candidate_count": c.get("candidate_count", str(n)),
            "trade_count": str(n),
            "winning_trades": str(wins),
            "losing_trades": str(losses),
            "zero_trades": str(zeros),
            "win_rate": str(Decimal(wins) / Decimal(decided)) if decided else "",
            "wilson_low": "" if low is None else str(low),
            "wilson_high": "" if high is None else str(high),
            "gross_pnl_usdt": str(gross),
            "costs_usdt": str(costs),
            "net_pnl_usdt": str(net),
            "net_per_trade_usdt": str(net / Decimal(n)) if n else "",
            "long_trades": str(len(long_rows)),
            "short_trades": str(len(short_rows)),
            "long_net_pnl_usdt": str(
                sum((r["net_pnl_usdt"] for r in long_rows), Decimal("0"))  # type: ignore[arg-type]
            ),
            "short_net_pnl_usdt": str(
                sum((r["net_pnl_usdt"] for r in short_rows), Decimal("0"))  # type: ignore[arg-type]
            ),
            "profitable": "yes" if net > 0 else ("no" if net < 0 else "zero"),
            "sample_bucket": sample_size_bucket(n),
        }
        for feat in key_feats:
            vals = []
            miss = 0
            for r in rows:
                enr = r["enrichment"]  # type: ignore[index]
                assert isinstance(enr, Mapping)
                val = _parse_number(enr.get(feat))
                if val is None:
                    miss += 1
                else:
                    vals.append(float(val))
            row[f"median_{feat}"] = f"{median(vals):.10g}" if vals else ""
            row[f"missing_rate_{feat}"] = (
                str(Decimal(miss) / Decimal(n)) if n else "1"
            )
        out.append(row)
    out.sort(key=lambda r: r["symbol"])
    return out


def _cost_scenarios(coins: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    total_gross = Decimal("0")
    total_trades = 0
    for c in coins:
        if c.get("status") and c["status"] != "complete":
            continue
        if c.get("gross_pnl_usdt", "") == "":
            continue
        gross = Decimal(c["gross_pnl_usdt"])
        n = int(c["trade_count"])
        total_gross += gross
        total_trades += n
        for cost in _COST_SCENARIOS:
            net = scenario_net_usdt(
                gross_pnl_usdt=gross,
                trade_count=n,
                notional_usdt=_NOTIONAL,
                cost_pct=cost,
            )
            # empirical break-even cost pct
            be = (
                (gross / (Decimal(n) * _NOTIONAL) * Decimal("100"))
                if n and gross > 0
                else Decimal("0")
            )
            if be < 0:
                be = Decimal("0")
            out.append(
                {
                    "scope": "coin",
                    "symbol": c["symbol"],
                    "cost_pct": str(cost),
                    "gross_pnl_usdt": str(gross),
                    "trade_count": str(n),
                    "scenario_net_usdt": str(net),
                    "profitable": "yes" if net > 0 else ("no" if net < 0 else "zero"),
                    "break_even_cost_pct": str(be),
                }
            )
    for cost in _COST_SCENARIOS:
        net = scenario_net_usdt(
            gross_pnl_usdt=total_gross,
            trade_count=total_trades,
            notional_usdt=_NOTIONAL,
            cost_pct=cost,
        )
        out.append(
            {
                "scope": "portfolio",
                "symbol": "",
                "cost_pct": str(cost),
                "gross_pnl_usdt": str(total_gross),
                "trade_count": str(total_trades),
                "scenario_net_usdt": str(net),
                "profitable": "yes" if net > 0 else ("no" if net < 0 else "zero"),
                "break_even_cost_pct": (
                    str(total_gross / (Decimal(total_trades) * _NOTIONAL) * Decimal("100"))
                    if total_trades and total_gross > 0
                    else "0"
                ),
            }
        )
    out.sort(key=lambda r: (r["scope"], r["symbol"], Decimal(r["cost_pct"])))
    return out


def _direction_from_medians(w_med: float | None, l_med: float | None) -> str:
    if w_med is None or l_med is None:
        return "INSUFFICIENT_DATA"
    if w_med > l_med:
        return "winner_higher"
    if w_med < l_med:
        return "winner_lower"
    return "equal"


def _median_for(
    rows: Sequence[Mapping[str, object]], feat: str, *, label: str
) -> float | None:
    vals: list[float] = []
    for r in rows:
        if r["label"] != label:
            continue
        enr = r["enrichment"]  # type: ignore[index]
        assert isinstance(enr, Mapping)
        val = _parse_number(enr.get(feat))
        if val is not None:
            vals.append(float(val))
    return median(vals) if vals else None


def _stability_analysis(
    joined: Sequence[Mapping[str, object]],
    numeric_analyzable: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Assess direction stability for numeric analyzable predictors only.

    Within-coin directions require:
    - coin trade_count >= 10
    - >=2 winners and >=2 losers with non-missing feature values
    Otherwise the coin contributes INSUFFICIENT_DATA, never MIXED.
    Near-constant features (n_unique < 4) are marked INSUFFICIENT_DATA.
    """
    out: list[dict[str, str]] = []
    eligible: list[Mapping[str, str]] = []
    for a in numeric_analyzable:
        feat = a["column"]
        # Constants are already usable=no; keep low-cardinality numerics but
        # let equal medians resolve to INSUFFICIENT_DATA rather than MIXED.
        vals = 0
        for r in joined:
            enr = r["enrichment"]  # type: ignore[index]
            assert isinstance(enr, Mapping)
            if _parse_number(enr.get(feat)) is not None:
                vals += 1
        if vals < 50 or Decimal(a["missing_rate"]) > Decimal("0.5"):
            out.append(
                {
                    "feature": feat,
                    "assessment": "INSUFFICIENT_DATA",
                    "pooled_direction": "INSUFFICIENT_DATA",
                    "long_direction": "INSUFFICIENT_DATA",
                    "short_direction": "INSUFFICIENT_DATA",
                    "coins_same_direction": "0",
                    "coins_opposite_direction": "0",
                    "leave_one_coin_flips": "0",
                    "notes": "high_missing_or_low_coverage",
                }
            )
            continue
        eligible.append(a)

    coins = sorted({str(r["symbol"]) for r in joined})
    for a in eligible:
        feat = a["column"]
        pooled = _direction_from_medians(
            _median_for(joined, feat, label="winner"),
            _median_for(joined, feat, label="loser"),
        )
        long_rows = [r for r in joined if r["side"] == "long"]
        short_rows = [r for r in joined if r["side"] == "short"]
        long_d = _direction_from_medians(
            _median_for(long_rows, feat, label="winner"),
            _median_for(long_rows, feat, label="loser"),
        )
        short_d = _direction_from_medians(
            _median_for(short_rows, feat, label="winner"),
            _median_for(short_rows, feat, label="loser"),
        )

        same = opp = 0
        for sym in coins:
            rows = [r for r in joined if r["symbol"] == sym]
            if len(rows) < 10:
                continue
            d = _coin_feature_direction(rows, feat)
            if d in ("INSUFFICIENT_DATA", "equal"):
                continue
            if pooled in ("INSUFFICIENT_DATA", "equal"):
                continue
            if d == pooled:
                same += 1
            else:
                opp += 1

        flips = 0
        for holdout in coins:
            subset = [r for r in joined if r["symbol"] != holdout]
            d = _direction_from_medians(
                _median_for(subset, feat, label="winner"),
                _median_for(subset, feat, label="loser"),
            )
            if pooled not in ("INSUFFICIENT_DATA", "equal") and d not in (
                pooled,
                "INSUFFICIENT_DATA",
                "equal",
            ):
                flips += 1

        notes = ""
        if pooled in ("INSUFFICIENT_DATA", "equal"):
            assessment = "INSUFFICIENT_DATA"
            notes = "pooled_direction_unavailable"
        elif same + opp == 0:
            assessment = "INSUFFICIENT_DATA"
            notes = "no_coin_with_sufficient_winner_loser_coverage"
        elif same >= 1 and opp == 0 and flips == 0:
            assessment = "STABLE_DIRECTION"
        elif same > 0 and opp > 0:
            assessment = "MIXED_DIRECTION"
            notes = "within_coin_directions_disagree"
        elif same == 0 and opp >= 1:
            assessment = "POSSIBLE_COIN_MIX_CONFOUNDING"
            notes = "pooled direction not reproduced inside coins"
        elif flips > 0 and same <= 1:
            assessment = "SINGLE_COIN_DRIVEN"
            notes = f"leave_one_coin_out flips={flips}"
        else:
            assessment = "MIXED_DIRECTION"
            notes = "residual_mixed_classification"

        out.append(
            {
                "feature": feat,
                "assessment": assessment,
                "pooled_direction": pooled,
                "long_direction": long_d,
                "short_direction": short_d,
                "coins_same_direction": str(same),
                "coins_opposite_direction": str(opp),
                "leave_one_coin_flips": str(flips),
                "notes": notes,
            }
        )
    out.sort(key=lambda r: r["feature"])
    return out


def _coin_feature_direction(
    rows: Sequence[Mapping[str, object]], feat: str
) -> str:
    """Direction inside one coin; requires >=2 winners and >=2 losers with values."""
    winners: list[float] = []
    losers: list[float] = []
    for r in rows:
        enr = r["enrichment"]  # type: ignore[index]
        assert isinstance(enr, Mapping)
        val = _parse_number(enr.get(feat))
        if val is None:
            continue
        if r["label"] == "winner":
            winners.append(float(val))
        elif r["label"] == "loser":
            losers.append(float(val))
    if len(winners) < 2 or len(losers) < 2:
        return "INSUFFICIENT_DATA"
    return _direction_from_medians(
        median(winners) if winners else None,
        median(losers) if losers else None,
    )


def _select_exploratory_hypotheses(
    joined: Sequence[Mapping[str, object]],
    trade_cmp: Sequence[Mapping[str, str]],
    stability: Sequence[Mapping[str, str]],
    *,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Deterministically pick the strongest unconfirmed exploratory observations."""
    stab_by = {s["feature"]: s for s in stability}
    candidates: list[dict[str, object]] = []
    for row in trade_cmp:
        if row.get("scope") != "pooled":
            continue
        feat = row["feature"]
        if "::" in feat:
            continue
        direction = row.get("effect_direction", "")
        if direction in ("", "INSUFFICIENT_DATA", "equal"):
            continue
        strength_raw = row.get("effect_strength", "")
        if not strength_raw:
            continue
        missing = Decimal(row["missing_rate"])
        if missing > Decimal("0.20"):
            continue
        n_winner = int(row["n_winner"])
        n_loser = int(row["n_loser"])
        n_trades = n_winner + n_loser
        if n_trades < 50:
            continue
        # Coins with at least one non-missing value for this feature
        coins_present: set[str] = set()
        for r in joined:
            enr = r["enrichment"]  # type: ignore[index]
            assert isinstance(enr, Mapping)
            if _parse_number(enr.get(feat)) is not None:
                coins_present.add(str(r["symbol"]))
        if len(coins_present) < 5:
            continue
        stab = stab_by.get(feat, {})
        assessment = str(stab.get("assessment", "INSUFFICIENT_DATA"))
        strength = float(strength_raw)
        candidates.append(
            {
                "feature": feat,
                "observed_direction": direction,
                "winner_loser_diff": row.get("median_diff", ""),
                "winner_median": row.get("winner_median", ""),
                "loser_median": row.get("loser_median", ""),
                "n_trades": n_trades,
                "n_coins": len(coins_present),
                "stability_status": assessment,
                "effect_strength": strength,
                "unstable": assessment
                in {
                    "MIXED_DIRECTION",
                    "SINGLE_COIN_DRIVEN",
                    "POSSIBLE_COIN_MIX_CONFOUNDING",
                },
            }
        )
    candidates.sort(
        key=lambda c: (-float(c["effect_strength"]), str(c["feature"]))
    )
    selected = candidates[:limit]
    out: list[dict[str, object]] = []
    for i, c in enumerate(selected, start=1):
        unstable = bool(c["unstable"])
        status = str(c["stability_status"])
        limitation = (
            "Exploratory only; MIXED_DIRECTION means within-coin signs disagree — "
            "not a filter candidate"
            if status == "MIXED_DIRECTION"
            else "Exploratory only; single 30d window; not a validated filter rule"
        )
        if unstable and status != "MIXED_DIRECTION":
            limitation = (
                f"Exploratory only; stability={status}; do not promote to a filter"
            )
        out.append(
            {
                "finding_id": f"F_HYP_{i}",
                "feature": c["feature"],
                "observation": (
                    f"Pooled {c['observed_direction']}: winner_median="
                    f"{c['winner_median']} vs loser_median={c['loser_median']} "
                    f"(diff={c['winner_loser_diff']}); n_trades={c['n_trades']}, "
                    f"n_coins={c['n_coins']}; stability={status}"
                    + (" [UNSTABLE]" if unstable else "")
                ),
                "observed_direction": c["observed_direction"],
                "winner_loser_diff": c["winner_loser_diff"],
                "n_trades": c["n_trades"],
                "n_coins": c["n_coins"],
                "stability_status": status,
                "support": (
                    f"effect_strength={c['effect_strength']:.6f}; "
                    "PREDICTOR_CAUSAL numeric pooled comparison"
                ),
                "limitations": limitation,
                "causality_status": "unconfirmed_exploratory_hypothesis",
                "next_test": (
                    "Pre-register this single directional contrast on a later holdout "
                    "window without retuning thresholds"
                ),
            }
        )
    return out


def _build_findings(
    joined: Sequence[Mapping[str, object]],
    coins: Sequence[Mapping[str, str]],
    stability: Sequence[Mapping[str, str]],
    cost_rows: Sequence[Mapping[str, str]],
    census: Mapping[str, int],
    hypotheses: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    wins = sum(1 for r in joined if r["label"] == "winner")
    losses = sum(1 for r in joined if r["label"] == "loser")
    zeros = sum(1 for r in joined if r["label"] == "zero")
    gross = sum((r["gross_pnl_usdt"] for r in joined), Decimal("0"))  # type: ignore[arg-type]
    costs = sum((r["costs_usdt"] for r in joined), Decimal("0"))  # type: ignore[arg-type]
    net = sum((r["net_pnl_usdt"] for r in joined), Decimal("0"))  # type: ignore[arg-type]
    prof = sum(
        1
        for c in coins
        if c.get("net_pnl_usdt") not in ("", None) and Decimal(c["net_pnl_usdt"]) > 0
    )
    unprof = sum(
        1
        for c in coins
        if c.get("net_pnl_usdt") not in ("", None) and Decimal(c["net_pnl_usdt"]) < 0
    )
    findings: list[dict[str, object]] = [
        {
            "finding_id": "F0_OVERALL",
            "feature": "",
            "observation": (
                f"{len(joined)} trades: winners={wins} losers={losses} zero={zeros}; "
                f"gross={gross} costs={costs} net={net}; "
                f"profitable_coins={prof} unprofitable_coins={unprof}"
            ),
            "support": "P2D4 exports joined 1:1 to enrichment",
            "limitations": "Single 30d window; descriptive only",
            "causality_status": "outcome_summary",
            "next_test": "Confirm any candidate filters on a later holdout window",
        }
    ]
    port_011 = next(
        (
            r
            for r in cost_rows
            if r["scope"] == "portfolio" and r["cost_pct"] == "0.11"
        ),
        None,
    )
    port_000 = next(
        (r for r in cost_rows if r["scope"] == "portfolio" and r["cost_pct"] == "0"),
        None,
    )
    if port_000 and port_011:
        findings.append(
            {
                "finding_id": "F1_COST_DRAG",
                "feature": "roundtrip_cost",
                "observation": (
                    f"At 0% costs portfolio net={port_000['scenario_net_usdt']}; "
                    f"at 0.11% net={port_011['scenario_net_usdt']}"
                ),
                "support": "Decimal scenario_net formula on unchanged gross",
                "limitations": "Does not re-simulate fills; costs applied uniformly",
                "causality_status": "accounting_identity",
                "next_test": "Compare cost scenarios on later windows without retuning",
            }
        )

    findings.extend(list(hypotheses))

    mixed = [s for s in stability if s["assessment"] == "MIXED_DIRECTION"]
    stable = [s for s in stability if s["assessment"] == "STABLE_DIRECTION"]
    findings.append(
        {
            "finding_id": "F_SAMPLE",
            "feature": "",
            "observation": (
                "Several profitable coins have SMALL/VERY_SMALL samples "
                "(e.g. JTO/LIT); treat those coin-level stories cautiously"
            ),
            "support": "sample_bucket in coin_analysis",
            "limitations": "Wilson intervals widen sharply below n=10",
            "causality_status": "sampling_warning",
            "next_test": "Require minimum n before coin promotion hypotheses",
        }
    )
    findings.append(
        {
            "finding_id": "F_CENSUS",
            "feature": "",
            "observation": (
                f"predictor_causal_total={census['predictor_causal_total']}; "
                f"analyzable={census['predictor_causal_analyzable']} "
                f"(numeric={census['predictor_causal_numeric_analyzable']}, "
                f"categorical={census['predictor_causal_categorical_analyzable']}); "
                f"excluded_missing={census['predictor_causal_excluded_missing']}; "
                f"excluded_constant={census['predictor_causal_excluded_constant']}; "
                f"identity={census['identity_context']}; "
                f"outcome_future={census['outcome_future']}; "
                f"unresolved={census['unresolved_availability']}"
            ),
            "support": "feature_availability.csv + feature_census in manifest",
            "limitations": "Audited semantic columns exclude meta suffixes",
            "causality_status": "feature_gate",
            "next_test": "Only promote PREDICTOR_CAUSAL analyzable fields",
        }
    )
    findings.append(
        {
            "finding_id": "F_STABILITY_SUMMARY",
            "feature": "",
            "observation": (
                f"STABLE_DIRECTION={len(stable)}; MIXED_DIRECTION={len(mixed)}; "
                "within-coin checks use trade_count>=10 and >=2 winners/losers "
                "with non-missing values"
            ),
            "support": "stability_analysis.csv",
            "limitations": "Mixed does not prove uselessness; no automatic filter",
            "causality_status": "stability_summary",
            "next_test": "Inspect Long/Short splits before any filter draft",
        }
    )
    return findings


def _build_report(
    joined: Sequence[Mapping[str, object]],
    coins: Sequence[Mapping[str, str]],
    stability: Sequence[Mapping[str, str]],
    cost_rows: Sequence[Mapping[str, str]],
    census: Mapping[str, int],
    hypotheses: Sequence[Mapping[str, object]],
) -> str:
    wins = sum(1 for r in joined if r["label"] == "winner")
    losses = sum(1 for r in joined if r["label"] == "loser")
    net = sum((r["net_pnl_usdt"] for r in joined), Decimal("0"))  # type: ignore[arg-type]
    gross = sum((r["gross_pnl_usdt"] for r in joined), Decimal("0"))  # type: ignore[arg-type]
    costs = sum((r["costs_usdt"] for r in joined), Decimal("0"))  # type: ignore[arg-type]
    prof = [
        c
        for c in coins
        if c.get("net_pnl_usdt") not in ("", None) and Decimal(c["net_pnl_usdt"]) > 0
    ]
    lines = [
        "# EDC Profitability Diagnosis (P2E1)",
        "",
        "Beschreibende Diagnose auf dem P2D4-Lauf und bestehendem Enrichment.",
        "Keine Filteroptimierung, kein ML, keine neuen Marktdatenabfragen.",
        "",
        "## Gesamtresultat",
        f"- Trades: {len(joined)} (Winner {wins} / Loser {losses})",
        f"- Gross {gross} / Costs {costs} / Net {net}",
        f"- Profitable Coins: {len(prof)} / {len(coins)}",
        "",
        "## Kostenwirkung",
    ]
    for r in cost_rows:
        if r["scope"] == "portfolio":
            lines.append(
                f"- cost {r['cost_pct']}% → scenario_net={r['scenario_net_usdt']}"
            )
    lines.extend(
        [
            "",
            "## Feature-Kausalität / Zensus",
            f"- Enrichment-Inputspalten: {census['enrichment_input_columns']}",
            f"- Audited semantic columns: {census['audited_semantic_columns']}",
            f"- PREDICTOR_CAUSAL (total): {census['predictor_causal_total']}",
            f"- PREDICTOR_CAUSAL analyzable: {census['predictor_causal_analyzable']}",
            f"- davon numeric analyzable: {census['predictor_causal_numeric_analyzable']}",
            f"- davon categorical analyzable: {census['predictor_causal_categorical_analyzable']}",
            f"- excluded missing: {census['predictor_causal_excluded_missing']}",
            f"- excluded constant: {census['predictor_causal_excluded_constant']}",
            f"- IDENTITY_CONTEXT: {census['identity_context']}",
            f"- OUTCOME_FUTURE: {census['outcome_future']}",
            f"- UNRESOLVED_AVAILABILITY: {census['unresolved_availability']}",
            "",
            "## Stabile Hinweise (explorativ)",
        ]
    )
    stables = [s for s in stability if s["assessment"] == "STABLE_DIRECTION"]
    if not stables:
        lines.append("- Keine STABLE_DIRECTION-Features in dieser Stichprobe.")
    for s in stables[:8]:
        lines.append(
            f"- {s['feature']}: pooled={s['pooled_direction']} "
            f"(same_coins={s['coins_same_direction']}, flips={s['leave_one_coin_flips']})"
        )
    lines.extend(["", "## Widersprüche / Coin-Mix"])
    conf = [s for s in stability if s["assessment"] == "POSSIBLE_COIN_MIX_CONFOUNDING"]
    mixed = [s for s in stability if s["assessment"] == "MIXED_DIRECTION"]
    insuff = [s for s in stability if s["assessment"] == "INSUFFICIENT_DATA"]
    lines.append(f"- POSSIBLE_COIN_MIX_CONFOUNDING: {len(conf)}")
    lines.append(f"- MIXED_DIRECTION: {len(mixed)}")
    lines.append(f"- INSUFFICIENT_DATA: {len(insuff)}")
    lines.extend(
        [
            "",
            "## Stichprobenprobleme",
            "- LIT/JTO und ähnliche Coins mit n<10 sind ausdrücklich unsicher.",
            "- Coin-Rankings sind keine Freigabeentscheidung.",
            "",
            "## Was wir noch nicht wissen",
            "- Ob beobachtete Richtungen out-of-sample halten.",
            "- Ob scheinbar stabile Features nur Fensterartefakte sind.",
            "",
            "## Nächste Hypothesen (unbestätigt)",
        ]
    )
    if not hypotheses:
        lines.append("- Keine Hypothese erfüllte die Mindestbedingungen.")
    for h in hypotheses:
        mark = " [UNSTABLE]" if "UNSTABLE" in str(h.get("observation", "")) else ""
        lines.append(
            f"- {h['feature']}: {h.get('observed_direction', '')} "
            f"diff={h.get('winner_loser_diff', '')}; "
            f"n_trades={h.get('n_trades')}; n_coins={h.get('n_coins')}; "
            f"stability={h.get('stability_status')}{mark}. "
            f"Einschränkung: {h.get('limitations')}. "
            f"Nächster Test: {h.get('next_test')}"
        )
    lines.append("")
    return "\n".join(lines)


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    _atomic_write_text(path, buf.getvalue())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
