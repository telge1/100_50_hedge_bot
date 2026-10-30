# LONG V3 Phase 2M — Ledger & Extended A/B

**Status:** `VALIDATED_EVIDENCE`

## 1. Ledger 17.09.2026 16:30 A

Reconstruction: **RESOLVED**

```json
{
  "status": "RESOLVED",
  "fill_utc": "2026-09-17T16:30:00Z",
  "fill_order_id": "c607b3de-e1f6-4738-82ce-880c45c61315",
  "entry_price": 0.08168,
  "sl_price": 0.08064,
  "initial_place": {
    "bar_utc": "2026-09-17T15:30:00Z",
    "order_id": "23f8a357-2746-4b71-9ba2-90b8739884ae",
    "limit": 0.08113,
    "sl": 0.07988,
    "lineage_id": "lin_v2h_572",
    "frozen_support_key": "0.080904|0.08113",
    "selection_reason": null
  },
  "replace_chain": [
    {
      "bar_utc": "2026-09-17T16:15:00Z",
      "replace_kind": null,
      "old_pool": "0.080904|0.08113",
      "new_pool": "0.081556|0.08168",
      "old_limit": null,
      "new_limit": 0.08168,
      "new_sl": 0.08064,
      "sl_pool": "0.08037399999999999|0.08064"
    }
  ],
  "effective_place": {
    "kind": "REPLACE_EFFECTIVE_PLACE",
    "bar_utc": "2026-09-17T16:15:00Z",
    "order_id": "c607b3de-e1f6-4738-82ce-880c45c61315",
    "limit": 0.08168,
    "sl": 0.08064,
    "prior_order_id": "23f8a357-2746-4b71-9ba2-90b8739884ae",
    "note": "Cross-lineage REPLACE creates new pending without separate PLACE/CANCEL ledger rows; fill order_id links to post-REPLACE pending."
  },
  "fill_event": {
    "action": "FILL",
    "bar_utc": "2026-09-17T16:30:00Z",
    "entry": 0.08168,
    "sl": 0.08064,
    "risk_pct": 1.2732615083251706,
    "rule_id": "CAUSAL_V2_LIMIT_TOUCH_INTRABAR",
    "sl_overlap": true,
    "order_id": "c607b3de-e1f6-4738-82ce-880c45c61315"
  },
  "exit_event": {
    "phase": "exit",
    "bar_utc": "2026-09-17T17:00:00Z",
    "action": "TP_LIMIT_FILL",
    "detail": {
      "price": 0.082106
    }
  },
  "parent_child": {
    "initial_order_id": "23f8a357-2746-4b71-9ba2-90b8739884ae",
    "fill_order_id": "c607b3de-e1f6-4738-82ce-880c45c61315",
    "replaced_without_explicit_cancel": true
  }
}
```

## 2. Fenster (known + extended)

```json
{
  "known_primary_16_17": {
    "window": "known_primary_16_17",
    "A_trades": 2,
    "B_trades": 3,
    "shared": [
      "2026-09-16T21:35:00Z|0.08028",
      "2026-09-17T16:30:00Z|0.08168"
    ],
    "only_A": [],
    "only_B": [
      "2026-09-17T02:15:00Z|0.08064"
    ],
    "A_tp": 2,
    "B_tp": 3,
    "A_sl": 0,
    "B_sl": 0,
    "A_sum_gross_pct": 1.044717,
    "B_sum_gross_pct": 1.701959,
    "delta_sum_gross_pct": 0.657242,
    "A_replaces": 1,
    "B_replaces": 2,
    "B_repricing_count": 1,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 0,
    "max_consecutive_sl_B": 0
  },
  "known_independent_18_19": {
    "window": "known_independent_18_19",
    "A_trades": 4,
    "B_trades": 4,
    "shared": [
      "2026-09-18T20:25:00Z|0.0877",
      "2026-09-18T23:25:00Z|0.0877",
      "2026-09-19T06:20:00Z|0.08717",
      "2026-09-19T20:10:00Z|0.08911"
    ],
    "only_A": [],
    "only_B": [],
    "A_tp": 3,
    "B_tp": 3,
    "A_sl": 1,
    "B_sl": 1,
    "A_sum_gross_pct": 1.557101,
    "B_sum_gross_pct": 1.557101,
    "delta_sum_gross_pct": 0.0,
    "A_replaces": 3,
    "B_replaces": 4,
    "B_repricing_count": 1,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 1,
    "max_consecutive_sl_B": 1
  },
  "known_control_20_21": {
    "window": "known_control_20_21",
    "A_trades": 1,
    "B_trades": 1,
    "shared": [
      "2026-09-21T13:30:00Z|0.09307"
    ],
    "only_A": [],
    "only_B": [],
    "A_tp": 1,
    "B_tp": 1,
    "A_sl": 0,
    "B_sl": 0,
    "A_sum_gross_pct": 1.418287,
    "B_sum_gross_pct": 1.418287,
    "delta_sum_gross_pct": 0.0,
    "A_replaces": 0,
    "B_replaces": 0,
    "B_repricing_count": 0,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 0,
    "max_consecutive_sl_B": 0
  },
  "extended_10_11": {
    "window": "extended_10_11",
    "A_trades": 0,
    "B_trades": 0,
    "shared": [],
    "only_A": [],
    "only_B": [],
    "A_tp": 0,
    "B_tp": 0,
    "A_sl": 0,
    "B_sl": 0,
    "A_sum_gross_pct": 0,
    "B_sum_gross_pct": 0,
    "delta_sum_gross_pct": 0,
    "A_replaces": 0,
    "B_replaces": 0,
    "B_repricing_count": 0,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 0,
    "max_consecutive_sl_B": 0
  },
  "extended_14_15": {
    "window": "extended_14_15",
    "A_trades": 1,
    "B_trades": 1,
    "shared": [
      "2026-09-14T21:35:00Z|0.08447"
    ],
    "only_A": [],
    "only_B": [],
    "A_tp": 0,
    "B_tp": 0,
    "A_sl": 1,
    "B_sl": 1,
    "A_sum_gross_pct": -1.053629,
    "B_sum_gross_pct": -1.053629,
    "delta_sum_gross_pct": 0.0,
    "A_replaces": 0,
    "B_replaces": 0,
    "B_repricing_count": 0,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 1,
    "max_consecutive_sl_B": 1
  },
  "extended_22_23": {
    "window": "extended_22_23",
    "A_trades": 1,
    "B_trades": 1,
    "shared": [
      "2026-09-22T07:05:00Z|0.09938"
    ],
    "only_A": [],
    "only_B": [],
    "A_tp": 0,
    "B_tp": 0,
    "A_sl": 1,
    "B_sl": 1,
    "A_sum_gross_pct": -0.835178,
    "B_sum_gross_pct": -0.835178,
    "delta_sum_gross_pct": 0.0,
    "A_replaces": 1,
    "B_replaces": 1,
    "B_repricing_count": 0,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 1,
    "max_consecutive_sl_B": 1
  },
  "extended_24_25": {
    "window": "extended_24_25",
    "A_trades": 1,
    "B_trades": 1,
    "shared": [
      "2026-09-24T22:00:00Z|0.09564"
    ],
    "only_A": [],
    "only_B": [],
    "A_tp": 1,
    "B_tp": 1,
    "A_sl": 0,
    "B_sl": 0,
    "A_sum_gross_pct": 0.637808,
    "B_sum_gross_pct": 0.637808,
    "delta_sum_gross_pct": 0.0,
    "A_replaces": 0,
    "B_replaces": 0,
    "B_repricing_count": 0,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 0,
    "max_consecutive_sl_B": 0
  },
  "extended_26_27": {
    "window": "extended_26_27",
    "A_trades": 0,
    "B_trades": 0,
    "shared": [],
    "only_A": [],
    "only_B": [],
    "A_tp": 0,
    "B_tp": 0,
    "A_sl": 0,
    "B_sl": 0,
    "A_sum_gross_pct": 0,
    "B_sum_gross_pct": 0,
    "delta_sum_gross_pct": 0,
    "A_replaces": 0,
    "B_replaces": 0,
    "B_repricing_count": 0,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 0,
    "max_consecutive_sl_B": 0
  },
  "extended_28_29": {
    "window": "extended_28_29",
    "A_trades": 1,
    "B_trades": 1,
    "shared": [
      "2026-09-29T14:00:00Z|0.0956"
    ],
    "only_A": [],
    "only_B": [],
    "A_tp": 0,
    "B_tp": 0,
    "A_sl": 1,
    "B_sl": 1,
    "A_sum_gross_pct": -3.033473,
    "B_sum_gross_pct": -3.033473,
    "delta_sum_gross_pct": 0.0,
    "A_replaces": 0,
    "B_replaces": 0,
    "B_repricing_count": 0,
    "tp_violations_B": [],
    "B_worse_shared_fills": [],
    "max_consecutive_sl_A": 1,
    "max_consecutive_sl_B": 1
  }
}
```

## 3. Totals & Repricing

```json
{
  "totals": {
    "A_trades": 11,
    "B_trades": 12,
    "A_sum_gross_pct": -0.264367,
    "B_sum_gross_pct": 0.392875,
    "delta_sum_gross_pct": 0.657242,
    "max_consecutive_sl_A": 1,
    "max_consecutive_sl_B": 1,
    "sample_note": "Addierte, nicht kapitalisierte Brutto-Trade-Renditen; keine Kontorendite."
  },
  "extended": {
    "windows_count": 6,
    "sum_delta_gross_pct": 0.0,
    "extra_b_fills": 0
  },
  "repricing_all_B": [
    {
      "window": "known_primary_16_17",
      "source": "repricing_audit",
      "bar_utc": "2026-09-17T02:10:00Z",
      "lineage_id": "lin_v2h_551",
      "old_limit": 0.08051,
      "new_limit": 0.08064,
      "entry_change_pct": 0.16147062476711604,
      "risk_pct": 0.9424603174603134,
      "tp_gap_pct": 0.65724206,
      "old_order_id": "341f3c7c-99c0-495e-a89d-d740b199fdb5",
      "new_order_id": "c1cac492-8020-4102-bd32-f059eabd53d8",
      "fill_risk_pct": 0.9424603174603134,
      "old_limit_a_frozen_reference": 0.08051,
      "filled_after_reprice": true,
      "fill_utc": "2026-09-17T02:15:00Z",
      "gross_return_pct": 0.657242
    },
    {
      "window": "known_primary_16_17",
      "source": "ledger_place_reprice",
      "bar_utc": "2026-09-17T02:10:00Z",
      "new_limit": 0.08064,
      "tp_gap_pct": 0.65724206,
      "repriced_from_limit": 0.08051,
      "order_id": "c1cac492-8020-4102-bd32-f059eabd53d8",
      "earliest_fill": "2026-09-17T02:15:00Z",
      "fill_risk_pct": 0.9424603174603134,
      "old_limit_a_frozen_reference": 0.08051,
      "entry_change_pct": 0.16147062476711604,
      "filled_after_reprice": true,
      "fill_utc": "2026-09-17T02:15:00Z",
      "gross_return_pct": 0.657242
    },
    {
      "window": "known_primary_16_17",
      "source": "ledger_replace",
      "bar_utc": "2026-09-17T02:10:00Z",
      "old_limit": 0.08051,
      "new_limit": 0.08064,
      "new_sl": 0.07988,
      "fill_risk_pct": 0.9424603174603134,
      "old_limit_a_frozen_reference": 0.08051,
      "entry_change_pct": 0.16147062476711604,
      "filled_after_reprice": true,
      "fill_utc": "2026-09-17T02:15:00Z",
      "gross_return_pct": 0.657242
    },
    {
      "window": "known_independent_18_19",
      "source": "repricing_audit",
      "bar_utc": "2026-09-19T16:05:00Z",
      "lineage_id": "lin_v2h_646",
      "old_limit": 0.08842,
      "new_limit": 0.08856,
      "entry_change_pct": 0.15833521827640945,
      "risk_pct": 1.0388437217705557,
      "tp_gap_pct": 1.43405601,
      "old_order_id": "355bd6f6-1598-48c6-9a91-ffe788298203",
      "new_order_id": "0c007994-e402-4fe7-a020-773b714cdbbf",
      "old_limit_a_frozen_reference": 0.08842,
      "filled_after_reprice": false,
      "fill_utc": null,
      "gross_return_pct": null
    },
    {
      "window": "known_independent_18_19",
      "source": "ledger_place_reprice",
      "bar_utc": "2026-09-19T16:05:00Z",
      "new_limit": 0.08856,
      "tp_gap_pct": 1.43405601,
      "repriced_from_limit": 0.08842,
      "order_id": "0c007994-e402-4fe7-a020-773b714cdbbf",
      "earliest_fill": "2026-09-19T16:10:00Z",
      "old_limit_a_frozen_reference": 0.08842,
      "entry_change_pct": 0.15833521827640945,
      "filled_after_reprice": false,
      "fill_utc": null,
      "gross_return_pct": null
    },
    {
      "window": "known_independent_18_19",
      "source": "ledger_replace",
      "bar_utc": "2026-09-19T16:05:00Z",
      "old_limit": 0.08842,
      "new_limit": 0.08856,
      "new_sl": 0.08764,
      "old_limit_a_frozen_reference": 0.08842,
      "entry_change_pct": 0.15833521827640945,
      "filled_after_reprice": false,
      "fill_utc": null,
      "gross_return_pct": null
    }
  ]
}
```

TP-Regelverletzungen B: **0**

Long pytest: rc=0

---
STOP.