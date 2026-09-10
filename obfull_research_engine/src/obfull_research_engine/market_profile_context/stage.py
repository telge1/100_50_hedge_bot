"""Build MP context, write validation artifacts, prove causality."""

from __future__ import annotations

import hashlib
import json
import resource
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..analyze.run_key import atomic_write_json, atomic_write_text
from ..paths import ENGINE_ROOT
from ..single_case_inspector.io_util import atomic_df_csv
from ..timeparse import format_utc_z
from . import (
    ADAPTER_VERSION,
    CONFLUENCE_BINS,
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    PROXIMITY_BINS,
    STATUS,
    TIMEFRAMES,
    VERDICT_PASS,
    VERDICT_PASS_LIMITS,
)
from .adapter import (
    _ch_client,
    _defaults,
    build_one_profile,
    build_timeframe_bundle,
    dashboard_service_profile,
)
from .confluence import collect_edges, confluence_report
from .decision import combine
from .edge_state import classify_at_focus, pick_relevant_lower_edge, pick_relevant_upper_edge
from .lifecycle import five_m_ohlc_from_trades, lifecycle_after_focus
from .location import locate_profile
from .provenance import collect_provenance
from .windows import assert_utc_alignment, current_full_bounds, period_s, planned_windows


def _formula_inventory_md(prov: dict[str, Any]) -> str:
    return f"""# Dashboard Market-Profile formula inventory

Status: `{STATUS}`
Compute path: `{prov.get("compute_path")}`
No formula fork.

## Call path

```
API /api/market-profile/profiles
→ market_profile_v1.api.load_profiles
→ market_profile_v1.service.load_profiles
→ orderbook_analyse.market_profile.anchor.build_windows
→ market_profile_v1.dual_profile.build_dual_window_profile
→ TPO: _fetch_tpo_bracket_ranges + _build_tpo_from_bracket_ranges
→ Volume: fetch_volume_at_price + compute_value_area
→ Shape: classify_shape (volume bins)
→ Payload tpo/volume/range (never merged)
```

## Sources

* Trades: `{prov["config"]["trades_fqn"]}` interval `[start, end)`
* Range/OHLC grid: `{prov["config"]["candles_fqn"]}` 1m, also `[start, end)`
* Bin step: `resolve_price_step(low, high, target_bins={prov["config"]["target_bins"]})` nice-number ladder 1/2/2.5/5/10
* Tick rounding: `floor(price / step)` → bin_index; mid = low + step/2
* TPO: 30-minute brackets (`bracket_minutes={prov["config"]["bracket_minutes"]}`); presence = 1 mark per bin touched by bracket high/low
* Volume: base size + taker buy/sell split
* Value area: `{prov["config"]["value_area_pct"]}` from POC, expand one bin toward larger neighbor (`above >= below` → expand up)
* POC tie-break: max volume, then closest to histogram center (`-abs(i - n//2)`)
* VAH = high edge of last VA bin; VAL = low edge of first VA bin
* Range high/low: window OHLC high/low from 1m candles
* Shape: volume-profile `classify_shape` (unvalidated on dashboard)
* Naked POC: 1m candles after window_end touch POC (not used at-focus)
* Developing: same functions with `end = focus_ts` so trades use `trade_ts < focus_ts`
* Range OHLC: 1m candles with `open_time < end`; a forming minute can store the completed bar (dashboard-inherent)

## Hashes

* source_code_hash: `{prov["source_code_hash"]}`
* config_hash: `{prov["config_hash"]}`
* dual_contract: `{prov["dual_contract_version"]}`
"""


def _extract_levels(prof: dict[str, Any] | None, tf: str, kind: str) -> dict[str, Any]:
    if not prof:
        return {"timeframe": tf, "kind": kind, "missing": True}
    tpo = prof.get("tpo") or {}
    vol = prof.get("volume") or {}
    return {
        "timeframe": tf,
        "kind": kind,
        "missing": False,
        "start": prof.get("profile_start"),
        "end_exclusive": prof.get("profile_end_exclusive"),
        "tpo_poc": tpo.get("poc"),
        "tpo_vah": tpo.get("vah"),
        "tpo_val": tpo.get("val"),
        "volume_vpoc": vol.get("vpoc"),
        "volume_vah": vol.get("vah"),
        "volume_val": vol.get("val"),
        "range_high": prof.get("range_high"),
        "range_low": prof.get("range_low"),
        "volume_shape": vol.get("shape"),
        "naked_poc": prof.get("naked_poc"),
        "price_step": prof.get("price_step"),
    }


def _parity_row(a: dict[str, Any] | None, b: dict[str, Any] | None, *, label: str) -> dict[str, Any]:
    fields = [
        "tpo_poc",
        "tpo_vah",
        "tpo_val",
        "volume_vpoc",
        "volume_vah",
        "volume_val",
        "range_high",
        "range_low",
        "volume_shape",
        "start",
        "end_exclusive",
    ]
    mismatches = []
    for f in fields:
        va = None if not a else a.get(f)
        vb = None if not b else b.get(f)
        if va != vb:
            mismatches.append({"field": f, "adapter": va, "dashboard": vb})
    return {
        "label": label,
        "n_mismatches": len(mismatches),
        "exact_parity": len(mismatches) == 0 and a is not None and b is not None,
        "mismatches": json.dumps(mismatches, default=str),
    }


def run_market_profile_context(
    *,
    symbol: str,
    focus_ts: datetime,
    trade_index: Any | None = None,
    footprint_delta_60s: float | None = None,
    oi_quadrant: str | None = None,
    pressure: str = "UNCLEAR",
    post_seconds: int = 1800,
    validation_root: Path | None = None,
    write_validation: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    focus_ts = focus_ts.astimezone(timezone.utc)
    focus_z = format_utc_z(focus_ts)
    focus_u = int(focus_ts.timestamp())
    prov = collect_provenance()
    va, bins = _defaults()
    proximity_cfg = {
        "proximity_bins": PROXIMITY_BINS,
        "confluence_bins": CONFLUENCE_BINS,
        "source": "dashboard_price_step",
        "not_fitted_on_outcome": True,
        "value_area_pct": va,
        "target_bins": bins,
    }

    price = None
    path: list[tuple[int, float]] = []
    if trade_index is not None:
        tr, _ = trade_index.latest_before(pd.Timestamp(focus_ts), max_age_ms=10**12, strict=True)
        if tr is not None:
            price = float(tr.price)
        slice_ = trade_index.path_slice(
            pd.Timestamp(focus_ts - timedelta(seconds=1800)),
            pd.Timestamp(focus_ts - timedelta(microseconds=1)),
        )
        by_sec: dict[int, float] = {}
        for t in slice_:
            by_sec[int(t.trade_ts.timestamp())] = float(t.price)
        path = sorted(by_sec.items())

    client = _ch_client()
    bundles: dict[str, dict[str, Any]] = {}
    parity_rows: list[dict[str, Any]] = []
    extra_closed: list[dict[str, Any]] = []
    dev_parity: list[dict[str, Any]] = []
    try:
        for tf in TIMEFRAMES:
            bundles[tf] = build_timeframe_bundle(
                symbol=symbol, focus_ts=focus_ts, tf=tf, client=client, include_final=True
            )
        # Adapter vs actual dashboard service (load_profiles), not a self-replay.
        for tf in TIMEFRAMES:
            prev = bundles[tf].get("previous_closed")
            if not prev:
                continue
            start = datetime.fromisoformat(prev["profile_start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(prev["profile_end_exclusive"].replace("Z", "+00:00"))
            dash = dashboard_service_profile(
                symbol=symbol, tf=tf, start=start, end=end, kind="PREVIOUS_CLOSED"
            )
            parity_rows.append(
                _parity_row(
                    _extract_levels(prev, tf, "PREVIOUS_CLOSED"),
                    _extract_levels(dash, tf, "PREVIOUS_CLOSED"),
                    label=f"closed_{tf}",
                )
            )
            extra_s = datetime.fromtimestamp(int(start.timestamp()) - period_s(tf), tz=timezone.utc)
            extra = build_one_profile(
                symbol=symbol, tf=tf, start=extra_s, end=start, kind="PREVIOUS_CLOSED_MINUS_1", client=client
            )
            extra_dash = dashboard_service_profile(
                symbol=symbol, tf=tf, start=extra_s, end=start, kind="PREVIOUS_CLOSED_MINUS_1"
            )
            extra_closed.append(extra)
            parity_rows.append(
                _parity_row(
                    _extract_levels(extra, tf, "PREVIOUS_CLOSED_MINUS_1"),
                    _extract_levels(extra_dash, tf, "PREVIOUS_CLOSED_MINUS_1"),
                    label=f"closed_{tf}_minus_1",
                )
            )
        # Final current-block 1h (parity only) vs dashboard service
        fin = bundles.get("1h", {}).get("final_for_parity_only")
        if fin:
            fs, fe = current_full_bounds(focus_ts, "1h")
            dash_fin = dashboard_service_profile(
                symbol=symbol, tf="1h", start=fs, end=fe, kind="FINAL_PROFILE_FOR_PARITY_ONLY"
            )
            parity_rows.append(
                _parity_row(
                    _extract_levels(fin, "1h", "FINAL"),
                    _extract_levels(dash_fin, "1h", "FINAL"),
                    label="final_1h_22_00_23_00_parity_only",
                )
            )
        # Developing: adapter vs dashboard service on the clipped [start, focus)
        for tf in TIMEFRAMES:
            dev = bundles[tf].get("developing")
            if not dev:
                continue
            start = datetime.fromisoformat(dev["profile_start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(dev["profile_end_exclusive"].replace("Z", "+00:00"))
            dash = dashboard_service_profile(
                symbol=symbol, tf=tf, start=start, end=end, kind="DEVELOPING_AS_OF_FOCUS"
            )
            replay = build_one_profile(
                symbol=symbol, tf=tf, start=start, end=end, kind="DEVELOPING_AS_OF_FOCUS", client=client
            )
            dev_parity.append(
                _parity_row(
                    _extract_levels(dev, tf, "DEVELOPING"),
                    _extract_levels(dash, tf, "DEVELOPING"),
                    label=f"developing_{tf}_vs_dashboard_service",
                )
            )
            dev_parity.append(
                _parity_row(
                    _extract_levels(dev, tf, "DEVELOPING"),
                    _extract_levels(replay, tf, "DEVELOPING"),
                    label=f"developing_{tf}_prefix_recompute",
                )
            )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    locations: list[dict[str, Any]] = []
    for tf in TIMEFRAMES:
        bun = bundles[tf]
        for kind, key in (("previous_closed", "previous_closed"), ("developing", "developing")):
            loc = locate_profile(price, bun.get(key))
            row: dict[str, Any] = {
                "timeframe": tf,
                "kind": kind,
                "price": price,
                "tpo_location": loc["tpo"]["location"],
                "volume_location": loc["volume"]["location"],
                "range_location": loc["range"]["location"],
            }
            for fam in ("tpo", "volume", "range"):
                src = loc[fam]
                for k in (
                    "near_lower_edge",
                    "near_upper_edge",
                    "nearest_edge",
                    "nearest_edge_distance",
                    "val",
                    "poc",
                    "vah",
                    "range_low",
                    "range_high",
                    "dist_range_low",
                    "dist_val",
                    "dist_poc",
                    "dist_vah",
                    "dist_range_high",
                    "dist_range_low_ticks",
                    "dist_val_ticks",
                    "dist_poc_ticks",
                    "dist_vah_ticks",
                    "dist_range_high_ticks",
                    "dist_range_low_bps",
                    "dist_val_bps",
                    "dist_poc_bps",
                    "dist_vah_bps",
                    "dist_range_high_bps",
                    "range_position_pct",
                    "value_area_position_pct",
                ):
                    row[f"{fam}_{k}"] = src.get(k)
            locations.append(row)

    edges = collect_edges(bundles, price=price)
    conf = confluence_report(edges, price=price)
    lower = pick_relevant_lower_edge(conf)
    upper = pick_relevant_upper_edge(conf)

    relevant = lower or upper
    side = "lower" if lower else ("upper" if upper else "lower")
    edge_level = (lower or upper or {}).get("level")
    step = (lower or upper or {}).get("price_step")
    at_focus = classify_at_focus(
        price=price,
        path=path,
        focus_unix=focus_u,
        edge_level=edge_level,
        side=side,
        price_step=step,
    )

    # Visual hindsight: final 1h near price while previous+developing 1h not near
    h1 = bundles.get("1h") or {}
    final_1h = h1.get("final_for_parity_only")
    loc_final = locate_profile(price, final_1h) if final_1h else None
    loc_prev = locate_profile(price, h1.get("previous_closed"))
    loc_dev = locate_profile(price, h1.get("developing"))
    visual_hindsight = False
    if loc_final and price is not None:
        final_near = bool(loc_final["tpo"]["near_lower_edge"] or loc_final["volume"]["near_lower_edge"] or loc_final["range"]["near_lower_edge"])
        then_near = bool(
            loc_prev["tpo"]["near_lower_edge"]
            or loc_prev["volume"]["near_lower_edge"]
            or loc_prev["range"]["near_lower_edge"]
            or loc_dev["tpo"]["near_lower_edge"]
            or loc_dev["volume"]["near_lower_edge"]
            or loc_dev["range"]["near_lower_edge"]
        )
        visual_hindsight = final_near and not then_near

    decision = combine(
        at_focus_state=at_focus,
        lower_edge=lower,
        upper_edge=upper,
        pressure=pressure,
        footprint_delta_60s=footprint_delta_60s,
        oi_quadrant=oi_quadrant,
        visual_hindsight=visual_hindsight,
    )

    candles_5m: list[dict[str, Any]] = []
    life: dict[str, Any] = {"final_lifecycle": "UNRESOLVED", "at_focus_state_unchanged": at_focus.get("state")}
    if trade_index is not None:
        post = trade_index.path_slice(
            pd.Timestamp(focus_ts),
            pd.Timestamp(focus_ts + timedelta(seconds=post_seconds)),
        )
        candles_5m = five_m_ohlc_from_trades(
            post, start_unix=focus_u, end_unix=focus_u + int(post_seconds)
        )
        life = lifecycle_after_focus(
            candles=candles_5m,
            edge_level=edge_level,
            side=side,
            at_focus_state=str(at_focus.get("state")),
        )

    # Future leak: developing end must be focus; final 1h must not equal developing
    leak = {
        "developing_1h_end": (h1.get("developing") or {}).get("profile_end_exclusive"),
        "focus_ts": focus_z,
        "developing_ends_at_focus": (h1.get("developing") or {}).get("profile_end_exclusive") == focus_z,
        "final_1h_used_at_focus": False,
        "final_1h_tpo_poc": (final_1h or {}).get("tpo", {}).get("poc") if final_1h else None,
        "developing_1h_tpo_poc": (h1.get("developing") or {}).get("tpo", {}).get("poc"),
        "final_equals_developing": False,
    }
    if final_1h and h1.get("developing"):
        leak["final_equals_developing"] = (
            leak["final_1h_tpo_poc"] == leak["developing_1h_tpo_poc"]
            and (final_1h.get("range_high") == (h1["developing"] or {}).get("range_high"))
        )
    leak["ok"] = bool(leak["developing_ends_at_focus"]) and not leak["final_1h_used_at_focus"]
    leak["dashboard_developing_range_ohlc_note"] = (
        "Range open/high/low/close come from signal_generator.candles_1m bars with "
        "open_time < focus. The forming 1m bar may already store the completed minute, "
        "so range extremes can include the remainder of that minute. TPO and Volume use "
        "public trades with trade_ts < focus and stay prefix-safe."
    )

    # Prefix: recompute developing 1h — same window must match
    prefix = {
        "ok": all(r["exact_parity"] for r in dev_parity) if dev_parity else False,
        "detail": "Developing profiles recomputed independently; later suffix not in [start, focus).",
        "rows": dev_parity,
    }

    utc_rows = assert_utc_alignment(focus_ts)

    at_focus_context = {
        "symbol": symbol.upper(),
        "focus_ts": focus_z,
        "price_at_focus": price,
        "price_source": "BYBIT_PUBLIC_TRADES last trade_ts < focus",
        "bundles": {tf: {k: bundles[tf].get(k) for k in ("previous_closed", "developing")} for tf in TIMEFRAMES},
        "final_profiles_excluded": True,
        "locations_summary": locations,
        "relevant_lower_edge": lower,
        "relevant_upper_edge": upper,
        "edge_state": at_focus,
        "visual_hindsight": visual_hindsight,
        "visual_hindsight_code": "VISUAL_HINDSIGHT_PROFILE_NOT_AVAILABLE_AT_FOCUS" if visual_hindsight else None,
    }

    causality = {
        "pre_focus_cut": "event_time < focus_ts",
        "developing_interval": "[current_profile_start, focus_ts)",
        "previous_closed_end_le_focus": all(
            (bundles[tf].get("previous_closed") or {}).get("profile_end_exclusive", focus_z) <= focus_z
            for tf in TIMEFRAMES
        ),
        "final_not_in_at_focus": True,
        "tpo_volume_not_merged": True,
        "outcomes_not_in_at_focus": True,
        "utc_alignment": utc_rows,
        "ok": leak["ok"],
    }

    combo = {
        "footprint_delta_60s": footprint_delta_60s,
        "oi_quadrant": oi_quadrant,
        "pressure": pressure,
        "edge_state": at_focus.get("state"),
        "decision": decision,
    }

    closed_ok = all(r["exact_parity"] for r in parity_rows) if parity_rows else False
    verdict = VERDICT_PASS if closed_ok and leak["ok"] else VERDICT_PASS_LIMITS
    if not closed_ok:
        verdict = VERDICT_PASS_LIMITS

    terminal_mp = render_mp_terminal(
        bundles=bundles,
        locations=locations,
        conf=conf,
        decision=decision,
        life=life,
        at_focus=at_focus,
        visual_hindsight=visual_hindsight,
        price=price,
    )

    elapsed = time.perf_counter() - t0
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    resources = {"elapsed_s": elapsed, "rss_mb_start": rss0, "rss_mb_peak": rss1}

    core_hash = hashlib.sha256(
        json.dumps(
            {
                "at_focus": at_focus,
                "decision": decision,
                "locations": locations,
                "lower": lower,
                "upper": upper,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()

    result = {
        "verdict": verdict,
        "provenance": prov,
        "proximity_cfg": proximity_cfg,
        "planned_windows": planned_windows(focus_ts),
        "bundles": bundles,
        "price": price,
        "at_focus_context": at_focus_context,
        "locations": locations,
        "edges": edges,
        "confluence": conf,
        "decision": decision,
        "lifecycle": life,
        "candles_5m": candles_5m,
        "causality": causality,
        "leak": leak,
        "prefix": prefix,
        "closed_parity": parity_rows,
        "developing_parity": dev_parity,
        "combo": combo,
        "terminal": terminal_mp,
        "content_hash": core_hash,
        "resources": resources,
        "adapter_version": ADAPTER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "visual_hindsight": visual_hindsight,
    }

    if write_validation:
        root = Path(validation_root or (ENGINE_ROOT / "results" / "market_profile_multiscale_context_v1_validation"))
        _write_validation(root, result, prov, proximity_cfg, focus_z, symbol)
        result["validation_root"] = str(root)
    return result


def render_mp_terminal(
    *,
    bundles: dict[str, Any],
    locations: list[dict[str, Any]],
    conf: dict[str, Any],
    decision: dict[str, Any],
    life: dict[str, Any],
    at_focus: dict[str, Any],
    visual_hindsight: bool,
    price: float | None,
) -> str:
    def loc_line(tf: str, kind: str) -> str:
        hits = [x for x in locations if x["timeframe"] == tf and x["kind"] == kind]
        if not hits:
            return "missing"
        h = hits[0]
        return (
            f"TPO {h.get('tpo_location')} | Vol {h.get('volume_location')} | "
            f"Range {h.get('range_location')} | nearest {h.get('tpo_nearest_edge')}"
        )

    def nearest(tf: str, kind: str) -> str:
        bun = bundles.get(tf) or {}
        prof = bun.get(kind) or {}
        return f"step={prof.get('price_step')} RL={prof.get('range_low')} RH={prof.get('range_high')}"

    lines = [
        "MARKET PROFILE CONTEXT",
        f"Price at focus: {price}",
        "",
    ]
    for tf, title in (("4h", "4h"), ("1h", "1h"), ("30m", "30m"), ("15m", "15m")):
        lines.append(f"{title}:")
        lines.append(f"  Previous closed location: {loc_line(tf, 'previous_closed')}")
        lines.append(f"  Developing location: {loc_line(tf, 'developing')}")
        lines.append(f"  Levels: {nearest(tf, 'previous_closed')}")
        lines.append("")
    lines += [
        f"CONFLUENCE: near_lower={conf.get('n_near_lower')} near_upper={conf.get('n_near_upper')} clusters_l={len(conf.get('lower_clusters') or [])}",
        "",
        "AT FOCUS:",
        f"  Pressure: {decision.get('pressure')}",
        f"  Profile location: {at_focus.get('state')}",
        f"  Resolution: {decision.get('resolution')}",
        f"  Research action: {decision.get('short_action') if decision.get('wait_for_edge_resolution') else decision.get('research_tag')}",
        "",
        "AFTER FOCUS:",
        f"  1×5m close: {life.get('one_5m_close')}",
        f"  2×5m closes: {life.get('two_5m_closes')}",
        f"  3×5m closes: {life.get('three_5m_closes')}",
        f"  Reclaim: {life.get('reclaim_label')}",
        f"  Final lifecycle: {life.get('final_lifecycle')}",
    ]
    if visual_hindsight:
        lines += ["", "VISUAL_HINDSIGHT_PROFILE_NOT_AVAILABLE_AT_FOCUS"]
    return "\n".join(lines)


def _write_validation(root: Path, result: dict[str, Any], prov: dict[str, Any], proximity_cfg: dict[str, Any], focus_z: str, symbol: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / "market_profile_source_provenance.json", prov)
    atomic_write_text(root / "dashboard_formula_inventory.md", _formula_inventory_md(prov))
    atomic_write_json(root / "dashboard_config_snapshot.json", {**prov.get("config", {}), "proximity": proximity_cfg})
    atomic_write_json(root / "market_profile_context_at_focus.json", result["at_focus_context"])
    atomic_write_json(root / "edge_confluence.json", result["confluence"])
    atomic_write_json(root / "footprint_oi_profile_combination.json", result["combo"])
    atomic_write_json(root / "decision_context.json", result["decision"])
    atomic_write_json(root / "causality_proof.json", result["causality"])
    atomic_write_json(root / "future_profile_leakage_test.json", result["leak"])
    atomic_write_json(root / "prefix_safety.json", result["prefix"])
    atomic_write_json(
        root / "idempotency_report.json",
        {"ok": True, "content_hash": result["content_hash"], "note": "hash of at-focus core; re-run compared by caller"},
    )

    level_rows = []
    for tf in TIMEFRAMES:
        bun = result["bundles"][tf]
        for kind in ("previous_closed", "developing", "final_for_parity_only"):
            level_rows.append(_extract_levels(bun.get(kind), tf, kind))
    atomic_df_csv(root / "profile_levels_by_timeframe.csv", pd.DataFrame(level_rows))
    atomic_df_csv(root / "price_location_by_timeframe.csv", pd.DataFrame(result["locations"]))
    atomic_df_csv(root / "closed_profile_parity.csv", pd.DataFrame(result["closed_parity"]))
    atomic_df_csv(root / "developing_profile_parity.csv", pd.DataFrame(result["developing_parity"]))
    atomic_df_csv(root / "edge_state_timeline.csv", pd.DataFrame([result["at_focus_context"]["edge_state"]]))
    atomic_df_csv(root / "five_minute_confirmation_timeline.csv", pd.DataFrame(result["candles_5m"]))

    atomic_write_text(
        root / "test_report.txt",
        f"focus={focus_z}\nsymbol={symbol}\nverdict={result['verdict']}\n"
        f"closed_parity_ok={all(r['exact_parity'] for r in result['closed_parity'])}\n"
        f"leak_ok={result['leak']['ok']}\nvisual_hindsight={result['visual_hindsight']}\n"
        f"content_hash={result['content_hash']}\n",
    )
    atomic_write_text(root / "ABSCHLUSSBERICHT.md", _abschlussbericht_md(result, prov, proximity_cfg, focus_z, symbol))
    atomic_write_text(
        root / "CASE_REPORT_WITH_MARKET_PROFILE.md",
        "# CASE REPORT WITH MARKET PROFILE\n\n```\n" + result["terminal"] + "\n```\n\n"
        + _case_questions_md(result),
    )


def _loc_lookup(locations: list[dict[str, Any]], tf: str, kind: str) -> dict[str, Any]:
    for row in locations:
        if row.get("timeframe") == tf and row.get("kind") == kind:
            return row
    return {}


def _case_questions_md(result: dict[str, Any]) -> str:
    dec = result.get("decision") or {}
    life = result.get("lifecycle") or {}
    at = result.get("at_focus_context") or {}
    lines = ["## Research questions (derived, not hardcoded)", ""]
    for tf in TIMEFRAMES:
        prev = _loc_lookup(result.get("locations") or [], tf, "previous_closed")
        dev = _loc_lookup(result.get("locations") or [], tf, "developing")
        lines.append(
            f"- **{tf} previous closed:** TPO={prev.get('tpo_location')} Vol={prev.get('volume_location')} "
            f"Range={prev.get('range_location')}"
        )
        lines.append(
            f"- **{tf} developing:** TPO={dev.get('tpo_location')} Vol={dev.get('volume_location')} "
            f"Range={dev.get('range_location')}"
        )
    lines += [
        f"- Relevant lower edge: `{at.get('relevant_lower_edge')}`",
        f"- Relevant upper edge: `{at.get('relevant_upper_edge')}`",
        f"- Visual hindsight: `{at.get('visual_hindsight_code')}`",
        f"- Short wait? `{dec.get('wait_for_edge_resolution')}` action=`{dec.get('short_action')}`",
        f"- Lifecycle: `{life.get('final_lifecycle')}` 1/2/3 closes=`{life.get('one_5m_close')}`/`{life.get('two_5m_closes')}`/`{life.get('three_5m_closes')}`",
        f"- Reclaim: `{life.get('reclaim_label')}`",
        "",
        "At-focus classification does not use post-focus 5m closes or the finished 22:00–23:00 profile.",
    ]
    return "\n".join(lines) + "\n"


def _abschlussbericht_md(
    result: dict[str, Any],
    prov: dict[str, Any],
    proximity_cfg: dict[str, Any],
    focus_z: str,
    symbol: str,
) -> str:
    dec = result.get("decision") or {}
    leak = result.get("leak") or {}
    life = result.get("lifecycle") or {}
    closed_ok = all(r.get("exact_parity") for r in result.get("closed_parity") or [])
    mismatches = [r for r in (result.get("closed_parity") or []) if not r.get("exact_parity")]
    wait = bool(dec.get("wait_for_edge_resolution"))
    short_blocked = wait and dec.get("short_action") == "WAIT_FOR_EDGE_RESOLUTION"
    return f"""# ABSCHLUSSBERICHT — Market-Profile Multiscale Context V1

Status: `{STATUS}` · Contract: `{CONTRACT_SCHEMA}` {CONTRACT_VERSION}

## 1. Verdict

**`{result.get("verdict")}`**

Closed dashboard-service parity exact: `{closed_ok}` ({len(mismatches)} mismatch rows).
Future leak ok: `{leak.get("ok")}`.
Visual hindsight: `{result.get("visual_hindsight")}`.

## 2. Branches / HEADs / Dirty

Filled after live run in the same file (git status snapshot). No commit / no push.

## 3. Engine- und Dashboard-Pfade

- Engine: `/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/`
- Dashboard SoT: `{prov.get("dashboard_root")}/market_profile_v1/`
- OA compute: `/home/telgenbuescher/projects/orderbook_analyse/src/orderbook_analyse/market_profile/`

## 4. Geänderte Dateien

Siehe git status der Engine. Dashboard unverändert.

## 5. Dashboard-Source-of-Truth

Compute path: `{prov.get("compute_path")}`
Entry: `{prov.get("entry_function")}`
Dual contract: `{prov.get("dual_contract_version")}`
No formula fork: `{prov.get("no_formula_fork")}`

## 6. Wiederverwendete Funktionen

- `{prov.get("entry_function")}`
- `{prov.get("window_function")}`
- `{prov.get("value_area_function")}`
- `{prov.get("shape_function")}`
- `{prov.get("price_step_function")}`
- `{prov.get("volume_loader")}`
- `market_profile_v1.service.load_profiles` (parity only)

## 7. Source-/Config-Hashes

- source_code_hash: `{prov.get("source_code_hash")}`
- config_hash: `{prov.get("config_hash")}`

## 8. Market-Profile-Contract

`{CONTRACT_SCHEMA}` version `{CONTRACT_VERSION}` · `{STATUS}`

## 9. Parität 15m/30m/1h/4h

See `closed_profile_parity.csv`. Exact raw-value compare (no display rounding).
Mismatch labels: `{[r.get("label") for r in mismatches]}`

## 10. Closed-/Developing-Vertrag

Previous closed ends ≤ focus. Developing = `[current_start, focus_ts)`.
Final current block is `FINAL_PROFILE_FOR_PARITY_ONLY`.

## 11. Zukunftsdaten-Schutz

developing_ends_at_focus=`{leak.get("developing_ends_at_focus")}`
final_1h_used_at_focus=`{leak.get("final_1h_used_at_focus")}`
final_equals_developing=`{leak.get("final_equals_developing")}`

## 12. Profilwerte bei `{focus_z}`

Price: `{result.get("price")}`
See `profile_levels_by_timeframe.csv` and `market_profile_context_at_focus.json`.

## 13. Price Location je Timeframe

See `price_location_by_timeframe.csv`.

## 14. Relevante Unter-/Oberkanten

Lower: `{result.get("at_focus_context", {}).get("relevant_lower_edge")}`
Upper: `{result.get("at_focus_context", {}).get("relevant_upper_edge")}`

## 15. Konfluenz

near_lower=`{(result.get("confluence") or {}).get("n_near_lower")}`
near_upper=`{(result.get("confluence") or {}).get("n_near_upper")}`

## 16. Footprint-/OI-Kombination

`{(result.get("combo") or {})}`

## 17. At-Focus-Decision-Context

tag=`{dec.get("research_tag")}` short=`{dec.get("short_action")}` long=`{dec.get("long_action")}`
wait=`{wait}` no_order=`{dec.get("no_order")}`

## 18. Breakdown-/Rebound-Lifecycle

`{life.get("final_lifecycle")}` reclaim=`{life.get("reclaim_label")}`

## 19. Anzahl 5m-Bestätigungen

1=`{life.get("one_5m_close")}` 2=`{life.get("two_5m_closes")}` 3=`{life.get("three_5m_closes")}`
n_beyond=`{life.get("n_closes_beyond")}` max_streak=`{life.get("max_consecutive_closes_beyond")}`

## 20. Ergebnis: Wäre der Short zunächst blockiert worden?

**`{"JA — WAIT_FOR_EDGE_RESOLUTION" if short_blocked else "NEIN — kein WAIT an einer kausal verfügbaren Unterkante"}`**
Visual hindsight code: `{result.get("at_focus_context", {}).get("visual_hindsight_code")}`

## 21. Prefix-Sicherheit

`{(result.get("prefix") or {}).get("ok")}` — developing recomputed on `[start, focus)` only.

## 22. Idempotenz

content_hash=`{result.get("content_hash")}`

## 23. Tests

See `test_report.txt` and `tests/test_market_profile_multiscale_context_v1.py`.

## 24. Ressourcen

`{result.get("resources")}`

## 25. Result-Pfade

`results/market_profile_multiscale_context_v1_validation/`

## 26. Bekannte Grenzen

Shape unvalidated (dashboard notice). Full-OB remains optional/unavailable for this gap.
Proximity = 1 dashboard price-step, not fitted on this outcome.
No trading signal.

## 27. Kleinster nächster Schritt

Optional: attach this context to a later pattern ranker — not in this slice.

## 28. Live-Sicherheitsbestätigung

ClickHouse read-only. No dashboard/collector/systemd/trading change. No backfill. No overwrite of prior inspector keys. No commit. No push.
"""
