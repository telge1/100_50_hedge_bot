"""Combined Protected-Low + Protected-High historical structure catalog."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.c3_protected_low_historical_catalog import (
    DEDUP_HOURS,
    GATE_MAX_REGIME_SHARE,
    GATE_MIN_REGIMES,
    GATE_MIN_SYMBOLS,
    LONG_GATE_MIN_EVENTS,
    SHORT_GATE_MIN_EVENTS,
    DEFAULT_OUTPUT_DIR as DEFAULT_LOW_DIR,
    _parse_ts,
    _write_csv,
    _write_json,
    run_protected_low_historical_catalog,
)
from orderbook_analyse.c3_protected_structure_historical_catalog import (
    DEFAULT_OUTPUT_DIR as DEFAULT_HIGH_DIR,
    run_protected_high_historical_catalog,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMBINED_DIR = ROOT / "results" / "c3_protected_structure_combined_catalog"

COMBINED_PRIMARY_DECISIONS = (
    "SUFFICIENT_COMBINED_LONG_AND_SHORT_SAMPLE_FOUND",
    "SUFFICIENT_COMBINED_LONG_SAMPLE_ONLY",
    "SUFFICIENT_COMBINED_SHORT_SAMPLE_ONLY",
    "COMBINED_SAMPLE_TOO_SMALL",
    "ASYMMETRIC_LOW_BREAKDOWN_DOMINANCE",
    "ASYMMETRIC_HIGH_BREAKOUT_DOMINANCE",
    "STRUCTURE_MIRROR_INSUFFICIENT_OVERALL",
)

ORIGIN_LOW = "PROTECTED_LOW"
ORIGIN_HIGH = "PROTECTED_HIGH"

CAND_ORIGIN = {
    ("LOW", "RECLAIM_CONFIRMED", "LONG"): "PROTECTED_LOW_RECLAIM",
    ("LOW", "BREAKDOWN_CONFIRMED", "SHORT"): "PROTECTED_LOW_BREAKDOWN",
    ("HIGH", "BREAKOUT_CONFIRMED", "LONG"): "PROTECTED_HIGH_BREAKOUT",
    ("HIGH", "RECLAIM_DOWN_CONFIRMED", "SHORT"): "PROTECTED_HIGH_RECLAIM_DOWN",
}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_catalog(
    *,
    side: str,
    directory: Path,
    overwrite: bool,
    run_if_missing: bool,
) -> Path:
    directory = Path(directory)
    marker = directory / "event_decisions.csv"
    if marker.exists() and not overwrite:
        return directory
    if not run_if_missing and not marker.exists():
        raise FileNotFoundError(f"Missing {side} catalog at {directory}")
    logger.info("Running %s historical catalog → %s", side, directory)
    if side == "LOW":
        run_protected_low_historical_catalog(output_dir=directory, overwrite=True)
    else:
        run_protected_high_historical_catalog(output_dir=directory, overwrite=True)
    return directory


def _event_origin_rows(low_dir: Path, high_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    low = _read_csv(low_dir / "event_decisions.csv")
    high = _read_csv(high_dir / "event_decisions.csv")
    for _, r in low.iterrows():
        rows.append(
            {
                "event_id": r.get("event_id"),
                "symbol": r.get("symbol"),
                "origin": ORIGIN_LOW,
                "level": r.get("protected_low"),
                "break_available_at": r.get("break_available_at"),
                "outcome": r.get("outcome"),
                "decision_ts": r.get("decision_ts"),
                "data_valid": r.get("data_valid"),
            }
        )
    for _, r in high.iterrows():
        rows.append(
            {
                "event_id": r.get("event_id"),
                "symbol": r.get("symbol"),
                "origin": ORIGIN_HIGH,
                "level": r.get("protected_high"),
                "break_available_at": r.get("break_available_at"),
                "outcome": r.get("outcome"),
                "decision_ts": r.get("decision_ts"),
                "data_valid": r.get("data_valid"),
            }
        )
    return rows


def _candidate_origin(
    *,
    side_catalog: str,
    outcome: str,
    cand_side: str,
    explicit_source: str | None = None,
) -> str:
    if explicit_source:
        return str(explicit_source)
    key = (side_catalog, str(outcome), str(cand_side).upper())
    if key in CAND_ORIGIN:
        return CAND_ORIGIN[key]
    # Fallbacks
    if side_catalog == "LOW" and str(cand_side).upper() == "LONG":
        return "PROTECTED_LOW_RECLAIM"
    if side_catalog == "LOW":
        return "PROTECTED_LOW_BREAKDOWN"
    if str(cand_side).upper() == "LONG":
        return "PROTECTED_HIGH_BREAKOUT"
    return "PROTECTED_HIGH_RECLAIM_DOWN"


def _combined_candidates(low_dir: Path, high_dir: Path) -> list[dict[str, Any]]:
    low_dec = _read_csv(low_dir / "event_decisions.csv")
    high_dec = _read_csv(high_dir / "event_decisions.csv")
    low_outcome = (
        dict(zip(low_dec["event_id"], low_dec["outcome"])) if not low_dec.empty else {}
    )
    high_outcome = (
        dict(zip(high_dec["event_id"], high_dec["outcome"])) if not high_dec.empty else {}
    )
    rows: list[dict[str, Any]] = []
    for path, catalog, outcomes in (
        (low_dir / "long_candidates.csv", "LOW", low_outcome),
        (low_dir / "short_candidates.csv", "LOW", low_outcome),
        (high_dir / "long_candidates.csv", "HIGH", high_outcome),
        (high_dir / "short_candidates.csv", "HIGH", high_outcome),
    ):
        df = _read_csv(path)
        if df.empty:
            continue
        for _, r in df.iterrows():
            eid = r.get("event_id")
            origin = _candidate_origin(
                side_catalog=catalog,
                outcome=str(outcomes.get(eid, "")),
                cand_side=str(r.get("side")),
                explicit_source=r.get("source") if "source" in df.columns else None,
            )
            rows.append(
                {
                    "candidate_id": r.get("candidate_id"),
                    "event_id": eid,
                    "symbol": r.get("symbol"),
                    "side": r.get("side"),
                    "candidate_type": r.get("candidate_type"),
                    "candidate_ts": r.get("candidate_ts"),
                    "candidate_price": r.get("candidate_price"),
                    "origin": origin,
                    "catalog": catalog,
                }
            )
    return rows


def _combined_forward_returns(low_dir: Path, high_dir: Path) -> list[dict[str, Any]]:
    cand_origin = {c["candidate_id"]: c["origin"] for c in _combined_candidates(low_dir, high_dir)}
    rows: list[dict[str, Any]] = []
    for path, catalog in (
        (low_dir / "candidate_forward_returns.csv", "LOW"),
        (high_dir / "candidate_forward_returns.csv", "HIGH"),
    ):
        df = _read_csv(path)
        if df.empty:
            continue
        for _, r in df.iterrows():
            cid = r.get("candidate_id")
            rows.append(
                {
                    **{k: r.get(k) for k in df.columns},
                    "origin": cand_origin.get(cid),
                    "catalog": catalog,
                }
            )
    return rows


def _union_find_regimes(
    events: list[dict[str, Any]],
    *,
    window_hours: float = DEDUP_HOURS,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Union-find across High+Low per symbol on overlapping 6h windows."""
    from datetime import timedelta

    window = timedelta(hours=window_hours)
    mapping: list[dict[str, Any]] = []
    regimes: dict[str, list[str]] = {}
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_sym[str(e["symbol"])].append(e)

    for symbol, rows in by_sym.items():
        rows = sorted(
            rows,
            key=lambda x: (_parse_ts(x["break_available_at"]) or pd.Timestamp.min),
        )
        parent = list(range(len(rows)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        for i, a in enumerate(rows):
            ta = _parse_ts(a["break_available_at"])
            if ta is None:
                continue
            a_end = ta + window
            for j in range(i + 1, len(rows)):
                b = rows[j]
                tb = _parse_ts(b["break_available_at"])
                if tb is None:
                    continue
                if tb > a_end + window:
                    break
                b_end = tb + window
                overlap = not (b_end <= ta or a_end <= tb)
                if overlap:
                    union(i, j)

        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(len(rows)):
            clusters[find(i)].append(i)
        for k, idxs in enumerate(sorted(clusters.values(), key=lambda ix: ix[0])):
            rid = f"{symbol}_COMB_REG_{k + 1:03d}"
            regimes[rid] = [rows[i]["event_id"] for i in idxs]
            origins = {rows[i]["origin"] for i in idxs}
            outcomes = {str(rows[i].get("outcome")) for i in idxs}
            # Direction flip: both LOW breakdown-ish and HIGH breakout-ish, or reclaim pairs
            longish = outcomes & {"RECLAIM_CONFIRMED", "BREAKOUT_CONFIRMED"}
            shortish = outcomes & {"BREAKDOWN_CONFIRMED", "RECLAIM_DOWN_CONFIRMED"}
            direction_flip = bool(longish and shortish) or (
                ORIGIN_LOW in origins and ORIGIN_HIGH in origins
            )
            for i in idxs:
                mapping.append(
                    {
                        "event_id": rows[i]["event_id"],
                        "symbol": symbol,
                        "regime_id": rid,
                        "origin": rows[i]["origin"],
                        "level": rows[i].get("level"),
                        "break_available_at": rows[i]["break_available_at"],
                        "outcome": rows[i].get("outcome"),
                        "direction_flip_flag": direction_flip,
                        "origins_in_regime": ",".join(sorted(origins)),
                    }
                )
    return mapping, regimes


def _gate_block(
    events: list[dict[str, Any]],
    *,
    long_outcomes: set[str],
    short_outcomes: set[str],
    regime_by_event: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    def _one(outcomes: set[str], min_n: int) -> dict[str, Any]:
        subset = [
            e
            for e in events
            if str(e.get("outcome")) in outcomes
            and str(e.get("data_valid")).lower() in {"true", "1"}
        ]
        symbols = {e["symbol"] for e in subset}
        regimes = [regime_by_event.get(e["event_id"]) for e in subset if e["event_id"] in regime_by_event]
        reg_counts = Counter(regimes)
        n = len(subset)
        n_reg = len(reg_counts)
        max_share = (max(reg_counts.values()) / n) if n else 0.0
        checks = {
            "n_events": n,
            "n_symbols": len(symbols),
            "n_regimes": n_reg,
            "max_regime_share": max_share,
            "pass_n": n >= min_n,
            "pass_symbols": len(symbols) >= GATE_MIN_SYMBOLS,
            "pass_regimes": n_reg >= GATE_MIN_REGIMES,
            "pass_concentration": max_share <= GATE_MAX_REGIME_SHARE if n else False,
        }
        checks["pass"] = all(
            checks[k] for k in ("pass_n", "pass_symbols", "pass_regimes", "pass_concentration")
        )
        return checks

    return _one(long_outcomes, LONG_GATE_MIN_EVENTS), _one(short_outcomes, SHORT_GATE_MIN_EVENTS)


def _gate_table_multi(blocks: dict[str, tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    """Rows: gate × columns for each pool (low_long, low_short, high_long, ...)."""
    cols = list(blocks.keys())
    specs = [
        ("≥20 Events", "pass_n"),
        ("≥2 Symbole", "pass_symbols"),
        ("≥10 Regime", "pass_regimes"),
        ("max. 30 % pro Regime", "pass_concentration"),
        ("overall", "pass"),
    ]
    rows = []
    for label, key in specs:
        row: dict[str, Any] = {"gate": label}
        for col in cols:
            long_g, short_g = blocks[col]
            # col encodes pool name; each block is (long, short) for that universe
            # For single-side pools we still store both; caller sets unused to fail
            row[f"{col}_long"] = "pass" if long_g.get(key) else "fail"
            row[f"{col}_short"] = "pass" if short_g.get(key) else "fail"
        rows.append(row)
    return rows


def decide_combined_primary(
    *,
    events: list[dict[str, Any]],
    combined_long_gate: dict[str, Any],
    combined_short_gate: dict[str, Any],
    low_events: list[dict[str, Any]],
    high_events: list[dict[str, Any]],
) -> tuple[str, str]:
    long_ok = bool(combined_long_gate.get("pass"))
    short_ok = bool(combined_short_gate.get("pass"))
    if long_ok and short_ok:
        return (
            "SUFFICIENT_COMBINED_LONG_AND_SHORT_SAMPLE_FOUND",
            "Combined Long and Short sample gates both pass.",
        )
    if long_ok and not short_ok:
        return (
            "SUFFICIENT_COMBINED_LONG_SAMPLE_ONLY",
            "Combined Long gate passes; Short insufficient.",
        )
    if short_ok and not long_ok:
        return (
            "SUFFICIENT_COMBINED_SHORT_SAMPLE_ONLY",
            "Combined Short gate passes; Long insufficient.",
        )

    n_low_bd = sum(1 for e in low_events if e.get("outcome") == "BREAKDOWN_CONFIRMED")
    n_low_rc = sum(1 for e in low_events if e.get("outcome") == "RECLAIM_CONFIRMED")
    n_high_bo = sum(1 for e in high_events if e.get("outcome") == "BREAKOUT_CONFIRMED")
    n_high_rd = sum(1 for e in high_events if e.get("outcome") == "RECLAIM_DOWN_CONFIRMED")

    # Asymmetry: low mostly breakdown and high does not balance with reclaim-down / shorts
    if n_low_bd >= 2 * max(n_low_rc, 1) and n_high_rd < max(n_low_bd // 2, 1):
        return (
            "ASYMMETRIC_LOW_BREAKDOWN_DOMINANCE",
            f"Low breakdowns dominate ({n_low_bd} vs reclaim {n_low_rc}) and High "
            f"reclaim-down ({n_high_rd}) does not balance.",
        )
    if n_high_bo >= 2 * max(n_high_rd, 1) and n_low_rc < max(n_high_bo // 2, 1):
        return (
            "ASYMMETRIC_HIGH_BREAKOUT_DOMINANCE",
            f"High breakouts dominate ({n_high_bo} vs reclaim-down {n_high_rd}) and Low "
            f"reclaim ({n_low_rc}) does not balance.",
        )

    n_valid = sum(
        1 for e in events if str(e.get("data_valid")).lower() in {"true", "1"}
    )
    if n_valid < 5:
        return ("COMBINED_SAMPLE_TOO_SMALL", f"Too few valid combined events ({n_valid}).")

    resolved = n_low_bd + n_low_rc + n_high_bo + n_high_rd
    if resolved < 5:
        return (
            "STRUCTURE_MIRROR_INSUFFICIENT_OVERALL",
            f"Too few resolved Low+High outcomes (resolved={resolved}).",
        )
    return (
        "COMBINED_SAMPLE_TOO_SMALL",
        f"Combined gates fail (low_bd={n_low_bd}, low_rc={n_low_rc}, "
        f"high_bo={n_high_bo}, high_rd={n_high_rd}).",
    )


def run_combined_structure_catalog(
    *,
    low_dir: Path = DEFAULT_LOW_DIR,
    high_dir: Path = DEFAULT_HIGH_DIR,
    output_dir: Path = DEFAULT_COMBINED_DIR,
    overwrite: bool = False,
    run_missing: bool = True,
) -> dict[str, Any]:
    """Merge Low+High artefacts into combined structure catalog."""
    low_dir = Path(low_dir)
    high_dir = Path(high_dir)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{output_dir} exists; pass overwrite=True")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Prefer High missing → run high; Low missing → run low
    if not (high_dir / "event_decisions.csv").exists() and run_missing:
        _ensure_catalog(side="HIGH", directory=high_dir, overwrite=True, run_if_missing=True)
    if not (low_dir / "event_decisions.csv").exists() and run_missing:
        _ensure_catalog(side="LOW", directory=low_dir, overwrite=True, run_if_missing=True)
    if not (high_dir / "event_decisions.csv").exists():
        raise FileNotFoundError(f"High catalog missing: {high_dir}")
    if not (low_dir / "event_decisions.csv").exists():
        raise FileNotFoundError(f"Low catalog missing: {low_dir}")

    events = _event_origin_rows(low_dir, high_dir)
    low_events = [e for e in events if e["origin"] == ORIGIN_LOW]
    high_events = [e for e in events if e["origin"] == ORIGIN_HIGH]
    cands = _combined_candidates(low_dir, high_dir)
    fwd = _combined_forward_returns(low_dir, high_dir)
    regime_mapping, regimes = _union_find_regimes(events)
    regime_by_event = {r["event_id"]: r["regime_id"] for r in regime_mapping}

    # Gate universes
    low_long, low_short = _gate_block(
        low_events,
        long_outcomes={"RECLAIM_CONFIRMED"},
        short_outcomes={"BREAKDOWN_CONFIRMED"},
        regime_by_event=regime_by_event,
    )
    high_long, high_short = _gate_block(
        high_events,
        long_outcomes={"BREAKOUT_CONFIRMED"},
        short_outcomes={"RECLAIM_DOWN_CONFIRMED"},
        regime_by_event=regime_by_event,
    )
    comb_long, comb_short = _gate_block(
        events,
        long_outcomes={"RECLAIM_CONFIRMED", "BREAKOUT_CONFIRMED"},
        short_outcomes={"BREAKDOWN_CONFIRMED", "RECLAIM_DOWN_CONFIRMED"},
        regime_by_event=regime_by_event,
    )

    gate_eval_rows = [
        {
            "universe": "low_only",
            "side": "long",
            **{k: low_long[k] for k in low_long},
        },
        {
            "universe": "low_only",
            "side": "short",
            **{k: low_short[k] for k in low_short},
        },
        {
            "universe": "high_only",
            "side": "long",
            **{k: high_long[k] for k in high_long},
        },
        {
            "universe": "high_only",
            "side": "short",
            **{k: high_short[k] for k in high_short},
        },
        {
            "universe": "combined",
            "side": "long",
            **{k: comb_long[k] for k in comb_long},
        },
        {
            "universe": "combined",
            "side": "short",
            **{k: comb_short[k] for k in comb_short},
        },
    ]

    primary, rationale = decide_combined_primary(
        events=events,
        combined_long_gate=comb_long,
        combined_short_gate=comb_short,
        low_events=low_events,
        high_events=high_events,
    )

    # Long / short pools
    long_pool = [c for c in cands if str(c.get("side")).upper() == "LONG"]
    short_pool = [c for c in cands if str(c.get("side")).upper() == "SHORT"]
    long_pool_summary = [
        {
            "origin": origin,
            "n": n,
            "share": n / len(long_pool) if long_pool else 0.0,
        }
        for origin, n in Counter(c["origin"] for c in long_pool).most_common()
    ]
    short_pool_summary = [
        {
            "origin": origin,
            "n": n,
            "share": n / len(short_pool) if short_pool else 0.0,
        }
        for origin, n in Counter(c["origin"] for c in short_pool).most_common()
    ]

    # Origin comparison
    origin_comparison = []
    for origin, subset in (
        (ORIGIN_LOW, low_events),
        (ORIGIN_HIGH, high_events),
    ):
        oc = Counter(str(e.get("outcome")) for e in subset)
        origin_comparison.append(
            {
                "origin": origin,
                "n_events": len(subset),
                "n_valid": sum(
                    1 for e in subset if str(e.get("data_valid")).lower() in {"true", "1"}
                ),
                **{f"n_{k}": v for k, v in oc.items()},
            }
        )

    # Direction symmetry
    direction_symmetry_summary = [
        {
            "metric": "n_low_breakdown",
            "value": sum(1 for e in low_events if e.get("outcome") == "BREAKDOWN_CONFIRMED"),
        },
        {
            "metric": "n_high_breakout",
            "value": sum(1 for e in high_events if e.get("outcome") == "BREAKOUT_CONFIRMED"),
        },
        {
            "metric": "n_low_reclaim",
            "value": sum(1 for e in low_events if e.get("outcome") == "RECLAIM_CONFIRMED"),
        },
        {
            "metric": "n_high_reclaim_down",
            "value": sum(
                1 for e in high_events if e.get("outcome") == "RECLAIM_DOWN_CONFIRMED"
            ),
        },
        {
            "metric": "n_regimes_with_direction_flip",
            "value": len(
                {
                    r["regime_id"]
                    for r in regime_mapping
                    if r.get("direction_flip_flag")
                }
            ),
        },
        {
            "metric": "n_combined_regimes",
            "value": len(regimes),
        },
        {
            "metric": "long_pool_n",
            "value": len(long_pool),
        },
        {
            "metric": "short_pool_n",
            "value": len(short_pool),
        },
    ]

    # Cross-symbol regime check
    no_cross = all(
        all(eid.split("_")[0] == rid.split("_")[0] or str(next(
            (e["symbol"] for e in events if e["event_id"] == eid), ""
        )) == rid.split("_COMB_REG_")[0]
            for eid in eids)
        for rid, eids in regimes.items()
    )

    decision_payload = {
        "primary_decision": primary,
        "rationale": rationale,
        "n_low_events": len(low_events),
        "n_high_events": len(high_events),
        "n_combined_events": len(events),
        "n_long_candidates": len(long_pool),
        "n_short_candidates": len(short_pool),
        "n_regimes": len(regimes),
        "gates": {
            "low_only": {"long": low_long, "short": low_short},
            "high_only": {"long": high_long, "short": high_short},
            "combined": {"long": comb_long, "short": comb_short},
        },
        "no_cross_symbol_regimes": no_cross,
    }

    _write_csv(
        output_dir / "combined_event_inventory.csv",
        events,
        headers=[
            "event_id",
            "symbol",
            "origin",
            "level",
            "break_available_at",
            "outcome",
            "decision_ts",
            "data_valid",
        ],
    )
    _write_csv(
        output_dir / "combined_candidates.csv",
        cands,
        headers=[
            "candidate_id",
            "event_id",
            "symbol",
            "side",
            "candidate_type",
            "candidate_ts",
            "candidate_price",
            "origin",
            "catalog",
        ],
    )
    _write_csv(output_dir / "combined_forward_returns.csv", fwd)
    _write_csv(
        output_dir / "combined_regime_mapping.csv",
        regime_mapping,
        headers=[
            "event_id",
            "symbol",
            "regime_id",
            "origin",
            "level",
            "break_available_at",
            "outcome",
            "direction_flip_flag",
            "origins_in_regime",
        ],
    )
    _write_csv(
        output_dir / "long_pool_summary.csv",
        long_pool_summary,
        headers=["origin", "n", "share"],
    )
    _write_csv(
        output_dir / "short_pool_summary.csv",
        short_pool_summary,
        headers=["origin", "n", "share"],
    )
    _write_csv(output_dir / "origin_comparison.csv", origin_comparison)
    _write_csv(
        output_dir / "direction_symmetry_summary.csv",
        direction_symmetry_summary,
        headers=["metric", "value"],
    )
    _write_csv(
        output_dir / "combined_sample_gate_evaluation.csv",
        gate_eval_rows,
    )
    _write_json(output_dir / "combined_decision.json", decision_payload)

    lines = [
        "# Combined Protected Structure Catalog",
        "",
        f"**Primäre Entscheidung: `{primary}`**",
        "",
        rationale,
        "",
        f"Low dir: `{low_dir}`",
        f"High dir: `{high_dir}`",
        f"Output: `{output_dir}`",
        "",
        f"- Low events: {len(low_events)}",
        f"- High events: {len(high_events)}",
        f"- Combined regimes: {len(regimes)}",
        f"- Long pool: {len(long_pool)} | Short pool: {len(short_pool)}",
        "",
        "## Combined gates",
        "",
        f"- Combined long pass: {comb_long.get('pass')} (n={comb_long.get('n_events')})",
        f"- Combined short pass: {comb_short.get('pass')} (n={comb_short.get('n_events')})",
        "",
    ]
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    return {
        "decision": primary,
        "rationale": rationale,
        "n_low": len(low_events),
        "n_high": len(high_events),
        "gates": decision_payload["gates"],
        "gate_eval_rows": gate_eval_rows,
        "output_dir": str(output_dir),
        "no_cross_symbol_regimes": no_cross,
        "primary_decisions": COMBINED_PRIMARY_DECISIONS,
    }


__all__ = [
    "run_combined_structure_catalog",
    "decide_combined_primary",
    "COMBINED_PRIMARY_DECISIONS",
    "DEFAULT_COMBINED_DIR",
]
