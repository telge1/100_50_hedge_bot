# APT Winner Forensic Manual Order Walkthrough

Timing: **T1** (close confirm → next open fill), threshold **6%**.

Engine note: Cobertura uses **implicit level triggers**, not a full resting OMS with create/replace/cancel states for every add. Events below are engine `order_events` + `fills`.

### Event 001 — Start-distance trigger (T1)

Zeit Trigger-Close: `2026-01-19T00:00:00+00:00`
Close: `1.6447`
Distanz am Close: `0.08183450163712698`
Warum 00:00 nicht: Distanz am Open `0.052197438380005054` < 6%
Kausal bekannt: ['all candles strictly before trigger candle', 'completed OHLC of trigger candle (close decision)', 'pre-signal TEM book quantities/averages']
Audit-Ergebnis: PASS

### Event 002 — Neutralization short fill

Zeit: `2026-01-19T00:05:00+00:00`
Candle OHLC: `{'open': 1.6447, 'high': 1.6522, 'low': 1.6208, 'close': 1.6327}`
Warum: Start-Guard erfüllt → vollständige Short-Neutralisierung
Position vorher: `{'long_qty': 296.365, 'long_avg': 1.864531340748192, 'short_qty': 197.59699999999998, 'short_avg': 1.864561269615919, 'net_qty': 98.76800000000003}`
Order: `research-neutralization-short-001` market short qty=98.76800000000003
Fill: `1.6447` notional=162.44372960000004 fee=0.08934405128000003
Position danach: `{'long_qty': 296.365, 'long_avg': 1.864531340748192, 'short_qty': 296.365, 'short_avg': 1.791289264225859, 'net_qty': 0.0}`
Average-Veränderung: short_avg → `1.791289264225859`
Ökonomische Wirkung: locked spread via post-neutralization short avg; explizite Fee=0.08934405128000003 (nicht im Engine-Ledger)
Nächster Schritt: CoberturaEngine seed qty-neutral, WAITING_MOVE
Audit-Ergebnis: PASS

### Event 003 — Fill overlay_short_add

Zeit: `2026-01-20T16:45:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`1.546`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O1` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.546` filled=`1.546` notional=`183.272116`
Gebühren: open=`0.10079966380000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.546; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 1.546; total_short: 1.791289264225859 → 1.7212066173041851
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`0.0`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 004 — Fill overlay_be_close

Zeit: `2026-01-20T16:50:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_be_close` (level=`None`, trigger=`1.5443`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.546; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O2` purpose=`shared_overlay_be_close` side=`buy` qty=`118.546`
Fill: raw=`1.5443` filled=`1.5443` notional=`183.0705878`
Gebühren: open=`0.0` close=`0.10068882329`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 1.546 → 0.0; total_short: 1.7212066173041851 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`0.20152820000000413` net_realized=`0.10083937671000412` cum_overlay=`0.20152820000000413`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 005 — Fill overlay_short_add

Zeit: `2026-01-25T19:50:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`1.4516`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O3` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.4516` filled=`1.4516` notional=`172.0813736`
Gebühren: open=`0.09464475548000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.4516; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 1.4516; total_short: 1.791289264225859 → 1.6942351887327567
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`0.20152820000000413`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 006 — Fill overlay_be_close

Zeit: `2026-01-25T19:55:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_be_close` (level=`None`, trigger=`1.4500000000000002`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.4516; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O4` purpose=`shared_overlay_be_close` side=`buy` qty=`118.546`
Fill: raw=`1.4500000000000002` filled=`1.4500000000000002` notional=`171.89170000000004`
Gebühren: open=`0.0` close=`0.09454043500000003`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 1.4516 → 0.0; total_short: 1.6942351887327567 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`0.18967359999997913` net_realized=`0.09513316499997909` cum_overlay=`0.3912017999999833`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 007 — Fill overlay_short_add

Zeit: `2026-01-31T11:40:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`1.363`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O5` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.363` filled=`1.363` notional=`161.57819800000001`
Gebühren: open=`0.08886800890000002` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.363; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 1.363; total_short: 1.791289264225859 → 1.668920903018471
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`0.3912017999999833`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 008 — Fill overlay_be_close

Zeit: `2026-01-31T11:45:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_be_close` (level=`None`, trigger=`1.3615000000000002`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.363; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O6` purpose=`shared_overlay_be_close` side=`buy` qty=`118.546`
Fill: raw=`1.3615000000000002` filled=`1.3615000000000002` notional=`161.40037900000002`
Gebühren: open=`0.0` close=`0.08877020845000001`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 1.363 → 0.0; total_short: 1.668920903018471 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`0.17781899999998044` net_realized=`0.08904879154998042` cum_overlay=`0.5690207999999637`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 009 — Fill overlay_short_add

Zeit: `2026-01-31T14:35:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`1.2798`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O7` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.2798` filled=`1.2798` notional=`151.7151708`
Gebühren: open=`0.08344334394000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.2798; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 1.2798; total_short: 1.791289264225859 → 1.6451494744470423
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`0.5690207999999637`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 010 — Fill overlay_be_close

Zeit: `2026-01-31T14:40:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_be_close` (level=`None`, trigger=`1.2783`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.2798; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O8` purpose=`shared_overlay_be_close` side=`buy` qty=`118.546`
Fill: raw=`1.2783` filled=`1.2783` notional=`151.5373518`
Gebühren: open=`0.0` close=`0.08334554349000001`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 1.2798 → 0.0; total_short: 1.6451494744470423 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`0.17781900000000675` net_realized=`0.09447345651000674` cum_overlay=`0.7468397999999704`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 011 — Fill overlay_short_add

Zeit: `2026-01-31T17:10:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`1.2016`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O9` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.2016` filled=`1.2016` notional=`142.4448736`
Gebühren: open=`0.07834468048` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.2016; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 1.2016; total_short: 1.791289264225859 → 1.622806617304185
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`0.7468397999999704`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 012 — Fill overlay_be_close

Zeit: `2026-01-31T17:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_be_close` (level=`None`, trigger=`1.2002000000000002`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.2016; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O10` purpose=`shared_overlay_be_close` side=`buy` qty=`118.546`
Fill: raw=`1.2002000000000002` filled=`1.2002000000000002` notional=`142.27890920000002`
Gebühren: open=`0.0` close=`0.07825340006000002`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 1.2016 → 0.0; total_short: 1.622806617304185 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`0.16596439999998172` net_realized=`0.0877109999399817` cum_overlay=`0.9128041999999521`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 013 — Fill overlay_short_add

Zeit: `2026-02-05T15:10:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`1.1282`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O11` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.1282` filled=`1.1282` notional=`133.7435972`
Gebühren: open=`0.07355897846000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.1282; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 1.1282; total_short: 1.791289264225859 → 1.6018351887327569
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`0.9128041999999521`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 014 — Fill overlay_short_add

Zeit: `2026-02-05T15:10:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`1`, trigger=`1.1162`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.1282; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O12` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.1162` filled=`1.1162` notional=`132.32104520000001`
Gebühren: open=`0.07277657486000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=237.092@1.1222; net=-237.09199999999998
Average-Veränderung overlay_short: 1.1282 → 1.1222; total_short: 1.6018351887327569 → 1.4939162579032552
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`0.9128041999999521`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 015 — Fill overlay_be_close

Zeit: `2026-02-05T15:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_be_close` (level=`None`, trigger=`1.1209`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=237.092@1.1222; net=-237.09199999999998
Order: `shared_be_t1_6pct_winner-O13` purpose=`shared_overlay_be_close` side=`buy` qty=`237.092`
Fill: raw=`1.1209` filled=`1.1209` notional=`265.7564228`
Gebühren: open=`0.0` close=`0.14616603254000002`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 1.1222 → 0.0; total_short: 1.4939162579032552 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`0.3082196000000187` net_realized=`0.16205356746001867` cum_overlay=`1.221023799999971`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 016 — Fill overlay_short_add

Zeit: `2026-02-05T20:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`1.0536`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O14` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`1.0536` filled=`1.0536` notional=`124.90006560000002`
Gebühren: open=`0.06869503608000002` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.0536; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 1.0536; total_short: 1.791289264225859 → 1.580520903018471
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.221023799999971`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 017 — Fill overlay_be_close

Zeit: `2026-02-05T20:20:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_be_close` (level=`None`, trigger=`1.0524`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@1.0536; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O15` purpose=`shared_overlay_be_close` side=`buy` qty=`118.546`
Fill: raw=`1.0524` filled=`1.0524` notional=`124.75781040000001`
Gebühren: open=`0.0` close=`0.06861679572000001`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 1.0536 → 0.0; total_short: 1.580520903018471 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`0.14225520000001066` net_realized=`0.07363840428001064` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 018 — Fill overlay_short_add

Zeit: `2026-02-06T00:10:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`0`, trigger=`0.9893000000000001`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O16` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9893000000000001` filled=`0.9893000000000001` notional=`117.27755780000001`
Gebühren: open=`0.06450265679000002` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@0.9893000000000001; net=-118.54599999999999
Average-Veränderung overlay_short: 0.0 → 0.9893000000000001; total_short: 1.791289264225859 → 1.5621494744470426
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 019 — Fill overlay_short_add

Zeit: `2026-02-06T00:10:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`1`, trigger=`0.9787`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=118.546@0.9893000000000001; net=-118.54599999999999
Order: `shared_be_t1_6pct_winner-O17` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9787` filled=`0.9787` notional=`116.02097020000001`
Gebühren: open=`0.06381153361000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=237.092@0.9840000000000001; net=-237.09199999999998
Average-Veränderung overlay_short: 0.9893000000000001 → 0.9840000000000001; total_short: 1.5621494744470426 → 1.432494035681033
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 020 — Fill overlay_short_add

Zeit: `2026-02-06T00:10:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`2`, trigger=`0.9682000000000001`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=237.092@0.9840000000000001; net=-237.09199999999998
Order: `shared_be_t1_6pct_winner-O18` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9682000000000001` filled=`0.9682000000000001` notional=`114.77623720000001`
Gebühren: open=`0.06312693046000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=355.63800000000003@0.9787333333333335; net=-355.63800000000003
Average-Veränderung overlay_short: 0.9840000000000001 → 0.9787333333333335; total_short: 1.432494035681033 → 1.3480769382844815
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 021 — Fill overlay_short_add

Zeit: `2026-02-06T00:10:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`3`, trigger=`0.9577`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=355.63800000000003@0.9787333333333335; net=-355.63800000000003
Order: `shared_be_t1_6pct_winner-O19` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9577` filled=`0.9577` notional=`113.5315042`
Gebühren: open=`0.062442327310000004` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=474.184@0.9734750000000001; net=-474.18399999999997
Average-Veränderung overlay_short: 0.9787333333333335 → 0.9734750000000001; total_short: 1.3480769382844815 → 1.2880189477791768
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 022 — Fill overlay_short_add

Zeit: `2026-02-06T00:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`4`, trigger=`0.9472`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=474.184@0.9734750000000001; net=-474.18399999999997
Order: `shared_be_t1_6pct_winner-O20` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9472` filled=`0.9472` notional=`112.28677120000002`
Gebühren: open=`0.06175772416000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=592.73@0.9682200000000001; net=-592.73
Average-Veränderung overlay_short: 0.9734750000000001 → 0.9682200000000001; total_short: 1.2880189477791768 → 1.2425764214086197
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 023 — Fill overlay_short_add

Zeit: `2026-02-06T00:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`5`, trigger=`0.9366000000000001`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=592.73@0.9682200000000001; net=-592.73
Order: `shared_be_t1_6pct_winner-O21` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9366000000000001` filled=`0.9366000000000001` notional=`111.03018360000002`
Gebühren: open=`0.06106660098000001` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=711.2760000000001@0.96295; net=-711.2760000000001
Average-Veränderung overlay_short: 0.9682200000000001 → 0.96295; total_short: 1.2425764214086197 → 1.2065791953605467
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 024 — Fill overlay_short_add

Zeit: `2026-02-06T00:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`6`, trigger=`0.9261`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=711.2760000000001@0.96295; net=-711.2760000000001
Order: `shared_be_t1_6pct_winner-O22` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9261` filled=`0.9261` notional=`109.7854506`
Gebühren: open=`0.060381997830000006` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=829.8220000000001@0.9576857142857143; net=-829.8220000000001
Average-Veränderung overlay_short: 0.96295 → 0.9576857142857143; total_short: 1.2065791953605467 → 1.1770550695331208
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 025 — Fill overlay_short_add

Zeit: `2026-02-06T00:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `overlay_short_add` (level=`7`, trigger=`0.9156000000000001`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=829.8220000000001@0.9576857142857143; net=-829.8220000000001
Order: `shared_be_t1_6pct_winner-O23` purpose=`overlay_short_add` side=`short` qty=`118.546`
Fill: raw=`0.9156000000000001` filled=`0.9156000000000001` notional=`108.54071760000002`
Gebühren: open=`0.059697394680000015` close=`0.0`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=948.3680000000002@0.9524249999999999; net=-948.3680000000002
Average-Veränderung overlay_short: 0.9576857142857143 → 0.9524249999999999; total_short: 1.1770550695331208 → 1.1521545867204426
Ökonomische Wirkung: gross_realized=`0.0` net_realized=`0.0` cum_overlay=`1.3632789999999815`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 026 — Fill full_exit

Zeit: `2026-02-06T00:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `full_exit` (level=`None`, trigger=`0.9052`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=948.3680000000002@0.9524249999999999; net=-948.3680000000002
Order: `shared_be_t1_6pct_winner-O24` purpose=`full_exit_overlay_short` side=`buy` qty=`948.3680000000002`
Fill: raw=`0.9052` filled=`0.9052` notional=`858.4627136000001`
Gebühren: open=`0.0` close=`0.47215449248000013`
Position danach: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 0.9524249999999999 → 0.0; total_short: 1.1521545867204426 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`44.78667879999987` net_realized=`44.31452430751987` cum_overlay=`46.149957799999854`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 027 — Fill full_exit

Zeit: `2026-02-06T00:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `full_exit` (level=`None`, trigger=`0.9052`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=296.365@1.864531340748192; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=0.0
Order: `shared_be_t1_6pct_winner-O25` purpose=`full_exit_core_long` side=`sell` qty=`296.365`
Fill: raw=`0.9052` filled=`0.9052` notional=`268.26959800000003`
Gebühren: open=`0.0` close=`0.14754827890000002`
Position danach: core_long=0.0@0.0; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=-296.365
Average-Veränderung overlay_short: 0.0 → 0.0; total_short: 1.791289264225859 → 1.791289264225859
Ökonomische Wirkung: gross_realized=`-284.31223280083793` net_realized=`-284.4597810797379` cum_overlay=`46.149957799999854`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 028 — Fill full_exit

Zeit: `2026-02-06T00:15:00+00:00`
Warum wurde die Order erzeugt? Engine-Trigger `full_exit` (level=`None`, trigger=`0.9052`)
Welche Informationen waren bekannt? Prior-bar levels / active BE; fill at slipped trigger, not candle extreme as opportunistic price.
Position vorher: core_long=0.0@0.0; core_short=296.365@1.791289264225859; overlay_short=0.0@0.0; net=-296.365
Order: `shared_be_t1_6pct_winner-O26` purpose=`full_exit_core_short` side=`buy` qty=`296.365`
Fill: raw=`0.9052` filled=`0.9052` notional=`268.26959800000003`
Gebühren: open=`0.0` close=`0.14754827890000002`
Position danach: core_long=0.0@0.0; core_short=0.0@0.0; overlay_short=0.0@0.0; net=0.0
Average-Veränderung overlay_short: 0.0 → 0.0; total_short: 1.791289264225859 → 0.0
Ökonomische Wirkung: gross_realized=`262.60584479229675` net_realized=`262.45829651339676` cum_overlay=`46.149957799999854`
Nächster erwarteter Schritt: siehe chronologische Folge
Audit-Ergebnis: PASS (shadow fill_ledger)

### Event 029 — Order event round_armed

Zeit: `2026-01-20T08:10:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.6447, "round": 1, "timestamp": "2026-01-20T08:10:00+00:00"}`

### Event 030 — Order event overlay_short_add_order

Zeit: `2026-01-20T16:45:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-01-20T16:45:00+00:00", "trigger": 1.546}`

### Event 031 — Order event round_armed

Zeit: `2026-01-25T19:20:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.5443, "round": 2, "timestamp": "2026-01-25T19:20:00+00:00"}`

### Event 032 — Order event overlay_short_add_order

Zeit: `2026-01-25T19:50:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-01-25T19:50:00+00:00", "trigger": 1.4516}`

### Event 033 — Order event round_armed

Zeit: `2026-01-31T08:40:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.4500000000000002, "round": 3, "timestamp": "2026-01-31T08:40:00+00:00"}`

### Event 034 — Order event overlay_short_add_order

Zeit: `2026-01-31T11:40:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-01-31T11:40:00+00:00", "trigger": 1.363}`

### Event 035 — Order event round_armed

Zeit: `2026-01-31T14:25:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.3615000000000002, "round": 4, "timestamp": "2026-01-31T14:25:00+00:00"}`

### Event 036 — Order event overlay_short_add_order

Zeit: `2026-01-31T14:35:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-01-31T14:35:00+00:00", "trigger": 1.2798}`

### Event 037 — Order event round_armed

Zeit: `2026-01-31T17:10:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.2783, "round": 5, "timestamp": "2026-01-31T17:10:00+00:00"}`

### Event 038 — Order event overlay_short_add_order

Zeit: `2026-01-31T17:10:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-01-31T17:10:00+00:00", "trigger": 1.2016}`

### Event 039 — Order event round_armed

Zeit: `2026-01-31T18:40:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.2002000000000002, "round": 6, "timestamp": "2026-01-31T18:40:00+00:00"}`

### Event 040 — Order event overlay_short_add_order

Zeit: `2026-02-05T15:10:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-05T15:10:00+00:00", "trigger": 1.1282}`

### Event 041 — Order event overlay_short_add_order

Zeit: `2026-02-05T15:10:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 1, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-05T15:10:00+00:00", "trigger": 1.1162}`

### Event 042 — Order event round_armed

Zeit: `2026-02-05T20:15:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.1209, "round": 7, "timestamp": "2026-02-05T20:15:00+00:00"}`

### Event 043 — Order event overlay_short_add_order

Zeit: `2026-02-05T20:15:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-05T20:15:00+00:00", "trigger": 1.0536}`

### Event 044 — Order event round_armed

Zeit: `2026-02-06T00:05:00+00:00`
Payload: `{"direction": "short", "event": "round_armed", "overlay_exit_policy": "shared_be", "recovery_reference_price": 1.0524, "round": 8, "timestamp": "2026-02-06T00:05:00+00:00"}`

### Event 045 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:10:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 0, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:10:00+00:00", "trigger": 0.9893000000000001}`

### Event 046 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:10:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 1, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:10:00+00:00", "trigger": 0.9787}`

### Event 047 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:10:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 2, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:10:00+00:00", "trigger": 0.9682000000000001}`

### Event 048 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:10:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 3, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:10:00+00:00", "trigger": 0.9577}`

### Event 049 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:15:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 4, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:15:00+00:00", "trigger": 0.9472}`

### Event 050 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:15:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 5, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:15:00+00:00", "trigger": 0.9366000000000001}`

### Event 051 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:15:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 6, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:15:00+00:00", "trigger": 0.9261}`

### Event 052 — Order event overlay_short_add_order

Zeit: `2026-02-06T00:15:00+00:00`
Payload: `{"configured_qty": 118.546, "event": "overlay_short_add_order", "level": 7, "post_add_action": "fill", "qty": 118.546, "timestamp": "2026-02-06T00:15:00+00:00", "trigger": 0.9156000000000001}`

### Event 053 — Order event full_exit

Zeit: `2026-02-06T00:15:00+00:00`
Payload: `{"estimated_exit_slippage_pre": 0.0, "estimated_remaining_close_fees_pre": 0.7672510502800002, "event": "full_exit", "reason": "recovered_profit", "timestamp": "2026-02-06T00:15:00+00:00", "total_exit_economics_pre": 21.858019294808667}`

Total walkthrough events written: 53

