# ABSCHLUSSBERICHT — Raw-OB200 Mechanismus A (16:04) vs B (16:13)

**Verdict:** `RAW_MECHANISM_ANALYSIS_COMPLETED`  
*(nicht `RAW_MECHANISM_UNRESOLVED_BLOCKED` — Archive vorhanden und `update_id`-Kette fail-closed sauber)*

**Output:** `results/btc_ob_fight_raw_ob200_mechanism_1604_vs_1613_v1/`  
**Symbol / Tag:** BTCUSDT · 2026-08-30  
**Fenster A:** 16:04:07Z–16:06:16Z  
**Fenster B:** 16:13:03Z–16:15:00Z  

Keine Tradingregeln · keine DB-Writes · keine Live-Änderungen · kein Commit.

---

## 1. Raw-Daten vorhanden?

**Ja.** Filesystem-Shadow `ob200_v3`:

| Segment | Datei | Größe | Events |
|---|---|---:|---:|
| 15:00–16:00 | `…/BTCUSDT_20260830T150000Z_20260830T160000Z_ob200_v3.zst` | 7.2 MB | 1 checkpoint + 35997 deltas |
| 16:00–17:00 | `…/BTCUSDT_20260830T160000Z_20260830T170000Z_ob200_v3.zst` | 11 MB | 1 checkpoint + 35997 deltas |

ClickHouse `research_ob200_snapshots_1s` allein reicht **nicht** für sub-1s; Mechanismus kommt aus dem Raw-Archive.

### Kontinuität (fail-closed)

| Check | 15h | 16h |
|---|---|---|
| Seed | `rotation_checkpoint` (200/200) | `rotation_checkpoint` (200/200) |
| `update_id` (`u`) consecutive | **0 Gaps** | **0 Gaps** |
| Book valid at EOF | ja | ja |
| Manifest `replayable` | `false` | `false` |
| Manifest `sequence_gaps` | 35997 Paare | 35997 Paare |

**Wichtig:** Manifest-`sequence_gaps` sind Bybit-**`seq`**-Sprünge (Cross-Stream), **nicht** `u`-Lücken. Reconstruct mit `MutableBook` (nur `u==last_u+1`) läuft lückenlos.  
`replayable=false` blockiert die Analyse daher **nicht**, solange Checkpoint + contiguous `u` gelten.

Seed vor beiden Fenstern: Checkpoint ~15:59:59.929Z / Start 16:00-Archiv, danach Deltas bis Fensterbeginn. `u_gaps` in Pre-Roll und Fenstern: **0**.

---

## 2. Rekonstruktion & Timelines

Pro Fenster erzeugt:

- `level_timeline_{A_FAILED|B_HELD}.csv` — Änderungen an 78900, 78910 und Nachbarlevels (±), mit:
  - Exchange-`ts`, `cts`, `local_receive_ts`
  - `qty_before` / `qty_after`
  - Change-Klasse: REFILL_OR_INCREASE / REDUCTION / DISAPPEARANCE / REAPPEARANCE
  - temporal zugeordnete Public Trades (**ohne** gemeinsame ID)
  - `consumption_status` (nie exchange-linked proven)
- `ask_exact_edge_*.csv`, `ask_above_outer_*.csv`, `bid_below_edge_*.csv`
- `bid_support_seconds_*.csv` — 1s-Raster Bid in Edge-Zone solange Mid > Outer
- `raw_mechanism_comparison.csv`, `raw_mechanism_extended_comparison.csv`
- `preflight_continuity.json`

Public Trades: Research-DB read-only, 40 310 Trades im Pad um beide Fenster.

**Assoziationsregel:** L2 und Trades haben **keine gemeinsame ID** → höchstens `TEMPORALLY_ASSOCIATED_*`. Nie `PROVEN_EXCHANGE_LINKED`.

---

## 3. Exact-Edge Ask-Pfad (78900 / 78910)

### A — vor FAILED-Cross (~16:04:08.6–.8)

| Zeit | Level | before → after | Klasse |
|---|---|---|---|
| 16:04:08.630 | 78900 | 1.448 → 1.446 | REDUCTION |
| 16:04:08.730 | 78900 | 1.446 → **6.307** | REFILL |
| 16:04:08.730 | 78910 | 0.014 → 0.060 | REFILL |
| 16:04:08.829 | 78900 | 6.307 → **0** | DISAPPEARANCE |
| 16:04:08.829 | 78910 | 0.060 → **0** | DISAPPEARANCE |

Best Ask springt mit der Disappearance auf 78904.9. Temporal-Trade-Match an diesen Exact-Tick-Disappearances: **kein** size-kompatibler Buy-only Match → `NO_TEMPORAL_TRADE_MATCH_UNMATCHED_L2_CHANGE`.

Nach Reclaim (~16:06:15): 78910 erscheint kurz wieder (0.002 / 0.257).

### B — vor HELD-Cross (~16:13:03–05)

| Zeit | Level | before → after | Klasse |
|---|---|---|---|
| 16:13:03.129 | 78910 | 0.029 → **0.729** | REFILL |
| 16:13:03.328 | 78910 | 0.729 → 0.029 | REDUCTION (unmatched) |
| 16:13:03.830 | 78910 | 0.029 → 0.282 | REFILL |
| 16:13:04.029–.529 | 78910 | 0.282 → … → **0.001** | REDUCTIONs (unmatched) |
| 16:13:05.230 | 78910 | 0.001 → **0** | DISAPPEARANCE |
| 16:13:05.230 | 78900 | 0.193 → **0** | DISAPPEARANCE |

Die Outer-Edge-Ask wird **mehrfach nachgefüllt und wieder reduziert**, dann verschwindet sie **vor/im** Cross-Batch (Best Ask bereits 78912.7). Final disappearance nur `TEMPORALLY_ASSOCIATED_AMBIGUOUS` (0.006 Buy vs 0.001 Reduktion) — **nicht** bewiesene Consumption.

---

## 4. Vergleich A vs B (Pflichtfragen)

| Frage | A FAILED | B HELD |
|---|---|---|
| Neue Ask-Liquidität **oberhalb** Outer Edge? | **Ja** — 24 990 Increase/Reappear-Events, 986 Preise | **Ja** — 42 650 Increases, 2921 Preise |
| Wiederholt nachgefüllt? | **Ja** — 969 Preise ≥2 Refills (max 91) | **Ja** — 2680 Preise ≥2 Refills (max 71) |
| Exact-Edge-Ask vor Cross weg? | **Ja** — beide Ticks 0 bei 16:04:08.829 | **Ja** — nach Refill-Zyklus 0 bei 16:13:05.230 |
| Persistente Bid-Liquidität unter dem Preis (Edge-Zone)? | **Ja** — 63 s mit Bid in [78900,78910] solange Mid>Outer; Streak **34 s** | **Nein** — nur **2 s** Streak, danach Edge-Zone leer während Preis >79 050 läuft |

Ask oberhalb Outer ist in **beiden** Fenstern massiv (OB200-Fenster wandert mit dem Mid). Der diskriminierende Fakt ist nicht „ob Ask darüber existiert“, sondern:

1. **Exact-Tick 78910** wird bei B unmittelbar vor dem Cross aktiv befüllt/geleert und endet bei 0.  
2. **Bid in der eingefrorenen Edge-Zone** bleibt bei A nach dem Ausbruch Minuten sichtbar; bei B nur ~2 Sekunden.

---

## 5. Consumption-Status

| Status | Bedeutung | Vorkommen Exact-Edge |
|---|---|---|
| `PROVEN_EXCHANGE_LINKED` | gemeinsame Trade↔L2-ID | **0** (unmöglich ohne Shared ID) |
| `TEMPORALLY_ASSOCIATED_SIZE_COMPATIBLE` | Zeitfenster + Side + Size ≈ Δqty | praktisch **0** an 78900/78910 |
| `TEMPORALLY_ASSOCIATED_AMBIGUOUS` | Trades da, Size/Side passen nicht | B Final-Disappearance |
| `NO_TEMPORAL_TRADE_MATCH_UNMATCHED_L2_CHANGE` | L2-Änderung ohne passenden Trade | Mehrheit der Reductions |

**Fazit:** Ask-Abbau an der Kante ist **beobachtet**, aber **nicht als Trade-Consumption bewiesen**. Cancel/Reprice bleibt offen trotz Raw-Deltas — weil Trade-Stream decoupled ist.

---

## 6. Was die Raw-Daten gegenüber 1s-Research-OB zusätzlich zeigen

- Sub-100ms Refill↔Reduction-Zyklen an 78910 in B (nicht in 1s-Samples sichtbar).  
- Gleichzeitige Disappearance beider Exact-Ticks in einem Delta.  
- Bid-Edge-Zone-Persistenz A (34 s Streak) vs. B (2 s) auf Sekundenbasis aus dem rekonstruierten Book.  
- Receive-Lag typisch ~80–100 ms (`local_receive_ts` − `ts`/`cts`).

---

## 7. Aussagen-Matrix

### Bewiesen

- Raw-Archive für beide Fenster vorhanden; `u`-Kontinuität ohne Gap.  
- L2 an 78900/78910: Refills, Reductions, Disappearances mit Exchange-/Receive-Timestamps.  
- Bei B verschwindet sichtbare Ask-Qty an 78910 nach Refill-Zyklen vor dem Cross.  
- Bei A bleibt nach Outside Bid-Masse in der Edge-Zone deutlich länger stehen als bei B.  
- Ask-Liquidität oberhalb Outer entsteht und wird in beiden Fenstern wiederholt angefasst (Book-Scroll + echte Qty-Änderungen).

### Plausibel

- B: wiederholtes Nachfüllen an 78910 war kein stabiler Wall-Halt; letzte Unmatched-Reductions räumten die Kante.  
- A: Bid unter/in der Edge-Zone korreliert mit begrenztem Upside und späterem Reclaim.

### Blockiert / nicht bewiesen

- Exchange-linked Consumption (fehlen Shared IDs).  
- Ob Unmatched-Reductions Cancels oder aggressor-fills ohne exakte Size-Zuordnung waren.  
- Order-Identität / „dieselbe Wall“ über Refill-Zyklen.

---

## 8. Warum nicht BLOCKED?

`RAW_MECHANISM_UNRESOLVED_BLOCKED` wäre fällig bei:

- fehlenden Raw-Dateien, oder  
- `u`-Sequenzlücken / invalidem Book nach fail-closed.

Beides **nicht** der Fall. Unbewiesene Consumption ist ein **Assoziationslimit**, kein Datenmangel.

---

## 9. Artefakte

```
results/btc_ob_fight_raw_ob200_mechanism_1604_vs_1613_v1/
  preflight_continuity.json
  level_timeline_A_FAILED.csv
  level_timeline_B_HELD.csv
  ask_exact_edge_A_FAILED.csv / ask_exact_edge_B_HELD.csv
  ask_above_outer_A_FAILED.csv / ask_above_outer_B_HELD.csv
  bid_below_edge_A_FAILED.csv / bid_below_edge_B_HELD.csv
  bid_support_seconds_A_FAILED.csv / bid_support_seconds_B_HELD.csv
  raw_mechanism_comparison.csv
  raw_mechanism_extended_comparison.csv
  ABSCHLUSSBERICHT.md
```
