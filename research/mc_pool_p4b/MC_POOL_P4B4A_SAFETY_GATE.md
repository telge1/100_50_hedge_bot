# MC-POOL-P4B4A — Targeted Safety Gate

**Status:** `MC_POOL_P4B4A_SAFETY_PASS_BROWSER_READY`  
**Datum:** 2026-11-26  
**Kein Restart · kein Commit · kein Push**

---

## 0. HTTP-JS-Verifikation

| Quelle | SHA-256 `pd1_exact_origin_density_research_v1.js` |
|--------|---------------------------------------------------|
| Disk | `d2892b74cb070202c67e86dc64386decd7e5267f4f6c16f5711cb9d81cfbe975` |
| `http://127.0.0.1:3000/static/.../pd1_exact_origin_density_research_v1.js` | **gleich** |

Enthält P4B4 Bounds-Single-Flight **und** P4B4A Fail-Closed-Gate (s. §3).

---

## 1. Neue Tests (P4B4-Implementierung)

**Suite:** `static/js/research/test_mc_pool_p4b4_bounds_safety_v1.js`  
**Ergebnis:** **24 PASS**

| ID | Prüfpunkt | Ergebnis |
|----|-----------|----------|
| T1 | Warm Caps: Re-Enable → **1** Bounds-GET, `skipStaleBoundsRefresh` | PASS |
| T1b | Ohne Skip + TTL abgelaufen: Stale-Refresh fetcht; Re-Enable mit Skip: weiter 1 GET | PASS |
| T2 | HTTP 5xx, Netzwerk, ungültiger Body, JSON-Fehler: fail-closed, kein Poison-Caps | PASS |
| T2 | Fehlgeschlagener Bounds: **kein** Cache-Repaint (`boundsSyncOk`) | PASS |
| T3 | Parallele gleiche URL → 1 Fetch; SOL+BNB → 2 Fetches; Inflight-Cleanup | PASS |
| T4 | Publication: `hiAdvanced` → Repaint blockiert | PASS |
| T5 | Cold-Caps: `loadCapsOnce`-Pfad → **1** Bounds-GET, Sync ok | PASS |
| T6 | Invalidierungs-Gate `!boundsSync.ok` (P4B4A) | PASS |

---

## 2. Regression (bestehend)

| Suite | Ergebnis |
|-------|----------|
| `test_mc_pool_p4b_cache_lifecycle_v1.js` | 13 PASS |
| `test_p24e_coalesce_logic_v1.js` | 5 PASS |
| `pytest test_mc_load_p4i_v1.py` | PASS |
| `pytest test_mc_load_p4g_v1.py` | PASS |
| `pytest test_mc_load_p4e_v1.py` | **1 FAIL** (erwartet `research-mc-load-p4i-v1` in Template — seit **P4B2** `research-mc-pool-p4b-v1`) |
| `pytest test_mc_load_p3_fast_first_v1.py` | **1 FAIL** (gleicher Template-String-Drift) |

**Einordnung:** Keine P4B4-/P4B4A-Logik-Regression; bekannte Asset-`?v=`-Drift aus P4B2-Aktivierung. Kein Blocker für Bounds-Safety; Browser nutzt `research-mc-pool-p4b-v1`.

---

## 3. Minimaler Sicherheitsfix (P4B4A)

**Problem (Test T2):** Nach fehlgeschlagenem Bounds-GET konnten `fullyCached` + `segMatch` + `freshnessKnown` trotzdem **Cache-Repaint** auslösen (`skipStaleBoundsRefresh` allein verhindert nur den **zweiten** GET, nicht falsche Freshness).

**Fix:**

1. `ensurePaneViewerBounds` liefert `{ ok: boolean }` (warm + cold).
2. `refreshPaneAfterReEnable`: Segment/Sweep-Cache-Invalidierung wenn  
   `!boundsSync.ok || hiAdvanced || !freshnessKnown`.

**Unverändert:** Window-Chunk-Cache, Pool-Mathematik, 30s-TTL global, `refreshPaneImpl`-Pfad.

---

## 4. Freshness-Vertrag (post P4B4A)

| Situation | Verhalten |
|-----------|-----------|
| Warm Re-Enable, Bounds OK | 1 GET; Caps patch; Hi-Check für Publication |
| Bounds fehlgeschlagen | **Kein** Cache-Repaint; Caches invalidiert → `refreshPane` oder Fetch |
| `skipStaleBoundsRefresh` | Kein redundanter 2. GET in derselben Re-Enable-Operation |
| Publication während OFF | Bei erfolgreichem Bounds + `hiAdvanced` → Invalidate + Nachladen |

---

## 5. Browser (ausstehend)

- **1×** Bounds-GET in P4A-HTTP-Log bei Re-ON (warm).
- 0 Window-POSTs bei vollem Cache.
- Fehlgeschlagener Bounds (DevTools Offline o.ä.): kein sichtbarer Cache-only-Repaint ohne Reload.

**Token:** `MC_POOL_P4B4A_SAFETY_PASS_BROWSER_READY`