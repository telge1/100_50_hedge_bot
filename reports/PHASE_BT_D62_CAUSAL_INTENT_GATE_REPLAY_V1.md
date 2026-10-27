# BT-D62 — Causal Short Intent Gate Replay V1

**VERDICT:** `CAUSAL_INTENT_GATE_REPLAY_PASS`

## Mandatory intents (decision-close = D41 intent bar)
- Sep-9 16:30 / 20:55: P2 = `SHORT_OK` / `SHORT_OK`
- Sep-11 12:05 / 12:30: P2 = `SHORT_NO` / `SHORT_NO` (not fill bars 12:25/12:35)

## Answers
1. Both Sep-9 desired allowed (P2)? **True** (R5 pass; R6 not sideways at anchors).
2. Sep-11 intents blocked before pending? **True** (R6 veto from 12:00; R5 fails at 12:05).
3. Combination: **R5 + NOT R6** (P2); R1 alone insufficient; R2 false-blocks Sep-9.
4. D46 pending intents replayed: **36**; independent episode estimate in CSV (not OOS).
5. Provisional research gate justified for **isolated further tests only** — not production.
6. Unvalidated: thresholds, pause requalification, Sep-9 regime UNCERTAIN vs PASS, single pilot day.

## Safety
No changes to strategy modules, D46, density SSOT, or dashboard.

Outputs: `results/phase_bt_d62_intent_gate_replay_v1/`