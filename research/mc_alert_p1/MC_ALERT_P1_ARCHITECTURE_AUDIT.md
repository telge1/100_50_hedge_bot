# MC-ALERT-P1 — Chart Integrated Price Alert Architecture Audit (READ-ONLY)

**Endstatus:** `MC_ALERT_P1_ARCHITECTURE_AUDIT_COMPLETE`  
**Datum:** 2026-11-27  
**Auftrag:** Integration von Preisalarmen direkt in Single- und Multi-Chart 2×2 unter Wiederverwendung des bestehenden Systems (`/price-alert`).  
**SSOT (Laufzeitcode):** `spread_recovery_hedge_short_dev` — `dashboard/`, `scripts/price_alert.py`  
**Baseline (Schutz):** Git-Tag `stable-mc-pool-p9-20261126` (Spread-Repo; kein Code geändert in diesem Audit)  
**Deployment-URL (Nutzer):** `http://dash.immotel.de:8080/price-alert` — gleiche Routen wie lokales Dashboard; Port/Reverse-Proxy ist Betriebsdetail, nicht im Quellcode verankert.

**Nicht in diesem Audit:** Implementierung, Service-Restarts, Deployment, Commits, Push.

---

## 0. Executive Summary

| Bereich | Befund |
|---------|--------|
| **Kanonisches Preisalarm-System** | Dedizierte Seite + REST-API in `dashboard/app.py`; **ein OS-Subprozess pro Alert** (`scripts/price_alert.py`); Persistenz nur für **aktive** Alerts in `data/state/price_alert.json`. |
| **Trigger / Preisquelle (Worker)** | Bybit `/v5/market/tickers` (linear), Poll **10 s** (Default); einmalige Auslösung, danach Prozessende + State-Eintrag entfernt. |
| **Benachrichtigungen** | Worker: **ntfy** + **Server-Ton** (`paplay`) via `dashboard/utils/notifications.py`. Browser-Ton nur auf **`price_alert.html`** via Status-Polling (5 s). |
| **Research-Charts** | Host `research_charts.js` + TRP-iframe `chart.js`; Rechtsklick = **Preis kopieren** (`coordinateToPrice`), kein Menü. Live-Preis im Chart: **forming-bar** alle **250 ms** (pro sichtbarem Symbol in Multi). |
| **Lücke** | Keine Chart-Linien, kein Context-Menü „Preisalarm erstellen“, kein Status-Sync in Research; **Admin-only** auf `start`/`stop`; Worker-Poll langsamer als Chart-Live; Multi-Tab **doppelter Browser-Ton** möglich. |
| **Empfehlung** | **Keine parallele Engine.** Phase 1: Context-Menü + API-Anbindung + `createPriceLine`-Overlays + zentraler **ein** Status-Listener pro Browser-Profil; optional Phase 2: **ein** konsolidierter Monitor-Prozess statt N× Subprozesse (gleiche Trigger-/Notify-Logik). |

---

## 1. Bestehende Preisalarm-Architektur

### 1.1 Routing und Frontend

| Pfad | Datei | Funktion |
|------|-------|----------|
| `GET /price-alert` | `dashboard/app.py` (~3424) | `price_alert_page` → Template |
| Template | `dashboard/templates/price_alert.html` | Formular, aktive Alerts, Polling |
| Navigation | `dashboard/templates/partials/nav.html` | Link „Price Alert“ |
| Audio-Asset | `/static/sounds/price_alert.wav` | Browser-Wiedergabe (inline JS) |

**Frontend-Verhalten (`price_alert.html`):**

- `GET /api/price-alert/current-price?symbol=` alle **5 s** (Anzeige).
- `GET /api/price-alert/status` alle **5 s** (`ALERTS_REFRESH_INTERVAL_MS`).
- `POST /api/price-alert/start` pro Zeile („Aktivieren“); Body: `{ symbol, target_price, trigger }` mit `trigger ∈ { above, below }`.
- `POST /api/price-alert/stop` („Stop“); setzt `manuallyStoppedKeys` damit kein Falsch-Trigger-Ton.
- **Browser-Ton bei Auslösung:** Wenn ein Key aus `knownAlertKeys` verschwindet und nicht in `manuallyStoppedKeys` → `playLaptopAlertSound(3)` (nur auf dieser Seite).
- Kein WebSocket/SSE; rein **HTTP-Polling**.

### 1.2 Backend-API (FastAPI)

Alle Endpunkte in `dashboard/app.py` ab Zeile ~4683; Auth: `require_auth` (Session).

| Methode | Route | Zweck |
|---------|-------|--------|
| GET | `/api/price-alert/current-price?symbol=` | Bybit-Ticker (Hilfsfunktion `_fetch_bybit_ticker_price`) |
| POST | `/api/price-alert/start` | Subprozess starten + State schreiben (**`role == admin`**) |
| GET | `/api/price-alert/status` | Komplettes State-Objekt `{ alerts: { key: entry } }` |
| POST | `/api/price-alert/stop` | `SIGTERM` an `pid`, Key aus State (**admin**) |
| POST | `/api/price-alert/test-sound` | Server-`paplay` (nicht identisch mit Browser-WAV) |

**State-Datei:** `project_root / data/state / price_alert.json` (`_PRICE_ALERT_STATE_FILE`).

**Alert-Key (kanonisch):** `_price_alert_key(symbol, target_price, trigger)` →  
`{SYMBOL}_{target:.6f}_{above|below}` (Python & Script identisch).

**State-Eintrag (pro aktivem Alert):**

```json
{
  "symbol": "TONUSDT",
  "pid": 12345,
  "target_price": 7.5,
  "trigger": "above",
  "started_at": "2026-11-27T11:00:00"
}
```

**Start-Call-Chain:**

```
price_alert.html activateRow()
  → POST /api/price-alert/start
    → api_price_alert_start (admin check)
    → _load_price_alert_state(); duplicate key → 409-like JSON error
    → subprocess.Popen([python, scripts/price_alert.py, --symbol, --target-price, --trigger])
    → state[alert_key] = { symbol, pid, target_price, trigger, started_at }
    → write price_alert.json
```

**Stop-Call-Chain:**

```
price_alert.html stopAlert()
  → POST /api/price-alert/stop
    → os.kill(pid, SIGTERM)
    → pop alert_key; rewrite or unlink price_alert.json
```

**Hinweis:** Es gibt **kein** separates „deaktivieren“ ohne Löschen — Stop = beenden. Kein Archiv / keine DB für erledigte Alerts.

### 1.3 Worker: `scripts/price_alert.py`

| Aspekt | Detail |
|--------|--------|
| Preisquelle | `GET https://api.bybit.com/v5/market/tickers?category=linear&symbol=...` |
| Poll | `--poll-interval` Default **10.0 s** (min 1 s) |
| Trigger | `above`: `current >= target`; `below`: `current <= target` |
| Einmaligkeit | Bei Treffer: `send_alert()` → `cleanup()` → `_remove_state_entry` → `break` |
| Signale | `SIGTERM`/`SIGINT` → `cleanup()` entfernt State-Eintrag |
| Notify | `send_ntfy_alert` + `play_alert_sound(repeats=2)` aus `dashboard.utils.notifications` |

**Mechanismus gegen doppelte Auslösung (Server):**

- Ein Prozess pro Key; nach Auslösung Prozess beendet, Key aus JSON.
- API verweigert zweiten Start mit gleichem Key (`bereits aktiv`).
- Kein Cross-Process-Lock über mehrere Keys desselben Symbols (mehrere Alerts = mehrere Prozesse).

**Verhalten bei Dashboard-Neustart:**

- `price_alert.json` bleibt; **Kinderprozesse laufen weiter** (Popen ohne Parent-Anbindung).
- **Keine** PID-Validierung in `GET /status` — tote PIDs können angezeigt werden, bis Stop oder manuelles Aufräumen.
- Nach Auslösung entfernt der Worker den Key; Browser erkennt das via Polling.

**BTCUSDT:**

- Price-Alert-API **filtert BTC nicht**; Bybit linear ticker funktioniert für BTCUSDT.
- Trading/Collector: `POST /api/collector/ensure_symbol` lehnt `BTCUSDT` ab (`btc_rejected`, `app.py` ~4674) — **unabhängig** vom Preisalarm.

### 1.4 Benachrichtigungen

| Kanal | Wo | Technik |
|-------|-----|---------|
| ntfy | Worker | `send_ntfy_alert` — Topic aus `config/config.yaml` |
| Server-Laptop-Ton | Worker, `test-sound` | `play_alert_sound` → `paplay` auf freedesktop-Sounds |
| Browser-Ton | `price_alert.html` | `new Audio('/static/sounds/price_alert.wav')` — **Autoplay-Policy**: erfordert Nutzerinteraktion auf der Seite (Test-Button trainiert Kontext) |
| Dashboard-Header | `price_alert.html` | `POST /api/alerts/test` (allgemeiner Alert-Test, separat) |

**Hintergrund-Tab:** Research-Charts pausiert `pollForming`/`pollIncremental` wenn `document.hidden` — betrifft **nur Chart-Live**, nicht Server-Worker.

### 1.5 CRUD-Abdeckung vs. Nutzerwunsch

| Operation | Vorhanden |
|-----------|-----------|
| Erstellen | `POST /start` |
| Bearbeiten | **Nein** (nur Stop + neuer Start) |
| Löschen / Stop | `POST /stop` |
| Deaktivieren (persistiert) | **Nein** |
| Liste aktiv | `GET /status` |

---

## 2. Research-Chart-Architektur (Integrationsziele)

### 2.1 Seiten und Host

| Komponente | Pfad |
|----------|------|
| Route | `GET /live-charts/research` (`research_charts/api.py` ~144); Multi: Redirect `?view=multi` |
| Host-JS | `dashboard/static/js/research/research_charts.js` |
| Template | `dashboard/templates/research_charts.html` |
| TRP-Pane | iframe `/static/research_trp/pane.html` → `chart.js` |

**Multi 2×2:** `isMultiManualMode()` (`state.multiManual`); Symbole pro Kachel via `pane.symbol` / Select; `paneSymbol(pane)` (~309):

- Multi: `pane.symbol` (uppercase)
- Single: `state.symbol`

### 2.2 Parent ↔ Iframe Bridge

`attachBridge(pane)` setzt `win.bridge` (`research_charts.js` ~2862):

- `on_chart_ready`, `on_crosshair_move`, `on_chart_click`, `on_visible_range`, …
- `on_drawing_event` → `handleDrawingEvent` (Long/Short-Boxen, Workspace-Zeichnungen)
- **Kein** Callback für Context-Menü / Preisalarm heute.

**Kanonische Chart-API:** Objekt aus `chart.js` (exportiert ans iframe `contentWindow`), konsumiert via `api(pane)` im Host.

### 2.3 Rechtsklick und Preis aus Koordinaten

**Registrierung:** `chart.js` ~1011 — `contextmenu` auf Chart-Element und `pricePane`.

**Handler:** `onChartContextMenu` (~6310):

```
ev.preventDefault/stopPropagation
→ xyFromEvent(ev) + chartSize() (Bounds-Check)
→ priceAtEvent(ev):
     candleSeries.coordinateToPrice(xy.y)  // echte Y-Koordinate, nicht Kerzen-Close
     Fallback: lastCursorPrice
→ formatCopiedPrice(price)  // lastPriceFormat.precision (inferPriceFormat aus Candles)
→ copyText + Toast „Preis kopiert“
```

**Erfüllung Nutzeranforderung (Preisermittlung):** Mechanismus **bereits vorhanden**; nur UX muss von Copy auf Menü erweitert werden.

### 2.4 Preispräzision / Tick-Size

| Quelle | Verhalten |
|--------|-----------|
| Chart | `inferPriceFormat(candles)` + `lastPriceFormat` (`chart.js` ~667–690); Copy/Anzeige über `formatCopiedPrice` |
| Backend (Bubbles) | `research_charts/trade_bubbles.py::tick_size(symbol)` — Referenz für Orderflow, **nicht** automatisch an Chart gebunden |

**Empfehlung:** Alert-Preis für API aus `formatCopiedPrice`/`coordinateToPrice`-Wert; optional später Bybit `priceFilter.tickSize` cachen — **nicht** für P1 zwingend.

### 2.5 Horizontale Linien (wiederverwendbar)

Lightweight Charts **`candleSeries.createPriceLine`** bereits genutzt für:

| Linie | Stil (Referenz) |
|-------|------------------|
| Live-Preis | `SparseDotted`, `#f0b429`, `ensureLivePriceLine` (~1201) |
| Wall breakpoint | `setWallBreakpoint` (~1363) |
| Wall target guides | gelb/cyan, `Dashed`/`Solid` (~1275) |

**Gewünschte Alert-Linie:** gelb, gestrichelt, Titel/Label „🔔“ + Preis — **`createPriceLine`** mit `LineStyle.Dashed`, `axisLabelVisible: true`, `title: "🔔 124.50"`.

**Kein zusätzliches Canvas** nötig, sofern Interaktion (Löschen) über Host-Menü oder kleines HTML-Overlay an Y-Position, nicht über native Price-Line-Klicks (LWC hat keine Price-Line-Hit-Tests).

### 2.6 Live-Preis im Chart (für Richtungs-Inferenz)

**Call-Chain Live:**

```
startPoll() → formingTimer @ FORMING_MS (250)
  → pollForming()
    → visiblePaneSymbolsUnique() in Multi
    → GET /api/research/forming-bar?symbol=
         ← fetch_forming_candle() (collector → CH fallback)
    → applyFormingToPane()
         → chart.updateFormingBar(bar) → candleSeries.update + ensureLivePriceLine(close)
```

**Collector-Hinweis:** `forming_from_ch.py` / `collector_control.py` — CH-Fallback wenn Collector forming null.

**Separat:** Chart pollt Kerzen inkrementell (`POLL_MS`, `pollIncremental`); forming ist **schneller** als der Price-Alert-Worker.

**Für Auto-Trigger-Richtung beim Rechtsklick:**

- `target > liveClose` → `trigger = "above"`
- `target < liveClose` → `trigger = "below"`
- Live: letzter `forming.close` aus Pane oder `GET /api/price-alert/current-price` (Bybit, konsistent mit Worker).

### 2.7 Zeichnungen / Konflikte

| Feature | Mechanismus | Konflikt mit Alert |
|---------|-------------|-------------------|
| Long/Short-Box | Workspace tools `long_position`/`short_position`; Multi blockiert (`MULTI_BLOCK_POSITION_TOOLS`) | Kein gemeinsamer State; Alert = PriceLine, keine Order |
| hline/vline | `ONE_POINT_TOOLS`; `on_drawing_event` → `/api/research/drawings/*` | Orthogonal zu PriceLines |
| PTB-Overlay, EOD density, OB | Canvas/DOM | Z-Index: PriceLines in Chart-Serie; Context-Menü auf `#chart` — **nicht** auf reinen Canvas-Overlays |
| Shift-Messung | Shift+Drag, `onShiftMeasureDown` | Rechtsklick bleibt separat |
| Wall BP / Target guides | `setWallBreakpoint`, `setWallTargetGuides` | Visuell ähnlich; Farben unterscheidbar (Alert = einheitlich gelb gestrichelt) |

**Trading:** Kein Codepfad verbindet `price_alert` mit Order-API — **keine Orders** bei Auslösung (Anforderung erfüllt im Ist-Zustand).

---

## 3. Soll-Bedienung vs. Ist-Zustand

| Anforderung | Ist | Gap |
|-------------|-----|-----|
| Context-Menü „Preisalarm bei X erstellen“ | Copy-Only | Menü + Bridge-Callback |
| Symbol nur aus Kachel | `paneSymbol` existiert | Host muss Symbol an Start-API binden |
| Preis aus Y-Koordinate | `coordinateToPrice` | — |
| Gelbe gestrichelte Linie + Glocke + Label | — | `createPriceLine` + Sync aus `GET /status` |
| Timeframe-übergreifend pro Symbol | — | Alerts sind symbolbasiert; Linien bei jedem Pane mit gleichem `paneSymbol` spiegeln |
| Löschen/Deaktivieren an Linie | — | UI → `POST /stop` (admin!) |
| Trigger von unten/oben | Manuell `above`/`below` | Auto aus Live vs. Target |
| Übersprungene Levels | 10 s Poll | Intrabar-Spike ohne Hold kann **verpasst** werden; schnelleres Poll oder CH/Collector-Feed im Worker |
| Kein Warten auf 5m-Close | Worker nutzt Ticker | OK für Worker; Chart nicht für Trigger zuständig |
| Mehrere Alerts pro Symbol | Ja (verschiedene Keys) | OK |
| BTCUSDT | API OK | Chart/Collector evtl. ohne CH-Daten — Alert trotzdem per Bybit |

---

## 4. Multi-Tab- und Multi-Monitor-Szenario

**Nutzer:** zwei Dashboard-Tabs (z. B. zwei× Multi 2×2 auf zwei Monitoren), plus optional `/price-alert`.

| Risiko | Ursache | Mitigation (Empfehlung) |
|--------|---------|-------------------------|
| Doppelter **Browser-Ton** | Jede `price_alert.html` + ggf. zukünftiger Research-Listener pollt `status` und spielt bei Key-Verschwinden | **Ein** Tab-Leiter pro Origin: `localStorage` Lock + `BroadcastChannel('price-alert-fired')` — nur Leader spielt Ton; alle zeigen Toast |
| Doppelter **Server-Ton/ntfy** | Ein Worker pro Key | Bereits sicher |
| Doppelter **Start** | Zwei Tabs klicken gleichzeitig | API-Duplikat-Check auf Key |
| Viele Worker (8 Coins × mehrere Levels) | 1 Prozess/Alert, je 10 s Bybit | Phase 2: **ein** `price_alert_daemon.py` mit Set-Monitoring |
| Alerts ohne sichtbares Chart | Worker unabhängig vom UI | Ist-Zustand OK |
| Reload | State in JSON; Worker läuft | Nach Reload Linien aus `GET /status` neu zeichnen |
| Zwei Browser-**Profile** | Getrennte Sessions | Server sendet ntfy einmal; Browser-Ton pro Profil |

**Research-Charts heute:** Kein `price-alert/status` Poll — **kein** Ton auf Research-Seite bei Auslösung (nur ntfy/Server wenn Worker läuft).

---

## 5. Wiederverwendbare Komponenten

| Komponente | Wiederverwendung |
|------------|------------------|
| `POST /api/price-alert/start` | Direkt (nach Admin-Entscheidung) |
| `GET /api/price-alert/status` | Linien-Sync + globaler Listener |
| `POST /api/price-alert/stop` | Linie entfernen |
| `scripts/price_alert.py` | Unverändert in P1 |
| `send_ntfy_alert` / `play_alert_sound` | Unverändert |
| `coordinateToPrice` + `formatCopiedPrice` | Context-Menü-Preis |
| `createPriceLine` / `removePriceLine` | Alert-Darstellung |
| `paneSymbol` | Symbol-Zuordnung |
| forming-bar / Bybit current-price | Trigger-Richtung + UX-Bestätigung |

**Nicht wiederverwenden als Trigger-Engine:** forming-bar-Poll im Browser (nur Anzeige; Tabs können hidden sein).

---

## 6. Empfohlene minimale Chart-Integration (Design only)

### 6.1 Phase P1a — UX + API (kein neuer Worker)

1. **`chart.js`:** `onChartContextMenu` → kleines fest positioniertes Menü bei `(clientX, clientY)`:
   - „Preisalarm bei {formatCopiedPrice(price)} erstellen“
   - Optional: „Preis kopieren“ (bestehendes Verhalten)
2. **Neuer Bridge-Hook:** `on_price_alert_request({ price, clientX, clientY })` oder JSON-string wie `on_drawing_event`.
3. **`research_charts.js`:**
   - Infer `trigger` aus `price` vs. live (`forming` oder einmalig `current-price`).
   - `POST /api/price-alert/start` mit `paneSymbol(pane)`.
   - Bei Erfolg: lokale Map `alertPriceLines[paneId][key]` → `createPriceLine`.
4. **Periodisch (niedrige Frequenz):** `GET /status` alle 5–10 s **einmal pro Tab** (nicht pro Pane) → Linien sync + Auslösung erkennen.
5. **Admin-Gate:** Entweder Chart nur für Admin (wie heute) **oder** API auf `require_auth` ohne Admin für Start/Stop öffnen (Policy-Entscheidung).

### 6.2 Phase P1b — Multi-Tab-Sound + sichtbare Notification

- Gemeinsames Modul `price_alert_client.js` (Host + optional `price_alert.html`):
  - Leader-Wahl, `BroadcastChannel`, einmaliger WAV-Play.
  - Sichtbare Notification: bestehendes `setStatus` / kleines Toast mit Symbol, Ziel, Auslösezeit (wenn Worker Metadaten erweitert — **optional**, heute nur Key-Verschwinden).

### 6.3 Phase P2 (optional, Performance)

- Ersetze N× `Popen(price_alert.py)` durch **einen** Daemon:
  - Liest `price_alert.json` oder SQLite-Queue.
  - Preis: Bybit-Batch oder bestehende interne Ticker/cache (kein zusätzliches 250 ms HTTP vom Browser).
  - Gleiche `condition_met` + einmalige `send_alert`.
- Reduziert Bybit-Last und Prozess-Overhead bei 8+ parallelen Alerts.

**Explizit ausgeschlossen (Schutzregeln):** Änderungen an Pool-Math, PD1 SSOT, D2 Density, Pool-Cache, Chart-Loading-Pfad, Pan/Zoom, Orderbook, Trading, Long/Short-Box-Logik.

---

## 7. Performance-Auswirkungen (P1a)

| Maßnahme | Zusatzlast |
|----------|------------|
| `GET /status` 1×/5–10 s pro Tab | Gering |
| `POST /start` / `stop` | Event-driven |
| `createPriceLine` pro Alert | Vernachlässigbar vs. bestehende Wall-Lines |
| Bestehende forming-bar 250 ms | **Unverändert** — nicht für Alarm-Trigger verdoppeln |
| Worker 10 s × N Prozesse | **Ist-Last** — bei vielen Alerts P2 erwägen |

Keine neuen hochfrequenten Poll-Schleifen für **Trigger** im Browser einführen.

---

## 8. Mögliche Konflikte und Risiken

1. **Rechtsklick-UX:** Copy-toast entfällt oder wird Menüpunkt — Schulung/Regression.
2. **Admin-only API:** Nicht-Admin kann Research nutzen, aber keine Alerts starten — Chart-Feature wirkungslos ohne Policy-Änderung.
3. **10 s Worker-Poll vs. „übersprungene Levels“:** Extrem kurze Spikes können verpasst werden; Mitigation = kürzeres Poll oder Websocket (größerer Scope).
4. **Key-Rundung `{:.6f}`:** Sehr enge Alerts / Tick-Size — Konsistenz zwischen Chart-Preis und gespeichertem Key prüfen beim Test.
5. **Stale PID** nach Crash: UI zeigt „aktiv“, Worker tot — `status` könnte PID pingen (neues Mini-Feature).
6. **Price-Line Klick-Target:** Löschen am Line-Ende erfordert Kontextmenü an Label oder Liste — kein natives LWC-Edit.
7. **`document.hidden`:** Research pausiert Live-Update — **nicht** Alarm-Auslösung (serverseitig OK).

---

## 9. Test- und Implementierungsplan (nach Audit)

### 9.1 Testplan (manuell + automatisiert wo möglich)

| # | Test |
|---|------|
| T1 | Single-Chart: Rechtsklick auf Kerzenbereich → Preis entspricht Y (Vergleich mit Crosshair-Label). |
| T2 | Multi 2×2: Alert in `pane-2` nur mit Symbol dieser Kachel. |
| T3 | Zwei Timeframes gleiches Symbol: Linie nach Reload/TF-Wechsel sichtbar. |
| T4 | `above`/`below` Auto-Richtung bei Preis über/unter Live. |
| T5 | Auslösung: genau **ein** ntfy; Server-Ton; mit Leader-Pattern genau **ein** Browser-Ton bei 2 Tabs. |
| T6 | Manueller Stop: kein Ton; Linie weg. |
| T7 | Duplikat-Start gleicher Key: API-Fehler. |
| T8 | BTCUSDT Alert ohne Collector ensure. |
| T9 | Regression: Shift-Messung, Long/Short (Single), Pool-Overlays, OB — unverändert. |
| T10 | Admin vs. non-admin Start (Policy). |

### 9.2 Implementierungsreihenfolge (Vorschlag)

1. Policy: Admin-Gate für Chart-Start klären.
2. `chart.js` Context-Menü + Bridge.
3. Host: Start + Linien + `status` Sync.
4. `price_alert_client.js` Leader/BroadcastChannel; optional Research `setStatus` Banner.
5. Dokumentation Nutzer (Ton: Tab fokussiert oder ntfy).
6. Optional P2: konsolidierter Worker.

---

## 10. Datei- und Funktionsindex (Call-Chains Kurzform)

```
/price-alert
  app.py:price_alert_page
  templates/price_alert.html

API
  app.py:api_price_alert_* 
  → _fetch_bybit_ticker_price
  → _load_price_alert_state / _price_alert_key
  → subprocess → scripts/price_alert.py

Worker
  price_alert.py:main → fetch_current_price → condition_met → send_alert → cleanup

Notify
  dashboard/utils/notifications.py:send_ntfy_alert, play_alert_sound

Research
  research_charts/api.py:research_charts_page, api_research_forming_bar
  research_charts/collector_control.py:fetch_forming_candle
  static/js/research/research_charts.js:attachBridge, paneSymbol, pollForming, startPoll
  static/research_trp/chart.js:onChartContextMenu, priceAtEvent, createPriceLine, updateFormingBar
```

---

**MC_ALERT_P1_ARCHITECTURE_AUDIT_COMPLETE**