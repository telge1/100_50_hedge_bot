# ABSCHLUSSBERICHT — OB/Wall-Faktenvergleich FAILED_BREAKOUT (16:04) vs HELD_BREAKOUT (16:13)

**Verdict:** `BTC_OB_FIGHT_WALL_COMPARISON_1604_VS_1613_FACTS_READY`  
**Quelle (read-only):** `results/btc_ob_fight_cases/20260830T160000Z/run_003/`  
**Output:** `results/btc_ob_fight_wall_comparison_1604_vs_1613_v1/`  
**Profil-Cutoff:** `2026-08-30T16:00:00Z`  
**Feste Kanten:** TPO-VAH `78900.0` · Volume-VAH / Outer Edge `78910.0`  
**Regeln:** `rules_frozen=false` · keine Tradinginterpretation · keine Cancellation-Behauptung bei nur UNMATCHED

---

## 1. Datenbasis

Heavy-Export aus `run_003` bestätigt:

| Artefakt | Zeilen (Header exkl.) |
|---|---:|
| wall_observations.csv | 72 010 |
| wall_tracks.csv | 32 214 |
| wall_transitions.csv | 45 467 |
| wall_trade_matches.csv | 17 203 |
| edge_book_coverage.csv | 86 412 |
| edge_region_depth_samples.csv | 4 936 |
| edge_region_consumption_events.csv | 1 634 |
| same_timestamp_multistate_groups.csv | 21 734 |

**Wichtige Quellenwahl:**

- **Volle 1s-OB200-as-of-Kantenmengen** kommen aus `edge_book_coverage.csv` (`UPPER` + `EXACT_LEVEL_TICK` bei 78900/78910) → Timelines neu gebaut.
- `edge_region_depth_samples.csv` ist **sparse** (Visit-/Fight-Sampling; A≈111s, B≈17s) und allein **nicht** ausreichend für „alle Samples“.
- `wall_observations.csv` = **Top-Wall-Kandidaten**, nicht das volle OB200.

---

## 2. Kanonische Sequenz (Same-Timestamp atomar)

### A — FAILED_BREAKOUT

| Phase | Zeit |
|---|---|
| Vorlauf | 16:03:30 – 16:04:08.648Z |
| Erster Outside-Kontakt (ambiguous batch) | 16:04:08.649Z |
| Kanonisches Outside | **16:04:10.201 – 16:06:14.683Z** (124.482 s) |
| Reclaim | 16:06:14.683Z (`rcl_1e8823dd805a`) |
| Nachlauf | bis 16:06:45Z |

Kanonisches Outside `exc_5c92e06fdcd6`: +5.81 Mio. USD Taker-Delta, 6178 Trades, max. Distanz ~9.87 bps über Outer Edge, **reclaimed=true**.

**Same-Timestamp:** 7 ambige Multistate-Gruppen im A-Fenster, u. a.:

- `16:04:08.649Z` — 287 Trades, States `INSIDE|BETWEEN|OUTSIDE`, Δ≈+435 k USD → **atomar**, keine Nullsekunden-Episode-Vervielfachung
- `16:06:14.683Z` — Reclaim-Batch, Δ≈−20 k USD (Mikro)

### B — HELD_BREAKOUT

| Phase | Zeit |
|---|---|
| Vorlauf | 16:12:30 – 16:13:05.158Z |
| Cross-Batch | 16:13:05.159 – 16:13:05.163Z |
| Bestätigung (Analysefenster) | bis 16:15:00Z |

Kanonisches Outside `exc_176e4b8a151a`: Start **16:13:05.163Z**, Dauer 4043.663 s (bis 17:20:28.826Z), Δ≈+8.07 Mio. USD, 148 159 Trades — **kein Reclaim im 16:15-Fenster**.

**Same-Timestamp:**

- `16:13:05.159Z` — INSIDE|BETWEEN, 45 Trades, Δ≈+41 k
- `16:13:05.163Z` — BETWEEN|OUTSIDE, 166 Trades, Δ≈+456 k → Cross atomar

---

## 3. Edge-Depth / OB200 pro Sekunde

Outputs:

- `edge_depth_timeline_1604.csv` — **196** Sekunden
- `edge_depth_timeline_1613.csv` — **151** Sekunden

Je Sekunde: Best Bid/Ask, sichtbare Range, Ask/Bid-Qty an 78900 & 78910, Top-Wall-Kandidaten, Coverage, Sample-Age.

### Schlüssel-Snapshots A

| sample_ts | best bid/ask | ask@78900 | ask@78910 | bid@78900 |
|---|---|---:|---:|---:|
| 16:04:07Z | 78890.8 / 78890.9 | **1.448** | 0.014 | 0 |
| 16:04:08Z | 78904.8 / 78904.9 | **0** | **0** | 0.459 |
| 16:04:10Z | 78914.2 / 78914.3 | 0 | 0 | 0.001 |
| 16:06:14Z (Reclaim-Sekunde) | 78906.6 / 78906.7 | 0 | 0 | 0.001 |
| 16:06:15Z | 78902.2 / 78902.3 | 0 | 0.257 | 0 |

Vor dem Cross war Ask an der TPO-VAH noch ~1.45 BTC sichtbar; **in der Cross-Sekunde ist Ask an beiden exakten Kanten bereits 0**. Das ist kein Beweis für Trade-Konsum — siehe Transitions.

### Schlüssel-Snapshots B

| sample_ts | phase | best bid/ask | ask@78900 | ask@78910 | bid@78900 | Top-Bid-Walls (Kandidaten) |
|---|---|---|---:|---:|---:|---|
| 16:13:03Z | VORLAUF | 78892.4 / 78892.5 | 0.193 | **0.282** | 0 | unter Mid |
| 16:13:04Z | VORLAUF | 78894.6 / 78894.7 | 0.193 | **0.001** | 0 | 78894.6:4.875 … |
| 16:13:05Z | VORLAUF* | 78912.6 / 78912.7 | 0 | 0 | **0.15** | unter Mid |
| 16:13:06Z | CONFIRM | 78936.6 / 78936.7 | 0 | 0 | 0.007 | **78903.6:4.863**, 78900.5:3.285 |
| 16:13:30Z | CONFIRM | 79004.7 / 79004.8 | 0 | 0 | 0 | weit unter Mid |
| 16:15:00Z | CONFIRM | 79094.3 / 79094.4 | 0 | 0 | 0 | 79059–79067 |

\*1s-Sample `16:13:05Z` liegt vor `16:13:05.159Z` (Vorlauf-Label); der eigentliche Cross sitzt im Millisekunden-Batch.

Ask an Outer Edge fällt **0.282 → 0.001 in einer Sekunde** (16:13:03→04), dann 0 — ohne trade-associated Wall-Transition am Exact-Tick.

---

## 4. Wall-Transitions ↔ Trades

Output: `wall_transition_evidence.csv` (462 Zeilen nahe Upper-Edge-Zone ±Band).

| Fenster | Transitions | TRADE_ASSOCIATED | UNMATCHED | QTY_INCREASE |
|---|---:|---:|---:|---:|
| A | 406 | **6** | 349 | 51 |
| B | 56 | **0** | 56 | 0 |

**Keine** Transition genau auf Tick 78900 oder 78910 in beiden Fenstern. Die 6 trade-associated Events in A liegen **neben** der Kante (z. B. Ask 78914.3, Bid 78918–78929), nicht am eingefrorenen Exact-Tick.

**Edge-Region-Consumption** in beiden Fenstern: **100 % UNMATCHED** (A: 192 Events, B: 28). Scopes: vor allem `PROFILE_EDGE_ZONE` / `TPO_EDGE_BIN` / `VOLUME_EDGE_BIN`. **0** `EXACT_LEVEL_TICK`-Consumption in A/B.

**Regel eingehalten:** UNMATCHED ≠ Cancellation. Sichtbarer Ask-Rückgang ohne Trade-Match = **UNRESOLVED** zwischen Konsum, Cancel und Reprice.

---

## 5. Ask-Seite beim „erfolgreichen“ Breakout (B)

**Bewiesen:**

- Sichtbare Ask-Qty an 78910 kollabiert unmittelbar vor dem Cross.
- Alle scope-bezogenen Consumption-Events im Fenster sind UNMATCHED.
- Keine trade-associated Wall-Transition am Exact-Tick.

**Nicht bewiesen (UNRESOLVED_AT_1S):**

- Ob die Ask-Liquidität **konsumiert**, **zurückgezogen/repriced** oder beides war.
- Sub-Sekunden-Reihenfolge Ask-Abbau ↔ aggressiver Buy-Flow.

**Raw-Archive-Bedarf:** L2/L3-Orderbuch-Deltas + Trade-IDs mit Exchange-Sequenz unterhalb 1 s am Outer Edge 78910 im Intervall `[16:13:03, 16:13:06)`.

---

## 6. Bid-Support nach 16:13

**Bewiesen (fakten):**

- Ab `16:13:06Z` erscheinen Bid-Wall-**Kandidaten** in der Edge-Zone unter dem neuen Mid, z. B. 78903.6 @ 4.863, 78900.5 @ 3.285.
- Exact-Tick Bid @78900: 0.15 BTC in der Cross-Sekunde, danach rasch ~0 während der Preis auf >79 000 läuft.
- Nach ~16:13:10 liegen 78900/78910 oft außerhalb der sichtbaren OB200-Range → Edge-Qty nicht mehr beobachtbar.

**Nicht bewiesen:**

- Dass diese Bids **gegen Sell-Aggression gehalten** haben (Absorption).
- Identität/Kontinuität einzelner Orders über 1s-Samples.

Status: **UNRESOLVED_AT_1S** — „Bid-Support sichtbar als Kandidat“, nicht „gehalten bewiesen“.

---

## 7. Was hielt den ersten Ausbruch auf?

**Bewiesen:**

1. Kanonisches Outside endet mit **Reclaim** bei 16:06:14.683Z (Cross zurück auf/unter Outer Edge).
2. Während Outside bleibt der Preis relativ nah (max ~9.9 bps über Outer); kein Drift wie bei B.
3. Am Cross selbst waren Exact-Ask-Mengen an 78900/78910 bereits 0 — der Stop kam **nicht** von einer dicken Rest-Ask-Wall genau auf den eingefrorenen Ticks.
4. Im Nachlauf starker Sell-Druck in ambigen Same-TS-Gruppen (z. B. 16:06:15–16:06:16, Δ −473 k / −811 k).

**Plausibel, nicht bewiesen:**

- Bid-Liquidität unter dem Mid (Wall-Kandidaten bis ~15 BTC bei ~78895) begrenzte den Upside und ermöglichte den Rücklauf.

**Blockiert:**

- Ob Bids „absorbiert“ haben oder Asks „nachgefüllt“ wurden → nur UNMATCHED/1s-OB.

---

## 8. Was unterschied den zweiten Ausbruch?

| Dimension | A FAILED | B HELD |
|---|---|---|
| Outside-Dauer (kanonisch, im Fokus) | 124 s, dann Reclaim | startet 16:13:05.163, **kein Reclaim bis 16:15** (läuft >1 h) |
| Distanz über Outer | max ~10 bps | innerhalb Minuten +100 bps und mehr |
| Ask@78910 vor Cross | klein (0.014) | kollabiert 0.282→0.001 in 1 s |
| Trade-associated near zone | 6 | **0** |
| Edge-consumption matching | alles UNMATCHED | alles UNMATCHED |
| Same-TS Cross-Batch | stark ambig, danach Flipper | ambig, dann **sofort** Outside+Trend |
| Bid nach Cross in Edge-Zone | Reclaim-Pfad | Kandidaten unter neuem Mid, Preis läuft weg |

Unterschied faktisch: **Preis bleibt bei A nahe der Kante und wird reclaimed; bei B verlässt er die beobachtbare Edge-Region sofort und bleibt außerhalb.** Mechanismus Ask konsumiert vs. cancel bleibt UNRESOLVED.

---

## 9. Refill-Reconciliation (Console „5“)

| Metrik | Wert |
|---|---|
| Console / `manifest.post_trade_refill_count` | **5** |
| `post_trade_refill_events.csv` Zeilen | 5 = **4 Recovery + 1× `NO_POST_TRADE_REFILL_OBSERVED_IN_WINDOW`** |
| `exact_refill_events.csv` | **4** |
| `fight_sequence_summary.exact_refill_count` | **4** |
| Zeitstempel aller Refills | **17:28:27Z – 17:29:51Z** |
| In Fenstern 16:03–16:15 | **keine** |

Die Console-5 ist also die Post-Trade-Refill-Zählung inkl. Sentinel-Zeile; Exact-Refill-Contract zählt 4 Recovery-Events. Beides ist **nicht** die Wall-`QTY_INCREASE_OBSERVED`-Menge und irrelevant für 16:04/16:13.

---

## 10. Same-Timestamp-Resolution

Output: `same_timestamp_resolution.csv` (3239 Gruppen in beiden Analysefenstern; 9 ambig).

Regeln angewandt:

- Gruppe = **eine** atomare Aggressionseinheit
- keine Aufspaltung in Nullsekunden-Episoden für Zählungen
- `exchange_order_proven=false`
- Multistate → `AMBIGUOUS_MULTI_STATE` / `ATOMIC_SAME_TIMESTAMP_GROUP`

---

## 11. Aussagen-Matrix

### Bewiesen

- Kanonische Outside-/Reclaim-Zeitstempel und Dauern (A reclaim, B kein Reclaim bis 16:15).
- Exact-Tick Ask/Bid-Mengenpfade aus voller 1s-`edge_book_coverage`.
- Ask an beiden Kanten = 0 zum jeweiligen Cross-Sekundenraster.
- Edge-Region-Consumption in A/B ausschließlich UNMATCHED.
- Keine Exact-Tick-Wall-Transitions; Wall-Observations nur Kandidaten.
- Refill-5 vs Exact-4 Reconciliation; keine Refills in A/B-Fenstern.
- Same-TS-Ambiguität an beiden Crosses.

### Plausibel

- A: Bid-Liquidität unter Mid begrenzte den Ausflug und begünstigte Reclaim.
- B: Ask-Abbau an Outer Edge unmittelbar vor Cross räumte den Weg; danach tragen Bid-Kandidaten unter dem Mid nicht nachweisbar „halten“, der Preis läuft einfach weiter.

### Blockiert / UNRESOLVED_AT_1S

- Ask: konsumiert vs. cancel/reprice.
- Bid: gehalten gegen Sell-Aggression.
- Exchange-Order innerhalb Same-Timestamp-Gruppen.
- Jede Absorptions-/Kontroll-/Breakout-Bestätigung.

**Benötigter Raw-Archive-Beweis:** Sub-1s Orderbuch-Deltas + Trade-Sequenz (Exchange-Order) für `[16:04:08.600, 16:04:10.300)` und `[16:13:03.000, 16:13:06.500)` an Ticks 78900/78910 und benachbarten Asks/Bids.

---

## 12. Artefakte

| Datei | Inhalt |
|---|---|
| `wall_comparison_1604_vs_1613.csv` | Fenstervergleich, Ask-Pfad, Transitions, Coverage |
| `edge_depth_timeline_1604.csv` | 196× 1s Exact-Edge + Wall-Kandidaten |
| `edge_depth_timeline_1613.csv` | 151× 1s Exact-Edge + Wall-Kandidaten |
| `wall_transition_evidence.csv` | Transitions nahe Edge-Zone mit Evidence-Klasse |
| `same_timestamp_resolution.csv` | Atomare Same-TS-Gruppen |
| `refill_reconciliation.json` | Console-5 vs Exact-4 |
| `ABSCHLUSSBERICHT.md` | dieser Bericht |

---

## 13. Sicherheit / Grenzen

- Read-only auf `run_003`; keine bestehenden Runs überschrieben.
- Keine Collector-/Dashboard-/Live-Änderungen.
- Kein Commit, kein Push.
- Keine Breakout-/Absorptions-/LONG-SHORT-Regeln.
