# PHASE 6XT – 5M PD1 DASHBOARD ACTIVATION V1

**Date:** 2026-10-21  
**Verdict:** **`5M_PD1_DASHBOARD_ACTIVATED_PASS`** (API + static assets; manual browser soak **ausstehend**)

---

## 1. Laufende Instanz (vor Restart)

| Item | Wert |
|------|------|
| Prozess | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| PID (vor Restart) | `1171055` |
| Gestartet | 2026-10-20 16:30:39 +0300 (~18.5h uptime) |
| CWD | `spread_recovery_hedge_short_dev/dashboard` |
| Port | **3000** (`0.0.0.0`, `DASHBOARD_PORT` default) |
| PYTHONPATH | `spread_recovery_hedge_short_dev` → `…/dashboard` → `pools+ob+delta_bot` → `trading_research_platform` |
| Env | `HISTORICAL_LMV2_BATCH_READER=1` |
| Research-Chart | `/live-charts/research` (Spread-Dashboard, nicht separater Dienst) |
| 15m PD1 Density (vor Reload) | **Ja** — Window-API Default 97 Preise Sep-Regression, Phase-5S aktiv |
| Weitere Dienste | **Port 8080** (HTTP 301, nicht neu gestartet); Hedge-Bots/Importer **nicht** angefasst |

**Abweichung (kritisch, behoben):** Nach `import research_charts.api` lädt Python `pd1_landscape_viewer_v1` aus dem **Shadow-Pfad**  
`pools+ob+delta_bot/backtester/short/dashboard/pd1_landscape_viewer_v1/` (alter Stand), **nicht** aus `spread_recovery_hedge_short_dev/dashboard/pd1_landscape_viewer_v1/`.  
Die Spread-Integration war auf Disk korrekt, aber die laufende API nutzte die Pools-Kopie → `timeframe=5m` wirkte nicht.

**Freigabe-Kriterium:** Gleicher Research-Chart-Stack; Abweichung dokumentiert und durch Sync der Runtime-Kopie geschlossen.

---

## 2. Baseline (vor Aktivierung)

Artefakte: `logs/historical_import/overnight_2026/phase6xt_5m_pd1_dashboard_activation_v1/`

| Artefakt | Inhalt |
|----------|--------|
| `probe_pre_restart.json` | Capabilities ohne `supported_viewer_scopes`; 5m-Request lieferte fälschlich `…15mpd1_v1` |
| `baseline_15m_window_summary.json` | 97 Preise, 182 Pools, Hash `4aeb36fe…` |
| `baseline_15m_window_prices_sha256.txt` | `ad3adb8eab932dafef90762094ae4188d150a8c5183f1a9c6c42d1c13af0cbdf` |
| `baseline_window_harness.json` | JS Window-Planner-Referenz (`phase6xt-density-min-depth-slider-v1`) |
| `process_baseline.json` | Startkommando + Rollback-Hinweis |

**Rollback:** Prozess per TERM beenden; `process_baseline.json` Startkommando; optional Pools-`pd1_landscape_viewer_v1` auf vorherigen Stand zurück (Git/Backup); keine CH/Publish-Änderungen.

---

## 3. Kontrollierter Reload

1. Einzelnen Listener auf **:3000** identifiziert und per **SIGTERM** beendet (keine zweite Instanz).  
2. Gleiches Startkommando wie in `process_baseline.json` (nohup `app.py`, Log `dashboard/logs/dashboard.log`).  
3. **Runtime-Sync:** `rsync` Spread → `pools+ob+delta_bot/backtester/short/dashboard/pd1_landscape_viewer_v1/` (aktive Import-Quelle).  
4. `historical_lmv2_pd_contract_v1.py` in Spread wiederhergestellt/5m-fähig (kurzzeitiger Ausfall nach versehentlichem Löschen während Diagnose — behoben vor finalem Reload).

**Keine** CH-Writes, Publishes, zusätzlichen Dashboard-Instanzen oder Neustarts anderer Dienste.

**PID nach finalem Reload:** neuer `python app.py` auf Port 3000 (siehe `ss -tlnp`).

---

## 4. Window-API-Validierung (live, authentifiziert)

Quelle: `probe_post_restart.json` + Hash-Check

| Test | Ergebnis |
|------|----------|
| Default (ohne `timeframe`) | 15m PD1, 97 Preise, Line `…15mpd1_v1` |
| `timeframe=15m`, `pool_detail=1` | unverändert (97 / 182 Pools) |
| `timeframe=5m`, `pool_detail=1` Pilot 2026-09-16…17 | **576** Preise, Line `…5mpd1_v1`, `membership_slot_minutes=5`, Kontext `2026-09-09T00:00:00Z` |
| `timeframe=5m`, `pool_detail=5` | **422** `INVALID_SCOPE` |
| Capabilities | `supported_viewer_scopes`: **2** (15m/5m PD1) |
| 15m Preis-Hash vs Baseline | **MATCH** (`ad3adb8e…`) |

---

## 5. Sichtprüfung Research-Chart

| Check | Status |
|-------|--------|
| Pane 5m + Membership 5m PD1 | **Manuell ausstehend** (Login `/live-charts/research`, DOGEUSDT) |
| Pane 15m + Membership 15m PD1 | **Manuell ausstehend** |
| Farben Upper/Lower, Min-Depth, 48h-Tiles, Umschaltung ohne Tile-Mix | **Manuell ausstehend** |
| Static JS (Membership-Select, scope caps) | **PASS** — served + on-disk (`probe_post_restart.json`) |

API und Assets sind aktiv; visuelle Canvas/Density-Soak vom Betreiber empfohlen.

---

## 6. Betriebshinweise

- **Zukünftige Viewer-Änderungen:** immer **beide** Pfade synchron halten oder Import-Pfad bereinigen:  
  - Spread: `spread_recovery_hedge_short_dev/dashboard/pd1_landscape_viewer_v1/`  
  - Runtime: `pools+ob+delta_bot/backtester/short/dashboard/pd1_landscape_viewer_v1/`
- Research-URL: `http://<host>:3000/live-charts/research`
- Keine weiteren Timeframes/PDs aktiviert.

---

## 7. Verdict

**`5M_PD1_DASHBOARD_ACTIVATED_PASS`**

- Kontrollierter Reload der **einzigen** Produktionsinstanz auf Port **3000**  
- 5m/15m Window-API verifiziert  
- 15m-Regression (97 Preise + Hash) unverändert  
- Manuelle Browser-Sichtprüfung **offen** (nicht blockierend für API-Freigabe)

**STOP**
