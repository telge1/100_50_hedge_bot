# MC-ALERT-P2C — Live Price Source Verification (READ-ONLY)

**Endstatus:** `MC_ALERT_P2C_PRICE_SOURCE_BLOCKED`  
**Datum:** 2026-11-27  
**Methode:** Laufende Prozesse, HTTP-Probes, OpenAPI des Live-Dashboards, Collector-Status, kein Restart/Deploy

---

## Executive Summary

| Befund | Schwere |
|--------|---------|
| **Trade-Fanout-API fehlt auf :8787/:8788/:8789** | Blocker für tick-genaue Alerts |
| **Laufender Collector = `signal_generator_stoch_waves` (ohne Fanout-Code)** | Ursache |
| **Public-Trade-WS läuft** (55 Symbole, lag ≈0,12 s) | Daten da, aber **kein HTTP-Fanout** |
| **Monitor `monitor_v1` ohne Fanout: nach Referenz-Seed kein weiterer Preispfad** | **Funktionslücke** (s. C.3) |
| **Prod: P2-JS live, Backend-Prozess ohne `/events`, ohne neuen Monitor im RAM** | Frontend/Backend-Split |

**Empfehlung (ressourcenschonend):** Fanout-Build aus `public_trades_ema_fanout_integration_v1` **oder** Monitor-Fallback: forming für **alle** aktiven Symbole, wenn Fanout `not_found` (kleiner Patch, nicht in P2C ausgeführt). Bis dahin: Browser-Tests nur mit Legacy-Worker oder nach Fanout-Rollout.

---

## A. Fanout-API — Ursache `not_found`

### A.1 Ist-Zustand HTTP (alle Collector-Ports)

| Port | Prozess | healthz | `POST .../trade_fanout/*` |
|------|---------|---------|-------------------------|
| **8787** | PID **1550962** (51er + public trades) | OK | **`not_found`** |
| **8788** | PID **169835** (XAU) | OK | **`not_found`** |
| **8789** | PID **2226456** (QNT) | OK | **`not_found`** |

### A.2 Kanonischer Code mit Fanout

| Repo-Pfad | Fanout in `control_api.py`? |
|-----------|------------------------------|
| `public_trades_ema_fanout_integration_v1/.../control_api.py` | **JA** (`/api/trade_fanout/poll_trade_events`, …) |
| `public_trades_live_fanout_v1/...` | **JA** |
| **`Signal_Generator_Ralf/signal_generator_stoch_waves/.../control_api.py`** (→ **Produktion**) | **NEIN** — nur `desired_state`, `ensure_symbol`, `signals` |

**Ursache:** Es läuft eine **ältere Collector-Version ohne Trade-Fanout-Routen**, nicht ein falscher Port.

### A.3 Prozess-Command (8787, Auszug)

```
.../signal_generator_stoch_waves/.venv/bin/python scripts/run_live_collector_service.py
  --api-port 8787
  --enable-public-trades
  --public-trade-spool-dir results/live_collector/public_trade_spool
```

**Env:** Keine Fanout-Flag gefunden — Fanout ist **Build-/Code-Thema**, nicht Konfiguration.

### A.4 Wer empfängt Public-Trades heute?

```
Bybit WS publicTrade.{SYMBOL}
  → Live1mCollector._on_public_trade
  → CanonicalPublicTradeRepository / Spool → ClickHouse
  → health.public_trade_metrics (lag_seconds, queue_depth, …)
```

**Kein** zweiter Consumer über HTTP; Dashboard-Research liest **forming** (`GET /api/collector/forming`), nicht den Trade-Stream.

### A.5 Call-Chain (Soll vs. Ist)

| Soll (P2A/P2) | Ist (Live) |
|---------------|------------|
| WS → Fanout → `poll_trade_events` → Monitor | WS → **nur CH-Insert**; Monitor-Polls → **404** |

---

## B. Symbolabdeckung (Live-Probe 2026-11-27)

Legende:

- **PT-WS:** in `public_trade_symbols` auf 8787
- **Forming:** `GET /api/collector/forming?symbol=`
- **Monitor heute:** siehe Spalte „Effektiv“ (inkl. Fanout-Lücke)

| Symbol | PT-WS 8787 | Forming | Collector | Monitor-Pfade (Code) | Effektiv ohne Fanout | Kurz-Touch-Risiko |
|--------|------------|---------|-----------|----------------------|----------------------|-------------------|
| **DOGEUSDT** | ja | 8787 ✓ | 8787 | Fanout → forming wenn kein `last_price` | **Kein Update nach Seed** | **Hoch** |
| **SOLUSDT** | ja | ✓ | 8787 | wie oben | **Kein Update** | Hoch |
| **BNBUSDT** | ja | ✓ | 8787 | wie oben | **Kein Update** | Hoch |
| **LINKUSDT** | ja | ✓ | 8787 | wie oben | **Kein Update** | Hoch |
| **XAUUSDT** | nein | **8788** ✓ | 8787+XAU | forming 8788, kein PT | forming ~0,9 s (QNT-Regel nur QNT; XAU: siehe C.3) | Mittel |
| **LITUSDT** | ja | ✓ | 8787 | wie DOGE | **Kein Update** | Hoch |
| **HYPEUSDT** | ja | ✓ | 8787 | wie oben | **Kein Update** | Hoch |
| **ZECUSDT** | ja | ✓ | 8787 | wie oben | **Kein Update** | Hoch |
| **QNTUSDT** | nein | **8789** ✓ | nur 8789 | `_FORMING_SYMBOLS` → forming close | forming **~0,9 s** | Mittel–hoch |
| **BTCUSDT** | ja (8787) | ✓ | 8787 | Ticker **2,5 s** + forming übersprungen nach Seed | **Ticker 2,5 s** | Mittel |

**Hinweise:**

- **BTC/QNT** nicht im Trading-`ensure_symbol`-Universum; **BTC** ist dennoch auf **8787 PT-WS** subscribed (Status).
- **XAU** nur auf **8788**; 8787 forming leer — Monitor nutzt `_COLLECTOR_BASE` 8787 für forming, **nicht** 8788 für XAU (zusätzliche Lücke für XAU-Alerts).

**Preistyp:** Collector/WS = **Last trade**; forming = **partial 1m OHLC close**; Monitor-Ticker = **Bybit REST lastPrice**. Kein Mark/Index in Monitor.

**Forming-Aktualität (Stichprobe):** SOL: 1× close-Änderung in 4 s bei 0,2 s Poll; Trade-lag **0,12 s** bei laufendem PT-Ingest.

**Ticker-WS:** Kein separater Bybit-Ticker-Stream im Collector; nur kline + publicTrade.

---

## C. Geschwindigkeit & Event-Verarbeitung

### C.1 Fanout-Soll (nicht aktiv)

| Parameter | Wert (Design) |
|-----------|----------------|
| Monitor poll | **0,35 s** |
| Batch limit | **256** Events |
| Subscriber queue | **4096** (Monitor-Config) |
| Cursor | `next_cursor`; Fehler `cursor_ahead` / `cursor_expired` |

**End-to-End:** Theoretisch Median **&lt; 0,5 s** WS→Trigger **nur mit Fanout**.

### C.2 Ist ohne Fanout

- Fanout-Schleife läuft, liefert **leere** Eventlisten.
- **0,35 s ≠ Trigger-Latenz** — ohne Events keine Auswirkung.

### C.3 Kritische Monitor-Logik (READ-ONLY-Analyse)

Nach Alert-Start setzt `_sync_alert_reference_seeds()`:

```text
_last_price_by_symbol[sym] = reference_price
```

Folge:

- **Forming** nur wenn `sym in {QNTUSDT}` **oder** `sym not in _last_price_by_symbol` → nach Seed **false** für DOGE/SOL/…
- **Bybit-Ticker** nur `BTCUSDT` oder Symbol ohne `last_price` → nach Seed **false**

→ **`monitor_v1` erhält für Standard-8787-Alerts keine weiteren Preise**, solange Fanout fehlt.

**QNT:** forming close alle **~0,9 s**.  
**BTC:** Ticker alle **~2,5 s** (REST, nicht WS).

### C.4 Public-Trade-Metriken (8787)

- `lag_seconds`: ~**0,12**
- `dropped_events`: **0** (Status-Snapshot)
- `queue_depth`: ~**13**
- `reconnect_count`: **1**

Trades werden **kontinuierlich** geschrieben; der Alert-Monitor **nutzt sie nicht** ohne Fanout.

---

## D. Produktionsstand Frontend / Backend

| Check | Ergebnis |
|-------|----------|
| Prod `chart.js` enthält `chart-ctx-menu` / `on_price_alert_create` | **JA** (HTTP :8080) |
| Prod JS ≡ lokaler P2-Hash (P2B) | **JA** |
| Dashboard-Prozess | PID **2255228**, CWD `spread_recovery_hedge_short_dev/dashboard`, gestartet ~10:26 |
| OpenAPI `:3000` Price-Alert-Routen | `/start`, `/status`, `/stop`, … — **kein** `/events` |
| `GET http://dash.immotel.de:8080/api/price-alert/events` | **404** |
| `GET .../status` | **401** (Route existiert) |

**Konflikt:**

- **Browser** kann Kontextmenü + Linien-Client zeigen.
- **Laufender `app.py`-Prozess** wurde **nicht neu geladen** → kein Monitor-Thread, kein `/events`, ggf. altes Start-Verhalten (Subprozess), obwohl **Disk-`app.py`** P2-Code enthält.

**Kein Testalarm auf Prod ausgeführt** (READ-ONLY).

---

## E. Isolierter Worktree `spread_recovery_mc_alert_p2b`

| Punkt | Status |
|-------|--------|
| Pfad / Branch | `/home/telgenbuescher/projects/spread_recovery_mc_alert_p2b`, `mc-alert-p2b-isolated` |
| P2-Dateien kopiert | ja |
| `run_mc_alert_p2b_dev.sh` | Port **13000**, `data/state/mc_alert_p2b_dev` vorbereitet |
| **State-Isolation in Code** | `price_alert_store.py` → `{worktree_root}/data/state/price_alert.json` — **getrennt**, wenn Dashboard **aus Worktree** startet |
| Prod-State | **keine** `price_alert.json` im Spread-Root (leer) |
| Dev-State-Ordner | noch **nicht** angelegt (bis Script-Lauf) |
| Collector/Trading | Script startet **keine** Collector; nur Lesen auf :8787–8789 |

**Lücke:** Worktree-`run_mc_alert_p2b_dev.sh` nutzt Symlink-Hinweis, **`app.py` ignoriert Symlink** — Isolation wirkt nur über **eigenes Repo-Root** beim Start aus Worktree.

---

## F. Empfehlung: zuverlässigste Nutzung vorhandener Live-Daten

### Priorität 1 (kanonisch, ressourcenschonend)

**Trade-Fanout auf dem laufenden 8787-Collector aktivieren** (Codebasis `public_trades_ema_fanout_integration_v1`):

- **Eine** zusätzliche HTTP-Subscription (`dashboard_price_alert_monitor_v1`).
- **Keine** zweite Bybit-WS.
- Gleiche Last-Trade-Preise wie CH-Ingest.
- Dann Monitor-Fanout-Poll **0,35 s** wie designed.

### Priorität 2 (bis Fanout live)

Monitor-Fallback (minimal, **nicht in P2C implementiert**):

1. Bei Fanout `not_found`: **forming close für alle** `monitor_v1`-Symbole alle **~0,5–1 s** (8787 / 8789 / **8788 für XAU**).
2. Optional: **kein** Bybit-REST außer BTC, wenn forming reicht.

### Priorität 3 (Browser-Tests)

1. Dashboard **neu aus Worktree** auf **:13000** starten (nach Fanout oder Fallback-Patch).
2. **Nicht** gegen `:8080` testen (P2-JS schon live, Backend-RAM veraltet).
3. Bis Patch: nur **`PRICE_ALERT_LEGACY_WORKER=1`** für echte Trigger-Tests ohne Fanout.

### Geschwindigkeit (realistisch nach Fix)

| Quelle | Erwartete Trigger-Latenz |
|--------|--------------------------|
| Fanout (PT-WS) | **~0,2–0,5 s** typisch |
| Forming close | **~0,9–4 s** (+ kurze Touch-Lücken) |
| Bybit REST (BTC) | **~2,5 s** |

---

## G. Schutz

READ-ONLY eingehalten: keine Collector-/Dashboard-Restarts, kein Commit/Push, keine Prod-Testalerts.

---

**MC_ALERT_P2C_PRICE_SOURCE_BLOCKED** — Browser-E2E für `monitor_v1` erst nach Fanout **oder** Monitor-Fallback + frischem Dashboard-Prozess.