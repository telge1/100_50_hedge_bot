# Phase 3B — ClickHouse Backup Readiness & Recovery (read-only Audit)

**Stand:** 2026-10-10 (Phase 3C ausgeführt)
**Status:** Produktives LMV2-Vier-Tabellen-Backup + isolierter Restore-Test **erfolgreich** (Phase 3C). Kein Pilot-Write auf `signal_generator`.

---

## A. `CLICKHOUSE_DEPLOYMENT`

| Merkmal | Nachweis |
|---------|----------|
| **Version** | `26.7.1.1315` (`SELECT version()`) |
| **Uptime** | ~21,4 Tage (`uptime()` ≈ 1 848 585 s) |
| **Container** | `orderbook-clickhouse` |
| **Image** | `clickhouse/clickhouse-server:latest` |
| **Status** | Up, healthy (Docker) |
| **Ports** | `127.0.0.1:8123` (HTTP), `127.0.0.1:9000` (native) |
| **Compose** | `/home/telgenbuescher/projects/orderbook_analyse/compose.yaml` |
| **Daten-Hostpfad** | `/home/telgenbuescher/projects/orderbook_analyse/data/clickhouse` → `/var/lib/clickhouse` |
| **Logs-Hostpfad** | `/home/telgenbuescher/projects/orderbook_analyse/logs/clickhouse` |
| **DB (Research/LMV2)** | `signal_generator` |
| **CH-User (Research)** | `orderbook_writer` (Credentials: Collector-`.env` via `clickhouse_config.py`, **nicht** in diesem Dokument) |
| **Freier Speicher (Disk `default`)** | ~264 GiB frei von ~916 GiB (`system.disks`, 2026-10-10) |

**Hinweis:** Host-`du` auf `data/clickhouse/store` ohne root oft „Permission denied“; Größen über `system.parts` verlässlicher.

---

## B. `EXISTING_BACKUP_DISCOVERY`

| Quelle | Ergebnis |
|--------|----------|
| `system.backups` | **Pilot-Preflight-Backup** registriert (siehe Phase 3C unten); ältere Syntax-Test-Jobs ebenfalls in `system.backups` |
| Dedizierte `backup`-Disk in `system.disks` | **Keine** (nur `default` → `/var/lib/clickhouse/`) |
| LMV2-spezifische Skripte im Repo `pools+ob+delta_bot` | **Keine** |
| `/home/telgenbuescher/backups/` | Vorhanden, Inhalt **EMA/Orderbook-Fanout** (Sep 2026) — **nicht** `signal_generator` / LMV2 |
| Docker-Volume-Snapshots | **Nicht** nachgewiesen (kein dokumentierter Snapshot des CH-Bind-Mounts für LMV2) |

**Fazit (Phase 3B):** Zum Audit-Zeitpunkt keine Sicherung. **Phase 3C** hat ein natives Backup der vier Tabellen erstellt und per Restore in `signal_generator_lmv2_restore_test` verifiziert (siehe Abschnitt **Phase 3C — Ausführung**).

---

## C. `BACKUP_METHOD`

**Empfehlung:** **Ein natives ClickHouse-Backup** aller vier Tabellen in **einem** `BACKUP`-Statement (ClickHouse ≥ 22.8, hier 26.7).

**Warum:**

- User `orderbook_writer` hat **`BACKUP`**-Grant (`SHOW GRANTS`).
- Build-Option `USE_AWS_S3=1` (optional später S3; für Pilot reicht lokal).
- Umfang nur LMV2-Publish-Pipeline-Tabellen (~**29 MB** `bytes_on_disk`, siehe D) — klein, schnell, geringes Risiko.
- **Kein** rohes Kopieren von `store/` während der Server läuft (inkonsistent).

**Alternative (nur wenn `BACKUP` scheitert):** Wartungsfenster + dokumentierter Cold-Copy des Bind-Mounts (hohes Risiko, nicht bevorzugt).

---

## D. `BACKUP_STORAGE_AND_CAPACITY`

**Aktuelle Zeilenzahlen (read-only, 2026-10-10):**

| Tabelle | Zeilen | bytes_on_disk (active parts) |
|---------|-------:|-----------------------------:|
| `research_lmv2_active_snapshot_meta_v1` | 2 051 | ~553 KiB |
| `research_lmv2_active_snapshot_member_v1` | 609 553 | ~1,4 MiB |
| `research_lmv2_snapshot_active_pool_v1` | 606 246 | ~23,2 MiB |
| `research_lmv2_active_snapshot_publish_v1` | 2 136 | ~677 KiB |
| **Summe LMV2 (alle `research_lmv2%`)** | — | **~29,0 MiB** |

Gesamt `signal_generator` (alle Tabellen, active parts): **~976 MiB** — Backup nur der vier Tabellen, nicht ganze DB.

**Zielpfad (Phase 3C, tatsächlich):**

- **Tag:** `lmv2_pilot_preflight_20261010T190041Z`
- **Backup-ID:** `605c7c2f-71f2-45d7-9060-455e3688c30d`
- **Logisch:** `File('backups/lmv2_pilot_preflight_20261010T190041Z/lmv2_four_tables')`
- **Container:** `/var/lib/clickhouse/backups/backups/lmv2_pilot_preflight_20261010T190041Z/lmv2_four_tables/`
  (doppeltes `backups/` weil `allowed_path` = `backups` und Pfad mit `backups/…` beginnt)
- **Host (Bind-Mount):** `/home/telgenbuescher/projects/orderbook_analyse/data/clickhouse/backups/backups/lmv2_pilot_preflight_20261010T190041Z/lmv2_four_tables/`
- **Größe:** 348 Dateien, ~27,0 MiB (`total_size` 27 041 622)
- **Zeit (UTC):** 2026-10-10 19:00:41

**Kapazität:** Benötigt grob **50–200 MiB** inkl. Metadaten; verfügbar **>>** ausreichend (~264 GiB frei).

---

## E. `CONSISTENCY_AND_CONCURRENT_WRITES`

- **Ein** `BACKUP`-Job mit **vier** `TABLE …`-Zeilen (siehe G) erzeugt einen **logisch konsistenten** Backup-Zeitpunkt für die genannten Tabellen (CH-Backup-Snapshot der Parts).
- **Parallele Schreibzugriffe** auf andere Tabellen (z. B. `candles_1m`, ~235 M Zeilen, laufender Collector) beeinträchtigen das LMV2-Backup **nicht**, solange nur die vier Tabellen gesichert werden.
- **Risiko:** Gleichzeitige **INSERT** in dieselben vier LMV2-Tabellen während des Backups (z. B. Pilot-Write) würde den „Vor-Pilot“-Stand vermischen — **Backup vor Pilot-Write** ausführen, Pilot-Write erst nach Backup-Freigabe.
- `system.merges`: leer zum Prüfzeitpunkt; `system.processes`: keine langen LMV2-Schreibqueries sichtbar.
- **ReplacingMergeTree** (`meta`, `publish`): Restore-Test muss `FINAL` bzw. erwartete Zeilenzahlen vergleichen, nicht nur rohe Part-Zahlen.

---

## F. `RESTORE_VERIFICATION_PLAN`

**Ziel:** Wiederherstellung **nur** in isolierter Test-DB, **ohne** `signal_generator` zu überschreiben.

1. **Backup erstellen** (nach Freigabe, Abschnitt G).
2. **Integrität:** `system.backup_log` / Backup-Verzeichnis auf Host: Dateien vorhanden, Größe > 0; CH `RESTORE … SETTINGS allow_different_database_defintion=1` nur bei Bedarf.
3. **Test-DB:** `CREATE DATABASE signal_generator_lmv2_restore_test` (einmalig nach Freigabe).
4. **RESTORE** der vier Tabellen **INTO** `signal_generator_lmv2_restore_test` (Tabellennamen gleich oder mit Suffix — siehe H).
5. **DDL:** `SHOW CREATE TABLE` Original vs Restore — Struktur gleich.
6. **Counts:** Zeilen pro Tabelle = Werte aus Abschnitt D (Toleranz 0 für MergeTree; bei `FINAL` für ReplacingMergeTree).
7. **Checksummen (stichproben):**
   - `SELECT count(), uniqExact(snapshot_id) FROM …_meta FINAL`
   - Publish: `count()` pro `pipeline_line_id` für bekannte Phase-10-Linie (2016) — muss mit Produktion übereinstimmen.
   - **Kein** Pilotlinie `historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1` vor Pilot (0 Zeilen).
8. **Konsistenz-Stichprobe:** Für eine bekannte `snapshot_id` aus Phase-10: Member-Anzahl = Meta `active_pool_count`; Catalog-`pool_id`-Menge = Member-Menge (SQL wie in `catalog_integrity`).
9. **Aufräumen:** Test-DB `DROP DATABASE signal_generator_lmv2_restore_test` nur nach dokumentierter Freigabe.

**Erfolg:** Restore-Test bestanden **nur**, wenn Schritte 5–8 dokumentiert grün — nicht allein Backup-Datei existiert.

---

## G. `EXACT_BACKUP_COMMANDS` (Phase 3C validiert)

Voraussetzungen:

```bash
source /home/telgenbuescher/projects/orderbook_analyse/.env
BACKUP_TAG="lmv2_pilot_preflight_$(date -u +%Y%m%dT%H%M%SZ)"
```

**Hinweis:** Host-`mkdir` unter `data/clickhouse/backups/` kann fehlschlagen (Permissions); ClickHouse legt den Pfad im Container bei erfolgreichem `BACKUP` an.

**Nicht verwenden:** `TO Disk('default', 'backups/…')` — auf dieser Instanz **Code 318** (`backups.allowed_disk` nicht gesetzt).
**Nicht verwenden:** ein einzelnes `BACKUP TABLE a, b, c, d` — **Syntax 62** (CH 26.7).

Backup (ein konsistenter Job, alle vier Tabellen — **`TABLE` vor jeder Tabelle**):

```bash
docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" \
  --password "${CLICKHOUSE_PASSWORD}" \
  --query "
BACKUP
  TABLE signal_generator.research_lmv2_active_snapshot_meta_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_member_v1,
  TABLE signal_generator.research_lmv2_snapshot_active_pool_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_publish_v1
TO File('backups/${BACKUP_TAG}/lmv2_four_tables')
SETTINGS compression_method='lz4'
"
```

**Prüfung (read-only nach Backup):**

```bash
docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "SELECT id, name, status, num_files, total_size FROM system.backups WHERE name LIKE '%${BACKUP_TAG}%' FORMAT Vertical"

docker exec -i orderbook-clickhouse ls -la "/var/lib/clickhouse/backups/backups/${BACKUP_TAG}/lmv2_four_tables/" | head
```

**Produktives Preflight-Backup (Referenz):** Tag `lmv2_pilot_preflight_20261010T190041Z`, ID `605c7c2f-71f2-45d7-9060-455e3688c30d`, Status `BACKUP_CREATED`.

---

## H. `EXACT_RESTORE_TEST_COMMANDS` (isoliert, nicht auf Produktion)

```bash
source /home/telgenbuescher/projects/orderbook_analyse/.env
BACKUP_TAG="<gleicher Tag wie Backup>"
TEST_DB="signal_generator_lmv2_restore_test"

docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "CREATE DATABASE IF NOT EXISTS ${TEST_DB}"

docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "
RESTORE
  TABLE signal_generator.research_lmv2_active_snapshot_meta_v1 AS ${TEST_DB}.research_lmv2_active_snapshot_meta_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_member_v1 AS ${TEST_DB}.research_lmv2_active_snapshot_member_v1,
  TABLE signal_generator.research_lmv2_snapshot_active_pool_v1 AS ${TEST_DB}.research_lmv2_snapshot_active_pool_v1,
  TABLE signal_generator.research_lmv2_active_snapshot_publish_v1 AS ${TEST_DB}.research_lmv2_active_snapshot_publish_v1
FROM File('backups/${BACKUP_TAG}/lmv2_four_tables')
"
```

**Phase 3C Restore-Test:** Status `RESTORED`, Restore-ID `40f8447a-5f98-48ff-a430-167abb8c0c0c`; Zeilen-/Spalten-/Fingerprint-Parität prod vs. `signal_generator_lmv2_restore_test` grün. Test-DB **nicht** automatisch gelöscht.

**Validierung (Beispiele):**

```bash
for t in research_lmv2_active_snapshot_meta_v1 research_lmv2_active_snapshot_member_v1 \
         research_lmv2_snapshot_active_pool_v1 research_lmv2_active_snapshot_publish_v1; do
  echo "== $t"
  docker exec -i orderbook-clickhouse clickhouse-client --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
    --query "SELECT count() FROM signal_generator.${t}; SELECT count() FROM ${TEST_DB}.${t}"
done
```

**Fehler:** Bei Abweichung **STOP** — kein Pilot-Write; Produktion unangetastet lassen.

**Cleanup (nur nach erfolgreichem Test + Freigabe):**

```bash
docker exec -i orderbook-clickhouse clickhouse-client \
  --user "${CLICKHOUSE_USER}" --password "${CLICKHOUSE_PASSWORD}" \
  --query "DROP DATABASE IF EXISTS ${TEST_DB}"
```

---

## I. `RISKS_AND_BLOCKERS`

| Blocker / Risiko | Beleg |
|------------------|--------|
| **Restore vor 3C nicht durchgeführt** | Behoben in Phase 3C |
| **Kein LMV2-Backup (Stand 3B)** | Behoben: Tag `lmv2_pilot_preflight_20261010T190041Z` |
| **Pilot-Write ohne Backup** | Daten anderer Linien schwer rückgängig (global `snapshot_id`) |
| **`BACKUP` Pfad/Disk** | `Disk()` deaktiviert; `File('backups/…')` + verschachtelter Host-Pfad `backups/backups/…` |
| **Syntax-Test-Artefakte** | Zusätzliche Backups unter `backups/_syntax_*` (inkl. ~929 MiB EXCEPT-Test) — nicht löschen ohne Freigabe |
| **Collector schreibt `candles_1m`** | Stört LMV2-Backup nicht, aber zeigt laufende CH-Last |
| **Host-Permissions auf `store/`** | Forensik per `system.parts`, nicht per `du` ohne root |

**Pilot-Isolation nach Write:** Rollback nur über gezielte Löschung/Mutation der 10 neuen `snapshot_id`s + Publish-Zeilen der Pilotlinie — **nicht** in 3B getestet.

---

## J. `READY_TO_EXECUTE_BACKUP`

**`YES` (erfüllt)** — Backup `605c7c2f-…` erstellt; Restore-Test in `signal_generator_lmv2_restore_test` bestanden (Phase 3C).

**Pilot-Write** erfordert weiterhin explizite Nutzer-Freigabe + `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` (Runbook).

---

## Bezug Pilot (unverändert)

- Pilotlinie: `historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1` — **0** Publish-Zeilen (Phase 3A).
- Geplanter Delta: +10 Meta, +2750 Member, +2750 Catalog, +10 Publish.
- Runbook: `PILOT_WRITE_RUNBOOK.md`
