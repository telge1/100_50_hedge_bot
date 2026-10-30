# LONG V3 Phase 2I — Causal Lower Formation

**Verdict:** `PHASE2I_FORMATION_COMPLETE`

## 1. Warum scheitert Legacy-Lineage-Formation?

LEGACY_LINEAGE_START verlangt, dass die Union-Oberkante am **Lineage-Gesamtstart** (bar_index − lineage_age + 1) unter dem Close lag. Bei `lin_v2h_*` mit sehr langem Streak (hier 13–14+) liegt dieser Start weit vor dem 16.09.; war der Pool damals noch nicht unter Preis → `FORMATION_NOT_BELOW_PRICE` — obwohl die Region **aktuell** kontinuierlich unter dem Kurs liegt (Below-Price-Streak separat).

## 2. Below-Price-Segment kausal?

CAUSAL_BELOW_PRICE_SEGMENT: `region_lineage_age_bars ≥ 5` UND `below_price_streak_bars ≥ 5` (jeweils consecutive, pro Bar Oberkante < Close) UND `height ≥ 0,1 %` am Entscheidungs-Close — **ohne** Rückgriff auf den Lineage-Gesamtstart. Kein rückwirkender Kursbezug.

## 3–6. September-Ankerpool

{
  "recognized_growing_pool": true,
  "first_P_segment_ema": null,
  "compare_00_40_00_45": {
    "2026-09-16T00:40:00Z": {
      "utc": "2026-09-16T00:40:00Z",
      "close": 0.08008,
      "region_lower": 0.079758,
      "region_upper": 0.07997,
      "pool_height_pct": 0.26580406,
      "origin_ids": [
        "lldv2:DOGEUSDT:5m:lower:1787375700",
        "lldv2:DOGEUSDT:5m:lower:1789515300",
        "lldv2:DOGEUSDT:5m:lower:1789516500",
        "lldv2:DOGEUSDT:5m:lower:1789516800",
        "lldv2:DOGEUSDT:5m:lower:1789517700"
      ],
      "lineage_id": "lin_v2h_517",
      "region_lineage_age_bars": 13,
      "below_price_streak_bars": 13,
      "formation_A_legacy": true,
      "formation_A_reason": null,
      "formation_B_segment": true,
      "formation_B_reason": null,
      "P_qualified_segment_ema": false,
      "ema_ok": false,
      "ema_reason": "EMA9_ABOVE_200",
      "Q_region": "0.078038|0.07831",
      "gap_pct": 1.81549186,
      "entry_limit": 0.07997,
      "structural_sl": 0.07831,
      "risk_pct": 2.075778
    },
    "2026-09-16T00:45:00Z": {
      "utc": "2026-09-16T00:45:00Z",
      "close": 0.08013,
      "region_lower": 0.079758,
      "region_upper": 0.08003,
      "pool_height_pct": 0.34103162,
      "origin_ids": [
        "lldv2:DOGEUSDT:5m:lower:1787375700",
        "lldv2:DOGEUSDT:5m:lower:1789515300",
        "lldv2:DOGEUSDT:5m:lower:1789516500",
        "lldv2:DOGEUSDT:5m:lower:1789516800",
        "lldv2:DOGEUSDT:5m:lower:1789517700",
        "lldv2:DOGEUSDT:5m:lower:1789519200"
      ],
      "lineage_id": "lin_v2h_517",
      "region_lineage_age_bars": 14,
      "below_price_streak_bars": 14,
      "formation_A_legacy": true,
      "formation_A_reason": null,
      "formation_B_segment": true,
      "formation_B_reason": null,
      "P_qualified_segment_ema": false,
      "ema_ok": false,
      "ema_reason": "EMA9_ABOVE_200",
      "Q_region": "0.078038|0.07831",
      "gap_pct": 1.81549186,
      "entry_limit": 0.08003,
      "structural_sl": 0.07831,
      "risk_pct": 2.149194
    }
  }
}

## 7. Varianten mit Fills (Primary 16–17 Sep)

- **EXACT_ATOMIC_5__NEXT_BAR**: fills=0, places=0, sum_gross_return_pct=0.0
- **EXACT_ATOMIC_5__SAME_BAR**: fills=2, places=3, sum_gross_return_pct=1.181472
- **LINEAGE__LEGACY_FORMATION__NEXT_BAR**: fills=0, places=0, sum_gross_return_pct=0.0
- **LINEAGE__LEGACY_FORMATION__SAME_BAR**: fills=0, places=0, sum_gross_return_pct=0.0
- **LINEAGE__BELOW_PRICE_SEGMENT__NEXT_BAR**: fills=1, places=4, sum_gross_return_pct=0.776164
- **LINEAGE__BELOW_PRICE_SEGMENT__SAME_BAR**: fills=3, places=7, sum_gross_return_pct=2.270851

## 8. NEXT_BAR vs SAME_BAR

NEXT_BAR: Recovery/Validierung auf **folgendem** Close, PLACE earliest next open. SAME_BAR: Recovery am Touch-Close möglich, Limit earliest ab nächster 5m — kein Fill auf Touch-Kerze (causal buy limit v2).

## 9. Kontrollfenster 20–21 Sep

{
  "EXACT_ATOMIC_5__NEXT_BAR": {
    "fills": 0,
    "places": 0,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 0,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=EXACT_ATOMIC_5|form=EXACT_ATOMIC_DEFAULT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 6,
      "TOUCH_ABORT_INVALID": 4,
      "TOUCH_ABORT_BREAK": 2
    }
  },
  "EXACT_ATOMIC_5__SAME_BAR": {
    "fills": 0,
    "places": 4,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 3,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=EXACT_ATOMIC_5|form=EXACT_ATOMIC_DEFAULT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 6,
      "RECOVERY_CONFIRMED": 4,
      "TOUCH_ABORT_BREAK": 1
    }
  },
  "LINEAGE__LEGACY_FORMATION__NEXT_BAR": {
    "fills": 0,
    "places": 0,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 0,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=LEGACY_LINEAGE_START",
    "touch_event_counts": {}
  },
  "LINEAGE__LEGACY_FORMATION__SAME_BAR": {
    "fills": 0,
    "places": 0,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 0,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=CAUSAL_REGION_LINEAGE_5|form=LEGACY_LINEAGE_START",
    "touch_event_counts": {}
  },
  "LINEAGE__BELOW_PRICE_SEGMENT__NEXT_BAR": {
    "fills": 0,
    "places": 0,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 0,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 4,
      "TOUCH_ABORT_INVALID": 3,
      "TOUCH_ABORT_BREAK": 1
    }
  },
  "LINEAGE__BELOW_PRICE_SEGMENT__SAME_BAR": {
    "fills": 0,
    "places": 4,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 3,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 4,
      "RECOVERY_CONFIRMED": 4
    }
  }
}

## 10. Tests

Long: pytest tools/long_v3_trend_continuation_v1/tests/ — Short: 15 pass, 1 BLOCKED (June bundle).

## Variant matrix (primary)

```json
{
  "EXACT_ATOMIC_5__NEXT_BAR": {
    "fills": 0,
    "places": 0,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 0,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=EXACT_ATOMIC_5|form=EXACT_ATOMIC_DEFAULT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 6,
      "TOUCH_ABORT_INVALID": 5,
      "RESET_CONTEXT_LOST": 1
    }
  },
  "EXACT_ATOMIC_5__SAME_BAR": {
    "fills": 2,
    "places": 3,
    "tp_exits": 2,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 1,
    "replaces": 0,
    "sum_gross_return_pct": 1.181472,
    "sum_wins_pct": 1.181472,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=EXACT_ATOMIC_5|form=EXACT_ATOMIC_DEFAULT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 5,
      "RECOVERY_CONFIRMED": 3,
      "RESET_CONTEXT_LOST": 1,
      "TOUCH_ABORT_BREAK": 1
    }
  },
  "LINEAGE__LEGACY_FORMATION__NEXT_BAR": {
    "fills": 0,
    "places": 0,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 0,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=LEGACY_LINEAGE_START",
    "touch_event_counts": {}
  },
  "LINEAGE__LEGACY_FORMATION__SAME_BAR": {
    "fills": 0,
    "places": 0,
    "tp_exits": 0,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 0,
    "replaces": 0,
    "sum_gross_return_pct": 0.0,
    "sum_wins_pct": 0.0,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=CAUSAL_REGION_LINEAGE_5|form=LEGACY_LINEAGE_START",
    "touch_event_counts": {}
  },
  "LINEAGE__BELOW_PRICE_SEGMENT__NEXT_BAR": {
    "fills": 1,
    "places": 4,
    "tp_exits": 1,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 3,
    "replaces": 0,
    "sum_gross_return_pct": 0.776164,
    "sum_wins_pct": 0.776164,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 8,
      "RECOVERY_CONFIRMED": 4,
      "TOUCH_ABORT_INVALID": 4
    }
  },
  "LINEAGE__BELOW_PRICE_SEGMENT__SAME_BAR": {
    "fills": 3,
    "places": 7,
    "tp_exits": 3,
    "sl_exits": 0,
    "ema_exits": 0,
    "ambiguous_exits": 0,
    "cancels": 4,
    "replaces": 0,
    "sum_gross_return_pct": 2.270851,
    "sum_wins_pct": 2.270851,
    "sum_losses_pct": 0.0,
    "fees_slippage": "not_modeled",
    "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT",
    "touch_event_counts": {
      "TOUCH_BEGIN": 7,
      "RECOVERY_CONFIRMED": 7
    }
  }
}
```

## Timeline 00:00–08:00 (Auszug 00:35–00:50)

```json
[
  {
    "utc": "2026-09-16T00:30:00Z",
    "close": 0.08004,
    "region_lower": 0.079758,
    "region_upper": 0.07997,
    "pool_height_pct": 0.26580406,
    "origin_ids": [
      "lldv2:DOGEUSDT:5m:lower:1787375700",
      "lldv2:DOGEUSDT:5m:lower:1789515300",
      "lldv2:DOGEUSDT:5m:lower:1789516500",
      "lldv2:DOGEUSDT:5m:lower:1789516800",
      "lldv2:DOGEUSDT:5m:lower:1789517700"
    ],
    "lineage_id": "lin_v2h_517",
    "region_lineage_age_bars": 11,
    "below_price_streak_bars": 11,
    "formation_A_legacy": true,
    "formation_A_reason": null,
    "formation_B_segment": true,
    "formation_B_reason": null,
    "P_qualified_segment_ema": false,
    "ema_ok": false,
    "ema_reason": "EMA9_ABOVE_200",
    "Q_region": "0.078038|0.07831",
    "gap_pct": 1.81549186,
    "entry_limit": 0.07997,
    "structural_sl": 0.07831,
    "risk_pct": 2.075778
  },
  {
    "utc": "2026-09-16T00:35:00Z",
    "close": 0.0802,
    "region_lower": 0.079758,
    "region_upper": 0.07997,
    "pool_height_pct": 0.26580406,
    "origin_ids": [
      "lldv2:DOGEUSDT:5m:lower:1787375700",
      "lldv2:DOGEUSDT:5m:lower:1789515300",
      "lldv2:DOGEUSDT:5m:lower:1789516500",
      "lldv2:DOGEUSDT:5m:lower:1789516800",
      "lldv2:DOGEUSDT:5m:lower:1789517700"
    ],
    "lineage_id": "lin_v2h_517",
    "region_lineage_age_bars": 12,
    "below_price_streak_bars": 12,
    "formation_A_legacy": true,
    "formation_A_reason": null,
    "formation_B_segment": true,
    "formation_B_reason": null,
    "P_qualified_segment_ema": false,
    "ema_ok": false,
    "ema_reason": "EMA9_ABOVE_200",
    "Q_region": "0.078038|0.07831",
    "gap_pct": 1.81549186,
    "entry_limit": 0.07997,
    "structural_sl": 0.07831,
    "risk_pct": 2.075778
  },
  {
    "utc": "2026-09-16T00:40:00Z",
    "close": 0.08008,
    "region_lower": 0.079758,
    "region_upper": 0.07997,
    "pool_height_pct": 0.26580406,
    "origin_ids": [
      "lldv2:DOGEUSDT:5m:lower:1787375700",
      "lldv2:DOGEUSDT:5m:lower:1789515300",
      "lldv2:DOGEUSDT:5m:lower:1789516500",
      "lldv2:DOGEUSDT:5m:lower:1789516800",
      "lldv2:DOGEUSDT:5m:lower:1789517700"
    ],
    "lineage_id": "lin_v2h_517",
    "region_lineage_age_bars": 13,
    "below_price_streak_bars": 13,
    "formation_A_legacy": true,
    "formation_A_reason": null,
    "formation_B_segment": true,
    "formation_B_reason": null,
    "P_qualified_segment_ema": false,
    "ema_ok": false,
    "ema_reason": "EMA9_ABOVE_200",
    "Q_region": "0.078038|0.07831",
    "gap_pct": 1.81549186,
    "entry_limit": 0.07997,
    "structural_sl": 0.07831,
    "risk_pct": 2.075778
  },
  {
    "utc": "2026-09-16T00:45:00Z",
    "close": 0.08013,
    "region_lower": 0.079758,
    "region_upper": 0.08003,
    "pool_height_pct": 0.34103162,
    "origin_ids": [
      "lldv2:DOGEUSDT:5m:lower:1787375700",
      "lldv2:DOGEUSDT:5m:lower:1789515300",
      "lldv2:DOGEUSDT:5m:lower:1789516500",
      "lldv2:DOGEUSDT:5m:lower:1789516800",
      "lldv2:DOGEUSDT:5m:lower:1789517700",
      "lldv2:DOGEUSDT:5m:lower:1789519200"
    ],
    "lineage_id": "lin_v2h_517",
    "region_lineage_age_bars": 14,
    "below_price_streak_bars": 14,
    "formation_A_legacy": true,
    "formation_A_reason": null,
    "formation_B_segment": true,
    "formation_B_reason": null,
    "P_qualified_segment_ema": false,
    "ema_ok": false,
    "ema_reason": "EMA9_ABOVE_200",
    "Q_region": "0.078038|0.07831",
    "gap_pct": 1.81549186,
    "entry_limit": 0.08003,
    "structural_sl": 0.07831,
    "risk_pct": 2.149194
  }
]
```

---
STOP — keine produktive Umstellung.