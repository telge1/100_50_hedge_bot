# PHASE 6XT – 5M PD1 / PD5 / PD10 EXACT ORIGIN-DENSITY MULTI-PD INTEGRATION V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_MULTI_PD_DENSITY_READY_FOR_DASHBOARD_APPROVAL`**

Anbindung der publizierten **5m PD5/PD10**-Lines an die bestehende Exact Unique-Origin-Density (Research Chart, Pane-`tf`, Checkboxen). Keine CH-Writes, kein Dashboard-Restart in dieser Phase.

**Kanonische Runtime-Quelle:** `spread_recovery_hedge_short_dev/dashboard` (Port 3000 lokal; `dash.immotel.de:8080` → Proxy, siehe [`PHASE_6XT_DASHBOARD_8080_ROUTING_VERIFICATION_V1.md`](PHASE_6XT_DASHBOARD_8080_ROUTING_VERIFICATION_V1.md)).

---

## 1. Geänderte Dateien (vorher / nachher)

| Datei | Änderung |
|-------|----------|
| `research_charts/historical_lmv2_pd_contract_v1.py` | `ALLOWED_VIEWER_SCOPES` += `("5m", 5)`, `("5m", 10)`; `_5M_LINE_BY_POOL_DETAIL`; `resolve_historical_pd_config` liest 5m-Lines pro PD |
| `static/js/research/pd1_exact_origin_density_research_v1.js` | `RELEASED_VIEWER_SCOPES` 5m PD5/10; PD5/10-Checkboxen aktiv auf 5m; **pro-Scope** Window-Cache + sequenzielle Fetches; Segment-Komposition per PD (`concat` nach `mergeExactOriginSegments` **innerhalb** jeder PD, keine `mergeDensityPools` über PDs) |
| `research_charts/tests/test_historical_lmv2_pd_contract_v1.py` | 5m PD5/10 erlaubt; 15m PD5 weiterhin `INVALID_SCOPE` |
| `test_5m_multi_pd_density_integration_v1.py` | **neu** — API-Parity PD1/5/10 + JS-Struktur |
| `test_density_pd_checkbox_ui_v1.py` | VERSION / Release-Matrix |
| `test_pd1_5m_viewer_integration_v1.py` | Invalid-Scope-Test → 15m PD5 (5m PD5 jetzt freigegeben) |
| `test_pd1_density_window_422_hotfix_v1.py` | VERSION-String |

**Unverändert:** Canvas-Engine, Origin-Sweep (`poolsToExactOriginZones`), Density-Buckets, `pd1_exact_density_trp_adapter.js` (außer Wiederverwendung), Historical LLD V2 Pools, ClickHouse-Daten, 15m SSOT.

---

## 2. Wiederverwendete Kette (15m-Referenz)

```
Checkbox activeScopesForPane(pane.tf × selected PDs)
  → pro Scope: POST /live-charts/viewer/api/window { from_utc, to_utc, timeframe, pool_detail }
  → resolve_viewer_historical_scope → resolve_historical_pd_config (eigene Publish-Line)
  → build_landscape_window (Phase-5S batch reader, 168h Kontext, 5m-Preise)
  → PD1ExactDensityTrpAdapter: planExactOriginLoadWindows(scopeKey), origin sweep, buckets, canvas/TRP
```

| Scope | `pipeline_line_id` |
|-------|-------------------|
| 15m PD1 | `…_15mpd1_v1` |
| 5m PD1 | `…_5mpd1_v1` |
| 5m PD5 | `…_5mpd5_v1` |
| 5m PD10 | `…_5mpd10_v1` |

---

## 3. API-Parity (Pilotfenster `2026-09-16` … `2026-09-17` 23:55Z)

| PD | Preise | Slot | Kontext ab | Line |
|----|-------:|-----:|------------|------|
| 1 | **576** | 5 | `2026-09-09T00:00:00Z` | `…_5mpd1_v1` |
| 5 | **576** | 5 | `2026-09-09T00:00:00Z` | `…_5mpd5_v1` |
| 10 | **576** | 5 | `2026-09-09T00:00:00Z` | `…_5mpd10_v1` |

**15m-Regression:** `2026-09-14T21:45:00Z` … `05:45:00Z` → **97** Preise, Line `…_15mpd1_v1`, `config_hash` unverändert, Phase-5S aktiv.

---

## 4. Multi-PD-Komposition (ohne Density-Mischung)

- Jede aktive PD: eigener `exactOriginWindowStores[tf\x1fpd]`, eigener `scopeKey` in Tile-/Window-Cache-Keys, eigener API-`pool_detail`.
- `gatherPoolsFromStore` + `mergeDensityPools` nur **innerhalb** eines Scopes (mehrere Zeitfenster-Tiles derselben Line).
- Darstellung: pro PD `poolsToExactOriginZones` → cull → `mergeExactOriginSegments`; Ergebnis-Listen **concat** (keine globale Origin-Deduplikation über PDs).
- Keine Auswahl → kein Density (bestehendes Verhalten).
- `loadSerial` / `replayGen` / `exactOriginScopeKeyForPane` verhindern stale Overwrites bei Checkbox- oder TF-Wechsel.

---

## 5. UI / Checkboxen

- **15m-Pane:** nur **PD1** freigegeben (PD5/PD10 disabled).
- **5m-Pane:** **PD1** (default checked), **PD5**, **PD10** ankreuzbar; alle 7 nichtleeren Kombinationen + „keine PD“ über JS-Logik abgedeckt.
- Timeframe nur aus **Pane** (`pane.tf`), kein Extra-Dropdown.

---

## 6. Tests (isoliert, ohne Dashboard-Restart)

```text
cd spread_recovery_hedge_short_dev/dashboard
HISTORICAL_LMV2_BATCH_READER=1 pytest \
  test_5m_multi_pd_density_integration_v1.py \
  test_density_pd_checkbox_ui_v1.py \
  test_pd1_5m_viewer_integration_v1.py \
  test_pd1_density_window_422_hotfix_v1.py \
  research_charts/tests/test_historical_lmv2_pd_contract_v1.py -q

→ 25 passed
```

`window_422_harness.js`: VERSION `phase6xt-5m-multi-pd-density-v1`, 72h-Limit PASS.

**Browser-Sichtprüfung:** in dieser Phase **nicht** ausgeführt (kein Reload von :3000/:8080).

---

## 7. Kontrollierte Dashboard-Aktivierung (nach Freigabe)

1. Prozess auf kanonischem Tree (`spread_recovery_hedge_short_dev/dashboard`) — ggf. bestätigen, dass :8080 auf dieselbe Instanz zeigt.
2. **Hard-Refresh** / Cache-Buster für `pd1_exact_origin_density_research_v1.js` (`VERSION` in Datei).
3. DOGEUSDT, Pane **5m**, Exact Origin B aktiv → PD1/5/10 einzeln und kombiniert prüfen; Pane **15m** → nur PD1.
4. Optional: Shadow-Pfad `pools+ob+delta_bot/backtester/short/dashboard/research_charts/historical_lmv2_pd_contract_v1.py` ist **nicht** automatisch synchron — nur relevant, wenn API aus Pools-Tree importiert wird (kein rsync in dieser Phase).

**STOP** — Implementierung abgeschlossen; Aktivierung separat.
