# L1 V3S — Drawing-Reset / Browser↔Server-Sync (Diagnose, STOP)

**Stand:** 2026-11-08 · **Kein Reset durchgeführt** · **Kein Backtest** · **`drawings.json` unverändert**

Sicherungskopie (read-only): `l1_v3s_server_drawings_backup_20261108T124320Z.json`  
Server-JSON SHA256: `4a18f0e46d42e3b7a83c4b62d656aebd00dcbeaccc06bea13190050b4b6d9571` (14 Zeichnungen)  
**`d-ac60572bfc69`:** weiterhin in der Server-JSON (`visible: true`, Farbe `#fafafa`)

---

## 1. Woher kommen „sichtbare“ Browser-Linien?

| Schicht | Quelle | Persistenz |
|---|---|---|
| **Browser (Research-Chart)** | `GET /api/research/overlays?symbol=&timeframe=` → `ResearchWorkspace.composed_overlays()` | **Kein** eigenes Langzeit-Drawing-Store; `pane.pendingOverlays` nur Render-Cache pro Pane |
| **Server (laufende App)** | Singleton `get_workspace()` → `DrawingManager` im RAM, Start: `_load_persisted_drawings()` aus `user_data/drawings.json` | Mutationen über `POST /api/research/drawings/*` → `persist_drawings()` → `save_drawings(path)` |
| **Backtester (L1)** | **Direkt** `research_charts/user_data/drawings.json` bei jedem Lauf | Liest **nicht** den Browser und **nicht** zwingend denselben RAM-Stand wie ein alter Server-Prozess |

**localStorage** (`research_charts.js`): Layout, Scanner-Flags, Sync-Toggles — **keine** manuellen Trendlinien.

Damit sind „Browser-Zeichnungen“ faktisch **Server-RAM + JSON**, sofern jeder Delete/Drag erfolgreich `persist_drawings()` auslöst.

---

## 2. Löschen & Persistenz

- UI: `Delete` → `POST /api/research/drawings/delete` → `delete_selected()` → `remove_drawing` + **`persist_drawings()`** (`workspace_session.py`).
- **Kein** separater „Speichern“-Button für Trendlinien — Schreiben erfolgt **sofort** bei erfolgreichem API-Call.
- Wenn Delete fehlschlägt (Auth, Netzwerk, keine Auswahl, andere Session), bleibt die ID in JSON.

---

## 3. Warum JSON-Linie, aber „im Chart nicht auffindbar“?

Für `d-ac60572bfc69` (Server-JSON):

- `visible: true`, `timeframe_scope: "all"`, Symbol `DOGEUSDT`
- wird in `compose_drawing` / `renderSegment` **gerendert**, sofern Overlays geladen werden
- **Farbe `#fafafa`** auf hellem Chart → praktisch unsichtbar (V3Q-Audit)
- Anker können außerhalb des sichtbaren Viewports liegen — Segment existiert trotzdem zwischen min/max Ankerzeit
- Nutzer kann die Linie **im Gefühl gelöscht** haben, während sie **persistiert** ist (kein erfolgreicher Delete)

**Nicht** die Ursache: separates verstecktes Browser-Archiv parallel zur JSON.

---

## 4. Atomare Ersetzung der gesamten Liste?

**Intern vorhanden:**

- `DrawingManager.replace_drawings()` (`trading_research_platform/drawings/manager.py`)
- `save_drawings(path, …)` in `persist_drawings()`

**Nicht öffentlich per Research-API:**

- Kein `POST /api/research/drawings/replace` / `import` / `sync-from-client`
- Kein Endpoint, der eine vom Browser exportierte Liste entgegennimmt

`clear_drawings(symbol)` löscht symbolweit im RAM + persistiert — **kein** Ersatz durch Client-Bestand.

---

## 5. Kann der aktuelle Browser-Zustand unabhängig vom Server-JSON erfasst werden?

**Mit den vorliegenden Mitteln in dieser Umgebung: Nein.**

- Kein headless Zugriff auf den angemeldeten Browser-Tab.
- Server-API liefert Overlays nur aus **Server-RAM** (gleiche Quelle wie JSON nach Persist), nicht „was der Nutzer meint“ ohne erfolgreiches Delete.
- Ein erneutes Lesen von `drawings.json` ist **keine** unabhängige Browser-Erfassung.

Gewünschter Ablauf (1–7) setzt voraus:

1. Client exportiert autoritative Liste **oder**
2. Neuer API-Endpoint + UI „Sync drawings now“ **oder**
3. Nutzer liefert explizit exportierte JSON / bestätigtes Server-Snapshot nach Delete

**Ohne das: STOP** (laut Auftrag).

---

## 6. Drei-Wege-Drift (häufiges Missverständnis)

| Situation | Chart | `drawings.json` | Backtest |
|---|---|---|---|
| Delete im UI OK | ID weg | ID weg | ID weg |
| Delete nur lokal gedacht / Tab alt | evtl. noch da (alter Overlay-Fetch) | ID da | ID da |
| JSON extern editiert, Server nicht neu gestartet | RAM alt | Datei neu | Backtest liest **Datei** |
| Server neu, Datei alt | = Datei | = Datei | = Datei |

Backtester kann eine **neuere Datei** nutzen, während ein langer Server-Prozess noch **alten RAM** anzeigt — oder umgekehrt nach fehlendem Persist.

---

## 7. Minimaler künftiger Workflow (Vorschlag, **nicht implementiert**)

`Chart (authoritative export) → POST drawings/replace (atomic write) → verify GET overlays IDs → drawing_refresh → Backtest`

Bausteine:

1. `POST /api/research/drawings/export` — serialisierte `drawings[]` für Symbol
2. `POST /api/research/drawings/replace` — Body = vollständige Liste; `replace_drawings` + atomares `save_drawings` (temp + rename)
3. Optional: Preflight `GET /api/research/drawings/health` — SHA256 Datei vs. RAM
4. Backtest nur über bestehendes `drawing_refresh.py` (bereits kanonische Datei, keine Snapshot-Merge)

**Keine** Strategieänderung; nur Research-Chart-Persistenz.

---

## 8. Antworten §7 (Ergebnis)

| # | Frage | Antwort |
|---|---|---|
| 1 | Woher sichtbare Browser-Linien? | Server: `composed_overlays` aus RAM-`DrawingManager`, gefüllt aus `drawings.json` + API-Mutationen; Browser cached nur Overlays pro Pane |
| 2 | Unabhängig vom alten JSON erfassbar? | **Nein** (ohne neuen Export/API oder live Browser) |
| 3 | Server-Liste durch Browser-Bestand ersetzt? | **Nein** — nicht durchgeführt |
| 4 | `d-ac60572bfc69` verschwunden? | **Nein** — noch in Server-JSON |
| 5 | Chart und Server vollständig gleich? | **Nicht verifizierbar** ohne laufende authentifizierte Session; Datei enthält die Linie; „nicht auffindbar“ passt zu **Persistenz-Lücke** und/oder **#fafafa** / Viewport |

**Backtest:** nicht gestartet (Sync-Voraussetzung nicht erfüllt).

---

## 9. Empfohlene Nutzer-Schritte (manuell, ohne Agent-Eingriff in JSON)

1. Linie `d-ac60572bfc69` im Chart **auswählen** (Hit-Test / Layer-Audit) → Delete → prüfen, dass API 200 liefert.
2. Seite neu laden; SHA256 von `drawings.json` muss sich ändern; ID darf fehlen.
3. Danach `python3 run_l1_v3s_full_backtest.py` (Refresh + Backtest).
4. Optional Server-Prozess neu starten, damit RAM = Datei.

**Kein** blindes Leeren von `drawings.json`.
