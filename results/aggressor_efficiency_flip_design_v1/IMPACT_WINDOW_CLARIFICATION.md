# IMPACT_WINDOW_CLARIFICATION — AGGRESSOR_EFFICIENCY_FLIP_DISCOVERY_V1

**Datum (UTC):** 2026-08-29  
**Scope:** Read-only Klärung des Impact-Fenster-Designs. Keine Implementierung.  
**Ergänzt (überschreibt nicht):** `feature_window_contract.md`, `ABSCHLUSSBERICHT.md`

---

## 1. Verdict

**B. DUAL_IMPACT_WINDOW_DESIGN_CONFIRMED_WITH_LIMITATIONS**

Contemporaneous Impact (im geschlossenen Flow-Fenster) und Post-Flow Follow-through können kausal getrennt gemessen werden und lösen das semantische Fehlklassifikationsproblem von Post-Flow-only. Einschränkungen bleiben durch die Trade-Preisquelle (keine Mid/BBO im DOGE-Ref-Fenster, ms-Ties, leere Sekunden).

---

## 2. Problem des bisherigen Post-Flow-only-Designs

Hypothese: Die dominante Aggressor-Seite erzeugt **bereits während ihres Bursts** wenig gerichtete Preisbewegung (Kompression/Absorption). Die Gegenseite wirkt später stärker.

**Post-Flow-only `(t1,t2]`** misst nur die Nachwirkung. Damit wird Fall C fälschlich als Compression gelesen:

| | Während `[t0,t1)` | Nach `t1` | Wahrheit | Post-Flow-only |
|---|---|---|---|---|
| Fall C | starkes Down bei hohem Sell | flach | **effiziente Sell-Initiative** | „wenig Impact“ → **falsche Compression** |

Smoke (DOGE 2026-08-29, unfitted Bänder, nur Semantik): mindestens **5** Sell-dominante 5s-Fenster mit hohem Sell-Notional (≥P90 unter Sell-dom), starkem contemporaneous Down und schwachem Post-Down — z. B. `10:21:35Z` Sell≈254k USDT, contemp ≈−9.5 bps, post ≈0. Siehe `dual_window_semantic_smoke.csv`.

---

## 3. Live-Sicherheitsbestätigung

Nur SELECT auf `public_trades_canonical`. Keine Writes. Keine Prozess-Eingriffe. Keine bestehenden Dateien geändert. Kein Commit. Keine Implementierung.

---

## 4. Empfohlenes Drei-Fenster-Modell

```text
A. FLOW + CONTEMPORANEOUS IMPACT   [t0, t1)     decision ≥ t1
B. POST-FLOW FOLLOW-THROUGH       [t1, t2)     decision ≥ t2
C. COUNTER-SIDE SEARCH            [t2, t3)     eigene geschlossene Gegen-Bursts
```

- **A** liefert Notional/Dominanz **und** Burst-interne Preiswirkung (Pflicht für Compression).
- **B** unterscheidet Absorption (flach/Rebound) vs. verzögerte Initiative (nachlaufendes Down).
- **C** ist ein **neues Event**, nicht derselbe 15m-Klumpen.

Intervalle durchgängig **halb-offen `[start,end)`**. Features erst nach Schließen der rechten Grenze.

---

## 5. Long-/Short-Compression-Semantik

### LONG — Sell-Compression (alle Gates deskriptiv, unfitted)

| Fall | Contemporaneous (Sell) | Post-Flow | Klassifikation |
|---|---|---|---|
| **A** | geringes Down | geringes Down | mögliche Sell-Absorption (stark) |
| **B** | geringes Down | Rebound (Up) | Absorption + Reclaim |
| **C** | **starkes** Down | flach | Sell-Initiative — **keine** Compression |
| **D** | geringes Down | **starkes** verzögertes Down | verzögerte Initiative — **keine** bestätigte Absorption |
| **E** | geringes Notional | — | kein Event |

**V1 Compression bestätigt (LONG)** nur wenn:

1. Sell-dominant + ausreichendes normalisiertes Sell-Notional, und  
2. contemporaneous sell-directional impact **niedrig** (Fall A/B/D-Kandidat, nicht C), und  
3. Post-Flow **kein** starkes verzögertes Sell-Follow-through (schließt D aus), und  
4. optional Reclaim-Flag für B (Analyse, nicht Pflicht).

### SHORT — Buy-Compression (Spiegel)

Buy↔Sell, Up↔Down, Rebound = Down nach Buy-Burst.

---

## 6. Initiative-Semantik

Gegeninitiative (LONG = Buy) als **eigenes** geschlossenes Burst-Ereignis in `[t2,t3)`:

| Label | Bedeutung |
|---|---|
| `BUY_BURST_WITH_IMPACT` | Dominanz + Notional + contemporaneous Up |
| `BUY_BURST_WITHOUT_IMPACT` | Burst ohne Up (keine Initiative) |
| `BUY_BURST_WITH_DELAYED_IMPACT` | schwach contemporaneous, starkes Post-Up |
| `BUY_BURST_FAILED_RECLAIM` | Up dann voller Reclaim |
| `BUY_INITIATIVE_CONFIRMED` | With-Impact (oder Delayed nach Policy) + kein sofortiger Full-Reclaim; Structure/Acceptance separat danach |

V1-Default für Flip: **`BUY_BURST_WITH_IMPACT`** (contemporaneous Pflicht); Delayed nur Sensitivitätsvariante. Sell gespiegelt.

---

## 7. Preis- und Bucket-Vertrag

| Name | Definition (kausal) |
|---|---|
| `flow_start_price` | `argMin(price,(trade_ts,trade_id))` in `[t0,t1)` |
| `flow_end_price` | `argMax(price,(trade_ts,trade_id))` in `[t0,t1)` |
| `post_flow_start_price` | `= flow_end_price` (bekannt bei `t1`) |
| `post_flow_end_price` | last trade in `[t1,t2)` per Tie-Break; wenn leer: `= post_flow_start_price`, `post_empty=true`, Moves=0 |
| `counter_flow_start/end_price` | analog im Gegen-Burst |
| `structure_break_price` | last/trigger price der bestätigenden geschlossenen Break-Bucket |
| `earliest_entry_price` | last trade der Entry-1s-Bucket nach `final_decision_ts` |

**Tie-Break:** `(trade_ts ASC, trade_id ASC)` — stabil, keine erfundene Sequence-ID.  
**Keine LOCF für Notional.** LOCF-Preis nur bei leerem Post-Fenster mit Flag.  
**Max up/down** im Fenster relativ zu `flow_start_price` bzw. `post_flow_start_price`, nur aus Trades **innerhalb** des geschlossenen Fensters.

---

## 8. Decision Timestamps

| Timestamp | Frühester kausaler Wert |
|---|---|
| `compression_flow_close_ts` | `t1` |
| `compression_confirmed_ts` | `t2` (A+B ausgewertet) |
| `counter_initiative_flow_close_ts` | Gegen-Burst `u1` |
| `counter_initiative_confirmed_ts` | Gegen Post-Ende `u2` (oder `u1` wenn Delayed-Variante aus) |
| `structure_break_confirmed_ts` | Break-Bucket-Close |
| `acceptance_confirmed_ts` | Acceptance-Regel erfüllt |
| `final_decision_ts` | `max` der erforderlichen Confirms |
| `earliest_entry_ts` | nächste geschlossene 1s nach `final_decision_ts` |

---

## 9. V1-Defaults

| Parameter | Default | Begründung |
|---|---|---|
| Burst / contemporaneous | **5s** `[t0,t1)` | Audit: 1s zu rauschig; 5s phase-trennend |
| Post-flow | **5s** `[t1,t2)` | trennt C vs A schnell; wenig Lag |
| Gegen-Suche | **3m** `[t2,t3)` | Raum für Flip ohne 15m-Mix |
| Acceptance | **N× geschlossene 1s** (5s-Block optional) | trade-grain |
| Sensitivität (vorab) | Post 15s/30s; Suche 1m/5m | deklariert, nicht gefittet |

---

## 10. Ordinaler Score (ohne fragile Division)

**Compression (Punkte, unweighted skeleton):**

- `aggressor_notional_rank` hoch  
- `contemporaneous_impact_rank` **invertiert** (wenig adverse Bewegung)  
- `post_flow_followthrough_rank` **invertiert**  
- optional `reclaim_flag`  

**Hard veto:** starkes contemporaneous adverse Impact → **keine** Compression (Fall C).

**Initiative:** counter notional rank + contemporaneous directional rank + post follow-through + no_immediate_reclaim.

**Flip:** Compression bestätigt ∧ spätere Gegeninitiative ∧ Reihenfolge ∧ keine Invalidation ∧ (später) Structure ∧ Acceptance.

---

## 11. Semantik-Smoke

Datei: `dual_window_semantic_smoke.csv`.

- Grid 5s + Post 5s; Selektion: feste Orientierungspunkte **oder** Top-Sell-Notional innerhalb deskriptiver Fall-Klassen (nicht nach Forward-Return).  
- **Case C existiert** → Post-Flow-only ist semantisch unzureichend.  
- **Case A existiert** (u. a. Nähe 11:55) → Dual-Fenster unterscheidet Absorption vs. Initiative.  
- Kein Profit-Claim; Bänder unfitted.

---

## 12. Einschränkungen

- Trade-Preis ≠ Mid; Bounce möglich.  
- Leere Post-Fenster → Moves 0 mit Flag (konservativ).  
- ms-Ties nur über `trade_id`.  
- Deskriptive Fall-Bänder im Smoke ≠ Produktions-Schwellen.  
- OB/Mid-Parität später an Tagen mit OB1s.

---

## 13. Exakte notwendige Änderungen am bisherigen Designbericht

Am **inhaltlichen Vertrag** (nicht Datei überschreiben) ändern:

1. **Verwerfe** „Impact ausschließlich post-flow (B-only)“ als Compression-Gate.  
2. **Ersetze** durch Dual: contemporaneous **Pflicht** + post-flow **Bestätigung/Veto**.  
3. FSM: `IMPACT_COMPRESSION` erst bei `compression_confirmed_ts = t2`.  
4. Fall C = Hard Veto.  
5. Ordinaler Score: contemporaneous Rank invertieren + Post Rank invertieren.  
6. `updated_feature_window_contract.md` ist die maßgebliche Fenster-Spezifikation für F0.

Bestehende Dateien `feature_window_contract.md` / `ABSCHLUSSBERICHT.md` bleiben historisch; bei Widerspruch gilt diese Klärung + `updated_feature_window_contract.md`.

---

## 14. Go/No-Go für F0-Scaffold

**GO** — unter der Bedingung, dass F0 Dual-Impact (A+B) implementiert und Post-Flow-only nicht als Compression-Gate verwendet.

**NO-GO**, falls jemand Post-Flow-only unverändert übernimmt.
