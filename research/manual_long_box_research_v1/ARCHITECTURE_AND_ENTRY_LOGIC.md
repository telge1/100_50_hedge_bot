# DOGEUSDT — Manuelle Long-Box-Forschung: Architektur & Entry-Logik

**Datum:** 2026-11-10  
**Modus:** Isoliertes Forschungsmodul (`research/manual_long_box_research_v1/`)  
**Keine Änderungen:** Coin-Import, Pool-SSOT, L1/P17, SHORT MAIN, Produktionsbots, Git-Push, Deployment.

---

## 1. Bestehende Zeichnungs-Persistenz (READ-ONLY)

| Aspekt | Befund |
|--------|--------|
| **Speicherort (kanonisch)** | `backtester/short/dashboard/research_charts/user_data/drawings.json` (auch spread_recovery `dashboard/research_charts/user_data/drawings.json` bei gleichem Stack) |
| **Format** | `{ "version": 1, "drawings": [ … ] }` |
| **RAM → Disk** | `ResearchWorkspace.persist_drawings()` → TRP `save_drawings()` |
| **API** | `POST /api/research/drawings/event`, `GET /api/research/overlays`, `POST /api/research/position` |
| **Typ Long** | `drawing_type: "long_position"` |

**Entry / SL / TP — Feldnamen (kein `SL`/`TP` als Keys):**

| Konzept | JSON |
|---------|------|
| Entry | `entry_price` |
| Stop (SL) | `stop_price` |
| Ziel (TP) | `target_price` |
| Zeitspanne | `start_timestamp`, `end_timestamp` (Unix-Sekunden UTC) |
| TF-Metadaten | `created_on_timeframe` (Anlage); `timeframe_scope` meist `"all"` |

Zeitachse ist **TF-unabhängig** (Unix UTC). Wechsel des Chart-TF ändert gespeicherte Werte nicht — nur Darstellung.

**Eindeutige ID:** `drawing_id` (manuell: `d-` + 12 Hex; Strategie-Imports: `ema59p16a-…`, `p17ab-pos-…`).

**Geometrie-Validierung (TRP `Drawing._validate_position`):** Alle drei Preisfelder erforderlich.  
Hinweis: `normalize_position_prices()` im UI kann ungültige SL/TP **still korrigieren** — das Forschungsmodul nutzt **strikte** Ablehnung (`reject_invalid`), keine UI-Normalisierung.

**Fazit §2-Voraussetzung:** Entry/SL/TP sind in der Zeichnungsstruktur **zuverlässig** vorhanden → Long-Box-Erkennung **ohne** Erweiterungsplan/STOP.

---

## 2. Abgrenzung: Trendlinie vs. Long-Box

| Spur | Trigger | Bestätigung | Trendlinie? |
|------|---------|-------------|-------------|
| **L1 V3AN** (`manual_channel_pool_long_v1/touch_watcher_research_v3an/`) | Support-`LineDef` Touch | A1/A2/A3/B_RECLAIM (Default **A2**) | **Ja** (manuelle Trendlinie) |
| **BT-P17** (`phase_ema59_bt_p17_landscape_long_research_v1/`) | EMA59−6σ **Down-Cross** + Lower-Pool-Near | Bullish + ≥0,10 % vs. Vorkerze | **Nein** (Pools + EMA-Schwelle) |
| **Neue Long-Box** (dieses Modul) | Preis berührt **`entry_price`** der Box | **Vorgegeben:** L1-**A2** (importierbar, nicht neu erfunden) | **Nein** — horizontales Entry-Level |

Trendlinienabhängige Regeln (Line-Geometrie, `approach_from_above` auf Linie) werden **nicht** still durch Box-Logik ersetzt. Entry-Touch ist **Level-basiert** (`low <= entry <= high`, optional Toleranz).

---

## 3. Wiederverwendbare Bestätigung (implementiert & getestet)

Aus `watcher_fsm.py` / `TouchWatcherConfig` (L1):

| Modus | Regel |
|-------|--------|
| **A1** | `close > entry_level` |
| **A2** (Default L1) | `close > entry_level` **und** `close > prev_close` |
| **A3** | wie A1 + `min_gain_pct` vs. `prev_close` |
| **B_RECLAIM** | Break unten, dann N Closes oben Linie |

**Tests (Referenz):** `touch_watcher_research_v3an/test_watcher_config.py`, V3AN-Matrix-Regression (45 Configs), V3AO Equality.

**Long-Box-Forschung:** Default **`confirmation_mode = "A2"`** — explizit konfigurierbar, **keine neue Regel**.

**P17-Bestätigung** wird für diese Strategie **nicht** verwendet (E1C-Cross-Voraussetzung fehlt).

---

## 4. Watcher-Konzept (Vorbereitung, nicht Produktion)

```text
Box IDLE
  → Entry-Level berührt (5m OHLC, kausal, kein Lookahead)
  → WATCHING (Watch-Fenster: `watch_window_bars`, Default 12)
  → Bar-Close: A2-Bestätigung?
       nein → weiter WATCHING / EXPIRED
       ja   → ENTRY_SCHEDULED (Fill am **nächsten** Bar-Open)
  → maximal **ein** offener Long global
```

- **Kein** Trade beim bloßen Intra-Bar-Touch.
- **Keine** Exchange-Orders.
- SL/TP aus Box (`stop_price`, `target_price`), **nicht** aus Pool-Landschaft überschrieben.
- Nach Fill: SL/TP-Simulation barweise (Long: SL wenn `low <= stop`, TP wenn `high >= target`).

### ⚠️ Offene Produktentscheidung (nicht eigenständig festgelegt)

| Thema | Status |
|-------|--------|
| Priorität bei **zwei** gleichzeitig WATCHING-Boxen (überlappend) | **Dokumentiert:** First-touch-order mit gesperrtem zweitem Entry solange Position offen; keine Doppel-Position |
| Welcher **Confirm-Modus** für manuelle Boxen produktiv (nur A2 vs. wählbar A3) | **Markiert** — Forschung nutzt A2 |
| Anbindung Research-Chart-Overlay (API-Route) | **Nicht implementiert** (nur Python-API + Events-JSON) |
| WATCH aktivieren wie P17-FSM vs. rein Box-FSM | **Box-FSM**; P12-`on_ema_cross` **nicht** gemappt |

---

## 5. Modul-Grenzen

| Komponente | Pfad | Aufgabe |
|-----------|------|---------|
| Erkennung | `recognition.py` | `long_position` → `LongBoxSetup` |
| Manuelle Filter | `recognition.is_manual_long_box` | `d-*` IDs (ohne Strategie-Prefix) |
| Validierung | `validation.py` | Long: `stop < entry < target`, finite, sichtbar |
| Entry-Touch | `entry_touch.py` | Kausaler Level-Touch |
| Bestätigung | `l1_confirm.py` | A1/A2/A3 (kopiert aus L1-Semantik) |
| Watcher-Pool | `watcher_pool.py` | Pro-Box-Zustand, 1× globale Position |
| Simulation | `simulator.py` | 5m-Replay, Events für Chart-Nachverfolgung |
| Tests | `tests/test_manual_long_box_research_v1.py` | §7-Szenarien |

Lesen von `drawings.json` ist **read-only** (`drawing_source.load_drawings_payload`).

---

## 6. Risiko / SL-TP zum Fill

- Geplantes Entry: `entry_price` (Box).  
- Ausgeführtes Entry: **Open** der Kerze nach Bestätigung.  
- Vor Fill: `validate_long_geometry(stop, actual_entry, target)` — bei Fail **kein** Trade.  
- Prozent-Abstände: `risk_pct`, `reward_pct` im Setup-Record.

---

## 7. STOP (Scope dieser Lieferung)

Erledigt lokal:

- Architekturbericht (dieses Dokument)
- Long-Box-Erkennung + Validierung
- Watcher-Zustände + historische Simulation (Forschung)
- Tests

Nicht erledigt (bewusst):

- Dashboard-Integration / Overlay-API
- Watcher-Anbindung an Live-Chart
- Git-Push, Neustart, Bot-Änderungen
