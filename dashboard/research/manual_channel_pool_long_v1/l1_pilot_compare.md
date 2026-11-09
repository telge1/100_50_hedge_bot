# L1 pilot compare (channel dynamic exit)

| Metric | Before | After |
| --- | --- | --- |
| Rows | 65 | 79 |
| Executed trades | 0 | 5 |
| Sum gross PnL % (executed) | 0.0000 | 1.7449 |

## Touch structure (after run only)

- Total touch events: 79
- Long triggers allowed: 49
- Hline touches (diagnostic only): 25
- Excluded upper / resistance: 2
- Unclassified / ambiguous trend: 3
- Pilot allowlist IDs: ['d-03350098db64', 'd-0e5e7004dccd', 'd-1ff495203465', 'd-248d74050cc8', 'd-a6d9a05da2f1']

**Exit kinds before**: NO_TRADE=65

**Exit kinds after**: CHANNEL_UPPER=1, NO_TRADE=74, SL=4

**GLOBAL_MAX_ONE_OPEN_LONG before**: 0
**after**: 28

**Structural reject counts (after)**:
- HLINE_NOT_A_LONG_TRIGGER: 25
- INACTIVE_LINE_BEFORE_CONTACT: 1
- TREND_APPROACH_NOT_FROM_ABOVE: 2
- TREND_RESISTANCE_FROM_BELOW: 1
- UPPER_CHANNEL_NOT_A_LONG_TRIGGER: 1

**Newly executed (not traded before)**: te-d-03350098db64-0001, te-d-a6d9a05da2f1-0001, te-d-a6d9a05da2f1-0033, te-d-1ff495203465-0001, te-d-1ff495203465-0008
**Previously blocked by GLOBAL_MAX_ONE_OPEN_LONG, now executed**: —
