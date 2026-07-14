# Phase C2B1 — Topping/Bottoming multi-bar turning exits

C1-C strict + `turning_multi_bar_mode` ∈ {off, loose, strict}.

Primary window: **24** bars (also sensitivity 12/36 for loose).

## Recommendation
`C2B_C_strict_w24` — strict reduces stickiness with extra HTF/indicator gate; passes acceptance

Production default remains **`turning_multi_bar_mode=off`**.

## Neutral
No →neutral timeout in C2B1. C2B2 still needed?: True
