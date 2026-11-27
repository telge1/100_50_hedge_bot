# MC-ALERT-P2E — Controlled Collector Fanout Activation

**Endstatus:** `MC_ALERT_P2E_FANOUT_LIVE_VERIFIED`  
**Datum:** 2026-11-27 (UTC)  
**Scope:** Nur Collector `:8787` — kein Dashboard-Deploy, keine neuen Preisalarme, kein Commit/Push

---

## Executive Summary

| Schritt | Ergebnis |
|---------|----------|
| Preflight | Bestanden (mit **Swap-Hinweis**) |
| Recovery-Checkpoint | `research/mc_alert_p2e/recovery_checkpoint_20261127_p2e/` |
| Code-Delta | `public_trade_event_fanout.py` + minimale Hooks in `collector.py` / `control_api.py` |
| Unit-Tests | **9 passed** (`test_public_trade_event_fanout_v1.py`) |
| Restart | `systemctl --user restart bybit-live-1m-collector.service` — PID **1550962 → 2350340**, LIVE ~**21 s** |
| Fanout HTTP | **200** (`trade_fanout_status`, `poll_trade_events`) — vorher **404** |
| Persistenz | `writer_fatal=false`, WS **true**, 55 Symbole, 1m **LIVE_CLOSED** nach Restart |
| Nicht gestartet | Dashboard, 8788, 8789, PD1-Publisher, Trading |

---

## 1. Preflight (vor Änderung)

### 1.1 Prozess & systemd

| Item | Wert |
|------|------|
| PID (alt) | **1550962** |
| CWD | `/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves` |
| Unit | `~/.config/systemd/user/bybit-live-1m-collector.service` (active) |
| WS | `websocket_connected: true` |
| State | `LIVE` |

### 1.2 Symbol- & Datenpfade

- **55** konfigurierte Kerzen- und Public-Trade-Symbole (inkl. DOGE, SOL, BNB, LINK, LIT, HYPE, ZEC, **BTC**).
- Public Trade: `lag_seconds ≈ 0.119`, `queue_depth 9`, `writer_fatal false`, `dropped_events 0`.
- Spool: ~56 KiB, `spool_batches_written 0` (laufend trivial).
- Forming (alle Zielsymbole): Bar-Zeit **1795779840** (konsistent vor Restart).

### 1.3 Neighbour Services (unverändert gehalten)

| Service | PID | Port |
|---------|-----|------|
| XAU Collector | **169835** | 8788 |
| QNT Collector | **2226456** | 8789 |

### 1.4 PD1 / Publisher / Catch-up

Aktive Publisher u. a.: LINK, XAU, LIT, HYPE, DOGE P4 live, ZEC, QNT, SOL/BNB MC4 — **nicht angefasst**.  
Kein laufender dedizierter **BTC-Replay**-Prozess in `pgrep`.  
Historischer DOGE-P4-Hintergrund-Monitor-Skript vorhanden (nur CH-Count-Poll).

### 1.5 Ressourcen

| Metrik | Vor Restart (8787) | Hinweis |
|--------|-------------------|---------|
| RSS 8787 | **296 760 KiB** (~1.6 % MEM) | |
| MemAvailable | ~**3.2 GiB** | |
| Swap | **~2.0 / 2.0 GiB belegt** | Erhöhtes Risiko; nicht als Hard-Abort gewertet (Collector klein, Restart kurz) |

### 1.6 Legacy-Preisalarme

Keine laufenden `price_alert.py`-Worker; keine `price_alert.json` in Standard-Pfaden gefunden.

### 1.7 Fanout vor Aktivierung

`POST /api/trade_fanout/trade_fanout_status` → **404** `not_found`.

---

## 2. Recovery-Checkpoint

**Pfad:** [`recovery_checkpoint_20261127_p2e/`](recovery_checkpoint_20261127_p2e/)

Enthält:

- `modules/collector.py`, `control_api.py` (Pre-Delta)
- `systemd/bybit-live-1m-collector.service` (+ drop-in)
- `git/git_status.txt`, `git_diff_worktree.patch`, `HEAD`
- `SHA256SUMS`, `SHA256SUMS_POST.txt`
- `preflight_post_metrics.json`, `post_restart_status.json`
- `restart_log.txt`, **`ROLLBACK.md`**

Dashboard-Tag `stable-mc-pool-p9-20261126` **unverändert**.

---

## 3. Minimaler Fanout-Delta

| Datei | Änderung |
|-------|----------|
| **NEU** `public_trade_event_fanout.py` | Kopie aus `public_trades_ema_fanout_integration_v1` |
| `collector.py` | `_trade_fanout`, Fanout in `_on_public_trade`, `note_reconnect` |
| `control_api.py` | `trade_fanout_op` + 6 HTTP-Routen |
| **NEU** `tests/unit/test_public_trade_event_fanout_v1.py` | 9 Tests |

**Nicht geändert:** WS-Subscribe, Symbol-Universen, Kerzen, CH-Insert, Spool, Scanner, Pool, Trading.

**Syntax + Tests:**

```text
py_compile: OK
pytest tests/unit/test_public_trade_event_fanout_v1.py → 9 passed in 0.54s
```

---

## 4. Kontrollierter Restart (nur 8787)

| Feld | Wert |
|------|------|
| Auslöser | `systemctl --user restart bybit-live-1m-collector.service` |
| CLI-Dauer | ~**0.65 s** |
| Alt-PID | **1550962** |
| Neu-PID | **2350340** |
| Zeit bis `state=LIVE` | ~**21 s** (RECOVERING → SUBSCRIBING → LIVE) |
| UTC LIVE | **2026-11-27T11:45:39Z** (ca.) |

8788/8789-PIDs unverändert.

---

## 5. Post-Restart Datenintegrität

| Prüfung | Ergebnis |
|---------|----------|
| `/healthz` | OK |
| WS | `true` |
| Symbole | **55** |
| `writer_fatal` | **false** |
| `lag_seconds` | ~**0.13** (post) |
| 1m Kerzen | `LIVE_CLOSED` z. B. DOGE @ `2026-11-27T11:45:00+00:00` |
| Logs | Keine Exceptions im Tail; normale LIVE_CLOSED-Zeilen |

### 5.1 Lücken / Zähler

- `last_trade_event_ts`: pre **11:45:14Z** → post **11:46:07Z** (laufender Stream).
- `rows_inserted` / `rows_received`: **Prozess-Zähler reset** beim Restart (pre ~382918 → post ~4611) — **kein** Ausschluss über CH-Lücken ohne separates CH-Audit.
- **Public-Trade-Events während ~21 s LIVE-Ausfall:** nicht in Fanout/CH hier quantifiziert; übliches WS-Reconnect-Fenster — **keine** behauptete vollständige Tick-Backfill-Wiederherstellung.

---

## 6. Fanout-Verifikation

### 6.1 Endpoints

| Endpoint | Vorher | Nachher |
|----------|--------|---------|
| `POST .../trade_fanout_status` | 404 | **200**, `ok: true`, `second_bybit_trade_ws: false` |
| `POST .../create_trade_subscriber` | 404 | **200** |
| `POST .../poll_trade_events` | 404 | **200**, Events mit `price`, `symbol`, `trade_id` |

### 6.2 Batch-Polling (Subscriber `p2e_verify_*`)

- Mehrere Batches (84 → 126 Events): **Cursor** monoton (z. B. 85 → 398).
- `coverage_valid: true`, `overflow: false` im Testfenster.
- Symbole inkl. **BTC, SOL, LINK, BNB, LIT, HYPE, ZEC** in Streams sichtbar.

### 6.3 Zielsymbole (alle 8 empfangen)

DOGE, SOL, BNB, LINK, LIT, HYPE, ZEC, BTC — jeweils mit **Last-Trade-Price** im Fanout-Stream verifiziert.

### 6.4 Duplikat-Verhalten

Wiederholtes `poll` mit **gleichem** Cursor liefert dieselben Events erneut (**at-least-once**). Consumer (Monitor) muss Cursor fortschreiben — wie P2D spezifiziert.

### 6.5 Persistenz

Post-Restart weiter steigende `rows_received` / stabiler Writer; Fanout `enqueued_total` steigt parallel — **keine** erhöhte `insert_failures`.

---

## 7. Ressourcen & Latenz (gemessen)

| Metrik | Vorher (8787) | Nachher (8787) |
|--------|---------------|----------------|
| RSS | ~297 MiB | ~**124 MiB** (frisch; kann wieder wachsen) |
| %CPU (Moment) | ~2.4 | ~38.7 (Catch-up-Spike, danach normalisierend) |
| `public_trade_metrics.lag_seconds` | ~0.119 | ~**0.131** |

### 7.1 Fanout-Latenz (kein Monitor E2E)

| Messung | Wert |
|---------|------|
| Collector PT `lag_seconds` | **~0.13 s** |
| Frischer Subscriber (SOL/DOGE), 1. s Pause + Poll | Event-`receive_time_ns` Alter **~718–744 ms** |
| BTC-only, schnelle Polls | **~2.0–2.1 s** (Burst-Queue beim ersten Poll) |
| Test-Subscriber mit Rückstau (hwm **3896**) | Alter **>20 s** — **Artefakt** durch manuelles Hochpollen während P2E |

**Keine** Behauptung: „Alert E2E < 1 s“ — Monitor/Dashboard weiter **nicht** produktiv deployed.

---

## 8. Rollback

Bei Bedarf: [`recovery_checkpoint_20261127_p2e/ROLLBACK.md`](recovery_checkpoint_20261127_p2e/ROLLBACK.md)

Kriterien für Rollback **nicht** ausgelöst (Start OK, WS, Kerzen, PT-Writer, akzeptable Ressourcen).

---

## 9. Offene Punkte / Nächste Schritte (nicht P2E)

1. **Dashboard-Reload** mit P2D-Monitor (separates Fenster).
2. CH-Vergleich optional: PT-Zeilen/min während Restart-Fenster.
3. Test-Subscriber (`p2e_*`) können via `remove_trade_subscriber` entfernt werden (Rahmen belassen oder cleanup).

---

## 10. Referenzen

- P2D Plan: `research/mc_alert_p2d/MC_ALERT_P2D_FANOUT_INTEGRATION.md`
- Fanout-SSOT: `public_trades_ema_fanout_integration_v1`
- Produktions-Repo (lokal gepatcht, uncommitted): `Signal_Generator_Ralf/signal_generator_stoch_waves`