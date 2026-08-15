# Feature Semantics — HTF Pivot Level Preview (htf_pivot_level_preview_v1)

## Role

Visual validation only. Python scanner is source of truth. Pine embeds the same levels.

## HTF-only review pines

Embedded families: **4h / 12h / 1D pivots only**.

Not embedded: external_swing, protected.

Selection rule: all HTF levels sorted by `(visible_from_timestamp ASC, level_id ASC)`.
No truncation that drops HTF levels below TradingView line limit (500).

## Lifecycle modes

### replacement (`close_break_or_replacement`)

New confirmed pivot of same `(source×tf×side)` ends the previous active level at the
new level's `visible_from`. Close-break may end earlier.

### persistent (`close_break_only`)

New pivots do **not** replace prior levels. Each level stays active until close-break
or data end. Diagnostic comparison only.

## Touch markers

- `first_touch_timestamp` = first 5m bar close at/after `visible_from` that wick-touches
- Pine `T` marker uses `firstTouchArr` only
- No `T` when `first_touch_timestamp` is missing
- Touches before `visible_from` are forbidden

## Arrays (identical length)

seqArr, priceArr, sideArr, srcArr, activeArr, touchArr, invReasonArr,
visArr, pivotArr, invArr, firstTouchArr, idArr, tfArr, labelArr
