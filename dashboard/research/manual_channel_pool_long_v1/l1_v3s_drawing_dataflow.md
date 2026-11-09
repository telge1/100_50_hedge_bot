# L1 V3S — Research-Chart Zeichnungs-Datenfluss (READ-ONLY Audit)

## 1. Führende Quelle für Backtests

| Pfad | Rolle |
|---|---|
| `research_charts/user_data/drawings.json` | **Kanonisch / SSOT** für Server-Workspace und alle L1-Backtester |
| `research/manual_channel_pool_long_v1/l1_v3*_drawings_snapshot_*.json` | **Nur Output** des jeweiligen Laufs (Audit); **kein** Eingabe-Merge |
| `static/**/user_data/drawings.json` | Alte **Template-/TRP-Bundles** (hunderte Zeichnungen); **nicht** die Live-Research-Zeichnungen |

Backtester laden ausschließlich über:

- `run_l1_pilot.py` → `DRAWINGS_PATH` = `research_charts/user_data/drawings.json`
- `run_l1_v3l_independent_support.py` → `_run()` → `json.loads(DRAWINGS_PATH.read_text())`
- `run_l1_v3s_full_backtest.py` → `drawing_refresh.refresh_drawings_for_run()` → danach `v3l._run()` (erneutes Lesen derselben Datei)

**Kein** Einlesen historischer V3J/V3L/V3P-Snapshots als aktuelle Geometrie.

## 2. Research-Chart → Server

- Workspace: `research_charts/workspace_session.py`
  - `DRAWINGS_PATH = user_data/drawings.json`
  - Start: `_load_persisted_drawings()` → TRP `load_drawings(path)`
  - Mutation (zeichnen, ziehen, löschen, Stil): In-Memory `DrawingManager`
  - Persistenz: `persist_drawings()` → TRP `save_drawings(path, …)` bei u. a. `delete_selected()`, `on_drag()`, `clear_drawings()`

- API (FastAPI): `research_charts/api.py`
  - `POST /api/research/drawings/event` (Punkte, Drag)
  - `POST /api/research/drawings/delete` (löscht Auswahl + `persist_drawings`)
  - Kein separates „Speichern“-Endpoint nötig — **Löschen/Ändern schreibt sofort** die JSON.

- Frontend: `static/js/research/research_charts.js`
  - Zeichnungen werden per `sendJson(…/drawings/…)` an den Server gesendet.
  - **`localStorage`**: Layout, Sync-Flags, Diagnose — **keine** persistenten Trendlinien-Geometrien.
  - Browser-Cache kann alte **JS-Bundles** liefern, nicht die Live-`drawings.json` auf dem Server.

## 3. Warum V3R/V3Q-„Löschung“ nicht griff

1. Backtests lesen **nur** `research_charts/user_data/drawings.json`, nicht den Chart-Bildschirm.
2. Löschen wirkt erst nach erfolgreichem `POST …/drawings/delete` → `persist_drawings()`.
3. Zwischenläufe zeigten: andere ID entfernt (`d-03350098db64`), **`d-ac60572bfc69` blieb** — Nutzererwartung ≠ persistierter Stand.
4. Statische Repo-Kopien (`static/.../drawings.json`) können abweichen; der Backtester nutzt sie **nicht**, verwirren aber bei manuellem Öffnen.

## 4. V3S Refresh-Ablauf (neu)

`drawing_refresh.py`:

1. Nur `CANONICAL_DRAWINGS_PATH` lesen  
2. SHA256, IDs, Validierung  
3. `l1_v3s_drawings_backup_<run_id>.json` (Sicherung vor Lauf)  
4. `l1_v3s_drawings_snapshot_<sha16>.json` (unveränderlicher Lauf-Snapshot, überschreibt Snapshot **mit gleichem SHA** idempotent)  
5. Warnung bei abweichenden `STALE_COPY_PATHS`  
6. `run_l1_v3s_full_backtest.py` startet Simulation

**Keine** Strategieänderung; keine Änderung an `drawings.json` durch den Refresh selbst.

## 5. Verifikation vor Backtest

Manifest-Felder: `drawings_sha256`, `drawing_ids_all`, `support_line_ids`, `drawing_id_diff_vs_v3p_snapshot`, `refresh_warnings`.

Bei Konflikt **Browser vs. Datei**: manuell `drawings.json` prüfen oder Delete im Chart und SHA erneut vergleichen — **nicht** Snapshots mergen.
