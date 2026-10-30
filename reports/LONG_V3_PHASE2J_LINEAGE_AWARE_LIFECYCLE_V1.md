# LONG V3 Phase 2J — Lineage-Aware Lifecycle

**Verdict:** `PHASE2J_LIFECYCLE_PASS`

## 1. Root-Cause unnötiger Touch-Abbrüche (2I)

Phase-2I-Referenz (`UNION_KEY_V2I`) bindet Touch-, Recovery- und Pending-Validierung an exakte Union-Keys. Wächst eine Region kausal weiter (`lin_v2h_*`), fehlt der alte Key in `snap_now` → `TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE` (0×) oder `TOUCH_ABORT_INVALID` / `REGION_NOT_FOUND` (4×) obwohl Lineage und Origins fortbestehen.

## 2. Lineage-aware State Machine

IDLE → TOUCH_BEGIN (Support + `lineage_id` + Touch-Oberkante eingefroren) → TOUCHED → Recovery (Re-Qualifikation über Lineage, Materialize aktuelles P/Q) → RECOVERED → PLACE (Entry/SL eingefroren, `frozen_support_key` dokumentiert) → Pending (Lineage-Continuity, kein Union-Key-Match nötig; Replace nur bei anderer Lineage). Fail-closed bei `LINEAGE_MATCH_AMBIGUOUS`, Origin-Verlust oder Deaktivierung.

## 3. Wachstum / Schrumpfung

Wachstum: Touch an alter Oberkante; aktive Lineage am Bar-Close; Recovery mit neuem Union; Pending behält `planned_entry`/`planned_sl`. Schrumpfung: solange qualifiziert und Origin-Kontinuität + Break-Test (Close < Touch-Lower) ok. Höhe <0,1 % oder Below-Streak <5 → Re-Qualifikation scheitert → Abort/Cancel.

## 4. Eingefrorene Order- und SL-Preise

PLACE setzt `planned_entry`/`planned_sl` und `frozen_support_key`; späteres P-Wachstum erzeugt STRUCTURE_AUDIT `PENDING_FROZEN_PRICES_HELD` ohne Repricing. REPLACE nur explizit und nur bei neuer Lineage (2J). Fill vor Cancel durch `_try_fill_pending` am Bar-Anfang.

## 5. Vergleich Phase 2I vs 2J (Primary 16–17 Sep, LONG A)

- **PHASE2I_REF__NEXT_BAR**: fills=1, places=4, cancels=3, replaces=0, sum_gross_return_pct=0.776164
- **PHASE2J_LIFECYCLE__NEXT_BAR**: fills=2, places=4, cancels=2, replaces=1, sum_gross_return_pct=1.044717
- **PHASE2I_REF__SAME_BAR**: fills=3, places=7, cancels=4, replaces=0, sum_gross_return_pct=2.270851
- **PHASE2J_LIFECYCLE__SAME_BAR**: fills=4, places=6, cancels=2, replaces=1, sum_gross_return_pct=2.792399

## 6. Fill-Timelines (alle Varianten, LONG A + B)

```json
{
  "LONG_A": {
    "PHASE2I_REF__NEXT_BAR": [
      {
        "signal_close_utc": "2026-09-16T22:55:00Z",
        "fill_bar_utc": "2026-09-16T23:00:00Z",
        "entry_price": 0.07988,
        "sl_price": 0.07901,
        "risk_pct": 1.0891337,
        "exit_bar_utc": "2026-09-16T23:20:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.776164,
        "pool_key": "0.07978|0.07988"
      }
    ],
    "PHASE2J_LIFECYCLE__NEXT_BAR": [
      {
        "signal_close_utc": "2026-09-16T20:45:00Z",
        "fill_bar_utc": "2026-09-16T21:35:00Z",
        "entry_price": 0.08028,
        "sl_price": 0.07901,
        "risk_pct": 1.58196313,
        "exit_bar_utc": "2026-09-16T23:30:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.523169,
        "pool_key": "0.08002200000000001|0.08028"
      },
      {
        "signal_close_utc": "2026-09-17T16:15:00Z",
        "fill_bar_utc": "2026-09-17T16:30:00Z",
        "entry_price": 0.08168,
        "sl_price": 0.08064,
        "risk_pct": 1.27326151,
        "exit_bar_utc": "2026-09-17T17:00:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.521548,
        "pool_key": "0.081556|0.08168"
      }
    ],
    "PHASE2I_REF__SAME_BAR": [
      {
        "signal_close_utc": "2026-09-16T21:35:00Z",
        "fill_bar_utc": "2026-09-16T21:40:00Z",
        "entry_price": 0.08028,
        "sl_price": 0.07901,
        "risk_pct": 1.58196313,
        "exit_bar_utc": "2026-09-16T23:30:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.523169,
        "pool_key": "0.08002200000000001|0.08028"
      },
      {
        "signal_close_utc": "2026-09-17T01:00:00Z",
        "fill_bar_utc": "2026-09-17T01:05:00Z",
        "entry_price": 0.08051,
        "sl_price": 0.07988,
        "risk_pct": 0.78251149,
        "exit_bar_utc": "2026-09-17T01:25:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.658303,
        "pool_key": "0.080422|0.08051"
      },
      {
        "signal_close_utc": "2026-09-17T10:15:00Z",
        "fill_bar_utc": "2026-09-17T10:20:00Z",
        "entry_price": 0.08078,
        "sl_price": 0.07988,
        "risk_pct": 1.11413716,
        "exit_bar_utc": "2026-09-17T12:30:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 1.089379,
        "pool_key": "0.08037399999999999|0.08078"
      }
    ],
    "PHASE2J_LIFECYCLE__SAME_BAR": [
      {
        "signal_close_utc": "2026-09-16T20:40:00Z",
        "fill_bar_utc": "2026-09-16T21:35:00Z",
        "entry_price": 0.08028,
        "sl_price": 0.07901,
        "risk_pct": 1.58196313,
        "exit_bar_utc": "2026-09-16T23:30:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.523169,
        "pool_key": "0.08002200000000001|0.08028"
      },
      {
        "signal_close_utc": "2026-09-17T01:00:00Z",
        "fill_bar_utc": "2026-09-17T01:05:00Z",
        "entry_price": 0.08051,
        "sl_price": 0.07988,
        "risk_pct": 0.78251149,
        "exit_bar_utc": "2026-09-17T01:25:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.658303,
        "pool_key": "0.080422|0.08051"
      },
      {
        "signal_close_utc": "2026-09-17T10:15:00Z",
        "fill_bar_utc": "2026-09-17T10:20:00Z",
        "entry_price": 0.08078,
        "sl_price": 0.07988,
        "risk_pct": 1.11413716,
        "exit_bar_utc": "2026-09-17T12:30:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 1.089379,
        "pool_key": "0.08037399999999999|0.08078"
      },
      {
        "signal_close_utc": "2026-09-17T16:15:00Z",
        "fill_bar_utc": "2026-09-17T16:30:00Z",
        "entry_price": 0.08168,
        "sl_price": 0.08064,
        "risk_pct": 1.27326151,
        "exit_bar_utc": "2026-09-17T17:00:00Z",
        "exit_reason": "FIXED_UPPER_TP",
        "gross_return_pct": 0.521548,
        "pool_key": "0.081556|0.08168"
      }
    ]
  },
  "LONG_B": {
    "PHASE2I_REF__NEXT_BAR": [],
    "PHASE2J_LIFECYCLE__NEXT_BAR": [],
    "PHASE2I_REF__SAME_BAR": [],
    "PHASE2J_LIFECYCLE__SAME_BAR": []
  }
}
```

## 7. LONG A / B getrennt

```json
{
  "LONG_A": {
    "PHASE2I_REF__NEXT_BAR": {
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
    "PHASE2J_LIFECYCLE__NEXT_BAR": {
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
    "PHASE2I_REF__SAME_BAR": {
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
    },
    "PHASE2J_LIFECYCLE__SAME_BAR": {
      "fills": 4,
      "places": 6,
      "tp_exits": 4,
      "sl_exits": 0,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 2,
      "replaces": 1,
      "sum_gross_return_pct": 2.792399,
      "sum_wins_pct": 2.792399,
      "sum_losses_pct": 0.0,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J",
      "touch_event_counts": {
        "TOUCH_BEGIN": 6,
        "RECOVERY_CONFIRMED": 6
      }
    }
  },
  "LONG_B": {
    "PHASE2I_REF__NEXT_BAR": {
      "fills": 0,
      "places": 2,
      "tp_exits": 0,
      "sl_exits": 0,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 1,
      "replaces": 0,
      "sum_gross_return_pct": 0.0,
      "sum_wins_pct": 0.0,
      "sum_losses_pct": 0.0,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT",
      "touch_event_counts": {
        "TOUCH_BEGIN": 3,
        "RECOVERY_CONFIRMED": 2,
        "TOUCH_ABORT_INVALID": 1
      }
    },
    "PHASE2J_LIFECYCLE__NEXT_BAR": {
      "fills": 0,
      "places": 1,
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
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J",
      "touch_event_counts": {
        "TOUCH_BEGIN": 1,
        "RECOVERY_CONFIRMED": 1
      }
    },
    "PHASE2I_REF__SAME_BAR": {
      "fills": 0,
      "places": 2,
      "tp_exits": 0,
      "sl_exits": 0,
      "ema_exits": 0,
      "ambiguous_exits": 0,
      "cancels": 1,
      "replaces": 0,
      "sum_gross_return_pct": 0.0,
      "sum_wins_pct": 0.0,
      "sum_losses_pct": 0.0,
      "fees_slippage": "not_modeled",
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT",
      "touch_event_counts": {
        "TOUCH_BEGIN": 2,
        "RECOVERY_CONFIRMED": 2
      }
    },
    "PHASE2J_LIFECYCLE__SAME_BAR": {
      "fills": 0,
      "places": 1,
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
      "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=SAME_BAR_TOUCH_CLOSE|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J",
      "touch_event_counts": {
        "TOUCH_BEGIN": 1,
        "RECOVERY_CONFIRMED": 1
      }
    }
  }
}
```

## 8. Risikodistanzen

Pro Fill: `risk_pct = (entry_fill - fixed_sl) / entry_fill * 100` (Runner-Feld). Gebühren/Slippage: not_modeled.

## 9. Tests

Long pytest: rc=0 — ..............................................                           [100%]
46 passed in 0.07s
Short pytest: rc=0 — June-D107 weiter BLOCKED wenn Bundle fehlt.

## 10. Empfehlung Baseline

Für Long-Forschungsbaseline: `CAUSAL_BELOW_PRICE_SEGMENT` + `LINEAGE_AWARE_V2J` + `NEXT_BAR` (LONG A), sofern Verdict PASS — SAME_BAR nur Vergleich. Keine produktive Aktivierung.

## Struktur-Audit (Auszug)

```json
[
  {
    "bar_utc": "2026-09-17T01:05:00Z",
    "phase": "RECOVERY",
    "old_union_key": "0.080422|0.08051",
    "new_union_key": "0.08037399999999999|0.08051",
    "lineage_id": "lin_v2h_551",
    "old_pool_height_pct": 0.10942279475765929,
    "new_pool_height_pct": 0.16920894816733165,
    "touch_phase": "TOUCHED",
    "pending_order_id": null,
    "decision": "CONTINUE",
    "reason": "LINEAGE_UNION_GROWTH_REQUALIFY"
  },
  {
    "bar_utc": "2026-09-17T02:10:00Z",
    "phase": "PENDING",
    "old_union_key": "0.08037399999999999|0.08051",
    "new_union_key": "0.08037399999999999|0.08064",
    "lineage_id": "lin_v2h_551",
    "old_pool_height_pct": null,
    "new_pool_height_pct": null,
    "touch_phase": null,
    "pending_order_id": "7e717609-0d9a-4200-802b-afcae4c67a51",
    "decision": "CONTINUE",
    "reason": "PENDING_FROZEN_PRICES_HELD",
    "planned_entry": 0.08051,
    "planned_sl": 0.07988
  },
  {
    "bar_utc": "2026-09-17T02:15:00Z",
    "phase": "PENDING",
    "old_union_key": "0.08037399999999999|0.08051",
    "new_union_key": "0.08037399999999999|0.08064",
    "lineage_id": "lin_v2h_551",
    "old_pool_height_pct": null,
    "new_pool_height_pct": null,
    "touch_phase": null,
    "pending_order_id": "7e717609-0d9a-4200-802b-afcae4c67a51",
    "decision": "CONTINUE",
    "reason": "PENDING_FROZEN_PRICES_HELD",
    "planned_entry": 0.08051,
    "planned_sl": 0.07988
  },
  {
    "bar_utc": "2026-09-17T21:50:00Z",
    "phase": "PENDING",
    "old_union_key": "0.08119|0.08147",
    "new_union_key": "0.08119|0.08153",
    "lineage_id": "lin_v2h_579",
    "old_pool_height_pct": null,
    "new_pool_height_pct": null,
    "touch_phase": null,
    "pending_order_id": "57435cbd-37cc-4e04-b4ff-646178fb871c",
    "decision": "CONTINUE",
    "reason": "PENDING_FROZEN_PRICES_HELD",
    "planned_entry": 0.08147,
    "planned_sl": 0.08064
  },
  {
    "bar_utc": "2026-09-17T21:50:00Z",
    "phase": "PENDING",
    "old_union_key": "0.08119|0.08147",
    "new_union_key": "0.08119|0.08153",
    "lineage_id": "lin_v2h_579",
    "old_pool_height_pct": null,
    "new_pool_height_pct": null,
    "touch_phase": null,
    "pending_order_id": "600ebd14-fe13-45c5-84ec-c89007a6228d",
    "decision": "CONTINUE",
    "reason": "PENDING_FROZEN_PRICES_HELD",
    "planned_entry": 0.08147,
    "planned_sl": 0.08064
  }
]
```

---
STOP — keine produktive Aktivierung.