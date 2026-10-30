# LONG V3 Phase 2K — Dynamic Buy Limit (Variant B)

**Verdict:** `PHASE2K_DYNAMIC_MEASURABLE_DELTA`

## 1. Technische Umsetzung Variante B

`buy_limit_semantics=DYNAMIC_UPPER_GROWTH_V2K`: Nach `_try_fill_pending` und Pending-Validierung prüft `try_dynamic_upper_growth_reprice` dieselbe Lineage; bei strikt höherer Oberkante → Re-Qualifikation (Formation/Gap/EMA), LONG-A-TP ≥0,5 %, Risiko; dann CANCEL+PLACE+REPLACE mit neuem Limit an `P.upper_edge`, earliest next open. Oberkante unverändert / nur Lower-Wachstum: kein Repricing. Limit ≥ Markt: fail-closed Audit.

## 2. Variante A unverändert

Default `FROZEN_AT_PLACE_V2J` — identischer Codepfad wie Phase 2J; Union-Drift nur `PENDING_FROZEN_PRICES_HELD`. Keine Änderung an Touch/Recovery/PLACE für A.

## 3. Historische Fenster

- **primary_16_17**: A fills=2, B fills=3, repricing B=1
- **control_20_21**: A fills=1, B fills=1, repricing B=0
- **independent_18_19**: A fills=4, B fills=4, repricing B=1

## 4. Order-Repricing-Timelines (Variante B)

```json
{
  "primary_16_17": [
    {
      "bar_utc": "2026-09-17T02:10:00Z",
      "lineage_id": "lin_v2h_551",
      "old_limit": 0.08051,
      "new_limit": 0.08064,
      "entry_change_pct": 0.16147062,
      "risk_pct": 0.9424603174603134,
      "tp_gap_pct": 0.65724206,
      "old_order_id": "0791ff42-7bfa-46b1-ba54-815601f9e405",
      "new_order_id": "006b403e-1fa0-441d-b56e-a151648cf0b6"
    }
  ],
  "control_20_21": [],
  "independent_18_19": [
    {
      "bar_utc": "2026-09-19T16:05:00Z",
      "lineage_id": "lin_v2h_646",
      "old_limit": 0.08842,
      "new_limit": 0.08856,
      "entry_change_pct": 0.15833522,
      "risk_pct": 1.0388437217705557,
      "tp_gap_pct": 1.43405601,
      "old_order_id": "2f1fa937-6190-4fc3-b310-0a2ad1ecc244",
      "new_order_id": "4d3c9b8d-124e-4421-9a38-092cda481a78"
    }
  ]
}
```

## 5. Zusätzliche / verpasste Fills

{
  "primary_16_17": {
    "only_in_A": [],
    "only_in_B": [
      "2026-09-17T02:15:00Z|0.08064"
    ],
    "shared": [
      "2026-09-16T21:35:00Z|0.08028",
      "2026-09-17T16:30:00Z|0.08168"
    ]
  },
  "control_20_21": {
    "only_in_A": [],
    "only_in_B": [],
    "shared": [
      "2026-09-21T13:30:00Z|0.09307"
    ]
  },
  "independent_18_19": {
    "only_in_A": [],
    "only_in_B": [],
    "shared": [
      "2026-09-18T20:25:00Z|0.0877",
      "2026-09-18T23:25:00Z|0.0877",
      "2026-09-19T06:20:00Z|0.08717",
      "2026-09-19T20:10:00Z|0.08911"
    ]
  }
}

## 6. Risiko- und TP-Abstände

Pro Trade: `risk_pct` und `tp_gap_pct` aus Runner-Feldern (LONG A TP an Fill). Repricing: `entry_change_pct` / `tp_gap_pct` in `repricing_audits`.

## 7. Regressionstests

Long pytest rc=0. Short rc=0 (June D107 weiter BLOCKED wenn Bundle fehlt).

## 8. Empfehlung

Primary 16–17: A 2 Fills / 1.044717 % Brutto-Summe; B 3 Fills / 1.701959 %; +1 nur in B, −0 nur in A. Keine Profitabilitätsaussage — Forschungsbaseline bleibt A (NEXT_BAR frozen), B nur wenn Nachziehen kausal mehr gültige Setups liefert.

## Metriken je Fenster

```json
{
  "primary_16_17": {
    "VARIANT_A_FROZEN": {
      "fills": 2,
      "places": 4,
      "tp_exits": 2,
      "sl_exits": 0,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 2,
      "replaces": 1,
      "sum_gross_return_pct": 1.044717,
      "sum_wins_pct": 1.044717,
      "sum_losses_pct": 0.0,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J",
      "touch_event_counts": {
        "TOUCH_BEGIN": 6,
        "RECOVERY_CONFIRMED": 4,
        "TOUCH_ABORT_INVALID": 2
      }
    },
    "VARIANT_B_DYNAMIC": {
      "fills": 3,
      "places": 5,
      "tp_exits": 3,
      "sl_exits": 0,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 2,
      "replaces": 2,
      "sum_gross_return_pct": 1.701959,
      "sum_wins_pct": 1.701959,
      "sum_losses_pct": 0.0,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J|buy=DYNAMIC_UPPER_GROWTH_V2K",
      "touch_event_counts": {
        "TOUCH_BEGIN": 6,
        "RECOVERY_CONFIRMED": 4,
        "TOUCH_ABORT_INVALID": 2
      }
    },
    "fill_diff": {
      "only_in_A": [],
      "only_in_B": [
        "2026-09-17T02:15:00Z|0.08064"
      ],
      "shared": [
        "2026-09-16T21:35:00Z|0.08028",
        "2026-09-17T16:30:00Z|0.08168"
      ]
    },
    "repricing_count_B": 1
  },
  "control_20_21": {
    "VARIANT_A_FROZEN": {
      "fills": 1,
      "places": 1,
      "tp_exits": 1,
      "sl_exits": 0,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 0,
      "replaces": 0,
      "sum_gross_return_pct": 1.418287,
      "sum_wins_pct": 1.418287,
      "sum_losses_pct": 0.0,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J",
      "touch_event_counts": {
        "TOUCH_BEGIN": 3,
        "TOUCH_ABORT_INVALID": 1,
        "RECOVERY_CONFIRMED": 1,
        "TOUCH_ABORT_BREAK": 1
      }
    },
    "VARIANT_B_DYNAMIC": {
      "fills": 1,
      "places": 1,
      "tp_exits": 1,
      "sl_exits": 0,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 0,
      "replaces": 0,
      "sum_gross_return_pct": 1.418287,
      "sum_wins_pct": 1.418287,
      "sum_losses_pct": 0.0,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J|buy=DYNAMIC_UPPER_GROWTH_V2K",
      "touch_event_counts": {
        "TOUCH_BEGIN": 3,
        "TOUCH_ABORT_INVALID": 1,
        "RECOVERY_CONFIRMED": 1,
        "TOUCH_ABORT_BREAK": 1
      }
    },
    "fill_diff": {
      "only_in_A": [],
      "only_in_B": [],
      "shared": [
        "2026-09-21T13:30:00Z|0.09307"
      ]
    },
    "repricing_count_B": 0
  },
  "independent_18_19": {
    "VARIANT_A_FROZEN": {
      "fills": 4,
      "places": 5,
      "tp_exits": 3,
      "sl_exits": 1,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 1,
      "replaces": 3,
      "sum_gross_return_pct": 1.557101,
      "sum_wins_pct": 2.836419,
      "sum_losses_pct": -1.279318,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J",
      "touch_event_counts": {
        "TOUCH_BEGIN": 5,
        "RECOVERY_CONFIRMED": 5
      }
    },
    "VARIANT_B_DYNAMIC": {
      "fills": 4,
      "places": 6,
      "tp_exits": 3,
      "sl_exits": 1,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 2,
      "replaces": 4,
      "sum_gross_return_pct": 1.557101,
      "sum_wins_pct": 2.836419,
      "sum_losses_pct": -1.279318,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J|buy=DYNAMIC_UPPER_GROWTH_V2K",
      "touch_event_counts": {
        "TOUCH_BEGIN": 5,
        "RECOVERY_CONFIRMED": 5
      }
    },
    "fill_diff": {
      "only_in_A": [],
      "only_in_B": [],
      "shared": [
        "2026-09-18T20:25:00Z|0.0877",
        "2026-09-18T23:25:00Z|0.0877",
        "2026-09-19T06:20:00Z|0.08717",
        "2026-09-19T20:10:00Z|0.08911"
      ]
    },
    "repricing_count_B": 1
  }
}
```

---
STOP — keine produktive Aktivierung.