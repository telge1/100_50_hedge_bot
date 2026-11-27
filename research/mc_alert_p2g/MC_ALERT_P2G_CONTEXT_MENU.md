# MC-ALERT-P2G — Kontextmenü: Copy + Alert (verbindlich)

**Endstatus:** `MC_ALERT_P2G_CONTEXT_MENU_READY`  
**Datum:** 2026-11-27

## Änderung

Datei: `spread_recovery_hedge_short_dev/dashboard/static/research_trp/chart.js`

- **Erweiterung** des bestehenden `onChartContextMenu` (kein separater Handler).
- Preis weiter über `priceAtEvent` → `candleSeries.coordinateToPrice`.
- **Kopieren** nur bei Klick auf „Preis kopieren“ via `formatCopiedPrice` + `copyText` + `showCopyToast` (unveränderte Formatierung).
- **Alert** bei Klick auf „🔔 Preisalarm erstellen“ → `window.bridge.on_price_alert_create` (unveränderte API-Brücke in `research_charts.js`).
- Zwei sichtbare Einträge (+ optional „Preisalarm löschen“ wenn Klick auf bestehende Alarmlinie — weiterhin zwei Buttons).
- Kein „Abbrechen“; Schließen per Escape, Außenklick, nach Auswahl.
- Ein delegierter Click-Handler am Menü (keine Listener-Stapelung).

## Regressionstest

`test_research_charts_interaction.py::test_chart_context_menu_alert_and_copy_price_regression`

## Aktivierung

Nach Hard-Reload im Browser; optional `systemctl --user restart dashboard.service` nur wenn statische Assets gecacht werden (Chart-JS ist versioniert in `pane.html`).

**Worktree** `spread_recovery_mc_alert_p2b` — gleicher `chart.js`-Stand.