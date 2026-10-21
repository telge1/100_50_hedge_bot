# PHASE 6XT – HISTORICAL DENSITY PD CHECKBOX UI V1

**Date:** 2026-10-21  
**Verdict:** **`DENSITY_PD_CHECKBOX_UI_READY_FOR_ACTIVATION`**

Kein Dashboard-Reload auf Port 3000/8080 in dieser Phase. Keine CH-Writes/Publishes.

---

## 1. Runtime-Zuordnung (Port 3000 vs 8080)

| Endpoint (lokal geprüft) | Beobachtung |
|--------------------------|-------------|
| `127.0.0.1:3000` | **Spread-Dashboard** — `python app.py`, CWD `spread_recovery_hedge_short_dev/dashboard`, Research unter `/live-charts/research` |
| `127.0.0.1:8080` | **Apache/WordPress** (HTTP 301), **nicht** die Spread-FastAPI-Instanz |

**`dash.immotel.de:8080/live-charts/research`:** Sehr wahrscheinlich Reverse-Proxy/VHost auf dieselbe Research-App; lokal ist 8080 ≠ Python-Dashboard. Für Aktivierung muss geklärt werden, welcher Upstream die produktive URL bedient (typisch: Proxy → Instanz wie :3000).

**Kanonische UI-Quelle (diese Änderung):**

- `spread_recovery_hedge_short_dev/dashboard/templates/research_charts.html`
- `spread_recovery_hedge_short_dev/dashboard/static/js/research/pd1_exact_origin_density_research_v1.js`
- `spread_recovery_hedge_short_dev/dashboard/static/js/research/research_charts.js`

**Bekannter API-Shadow-Pfad (unverändert, nur Dokumentation):**

- Nach `import research_charts.api` kann `pd1_landscape_viewer_v1` aus  
  `pools+ob+delta_bot/backtester/short/dashboard/pd1_landscape_viewer_v1/` geladen werden.  
- **Diese UI-Phase ändert nur Frontend** — kein rsync, keine zweite API-Implementierung.

---

## 2. UI-Änderungen

### Entfernt
- Membership-Dropdown `researchExactOriginPd1Timeframe` („15m PD1 / 5m PD1“).

### Neu (analog Historical PD, aber getrennt)
- `#researchExactOriginPdWrap` mit **Checkboxen** `name="researchExactOriginPoolDetail"`:
  - **PD1** — aktivierbar (default checked)
  - **PD5 / PD10** — `disabled`, nicht ankreuzbar (noch nicht freigegeben)

**Unabhängig** von `name="researchHistoricalLmv2PoolDetail"` (Radio bei Historical LLD V2). Kein gegenseitiges Umschalten mehr beim Aktivieren von Exact Origin.

### Timeframe
- Ausschließlich **`pane.tf`** (Chart-Pane).
- 5m-Pane → API `timeframe=5m`, `pool_detail=1` wenn PD1 checked und freigegeben.
- 15m-Pane → `timeframe=15m`, `pool_detail=1`.
- 4h (und andere TF ohne Freigabe) → PD1-Checkbox disabled, keine Density, klarer Hint.

### Freigabe-Matrix (JS `RELEASED_VIEWER_SCOPES`)
- Freigegeben: **15m PD1**, **5m PD1**
- Nicht freigegeben: alle PD5/PD10 und 4h-Kombinationen — keine Fallbacks auf PD1 bei falschem PD.

### Architektur (Multi-PD vorbereitet, Phase nicht aktiviert)
- `selectedPoolDetails()` → Menge gewählter PDs
- `activeScopesForPane(pane)` → Schnitt mit Freigabe + Pane-TF
- Cache/Inflight-Keys: `exactOriginScopeKeyForPane` (sortierte Scope-Keys, später `+`-joined)
- **Aktuell:** nur PD1 fetchbar; PD5/PD10 UI disabled — keine Multi-PD-Darstellung

### Weitere Unverändert
- Min-Depth 1–10, Canvas/TRP, 48h-Tiles, Origin-Sweep, Farben, mutual exclusion mit Historical LLD V2

### TF-Wechsel
- `research_charts.js` ruft `PD1ExactOriginResearch.onPaneTimeframeChange(pane)` → UI sync, Cache invalidieren, kein Tile-Mix

---

## 3. Geänderte Dateien

| Datei | Änderung |
|-------|----------|
| `templates/research_charts.html` | PD-Checkboxen, Dropdown entfernt |
| `static/js/research/pd1_exact_origin_density_research_v1.js` | Pane-TF, PD-Set, VERSION `phase6xt-density-pd-checkbox-v1` |
| `static/js/research/research_charts.js` | TF-Change-Hook |
| `test_density_pd_checkbox_ui_v1.py` | **neu** |
| `test_pd1_5m_viewer_integration_v1.py` | HTML/JS Assertions |
| `test_pd1_density_window_422_hotfix_v1.py` | VERSION-String |

**Python Window-API:** unverändert (bereits parametrisiert).

---

## 4. Tests (isoliert, ohne Dashboard-Restart)

```text
cd spread_recovery_hedge_short_dev/dashboard
HISTORICAL_LMV2_BATCH_READER=1 pytest \
  test_density_pd_checkbox_ui_v1.py \
  test_pd1_5m_viewer_integration_v1.py \
  test_pd1_density_window_422_hotfix_v1.py \
  test_pd1_density_research_live_integration_v1.py -q

18 passed
```

| Prüfung | Ergebnis |
|---------|----------|
| 15m PD1 API | 97 Preise, Line `…15mpd1_v1` |
| 5m PD1 Pilot | 576 Preise, Line `…5mpd1_v1` |
| Kein Membership-Dropdown im Template | PASS |
| PD5/PD10 disabled in HTML | PASS |
| Kein `researchHistoricalLmv2PoolDetail` in Exact-Origin-JS | PASS |
| Window-422-Harness (VERSION aus JS) | PASS |

Artefakt: `logs/historical_import/overnight_2026/phase6xt_density_pd_checkbox_ui_v1/api_regression.json`

---

## 5. Aktivierung (separat freizugeben)

1. Klären: `dash.immotel.de:8080` → welcher Upstream (vermutlich Spread-Dashboard).  
2. **Nur** die kanonische Spread-Instanz neu laden (kein zweites Dashboard).  
3. Falls API-Shadow weiterhin Pools-Pfad nutzt: **nur** bei API-Änderungen synchron halten — diese Phase ist **nur Static/Template**.  
4. Manuell: Exact Origin an → PD1 an/aus; Pane 5m/15m; PD5/PD10 nicht klickbar; Umschalten TF ohne vermischte Tiles.

---

## 6. Risiken

- Produktions-8080 lokal ≠ Python; Proxy-Zuordnung vor Reload verbindlich klären.  
- Alte Browser-Cache: Hard-Refresh nach Deploy.  
- Multi-PD gleichzeitig: bewusst **nicht** in dieser Phase.

---

**STOP** — bereit zur separaten Reload-Freigabe.
