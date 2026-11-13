# DOGE-LIVE-P24D — 30m/30m Viewport & Supersede-Loop

**Erstellt:** 2026-11-13  
**Vorgänger:** P24C (Viewport-Suppress), Nutzer-Konsole-Trace

**Ergebnis:** `P24D_ON_DEMAND_CHART_VISIBLE_VIEWPORT`  
**Browser:** Nutzer-Trace analysiert; **Re-Verify nach Hard-Refresh empfohlen** (`pd1-p24d-on-demand-viewport-v1`)

Kein Commit / kein Push / kein Restart.

---

## Phase A — P24C Auslieferung

| Prüfung | Befund |
|---------|--------|
| Template `research_charts.html` | `research_charts.js` + `pd1_exact_origin…` mit **`pd1-p24d-on-demand-viewport-v1`** (P24D-Bust inkl. P24C-Inhalt). |
| Live Static (`curl :3000/static/…`) | `beginExactOriginViewportSuppress` **4×** in `research_charts.js`; `runExactOriginPoolTfSwitch` **3×** in EO-JS — **P24C war geladen**. |
| `/live-charts/research` ohne Session | **401** (kein Template aus Route; Static unabhängig davon OK). |

**Fazit Phase A:** P24C **war aktiv**; verbleibendes Problem **nicht** „alter Cache ohne P24C“, sondern **falsche `resolveViewSec`-Quelle** + Supersede mit ungebundenem `viewport:true`.

---

## Phase B — Root Cause (Code + Nutzer-Trace)

### 1. Pilot-Cutoff bei 30m/30m

`resolveHistoricalLmv2ViewportCloseWindow(..., tfSec=1800)` nutzt `historicalLmv2CoverageBoundsUnix(1800)` = **Apr–Sep-Pilot**, nicht On-Demand-CH. Live-November → oft `viewport_after_replay_cutoff: true` → Hist-Pfad übersprungen.

### 2. Fallback `source=candles` (Feb–…)

Alte Reihenfolge in `resolveViewSec`:

1. Hist (bei 30m/30m oft Cutoff)  
2. Nur bei **crossTf** → Chart sichtbar  
3. **`candleBoundsSec`** = **gesamte** geladene Historie (z. B. ab **2026-02-10**)  
4. `chart_visible_range` **zu spät**

→ Trace: `resolveViewSec source=candles from=2026-02-10` trotz November im Chart.

### 3. August vs. November in `buildWindows`

Wechselnde View-Quellen (Hist-Pilot-Schnitt vs. volle Candles vs. sichtbarer Bereich) → unterschiedliche `intersectViewWithData` / 72h-Tiles → **August- und November-Fenster** im Sturm.

### 4. Supersede trotz P24C

P24C unterdrückt nur **`scheduleViewport`** (`on_visible_range`). **Nicht** betroffen:

- `bumpExactOriginLoadSerial` bei Coalesce (`refreshPane` + `viewport:true` während `force` inflight)  
- `scheduleSupersededDensityRefresh` → `refreshPane({ viewport: true })` **ohne** `fromUnix`/`toUnix` → erneut falsche `resolveViewSec` (candles)

→ wiederholte `loadSerial_mismatch_after_fetch` + `delay_ms=48`.

### 5. Was 5m/5m und 30m/5m stabil hält

- **5m/5m:** Hist-Resolver mit **300s** + Live-SSOT.  
- **30m/5m:** `crossTf` → `chart_visible_cross_pool_tf` **vor** Candles.

---

## Phase C — Fix (minimal)

**Datei:** `pd1_exact_origin_density_research_v1.js`

| Änderung | Zweck |
|----------|--------|
| `poolTfPrefersChartVisibleViewport()` (30m/1h/4h) | On-Demand-Pool-TFs ohne Publish-Hist-VP. |
| `resolveChartVisibleViewSec()` | Einheitlicher sichtbarer Bereich. |
| **`resolveViewSec`:** bei On-Demand + !crossTf → **zuerst** `chart_visible_on_demand_pool_tf`; Hist nur noch für **5m/15m**; **Chart sichtbar vor Candles**; Candles nur letzter Fallback. |
| `scheduleSupersededDensityRefresh` | Retry mit **explizitem** sichtbarem `fromUnix`/`toUnix` (`chart_visible_supersede_retry`). |

**Unverändert:** P24C-Suppress in `research_charts.js`, 5m-Publish-Pfad, Backend, PD1.

**Cache-Bust:** `pd1-p24d-on-demand-viewport-v1`

---

## Phase D — Tests

```text
pytest test_p24d_on_demand_viewport_resolve_v1.py \
       test_p24c_tf_switch_viewport_suppress_v1.py \
       test_p23_exact_origin_auth_session_v1.py -q
# 13 passed
```

Abgedeckt (strings/Codepfad): P24C+P24D Auslieferung, On-Demand-Vorrang, Supersede-Pin, P23 weiterhin im Bundle.

| Szenario | Automatisiert | Browser |
|----------|---------------|---------|
| 5m/5m | Hist-Pfad unverändert | Nutzer: **OK** |
| 30m/5m | crossTf-Pfad unverändert | Nutzer: **OK** |
| 30m/30m November | **chart_visible_on_demand** | **Re-Verify** |
| TF-Wechsel + Supersede | P24C + P24D | **Re-Verify** |
| Kein Feb/Aug-Fenster bei Nov-Viewport | Code-Reihenfolge | Trace: `source=chart_visible_on_demand_pool_tf` erwarten |

---

## Erwartetes Trace-Bild nach Fix

- `resolveViewSec` → `chart_visible_on_demand_pool_tf` (November)  
- `buildWindows` → stabile Nov-Tiles  
- `refreshPane_render result=OK`, `canvas_drawn>0`  
- Deutlich weniger `loadSerial_mismatch_after_fetch` / kein Dauer-Loop  

---

## Browser-Verifikationsstatus

| Status | Detail |
|--------|--------|
| Vor Fix (Nutzer) | 30m/30m: Mismatch-Loop, candles Feb, Aug/Nov-Mix |
| Nach Fix | **PENDING** — Hard-Refresh mit `pd1-p24d-on-demand-viewport-v1` |

---

## STOP

P24D implementiert. Bitte 30m/30m erneut mit Trace prüfen.
