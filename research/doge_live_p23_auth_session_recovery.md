# DOGE-LIVE-P23 — HTTP-401 Session-Handling (Exact-Origin-Pools)

**Modus:** Phase A read-only; Phase B minimal Frontend-Fix; Phase C Regressionstests  
**Erstellt:** 2026-11-13  
**Ergebnis:** `P23_FRONTEND_AUTH_RECOVERY_WIRED` (Backend/Session unverändert; Browser-E2E **UNVERIFIED**)

**Kein Commit / kein Push** (wie beauftragt).

---

## Phase A — Ursachenbefund (mit Nachweis)

### 1. Warum HTTP 401 auf dem Window-Endpunkt?

| Beleg | Detail |
|--------|--------|
| Route | `POST /live-charts/viewer/api/window` in `spread_recovery_hedge_short_dev/dashboard/pd1_landscape_viewer_v1/api.py` mit `user: dict = Depends(require_auth)` |
| Auth-Dependency | `require_auth()` in `dashboard/app.py`: fehlender/ungültiger `session_id`-Cookie → `HTTPException(401, "Not authenticated")` |
| Live-Probe (ohne Cookie) | `curl -X POST …/api/window` → **401** (2026-11-13) |
| P22-Journal | **6× HTTP 401** auf Window-POST ~14:37 CET, Prozess **2379483** — siehe `research/doge_live_p22_recurring_pool_failure_report.md` |

**Beweisgrad:** **HOCH** — 401 kommt vom Dashboard (FastAPI), nicht von einem generischen Window-Compute-Fehler.

### 2. Session- und Login-Mechanismus

| Mechanismus | Implementierung |
|-------------|-----------------|
| Cookie | `session_id`, `httponly=True`, `max_age=86400` (24 h) bei Login |
| Speicher | In-Memory `sessions` + Persistenz `dashboard/logs/sessions.json` (überlebt Neustarts) |
| Login/Logout | `app.py` setzt/löscht Cookie; ungültige ID nach Restart ohne Eintrag → `get_current_user` → `None` → 401 |
| Landscape-Router | Alle relevanten Viewer-APIs hängen an `require_auth` (Capabilities, Window, …) |

### 3. Session-TTL

- **Cookie:** fest **86400 s (24 h)** (`set_cookie` bei Login).
- **Server:** kein separates serverseitiges Expiry-Feld in `sessions.json`; Gültigkeit = Cookie vorhanden **und** `session_id in sessions`.
- **Änderung in P23:** keine.

### 4. Werden Auth-Cookies bei Window-Fetches mitgesendet?

- `fetchWindow` (und Caps/Bounds) nutzen `credentials: "same-origin"` in `pd1_exact_origin_density_research_v1.js`.
- **Beweisgrad:** **HOCH** für korrekte Browser-Semantik; Fehler tritt auf, wenn Cookie fehlt/ungültig ist, nicht wegen explizitem `credentials: "omit"`.

### 5. Proxy statt Dashboard?

- P22: Journal-Einträge `dashboard-user[2379483]` mit Status **401** auf Window-POST.
- Kein Hinweis auf externes Auth-Gateway vor FastAPI.
- **Beweisgrad:** **MITTEL-HOCH** (Log-Korrelation); kein separater Reverse-Proxy-401 ohne App-Log in dieser Untersuchung.

### 6. Verhalten vor P23 nach erneutem Login

- Bei total fehlgeschlagenem Ladepfad ohne Pools: generischer Hinweis **„B – Exact Origin: window load failed (prior data retained)“** — identisch für 401, 503, Timeout usw. (P22).
- Bereits gerenderte Bänder: `exactOriginSegmentCache` → `pushCanvasDensity` bei Fehlerpfad (Prior-Daten bleiben).
- Automatischer Pool-Refresh nach Login: **fehlte** — nur manueller Toggle/Viewport/Force-Refresh.

### 7. Refresh-Gate nach Session-Wiederherstellung

- `_eoRefreshGate` coalesced weiterhin Viewport-Refreshs; bei dauerhaftem 401 führten parallele Window-Worker zu wiederholten 401-Requests (kein Batch-Abbruch, keine Pause).
- Nach Login ohne expliziten Re-Refresh: Gate konnte pending Work abarbeiten, aber **kein** gezieltes Session-Recovery.

---

## Phase B — Minimaler Fix (nur Spread-Frontend)

**Repo / Pfad:** `spread_recovery_hedge_short_dev` (laufendes Dashboard; **nicht** committed)

### Geänderte Dateien

| Datei | Änderung |
|-------|----------|
| `dashboard/static/js/research/pd1_exact_origin_density_research_v1.js` | 401/403-Erkennung, Auth-Hinweis, Batch-Abbruch, `authSessionPaused`, Recovery nach Focus/Visibility + Bounds-Probe, Prior-Segmente bei Pause |
| `dashboard/templates/research_charts.html` | Cache-Bust `?v=pd1-p23-auth-session-v1` |

### Verhalten (kurz)

1. **`windowHttpError`:** setzt `err.authFailure` bei HTTP 401/403.
2. **`fetchPlannedWindows`:** zählt `authFailures`, bei Auth-Fehler `authBatchAbort` + `authSessionPaused` (keine weiteren Window-POSTs in diesem Batch).
3. **Hinweis:** `EXACT_ORIGIN_AUTH_HINT` = *„B – Exact Origin: Sitzung abgelaufen – bitte erneut anmelden.“* statt generischem „window load failed“ wenn Auth betroffen.
4. **Prior-Daten:** bei Pause/Fehler weiterhin `pushCanvasDensity` aus `exactOriginSegmentCache`.
5. **Kein Retry-Loop bei 401:** `refreshPaneImpl` bricht bei `authSessionPaused` früh ab; Recovery nur über `probeSessionAuthOk()` (GET Bounds, 200) dann `refreshAll({ force: true })`.
6. **Gate:** `authSessionPaused` verhindert neue Window-Läufe bis Probe OK; erfolgreiche Fetches (`fetched > 0`, keine Auth-Failures) heben Pause auf.
7. **429/503/Timeout:** unveränderte generische Fehlerpfade / bestehende Cache-Fehlermarkierung (nur Nicht-Auth-Fehler schreiben weiter `touchExactOriginWindowCache`).

### Test-Hooks (nur JS)

`window.PD1ExactOriginResearch._p23AuthTestHooks`: `isAuthHttpStatus`, `exactOriginFailureHint`, `windowHttpError`.

---

## Phase C — Regressionstests

| # | Fall | Ergebnis |
|---|------|----------|
| 1 | Gültige Session → Window 200 | **UNVERIFIED** (Browser); Server-Probe mit Session in P22 **200** |
| 2 | Ungültige Session → 401 → Auth-Meldung | **CODE + curl 401**; UI **UNVERIFIED** |
| 3 | Nach Login → 200 → Pools laden | **CODE** (Recovery-Watch); E2E **UNVERIFIED** |
| 4 | Kein endloses Retry bei 401 | **CODE** (`authBatchAbort`, `authSessionPaused`, early return) |
| 5 | Prior-Bänder bei Fehler | **CODE** (segment cache repaint); P22-Verhalten beibehalten |
| 6 | P12/P16/P17/P18/P19 unberührt | Keine Änderungen an PD1-Engine, Publisher, Trading, Multicoin-Backend |
| 7 | Kanonische DOGE-PD1-D2-Berechnung | Keine Backend-/Engine-Änderung |

**Ausgeführt:**

```text
cd spread_recovery_hedge_short_dev/dashboard && python -m pytest test_p23_exact_origin_auth_session_v1.py -q
# 3 passed
```

Neue Testdatei: `spread_recovery_hedge_short_dev/dashboard/test_p23_exact_origin_auth_session_v1.py`

---

## Browser-Verifikation

**Status: UNVERIFIED** — kein manueller Login/Logout-Zyklus in dieser Session dokumentiert.  
Empfohlener Check: Research-Charts → Exact Origin → Session ablaufen simulieren (Logout / Cookie löschen) → Auth-Hinweis → Login → Tab-Focus → Pools-Refresh.

**Hinweis:** Nach Deploy des JS **Hard-Refresh** oder Cache-Bust-Query (`pd1-p23-auth-session-v1`) nötig; **kein** Dashboard-Restart erforderlich für statisches JS.

---

## Grenzen eingehalten

- Keine Änderungen an Publisher, Collector, PD1/D2-Engine, Candles/EMA, Trading-Boxen, Multicoin-Backend, `app.py` Session-TTL.
- Kein `git commit` / kein `git push`.

---

## STOP

P23 abgeschlossen. Nächster Schritt nur auf Wunsch: Browser-E2E bestätigen oder Commit auf Spread-Branch.
