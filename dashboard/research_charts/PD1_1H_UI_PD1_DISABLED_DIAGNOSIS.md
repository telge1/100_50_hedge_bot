# PD1 1h UI — Diagnose „PD1 nicht aktivierbar“

**Datum:** 2026-11-10  
**Symptom:** Kerzen-TF 1h + Exact Origin → PD1-Checkbox deaktiviert, kein Pool-TF-Selector, keine Bänder.

---

## 1. Fehlerursache (primär)

**Kombination aus veralteter Frontend-Logik im Browser-Cache und fehlender Cache-Invalidierung.**

| Faktor | Detail |
|--------|--------|
| **Alte JS-Logik (vor Pool-TF-Entkopplung)** | `activeScopesForPane` koppelte Pool-Timeframe an **Kerzen-`pane.tf`**. Bei **Kerzen 1h** war `isScopeReleased("1h", 1)` **false** (1h war nicht in `RELEASED_VIEWER_SCOPES`). → `syncPoolDetailUi` setzte **PD1 `disabled`**. |
| **Cache-Buster unverändert** | `research_charts.html` lud `pd1_exact_origin_density_research_v1.js?v=phase6xt-bt-d36-density-supersede-v1` — Browser hält altes Bundle trotz neuer Datei auf der Platte. |
| **HTML ebenfalls gecacht** | Neues Markup `researchExactOriginPoolTimeframe` (Pool-TF 5m/1h) in **neuem** Template — ohne Hard-Reload sieht man nur alte „Density PD“-Zeile ohne Pool-TF. |

**Backend** auf dem Server ist die **neue** On-Demand-1h-Pipeline in der Codebasis vorhanden; API-Tests lokal OK. Für **Capabilities 1h** ist kein separater Prozess-Neustart nötig (dynamischer Import). **Für korrekte UI muss der Browser frische JS/HTML laden.**

---

## 2. `dash.inmotel.de:8080` vs. lokaler Stand

| Prüfung | Ergebnis |
|---------|----------|
| **Laufender Dashboard-Prozess** | `PID 1313700` — `python app.py` in `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` (Port **3000**). |
| **Lokaler Port 8080** | **Apache/WordPress** (301 Redirect) — **nicht** das Trading-Dashboard. |
| **Extern `dash.inmotel.de`** | Von dieser Umgebung nicht erreichbar (DNS/Netz); Proxy-Ziel nicht in `/etc/nginx` / `/etc/apache2` auffindbar. |
| **Code auf Platte** | Pool-TF-Selector + `selectedPoolTimeframe()` in `research_charts.html` / `pd1_exact_origin_density_research_v1.js` **vorhanden**. |

**Fazit:** Wenn du `dash.inmotel.de:8080` nutzt, muss dort derselbe Dashboard-Code + frischer Browser-Cache vorliegen wie auf `spread_recovery_hedge_short_dev`. **Nicht annehmen, dass :8080 automatisch Port-3000-App ist** (lokal ist :8080 WordPress).

---

## 3. Betroffene Dateien / Funktionen

| Datei | Funktion / Stelle |
|-------|-------------------|
| `pd1_exact_origin_density_research_v1.js` | **`activeScopesForPane`** (alt: `paneChartTimeframe`), **`syncPoolDetailUi`** (`el.disabled = !released`) |
| `research_charts.html` | Fehlendes **`researchExactOriginPoolTimeframe`** in gecachter Seite |
| `pd1_exact_origin_density_research_v1.js` | Query-Param **`?v=phase6xt-...`** ohne Bump → stale Cache |

---

## 4. Wird neue UI ausgeliefert?

- **Server-seitig (Template + static):** Ja, ab Dateiänderung bei nächstem Request (kein separates Deploy nötig).
- **Browser:** Oft **Nein**, solange alter `?v=` — genau das Symptom (kein Pool-TF, PD1 disabled an 1h-Chart).

---

## 5. Datenpfad (wenn UI korrekt)

Bei PD1 + Pool-TF **1h**:

- `POST /live-charts/viewer/api/window` mit `"timeframe":"1h"`, `"pool_detail":1`, `"response_mode":"exact_origin_v1"`.
- `provenance.pipeline_line_id` = `on_demand_ch1m_1hpd1_v1`.
- Density über `PD1ExactDensityTrpAdapter` — lokal verifiziert (keine 5m-IDs in 1h-Payload).

---

## 6. Durchgeführter lokaler Fix (ohne Neustart)

| Änderung | Zweck |
|----------|--------|
| Cache-Buster `?v=pd1-1h-on-demand-pool-tf-v1` (Adapter + Exact-Origin JS) | Erzwingt frisches Bundle |
| `VERSION = "pd1-1h-on-demand-pool-tf-v1"` | Debug/Hinweise |
| Hinweis in `syncPoolDetailUi` wenn Pool-TF-Radios fehlen + Chart 1h | „Strg+Shift+R“ |

**Kein** Neustart von `app.py`, **kein** Deploy, **keine** CH-/Strategie-Änderungen.

---

## 7. Nutzeraktion (sofort testbar)

1. Research-Chart **Hard-Reload:** `Strg+Shift+R` (oder Cache leeren).
2. Prüfen: neben „Density PD“ erscheint **„Pool-TF: 5m | 1h“**.
3. **Kerzen 1h + Pool-TF 5m** → PD1 anklickbar → 5m frozen Pools.
4. **Kerzen beliebig + Pool-TF 1h** → PD1 → On-Demand-1h-Bänder.
5. DevTools → Network: `pd1_exact_origin_density_research_v1.js?v=pd1-1h-on-demand-pool-tf-v1`.

---

## 8. Deployment / Neustart

| Maßnahme | Erforderlich? |
|----------|----------------|
| **Browser Hard-Reload** | **Ja** |
| **Dashboard-Neustart** | Nein (für UI/On-Demand-Route; Prozess lädt Static neu) |
| **Produktions-Deploy** | Nur falls `dash.inmotel.de:8080` **nicht** auf dieses Repo/`app.py` zeigt |

---

## 9. Antworten auf die Checkliste

1. **Ursache:** Gecachtes **altes** Exact-Origin-JS + Kopplung Chart-TF↔Pool-TF; fehlender Pool-TF in gecachtem HTML.  
2. **Datei/Funktion:** `pd1_exact_origin_density_research_v1.js` — `syncPoolDetailUi` / `activeScopesForPane`.  
3. **Neue UI ausgeliefert:** Auf Server ja, im Browser oft nein bis Hard-Reload.  
4. **PD1 aktivierbar:** Nach Reload + Pool-TF **5m** auch bei Kerzen **1h**; für 1h-Pools Pool-TF **1h**.  
5. **1h-Pools laden:** API ja (lokal getestet); UI nach frischem JS.  
6. **Änderung:** Cache-Buster + Hinweis-Text.  
7. **Neustart:** nicht nötig für diesen Fix; ggf. Proxy/Deploy klären falls anderer Host als `spread_recovery` Port 3000.
