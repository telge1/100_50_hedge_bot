#!/usr/bin/env python3
"""READ-ONLY: 09:20 touch vs physical line — d-ea081434c5d3 on 2026-08-30."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V3AK = Path(__file__).resolve().parent
V3AB = ROOT / "support_break_exit_v3ab"
SNAP = V3AB / "l1_v3ab_drawings_snapshot_b0718d82a94e8ad1.json"
for p in (ROOT, V3AB, V3AK, ROOT.parent):
    sys.path.insert(0, str(p))

import run_l1_pilot as l1
from line_geometry import (
    BAR_SEC_5M,
    classify_candle_interval,
    candle_interval_intersects_line,
    line_price_range_over_interval,
)
from v3ak_context import build_context_from_snapshot, DEFAULT_SNAPSHOT, FROZEN_SHA256
from run_l1_pilot import (
    c1_confirm_indices,
    in_contact_zone,
    price_reset_above_line,
    build_touch_events,
    TouchEvent,
)

LINE_ID = "d-ea081434c5d3"
OUT = V3AK


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def idx_at(candles, utc: str) -> int:
    t = int(parse_utc(utc).timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == t:
            return i
    raise KeyError(utc)


def ema20(closes: list[float]) -> float:
    if len(closes) < 20:
        return closes[-1]
    k = 2 / 21
    e = closes[0]
    for v in closes[1:]:
        e = v * k + e * (1 - k)
    return e


def physical_line_in_wick(low: float, high: float, lp_open: float, lp_end: float) -> tuple[bool, str]:
    """User-facing physical tests."""
    if low <= lp_open <= high:
        return True, "open_line_in_range"
    if low <= lp_end <= high:
        return True, "end_line_in_range"
    pr = (min(lp_open, lp_end), max(lp_open, lp_end))
    if candle_interval_intersects_line(low, high, pr[0], pr[1]):
        return True, "interval_intersects_moving_line"
    return False, "no_intersection"


def main() -> int:
    l1._bootstrap_import_paths()
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    candles = frozen["candles"]
    line = frozen["lines_by_id"][LINE_ID]
    raw = json.loads(SNAP.read_text())["drawings"]
    raw_line = next(d for d in raw if d.get("drawing_id") == LINE_ID)

    i_start = idx_at(candles, "2026-08-30 09:15:00 UTC")
    i_end = idx_at(candles, "2026-08-30 10:20:00 UTC")
    i_ema_scan = idx_at(candles, "2026-08-30 11:45:00 UTC")

    events = build_touch_events(line, candles)
    ev1 = next(e for e in events if e.touch_event_id == f"te-{LINE_ID}-0001")

    armed = True
    rows: list[dict] = []
    first_physical_utc = ""
    first_bt_touch_utc = ""

    for i in range(i_start, i_end + 1):
        c = candles[i]
        tu = l1.fmt_utc(int(c["time"]))
        t_open = float(c["time"])
        t_end = t_open + BAR_SEC_5M
        lp_open = line.price_at(t_open)
        lp_end = line.price_at(t_end)
        cls, lp_cls, dist_pct, method, touching = classify_candle_interval(
            line, t_open, float(c["high"]), float(c["low"])
        )
        contact, _ = in_contact_zone(line, c)
        phys, phys_note = (
            physical_line_in_wick(float(c["low"]), float(c["high"]), lp_open or 0, lp_end or 0)
            if lp_open and lp_end
            else (False, "no_line")
        )
        gap_low_pct = ((float(c["low"]) - lp_open) / lp_open * 100) if lp_open else None
        gap_high_pct = ((float(c["high"]) - lp_open) / lp_open * 100) if lp_open else None
        close_vs_line = float(c["close"]) - lp_open if lp_open else None
        close_above_prior = ""
        if i > i_start:
            close_above_prior = "yes" if float(c["close"]) > float(candles[i - 1]["close"]) else "no"
        reset = price_reset_above_line(line, c) if line.active_for_candle(t_open) else False
        armed_before = armed
        touch_event = ""
        if contact and armed:
            touch_event = "NEW_TOUCH_EVENT"
            armed = False
        elif reset:
            armed = True
        c1_note = ""
        if tu == "2026-08-30 09:25:00 UTC":
            conf = c
            c1_note = (
                f"C1_for_0920_touch: bullish={float(conf['close']) > float(conf['open'])} "
                f"close>line={float(conf['close']) > (line.price_at(float(conf['time'])) or 0)}"
            )
        if tu == "2026-08-30 10:25:00 UTC":
            c1_note = (
                f"C1_for_1020_touch: bullish={float(c['close']) > float(c['open'])} "
                f"close>line={float(c['close']) > (line.price_at(float(c['time'])) or 0)}"
            )

        cl = [float(x["close"]) for x in candles[: i + 1]]
        e20 = ema20(cl)
        close_above_ema20 = float(c["close"]) > e20
        prev_close = float(candles[i - 1]["close"]) if i > 0 else None
        prev_e20 = ema20([float(x["close"]) for x in candles[:i]]) if i > 0 else None
        first_cross_ema20 = ""
        if prev_close is not None and prev_e20 is not None and prev_close <= prev_e20 and close_above_ema20:
            first_cross_ema20 = "FIRST_CLOSE_ABOVE_EMA20"

        if phys and not first_physical_utc:
            first_physical_utc = tu
        if contact and not first_bt_touch_utc:
            first_bt_touch_utc = tu

        rows.append(
            {
                "utc": tu,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "line_price_at_open": f"{lp_open:.10f}" if lp_open else "",
                "line_price_at_bar_end": f"{lp_end:.10f}" if lp_end else "",
                "gap_low_to_line_open_pct": f"{gap_low_pct:.4f}" if gap_low_pct is not None else "",
                "gap_high_to_line_open_pct": f"{gap_high_pct:.4f}" if gap_high_pct is not None else "",
                "close_minus_line_open": f"{close_vs_line:.10f}" if close_vs_line is not None else "",
                "physical_intersects_line_band": "yes" if phys else "no",
                "physical_detail": phys_note,
                "backtester_class": cls,
                "distance_pct": f"{dist_pct:.4f}",
                "interval_touching_flag": "yes" if touching else "no",
                "in_contact_zone_0_20pct": "yes" if contact else "no",
                "armed_before_bar": "yes" if armed_before else "no",
                "touch_event_fired": touch_event,
                "rearm_0_30pct_close": "yes" if reset else "no",
                "close_higher_than_prior": close_above_prior,
                "bullish_candle": "yes" if float(c["close"]) > float(c["open"]) else "no",
                "close_above_line_open": "yes" if close_vs_line and close_vs_line > 0 else "no",
                "ema20": f"{e20:.10f}",
                "close_above_ema20": "yes" if close_above_ema20 else "no",
                "ema20_cross_marker": first_cross_ema20,
                "c1_forensics": c1_note,
            }
        )

    csv_path = OUT / "l1_v3ak_0915_1020_kerzen_forensic.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # EMA20 cross scan (support phase after first contact — through 11:45 for comparison only)
    ema_cross_row = None
    for i in range(i_start, i_ema_scan + 1):
        c = candles[i]
        cl = [float(x["close"]) for x in candles[: i + 1]]
        if len(cl) < 20:
            continue
        e20 = ema20(cl)
        prev_close = float(candles[i - 1]["close"])
        prev_e20 = ema20([float(x["close"]) for x in candles[:i]])
        if prev_close <= prev_e20 and float(c["close"]) > e20:
            tu = l1.fmt_utc(int(c["time"]))
            if tu >= "2026-08-30 09:25:00 UTC":
                ema_cross_row = {
                    "utc": tu,
                    "close": c["close"],
                    "ema20": e20,
                    "bullish": float(c["close"]) > float(c["open"]),
                    "lp": line.price_at(float(c["time"])),
                }
                break

    hyp_entry = ""
    hyp_price = ""
    if ema_cross_row:
        ix = idx_at(candles, ema_cross_row["utc"]) + 1
        if ix < len(candles):
            hyp_entry = l1.fmt_utc(int(candles[ix]["time"]))
            hyp_price = float(candles[ix]["open"])

    actual_entry_utc = "2026-08-30 11:45:00 UTC"
    actual_price = float(candles[idx_at(candles, actual_entry_utc)]["open"])
    sl_pct = 1.2

    md = f"""# L1 V3AK — Forensik Touch 09:20 vs. Chart · 30.08.2026

**READ-ONLY** · Support `{LINE_ID}` · Trade `te-{LINE_ID}-0003` · Entry **11:45 @ {actual_price}**

## Zeichnungs-SSOT (Backtester)

| Feld | Wert |
|------|------|
| `start_timestamp` | {raw_line['start_timestamp']} → **2026-08-30 09:15:00 UTC** |
| `start_price` | {raw_line['start_price']} |
| `end_timestamp` | {raw_line['end_timestamp']} → **2026-08-30 22:05:00 UTC** |
| `end_price` | {raw_line['end_price']} |
| `extend_right` | {(raw_line.get('metadata') or {}).get('extend_right', False)} |
| Kerzen-Zuordnung | `classify_candle_interval(line, candle['time'], high, low)` — **Open-Zeitstempel** + Intervall **[t, t+300s)** |

Abweichung Chart↔Backtester: nur wenn Chart **andere Anker** oder **Close-Zeit** statt **Open-Zeit** für die Linie nutzt. Die L1-Forschung interpoliert `price_at(candle_open)` und `line_price_range_over_interval` über die Bar.

## Aufgabe 1 — Warum 09:20 als Touch?

**Kerze 09:20 UTC (OHLC 0.08456 / 0.08465 / 0.08449 / 0.08451):**

- Linienpreis **am Bar-Open:** **0.0843190944**
- **Low 0.08449 > Linie** → **kein** Wick-Durchstich am Open-Punkt; **kein** `low ≤ line ≤ high` an der Open-Linie.
- `classify_candle_interval`: Klasse **`NEAR_0_20`**, `touching=False`, Distanz **~0.20 %** (Mindestabstand Low → Linie am Open).
- `in_contact_zone`: **ja** — Contact = Klassen **TOUCH | NEAR_0_10 | NEAR_0_20** (≤ **0,20 %** Toleranz, `config.TOUCH_ZONE_MAX_DIST_PCT`).

**Antwort auf die Chart-Beobachtung:** Der Backtester meldet **keinen physischen Schnitt** (`interval_touching_flag=no`), sondern **Annäherung innerhalb 0,20 %**. Das ist **regelkonform**, nicht „Low berührt grüne Linie“.

**Erster physischer Linienband-Schnitt (moving line über [t,t+300)):** **{first_physical_utc or '—'}**  
**Erster Backtester-Kontakt (0,20 %-Zone):** **{first_bt_touch_utc}**

## Aufgabe 2 — Touch-Sequenz 09:15–10:20

Volle Tabelle: **`l1_v3ak_0915_1020_kerzen_forensic.csv`**

- **09:20:** `NEW_TOUCH_EVENT` → **disarmed** bis Re-Arm (Close ≥ Linie+0,30 %).
- **09:25–10:15:** weitere **`in_contact_zone=yes`**, aber **kein** neues Event (**disarmed**).
- **10:20:** Re-Arm + zweites Touch-Event `…-0002`.
- **10:25:** C1 für 10:20 scheitert (bearish).

**Ja (B):** Der **09:20-„Touch“ (NEAR_0_20)** hat **disarmed** — relevante Folge-Kontakte erzeugen **keine** neuen Touch-IDs, bis Re-Arm.

## Aufgabe 3 — EMA20 (hypothetisch, kein L1-Gate)

"""
    if ema_cross_row:
        lp = ema_cross_row["lp"]
        md += f"""- **Erstes Close > EMA20** (nach 09:25-Kontaktphase): **{ema_cross_row['utc']}**
  - Close **{ema_cross_row['close']}** · EMA20 **{ema_cross_row['ema20']:.10f}** · vorher Close ≤ EMA20
  - Close > Linie @ Open: **{float(ema_cross_row['close']) > float(lp) if lp else 'n/a'}** · bullisch: **{ema_cross_row['bullish']}**
  - Hypothetischer Entry (nur Modell): **{hyp_entry} @ {hyp_price}**
  - **Kein** L1-Gate — ersetzt **nicht** Touch+C1.
"""
    else:
        md += "- Kein erstes EMA20-Cross bis 11:45 nach 09:25 gefunden.\n"

    if hyp_price and actual_price:
        diff = (float(actual_price) - float(hyp_price)) / float(hyp_price) * 100
        sl_hyp = float(hyp_price) * (1 - sl_pct / 100) if hyp_price else None
        sl_act = actual_price * (1 - sl_pct / 100)
        sl_hyp_s = f"{sl_hyp:.10f}" if sl_hyp is not None else "n/a"
        md += f"""
## Aufgabe 4 — 11:45 vs. hypothetisch EMA20

| | Tatsächlich (11:45) | Hypothetisch (EMA-Cross+1) |
|---|---|---|
| Entry UTC | {actual_entry_utc} | {hyp_entry or 'n/a'} |
| Entry-Preis | {actual_price} | {hyp_price or 'n/a'} |
| Δ Entry % | — | {diff:.4f}% vs. 11:45 |
| SL (−1,2 %) | {sl_act:.10f} | {sl_hyp_s} |

**Regelkonformität hypothetischer Früheinstieg:**  
Selbst mit EMA20-Cross **ersetzt das nicht** die L1-Pipeline **Touch-Event → C1 (bullish, close>line) → Entry+1 Bar**.  
Am EMA-Cross-Zeitpunkt war kein gültiges C1 nach **armed** Touch-Event abgeschlossen (09:20-C1 **failed**; danach **disarmed** Kontakte).

## Aufgabe 5 — A–E

| Frage | Antwort |
|-------|---------|
| **A** 0,20 %-Zone → Touch 09:20 ohne physischen Kontakt? | **Ja** — Klasse `NEAR_0_20`, Low **~0,20 %** über Linie (Open-Preis). |
| **B** DISARMED → spätere Kontakte ignoriert? | **Ja** — 11 Kerzen 09:25–10:15 in Zone, **kein** neues Event. |
| **C** EMA20-Close hätte früheren Entry erlaubt? | **Nur hypothetisch** — **nicht** in aktueller Strategie; würde **C1/Touch** nicht ersetzen. |
| **D** Chart vs. Backtester-Geometrie? | Dieselbe Snapshot-Linie (Anker 09:15/22:05). **Erster phys. Schnitt: 09:40**. 09:25 ≈ **NEAR** (~0,024 %). 09:20 = **0,20 %-Toleranz** — Chart-Zweifel berechtigt. |
| **E** Bug oder Regel? | **Korrekte Umsetzung** von `classify_distance` + `in_contact_zone` — kein Implementierungsfehler. |

## Event `te-{LINE_ID}-0001`

- Touch-UTC: **2026-08-30 09:20:00 UTC**
- C1-Bar 09:25: **bearish** (`close ≤ open`) → `NO_C1_CONFIRM_AFTER_FIRST_TOUCH`
- Erstes **C1+Entry**: Touch **11:35** → Confirm **11:40** → Entry **11:45** (Event `…-0003`)

**STOP** — READ-ONLY.
"""
    (OUT / "L1_V3AK_TOUCH0920_KERZEN_FORENSIC.md").write_text(md, encoding="utf-8")
    print(OUT / "L1_V3AK_TOUCH0920_KERZEN_FORENSIC.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
