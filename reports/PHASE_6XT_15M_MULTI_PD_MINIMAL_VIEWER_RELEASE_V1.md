# PHASE 6XT – 15M PD5 / PD10 MINIMAL VIEWER RELEASE V1

**Date:** 2026-10-21  
**VERDICT:** **`15M_MULTI_PD_VIEWER_READY_FOR_ACTIVATION`**

DOGEUSDT 15m PD5 und PD10 nutzen dieselbe Window-API, dieselbe Checkbox-Matrix und dieselbe Multi-PD-Komposition wie 5m PD5/PD10. Die Publish-Lines waren schon vollständig. Kein Import, keine neue Density-Rechnung, kein Dashboard-Reload.

Die laufende Instanz auf Port 3000 hat den neuen Contract noch nicht geladen. Die Freigabe liegt auf der Platte und ist testseitig geprüft. Aktivierung bleibt ein separater Reload.

---

## 1. Parallel-Agent

Vor dem Edit: keine offenen Writer auf Contract oder Exact-Origin-JS. Letzte Änderung dieser Dateien 13:46–13:48, das ist die abgeschlossene 5m-Integration. Der 5m-Aktivierungsbericht `5M_MULTI_PD_DASHBOARD_ACTIVATED_PASS` lag bereits vor. Kein zweiter Agent hat die Dateien während dieser Änderung gehalten.

---

## 2. Änderung

| Datei | Änderung |
|-------|----------|
| `spread_recovery_hedge_short_dev/dashboard/research_charts/historical_lmv2_pd_contract_v1.py` | `ALLOWED_VIEWER_SCOPES` += `("15m", 5)`, `("15m", 10)` |
| `static/js/research/pd1_exact_origin_density_research_v1.js` | dieselben Paare in `RELEASED_VIEWER_SCOPES`; VERSION `phase6xt-15m-multi-pd-viewer-v1` |
| `research_charts/tests/test_historical_lmv2_pd_contract_v1.py` | 15m PD5/PD10 lösen auf `…_15mpd5_v1` / `…_15mpd10_v1`; Sperre bleibt `4h` PD10 und `15m` PD3 |
| `test_pd1_5m_viewer_integration_v1.py` | Ungültiger Scope ist jetzt `4h` PD10 |
| `test_5m_multi_pd_density_integration_v1.py` | VERSION-String; 5m-Scopes bleiben in der Matrix |
| `test_density_pd_checkbox_ui_v1.py` | VERSION-String |
| `test_pd1_density_window_422_hotfix_v1.py` | VERSION-String |
| `test_15m_multi_pd_minimal_viewer_release_v1.py` | neu |

`resolve_historical_pd_config(5|10, timeframe="15m")` zeigte schon auf die Produktions-Lines. Der Sweep, die Canvas-Engine und `historical_window_v1.py` sind bytegleich:

| Datei | SHA-256 |
|-------|---------|
| `pd1_exact_density_trp_adapter.js` | `4b1e26ce346be96f9f8c3590816b9489e7e00af277066b6da9af9d953946cfbf` |
| `static/research_trp/chart.js` | `8ba510e87c3194bcc6bcb1bffd8f4a48eadc20c6534a05118ab0e7600a60d886` |
| `pd1_landscape_viewer_v1/historical_window_v1.py` | `4e12bede667b36dc5a80422c42488b089099cea07726bbe6e02682a4d25b8dbc` |

5m-Scopes `("5m", 1|5|10)` sind unverändert in beiden Matrizen. Kein zusätzliches Timeframe-Dropdown. Checkboxen bleiben `researchExactOriginPoolDetail`; `syncPoolDetailUi` schaltet sie über `pane.tf` × Release-Matrix.

---

## 3. Referenzfenster

`2026-09-16T00:00:00Z` … `2026-09-17T23:45:00Z`, Kontext ab `2026-09-09T00:00:00Z`.

| PD | Preise (5m-Achse) | Membership sichtbar | Vorlauf 168h | Geladen | Slot | Line | `active_state_fingerprint` am 16.09. 00:00Z |
|----|------------------:|--------------------:|-------------:|--------:|-----:|------|-----------------------------------------------|
| 1 | **574** | **192** | **672** | **864** | 15 | `…_15mpd1_v1` | `50e16fc8ac56e3468413379e292b58b548e6108c730d4c419aba00440880e457` |
| 5 | **574** | **192** | **672** | **864** | 15 | `…_15mpd5_v1` | `4ef7e7333f080cba162accb5645225806d94c0a79c6f1adfc219a9ecbad33876` |
| 10 | **574** | **192** | **672** | **864** | 15 | `…_15mpd10_v1` | `ffd67753c8d8fc9b26362aa462f5c264b607e2f3a0f4cc8e00c9c6b8d08b3008` |

574 ist die 5m-Preisachse. 192 ist die 15m-Mitgliedschaft. Die Fingerprints sind die Original-JSON-Werte aus dem Readiness-Audit und kommen unverändert aus dem Publish.

Bounds am Fensteranfang: jede ACTIVE-Zeile der Line trifft `bottom`/`top` des Window-Pools (PD1 148/148, PD5 151/151, PD10 188/188, Abweichung 0). Pool-IDs tragen `:15m:`. `bottom < top`. Origin gesetzt.

15m-PD1-Regression `2026-09-14T21:45:00Z` … `2026-09-15T05:45:00Z`: **97** Preise, Line `…_15mpd1_v1`, Hash `4aeb36fe…`.

Gesperrt, jeweils `INVALID_SCOPE`: `4h` PD10, `15m` PD3, `30m` PD1.

---

## 4. Multi-PD

- Acht Kombinationen je Pane-TF (leer plus sieben nichtleere Teilmengen von PD1/PD5/PD10) folgen aus derselben `activeScopesForPane`-Logik. 15m und 5m sind beide vollständig freigegeben.
- Cache- und Tile-Keys für `15m` PD1/5/10 und `5m` PD1/5/10 sind sechs verschiedene Scope-Keys, sechs Window-Keys, sechs Tile-Keys.
- Im Render-Pfad steht `mergedSegments.concat` nach dem Sweep **je** PD. `mergeDensityPools` liegt nur in `gatherPoolsFromStore` (Tiles derselben Line).
- Slot bleibt 15 auf der 15m-Pane und 5 auf der 5m-Pane.

---

## 5. Tests

```text
cd spread_recovery_hedge_short_dev/dashboard
PYTHONPATH=spread:dashboard:pools+ob+delta_bot:trading_research_platform \
HISTORICAL_LMV2_BATCH_READER=1 pytest \
  test_15m_multi_pd_minimal_viewer_release_v1.py \
  test_5m_multi_pd_density_integration_v1.py \
  test_density_pd_checkbox_ui_v1.py \
  test_pd1_5m_viewer_integration_v1.py \
  test_pd1_density_window_422_hotfix_v1.py \
  research_charts/tests/test_historical_lmv2_pd_contract_v1.py -q

→ 29 passed
```

Artefakt: `logs/historical_import/overnight_2026/phase6xt_15m_multi_pd_minimal_viewer_release_v1/reference_window.json`

Kein ClickHouse-Write, kein Publish, kein Import, kein Generator, kein Dashboard-Restart, kein Git-Commit. Browser-Sichtprüfung erst nach dem Reload.

---

## 6. Aktivierung (separat)

1. Kanonische Spread-Instanz neu laden (dieselbe, die 5m schon bedient).
2. Hard-Refresh. JS-VERSION ist `phase6xt-15m-multi-pd-viewer-v1`.
3. DOGEUSDT, Pane **15m**: PD1/PD5/PD10 einzeln und kombiniert. Pane **5m**: unverändert PD1/PD5/PD10.
4. Capabilities müssen sechs Scopes melden. Vorher waren es vier.

**STOP** — Viewer-Code ist freigabefertig. Live-Schaltung nicht in dieser Phase.
