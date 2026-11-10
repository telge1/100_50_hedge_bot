# PD1 1h — Lokale End-to-End-Validierung

**Datum:** 2026-11-10  
**Dashboard:** `127.0.0.1:3000` (läuft, Research-Route **401** ohne Login)  
**Kein Produktions-Deploy, keine Dienste neu gestartet, keine CH-Writes**

---

## Gesamturteil

| Bereich | Ergebnis |
|---------|----------|
| API / Engine / Lifetimes / Cache / 5m-Regression | **PASS** |
| Static UI (HTML/JS/Adapter) | **PASS** |
| Browser-Chart (visuell, Density, Slider) | **NICHT VERIFIZIERT** (Auth 401) |

**Empfehlung:** Datenpfad für lokale Anzeige **freigegeben zur manuellen UI-Prüfung** nach Login. Formales **UI-PASS** ausstehend.

---

## 1. Dashboard & UI (statisch + HTTP)

| Check | Ergebnis |
|-------|----------|
| Pool-TF-Selector `5m \| 1h` in `research_charts.html` | OK |
| `selectedPoolTimeframe()` + Scope `1h` PD1 in JS | OK |
| Scopes entkoppelt von `pane.tf` (`activeScopesForPane`) | OK |
| Adapter `1h → 60` min | OK |
| Live-URL `/live-charts/research` | Antwort `401 Unauthorized` (erwartbar) |
| JS-Fehler im Browser | nicht prüfbar ohne Session |

Screenshot (Auth-Fehler, kein Chart):  
`/tmp/cursor/screenshots/page-2026-11-09T21-25-12-487Z.png`

---

## 2. Vier Kombinationen (Pool-API; Kerzen-TF client-seitig)

Fenster: `2026-08-15T06:00:00Z` … `2026-08-15T12:00:00Z`, PD1, `exact_origin_v1`

| Kombination (Kerzen + Pools) | API `timeframe` | Pools | 5m/1h IDs | Fehler |
|------------------------------|-----------------|-------|-----------|--------|
| 5m + 5m | 5m | 379 | nur `:5m:` | — |
| 5m + 1h | 1h | 122 | nur `:1h:` | — |
| 1h + 1h | 1h | 122 | nur `:1h:` | — |
| 1h + 5m | 5m | 379 | nur `:5m:` | — |

**Preis-Ausrichtung:** Window-`prices` aus 5m-CH (wie frozen Density); Pool-Bänder aus Lifetimes — engine bounds (`bottom`/`top`) konsistent mit 1h-Engine. **Keine Kreuz-Kontamination** der `pool_id`-Timeframes.

---

## 3. Historische ACTIVE-Pools (3 Fenster)

| Fenster | Zeitraum | 1h-Pools | Origins @ Mid | Lookahead |
|---------|----------|----------|---------------|-----------|
| W1_trend | 2026-07-25 … 07-26 (24h) | 125 | 98 | 0 |
| W2_flat | 2026-08-10 … 08-11 (24h) | 128 | 120 | 0 |
| W3_recent | 2026-08-14 … 08-15 (30h) | 145 | 125 | 0 |

- `membership_slot_minutes`: **60**
- `known_at` / Segmente: **kein** Segmentbeginn nach `to_utc`
- D2-Vorbedingung: 122 Pools, 122 Segmente, `bad_segments=0`

**Depth-Slider / Canvas-Density:** nur im Browser mit Login testbar.

---

## 4. Performance (begrenztes Fenster 6h)

| Metrik | 1h on-demand | 5m frozen |
|--------|--------------|-----------|
| Erster Request (Wall) | **0,78 s** | **7,08 s** |
| Cache-Hit (Wall) | **0,001 s** | **0,003 s** |
| Response-Größe | **~45 KB** | **~126 KB** |
| Prozess RSS max (Lauf) | **~975 MB** (inkl. PD1-Warmup/Engine) | — |

Wechsel 5m ↔ 1h: getrennte Cache-Keys (bereits in Unit-Tests); hier erneut implizit durch separate Requests bestätigt.

---

## 5. Regression

| Check | Ergebnis |
|-------|----------|
| 5m-PD1 Window (`5mpd1` line, 379 Pools) | OK |
| `drawings.json` SHA unverändert (nur Lesen) | OK |
| L1/P17/Bots/CH-Writes | keine Aktion in diesem Test |

---

## Festgestellte Fehler

- **Keine** API-/Engine-/L lifetime-Fehler in den geprüften Fenstern.
- **Blocker für UI-E2E:** fehlende Dashboard-Session (401).

---

## Offene Risiken

1. Visuelle Density (Farben, Slider, Pan/Zoom) ungetestet im Browser.  
2. Parität vs. frozen Candles nicht für 1h publiziert.  
3. Erster 1h-Request lädt ~150d 1m-Warmup (CH-lastig).

---

## Manuelle UI-Checkliste (nach Login)

1. `/live-charts/research` → **B – Exact Origin-Density**  
2. **Pool-TF 1h**, PD1, DOGE-Pane (Kerzen z. B. 5m) → Density sichtbar, Netzwerk: `POST /live-charts/viewer/api/window` mit `"timeframe":"1h"`  
3. Pool-TF 5m → `"timeframe":"5m"`, bestehende Density  
4. Kerzen-TF wechseln (5m ↔ 1h) bei festem Pool-TF 1h → Pools unverändert im Scope  
5. Depth-Slider 1–10 → Bandfilter reagiert  

---

## Rohdaten

Validierungslauf: automatisiert via `get_landscape_window_response` (2026-11-10); `verdict: PASS`, `failures: []`.
