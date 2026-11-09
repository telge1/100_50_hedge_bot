# L1 V3T — Drawing Health & Persistenz (Implementierung)

## Neu

- `GET /api/research/drawings/health` (auth) → RAM `DrawingManager` vs `drawings.json`
- `research_charts/drawing_health.py` → `compute_drawing_health`
- `persist_drawings()` → log + `DrawingPersistError` (kein stilles `pass`)
- `delete_selected()` → `drawing_delete` mit `deleted_id`, Rollback bei Persist-Fehler, HTTP **503** bei `persist_failed`
- UI `trpDelete` → Fehler/status vor `applyWorkspace`
- `drawing_refresh.assert_backtest_drawing_health()` → Backtest bei `drift=true` abbrechen (wenn Health-Payload übergeben)

## Backtest-Preflight

1. Eingeloggt: `GET /api/research/drawings/health` → `drift` muss `false` sein.
2. CLI-Backtest: nur Datei-SSOT; ohne Health **keinen** RAM-Gleichstand behaupten (`assert_backtest_drawing_health(None)` → Warnung).

## Server-Neustart

Health liest den **laufenden** Singleton-Workspace. Nach Code-Deploy: Dashboard-Prozess neu laden, damit neues Verhalten aktiv ist.
