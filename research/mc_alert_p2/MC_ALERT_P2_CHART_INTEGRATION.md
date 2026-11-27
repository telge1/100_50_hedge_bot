# MC-ALERT-P2 — Chart Integrated Price Alert Implementation

**Endstatus:** `MC_ALERT_P2_IMPLEMENTATION_READY_BROWSER_PENDING`  
**Datum:** 2026-11-27  
**Arbeitskopie:** `spread_recovery_hedge_short_dev` (lokal, **kein** Commit/Push/Deployment/Restart)  
**Vorgänger:** [MC_ALERT_P1](../mc_alert_p1/MC_ALERT_P1_ARCHITECTURE_AUDIT.md), [MC_ALERT_P2A](../mc_alert_p2a/MC_ALERT_P2A_WEBSOCKET_PRICE_REUSE.md)

---

## 1. Geänderte / neue Dateien

| Datei | Änderung |
|-------|----------|
| `dashboard/price_alert_store.py` | **neu** — State, Keys, fired JSONL, Trigger-Math |
| `dashboard/price_alert_monitor.py` | **neu** — zentraler Fanout/forming/ticker Monitor |
| `dashboard/app.py` | Start/Stop/Events API; Monitor default; Referenzpreis |
| `dashboard/static/research_trp/chart.js` | Context-Menü, `createPriceLine` Alerts |
| `dashboard/static/research_trp/style.css` | Context-Menü Styles |
| `dashboard/static/js/research/price_alert_chart.js` | **neu** — Host, Leader, Linien-Sync |
| `dashboard/static/js/research/research_charts.js` | Bridge, `MCPriceAlert.initHost` |
| `dashboard/templates/research_charts.html` | Script-Tags |
| `dashboard/static/css/research.css` | Banner + Sound-Button |
| `dashboard/tests/test_price_alert_store.py` | **neu** — 4 Unit-Tests |

**Unverändert bewusst:** Pool/PD1/D2/OB/Trading/Drawings/Long-Short-Kernlogik.

---

## 2. Wiederverwendete Originalfunktionen

- `candleSeries.coordinateToPrice` / `formatCopiedPrice` (`chart.js`)
- `createPriceLine` / `removePriceLine`
- `paneSymbol(pane)` (`research_charts.js`)
- `POST/GET /api/price-alert/*` (erweitert, rückwärtskompatibel)
- `send_ntfy_alert`, `play_alert_sound`
- Collector `fetch_forming_candle` für Referenzpreis beim Start
- Collector **trade fanout** (`poll_trade_events`)

---

## 3. Alert-Lifecycle

1. **Rechtsklick** im Kerzenbereich → Menü (erstellen / kopieren / löschen nahe Linie).
2. Host ermittelt **nur Pane-Symbol** (`paneSymbol`); Multi ohne globales Fallback.
3. `POST /api/price-alert/start` (admin): Server holt Referenz (forming → Bybit), leitet `trigger` ab, speichert `engine=monitor_v1`, startet Monitor.
4. **Linien** nach erfolgreichem Start via `GET /status` (periodisch, 5 s, **ein** Poll pro Tab).
5. Monitor feuert → ntfy + Server-Ton + `price_alert_fired.jsonl` + Key aus State.
6. Browser Leader spielt WAV + Banner; Linien-Refresh entfernt ausgelösten Alert.
7. **Stop:** `POST /api/price-alert/stop` mit `alert_key` oder exakt symbol/target/trigger.

---

## 4. Multi-Tab-Konzept

- `localStorage` Leader-Lock (`mc_price_alert_leader_v1`, TTL 4 s).
- `BroadcastChannel('mc_price_alert_v1')` für Banner-Sync.
- `GET /api/price-alert/events?since=` für idempotente Trigger (`event_id` in `sessionStorage`).
- **Kein** Pane-Polling; Leader-Wechsel bei Tab-Close via TTL.

**Grenzen:** Zwei Browser-Profile oder zwei Geräte → getrennte Leader → bis zu zwei lokale Töne; ntfy bleibt einmalig serverseitig.

---

## 5. Testergebnisse (lokal)

| Test | Ergebnis |
|------|----------|
| `pytest tests/test_price_alert_store.py` | **4 passed** |
| `py_compile` store + monitor | OK |
| Browser E2E (8 Panes, 2 Tabs, Audio) | **Ausstehend** (`BROWSER_PENDING`) |

---

## 6. Performance

- +1 Fanout-Poll / ~350 ms (**global**, nicht pro Alert).
- +1 Status/Events HTTP / 5 s **pro Tab** (nicht pro Pane).
- Keine Änderung an forming 250 ms Chart-Poll.
- Keine zusätzlichen CH-Queries im Monitor.

---

## 7. Einschränkungen

1. **Legacy `price_alert.py` (10 s)** nur mit `PRICE_ALERT_LEGACY_WORKER=1`; Default = Monitor.
2. QNT ohne Public Trades: etwas träger (forming/ticker).
3. Extrem kurze Spikes **ohne** Trade in Fanout und **ohne** forming high/low noch möglich.
4. **Admin-only** Start/Stop unverändert.
5. Statische JS live ohne Restart — nur isoliert testen (Worktree/Dev).

---

## 8. Browser-Testplan

1. Single + Multi: Rechtsklick-Preis vs. Crosshair.
2. Vier Panes: je Symbol eigener Alert.
3. Oben/unten Trigger vs. Live-Referenz.
4. Zwei Tabs: ein Ton, beide Banner, Linien sync.
5. Reload / TF-Wechsel / Symbolwechsel: keine falschen Linien.
6. BTCUSDT Alert (ohne Collector ensure).
7. `/price-alert` Seite weiterhin Start/Stop.
8. Regression: Shift-Messung, Drawings, Pools.

---

## 9. Aktivierung lokal

```bash
# Optional Legacy-Worker:
# export PRICE_ALERT_LEGACY_WORKER=1

cd spread_recovery_hedge_short_dev/dashboard
# Dashboard wie üblich starten (kein Produktiv-Restart laut Auftrag)
```

Ton: einmal „🔊 Alert-Ton“ in Research klicken (Autoplay).

---

**MC_ALERT_P2_IMPLEMENTATION_READY_BROWSER_PENDING**