# Manual Review — individual_tp_scaled

Chronologische, menschenlesbare Darstellung aller relevanten Orders und Fills aus dem bestehenden Full-Order-Audit (keine neue Simulation).

### Fill 1 – Core Long geöffnet

- Zeit: `2026-01-19T03:55:00+00:00`
- Candle: O=None H=None L=None C=None (index=0)
- Aktion: `CORE_LONG_OPEN` / `seed`
- Menge: `395.153`
- Triggerpreis: `None`
- tatsächlicher Fill-Preis: `1.768355389945979`
- Fee: `0.0` (rate=0.0)
- Position vorher: long=0.0 @ 0.0; short=0.0 @ 0.0; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=0.0 @ 0.0; overlay_short=0.0
- Average vorher: long=0.0 short=0.0
- Average danach: long=1.768355389945979 short=0.0
- realisierter PnL: gross=`0.0` net=`0.0`
- gesamte Economics danach: `-28.309310161323406`
- Grund für die Aktion: Config-seeded qty-neutral core long at audit start
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `seed_at_start`

### Fill 2 – Core Short geöffnet

- Zeit: `2026-01-19T03:55:00+00:00`
- Candle: O=None H=None L=None C=None (index=0)
- Aktion: `CORE_SHORT_OPEN` / `seed`
- Menge: `395.153`
- Triggerpreis: `None`
- tatsächlicher Fill-Preis: `1.696714`
- Fee: `0.0` (rate=0.0)
- Position vorher: long=395.153 @ 1.768355389945979; short=0.0 @ 0.0; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=0.0
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.0` net=`0.0`
- gesamte Economics danach: `-28.309310161323406`
- Grund für die Aktion: Config-seeded qty-neutral core short at audit start
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `seed_at_start`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-20T16:45:00+00:00`
- Order-ID: `individual_tp_scaled-O1-CREATE`
- Trigger: `1.5469000000000002`
- Qty: `158.061`
- Active-from: `2026-01-20T16:45:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.5469000000000002
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-20T16:45:00+00:00`
- Order-ID: `individual_tp_scaled-O1`
- Trigger: `1.5469000000000002`
- Qty: `158.061`
- Active-from: `2026-01-20T16:45:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-20T16:45:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T1`
- Trigger: `1.5297`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.5297 (optical 1.5314310000000002); active next bar
- Status danach: `pending_next_bar`

### Fill 3 – Short-Add gefüllt

- Zeit: `2026-01-20T16:45:00+00:00`
- Candle: O=1.5533 H=1.5539 L=1.5427 C=1.5486 (index=442)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.5469000000000002`
- tatsächlicher Fill-Preis: `1.5469000000000002`
- Fee: `0.13447750849500004` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6539100386866565; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.6539100386866565
- realisierter PnL: gross=`0.0` net=`-0.13447750849500004`
- gesamte Economics danach: `-28.71249136981838`
- Grund für die Aktion: low<=add_level trigger=1.5469000000000002; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 4 – Scaled-TP Teilfill

- Zeit: `2026-01-20T22:30:00+00:00`
- Candle: O=1.544 H=1.546 L=1.516 C=1.5184 (index=511)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.5297`
- tatsächlicher Fill-Preis: `1.5297`
- Fee: `0.06649070505000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6539100386866565; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=474.184 @ 1.671744894686451; overlay_short=79.031
- Average vorher: long=1.768355389945979 short=1.6539100386866565
- Average danach: long=1.768355389945979 short=1.671744894686451
- realisierter PnL: gross=`1.3593160000000082` net=`1.292825294950008`
- gesamte Economics danach: `-24.898578874868388`
- Grund für die Aktion: low<=active_tp trigger=1.5297 tranche=R1-T1; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-20T22:30:00+00:00`
- Order-ID: `individual_tp_scaled-O3-CREATE`
- Trigger: `1.5304`
- Qty: `158.061`
- Active-from: `2026-01-20T22:30:00+00:00`
- Grund: Add level 1 armed; fills when low <= trigger 1.5304
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-20T22:30:00+00:00`
- Order-ID: `individual_tp_scaled-O3`
- Trigger: `1.5304`
- Qty: `158.061`
- Active-from: `2026-01-20T22:30:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-20T22:30:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T2`
- Trigger: `1.5134`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.5134 (optical 1.515096); active next bar
- Status danach: `pending_next_bar`

### Fill 5 – Short-Add gefüllt

- Zeit: `2026-01-20T22:30:00+00:00`
- Candle: O=1.544 H=1.546 L=1.516 C=1.5184 (index=511)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.5304`
- tatsächlicher Fill-Preis: `1.5304`
- Fee: `0.13304310492000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=474.184 @ 1.671744894686451; overlay_short=79.031
- Position danach: long=395.153 @ 1.768355389945979; short=632.245 @ 1.6364087269049181; overlay_short=237.092
- Average vorher: long=1.768355389945979 short=1.671744894686451
- Average danach: long=1.768355389945979 short=1.6364087269049181
- realisierter PnL: gross=`0.0` net=`-0.13304310492000002`
- gesamte Economics danach: `-23.13488997978835`
- Grund für die Aktion: low<=add_level trigger=1.5304; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 6 – Scaled-TP Teilfill

- Zeit: `2026-01-21T16:55:00+00:00`
- Candle: O=1.5284 H=1.5307 L=1.5105 C=1.5158 (index=732)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.5142`
- tatsächlicher Fill-Preis: `1.5142`
- Fee: `0.03290848715` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=632.245 @ 1.6364087269049181; overlay_short=237.092
- Position danach: long=395.153 @ 1.768355389945979; short=592.73 @ 1.6431092506290237; overlay_short=197.577
- Average vorher: long=1.768355389945979 short=1.6364087269049181
- Average danach: long=1.768355389945979 short=1.6431092506290237
- realisierter PnL: gross=`0.8574764166589419` net=`0.8245679295089419`
- gesamte Economics danach: `-22.488135266938365`
- Grund für die Aktion: low<=active_tp trigger=1.5142 tranche=R1-T1; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 7 – Scaled-TP Teilfill

- Zeit: `2026-01-21T16:55:00+00:00`
- Candle: O=1.5284 H=1.5307 L=1.5105 C=1.5158 (index=732)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.5134`
- tatsächlicher Fill-Preis: `1.5134`
- Fee: `0.0657822011` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=592.73 @ 1.6431092506290237; overlay_short=197.577
- Position danach: long=395.153 @ 1.768355389945979; short=513.7 @ 1.6596028173876254; overlay_short=118.547
- Average vorher: long=1.768355389945979 short=1.6431092506290237
- Average danach: long=1.768355389945979 short=1.6596028173876254
- realisierter PnL: gross=`1.778176833317877` net=`1.712394632217877`
- gesamte Economics danach: `-22.36424546803837`
- Grund für die Aktion: low<=active_tp trigger=1.5134 tranche=R1-T2; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-21T16:55:00+00:00`
- Order-ID: `individual_tp_scaled-O6-CREATE`
- Trigger: `1.514`
- Qty: `158.061`
- Active-from: `2026-01-21T16:55:00+00:00`
- Grund: Add level 2 armed; fills when low <= trigger 1.514
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-21T16:55:00+00:00`
- Order-ID: `individual_tp_scaled-O6`
- Trigger: `1.514`
- Qty: `158.061`
- Active-from: `2026-01-21T16:55:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-21T16:55:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T3`
- Trigger: `1.4972`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4972 (optical 1.49886); active next bar
- Status danach: `pending_next_bar`

### Fill 8 – Short-Add gefüllt

- Zeit: `2026-01-21T16:55:00+00:00`
- Candle: O=1.5284 H=1.5307 L=1.5105 C=1.5158 (index=732)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.514`
- tatsächlicher Fill-Preis: `1.514`
- Fee: `0.13161739470000003` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=513.7 @ 1.6596028173876254; overlay_short=118.547
- Position danach: long=395.153 @ 1.768355389945979; short=671.761 @ 1.6253434201926331; overlay_short=276.608
- Average vorher: long=1.768355389945979 short=1.6596028173876254
- Average danach: long=1.768355389945979 short=1.6253434201926331
- realisierter PnL: gross=`0.0` net=`-0.13161739470000003`
- gesamte Economics danach: `-22.78037266273837`
- Grund für die Aktion: low<=add_level trigger=1.514; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 9 – Scaled-TP Teilfill

- Zeit: `2026-01-25T16:15:00+00:00`
- Candle: O=1.5032 H=1.5054 L=1.4846 C=1.4935 (index=1876)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4988000000000001`
- tatsächlicher Fill-Preis: `1.4988000000000001`
- Fee: `0.032573795100000004` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=671.761 @ 1.6253434201926331; overlay_short=276.608
- Position danach: long=395.153 @ 1.768355389945979; short=632.2460000000001 @ 1.6317157121824375; overlay_short=237.09300000000002
- Average vorher: long=1.768355389945979 short=1.6253434201926331
- Average danach: long=1.768355389945979 short=1.6317157121824375
- realisierter PnL: gross=`0.9715071275258352` net=`0.9389333324258352`
- gesamte Economics danach: `-16.854017557838375`
- Grund für die Aktion: low<=active_tp trigger=1.4988000000000001 tranche=R1-T1; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 10 – Scaled-TP Teilfill

- Zeit: `2026-01-25T16:15:00+00:00`
- Candle: O=1.5032 H=1.5054 L=1.4846 C=1.4935 (index=1876)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4981`
- tatsächlicher Fill-Preis: `1.4981`
- Fee: `0.032558581825` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=632.2460000000001 @ 1.6317157121824375; overlay_short=237.09300000000002
- Position danach: long=395.153 @ 1.768355389945979; short=592.731 @ 1.638937634503631; overlay_short=197.57800000000003
- Average vorher: long=1.768355389945979 short=1.6317157121824375
- Average danach: long=1.768355389945979 short=1.638937634503631
- realisierter PnL: gross=`0.999167627525841` net=`0.9666090457008409`
- gesamte Economics danach: `-17.06834513966337`
- Grund für die Aktion: low<=active_tp trigger=1.4981 tranche=R1-T2; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 11 – Scaled-TP Teilfill

- Zeit: `2026-01-25T16:15:00+00:00`
- Candle: O=1.5032 H=1.5054 L=1.4846 C=1.4935 (index=1876)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.4972`
- tatsächlicher Fill-Preis: `1.4972`
- Fee: `0.06507804380000001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=592.731 @ 1.638937634503631; overlay_short=197.57800000000003
- Position danach: long=395.153 @ 1.768355389945979; short=513.701 @ 1.6567146351319542; overlay_short=118.54800000000003
- Average vorher: long=1.768355389945979 short=1.638937634503631
- Average danach: long=1.768355389945979 short=1.6567146351319542
- realisierter PnL: gross=`2.069462255051674` net=`2.0043842112516743`
- gesamte Economics danach: `-17.425834183463373`
- Grund für die Aktion: low<=active_tp trigger=1.4972 tranche=R1-T3; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T16:15:00+00:00`
- Order-ID: `individual_tp_scaled-O10-CREATE`
- Trigger: `1.4975`
- Qty: `158.061`
- Active-from: `2026-01-25T16:15:00+00:00`
- Grund: Add level 3 armed; fills when low <= trigger 1.4975
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T16:15:00+00:00`
- Order-ID: `individual_tp_scaled-O10`
- Trigger: `1.4975`
- Qty: `158.061`
- Active-from: `2026-01-25T16:15:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T16:15:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T4`
- Trigger: `1.4808000000000001`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4808000000000001 (optical 1.482525); active next bar
- Status danach: `pending_next_bar`

### Fill 12 – Short-Add gefüllt

- Zeit: `2026-01-25T16:15:00+00:00`
- Candle: O=1.5032 H=1.5054 L=1.4846 C=1.4935 (index=1876)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4975`
- tatsächlicher Fill-Preis: `1.4975`
- Fee: `0.13018299112500004` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=513.701 @ 1.6567146351319542; overlay_short=118.54800000000003
- Position danach: long=395.153 @ 1.768355389945979; short=671.7620000000001 @ 1.6192525214018059; overlay_short=276.60900000000004
- Average vorher: long=1.768355389945979 short=1.6567146351319542
- Average danach: long=1.768355389945979 short=1.6192525214018059
- realisierter PnL: gross=`0.0` net=`-0.13018299112500004`
- gesamte Economics danach: `-16.92377317458837`
- Grund für die Aktion: low<=add_level trigger=1.4975; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 13 – Scaled-TP Teilfill

- Zeit: `2026-01-25T18:10:00+00:00`
- Candle: O=1.4976 H=1.4988 L=1.4801 C=1.4945 (index=1899)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4828000000000001`
- tatsächlicher Fill-Preis: `1.4828000000000001`
- Fee: `0.03222606310000001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=671.7620000000001 @ 1.6192525214018059; overlay_short=276.60900000000004
- Position danach: long=395.153 @ 1.768355389945979; short=632.2470000000001 @ 1.6261686007682858; overlay_short=237.09400000000005
- Average vorher: long=1.768355389945979 short=1.6192525214018059
- Average danach: long=1.768355389945979 short=1.6261686007682858
- realisierter PnL: gross=`1.0192509519734816` net=`0.9870248888734816`
- gesamte Economics danach: `-16.770282737688326`
- Grund für die Aktion: low<=active_tp trigger=1.4828000000000001 tranche=R1-T2; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 14 – Scaled-TP Teilfill

- Zeit: `2026-01-25T18:10:00+00:00`
- Candle: O=1.4976 H=1.4988 L=1.4801 C=1.4945 (index=1899)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.482`
- tatsächlicher Fill-Preis: `1.482`
- Fee: `0.032208676500000005` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=632.2470000000001 @ 1.6261686007682858; overlay_short=237.09400000000005
- Position danach: long=395.153 @ 1.768355389945979; short=592.7320000000001 @ 1.6340068131600334; overlay_short=197.57900000000006
- Average vorher: long=1.768355389945979 short=1.6261686007682858
- Average danach: long=1.768355389945979 short=1.6340068131600334
- realisierter PnL: gross=`1.050862951973487` net=`1.0186542754734869`
- gesamte Economics danach: `-16.30855391418833`
- Grund für die Aktion: low<=active_tp trigger=1.482 tranche=R1-T3; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 15 – Scaled-TP Teilfill

- Zeit: `2026-01-25T18:10:00+00:00`
- Candle: O=1.4976 H=1.4988 L=1.4801 C=1.4945 (index=1899)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.4808000000000001`
- tatsächlicher Fill-Preis: `1.4808000000000001`
- Fee: `0.06436519320000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=592.7320000000001 @ 1.6340068131600334; overlay_short=197.57900000000006
- Position danach: long=395.153 @ 1.768355389945979; short=513.7020000000001 @ 1.6533008251360244; overlay_short=118.54900000000006
- Average vorher: long=1.768355389945979 short=1.6340068131600334
- Average danach: long=1.768355389945979 short=1.6533008251360244
- realisierter PnL: gross=`2.1965619039469635` net=`2.1321967107469635`
- gesamte Economics danach: `-15.290208107388343`
- Grund für die Aktion: low<=active_tp trigger=1.4808000000000001 tranche=R1-T4; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T18:10:00+00:00`
- Order-ID: `individual_tp_scaled-O14-CREATE`
- Trigger: `1.481`
- Qty: `158.061`
- Active-from: `2026-01-25T18:10:00+00:00`
- Grund: Add level 4 armed; fills when low <= trigger 1.481
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T18:10:00+00:00`
- Order-ID: `individual_tp_scaled-O14`
- Trigger: `1.481`
- Qty: `158.061`
- Active-from: `2026-01-25T18:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T18:10:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T5`
- Trigger: `1.4645000000000001`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4645000000000001 (optical 1.46619); active next bar
- Status danach: `pending_next_bar`

### Fill 16 – Short-Add gefüllt

- Zeit: `2026-01-25T18:10:00+00:00`
- Candle: O=1.4976 H=1.4988 L=1.4801 C=1.4945 (index=1899)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.481`
- tatsächlicher Fill-Preis: `1.481`
- Fee: `0.12874858755000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=513.7020000000001 @ 1.6533008251360244; overlay_short=118.54900000000006
- Position danach: long=395.153 @ 1.768355389945979; short=671.7630000000001 @ 1.612759680830927; overlay_short=276.61000000000007
- Average vorher: long=1.768355389945979 short=1.6533008251360244
- Average danach: long=1.768355389945979 short=1.612759680830927
- realisierter PnL: gross=`0.0` net=`-0.12874858755000002`
- gesamte Economics danach: `-17.552780194938297`
- Grund für die Aktion: low<=add_level trigger=1.481; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 17 – Scaled-TP Teilfill

- Zeit: `2026-01-25T19:20:00+00:00`
- Candle: O=1.4756 H=1.4784 L=1.4662 C=1.4713 (index=1913)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4669`
- tatsächlicher Fill-Preis: `1.4669`
- Fee: `0.031880504425000004` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=671.7630000000001 @ 1.612759680830927; overlay_short=276.61000000000007
- Position danach: long=395.153 @ 1.768355389945979; short=632.248 @ 1.6202554287506394; overlay_short=237.09500000000008
- Average vorher: long=1.768355389945979 short=1.612759680830927
- Average danach: long=1.768355389945979 short=1.6202554287506394
- realisierter PnL: gross=`1.024473657291874` net=`0.992593152866874`
- gesamte Economics danach: `-10.993442699363326`
- Grund für die Aktion: low<=active_tp trigger=1.4669 tranche=R1-T3; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 18 – Scaled-TP Teilfill

- Zeit: `2026-01-25T19:25:00+00:00`
- Candle: O=1.4713 H=1.4713 L=1.4588 C=1.4626 (index=1914)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4659`
- tatsächlicher Fill-Preis: `1.4659`
- Fee: `0.031858771175` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=632.248 @ 1.6202554287506394; overlay_short=237.09500000000008
- Position danach: long=395.153 @ 1.768355389945979; short=592.7330000000002 @ 1.6287505962371627; overlay_short=197.5800000000001
- Average vorher: long=1.768355389945979 short=1.6202554287506394
- Average danach: long=1.768355389945979 short=1.6287505962371627
- realisierter PnL: gross=`1.0639886572918784` net=`1.0321298861168784`
- gesamte Economics danach: `-9.09297447053831`
- Grund für die Aktion: low<=active_tp trigger=1.4659 tranche=R1-T4; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 19 – Scaled-TP Teilfill

- Zeit: `2026-01-25T19:25:00+00:00`
- Candle: O=1.4713 H=1.4713 L=1.4588 C=1.4626 (index=1914)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.4645000000000001`
- tatsächlicher Fill-Preis: `1.4645000000000001`
- Fee: `0.06365668925000001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=592.7330000000002 @ 1.6287505962371627; overlay_short=197.5800000000001
- Position danach: long=395.153 @ 1.768355389945979; short=513.7030000000001 @ 1.6496617166823213; overlay_short=118.5500000000001
- Average vorher: long=1.768355389945979 short=1.6287505962371627
- Average danach: long=1.768355389945979 short=1.6496617166823213
- realisierter PnL: gross=`2.2386193145837447` net=`2.1749626253337446`
- gesamte Economics danach: `-9.306788159788326`
- Grund für die Aktion: low<=active_tp trigger=1.4645000000000001 tranche=R1-T5; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T19:25:00+00:00`
- Order-ID: `individual_tp_scaled-O18-CREATE`
- Trigger: `1.4646000000000001`
- Qty: `158.061`
- Active-from: `2026-01-25T19:25:00+00:00`
- Grund: Add level 5 armed; fills when low <= trigger 1.4646000000000001
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T19:25:00+00:00`
- Order-ID: `individual_tp_scaled-O18`
- Trigger: `1.4646000000000001`
- Qty: `158.061`
- Active-from: `2026-01-25T19:25:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T19:25:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T6`
- Trigger: `1.4483000000000001`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4483000000000001 (optical 1.4499540000000002); active next bar
- Status danach: `pending_next_bar`

### Fill 20 – Short-Add gefüllt

- Zeit: `2026-01-25T19:25:00+00:00`
- Candle: O=1.4713 H=1.4713 L=1.4588 C=1.4626 (index=1914)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4646000000000001`
- tatsächlicher Fill-Preis: `1.4646000000000001`
- Fee: `0.12732287733000003` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=513.7030000000001 @ 1.6496617166823213; overlay_short=118.5500000000001
- Position danach: long=395.153 @ 1.768355389945979; short=671.7640000000001 @ 1.606118091241654; overlay_short=276.6110000000001
- Average vorher: long=1.768355389945979 short=1.6496617166823213
- Average danach: long=1.768355389945979 short=1.606118091241654
- realisierter PnL: gross=`0.0` net=`-0.12732287733000003`
- gesamte Economics danach: `-9.11798903711834`
- Grund für die Aktion: low<=add_level trigger=1.4646000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 21 – Scaled-TP Teilfill

- Zeit: `2026-01-25T19:50:00+00:00`
- Candle: O=1.4582 H=1.4615 L=1.4466 C=1.4533 (index=1919)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4509`
- tatsächlicher Fill-Preis: `1.4509`
- Fee: `0.03153277242500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=671.7640000000001 @ 1.606118091241654; overlay_short=276.6110000000001
- Position danach: long=395.153 @ 1.768355389945979; short=632.2490000000001 @ 1.6142067824756852; overlay_short=237.09600000000012
- Average vorher: long=1.768355389945979 short=1.606118091241654
- Average danach: long=1.768355389945979 short=1.6142067824756852
- realisierter PnL: gross=`1.019375931389027` net=`0.987843158964027`
- gesamte Economics danach: `-6.482203509543378`
- Grund für die Aktion: low<=active_tp trigger=1.4509 tranche=R1-T4; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 22 – Scaled-TP Teilfill

- Zeit: `2026-01-25T19:50:00+00:00`
- Candle: O=1.4582 H=1.4615 L=1.4466 C=1.4533 (index=1919)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4497`
- tatsächlicher Fill-Preis: `1.4497`
- Fee: `0.031506692525` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=632.2490000000001 @ 1.6142067824756852; overlay_short=237.09600000000012
- Position danach: long=395.153 @ 1.768355389945979; short=592.7340000000002 @ 1.6233739494985617; overlay_short=197.58100000000013
- Average vorher: long=1.768355389945979 short=1.6142067824756852
- Average danach: long=1.768355389945979 short=1.6233739494985617
- realisierter PnL: gross=`1.0667939313890304` net=`1.0352872388640304`
- gesamte Economics danach: `-6.371456202068373`
- Grund für die Aktion: low<=active_tp trigger=1.4497 tranche=R1-T5; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 23 – Scaled-TP Teilfill

- Zeit: `2026-01-25T19:50:00+00:00`
- Candle: O=1.4582 H=1.4615 L=1.4466 C=1.4533 (index=1919)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.4483000000000001`
- tatsächlicher Fill-Preis: `1.4483000000000001`
- Fee: `0.06295253195000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=592.7340000000002 @ 1.6233739494985617; overlay_short=197.58100000000013
- Position danach: long=395.153 @ 1.768355389945979; short=513.7040000000002 @ 1.645939209582371; overlay_short=118.55100000000013
- Average vorher: long=1.768355389945979 short=1.6233739494985617
- Average danach: long=1.768355389945979 short=1.645939209582371
- realisierter PnL: gross=`2.244229862778049` net=`2.1812773308280486`
- gesamte Economics danach: `-6.039258734018382`
- Grund für die Aktion: low<=active_tp trigger=1.4483000000000001 tranche=R1-T6; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `individual_tp_scaled-O22-CREATE`
- Trigger: `1.4481000000000002`
- Qty: `158.061`
- Active-from: `2026-01-25T19:50:00+00:00`
- Grund: Add level 6 armed; fills when low <= trigger 1.4481000000000002
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `individual_tp_scaled-O22`
- Trigger: `1.4481000000000002`
- Qty: `158.061`
- Active-from: `2026-01-25T19:50:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T7`
- Trigger: `1.4320000000000002`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4320000000000002 (optical 1.4336190000000002); active next bar
- Status danach: `pending_next_bar`

### Fill 24 – Short-Add gefüllt

- Zeit: `2026-01-25T19:50:00+00:00`
- Candle: O=1.4582 H=1.4615 L=1.4466 C=1.4533 (index=1919)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4481000000000002`
- tatsächlicher Fill-Preis: `1.4481000000000002`
- Fee: `0.12588847375500004` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=513.7040000000002 @ 1.645939209582371; overlay_short=118.55100000000013
- Position danach: long=395.153 @ 1.768355389945979; short=671.7650000000001 @ 1.599389205777768; overlay_short=276.61200000000014
- Average vorher: long=1.768355389945979 short=1.645939209582371
- Average danach: long=1.768355389945979 short=1.599389205777768
- realisierter PnL: gross=`0.0` net=`-0.12588847375500004`
- gesamte Economics danach: `-6.9870644077733814`
- Grund für die Aktion: low<=add_level trigger=1.4481000000000002; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 25 – Scaled-TP Teilfill

- Zeit: `2026-01-30T01:40:00+00:00`
- Candle: O=1.4888 H=1.4925 L=1.434 C=1.4618 (index=3141)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4349`
- tatsächlicher Fill-Preis: `1.4349`
- Fee: `0.031185040425000005` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=671.7650000000001 @ 1.599389205777768; overlay_short=276.61200000000014
- Position danach: long=395.153 @ 1.768355389945979; short=632.2500000000002 @ 1.6080786281763662; overlay_short=237.09700000000015
- Average vorher: long=1.768355389945979 short=1.599389205777768
- Average danach: long=1.768355389945979 short=1.6080786281763662
- realisierter PnL: gross=`1.0059036547948041` net=`0.9747186143698041`
- gesamte Economics danach: `-8.306497948198363`
- Grund für die Aktion: low<=active_tp trigger=1.4349 tranche=R1-T5; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 26 – Scaled-TP Teilfill

- Zeit: `2026-01-30T06:25:00+00:00`
- Candle: O=1.4423 H=1.4446 L=1.4326 C=1.4341 (index=3198)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4337`
- tatsächlicher Fill-Preis: `1.4337`
- Fee: `0.031158960525` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=632.2500000000002 @ 1.6080786281763662; overlay_short=237.09700000000015
- Position danach: long=395.153 @ 1.768355389945979; short=592.7350000000001 @ 1.6179266206816076; overlay_short=197.58200000000016
- Average vorher: long=1.768355389945979 short=1.6080786281763662
- Average danach: long=1.768355389945979 short=1.6179266206816076
- realisierter PnL: gross=`1.0533216547948077` net=`1.0221626942698077`
- gesamte Economics danach: `-1.7542640087233607`
- Grund für die Aktion: low<=active_tp trigger=1.4337 tranche=R1-T6; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 27 – Scaled-TP Teilfill

- Zeit: `2026-01-30T06:35:00+00:00`
- Candle: O=1.4348 H=1.4349 L=1.4253 C=1.4325 (index=3200)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.4320000000000002`
- tatsächlicher Fill-Preis: `1.4320000000000002`
- Fee: `0.06224402800000001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=592.7350000000001 @ 1.6179266206816076; overlay_short=197.58200000000016
- Position danach: long=395.153 @ 1.768355389945979; short=513.7050000000002 @ 1.6421677445228744; overlay_short=118.55200000000016
- Average vorher: long=1.768355389945979 short=1.6179266206816076
- Average danach: long=1.768355389945979 short=1.6421677445228744
- realisierter PnL: gross=`2.2409943095896008` net=`2.178750281589601`
- gesamte Economics danach: `-1.4608618367233994`
- Grund für die Aktion: low<=active_tp trigger=1.4320000000000002 tranche=R1-T7; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-30T06:35:00+00:00`
- Order-ID: `individual_tp_scaled-O26-CREATE`
- Trigger: `1.4317`
- Qty: `158.061`
- Active-from: `2026-01-30T06:35:00+00:00`
- Grund: Add level 7 armed; fills when low <= trigger 1.4317
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-30T06:35:00+00:00`
- Order-ID: `individual_tp_scaled-O26`
- Trigger: `1.4317`
- Qty: `158.061`
- Active-from: `2026-01-30T06:35:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-30T06:35:00+00:00`
- Order-ID: `individual_tp_scaled-TP-R1-T8`
- Trigger: `1.4158000000000002`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4158000000000002 (optical 1.417383); active next bar
- Status danach: `pending_next_bar`

### Fill 28 – Short-Add gefüllt

- Zeit: `2026-01-30T06:35:00+00:00`
- Candle: O=1.4348 H=1.4349 L=1.4253 C=1.4325 (index=3200)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4317`
- tatsächlicher Fill-Preis: `1.4317`
- Fee: `0.124462763535` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=513.7050000000002 @ 1.6421677445228744; overlay_short=118.55200000000016
- Position danach: long=395.153 @ 1.768355389945979; short=671.7660000000002 @ 1.592646419884488; overlay_short=276.61300000000017
- Average vorher: long=1.768355389945979 short=1.6421677445228744
- Average danach: long=1.768355389945979 short=1.592646419884488
- realisierter PnL: gross=`0.0` net=`-0.124462763535`
- gesamte Economics danach: `-1.711773400258462`
- Grund für die Aktion: low<=add_level trigger=1.4317; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 29 – Scaled-TP Teilfill

- Zeit: `2026-01-30T18:20:00+00:00`
- Candle: O=1.4259 H=1.4266 L=1.4125 C=1.4181 (index=3341)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.419`
- tatsächlicher Fill-Preis: `1.419`
- Fee: `0.030839481750000005` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=671.7660000000002 @ 1.592646419884488; overlay_short=276.61300000000017
- Position danach: long=395.153 @ 1.768355389945979; short=632.2510000000002 @ 1.6019378082654996; overlay_short=237.09800000000018
- Average vorher: long=1.768355389945979 short=1.592646419884488
- Average danach: long=1.768355389945979 short=1.6019378082654996
- realisierter PnL: gross=`0.9871486864526681` net=`0.9563092047026681`
- gesamte Economics danach: `2.205050817991591`
- Grund für die Aktion: low<=active_tp trigger=1.419 tranche=R1-T6; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 30 – Scaled-TP Teilfill

- Zeit: `2026-01-30T18:20:00+00:00`
- Candle: O=1.4259 H=1.4266 L=1.4125 C=1.4181 (index=3341)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `39.515`
- Triggerpreis: `1.4175`
- tatsächlicher Fill-Preis: `1.4175`
- Fee: `0.030806881875` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=632.2510000000002 @ 1.6019378082654996; overlay_short=237.09800000000018
- Position danach: long=395.153 @ 1.768355389945979; short=592.7360000000002 @ 1.6124680254400232; overlay_short=197.5830000000002
- Average vorher: long=1.768355389945979 short=1.6019378082654996
- Average danach: long=1.768355389945979 short=1.6124680254400232
- realisierter PnL: gross=`1.0464211864526702` net=`1.0156143045776702`
- gesamte Economics danach: `2.1979529361165824`
- Grund für die Aktion: low<=active_tp trigger=1.4175 tranche=R1-T7; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 31 – Scaled-TP Teilfill

- Zeit: `2026-01-30T18:20:00+00:00`
- Candle: O=1.4259 H=1.4266 L=1.4125 C=1.4181 (index=3341)
- Aktion: `SCALED_TP_PARTIAL_FILLED` / `overlay_tp_partial`
- Menge: `79.03`
- Triggerpreis: `1.4158000000000002`
- tatsächlicher Fill-Preis: `1.4158000000000002`
- Fee: `0.06153987070000001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=592.7360000000002 @ 1.6124680254400232; overlay_short=197.5830000000002
- Position danach: long=395.153 @ 1.768355389945979; short=513.7060000000002 @ 1.6383884559540132; overlay_short=118.5530000000002
- Average vorher: long=1.768355389945979 short=1.6124680254400232
- Average danach: long=1.768355389945979 short=1.6383884559540132
- realisierter PnL: gross=`2.227193372905326` net=`2.165653502205326`
- gesamte Economics danach: `2.318182065416565`
- Grund für die Aktion: low<=active_tp trigger=1.4158000000000002 tranche=R1-T8; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — FULL_EXIT_GATE_CHECK

- Zeit: `2026-01-30T18:20:00+00:00`
- Order-ID: `None`
- Trigger: `1.4181`
- Qty: `None`
- Active-from: `None`
- Grund: total_exit_economics>=0.24 (target=0.0+buffer=0.25-tol=0.01)
- Status danach: `triggered`

#### Order-Event — FULL_EXIT_TRIGGERED

- Zeit: `2026-01-30T18:20:00+00:00`
- Order-ID: `None`
- Trigger: `None`
- Qty: `None`
- Active-from: `None`
- Grund: Net-BE gate passed; flatten all remaining positions
- Status danach: `executing`

### Fill 32 – Overlay geschlossen (Full Exit)

- Zeit: `2026-01-30T18:20:00+00:00`
- Candle: O=1.4259 H=1.4266 L=1.4125 C=1.4181 (index=3341)
- Aktion: `OVERLAY_CLOSED` / `full_exit_short`
- Menge: `118.5530000000002`
- Triggerpreis: `1.4181`
- tatsächlicher Fill-Preis: `1.4181`
- Fee: `0.09246600511500015` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=513.7060000000002 @ 1.6383884559540132; overlay_short=118.5530000000002
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.6383884559540132
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`3.068343612312386` net=`2.975877607197386`
- gesamte Economics danach: `2.225716060301565`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 33 – Core Long geschlossen (Full Exit)

- Zeit: `2026-01-30T18:20:00+00:00`
- Candle: O=1.4259 H=1.4266 L=1.4125 C=1.4181 (index=3341)
- Aktion: `CORE_LONG_CLOSED` / `full_exit_core_long`
- Menge: `395.153`
- Triggerpreis: `1.4181`
- tatsächlicher Fill-Preis: `1.4181`
- Fee: `0.308201558115` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=0.0 @ 0.0; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=0.0 short=1.696714
- realisierter PnL: gross=`-138.40446810332347` net=`-138.71266966143847`
- gesamte Economics danach: `1.9175145021865632`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 34 – Core Short geschlossen (Full Exit)

- Zeit: `2026-01-30T18:20:00+00:00`
- Candle: O=1.4259 H=1.4266 L=1.4125 C=1.4181 (index=3341)
- Aktion: `CORE_SHORT_CLOSED` / `full_exit_core_short`
- Menge: `395.153`
- Triggerpreis: `1.4181`
- tatsächlicher Fill-Preis: `1.4181`
- Fee: `0.308201558115` (rate=0.00055)
- Position vorher: long=0.0 @ 0.0; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=0.0 @ 0.0; short=0.0 @ 0.0; overlay_short=0.0
- Average vorher: long=0.0 short=1.696714
- Average danach: long=0.0 short=0.0
- realisierter PnL: gross=`110.09515794200006` net=`109.78695638388506`
- gesamte Economics danach: `1.6093129440715634`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 35 – FINAL_FLAT

- Zeit: `2026-01-30T18:20:00+00:00`
- Candle: O=None H=None L=None C=None (index=None)
- Aktion: `FINAL_FLAT` / `RECOVERED_BE`
- Menge: `None`
- Triggerpreis: `None`
- tatsächlicher Fill-Preis: `None`
- Fee: `None` (rate=None)
- Position vorher: long=None @ None; short=None @ None; overlay_short=None
- Position danach: long=0.0 @ None; short=0.0 @ None; overlay_short=0.0
- Average vorher: long=None short=None
- Average danach: long=None short=None
- realisierter PnL: gross=`None` net=`None`
- gesamte Economics danach: `1.6093129440715634`
- Grund für die Aktion: All legs flat after net-BE full exit
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `first_be=2026-01-30T18:20:00+00:00 exit=2026-01-30T18:20:00+00:00 delay=0`

## Tranches

### Tranche `R1-T1`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.5469000000000002` entry_fee: `0.13447750849500004`
- tp_pct: `0.03` tp_trigger: `1.4988000000000001`
- steps_completed: `3`
- closed_qty_from_tp_events: `158.06`
- realized_gross: `3.188299544184785` close_fees: `0.1319729873` net: `2.921849048389785`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T2`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.5304` entry_fee: `0.13304310492000002`
- tp_pct: `0.03` tp_trigger: `1.4828000000000001`
- steps_completed: `3`
- closed_qty_from_tp_events: `158.06`
- realized_gross: `3.7965954128172` close_fees: `0.13056684602500002` net: `3.5329854618721996`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T3`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.514` entry_fee: `0.13161739470000003`
- tp_pct: `0.03` tp_trigger: `1.4669`
- steps_completed: `3`
- closed_qty_from_tp_events: `158.06`
- realized_gross: `4.144798864317035` close_fees: `0.12916722472500003` net: `3.8840142448920343`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T4`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.4975` entry_fee: `0.13018299112500004`
- tp_pct: `0.03` tp_trigger: `1.4509`
- steps_completed: `3`
- closed_qty_from_tp_events: `158.06`
- realized_gross: `4.279926492627869` close_fees: `0.12775673680000005` net: `4.021986764702868`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T5`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.481` entry_fee: `0.12874858755000002`
- tp_pct: `0.03` tp_trigger: `1.4349`
- steps_completed: `3`
- closed_qty_from_tp_events: `158.06`
- realized_gross: `4.311316900767579` close_fees: `0.1263484222` net: `4.056219891017579`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T6`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.4646000000000001` entry_fee: `0.12732287733000003`
- tp_pct: `0.03` tp_trigger: `1.419`
- steps_completed: `3`
- closed_qty_from_tp_events: `158.06`
- realized_gross: `4.284700204025524` close_fees: `0.12495097422500002` net: `4.032426352470524`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T7`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.4481000000000002` entry_fee: `0.12588847375500004`
- tp_pct: `0.03` tp_trigger: `1.403`
- steps_completed: `2`
- closed_qty_from_tp_events: `118.545`
- realized_gross: `3.287415496042271` close_fees: `0.093050909875` net: `3.068476112412271`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T8`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.4317` entry_fee: `0.124462763535`
- tp_pct: `0.02` tp_trigger: `1.4015`
- steps_completed: `1`
- closed_qty_from_tp_events: `79.03`
- realized_gross: `2.227193372905326` close_fees: `0.06153987070000001` net: `2.041190738670326`
- final_status: `closed` qty_sum: `PASS`

## Scaled-TP Stufen (Policy)

- 50% bei 1%
- 25% bei 2%
- 25% bei 3%

Teilfills erscheinen als `SCALED_TP_PARTIAL_FILLED` in der Timeline oben.

## Finaler Netto-BE-Exit

- erste erreichbare BE-Candle: `2026-01-30T18:20:00+00:00`
- tatsächlicher Exit: `2026-01-30T18:20:00+00:00`
- Ziel / Safety-Buffer / Tol: `0.0` / `0.25` / `0.01`
- Economics vor Exit: `1.6093129440715597`
- geschätzte Rest-Close-Fees: `0.7088691213450001`
- geschätzte Exit-Slippage: `0.0`
- Exit-Fill-Preise: `[1.4181, 1.4181, 1.4181]`
- Exit-Fill-Mengen: `[118.5530000000002, 395.153, 395.153]`
- finale Economics: `1.6093129440715634`
- flat: `True` status: `RECOVERED_BE`
- open_tranches_remaining: `0`
- pass_fail: `PASS`
