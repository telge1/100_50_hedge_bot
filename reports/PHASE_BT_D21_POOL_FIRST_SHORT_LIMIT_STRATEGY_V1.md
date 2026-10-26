# PHASE BT-D21 — Pool-First Short Limit Order Strategy

**Datum:** 2026-10-25  
**Projekt:** `pools+ob+delta_bot`  
**Verdict:** `POOL_FIRST_SHORT_READY`

---

## Zusammenfassung

Neuer, **explizit wählbarer** Signalmodus `pool_first_structural_v1` implementiert. Der bisherige Modus `nearest_upper_proximity_v1` (bearish + 0,05 % Proximity) bleibt **Default** in `configs/backtester.yaml` und `bt_d9_24h_v1.json` — **keine stillschweigende Migration**.

| Komponente | Pfad |
|------------|------|
| Pool-First-Logik | `pool_first_signal_v1.py` |
| Signal-Dispatch | `signal_engine_v1.detect_short_signal` |
| Pool-Cancel-Identität | `pool_identity_v1.py` (`exact_band_v1` \| `origin_set_v1`) |
| Pilot-YAML | `configs/pilot_bt_d21_pool_first_24h.yaml` |
| Historischer Vergleich | `tools/phase_bt_d21_pool_first_short_limit_strategy_v1/run_historical_comparison.py` |
| Ergebnis | `results/phase_bt_d21_pool_first_short_limit_strategy_v1/historical_comparison.json` |

---

## YAML-Aktivierung (Pool-First)

```yaml
short_entry:
  signal_selection_mode: pool_first_structural_v1   # → entry_pool_selection_mode
  entry_price_mode: pool_lower_edge | pool_lower_edge_offset
  entry_offset_pct: null | <nonnegative>
  pending_limit_fill_model: causal_sell_limit_v2
  pool_cancel_identity_mode: origin_set_v1          # empfohlen für Pool-First
```

**Baseline** (unverändert): `entry_pool_selection_mode: nearest_upper_proximity_v1`, kein `pool_cancel_identity_mode` → `exact_band_v1`.

---

## Strukturelle Entry-Pool-Bedingungen (Pool-First)

Alle müssen am **Decision-Close** erfüllt sein (`pool_first_signal_v1._structural_eligible_upper`):

| Regel | Implementierung |
|-------|-----------------|
| Upper-Pool **über** Preis | `pool_lower > close` (5m-Close der Signalkerze) |
| Upper-Gap | `has_upper_large_gap_above_pool` (≥ `upper_gap_min_pct`, Union-Gaps) |
| Pool-Alter | `pool_min_age_bars` (aktuell **0** in Pilot = Gate aus) |
| Gültiger TP | `select_short_tp_depth2(entry, intervals, min_distance_pct)` ≠ null |
| SL-Plausibilität | `entry_level < short_sl(pool_upper, sl_pct)` |
| Depth | Bänder aus Density (≥2 Origins im Provider/Cache) |

**Nicht verwendet:** `close < open`, Proximity 0,05 %, Touch/Rejection.

**Pool-Auswahl:** unter allen eligible Bands mit `pool_lower > close` → **kleinstes `pool_lower`** (nächster Upper über dem Preis).

**Kein Doppel-Signal:** `pool_signal_hold` blockiert erneute Signale für dieselbe `pool_key`, bis `pool_cancel_identity_mode` „inactive“ meldet.

---

## Entry-Preis

Wie BT-D17: `upper_pool_lower_edge` (+ optional `entry_offset_pct`).  
`pool_upper_edge` mit Pool-First → **Validator FAIL**.

---

## Execution & Pending

- Sell-Limit, aktiv ab **nächstem 5m-Open** nach Signal-Close (unverändert).
- Fill: `causal_sell_limit_v2` (wenn in Config gesetzt).
- **Fill vor Cancel** auf aktiver Bar (BT-D18).
- **Cancel:** `pool_still_active_for_pending` — Baseline `exact_band_v1`; Pool-First-Pilot `origin_set_v1` (Signal-`origin_ids` ⊆ irgendein Upper-Band).

---

## Tests

```bash
pytest tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests/test_pool_first_signal_v1.py \
       tools/phase_bt_d7_config_driven_short_pnl_integration_v1/tests/test_bt_d7_integration_v1.py
```

**30+ passed** inkl. Pool-First ohne bearish, Fill nach Aktivierung, Origin-Set nach Split, keine Duplikat-Signale, Baseline 0 Signale auf Pool-First-Fixture.

---

## Historischer 24h-Vergleich (BT-D9-Cache, kein `latest_result`)

| | Baseline (proximity+bearish) | Pool-First |
|--|------------------------------|------------|
| Signale | 3 | 7 |
| Fills | 0 | 3 |
| Trades | 0 | 1 |
| Netto USDT | 0 | ≈ −3.22 |
| Erstes Signal | 12:00 | 12:00 |
| Erster Fill | — | 12:25 @ 0.08384 |

**Hinweis:** Pilot erzeugt **mehr** frühe Pending-Orders; PnL des einen abgeschlossenen Trades ist **nicht** Optimierung — nur Funktionsnachweis.

---

## Abgrenzung

- Keine Änderung an LMV2 Short V2, Density/SSOT, Dashboard.
- `pending_limit_expiry_bars` weiterhin **nicht** in Engine enforced.
- Long: blockiert.

---

## STOP

Keine Parameteroptimierung, kein `publish_latest` in dieser Phase.
