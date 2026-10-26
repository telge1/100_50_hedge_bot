# CURRENT STATUS — pools+ob+delta_bot / DOGEUSDT LMV2

**Stand:** 2026-10-11 (Phase 4X abgeschlossen — Mai 2026 vollständig). **Aktualisieren:** nach Phasenabschluss oder auf Anforderung.

---

## 1. Git

| Feld | Wert |
|------|------|
| Branch | `pools-ob-delta-bot` |
| **Letzter Remote-Checkpoint** | `a81d8c0f` |
| **Lokal** | 4O–4X-Artefakte im Working Tree (kein Push freigegeben) |

---

## 2. ClickHouse (nach Phase 4X)

| Metrik | Stand |
|--------|--------|
| April 2026 (alle 4 Kombinationen) | **vollständig** (unverändert) |
| Mai 2026 `15m_PD1` / `15m_PD5` / `15m_PD10` | **je 2.976** auf Produktionslinie |
| Mai 2026 `4h_PD10` | **186** |
| Aug-Pilot | **10** |
| Globale Tabellenzeilen | meta **19.995** · member **4.362.090** · catalog **4.358.783** · publish **20.080** · sidecar **17.935** |
| Orphans | **0** |
| Letzter Lauf | Phase **4X** · nohup `may2026_monthly_runner` · **184** Batches `readback_ok` |

### Phase 4X — Kurzprotokoll

| Feld | Wert |
|------|------|
| Monatsplan | `phase4w_may2026_monthly_run_plan_v1.json` |
| Backup (pre-Mai) | `ebfdfb7d-ba12-4c2a-af6b-ff9c5ca57b02` |
| Scope-Writes Mai | **9.114** meta/sidecar/publish · **1.984.418** member/catalog |
| Fortschritt | `doge_apr_sep_2026_v1_history/_controlled_import_progress_phase4x/` |
| Logs | `logs/historical_import/may2026/phase4x_*` |

---

## 3. Monatsstatus Apr–Sep 2026 (Katalog)

| Monat | 15m_PD1 | 15m_PD5 | 15m_PD10 | 4h_PD10 |
|-------|---------|---------|----------|---------|
| 2026-04 | ✓ | ✓ | ✓ | ✓ |
| 2026-05 | ✓ | ✓ | ✓ | ✓ |
| 2026-06 … 09 | offen | offen | offen | offen |

---

## 4. Freigaben

| Schritt | Status |
|---------|--------|
| Juni 2026 Monatsimport-Vorbereitung | **offen** (empfohlen: post-4X Backup) |
| Git commit/push | **NEIN** |

---

## 5. Verweise

- `MASTER_PROJECT_CONTEXT.md`
- `manifests/phase4w_may2026_backup_binding.json`
- `historical_json_import/phase4x/phase4x_start_may.sh`
