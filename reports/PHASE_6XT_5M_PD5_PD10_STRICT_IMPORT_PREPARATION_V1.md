# PHASE 6XT – 5M PD5 / PD10 STRICT PD1 REFERENCE IMPORT PREPARATION V1

**Date:** 2026-10-21  
**Mode:** Read-only (keine CH-Writes, Publishes, Snapshot-Regeneration, Dashboard-Restarts, Git).

**VERDICT:** **`5M_PD5_PD10_READY_FOR_CONTROLLED_IMPORT_PILOT`**

Bindende Architektur: identisch zu validiertem **5m PD1** und **15m PD1** ([`PHASE_6XT_5M_STRICT_15M_REFERENCE_TRACE_V1.md`](PHASE_6XT_5M_STRICT_15M_REFERENCE_TRACE_V1.md), [`PHASE_6XT_5M_PD1_CONTROLLED_CH_IMPORT_COMPLETION_V1.md`](PHASE_6XT_5M_PD1_CONTROLLED_CH_IMPORT_COMPLETION_V1.md)). Staging-JSON sind Generator-Eingang; Chart-SSOT bleibt `doge_apr_sep_2026_v1_history/production/results` → Spread-Importer → ACTIVE → Catalog → Sidecar → Publish-Ledger.

---

## 1. Ziel-Lines & Importer-Mapping

| Combo | Folder | `StrategyCombo.pipeline_line_id()` |
|-------|--------|-------------------------------------|
| 5m PD5 | `5m_PD5` | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd5_v1` |
| 5m PD10 | `5m_PD10` | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd10_v1` |

Referenz (unverändert): **5m PD1** → `…_5mpd1_v1` (bereits **2592** Publish in CH).

**Aktueller Code-Stand (Spread-Dashboard, read-only):**

| Mechanismus | 5m PD1 | 5m PD5 / PD10 |
|-------------|--------|----------------|
| `ISOLATED_IMPORT_COMBOS` | `StrategyCombo("5m", 1)` | **nicht** eingetragen |
| `ALLOWED_VIEWER_SCOPES` | `("5m", 1)` | **nicht** freigegeben |
| `resolve_historical_pd_config(pd, timeframe="5m")` | PD1 mit eigener Line | PD5/10: Contract erlaubt nur **15m** für PD≠1 → **5m-Line-Mapping vor Viewer-Freigabe ergänzen** (gleiches Muster wie `_PD1_LINE_BY_TIMEFRAME`, ohne neue Density-Logik) |
| JS `RELEASED_VIEWER_SCOPES` | 5m+15m PD1 | PD5/PD10 Checkboxen **disabled** ([`PHASE_6XT_DENSITY_PD_CHECKBOX_UI_V1.md`](PHASE_6XT_DENSITY_PD_CHECKBOX_UI_V1.md)) |

Keine neuen DB-Tabellen. Importer-Pfad: bestehendes `historical_json_import` / `run_completion_import.py`-Muster wie 5m PD1.

---

## 2. LMV2-Config & Fingerprints (Original)

| PD | `config_hash` (Phase 5V / `identity.py`) | Symbol | TF in JSON |
|----|------------------------------------------|--------|------------|
| 5 | `533e04c7cd0e1c93540ed9466a2a5a2eb626f82b828b9a45d1da234703ebd03c` | DOGEUSDT | 5m |
| 10 | `bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2` | DOGEUSDT | 5m |

Stichprobe `20260909T000000Z.json` (Staging): `pool_detail` 5 bzw. 10, `timeframe` `5m`, `active_state_fingerprint` jeweils PD-spezifisch (≠ PD1).

**Frozen-Input (September-Segment, kausal):**

| Segment | `frozen_snapshot_id` | Autorisiert in `AUTHORIZED_FROZEN_INPUT_IDS` |
|---------|----------------------|-----------------------------------------------|
| September-Plan | `fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66` | ja (identisch 5m PD1-Import) |
| April-Head-Plan | `84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93` | ja |

Overnight-Runs **COMPLETE** (`progress.json`):

- PD5: `phase6xt_5m_pd5_20261020T210058Z` — September **8640/8640** erwartet, `last_close` `2026-09-30T23:55:00Z`
- PD10: `phase6xt_5m_pd10_20261020T233508Z` — September **8640/8640**, gleiches `fe549309…`

Gate-D: `logs/historical_import/overnight_2026/phase6xt_5m_pd5_pd10_sequential_nohup_preparation_v1/gate_d_approved_pd5.json` / `gate_d_approved_pd10.json`.

---

## 3. Dateninventar je PD

### 3.1 Catalog-Ziel `doge_apr_sep_2026_v1_history/production/results`

| Combo | Dateien gesamt | Sep 9–17 (2592) | Pilot sichtbar Sep 16–17 (576) |
|-------|----------------:|----------------:|-------------------------------:|
| **5m PD1** (Baseline) | **2592** | **2592/2592** | **576/576** |
| **5m PD5** | **0** | **0/2592** | **0/576** |
| **5m PD10** | **0** | **0/2592** | **0/576** |

→ Vor CH-Import: **verpflichtende Datei-Phase** wie 5m PD1 ([`PHASE_6XT_5M_PD1_STRICT_REFERENCE_CONTROLLED_IMPORT_V1.md`](PHASE_6XT_5M_PD1_STRICT_REFERENCE_CONTROLLED_IMPORT_V1.md)): Staging → `production/results/2026-09/{5m_PD5|5m_PD10}/`, SHA256-Parität, `provenance_lookup` / `provenance_index` — **ohne** Coverage-Matrix-Bulk-Änderung.

### 3.2 Generator-Staging (Original-JSON, read-only)

| Root | Combo | Gesamt | Sep 9–17 | Pilot 16–17 | Vollmonat Sep (Ordner) |
|------|-------|-------:|---------:|------------:|------------------------:|
| `…/staging/phase6xt_5m_pd5_jan_sep_v1/results` | 5m_PD5 | **10717** | **2592/2592**, **0 Lücken** | **576/576** | `2026-09/5m_PD5` **8640** |
| `…/staging/phase6xt_5m_pd10_jan_sep_v1/results` | 5m_PD10 | **9007** | **2592/2592**, **0 Lücken** | **576/576** | `2026-09/5m_PD10` **8640** |

Manifeste: `…/phase6xt_5m_pd5_jan_sep_v1/run/manifest.json`, `…/phase6xt_5m_pd10_jan_sep_v1/run/manifest.json` (Warmup: `rolling_history_from_close(decision_close, 4)`; Mai–Aug **regen_skip** — ältere **ACTIVE**-Origins in `doge_multi_month_2026_history/production/results` unverändert).

### 3.3 `doge_multi_month_2026_history/production/results` (ältere viermonatige Historie)

| Combo | Gesamt-JSON | Monate mit `5m_PD*` (Beispiel PD5) | **2026-09** |
|-------|------------:|-------------------------------------|-------------|
| 5m_PD5 | **39107** | 2026-04 … 2026-08 (kein Sep-Ordner) | **fehlt** (Sep nur Staging) |
| 5m_PD10 | **40817** | analog | **fehlt** |
| 5m_PD1 | 37589 | verteilt | **fehlt** (Sep nur in apr_sep nach Copy) |

**Interpretation:** Erwartbar — Pilot-Import-SSOT ist **apr_sep** + September-Staging-Slice, nicht Multi-Month-Production-September. Keine Snapshots neu berechnet.

### 3.4 Beispiel-Hashes (Pilot-Start `20260916T000000Z`)

| Quelle | SHA256 (prefix) |
|--------|-----------------|
| Staging 5m_PD5 | `a1f8ece82bf34036…` |
| Staging 5m_PD10 | `b766bd96184b0290…` |
| apr_sep 5m_PD1 (Referenz) | `484b95d1d923a199…` |

---

## 4. ClickHouse-Bestand (read-only, 2026-10-21)

`research_lmv2_active_snapshot_publish_v1` — `publish_complete` Zählung pro Line:

| Line | Publish count |
|------|---------------|
| `…_15mpd1_v1` | **17568** |
| `…_5mpd1_v1` | **2592** |
| `…_5mpd5_v1` | **0** |
| `…_5mpd10_v1` | **0** |

→ Sauberer Ledger für PD5/PD10; kein Teilpilot auf diesen Lines.

**Aktive Import-Writer:** keine `run_completion_import` / Batch-Import-Prozesse beobachtet; Lock-Datei-Design `/tmp/historical_lmv2_json_import.flock` (ein Writer).

---

## 5. Pilot-Fenster & Membership

| Anforderung | PD5 | PD10 |
|-------------|-----|------|
| Sichtbar `2026-09-16 00:00` – `2026-09-17 23:55` UTC | **576/576** (Staging) | **576/576** |
| Kontext ab `2026-09-09 00:00` (≥168 h bis Pilot-Start) | **2592/2592** im Import-Plan | **2592/2592** |
| 5-Minuten-Raster ohne Lücken (Sep 9–17) | **PASS** | **PASS** |

**Geplanter minimaler CH-Umfang pro PD:** **2592** Closes (gleiche Stem-Menge wie 5m PD1), nicht der volle September-Monat (8640).

---

## 6. Import-Safety & Reihenfolge (Freigabe-Gates)

Vor jedem Write (wie 5m PD1 Completion):

1. **Kein paralleler Writer** — `historical_import_write_lock`; PD5 und PD10 **niemals parallel**.
2. **Env-Gates:** `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`, `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`.
3. **`productive_prewrite_gate`** — Line-Isolation, keine Konflikte mit bereits publizierten `snapshot_id` auf derselben Line.
4. **Datei-SSOT:** 2592/2592 Staging ↔ apr_sep SHA256; `frozen_input_id` = `fe549309…` im Sidecar (Parität zu PD1).
5. **Batch:** `validate_batch` → `execute_batch_import` → `run_batch_readback_gate(require_sidecar=True)`; bei Fehler **STOP** (kein unsupervised Completion ohne separate Freigabe).
6. **Baselines nach jedem PD:** `…_15mpd1_v1` **17568**; `…_5mpd1_v1` **2592** unverändert.
7. **Rollback:** unpublish/ledger recovery nur nach dokumentiertem Verfahren aus 5m-PD1-Phasen; keine Überschreibung bei Hash-Mismatch.

**Verbindliche Reihenfolge:**

```
A) 5m PD5 — Datei-Copy + Provenance-Index (apr_sep)
B) 5m PD5 — CH Pilot optional (50) ODER direkt Completion (2592) nach Freigabe
C) 5m PD5 — Full-line readback + Baseline-Check
D) [Freigabe] 5m PD10 — gleiche Schritte A–C
E) Viewer — Contract + ALLOWED_VIEWER_SCOPES + JS RELEASED + Checkbox enable (Dashboard-Reload separat)
```

**Nicht in dieser Vorbereitung:** `run_completion_import` starten, Coverage-Matrix auf 56394+ erweitern ohne Test-Anpassung (5m-PD1-Muster: Disk-Assert isoliert).

---

## 7. Viewer-Freischaltungsplan (nach erfolgreichem Publish)

| Schritt | Aktion |
|---------|--------|
| 1 | `historical_lmv2_pd_contract_v1.py`: 5m-Lines für PD5/10 (analog PD1), `ALLOWED_VIEWER_SCOPES` += `("5m", 5)`, `("5m", 10)` |
| 2 | `ISOLATED_IMPORT_COMBOS` += `StrategyCombo("5m", 5)` dann später `("5m", 10)` — **nur ein Combo aktiv im Writer** |
| 3 | `pd1_exact_origin_density_research_v1.js`: `RELEASED_VIEWER_SCOPES` erweitern; PD5/PD10 `disabled` entfernen wenn Scope freigegeben |
| 4 | Timeframe **nur** `pane.tf`; 5m-Pane kann PD1+PD5+PD10 parallel (getrennte Lines, getrennte Density-Identitäten, **keine** Pool-Zusammenrechnung) |
| 5 | Pools-Shadow `pd1_landscape_viewer_v1` rsync nur bei API-Freigabe (bekanntes Split-Brain-Muster) |
| 6 | Tests: Contract + Checkbox + 5m-Viewer-Integration analog PD1 |

15m-Pane: nur bereits freigegebene 15m-PDs (aktuell PD1).

---

## 8. Referenz-Regression (unverändert, Pflicht nach Import)

| Check | Soll |
|-------|------|
| 15m PD1 publishes | **17568** |
| 15m September-Regression | **97** Preise |
| 5m PD1 publishes | **2592** |
| Canvas | Upper violett / Lower blau |
| UX | Min-Depth-Slider, Origin-Sweep, 48h UTC-Tiles, Pane-TF-Auswahl |

Neue PDs: eigene Parity-Readbacks (**2592** Stems je Line), keine Änderung an 5m/15m PD1-Fingerprints auf Referenz-Closes.

---

## 9. Multi-PD-Parity-Anforderungen

- Gleicher API-Pfad `POST /live-charts/viewer/api/window` mit `timeframe=5m` und `pool_detail` 5 bzw. 10.
- Eigene `pipeline_line_id` je PD; `config_hash` je PD fix (siehe §2).
- Gleichzeitig aktive Checkboxen: unabhängige Fetches/Cache-Keys (`exactOriginScopeKeyForPane`); Density-Rechtecke bleiben pro PD-Origin-Set.
- Keine neue Density-Formel, kein Staging-Direktzugriff im Chart.

---

## 10. BLOCKED-Kriterien (Prüfung)

| Kriterium | Ergebnis |
|-----------|----------|
| Pilot-/Kontext-Abdeckung Sep 9–17 in **Original-Staging** | **PASS** (2592/2592 je PD) |
| Kausale Frozen-ID September (`fe549309…`) in Plänen/Runs | **PASS** |
| apr_sep Catalog bereits befüllt für PD5/10 | **NEIN** — **Gate vor CH**, kein BLOCKED (gleicher Zwischenstand wie PD1 vor File-Import) |
| CH-Lines PD5/10 bereits belegt | **NEIN** (0) — gut |
| Konkurrierender Import-Writer | **nicht aktiv** |

---

## 11. Zusammenfassung

| PD | Staging Import-Window | apr_sep files | CH publish | Bereit für kontrollierten Pilot |
|----|----------------------:|--------------:|-----------:|:-------------------------------:|
| **5m PD5** | **2592/2592** | **0** → Copy aus Staging | **0** | **ja** (nach Freigabe Schritt A) |
| **5m PD10** | **2592/2592** | **0** | **0** | **ja** (nach PD5 vollständig PASS) |

**Nächster explizit freizugebender Schritt:** kontrollierte **5m PD5** Datei-Registration in `doge_apr_sep_2026_v1_history` (Byte-Parität zu `phase6xt_5m_pd5_jan_sep_v1/results/2026-09/5m_PD5/`), danach isolierter CH-Import auf `…_5mpd5_v1`.

**STOP** — keine weiteren Aktionen bis Freigabe.
