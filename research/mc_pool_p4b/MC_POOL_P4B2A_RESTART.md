# MC-POOL-P4B2A — Controlled Dashboard Restart

**Status:** `MC_POOL_P4B2A_RESTART_OK_BROWSER_READY`  
**Datum:** 2026-11-26  
**Aktion:** `systemctl --user restart dashboard.service` (einziger Service-Neustart)

---

## 1. Vor Neustart — Service & Pfade

| Feld | Wert |
|------|------|
| Unit | `dashboard.service` (user) |
| WorkingDirectory | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` |
| ExecStart | `…/spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| Port | 3000 (Host 0.0.0.0) |
| Reload | `False` (Produktion) |
| Vorherige PID | 1928817 (seit 11:25:13 CET) |
| Nachherige PID | 1986911 (seit 13:37:20 CET) |

**Produktive Dateien (P4B/P4B1):**

- `static/js/research/pd1_exact_origin_density_research_v1.js`
- `static/js/research/research_charts.js`
- `templates/research_charts.html` (Default-`{% else %}`-Zweig)

---

## 2. P4B2A-Backup (selektiver Rollback — **nicht** 22.11.-.bak)

Verzeichnis:

`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/.rollback_mc_pool_p4b2a_20261126T123710Z/`

| Datei | SHA-256 |
|-------|---------|
| `pd1_exact_origin_density_research_v1.js` | `e5073ab84a11030ef7dba8f13ad982462fb87a24f8e6bc6b409bf9148f0863e4` |
| `research_charts.js` | `881aca0d70cc8a5b20725cbd1a0f109df4ce23eea4b612e7659b7ed8dc6824b6` |
| `research_charts.html` | `682a16c888074529a9f956d100d4d3b746c300b2d6abeff4a287ab28c9294503` |

**Rollback P4B only:** drei Dateien aus diesem Ordner nach `dashboard/` kopieren (optional nur `research_charts.html` für alte `?v=`).

**Nicht verwenden:** `pd1_exact_origin_density_research_v1.js.bak-20261122T092540Z` — würde MC-LOAD-P4J und spätere Änderungen vernichten.

---

## 3. Nach Neustart — Health

| Check | Ergebnis |
|-------|----------|
| `systemctl --user is-active dashboard.service` | `active` |
| Startfehler in Journal (seit Restart) | keine ERROR/Traceback-Zeilen |
| Static `pd1_exact_origin_density_research_v1.js` | HTTP 200 |
| Static `research_charts.js` | HTTP 200 |
| `GET /live-charts/research` (ohne Auth) | HTTP 401 |

**Asset-Versionen auf Disk (Template):**

- `pd1-mc-pool-p4b-v1`
- `research-mc-pool-p4b-v1`

**HTML mit neuen `?v=` im gerenderten Response:** **BROWSER-PENDING** (401 ohne Session). Nach Login: View Source → beide Strings müssen sichtbar sein.

---

## 4. Referenz

- P4B2 Aktivierung: `MC_POOL_P4B2_CONTROLLED_ACTIVATION.md`
- B1 Equality: `MC_POOL_P4B2_B1_EQUALITY_SNAPSHOT.js`
- P4A Harness: `../mc_pool_p4a/MC_POOL_P4A_BROWSER_MEASUREMENT_CONSOLE.js`

**Token:** `MC_POOL_P4B2A_RESTART_OK_BROWSER_READY`