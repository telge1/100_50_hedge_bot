# MASTER PROJECT CONTEXT — DOGEUSDT Original Live Micro LLD V2

**Status:** Verbindlicher Masterplan (Änderungen nur nach ausdrücklicher Freigabe).

**Geltungsbereich:** DOGEUSDT, **ausschließlich Original Live Micro LLD V2** (frozen historical JSON + gleiche Semantik in ClickHouse). Keine parallele „neue“ V2-Engine, keine neuen Berechnungsmodelle ohne Genehmigung.

---

## 1. Zielbild

Wir bewahren die **Original-V2-Engine-Ausgabe** als SoT für historische Entscheidungssnapshots und spiegeln sie **verlustfrei** in ClickHouse, damit **Research Chart**, **Backtester** und später **Live-Ingest / Trading-Bot** dieselbe kausale Pool-Wahrheit nutzen.

---

## 2. Verbindliche Pool-Semantik

| Konzept | Regel |
|---------|--------|
| **Original ACTIVE** | Vollständige aktive Pool-Membership und Catalog-Bounds pro `decision_close`; Engine-Output unverändert importieren. |
| **Original Remaining** | Getrennt von ACTIVE; Darstellung/Projektion des unverbrauchten Rests — **nicht** mit ACTIVE vermischen oder still ersetzen. |
| **Mixed-Frozen-Provenienz** | Jeder Snapshot an autorisierte `frozen_input_id` / SHA256 gebunden; Sidecar **vor** Publish. |
| **Pool-IDs, Bounds, Strength, `known_at`** | Aus Original-JSON; Readback prüft Parität (inkl. Catalog-Mengen und IDs). |
| **Kausalität** | Sichtbarkeit ab `known_at` / Entscheidungs-Close; keine Zukunftsinformation in historischen Pins. |

### A. Historisch eingefrorene Engine-Ausgaben

Die historischen **Original-Live-Micro-LLD-V2-Snapshots** (frozen JSON) sind die **autoritative Quelle**. Sie dürfen weder beim **CH-Import** noch bei **Chart-** oder **Backtest-Abfragen** stillschweigend neu berechnet, ersetzt oder durch eine andere Engine-Version überschrieben werden.

### B. ACTIVE/Remaining-Identität

**Original ACTIVE** und **Remaining** sind getrennte Zustände **desselben** historischen Poolsystems. Remaining **referenziert die Original-Pool-Identität** (`pool_id`, Bounds, Provenienz) und nutzt ausschließlich die **bestehende, bereits entwickelte Remaining-Projektion** (Server- und Client-Pfade). Es gibt **keine** zweite, unabhängige Pool-Erzeugung oder parallele Pool-ID-Welt.

### C. Historische vs. tatsächliche Verfügbarkeit

Fachlich zu trennen:

| Begriff | Bedeutung |
|---------|-----------|
| **`known_at`** | Historische fachliche Bekanntheit / Sichtbarkeit des Pools in der Original-Engine. |
| **Decision-Close** | Historisch zulässiger Entscheidungszeitpunkt des Snapshots (frozen close). |
| **Publikations- / Verfügbarkeitszeit (Live)** | Wann ein Signal im **Live-System** tatsächlich empfangen oder handelbar war. |

Historisch **nachimportierte** Snapshots (CH-Batches nachträglich) dürfen **nicht** dargestellt werden, als wären sie **damals live** empfangen worden. UI und Research müssen historische Pins und Live-Verfügbarkeit klar unterscheiden.

---

## 3. Datenräume

| Schicht | Inhalt |
|---------|--------|
| **Frozen-Katalog** | ~53.802 Original-V2-Snapshots Apr–Sep 2026 (`doge_apr_sep_2026_v1_history`); **nicht** in Git (~43 GiB unter `production/results/`). |
| **Zeitliche Rollen** | **Apr–Jul 2026:** Vorgeschichte für Import und Abnahme. **Ab 01.08.2026:** Research Chart als primärer Konsument für kausale UI. |
| **ClickHouse** | DB `signal_generator`; LMV2 meta/member/catalog/publish + Import-Provenance-Sidecar; **gemeinsame Quelle** für Research Chart und Backtester nach Abnahme. |
| **Externe Metadaten** | `catalog_manifest.json`, `coverage_matrix.json`, `provenance_lookup.json` — lokal sichern (`docs/live_micro_lld_v2/DEVELOPMENT_CHECKPOINT.md`). |

---

## 4. Pipeline- und Import-Governance

- **Getrennte Publish-Lines** (z. B. Pilot Aug 15m vs. Produktion Apr–Sep `4h_PD10`).
- **Batch-Import:** Preflight, Backup, UUID-`test_run_id`, Writer-Lock, `fail_closed` + `require_sidecar` Readback — **je Batch explizite Freigabe**.
- **Kein** automatischer Vollimport, **kein** Live-Cutover, **keine** CH-Writes ohne Freigabe-Flags.
- **Keine** Engine-Neuberechnung der historischen JSONs im Import-Pfad.

Produktionslinie (Beispiel): `historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1`.

---

## 5. Verbindliche Reihenfolge (Masterplan)

1. **Original Live Micro LLD V2 ausschließlich** — keine Ersatz-Engine, keine neue Pool-Berechnung.
2. **Historische Original-Snapshots** mit unveränderter **Mixed-Frozen-Provenienz** erzeugen und versionieren (extern).
3. **Vollständiger kausaler CH-Import** in regulierten Batches mit Readback und Sidecar.
4. **Original ACTIVE** und **separate Remaining-Projektion** in Chart und API getrennt halten.
5. **Research Chart ab 01.08.2026**; **April–Juli** als Vorgeschichte / Import- und Parity-Phase.
6. **Visuelle und kausale Abnahme** des Charts (Decision-Snapshot, Pins, Remaining) vor Downstream.
7. **Backtester** erst danach auf **identischer** CH-/Katalog-Datenquelle.
8. **Historische Daten** bis zum letzten geschlossenen Zeitpunkt fortführen (Import-Batches mit Freigabe).
9. **Kontinuierlichen Live-Ingest** aufbauen (eigene Cutover-Freigabe).
10. **Forwardtesting**, anschließend **Trading-Bot** (eigene Freigaben).

---

## 6. Explizite Nicht-Ziele

- Neue V2-Engine oder abweichende Pool-Logik ohne Genehmigung.
- Neue Datenbank oder Schema-Brüche ohne Migrationsplan.
- Historische JSON-Snapshots oder CH-Backups in Git.
- Automatisches DELETE/RESTORE in CH ohne Freigabe.

---

## 7. Technische Referenzen (Detail-Doku)

| Thema | Pfad |
|--------|------|
| Import-Stack & Checkpoint | `docs/live_micro_lld_v2/DEVELOPMENT_CHECKPOINT.md` |
| Backup/Restore | `docs/live_micro_lld_v2/PHASE_3B_BACKUP_READINESS.md` |
| Importer-Code | `.../historical_json_import/` |
| Runbooks | `docs/live_micro_lld_v2/` |
| Pool-Struktur (Fachlich) | `docs/pool-structure-logik.md` |

---

## 8. Cursor-Arbeitsregeln (Dokumentation)

1. **Zuerst lesen:** `MASTER_PROJECT_CONTEXT.md` und `CURRENT_STATUS.md`.
2. **MASTER** nicht ohne ausdrückliche Freigabe ändern.
3. **CURRENT_STATUS** nur nach Phasenabschluss oder auf Anforderung aktualisieren.
4. Keine großen Logs oder historischen Datenbestände in diese Dateien kopieren.
5. Keine weiteren allgemeinen Master-/Statusdateien anlegen.

---

*Letzte inhaltliche Ausrichtung: Git-Cleanup C1–D + Checkpoints Import/A/B (Okt 2026).*
