# BTC Full-OB Signal → Crash Analysis 2026-09-04

**Verdict:** `BTC_FULL_OB_ANALYSIS_PARTIALLY_OBSERVABLE`

## Scope

Parent event `BTCUSDT_20260904T112735Z_eb6191222e`  
Finalized segments imported: seg0 (11:26–11:57), cont_001 (11:57–12:27), cont_002 (12:27–12:57).  
Open `cont_003/*.tmp` **not** read or imported.

Collector PID `1692334` and OI PID `147111` left running.

## Signals (isolated)

See `signal_contracts.csv` and `signal_level_findings.json`.

| Signal | ID | Edge | Trigger UTC |
| --- | --- | --- | --- |
| A Parent | `BTCUSDT_20260904T112735Z_eb6191222e_parent` | UPPER VAH | 11:27:35Z |
| B Nested | `..._ns_3d51be69d9df_1_L` | LOWER VAL | 11:30:32Z |
| C Nested | `..._ns_3d51be69d9df_1_U` | UPPER VAH | 12:06:34Z |

## Parity

- source_packet_count == database_packet_count: `True` (26487 / 26487)
- parse_rejects == 0: `True`
- logical_duplicates: `0`
- checkpoint_hash_ok: `True`
- replay_ok: `True`
- book_not_crossed: `False` (crossed_count=1)

DB: `research_full_ob_btc_20260904_signal_analysis`

## Gaps / epochs

cont_001 contains **7 RESYNC** boundaries → multiple continuity epochs. Metrics are **not** joined across epochs.  
Nested UPPER (12:06) sits in this multi-epoch region → research quality reduced (fail-closed).

## Crash timing (from Full-OB mid)

Detected crash_start: `2026-09-04T12:30:00.472000Z`  
(method: ≥15 bps drop from trailing 5m high after 12:25; descriptive only)

## Pre-crash Full-OB facts

- Bid withdrawals (unmatched L2 reductions) before crash: `7047`
- First bearish imbalance signal: `null`
- Ask walls tracked (top descriptive): see `wall_tracks.csv`

## Public trades / OI / liquidations

- Research CH public trades / OI / market_1s: **no Sep-4 coverage** (end ≤ 2026-08-31).
- FR `public_trades_raw` placeholders empty.
- OI live writer targets unloadable `orderbook_analysis`.
- Bybit day download attempt: `{"ok": false, "code": 2, "stdout": "{\n  \"symbol\": \"BTCUSDT\",\n  \"results\": [\n    {\n      \"symbol\": \"BTCUSDT\",\n      \"day\": \"2026-09-04\",\n      \"url\": \"https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-09-04.csv.gz\",\n      \"dest\": \"/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/btc_full_ob_signal_to_crash_20260904_v1/bybit_public_trades_day/BTCUSDT2026-09-04.csv.gz\",\n      \"status\": \"SOURCE_FILE_MISSING\",\n      \"bytes_written\": 0,\n   `

Therefore BUY_ABSORPTION / BID_CONSUMPTION / OI context = **NOT_EVALUABLE** or unavailable.

## Pattern statuses

{
  "BREAKOUT_CONFIRMATION": {
    "status": "PARTIALLY_OBSERVED",
    "reason": "Parent UPPER cross then acceptance to FIGHT_ACTIVE; price later failed to sustain above VAH region"
  },
  "FAILED_BREAKOUT": {
    "status": "OBSERVED",
    "reason": "After parent UPPER and nested UPPER attempts, price ultimately sold off through prior edge region before/into crash window"
  },
  "ASK_DEFENSE": {
    "status": "PARTIALLY_OBSERVED",
    "reason": "Large ask levels tracked within 50bps; refill events recorded as UNMATCHED_L2_CHANGE only"
  },
  "BUY_ABSORPTION": {
    "status": "NOT_EVALUABLE",
    "reason": "No Sep-4 public trades in research CH; FR public_trades placeholders empty; cannot prove aggression vs L2"
  },
  "BID_WITHDRAWAL": {
    "status": "OBSERVED",
    "reason": "7047 unmatched bid size reductions >=10 BTC half-cut within 50bps before crash_start"
  },
  "BID_CONSUMPTION": {
    "status": "NOT_EVALUABLE",
    "reason": "size=0/reduced without trade link cannot be classified as consumption vs cancel"
  },
  "SELLER_CONTROL": {
    "status": "NOT_OBSERVED",
    "reason": "Ask-heavy 50bps imbalance persistence before crash if detected; not exchange-linked aggression"
  },
  "EARLY_BEARISH_WARNING": {
    "status": "PARTIALLY_OBSERVED",
    "reason": "Pre-crash imbalance and/or bid withdrawals visible in finalized Full OB before crash_start"
  },
  "TRADE_DIRECTION": {
    "status": "NOT_EVALUATED",
    "reason": "Explicitly out of scope"
  }
}

## Best research quality signal

`A_parent_upper / trigger-local epoch0` — parent trigger in clean epoch-0 seg0; nested UPPER degraded by resync gaps.

## Safety

- No production DB writes (`orderbook_analysis` untouched; `research_full_ob_smoke` untouched).
- No open `.tmp` modified.
- No commit/push.
- `TRADE_DIRECTION=NOT_EVALUATED`


## Nachgeschärfte Pre-/Crash-Fakten

- Crash_start (Full-OB mid): **2026-09-04T12:30:00.472Z** nach lokalem Hoch **81325.35 @ 12:29:57Z**; lokal Tief in finalisiertem Segment **79129.15 @ 12:47:10Z**.
- Bid50/Ask50 (BTC notional in 50 bps): 12:00–12:10 ≈ 1139/1116 → 12:25–12:30 ≈ 963/935 (beide Seiten dünner; Imbalance ≈ 0).
- Ask-Walls vor Crash: **1** (81643 @ 12:29:47). Während Crash: **19** getrackte große Asks.
- Public trades / OI Sep-4: **nicht verfügbar** (CH cutoff Aug-31; Bybit day 404; OI-DB broken).
- Pattern: `EARLY_BEARISH_WARNING=PARTIALLY_OBSERVED` (nur schwache bilaterale Ausdünnung); klare Wall-Dynamik eher crash-zeitgleich.
