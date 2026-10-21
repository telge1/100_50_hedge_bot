# PHASE 6XT — 4H PD10 EXISTING PUBLISHED DATA VIEWER READINESS AUDIT V1

**Mode:** read-only (no CH writes, imports, publishes, code changes, dashboard restarts)

**Audit timestamp (UTC):** 2026-10-21

**VERDICT:** `4H_PD10_EXISTING_DATA_VIEWER_BLOCKED`

**Scope:** Whether the **already published** line `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1` (1098 closes) is sufficient for a **causal** Exact-Origin-Density window on the **existing** Research Chart viewer — not whether Phase-6XT overnight regeneration is complete.

**References:** `PHASE_6XT_4H_PD10_GENERATION_SERVER_STATUS_AUDIT_V1.md`, `PHASE_6XT_15M_MULTI_PD_READINESS_AUDIT_V1.md`, `PHASE_6XT_5M_MULTI_PD_DENSITY_INTEGRATION_V1.md`

**Artifacts:**

- `logs/historical_import/overnight_2026/phase6xt_4h_pd10_existing_data_viewer_readiness_audit_v1/ch_coverage_analysis.json`
- `logs/historical_import/overnight_2026/phase6xt_4h_pd10_existing_data_viewer_readiness_audit_v1/ssot_test_window.json`

**Kanonische Viewer-Runtime:** `spread_recovery_hedge_short_dev/dashboard` (wie 5m/15m Multi-PD-Integration).

---

## Executive summary

| Layer | Status |
|-------|--------|
| **Publish / CH SSOT (4h PD10 line)** | **Vollständig** für Apr 1 … Sep 30 2026 auf dem kanonischen 4h-Raster (1098/1098); Testfenster Sep 16–17 inkl. **168h**-Vorlauf lückenlos |
| **JSON-Produktion (apr-sep catalog)** | **1098** Dateien; Sidecar **1098/1098** |
| **Research Exact-Origin-Density Viewer** | **Nicht anschlussfähig** — kein Scope `("4h", 10)`, kein PD-Contract, `membership_slot_minutes` ohne 4h, Window-Loader **15m-only**, JS-Slot **5/15m only**; Spread-Tree ohne `historical_lmv2_ssot_four_h_pools_v1` |

**Schluss:** Die **Daten** reichen für das Kandidatenfenster; **Viewer-Readiness** ist durch **Architektur-/Freigabe-Gaps** blockiert, nicht durch fehlende Publishes im Fenster.

---

## 1. Vorhandene 4h-PD10-Daten (nur Publish-Line)

**Pipeline:** `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1`  
**Symbol / TF / PD:** DOGEUSDT · 4h · PD10  
**config_hash (CH):** `bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2` (Phase-5V PD10; identisch zu Overnight-Manifest)

### 1.1 Publish-Inventar (ClickHouse, SELECT)

| Metric | Value |
|--------|------:|
| `publish_complete=1` rows | **1098** |
| Distinct `decision_close` | **1098** |
| Duplicates per close | **0** |
| First close | `2026-04-01T00:00:00+00:00` |
| Last close | `2026-09-30T20:00:00+00:00` |
| Lücken vs. erwartetes 4h-Raster (00/04/08/12/16/20 UTC, Apr 1 … Sep 30 20:00) | **0 missing, 0 extra, 0 non-4h gaps** |

**Wichtig:** 1098 = **lückenloses Import-Raster** über **Apr–Sep 2026** auf dieser Line — **nicht** „jede conceivable Policy-Coverage“ (kein Publish vor `2026-04-01`; kein Oktober; keine multi_month-Overnight-Ergänzungen).

### 1.2 CH-Schichten (ACTIVE SSOT)

| Table / layer | Rows | Uniq `snapshot_id` | Notes |
|---------------|-----:|-------------------:|-------|
| `research_lmv2_active_snapshot_publish_v1` | 1098 | 1098 | `publish_complete=1` |
| `research_lmv2_active_snapshot_meta_v1` | 1098 | 1098 | aligned |
| `research_lmv2_active_snapshot_member_v1` | 81506 | 1098 | 1358 distinct `pool_id` |
| `research_lmv2_snapshot_active_pool_v1` (catalog) | 81506 | 1098 | member/catalog parity path used by 4h SSOT loader |
| `research_lmv2_snapshot_import_provenance_v1` (sidecar) | 1098 | 1098 | `pipeline_line_id` = 4h line |

### 1.3 Original-JSON (apr-sep production tree)

| Check | Result |
|-------|--------|
| `doge_apr_sep_2026_v1_history/production/results/**/4h_PD10/*.json` | **1098** files |
| Parity vs publish count | **1:1** on stem count |
| Sample stem `20260917T200000Z.json` | `decision_close` present; full snapshot payload (`pools`, `frozen_input`, fingerprints) |

Provenance lookup in production tree lists 4h_PD10 paths (Phase-5V inventory); keine erneute Berechnung in diesem Audit.

### 1.4 Dedizierte 4h-SSOT-Hilfsmodule

In **pools+ob** `backtester/short/dashboard`: `historical_lmv2_ssot_four_h_pools_v1.py` (`FOUR_H_PIPELINE_LINE_ID`, `load_ssot_legacy_pools_4h_at_close`, `inventory_four_h_source`).

In **Spread-Dashboard-Tree:** Modul **nicht vorhanden** (0 files). CH-Line ist dennoch real; Anbindung erfolgte historisch über Import/Guard-Pfade, nicht über den Multi-PD-Viewer-Contract.

---

## 2. Historischer Kontext (168h + ältere ACTIVE Origins)

**Regel:** Mindestens **168h** Membership-Vorgeschichte vor dem Untersuchungsfenster; zusätzlich alle zum jeweiligen Close noch ACTIVE gebliebenen Origins (kein willkürlicher Ersatz der Generator-Historie durch nur 168h).

**Mechanik im Viewer (bestehend):** `build_landscape_window` setzt `context_from = max(data_lo, vis_from - 168h)` und lädt Membership über **veröffentlichte Entscheidungsslots** entlang eines **5m-Preisgitters** (`WARMUP_HOURS = 168`). Die **Membership-Slot-Breite** kommt aus `membership_slot_minutes(timeframe)` (heute nur 5m/15m).

Für **reine Datenverfügbarkeit** am Kandidatenfenster:

- `data_lo` = erster Publish (`2026-04-01T00:00:00Z`) — weit vor Sep 9.
- **168h vor** `2026-09-16T00:00:00Z` → `2026-09-09T00:00:00Z`.
- **42** veröffentlichte 4h-Closes im Warmup `[Sep 9 00:00, Sep 16 00:00)` — exakt `168h / 4h`.
- **Keine fehlenden** 4h-Publishes im kombinierten Kontext+Sichtfenster `[Sep 9 00:00, Sep 17 20:00]` (54 closes).

**SSOT-Stichprobe** (`load_ssot_legacy_pools_4h_at_close` an allen 12 sichtbaren Closes): je **65–70** ACTIVE Pools, Lower **17–20**, Upper **48–50** — konsistente Membership ohne `MISSING_4H_PUBLICATION`.

**Hinweis:** Legacy-Bridge-Dicts für Guard/Backtester tragen **keine** `segments`/`origin`-Felder; der **Density-Viewer** erzeugt Origins/Segmente in `historical_window_v1._build_lifetimes` aus `load_historical_pool_state_at_close` — dieser Pfad ist für **4h heute nicht erreichbar** (siehe §4).

---

## 3. Testfenster

### 3.1 Kandidat (vorgegeben)

| Field | Value |
|-------|------:|
| Visible `from_utc` | `2026-09-16T00:00:00Z` |
| Visible `to_utc` | `2026-09-17T20:00:00Z` |
| Erwartete 4h-Membership-Closes im sichtbaren Intervall | **12** |
| Tatsächlich publiziert | **12** (alle listed in artifact) |
| Kontext-Closes (168h + sichtbar, auf Line) | **54** |
| Fehlende Closes im Kontext | **0** |

**Preisachse (für Density):** Window-API würde **5m**-Bars aus `ClickHouseResearchCandleSource` für `[context_from, vis_to]` laden (wie 5m/15m Density) — Daten technisch verfügbar; API lehnt Scope ab bevor geladen wird.

**Origins / Upper-Lower:** Am Close `2026-09-17T20:00:00Z` via dediziertem 4h-Loader: **70** Pools (**20** lower / **50** upper). Origin-Keys im Viewer wären logische Keys aus `pool_id` (`_origin_logical_v2_key`), sobald der Window-Pfad 4h-Membership lädt.

### 3.2 Alternativfenster

**Nicht erforderlich** — Kandidat ist vollständig durch Publishes + 168h-Vorlauf abgedeckt. Jedes andere 2-Tages-4h-Fenster **innerhalb** Apr 1 … Sep 30 mit gleichem Vorlauf wäre analog belegbar, sofern `vis_from - 168h >= 2026-04-01T00:00:00Z`.

---

## 4. Bestehender Viewer (read-only Code / Runtime-Probes)

Runtime-Probes mit Spread-`PYTHONPATH` + `pool_pattern` (kanonische Abhängigkeiten):

| Probe | Result |
|-------|--------|
| `ALLOWED_VIEWER_SCOPES` | `15m×{1,5,10}`, `5m×{1,5,10}` — **kein** `("4h", 10)` |
| `resolve_viewer_historical_scope(timeframe="4h", pool_detail=10)` | **`INVALID_SCOPE`** |
| `membership_slot_minutes("4h")` | **`ValueError`** unsupported |
| `build_landscape_window(..., timeframe="4h", pool_detail=10)` | **`INVALID_SCOPE`** (vor Datenladung) |
| `load_historical_pool_state_at_close(..., timeframe="4h", pool_detail=10)` | **`INVALID_SCOPE`** — `historical scope supports DOGEUSDT 15m only` (u.a. weil `resolve_historical_pd_config(pool_detail)` **ohne** `timeframe`-Argument 15m annimmt und TF-Mismatch wirft) |

### 4.1 Contract / API-Gaps

| Component | 4h PD10 readiness |
|-----------|-------------------|
| `historical_lmv2_pd_contract_v1.py` | **Kein** `_4H_LINE` / kein Eintrag in `ALLOWED_VIEWER_SCOPES`; `resolve_historical_pd_config` kennt nur 15m-PD5/10 und 5m-Lines |
| `membership_slot_minutes` | Nur **5** und **15** — **240** (4h) fehlt |
| `historical_window_v1.build_landscape_window` | Nutzt `resolve_viewer_historical_scope` + `membership_slot_minutes(hcfg.timeframe)` — **4h bricht ab** |
| `historical_pool_state_v1.load_historical_pool_state_at_close` | Nicht für 4h-Publish-Line verdrahtet (15m-only guard + config resolve) |
| `pd1_exact_origin_density_research_v1.js` `RELEASED_VIEWER_SCOPES` | **Kein** `{ timeframe: "4h", pool_detail: 10 }` → `syncPoolDetailUi` deaktiviert PD-Checkboxen auf **4h-Pane** |
| `pd1_exact_density_trp_adapter.js` `membershipSlotMinutesForTimeframe` | **`5m` → 5, else → 15** — 4h-Pane würde fälschlich **900s** Membership nutzen |
| `research_charts.js` | Chart-Pane **4h** mit `TF_SEC["4h"]=14400` für **Kerzen** — Density-Pfad ist separat und nicht freigegeben |
| **48h UTC tiles** (`phase6xt_eod_fixed_tile_48h_v1`) | **Wall-clock**-Chunking auf 5m-API-Fenstern — grundsätzlich **kompatibel**, sobald Window-API 4h-Membership korrekt liefert (kein Ersatz für 240min-Slots) |
| **Origin sweep** | Unverändert nutzbar **wenn** API `pools[].segments` + `origin` wie bei 5m/15m liefert — heute **kein** 4h-Window-Payload |

**14400s:** Nur in **Chart-TF**-Metadaten; Density-Membership-Sekunden kommen aus **Slot-Minuten × 60**, nicht aus `TF_SEC`.

### 4.2 Architekturzusatz

- **FOUR_H SSOT** existiert als **separates** Read-Modul (pools+ob), **nicht** in der Spread-Viewer-Kette integriert.
- 5m/15m Multi-PD-Muster (`PHASE_6XT_5M_MULTI_PD_DENSITY_INTEGRATION_V1.md`) ist die **kleinste** bekannte Integrationsform: Contract + `membership_slot_minutes` + `RELEASED_VIEWER_SCOPES` + Window-Loader/Pool-State + JS-Slot — für 4h **noch nicht umgesetzt**.

---

## 5. 255-Snapshot-Plan vs. 1098 Publishes

| Set | Count | Relation |
|-----|------:|----------|
| Phase-6XT overnight plan (`jan_sep_v1` April-head + September) | **255** unique closes | **`plan ⊆ published`** — **0** plan stems fehlen in CH |
| Published 4h PD10 closes | **1098** | **`published \ plan` = 843** |
| Davon Apr 1–10 (vor April-head-Start Apr 11) | **60** | Bereits publiziert, nicht im 255-Regen-Scope |
| Davon May–Aug (Overnight „skip complete months“) | **738** | Bereits publiziert; 255-Job würde diese **nicht** neu generieren |
| September im Plan | **180** | Subset der **180** Sep-Publishes auf Line |

**Interpretation:**

| Thema | Bedeutung für Viewer |
|-------|---------------------|
| Fehlende **Overnight-Generierung** (0/255 staging) | **Kein** zusätzlicher Publish-Bedarf für das Testfenster — Sep 16–17 bereits in CH |
| **1098 historische Coverage** | Ausreichend für Apr–Sep-Viewer-Fenster **innerhalb** Import-Bounds |
| **Lücken im gewünschten Viewer-Fenster** | **Keine** (Kandidat) — Blockade ist **Software/Freigabe**, nicht Daten |

---

## 6. Coverage-Matrix (Lifecycle)

| Stage | 4h PD10 apr-sep line | Testfenster Sep 16–17 |
|-------|----------------------|------------------------|
| GENERATED (JSON) | 1098 apr-sep production | Covered |
| IMPORTED (meta/member/catalog) | Yes | Covered |
| PUBLISHED (`publish_complete=1`) | 1098 | Covered |
| PROVENANCE (sidecar) | 1098 | Covered |
| VIEWER_CONTRACT | **Missing** | **Blocked** |
| VIEWER_READY | **No** | **Blocked** |

---

## 7. Kleinster sicherer nächster Schritt (Empfehlung — keine Ausführung)

Analog **5m Multi-PD** (`PHASE_6XT_5M_MULTI_PD_DENSITY_INTEGRATION_V1.md`), minimal:

1. **Spread** `historical_lmv2_pd_contract_v1.py`: `ALLOWED_VIEWER_SCOPES` += `("4h", 10)`; `membership_slot_minutes("4h")` → **240**; `resolve_historical_pd_config` / `resolve_viewer_historical_scope` → Line `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1` + PD10 hash.
2. **`load_historical_pool_state_at_close`:** `timeframe` an `resolve_historical_pd_config(..., timeframe=timeframe)` durchreichen; 15m-only-Guard auf **contract-backed** TF erweitern.
3. **JS:** `RELEASED_VIEWER_SCOPES` += `{4h,10}`; `membershipSlotMinutesForTimeframe("4h")` → **240**; `syncPoolDetailUi` — typisch nur **PD10** auf 4h-Pane (PD1/5 ohne Publish-Line).
4. **Tests:** API-Parity-Fenster `2026-09-16T00:00:00Z` … `2026-09-17T20:00:00Z` (erwartete Slot-Anzahl / Kontext ab `2026-09-09T00:00:00Z`).
5. **Dashboard-Freigabe** erst nach grünen Tests + kontrolliertem Reload (wie 5m Activation Report).

**Kein** neuer CH-Import nötig für dieses Fenster, sofern Line unverändert bleibt.

---

## VERDICT rationale

`4H_PD10_EXISTING_DATA_VIEWER_BLOCKED` — die **1098** publizierten Original-Snapshots bilden ein **lückenloses Apr–Sep-4h-Raster** und tragen das **Kandidatenfenster inkl. 168h-Vorlauf** vollständig; **Sidecar/Meta/Member/Catalog** sind konsistent. Der **bestehende** Exact-Origin-Density-Viewer kann **4h PD10** jedoch **nicht** laden (Scope, Contract, 240min-Membership, Pool-State-Pfad, JS-Matrix). Datenreichtum ≠ Viewer-Readiness.

**STOP** — kein weiterer Aktionsschritt in diesem Audit.
