# MC-LOAD-P4F — Live Deployment & Runtime Verification

**Endstatus:** `MC_LOAD_P4F_DEPLOYED_BROWSER_PENDING`

Kein Commit, Push oder funktionaler Code-Change in P4F.

---

## 1. Pre-Deployment

### Tests

| Suite | Ergebnis |
|-------|----------|
| P4E (`test_mc_load_p4e_v1.py`, `test_candle_cache_limit_hit.py`, P3-hooks in gleichem Lauf) | **9 passed** |
| P4B/P3 (`test_mc_load_p3_fast_first_v1.py`) | **3 passed** |

### P4E-Diff (funktional relevant)

Nur MC-LOAD-Pfad:

- `research_charts/service.py` — `_range_end_is_live`, `_tail_limit_candles`
- `static/js/research/research_charts.js` — Phase-B-Range, EMA-Gate, Merge/Inflight, Viewport-Snapshot
- `static/research_trp/chart.js` — `viewportSnapshot`
- Assets `mc-load-p4e-v1` (Template, `pane.html`, `mc_load_diag_v1.js`)
- Tests `test_mc_load_p4e_v1.py`, erweiterte Cache-Tests

**Keine Änderungen** an Trading-Bots, Pool-Berechnungskernen, Collector-Logik oder kanonischer EMA-Formel (nur Pane-Load-Orchestrierung + Candle-Response-Trim).

*(Hinweis: Das Dashboard-Git-Working-Tree enthält weitere unabhängige Änderungen; P4F deployt den aktuellen Stand des Dashboard-CWD wie bei P4B.)*

---

## 2. Live-Deployment (nur Dashboard)

| | Wert |
|---|------|
| **Unit** | `systemctl --user` → `dashboard.service` |
| **Port** | 3000 (`python app.py`) |
| **PID vorher** | **1908793** |
| **PID nachher** | **1917822** |
| **Status** | `active` |
| **Health** | `GET http://127.0.0.1:3000/login` → **200** |

**Nicht** neu gestartet: nginx, ClickHouse, Orderbook, Queue-Consumer, Trading.

Orderbook-Kind unter alter Dashboard-Unit bleibt System-spezifisch (unverändert vs. P4B-Doku).

---

## 3. Deployment-Verifikation (öffentliche Instanz)

**Routing:** `dash.immotel.de` → `127.0.0.1:3000` (wie P4B).

| Asset | Lokal (nach Restart) |
|-------|----------------------|
| Host JS | `research-mc-load-p4e-v1`, `mcLoadHistoryPhaseBRange` in Body |
| Pane | `chart.js?v=mc-load-p4e-v1` |
| Diag | `ASSET_V = "mc-load-p4e-v1"` |
| Research-HTML (auth) | `research-mc-load-p4e-v1` / `mc-load-p4e-v1` |

`https://dash.immotel.de/static/...` von diesem Host aus **Timeout (15–25 s)** — statische P4E-URLs wurden über **127.0.0.1:3000** (gleicher Prozess wie öffentliche Route) verifiziert.

**Backend im laufenden Prozess:** `POST /api/research/pane` nach Restart zeigt P4E-Semantik (s. unten).

### API — P4D-nahe Szenarien (SOLUSDT, 5m, `limit=24192`, auth lokal)

| Szenario | `from` | `to` | cache | `len(candles)` | Erwartung |
|----------|--------|------|-------|----------------|-----------|
| **Historischer Pin** (custom Ende in Vergangenheit) | 1770723300 | 1790764500 | miss→**hit** | **66804** | Vollständiges Fenster, **kein** blinder Tail auf 24192 |
| **Live Phase-B-Äquivalent** (custom `from`, **`to=now`**) | 1770723300 | now | miss | **24192** | `last` ≈ Live-Tip (gemessen **1795686300**) |
| **Rolling unbounded** | — | — | miss→hit | **24192** | `candles_returned=24192`, `cache_tail_trimmed=false` |

→ P4D-Bug-Pfad (`to=1790764500` + limit 24192 → 66804) bleibt für **explizite historische Navigation** korrekt ungetrimmt.  
→ P4E-Pfad (`to≈now` + limit) liefert **24192** statt 66804.

Frontend Phase B nutzt `mcLoadHistoryPhaseBRange()` → dieselbe Live-Semantik (`liveRestorePaneLoadRange` / rolling).

---

## 4. Browser-Diagnose (P4D-Skript, P4E-kompatibel)

Voraussetzungen: eingeloggt auf `/live-charts/research`, Konsole im **Top-Fenster**, Hard-Reload (Assets `mc-load-p4e-v1`).

```javascript
(function MC_LOAD_P4D_RUN() {
  var D = window.__MC_LOAD_DIAG__;
  if (!D) {
    console.error("MC-LOAD-P4D: __MC_LOAD_DIAG__ fehlt. Top-Frame /live-charts/research, Hard-Reload mc-load-p4e-v1.");
    return;
  }
  console.log("MC-LOAD-P4D where", D.where ? D.where() : "?");
  D.enable();
  var loadId = D.start({ symbol: "SOLUSDT", timeframe: "1m", test: "p4f-a-after-p4e" });
  window.__MC_LOAD_P4D__ = { loadId: loadId, samples: [], startedWallMs: Date.now() };
  function paneApi() {
    var fr = document.querySelector(".trp-pane iframe, iframe[src*='pane.html']");
    try {
      return fr && fr.contentWindow && fr.contentWindow.chartApi ? fr.contentWindow.chartApi : null;
    } catch (e) { return null; }
  }
  function sample(tag) {
    var api = paneApi();
    var di = api && api.debugInfo ? api.debugInfo() : { error: "no_chartApi" };
    if (api && api.getVisibleTimeRange) di.visibleTimeApi = api.getVisibleTimeRange();
    var row = { tag: tag, wall_ms: Date.now(), t_rel_ms: Date.now() - window.__MC_LOAD_P4D__.startedWallMs, debugInfo: di };
    window.__MC_LOAD_P4D__.samples.push(row);
    D.emit("p4d_viewport_sample", row);
    return row;
  }
  sample("start_before_tf");
  console.info("MC-LOAD-P4F: JETZT SOLUSDT + 1m→5m wechseln.");
  window.__MC_LOAD_P4D__.interval = setInterval(function () { sample("periodic"); }, 2000);
  setTimeout(function () {
    clearInterval(window.__MC_LOAD_P4D__.interval);
    sample("t60_end");
    D.stop({ test: "p4f-a", p4d_samples: window.__MC_LOAD_P4D__.samples });
    var full = D.exportJSON();
    try {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify({ mc_load_p4f: true, diag: full, p4d_samples: window.__MC_LOAD_P4D__.samples }, null, 2)], { type: "application/json" }));
      a.download = "MC_LOAD_P4F_" + loadId + ".json";
      a.click();
    } catch (e2) {}
    console.log("MC-LOAD-P4F DONE", { loadId: loadId, events: (full.event_ring || []).length });
    console.log("MC-LOAD-P4F payload (copy):", full);
  }, 60000);
  return { loadId: loadId, hint: "60s — switch 1m→5m now" };
})();
```

**Erfolgskriterien (Browser, vom Benutzer):** Phase B ~24192 Kerzen, EMA bis Live-Tip, kein Nachsprung wie P4D ~26 s (Caller dort weiterhin **unbestätigt** ohne neuen Export).

---

## 5. Candle Integrity (READ-ONLY ClickHouse)

**Tabelle:** `signal_generator.candles_1m` · `exchange=bybit` · `SOLUSDT` · `is_closed=1`

| `open_time` (unix) | CH-Zeile(n) | OHLC (CH) |
|--------------------|-------------|-----------|
| **1795685460** | **1** | O=70.91 H=70.93 L=70.91 C=70.93 Vol=624.6 |
| **1795685640** | **1** | O=71.00 H=71.03 L=70.98 C=70.98 Vol=2584.4 |

**Vergleich P4D-Browser-Debug** (122.28 / 122.28 / 70.89 / 70.93): **stimmt nicht mit CH überein** → Auffälligkeit sehr wahrscheinlich **Frontend/Forming/5m-Debug-Payload**, nicht kanonische 1m-CH-Zeile.

**Doppel-Timestamps:** In CH **keine** Duplikate für 1795685640 (`count=1`).

---

## 6. Offen / Grenzen

| Thema | Status |
|-------|--------|
| Browser-PASS P4F | **ausstehend** (Benutzer-Export) |
| t≈26 s Viewport-Caller (P4D) | **weiterhin unbestätigt** |
| Öffentlicher HTTPS-Curl von diesem Agenten | Timeout; gleicher Code über :3000 verifiziert |

**Kein Commit · Kein Push · Keine DB-Mutation.**