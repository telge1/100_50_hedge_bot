# MC-BASELINE-01 — Full Working System Recovery Audit

**Endstatus:** `MC_BASELINE_01_FULL_RECOVERY_AUDIT_COMPLETE`  
**Datum:** 2026-11-26  
**Live-Service:** `dashboard.service` (user)  
**WorkingDirectory:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`

---

## Executive Summary

| Aspekt | Ergebnis |
|--------|----------|
| GitHub-Checkpoint MC-LOAD | Tag `stable-mc-load-p4j` → `eb9a8edb` (Code), Doku `2994f30a` |
| **Tatsächlicher Live-Stand** | `eb9a8edb` **plus 19 uncommittete Änderungen** im Spread-Dashboard |
| Vollständige Wiederherstellung nur aus GitHub | **Nein** — Pool/PD1/OPI/CSS-Delta fehlt auf Remote |
| Pools-Clone | Spiegel teils **abweichend**; Live importiert primär **Spread** (PYTHONPATH-Reihenfolge) |

**PYTHONPATH (live):** Spread-Root → Spread-`dashboard` → `pools+ob+delta_bot` → `backtester/short`  
→ Python-Module unter `spread_recovery_.../dashboard/` haben Vorrang.

**Umgebungsvariablen (live, systemd drop-ins):**  
`HISTORICAL_LMV2_BATCH_READER=1`, `PHASE5S_COMPACT_BATCH_LOADER=1`, `RESEARCH_PANE_GZIP_ENABLED=1`, `POOLS_OB_DELTA_BOT_ROOT=...`, weitere in `~/.config/systemd/user/dashboard.service.d/`.

---

## 1. Git-Inventar

### 1.1 `spread_recovery_hedge_short_dev`

| Kategorie | Modified | Untracked | Deleted |
|-----------|----------|-----------|---------|
| **Gesamt** | **19** | **288** | **0** |

#### Modified (vollständige Liste — **Live-relevant**)

| Datei | Klassifikation |
|-------|----------------|
| `dashboard/app.py` | Produktiv |
| `dashboard/pd1_landscape_viewer_v1/api.py` | PD1 |
| `dashboard/pd1_landscape_viewer_v1/historical_window_batch_v1.py` | PD1 |
| `dashboard/pd1_landscape_viewer_v1/historical_window_v1.py` | PD1 |
| `dashboard/pd1_landscape_viewer_v1/window_service_v1.py` | PD1 |
| `dashboard/research_charts/api.py` | Produktiv / Pools-API |
| `dashboard/research_charts/historical_pool_state_batch_reader_v1.py` | **Pool-State** |
| `dashboard/research_charts/historical_pool_state_v1.py` | **Pool-State** |
| `dashboard/research_charts/orderflow_price_impact_v1.py` | OPI |
| `dashboard/research_charts/trade_bubbles.py` | Produktiv |
| `dashboard/research_charts/workspace_session.py` | Produktiv |
| `dashboard/start_tf1d_local.sh` | Hilfsskript |
| `dashboard/static/css/research.css` | CSS |
| `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` | PD1 / JS |
| `dashboard/static/js/research/research_opi_e2e_diag.js` | OPI / Diag |
| `dashboard/static/research_trp/opi_pane.js` | OPI |
| `dashboard/static/research_trp/style.css` | CSS |
| `dashboard/templates/partials/nav.html` | UI |
| `dashboard/test_research_charts_interaction.py` | Tests |

**Diff-Umfang (19 Dateien):** ca. +1234 / −573 Zeilen ggü. `HEAD`.

**MC-LOAD-Kern unverändert ggü. `eb9a8edb`:** u.a. `research_charts/service.py`, `static/js/research/research_charts.js`, `static/research_trp/chart.js` (kein Diff).

#### Untracked (Zusammenfassung)

| Präfix / Muster | Anz. | Klassifikation |
|-----------------|------|----------------|
| `dashboard/static/js/research/` | 122 | Forschung / JS-Artefakte, Duplikate |
| `dashboard/static/research_trp/` | 57 | Static / Experimente |
| `dashboard/templates/tests/` | 10 | Tests |
| `dashboard/.rollback_*`, `.integration_*` | 8 | **Backups / temporär** |
| `dashboard/backups/` | 1+ | **Backups** |
| `research/` | 2 | Dokumentation |
| `FULL_RECOVERY_BACKUP_INVENTORY_PHASE1.md` | 1 | Doku |
| Diverse `*.bak_*` unter `dashboard/research/` | mehrere | **Backups** |
| `pd1_landscape_viewer_v1/phase5s_member_prefill_reuse_v1.py` (untracked) | 1 | PD1-Experiment |

#### Ignoriert (relevant, **nicht** in Git)

| Pfad | Klassifikation |
|------|----------------|
| `dashboard/.env` | **Zugangsdaten** (gitignore `*.env`) |
| `dashboard/logs/sessions.json` | Sessions / Runtime |
| `dashboard/research_charts/user_data/` | Zeichnungen, Indicator-Settings |
| Weitere `!!` unter `user_data`-Artefakten in `static/` | Runtime-Kopien |
| `.env.market_state_live` (Repo-Root) | **Zugangsdaten** |

---

### 1.2 `pools+ob+delta_bot`

| Kategorie | Modified | Untracked | Deleted |
|-----------|----------|-----------|---------|
| **Gesamt** | **43** | **1092** | **0** |

#### Modified — `backtester/short/dashboard/` (29 Dateien, Spiegel)

Overlapping mit Spread (nicht 1:1 gleicher Inhalt — viele **`diff`** zum Spread-Working-Tree):

- PD1: `pd1_landscape_viewer_v1/api.py`, `historical_window_*`, (Spread hat zusätzlich `window_service_v1.py` dirty)
- **Pool-State:** `historical_pool_state_v1.py`, `historical_pool_state_batch_reader_v1.py`
- Zusätzlich **nur im Pools-Spiegel** modified: `historical_lmv2_pd_contract_v1.py`, `live_diag.py`, `live_micro_lld_v2_ch_test/*`, `static/js/research/research_charts.js`, `static/research_trp/chart.js`, `pane.html`, Templates `research_charts.html`, …
- **User-Data tracked dirty:** `research_charts/user_data/drawings.json`, `indicator_settings.json` (sollten nicht nach GitHub)

#### Modified — außerhalb Dashboard-Spiegel (14)

| Pfad | Klassifikation |
|------|----------------|
| `reports/*.md` (3) | Dokumentation |
| `results/phase_bt_d71_.../input_manifest.json` | Forschungsartefakt |
| `tools/long_v3_*`, `tools/phase_bt_d*` (9) | Trading-Logik / Tools |

#### Untracked (Zusammenfassung)

| Präfix | Anz. | Klassifikation |
|--------|------|----------------|
| `research/` | 378 | Dokumentation / MC-LOAD / Forschung |
| `backtester/short/dashboard/` | 207 | Backtester-Spiegel / Experimente |
| `reports/` | 154 | Reports |
| `results/` | 23 | Ergebnisdaten |
| Root-`*.md` (`To_Do.md`, Pool-Hypothesen, …) | 10+ | Dokumentation |
| `backtester/ob200_*` | neu | Backtester |
| `deploy/`, `configs/`, Audit-JSONs | wenige | Ops / Artefakte |

#### Ignoriert (relevant)

| Pfad | Klassifikation |
|------|----------------|
| `backtester/short/dashboard/.env` | **Zugangsdaten** |
| `research/regime_scanner/.env.*` | **Zugangsdaten** |

---

## 2. Live-Stand vs. GitHub

### 2.1 Was läuft wirklich?

- Prozess-CWD: **Spread** `dashboard/` (bestätigt).
- Quellcode effektiv: **Committed** `eb9a8edb` + **19 lokale Modifications** (Tabelle oben).
- Daten: ClickHouse via `clickhouse_config.py` → Collector-`.env` (nicht versioniert).
- Feature-Flags: systemd drop-ins (nicht im Git-Repo).

### 2.2 Auf GitHub gesichert (produktiv)

| Komponente | Remote |
|------------|--------|
| MC-LOAD Fast-First, History Phase B, EMA, Viewport, Final Reset (`mc-load-p4i-v1`) | `eb9a8edb` / Tag |
| MC-LOAD Forschungsdoku `research/mc_load_p*` | `2994f30a` |
| Basis-Dashboard, Research-Charts-Framework, Pool-State **bis** letzter Commit | in `eb9a8edb` |

### 2.3 **Nicht** auf GitHub, aber live aktiv

| Priorität | Komponente | Dateien |
|-----------|------------|---------|
| **Hoch (MC-POOL-P1)** | Pool-State Reader / API-Pfad | `historical_pool_state_v1.py`, `historical_pool_state_batch_reader_v1.py`, `research_charts/api.py` |
| **Hoch** | PD1 Landscape / Window Service | `pd1_landscape_viewer_v1/*` (4×) |
| **Mittel** | OPI | `orderflow_price_impact_v1.py`, `opi_pane.js`, `research_opi_e2e_diag.js` |
| **Mittel** | UI/CSS/Nav | `research.css`, `style.css`, `nav.html` |
| **Mittel** | Sonstiges Produktiv | `app.py`, `trade_bubbles.py`, `workspace_session.py` |
| — | Sessions, User-Drawings, `.env` | nur lokal / ignoriert |

### 2.4 Pools-Clone vs. Live

Änderungen **nur** im `pools+ob+delta_bot`-Spiegel (z. B. `research_charts.js`, `chart.js`, `historical_lmv2_pd_contract_v1.py`) sind für den **aktuellen** Live-Prozess **nicht** maßgeblich, solange PYTHONPATH unverändert bleibt. Sie sind für **Parity/Backtester-Arbeit** und spätere Sync gefährdet.

**Antwort „vollständig aus GitHub wiederherstellbar?“:**  
Nur für Stand **MC-LOAD P4J ohne Post-P4J-Entwicklung**. Der **heute funktionierende** Live-Stack benötigt zusätzlich die **19 Spread-Modifications** und lokale Secrets/User-Data.

---

## 3. Vorschlag: vollständiger Recovery-Checkpoint (MC-BASELINE-02)

Keine Aktion in MC-BASELINE-01 ausgeführt — nur Plan.

### Phase A — Offline-Sicherung (ohne Git-Destruktion)

1. **Tarball beider Working Trees** (inkl. dirty state), z. B.  
   `~/backups/mc-baseline-01_YYYYMMDD_spread.tar.zst` und `_pools.tar.zst`
2. **`dashboard/.env`**, `sessions.json`, `user_data/`, systemd-Unit + `dashboard.service.d/`, Nginx-Site — Kopie in **verschlüsseltes** Backup (nicht GitHub).
3. Inventar-Checksum-Liste (`sha256sum`) der 19 Spread-Modified-Dateien.

### Phase B — Selektive Versionierung (kein `git add -A`)

Neuer Branch-Vorschlag: `baseline/live-pre-mc-pool-p1` (Spread) parallel zu `feature/ema-touch-structure-v2`.

| Commit-Paket | Inhalt |
|--------------|--------|
| B1 | Pool-State: `historical_pool_state_v1.py`, `batch_reader_v1.py`, zugehörige Tests |
| B2 | PD1: `pd1_landscape_viewer_v1/*` (ohne Rollback-Ordner) |
| B3 | OPI + CSS + Nav (ohne Diag-JS falls rein debug) |
| B4 | `app.py`, `workspace_session.py`, `trade_bubbles.py` nach Review |
| B5 | Doku: `research/mc_baseline_01/*`, aktualisierte Recovery-MD |

Pools-Clone: entweder **einmaliger Sync** Spread→`backtester/short/dashboard` vor Commit, oder dokumentierter “Spread = SSOT für Live”.

### Phase C — Tag (neuer Name, **nicht** `stable-mc-load-p4j` überschreiben)

Vorschlag: **`stable-mc-baseline-pre-pool-p1`** auf dem Merge-Commit von B1–B5.

### Phase D — Verifikation

- pytest MC-LOAD-Suite (15) + Pool/PD1-Smoke-Tests
- Health-Check HTTP :3000 (nach manuellem Deploy)

### Verboten / Warnung

- **Kein** `reset --hard` / `clean` bei dirty Tree ohne Phase A.
- **Keine** `.env` / `sessions.json` / Credentials committen.
- **Kein** Überschreiben bestehender Tags.

---

## 4. Bezug MC-LOAD Recovery

Siehe aktualisierte [`research/mc_load_p4j/MC_LOAD_P4J_RECOVERY_CHECKPOINT.md`](../mc_load_p4j/MC_LOAD_P4J_RECOVERY_CHECKPOINT.md) — enthält Warnhinweis und Verweis auf dieses Audit.

---

## 5. Abschluss-Checkliste

- [x] Inventar beider Clones
- [x] Live vs. GitHub Gap (Pool/PD1/OPI)
- [x] Recovery-Vorschlag ohne destruktive Git-Ops
- [x] Recovery-MD korrigiert (separater Commit)
- [ ] MC-BASELINE-02 Umsetzung (bewusst offen)