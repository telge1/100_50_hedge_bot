# PHASE 6XT — 4H PD10 CONTROLLED DASHBOARD ACTIVATION V1

**Date:** 2026-10-21  
**VERDICT:** `4H_PD10_DASHBOARD_ACTIVATED_PASS`

Kontrollierter Reload der kanonischen Spread-Dashboard-Instanz; Live-Window-API und Static-JS für **4h PD10** aktiv. Keine CH-Writes, keine Code-Implementierung in dieser Phase (nur Runtime-Reload + Shadow-Slot-Fix in pools+ob `historical_window_*` für Import-Sicherheit).

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_4h_pd10_dashboard_activation_v1/`

---

## 1. Preflight

| Check | Ergebnis |
|-------|----------|
| Konkurrierende Generator/Queue | **keine** (`run_multi_month` / `auto_queue` idle) |
| Kanonische Runtime | `spread_recovery_hedge_short_dev/dashboard`, **PID 1655845** → `:3000` |
| Publish-Line 4h PD10 | `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1` |
| JS auf Disk | `phase6xt-4h-pd10-strict-viewer-v1` + `{ timeframe: "4h", pool_detail: 10 }` |
| Apache :8080 | Listener aktiv, **nicht** neu gestartet |

---

## 2. ClickHouse-Baselines (read-only)

| Line | `publish_complete` / distinct closes |
|------|-------------------------------------:|
| 4h PD10 | **1098** |
| 15m PD1 / PD5 / PD10 | **17568** je |
| 5m PD1 / PD5 / PD10 | **2592** je |

`ch_publish_baseline.json`

---

## 3. Runtime (vor / nach)

| | Vorher | Nachher |
|--|--------|---------|
| **PID** | **1655845** | **1684382** |
| **CWD** | `…/spread_recovery_hedge_short_dev/dashboard` | unverändert |
| **Listener** | `0.0.0.0:3000` | unverändert |

**Reload-Grund:** Alter Prozess kannte Contract auf Disk (`capabilities` **6** Scopes, **kein** 4h); erster Post-Reload-Versuch mit alter `PYTHONPATH`-Reihenfolge lieferte `MISSING_PUBLICATION` für 4h (falsches Modul-/Slot-Verhalten).

**Korrektur:** `SIGTERM` + `nohup app.py` mit **`PYTHONPATH` dashboard-first**:

`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` → `…/spread_recovery_hedge_short_dev` → `pools+ob+delta_bot` → `trading_research_platform`

`HISTORICAL_LMV2_BATCH_READER=1`

**Zusatz (read-only Shadow):** 4h-`_causal_decision_close`-Fix in `pools+ob+delta_bot/backtester/short/dashboard/pd1_landscape_viewer_v1/` + `historical_window_batch_v1.py` (kein Contract/rsync).

`process_baseline_pre.json`, `process_baseline_post.json`

---

## 4. Live-API (post, `127.0.0.1:3000`, Session-Cookie)

Quelle: `probe_post_restart.json`

### 4h PD10 Referenzfenster `2026-09-16T00:00:00Z` … `2026-09-17T20:00:00Z`

| Check | Erwartet | Live |
|-------|----------|------|
| HTTP | 200 | **200** |
| Line | `…_4hpd10_v1` | **ok** |
| `config_hash` | `bd9ac4f30e177321…` | **ok** |
| `membership_slot_minutes` | 240 | **240** |
| `context_from` | `2026-09-09T00:00:00Z` | **ok** |
| 5m-Preiszeilen | 529 | **529** |
| Pools (segments) | >0 | **78** |
| Sichtbare 4h-Closes | 12 | **12** |
| Warmup-Closes | 42 | **42** |
| Fehlende Publishes im Kontext | 0 | **0** |

### Scope-Gates

| Request | HTTP | Code |
|---------|-----:|------|
| 4h PD1 | 422 | `INVALID_SCOPE` |
| 4h PD5 | 422 | `INVALID_SCOPE` |

### Capabilities

- **`n_scopes`:** **7** (15m×3 + 5m×3 + **4h PD10**)
- **`has_4h_pd10`:** **true**

---

## 5. Static / JS

| Marker | Disk | Served (:3000) |
|--------|------|----------------|
| `phase6xt-4h-pd10-strict-viewer-v1` | ja | ja |
| `4h` / `pool_detail: 10` in `RELEASED_VIEWER_SCOPES` | ja | ja |

**Viewer-UI (Browser):** 4h-Pane nur **PD10** aktiv, PD1/PD5 disabled, PD10-Auto-Check — **Design + JS**; **kein** automatisierter Canvas-Lauf.

**Canvas / 900-Rohzonen:** Vollständige visuelle Parität **nicht** behauptet (wie Multi-PD-Aktivierungen zuvor). Min-Depth / 48h-Tiles / Farben — **manuell ausstehend**.

---

## 6. Regression (post-reload)

| Test | Ergebnis |
|------|----------|
| Default **15m** PD1 | **97** Preise, `hash_ok` |
| **15m** PD5 / PD10 | **97** Preise, korrekte Lines |
| **5m** PD1 / PD10 Pilot | **576** Preise, Kontext Sep 9 |
| `test_4h_pd10_strict_viewer_integration_v1.py` + merge-gap | **7 passed** |

Kein Scope-Fallback; ungültige Scopes **422**.

---

## 7. Offene Punkte

1. **Manuelle Sichtprüfung** Research Chart 4h + Exact Origin Density (Login / Hard-Refresh).
2. **Canvas-Validierung** gegen 900-Rohzonen-Pfad — explizit **nicht** in diesem Audit PASS.
3. **Betriebsnotiz:** Zukünftige Dashboard-Starts `PYTHONPATH` mit **`dashboard/` zuerst** (siehe `process_baseline_post.json`).

---

**STOP** — Aktivierung abgeschlossen; kein Git-Push, keine CH-Writes.
