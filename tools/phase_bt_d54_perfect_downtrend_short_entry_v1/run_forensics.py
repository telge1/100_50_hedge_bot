#!/usr/bin/env python3
"""BT-D54 read-only perfect downtrend short-entry forensics (DOGE 5m PD1 D2)."""

from __future__ import annotations

import copy
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_bt_d54_perfect_downtrend_short_entry_v1"
CFG = ROOT / "configs/pilot_bt_d21_pool_first_24h.yaml"

WARMUP_START = "2026-09-02T16:00:00Z"  # ≥168h vor erstem Fenster (16:30 UTC)
REPLAY_END = "2026-09-09T22:00:00Z"

WINDOWS = [
    {
        "id": "window1",
        "label": "2026-09-09 16:30–17:25 UTC",
        "start": "2026-09-09T16:30:00Z",
        "end": "2026-09-09T17:25:00Z",
        "json_name": "window1_2026_09_09_1630_1725.json",
    },
    {
        "id": "window2",
        "label": "2026-09-09 20:55–21:25 UTC",
        "start": "2026-09-09T20:55:00Z",
        "end": "2026-09-09T21:25:00Z",
        "json_name": "window2_2026_09_09_2055_2125.json",
    },
]

EMA_PERIODS = (9, 20, 59, 200)

for p in (
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
    ROOT / "tools/phase_bt_d53_touch_connected_sl_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from gap_detection_v1 import compute_side_gaps  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range, measure_upper_gap_pct  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402

from connectivity_component_v1 import component_from_entry, intervals_strict_overlap  # noqa: E402


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _bars_in_window(all_bars: list[str], start: str, end: str) -> list[str]:
    s, e = _parse_utc(start), _parse_utc(end)
    out = []
    for b in all_bars:
        t = _parse_utc(b)
        if s <= t <= e:
            out.append(b)
    return out


def _compute_emas(bars: list[str], prices: dict[str, dict]) -> dict[int, dict[str, float]]:
    ema: dict[int, dict[str, float]] = {p: {} for p in EMA_PERIODS}
    for period in EMA_PERIODS:
        k = 2.0 / (period + 1)
        prev: float | None = None
        for bar in bars:
            c = float(prices[bar]["c"])
            if prev is None:
                prev = c
            else:
                prev = c * k + prev * (1.0 - k)
            ema[period][bar] = prev
    return ema


def _band_rows(snapshot: dict) -> list[dict[str, Any]]:
    rows = []
    for b in snapshot.get("upper_bands") or []:
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        rows.append(
            {
                "pool_lower": lo,
                "pool_upper": hi,
                "pool_key": pool_key(lo, hi),
                "origin_ids": b.get("origin_ids") or "",
                "origin_count": len([x for x in str(b.get("origin_ids") or "").split("|") if x.strip()]),
            }
        )
    rows.sort(key=lambda r: (r["pool_lower"], r["pool_upper"]))
    return rows


def _lower_rows(snapshot: dict) -> list[dict[str, Any]]:
    rows = []
    for b in snapshot.get("lower_bands") or []:
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        rows.append({"pool_lower": lo, "pool_upper": hi, "pool_key": pool_key(lo, hi)})
    rows.sort(key=lambda r: (r["pool_lower"], r["pool_upper"]))
    return rows


def _next_atomic_upper_above(uppers: list[dict[str, Any]], pool_hi: float, skip_pk: str) -> dict[str, Any] | None:
    for u in uppers:
        if u["pool_key"] == skip_pk:
            continue
        if u["pool_lower"] >= pool_hi:
            return u
    return None


def _count_stack_between(uppers: list[dict[str, Any]], lo_exclusive: float, hi_exclusive: float) -> int:
    return sum(1 for u in uppers if u["pool_lower"] > lo_exclusive and u["pool_lower"] < hi_exclusive)


def analyze_bar(
    bar: str,
    *,
    config: dict,
    snap: dict,
    candle: dict,
    pool_age: dict[str, int],
    emas: dict[int, dict[str, float]],
    window_id: str,
) -> dict[str, Any]:
    close = float(candle["c"])
    uppers = _band_rows(snap)
    lowers = _lower_rows(snap)
    min_gap = float(config["short_entry"]["upper_gap_min_pct"])

    above = [u for u in uppers if u["pool_lower"] > close]
    nearest_above = min(above, key=lambda u: u["pool_lower"]) if above else None

    elig = []
    for b in snap.get("upper_bands") or []:
        ok, fail = _structural_eligible_upper(
            config,
            band=b,
            decision_close_utc=bar,
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
        )
        if ok:
            lo, hi = float(b["lower_price"]), float(b["upper_price"])
            elig.append(
                {
                    "pool_lower": lo,
                    "pool_upper": hi,
                    "pool_key": pool_key(lo, hi),
                    "origin_count": len([x for x in str(b.get("origin_ids") or "").split("|") if x.strip()]),
                    "pool_age_bars": pool_age.get(pool_key(lo, hi), 0),
                    "pool_width": hi - lo,
                }
            )
    elig.sort(key=lambda r: (r["pool_lower"], r["pool_upper"]))
    lowest_elig = elig[0] if elig else None

    entry = lowest_elig
    next_upper = None
    gap_abs = None
    gap_pct = None
    stack_between = None
    strict_comp_n = None
    touch_comp_n = None
    touch_unresolved = None

    if entry:
        next_upper = _next_atomic_upper_above(uppers, entry["pool_upper"], entry["pool_key"])
        if next_upper:
            gap_abs = next_upper["pool_lower"] - entry["pool_upper"]
            gap_pct = gap_abs / entry["pool_upper"] * 100.0
            stack_between = _count_stack_between(
                uppers, entry["pool_upper"], next_upper["pool_lower"]
            )
        strict_c = component_from_entry(
            snap,
            entry_pool_lower=entry["pool_lower"],
            entry_pool_upper=entry["pool_upper"],
            mode="strict_overlap",
        )
        touch_c = component_from_entry(
            snap,
            entry_pool_lower=entry["pool_lower"],
            entry_pool_upper=entry["pool_upper"],
            mode="touch_or_overlap",
        )
        strict_comp_n = strict_c.get("member_count")
        touch_comp_n = touch_c.get("member_count")
        touch_unresolved = touch_c.get("status")

    ema_ctx = {f"ema{p}": emas[p].get(bar) for p in EMA_PERIODS}
    under_200 = close < ema_ctx["ema200"] if ema_ctx.get("ema200") else None
    ema_order = (
        ema_ctx["ema9"] < ema_ctx["ema20"] < ema_ctx["ema59"] < ema_ctx["ema200"]
        if all(ema_ctx.get(f"ema{p}") for p in EMA_PERIODS)
        else None
    )

    large_gap_qualifying = bool(entry and measure_upper_gap_pct(snap.get("upper_bands") or [], entry["pool_upper"], min_gap))

    # Descriptive „Nutzer-Perfekt“-Heuristik (nur Forschung, nicht aktiv)
    perfect_likely = False
    perfect_reason = ""
    if entry and gap_pct is not None:
        narrow = entry["pool_width"] < 0.00005
        spacious = gap_pct >= min_gap
        sparse_stack = (stack_between or 0) == 0
        near_price = (entry["pool_lower"] - close) / close * 100.0 < 1.5
        if spacious and sparse_stack and near_price and (narrow or entry["pool_width"] < 0.0002):
            perfect_likely = True
            perfect_reason = (
                "kleiner naher Upper-Pool, keine Zwischenbänder bis nächstes Level, "
                f"Gap {gap_pct:.3f}% >= Gate {min_gap}%"
            )
        elif spacious and near_price:
            perfect_reason = f"Gap ok ({gap_pct:.3f}%), aber stack_between={stack_between}"
        else:
            perfect_reason = "Gap/Proximity/Breite nicht im Nutzer-Muster"

    return {
        "window_id": window_id,
        "decision_close_utc": bar,
        "close_price": close,
        "upper_band_count": len(uppers),
        "lower_band_count": len(lowers),
        "structurally_eligible_upper_count": len(elig),
        "nearest_upper_above_close_lower": nearest_above["pool_lower"] if nearest_above else None,
        "nearest_upper_above_close_upper": nearest_above["pool_upper"] if nearest_above else None,
        "lowest_eligible_pool_lower": entry["pool_lower"] if entry else None,
        "lowest_eligible_pool_upper": entry["pool_upper"] if entry else None,
        "entry_pool_width": entry["pool_width"] if entry else None,
        "entry_origin_count": entry["origin_count"] if entry else None,
        "entry_pool_age_bars": entry["pool_age_bars"] if entry else None,
        "next_upper_pool_lower": next_upper["pool_lower"] if next_upper else None,
        "next_upper_pool_upper": next_upper["pool_upper"] if next_upper else None,
        "gap_to_next_upper_abs": gap_abs,
        "gap_to_next_upper_pct": gap_pct,
        "upper_bands_between_entry_and_next": stack_between,
        "large_gap_gate_pass": large_gap_qualifying,
        "strict_overlap_component_members": strict_comp_n,
        "touch_component_members": touch_comp_n,
        "touch_component_status": touch_unresolved,
        "ema9": ema_ctx.get("ema9"),
        "ema20": ema_ctx.get("ema20"),
        "ema59": ema_ctx.get("ema59"),
        "ema200": ema_ctx.get("ema200"),
        "close_below_ema200": under_200,
        "ema9_lt_20_lt_59_lt_200": ema_order,
        "research_perfect_pattern_likely": perfect_likely,
        "research_perfect_pattern_note": perfect_reason,
        "distance_close_to_entry_lower_pct": (
            (entry["pool_lower"] - close) / close * 100.0 if entry else None
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    config = load_yaml_and_validate(CFG)
    config = copy.deepcopy(config)
    config["backtest"]["start_utc"] = WARMUP_START
    config["backtest"]["end_utc"] = REPLAY_END
    config["backtest"]["end_utc_exclusive"] = True
    # Sep-09 liegt außerhalb des D9-Cache-Bundles → kanonischer Provider (gleiche D2-Math, read-only).
    config["density"]["density_source"] = "canonical_provider"

    prices = load_prices_from_ssot_window(WARMUP_START, REPLAY_END, end_utc_exclusive=True)
    bars = _bars_in_range(prices, WARMUP_START, REPLAY_END, end_exclusive=True)
    emas = _compute_emas(bars, prices)
    density = create_density_session(config)

    pool_age: dict[str, int] = {}
    window_docs: dict[str, Any] = {}
    structural_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    ema_rows: list[dict[str, Any]] = []

    target_bars: list[str] = []
    for win in WINDOWS:
        target_bars.extend(_bars_in_window(bars, win["start"], win["end"]))
    target_bars = sorted(set(target_bars))

    snap_cache: dict[str, dict] = {}
    for bar in target_bars:
        try:
            snap_cache[bar] = density.collect_at_close(bar)
        except Exception as exc:  # noqa: BLE001 — Forschung: fehlende Closes dokumentieren
            snap_cache[bar] = {"_error": str(exc), "upper_bands": [], "lower_bands": []}

    for win in WINDOWS:
        win_bars = _bars_in_window(bars, win["start"], win["end"])
        snapshots: list[dict[str, Any]] = []
        for bar in win_bars:
            snap = snap_cache.get(bar)
            if not snap or snap.get("_error"):
                continue
            for b in snap.get("upper_bands") or []:
                pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
                pool_age[pk] = pool_age.get(pk, 0) + 1
            row = analyze_bar(
                bar,
                config=config,
                snap=snap,
                candle=prices[bar],
                pool_age=dict(pool_age),
                emas=emas,
                window_id=win["id"],
            )
            structural_rows.append(row)
            candidate_rows.append(row)
            gap_rows.append(
                {k: row[k] for k in row if k.startswith("gap_") or k in ("decision_close_utc", "window_id", "lowest_eligible_pool_lower", "lowest_eligible_pool_upper", "next_upper_pool_lower", "upper_bands_between_entry_and_next", "large_gap_gate_pass")}
            )
            ema_rows.append(
                {k: row[k] for k in row if k.startswith("ema") or k in ("decision_close_utc", "window_id", "close_price", "close_below_ema200", "ema9_lt_20_lt_59_lt_200")}
            )
            snapshots.append(
                {
                    "bar_analysis": row,
                    "upper_bands_above_close": [
                        u for u in _band_rows(snap) if u["pool_lower"] > float(prices[bar]["c"])
                    ][:40],
                    "lower_bands": _lower_rows(snap)[:30],
                }
            )
        best = max(
            [r for r in structural_rows if r["window_id"] == win["id"] and r["gap_to_next_upper_pct"] is not None],
            key=lambda r: r["gap_to_next_upper_pct"] or 0,
            default=None,
        )
        window_docs[win["id"]] = {
            "label": win["label"],
            "bars": win_bars,
            "snapshots": snapshots,
            "strongest_gap_bar": best,
        }
        (OUT / win["json_name"]).write_text(
            json.dumps(window_docs[win["id"]], indent=2), encoding="utf-8"
        )

    def wcsv(name: str, rows: list[dict]) -> None:
        with (OUT / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    wcsv("structural_snapshots.csv", structural_rows)
    wcsv("candidate_entries.csv", candidate_rows)
    wcsv("gap_analysis.csv", gap_rows)
    wcsv("ema_context.csv", ema_rows)

    comparison = _comparison_md()
    (OUT / "comparison_vs_bt_d50_cases.md").write_text(comparison, encoding="utf-8")
    (OUT / "summary.md").write_text(_summary_md(structural_rows, window_docs), encoding="utf-8")
    print("Wrote", OUT, "bars", len(structural_rows))
    return 0


def _comparison_md() -> str:
    return """# Vergleich 09.09. (BT-D54) vs. 11.09. (BT-D50/52/53)

## 11.09. problematische Referenz (BT-D46-Fills)

| Aspekt | 12:05 → Fill 12:25 | 12:30 → Fill 12:35 |
|--------|-------------------|-------------------|
| Gewählter Entry-Pool | 0.08384–0.083844 (weit unter Nutzer-Shelf) | 0.08408–0.08409 (kleinstes atomares Band) |
| Nächstes Level / Gap | Shelf ~0.08410+ strukturell sichtbar, aber nicht Entry | Touch-Kette bis ~0.084378, SL-Debatte (D53) |
| Nutzer-Relevanz | Kein „perfekter“ Shelf-Entry | Shelf visuell relevant, System wählt atomar + Pending |

## Hypothese für „perfekte“ 09.09.-Fenster

- **Kleiner** Upper-Pool **nah** am Close (niedrige `distance_close_to_entry_lower_pct`).
- **Klarer** Abstand (`gap_to_next_upper_pct` ≥ 0,5 % Gate) zum **nächsten atomaren** Upper-Band.
- **`upper_bands_between_entry_and_next` = 0** (kein dichter Zwischen-Stack).
- Optional: Close **unter EMA200**, fallende EMA-Reihenfolge — nur dokumentieren.

## 11.09. Gegenbeispiel

- Dichter **Touch-Stack** / viele Nachbarbänder (D53: 30 Member, articulation).
- Entry oft **nicht** der visuelle Shelf, sondern **globales Minimum** `pool_lower` (BT-D21).

Siehe `gap_analysis.csv` / `structural_snapshots.csv` für 09.09.-Zahlen.
"""


def _summary_md(rows: list[dict], windows: dict) -> str:
    lines = ["# BT-D54 Kurzsummary\n"]
    for wid, doc in windows.items():
        b = doc.get("strongest_gap_bar")
        lines.append(f"## {doc['label']}\n")
        if b:
            lines.append(
                f"- Stärkster Gap-Bar: **{b['decision_close_utc']}** "
                f"Gap **{b['gap_to_next_upper_pct']:.4f}%**, "
                f"Entry **{b['lowest_eligible_pool_lower']}–{b['lowest_eligible_pool_upper']}**, "
                f"perfect_likely={b['research_perfect_pattern_likely']}\n"
            )
        perfect = [r for r in rows if r["window_id"] == wid and r["research_perfect_pattern_likely"]]
        lines.append(f"- Bars mit Forschungs-Flag „perfect_likely“: **{len(perfect)}**\n")
    return "".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
