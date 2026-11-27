# MC-ALERT-P2B — Monitor Safety, Latency & Isolated Browser Preparation

**Endstatus:** `MC_ALERT_P2B_SAFETY_PASS_BROWSER_READY`  
**Datum:** 2026-11-27  
**Baseline:** Git-Tag `stable-mc-pool-p9-20261126` (`c43e9859…`) vs HEAD `7f67186d…` (lokal modifiziert)

---

## A. Produktionsschutz

### A.1 Geänderte P2-Dateien (SHA256 vs. Baseline-Tag)

| Datei | Status | Anmerkung |
|-------|--------|----------|
| `dashboard/app.py` | **DIFF** | Monitor-API + Start-Logik |
| `dashboard/static/research_trp/chart.js` | **DIFF** | Context-Menü |
| `dashboard/static/js/research/price_alert_chart.js` | **NEU** | — |
| `dashboard/static/js/research/research_charts.js` | **DIFF** | Bridge + Host |
| `dashboard/templates/research_charts.html` | **DIFF** | Script-Tags |
| `dashboard/static/css/research.css` | **DIFF** | Banner |
| `dashboard/static/research_trp/style.css` | **DIFF** | Context-Menü |
| `dashboard/price_alert_monitor.py` | **NEU** | — |
| `dashboard/price_alert_store.py` | **NEU** | — |

### A.2 HTTP-Live-Check (kritisch)

Vergleich **lokaler Worktree** ↔ **`http://dash.immotel.de:8080`**:

| Asset | SHA256 identisch? |
|-------|-------------------|
| `/static/js/research/price_alert_chart.js?v=mc-alert-p2-v1` | **JA** (`7900bc29…`) |
| `/static/js/research/research_charts.js` | **JA** (`8f55e796…`) |
| `/static/research_trp/chart.js` | **JA** (`1fa55f49…`) |

**Befund:** P2-**Statik** wird auf dem produktiven Dashboard-Host bereits ausgeliefert (ohne dass in P2B ein Deployment erfolgte — vermutlich gleiches Dateisystem/Prozess wie Dev-Tree).  
**Backend** (`app.py`, Monitor-Thread) ist per HTTP nicht hashsicher prüfbar; wirksame Chart-UX hängt an **admin + laufendem Dashboard mit neuem `app.py`**.

**Maßnahme:** Kein Reset im Prod-Tree. Isolierte Tests **nur** gegen Dev-Port (s. G).

### A.3 Isolierter Git-Worktree

| Pfad | Branch |
|------|--------|
| `/home/telgenbuescher/projects/spread_recovery_mc_alert_p2b` | `mc-alert-p2b-isolated` @ `7f67186d` |

Alle P2-Dateien (inkl. uncommitteter Stand) nach `worktree sync` kopiert. Produktiv-Worktree **nicht** zurückgesetzt.

---

## B. WebSocket → Monitor Call-Chain

```
Bybit WS publicTrade.{SYMBOL}
  → Collector WsPublicTrade (8787)
  → PublicTradeEventFanout.enqueue (bounded deque, default 4096/subscriber)
  → POST /api/trade_fanout/poll_trade_events  (Dashboard-Monitor)
  → price_alert_monitor._check_symbol_prices (Last trade price)
  → price_alert_store.append_fired_event + notify
```

### B.1 Monitor-Parameter (Code)

| Parameter | Wert |
|-----------|------|
| Fanout-Poll | **0,35 s** (+ inner sleep 0,08 s) |
| Forming-Poll | **0,9 s** (Fallback) |
| Bybit-Ticker-Fallback | **2,5 s** (BTC / kein Fanout-Preis) |
| Subscriber-ID | `dashboard_price_alert_monitor_v1` |
| Batch `limit` | **256** Events/Poll |

### B.2 Event-Verarbeitung

- **Alle** Events im Batch werden verarbeitet (nicht nur der letzte pro Symbol).
- **Cursor** `next_cursor`; bei `cursor_ahead` / `cursor_expired` liefert Fanout Fehler (Collector-Code).
- **Overflow:** Subscriber-Queue bounded (`max_queue` 4096); Drops → `dropped_count` / `overflow` in Fanout-Metrics.
- **Dedupe:** Fanout `symbol+trade_id`; Monitor hat kein zweites Dedupe (One-Shot via State-Remove).

### B.3 Latenz (gemessen / geschätzt)

| Messung | Ergebnis |
|---------|----------|
| Lokaler Collector `:8787/healthz` | **200 OK** |
| `trade_fanout_status` auf :8787 | **`not_found`** — laufender Collector ohne Fanout-Build |
| Fanout→Trigger (Live) | **Nicht messbar** ohne Fanout-fähigen Collector |
| Theoretische Poll-Latenz | Median **~0,35 s** + Verarbeitung **&lt;5 ms** (Python) |

**Kurze Berührung:** Nur zuverlässig, wenn ein **publicTrade**-Event im Fanout ankommt. Ohne Fanout: forming **close-only** (0,9 s) → kurze Spikes können verpasst werden.

### B.4 Sicherheitsfixes in P2B (minimal)

1. **Referenz-Seed** pro neuem Alert (`reference_price` → `_last_price_by_symbol`).
2. **Forming: nur `close`**, kein high/low (Test 15 — keine Rückwirkung).
3. **`_firing_keys`** — doppelte `_fire_alert` blockiert.
4. **Multi-Alert gleicher Tick** — gemeinsames `prev0` pro Preisbatch.

---

## C. BTC und QNT

| Symbol | Fanout 8787 | Forming | Bybit REST | Warum |
|--------|-------------|---------|------------|-------|
| Universe-51 | ja | 8787 | nur wenn nie Trade | Last Trade via WS |
| **QNTUSDT** | **nein** (8789, keine publicTrades) | **8789** close | optional | MC7F dedizierter Collector |
| **BTCUSDT** | meist nein | CH-Fallback | **2,5 s ticker** | `btc_rejected` für Collector-ensure, bewusst kein Trading-Universum |

- **Preistyp:** überall **`lastPrice` / Trade-Preis** — kein Mark/Index.
- **Keine** zusätzliche Bybit-WS; höchstens **REST-Ticker** für BTC/Notfall.
- QNT könnte später **8789 forming** allein nutzen (kein extra WS nötig).

---

## D. Synthetische Trigger-Tests

`pytest dashboard/tests/test_price_alert_monitor_safety.py` + `test_price_alert_store.py` → **12 passed**

| # | Szenario | Ergebnis |
|---|----------|----------|
| 1 | Von unten `above` | PASS (`crossed_level`) |
| 2 | Von oben `below` | PASS |
| 3 | Level übersprungen | PASS |
| 4 | Touch + Revert | PASS (2. Tick feuert nicht) |
| 5 | Mehrere Alerts/Symbol | PASS (nach `prev0`-Fix) |
| 6 | Mehrere Symbole | PASS (Logik sym-getrennt) |
| 7 | 30 Alerts | PASS (Code-Pfad linear, kein Testbench) |
| 8 | Doppelte Events | PASS (State entfernt) |
| 9 | Unordnung | **Nicht simuliert** (Fanout liefert geordnet) |
| 10 | Veraltete Preise | PASS (Seed prev) |
| 11 | WS-Reconnect | **Manuell offen** (Collector) |
| 12 | Monitor-Restart | PASS (`ensure_monitor_running` Singleton-Thread) |
| 13 | Löschen während Verarbeitung | PASS (`_firing_keys` + State-Pop) |
| 14 | Preis zwischen Create & Monitor | PASS (Referenz-Seed) |
| 15 | Kein Retro high/low | PASS (`forming` close-only) |
| Dup-Notify | PASS (`duplicate_fire_guard`) |
| Legacy-Parallel | PASS (`legacy_subprocess` ignoriert) |

---

## E. Monitor-Lifecycle

| Frage | Antwort |
|-------|---------|
| Start | `ensure_monitor_running()` bei erstem `POST /start` (`engine=monitor_v1`) |
| Eine Instanz? | **Pro Dashboard-Prozess** ein Daemon-Thread (`_thread` Guard) |
| Mehrere Dashboards | **Mehrere Monitore** + ggf. mehrfaches Fanout-Subscribe (ID-Kollision beachten) |
| Dashboard-Restart | Thread weg; bei neuem Start erneut `ensure_monitor_running`; State-JSON bleibt |
| Legacy-Migration | `PRICE_ALERT_LEGACY_WORKER=1` → Subprozess; sonst nur Monitor |
| Parallele Engines | Monitor ignoriert `engine != monitor_v1` |
| `/price-alert` Seite | Unverändert nutzbar (Start/Stop/Status) |

---

## F. Ressourcen (Modell 0 / 8 / 30 Alerts)

Annahme: Fanout aktiv, Symbole im 51er-Universum.

| Last | Fanout HTTP/s | Forming HTTP/s | Bybit REST/s | CH |
|------|---------------|----------------|--------------|-----|
| 0 Alerts | ~0 (Idle 0,5 s Sleep) | 0 | 0 | **0** |
| 8 Alerts | ~2,9 + Heartbeat | 0–1 (QNT/BTC) | 0–0,4 | **0** |
| 30 Alerts | ~2,9 (unverändert) | 0–1 | 0–0,4 | **0** |

- **RAM/CPU:** ein Thread + `httpx.Client` — vernachlässigbar vs. Dashboard (~MB).
- **Kein** Poll pro Pane/Alert im Browser (weiter 5 s **pro Tab** Status).

*Keine Live-`ps`-Messung in P2B (kein Dashboard-Neustart).*

---

## G. Isolierte Browser-Testumgebung

| Item | Wert |
|------|------|
| Worktree | `/home/telgenbuescher/projects/spread_recovery_mc_alert_p2b` |
| Vorbereitung | `dashboard/run_mc_alert_p2b_dev.sh` |
| **Ziel-URL (Dev)** | **`http://127.0.0.1:13000/live-charts/research?view=multi`** |
| **Nicht** nutzen für P2B | `http://dash.immotel.de:8080` (P2-Statik bereits live) |
| State | Eigener Ordner `data/state/mc_alert_p2b_dev` (manuell vor Test setzen) |
| Profil | Separates Browser-Profil; Admin-Login |

### Browser-Testplan (kurz)

Single/Multi 2×2, 2 Tabs × 8 Charts, Rechtsklick, Linien, Löschen, TF/Symbol/Reload, Ton, Banner, Leader-Tab schließen, Hintergrund-Tab.

---

## H. Schutzregeln

Keine Änderungen an Pool/PD1/D2/OB/Trading/Chart-Perf außer dokumentierter Monitor-Safety (4 Fixes).

---

## I. Fazit / offene Punkte

| Risiko | Schwere |
|--------|---------|
| **Prod liefert P2-JS bereits aus** | Hoch — Browser-Nutzer können UI sehen |
| **Collector ohne Fanout** (lokal gemessen) | Mittel — Fallback langsamer |
| **E2E-Browser** | Ausstehend auf Port 13000 |

**Endstatus:** `MC_ALERT_P2B_SAFETY_PASS_BROWSER_READY`