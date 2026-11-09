# L1 V3AD — READ-ONLY Audit: Vorhandene Upper-Pool-TP-Logik

**Auftrag:** Code- und Dokumentationsrecherche. Keine Implementierung, keine Backtests, keine Parameteränderungen.

**Scope:** `manual_channel_pool_long_v1/` (V3U–V3AB/V3AC), verwandte Specs (`Long_kanal+pools.md`, `Long_logik.md`), P17/EMA59 nur wo TP-Selektion berührt; isoliert von Manual-Channel-L1.

**Referenztrade (V3AC):** `te-d-c0791ea0c65e-0003` · Entry `2026-08-26 20:30 UTC` @ `0.08491` · V3AB-TP `0.09496` · Supportbruch `+2.89 %` · Violett-Konzentration ~`0.08929–0.08962` (depth 5, 6 Origins, merged).

---

## Executive summary

| Layer | Was existiert? |
|-------|----------------|
| **Nutzer-/Forschungstext** | Klare **Intention**: keine dünnen/unmittelbaren Mini-Pools als TP; **substanzielle** Upper-D2-Konzentrationen; Pool-Unterkante als TP (`Long_kanal+pools.md`, §4/8/189–192; `Long_logik.md` §9 LONG A). Teilweise **noch offen** (exakte Schwellen, Fix vs. dynamisch). |
| **L1-Produktionspfad (V3U→V3AB)** | **`pick_tp()` → `max(lower_bound)`** unter D2-qualifizierten Bändern (**Fernziel**). **Widerspricht** der Intuition „nächste sinnvolle Konzentration“, **vermeidet** aber explizit „nächstes dünnes Band“ als Limit-Preis. |
| **Substantial-Zone (ausführbar)** | **`tp_research_v3w/tp_targets.py`**: `merge_qualified_zones` + `resolve_tp2` / `resolve_tp3` / `resolve_multi`. **RESEARCH_ONLY**, nicht an V3Y/V3AB angebunden. |
| **Entry-Landschaft (substantial)** | **`pool_landscape.py`**: Zonen-Merge + Filter — nur **Entry-Gate** (`pool_landscape=True`), **kein** TP. |
| **Violett für Referenztrade** | Wird von **`merge_qualified_zones`** als **Zone #4** erkannt (`0.08929–0.089624`, depth 5, 6 Origins). **Kein** freigegebener TP-Resolver wählt diese Zone als **primäres** Ziel @ Entry. |

---

## 1. Suchmatrix — Begriffe und Treffer

| Begriff / Konzept | Primäre Fundstellen | Rolle |
|-------------------|---------------------|--------|
| `substantial` | `pool_landscape.py`, `config.py` (L3 V3), `tp_research_v3w/tp_targets.py`, `l1_v3_pool_landscape.md` | Entry-Filter vs. TP2-Forschung |
| `merge_qualified_zones` | `tp_research_v3w/zone_features.py` | D2-Segment-Cluster → `ResearchZone` |
| `pick_tp` / `max(lower_bound)` | `run_l1_pilot.py` L436–518, `pool_d2_metrics.apply_d2_gate_to_pending` L130 | **V3AB Exit-TP** |
| `qualify_upper_d2_bands` | `pd1_exact_d2_adapter.py` L148–171 | Gate: depth≥2, dist≥0,5 % |
| `TP2` / first substantial | `tp_research_v3w/config_v3w.py`, `resolve_tp2` | Forschung |
| `REJECTED_FIRST_D2_TOO_FAR` | `pool_d2_metrics` + Variant V3E-B/C | **Entry**, nicht TP |
| `POOL_BOTTOM_*` | `run_l1_pilot.pick_tp`, Exit `support_sim.simulate_exit_v3y` | Produktion |
| Deutsch „dünn / klein / substanziell“ | `Long_kanal+pools.md` L35–36, L136, L189–192; `l1_v3v_single_trade_pool_tp_audit.md` | Spec / Forensik |

**Nicht L1-gebunden:** P17 EMA59 WATCH-TP (`pools+ob+delta_bot/results/phase_ema59_*`) — anderes Regelwerk (Progressive Upper-Pool, chart-after-watch). **Nicht** V3AB-Aufrufpfad.

---

## 2. Frühere Entscheidungen — Status je Frage

### 2.1 Kleine nahe Pools nicht als TP?

| Status | Beleg |
|--------|--------|
| **APPROVED** (Intention, Manual-Channel) | `Long_kanal+pools.md` L136: „keine kleinen isolierten Streifen automatisch als qualifizierte TP-Pools“; L189–191: „erster Pool zu klein/zu dünn“ → Long ablehnen. |
| **APPROVED** (LONG V3 Master, separat) | `Long_logik.md` L81–83: TP an geeignetem Upper-Pool; „Nähere nicht qualifizierte Upper-Pools nicht als erfüllten TP behandeln“ — Auswahl **noch zu spezifizieren**. |
| **IMPLEMENTED** (L1 Exit, indirekt) | `pick_tp` ohne Kanal: **fernstes** qual. D2 — setzt Limit weit über nahe dünne Bänder (V3M/V3N). |
| **CONTRADICTORY** | V3N L168: Nutzer-„sinnvolles Ziel“ **nicht** als `max()` dokumentiert; Fernziel ist **Implementierungs-Artefakt**. Intention „nicht zu nah“ ≠ „immer am weitesten“. |
| **RESEARCH_ONLY** | `TP1_nearest_d2` — explizit **nächstes** Band (würde Mini-Ziele treffen). |

### 2.2 Auswahl dichter / relevanter Upper-Bänder?

| Status | Beleg |
|--------|--------|
| **APPROVED** (unvollständig) | `Long_kanal+pools.md` L35–36: „dichte, zusammenhängende Bänder“; L153: „TP bevorzugt an Unterkante des … Upper-D2-Pools“. Schwellen **offen** (L75–79). |
| **RESEARCH_ONLY** | `tp_research_v3w` TP2: **erste** Zone mit width/depth/origins-Schwellen (`config_v3w.py` L21–25). |
| **IMPLEMENTED** (Entry, nicht TP) | `pool_landscape.analyze_upper_landscape` — „substantial“ vs „thin“ für **Filter** `L1_V3_VARIANT_B+POOL`. |
| **NOT_FOUND** als V3AB-TP | Keine freigegebene Produktregel „nächste **D2-Konzentration** mit depth≥X und ≥Y Origins“. |

### 2.3 Kriterien Breite / Density / Origins?

| Quelle | Breite | Depth | Origins | Status |
|--------|--------|-------|---------|--------|
| Gate (alle L1-Exits) | Einzelband `width_pct` nur in Metriken | **≥ 2** (`D2_MIN_DEPTH`, `config.py` L78) | in Exact-D2-Merge implizit | **IMPLEMENTED** (Gate only) |
| `pool_landscape` (Micro-Member) | `MIN_UPPER_BAND_WIDTH_PCT` **0.12 %** oder ≥3 Bänder/Zone | nicht D2 | Proxy `pool_id`-Tail | **RESEARCH_ONLY** (Entry) |
| TP2a/b/c | **0.08 / 0.12 / 0.15 %** Zonenbreite | **3 / 4 / 5** | **3 / 4 / 4** unique origins | **RESEARCH_ONLY** |
| `Long_kanal+pools.md` | „muss definiert werden“ | depth≥2 SSOT | Unique-Origin explizit | **APPROVED** Rahmen, **keine** TP-Schwellen |

### 2.4 Mindestabstand Entry → TP?

| Wert | Status | Fundstelle |
|------|--------|------------|
| **0,50 %** | **IMPLEMENTED** | `config.MIN_POOL_DIST_PCT`, `qualify_upper_d2_bands` |
| **0,50 %** (Vergleich) | **APPROVED** Forschung | `Long_kanal+pools.md` L140, L167 |
| **2,50 %** erste D2 im Kanal | **IMPLEMENTED** Entry-Reject V3E-B | `MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT` |
| **2,20 %** erste subst. Zone | **RESEARCH_ONLY** Entry | `MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT` |

### 2.5 Erste substanzielle Zone vs. stärkste Konzentration?

| Regel | Wahl | Status |
|-------|------|--------|
| `resolve_tp2` | **Erste** merged Zone ≥ Schwellen (von unten) | **RESEARCH_ONLY** |
| `resolve_tp3` | **Max score** (depth / origins / depth×width) unter allen Zonen | **RESEARCH_ONLY** — tendiert **fern** |
| `pick_tp` pool-only | **Max** `lower_bound` | **IMPLEMENTED** V3AB |
| Nutzerintuition Violett | „sichtbare Konzentration“ mid-range | **APPROVED** narrativ, **NOT_FOUND** als Code-Regel |

### 2.6 Zone aus mehreren D2-Segmenten?

| Status | Implementierung |
|--------|-----------------|
| **IMPLEMENTED** | `zone_features.merge_qualified_zones` (gap 0,15 % = `ZONE_MERGE_GAP_PCT`) — **nur V3W** |
| **IMPLEMENTED** (Micro) | `pool_landscape.py` Zonen-Merge — Entry-Metriken |
| **IMPLEMENTED** (SSOT) | `pd1_exact_d2_adapter.compute_exact_bands_for_side` — D2-Bänder vor Qualifikation |
| **Nicht** in `pick_tp` | Produktion arbeitet auf **Einzel-D2-Qualify-Liste**, Auswahl **ein** Band (`max`) |

### 2.7 TP fix @ Entry vs. dynamisch?

| Pfad | Verhalten | Status |
|------|-----------|--------|
| V3U/V3Y/V3AB | `apply_d2_gate_to_pending` einmalig; `_pool_tp` statisch; `support_sim` | **IMPLEMENTED** |
| `tp_targets.py` Header | „static at entry decision“ | **RESEARCH_ONLY** |
| LONG B Dynamic TP | `Long_logik.md` L86–95 | **APPROVED** Spec, **NOT_FOUND** in L1 manual channel |

### 2.8 Implementiert vs. nur erforscht?

| Artefakt | Implementiert | Verdrahtet V3AB |
|----------|---------------|-----------------|
| D2 Gate depth≥2 + 0,5 % | Ja | Ja |
| `pick_tp` max D2 | Ja | Ja (`v3ab_context` → `apply_d2_gate_to_pending`) |
| `pick_tp` near channel | Ja | Ja (wenn Deckel) |
| `pool_landscape` substantial | Ja | **Nein** (nur alte Variant `B+POOL`) |
| V3E Nearness | Ja | **Nein** in V3AB (V3L Varianten) |
| V3W TP1–TP5, M1–M5 | Ja | **Nein** (`execute_variant_v3y` nutzt `_pool_tp` aus Gate) |

---

## 3. Inventar — TP-Auswahlfunktionen

### 3.1 Produktion / V3AB-Pfad

| Funktion | Datei | Kriterien | Auswahl | Aufrufer |
|----------|-------|-----------|---------|----------|
| `upper_d2_bands_at_close` | `pd1_exact_d2_adapter.py` | ACTIVE Exact D2 @ `decision_close`, depth≥2, Preisfenster | alle Bänder | `analyze_d2_landscape` |
| `qualify_upper_d2_bands` |同上 | `bottom>entry`, dist≥**0,5 %** | sortiert nach bottom | Gate |
| `pick_tp` | `run_l1_pilot.py` | + optional `channel_upper_at` (12 %, max price) | Mit Kanal: near-pool **max** unter Deckel oder Deckel; **ohne: `max(lower_bound)`** | `apply_d2_gate_to_pending`, `build_pilot_context` |
| `apply_d2_gate_to_pending` | `pool_d2_metrics.py` | kombiniert oben | schreibt `tp_price`, `_pool_tp` | `v3ab_context.build_context_from_snapshot` |
| `simulate_exit_v3y` | `support_break_exit_v3y/support_sim.py` | `high >= pool_tp` | chronologisch vs. SL/Supportbruch | `support_break_exit_v3ab` Engine |

**PD1-Density:** Exact Unique-Origin D2, **nicht** Micro-Strength direkt in `pick_tp`.

### 3.2 Forschung — substantielle Zonen (ausführbar, isoliert)

| Funktion | Datei | Kriterien | Auswahl |
|----------|-------|-----------|---------|
| `merge_qualified_zones` | `tp_research_v3w/zone_features.py` | Merge qual. D2 mit gap **0,15 %** | Zonenliste |
| `enrich_zone_origins` |同上 | ACTIVE Micro-Origins in Zonen-Spanne | `unique_origins` |
| `resolve_tp2` | `tp_research_v3w/tp_targets.py` | width, depth, origins ≥ Grid | **Erste** passende Zone von unten |
| `resolve_tp3` |同上 | depth≥2, dist≥0,5 % | **Max** `zone_score` |
| `resolve_tp1` / `resolve_tp4` |同上 | qual. Liste | **min** / **max** bottom |
| `resolve_multi` (M3…) |同上 | subs: depth≥3, origins≥3 | z. B. M3: **erste zwei** subst. Zonen |

**Aufrufer:** `tp_research_v3w/run_v3w.py` (eigener Backtest); **nicht** `run_v3ab.py`.

### 3.3 Entry-only — „substantial“ (kein TP)

| Funktion | Datei | Zweck |
|----------|-------|--------|
| `analyze_upper_landscape` | `pool_landscape.py` | substantial vs thin; Filter `FIRST_SUBSTANTIAL_TOO_FAR` etc. |
| `enrich_pool_landscape` | `pilot_engine.py` L245+ | setzt `_pool_landscape_pass` |
| `analyze_d2_channel_filters` | `pool_d2_metrics.py` | V3E Nearness / Quarter |

---

## 4. Referenztrade — welche Logik trifft Violett?

**Merge-Zonen @ Entry (Auszug, `merge_qualified_zones` + Origins):**

| Idx | Bottom | Top | depth | origins | dist % |
|-----|--------|-----|-------|---------|--------|
| 0 | 0.08536 | 0.08546 | 4 | 4 | 0.53 |
| 1 | 0.08604 | 0.086276 | 5 | 5 | 1.33 |
| 2 | 0.08702 | … | 4 | 5 | 2.49 |
| 3 | 0.08895 | … | 3 | 3 | 4.76 |
| **4** | **0.08929** | **0.089624** | **5** | **6** | **5.16** ← Violett |
| … | … | … | … | … | … |

### 4.1 Welche Funktion „erkennt“ die Konzentration?

| Funktion | Erkennung | Gewählt als TP? |
|----------|-----------|-----------------|
| `merge_qualified_zones` + `enrich_zone_origins` | **Ja**, Zone #4 | — |
| `resolve_tp2` (alle Grid-Parameter) | Ja, aber **überspringt** zu #0/#1 (erste Substanz) | TP **0.08536–0.08604** |
| `resolve_tp3` | Alle Zonen scores | **0.09342–0.09436** (ferner) |
| `pick_tp` / V3AB | 73 Einzelbänder | **0.09496** (`max`) |
| `analyze_upper_landscape` (Micro) | 14 subst. Zonen; nearest dist **0,53 %** | **kein TP** (nur Metrik) |
| `M3_50_50_substantial_2zones` | subs[0], subs[1] | **0.08536**, zweites Bein 0.08604 |

**Touch Violett-Unterkante:** `2026-08-27 08:45 UTC` (V3AC) — wäre mit Limit **0.08929** intrabar ausführbar gewesen; **nicht** mit V3AB-TP 0.09496.

### 4.2 Warum V3AB es nicht nutzt

1. `v3ab_context.build_context_from_snapshot` → `apply_d2_gate_to_pending` → **`l1_module.pick_tp`** (ohne `independent_support`, ohne V3W).
2. Kein Import/`resolve()` aus `tp_research_v3w`.
3. V3Y-Design explizit: **keine** Änderung an `pick_tp` (`L1_V3X_SUPPORT_BREAK_EXIT_PLAN.md`).

### 4.3 Wo ist die Nutzer-Anforderung dokumentiert, ohne TP-Code?

- `Long_kanal+pools.md` (Forschungsentwurf + Merksatz L189–192).
- `l1_v3n_channel_tp_architecture_audit.md` §6.3, §9, §10.C.2 (offene Entscheidung substantial vs max).
- `l1_v3v_single_trade_pool_tp_audit.md` §7: „Keine validierte objektive Regel im Code“ für substantielle TP-Zone.

### 4.4 Was fehlt wirklich?

1. **Freigegebene** Auswahlregel zwischen „erste substanzielle“, „beste score-Konzentration im sichtbaren Band“ und „fern“ — nur Teilfragen in V3W getestet.
2. **Verdrahtung** einer gewählten V3W-Variante (oder extrahiertes `resolve_tp2`) in den Gate-Pfad.
3. Optional: Skip-Regel für **zu nahe** Substanz-Zonen (ähnlich V3E), ohne Fern-`max` zu erzwingen.
4. **Keine** Lücke in D2-SSOT oder Zonen-Erkennung für Violett — Lücke ist **Selektor + Freigabe**.

---

## 5. Widersprüche (CONTRADICTORY)

| A | B |
|---|---|
| Nutzer: keine kleinen nahen TP-Pools | `TP1`/nearest-Forschung wählt exakt diese |
| Nutzer: substantielle Konzentration | `pick_tp` max wählt oft **fernste** Einzelband-Unterkante, nicht merged Konzentration |
| Nutzer: realistisches Ziel (`Long_kanal` L153) | V3AB Referenz: Ziel +11,8 %, Kurs max ~5,2 % über Entry |
| V3W Aug-26-Bericht | TP2 trifft **0.08604**, nicht Chart-Violett 0.0894 — substantiell ≠ visuell dominantes Band |

---

## 6. P17 / EMA59 / LONG V3 (kurz)

| System | TP-Logik | Bezug V3AB |
|--------|----------|------------|
| P17 landscape research | Progressive Upper, WATCH→TP origins | separates Repo-Pfad; nicht `pick_tp` |
| `Long_logik.md` LONG A/B | fixed vs dynamic upper gap 3,5 % | Spec **nicht implementiert** in manual_channel L1 |
| `long-uptrend-pool-rule.md` | Spike 1m, Exit bei neuem rotem Pool | andere Strategieklasse |

---

## 7. Minimaler Übernahmeplan (nur Beschreibung, keine Ausführung)

**Ziel:** Vorhandene substantial-Logik **wiederverwenden**, nicht neu erfinden.

1. **Entscheidung dokumentieren:** welches V3W-Modell (z. B. `TP2b_w012_d4_o4` vs. `M3_…`) strategisch dem Manual-Channel-Fall B entspricht — **nicht** TP3/max, sofern Intention „Konzentration“ gilt.
2. **Isolierter Fork** `support_break_exit_v3ad/` (V3AB klonen):
   - In Kontext-Build `apply_d2_gate_to_pending` ersetzen oder erweitern: nach `analyze_d2_landscape` **`tp_targets.resolve(model_id, row)`** statt `pick_tp` **nur wenn** `channel_upper is None`; mit Kanal unverändert `pick_tp`.
   - `row["_pool_tp"]` = `resolution.pool_tp`; Manifest mit `model_id`.
3. **Keine** Änderung an `simulate_exit_v3y`, Supportbruch, Zeichnungen.
4. **Smoke:** `test_v3w_smoke.py`-Muster + Referenztrade-Forensik (erwartet für TP2: ~0.08604 @ diesem Entry, **nicht** Violett — transparent kommunizieren).

**Hinweis:** Selbst `resolve_tp2` trifft den Referenz-Violett-Cluster **nicht** ohne **neue** Freigabe (z. B. „zweite substanzielle Zone“ oder höhere Mindest-Distanz zum ersten Substanz-Cluster). Das wäre dann **Strategieentscheidung**, nicht Wiederverwendung pur.

---

## 8. Antworten A / B / C

### A. Haben wir die Auswahl relevanter, substantieller Upper-Pool-Konzentrationen bereits **verbindlich** definiert?

**Teilweise APPROVED, nicht vollständig operationalisiert.**

- **Verbindlich als Intention:** `Long_kanal+pools.md` (§4 Substanz, §8 L189–192, Regel 2 §8 L136–141); ergänzend `Long_logik.md` §9 LONG A (Unterkante qualifiziertes Upper-Ziel, ≥0,5 %).
- **Explizit offen / Forschung:** Schwellen Breite/Origins/„erste vs. stärkste Zone“ (`Long_kanal` L75–79); V3N §10.C.2 listet Auswahl als **offene Entscheidung**.
- **Nicht verbindlich:** V3W-TP2-Parameter (RESEARCH_ONLY, `V3W_TP_RESEARCH_REPORT.md`).

### B. Ist diese Logik bereits als **ausführbarer Code** vorhanden? Warum nutzt V3AB sie nicht?

**Ja — aber nur im V3W-Forschungsmodul und im Entry-`pool_landscape`; nicht im V3AB-Exit-Pfad.**

| Code | Vorhanden | V3AB |
|------|-----------|------|
| Substantial-Zonen + TP2/TP3/M* | `tp_research_v3w/tp_targets.py`, `zone_features.py` | **Ruft `pick_tp` (max D2)** via `apply_d2_gate_to_pending` |
| Grund | V3Y/V3AB Scope: Supportbruch isoliert; `L1_V3X` verbietet `pick_tp`-Änderung im gleichen Schritt | bewusste Trennung |
| Violett-Cluster | von `merge_qualified_zones` **erkannt**, von **keinem freigegebenen Resolver** als primäres TP @ diesem Entry gewählt | |

### C. **Kleinste** Maßnahme zur Übernahme in isolierten Forschungsbacktester (ohne Neuerfindung)

1. Neuer Runner-Ordner (Klon V3AB) + Konfig-Flag `tp_model_id` (z. B. `TP2b_w012_d4_o4`).
2. Eine Zeile Pfadwechsel im Gate: **`from tp_targets import resolve`** → bei fehlendem Kanal `resolve(tp_model_id, row)` statt `pick_tp`; Felder wie heute mappen.
3. Manifest/CSV: `tp_model_id` mitschreiben; Regression mit `TP5_v3u_channel_pool` als Kontrolle.
4. **Vor Merge:** Nutzer-Freigabe welches `model_id` = „die früher gemeinte“ substantial-Regel (TP2-Grid vs. M3 zwei Zonen).

---

## 9. Fundstellen-Index (kurz)

| Pfad | Inhalt |
|------|--------|
| `pools+ob+delta_bot/Long_kanal+pools.md` | Substanz, kein dünner TP, Pool-Unterkante |
| `pools+ob+delta_bot/Long_logik.md` §9 | LONG A/B TP-Semantik (offen) |
| `run_l1_pilot.py` `pick_tp` | Produktions-Selektor |
| `pool_d2_metrics.py` `apply_d2_gate_to_pending` | V3AB Gate |
| `tp_research_v3w/tp_targets.py` `resolve_tp2` | first substantial zone |
| `tp_research_v3w/zone_features.py` | D2-Cluster |
| `pool_landscape.py` | Entry substantial (Micro) |
| `config.py` L19–24, L95–96 | Schwellen |
| `l1_v3n_channel_tp_architecture_audit.md` | Soll-Ist max vs intuition |
| `l1_v3v_single_trade_pool_tp_audit.md` | Violett @ Entry, kein Code-TP |
| `support_break_exit_v3ab/L1_V3AC_POOL_TP_AUDIT_AND_PLAN.md` | Referenztrade-Fakten |

---

**STOP** — READ-ONLY Audit abgeschlossen.
