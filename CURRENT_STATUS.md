# CURRENT STATUS — pools+ob+delta_bot / DOGEUSDT LMV2

**Stand:** 2026-10-11 (nach Git-Cleanup C1–D und Etablierung der Root-Dokumentation). **Aktualisieren:** nach Phasenabschluss oder auf Anforderung.

---

## 1. Git

| Feld | Wert |
|------|------|
| Branch | `pools-ob-delta-bot` |
| **Letzter Code-/Hygiene-Commit vor Root-Doku** | `aae3d3f2` — `chore: extend gitignore for research results and runtime artifacts` |
| **Zentrale Dokumentation** | `MASTER_PROJECT_CONTEXT.md`, `CURRENT_STATUS.md` (Repository-Root) |
| Remote | `origin/pools-ob-delta-bot` — **Push nicht erfolgt** (lokale Commits noch nicht auf GitHub; Zähler: `git rev-list --left-right --count origin/pools-ob-delta-bot...HEAD`) |
| LMV2/Dashboard-Checkpoints | `3c21f371` Import · `133ab776` Chart/API · `77fe9b9c` EMA V2 |
| Forschungs-Cleanup | `004286e2` Phase11v · `3b0e849f` Phase11u_f · `bce35ddb` april_smoke · `aae3d3f2` gitignore |
| Working tree | `indicator_settings.json` modified (tracked legacy; `user_data/` per `.gitignore` für neue Adds) |

---

## 2. Historischer Katalog (Disk, extern zu Git)

| Metrik | Stand |
|--------|--------|
| Original-V2-Snapshots im Katalog | **53.802** (Apr–Sep 2026) |
| Pfad | `.../doge_apr_sep_2026_v1_history/production/results/` (ignoriert) |

---

## 3. ClickHouse (verifiziert Phase 4H)

| Metrik | Stand |
|--------|--------|
| **Historische Snapshots importiert (gesamt)** | **160** |
| August-Pilot (15m PD10) | **10** |
| April 4h PD10 (Produktionslinie) | **150 / 180** |
| Letzter Batch | `phase4h_second_100_apr2026_4hpd10_v1` — **100/100** Readback + Sidecar PASS |
| Backup (vor 100er-Batch) | `e134b0fa-9cba-4c0f-b8d9-60125001de50` |

**Weitere historische Imports:** **nicht freigegeben.**

---

## 4. Tests (letzter grüner Lauf)

| Suite | Ergebnis |
|-------|----------|
| LMV2 erweitert | **117 passed** |
| Phase-4 Import | **74 passed** |
| Research Chart/API | **21 passed** |
| EMA V2 | **14 passed** |
| Phase 11V-A | **17 passed** (TRP-Pfad) |
| Phase 11U-F viewer | **5 passed** |
| April smoke (Abhängigkeiten) | **43 passed** |

---

## 5. Implementiert / getestet / abgenommen / Freigabe

| Thema | Impl. | Getestet | CH/Abnahme | Freigabe nächster Schritt |
|-------|:-----:|:--------:|:----------:|---------------------------|
| CH Import + Sidecar | ✓ | ✓ | ✓ (160 Snapshots) | Weitere Batches: **NEIN** |
| Research API + Remaining | ✓ | ✓ | — | Chart-Gesamtabnahme offen |
| EMA V2 Dashboard | ✓ | ✓ | — | optional |
| Phase11v / 11u_f Tools | ✓ | ✓ | — | in Git (C1/C2) |
| Root Master + Status | ✓ | Review | — | versioniert mit Commit E |
| GitHub Push | — | — | — | **ausstehend** (separate Freigabe) |

---

## 6. Nächster freigegebener Schritt

1. **GitHub-Push** der lokalen Commit-Kette nur nach **separater** Freigabe und Review.
2. **Kein** weiterer historischer CH-Import ohne neue Batch-Freigabe.
3. Chart-/Backtest-Gesamtabnahme gemäß `MASTER_PROJECT_CONTEXT.md` §5.

---

## 7. Verweise

- `MASTER_PROJECT_CONTEXT.md`
- `docs/live_micro_lld_v2/DEVELOPMENT_CHECKPOINT.md`
