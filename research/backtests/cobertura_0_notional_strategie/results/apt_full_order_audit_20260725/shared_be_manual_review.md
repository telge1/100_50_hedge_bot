# Manual Review — shared_be

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
- Order-ID: `shared_be-O1-CREATE`
- Trigger: `1.5469000000000002`
- Qty: `158.061`
- Active-from: `2026-01-20T16:45:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.5469000000000002
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-20T16:45:00+00:00`
- Order-ID: `shared_be-O1`
- Trigger: `1.5469000000000002`
- Qty: `158.061`
- Active-from: `2026-01-20T16:45:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-01-20T16:45:00+00:00`
- Order-ID: `shared_be-BE-1`
- Trigger: `1.5451000000000001`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
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
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.4508`
- Kausal-Check: `ok`

### Fill 4 – Shared-BE Overlay geschlossen

- Zeit: `2026-01-20T16:50:00+00:00`
- Candle: O=1.5486 H=1.5548 L=1.5486 C=1.5529 (index=443)
- Aktion: `SHARED_BE_CLOSE_FILLED` / `overlay_be_close`
- Menge: `158.061`
- Triggerpreis: `1.5451000000000001`
- tatsächlicher Fill-Preis: `1.5451000000000001`
- Fee: `0.13432102810500002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6539100386866565; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.6539100386866565
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.28450980000000375` net=`0.15018877189500374`
- gesamte Economics danach: `-28.293598897923406`
- Grund für die Aktion: high>=active_shared_be trigger=1.5451000000000001; close all overlay short; active_from prior bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.4508`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `shared_be-O3-CREATE`
- Trigger: `1.4524000000000001`
- Qty: `158.061`
- Active-from: `2026-01-25T19:50:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.4524000000000001
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `shared_be-O3`
- Trigger: `1.4524000000000001`
- Qty: `158.061`
- Active-from: `2026-01-25T19:50:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-01-25T19:50:00+00:00`
- Order-ID: `shared_be-BE-3`
- Trigger: `1.4508`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 5 – Short-Add gefüllt

- Zeit: `2026-01-25T19:50:00+00:00`
- Candle: O=1.4582 H=1.4615 L=1.4466 C=1.4533 (index=1919)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.4524000000000001`
- tatsächlicher Fill-Preis: `1.4524000000000001`
- Fee: `0.12626228802000003` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6269100630895097; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.6269100630895097
- realisierter PnL: gross=`0.0` net=`-0.12626228802000003`
- gesamte Economics danach: `-28.56211608594339`
- Grund für die Aktion: low<=add_level trigger=1.4524000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.3623`
- Kausal-Check: `ok`

### Fill 6 – Shared-BE Overlay geschlossen

- Zeit: `2026-01-25T19:55:00+00:00`
- Candle: O=1.4533 H=1.4575 L=1.4499 C=1.4569 (index=1920)
- Aktion: `SHARED_BE_CLOSE_FILLED` / `overlay_be_close`
- Menge: `158.061`
- Triggerpreis: `1.4508`
- tatsächlicher Fill-Preis: `1.4508`
- Fee: `0.12612319434000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6269100630895097; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.6269100630895097
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.25289760000000727` net=`0.12677440566000725`
- gesamte Economics danach: `-28.293086780283385`
- Grund für die Aktion: high>=active_shared_be trigger=1.4508; close all overlay short; active_from prior bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.3623`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-31T11:40:00+00:00`
- Order-ID: `shared_be-O5-CREATE`
- Trigger: `1.3638000000000001`
- Qty: `158.061`
- Active-from: `2026-01-31T11:40:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.3638000000000001
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-31T11:40:00+00:00`
- Order-ID: `shared_be-O5`
- Trigger: `1.3638000000000001`
- Qty: `158.061`
- Active-from: `2026-01-31T11:40:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-01-31T11:40:00+00:00`
- Order-ID: `shared_be-BE-5`
- Trigger: `1.3623`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 7 – Short-Add gefüllt

- Zeit: `2026-01-31T11:40:00+00:00`
- Candle: O=1.38 H=1.3809 L=1.3624 C=1.3685 (index=3549)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.3638000000000001`
- tatsächlicher Fill-Preis: `1.3638000000000001`
- Fee: `0.11855997549000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6015958002545128; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.6015958002545128
- realisierter PnL: gross=`0.0` net=`-0.11855997549000002`
- gesamte Economics danach: `-29.154533455773375`
- Grund für die Aktion: low<=add_level trigger=1.3638000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.2791000000000001`
- Kausal-Check: `ok`

### Fill 8 – Shared-BE Overlay geschlossen

- Zeit: `2026-01-31T11:45:00+00:00`
- Candle: O=1.3685 H=1.3696 L=1.366 C=1.3673 (index=3550)
- Aktion: `SHARED_BE_CLOSE_FILLED` / `overlay_be_close`
- Menge: `158.061`
- Triggerpreis: `1.3623`
- tatsächlicher Fill-Preis: `1.3623`
- Fee: `0.11842957516500002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.6015958002545128; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.6015958002545128
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.237091500000009` net=`0.11866192483500898`
- gesamte Economics danach: `-28.292984830938405`
- Grund für die Aktion: high>=active_shared_be trigger=1.3623; close all overlay short; active_from prior bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.2791000000000001`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-31T14:35:00+00:00`
- Order-ID: `shared_be-O7-CREATE`
- Trigger: `1.2806`
- Qty: `158.061`
- Active-from: `2026-01-31T14:35:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.2806
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-31T14:35:00+00:00`
- Order-ID: `shared_be-O7`
- Trigger: `1.2806`
- Qty: `158.061`
- Active-from: `2026-01-31T14:35:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-01-31T14:35:00+00:00`
- Order-ID: `shared_be-BE-7`
- Trigger: `1.2791000000000001`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 9 – Short-Add gefüllt

- Zeit: `2026-01-31T14:35:00+00:00`
- Candle: O=1.2987 H=1.3002 L=1.2738 C=1.2872 (index=3584)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.2806`
- tatsächlicher Fill-Preis: `1.2806`
- Fee: `0.11132710413000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.577824393167924; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.577824393167924
- realisierter PnL: gross=`0.0` net=`-0.11132710413000002`
- gesamte Economics danach: `-29.447514535068393`
- Grund für die Aktion: low<=add_level trigger=1.2806; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.201`
- Kausal-Check: `ok`

### Fill 10 – Shared-BE Overlay geschlossen

- Zeit: `2026-01-31T14:40:00+00:00`
- Candle: O=1.2872 H=1.2873 L=1.2728 C=1.2796 (index=3585)
- Aktion: `SHARED_BE_CLOSE_FILLED` / `overlay_be_close`
- Menge: `158.061`
- Triggerpreis: `1.2791000000000001`
- tatsächlicher Fill-Preis: `1.2791000000000001`
- Fee: `0.11119670380500002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.577824393167924; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.577824393167924
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.2370914999999739` net=`0.12589479619497387`
- gesamte Economics danach: `-28.2784171388734`
- Grund für die Aktion: high>=active_shared_be trigger=1.2791000000000001; close all overlay short; active_from prior bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.201`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-01-31T17:10:00+00:00`
- Order-ID: `shared_be-O9-CREATE`
- Trigger: `1.2024000000000001`
- Qty: `158.061`
- Active-from: `2026-01-31T17:10:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.2024000000000001
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-01-31T17:10:00+00:00`
- Order-ID: `shared_be-O9`
- Trigger: `1.2024000000000001`
- Qty: `158.061`
- Active-from: `2026-01-31T17:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-01-31T17:10:00+00:00`
- Order-ID: `shared_be-BE-9`
- Trigger: `1.201`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 11 – Short-Add gefüllt

- Zeit: `2026-01-31T17:10:00+00:00`
- Candle: O=1.2699 H=1.271 L=1.2 C=1.2351 (index=3615)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.2024000000000001`
- tatsächlicher Fill-Preis: `1.2024000000000001`
- Fee: `0.10452890052000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.5554815562187507; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.5554815562187507
- realisierter PnL: gross=`0.0` net=`-0.10452890052000002`
- gesamte Economics danach: `-33.551540739393396`
- Grund für die Aktion: low<=add_level trigger=1.2024000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.1276000000000002`
- Kausal-Check: `ok`

### Fill 12 – Shared-BE Overlay geschlossen

- Zeit: `2026-01-31T17:15:00+00:00`
- Candle: O=1.2351 H=1.2486 L=1.2349 C=1.24 (index=3616)
- Aktion: `SHARED_BE_CLOSE_FILLED` / `overlay_be_close`
- Menge: `158.061`
- Triggerpreis: `1.201`
- tatsächlicher Fill-Preis: `1.201`
- Fee: `0.10440719355000001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.5554815562187507; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.5554815562187507
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.22128540000001073` net=`0.11687820645001072`
- gesamte Economics danach: `-28.266067832943392`
- Grund für die Aktion: high>=active_shared_be trigger=1.201; close all overlay short; active_from prior bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.1276000000000002`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-O11-CREATE`
- Trigger: `1.1289`
- Qty: `158.061`
- Active-from: `2026-02-05T15:10:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.1289
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-O11`
- Trigger: `1.1289`
- Qty: `158.061`
- Active-from: `2026-02-05T15:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-BE-11`
- Trigger: `1.1276000000000002`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-BE-11`
- Trigger: `1.1216000000000002`
- Qty: `316.122`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 13 – Short-Add gefüllt

- Zeit: `2026-02-05T15:10:00+00:00`
- Candle: O=1.1379 H=1.1413 L=1.116 C=1.1198 (index=5031)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.1289`
- tatsächlicher Fill-Preis: `1.1289`
- Fee: `0.09813928459500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.5344815751987477; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.5344815751987477
- realisierter PnL: gross=`0.0` net=`-0.09813928459500001`
- gesamte Economics danach: `-26.925852017538404`
- Grund für die Aktion: low<=add_level trigger=1.1289; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.0531000000000001`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-O12-CREATE`
- Trigger: `1.1169`
- Qty: `158.061`
- Active-from: `2026-02-05T15:10:00+00:00`
- Grund: Add level 1 armed; fills when low <= trigger 1.1169
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-O12`
- Trigger: `1.1169`
- Qty: `158.061`
- Active-from: `2026-02-05T15:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-BE-12`
- Trigger: `1.1276000000000002`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-05T15:10:00+00:00`
- Order-ID: `shared_be-BE-12`
- Trigger: `1.1216000000000002`
- Qty: `316.122`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 14 – Short-Add gefüllt

- Zeit: `2026-02-05T15:10:00+00:00`
- Candle: O=1.1379 H=1.1413 L=1.116 C=1.1198 (index=5031)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.1169`
- tatsächlicher Fill-Preis: `1.1169`
- Fee: `0.09709608199500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.5344815751987477; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.441685734831113; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.5344815751987477
- Average danach: long=1.768355389945979 short=1.441685734831113
- realisierter PnL: gross=`0.0` net=`-0.09709608199500001`
- gesamte Economics danach: `-27.481324999533385`
- Grund für die Aktion: low<=add_level trigger=1.1169; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.0531000000000001`
- Kausal-Check: `ok`

### Fill 15 – Shared-BE Overlay geschlossen

- Zeit: `2026-02-05T15:15:00+00:00`
- Candle: O=1.1198 H=1.1355 L=1.1192 C=1.1256 (index=5032)
- Aktion: `SHARED_BE_CLOSE_FILLED` / `overlay_be_close`
- Menge: `316.122`
- Triggerpreis: `1.1216000000000002`
- tatsächlicher Fill-Preis: `1.1216000000000002`
- Fee: `0.19500933936000003` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.441685734831113; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.441685734831113
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.41095859999995477` net=`0.21594926063995473`
- gesamte Economics danach: `-28.245353938893466`
- Grund für die Aktion: high>=active_shared_be trigger=1.1216000000000002; close all overlay short; active_from prior bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `1.0531000000000001`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-02-05T20:15:00+00:00`
- Order-ID: `shared_be-O14-CREATE`
- Trigger: `1.0543`
- Qty: `158.061`
- Active-from: `2026-02-05T20:15:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 1.0543
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-02-05T20:15:00+00:00`
- Order-ID: `shared_be-O14`
- Trigger: `1.0543`
- Qty: `158.061`
- Active-from: `2026-02-05T20:15:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-02-05T20:15:00+00:00`
- Order-ID: `shared_be-BE-14`
- Trigger: `1.0531000000000001`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 16 – Short-Add gefüllt

- Zeit: `2026-02-05T20:15:00+00:00`
- Candle: O=1.0697 H=1.0715 L=1.0525 C=1.055 (index=5092)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `1.0543`
- tatsächlicher Fill-Preis: `1.0543`
- Fee: `0.09165404176500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.5131673087485134; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.5131673087485134
- realisierter PnL: gross=`0.0` net=`-0.09165404176500001`
- gesamte Economics danach: `-28.447650680658427`
- Grund für die Aktion: low<=add_level trigger=1.0543; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `0.9888`
- Kausal-Check: `ok`

### Fill 17 – Shared-BE Overlay geschlossen

- Zeit: `2026-02-05T20:20:00+00:00`
- Candle: O=1.055 H=1.0581 L=1.0342 C=1.0406 (index=5093)
- Aktion: `SHARED_BE_CLOSE_FILLED` / `overlay_be_close`
- Menge: `158.061`
- Triggerpreis: `1.0531000000000001`
- tatsächlicher Fill-Preis: `1.0531000000000001`
- Fee: `0.09154972150500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.5131673087485134; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.5131673087485134
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`0.18967319999997911` net=`0.0981234784949791`
- gesamte Economics danach: `-28.23888450216343`
- Grund für die Aktion: high>=active_shared_be trigger=1.0531000000000001; close all overlay short; active_from prior bar
- danach aktive Order bzw. Trigger: `shared_overlay_be` @ `0.9888`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O16-CREATE`
- Trigger: `0.9899`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add level 0 armed; fills when low <= trigger 0.9899
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O16`
- Trigger: `0.9899`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_CREATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-16`
- Trigger: `0.9888`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-16`
- Trigger: `0.9835`
- Qty: `316.122`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-16`
- Trigger: `0.9783000000000001`
- Qty: `474.183`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-16`
- Trigger: `0.9730000000000001`
- Qty: `632.244`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 18 – Short-Add gefüllt

- Zeit: `2026-02-06T00:10:00+00:00`
- Candle: O=1.0087 H=1.0132 L=0.9277 C=0.9437 (index=5139)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `0.9899`
- tatsächlicher Fill-Preis: `0.9899`
- Fee: `0.08605552114500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=395.153 @ 1.768355389945979; short=553.214 @ 1.4947673253786058; overlay_short=158.061
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=1.768355389945979 short=1.4947673253786058
- realisierter PnL: gross=`0.0` net=`-0.08605552114500001`
- gesamte Economics danach: `-21.02252182330848`
- Grund für die Aktion: low<=add_level trigger=0.9899; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O17-CREATE`
- Trigger: `0.9794`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add level 1 armed; fills when low <= trigger 0.9794
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O17`
- Trigger: `0.9794`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-17`
- Trigger: `0.9888`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-17`
- Trigger: `0.9835`
- Qty: `316.122`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-17`
- Trigger: `0.9783000000000001`
- Qty: `474.183`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-17`
- Trigger: `0.9730000000000001`
- Qty: `632.244`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 19 – Short-Add gefüllt

- Zeit: `2026-02-06T00:10:00+00:00`
- Candle: O=1.0087 H=1.0132 L=0.9277 C=0.9437 (index=5139)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `0.9794`
- tatsächlicher Fill-Preis: `0.9794`
- Fee: `0.08514271887000001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=553.214 @ 1.4947673253786058; overlay_short=158.061
- Position danach: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.380241333579839; overlay_short=316.122
- Average vorher: long=1.768355389945979 short=1.4947673253786058
- Average danach: long=1.768355389945979 short=1.380241333579839
- realisierter PnL: gross=`0.0` net=`-0.08514271887000001`
- gesamte Economics danach: `-15.464886842178437`
- Grund für die Aktion: low<=add_level trigger=0.9794; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O18-CREATE`
- Trigger: `0.9689000000000001`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add level 2 armed; fills when low <= trigger 0.9689000000000001
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O18`
- Trigger: `0.9689000000000001`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-18`
- Trigger: `0.9888`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-18`
- Trigger: `0.9835`
- Qty: `316.122`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-18`
- Trigger: `0.9783000000000001`
- Qty: `474.183`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-18`
- Trigger: `0.9730000000000001`
- Qty: `632.244`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 20 – Short-Add gefüllt

- Zeit: `2026-02-06T00:10:00+00:00`
- Candle: O=1.0087 H=1.0132 L=0.9277 C=0.9437 (index=5139)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `0.9689000000000001`
- tatsächlicher Fill-Preis: `0.9689000000000001`
- Fee: `0.08422991659500001` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=711.2750000000001 @ 1.380241333579839; overlay_short=316.122
- Position danach: long=395.153 @ 1.768355389945979; short=869.336 @ 1.3054520432168921; overlay_short=474.183
- Average vorher: long=1.768355389945979 short=1.380241333579839
- Average danach: long=1.768355389945979 short=1.3054520432168921
- realisierter PnL: gross=`0.0` net=`-0.08422991659500001`
- gesamte Economics danach: `-11.565979558773352`
- Grund für die Aktion: low<=add_level trigger=0.9689000000000001; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — SHORT_ADD_TRIGGER_CREATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O19-CREATE`
- Trigger: `0.9583`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add level 3 armed; fills when low <= trigger 0.9583
- Status danach: `active`

#### Order-Event — SHORT_ADD_ORDER_ACTIVATED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-O19`
- Trigger: `0.9583`
- Qty: `158.061`
- Active-from: `2026-02-06T00:10:00+00:00`
- Grund: Add order active on this candle (same-bar fill allowed for adds)
- Status danach: `active`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-19`
- Trigger: `0.9888`
- Qty: `158.061`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-19`
- Trigger: `0.9835`
- Qty: `316.122`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-19`
- Trigger: `0.9783000000000001`
- Qty: `474.183`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

#### Order-Event — SHARED_BE_TRIGGER_REPLACED

- Zeit: `2026-02-06T00:10:00+00:00`
- Order-ID: `shared_be-BE-19`
- Trigger: `0.9730000000000001`
- Qty: `632.244`
- Active-from: `next_bar`
- Grund: Shared overlay BE recomputed after add; active next bar
- Status danach: `pending_next_bar`

### Fill 21 – Short-Add gefüllt

- Zeit: `2026-02-06T00:10:00+00:00`
- Candle: O=1.0087 H=1.0132 L=0.9277 C=0.9437 (index=5139)
- Aktion: `SHORT_ADD_FILLED` / `overlay_short_add`
- Menge: `158.061`
- Triggerpreis: `0.9583`
- tatsächlicher Fill-Preis: `0.9583`
- Fee: `0.083308420965` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=869.336 @ 1.3054520432168921; overlay_short=474.183
- Position danach: long=395.153 @ 1.768355389945979; short=1027.397 @ 1.2520440625600426; overlay_short=632.244
- Average vorher: long=1.768355389945979 short=1.3054520432168921
- Average danach: long=1.768355389945979 short=1.2520440625600426
- realisierter PnL: gross=`0.0` net=`-0.083308420965`
- gesamte Economics danach: `-9.341597379738463`
- Grund für die Aktion: low<=add_level trigger=0.9583; shallow→deep; fill at slipped trigger; TP/BE active next bar
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

#### Order-Event — FULL_EXIT_GATE_CHECK

- Zeit: `2026-02-06T00:15:00+00:00`
- Order-ID: `None`
- Trigger: `0.9052`
- Qty: `None`
- Active-from: `None`
- Grund: total_exit_economics>=0.24 (target=0.0+buffer=0.25-tol=0.01)
- Status danach: `triggered`

#### Order-Event — FULL_EXIT_TRIGGERED

- Zeit: `2026-02-06T00:15:00+00:00`
- Order-ID: `None`
- Trigger: `None`
- Qty: `None`
- Active-from: `None`
- Grund: Net-BE gate passed; flatten all remaining positions
- Status danach: `executing`

### Fill 22 – Overlay geschlossen (Full Exit)

- Zeit: `2026-02-06T00:15:00+00:00`
- Candle: O=0.9437 H=0.9702 L=0.8982 C=0.9052 (index=5140)
- Aktion: `OVERLAY_CLOSED` / `full_exit_short`
- Menge: `632.244`
- Triggerpreis: `0.9052`
- tatsächlicher Fill-Preis: `0.9052`
- Fee: `0.31476899784` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=1027.397 @ 1.2520440625600426; overlay_short=632.244
- Position danach: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.2520440625600426
- Average danach: long=1.768355389945979 short=1.696714
- realisierter PnL: gross=`43.57741770000001` net=`43.262648702160014`
- gesamte Economics danach: `14.685027622421586`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 23 – Core Long geschlossen (Full Exit)

- Zeit: `2026-02-06T00:15:00+00:00`
- Candle: O=0.9437 H=0.9702 L=0.8982 C=0.9052 (index=5140)
- Aktion: `CORE_LONG_CLOSED` / `full_exit_core_long`
- Menge: `395.153`
- Triggerpreis: `0.9052`
- tatsächlicher Fill-Preis: `0.9052`
- Fee: `0.19673087258000002` (rate=0.00055)
- Position vorher: long=395.153 @ 1.768355389945979; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=0.0 @ 0.0; short=395.153 @ 1.696714; overlay_short=0.0
- Average vorher: long=1.768355389945979 short=1.696714
- Average danach: long=0.0 short=1.696714
- realisierter PnL: gross=`-341.0784418033234` net=`-341.27517267590343`
- gesamte Economics danach: `14.488296749841652`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 24 – Core Short geschlossen (Full Exit)

- Zeit: `2026-02-06T00:15:00+00:00`
- Candle: O=0.9437 H=0.9702 L=0.8982 C=0.9052 (index=5140)
- Aktion: `CORE_SHORT_CLOSED` / `full_exit_core_short`
- Menge: `395.153`
- Triggerpreis: `0.9052`
- tatsächlicher Fill-Preis: `0.9052`
- Fee: `0.19673087258000002` (rate=0.00055)
- Position vorher: long=0.0 @ 0.0; short=395.153 @ 1.696714; overlay_short=0.0
- Position danach: long=0.0 @ 0.0; short=0.0 @ 0.0; overlay_short=0.0
- Average vorher: long=0.0 short=1.696714
- Average danach: long=0.0 short=0.0
- realisierter PnL: gross=`312.76913164200005` net=`312.57240076942`
- gesamte Economics danach: `14.291565877261585`
- Grund für die Aktion: net_be gate: total_exit_economics >= target+safety_buffer-tol; adverse close fills; flatten all remaining
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `ok`

### Fill 25 – FINAL_FLAT

- Zeit: `2026-02-06T00:15:00+00:00`
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
- gesamte Economics danach: `14.291565877261585`
- Grund für die Aktion: All legs flat after net-BE full exit
- danach aktive Order bzw. Trigger: `None` @ `None`
- Kausal-Check: `first_be=2026-02-06T00:15:00+00:00 exit=2026-02-06T00:15:00+00:00 delay=0`

## Shared-BE Runden

### Round 1

- n_adds: `1`
- add_timestamps: `["2026-01-20T16:45:00+00:00"]`
- round_qty: `158.061`
- overlay_avg: `1.5469000000000002`
- round_open_fees: `0.13447750849500004`
- shared_be_trigger: `1.5451000000000001` (timeline=1.5451000000000001)
- close_timestamp: `2026-01-20T16:50:00+00:00`
- close_fill: `1.5451000000000001` fee=`0.13432102810500002`
- gross_pnl: `0.28450980000000375` net_pnl: `0.0157112634000037`
- Nur Overlay wird geschlossen; Core-Short bleibt unverändert (Core-Freeze bis Full Exit).
- pass_fail: `PASS`

### Round 2

- n_adds: `1`
- add_timestamps: `["2026-01-25T19:50:00+00:00"]`
- round_qty: `158.061`
- overlay_avg: `1.4524000000000001`
- round_open_fees: `0.12626228802000003`
- shared_be_trigger: `1.4508` (timeline=1.4508)
- close_timestamp: `2026-01-25T19:55:00+00:00`
- close_fill: `1.4508` fee=`0.12612319434000002`
- gross_pnl: `0.25289760000000727` net_pnl: `0.0005121176400072203`
- Nur Overlay wird geschlossen; Core-Short bleibt unverändert (Core-Freeze bis Full Exit).
- pass_fail: `PASS`

### Round 3

- n_adds: `1`
- add_timestamps: `["2026-01-31T11:40:00+00:00"]`
- round_qty: `158.061`
- overlay_avg: `1.3638000000000001`
- round_open_fees: `0.11855997549000002`
- shared_be_trigger: `1.3623` (timeline=1.3623)
- close_timestamp: `2026-01-31T11:45:00+00:00`
- close_fill: `1.3623` fee=`0.11842957516500002`
- gross_pnl: `0.237091500000009` net_pnl: `0.00010194934500895592`
- Nur Overlay wird geschlossen; Core-Short bleibt unverändert (Core-Freeze bis Full Exit).
- pass_fail: `PASS`

### Round 4

- n_adds: `1`
- add_timestamps: `["2026-01-31T14:35:00+00:00"]`
- round_qty: `158.061`
- overlay_avg: `1.2806`
- round_open_fees: `0.11132710413000002`
- shared_be_trigger: `1.2791000000000001` (timeline=1.2791000000000001)
- close_timestamp: `2026-01-31T14:40:00+00:00`
- close_fill: `1.2791000000000001` fee=`0.11119670380500002`
- gross_pnl: `0.2370914999999739` net_pnl: `0.014567692064973853`
- Nur Overlay wird geschlossen; Core-Short bleibt unverändert (Core-Freeze bis Full Exit).
- pass_fail: `PASS`

### Round 5

- n_adds: `1`
- add_timestamps: `["2026-01-31T17:10:00+00:00"]`
- round_qty: `158.061`
- overlay_avg: `1.2024000000000001`
- round_open_fees: `0.10452890052000002`
- shared_be_trigger: `1.201` (timeline=1.201)
- close_timestamp: `2026-01-31T17:15:00+00:00`
- close_fill: `1.201` fee=`0.10440719355000001`
- gross_pnl: `0.22128540000001073` net_pnl: `0.012349305930010698`
- Nur Overlay wird geschlossen; Core-Short bleibt unverändert (Core-Freeze bis Full Exit).
- pass_fail: `PASS`

### Round 6

- n_adds: `2`
- add_timestamps: `["2026-02-05T15:10:00+00:00", "2026-02-05T15:10:00+00:00"]`
- round_qty: `316.122`
- overlay_avg: `1.1229`
- round_open_fees: `0.19523536659000001`
- shared_be_trigger: `1.1216000000000002` (timeline=1.1216000000000002)
- close_timestamp: `2026-02-05T15:15:00+00:00`
- close_fill: `1.1216000000000002` fee=`0.19500933936000003`
- gross_pnl: `0.41095859999995477` net_pnl: `0.020713894049954718`
- Nur Overlay wird geschlossen; Core-Short bleibt unverändert (Core-Freeze bis Full Exit).
- pass_fail: `PASS`

### Round 7

- n_adds: `1`
- add_timestamps: `["2026-02-05T20:15:00+00:00"]`
- round_qty: `158.061`
- overlay_avg: `1.0543`
- round_open_fees: `0.09165404176500001`
- shared_be_trigger: `1.0531000000000001` (timeline=1.0531000000000001)
- close_timestamp: `2026-02-05T20:20:00+00:00`
- close_fill: `1.0531000000000001` fee=`0.09154972150500001`
- gross_pnl: `0.18967319999997911` net_pnl: `0.006469436729979086`
- Nur Overlay wird geschlossen; Core-Short bleibt unverändert (Core-Freeze bis Full Exit).
- pass_fail: `PASS`

## Finaler Netto-BE-Exit

- erste erreichbare BE-Candle: `2026-02-06T00:15:00+00:00`
- tatsächlicher Exit: `2026-02-06T00:15:00+00:00`
- Ziel / Safety-Buffer / Tol: `0.0` / `0.25` / `0.01`
- Economics vor Exit: `14.291565877261606`
- geschätzte Rest-Close-Fees: `0.7082307430000001`
- geschätzte Exit-Slippage: `0.0`
- Exit-Fill-Preise: `[0.9052, 0.9052, 0.9052]`
- Exit-Fill-Mengen: `[632.244, 395.153, 395.153]`
- finale Economics: `14.291565877261585`
- flat: `True` status: `RECOVERED_BE`
- open_tranches_remaining: `0`
- pass_fail: `PASS`
