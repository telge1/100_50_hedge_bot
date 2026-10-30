# LONG V3 Phase 2L — Fixed vs Dynamic Buy Limit Evidence

**STOP — Auswertung only, keine produktive Aktivierung.**

## 1. A/B-Vergleich ( drei Fenster )

```json
{
  "primary_16_17": {
    "A_trades": 2,
    "B_trades": 3,
    "shared_fills": [
      "2026-09-16T21:35:00Z|0.08028",
      "2026-09-17T16:30:00Z|0.08168"
    ],
    "only_A": [],
    "only_B": [
      "2026-09-17T02:15:00Z|0.08064"
    ],
    "A_pending_places": 2,
    "B_pending_places": 2,
    "A_cancels": 2,
    "B_cancels": 2,
    "A_replaces": 1,
    "B_replaces": 2,
    "A_tp_exits": 2,
    "B_tp_exits": 3,
    "A_sl_exits": 0,
    "B_sl_exits": 0,
    "A_avg_risk_pct": 1.42761232,
    "B_avg_risk_pct": 1.26589499,
    "A_sum_positive_gross_pct": 1.044717,
    "B_sum_positive_gross_pct": 1.701959,
    "A_sum_negative_gross_pct": 0,
    "B_sum_negative_gross_pct": 0,
    "A_sum_gross_pct": 1.044717,
    "B_sum_gross_pct": 1.701959
  },
  "independent_18_19": {
    "A_trades": 4,
    "B_trades": 4,
    "shared_fills": [
      "2026-09-18T20:25:00Z|0.0877",
      "2026-09-18T23:25:00Z|0.0877",
      "2026-09-19T06:20:00Z|0.08717",
      "2026-09-19T20:10:00Z|0.08911"
    ],
    "only_A": [],
    "only_B": [],
    "A_pending_places": 1,
    "B_pending_places": 2,
    "A_cancels": 1,
    "B_cancels": 2,
    "A_replaces": 3,
    "B_replaces": 4,
    "A_tp_exits": 3,
    "B_tp_exits": 3,
    "A_sl_exits": 1,
    "B_sl_exits": 1,
    "A_avg_risk_pct": 1.42377656,
    "B_avg_risk_pct": 1.42377656,
    "A_sum_positive_gross_pct": 2.836419,
    "B_sum_positive_gross_pct": 2.836419,
    "A_sum_negative_gross_pct": -1.279318,
    "B_sum_negative_gross_pct": -1.279318,
    "A_sum_gross_pct": 1.557101,
    "B_sum_gross_pct": 1.557101
  },
  "control_20_21": {
    "A_trades": 1,
    "B_trades": 1,
    "shared_fills": [
      "2026-09-21T13:30:00Z|0.09307"
    ],
    "only_A": [],
    "only_B": [],
    "A_pending_places": 0,
    "B_pending_places": 0,
    "A_cancels": 0,
    "B_cancels": 0,
    "A_replaces": 0,
    "B_replaces": 0,
    "A_tp_exits": 1,
    "B_tp_exits": 1,
    "A_sl_exits": 0,
    "B_sl_exits": 0,
    "A_avg_risk_pct": 4.95326099,
    "B_avg_risk_pct": 4.95326099,
    "A_sum_positive_gross_pct": 1.418287,
    "B_sum_positive_gross_pct": 1.418287,
    "A_sum_negative_gross_pct": 0,
    "B_sum_negative_gross_pct": 0,
    "A_sum_gross_pct": 1.418287,
    "B_sum_gross_pct": 1.418287
  }
}
```

## 2. Zusätzlicher B-Fill 17.09.

```json
{
  "trade": {
    "variant": "B",
    "window": "primary_16_17",
    "position_id": "4f4353ae-92f6-41d2-a065-2f076ec67e5e",
    "signal_close_utc": "2026-09-17T02:10:00Z",
    "place_utc": "2026-09-17T02:10:00Z",
    "fill_utc": "2026-09-17T02:15:00Z",
    "entry_price_usdt": 0.08064,
    "entry_price_usdt_repr": "0.08064",
    "planned_limit_at_last_place": 0.08064,
    "p_lower_at_place": null,
    "p_upper_at_place": 0.08064,
    "p_upper_at_fill": 0.08064,
    "pool_key": "0.08037399999999999|0.08064",
    "q_lower": null,
    "q_upper": null,
    "structural_sl_usdt": 0.07988,
    "risk_pct_entry_to_sl": 0.94246032,
    "sl_distance_pct": 0.94246032,
    "tp_price_usdt": 0.08117,
    "tp_upper_pool_lower": 0.08117,
    "tp_upper_pool_upper": 0.08122,
    "tp_distance_pct": 0.65724206,
    "tp_min_0_5_ok_at_fill": true,
    "tp_rule_violation": null,
    "exit_utc": "2026-09-17T02:40:00Z",
    "exit_price_usdt": 0.08117,
    "exit_reason": "FIXED_UPPER_TP",
    "gross_return_pct": 0.657242,
    "repricing_events": [
      {
        "action": "REPRICING_AUDIT",
        "bar_utc": "2026-09-17T02:10:00Z",
        "lineage_id": "lin_v2h_551",
        "old_limit": 0.08051,
        "new_limit": 0.08064,
        "entry_change_pct": 0.16147062,
        "risk_pct": 0.9424603174603134,
        "tp_gap_pct": 0.65724206,
        "old_order_id": "87eb2d88-e417-449a-aadd-e2837491c295",
        "new_order_id": "a1fbf4ff-79e3-46ae-b14f-a2821401827e"
      }
    ],
    "chart_markers": {
      "ENTRY": "0.08064",
      "STOP_LOSS": "0.07988",
      "TAKE_PROFIT": "0.08117"
    },
    "event_timeline_utc": {
      "PLACE": "2026-09-17T02:10:00Z",
      "FILL": "2026-09-17T02:15:00Z",
      "EXIT": "2026-09-17T02:40:00Z",
      "CANCEL_REPLACE": [
        {
          "bar": "2026-09-17T02:10:00Z",
          "action": "REPRICING_AUDIT",
          "detail": {
            "action": "REPRICING_AUDIT",
            "bar_utc": "2026-09-17T02:10:00Z",
            "lineage_id": "lin_v2h_551",
            "old_limit": 0.08051,
            "new_limit": 0.08064,
            "entry_change_pct": 0.16147062,
            "risk_pct": 0.9424603174603134,
            "tp_gap_pct": 0.65724206,
            "old_order_id": "87eb2d88-e417-449a-aadd-e2837491c295",
            "new_order_id": "a1fbf4ff-79e3-46ae-b14f-a2821401827e"
          }
        }
      ]
    },
    "open_at_window_end": false
  },
  "repricing_02_10": [
    {
      "bar_utc": "2026-09-17T02:10:00Z",
      "lineage_id": "lin_v2h_551",
      "old_limit": 0.08051,
      "new_limit": 0.08064,
      "entry_change_pct": 0.16147062,
      "risk_pct": 0.9424603174603134,
      "tp_gap_pct": 0.65724206,
      "old_order_id": "87eb2d88-e417-449a-aadd-e2837491c295",
      "new_order_id": "a1fbf4ff-79e3-46ae-b14f-a2821401827e"
    }
  ],
  "ledger_B_slice": [
    {
      "phase": "entry",
      "action": "CANCEL",
      "bar_utc": "2026-09-17T02:10:00Z",
      "reason": "DYNAMIC_UPPER_GROWTH_REPRICE",
      "order_id": "87eb2d88-e417-449a-aadd-e2837491c295",
      "old_limit": 0.08051,
      "new_limit": 0.08064
    },
    {
      "phase": "entry",
      "action": "PLACE",
      "bar_utc": "2026-09-17T02:10:00Z",
      "limit": 0.08064,
      "sl": 0.07988,
      "sl_pool": "0.07945799999999999|0.07988",
      "gap_pct": 0.61462662,
      "risk_pct": 0.9424603174603134,
      "sl_overlap": true,
      "earliest": "2026-09-17T02:15:00Z",
      "order_id": "a1fbf4ff-79e3-46ae-b14f-a2821401827e",
      "lineage_id": "lin_v2h_551",
      "selection_reason": "DYNAMIC_UPPER_GROWTH_REPRICE",
      "repriced_from_limit": 0.08051,
      "entry_change_pct": 0.16147062,
      "tp_gap_pct": 0.65724206
    },
    {
      "phase": "entry",
      "action": "REPLACE",
      "bar_utc": "2026-09-17T02:10:00Z",
      "replace_kind": "DYNAMIC_UPPER_GROWTH_V2K",
      "old_pool": "0.08037399999999999|0.08051",
      "new_pool": "0.08037399999999999|0.08064",
      "old_limit": 0.08051,
      "new_limit": 0.08064,
      "new_sl": 0.07988,
      "entry_change_pct": 0.16147062
    },
    {
      "phase": "entry",
      "action": "FILL",
      "bar_utc": "2026-09-17T02:15:00Z",
      "entry": 0.08064,
      "sl": 0.07988,
      "risk_pct": 0.9424603174603134,
      "rule_id": "CAUSAL_V2_LIMIT_TOUCH_INTRABAR",
      "sl_overlap": true,
      "order_id": "a1fbf4ff-79e3-46ae-b14f-a2821401827e"
    }
  ],
  "ledger_A_slice": [
    {
      "phase": "entry",
      "action": "PLACE",
      "bar_utc": "2026-09-17T01:05:00Z",
      "limit": 0.08051,
      "sl": 0.07988,
      "sl_pool": "0.07945799999999999|0.07988",
      "gap_pct": 0.61462662,
      "risk_pct": 0.7825114892559828,
      "sl_overlap": true,
      "earliest": "2026-09-17T01:10:00Z",
      "order_id": "df9e539b-2d09-4f08-a39a-face1787bd96",
      "lineage_id": "lin_v2h_551",
      "frozen_support_key": "0.08037399999999999|0.08051",
      "entry_support": {
        "lower_edge": 0.08037399999999999,
        "upper_edge": 0.08051,
        "distinct_origin_ids": [
          "lldv2:DOGEUSDT:5m:lower:1787375700",
          "lldv2:DOGEUSDT:5m:lower:1789601700",
          "lldv2:DOGEUSDT:5m:lower:1789606800"
        ],
        "atomics": [
          {
            "pool_key": "0.080373999999999987|0.080421999999999993",
            "lower_edge": 0.08037399999999999,
            "upper_edge": 0.080422,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789601700",
              "lldv2:DOGEUSDT:5m:lower:1789606800"
            ]
          },
          {
            "pool_key": "0.080421999999999993|0.080449999999999994",
            "lower_edge": 0.080422,
            "upper_edge": 0.08045,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789601700",
              "lldv2:DOGEUSDT:5m:lower:1789606800"
            ]
          },
          {
            "pool_key": "0.080449999999999994|0.080509999999999998",
            "lower_edge": 0.08045,
            "upper_edge": 0.08051,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789601700",
              "lldv2:DOGEUSDT:5m:lower:1789606800"
            ]
          }
        ]
      },
      "sl_pool_audit": {
        "lower_edge": 0.07945799999999999,
        "upper_edge": 0.07988,
        "distinct_origin_ids": [
          "lldv2:DOGEUSDT:5m:lower:1787375700",
          "lldv2:DOGEUSDT:5m:lower:1789596000",
          "lldv2:DOGEUSDT:5m:lower:1789597200",
          "lldv2:DOGEUSDT:5m:lower:1789597800",
          "lldv2:DOGEUSDT:5m:lower:1789599000",
          "lldv2:DOGEUSDT:5m:lower:1789599600"
        ],
        "atomics": [
          {
            "pool_key": "0.079457999999999987|0.079763999999999988",
            "lower_edge": 0.07945799999999999,
            "upper_edge": 0.07976399999999999,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789596000",
              "lldv2:DOGEUSDT:5m:lower:1789599600"
            ]
          },
          {
            "pool_key": "0.079763999999999988|0.079769999999999994",
            "lower_edge": 0.07976399999999999,
            "upper_edge": 0.07977,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789596000",
              "lldv2:DOGEUSDT:5m:lower:1789599600"
            ]
          },
          {
            "pool_key": "0.079769999999999994|0.079780000000000004",
            "lower_edge": 0.07977,
            "upper_edge": 0.07978,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789596000",
              "lldv2:DOGEUSDT:5m:lower:1789597200",
              "lldv2:DOGEUSDT:5m:lower:1789599600"
            ]
          },
          {
            "pool_key": "0.079780000000000004|0.079816000000000012",
            "lower_edge": 0.07978,
            "upper_edge": 0.07981600000000001,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789597200",
              "lldv2:DOGEUSDT:5m:lower:1789599000",
              "lldv2:DOGEUSDT:5m:lower:1789599600"
            ]
          },
          {
            "pool_key": "0.079816000000000012|0.079820000000000002",
            "lower_edge": 0.07981600000000001,
            "upper_edge": 0.07982,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789597200",
              "lldv2:DOGEUSDT:5m:lower:1789597800",
              "lldv2:DOGEUSDT:5m:lower:1789599000",
              "lldv2:DOGEUSDT:5m:lower:1789599600"
            ]
          },
          {
            "pool_key": "0.079820000000000002|0.079839999999999994",
            "lower_edge": 0.07982,
            "upper_edge": 0.07984,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789597200",
              "lldv2:DOGEUSDT:5m:lower:1789597800",
              "lldv2:DOGEUSDT:5m:lower:1789599000",
              "lldv2:DOGEUSDT:5m:lower:1789599600"
            ]
          },
          {
            "pool_key": "0.079839999999999994|0.07986",
            "lower_edge": 0.07984,
            "upper_edge": 0.07986,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789597200",
              "lldv2:DOGEUSDT:5m:lower:1789597800",
              "lldv2:DOGEUSDT:5m:lower:1789599000",
              "lldv2:DOGEUSDT:5m:lower:1789599600"
            ]
          },
          {
            "pool_key": "0.07986|0.079880000000000007",
            "lower_edge": 0.07986,
            "upper_edge": 0.07988,
            "origin_ids": [
              "lldv2:DOGEUSDT:5m:lower:1787375700",
              "lldv2:DOGEUSDT:5m:lower:1789597200",
              "lldv2:DOGEUSDT:5m:lower:1789597800",
              "lldv2:DOGEUSDT:5m:lower:1789599000"
            ]
          }
        ]
      },
      "strength": {
        "pool_height_pct": 0.16920895,
        "atomics": [
          {
            "pool_key": "0.08037399999999999|0.080422",
            "consecutive_active_bars": 1,
            "age_ok": false,
            "formation_below_price_ok": true,
            "formation_reference_close": 0.08058,
            "atomic_upper_at_formation": 0.080422
          },
          {
            "pool_key": "0.080422|0.08045",
            "consecutive_active_bars": 1,
            "age_ok": false,
            "formation_below_price_ok": true,
            "formation_reference_close": 0.08058,
            "atomic_upper_at_formation": 0.08045
          },
          {
            "pool_key": "0.08045|0.08051",
            "consecutive_active_bars": 1,
            "age_ok": false,
            "formation_below_price_ok": true,
            "formation_reference_close": 0.08058,
            "atomic_upper_at_formation": 0.08051
          }
        ]
      },
      "skipped_pools": [],
      "config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J"
    },
    {
      "phase": "entry",
      "action": "CANCEL",
      "bar_utc": "2026-09-17T02:15:00Z",
      "reason": "GAP_OR_SUPPORT_LOST",
      "order_id": "df9e539b-2d09-4f08-a39a-face1787bd96",
      "detail": "NOT_BELOW_MARKET_CLOSE"
    }
  ],
  "narrative": "B: REPLACE 02:10 UTC hebt Limit 0.08051\u21920.08064 (Lineage lin_v2h_551); Fill 02:15 @ 0.08064. A: Limit bleibt 0.08051 (FROZEN) \u2014 kein Fill in dieser Sequenz, da Kurs das h\u00f6here B-Limit erreicht ohne Retrace auf 0.08051."
}
```

## 3. TP-Mindestabstand Variante B (PLACE/REPLACE)

Regelverletzungen: 0 — `[]`

## 4. B-Vorteil nur ein Trade?

Primary only-B keys: ['2026-09-17T02:15:00Z|0.08064']. Einschränkung auf einen Zusatz-Fill: **True**.

## 5. Tests

Long pytest rc=0. Short: rc=0 (June D107 falls Bundle fehlt).

## 6. Artefakte

- `/home/telgenbuescher/projects/pools+ob+delta_bot/logs/research/long_v3_phase2l_evidence_v1/trade_chart_review.csv`
- `/home/telgenbuescher/projects/pools+ob+delta_bot/logs/research/long_v3_phase2l_evidence_v1/trade_chart_review.json`

## Kompakte Chart-Tabelle

| Datum/Uhrzeit UTC | Variante | ENTRY USDT | SL USDT | TP USDT | EXIT USDT | Ergebnis % |
|---|---|---|---|---|---|---|
| 2026-09-16T21:35:00Z | A | 0.08028 | 0.07901 | 0.0807 | 0.0807 | 0.523169 |
| 2026-09-16T21:35:00Z | B | 0.08028 | 0.07901 | 0.0807 | 0.0807 | 0.523169 |
| 2026-09-17T02:15:00Z | B | 0.08064 | 0.07988 | 0.08117 | 0.08117 | 0.657242 |
| 2026-09-17T16:30:00Z | A | 0.08168 | 0.08064 | 0.082106 | 0.082106 | 0.521548 |
| 2026-09-17T16:30:00Z | B | 0.08168 | 0.08064 | 0.082106 | 0.082106 | 0.521548 |
| 2026-09-18T20:25:00Z | A | 0.0877 | 0.08681 | 0.08818 | 0.08818 | 0.54732 |
| 2026-09-18T20:25:00Z | B | 0.0877 | 0.08681 | 0.08818 | 0.08818 | 0.54732 |
| 2026-09-18T23:25:00Z | A | 0.0877 | 0.08681 | 0.08846 | 0.08846 | 0.866591 |
| 2026-09-18T23:25:00Z | B | 0.0877 | 0.08681 | 0.08846 | 0.08846 | 0.866591 |
| 2026-09-19T06:20:00Z | A | 0.08717 | 0.08509 | 0.08841 | 0.08841 | 1.422508 |
| 2026-09-19T06:20:00Z | B | 0.08717 | 0.08509 | 0.08841 | 0.08841 | 1.422508 |
| 2026-09-19T20:10:00Z | A | 0.08911 | 0.08797 | 0.09019 | 0.08797 | -1.279318 |
| 2026-09-19T20:10:00Z | B | 0.08911 | 0.08797 | 0.09019 | 0.08797 | -1.279318 |
| 2026-09-21T13:30:00Z | A | 0.09307 | 0.08846 | 0.09439 | 0.09439 | 1.418287 |
| 2026-09-21T13:30:00Z | B | 0.09307 | 0.08846 | 0.09439 | 0.09439 | 1.418287 |