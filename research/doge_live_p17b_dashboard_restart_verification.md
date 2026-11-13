# DOGE-LIVE-P17B — Dashboard-Fix aktivieren & verifizieren

**Messzeitpunkt (UTC):** 2026-11-13 **08:50–09:02** (Phase D Watch bis **~08:59**)  
**Verdikt:** **`P17B_PASS`** (Backend + Live-Tail-Watch) · Browser **UNVERIFIED**  
**Artefakte:** [`doge_live_p17b_tail_watch_v1.json`](doge_live_p17b_tail_watch_v1.json)

---

## Phase A — Sicherheitsprüfung

| Prüfpunkt | Ergebnis |
|-----------|----------|
| Port **3000** (vorher) | **PID 2288268** — `…/spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| CWD | `…/spread_recovery_hedge_short_dev/dashboard` |
| `dashboard.service` | **inactive** (manueller Prozess); Unit-Datei identisch mit `ExecStart` oben |
| Andere Services | Publisher/Collector **unberührt** |
| Git-Diff (Spread P17) | `historical_pool_state_v1.py` (+2), `pd1_exact_origin_density_research_v1.js`, `research_charts.html` |
| Syntax | **py_compile OK**, **node --check OK** |

---

## Phase B — Kontrollierter Neustart

| | |
|--|--|
| **Vorherige PID** | **2288268** (SIGTERM, Port frei nach ~2 s) |
| **Neustart** | `systemctl --user start dashboard.service` |
| **Neue PID** | **2316538** |
| **Port 3000** | **ein** Listener (`python`) |
| **Service-Status** | **active** |
| **HTTP** | Journal: Uvicorn **0.0.0.0:3000**; `/api/logs/changed` **200** |
| **P17 Backend** | `doge_5m_pd1_viewer_bounds` enthält Cache-Pop (Quelltext auf Disk) |
| **P17 JS ausgeliefert** | Statisch: `?v=pd1-p17-live-bounds-refresh-v1` — enthält `DOGE_5M_BOUNDS_REFRESH_MS`, `refreshDoge5mViewerBoundsIfStale` |
| **Research-HTML** | **401** ohne Auth — Template auf Disk referenziert **p17** Cache-Bust |

---

## Phase C — Backend-Verifikation @ 08:51:33 UTC

| Größe | UTC-Wert | Semantik |
|-------|----------|----------|
| Letzte geschlossene **5m-Open** (CH) | **2026-11-13T08:50:00Z** | Kerze |
| **Publisher** `max_decision_close` | **2026-11-13T08:45:00Z** | `decision_close` = Bucket-Open |
| **Bounds** `data_available_to` | **2026-11-13T08:45:00Z** | validierter Publish-Tail |
| **Window-API** `to_utc` | **2026-11-13T08:45:00Z** | 290 Pools |
| **Parität** | **`Bounds-Tail == Publisher-Tail`** | **PASS** |

Gap Kerze vs. Publish: **1 × 5m** (Forming/Design), nicht stale Cache.

---

## Phase D — Automatische Tail-Aktualisierung (ein Prozess, kein Neustart)

Watch ~**11 min**, **25 s** Poll, `doge_5m_pd1_viewer_bounds()` in **dieselbem** Python-Prozess (Dashboard-P17-Semantik):

| Metrik | Wert |
|--------|------|
| Start Publish / Bounds | **08:45:00Z** / **08:45:00Z** |
| End Publish / Bounds | **08:55:00Z** / **08:55:00Z** |
| Neue Publishes beobachtet | **2** (→ **08:50Z**, **08:55Z**) |
| Alle Samples `bounds == pub` | **true** |
| **phase_d_pass** | **true** |

Kein erneutes Einfrieren des Bounds-Tails im Langläufer.

---

## Phase E — Browser

| Check | Status |
|-------|--------|
| `pd1-p17-live-bounds-refresh-v1` im ausgelieferten JS | **PASS** (öffentlicher Static-Pfad) |
| Authentifizierter Nutzer-Trace `scope_bounds.hi` | **UNVERIFIED** |

**Nutzer:** Hard-Refresh auf `/live-charts/research` mit eingeloggtem Session; Trace erwarten: `scope_bounds.hi` ≈ aktueller Publish-Tail, optional `loadCaps` mit `doge_bounds_refresh=true`.

---

## Geänderte Dateien (weiterhin uncommitted)

- `spread…/historical_pool_state_v1.py`
- `spread…/pd1_exact_origin_density_research_v1.js`
- `spread…/templates/research_charts.html`
- Mirror in `pools+ob+delta_bot/backtester/short/…`

---

## Verbleibende Punkte

- Browser-PASS nach Hard-Refresh offen.
- `/live-charts/research` weiterhin **auth-pflichtig** für Abnahme.
- Kein Commit/Push in diesem Lauf.

**STOP**
