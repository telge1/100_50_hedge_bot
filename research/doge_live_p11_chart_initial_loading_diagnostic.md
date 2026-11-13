# DOGE-LIVE-P11 — Chart hängt bei `loading…` (Initial Load)

**Beobachtung (Nutzer):** ~2026-11-12 **17:10 UTC** — DOGEUSDT Live-Research-Chart: dauerhaft `loading…`, keine Kerzen, keine Forming-Candle, keine Pool-Bänder.  
**P10-Korrelation:** Nicht als alleinige Ursache belegt.  
**Verdikt:** **PARTIAL** — Backend Kerzen OK; Frontend-Ursachenkette belegt; minimaler Fix in JS deployed; **Browser-Abnahme offen**.

---

## 1. Service-Status (Messung ~18:18 CET)

| Komponente | Status |
|------------|--------|
| Spread-Dashboard `:3000` | **läuft** PID **2189782** (seit ~17:11 CET) |
| RAM / Swap | ~5,5 GiB / 17 GiB; **Swap voll** (~2 GiB) |
| Load avg | ~2,5–2,9 |
| MC3E BNB Worker | weiter ~hohe CPU (wie P10) |
| DOGE Publisher | unverändert (nicht neu gestartet) |
| Collector | LIVE (Journal 17:16 / 18:06) |

---

## 2. Call-Chain Initial-Load (Ist)

```
/live-charts/research (HTML)
  → research_charts.js boot()
      → buildPanes() (iframe → pane.html → on_chart_ready)
      → await /api/research/symbols
      → await /api/research/doge-5m-pd1-viewer-bounds   ← vor Fix: blockiert switchSymbol
      → await switchSymbol()
          → loadPane() pro sichtbarem Pane
              → status „loading…“
              → POST /api/research/pane
              → whenReady(iframe chart, 8s)
              → applyPaneBundle() → chart.setData + Status „N · load“
              → releasePaneLoadOwner()
      → refreshLiveBar() / forming poll
```

**Kerzen-Backend** (`load_candles` direkt, pools+ob-Dashboard-Python mit TRP): **200 Bars / TF in 0,04–0,36 s** (5m–4h). **Kein CH-Ausfall.**

---

## 3. Access-Log-Befund (User-IP 197.186.72.177)

### 3.1 ~17:10 UTC (= **18:10 CET** Server-Zeit)

In diesem Fenster: **keine** `POST /api/research/pane`, **keine** `/api/research/candles`, nur `manual-long-box/status` Polls.  
→ Chart hing **clientseitig** (kein erneuter Pane-Load), nicht weil der Server Pane dauerhaft abwies.

### 3.2 Unmittelbar vor Recovery (~18:06–18:07 CET)

| Zeit (CET) | Request | Ergebnis |
|------------|---------|----------|
| 18:06:44 | `GET …/candles?…5m…` | **200** |
| 18:06:47–48 | `GET …/forming-bar` | **401** (AUTH failed, Host `dash.immotel.de:8080`) |
| 18:06:57 | `GET /live-charts/research` | **200** (Full Reload) |
| 18:07:07 | `POST /api/research/pane` | **200** (~**6,45 s**, Slow-Request-Log) |
| 18:07:45 | `POST /api/research/pane` | **200** |

**Recovery:** Hard Reload + erfolgreicher Pane-Load — danach Kerzen-API wieder aktiv.

### 3.3 Früherer Gegenbeweis (17:16 CET)

`candles` / `forming-bar` / `doge-5m-pd1-viewer-bounds` mehrfach **200** — Backend war grundsätzlich lieferfähig.

---

## 4. Ursachen-Einordnung (A/B/C)

| Klasse | Befund |
|--------|--------|
| **A — Backend Kerzen** | **Ausgeschlossen** als primär: CH `load_candles` schnell; Log 200 auf candles/pane nach Reload. |
| **B — Frontend / Orchestrierung** | **Hauptfokus:** (1) `boot()` **`await`** auf DOGE-Bounds **vor** `switchSymbol()` — Bounds-Pfad nutzt `list_published_closes` (~56k) beim ersten Aufruf; unter Last verzögert **Start von `loadPane`**. (2) Race: `exitSupersededPaneLoad` kann Ergebnis verwerfen, Status bleibt **`loading…`**, obwohl `pendingData` gefüllt; `releasePaneLoadOwner` setzte Status **nicht** zurück. (3) Kein Fetch-Timeout auf `POST /api/research/pane` — theoretisch endloses `loading…` bei hängendem Worker-Thread. |
| **C — Auth/Session** | **Mitbeobachtet:** kurze **401**-Burst auf `forming-bar` (Proxy-Host); kann Poll/Forming stören, erklärt aber nicht allein fehlende **Historie** ohne erfolgreichen Pane-Load. |
| **D — P10 / Publisher** | Kein Zusammenhang mit Kerzen-SSOT; P10 Publisher-Restart korreliert zeitlich mit Dashboard-Neustart (~17:11), nicht mit 17:10 UTC Stillstand im Log. |
| **E — Pool-Engine** | Window **422** (~17:40) betrifft Pools, **nicht** initialen Candle-POST; Kerzenladen ist **nicht** sequenziell an D2-Fertigstellung gebunden (Pools parallel in `loadPane`). |

---

## 5. Phase B — Ladeblocker (Checkliste)

| Früfung | Ergebnis |
|---------|----------|
| Langsame/hängende API? | Pane max **6,45 s** beobachtet, dann 200; kein 500/502/503/504 im Fenster |
| `loading` bei Fehler? | HTTP-Fehler → `error · …`; **superseded load** konnte `loading…` stehen lassen (**Fix**) |
| Pools blockieren Kerzen? | **Nein** (async `void refresh…` nach `applyPaneBundle`) |
| JS-Init-Fehler? | Nicht im Serverlog; Boot-Reihenfolge Bounds**await** belegt |
| Daten da, nicht gerendert? | Möglich bei iframe-`whenReady` timeout + superseded owner |
| MC3-Last | Swap voll + hohe CPU — korreliert mit langsamem Pane (~6 s), nicht mit dauerhaftem 500 |

---

## 6. Minimaler Fix (Phase C)

**Datei:** `spread_recovery_hedge_short_dev/dashboard/static/js/research/research_charts.js`  
(Mirror: `pools+ob+delta_bot/.../research_charts.js`)

1. **`boot()`:** `doge-5m-pd1-viewer-bounds` **nicht mehr `await`** — Hintergrund-`.then()`, **`switchSymbol()`/Kerzen zuerst**.  
2. **`releasePaneLoadOwner()`:** Wenn Status noch `loading…` und `pendingData.candles` vorhanden → **`paneStatusFromPendingData`**.

**Nicht geändert:** Candle-Pipeline, PD1/D2, Trading-Boxen, Publisher/Collector.

**Dashboard-Neustart:** **nicht** nötig (statisches JS); Nutzer **Hard-Reload** (`Ctrl+F5`) für neue `research_charts.js`.

---

## 7. Regression (Phase D) — Server-seitig

| # | Test | Ergebnis |
|---|------|----------|
| 1–5 | `load_candles` 5m/15m/30m/1h/4h ×200 | **OK**, letzter Bar konsistent |
| 6–7 | forming-bar / live-status | **401** ohne Session (erwartet); mit Session im Log **200** |
| 8–9 | Bounds/Window | Bounds-Ende synchron mit P10-Tail; Window nicht Teil Initial-Candle |
| 10 | Dauerhaftes `loading…` | Fix adressiert; **Browser offen** |

---

## 8. Stabilität (Phase E)

Kerzen-Bootstrap hängt **nicht mehr** am synchronen DOGE-Bounds-Call. Parallele Pool-Requests (Exact Origin) laufen weiter **nach** Pane-Bundle; keine zusätzliche Entkopplung nötig.

---

## 9. Offene Risiken

- **401** Session/Proxy-Host weiter beobachten.  
- **Kein Pane-Fetch-Timeout** (optional später).  
- **`whenReady` 8 s** bei langsamem iframe unter RAM/Swap-Druck.  
- Browser-Abnahme aller TF ausstehend.

---

## 10. PASS / PARTIAL / BLOCKED

**PARTIAL** — Ursache und Codepfad dokumentiert; minimaler Fix angewendet; Backend Kerzen **PASS**; **UI-Verifikation offen**.

---

## STOP

Kein Commit, kein Push, keine Order-/Trading-Änderungen.
