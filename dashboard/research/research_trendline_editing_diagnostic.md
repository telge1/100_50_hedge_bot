# Research Live-Charts: Trendlinien-Bearbeitung — Diagnose & Reparaturplan

## Verständnis der Anforderung

Nach dem erfolgreichen Fix der historischen Navigation soll die **manuelle Bearbeitung bereits gezeichneter Trendlinien** im Research-Dashboard dem TradingView-ähnlichen Verhalten nähern:

- Trendlinie **auswählen** (Markierung, zwei Anker-Handles),
- **Einzelanker** per Drag verschieben (UTC/Preis, Live-Vorschau, Persistenz beim Loslassen),
- **Ganze Linie** per Drag auf dem Segment verschieben (Steigung und Abstand erhalten),
- Auswahl per Klick ins Leere **aufheben**, später erneut auswählen — ohne Scroll/Zoomen zu blockieren,
- bestehende Zeichnungen **ohne ID-Wechsel**, inkl. optionaler **Rechtsverlängerung**, **TF-übergreifend** konsistent.

Diese Datei enthält **nur Analyse und Implementierungsplan**. Es wurden **keine** produktiven Code-, Daten- oder Deploy-Änderungen vorgenommen.

**Projektpfad (laufendes Dashboard):**  
`/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`

---

## 1. Architekturkurzüberblick

| Schicht | Rolle |
|--------|--------|
| `static/research_trp/chart.js` (TRP-Pane, iframe) | Kerzen (Lightweight Charts), Overlay-DOM, Hit-Test, Pointer-Drag, Bridge-Events |
| `static/js/research/research_charts.js` (Host) | Iframe-Bridge, `handleDrawingEvent` → REST, Workspace-Sync, Overlay-Refresh |
| `research_charts/workspace_session.py` | Zeichenwerkzeug-State, `on_point` / `on_hit` / `on_drag`, `persist_drawings()` |
| `trading_research_platform/drawings/*` (via `trp_import.py`) | `Drawing`-Model, `compose_drawings()` → Overlay-Primitive (`kind: segment` für `trend`) |

**Persistenz:** `templates/user_data/drawings.json` (und Spiegelpfade); Geometrie = `start_timestamp`, `start_price`, `end_timestamp`, `end_price` (+ `timeframe_scope: "all"` standard).

**Cross-TF:** Zeichnungen mit Scope `"all"` werden auf jedem Pane-TF aus absoluten UTC-/Preisankern neu projiziert — unabhängig vom Erstell-TF. Das ist **korrekt** und darf bei Edits unverändert bleiben.

---

## 2. Nachgewiesene Ursache (Warum Trendlinien nicht „wie TradingView“ editierbar sind)

Die Pipeline ist für **`trend` → Overlay `type: line`, `kind: segment`** **nur zur Erstellung** (zwei Klicks + `on_point`) und **minimal zur Auswahl** (`on_hit`) implementiert. **Verschieben, Anker-Handles und Server-Persistenz fehlen für Segmente/Trendlinien.**

### 2.1 Auswahl per Klick existiert — Bearbeitung nicht

Im **Select-Modus** löst ein Chart-Klick einen Hit-Test aus und meldet `hit` an den Server:

```746:816:static/research_trp/chart.js
    chart.subscribeClick(function (param) {
      ...
      if (interactionMode && interactionMode !== "select") {
        ...
        emitDrawing({ type: "point", time: pt.time, price: pt.price });
        ...
        return;
      }
      const hit = pt.x != null && pt.y != null ? hitTestXY(pt.x, pt.y) : null;
      if (hit) {
        emitDrawing({ type: "hit", overlay_id: hit });
        return;
      }
      ...
    });
```

`hitTestXY` enthält explizit `kind === "segment"` (Trendlinie) per Abstands-Test zum Segment:

```4818:4825:static/research_trp/chart.js
      } else if (p.type === "line" && (p.kind === "segment" || p.kind === "arrow")) {
        const x1 = xOf(p.start_timestamp);
        ...
          hit = distToSeg(x, y, x1, y1, x2, y2) <= Math.max(7, thresh);
```

Serverseitig setzt `on_hit` nur `selected_id` (korrekt für alle Typen):

```4159:4164:research_charts/workspace_session.py
    def on_hit(self, overlay_id: str) -> dict[str, Any]:
        did = overlay_id.rsplit(":", 1)[0] if overlay_id else ""
        ...
        self._selected_drawing_id = did
        return self.snapshot()
```

**Folge:** Auswahl kann funktionieren, aber es gibt **keine sichtbaren Anker-Handles** und **kein Drag** für Trendlinien (siehe unten). Subjektiv wirkt das wie „nicht auswählbar / nicht editierbar“.

### 2.2 Anker-Handles nur für Pfeil, Position, Ellipse — nicht für Trend (`segment`)

Nach `renderOneOverlay` werden Handles nur bei ausgewähltem **arrow**, **position** oder **ellipse** gesetzt; **`segment` fällt in `clearHandles`**:

```3214:3262:static/research_trp/chart.js
    if (selected && p.kind === "arrow") {
      syncHandles(rec, [ ... start/end ... ]);
    } else if (selected && p.type === "position") {
      ...
    } else if (selected && p.shape === "ellipse") {
      ...
    } else {
      clearHandles(rec);
    }
```

CSS-Handles (`.ov-handle`) sind zudem `pointer-events: none` — Interaktion läuft bewusst über Chart-Koordinaten + `onPointerDown`, nicht über DOM der Handles (wie bei Pfeil/Position).

### 2.3 Drag-Start schließt Trendlinien (`segment`) explizit aus

```5907:5911:static/research_trp/chart.js
    const movable =
      (p.type === "line" && (kind === "horizontal" || kind === "vertical" || kind === "arrow")) ||
      (p.type === "zone" && p.shape === "ellipse") ||
      p.type === "position";
    if (!movable) return;
```

Trendlinien haben `kind === "segment"` → **`onPointerDown` beendet ohne `dragState`**. Chart-Pan scrollt stattdessen.

`detectDragMode` und `onPointerMove` enthalten zwar generische `resize-start` / `resize-end`-Logik (für Pfeile), diese wird für Segmente **nie erreicht**.

### 2.4 Backend speichert Trend-Drags nicht

`on_drag` persistiert nur `hline`, `vline`, `position`, `circle`, `arrow` — **`trend`, `measure`, `rectangle` enden in `return self.snapshot()` ohne Update**:

```4218:4237:research_charts/workspace_session.py
        elif drawing.drawing_type in ("circle", "arrow"):
            ...
        else:
            return self.snapshot()
```

Selbst wenn Drag im Pane lokal liefen, ginge ein Trend-Update **nicht** nach `drawings.json`.

### 2.5 Drag-Event vom Pane enthält keine Segment-Geometrie

Bei `pointerup` sendet `chart.js` `start_timestamp` / `end_*` nur für **`arrow`**, Ellipse und Position — **nicht für `segment`**:

```6079:6098:static/research_trp/chart.js
      if (payload.kind === "arrow") {
        event.start_timestamp = ...
        event.end_timestamp = ...
      } else if (payload.shape === "ellipse") {
        ...
      } else if (payload.type === "position") {
        ...
      }
      emitDrawing(event);
```

### 2.6 Abhängigkeit vom aktiven Werkzeug

Solange `interactionMode !== "select"` (z. B. Werkzeug **Trend** noch aktiv, oder Pending-Zweipunkt-Zeichnung), fängt `subscribeClick` Klicks ab und sendet **`point`**, **kein `hit`** (siehe 2.1). Nach erfolgreichem Abschluss setzt `_commit` das Werkzeug auf `select` — wenn die UI/Bridge aber nicht synchron ist, bleibt Hit-Test blockiert.

**Host:** `toolMode()` mappt Workspace-Tool auf Pane-Modus; `deactivateToolsLocal()` setzt alle Panes auf `select`.

### 2.7 Deselektion / Klick ins Leere

Klick ohne Treffer ruft `on_chart_click` → Host `handleClick` (Kerzen-Marker / Scanner), **ohne** `selected_id` zu löschen. `cancel_drawing` / Escape setzt Tool zurück, **löscht Auswahl nicht**.

→ Anforderung **D** (Auswahl per Leerklick beenden) ist **noch nicht** umgesetzt.

### 2.8 Visuelle Auswahl schwer erkennbar

Trendlinien rendern als schmale rotierte `.ov-vline`-DIV (Linienlänge × Strichbreite). Klasse `ov-selected` setzt nur eine dünne `outline` — bei 1–2 px Höhe kaum sichtbar.

### 2.9 Parität mit TRP-Referenz

Die Dashboard-`chart.js` ist vom TRP-`charts/web/chart.js` abgeleitet; dort gilt **dieselbe Lücke** (`movable` ohne `segment`, keine Handles für `segment`). Edit für Trend war in der Web-Portierung **nie vollendet** — nicht durch die jüngste GO-TO-Integration regressiert.

### 2.10 Rechtsverlängerung („Ray“)

- TRP-`compose._segment()` setzt **kein** `extend_right` (nur Zonen in `chart.js` nutzen `extend_right` fürs Rendering).
- `hitTestXY` misst nur das **endliche Segment** zwischen den Ankern (`distToSeg` mit `t ∈ [0,1]`).
- Nutzer verlängern heute faktisch durch **weit rechts gesetzten zweiten Anker** oder erwarten TradingView-„Ray“ — dafür fehlt ein **`Drawing`-/Overlay-Feld** (z. B. `metadata.extend_right` oder `end_timestamp: null` + Ray-Logik).

**Fazit Ursache:** Es gibt **kein zweites, kaputtes Zeichnungssystem**, sondern eine **bewusst unvollständige Übernahme** der TRP-Bearbeitungslogik: **Pfeil/Position/Hline** editierbar; **Trend (`segment`)** nur erstellen + Server-Select-Flag, ohne Handles, Drag und `on_drag`-Persistenz.

---

## 3. Antworten auf die Analysefragen (Auftrag §3)

| Frage | Befund |
|-------|--------|
| Warum nicht mehr auswählbar? | Hit-Test nur in **Select**; oft fehlen sichtbare Handles/Feedback; ggf. aktives Zeichenwerkzeug oder dünne Trefferzone; kein echter Edit-Flow. |
| Auswahl existiert? | **Ja** — `hit` + `metadata.selected` + `ov-selected`. |
| Einzelanker verschieben? | **Nein** für `segment` (Logik nur für `arrow` beim Drag, Trend aus `movable` ausgeschlossen). |
| Ganze Linie verschieben? | **Nein** für `segment` (`move`-Branch nur `arrow` / ellipse / position). |
| Events von Overlays abgefangen? | `#overlay-layer { pointer-events: none }` — Hit-Test geht über Chart-Koordinaten; Forschungs-Marker/VP/PTB ebenfalls `pointer-events: none`. Konflikt vor allem **aktives Werkzeug** und **Pan vs. fehlendem Drag**. |
| Abhängig vom Werkzeug? | **Ja** — nur `select` erlaubt `hit`. |
| Wiederverwendbare Bausteine? | `hitTestXY`, `distToSeg`, `detectDragMode` (resize-start/end), `syncHandles`, `onPointerMove`-Geometrie für Pfeil, `on_drag` für Pfeil als Vorlage, `compose_drawings` + UTC-Anker. |

---

## 4. Timeframe-übergreifende Bearbeitung

- **Speicherung:** `Drawing.start_*` / `end_*` als UTC + `timeframe_scope: "all"` (Default in TRP-`make_trend`).
- **Anzeige:** `workspace_session.composed_overlays(..., timeframe= pane.tf)` filtert per `scope_matches`, projiziert Anker auf sichtbare Kerzen.
- **Edit-Plan:** Jede `on_drag`-Aktualisierung muss **nur** `dc_replace(drawing, start_timestamp=..., start_price=..., end_timestamp=..., end_price=...)` ausführen — **ohne** `created_on_timeframe` zu ändern. Refresh aller sichtbaren Panes via bestehendem `refreshOverlaysVisible()`.

**Regressionstest:** Trend auf 15m anlegen → 1h: gleiche Linie → Anker auf 1h schieben → zurück 15m: identische UTC/Preis aus `drawings.json`.

---

## 5. Verlängerte Trendlinien (Ray)

| Aspekt | Ist | Soll (Plan) |
|--------|-----|-------------|
| Datenmodell | Nur zwei endliche Anker | Optional `extend_right: true` oder Metadaten-Flag + Rendering |
| Rendering | `renderSegment`: nur zwischen Ankern | Erweiterung: Linie bis `plotRightX()` bei gleicher Steigung (analog `renderZone`) |
| Hit-Test | Nur Segment `t∈[0,1]` | Ray: Hit auf **unbegrenzte** Halbgerichtung rechts, aber **Resize-Handles nur an echten Ankern** (nicht am Plot-Rand) |
| Edit nach Verschieben | — | Nach Anker-Drag Ray aus **denselben** Ankern neu berechnen |

**Minimaler Schritt 1:** Endliche Trendlinien editierbar machen (90 % Nutzen). **Schritt 2:** Ray-Flag + Render/Hittest — nur wenn in bestehenden JSON-Zeichnungen oder UI schon genutzt (derzeit **kein** `extend_right` in Produktions-`drawings.json` gefunden).

---

## 6. Minimaler Implementierungsplan

### Phase A — Trendlinie = Segment (Pfeil-Parität)

**Dateien (primär):**

1. `static/research_trp/chart.js`
2. `research_charts/workspace_session.py` (`on_drag`, optional `on_hit` bei Leerklick-API)
3. Optional Spiegelaktualisierung `trading_research_platform/charts/web/chart.js` (DRY, nicht zwingend für Dashboard)

**A.1 Auswahl-UX**

- In `renderOneOverlay`: bei `selected && kind === "segment"` (und optional `metadata.drawing_type === "trend"`) **`syncHandles`** für Start/Ende wie bei `arrow`.
- `detectDragMode`: Zweig für `kind === "segment"` (HANDLE-Radius 14, analog Pfeil) — bereits für `resize-start`/`resize-end` in `onPointerMove` vorhanden, aber `movable` erweitern.

**A.2 Drag im Pane**

- `movable` um `(p.type === "line" && kind === "segment")` erweitern (nur wenn `metadata.drawing_id` gesetzt — keine Forschungs-Overlays).
- `onPointerMove` `mode === "move"`: `segment` wie `arrow` (Δt, Δp auf beide Anker).
- `onPointerUp` `emitDrawing`: `start_*` / `end_*` auch für `kind === "segment"` (oder generisch für alle two-point lines).

**A.3 Persistenz**

- `workspace_session.on_drag`: Zweig `drawing.drawing_type in ("trend", "measure")` — **`measure`** nur wenn gewünscht (gleiche Geometrie); mindestens **`trend`**.
- `dc_replace` mit validierten UTC-Zeiten und Preisen; `drawing_id` unverändert; `persist_drawings()`.

**A.4 Live-Vorschau**

- Bereits durch lokales Mutieren von `rec.payload` + `renderOneOverlay` in `onPointerMove` — kein zusätzlicher Roundtrip während Drag.

**A.5 Deselektion**

- Option 1 (minimal Host): Bei `hit` fehlend neuen REST-Event `deselect` oder Klick `on_chart_click` → API `drawings/deselect`.
- Option 2 (Pane): Leerklick sendet `{ type: "deselect" }` wenn select-Modus.
- Escape könnte Auswahl löschen (ohne Zeichnung zu löschen) — definiertes Verhalten abstimmen.

**A.6 Pan vs. Edit**

- Bestehendes Muster beibehalten: während `dragState` `setPanEnabled(false)`; sonst Pan aktiv — wie bei Positionen.

### Phase B — Rechteck / Kreis (optional gleicher Sprint)

Selbe Lücken (`movable` ohne `rectangle`-Zone, `on_drag` ohne `rectangle`). Für Kanäle oft zusammen mit Trend relevant — **kleiner Zusatz**, wenn `zone` + `rect`/`ellipse` in `movable` und `on_drag` ergänzt werden.

### Phase C — Ray / Verlängerung

- `Drawing.metadata["extend_right"]` oder TRP-Model-Erweiterung.
- `compose._segment`: Flag auf Overlay durchreichen.
- `renderSegment`: bei Flag Linie bis Plotkante zeichnen.
- `hitTestXY`: Ray-Variante mit Projektion entlang Steigung, Handles unverändert an Ankern.

**Bewusst nicht anfassen:** ClickHouse, Kerzenaggregation, GO-TO, EMA/P17/LONG/SHORT-Backtester, `drawings.json`-Inhalt während Implementierung (nur Test-Symbole).

---

## 7. Geplante Bearbeitungs-UX (Soll)

1. Werkzeug **Auswahl** (automatisch nach Zeichnen oder manuell).
2. Klick nahe Trendlinie → `selected_id`, Highlight + **zwei Handles**.
3. Drag Handle → nur dieser Anker; Live-Linie; Loslassen → `drag` → JSON-Update.
4. Drag auf Linie (nicht Handle) → **move** beider Anker.
5. Klick ins Leere → Deselect, Handles weg, Pan unverändert.
6. Doppelklick: derzeit nur Position-Editor (`on_edit`); für Trend **nicht** nötig für MVP.

---

## 8. Tests

### Automatisiert (Python, vorhandenes Muster)

Erweiterung von `test_research_charts_interaction.py`:

- `on_hit` auf Trend-Overlay-id → `selected_id` gesetzt.
- Simulierter `on_drag` mit `start_*`/`end_*` für `trend` → Koordinaten in `DrawingManager` geändert, `drawing_id` gleich.
- Negativ: `on_drag` ohne Felder → keine Änderung (Regression).

**Neu (optional):** kleines Node-Skript oder JSDOM-freier Test der **reinen** `distToSeg`/`detectDragMode`-Logik — nur wenn aus `chart.js` extrahiert oder dupliziert wie bei Epoch-Nav-Test.

### Manuell / Browser

1. Frischer Start, Trend 2-Punkt, auto-select, erneuter Klick → Handles sichtbar.
2. TF 15m → 1h → Anker verschieben → 15m gleiche Weltkoordinaten.
3. Pan/Zoom mit und ohne Auswahl.
4. Leerklick deselect.
5. Verlängerte Linie (Phase C): Hit auf Ray vs. Anker.
6. GO-TO / Replay: Zeichnen + Edit in historischer Ansicht.
7. Position/Pfeil/Hline unverändert editierbar.

---

## 9. Regression & Risiken

| Risiko | Mitigation |
|--------|------------|
| LLD/Research-Overlays versehentlich draggable | `movable` nur bei `metadata.drawing_id` + `source: drawing` |
| Measure-Tool mit Label/Zone | Trend zuerst; Measure separat testen (mehrere Overlay-IDs pro Drawing) |
| JSON-Migration | Keine ID-Änderung; nur Felder ersetzen |
| Performance | Ein `persist_drawings()` pro Drag-Ende, nicht pro Move |
| TRP-Drift | Optional TRP `chart.js` + Tests synchron halten |

**Rollback:** Git-Revert der gezielten Commits in `chart.js` + `workspace_session.py`; keine Datenmigration nötig.

---

## 10. Zusammenfassung

| Ebene | Status Trendlinie |
|-------|------------------|
| Erstellen (`on_point`) | ✅ |
| Auswahl (`on_hit`, `selected`) | ⚠️ teilweise (wenig Feedback) |
| Handles | ❌ |
| Drag Pane | ❌ (`segment` nicht `movable`) |
| Persistenz (`on_drag`) | ❌ |
| Deselect Leerklick | ❌ |
| Cross-TF | ✅ (Speicher modell), abhängig von Edit-Persistenz |
| Ray-Rechts | ❌ (Infrastruktur teilweise in `chart.js` für Zonen) |

**Kleinstmögliche Korrektur:** Trend `segment` auf **Pfeil-Niveau** bringen (Handles + `movable` + Move/Resize in `chart.js` + `on_drag` für `trend` + Drag-Payload + Deselect). Ray als **zweiter**, optionaler Schritt.

---

## 11. Freigabe-Status

- **Analyse:** abgeschlossen  
- **Implementierung:** **nicht** durchgeführt (Stop-Bedingung eingehalten)  
- **Nächster Schritt:** Freigabe für Phase A (ggf. B/C getrennt)

---

*Erstellt: 2026-11-07 · Nur Leseanalyse im Dashboard-Tree, `drawings.json` unverändert.*
