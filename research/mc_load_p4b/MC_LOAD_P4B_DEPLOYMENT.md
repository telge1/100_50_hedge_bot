# MC-LOAD-P4B — Deployment (tatsächlicher Stand)

**Datum:** 2026-11-26 (Server-Zeit ~10:15 UTC+1)  
**Kein Commit/Push.** **Keine Browser-Messungen** (nur HTTP/API/Prozess).

---

## 1. Aktive Prozesse & Routing

| Komponente | PID (nach Deploy) | CWD / Binary | Port |
|------------|-------------------|--------------|------|
| **Research-Dashboard (API+Static)** | **1908793** (neu) | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` · `.venv/bin/python app.py` | **0.0.0.0:3000** (uvicorn) |
| Vorher (ohne P4B-Backend im RAM) | 1616938 (beendet) | gleiches CWD, gestartet 2025-11-25 10:57 | :3000 |

**Nginx:** `dash.immotel.de` (80/443) → `proxy_pass http://127.0.0.1:3000` (Root, nicht nur `/hedgebot/`).  
Referenz-Konfig: `/etc/nginx/sites-enabled/dash.immotel.de`.  
`dashboard/nginx_location.conf` beschreibt optional `/hedgebot/` → :3000 (nicht aktiv auf dieser Host-Route).

**Weitere Prozesse (unberührt):**

- `orderbook_v2_live` PID 3431475 — läuft weiter
- `symbol_onboarding.queue_consumer` PID 556665 — läuft weiter
- Dashboard **ohne** Kind-Prozesse unter altem PID (nur Main `app.py`)

**Systemd:**

- **Aktiv:** `systemctl --user` **`dashboard.service`**  
  Unit: `~/.config/systemd/user/dashboard.service`  
  `WorkingDirectory=.../spread_recovery_hedge_short_dev/dashboard`  
  `ExecStart=.../spread_recovery_hedge_short_dev/.venv/bin/python app.py`
- Vorher: User-Unit **inactive**, Prozess 1616938 lief **ohne** Unit (PPID 1, Code vom 25.11.).
- `/etc/systemd/system/dashboard.service` zeigt dieselbe Spread-Recovery-CWD (system scope).
- Repo-`dashboard/dashboard.service` verweist auf **burn_reentry_simple** — **nicht** der laufende Stack.

---

## 2. Dateiquellen (Projekt)

Alle P4B-Artefakte liegen unter:

`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/`

| Datei | Mtime (lokal) | Auslieferung |
|-------|----------------|--------------|
| `static/js/research/research_charts.js` | 2026-11-26 ~10:11 | HTTP 200, enthält `mc-load-p4b-v1` |
| `static/research_trp/chart.js` | 2026-11-26 ~10:10 | `pane.html` referenziert `chart.js?v=mc-load-p4b-v1` |
| `research_charts/service.py` | 2026-11-26 ~10:11 | nach Neustart im API-Prozess |
| `templates/research_charts.html` | 2026-11-26 ~10:11 | `research-mc-load-p4b-v1` / `mc-load-p4b-v1` |

**On-Disk-Check:** `_tail_limit_candles`, `cache_tail_trimmed` in `service.py` — **vorhanden**.

---

## 3. Durchgeführter Neustart (nur Dashboard)

1. `SIGTERM` an PID **1616938** (verwaister `app.py` auf :3000).
2. `systemctl --user start dashboard.service`.
3. Neuer Listener: PID **1908793**, `Active: active`.

**Nicht** neu gestartet: Trading-Bots, Collector, Queue-Consumer, nginx, ClickHouse, MySQL.

---

## 4. Backend Cache-Tail-Trim (live API)

**Vor Neustart** (PID 1616938):

- Authentifizierter `POST /api/research/pane` (SOLUSDT 5m, `limit=200`):  
  `cache=miss`, **`candles_returned` / `cache_tail_trimmed` fehlen** → alter Python-Code im Speicher.

**Nach Neustart** (PID 1908793):

| Request | cache | limit | `len(candles)` | `candles_returned` | `cache_tail_trimmed` | `cache_stored_count` |
|---------|-------|-------|----------------|--------------------|----------------------|----------------------|
| 1. POST | miss | 24192 | **24192** | — | — | — |
| 2. POST (gleicher Key) | hit | 24192 | **24192** | **24192** | false | 24192 |

→ Cache-Hit liefert **24192** Kerzen (kein 66804 in diesem Test).  
→ P4B-Metadaten auf Hit-Pfad **aktiv**.  
`cache_tail_trimmed: false`, weil Eintrag bereits korrekte Länge hatte.

*(Miss-Pfad setzt `candles_returned` noch nicht — nur Hit/`_packed_from_cached`.)*

---

## 5. Asset-Versionen (HTTP :3000)

| Asset | Erwartung | Ergebnis |
|-------|-----------|----------|
| `research_charts.js` | `mc-load-p4b-v1` | `MC_LOAD_P3_ASSET = "mc-load-p4b-v1"` ✓ |
| `pane.html` → `chart.js` | `?v=mc-load-p4b-v1` | ✓ |
| Template (Host) | `research-mc-load-p4b-v1` | in `templates/research_charts.html` ✓ (Browser: Hard-Reload `/live-charts/research`) |

---

## 6. Browser-Test-Vorbereitung (manuell)

1. **Hard-Reload** Research-Host + Pane-Iframe (Query-Strings oben).
2. Optional: `localStorage` Diagnose `mc.load.diag=1`.
3. **Kein PASS** aus dieser Deployment-Session — nur Infrastruktur/ API verifiziert.

---

## 7. Offene Hinweise

- Alter Prozess lief **23h ohne** Code-Reload trotz geänderter `service.py` → **Neustart war nötig** für Cache-Trim.
- `/live-charts/research` erfordert Session (401 ohne Cookie) — Deployment-Checks via gespeicherte `sessions.json` **lokal**, Session-ID **nicht** in diesem Log.