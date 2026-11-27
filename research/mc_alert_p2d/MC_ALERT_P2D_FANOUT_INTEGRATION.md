# MC-ALERT-P2D — Canonical Trade Fanout Integration & Fallback Repair

**Endstatus:** `MC_ALERT_P2D_FANOUT_READY_CONTROLLED_ACTIVATION_PENDING`  
**Datum:** 2026-11-27  
**Scope:** Isoliertes Worktree `spread_recovery_mc_alert_p2b` (Branch `mc-alert-p2b-isolated`). Kein Commit/Push/Deploy, kein Collector-/Dashboard-Restart in Produktion.

---

## Executive Summary

| Bereich | Ergebnis |
|---------|----------|
| **Entscheidung P2D** | Public Trades über Fanout **primär**; kein zweites Bybit-WS; Forming nur als **degradiertes** Fallback |
| **Collector (:8787)** | Fanout-Code in `public_trades_ema_fanout_integration_v1` **wiederverwendbar**; Produktion **ohne** Fanout → HTTP **404** (verifiziert) |
| **Monitor-Fallback-Bug** | Behoben im Worktree: `_cross_reference` ≠ `_last_price_by_symbol`; Forming/Fanout parallel nach Verfügbarkeit |
| **XAU / QNT** | Forming über `:8788` / `:8789` (`GET /api/collector/forming`) — **200 OK** live |
| **Tests** | `pytest` Worktree: **15 passed** (`test_price_alert_monitor_safety.py`, `test_price_alert_store.py`) |
| **Aktivierung** | Fanout erfordert **kontrollierten Restart** nur `:8787`; Dashboard-Reload für `/api/price-alert/monitor-status` + P2-Monitor |

---

## 1. Entscheidung (bestätigt)

- Kein exklusiver Umstieg auf Forming-Polling.
- Bestehende Public-Trade-WS-Daten des laufenden Collectors bleiben **kanonische** Tick-Quelle für `monitor_v1`.
- Keine parallele Bybit-WS-Verbindung für dieselben 55 Symbole.
- Implementierung folgt **P2A-Fanout** (`public_trade_event_fanout.py` + HTTP-Subscriber), nicht Ersatz des gesamten Collectors.

---

## 2. Fanout vs. Produktions-Collector

### 2.1 Ist-Zustand (READ-ONLY, 2026-11-27)

| Probe | Ergebnis |
|-------|----------|
| `POST http://127.0.0.1:8787/api/trade_fanout/trade_fanout_status` | **HTTP 404** `not_found` |
| `GET .../api/collector/forming?symbol=DOGEUSDT` (8787) | **200** |
| XAU forming (8788), QNT forming (8789) | **200** |

Produktion: `Signal_Generator_Ralf/signal_generator_stoch_waves` — `_on_public_trade` enqueued **nur** CH-Buffer; `control_api.py` **ohne** `trade_fanout_*`.

### 2.2 Integration-Branch (Soll-Fanout)

| Komponente | Pfad | Rolle |
|------------|------|--------|
| `PublicTradeEventFanout` | `public_trades_ema_fanout_integration_v1/.../public_trade_event_fanout.py` | Bounded in-memory ring pro Subscriber; `on_trade` im Hot Path |
| Hook | `collector._on_public_trade` | Fanout **vor** `buf.enqueue` |
| HTTP | `control_api.trade_fanout_op` + 6 `/api/trade_fanout/*` Routen | Localhost IPC |

### 2.3 Technische Eigenschaften (Fanout-Modul)

- **Threading:** Lock pro Subscriber; `on_trade` non-blocking, Fehler isoliert von CH-Insert.
- **Buffer:** Default global ring 8192; pro Subscriber `max_queue` (Monitor: **4096**).
- **Overflow:** `overflow` / `coverage_valid` in Poll-Response; Dedupe `symbol+trade_id`.
- **Reihenfolge:** Monotonic `record_ordinal` + cursor; `cursor_ahead` / `cursor_expired` bei Lücken.
- **CH/Spool:** Unverändert — Fanout beobachtet dieselben normalisierten Trades.

### 2.4 Minimaler Collector-Delta (nur 8787)

Siehe Worktree-Artefakt:

`spread_recovery_mc_alert_p2b/research/mc_alert_p2d/COLLECTOR_MINIMAL_DELTA.md`  
Kopie Modul: `.../collector_artifacts/public_trade_event_fanout.py`

`diff` Produktion ↔ Integration betrifft im Wesentlichen:

- `self._trade_fanout`
- `_on_public_trade` (+12 Zeilen Fanout-Aufruf)
- `_ensure_trade_fanout`, `trade_fanout`
- `fanout.note_reconnect()` am Trade-WS-Reconnect
- `control_api.py` (~140 Zeilen Routen + `trade_fanout_op`)

**8788/8789:** Kein Fanout nötig (XAU/QNT über Forming).

---

## 3. Symbolabdeckung

| Symbol | Primär | Fallback / Sonderfall |
|--------|--------|------------------------|
| DOGE, SOL, BNB, LINK, LIT, HYPE, ZEC, BTCUSDT | Fanout :8787 (nach Aktivierung) | Forming :8787 wenn Fanout 404/stale |
| **BTCUSDT** | Wie oben (Alerts erlaubt) | **Nicht** Trading-Universe |
| **XAUUSDT** | — | **Immer** Forming :8788 |
| **QNTUSDT** | — | **Immer** Forming :8789 (kein PT auf 8787) |

Monitor-Logik: `_FORMING_ONLY_SYMBOLS = {QNTUSDT, XAUUSDT}`; `_collector_base_for_symbol()` mappt Ports.

---

## 4. Monitor-Fallback-Reparatur (Worktree)

**Datei:** `spread_recovery_mc_alert_p2b/dashboard/price_alert_monitor.py`

### 4.1 Ursache P2C

Reference-Seed setzte `_last_price_by_symbol` → ohne Fanout wurden Forming/Ticker-Pfade effektiv stillgelegt.

### 4.2 P2D-Verhalten

| Mechanismus | Beschreibung |
|-------------|--------------|
| `_cross_reference` | Nur Crossing-Baseline aus `reference_price` |
| `_last_price_by_symbol` | Nur aus **echten** Live-Updates (Fanout oder Forming) |
| Fanout-Probe | Alle **5 s**; 404 → `_fanout_available=False`, Subscriber reset |
| Fanout-Poll | **0,35 s** + Heartbeat |
| Forming | **0,9 s** wenn `_should_use_forming_fallback()` (QNT/XAU immer; sonst Fanout off/stale > **2,5 s**) |
| Quellenwechsel | Kein Mischen in einem Tick: Fanout-Events und Forming laufen in getrennten Pfaden; Stale markiert `STALE` |
| Fire | `crossed_level` / `condition_met`; `_firing_keys` gegen Doppel-Auslösung |

**Quellen-Labels in Fired-Log:** `public_trade_fanout` vs `collector_forming` (unterscheidbar).

### 4.3 Monitoring-API

`GET /api/price-alert/monitor-status` (auth) → `get_monitor_status()`:

- `fanout_available`, `fanout_cursor`, `fanout_coverage_valid`, `fanout_last_error`
- Pro Symbol: `status` (`LIVE_PUBLIC_TRADE` | `LIVE_FORMING_FALLBACK` | `STALE` | `UNAVAILABLE`), `source`, `last_price`, `age_seconds`, `alert_count`, optional `forming_time` / `collector_base`

Keine Chart-Layout-Erweiterung — API-only.

---

## 5. Produktionskonflikt (unverändert kritisch)

| Layer | Befund |
|-------|--------|
| **Frontend** | P2-JS (Context-Menü, Price Lines) bereits ausgeliefert |
| **Backend-Prozess** | Läuft ohne Reload: `/api/price-alert/events` auf Prod-Instanz **nicht** im laufenden OpenAPI (P2C); lokale Probe `:8080/.../events` → **301** (Redirect/Proxy) |
| **Monitor RAM** | Produktiver Dashboard-Prozess **ohne** P2D-Monitor bis Reload |

**Folge:** Chart-UI kann Alerts anlegen; zentraler `monitor_v1` + SSE/Events erst nach **koordiniertem** Dashboard-Reload konsistent.

**Empfehlung:** Aktivierung in zwei Schritten (s. §8): (1) Collector 8787 + Fanout, (2) Dashboard mit P2D-Backend — oder zuerst Dashboard mit Forming-Fallback only (sofort nutzbar ohne Fanout).

---

## 6. Tests (Worktree)

```text
cd spread_recovery_mc_alert_p2b/dashboard && python3 -m pytest \
  tests/test_price_alert_monitor_safety.py tests/test_price_alert_store.py -q
→ 15 passed in 0.12s
```

| Szenario | Abdeckung |
|----------|-----------|
| Reference-Seed + Forming trotzdem aktiv | `test_forming_fallback_after_reference_seed` |
| XAU → :8788 | `test_xau_uses_8788_base` |
| Fanout off → Forming | `test_should_use_forming_when_fanout_unavailable` |
| Touch-Revert / Multi-Alert / Duplicate / Forming-High | bestehende P2B-Tests |
| Fanout HTTP 404 live | Manuell: Probe + Monitor-Probe-Logik (Unit: `_fanout_available=False`) |
| 30 Alerts / Ressourcen / CH-Persistenz | **Nicht** im isolierten Lauf gemessen; Fanout-Design = O(1) enqueue, bounded RAM |

**Offen für E2E (nach Aktivierung):** Fanout-Reconnect, kurzer PT-Touch mit Rücksprung, `cursor_expired`-Recovery, Lasttest 30 Alerts.

---

## 7. Latenz & Ressourcen (Schätzung + Design)

| Pfad | Erwartete Trigger-Latenz |
|------|---------------------------|
| Fanout (nach Rollout) | WS lag ~0,12 s + Poll 0,35 s ≈ **0,2–0,5 s** typisch |
| Forming-Fallback | Poll 0,9 s + Partial-Bar-Granularität (nur **close**) — **degradiert**, nicht tick-genau |

**Zusatz-RAM/CPU 8787:** Ein `PublicTradeEventFanout`-Singleton + ein Subscriber (~4096 Events Queue cap) — im Vergleich zu bestehendem PT-Insert **vernachlässigbar** (kein zweites WS).

---

## 8. Aktivierungs- und Rollback-Plan

### Phase A — Monitor (Dashboard)

1. Deploy/Reload `spread_recovery_hedge_short_dev` Dashboard **mit** Worktree-Stand: `price_alert_monitor.py`, `app.py` (`/monitor-status`, `/events` falls fehlend).
2. State-Datei: **eigene** `price_alert.json` in Dev; Produktion nicht teilen.
3. Validieren: Aktive Alerts → `monitor-status` zeigt `LIVE_FORMING_FALLBACK` solange Fanout 404.

### Phase B — Fanout (Collector :8787)

1. Cherry-pick / merge Minimal-Delta aus §2.4 in `signal_generator_stoch_waves`.
2. **Wartungsfenster:** Restart `run_live_collector_service.py --api-port 8787`.
3. Post-check: `POST .../trade_fanout_status` → `ok: true`; Monitor → `LIVE_PUBLIC_TRADE` + sinkende `age_seconds`.
4. **Datenlücken Fanout:** Nach Restart `create_trade_subscriber` mit neuem Cursor — kein Backfill historischer Ticks; Crossing-Logic nutzt `_cross_reference` + erste Live-Events; optional kurz Forming bis Fanout stabil.

### Phase C — Koordination Prod

1. Frontend bereits P2 — Backend-Reload abstimmen (ein Fenster).
2. Rollback Dashboard: vorheriges `app.py` + Monitor; Legacy-Worker nur wenn explizit gewünscht (`PRICE_ALERT_LEGACY_WORKER=1`).
3. Rollback Collector: vorheriges Image/Code, Restart 8787; Monitor fällt automatisch auf Forming zurück.

**In P2D ausgeführt:** Keine Produktionsschritte — nur Vorbereitung + Tests im Worktree.

---

## 9. Datei-Inventar (P2D)

| Artefakt | Ort |
|----------|-----|
| Monitor P2D | `spread_recovery_mc_alert_p2b/dashboard/price_alert_monitor.py` |
| API `monitor-status` | `spread_recovery_mc_alert_p2b/dashboard/app.py` |
| Tests | `spread_recovery_mc_alert_p2b/dashboard/tests/test_price_alert_monitor_safety.py` |
| Collector-Delta-Doku | `spread_recovery_mc_alert_p2b/research/mc_alert_p2d/COLLECTOR_MINIMAL_DELTA.md` |
| Fanout-Modul-Kopie | `spread_recovery_mc_alert_p2b/research/mc_alert_p2d/collector_artifacts/public_trade_event_fanout.py` |
| Dieser Bericht | `pools+ob+delta_bot/research/mc_alert_p2d/MC_ALERT_P2D_FANOUT_INTEGRATION.md` |

---

## 10. Blocker-Bewertung

| Blocker | Status |
|---------|--------|
| Fanout nicht live ohne Collector-Restart | **Akzeptiert** — Aktivierung bewusst pending |
| Prod FE/BE-Split | **Offen** bis Dashboard-Reload |
| E2E Browser (Port 13000) | **Pending** nach B + A |

**Kein** `MC_ALERT_P2D_BLOCKED`: Monitor + Fallback + Integrationspfad sind implementiert und getestet; verbleibend ist **kontrollierte** Produktionsaktivierung.

---

## Referenzen

- P2C Ist-Analyse: `research/mc_alert_p2c/MC_ALERT_P2C_LIVE_PRICE_SOURCE_VERIFICATION.md`
- Fanout-SSOT: `public_trades_ema_fanout_integration_v1`
- Produktions-Collector: `Signal_Generator_Ralf/signal_generator_stoch_waves` PID **1550962** (:8787)