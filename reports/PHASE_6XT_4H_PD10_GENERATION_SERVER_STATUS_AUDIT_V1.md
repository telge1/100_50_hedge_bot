# PHASE 6XT — 4H PD10 GENERATION & SERVER STATUS AUDIT V1

**Mode:** read-only (no generation, imports, publishes, queue resume, or process changes)

**Audit timestamp (UTC):** 2026-10-21 (server-local verification)

**VERDICT:** `4H_PD10_GENERATION_BLOCKED`

Machine artifacts:

- `logs/historical_import/overnight_2026/phase6xt_4h_pd10_generation_server_status_audit_v1/inventory_summary.json`
- `logs/historical_import/overnight_2026/phase6xt_4h_pd10_generation_server_status_audit_v1/clickhouse_publish_summary.json`

Prior references: `reports/PHASE_6XT_4H_PD10_ISOLATED_PILOT_V1.md`, `reports/PHASE_6XT_4H_PD10_ISOLATED_READINESS_V1.md`, queue `phase6xt_auto_queue_20261020T173116Z`.

---

## Executive answers (Kernfragen)

| # | Question | Answer |
|---|----------|--------|
| 1 | Werden aktuell noch 4h-PD10-Snapshots generiert? | **Nein.** Kein `run_multi_month.py`, kein Overnight-4h-Runner, kein Auto-Queue-Supervisor aktiv. |
| 2 | Läuft ein aktiver Hintergrundprozess? | **Nein.** Queue-Status zeigt veraltetes `pd1_worker.status=RUNNING` / PID `1245524` — Prozess **nicht** mehr vorhanden. Isolierter Pilot-Worker PID `1557320` ebenfalls **beendet** (`worker_0_status.json` bleibt `COMPLETE`). |
| 3 | Wie viele 4h-PD10-Snapshots fertiggestellt? | **Abhängig vom Katalog** (siehe Inventar). Phase-6XT-Overnight-Scope (**255** geplante Closes): **0** unter `jan_sep_v1/results` + **1** isolierter Pilot. Legacy **apr-sep** JSON-Produktion: **1098** Dateien. **multi_month** `production/results`: **497** (Jun–Aug-Slice, **0** Überlappung mit 255-Plan). |
| 4 | Historischer Zeitraum? | **CH publish (apr-sep line):** `2026-04-01 00:00:00` … `2026-09-30 20:00:00` (1098 × 4h). **multi_month production:** `20260610T040000Z` … `20260831T200000Z`. **255-Plan:** April-Head `2026-04-11` … `2026-04-23` + September `2026-09-01` … `2026-09-30` (May–Aug bewusst skip). |
| 5 | ClickHouse / Publish? | **Ja** für Pipeline `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1`: **1098** `publish_complete=1`, **1098** Meta-Zeilen, Member vorhanden. **Keine** Publish-Zeilen für `multi_month`-Pipeline auf 4h PD10. |
| 6 | 4h-Importqueue blockiert? | **Ja.** `queue_status.json`: `BLOCKED`, `current_job_id`: **`4h_pd1`** (nicht 4h_pd10). Job **`4h_pd10`** in Manifest **nicht erreicht**; `gate_d_approved_4h_pd10.json` **fehlt**. |

---

## Korrektur zur historischen Referenz „255/255“

Die Zahl **255** bezeichnet im Manifest **`overnight_generate_total`** (75 April-Head + 180 September) für `staging/phase6xt_4h_pd10_jan_sep_v1` — **Planungsumfang**, nicht einen verifizierten Fertigstellungsstand auf dem Server.

- `run/manifest.json`: `already_in_staging: 0`, `overnight_generate_total: 255`
- `results/**/4h_PD10/*.json` unter `jan_sep_v1`: **0**
- Eindeutige Plan-Closes: **255**; Überlappung mit `multi_month/production` 4h_PD10: **0** (497 Prod-Stems liegen vollständig in May–Aug, außerhalb des 255-Scopes)

Der isolierte Pilot (`20260411T000000Z`) deckt **1/255** des Overnight-Scopes ab; Gates A–C bestanden (`PHASE_6XT_4H_PD10_ISOLATED_PILOT_V1.md`), Gate D / Overnight **nicht** freigegeben.

---

## 1. Aktive Prozesse & Locks

| Check | Result |
|-------|--------|
| `pgrep` `run_multi_month`, `phase6xt`, `auto_queue`, `overnight_4h` | Keine relevanten Generator-/Queue-Prozesse (nur Audit-Shell) |
| Queue `pd1_supervisor_pid` 1245524 | **Not running** |
| Pilot `worker_0_status.json` pid 1557320 | **Not running** |
| `/tmp/historical_lmv2_json_import.flock` | Leere Lock-Datei (0 bytes); kein laufender Import daraus ablesbar |

**Fazit:** Keine laufende 4h-PD10-Generation; Queue-Heartbeat (`2026-10-21T01:16:38Z`) und `pd1_worker.RUNNING` sind **stale** gegenüber `ps`.

---

## 2. Generation-Inventar (Staging vs Production)

### Phase-6XT multi_month (Original LMV2 Worker)

| Location | 4h_PD10 JSON | Status vs 255-Scope |
|----------|-------------|-------------------|
| `staging/phase6xt_4h_pd10_jan_sep_v1/results/` | **0** | OVERNIGHT **nicht ausgeführt** |
| `staging/phase6xt_4h_pd10_isolated_pilot_v1/results/` | **1** | Pilot **COMPLETE** (frozen `84c22453…` in worker status) |
| `production/results/**/4h_PD10/` | **497** | Älterer Jun–Aug-Slice; **nicht** Ersatz für April-Head + September |

**Manifest / Konfiguration (`jan_sep_v1/run/manifest.json`):**

- `combo`: `4h_PD10`, `config_hash`: `bd9ac4f30e177321…`
- Frozen April: `84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93`
- Frozen September: `fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66`
- `regen_skip_complete_months`: 2026-05 … 2026-08

**Duplikate / Raster:** Prod-497 und Pilot-1: eindeutige Stems, keine Doppeldateien im jeweiligen Baum geprüft (unique stem count = file count).

### Legacy `doge_apr_sep_2026_v1_history` (separater Katalog)

| Location | 4h_PD10 JSON |
|----------|-------------|
| `production/results/**/4h_PD10/` | **1098** |

Dieser Bestand stützt die **apr-sep** ClickHouse-Pipeline (Phase-4z / controlled import), nicht den ungefüllten **multi_month** Overnight-Staging-Ordner.

---

## 3. Queue-Status (`phase6xt_single_worker_auto_nohup_queue_v1`)

| Field | Value |
|-------|--------|
| `queue_run_id` | `phase6xt_auto_queue_20261020T173116Z` |
| `status` | **BLOCKED** |
| `current_job_index` | 3 → **`4h_pd1`** |
| `jobs_completed` | `5m_pd1_monitor`, `5m_pd5`, `5m_pd10` |
| Block reason | `pilot gates failed` (Gate B) |
| Gate B root cause (4h_pd1) | `ImmutableInputCoverageError` — angefordertes 1m-Fenster außerhalb eingefrorener Coverage (`gates/4h_pd1/gate_b_result.json`) |

**4h_pd10 in Queue:**

- Job 6 in `queue_manifest.json`, Staging `phase6xt_4h_pd10_jan_sep_v1`, `overnight_generate_total: 255`
- **Nicht gestartet** (Queue vorher an 4h_pd1 hängen geblieben)
- `gate_d_approved_4h_pd10.json`: **nicht vorhanden**
- Separater isolierter Pilot lief **außerhalb** der Queue-Fortsetzung; Queue blieb **BLOCKED** auf `4h_pd1`

**Letzter vollständig abgeschlossener Queue-Job:** `5m_pd10` (4h-Zweig: keiner).

---

## 4. Lifecycle: GENERATED → VIEWER

| Track | GENERATED (JSON) | STAGED (6XT overnight) | REGISTERED | IMPORTED | PUBLISHED (CH) | VIEWER_READY |
|-------|------------------|------------------------|------------|----------|----------------|--------------|
| **apr-sep catalog** `…_4hpd10_v1` | 1098 unter `doge_apr_sep_…/production` | n/a (eigener Import-Pfad) | provenance / import manifests (Phase 4z) | ja (Meta/Member) | **1098** `publish_complete=1` | **nein** — `ALLOWED_VIEWER_SCOPES` enthält **kein** `("4h", 10)` |
| **multi_month 255-scope** | 1 pilot + 0 overnight | 0/255 overnight | nicht für CH multi_month line | nein | **0** Zeilen (`multi_month` pipeline) | nein |

**ClickHouse (SELECT only):**

- Pipeline `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1`: 1098 publishes, 1098 distinct `decision_close`, min/max wie oben
- `research_lmv2_active_snapshot_meta_v1`: 1098 Zeilen für `4h` / PD10 (gesamt)
- Keine Publish-Einträge mit `pipeline_line_id` LIKE `%multi_month%` für 4h PD10

---

## 5. Frozen-Input-Integrität (4h PD10 Overnight-Scope)

| Scope | Frozen snapshot ID | Verwendung |
|-------|-------------------|------------|
| April-Head | `84c22453fc6e2b5c…` | Manifest + Pilot-Worker-Status konsistent |
| September | `fe549309ef047ea0…` | Nur in Plänen; **keine** generierten September-PD10-JSONs im 6XT-Staging |

Pilot-JSON: `timeframe=4h`, `pd=10`; Required 1m ab `2025-10-10T20:00:00Z` (Pilot-Bericht). **4h_pd1**-Blockade betrifft **PD1**-Gate mit anderem Coverage-Fenster — blockiert die **gesamte** 4h-Job-Kette in der Auto-Queue, obwohl **4h_pd10** isoliert Gate B/C bestanden hat.

---

## 6. Fehlende Arbeitsschritte (exakt)

1. **Queue-Block 4h_pd1** auflösen (Immutable 1m-Coverage für PD1-Gate B) **oder** operatorisch neu sequenzieren (nicht Teil dieses Audits).
2. **Gate D** für 4h_pd10 (`gate_d_approved_4h_pd10.json`) — **fehlt**; ohne Gate D kein freigegebenes Overnight.
3. **Overnight-Generierung** `phase6xt_4h_pd10_jan_sep_v1`: **254** verbleibende Closes im 255-Plan (75+180−1 Pilot), **0** unter `jan_sep_v1/results`.
4. Optional später: Promote/Register/Import der **multi_month**-PD10-Snapshots in eine **eigene** Pipeline-Line — **nicht** gestartet; bestehende CH-1098-Line ist **apr-sep**, nicht der ungefüllte 6XT-Staging-Baum.
5. **Dashboard:** 4h PD10 nicht in Viewer-Allowlist — Publish ≠ Viewer-sichtbar.

---

## 7. Sicherer nächster Freigabeschritt (Empfehlung, keine Ausführung)

**Operator-Freigabe-Pfad A (Queue-konform):** 4h_pd1 Input-Coverage / Gate B klären → Queue bis `4h_pd10` laufen lassen → Gate D → Overnight `overnight_4h_pd_nohup.sh 10 start`.

**Pfad B (bereits vorbereitet):** Auf Basis `4H_PD10_PILOT_PASS_AWAITING_GATE_D` nur **Gate D** dokumentieren und **isolierten** Overnight-Lauf für `jan_sep_v1` starten — **nur** nach expliziter Freigabe; Queue bleibt bis dahin logisch **BLOCKED** auf `4h_pd1`.

---

## VERDICT rationale

`4H_PD10_GENERATION_BLOCKED` — weil die Phase-6XT-Auto-Queue an **`4h_pd1`** hängt, **kein** Worker läuft, der **255-Close-Overnight**-Staging **leer** ist (0/255), und **kein** Gate-D für 4h_pd10 existiert. Legacy **1098** CH-Publishes sind **abgeschlossen** für die **apr-sep**-Pipeline, ändern aber nicht den **unvollständigen multi_month-Generierungsauftrag** und begründen **nicht** `4H_PD10_GENERATION_COMPLETE` im Sinne des aktuellen 6XT-Scopes.

**STOP** — Bericht erstellt; keine weiteren Aktionen.
