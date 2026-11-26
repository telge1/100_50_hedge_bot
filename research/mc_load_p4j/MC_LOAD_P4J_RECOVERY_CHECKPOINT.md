# MC-LOAD P4J — Recovery Checkpoint

**Endstatus:** `MC_LOAD_P4J_RECOVERY_CHECKPOINT_VERIFIED`  
**Tag:** `stable-mc-load-p4j` → Commit **`eb9a8edb`** (Code)  
**Docs-Branch:** `pools-ob-delta-bot` → **`2994f30a`** (+ spätere Doku-Commits für Recovery/Baseline)

> **Warnung (MC-BASELINE-01):** Der **live laufende** Dashboard-Stand ist **nicht** identisch mit diesem Tag. Es existieren **uncommittete Änderungen** (Pool-State, PD1, OPI, CSS). Vor jedem `checkout` / `reset` zuerst [**MC_BASELINE-01 Audit**](../mc_baseline_01/MC_BASELINE_01_FULL_RECOVERY_AUDIT.md) lesen und **vollständige Working-Tree-Sicherung** anlegen. **Kein `git reset --hard`** ohne Backup.

---

## GitHub

| Ref | Commit | Inhalt |
|-----|--------|--------|
| `origin/feature/ema-touch-structure-v2` | `eb9a8edb` | MC-LOAD Code (Dashboard) |
| `origin/pools-ob-delta-bot` | `2994f30a` | `research/mc_load_p*` Dokumentation (Basis) |
| `stable-mc-load-p4j` (Tag) | `eb9a8edb` | Annotierter MC-LOAD-Checkpoint |

**Hinweis:** Es gibt **keinen** Remote-Branch `origin/main`; Default auf GitHub ist **`origin/EMA_Bot`** — MC-LOAD liegt auf `feature/ema-touch-structure-v2`.

---

## Wiederherstellung — nur MC-LOAD P4J (bewusst älter als Live)

**Voraussetzung:** Backup des aktuellen dirty Trees (Tarball oder `git stash`/Branch), falls Live-Stand erhalten bleiben soll.

```bash
cd /home/telgenbuescher/projects/spread_recovery_hedge_short_dev
git fetch origin tag stable-mc-load-p4j
git checkout stable-mc-load-p4j
```

Optional eigener Branch vom Tag:

```bash
git checkout -b recovery/mc-load-p4j stable-mc-load-p4j
```

Dokumentation (ohne destruktiven Reset):

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot
git fetch origin pools-ob-delta-bot
git checkout pools-ob-delta-bot
# nur bei explizitem Wunsch und nach Backup:
# git merge --ff-only <commit-mit-baseline-doku>
```

**Nicht empfohlen ohne Backup:** `git reset --hard eb9a8edb` (verwirft uncommittete Pool/PD1/OPI-Arbeit).

---

## Vollständiger Live-Stand (aktuell)

Siehe **MC-BASELINE-01**: Tag `eb9a8edb` **+ 19 modified Dateien** in Spread-`dashboard/` + lokale `.env` / `user_data` / systemd drop-ins. Geplanter nächster Checkpoint: Tag-Vorschlag `stable-mc-baseline-pre-pool-p1` (MC-BASELINE-02).

---

## Dashboard-Deploy (nach Checkout)

```bash
systemctl --user restart dashboard.service
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/login
curl -sS "http://127.0.0.1:3000/static/research_trp/pane.html" | rg 'mc-load-p4i-v1'
```

Unit: `~/.config/systemd/user/dashboard.service` (+ `.d/*.conf`)  
CWD: `.../spread_recovery_hedge_short_dev/dashboard`  
Port: **3000**

---

## Nicht versioniert (lokal sichern, nicht ins Git)

| Pfad | Zweck |
|------|--------|
| `dashboard/logs/sessions.json` | Login-Sessions |
| `dashboard/.env` / Collector `.env` | ClickHouse-Credentials |
| `dashboard/research_charts/user_data/` | Zeichnungen / Indicator-Settings |
| `~/.config/systemd/user/dashboard.service` (+ drop-ins) | Service + Feature-Flags |
| Nginx `dash.immotel.de` → `:3000` | Host-Routing |

---

## Tests nach MC-LOAD-Restore

```bash
cd dashboard
python -m pytest test_mc_load_p3_fast_first_v1.py test_mc_load_p4e_v1.py \
  test_mc_load_p4g_v1.py test_mc_load_p4i_v1.py \
  research_charts/tests/test_candle_cache_limit_hit.py -q
```

---

## Offene Punkte

- Formeller Browser-PASS P4J ausstehend
- Manuelle Chart-Zentrierung über Toolbar ⟲ akzeptiert
- P4K nicht implementiert
- **Live ≠ Tag** bis MC-BASELINE-02