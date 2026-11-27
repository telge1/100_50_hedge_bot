# MC-ALERT-P3 — Final Recovery Checkpoint, Commit & Push

**Endstatus:** `MC_ALERT_P3_FULL_RECOVERY_CHECKPOINT_VERIFIED`  
**Datum:** 2026-11-27  
**Nutzer-Browser:** Rechtsklick-Menü Multi-2×2 auf `http://dash.immotel.de:8080/live-charts/research?view=multi` — **PASS** (vom Nutzer bestätigt).

---

## 1. Repositories & Remotes

| Projekt | Pfad | Branch | Remote | Push |
|---------|------|--------|--------|------|
| Dashboard | `spread_recovery_hedge_short_dev` | `feature/ema-touch-structure-v2` | `origin` → `telge1/100_50_hedge_bot` | OK |
| Collector | `Signal_Generator_Ralf/signal_generator_stoch_waves` | `main` | `research_push` → gleiches GitHub-Repo | OK (Branch `main` neu auf Remote) |
| Docs | `pools+ob+delta_bot` | `pools-ob-delta-bot` | `origin` | OK (rebase auf `3cf6449f`) |

**Worktree `spread_recovery_mc_alert_p2b`:** nicht committiert (isolierter Dev-Stand; produktiver Code = `spread_recovery_hedge_short_dev`).

---

## 2. Commits & Tags

| Repo | Commit | Message | Annotated Tag → `^{commit}` |
|------|--------|---------|----------------------------|
| Dashboard | `255c91111d062cc9fcf564bfe3a3808fee68936e` | `feat(price-alert): integrate chart context menu and centralized live price alerts` | `stable-mc-alert-p2h-20261127` → **255c9111** |
| Collector | `0470cc47ae8d0dc0b7b54381c482f8b5ad5bed75` | `feat(collector): expose existing public trade stream through bounded fanout` | `stable-collector-trade-fanout-p2e-20261127` → **0470cc4** |
| Docs | `230f7713929c9e2dd2c4b14c6ee72f3a0467daf3` | `docs(research): finalize MC-ALERT integration and recovery checkpoint` | — |

**Unveränderte Tags:** `stable-mc-pool-p9-20261126`, `stable-mc-baseline-pre-pool-p1`.

**P2E Collector-Checkpoint:** `research/mc_alert_p2e/recovery_checkpoint_20261127_p2e/` (in Docs-Commit enthalten).

---

## 3. P3 Recovery-Checkpoint (lokal)

**Pfad:** `research/mc_alert_p3/recovery_checkpoint_20261127/` (~144 KiB)

- `sha256/PROD_FILES.sha256` — chart.js, pane.html, research_charts.js, monitor, fanout module
- `systemd/` — `dashboard.service`, `bybit-live-1m-collector.service` (Snapshots)
- `RESTORE.md` — Kurzanleitung
- `*_git_status.txt`, `*_HEAD_pre.txt` — Zustand vor P3-Commits

**Keine Secrets** in Git (keine `.env` committed).

---

## 4. Produktive Datei-Prüfsummen (laufender Stand = Commits)

| Datei | SHA256 |
|-------|--------|
| `chart.js` | `7c60c7d38c82ed6d2dbcf5edd74e24b3e3ebe05542ee43a8f43a5094fd124d97` |
| `pane.html` | `193510297c96d9eb170b2b1495c8b298ce350ab85aac87941142e5e976526e2f` |
| `research_charts.js` | `b952a378afe7a999aecfb9df53280b3bee71a021f72e43e862ad04d0ac18a556` |
| `price_alert_monitor.py` | `641a78f8f4aafd502f422132187d019e12e93626e7a06807e5315c6263125f6d` |
| `public_trade_event_fanout.py` | `c5bc8ce2fcb54c9fe36c897131f55539e98b7510ed264bfada92f0e65c5a450e` |

Cache-Buster: `ASSET_V=research-mc-alert-p2h-v1`, `chart.js?v=mc-alert-p2h-v1`, `research_charts.js?v=research-mc-alert-p2h-v1`.

---

## 5. Regressionstests (vor Commit, lokal)

| Suite | Ergebnis |
|-------|----------|
| `test_price_alert_monitor_safety` + `test_price_alert_store` | **15 passed** |
| `test_chart_context_menu_alert_and_copy_price_regression` | **passed** |
| `test_shift_measure_host_shift_copy_price_and_crosshair` | **passed** |
| `test_research_charts_phase1` | **10 passed** |
| `test_public_trade_event_fanout_v1` (Collector) | **9 passed** |

---

## 6. Im Commit enthalten (selektiv)

### Dashboard (13 Dateien)

`app.py`, `price_alert_monitor.py`, `price_alert_store.py`, `price_alert_chart.js`, `research_charts.js`, `chart.js`, `pane.html`, `style.css`, `research_charts.html`, `research.css`, Tests, `test_research_charts_interaction.py`.

**Nicht** committiert: übrige 280+ dirty Dashboard-Dateien, `research_charts/api.py`, Load-Tests, `.bak`-Artefakte.

### Collector (4 Dateien)

`public_trade_event_fanout.py`, `collector.py`, `control_api.py`, Fanout-Unit-Test.

**Nicht** committiert: live_universe, backfill, spool, weitere lokale WIP-Änderungen.

### Docs

`research/mc_alert_p1` … `mc_alert_p2h`, `mc_alert_p3/recovery_checkpoint_20261127/`.

---

## 7. Nicht in Git / Laufzeit

| Zustand | Ort |
|---------|-----|
| Aktive Alerts | `spread_recovery_hedge_short_dev/data/state/price_alert.json` (falls angelegt) |
| Fired Events | `…/price_alert_fired.jsonl` |
| Fanout-Subscriber im RAM | Collector :8787 — nach Restart neu subscriben |
| CH Public Trades | ClickHouse — kontinuierlich; kein Snapshot im Commit |
| Produktionsprozesse | PID Dashboard/Collector unverändert in P3 (kein Restart) |

---

## 8. Verifikation Browser (explizit)

| Test | Status |
|------|--------|
| Rechtsklick-Menü Multi-2×2 | **PASS** (Nutzer) |
| Audio-Deduplizierung zwei Tabs | **Offen** |
| E2E Trigger-Latenz & Alarm-Ton | **Offen** |
| Vollständiger Alert-Fire über Ntfy in Prod | **Offen** |

---

## 9. Wiederherstellung

```bash
# Dashboard
cd spread_recovery_hedge_short_dev
git fetch origin
git checkout stable-mc-alert-p2h-20261127   # → 255c9111
# Nur bei Restore: systemctl --user restart dashboard.service

# Collector
cd Signal_Generator_Ralf/signal_generator_stoch_waves
git fetch research_push
git checkout stable-collector-trade-fanout-p2e-20261127   # → 0470cc4
# Nur bei Restore: systemctl --user restart bybit-live-1m-collector.service

# Docs / Forensik
cd pools+ob+delta_bot
git checkout 230f7713   # oder origin/pools-ob-delta-bot
```

Rollback P2F Dashboard-Module: `research/mc_alert_p2f/rollback_20261127/`.

---

## 10. Verbleibende lokale Änderungen

- **Dashboard:** großer dirty Worktree (EMA/Load/MC-Pool parallel) — bewusst **nicht** gepusht.
- **Collector:** weitere PT/Backfill-Änderungen — **nicht** gepusht.
- **pools+ob+delta_bot:** Backtester/PD1/Tools WIP — **nicht** gepusht.

---

## 11. Dokumentationskette

P1 → P2 → P2A–P2H unter `research/mc_alert_*`; P2E/P2F/P2H Aktivierungs- und Debug-Berichte inkl. Recovery-Artefakte.