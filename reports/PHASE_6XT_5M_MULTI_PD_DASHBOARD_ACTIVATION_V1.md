# PHASE 6XT – CONTROLLED 5M MULTI-PD DASHBOARD ACTIVATION V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_MULTI_PD_DASHBOARD_ACTIVATED_PASS`**

Kontrollierter Reload der kanonischen Spread-Dashboard-Instanz; Live-Window-API und Static-JS für **5m PD1/PD5/PD10** aktiv. Keine CH-Writes.

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_multi_pd_dashboard_activation_v1/`

---

## 1. Runtime (vor / nach)

| | Vorher | Nachher |
|--|--------|---------|
| **PID** | **1596085** | **1643583** |
| **Gestartet** | 2026-10-21 11:14:17 +0300 | nach SIGTERM + nohup (~13:56) |
| **CWD** | `spread_recovery_hedge_short_dev/dashboard` | unverändert |
| **Listener** | `0.0.0.0:3000` | unverändert |
| **Apache :8080** | Proxy → `127.0.0.1:3000` | **nicht** neu gestartet |

**PYTHONPATH:** `spread_recovery_hedge_short_dev` → `…/dashboard` → `pools+ob+delta_bot` → `trading_research_platform`  
**Env:** `HISTORICAL_LMV2_BATCH_READER=1`

**Importpfade (frischer Prozess, nach `import research_charts.api`):**

- `pd1_landscape_viewer_v1` → **Spread** `…/spread_recovery_hedge_short_dev/dashboard/pd1_landscape_viewer_v1/`
- `historical_lmv2_pd_contract_v1` → **Spread** `…/dashboard/research_charts/` (4 Scopes auf Disk)

**Pools-Shadow:** `pools+ob+delta_bot/backtester/short/dashboard/research_charts/historical_lmv2_pd_contract_v1.py` **ohne** `ALLOWED_VIEWER_SCOPES` — wird durch PYTHONPATH-Reihenfolge **nicht** für die laufende API verwendet. **Kein rsync** in dieser Phase.

---

## 2. Baseline (vor Aktivierung)

| Check | Ergebnis |
|-------|----------|
| 15m PD1 Publish | **17568** |
| 5m PD1 / PD5 / PD10 | **2592** je Line |
| 15m Ref-FP | `3c6bacc2…` / `71cc7b3e…` unverändert |
| Live-API 15m Default (pre) | **97** Preise, Hash `4aeb36fe…`, Line `…_15mpd1_v1` |
| Live-API 5m PD5 (pre) | **422** `INVALID_SCOPE` (alter Contract im RAM) |
| Capabilities `n_scopes` (pre) | **2** |

`probe_pre_restart.json`, `baseline_15m_api.json`, `process_baseline.json`

---

## 3. Kontrollierte Aktivierung

1. **SIGTERM** an PID **1596085** (einziger :3000-Listener).  
2. **Gleiches Startkommando** wie PD1-Aktivierung (`nohup …/python app.py >> logs/dashboard.log`).  
3. Keine zweite Instanz, kein Apache-Restart, keine anderen Dienste.

**Grund für Reload:** Contract/JS auf Disk ab ~13:46; alter Prozess kannte **keine** `("5m", 5|10)`-Scopes.

---

## 4. Live-API (post, authentifiziert `127.0.0.1:3000`)

Quelle: `probe_post_restart.json`

| Test | HTTP | Preise | Line | Kontext | Pools (Stichprobe) |
|------|-----:|-------:|------|---------|-------------------|
| Default 15m | 200 | **97** | `…_15mpd1_v1` | 168h | 182 |
| 15m PD1 explizit | 200 | **97** | `…_15mpd1_v1` | 168h | 182 |
| 5m PD1 Pilot | 200 | **576** | `…_5mpd1_v1` | `2026-09-09T00:00:00Z` | 599 |
| 5m PD5 Pilot | 200 | **576** | `…_5mpd5_v1` | `2026-09-09T00:00:00Z` | 692 |
| 5m PD10 Pilot | 200 | **576** | `…_5mpd10_v1` | `2026-09-09T00:00:00Z` | 913 |
| 15m PD5 | **422** | — | `INVALID_SCOPE` | — | — |

- **Capabilities:** **4** `supported_viewer_scopes` (15m PD1; 5m PD1/5/10).  
- **5m-Preis-SHA** identisch über PD1/5/10 (gemeinsame 5m-Kerzen, getrennte Pool-Lines) — erwartet.  
- **15m-Regression:** 97 Preise, `config_hash` unverändert vs. Pre-Reload.

---

## 5. Static / JS-Auslieferung

| Marker | Disk | Served (:3000) |
|--------|------|----------------|
| `phase6xt-5m-multi-pd-density-v1` | ja | ja |
| `windowStoreForScope` | ja | ja |
| Research-HTML enthält PD-Checkboxen (`value="5"`, …) | — | ja (authentifiziert) |

---

## 6. Checkboxen / Multi-PD (Browser)

| Prüfung | Status |
|---------|--------|
| PD1 / PD5 / PD10 einzeln & Kombinationen | **Manuell ausstehend** (Login `http://dash.immotel.de:8080/live-charts/research`) |
| Separate Requests / Cache-Keys / keine PD-Density-Mischung | **Design + Code** (`windowStoreForScope`, per-Scope `fetchWindow`); **kein** automatisierter Canvas-Lauf |
| Min-Depth, Farben, 15m nur PD1 | **Manuell ausstehend** |

Unauthentifizierter Zugriff :8080/:3000 → **401** (Proxy identisch).

---

## 7. 15m-Regression nach Reload

- Default-API **97/97**, gleiche Line und `config_hash`.  
- Keine 5m-Scopes in 15m-Default-Request.  
- **PASS** (API-Ebene).

---

## 8. Verdict & STOP

**`5M_MULTI_PD_DASHBOARD_ACTIVATED_PASS`** — Backend + API + Static für Multi-PD freigegeben; **visuelle Soak** der Checkbox-Kombinationen durch Betreiber empfohlen.

**STOP** — keine weiteren Aktivierungen ohne neue Freigabe.
