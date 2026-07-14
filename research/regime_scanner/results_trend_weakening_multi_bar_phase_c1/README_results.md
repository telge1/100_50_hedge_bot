# Phase C1 — Weakening multi-bar evidence audit

## Question
Can the trend state machine accumulate opposing structure over multiple **closed** 5m candles and exit `*_weakening` without March hardcodes, ping-pong, or policy changes?

## Variants
| ID | mode | rule |
|---|---|---|
| C1-A | `off` | Baseline same-bar exits only |
| C1-B | `loose` | ≥2 distinct counter categories inside window → topping/bottoming |
| C1-C | `strict` | loose + require BOS/CHoCH + (impulse OR 15m counter-bias OR ≥2 indicator confirms) |

Default production/research config remains **`weakening_multi_bar_mode=off`**.

## Code audit (pre-change)
See `code_audit.json`. Weakening exits previously required concurrent same-bar events; persisted `last_bos`/`last_choch` were not used.

## Recommendation
`C1_C_strict` — exits stuck weakening with stricter BOS/CHoCH+impulse/HTF gate

## Safety
- Policy unchanged
- No writes into `results/`, Phase-B, or Phase-C0 dirs
- No live wiring
