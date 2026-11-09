# L1 — per allowlist trend line

Retrospective research pilot (not a causal backtest).

| drawing_id | touch_events | long_trigger_ok | entry_attempts | executed_trades | sum_pnl_% |
| --- | ---: | ---: | ---: | ---: | ---: |
| `d-03350098db64` | 9 | 9 | 5 | 1 | -1.2000 |
| `d-0e5e7004dccd` | 2 | 1 | 0 | 0 | 0.0000 |
| `d-1ff495203465` | 9 | 7 | 5 | 2 | -2.4000 |
| `d-248d74050cc8` | 0 | 0 | 0 | 0 | 0.0000 |
| `d-a6d9a05da2f1` | 33 | 32 | 23 | 2 | 5.3449 |

## Blocked on allowlist lines (reject reason counts)

### `d-0e5e7004dccd`
- INACTIVE_LINE_BEFORE_CONTACT: 1
### `d-1ff495203465`
- TREND_APPROACH_NOT_FROM_ABOVE: 1
- TREND_RESISTANCE_FROM_BELOW: 1
### `d-a6d9a05da2f1`
- TREND_APPROACH_NOT_FROM_ABOVE: 1

## 2026-08-26 15:00–16:50 UTC (reference window)

### `d-03350098db64`
- `te-d-03350098db64-0004` touch 2026-08-26 15:25:00 UTC | long_ok=yes | reject=NO_C1_CONFIRM_AFTER_FIRST_TOUCH | entry=— | exit=NO_TRADE
- `te-d-03350098db64-0005` touch 2026-08-26 15:50:00 UTC | long_ok=yes | reject=GLOBAL_MAX_ONE_OPEN_LONG | entry=2026-08-26 16:00:00 UTC | exit=NO_TRADE
- `te-d-03350098db64-0006` touch 2026-08-26 15:55:00 UTC | long_ok=yes | reject=GLOBAL_MAX_ONE_OPEN_LONG | entry=2026-08-26 16:05:00 UTC | exit=NO_TRADE
- `te-d-03350098db64-0007` touch 2026-08-26 16:00:00 UTC | long_ok=yes | reject=NO_C1_CONFIRM_AFTER_FIRST_TOUCH | entry=— | exit=NO_TRADE
- `te-d-03350098db64-0008` touch 2026-08-26 16:10:00 UTC | long_ok=yes | reject=NO_C1_CONFIRM_AFTER_FIRST_TOUCH | entry=— | exit=NO_TRADE
- `d-0e5e7004dccd`: no touch events in reference window
- `d-1ff495203465`: no touch events in reference window
- `d-248d74050cc8`: no touch events in reference window
### `d-a6d9a05da2f1`
- `te-d-a6d9a05da2f1-0001` touch 2026-08-26 15:00:00 UTC | long_ok=yes | reject=— | entry=2026-08-26 15:10:00 UTC | exit=CHANNEL_UPPER
- `te-d-a6d9a05da2f1-0002` touch 2026-08-26 15:05:00 UTC | long_ok=yes | reject=NO_C1_CONFIRM_AFTER_FIRST_TOUCH | entry=— | exit=NO_TRADE
- `te-d-a6d9a05da2f1-0003` touch 2026-08-26 15:15:00 UTC | long_ok=yes | reject=GLOBAL_MAX_ONE_OPEN_LONG | entry=2026-08-26 15:25:00 UTC | exit=NO_TRADE
- `te-d-a6d9a05da2f1-0004` touch 2026-08-26 16:15:00 UTC | long_ok=yes | reject=NO_C1_CONFIRM_AFTER_FIRST_TOUCH | entry=— | exit=NO_TRADE
