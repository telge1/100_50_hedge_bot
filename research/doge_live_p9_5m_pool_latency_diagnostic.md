# DOGE-LIVE-P9 — 5m-Pool-Latenz-Diagnose (READ-ONLY)

**Messfenster:** 2026-11-12, Schwerpunkt **16:00–16:45 UTC**  
**Nutzerbeobachtung (nicht repliziert im Browser):** Kerzen ~**16:35 UTC**, blaue Exact-Origin-Bänder ~**5×5m (~25 min)** früher.  
**Verdict-Klassifikation:** **Primär A** (Publisher-Rückstand zur Wanduhr), mit **B** möglich für exakte UI-Darstellung; **C** für kanonische D2-Segmentenden **nicht** bestätigt.

---

## 1. Kanonische Kette (Ist)

```
CH candles_1m → strict 5m close → LIVE Publisher (user systemd)
  → publish SSOT (research_lmv2_active_snapshot_publish_v1 FINAL)
  → PublishedActiveReader / validated high water
  → doge_5m_pd1_viewer_bounds
  → build_landscape_window (5m PD1) → D2 pool segments
  → POST /live-charts/viewer/api/window → Canvas (Exact Origin B, min origins UI)
```

**Geschützte Line:** `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`  
**Publisher:** `doge-pd1-5m-live-publisher.service` **active** (PID **2121713**)

---

## 2. Snapshot-Tabelle (Server, UTC)

| # | Stufe | Zeitstempel (UTC) | Δ vs. Wanduhr¹ | Δ in 5m-Kerzen |
|---|--------|-------------------|----------------|----------------|
| — | **Wanduhr (Messung)** | **2026-11-12 16:42:51** | 0 | 0 |
| 1 | CH `candles_1m` MAX(open_time) | **16:41:00** | **~2 min** | **~0,4** |
| 2 | Letzter **vollständiger** 5m-Close (Publisher-Preflight) | **16:35:00** | **~8 min** | **~1,6** |
| 3 | Letzter **publish_complete** `decision_close` (CH) | **16:35:00** | **~8 min** | **~1,6** |
| 4 | Publisher `pending_closes` | **0** | — | — |
| 5 | Publisher `publish_lag_sec` (Tool) | **0.0** | — | — |
| 6 | `doge_5m_pd1_viewer_bounds` `data_available_to` | **16:35:00** | **~8 min** | **~1,6** |
| 7 | Window-API `to_utc` (Testfenster 16:00–16:35) | **16:35:00** | **~8 min** | **~1,6** |
| 8 | **D2-Segment-Ende** (max `segments[*][1]` in Window) | **16:35:00** | **~8 min** | **~1,6** |
| 9 | Canvas / Browser-Render | **nicht gemessen** (keine Session) | — | — |

¹ Wanduhr ≈ Messzeitpunkt; während langer `build_landscape_window`-Läufe (~4 min) driftet die Uhr leicht.

**Nach Catch-up (Publisher-Log, später):** `live publish 2026-11-12T16:45:00+00:00` um **~17:50 UTC** (Log-Zeile); Status danach bis **16:35+** SSOT konsistent.

---

## 3. Entscheidende Unterscheidung

### A — Publisher / API liegt zurück (**BESTÄTIGT im Journal 15:40–16:40 UTC**)

`journalctl` zeigt: **Wanduhr ~16:35**, letzter Publish in dieser Minute war **`decision_close=15:30:00`** — **~65 min (~13 Kerzen)** hinter der Markt-5m-Kante.

| Wanduhr (Log) | `live publish` decision_close | Rückstand |
|---------------|-------------------------------|-----------|
| 16:00:09 | 14:55:00 | ~65 min |
| 16:25:23 | 15:20:00 | ~65 min |
| 16:35:16 | **15:30:00** | **~65 min** |
| 16:40:23 | 15:35:00 | ~65 min |

**Interpretation:** Der Publisher verarbeitete im beobachteten Fenster ca. **einen** 5m-Close alle **~5 min Wandzeit** → die **Backlog-Lücke zur Live-Kante blieb groß** (nicht nur 25 min), bis ein **Catch-up-Block** im späteren Log (16:10…16:45 publishes zwischen 17:15–17:50 Log-Zeit) die SSOT-Kante nachzog.

**Zum Nutzerbild „~25 min“:**  
Wenn die **Chart-Kerzen** schon die **Forming-/Collector-Kante (~16:35)** zeigen, die **Bounds/Window** aber noch **`data_available_to ≈ 16:10`** (oder Client-Cache dieser Grenze), entstehen **~25 min (~5 Kerzen)** sichtbarer Abstand — **konsistent mit teilweise nachgezogener SSOT + Live-Kerzen voraus**.

### B — Daten aktuell, Frontend/Cache veraltet (**PLAUSIBEL**, nicht authentifiziert)

Bekannt aus P5C/P5N/P8: Exact-Origin nutzt **Tile-Cache**, **`loadCaps`**, **Bounds-Merge**. Wenn Kerzen via **250 ms Forming** laufen, Pools aber erst nach **Publish + Tile-Invalidation** (P5N) neu laden, kann die **Darstellung** hinter der Kette hängen, **ohne** dass D2 falsch berechnet ist.

**Nach Catch-up (16:42 UTC):** Backend-Window endet auf **16:35** — dann wäre reines **B** nur noch **≤1–2 Kerzen** (Forming vs. letzter Publish), **nicht** 25 min, sofern Hard-Refresh / Caps warm.

### C — Kanonisch beendete Bänder (**NICHT** als 25-min-Hauptursache)

`build_landscape_window('2026-11-12T16:00:00Z'…'16:35:00Z')`:

- **280** Pools; **268** mit Segment-Ende **≥ 16:35:00**
- **Globales** `segments[][1]`-Maximum: **16:35:00Z**
- Beispiel Unterband nahe Tail: `known_at 16:35:00Z`, Segment `['16:35:00Z','16:35:00Z']`

Die **SSOT-D2-Schicht** endet am Publish-Tail **nicht** künstlich 25 min früher.

**Mindest-Origins 2 (UI):** Kann einzelne **Density-Segmente** ausblenden, ändert aber nicht das globale D2-Segment-Ende der Pools in der API-Antwort (Canvas-Filter im Browser nicht nachgemessen).

---

## 4. Schritt-für-Schritt-Lags (zusammengefasst)

| Phase | Typische Latenz (catch-up) | Typische Latenz (Backlog 16:35) |
|-------|---------------------------|--------------------------------|
| Bybit → Collector forming | &lt; 1 s | &lt; 1 s |
| 1m → abgeschlossener 5m | 0–5 min (letzter Bucket) | 0–5 min |
| 5m → Publisher publish | **0** (pending 0) nach Catch-up | **~60–65 min** (Journal) |
| Publish → Bounds API | **0** (gleicher Tail) | gleich Publish-Tail |
| Bounds → Window D2 | Rechenzeit **~4 min** (Last) | gleiche `to` |
| Window → Canvas | unbekannt | **B** möglich |

---

## 5. Dienste-Health

| Dienst | Status |
|--------|--------|
| `doge-pd1-5m-live-publisher` | **active**, mode **live**, zuletzt **WAITING**, pending **0** |
| Bybit Collector | **LIVE**, WS **true** |
| Dashboard :3000 | läuft (nicht neu gestartet) |

Keine Publisher-Fehler im untersuchten Log-Ausschnitt; nur **Live publish** INFO-Zeilen.

---

## 6. 16:00–16:35 UTC — Publishes (CH)

Vollständige `publish_complete=1` für jede 5m-Marke **16:00 … 16:35** (Inventory beim Messzeitpunkt): **vorhanden** (Tail **16:35**).

---

## 7. API-/Canvas-Parität

| Prüfung | Ergebnis |
|---------|----------|
| `data_available_to` == `max(decision_close)` | **JA** (16:35:00) |
| Window-`to_utc` == Bounds | **JA** (Test 16:00–16:35) |
| Max D2-Segment-Ende == Window-`to` | **JA** (16:35:00) |
| Browser Canvas vs. API | **OFFEN** |

---

## 8. Belegte Ursache (kein Fix)

**Hauptursache für sichtbare Pool-Lücke zur Live-Kerze im Fenster ~16:00–16:35 UTC:**

1. **Publisher-Rückstand zur 5m-Marktkante (~13 Kerzen / ~65 min)** im Journal bis zum Catch-up — **Kategorie A**.
2. Die **~25 min** Nutzerangabe passt zu **Teil-SSOT / teil-gerenderter Bounds-Kante (~5 Kerzen)** während Kerzen schon weiter laufen — **Mischung A + B**, nicht zu einem separaten D2-Engine-Bug (**nicht C**).

**Kein Fix vorgenommen** (READ-ONLY).

---

## 9. Empfohlene Nutzer-Verifikation (manuell)

1. `python3 tools/doge_live_p4_live_publisher_v1/run_doge_p4_live.py --status` → `max_decision_close` vs. `last_complete_5m_close`.
2. Browser: Netzwerk **`doge-5m-pd1-viewer-bounds`** `data_available_to` vs. sichtbare Chart-Spitze.
3. Exact-Origin TRACE: `scope_bounds` / `window_http_response` nach Hard-Refresh.
4. Mindest-Origins **2** kurz auf **1** setzen (nur Diagnose): ändert nur Sichtbarkeit, nicht SSOT.

---

## 10. Artefakte

- Publisher: `results/doge_live_p4_live_publisher_v1/publisher_state.json`
- Log: `results/doge_live_p4_live_publisher_v1/publisher.log`
- Journal: `journalctl --user -u doge-pd1-5m-live-publisher.service`

**Git:** unverändert (READ-ONLY). **Kein Commit.**
