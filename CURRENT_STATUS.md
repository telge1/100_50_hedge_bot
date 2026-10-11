# CURRENT STATUS — pools+ob+delta_bot / DOGEUSDT LMV2

**Stand:** 2026-10-11 (Phase 4J abgeschlossen, Phase 4K/4L Multi-Batch-Vorbereitung). **Aktualisieren:** nach Phasenabschluss oder auf Anforderung.

---

## 1. Git

| Feld | Wert |
|------|------|
| Branch | `pools-ob-delta-bot` |
| **Letzter Remote-Checkpoint** | `a81d8c0f` |
| **Lokal (nach 4L-Commit)** | Controlled-Orchestrator-Checkpoint (siehe `git log -1`) |
| Remote Ahead/Behind | nach Commit: `git status` prüfen — **Push nicht freigegeben** |

---

## 2. ClickHouse (nach Phase 4J)

| Metrik | Stand |
|--------|--------|
| Historische Original-V2-Snapshots | **190** |
| April `4h_PD10` Produktionslinie | **180 / 180** |
| Letzter Batch | `phase4i_final_30_apr2026_4hpd10_v1` (30/30) |
| `test_run_id` 4J | `903090d6-d0e2-49d2-a1b9-c65d327115bf` |
| Backup-Referenz (vor 500er) | `e134b0fa-9cba-4c0f-b8d9-60125001de50` — **neues Backup vor 500er: ausstehend** |
| Orphans | **0** |

---

## 3. Multi-Batch-Vorbereitung (April `15m_PD1`, 500)

| Feld | Wert |
|------|------|
| Manifest | `manifests/phase4k_april_15mpd1_500_v1.json` |
| Scope | `stems[0:500]` · `20260401T000000Z` … `20260406T044500Z` |
| Batches | **10 × 50** |
| Pipeline | `historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1` |
| Dry-Run | OK · **0** Publish auf Ziel-Linie |
| Erwartete Writes (bei Freigabe) | 500 meta · **108.250** member/catalog · 500 sidecar/publish |

---

## 4. Phase 4K/4L — Sicherheit

- **Pre-Write-Gate:** `HISTORICAL_JSON_IMPORT_ALLOW_WRITE` + `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE` vor erstem INSERT.
- **Recovery-Bridge:** `recovery_orchestrator_bridge.py` — `meta_without_publish` + layer `SAFE_RESUME` (Catalog ohne Sidecar) zulässig.
- **Ressourcen-Limits:** optional per Env (`resource_limit_proposal.proposed_500_batch_limit_environ()`); Batch/Readback-Wall-Clock in Orchestrator.

---

## 5. Tests (Phase 4L)

| Suite | Ergebnis |
|-------|----------|
| 4I + 4K + 4L + 4G + 4A + 4C + historical_json_import | **59 passed** |

---

## 6. Freigaben

| Schritt | Status |
|---------|--------|
| 500er `15m_PD1` CH-Import | **NEIN** |
| Neues Fünf-Tabellen-Backup | **NEIN** (Preflight bereit) |
| GitHub-Push | **NEIN** |

---

## 7. Verweise

- `MASTER_PROJECT_CONTEXT.md`
- `historical_json_import/controlled_monthly_orchestrator.py`
- `historical_json_import/productive_prewrite_gate.py`
