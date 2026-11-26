# MC-POOL-P9 — Full Current State Recovery Checkpoint

**Status:** `MC_POOL_P9_FULL_RECOVERY_CHECKPOINT_VERIFIED`  
**Datum:** 2026-11-26  
**Performance-Arbeit:** beendet (Nutzer bestätigt praktische Performance)

---

## 1. Vorheriger Checkpoint (unverändert)

| Item | Wert |
|------|------|
| Token | `MC_BASELINE_02_FULL_RECOVERY_CHECKPOINT_VERIFIED` |
| Tag | `stable-mc-baseline-pre-pool-p1` |
| Backup | `/home/telgenbuescher/backups/mc-baseline-02_20261126/` |

---

## 2. Neuer Checkpoint P9

| Item | Wert |
|------|------|
| Backup-Root | `/home/telgenbuescher/backups/mc-pool-p9_20261126/` |
| Backup-Größe (gesamt) | **~6,8 GB** (Spread-Archiv dominant) |
| Spread-Archiv | `archives/spread_recovery_hedge_short_dev.tar.gz` (4,2 GB) — **tar -tzf OK** |
| Pools-Research-Archiv | `archives/pools_ob_delta_bot_research.tar.gz` (~neu, nur `research/mc_pool_*`) — **tar -tzf OK** |
| Archiv-SHA-256 | siehe `checksums/archives.sha256` |

### 2.1 Produktive Datei-Prüfsummen (live = Disk = Git `e3a7e87f`)

| Datei | SHA-256 |
|-------|---------|
| `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` | `d2892b74cb070202c67e86dc64386decd7e5267f4f6c16f5711cb9d81cfbe975` |
| `dashboard/static/js/research/research_charts.js` | `881aca0d70cc8a5b20725cbd1a0f109df4ce23eea4b612e7659b7ed8dc6824b6` |
| Template `?v=` | `pd1-mc-pool-p4b-v1`, `research-mc-pool-p4b-v1` |

HTTP-Live (`127.0.0.1:3000`) **pd1**: gleiche SHA wie oben (ohne Restart während P9).

---

## 3. Git / GitHub

| Item | Wert |
|------|------|
| Remote | `git@github.com:telge1/100_50_hedge_bot.git` |
| Recovery-Branch | `recovery/mc-pool-p9-20261126` |
| Commit (HEAD Branch + Tag^{commit}) | `5727080c23ceb66a59296596cc8db7709f78306b` |
| Annotierter Tag-Objekt (remote) | `42e6d17b768addc3dfd8ade90fea3691224d7c99` |
| Funktions-Commit (pd1/recovery core) | `e3a7e87ffc748a03d2dbf2c7325efc3b5c1f6edd` |
| Push-Verifikation | `git ls-remote` Branch + Tag auf `origin` |

**Primary Working Copy (Produktion):**  
`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev`  
Branch: `recovery/mc-pool-p9-20261126` (lokaler Dirty-Tree **neben** Commit für untracked Artefakte).

**Pools-Workspace:** `/home/telgenbuescher/projects/pools+ob+delta_bot` — gleiches HEAD vor Branch-Switch lokal; **massive lokale D/??-Artefakte nicht versioniert** (siehe §7).

---

## 4. Gesicherte produktive / versionierte Kerndateien (Commit)

- `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` (P4B–P4B4A)
- `dashboard/static/js/research/research_charts.js` (Multi-Pane Re-Enable)
- `dashboard/templates/research_charts.html` (Asset `?v=` P4B)
- `dashboard/static/js/research/test_mc_pool_p4b_cache_lifecycle_v1.js`
- `dashboard/static/js/research/test_mc_pool_p4b4_bounds_safety_v1.js`
- `dashboard/test_mc_load_p3_fast_first_v1.py`, `test_mc_load_p4e_v1.py` (Template-Erwartung P4B)
- `research/mc_pool_p1` … `research/mc_pool_p4b` (Dokumentation + Harness)

**Funktionsstand erhalten:** MC-LOAD-P4J, Multi-Chart/Coin, Exact Origin, OPI, OB-Level, Batch/Prefill, P4B/P4B1/P4B4/P4B4A (siehe Commit-Message).

---

## 5. Regressionstests (P9-Lauf)

| Suite | Ergebnis |
|-------|----------|
| `test_mc_pool_p4b4_bounds_safety_v1.js` | **24 PASS** |
| `test_mc_pool_p4b_cache_lifecycle_v1.js` | **13 PASS** |
| `test_p24e_coalesce_logic_v1.js` | **5 PASS** |
| `pytest` MC-LOAD P4i/P4g/P4e/P3 | **11 PASS** (Template-FIX: `research-mc-pool-p4b-v1` akzeptiert) |

---

## 6. Backup-Inhalt

- `archives/` — Projekt-Tar(s)
- `checksums/` — `archives.sha256`, `prod_files.sha256`
- `git-meta/` — `status`, `diff.patch`, `HEAD` je Clone
- `systemd/` — `dashboard.service` + Drop-ins (`mc4l`, `mc4o`, `mc4r`, `mc4s`, `mc5j`, `ob-live-03-on-demand`)
- `research/mc_pool_p9/RESTORE_FROM_BACKUP.md` (Kurzanleitung)

**Secrets:** nicht in Git; Operator-Credentials nur lokal/`config/` — nicht im P9-Commit.

---

## 7. Verbleibende lokale Änderungen (nicht im P9-Commit)

Im Spread-Clone u. a. **unversioniert**:

- `dashboard/.rollback_*`, `.bak_*`, große History-JSON-Importe, `run/ob1000_materializer/shard*`, diverse fehlplatzierte Python-Dateien unter `static/js/research/`, Forensic-Runs.

**Pools-Clone:** ~10k Status-Zeilen (v. a. `D` aus Quarantine/Bot-Cleanup) — **nicht** committet.

**Nicht durch Git wiederherstellbar:**

- ClickHouse-Daten, Live-OB-RAM, offene systemd-Prozesse außer Dashboard, PD1-Publication-Wasserstände auf Servern, Browser-Caches.

---

## 8. Offene Punkte (bewusst zurückgestellt, nicht implementiert)

| ID | Thema |
|----|--------|
| P5 | Pan-/Zoom-Segment-Reuse |
| P6 | Inkrementelle Live-Pool-Updates |
| P7 | Doppelte Window-Requests |
| P8 | Weitere Multi-Chart-Performance |

**Dokumentiert, kein PASS behauptet:**

- Nutzer: sehr gute praktische Performance; Window-Cache OFF→ON im Browser bestätigt.
- P4B4A lokale Safety-Tests PASS.
- P4B4A abschließende Browser-Messung (1× Bounds) **ausstehend**.
- Vollständige Segment-Equality bei fixem Viewport **nicht formal PASS**.

---

## 9. Recovery-Anleitung (Kurz)

```bash
# A) Git (empfohlen für versionierbaren Dashboard-Kern)
cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev
git fetch origin
git checkout recovery/mc-pool-p9-20261126
# oder: git checkout stable-mc-pool-p9-20261126

# B) Vollbackup
BK=/home/telgenbuescher/backups/mc-pool-p9_20261126
sha256sum -c $BK/checksums/archives.sha256
tar -xzf $BK/archives/spread_recovery_hedge_short_dev.tar.gz -C /home/telgenbuescher/projects

# C) Produktion validieren
sha256sum dashboard/static/js/research/pd1_exact_origin_density_research_v1.js
# Erwartung: d2892b74cb070202c67e86dc64386decd7e5267f4f6c16f5711cb9d81cfbe975
```

**Kein automatischer Dashboard-Restart in P9.**

---

## 10. Token

`MC_POOL_P9_FULL_RECOVERY_CHECKPOINT_VERIFIED`