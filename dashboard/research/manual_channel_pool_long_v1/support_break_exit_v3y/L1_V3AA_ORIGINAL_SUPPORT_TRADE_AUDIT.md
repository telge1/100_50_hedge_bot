# L1 V3AA — Original-Support Trade-Audit

Trade: DOGEUSDT 5m · `te-d-9b6e62dc884c-0006` · **nur eingefrorener V3U-Snapshot**
SHA256: `d05d218646aced84ffc0adbb8ca66a6a8f92048190e72dc9cea3140270fb594c`

**Hinweis:** Nutzer-neu gezeichnete grüne Linie im Live-Chart ist **nicht** verwendet.

## 2. Originale Supportlinie `d-9b6e62dc884c`

| Feld | Wert |
|---|---|
| Typ | `trend` |
| Anker 1 | `2026-08-28 14:00:00 UTC` @ `0.0840025339` |
| Anker 2 | `2026-09-04 00:00:00 UTC` @ `0.0784391768` |
| Steigung (ΔPreis/s) | `-1.003491535542e-08` |
| extend_left (Metadata) | `False` |
| extend_right (Linie) | `False` |
| Linie @ Entry-Close-Eval | `0.0818891807` |
| Linie @ 02:45 Close | `0.0818048875` |
| Linie @ 02:50 Close | `0.0818018770` |
| Linie @ 02:55 Close | `0.0817988665` |

## 3. Entry-Nachweis (Fakten aus Pending-Row + Kerzen)

| Schritt | UTC | Kerze O/H/L/C | Regel |
|---|---|---|---|
| Support-Touch (Kontakt) | `2026-08-31 00:15:00 UTC` | 0.08198 / 0.08226 / 0.08189 / **0.08217** | Touch-Phase endet hier (`contact_phase_end_utc`) |
| **C1-Bestätigung** (abgeschlossen) | `2026-08-31 00:20:00 UTC` | 0.08217 / 0.08239 / 0.08216 / **0.08233** | `confirm_variant=C1`, `confirm_utc` |
| **Entry-Fill** (nächster Open) | `2026-08-31 00:25:00 UTC` | **Open 0.08233** (= pending `entry_price`) | 5m-Open nach C1 |

- Touch-Event: `te-d-9b6e62dc884c-0006` · Support-ID = `touch_drawing_id` = **`d-9b6e62dc884c`** (eingefroren)
- `structure_class`: `INDEPENDENT_SUPPORT_TREND` · `long_trigger_allowed`: **yes**
- `drawing_causality`: **`RETROSPECTIVE_DRAWING`** — Linie war zum Entry-Zeitpunkt **nicht live „bekannt“**, nur Snapshot-Pilot
- `drawing_geometry_active`: **yes** · `drawing_anchor_status`: **BETWEEN_ANCHORS** (Segment 28.08. 14:00 → 04.09. 00:00, `extend_right=false`)
- Touch-Line-Preis (Pilot): `0.0818982122` bei Touch `00:15`
- TP / SL / Kanal: TP `0.085936`, SL `0.08134204`, oberer Kanal-TP `d-0e5e7004dccd`

## 4. Supportbruch (unabhängige Kerzenprüfung)

| Kerze | O | H | L | C | Linie@Close | C vs Linie % |
|---|---:|---:|---:|---:|---:|---:|
| Bruch 02:45 | 0.08194 | 0.08199 | 0.08178 | 0.08180 | 0.0818048875 | **-0.005975** |
| Bestätigung 02:50 | 0.08180 | 0.08182 | 0.08158 | 0.08160 | 0.0818018770 | -0.2468 |
| Exit-Fill 02:55 Open | **0.08160** | | | | | |

Δ Close Bruch→Bestätigung: **-0.2445 %** (Schwelle ≤ −0,20 %)

### Kerzenfolge Entry → Exit (FSM)

| UTC | C | Linie@Close | vs Linie % | State | Notiz |
|---|---:|---:|---:|---|---|
| 00:25:00 | 0.08226 | 0.08188918074453533 | 0.45283 | MONITOR |  |
| 00:30:00 | 0.08240 | 0.08188617026992871 | 0.62749 | MONITOR |  |
| 00:35:00 | 0.08244 | 0.08188315979532208 | 0.68004 | MONITOR |  |
| 00:40:00 | 0.08246 | 0.08188014932071545 | 0.70817 | MONITOR |  |
| 00:45:00 | 0.08240 | 0.08187713884610884 | 0.63859 | MONITOR |  |
| 00:50:00 | 0.08245 | 0.08187412837150221 | 0.70336 | MONITOR |  |
| 00:55:00 | 0.08233 | 0.08187111789689558 | 0.56049 | MONITOR |  |
| 01:00:00 | 0.08207 | 0.08186810742228896 | 0.24661 | MONITOR |  |
| 01:05:00 | 0.08175 | 0.08186509694768233 | -0.14059 | BREAK_PENDING | BREAK |
| 01:10:00 | 0.08188 | 0.0818620864730757 | 0.02188 | MONITOR | RECLAIM |
| 01:15:00 | 0.08209 | 0.08185907599846909 | 0.28210 | MONITOR |  |
| 01:20:00 | 0.08213 | 0.08185606552386246 | 0.33465 | MONITOR |  |
| 01:25:00 | 0.08205 | 0.08185305504925583 | 0.24061 | MONITOR |  |
| 01:30:00 | 0.08179 | 0.08185004457464921 | -0.07336 | BREAK_PENDING | BREAK |
| 01:35:00 | 0.08197 | 0.08184703410004258 | 0.15024 | MONITOR | BULL_HOLD |
| 01:40:00 | 0.08172 | 0.08184402362543595 | -0.15154 | BREAK_PENDING | BREAK |
| 01:45:00 | 0.08176 | 0.08184101315082934 | -0.09899 | BREAK_PENDING | NEUTRAL_ROLL |
| 01:50:00 | 0.08160 | 0.08183800267622271 | -0.29082 | BREAK_PENDING | NEUTRAL_ROLL |
| 01:55:00 | 0.08156 | 0.08183499220161608 | -0.33603 | BREAK_PENDING | NEUTRAL_ROLL |
| 02:00:00 | 0.08163 | 0.08183198172700946 | -0.24682 | BREAK_PENDING | NEUTRAL_ROLL |
| 02:05:00 | 0.08189 | 0.08182897125240284 | 0.07458 | MONITOR | BULL_HOLD |
| 02:10:00 | 0.08222 | 0.0818259607777962 | 0.48156 | MONITOR |  |
| 02:15:00 | 0.08209 | 0.08182295030318959 | 0.32638 | MONITOR |  |
| 02:20:00 | 0.08208 | 0.08181993982858296 | 0.31784 | MONITOR |  |
| 02:25:00 | 0.08208 | 0.08181692935397633 | 0.32154 | MONITOR |  |
| 02:30:00 | 0.08201 | 0.08181391887936972 | 0.23967 | MONITOR |  |
| 02:35:00 | 0.08203 | 0.08181090840476309 | 0.26780 | MONITOR |  |
| 02:40:00 | 0.08194 | 0.08180789793015647 | 0.16148 | MONITOR |  |
| 02:45:00 | 0.08180 | 0.08180488745554984 | -0.00597 | BREAK_PENDING | BREAK |
| 02:50:00 | 0.08160 | 0.08180187698094321 | -0.24679 | BREAK_PENDING | CONFIRM_EXIT_SIGNAL |
| 02:55:00 | 0.08176 | 0.0817988665063366 | -0.04751 | BREAK_PENDING | NEUTRAL_ROLL |

## 5. Nach dem Exit (kontrafaktisch / Beobachtung)

- Erster Close **auf/über** Linie: `2026-08-31 03:00:00 UTC`
- Minuten mit Close unter Linie (ab Exit-Bar): `5`
- Linie bei Reclaim noch gültig: `True`

| Fenster nach Exit | High | Low | MFE vs Entry % | MAE vs Entry % |
|---|---:|---:|---:|---:|
| +1 h | 0.08254 | 0.08154 | 0.2551 | -0.9596 |
| +3 h | 0.08302 | 0.08154 | 0.8381 | -0.9596 |
| +6 h | 0.08302 | 0.08154 | 0.8381 | -0.9596 |
| +12 h | 0.08325 | 0.08154 | 1.1175 | -0.9596 |
| +24 h | 0.08396 | 0.08154 | 1.9798 | -0.9596 |

**Ohne Support-Exit (V3U-Regel ab Exit-Zeit):** erstes späteres Ereignis → `SL @ 2026-09-01 18:25:00 UTC price 0.08134204`
**V3U gesamt (ohne Support-Exit):** `SL` @ `2026-09-01 18:25:00 UTC` → `-1.2000 %`

## 6. Diagnosechart

Statisches PNG (keine Research-Chart-Änderung): `l1_v3aa_original_support_diagnostic.png`

## 7. Bewertung (Fakten vs. Interpretation)

### Fakten

| # | Frage | Ergebnis |
|---|---|---|
| 1 | Entry regelkonform (originale Linie)? | **Ja** — Touch `00:15`, C1-Close `00:20`, Entry-Open `00:25` @ 0.08233; Geometrie aktiv |
| 2 | Supportbruch korrekt? | **Ja** — 02:45-Close 0.08180 < Linie 0.08180489 (−0.0060 % unter Linie) |
| 3 | 0,20-%-Bestätigung korrekt? | **Ja** — 02:50-Close 0.08160 ≤ 0.08180×0.998 (−0.2445 %) |
| 4 | Exit −0.8867 % technisch korrekt? | **Ja** — Fill 02:55-Open 0.08160; `(0.0816−0.08233)/0.08233×100 = −0.8867` |
| 5 | Reclaim nach Exit? | **Ja** — erster Close ≥ Linie: **03:00 UTC** (5 min nach Fill); Linie noch gültig |
| 6 | Ohne Support-Exit: TP oder SL zuerst? | **SL zuerst** — V3U-Sim: SL `2026-09-01 18:25`; in 24 h nach Exit kein TP (High 0.08396 < 0.085936) |
| 7 | Falsche Supportlinie? | **Nein** — überwachte ID = Entry-`d-9b6e62dc884c` aus Snapshot SHA oben |
| 8 | Backtester = Regel? | **Ja** — `simulate_exit_v3y` + FSM-Replay = dokumentierter Exit |

### Interpretation (keine Normänderung)

- **Haarlinien-Bruch** bei 02:45 (−0.006 % unter Linie) ist visuell fragwürdig, aber **regelkonform** (Close strikt unter `price_at` Kerzenende).
- **Fehlausbruch-Hinweis:** Reclaim bereits **03:00**; in 24 h post-Exit MFE bis **+1.98 %** vs. Entry, aber **kein** Erreichen des ursprünglichen TP.
- Support-Exit **−0.8867 %** vs. hypothetisch **−1.20 %** SL (V3U) = weniger Verlust, trotzdem realisierter Shortfall gegenüber Entry.
- Zwischen Entry und finalem Bruch gab es bereits **mehrere** vorübergehende Bruch-/Reclaim-Episoden (01:05–02:05) ohne Exit — erst die Episode 02:45/02:50 erfüllte −0,20 %-Bestätigung.

Payload: `l1_v3aa_audit_payload.json`

**STOP** — keine Strategie-/Zeichnungsänderung.