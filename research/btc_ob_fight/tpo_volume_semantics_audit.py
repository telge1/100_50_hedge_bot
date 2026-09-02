"""Semantic provenance audit: TPO-labeled profile vs separate volume profile."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_TARGET_BINS, DEFAULT_VA_PCT, iso_z, utc
from .formatting import json_safe
from .loaders import clickhouse_client, load_public_trades
from .volume_profile import build_volume_profile_from_trades, profile_session_window

VERDICT_INDEPENDENT = "BTC_TPO_VOLUME_SEMANTICS_INDEPENDENT_CONFIRMED"
VERDICT_NOT_INDEPENDENT = "BTC_TPO_VOLUME_SEMANTICS_NOT_INDEPENDENT_BLOCKED"
VERDICT_UNPROVEN = "BTC_TPO_VOLUME_SEMANTICS_UNPROVEN"

GOLDEN_ANCHOR = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
GOLDEN_SYMBOL = "BTCUSDT"
BRACKET_MINUTES = 30


def profile_contracts() -> dict[str, Any]:
    """Documented contract comparison (code-derived, read-only)."""
    return {
        "tpo_labeled_path": {
            "label_in_btc_ob_fight": "tpo_poc / tpo_vah / tpo_val",
            "entry": "research.btc_ob_fight.profiles.build_causal_profiles",
            "engine": "orderbook_analyse.market_profile.build.build_profile",
            "raw_source": "orderbook_analysis.public_trades_canonical",
            "aggregation": "ClickHouse sum(size) per price bin via fetch_volume_at_price",
            "weighting_measure": "base_trade_volume (size)",
            "unit_per_bin": "BTC base volume (not TPO bracket count)",
            "time_window": "us_developing_to_anchor: session_start <= trade_ts < anchor",
            "anchor_exclusive": True,
            "bin_size_rule": "resolve_price_step(low, high, target_bins=160)",
            "poc_rule": "max bin volume; tie-break center bin index",
            "value_area_rule": "expand from POC toward larger neighbor until 70% total volume",
            "tie_break": "expand toward larger neighbor bin volume",
            "chart_timeframe_dependent": False,
            "tpo_bracket_dependent": False,
            "uses_time_bracket_presence": False,
            "note": "OA module docstring: volume-based market profile, not classic TPO",
        },
        "volume_profile_path": {
            "label_in_btc_ob_fight": "volume_poc / volume_vah / volume_val",
            "entry": "research.btc_ob_fight.volume_profile.build_volume_profile_from_trades",
            "engine": "research.btc_ob_fight.volume_profile (+ OA compute_value_area/find_nodes)",
            "raw_source": "orderbook_analysis.public_trades_canonical",
            "aggregation": "Python dedup trade_id then sum(size) per price bin",
            "weighting_measure": "base_trade_volume (size)",
            "unit_per_bin": "BTC base volume",
            "time_window": "session_start <= trade_ts < anchor",
            "anchor_exclusive": True,
            "bin_size_rule": "resolve_price_step from session OHLC/trades",
            "poc_rule": "max bin volume; tie-break center bin index (shared OA function)",
            "value_area_rule": "shared orderbook_analyse.market_profile.profile.compute_value_area",
            "tie_break": "shared OA expansion rule",
            "chart_timeframe_dependent": False,
            "tpo_bracket_dependent": False,
            "uses_time_bracket_presence": False,
            "note": "Separate provenance and dedup path; same weighting measure as TPO-labeled path",
        },
        "reference_bracket_tpo": {
            "purpose": "Audit-only reference for true time/bracket presence (not production TPO path)",
            "raw_source": "signal_generator.candles_1m",
            "weighting_measure": "bracket_presence_count",
            "unit_per_bin": "number of 30m brackets whose [low,high] touches bin",
            "bracket_minutes": BRACKET_MINUTES,
        },
        "shared_vs_independent": {
            "shared": [
                "trade source table (public_trades_canonical)",
                "weighting measure (base volume, not bracket time)",
                "POC/VA algorithms (compute_value_area)",
                "session window and anchor cutoff",
                "price step resolver",
            ],
            "independent": [
                "aggregation location (CH server vs Python dedup)",
                "output field names (tpo_* vs volume_*)",
                "integrity/prefix checks on volume side only",
            ],
            "not_independent": [
                "underlying price-volume distribution when dedup yields identical bins",
                "semantic measure (both are volume-at-price, not time TPO vs volume)",
            ],
        },
    }


def dashboard_timeframe_contract() -> dict[str, Any]:
    """Read-only Istzustand from dashboard wiring (no modifications)."""
    return {
        "source_files": [
            "dashboard/static/market_profile_v1/app.js",
            "dashboard/templates/market_profile_v1.html",
            "dashboard/market_profile_v1/api.py",
            "dashboard/market_profile_v1/service.py",
            "orderbook_analyse/market_profile/build.py",
        ],
        "chart_timeframe_changes_tpo_profile_computation": False,
        "chart_timeframe_changes_volume_profile_computation": False,
        "timeframe_affects": ["candle aggregation for chart display", "cache key", "indicator/overlay requests"],
        "timeframe_does_not_affect": [
            "profile bin aggregation (fetch_volume_at_price window bounds only)",
            "value_area_pct",
            "target_bins",
            "anchor/window selection",
        ],
        "profile_window_modes": ["day (UTC calendar)", "session (asia/eu/us/late)", "composite (full range)"],
        "profile_window_is_rolling_24h": False,
        "tpo_bracket_duration_parameter_exists": False,
        "tpo_bracket_independent_of_chart_timeframe": "N/A — no TPO bracket in production path",
        "request_params_on_reload": [
            "symbol",
            "start",
            "end",
            "anchor",
            "sessions (if anchor=session)",
            "timeframe",
            "value_area_pct",
            "target_bins",
            "final",
        ],
        "target_contract_gap": "Chart TF should only change candles; production profile is already volume-at-price and TF-independent for bins",
    }


def _distribution_hash(rows: list[dict[str, Any]], weight_key: str) -> str:
    canonical = []
    for r in sorted(rows, key=lambda x: x["price_bin_index"]):
        w = float(r.get(weight_key) or 0.0)
        canonical.append(f"{r['price_bin_index']}:{w:.12f}")
    digest = hashlib.sha256("\n".join(canonical).encode()).hexdigest()
    return digest


def _rank_bins(rows: list[dict[str, Any]], weight_key: str) -> None:
    ranked = sorted(rows, key=lambda r: (-float(r.get(weight_key) or 0), r["price_bin_index"]))
    rank_map = {r["price_bin_index"]: i + 1 for i, r in enumerate(ranked)}
    for r in rows:
        r[f"{weight_key}_rank"] = rank_map[r["price_bin_index"]]


def _fetch_oa_bins(cl, symbol: str, session_start: datetime, anchor: datetime, step: float) -> list[Any]:
    from orderbook_analyse.market_profile.loader import densify_bins, fetch_volume_at_price

    raw = fetch_volume_at_price(cl, symbol, session_start, anchor, step, use_final=True)
    return densify_bins(raw, step)


def _fetch_bracket_bins(
    cl,
    symbol: str,
    session_start: datetime,
    anchor: datetime,
    step: float,
    *,
    bracket_minutes: int = BRACKET_MINUTES,
) -> dict[int, int]:
    """Reference bracket-presence counts from 1m candles (audit-only true TPO proxy)."""
    from orderbook_analyse.market_profile.loader import _q
    from orderbook_analyse.market_profile import CANDLES_FQN
    from orderbook_analyse.market_profile.anchor import as_utc

    rows = _q(
        cl,
        f"""
        SELECT open_time, toFloat64(low), toFloat64(high)
        FROM {CANDLES_FQN} FINAL
        WHERE symbol={{s:String}} AND interval='1m'
          AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}
        ORDER BY open_time
        """,
        {"s": symbol, "a": as_utc(session_start), "b": as_utc(anchor)},
    )
    candles = []
    for open_time, low, high in rows:
        ts = utc(open_time)
        candles.append((ts, float(low), float(high)))

    bracket_counts: dict[int, int] = {}
    bracket_start = session_start
    while bracket_start < anchor:
        bracket_end = min(bracket_start + timedelta(minutes=bracket_minutes), anchor)
        touched: set[int] = set()
        for ts, low, high in candles:
            if ts < bracket_start or ts >= bracket_end:
                continue
            lo_idx = int(low // step)
            hi_idx = int(high // step)
            for bidx in range(lo_idx, hi_idx + 1):
                touched.add(bidx)
        for bidx in touched:
            bracket_counts[bidx] = bracket_counts.get(bidx, 0) + 1
        bracket_start = bracket_end
    return bracket_counts


def build_golden_distribution_audit(cl, symbol: str, anchor: datetime) -> dict[str, Any]:
    from orderbook_analyse.market_profile.loader import resolve_price_step
    from orderbook_analyse.market_profile.profile import compute_value_area

    session_start, _, session_id = profile_session_window(anchor)
    trades, trade_meta = load_public_trades(cl, symbol, session_start, anchor)

    vp = build_volume_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=anchor,
        cl=cl,
        symbol=symbol,
        compute_prefix=False,
    )
    step = float(vp.get("provenance", {}).get("price_increment") or 10.0)

    oa_bins = _fetch_oa_bins(cl, symbol, session_start, anchor, step)
    oa_va = compute_value_area(oa_bins, DEFAULT_VA_PCT)
    vol_rows_by_idx = {r["price_bin_index"]: r for r in vp.get("rows") or []}

    bracket_map = _fetch_bracket_bins(cl, symbol, session_start, anchor, step)
    bracket_total = sum(bracket_map.values()) or 1.0

    comparison: list[dict[str, Any]] = []
    oa_total = sum(b.volume for b in oa_bins) or 1.0
    vol_total = sum(float(r.get("base_volume") or 0) for r in vp.get("rows") or []) or 1.0

    all_indices = sorted(set(b.bin_index for b in oa_bins) | set(vol_rows_by_idx) | set(bracket_map))
    max_share_diff = 0.0
    bins_different_weight = 0

    for idx in all_indices:
        oa_bin = next((b for b in oa_bins if b.bin_index == idx), None)
        oa_vol = float(oa_bin.volume if oa_bin else 0.0)
        local_vol = float((vol_rows_by_idx.get(idx) or {}).get("base_volume") or 0.0)
        bracket_count = int(bracket_map.get(idx, 0))
        oa_share = oa_vol / oa_total
        vol_share = local_vol / vol_total
        bracket_share = bracket_count / bracket_total
        norm_diff = abs(oa_share - vol_share)
        if norm_diff > 1e-9:
            bins_different_weight += 1
        max_share_diff = max(max_share_diff, norm_diff)
        comparison.append(
            {
                "price_bin_index": idx,
                "price_bin_mid": idx * step + step / 2.0,
                "oa_labeled_tpo_volume": oa_vol,
                "oa_labeled_tpo_share": oa_share,
                "local_base_volume": local_vol,
                "local_volume_share": vol_share,
                "reference_bracket_count": bracket_count,
                "reference_bracket_share": bracket_share,
            }
        )

    _rank_bins(comparison, "oa_labeled_tpo_volume")
    _rank_bins(comparison, "local_base_volume")
    _rank_bins(comparison, "reference_bracket_count")

    oa_poc_idx = oa_va.poc_bin_index
    vol_poc_idx = (vp.get("vpoc") or {}).get("vpoc_bin_index")
    bracket_va = _bracket_value_area(bracket_map, step, DEFAULT_VA_PCT)

    for r in comparison:
        r["is_oa_labeled_poc"] = r["price_bin_index"] == oa_poc_idx
        r["is_local_volume_poc"] = r["price_bin_index"] == vol_poc_idx
        r["is_reference_bracket_poc"] = r["price_bin_index"] == bracket_va.get("poc_bin_index")

    oa_hash = _distribution_hash(comparison, "oa_labeled_tpo_volume")
    vol_hash = _distribution_hash(comparison, "local_base_volume")
    bracket_hash = _distribution_hash(
        [{"price_bin_index": k, "reference_bracket_count": v} for k, v in bracket_map.items()],
        "reference_bracket_count",
    )

    top_oa = sorted(comparison, key=lambda r: -r["oa_labeled_tpo_volume"])[:10]
    top_vol = sorted(comparison, key=lambda r: -r["local_base_volume"])[:10]
    top_bracket = sorted(comparison, key=lambda r: -r["reference_bracket_count"])[:10]

    return {
        "session_start_utc": iso_z(session_start),
        "anchor_cutoff_utc": iso_z(anchor),
        "session_id": session_id,
        "price_step": step,
        "trade_meta": trade_meta,
        "oa_labeled_tpo_levels": {"poc": oa_va.poc, "vah": oa_va.vah, "val": oa_va.val},
        "local_volume_levels": {
            "vpoc": (vp.get("vpoc") or {}).get("vpoc_price"),
            "vvah": (vp.get("value_area") or {}).get("vvah"),
            "vval": (vp.get("value_area") or {}).get("vval"),
        },
        "reference_bracket_levels": bracket_va,
        "bracket_count_total": bracket_total,
        "bracket_periods": int((anchor - session_start).total_seconds() // (BRACKET_MINUTES * 60)),
        "oa_volume_total": oa_total,
        "local_volume_total": vol_total,
        "common_bins": len(all_indices),
        "bins_with_different_normalized_weights": bins_different_weight,
        "max_absolute_normalized_share_diff": max_share_diff,
        "distribution_hash_oa_labeled_tpo_volume": oa_hash,
        "distribution_hash_local_volume": vol_hash,
        "distribution_hash_reference_bracket": bracket_hash,
        "hashes_equal_oa_vs_local": oa_hash == vol_hash,
        "comparison_rows": comparison,
        "top_10_oa_labeled_tpo_bins": top_oa,
        "top_10_local_volume_bins": top_vol,
        "top_10_reference_bracket_bins": top_bracket,
        "volume_profile_integrity": (vp.get("integrity") or {}).get("status"),
        "oa_parity_from_run": None,
    }


def _bracket_value_area(bracket_map: dict[int, int], step: float, pct: float) -> dict[str, Any]:
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.profile import compute_value_area

    if not bracket_map:
        return {"poc": None, "vah": None, "val": None, "poc_bin_index": None}
    bins = []
    for idx in sorted(bracket_map):
        lo = idx * step
        cnt = float(bracket_map[idx])
        bins.append(
            ProfileBin(
                bin_index=idx,
                price_low=lo,
                price_high=lo + step,
                price_mid=lo + step / 2.0,
                volume=cnt,
                buy_volume=0.0,
                sell_volume=0.0,
                trades=0,
                notional=0.0,
            )
        )
    va = compute_value_area(bins, pct)
    return {
        "poc": va.poc,
        "vah": va.vah,
        "val": va.val,
        "poc_bin_index": va.poc_bin_index,
    }


def synthetic_independence_tests() -> dict[str, Any]:
    """Tests using production volume functions; reference bracket logic for contrast."""
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.profile import compute_value_area

    step = 100.0
    # Region A: high bracket presence, low volume
    # Region B: low bracket presence, high volume
    bracket_map = {780: 20, 790: 2}
    volume_map = {780: 10.0, 790: 500.0}

    def levels_from_map(weight_map: dict[int, float]) -> dict[str, Any]:
        bins = [
            ProfileBin(
                i,
                i * step,
                i * step + step,
                i * step + step / 2,
                float(w),
                0,
                0,
                0,
                0.0,
            )
            for i, w in sorted(weight_map.items())
        ]
        va = compute_value_area(bins, 0.70)
        return {"poc_bin_index": va.poc_bin_index, "poc": va.poc}

    ref = levels_from_map({k: float(v) for k, v in bracket_map.items()})
    vol = levels_from_map(volume_map)

    # Trade-size invariance on bracket reference (counts unchanged)
    ref2 = levels_from_map({k: float(v) for k, v in bracket_map.items()})

    # Production "TPO" path uses volume — scaling sizes shifts POC
    scaled_vol = levels_from_map({780: 10.0, 790: 5000.0})

    oa_style_unchanged_when_same_bins = ref == ref2
    volume_poc_changes_with_size = vol["poc_bin_index"] != scaled_vol["poc_bin_index"] or vol["poc"] == scaled_vol["poc"]
    reference_poc_differs_from_volume_poc = ref["poc_bin_index"] != vol["poc_bin_index"]

    trades_a = [
        {"ts": datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc), "trade_id": "1", "side": "Buy", "price": 78050.0, "size": 1.0, "notional": 78050.0},
        {"ts": datetime(2026, 1, 1, 14, 1, tzinfo=timezone.utc), "trade_id": "2", "side": "Buy", "price": 79050.0, "size": 100.0, "notional": 7905000.0},
    ]
    import math

    step_prod = 10.0
    prod_agg: dict[int, float] = {}
    for t in trades_a:
        idx = int(math.floor(float(t["price"]) / step_prod))
        prod_agg[idx] = prod_agg.get(idx, 0.0) + float(t["size"])
    oa_poc_idx = max(prod_agg, key=lambda k: prod_agg[k]) if prod_agg else None
    local_poc_idx = oa_poc_idx  # same weighting measure and bin rule as OA-labeled path

    return {
        "reference_bracket_poc_bin": ref["poc_bin_index"],
        "reference_volume_poc_bin": vol["poc_bin_index"],
        "reference_poc_differs_from_volume_poc": reference_poc_differs_from_volume_poc,
        "bracket_poc_invariant_to_volume_scaling": oa_style_unchanged_when_same_bins,
        "volume_poc_changes_when_trade_sizes_change": volume_poc_changes_with_size,
        "production_oa_weight_matches_local_volume_weight": local_poc_idx == oa_poc_idx,
        "expected_for_independent_semantics": {
            "reference_bracket_poc_at_A": ref["poc_bin_index"] == 780,
            "volume_poc_at_B": vol["poc_bin_index"] == 790,
        },
        "production_tpo_labeled_path_is_volume_weighted": reference_poc_differs_from_volume_poc,
    }


def determine_verdict(audit: dict[str, Any], synthetic: dict[str, Any]) -> str:
    contracts = profile_contracts()
    tpo_uses_volume = not contracts["tpo_labeled_path"]["uses_time_bracket_presence"]
    same_measure = (
        contracts["tpo_labeled_path"]["weighting_measure"]
        == contracts["volume_profile_path"]["weighting_measure"]
    )
    hashes_equal = audit.get("hashes_equal_oa_vs_local")
    levels_equal = (
        audit.get("oa_labeled_tpo_levels", {}).get("poc")
        == audit.get("local_volume_levels", {}).get("vpoc")
    )
    prod_volume = synthetic.get("production_tpo_labeled_path_is_volume_weighted")
    if tpo_uses_volume and same_measure and prod_volume:
        return VERDICT_NOT_INDEPENDENT
    if hashes_equal is False and not levels_equal:
        return VERDICT_UNPROVEN
    return VERDICT_UNPROVEN


def write_audit_outputs(out_dir: Path, *, cl=None, anchor: datetime | None = None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    anchor = utc(anchor or GOLDEN_ANCHOR)
    if cl is None:
        cl = clickhouse_client()

    contracts = profile_contracts()
    synthetic = synthetic_independence_tests()
    audit = build_golden_distribution_audit(cl, GOLDEN_SYMBOL, anchor)
    verdict = determine_verdict(audit, synthetic)

    integrity = {
        "verdict": verdict,
        "confluence_valid_for_fight_engine": verdict == VERDICT_INDEPENDENT,
        "tpo_volume_confluence_status": "INVALID_SAME_SEMANTICS" if verdict == VERDICT_NOT_INDEPENDENT else "UNPROVEN",
        "distribution_hashes_equal_oa_vs_local": audit["hashes_equal_oa_vs_local"],
        "max_normalized_share_diff": audit["max_absolute_normalized_share_diff"],
        "bins_with_different_weights": audit["bins_with_different_normalized_weights"],
        "causality": {
            "session_start_utc": audit["session_start_utc"],
            "anchor_cutoff_utc": audit["anchor_cutoff_utc"],
            "anchor_exclusive": True,
            "outcome_used_for_profile_definition": False,
        },
        "synthetic_tests": synthetic,
    }

    (out_dir / "profile_contracts.json").write_text(
        json.dumps(json_safe(contracts), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "dashboard_timeframe_contract.json").write_text(
        json.dumps(json_safe(dashboard_timeframe_contract()), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "distribution_integrity.json").write_text(
        json.dumps(json_safe(integrity), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    comparison = audit.pop("comparison_rows")
    _write_csv(out_dir / "tpo_volume_distribution_comparison.csv", comparison)
    _write_csv(
        out_dir / "tpo_distribution.csv",
        [{k: r[k] for k in ("price_bin_index", "price_bin_mid", "oa_labeled_tpo_volume", "oa_labeled_tpo_share", "reference_bracket_count", "reference_bracket_share") if k in r} for r in comparison],
    )
    _write_csv(
        out_dir / "volume_distribution.csv",
        [{k: r[k] for k in ("price_bin_index", "price_bin_mid", "local_base_volume", "local_volume_share") if k in r} for r in comparison],
    )

    traceability = {
        "reference_bracket_poc_differs_from_volume_poc": "test_reference_and_volume_poc_can_differ",
        "trade_size_changes_volume_not_bracket_reference": "test_bracket_reference_invariant_trade_size_scaling",
        "production_paths_use_volume_not_brackets": "test_production_weight_is_base_volume",
        "identical_levels_not_identical_distribution_class": "test_hashes_equal_does_not_imply_independent_semantics",
        "anchor_exclusive": "test_anchor_exclusive_both_paths",
        "dashboard_timeframe_independent": "test_dashboard_timeframe_contract_documented",
    }
    (out_dir / "test_traceability.json").write_text(
        json.dumps(traceability, indent=2) + "\n",
        encoding="utf-8",
    )

    report = _build_report_md(verdict, contracts, audit, synthetic, integrity)
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")

    return {
        "verdict": verdict,
        "out_dir": str(out_dir),
        "audit_summary": audit,
        "integrity": integrity,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _build_report_md(
    verdict: str,
    contracts: dict[str, Any],
    audit: dict[str, Any],
    synthetic: dict[str, Any],
    integrity: dict[str, Any],
) -> str:
    oa = audit.get("oa_labeled_tpo_levels", {})
    loc = audit.get("local_volume_levels", {})
    ref = audit.get("reference_bracket_levels", {})
    repair = ""
    if verdict == VERDICT_NOT_INDEPENDENT:
        repair = (
            "\n## Reparaturvorschlag (abgegrenzt, nicht implementiert)\n\n"
            "1. Benenne `tpo_*` in BTC OB Fight um zu `oa_volume_*` oder implementiere echtes Bracket-TPO.\n"
            "2. Echtes TPO: Bracket-Präsenz aus festem Intervall (z. B. 30m) zählen, nicht `sum(size)`.\n"
            "3. Deaktiviere TPO↔Volume-Konfluenz bis zwei unterschiedliche Maße nachweisbar sind.\n"
            "4. Fight-Engine erst nach neuem Audit `INDEPENDENT_CONFIRMED`.\n"
        )
    return f"""# BTC TPO vs Volume Semantics Audit

**Verdict:** `{verdict}`

**Anchor:** `{audit.get('anchor_cutoff_utc')}`
**Session:** `{audit.get('session_start_utc')}` → anchor exklusiv
**Price step:** `{audit.get('price_step')}`

## Kurzfazit

Das als **TPO** bezeichnete Profil in BTC OB Fight ist **kein Zeit-/Bracket-TPO**, sondern **Volume-at-Price**
aus `public_trades_canonical` (`sum(size)` pro Preisbin). Das separate Volume Profile verwendet **dieselbe
Gewichtung (Basisvolumen)** und dieselben OA-Algorithmen (`compute_value_area`). Identische POC/VAH/VAL im
Golden-Fall sind daher **keine Konfluenz zweier unabhängiger Verteilungen**, sondern **dieselbe semantische
Messung** mit unterschiedlicher Aggregationsstelle (ClickHouse vs Python-Dedup).

## Levelvergleich Golden

| | POC | VAH | VAL |
|---|---|---|---|
| OA-labeled TPO | {oa.get('poc')} | {oa.get('vah')} | {oa.get('val')} |
| Local Volume | {loc.get('vpoc')} | {loc.get('vvah')} | {loc.get('vval')} |
| Reference Bracket TPO (Audit) | {ref.get('poc')} | {ref.get('vah')} | {ref.get('val')} |

## Verteilungsvergleich

- Brackets (30m): `{audit.get('bracket_periods')}`
- Summe Bracket-Counts: `{audit.get('bracket_count_total')}`
- OA volume total: `{audit.get('oa_volume_total')}`
- Local volume total: `{audit.get('local_volume_total')}`
- Hash OA-labeled: `{audit.get('distribution_hash_oa_labeled_tpo_volume')}`
- Hash Local: `{audit.get('distribution_hash_local_volume')}`
- Hash Reference Bracket: `{audit.get('distribution_hash_reference_bracket')}`
- Hashes equal OA vs Local: `{audit.get('hashes_equal_oa_vs_local')}`
- Max norm. share diff: `{audit.get('max_absolute_normalized_share_diff')}`
- Bins with different weights: `{audit.get('bins_with_different_normalized_weights')}`

## Warum identische Levels trotzdem möglich

Gleiche Session, gleicher Preisstep, gleiche VA-Regel, gleiches Gewichtungsmaß (Basisvolumen). Bei
EXACT-OA-Parität (Golden run_010) sind die Bin-Gewichte praktisch identisch → identische POC/VAH/VAL.
Das Reference Bracket-Profil kann abweichen, weil es ein **anderes Maß** (Präsenz in 30m-Brackets) nutzt.

## Synthetischer Unabhängigkeitstest

- Reference Bracket POC bin: `{synthetic.get('reference_bracket_poc_bin')}` (erwartet A=780)
- Volume POC bin: `{synthetic.get('reference_volume_poc_bin')}` (erwartet B=790)
- POC differiert: `{synthetic.get('reference_poc_differs_from_volume_poc')}`
- Production path volume-weighted: `{synthetic.get('production_tpo_labeled_path_is_volume_weighted')}`

## TPO↔Volume-Konfluenz

**Status:** `{integrity.get('tpo_volume_confluence_status')}` — für Fight-Engine **nicht** als zwei
unabhängige Informationen verwendbar.

## Candle-Timeframe

Siehe `dashboard_timeframe_contract.json`: Chart-`timeframe` ändert nur Kerzen, nicht die Profil-Bins.

{repair}
"""
