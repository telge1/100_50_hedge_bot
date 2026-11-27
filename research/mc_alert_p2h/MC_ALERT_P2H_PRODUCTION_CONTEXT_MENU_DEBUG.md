# MC-ALERT-P2H — Production Context Menu Debugging

**Endstatus:** `MC_ALERT_P2H_BROWSER_DIAGNOSTICS_REQUIRED`  
**Datum:** 2026-11-27  
**URL (Nutzer):** `http://dash.immotel.de:8080/live-charts/research?view=multi`

---

## Executive Summary

| Befund | Details |
|--------|---------|
| HTTP-Assets (vor Fix) | **Pane + chart.js P2G bereits korrekt** auf Origin (SHA256 = Disk) |
| Unit-Tests allein | Reichten **nicht** — Nutzer sieht kein Menü in Multi-2×2 |
| Wahrscheinliche Ursache (nachgewiesen per Architektur + Code) | **Iframe-Menüposition:** `position:fixed` im Pane nutzte **Top-Level-`clientX/Y`**, Menü **außerhalb** des sichtbaren Iframe-Bereichs |
| Zusatz-Risiko | `priceAtEvent` ohne Y-Clamping → leerer Preis → früher `return` ohne Menü |
| Fix (minimal, nur chart/host cache) | `contextMenuViewportXY`, robustes `priceAtEvent`, `ASSET_V` → `research-mc-alert-p2h-v1` |
| Dashboard | **Restart durchgeführt** (Template-Query + statische JS); **kein** Collector-Restart |

**Browser-Verifikation durch Nutzer steht noch aus** → kein `MC_ALERT_P2H_BROWSER_VERIFIED`.

---

## 1. HTTP-Asset-Kette (Produktion, unauthentifiziert)

| Ressource | URL | SHA256 (HTTP = Disk, 2026-11-27) | Disk match |
|-----------|-----|----------------------------------|------------|
| pane.html | `?v=research-mc-alert-p2h-v1` | `193510297c96d9eb170b2b1495c8b298ce350ab85aac87941142e5e976526e2f` | Ja |
| chart.js | `?v=mc-alert-p2h-v1` | `7c60c7d38c82ed6d2dbcf5edd74e24b3e3ebe05542ee43a8f43a5094fd124d97` | Ja |
| research_charts.js | `?v=research-mc-alert-p2h-v1` | `b952a378afe7a999aecfb9df53280b3bee71a021f72e43e862ad04d0ac18a556` | Ja |

**Hinweis:** Änderungen nur unter `spread_recovery_hedge_short_dev` (produktiv), nicht nur im Worktree `spread_recovery_mc_alert_p2b`.

**Research-HTML** (`/live-charts/research?view=multi`): **401** ohne Session — erwartet.

### Iframe-URL (Host)

```text
/static/research_trp/pane.html?v= + ASSET_V
```

- **Vor P2H:** `ASSET_V = research-mc-opi-p3u-v1` (Browser-Cache-Risiko auf alte pane-Shell)
- **Nach P2H:** `ASSET_V = research-mc-alert-p2h-v1`

HTTP-Probe `pane.html?v=research-mc-opi-p3u-v1` lieferte **bereits** den neuen Inhalt (`chart.js?v=mc-alert-p2g-v1`) — Server korrekt; Client-Cache kann trotzdem alt halten → neues `ASSET_V` erzwingt Reload.

### Call-Chain

```text
research_charts.html (auth)
  → research_charts.js (Host, Bridge MCPriceAlert)
  → 4× iframe.trp-pane-frame
      → pane.html?v=ASSET_V
          → chart.js?v=mc-alert-p2h-v1
          → contextmenu auf #chart + #price-pane
          → Menü #chart-ctx-menu an iframe document.body
```

`price_alert_chart.js` nur im **Host** — Alert-Bridge `win.bridge.on_price_alert_create` wird bei iframe-`load` gesetzt; **Menü muss auch ohne Bridge sichtbar sein** (nur Create schlägt ggf. fehl).

---

## 2. Template- / Static-Cache

- **P4B2-Analogie:** Geändertes `research_charts.html` (Script-`?v=research-mc-alert-p2h-v1`) kann im laufenden Prozess gecacht sein → **kontrollierter Dashboard-Restart** (nur Dashboard).
- Log: `research/mc_alert_p2h/dashboard_restart_p2h.log` (Restart **2026-11-27T12:04:59Z**, Dashboard-PID **2364369** / danach **2364395**)
- Rollback Template: `research/mc_alert_p2h/rollback_20261127_p2h/research_charts.html`

---

## 3. Root-Cause-Analyse (Rechtsklick)

| Schritt | Befund |
|---------|--------|
| Handler registriert? | Ja — `el` + `pricePane` `contextmenu` → `onChartContextMenu` |
| Server-JS enthält Menü? | Ja — `chart-ctx-menu`, zwei Buttons |
| P2G-Label / kein Sofort-Copy | Ja in ausgeliefertem JS |
| **Iframe** | Multi = **nur** Iframes; Menü `position:fixed` **im iframe-Dokument** |
| **Position-Bug** | `menu.style.left = ev.clientX` — in eingebetteten Panes oft **Viewport des Top-Fensters**, Menü **clip/unten sichtbar** |
| Preis leer | `!text` → stiller Abbruch; Copy + Menü weg |

### Fix (nur `chart.js` + Cache-Bust)

1. `contextMenuViewportXY(ev)` — bei `window.frameElement` Offset subtrahieren  
2. `priceAtEvent` — gleiches Y-Clamping wie `marketPointFromXY`  
3. `pane.html` → `chart.js?v=mc-alert-p2h-v1`  
4. `research_charts.js` → `ASSET_V = research-mc-alert-p2h-v1`  
5. `templates/research_charts.html` → `research_charts.js?v=research-mc-alert-p2h-v1`

**Nicht geändert:** Monitor, Collector, Pool, OB, Trading, übrige Chart-Tools.

---

## 4. HTTP / Clipboard

- Nutzer nutzt **HTTP :8080** → `window.isSecureContext` typisch **false**
- `navigator.clipboard` oft **nicht** verfügbar → bestehender **`copyTextFallback`** (`execCommand`) bleibt maßgeblich
- Menü-Anzeige **ohne** Secure Context erforderlich (Fix oben)

---

## 5. Nutzer-Verifikation

1. **Hard-Reload** auf Multi-URL (Ctrl+Shift+R) oder Cache leeren.
2. Rechtsklick **in die Kerzenfläche** eines Pane.
3. Erwartung: Menü mit **🔔 Preisalarm erstellen** + **Preis kopieren** an der Maus.

### Diagnose-Skript (Konsole)

Datei: [`p2h_browser_context_menu_diag.js`](p2h_browser_context_menu_diag.js)

1. Auf Multi-Seite einloggen  
2. Inhalt der Datei in DevTools-Konsole einfügen  
3. Rechtsklick im Chart  
4. Ausführen: `copy(JSON.stringify(window.__MC_P2H_CTX_DIAG__, null, 2))`  
5. JSON hier posten (keine Cookies/Tokens)

Erwartung nach Fix: `menuAfterCtx.hidden === false`, `buttons: 2`, `chartJs` enthält `mc-alert-p2h-v1`.

---

## 6. Tests (lokal)

`test_chart_context_menu_alert_and_copy_price_regression` — **passed** (inkl. `contextMenuViewportXY`, `ASSET_V` p2h).

---

## 7. Rollback

1. `chart.js` / `pane.html` / `research_charts.js` / `research_charts.html` aus Git/Backup vor P2H  
2. `systemctl --user restart dashboard.service`

---

## 8. Nächster Endstatus

Nach erfolgreichem Nutzer-Test: **`MC_ALERT_P2H_BROWSER_VERIFIED`** (manuell setzen / bestätigen).