# ZEC causal trade-context analysis

Final label: **ZEC_CAUSAL_CONTEXT_ANALYSIS_COMPLETE**

- Symbol: `ZECUSDT`
- Evaluation: `94d0cfbfb2da4c829dc0d95588dc052d`
- Source job: `f5909d14cba34fc9973a8b431530752d`
- Strategy: `wave_fade_frozen_f16ae32_causal_entry_v1`
- Semantics: `cross_recognition`, `NO_BE50`, `full_1m_scan`, `SL_FIRST`, no max-hold, entry = first 1m open strictly after confirmation
- Views: SIGNAL_VIEW keeps every ZEC outcome. EXECUTION_DIAGNOSTIC_VIEW only adds overlap flags.
- No strategy change, no filter search, no ML, no ClickHouse writes, no new backtest.

## Inventory (before any feature work)

- ZEC trades: 1158 (expected 1158)
- Wins: 527 (expected 527)
- Losses: 629 (expected 629)
- Open: 2 (expected 2)
- Period entries: 2026-03-01T02:46:00Z → 2026-08-16T21:31:00Z
- Last exit: 2026-08-16T14:04:00Z
- Timeframes: ['15m', '1h', '30m', '4h']
- LONG/SHORT: {'LONG': 561, 'SHORT': 597}
- By TF: {'15m': 646, '1h': 165, '30m': 309, '4h': 38}
- Duplicate signal_ids: 0
- Duplicate setup_ids: 0
- Duplicate generation_keys: 0
- Trades sharing an entry time: 56 across 28 timestamps
- Trades overlapping a still-open ZEC trade: 314
- Overlap same direction: 208
- Overlap opposite direction: 106
- Source raw signals in job artifact: 5050
- Outcomes missing source signal join: 0
- No trade was removed.

## Pflichtfragen

### 1. Wie viele ZEC-Trades/Wins/Losses wurden analysiert?

1158 Trades, 527 Wins, 629 Losses, 2 Open. Closed win-rate = 45.59% (OPEN excluded). Alle Evaluation-Outcomes sind in SIGNAL_VIEW enthalten.

### 2. Sind alle Features strikt kausal?

Ja. Jeder TF-Snapshot erfüllt available_at <= entry_time. Lookahead-Hard-Fail wurde nicht ausgelöst.
Outcome-Felder (MFE/MAE/PnL) sind getrennt gespeichert und nicht als Entry-Feature verwendet.
1d ist nur enthalten, wenn die letzte UTC-Tageskerze vollständig geschlossen war.

### 3. Welche EMA-/Preisstrukturmerkmale unterscheiden Wins und Losses?

- `tf_4h_range20_pos_entry`: SMD=-0.111, mean WIN=0.4977, mean LOSS=0.5346 (nW=527, nL=629)
- `tf_5m_ret_5bar_pct`: SMD=0.072, mean WIN=0.0269, mean LOSS=-0.0348 (nW=527, nL=629)
- `tf_4h_close_minus_ema20_pct`: SMD=-0.071, mean WIN=0.6339, mean LOSS=1.1402 (nW=527, nL=629)
- `tf_4h_close_minus_ema50_pct`: SMD=-0.070, mean WIN=1.6259, mean LOSS=2.3740 (nW=527, nL=629)
- `tf_1h_close_minus_ema200_pct`: SMD=-0.069, mean WIN=1.7492, mean LOSS=2.5000 (nW=527, nL=629)
- `tf_1h_close_minus_ema50_pct`: SMD=-0.060, mean WIN=0.5087, mean LOSS=0.8750 (nW=527, nL=629)
- `tf_4h_close_minus_ema200_pct`: SMD=-0.060, mean WIN=6.0492, mean LOSS=7.1978 (nW=527, nL=629)
- `room_to_target_vs_tp`: SMD=0.056, mean WIN=9.9059, mean LOSS=9.3553 (nW=527, nL=629)

Bucket 4h EMA-Trend: NEUTRAL: n=434 loss=53.5%; STRONG_BULL: n=291 loss=58.4%; STRONG_BEAR: n=211 loss=50.2%; BEAR: n=119 loss=49.6%; BULL: n=101 loss=61.4%

### 4. Welche Stoch-Zustände unterscheiden Wins und Losses?

- `tf_4h_stoch_k`: SMD=-0.096, mean WIN=48.4685, mean LOSS=51.7119 (nW=527, nL=629)
- `ltf_5m_exhausted`: SMD=-0.071, mean WIN=0.4668, mean LOSS=0.5374 (nW=527, nL=629)
- `tf_5m_stoch_opposes_trade`: SMD=-0.069, mean WIN=0.5579, mean LOSS=0.6264 (nW=527, nL=629)
- `tf_1m_stoch_opposes_trade`: SMD=0.057, mean WIN=0.7230, mean LOSS=0.6661 (nW=527, nL=629)
- `tf_1h_stoch_k`: SMD=0.025, mean WIN=48.6677, mean LOSS=47.8189 (nW=527, nL=629)
- `tf_5m_stoch_k`: SMD=0.017, mean WIN=49.2937, mean LOSS=48.7198 (nW=527, nL=629)
- `tf_15m_stoch_k`: SMD=0.016, mean WIN=48.9951, mean LOSS=48.4463 (nW=527, nL=629)
- `tf_4h_stoch_exhausted_in_trade_direction`: SMD=0.012, mean WIN=0.1822, mean LOSS=0.1701 (nW=527, nL=629)

Bucket 5m Stoch: BULL_MOMENTUM: n=293 loss=51.2%; OVERSOLD: n=255 loss=59.6%; BEAR_MOMENTUM: n=244 loss=50.0%; OVERBOUGHT: n=240 loss=58.3%; OVERSOLD_TURNING_UP: n=65 loss=47.7%; OVERBOUGHT_TURNING_DOWN: n=59 loss=57.6%

### 5. Verlieren LONGs/SHORTs häufiger gegen 1h/4h-Trend?

SHORT vs 4h SUPPORTS: n=80, loss-rate=57.5%; SHORT vs 4h OPPOSES: n=376, loss-rate=53.2%.
LONG vs 4h SUPPORTS: n=91, loss-rate=53.8%; LONG vs 4h OPPOSES: n=319, loss-rate=53.3%.
SHORT vs 1h OPPOSES: n=417, loss-rate=57.1%; LONG vs 1h OPPOSES: n=355, loss-rate=51.3%.
1h+4h alignment: partial_oppose: n=503 loss=52.5%; 1h+4h_oppose: n=482 loss=54.6%; partial_support: n=83 loss=57.8%; neutral_or_missing: n=71 loss=64.8%; 1h+4h_support: n=17 loss=47.1%

### 6. Verlieren Shorts am unteren 4h-Range-Rand häufiger?

SHORT near 4h low: n=33, loss-rate=51.5%; other SHORTs: n=564, loss-rate=55.1%.
Range buckets: [0.80,1]: n=281 loss=57.7%; [0,0.20): n=238 loss=49.6%; [0.20,0.40): n=228 loss=53.9%; [0.60,0.80): n=207 loss=56.0%; [0.40,0.60): n=202 loss=54.5%

### 7. Verlieren Longs am oberen 4h-Range-Rand häufiger?

LONG near 4h high: n=36, loss-rate=66.7%; other LONGs: n=523, loss-rate=53.0%.

### 8. Ist 5m beim Entry häufig bereits erschöpft?

5m exhausted: n=584, loss-rate=57.9%; not exhausted: n=572, loss-rate=50.9%.
Share exhausted among all closed trades: 50.5%.

### 9. Dreht 1m bei Losses häufiger gegen den Trade?

1m opposite recross overall: n=157, loss-rate=49.7%; ohne Recross: n=999, loss-rate=55.2%.
Share among LOSS: 12.4%; among WIN: 15.0%.

### 10. Wie stark wirkt bereits verbrauchter TP-Weg?

(25,50%]: n=302 loss=55.0%; (0,25%]: n=257 loss=50.6%; (50,75%]: n=206 loss=51.0%; <=0: n=151 loss=57.6%; >100%: n=131 loss=59.5%; (75,100%]: n=109 loss=57.8%

Numeric SMD for `tp_consumed_frac` is in `win_loss_feature_comparison.csv`. No threshold was chosen from profit.

### 11. Wie viele Losses überlappen mit bereits offenen ZEC-Trades?

173 Losses überlappen (von 314 überlappenden Trades insgesamt). Overlap ist nur Diagnose; Outcomes wurden nicht geändert.

### 12. Erklären die objektiven Werte unsere zwei manuellen Beispiele?

### 2026-08-16T05:31:00Z SHORT `8c914b1f-c154-58e6-a8ec-5f8014234267` (LOSS)
- Signal-TF: 15m; Overlap: open=1, same=False, opposite=True, exact_dup=False
- Matrix: [{"tf": "1m", "price_trend": "RANGE", "ema_trend": "BEAR", "stoch_phase": "BULL_MOMENTUM", "supports_opposes": "MIXED"}, {"tf": "5m", "price_trend": "RANGE", "ema_trend": "NEUTRAL", "stoch_phase": "OVERSOLD", "supports_opposes": "OPPOSES"}, {"tf": "15m", "price_trend": "COMPRESSION", "ema_trend": "NEUTRAL", "stoch_phase": "OVERBOUGHT_TURNING_DOWN", "supports_opposes": "SUPPORTS"}, {"tf": "30m", "price_trend": "UP", "ema_trend": "STRONG_BEAR", "stoch_phase": "OVERBOUGHT", "supports_opposes": "SUPPORTS"}, {"tf": "1h", "price_trend": "COMPRESSION", "ema_trend": "STRONG_BEAR", "stoch_phase": "BULL_MOMENTUM", "supports_opposes": "MIXED"}, {"tf": "4h", "price_trend": "DOWN", "ema_trend": "STRONG_BEAR", "stoch_phase": "BEAR_MOMENTUM", "supports_opposes": "SUPPORTS"}, {"tf": "1d", "price_trend": "COMPRESSION", "ema_trend": "NEUTRAL", "stoch_phase": "BEAR_MOMENTUM", "supports_opposes": "SUPPORTS"}]
- 4h EMA=STRONG_BEAR Stoch=BEAR_MOMENTUM range_pos=0.3523706896551735 near_low=False
- 1h supports/opposes=MIXED; 4h=SUPPORTS
- 5m exhausted=True; 1m opposite recross=False
- TP already consumed=0.22946117598853233; 5m pre-entry aligned=-0.030811576936518037
- Weakness note: 5m Stoch exhausted (OVERSOLD, K=18.76612708533856); 1m Stoch opposes the trade (BULL_MOMENTUM, K=59.164142713241176); overlaps 1 open ZEC trade(s) same=False opp=True

### 2026-08-16T09:46:00Z SHORT `188fabf8-ddcd-5bed-96c4-586e7cce26f4` (LOSS)
- Signal-TF: 15m; Overlap: open=2, same=True, opposite=True, exact_dup=False
- Matrix: [{"tf": "1m", "price_trend": "RANGE", "ema_trend": "BEAR", "stoch_phase": "OVERSOLD_TURNING_UP", "supports_opposes": "OPPOSES"}, {"tf": "5m", "price_trend": "DOWN", "ema_trend": "NEUTRAL", "stoch_phase": "OVERSOLD", "supports_opposes": "OPPOSES"}, {"tf": "15m", "price_trend": "DOWN", "ema_trend": "NEUTRAL", "stoch_phase": "BEAR_MOMENTUM", "supports_opposes": "SUPPORTS"}, {"tf": "30m", "price_trend": "COMPRESSION", "ema_trend": "NEUTRAL", "stoch_phase": "OVERBOUGHT", "supports_opposes": "NEUTRAL"}, {"tf": "1h", "price_trend": "UP", "ema_trend": "STRONG_BEAR", "stoch_phase": "BULL_MOMENTUM", "supports_opposes": "MIXED"}, {"tf": "4h", "price_trend": "COMPRESSION", "ema_trend": "STRONG_BEAR", "stoch_phase": "BEAR_MOMENTUM", "supports_opposes": "SUPPORTS"}, {"tf": "1d", "price_trend": "COMPRESSION", "ema_trend": "NEUTRAL", "stoch_phase": "BEAR_MOMENTUM", "supports_opposes": "SUPPORTS"}]
- 4h EMA=STRONG_BEAR Stoch=BEAR_MOMENTUM range_pos=0.3469827586206895 near_low=False
- 1h supports/opposes=MIXED; 4h=SUPPORTS
- 5m exhausted=True; 1m opposite recross=False
- TP already consumed=0.45796532548250113; 5m pre-entry aligned=0.10873802342997729
- Weakness note: 5m Stoch exhausted (OVERSOLD, K=12.413350897543664); 1m Stoch opposes the trade (OVERSOLD_TURNING_UP, K=14.151765868585658); already consumed 0.46 of TP distance before entry; overlaps 2 open ZEC trade(s) same=True opp=True


### 13. Welche Merkmale erscheinen robust genug für eine spätere feste Regel?

Noch keine Regel. Kandidaten nur, wenn der Unterschied in der geschlossenen Population sichtbar ist, das Bootstrap-CI die Null meidet, und der Split `development` später getrennt geprüft wird. Testfenster bleibt unangetastet.

- Kein einzelnes Entry-Feature erreicht zugleich |SMD|>=0.15 und ein CI ohne 0. Spätere Regeln müssen auf den Bucket-Tabellen und Replikation im Validation-Split beruhen, nicht auf Profit-Suche.

### 14. Welche Auffälligkeiten verschwinden beim WIN-Vergleich?

Boolean-Features mit |WIN-rate − LOSS-rate| < 3pp: `ltf_1m_opposite_recross`, `htf_support_before_short_tp`, `htf_resistance_before_long_tp`, `tf_4h_stoch_exhausted_in_trade_direction`, `tf_4h_ema_trend_opposes_trade`, `tf_4h_ema_strongly_opposes_trade`, `tf_1h_ema_trend_opposes_trade`, `already_ran_25pct_tp`, `already_ran_50pct_tp`, `already_ran_100pct_tp`, `exact_entry_duplicate`, `overlaps_previous_trade`, `overlap_same_direction`, `overlap_opposite_direction`, `higher_tf_would_win`.
Einzelne manuelle Chart-Eindrücke (z.B. „4h sieht überkauft aus“) können in der vollen WIN/LOSS-Population schwächer sein als im Einzelfall. Deshalb keine Filter aus den zwei August-SHORTs ableiten.

### 15. Gibt es Daten-/Lookahead-Probleme?

- Lookahead failures: 0
- Snapshots missing: 0
- 1m gaps: 0
- Incomplete HTF buckets discarded: {'1m': 0, '5m': 1, '15m': 1, '30m': 1, '1h': 1, '4h': 1, '1d': 1}
- Incomplete HTF note: Exactly one incomplete bucket per HTF is the still-open pin bucket after last 1m 2026-08-17T00:00. Not a mid-history gap.
- EMA200 missing share (4h): 0.0
- Manual bar-time check 09:46: True
- Issue rows: 0
- ClickHouse writes: 0 (SELECT only)

## Split

- development (first 60% by entry): 694
- validation (next 20%): 231
- test (last 20%, untouched): 233

Test-window metrics were not used to pick thresholds.

## Files

- `zec_trade_context.parquet` / `.csv`
- `timeframe_snapshots.parquet`
- `feature_dictionary.json`
- `feature_availability_audit.csv`
- `win_loss_feature_comparison.csv`
- `feature_bucket_outcomes.csv`
- `timeframe_alignment_summary.csv`
- `overlap_diagnostics.csv`
- `selected_case_studies.csv`
- `data_quality_audit.json`
- `data_quality_issues.csv`
