# DOGE-LIVE-P24E — Identischer Viewport: Supersede-Endlosschleife

**Erstellt:** 2026-11-13  
**Vorgänger:** P24D (November-Viewport OK), Nutzer-Trace 30m/30m

**Ergebnis:** `P24E_IDENTICAL_VIEWPORT_NO_SERIAL_BUMP`  
**Browser:** **UNVERIFIED** (Hard-Refresh `pd1-p24e-identical-viewport-v1`)

Kein Commit / Push / Restart.

---

## Phase A — Root Cause (Call-Sites)

| Metrik (Nutzer-Trace) | Bedeutung |
|------------------------|-----------|
| **97** `exactOrigin_load_serial_bump` | Ausschließlich `refreshPane` → `bumpExactOriginLoadSerial("coalesced_viewport_or_force")` bei `gate.inflight` (~1413–1415). |
| **97** `refreshPane_coalesced` | Folgeaufrufe mit **gleichen** `pending_from_unix` / `pending_to_unix`. |
| **96** `async_superseded` | `loadSerial_mismatch_after_fetch` / `_before_render` nach Bump während Fetch. |
| **0** `refreshPane_render` | Render vor `pushCanvasDensity` nie abgeschlossen. |

### Warum Bump trotz identischem Viewport?

`eoRefreshPendingSupersedesInflight(pending)` lieferte **true** für jedes `viewport:true` oder gesetztes `fromUnix`/`toUnix` — **ohne** Vergleich mit `gate.inflightOpts`.

**Kette:**

1. `refreshPaneImpl` startet mit `loadSerial = N` (lokaler Capture).  
2. Während Window-Fetch (30m ~2 s): dutzende `scheduleViewport` / `refreshPane({viewport, gleiche from/to})`.  
3. Jeder Coalesce → **Bump** → `pane.exactOriginLoadSerial = N+k`.  
4. Fetch endet mit lokalem `N` → Mismatch → `scheduleSupersededDensityRefresh(48ms)`.  
5. Supersede startet neuen Lauf → erneut identische Coalesces → **Endlosschleife**.

**Kein** neuer Chart-/Pool-TF, **kein** neues Symbol, **kein** geänderter November-Viewport — nur **duplizierte** Viewport-Refreshes.

P24C-Suppression blockiert nur `on_visible_range`→`scheduleViewport` während TF-Wechsel, nicht Coalesce auf bereits laufendem Gate (Post-TF, `exactOriginRefreshAfterTfSwitch`, Supersede, parallele `refreshPane` aus `loadPane`).

---

## Phase B — Fix (minimal)

**Datei:** `pd1_exact_origin_density_research_v1.js`

| Änderung | Wirkung |
|----------|---------|
| `pendingRefreshMateriallyChangesInflight(inflightOpts, pending)` | Bump nur bei **force**, **anderem** `fromUnix`/`toUnix`, oder explizitem neuem VP auf offenem inflight. **Gleiches VP-Paar → kein Bump**, `gate.pending = null`. |
| `gate.inflightOpts` | Gespeicherte Ops des laufenden Laufes zum Vergleich. |
| Trace `material_change` | Sichtbar in `refreshPane_coalesced`. |
| `scheduleSupersededDensityRefresh` | Kein Schedule wenn `gate.inflight` aktiv; Dedup `_eoSupersedeScheduledKey` für identischen Retry. |

**Unverändert:** P24C/D, 5m-Hist-VP, On-Demand-Viewport-Priorität, P23-Auth.

**Cache-Bust:** `pd1-p24e-identical-viewport-v1`

---

## Phase C — Tests

```text
pytest test_p24e_identical_viewport_coalesce_v1.py \
       test_p24d_on_demand_viewport_resolve_v1.py \
       test_p24c_tf_switch_viewport_suppress_v1.py -q
# 13 passed
node static/js/research/test_p24e_coalesce_logic_v1.js  # 5 coalesce cases
```

| Szenario | Abdeckung |
|----------|-----------|
| 100× identisches VP während Fetch | Harness: identical → no material change |
| Echter VP-Wechsel | Harness: moved from → material |
| force | Harness: always material |
| Wiring / Supersede dedup | pytest strings |

---

## Phase D — Browser

| Status | Hinweis |
|--------|---------|
| **UNVERIFIED** | Erwartung: `material_change=false` bei Duplikaten; **ein** `refreshPane_render result=OK`, `canvas_drawn>0`; ≪97 Window-POSTs. |

---

## Geänderte Dateien

- `pd1_exact_origin_density_research_v1.js` (Coalesce + Supersede)
- `templates/research_charts.html` (cache bust)
- `test_p24e_identical_viewport_coalesce_v1.py`, `test_p24e_coalesce_logic_v1.js` (neu)
- Cache-bust-Asserts in P23/P24C/P24D-Tests

---

## STOP

P24E implementiert. Bitte 30m/30m mit Trace erneut prüfen.
