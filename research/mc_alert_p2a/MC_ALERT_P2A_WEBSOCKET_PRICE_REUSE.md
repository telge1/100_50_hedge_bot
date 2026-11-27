# MC-ALERT-P2A — Existing WebSocket Live Price Reuse (READ-ONLY)

**Endstatus:** `MC_ALERT_P2A_ARCHITECTURE_READY`  
**Datum:** 2026-11-27  
**SSOT:** `spread_recovery_hedge_short_dev` + Signal-Generator Collector (`public_trades_ema_fanout_integration_v1`)  
**Baseline:** `stable-mc-pool-p9-20261126`

---

## 0. Executive Summary

| Quelle | Port | WS-Inhalt | Dashboard-Zugriff |
|--------|------|-----------|-------------------|
| **Bybit Live 1m + Public Trades** | **8787** | Kline 1m + `publicTrade.{SYMBOL}` | `STOCH_COLLECTOR_API_BASE` → `collector_control.py` |
| **QNT-only 1m** | **8789** | Kline 1m (keine Public Trades laut MC7F-Handoff) | `QNT_COLLECTOR_API_BASE` (neu in Monitor) |
| **Research forming** | — | Aggregiert aus Collector/CH | `GET /api/research/forming-bar` |
| **Browser forming poll** | — | 250 ms | `research_charts.js` only |

**Empfehlung P2/P3:** Ein **zentraler Dashboard-Monitor** (`price_alert_monitor.py`) hängt an **`/api/trade_fanout/poll_trade_events`** (bestehende Bybit-WS-Pipeline, **keine zweite WS**) und ergänzt **forming/ticker-Fallback** für QNT/BTC.

---

## 1. Kanonische Live-Preis-Infrastruktur

### 1.1 Collector Control API (:8787 Default)

| Endpoint | Datei | Zweck |
|----------|-------|-------|
| `GET /api/collector/status` | `signal_generator/.../control_api.py` | WS connected, Symbol-Health, public_trade_metrics |
| `GET /api/collector/forming?symbol=` | `HealthState.forming_payload` | Letzte forming-1m-OHLC pro Symbol |
| `POST /api/trade_fanout/create_trade_subscriber` | `PublicTradeEventFanout` | Subscriber ohne neue Bybit-WS |
| `POST /api/trade_fanout/poll_trade_events` | fanout | Batch Trade-Events (price = **Last trade**) |

**Dashboard-Proxy:** `dashboard/app.py` → `_proxy_collector_api`, `research_charts/collector_control.py` → `fetch_forming_candle()`.

**Call-Chain Research-Live (UI only):**

```
research_charts.js pollForming (250 ms)
  → GET /api/research/forming-bar
    → fetch_forming_candle(symbol)
      → GET :8787/api/collector/forming
      → fallback forming_bar_from_clickhouse()
  → chart.updateFormingBar()
```

### 1.2 WebSocket-Pfade im Collector

| Modul | Pfad | Daten |
|-------|------|-------|
| `ws_kline.py` | Bybit kline 1m | Candle close/high/low Updates |
| `ws_public_trade.py` | `publicTrade.SYMBOL` | **Last trade price**, trade_id, side |
| `public_trade_event_fanout.py` | Hook nach WS-Normalisierung | Bounded queues, dedupe `symbol+trade_id` |

**Preissemantik:** Alerts nutzen **Last Trade Price** (nicht Mark/Index) — konsistent mit Chart-„Live“-Close aus forming (aggregiert aus Trades/Candles).

### 1.3 QNT Collector (:8789)

Quelle: `research/mc7f_zec_qnt_live_handoff.md` — dedizierter Writer, `candle_symbols=['QNTUSDT']`, `public_trade_symbols=[]`.  
→ Preisalarme für QNT: **forming high/low/close** oder Bybit-Ticker-Fallback, **nicht** 8787-Fanout.

### 1.4 BTCUSDT

- **Trading/Collector ensure:** `app.py` `btc_rejected` für `POST /api/collector/ensure_symbol`.
- **Preis:** Bybit linear ticker + optional manueller Fanout nur wenn BTC auf einem Collector läuft; Monitor nutzt **`bybit_ticker_fallback`** für BTC.

### 1.5 Symbolabdeckung

| Symbol-Typ | Fanout 8787 | Forming | Ticker-Fallback |
|------------|-------------|---------|-----------------|
| 51-Coin-Universum | Ja | Ja | Bei Fanout-Ausfall |
| QNTUSDT | Nein (8789) | Ja (:8789) | Ja |
| BTCUSDT | Typisch nein | CH fallback | Ja |
| ZECUSDT | Ja (51er) | Ja | — |

---

## 2. Aktualisierungsfrequenz und Touch-Erkennung

| Quelle | Frequenz | Kurze Berührung |
|--------|----------|-----------------|
| publicTrade fanout | Event-getrieben (ms) | **Ja**, solange Trade auf Bybit sichtbar |
| forming API | ~1m bucket + partials | high/low der laufenden Minute hilft Intrabar |
| Legacy `price_alert.py` | **10 s REST** | **Nein** bei Spike-and-revert |
| Browser forming 250 ms | Poll | Nur UI, kein Server-Trigger |

**Reconnect:** Collector setzt `forming_by_symbol` zurück bei `note_disconnected`; Dashboard hält 15 s forming hold (`_FORMING_HOLD_S`).

---

## 3. Zentraler Alert-Monitor (P2-Implementierung)

**Modul:** `dashboard/price_alert_monitor.py`

| Regel | Umsetzung |
|-------|-----------|
| Ein Prozess / Thread | Daemon-Thread im Dashboard |
| Keine WS pro Alert | Ein `trade_subscriber` auf bestehendem Fanout |
| Kein REST pro Alert | Ein Fanout-Poll (~350 ms) für alle Symbole |
| Trigger | `crossed_level` + Last-Trade-Preis |
| Notify | bestehende `send_ntfy_alert` + `play_alert_sound` |
| Persistenz | `price_alert.json` + `price_alert_fired.jsonl` |

**Migration:** Default `engine=monitor_v1`; Legacy-Subprozesse nur mit `PRICE_ALERT_LEGACY_WORKER=1`.

---

## 4. forming-bar 250 ms — Eignung

| Kriterium | Bewertung |
|-----------|-----------|
| Preisaktualität | Gut für **Chart-Anzeige** |
| Symbolabdeckung | Nur sichtbare Multi-Pane-Symbole + Auth |
| Server-Trigger | **Ungeeignet** (N×HTTP, tab-hidden pause, kein zentraler State) |
| high/low | Nutzbar im Monitor via forming **wenn** nicht als Pre-Alert-History missbraucht (nur Updates nach `started_at_unix`) |

---

## 5. Multi-Tab

Server-Monitor **tab-unabhängig**. Browser: Leader + `BroadcastChannel` + `GET /events` (`price_alert_chart.js`).

**Audio-Deduplizierung:** Nur Leader spielt WAV; andere Tabs zeigen Banner. Grenze: **zwei Browser-Profile** = zwei Leader → zwei Töne (dokumentiert).

---

## 6. Schutz / Collector-Stabilität

Monitor: read-only Fanout-Poll + kurze GET forming; **kein** `ensure_symbol`, kein Collector-Restart, kein zusätzlicher Bybit-WS.

---

## 7. P3-Ausblick

- Fanout-Subscriber-Symbolfilter auf aktive Alert-Symbole (weniger Queue-Druck).
- Optional Kline-WS-Deltas aus Health für forming-only-Symbole ohne Trades.
- Kein Ersatz der gesamten `price_alert.py`-CLI ohne Migrationsfenster.

---

**MC_ALERT_P2A_ARCHITECTURE_READY**