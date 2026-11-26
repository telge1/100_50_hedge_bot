# MC-BASELINE-02 — Full Live System Recovery Checkpoint

**Endstatus:** `MC_BASELINE_02_FULL_RECOVERY_CHECKPOINT_VERIFIED`  
**Datum:** 2026-11-26

---

## Phase A — Lokales Backup

**Backup-Root (außerhalb der Repos):**  
`/home/telgenbuescher/backups/mc-baseline-02_20261126/`

| Artefakt | Pfad | Zweck |
|----------|------|--------|
| Produktiv-Snapshot (20 Dateien) | `spread/dashboard_productive_snapshot.tar.zst` | Schnell-Restore Dashboard-Delta |
| Checksums Live | `manifests/spread_live_productive.sha256` | Paritätsreferenz |
| Git-Diffs | `diffs/spread_full.patch`, `diffs/pools_full.patch` | Dirty-State Export |
| Spread Full Tree | `spread/spread_repo_full.tar.zst` | Gesamter Clone inkl. dirty/untracked |
| Pools Full Tree | `pools/pools_repo_full.tar.zst` | Zweiter Clone (läuft/Archiv) |
| Pools Doku-Slice | `pools/pools_docs_and_research.tar.zst` | MC-LOAD/Baseline MD |
| systemd | `systemd/dashboard.service`, `systemd/dashboard.service.d/` | Feature-Flags Live |
| User-Data | `user_data/research_charts_user_data/` | Drawings (chmod go-rwx) |
| Secrets | `secrets/dashboard.env` (mode **600**, dir **700**) | **Nicht** auf GitHub |
| Inventar | `manifests/backup_inventory.json` | Größen + SHA256 |

**Speicher:** ~185 G frei auf `/` vor Start (79 % belegt).

**Integrität:** `zstd -t` auf Snapshot + Full-Archive (siehe Abschlussbericht).

---

## Phase B/C — GitHub Quellcode (Spread SSOT)

**Basis:** `eb9a8edb` (MC-LOAD P4J, Tag `stable-mc-load-p4j` unverändert)

**Branch:** `baseline/live-pre-mc-pool-p1`  
**HEAD / Tag `stable-mc-baseline-pre-pool-p1`:** `5d15cfaf`

**Worktree (nicht Live-CWD):**  
`/home/telgenbuescher/projects/spread_baseline_live_pre_mc_pool_p1`

**Gesicherte produktive Dateien:** **20** (19 modified + 1 untracked Import)

| Commit | Hash | Paket |
|--------|------|-------|
| B1 | `7a7c3bc8` | Pool-State + Batch Reader |
| B2 | `9925e0b6` | PD1 + `phase5s_member_prefill_reuse_v1.py` |
| B3 | `0e916ec5` | OPI |
| B4 | `364f80e2` | `app.py`, `research_charts/api.py` |
| B5 | `9f2c584b` | Bubbles, Session, CSS, Nav, PD1-JS |
| B6 | `5d15cfaf` | Tests, `start_tf1d_local.sh` |

**Parität:** Live-Working-Tree vs. Worktree-`HEAD` — **bytegenau** für alle 20 Dateien.  
**MC-LOAD-Kern:** `service.py`, `research_charts.js`, `chart.js` — **unverändert** seit `eb9a8edb`.

**Regression (Worktree, keine Prod-Instanz):** MC-LOAD pytest **15 passed**.

---

## Phase D — Remote

```text
origin/baseline/live-pre-mc-pool-p1 → 5d15cfaf
refs/tags/stable-mc-baseline-pre-pool-p1 → 5d15cfaf (annotiert)
refs/tags/stable-mc-load-p4j → eb9a8edb (unverändert)
```

---

## Wiederherstellung (später ausführen — nicht in MC-BASELINE-02)

### Nur Quellcode (ohne lokale Secrets)

```bash
cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev
git fetch origin baseline/live-pre-mc-pool-p1 tag stable-mc-baseline-pre-pool-p1
git checkout -B restore/baseline-pre-pool-p1 stable-mc-baseline-pre-pool-p1
# ODER neues Worktree:
git worktree add ../spread_restore_baseline stable-mc-baseline-pre-pool-p1
```

**Live-CWD unangetastet lassen**, bis bewusst umgeschaltet wird.

### Runtime-Vollständigkeit

Aus Backup restoren:

- `secrets/dashboard.env` → `dashboard/.env`
- `user_data/research_charts_user_data/` → `dashboard/research_charts/user_data/`
- `systemd/` → `~/.config/systemd/user/`
- optional Full-Tar: `spread/spread_repo_full.tar.zst`

### Health (nur nach bewusstem Deploy/Restart)

```bash
systemctl --user restart dashboard.service   # nicht in Baseline-02 ausgeführt
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/login
```

---

## pools+ob+delta_bot (zweiter Clone)

**Nicht** mit Spread blind gemergt. Klassifikation:

| Kategorie | Status |
|-----------|--------|
| `backtester/short/dashboard/` Spiegel (29 modified) | Teilweise **abweichend** von Live-Spread; **nicht** SSOT |
| Zusätzliche Pool/CH-Änderungen nur in Pools | **Nicht** im Live-PYTHONPATH-Vorrang |
| `research/`, Root-`*.md`, `tools/` | Lokal + Full-Tar; Doku teils auf `pools-ob-delta-bot` (`35494db6`+) |
| `.env`, CH-Daten, große Import-JSON | Nur lokales Backup / ignoriert |

Geplante Folge: selektive Sync-Commits auf `pools-ob-delta-bot` **nach** Spread-SSOT-Review (MC-POOL-P1-Vorbereitung).

---

## Vollständige Wiederherstellbarkeit

| Frage | Antwort |
|-------|---------|
| Live-Dashboard-**Quellcode** aus GitHub? | **JA** (Tag/Branch `5d15cfaf` + 20 Dateien) |
| Identisch mit **laufendem** Prozess (heute)? | **JA** für diese 20 Dateien; Prod-CWD weiterhin original dirty Tree |
| Inkl. Sessions, CH-Creds, User-Drawings? | **NEIN** ohne lokales Backup-Set |
| pools-Spiegel 1:1? | **NEIN** — Spread ist SSOT |

**Einschränkungen:**

- 288 / 1092 untracked Dateien **nicht** versioniert (Experimente, Backups, Forschung)
- `.venv` nicht im Git; Recreate aus Projekt-Setup
- Kein Deployment/Restart in dieser Phase

---

## Verwandte Doku

- [MC-BASELINE-01 Audit](../mc_baseline_01/MC_BASELINE_01_FULL_RECOVERY_AUDIT.md)
- [MC-LOAD P4J Recovery](../mc_load_p4j/MC_LOAD_P4J_RECOVERY_CHECKPOINT.md)