# DOGE-LIVE-P17 — Veraltete Pool-Bounds (07:25Z im Browser)

**Messzeitpunkt (UTC):** 2026-11-13 **08:46–08:49**  
**Nutzer-Trace (Referenz):** ~**08:40 UTC** — `scope_bounds.hi=2026-11-13T07:25:00Z`, `refreshPane_render` OK, `canvas_drawn=117`  
**Verdikt:** **`P17_PARTIAL`** — Ursache **bestätigt** + Fix implementiert; **Dashboard-Neustart** + **Browser-PASS** ausstehend  
**Rohdaten:** [`doge_live_p17_probe_v1.json`](doge_live_p17_probe_v1.json)

---

## Kurzfassung

| Ebene | Befund @ 08:48 UTC |
|--------|---------------------|
| **Publisher / SSOT** | `max_decision_close` **2026-11-13 08:40:00**, `pending_closes=0` |
| **Frischer Python-Prozess** (`doge_5m_pd1_viewer_bounds`) | `data_available_to` **2026-11-13T08:40:00Z** |
| **Laufender Dashboard `app.py` (PID 2288268)** | Gestartet **08:26** lokal (~05:26 UTC), **ohne** Code-Neustart seit Commit `e14ce2eb` |
| **Nutzer `scope_bounds.hi`** | **07:25:00Z** → **exakt 15 × 5m** hinter SSOT (**75 min**) |
| **Ursache** | **Fall A:** `_PUBLISHED_CLOSES_CACHE` im **Dashboard-Prozess** friert die Publish-Liste beim **ersten** `get_published_decision_closes` ein; Bounds-API liefert danach einen **eingefrorenen Tail** |
| **Verstärker** | **Fall D:** Frontend `loadCaps` **HIT** hält `data_available_to` in **`caps`-Singleton**, bis `invalidateCapsCache` |

**Kein** Publisher-Rückstand (Fall E widerlegt). **Kein** Proxy-4291-Nachweis (extern/internal beide 401 ohne Auth).

---

## Phase A — Datenstand (UTC, gleicher Messblock)

| # | Größe | Wert | Semantik |
|---|--------|------|----------|
| 1 | Wall | **2026-11-13T08:48:30Z** | — |
| 2 | CH letzte **geschlossene** 5m-Open | **2026-11-13T08:45:00Z** | `complete` candle open |
| 3 | Forming 5m-Open | **~2026-11-13T08:45:00Z** (Bucket offen bis +5m) | Kerze frischer als Publish |
| 4 | Publisher `max_decision_close` | **2026-11-13 08:40:00** | `decision_close` = 5m-Bucket-**Open** |
| 5 | Publisher `last_complete_5m_close` | **2026-11-13T08:40:00+00:00** | Publisher-UTC |
| 6 | SSOT `data_available_to` (frisch) | **2026-11-13T08:40:00Z** | validierter Publish-Tail |
| 7 | Viewer-Capabilities Root (ungepatcht) | **2026-09-30T23:45:00Z** (15m frozen) | irrelevant für Live-5m nach JS-Patch |
| 8 | Capabilities 5m/PD1 (direkt) | **2026-11-13T08:40:00Z** | `landscape_capabilities` mit publish-Liste |
| 9 | **Nutzer-Frontend** `scope_bounds.hi` | **2026-11-13T07:25:00Z** | aus gecachten/patched Caps |

**Line:** `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`

`data_available_to` ist **kein** Bar-Close und **kein** Forming-Ende — es ist der letzte **vollständig validierte `decision_close`** (5m-Open des abgeschlossenen Buckets).

---

## Phase B — Prozess-Diagnose

| Prüfpunkt | Ergebnis |
|-----------|----------|
| Port **3000** | **PID 2288268** — `…/spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| CWD | `…/spread_recovery_hedge_short_dev/dashboard` |
| Git | Branch **`feature/ema-touch-structure-v2`**, **`e14ce2eb`** |
| `dashboard.service` | **inactive** (manueller `app.py`) |
| Collector / Publisher | Services **laufen**; Publisher journal synchron |
| In-Memory-Cache | **`_PUBLISHED_CLOSES_CACHE`** in `historical_pool_state_v1.py` — **kein TTL**, kein Live-Invalidate |
| Bounds-Berechnung | dynamisch aus Publish-Liste — **aber Liste im Prozess eingefroren** |

**Mechanismus (belegt):** Erster Bounds-Request nach Prozessstart füllt `_PUBLISHED_CLOSES_CACHE[(line_id, hash)]`. Spätere Publishes landen in CH/Inventar, **nicht** im Dashboard-Speicher → `doge_5m_pd1_viewer_bounds()` liefert weiter den alten Tail (z. B. **07:25Z**).

Simulation P17: Cache künstlich **15 Closes** verkürzt → Tail **07:25-äquivalent**; mit Fix **`_PUBLISHED_CLOSES_CACHE.pop(...)`** vor Reload → wieder **08:40Z** (`fix_recovered_live_tail=true` in Probe).

---

## Phase C — HTTP-Vergleich

| Kanal | Ergebnis |
|-------|----------|
| `127.0.0.1:3000/.../doge-5m-pd1-viewer-bounds` | **401** (Auth) — Inhalt im **alten Prozess** nicht ohne Cookie gemessen; Codepfad = eingefrorener Cache **plausibel** |
| `dash.immotel.de:8080` | **401** — kein Proxy-Stale-Nachweis |
| Frischer Import (neuer Interpreter) | **08:40Z** |
| Frontend | Patch via `doge-5m-pd1-viewer-bounds` in `loadCaps`; bei **HIT** ohne Re-Fetch → **07:25** möglich |

---

## Phase D — Implementierter Fix

### Fall A — Backend (`historical_pool_state_v1.py`)

Vor Reload der Publish-Liste in `doge_5m_pd1_viewer_bounds()`:

```python
_PUBLISHED_CLOSES_CACHE.pop((pd_cfg.pipeline_line_id, pd_cfg.config_hash), None)
```

**Dateien:** Spread + Pools-Mirror `research_charts/historical_pool_state_v1.py`

### Fall D — Frontend (`pd1_exact_origin_density_research_v1.js`)

- `refreshDoge5mViewerBoundsIfStale()` — bei `loadCaps` **HIT** alle **30 s** Bounds nachladen und Caps patchen
- Cache-Bust: **`?v=pd1-p17-live-bounds-refresh-v1`**

**P11C / P12 / P16 unverändert** in der Refresh-Gate-Logik.

### Deployment

Der **laufende** PID **2288268** hat den Python-Fix **noch nicht** geladen → **kontrollierter Neustart** nur dieses `app.py` nötig (kein Collector/Publisher).

---

## Phase F — Verifikation

| Check | Status |
|-------|--------|
| Bounds = SSOT nach Fix (Unit-Simulation) | **PASS** |
| HTTP :3000 nach Neustart | **UNVERIFIED** (Neustart in P17 nicht ausgeführt — Trading-Session) |
| Browser `scope_bounds.hi` ≈ Publish-Tail | **UNVERIFIED** |
| `refreshPane_render` / `canvas_drawn` | **UNVERIFIED** |

**Erwartung nach Neustart + Hard-Refresh:** `scope_bounds.hi` ≥ **08:40Z** (bis +5m Publish-Zyklus); `buildWindows api_to` clipped an aktuelle `data_available_to`.

---

## Geänderte Dateien (uncommitted)

| Datei | Änderung |
|-------|----------|
| `spread…/historical_pool_state_v1.py` | Cache-Pop vor Bounds |
| `pools…/historical_pool_state_v1.py` | Mirror |
| `spread…/pd1_exact_origin_density_research_v1.js` | 30s Bounds-Refresh bei Caps-HIT |
| `pools…/pd1_exact_origin_density_research_v1.js` | Mirror |
| `spread…/templates/research_charts.html` | JS Cache-Bust P17 |

---

## Verbleibende Risiken

- `list_published_closes` bei **jedem** Bounds-GET etwas teurer (akzeptabel für Live; ggf. später TTL statt vollem List).
- Historische Imports profitieren weiter von Cache — nur Bounds-Endpoint invalidiert **eine** Line.
- **`invalidateCapsCache`** bleibt für sofortigen Caps-Flush bei neuem Pool-Bucket relevant.

---

## Verdict

**`P17_PARTIAL`** — Root Cause **PASS**, Fix **implementiert**, Live-Betrieb **PASS** nach Dashboard-Neustart + Browser-Hard-Refresh offen.

**Kein Commit. Kein Push. STOP.**
