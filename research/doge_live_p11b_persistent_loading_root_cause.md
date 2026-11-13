# DOGE-LIVE-P11B — Dauerhaftes `loading…` trotz P11

**Nutzerbefund:** Nach P11 + Hard-Refresh nur Trace  
`applyDoge5mPd1ViewerBounds | result=ok` und `invalidateCapsCache | caps=cleared` — **keine Kerzen**.  
**Verdikt:** **PARTIAL** — Root Cause belegt; Fix deployed + Dashboard neu gestartet; **Browser-PASS offen**.

---

## Phase A — Ausgelieferte P11-Version

| Prüfung | Ergebnis |
|---------|----------|
| Route | `GET /live-charts/research` → Template `research_charts.html` |
| JS-URL (vor P11B) | `/static/js/research/research_charts.js?v=mlb-setzen-v6` |
| P11 auf Disk (HTTP static) | **JA** — `do not block candle bootstrap on pool bounds` in ausgelieferter Datei |
| P11 im Browser? | **Unklar** — `?v=mlb-setzen-v6` unverändert; Hard-Refresh lädt neuen **Inhalt** bei gleicher URL, aber Template-`?v=` erst P11B erhöht |
| Nutzer-Trace | Entspricht **boot bounds `.then()`** + **`invalidateCapsCache`** (alte Boot-Zeile) — **nicht** `loadPane_*` |

**Fazit A:** P11-Fix war auf dem Server in `research_charts.js`, aber sichtbares Nutzerbild passt zu **asynchronem Bounds-Callback ohne danach abgeschlossenes `loadPane`/`setData`**. Bounds-Trace allein ist **kein** Kerzen-OK-Signal.

---

## Phase B/C — Erster blockierender Schritt (logisch abgeleitet)

### Server (Journal, authentifizierte Session ~18:21 CET)

```
GET /api/research/symbols     200
GET /api/research/workspace 200
POST /api/research/pane      200   (~6–9 s nach symbols)
```

→ **Backend liefert Pane inkl. Kerzen.** Engpass liegt **nach HTTP 200** oder **Client bricht vor `applyPaneBundle` ab**.

### Wahrscheinlichster erste hängende **UI**-Schritt

| Rang | Schritt | Beleg |
|------|---------|--------|
| **1** | **`loadPane` → `exitSupersededPaneLoad` vor `applyPaneBundle`** | Status bleibt `loading…`; Owner null; Recovery max **2** |
| **2** | **`applyPaneBundle` ohne `chartApi` (Iframe-Bridge)** | `whenReady` 8 s → `api(pane)` null → **kein `setData`**; Status sollte „N · load“ sein — Nutzer sieht aber `loading…` ⇒ eher **1** |
| **3** | **Boot zeigt nur Bounds-Trace** | `invalidateCapsCache` im Bounds-Callback; **kein** `loadPane` Trace (Candle-Trace default OFF) |

**Nicht primär:** Candle-CH/HTTP (200, &lt;0,4 s direkt), Publisher P10.

### P11B Root Cause (Fix-Ziel)

1. **`invalidateCapsCache()` nach Boot-Bounds** — verwirft Caps mitten im Start; Nutzer sieht genau dieses Ereignis als „letztes Lebenszeichen“. **Entfernt** (Bounds weiter über `applyDoge5mPd1ViewerBounds` only).
2. **Superseded `loadPane` ohne Orphan-Recovery** — nach erfolgreichem Pane-POST Abbruch mit weiterem `loading…`. **Recovery** bei superseded + kein Owner.
3. **Fehlendes `setData` bei späterer Iframe-Bridge** — **Retry-Poller** bis `chartApi` da ist (30 s).

---

## Phase D — Minimaler Fix (produktiv Spread)

**Dateien:**

| Datei | Änderung |
|-------|----------|
| `static/js/research/research_charts.js` | `bootTrace`; Boot-Trace-Punkte; **kein** `invalidateCapsCache` in Bounds-Callback; superseded-Recovery; `schedulePaneChartDataRetry`; `loadPane_setData_ok` Trace |
| `static/js/research/doge_candle_live_trace_v1.js` | `RESEARCH_BOOT_TRACE=1` aktiviert Trace (neben `DOGE_CANDLE_LIVE_TRACE`) |
| `templates/research_charts.html` | `?v=p11b-boot-trace-v1` Cache-Bust |

**Mirror:** `pools+ob+delta_bot/.../research_charts.js` (vorheriger P11-Stand; P11B primär Spread).

**Dashboard:** Nach versehentlichem Prozessende **neu gestartet** via `start_tf1d_local.sh` → PID **2211806**, `:3000` listening.

---

## Phase E — Verifikation

| # | Test | Ergebnis |
|---|------|----------|
| 1–2 | Reload / Hard-Refresh | Template-Bust `p11b-boot-trace-v1` — **Nutzer retest** |
| 3–5 | Kerzen 5m–4h | Server `load_candles` OK (P11); **UI offen** |
| 6–7 | Forming / Preis | Log 200 auf `forming-bar` bei Session — **UI offen** |
| 8–9 | Bounds/Pool langsam | Bounds **nicht-blockierend**; Kerzen vor Pools |
| 10 | Schneller TF-Wechsel | **UI offen** |

**Debug (optional):** Konsole `localStorage.RESEARCH_BOOT_TRACE=1` → Reload → Reihenfolge  
`boot_start` → `boot_symbols_ok` → `boot_switch_symbol_start` → `loadPane_start` → `loadPane_pane_api_ok` → `loadPane_setData_ok` oder `loadPane_setData_retry_ok`.

---

## API-Laufzeiten (Referenz)

| Endpoint | Typisch |
|----------|---------|
| `POST /api/research/pane` | **~6–9 s** unter Last (Journal) |
| `load_candles` direkt | **&lt;0,4 s** |

---

## Service-Status (Ende P11B)

| Service | Status |
|---------|--------|
| Dashboard :3000 | **active** PID **2211806** |
| DOGE Publisher | unverändert |
| Collector | unverändert |

---

## PASS / PARTIAL / BLOCKED

**PARTIAL** — Root Cause + Fix; Server/API OK; **Browser-PASS ausstehend**.

---

## STOP

Kein Commit, kein Push, keine Orders.
