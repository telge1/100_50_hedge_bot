# PHASE 6XT — 4H PD10 STRICT ORIGINAL VIEWER INTEGRATION V1

**Date:** 2026-10-21  
**VERDICT:** `4H_PD10_VIEWER_READY_FOR_ACTIVATION`

**Scope:** Minimale Exact-Unique-Origin-Density-Freigabe für **DOGEUSDT 4h PD10** auf der bestehenden Publish-Line. Kein CH-Write, kein Import, keine Generierung, **kein Dashboard-Restart** in dieser Phase.

**Kanonische Quelle:** `spread_recovery_hedge_short_dev/dashboard`

**Readback artifact:** `logs/historical_import/overnight_2026/phase6xt_4h_pd10_strict_viewer_integration_v1/window_readback.json`

---

## 1. Geänderte Dateien

| Datei | Änderung |
|-------|----------|
| `research_charts/historical_lmv2_pd_contract_v1.py` | `ALLOWED_VIEWER_SCOPES` += `("4h", 10)`; `membership_slot_minutes("4h")` → **240**; `resolve_historical_pd_config` → Line `…_4hpd10_v1` nur für PD10; 4h PD1/PD5 → `INVALID_SCOPE` |
| `research_charts/historical_pool_state_v1.py` | `include_remaining=False` für Window-Pfad (active-only); vermeidet 4h-Adapter-Remaining in Density |
| `pd1_landscape_viewer_v1/historical_window_v1.py` | `_causal_decision_close` für Slots **≥ 60 min** (4h-Raster UTC) |
| `pd1_landscape_viewer_v1/historical_window_batch_v1.py` | dieselbe 4h-Slot-Ausrichtung für Phase-5S `unique_decision_closes_between` |
| `static/js/research/pd1_exact_origin_density_research_v1.js` | `RELEASED_VIEWER_SCOPES` += 4h PD10; `membershipTfSecForPane` **14400**; 4h-Pane auto-PD10 wenn keine Scope-Checkbox aktiv; `VERSION` bump |
| `static/js/research/pd1_exact_density_trp_adapter.js` | `membershipSlotMinutesForTimeframe("4h")` → **240** |
| `research_charts/tests/test_historical_lmv2_pd_contract_v1.py` | 4h PD10-Line + Reject 4h PD1/5 |
| `test_4h_pd10_strict_viewer_integration_v1.py` | **neu** — Referenzfenster + Regression |
| `test_5m_multi_pd_density_integration_v1.py` | VERSION-String |
| `test_pd1_density_window_422_hotfix_v1.py` | VERSION-String |
| `test_density_pd_checkbox_ui_v1.py` | VERSION-String |

**Nicht geändert:** Canvas/TRP-Engine, Density-Buckets, `mergeDensityPools`/`mergeExactOriginSegments`, Historical LLD V2 Pools, ClickHouse-Daten, 5m/15m Line-Mappings, `research_charts.js` Viewport-Cap-Pfad (paralleler Agent-Schutz).

---

## 2. Contract / Scope-Mapping

| Scope | `pipeline_line_id` | `config_hash` (PD10) | Membership slot |
|-------|-------------------|----------------------|-----------------|
| `("4h", 10)` | `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1` | `bd9ac4f30e177321…` | **240 min** |

Gesperrt (422 `INVALID_SCOPE`): `("4h", 1)`, `("4h", 5)`, alle nicht gelisteten Kombinationen.

**Preisachse:** weiterhin **5m**-Bars aus `ClickHouseResearchCandleSource` (getrennt von Membership).

---

## 3. Referenzfenster-Readback

| Check | Erwartet | Gemessen |
|-------|----------|----------|
| Sichtbare 4h-Closes | 12 | **12** |
| Warmup-Closes (168h) | 42 | **42** |
| Gesamt Kontext+Sicht | 54 | **54** |
| `context_from` | `2026-09-09T00:00:00Z` | **match** |
| `membership_slot_minutes` | 240 | **240** |
| Publish-Line | `…_4hpd10_v1` | **match** |
| 5m-Preiszeilen im Payload | >0 | **529** |
| Pools mit `segments` + `origin` | >0 | **78** |
| Phase-5S batch reader | aktiv | **true** |

Fingerprint: `config_hash` = Phase-5V PD10 / CH publish (unverändert).

---

## 4. JS / Python-Parität

| Layer | 4h membership |
|-------|----------------|
| Python `membership_slot_minutes` | 240 |
| JS `membershipSlotMinutesForTimeframe` | 240 |
| JS `membershipTfSecForPane` fallback | 14400 |

Checkbox-Matrix: auf **4h-Pane** nur **PD10** in `RELEASED_VIEWER_SCOPES`; PD1/PD5 disabled; bei leerer Auswahl wird PD10 auf 4h-Pane gesetzt.

---

## 5. Regression (pytest)

```text
cd spread_recovery_hedge_short_dev/dashboard
HISTORICAL_LMV2_BATCH_READER=1 pytest \
  test_4h_pd10_strict_viewer_integration_v1.py \
  research_charts/tests/test_historical_lmv2_pd_contract_v1.py \
  test_5m_multi_pd_density_integration_v1.py \
  test_pd1_density_window_422_hotfix_v1.py \
  test_density_pd_checkbox_ui_v1.py -q

→ 26 passed
```

- **5m** PD1/PD5/PD10 Pilotfenster: unverändert (576 Preise, Lines, Kontext Sep 9).
- **15m** PD1: **97** Preise, Hash `4aeb36fe…`, Line `…_15mpd1_v1`.
- **4h** PD1/PD5: `LandscapeWindowError` / `INVALID_SCOPE`.

---

## 6. 48h-Tile-Hinweis

Fixed **48h UTC** Tiles (`phase6xt_eod_fixed_tile_48h_v1`) chunken weiterhin **API-Zeit** auf der **5m-Preisachse**; Membership-Entscheidungen folgen **240-Minuten**-Slots. Keine neue Tile-Engine — bei sehr langen 4h-Viewport-Spans gilt unverändert das **72h** API-Sichtlimit (`MAX_EXPORT_HOURS` / `MAX_API_VISIBLE_MS`).

---

## 7. Aktivierung (nach Operator-Freigabe, nicht in dieser Phase)

1. Bestätigen, dass :8080 / :3000 die **Spread**-Instanz mit diesen Dateien lädt.
2. **Hard-Refresh** / Cache-Buster für `pd1_exact_origin_density_research_v1.js` (`VERSION=phase6xt-4h-pd10-strict-viewer-v1`).
3. Research Chart → Pane **4h** → Exact Origin Density → nur **PD10** → Fenster Sep 16–17 (Sichtprüfung).
4. Optional: `GET /live-charts/viewer/api/capabilities` — `supported_viewer_scopes` enthält `4h`/10.

**STOP** — Integration abgeschlossen; kein Prozess-Restart, kein Git-Push in dieser Phase.
