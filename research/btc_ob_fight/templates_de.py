"""Deterministic German fact sentence templates."""

from __future__ import annotations

from typing import Any

from .config import REPORT_MICRO_EPISODE_SECONDS
from .formatting import fmt_bps, fmt_duration_seconds, fmt_fraction_as_pct, fmt_mio_usd, fmt_oi_delta, fmt_pct, fmt_price, fmt_ts_display


def render_german_fact(code: str, fields: dict[str, Any]) -> str:
    fn = _TEMPLATES.get(code)
    if fn is None:
        return f"{code}: {fields}"
    return fn(fields)


def render_all_german(reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in reasons:
        text = render_german_fact(r["code"], r.get("fields") or {})
        out.append({"code": r["code"], "text_de": text, "fields": r.get("fields")})
    return out


def render_report_sections(
    reasons: list[dict[str, Any]],
    summary: dict[str, Any],
    manifest: dict[str, Any],
    *,
    level_events: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    german = render_all_german(reasons)
    by_code: dict[str, list[str]] = {}
    for g in german:
        by_code.setdefault(g["code"], []).append(g["text_de"])
    pf = summary.get("profile_facts") or {}
    oi = summary.get("oi_liquidation_facts") or {}
    ws = summary.get("wall_summary") or {}
    return {
        "profile": _profile_section(pf, volume_profile=summary.get("volume_profile") or {}),
        "episodes": _compressed_episode_lines(level_events or summary.get("level_events") or [], manifest),
        "trade_windows": _combined_trade_window_lines(reasons),
        "oi": _oi_lines(oi),
        "liquidations": _liq_lines(oi),
        "walls": _wall_lines(ws, manifest),
        "heuristics": _refill_heuristic_lines(ws),
    }


def _profile_section(pf: dict[str, Any], *, volume_profile: dict[str, Any] | None = None) -> list[str]:
    lines = [
        f"Ankerpreis: {fmt_price(pf.get('price_at_anchor'))}",
        "TPO PROFILE — 30m BRACKET PRESENCE",
        f"TPO POC/VAH/VAL: {fmt_price(pf.get('tpo_poc'))} / {fmt_price(pf.get('tpo_vah'))} / {fmt_price(pf.get('tpo_val'))}",
        f"TPO-Status: {pf.get('tpo_profile_status')}",
    ]
    if pf.get("tpo_value_area_share") is not None:
        lines.append(f"TPO-Value-Area-Anteil: {fmt_fraction_as_pct(pf.get('tpo_value_area_share'))}")
    if pf.get("inside_tpo_value_area") is not None:
        lines.append(f"Anker in TPO-Value-Area: {pf.get('inside_tpo_value_area')}")
    vah_bps = _distance_bps(pf, "tpo_vah")
    val_bps = _distance_bps(pf, "tpo_val")
    if vah_bps is not None:
        lines.append(f"Abstand zu TPO-VAH: {fmt_bps(vah_bps)}")
    if val_bps is not None:
        lines.append(f"Abstand zu TPO-VAL: {fmt_bps(val_bps)}")
    conf_status = pf.get("tpo_volume_confluence_status")
    if conf_status:
        lines.append(f"TPO↔Volume Konfluenz-Status: {conf_status}")
    lines.append("VOLUME PROFILE — BASE VOLUME")
    status = pf.get("volume_profile_status")
    lines.append(f"Volume-Profile: {status}")
    if status == "COMPUTED_SEPARATELY":
        lines.append(
            f"VPOC/VVAH/VVAL: {fmt_price(pf.get('volume_poc'))} / "
            f"{fmt_price(pf.get('volume_vah'))} / {fmt_price(pf.get('volume_val'))}"
        )
        if pf.get("inside_volume_value_area") is not None:
            lines.append(f"Anker in Volume-Value-Area: {pf.get('inside_volume_value_area')}")
        share = (volume_profile or {}).get("value_area_share")
        if share is not None:
            lines.append(f"Volume-Value-Area-Anteil: {fmt_fraction_as_pct(share)}")
    elif status == "NOT_SEPARATELY_COMPUTED":
        lines.append("VPOC/VVAH/VVAL: NOT_AVAILABLE (keine separate Volume-Pipeline)")
    nearest = (pf.get("nearest_tpo_levels") or pf.get("nearest_profile_levels") or [{}])[0]
    if nearest:
        lines.append(
            f"Nächstes TPO-Level: {nearest.get('kind')} {fmt_price(nearest.get('price'))} "
            f"({fmt_bps(nearest.get('distance_bps'))})"
        )
    return lines


def _distance_bps(pf: dict[str, Any], kind: str) -> float | None:
    price = pf.get("price_at_anchor")
    lvl = pf.get(f"tpo_{kind.split('_')[-1]}" if kind.startswith("tpo_") else kind)
    if kind == "tpo_vah":
        lvl = pf.get("tpo_vah")
    elif kind == "tpo_val":
        lvl = pf.get("tpo_val")
    if price is None or lvl is None:
        return None
    return (float(price) - float(lvl)) / float(price) * 10000.0


def _compressed_episode_lines(level_events: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    micro = float((manifest.get("heuristics") or {}).get("report_micro_episode_seconds") or REPORT_MICRO_EPISODE_SECONDS)
    lines: list[str] = []
    for ev in level_events:
        label = ev.get("label") or ev.get("level_id")
        price = ev.get("price")
        episodes = ev.get("episodes") or []
        above = [e for e in episodes if e.get("direction") == "ABOVE" and e.get("complete")]
        below = [e for e in episodes if e.get("direction") == "BELOW" and e.get("complete")]
        above_durs = [float(e.get("duration_seconds") or 0) for e in above]
        below_durs = [float(e.get("duration_seconds") or 0) for e in below]
        incomplete = any(not e.get("complete") for e in episodes)
        micro_count = sum(
            1
            for e in episodes
            if e.get("complete") and (e.get("duration_seconds") or 0) <= micro + 1e-9
        )
        anchor = ev.get("anchor_state") or {}
        lines.append(
            f"{label} {fmt_price(price)}: initial_side={anchor.get('initial_side_at_anchor')}, "
            f"final_side={anchor.get('final_side_at_window_end')}, "
            f"complete_above={len(above)}, complete_below={len(below)}, "
            f"total_above_s={sum(above_durs):.3f}, total_below_s={sum(below_durs):.3f}, "
            f"micro_flicker(<={micro}s)={micro_count}, incomplete_final={incomplete}"
        )
        if above:
            first = min(above, key=lambda e: e.get("start_ts") or "")
            lines.append(
                f"  Erste ABOVE-Episode: {fmt_ts_display(first.get('start_ts'))}–{fmt_ts_display(first.get('end_ts'))} "
                f"({fmt_duration_seconds(first.get('duration_seconds'))})"
            )
        if below:
            first_b = min(below, key=lambda e: e.get("start_ts") or "")
            lines.append(
                f"  Erste BELOW-Episode: {fmt_ts_display(first_b.get('start_ts'))}–{fmt_ts_display(first_b.get('end_ts'))} "
                f"({fmt_duration_seconds(first_b.get('duration_seconds'))})"
            )
        for rank, ep in enumerate(sorted(above, key=lambda e: -(e.get("duration_seconds") or 0))[:3], 1):
            lines.append(
                f"  Top-{rank} ABOVE: {fmt_duration_seconds(ep.get('duration_seconds'))} "
                f"({fmt_ts_display(ep.get('start_ts'))}–{fmt_ts_display(ep.get('end_ts'))})"
            )
        for rank, ep in enumerate(sorted(below, key=lambda e: -(e.get("duration_seconds") or 0))[:3], 1):
            lines.append(
                f"  Top-{rank} BELOW: {fmt_duration_seconds(ep.get('duration_seconds'))} "
                f"({fmt_ts_display(ep.get('start_ts'))}–{fmt_ts_display(ep.get('end_ts'))})"
            )
    return lines


def _combined_trade_window_lines(reasons: list[dict[str, Any]]) -> list[str]:
    windows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in reasons:
        f = r.get("fields") or {}
        key = (f.get("start_utc") or "", f.get("end_utc") or "")
        if not key[0]:
            continue
        slot = windows.setdefault(key, {"start_utc": key[0], "end_utc": key[1]})
        if "delta_notional" in f and "delta" not in slot:
            slot["delta_notional"] = f.get("delta_notional")
        if "price_change_bps" in f and "price_change_bps" not in slot:
            slot["price_change_bps"] = f.get("price_change_bps")
        if "buy_notional" in f:
            slot["buy_notional"] = f.get("buy_notional")
        if "sell_notional" in f:
            slot["sell_notional"] = f.get("sell_notional")
        if "trade_count" in f:
            slot["trade_count"] = f.get("trade_count")
    lines = []
    for slot in sorted(windows.values(), key=lambda x: x.get("start_utc") or ""):
        parts = [
            f"{fmt_ts_display(slot.get('start_utc'))}–{fmt_ts_display(slot.get('end_utc'))}:"
        ]
        if slot.get("delta_notional") is not None:
            parts.append(f"Delta {fmt_mio_usd(slot.get('delta_notional'))}")
        if slot.get("price_change_bps") is not None:
            parts.append(f"Preisänderung {fmt_bps(slot.get('price_change_bps'))}")
        if slot.get("buy_notional") is not None:
            parts.append(f"Buy {fmt_mio_usd(slot.get('buy_notional'))}")
        if slot.get("sell_notional") is not None:
            parts.append(f"Sell {fmt_mio_usd(slot.get('sell_notional'))}")
        if slot.get("trade_count") is not None:
            parts.append(f"Trades {slot.get('trade_count')}")
        lines.append(" ".join(parts))
    return lines


def _oi_lines(oi: dict[str, Any]) -> list[str]:
    unit = (oi.get("oi_unit") or {}).get("display_label") or "Source-Einheiten"
    return [
        f"OI Start: {fmt_oi_delta(oi.get('oi_first'))} {unit}",
        f"OI Ende: {fmt_oi_delta(oi.get('oi_last'))} {unit}",
        f"OI Delta: {fmt_oi_delta(oi.get('oi_delta'))} {unit} ({fmt_pct(oi.get('oi_delta_pct'))})",
        f"OI Sample Count: {oi.get('oi_sample_count')}",
        f"OI Freshness: {(oi.get('freshness') or {}).get('oi_last_ts')}",
        f"OI Source Field: {(oi.get('oi_unit') or {}).get('source_field')}",
    ]


def _liq_lines(oi: dict[str, Any]) -> list[str]:
    ls = oi.get("liquidation_summary") or {}
    return [
        f"Liquidationen gesamt: {oi.get('liquidation_count')}",
        f"Long-Liquidationen: {ls.get('long_count')} ({fmt_mio_usd(ls.get('long_notional'))})",
        f"Short-Liquidationen: {ls.get('short_count')} ({fmt_mio_usd(ls.get('short_notional'))})",
        f"Größtes Ereignis: {fmt_mio_usd(ls.get('largest_notional'))} @ {fmt_ts_display(ls.get('largest_ts'))}",
        f"Side-Semantik: {ls.get('side_semantics')}",
        f"Freshness: {(oi.get('freshness') or {}).get('liq_last_ts')}",
    ]


def _wall_lines(ws: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    heur = manifest.get("heuristics") or {}
    td = ws.get("trade_associated_decreases") or {}
    ud = ws.get("unmatched_decreases") or {}
    tad = ws.get("trade_associated_disappearances") or {}
    uad = ws.get("unmatched_disappearances") or {}
    rf = ws.get("refill_sequences_heuristic") or {}
    vis = ws.get("tracks_visible_at_window_end") or {}
    qd = ws.get("qty_decreases") or {}
    return [
        f"Book-Samples: {ws.get('book_samples_total')}",
        f"Sample-Gap P50/P95/Max: {ws.get('sample_gap_p50_seconds')} / {ws.get('sample_gap_p95_seconds')} / {ws.get('sample_gap_max_seconds')} s",
        f"Wall-Beobachtungen: Ask {ws.get('ask_wall_observations')} / Bid {ws.get('bid_wall_observations')}",
        f"Eindeutige Wall-Tracks: Ask {ws.get('ask_wall_tracks')} / Bid {ws.get('bid_wall_tracks')}",
        f"Eindeutige Preislevel: Ask {ws.get('ask_unique_wall_price_levels')} / Bid {ws.get('bid_unique_wall_price_levels')}",
        f"Quantity-Decreases: Ask {qd.get('ask')} / Bid {qd.get('bid')}",
        f"Trade-associated Decreases: Ask {td.get('ask')} / Bid {td.get('bid')}",
        f"Unmatched Decreases: Ask {ud.get('ask')} / Bid {ud.get('bid')}",
        f"Trade-associated Disappearances: Ask {tad.get('ask')} / Bid {tad.get('bid')}",
        f"Unmatched Disappearances: Ask {uad.get('ask')} / Bid {uad.get('bid')}",
        f"Refill-Sequenzen nach UNFROZEN_HEURISTIC: Ask {rf.get('ask')} / Bid {rf.get('bid')}",
        f"Tracks bis Fensterende sichtbar: Ask {vis.get('ask')} / Bid {vis.get('bid')}",
        f"Heuristik-Contract: {ws.get('status')} ({ws.get('heuristic_contract_version')}); Tick-Size {heur.get('btcusdt_tick_size')}",
        f"Wall-Schwellen (UNFROZEN): max_bps={heur.get('wall_max_bps')}, qty_mult={heur.get('wall_qty_median_mult')}, match_frac={heur.get('wall_trade_match_frac')}, sample_interval_s={heur.get('wall_sample_interval_seconds')}",
    ]


def _refill_heuristic_lines(ws: dict[str, Any]) -> list[str]:
    rf = ws.get("refill_sequences_heuristic") or {}
    total = (rf.get("ask") or 0) + (rf.get("bid") or 0)
    if total <= 0:
        return []
    return [
        f"UNFROZEN_HEURISTIC: {total} Refill-Sequenz(en) beobachtet (Ask {rf.get('ask')}, Bid {rf.get('bid')})."
    ]


_TEMPLATES: dict[str, Any] = {
    "ANCHOR_INSIDE_TPO_VALUE_AREA": lambda f: (
        f"Der Ankerpreis {fmt_price(f.get('price_at_anchor'))} lag innerhalb der TPO-Value-Area "
        f"(VAL {fmt_price(f.get('tpo_val'))}, VAH {fmt_price(f.get('tpo_vah'))})."
    ),
    "LEVEL_TOUCHED": lambda f: (
        f"Level {f.get('label') or f.get('level_id')} {fmt_price(f.get('price'))} wurde erstmals berührt um {fmt_ts_display(f.get('first_touch_ts'))}."
    ),
    "PROFILE_LEVEL_ABOVE_EPISODE_COMPLETE": lambda f: _above_complete(f),
    "PROFILE_LEVEL_ABOVE_EPISODE_INCOMPLETE": lambda f: _above_incomplete(f),
    "PROFILE_LEVEL_BELOW_EPISODE_COMPLETE": lambda f: _below_complete(f),
    "PROFILE_LEVEL_BELOW_EPISODE_INCOMPLETE": lambda f: _below_incomplete(f),
    "POSITIVE_TAKER_DELTA_OBSERVED": lambda f: (
        f"Fenster {fmt_ts_display(f.get('start_utc'))}–{fmt_ts_display(f.get('end_utc'))}: "
        f"Taker-Delta {fmt_mio_usd(f.get('delta_notional'))}."
    ),
    "NEGATIVE_TAKER_DELTA_OBSERVED": lambda f: (
        f"Fenster {fmt_ts_display(f.get('start_utc'))}–{fmt_ts_display(f.get('end_utc'))}: "
        f"Taker-Delta {fmt_mio_usd(f.get('delta_notional'))}."
    ),
    "PRICE_MOVED_UP_IN_WINDOW": lambda f: (
        f"Fenster {fmt_ts_display(f.get('start_utc'))}–{fmt_ts_display(f.get('end_utc'))}: "
        f"Preisänderung {fmt_bps(f.get('price_change_bps'))}."
    ),
    "PRICE_MOVED_DOWN_IN_WINDOW": lambda f: (
        f"Fenster {fmt_ts_display(f.get('start_utc'))}–{fmt_ts_display(f.get('end_utc'))}: "
        f"Preisänderung {fmt_bps(f.get('price_change_bps'))}."
    ),
    "HEURISTIC_TRADE_BACKED_ASK_REDUCTION_OBSERVED": lambda f: (
        f"Heuristik (UNFROZEN): Ask-Wall bei {fmt_price(f.get('price'))} reduzierte sich um {f.get('reduced_qty')} "
        f"mit passenden Trades um {fmt_ts_display(f.get('ts'))}."
    ),
    "HEURISTIC_TRADE_BACKED_BID_REDUCTION_OBSERVED": lambda f: (
        f"Heuristik (UNFROZEN): Bid-Wall bei {fmt_price(f.get('price'))} reduzierte sich um {f.get('reduced_qty')} "
        f"mit passenden Trades um {fmt_ts_display(f.get('ts'))}."
    ),
    "HEURISTIC_ASK_PULLING_OBSERVED": lambda f: (
        f"Heuristik (UNFROZEN): Ask-Wall bei {fmt_price(f.get('price'))} verschwand/reduzierte ohne ausreichende "
        f"Trade-Unterstützung um {fmt_ts_display(f.get('ts'))}."
    ),
    "HEURISTIC_BID_PULLING_OBSERVED": lambda f: (
        f"Heuristik (UNFROZEN): Bid-Wall bei {fmt_price(f.get('price'))} verschwand/reduzierte ohne ausreichende "
        f"Trade-Unterstützung um {fmt_ts_display(f.get('ts'))}."
    ),
    "OI_INCREASE_OBSERVED": lambda f: (
        f"OI-Delta: {fmt_oi_delta(f.get('oi_delta'))} "
        f"{((f.get('oi_unit') or {}).get('display_label') or 'Source-Einheiten')} ({fmt_pct(f.get('oi_delta_pct'))}); "
        f"Start {fmt_oi_delta(f.get('oi_first'))}, Ende {fmt_oi_delta(f.get('oi_last'))}."
    ),
    "OI_DECREASE_OBSERVED": lambda f: (
        f"OI-Delta: {fmt_oi_delta(f.get('oi_delta'))} "
        f"{((f.get('oi_unit') or {}).get('display_label') or 'Source-Einheiten')} ({fmt_pct(f.get('oi_delta_pct'))}); "
        f"Start {fmt_oi_delta(f.get('oi_first'))}, Ende {fmt_oi_delta(f.get('oi_last'))}."
    ),
    "LIQUIDATIONS_OBSERVED": lambda f: (
        f"Es wurden {f.get('liquidation_count')} Liquidationen beobachtet "
        f"(Long {((f.get('liquidation_summary') or {}).get('long_count'))}, "
        f"Short {((f.get('liquidation_summary') or {}).get('short_count'))})."
    ),
    "VOLUME_PROFILE_COMPUTED_FROM_TRADES": lambda f: (
        f"Das separat aus Public Trades berechnete Volume Profile verwendete "
        f"{f.get('deduped_trade_rows_used')} deduplizierte Trades vom Sessionstart "
        f"{fmt_ts_display(f.get('session_start_utc'))} bis zum Anchor "
        f"{fmt_ts_display(f.get('cutoff_utc'))}."
    ),
    "VOLUME_VALUE_AREA_COMPUTED": lambda f: (
        f"Die Volume-Value-Area reichte von VVAL {fmt_price(f.get('vval'))} bis VVAH "
        f"{fmt_price(f.get('vvah'))} und enthielt "
        f"{fmt_fraction_as_pct(f.get('actual_value_area_share'))} "
        f"des verwendeten Volumens."
    ),
    "ANCHOR_INSIDE_VOLUME_VALUE_AREA": lambda f: (
        f"Der Ankerpreis {fmt_price(f.get('price_at_anchor'))} lag innerhalb der separat "
        f"berechneten Volume-Value-Area (VVAL {fmt_price(f.get('volume_val'))}, "
        f"VVAH {fmt_price(f.get('volume_vah'))})."
    ),
    "ANCHOR_OUTSIDE_VOLUME_VALUE_AREA": lambda f: (
        f"Der Ankerpreis {fmt_price(f.get('price_at_anchor'))} lag außerhalb der separat "
        f"berechneten Volume-Value-Area (VVAL {fmt_price(f.get('volume_val'))}, "
        f"VVAH {fmt_price(f.get('volume_vah'))})."
    ),
    "TPO_VOLUME_LEVEL_DISTANCE_OBSERVED": lambda f: (
        f"TPO-{f.get('tpo_kind')} ({fmt_price(f.get('tpo_price'))}) und Volume-{f.get('volume_kind')} "
        f"({fmt_price(f.get('volume_price'))}) lagen {fmt_bps(f.get('distance_bps'))} auseinander."
    ),
}


def _level_label(f: dict[str, Any]) -> str:
    return str(f.get("label") or f.get("level_id") or "Profil-Level")


def _above_complete(f: dict[str, Any]) -> str:
    prefix = "Eine weitere Episode oberhalb" if (f.get("episode_index") or 0) > 1 else "Der Preis"
    if (f.get("episode_index") or 0) > 1:
        return (
            f"{prefix} des {_level_label(f)} {fmt_price(f.get('level_price'))} begann am {fmt_ts_display(f.get('start_ts'))} "
            f"und endete am {fmt_ts_display(f.get('end_ts'))} mit einer Dauer von {fmt_duration_seconds(f.get('duration_seconds'))}."
        )
    return (
        f"Der Preis überschritt das {_level_label(f)} {fmt_price(f.get('level_price'))} am {fmt_ts_display(f.get('start_ts'))} "
        f"und fiel am {fmt_ts_display(f.get('end_ts'))} wieder darunter. "
        f"Die Episode oberhalb dauerte {fmt_duration_seconds(f.get('duration_seconds'))}."
    )


def _above_incomplete(f: dict[str, Any]) -> str:
    if (f.get("episode_index") or 0) > 1:
        return (
            f"Eine weitere Episode oberhalb des {_level_label(f)} {fmt_price(f.get('level_price'))} begann am "
            f"{fmt_ts_display(f.get('start_ts'))}. Diese Episode war am Ende des Beobachtungsfensters noch nicht abgeschlossen."
        )
    return (
        f"Der Preis überschritt das {_level_label(f)} {fmt_price(f.get('level_price'))} am {fmt_ts_display(f.get('start_ts'))}. "
        f"Diese Episode war am Ende des Beobachtungsfensters noch nicht abgeschlossen."
    )


def _below_complete(f: dict[str, Any]) -> str:
    if (f.get("episode_index") or 0) > 1:
        return (
            f"Eine weitere Episode unterhalb des {_level_label(f)} {fmt_price(f.get('level_price'))} begann am "
            f"{fmt_ts_display(f.get('start_ts'))} und endete am {fmt_ts_display(f.get('end_ts'))} "
            f"mit einer Dauer von {fmt_duration_seconds(f.get('duration_seconds'))}."
        )
    return (
        f"Der Preis fiel unter das {_level_label(f)} {fmt_price(f.get('level_price'))} am {fmt_ts_display(f.get('start_ts'))} "
        f"und stieg am {fmt_ts_display(f.get('end_ts'))} wieder darüber. "
        f"Die Episode unterhalb dauerte {fmt_duration_seconds(f.get('duration_seconds'))}."
    )


def _below_incomplete(f: dict[str, Any]) -> str:
    return (
        f"Episode unterhalb des {_level_label(f)} {fmt_price(f.get('level_price'))} begann am {fmt_ts_display(f.get('start_ts'))}. "
        f"Diese Episode war am Ende des Beobachtungsfensters noch nicht abgeschlossen."
    )
