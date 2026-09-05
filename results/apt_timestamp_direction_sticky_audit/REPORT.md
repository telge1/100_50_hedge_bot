# APT Timestamp Direction Sticky Audit

## Primärentscheidung

**STALE_BULLISH_STATE_NEEDS_NEUTRAL_INVALIDATION**

Am Timestamp `2026-04-11T20:31:00Z` war die Ausgabe `BULLISH` nur wegen sticky `major_direction=+1`.
Der Scanner hatte kausal seit **20:15 UTC** bereits `protected_structure_state=bearish_internal_break` (mit `internal_bos_down`).
Das Protected Low `0.8714` war bis 20:30 **nicht** per Close gebrochen.
Ein bestätigter bearisher Major-Flip kam erst **ex post** (CHOCH 20:35, major −1 ab 20:45).

Korrekte kausale Hauptausgabe: **UNCLEAR**.

---

## 1. Read-Path

| Rolle | Datei / Funktion |
|---|---|
| CLI | `scripts/query_trend_direction_at.py` |
| Core | `research/regime_scanner/trend_direction_at.py::query_trend_direction_at` |
| Scanner | `market_structure_c3_4b.apply_protected_structure` (`protected_medium`) |
| Eventquelle | Spalten `choch_side`, `external_bos_*`, `internal_bos_*`, `protected_structure_state` |
| State-Reducer | C3.4B sticky `major_direction` bis CHOCH-Hold; States wie `bearish_internal_break` parallel |
| Direction-Mapping (vorher) | nur `map_major_to_direction(major)` |
| Direction-Mapping (Fix) | `map_structure_to_direction(major, protected_structure_state)` |
| Invalidierungsregel (Scanner) | Externer Flip erst nach Protected-Level-Close-BOS + CHOCH-Hold; intern: `bearish_internal_break` |

Feldnamen (real, kein `pivot_high`/`internal_structure_direction`):
`protected_high`, `protected_low`, `micro_swing_high`, `micro_swing_low`, `major_direction`, `protected_structure_state`.

---

## 2–4. Kausaler Befund bei 20:30

| Feld | Wert |
|---|---|
| last candle | `[20:25, 20:30]` close `0.8736` |
| major_direction | `+1` |
| protected_structure_state | `bearish_internal_break` (seit 20:15) |
| protected_low | `0.8714` (intakt) |
| close_break_protected_down | false |
| internal_bos_down | true |
| bullish confirm seit | 18:45 (`bullish_structure` / CHOCH-up hold) |
| lokales Hoch | ~19:30–20:00 (close peak ~0.8790) |

**Zustandsklasse:** nicht mehr klar bullish, noch nicht bearish bestätigt → **UNCLEAR**.

Markierte Timeline (Ausschnitt):

| close_utc | state | old→new |
|---|---|---|
| 18:45 | bullish_structure | BULLISH→BULLISH |
| 20:15 | bearish_internal_break | BULLISH→**UNCLEAR** |
| 20:30 | bearish_internal_break | BULLISH→**UNCLEAR** |
| 20:35 EX_POST | bearish_choch | BULLISH→UNCLEAR |
| 20:45 EX_POST | bearish_structure | BEARISH→BEARISH |

## 5. Sticky-Mechanismus

Der Runner las faktisch `direction = f(last_relevant_major)` und ignorierte den aktuellen `protected_structure_state`.
Dadurch blieb `BULLISH` aktiv, obwohl C3.4B bereits einen internen Gegenbruch signalisierte.
Kein Alters-/Staleness-Timeout; kein Mapping von Internal-Break auf Neutral.

## 6. EX_POST_ONLY

| Event | Zeit | Lag vs 20:31 | Preisänderung vs 20:30 |
|---|---|---:|---:|
| first_external_bos_down / choch down | 20:35 | +4m | ~−0.50% |
| first_major_bearish | 20:45 | +14m | ~−0.37% |

## 7–8. Mapping-Fix & Vergleich

Neu: bei Konflikt major vs. opposite internal/CHOCH-pending → `UNCLEAR`; normale `*_pullback`/`*_structure` bleiben gerichtet.

Vergleichsfälle (APT Historie, je 10):

- bullish_normal_pullback_keep
- bullish_to_unclear_internal_break
- bullish_to_bearish_major_flip
- bearish_normal_pullback_keep
- bearish_to_unclear_internal_break
- bearish_to_bullish_major_flip

Anteil Bars mit Mapping-Änderung auf APT: ~18.7% (überwiegend Transition/CHOCH-Fenster).

## 9. Fix umgesetzt

Ja — nur Runner-Mapping in `trend_direction_at.py` (`map_structure_to_direction`). Keine Änderung an C3.4B.

Geänderte Dateien:

- `research/regime_scanner/trend_direction_at.py`
- `research/regime_scanner/tests/test_trend_direction_at.py`

## 10. Tests

`pytest research/regime_scanner/tests/test_trend_direction_at.py` — **20 passed** (inkl. Konflikt→UNCLEAR, Pullback-keep, HTF unabhängig, Determinismus).

## 11. CLI nach Fix

```text
symbol: APTUSDT
requested_at_utc: 2026-04-11T20:31:00Z
last_5m_open_utc: 2026-04-11T20:25:00Z
last_5m_close_utc: 2026-04-11T20:30:00Z
direction: UNCLEAR
direction_since_utc: 2026-04-11T18:45:00Z
source_timeframe: 5m
structure_event: bearish_internal_break
causality_pass: true
reason: MAJOR_CHALLENGED:bearish_internal_break
warmup_bars: 30486/72
```

## 12. Kurzantworten

1. Warum 20:31 BULLISH? Sticky `major_direction=+1` ohne State-Mapping.
2. Welches Event seit 18:45? Bullish CHOCH → `bullish_structure`, major +1.
3. Bullishe Struktur bis 20:30 intakt? Extern ja (PL 0.8714 nicht close-broken); intern nein (`bearish_internal_break`).
4. Kausaler bearish Bruch? Intern ja; externes bearish CHOCH/BOS erst EX_POST 20:35.
5. Korrekte Ausgabe: **UNCLEAR**.
6. Sticky-Ursache: `decide_from_structure` / altes `map_major_to_direction`.
7. Fix: ja, `map_structure_to_direction`.
8. Vergleichsfälle: je 10 in den 6 Kategorien oben.
9. Tests: `test_trend_direction_at.py` bestanden; CLI Smoke UNCLEAR.
10. CLI nach Fix: siehe Abschnitt 11.
