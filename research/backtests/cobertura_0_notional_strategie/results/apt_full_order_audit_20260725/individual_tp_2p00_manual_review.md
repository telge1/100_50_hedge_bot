# Manual Review — individual_tp_2p00

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
- Order-ID: `individual_tp_2p00-O1-CREATE`
- Trigger: `1.5469000000000002`
- Qty: `158.061`
- Active-from: `2026-01-20T16:45:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.5469000000000002
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-20T16:45:00+00:00`
- Order-ID: `individual_tp_2p00-O1`
- Trigger: `1.5469000000000002`
- Qty: `158.061`
- Active-from: `2026-01-20T16:45:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-20T16:45:00+00:00`
- Order-ID: `individual_tp_2p00-TP-R1-T1`
- Trigger: `1.5142`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.5142 (optical 1.515962); active next bar
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

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-20T22:30:00+00:00`
- Order-ID: `individual_tp_2p00-O2-CREATE`
- Trigger: `1.5304`
- Qty: `158.061`
- Active-from: `2026-01-20T22:30:00+00:00`
- Grund: Add level 1 armed; fills when low <= trigger 1.5304
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-20T22:30:00+00:00`
- Order-ID: `individual_tp_2p00-O2`
- Trigger: `1.5304`
- Qty: `158.061`
- Active-from: `2026-01-20T22:30:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-20T22:30:00+00:00`
- Order-ID: `individual_tp_2p00-TP-R1-T2`
- Trigger: `1.4981`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4981 (optical 1.499792); active next bar
- Status danach: `pending_next_bar`

### Fill 4 – Short-Add gefüllt

- Zeit: `2026-01-20T22:30:00+00:00`
- Candle: O=1.544 H=1.546 L=1.516 C=1.5184 (index=511)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.5304`
- tatsächlicher Fill-Preis: `1.5304`
- Fee: `0.13304310492000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6539100386866565; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6264633827169521; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.6539100386866565
- Average danach: long=1.768355389945979 short=1.6264633827169521
- realisierter PnL: gross=`0.0` net=`-0.13304310492000002`
- gesamte Economics danach: `-22.17536027473838`
- Grund für die Aktion: low<=add_level trigger=1.5304; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 5 – Individual-TP geschlossen

- Zeit: `2026-01-21T16:55:00+00:00`
- Candle: O=1.5284 H=1.5307 L=1.5105 C=1.5158 (index=732)
- Aktion: `INDIVIDUAL_TP_FILLED` / `overlay_tp_close`
- Menge: `158.061`
- Triggerpreis: `1.5142`
- tatsächlicher Fill-Preis: `1.5142`
- Fee: `0.13163478141` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6264633827169521; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6515528979599217; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.6264633827169521
- Average danach: long=1.768355389945979 short=1.6515528979599217
- realisierter PnL: gross=`3.864591450000013` net=`3.7329566685900133`
- gesamte Economics danach: `-21.23218025614839`
- Grund für die Aktion: low<=active_tp trigger=1.5142 tranche=R1-T1; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-21T16:55:00+00:00`
- Order-ID: `individual_tp_2p00-O4-CREATE`
- Trigger: `1.514`
- Qty: `158.061`
- Active-from: `2026-01-21T16:55:00+00:00`
- Grund: Add level 2 armed; fills when low <= trigger 1.514
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-21T16:55:00+00:00`
- Order-ID: `individual_tp_2p00-O4`
- Trigger: `1.514`
- Qty: `158.061`
- Active-from: `2026-01-21T16:55:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-21T16:55:00+00:00`
- Order-ID: `individual_tp_2p00-TP-R1-T3`
- Trigger: `1.482`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.482 (optical 1.48372); active next bar
- Status danach: `pending_next_bar`

### Fill 6 – Short-Add gefüllt

- Zeit: `2026-01-21T16:55:00+00:00`
- Candle: O=1.5284 H=1.5307 L=1.5105 C=1.5158 (index=732)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.514`
- tatsächlicher Fill-Preis: `1.514`
- Fee: `0.13161739470000003` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6515528979599217; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6209856087898493; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.6515528979599217
- Average danach: long=1.768355389945979 short=1.6209856087898493
- realisierter PnL: gross=`0.0` net=`-0.13161739470000003`
- gesamte Economics danach: `-21.648307450848357`
- Grund für die Aktion: low<=add_level trigger=1.514; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 7 – Individual-TP geschlossen

- Zeit: `2026-01-25T16:15:00+00:00`
- Candle: O=1.5032 H=1.5054 L=1.4846 C=1.4935 (index=1876)
- Aktion: `INDIVIDUAL_TP_FILLED` / `overlay_tp_close`
- Menge: `158.061`
- Triggerpreis: `1.4981`
- tatsächlicher Fill-Preis: `1.4981`
- Fee: `0.13023515125500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6209856087898493; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.648031472571193; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.6209856087898493
- Average danach: long=1.768355389945979 short=1.648031472571193
- realisierter PnL: gross=`4.461271725000026` net=`4.331036573745026`
- gesamte Economics danach: `-15.456102602103353`
- Grund für die Aktion: low<=active_tp trigger=1.4981 tranche=R1-T2; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T16:15:00+00:00`
- Order-ID: `individual_tp_2p00-O6-CREATE`
- Trigger: `1.4975`
- Qty: `158.061`
- Active-from: `2026-01-25T16:15:00+00:00`
- Grund: Add level 3 armed; fills when low <= trigger 1.4975
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T16:15:00+00:00`
- Order-ID: `individual_tp_2p00-O6`
- Trigger: `1.4975`
- Qty: `158.061`
- Active-from: `2026-01-25T16:15:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T16:15:00+00:00`
- Order-ID: `individual_tp_2p00-TP-R1-T4`
- Trigger: `1.4659`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4659 (optical 1.4675500000000001); active next bar
- Status danach: `pending_next_bar`

### Fill 8 – Short-Add gefüllt

- Zeit: `2026-01-25T16:15:00+00:00`
- Candle: O=1.5032 H=1.5054 L=1.4846 C=1.4935 (index=1876)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4975`
- tatsächlicher Fill-Preis: `1.4975`
- Fee: `0.13018299112500004` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.648031472571193; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6145800577371623; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.648031472571193
- Average danach: long=1.768355389945979 short=1.6145800577371623
- realisierter PnL: gross=`0.0` net=`-0.13018299112500004`
- gesamte Economics danach: `-14.954041593228318`
- Grund für die Aktion: low<=add_level trigger=1.4975; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 9 – Individual-TP geschlossen

- Zeit: `2026-01-25T18:10:00+00:00`
- Candle: O=1.4976 H=1.4988 L=1.4801 C=1.4945 (index=1899)
- Aktion: `INDIVIDUAL_TP_FILLED` / `overlay_tp_close`
- Menge: `158.061`
- Triggerpreis: `1.482`
- tatsächlicher Fill-Preis: `1.482`
- Fee: `0.12883552110000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6145800577371623; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6439136191500938; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.6145800577371623
- Average danach: long=1.768355389945979 short=1.6439136191500938
- realisierter PnL: gross=`4.727999662500037` net=`4.599164141400037`
- gesamte Economics danach: `-13.423236614328273`
- Grund für die Aktion: low<=active_tp trigger=1.482 tranche=R1-T3; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T18:10:00+00:00`
- Order-ID: `individual_tp_2p00-O8-CREATE`
- Trigger: `1.481`
- Qty: `158.061`
- Active-from: `2026-01-25T18:10:00+00:00`
- Grund: Add level 4 armed; fills when low <= trigger 1.481
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T18:10:00+00:00`
- Order-ID: `individual_tp_2p00-O8`
- Trigger: `1.481`
- Qty: `158.061`
- Active-from: `2026-01-25T18:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T18:10:00+00:00`
- Order-ID: `individual_tp_2p00-TP-R1-T5`
- Trigger: `1.4497`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4497 (optical 1.4513800000000001); active next bar
- Status danach: `pending_next_bar`

### Fill 10 – Short-Add gefüllt

- Zeit: `2026-01-25T18:10:00+00:00`
- Candle: O=1.4976 H=1.4988 L=1.4801 C=1.4945 (index=1899)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.481`
- tatsächlicher Fill-Preis: `1.481`
- Fee: `0.12874858755000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6439136191500938; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.607710618121683; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.6439136191500938
- Average danach: long=1.768355389945979 short=1.607710618121683
- realisierter PnL: gross=`0.0` net=`-0.12874858755000002`
- gesamte Economics danach: `-15.685808701878287`
- Grund für die Aktion: low<=add_level trigger=1.481; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 11 – Individual-TP geschlossen

- Zeit: `2026-01-25T19:25:00+00:00`
- Candle: O=1.4713 H=1.4713 L=1.4588 C=1.4626 (index=1914)
- Aktion: `INDIVIDUAL_TP_FILLED` / `overlay_tp_close`
- Menge: `158.061`
- Triggerpreis: `1.4659`
- tatsächlicher Fill-Preis: `1.4659`
- Fee: `0.12743589094500002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.607710618121683; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6394975517128092; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.607710618121683
- Average danach: long=1.768355389945979 short=1.6394975517128092
- realisierter PnL: gross=`4.82975143125001` net=`4.70231554030501`
- gesamte Economics danach: `-6.250554092823297`
- Grund für die Aktion: low<=active_tp trigger=1.4659 tranche=R1-T4; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T19:25:00+00:00`
- Order-ID: `individual_tp_2p00-O10-CREATE`
- Trigger: `1.4646000000000001`
- Qty: `158.061`
- Active-from: `2026-01-25T19:25:00+00:00`
- Grund: Add level 5 armed; fills when low <= trigger 1.4646000000000001
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T19:25:00+00:00`
- Order-ID: `individual_tp_2p00-O10`
- Trigger: `1.4646000000000001`
- Qty: `158.061`
- Active-from: `2026-01-25T19:25:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T19:25:00+00:00`
- Order-ID: `individual_tp_2p00-TP-R1-T6`
- Trigger: `1.4337`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4337 (optical 1.435308); active next bar
- Status danach: `pending_next_bar`

### Fill 12 – Short-Add gefüllt

- Zeit: `2026-01-25T19:25:00+00:00`
- Candle: O=1.4713 H=1.4713 L=1.4588 C=1.4626 (index=1914)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4646000000000001`
- tatsächlicher Fill-Preis: `1.4646000000000001`
- Fee: `0.12732287733000003` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6394975517128092; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6006314564314084; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.6394975517128092
- Average danach: long=1.768355389945979 short=1.6006314564314084
- realisierter PnL: gross=`0.0` net=`-0.12732287733000003`
- gesamte Economics danach: `-6.061754970153299`
- Grund für die Aktion: low<=add_level trigger=1.4646000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `None`
- Kausal-Check: `ok`

### Fill 13 – Individual-TP geschlossen

- Zeit: `2026-01-25T19:50:00+00:00`
- Candle: O=1.4582 H=1.4615 L=1.4466 C=1.4533 (index=1919)
- Aktion: `INDIVIDUAL_TP_FILLED` / `overlay_tp_close`
- Menge: `158.061`
- Triggerpreis: `1.4497`
- tatsächlicher Fill-Preis: `1.4497`
- Fee: `0.12602756743500002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.6006314564314084; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6349466629688059; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.6006314564314084
- Average danach: long=1.768355389945979 short=1.6349466629688059
- realisierter PnL: gross=`4.872724265624997` net=`4.746696698189997`
- gesamte Economics danach: `-2.6788283375883353`
- Grund für die Aktion: low<=active_tp trigger=1.4497 tranche=R1-T5; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `individual_tp_2p00-O12-CREATE`
- Trigger: `1.4481000000000002`
- Qty: `158.061`
- Active-from: `2026-01-25T19:50:00+00:00`
- Grund: Add level 6 armed; fills when low <= trigger 1.4481000000000002
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `individual_tp_2p00-O12`
- Trigger: `1.4481000000000002`
- Qty: `158.061`
- Active-from: `2026-01-25T19:50:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — INDIVIDUAL_TP_CREATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `individual_tp_2p00-TP-R1-T7`
- Trigger: `1.4175`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Fee-aware TP trigger 1.4175 (optical 1.4191380000000002); active next bar
- Status danach: `pending_next_bar`

### Fill 14 – Short-Add gefüllt

- Zeit: `2026-01-25T19:50:00+00:00`
- Candle: O=1.4582 H=1.4615 L=1.4466 C=1.4533 (index=1919)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4481000000000002`
- tatsächlicher Fill-Preis: `1.4481000000000002`
- Fee: `0.12588847375500004` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6349466629688059; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.5934252114971352; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.6349466629688059
- Average danach: long=1.768355389945979 short=1.5934252114971352
- realisierter PnL: gross=`0.0` net=`-0.12588847375500004`
- gesamte Economics danach: `-3.6266340113433486`
- Grund für die Aktion: low<=add_level trigger=1.4481000000000002; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 15 – Individual-TP geschlossen

- Zeit: `2026-01-30T06:25:00+00:00`
- Candle: O=1.4423 H=1.4446 L=1.4326 C=1.4341 (index=3198)
- Aktion: `INDIVIDUAL_TP_FILLED` / `overlay_tp_close`
- Menge: `158.061`
- Triggerpreis: `1.4337`
- tatsächlicher Fill-Preis: `1.4337`
- Fee: `0.12463663063500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.5934252114971352; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6303140778700693; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.5934252114971352
- Average danach: long=1.768355389945979 short=1.6303140778700693
- realisierter PnL: gross=`4.838889332812498` net=`4.714252702177498`
- gesamte Economics danach: `2.381496158021676`
- Grund für die Aktion: low<=active_tp trigger=1.4337 tranche=R1-T6; TP active from next bar after entry
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — FULL_EXIT_GATE_CHECK

- Zeit: `2026-01-30T06:25:00+00:00`
- Order-ID: `None`
- Trigger: `1.4341`
- Qty: `None`
- Active-from: `None`
- Grund: total_exit_economics>=0.24 (target=0.0+buffer=0.25-tol=0.01)
- Status danach: `triggered`

#### Order-Event — FULL_EXIT_TRIGGERED

- Zeit: `2026-01-30T06:25:00+00:00`
- Order-ID: `None`
- Trigger: `None`
- Qty: `None`
- Active-from: `None`
- Grund: Net-BE gate passed; flatten all remaining positions
- Status danach: `executing`

### Fill 16 – Overlay geschlossen (Full Exit)

- Zeit: `2026-01-30T06:25:00+00:00`
- Candle: O=1.4423 H=1.4446 L=1.4326 C=1.4341 (index=3198)
- Aktion: `OVERLAY_CLOSED` / `full_exit_short`
- Menge: `158.061`
- Triggerpreis: `1.4341`
- tatsächlicher Fill-Preis: `1.4341`
- Fee: `0.12467140405500002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6303140778700693; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.6303140778700693
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`4.7756649328125045` net=`4.650993528757504`
- gesamte Economics danach: `2.2568247539666757`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 17 – Core Long geschlossen (Full Exit)

- Zeit: `2026-01-30T06:25:00+00:00`
- Candle: O=1.4423 H=1.4446 L=1.4326 C=1.4341 (index=3198)
- Aktion: `CORE_LONG_CLOSED` / `full_exit_core_long`
- Menge: `395.153`
- Triggerpreis: `1.4341`
- tatsächlicher Fill-Preis: `1.4341`
- Fee: `0.311678904515` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=0.0 @ 0.0; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=0.0 short=1.696714
- realisierter PnL: gross=`-132.08202010332346` net=`-132.39369900783845`
- gesamte Economics danach: `1.9451458494516771`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 18 – Core Short geschlossen (Full Exit)

- Zeit: `2026-01-30T06:25:00+00:00`
- Candle: O=1.4423 H=1.4446 L=1.4326 C=1.4341 (index=3198)
- Aktion: `CORE_SHORT_CLOSED` / `full_exit_core_short`
- Menge: `395.153`
- Triggerpreis: `1.4341`
- tatsächlicher Fill-Preis: `1.4341`
- Fee: `0.311678904515` (rate=0.00055)
- Position vorher: long=0.0 @ 0.0; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=0.0 @ 0.0; short=0.0 @ 0.0; overlay_short=0.0
- Average vorher: long=0.0 short=1.696714
- Average danach: long=0.0 short=0.0
- realisierter PnL: gross=`103.77270994200005` net=`103.46103103748504`
- gesamte Economics danach: `1.633466944936675`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 19 – FINAL_FLAT

- Zeit: `2026-01-30T06:25:00+00:00`
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
- gesamte Economics danach: `1.633466944936675`
- Grund für die Aktion: All legs flat after net-BE full exit
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `first_be=2026-01-30T06:25:00+00:00 exit=2026-01-30T06:25:00+00:00 delay=0`

## Tranches

### Tranche `R1-T1`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.5469000000000002` entry_fee: `0.13447750849500004`
- tp_pct: `0.02` tp_trigger: `1.5142`
- steps_completed: `0`
- closed_qty_from_tp_events: `158.061`
- realized_gross: `3.864591450000013` close_fees: `0.13163478141` net: `3.5984791600950135`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T2`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.5304` entry_fee: `0.13304310492000002`
- tp_pct: `0.02` tp_trigger: `1.4981`
- steps_completed: `0`
- closed_qty_from_tp_events: `158.061`
- realized_gross: `4.461271725000026` close_fees: `0.13023515125500001` net: `4.197993468825026`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T3`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.514` entry_fee: `0.13161739470000003`
- tp_pct: `0.02` tp_trigger: `1.482`
- steps_completed: `0`
- closed_qty_from_tp_events: `158.061`
- realized_gross: `4.727999662500037` close_fees: `0.12883552110000002` net: `4.4675467467000365`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T4`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.4975` entry_fee: `0.13018299112500004`
- tp_pct: `0.02` tp_trigger: `1.4659`
- steps_completed: `0`
- closed_qty_from_tp_events: `158.061`
- realized_gross: `4.82975143125001` close_fees: `0.12743589094500002` net: `4.57213254918001`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T5`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.481` entry_fee: `0.12874858755000002`
- tp_pct: `0.02` tp_trigger: `1.4497`
- steps_completed: `0`
- closed_qty_from_tp_events: `158.061`
- realized_gross: `4.872724265624997` close_fees: `0.12602756743500002` net: `4.617948110639998`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T6`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.4646000000000001` entry_fee: `0.12732287733000003`
- tp_pct: `0.02` tp_trigger: `1.4337`
- steps_completed: `0`
- closed_qty_from_tp_events: `158.061`
- realized_gross: `4.838889332812498` close_fees: `0.12463663063500001` net: `4.5869298248474974`
- final_status: `closed` qty_sum: `PASS`

### Tranche `R1-T7`

- initial_qty: `158.061` remaining: `0.0`
- entry_price: `1.4481000000000002` entry_fee: `0.12588847375500004`
- tp_pct: `0.02` tp_trigger: `1.4175`
- steps_completed: `0`
- closed_qty_from_tp_events: `0`
- realized_gross: `0.0` close_fees: `0.0` net: `-0.12588847375500004`
- final_status: `closed` qty_sum: `PASS`

## Finaler Netto-BE-Exit

- erste erreichbare BE-Candle: `2026-01-30T06:25:00+00:00`
- tatsächlicher Exit: `2026-01-30T06:25:00+00:00`
- Ziel / Safety-Buffer / Tol: `0.0` / `0.25` / `0.01`
- Economics vor Exit: `1.633466944936683`
- geschätzte Rest-Close-Fees: `0.748029213085`
- geschätzte Exit-Slippage: `0.0`
- Exit-Fill-Preise: `[1.4341, 1.4341, 1.4341]`
- Exit-Fill-Mengen: `[158.061, 395.153, 395.153]`
- finale Economics: `1.633466944936675`
- flat: `True` status: `RECOVERED_BE`
- open_tranches_remaining: `0`
- pass_fail: `PASS`
