# PHASE 6XT – 15M MULTI-PD LIVE ACTIVATION V1

**Date:** 2026-10-21  
**VERDICT:** **`15M_MULTI_PD_DASHBOARD_ACTIVATED_PASS`**

Kontrollierter Reload der kanonischen Spread-Dashboard-Instanz. Die Live-Window-API auf `127.0.0.1:3000` liefert 15m PD1, PD5 und PD10. Keine ClickHouse-Writes, keine neuen Imports, keine Density- oder Canvas-Änderung, kein Git-Commit.

**Referenz:** `reports/PHASE_6XT_15M_MULTI_PD_MINIMAL_VIEWER_RELEASE_V1.md`  
**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_15m_multi_pd_dashboard_activation_v1/`

---

## 1. Preflight

| Check | Ergebnis |
|-------|----------|
| Schreibzugriff auf Contract, Exact-Origin-JS, Adapter, `chart.js` | kein offener Writer (`lsof`) |
| Disk-Scopes | `("15m", 1)`, `("15m", 5)`, `("15m", 10)`, `("5m", 1)`, `("5m", 5)`, `("5m", 10)` |
| 5m-Marker auf Disk | `("5m", 10)` und `mergedSegments.concat` unverändert vorhanden |
| Publish-Lines, `SELECT` `FINAL`, `publish_complete=1` | 15m PD1/PD5/PD10 je **17568** (2026-04-01 00:00Z–2026-09-30 23:45Z); 5m PD1/PD5/PD10 je **2592** |
| Fingerprints 2026-09-16 00:00Z | PD1 `50e16fc8…`, PD5 `4ef7e733…`, PD10 `ffd67753…` |
| Laufender Prozess vor Reload | PID **1643583**, Start 2026-10-21 13:54:48 +0200, CWD Spread-Dashboard, `HISTORICAL_LMV2_BATCH_READER=1`, PYTHONPATH Spread → Dashboard → Pools → TRP |
| Contract/JS-mtime | 14:28 +0200, nach dem Prozessstart. RAM-Contract kannte 15m PD5/PD10 noch nicht. Reload erforderlich. |

---

## 2. Runtime

| | Vorher | Nachher |
|--|--------|---------|
| **PID** | **1643583** | **1655845** |
| **Gestartet** | 2026-10-21 13:54:48 +0200 | 2026-10-21 14:48:04 +0200 |
| **CWD** | `spread_recovery_hedge_short_dev/dashboard` | unverändert |
| **Listener** | ein Prozess auf `0.0.0.0:3000` | ein Prozess auf `0.0.0.0:3000` |
| **Apache :8080** | Master PID **795137**, Start 2026-10-20 00:05:55 +0200 | dieselbe PID, derselbe Start. Port 8080 nicht neu gestartet. |

Startkommando unverändert:

```text
cd spread_recovery_hedge_short_dev/dashboard
PYTHONPATH=spread:dashboard:pools+ob+delta_bot:trading_research_platform
HISTORICAL_LMV2_BATCH_READER=1
nohup .venv/bin/python app.py >> logs/dashboard.log
```

SIGTERM nur an PID 1643583. Port 3000 war frei, bevor die neue Instanz gebunden hat. Keine zweite `app.py`.

**Importpfade** (dieselbe Startumgebung, nach `import research_charts.api`):

- `research_charts.historical_lmv2_pd_contract_v1` → Spread `…/dashboard/research_charts/historical_lmv2_pd_contract_v1.py`, **6** Scopes. Die Live-Capabilities mit 15m PD5/PD10 können nur aus diesem Modul kommen.
- `pd1_landscape_viewer_v1` → Pools `…/backtester/short/dashboard/pd1_landscape_viewer_v1/`, weil `ensure_strategy_paths` den Pools-Dashboard-Pfad voranstellt. `historical_window_v1.py` ist bytegleich zur Spread-Kopie, SHA256 `4e12bede667b36dc5a80422c42488b089099cea07726bbe6e02682a4d25b8dbc`.
- Pools-Contract-Shadow hat kein `ALLOWED_VIEWER_SCOPES` und wird für die Allowlist nicht verwendet. Kein rsync.
- Static-JS kommt aus dem Spread-Dashboard.

---

## 3. Live-API vor dem Reload

Quelle: `probe_pre_restart.json`. Authentifiziert gegen `127.0.0.1:3000`.

| Test | HTTP | Ergebnis |
|------|-----:|----------|
| Capabilities | 200 | **4** Scopes: 15m PD1, 5m PD1/PD5/PD10 |
| 15m PD1, 16.–17.09. 23:45Z | 200 | **574** Preise, Slot 15, `decision_closes` **864**, Line `…_15mpd1_v1`, Hash `4aeb36fe…` |
| 15m PD5 / PD10 | **422** | `INVALID_SCOPE` |
| 5m PD1 / PD5 / PD10, bis 23:55Z | 200 | je **576** Preise, Slot 5, Lines `…_5mpd1/5/10_v1` |
| 4h PD10, 15m PD3, 30m PD1 | **422** | `INVALID_SCOPE` |
| Default-15m-Regression | 200 | **97** Preise, Line `…_15mpd1_v1` |

Served JS enthielt bereits `phase6xt-15m-multi-pd-viewer-v1` (StaticFiles liest von Disk). Die API im RAM blieb beim alten Contract.

---

## 4. Live-API nach dem Reload

Quelle: `probe_post_restart.json`.

**Capabilities:** **6** Scopes. Jede 15m-Line mit eigenem `pipeline_line_id` und `config_hash`, Datenfenster 2026-04-01 00:00Z–2026-09-30 23:45Z, Slot 15.

Referenzfenster `2026-09-16T00:00:00Z`–`2026-09-17T23:45:00Z`:

| Scope | HTTP | Preise | Pools | Slot | Kontext | `decision_closes` | Line | Hash |
|-------|-----:|-------:|------:|-----:|---------|------------------:|------|------|
| 15m PD1 | 200 | **574** | 285 | 15 | 2026-09-09T00:00:00Z | **864** | `…_15mpd1_v1` | `4aeb36fe…` |
| 15m PD5 | 200 | **574** | 311 | 15 | 2026-09-09T00:00:00Z | **864** | `…_15mpd5_v1` | `533e04c7…` |
| 15m PD10 | 200 | **574** | 410 | 15 | 2026-09-09T00:00:00Z | **864** | `…_15mpd10_v1` | `bd9ac4f30e…` |

`decision_closes` 864 ist die vom Server geladene Membership über den Kontext: **192** sichtbare 15m-Closes im Fenster und **672** historische Kontext-Closes ab 2026-09-09 00:00Z. Preis-SHA256 der drei 15m-Antworten identisch (`5fe6219e…`). `bad_bounds=0`, `missing_origin=0`. Pool-Zahlen 285/311/410 entsprechen dem Release-Readback.

Fingerprint-Parität auf den Publish-Lines, Close 2026-09-16 00:00Z: PD1 `50e16fc8ac56e3468413379e292b58b548e6108c730d4c419aba00440880e457`, PD5 `4ef7e7333f080cba162accb5645225806d94c0a79c6f1adfc219a9ecbad33876`, PD10 `ffd67753c8d8fc9b26362aa462f5c264b607e2f3a0f4cc8e00c9c6b8d08b3008`.

5m-Referenz bis `2026-09-17T23:55:00Z`:

| Scope | HTTP | Preise | Pools | Slot | Line |
|-------|-----:|-------:|------:|-----:|------|
| 5m PD1 | 200 | **576** | 599 | 5 | `…_5mpd1_v1` |
| 5m PD5 | 200 | **576** | 692 | 5 | `…_5mpd5_v1` |
| 5m PD10 | 200 | **576** | 913 | 5 | `…_5mpd10_v1` |

5m-Preis-SHA256 identisch über die drei PDs und identisch zum Pre-Reload (`7daa7ad4…`). 15m-PD1-Preis-SHA und die 97er-Regression (`a92348e7…`, 182 Pools, Line `…_15mpd1_v1`) sind vor und nach dem Reload gleich.

Nicht freigegebene Scopes bleiben **HTTP 422** `INVALID_SCOPE`: 4h PD10, 15m PD3, 30m PD1.

---

## 5. Regressionen

Keine. 15m PD1, die 97er-Regression und 5m PD1/PD5/PD10 liefern dieselben Preis-SHA und Pool-Zahlen wie vor dem Reload. Apache 8080 unverändert. Eine Listener-Instanz auf Port 3000.

---

## 6. Nicht ausgeführt

Keine ClickHouse-Writes, keine Imports, keine Generatoren, keine Änderung an Density, Canvas, Adapter oder `historical_window_v1.py`, kein Git-Commit, kein Push.
