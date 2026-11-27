# MC-ALERT-P2F — Dashboard Activation & Final Browser Test

**Endstatus:** `MC_ALERT_P2F_LIVE_BROWSER_TEST_READY`  
**Datum:** 2026-11-27  
**Kein Commit/Push** (wie angewiesen — erst nach manuellem Browser-Test)

---

## Executive Summary

| Schritt | Ergebnis |
|---------|----------|
| Preflight | 8787 LIVE, Fanout OK, 8788/8789 OK, MemAvailable ~3.4 GiB (Swap ~1.9/2 GiB) |
| Regression | **15 passed** (`test_price_alert_monitor_safety`, `test_price_alert_store`) |
| Isolierter Probe | `p2f_integration_probe.py` — Fanout + Monitor `LIVE_PUBLIC_TRADE` (DOGE) |
| Dev :13000 | OpenAPI mit **events** + **monitor-status** (danach beendet) |
| Prod-Aktivierung | P2D-`price_alert_monitor.py`, `app.py` (+`monitor-status`), **Dashboard-Restart** |
| Prod OpenAPI | Alle Alert-Routen inkl. `/events`, `/monitor-status` |
| Collector / PD1 | **Kein** Restart |

---

## 1. Preflight

- **8787:** `healthz` OK, Fanout `ok: true`, `enqueued_total` steigend.
- **8788 / 8789:** `healthz` OK.
- **Memory:** MemAvailable ~3.4 GiB; Swap fast voll — Dev :13000 nur kurz, Prod-Restart durchgeführt.
- **Legacy-Alerts:** Kein `price_alert.py`-Worker; keine produktive `price_alert.json` unter `spread_recovery_hedge_short_dev/data/state/`.
- **Dashboard (vorher):** PID **2255228**, OpenAPI **ohne** `/events` und `/monitor-status` (alter Prozessstand).

---

## 2. Backup & Rollback

| Artefakt | Pfad |
|----------|------|
| Pre-`app.py`, Pre-`price_alert_monitor.py` | `research/mc_alert_p2f/rollback_20261127/` |
| SHA256 Pre/Post | `SHA256SUMS_PRE.txt`, `SHA256SUMS_POST.txt`, `SHA256SUMS_ASSETS.txt` |
| Rollback-Anleitung | `rollback_20261127/ROLLBACK.md` |

Tag `stable-mc-pool-p9-20261126` **unverändert**.

### Geänderte Produktionsdateien (minimal)

| Datei | Änderung |
|-------|----------|
| `dashboard/price_alert_monitor.py` | P2D-Stand aus Worktree |
| `dashboard/app.py` | `get_monitor_status`, Route `GET /api/price-alert/monitor-status` |
| `dashboard/tests/test_price_alert_monitor_safety.py` | übernommen |

**Unverändert** (bereits P2-Frontend): `price_alert_chart.js`, `research_charts.js`, `chart.js`, `research_charts.html` — SHA256 in `SHA256SUMS_ASSETS.txt`.

---

## 3. Isolierter Integrationstest

### 3.1 Pytest (Prod-Baum, vor Restart)

```text
15 passed in 0.12s
```

### 3.2 `p2f_integration_probe.py` (Worktree-State, kein Prod-State)

```json
{
  "fanout_status_ok": true,
  "fanout_doge_events": 1,
  "forming_XAUUSDT": true,
  "forming_QNTUSDT": true,
  "monitor_fanout_available": true,
  "monitor_doge_source": "public_trade_fanout",
  "monitor_doge_status": "LIVE_PUBLIC_TRADE",
  "fired_count_after_warmup": 0
}
```

Ergebnisdatei: `research/mc_alert_p2f/p2f_integration_probe_result.json`

**Hinweis:** Kein erzwungener Live-Fire (kein Spam Ntfy/Ton); One-Shot/Fire in Synthese-Tests abgedeckt (pytest).

### 3.3 Dev-Dashboard :13000 (kurz)

- OpenAPI: alle 8 `price-alert`-Pfade inkl. **events**, **monitor-status**.
- Prozess nach Test **beendet** (Port frei).

---

## 4. Kontrollierte Produktions-Aktivierung

| Feld | Wert |
|------|------|
| Aktion | `systemctl --user restart dashboard.service` |
| Alt-PID | **2255228** |
| Neu-PID | **2354511** |
| Start ~UTC | siehe `dashboard_restart.log` (**2026-11-27T11:50:23Z**) |
| Bind | **0.0.0.0:3000** (extern typisch **https://dash.immotel.de:8080** via Nginx) |

---

## 5. Produktionsverifikation (technisch)

| Prüfung | Ergebnis |
|---------|----------|
| OpenAPI :3000 | `/api/price-alert/events`, `/monitor-status`, start/stop/status, test-sound |
| Auth ohne Session | **401** (erwartet) |
| Laufende Research-API | Log: `forming-bar`, `live-status` **200** nach Restart |
| Ein Dashboard-Prozess | eine `app.py`-Instanz (PID **2354511**) |
| Monitor | Startet mit erstem `monitor_v1`-Alert (`ensure_monitor_running` in `start`) |
| Collector 8787 | unverändert (kein Restart) |

**Nicht automatisiert:** Poollandschaft, Orderbook, Long/Short-Boxen, Trading — **manuelles UI** (Browser-Schritt 7).

---

## 6. Gemessene Latenz / Quellen

| Messung | Wert |
|---------|------|
| Fanout-Probe | ~0.23 s |
| Monitor-Warmup (DOGE) | 4 s bis `LIVE_PUBLIC_TRADE` |
| E2E Alert-Fire → Ton | **nicht** automatisiert — User-Browser-Test |

Erwartete Live-Quelle (mit Fanout P2E): **public_trade_fanout** für 8787-Symbole; **forming** auf 8788/8789 für XAU/QNT.

---

## 7. Browser-Testanleitung (Nutzer)

**URL (Research Multi-Chart):**

[https://dash.immotel.de:8080/live-charts/research?view=multi](https://dash.immotel.de:8080/live-charts/research?view=multi)

*(Single-Chart: `view=single` oder Standard Research-Pfad nach Login.)*

**Vorbereitung:** eingeloggt als Admin (Alert-Start erfordert Admin), **Hard-Reload** (Ctrl+Shift+R).

1. Alert-Ton im Chart aktivieren (Kontextmenü / Sound-Hinweis, falls angezeigt).
2. **Rechtsklick** auf einen **Preis** im Chart (nicht auf leere Fläche).
3. **Preisalarm erstellen** (Ziel + above/below).
4. **Gelbe gestrichelte Linie** am Zielpreis sichtbar?
5. Test **knapp oberhalb** des aktuellen Kurses (`above`, kleines Delta).
6. Test **knapp unterhalb** (`below`).
7. Bei Trigger: **Ton** + Tab-Badge/Sync (kein echtes Trading).
8. **Zwei Tabs** gleichzeitig Multi-Chart — nach Auslösung nur **einmal** piepsen (Leader Election).
9. Alert im Chart **löschen** (Stopp/Linie weg).

**Symbole empfohlen:** DOGE/SOL (Fanout); optional XAU/QNT (Forming). **BTC** nur Alert-Test, nicht Trading.

**Optional Admin-Check (eingeloggt):**

- `GET /api/price-alert/monitor-status` — `LIVE_PUBLIC_TRADE` / `LIVE_FORMING_FALLBACK`
- `GET /api/price-alert/events?since=0` — ein Event pro One-Shot

---

## 8. Einschränkungen

- **Swap** weiterhin hoch — bei OOM nur Dashboard-Rollback, keine Extra-Instanzen.
- **Kein Commit** — Code nur auf Disk im Dashboard-Repo.
- **Alert-Latenz** subjektiv: Fanout ~0.13 s + Monitor-Poll ~0.35 s (s. P2D), plus Browser/Notify.

---

## 9. Referenzen

- P2E: `research/mc_alert_p2e/MC_ALERT_P2E_CONTROLLED_FANOUT_ACTIVATION.md`
- P2D: `research/mc_alert_p2d/MC_ALERT_P2D_FANOUT_INTEGRATION.md`