# Implementation Plan (post Phase A) — gated

## Phase A gate status

| Gate | Status | Notes |
|------|--------|-------|
| OI target + semantics proven | **PARTIAL PASS** | CH pipeline fully proven. Research MySQL DDL/code proven; **live row stats blocked by ACL**. |
| REST OI parity | **PASS** | `DIRECT_5M_COMPATIBLE` for CH `open_interest_5m_history` ↔ REST `openInterest` @ exact `timestamp` (BTC 49/49). |
| Canonical OI key | **PASS (CH)** | `(symbol, bucket_time)` + `source=BYBIT_REST_5M_HISTORY`. Research key separate. |
| Public trades classified | **PASS** | `PUBLIC_TRADES_BACKFILL_PARTIALLY_READY`. |
| No unresolvable dirty overlap | **FAIL unless scoped** | OA: dirty `oi_liquidation_collector/{collector,settings,writer}.py` + systemd. SG: dirty PT backfill/live files. SR dashboard `stoch_signale` / `app.py` **clean** for additive health UI. |
| Safe test/staging | **PASS** | dry-run, fixtures, TestClient, isolated pilot DB/tables. |

## STOP decision

**Do not enter Phase B production-code edits until human confirms:**

1. **OI backfill SoT** = ClickHouse `orderbook_analysis.open_interest_5m_history` (recommended; REST-parity proven).  
   *Not* MySQL `research_open_interest_5m` until credentials + conversion design approved.
2. **Implementation constraint** = **new files only** (wrappers/CLI/health service under SR `dashboard/` + optional new OA script that *imports* existing clean `backfill.py` without editing dirty modules).  
   No edits to dirty OA collector/writer or dirty SG PT modules until those worktrees are clean or patch-isolated.
3. Public-trades UI button stays **disabled** until re-audit reaches automatic/safe READY (wrapper around existing CLI is OK behind gate).

If (1)–(2) rejected → remain blocked: `IMPLEMENTATION_BLOCKED_BY_DIRTY_OVERLAP` and/or `IMPLEMENTATION_BLOCKED_BY_UNPROVEN_SEMANTICS` (research).

## Proposed Phase B scope (after approval)

### B1 OI backfill (new-file wrapper)

- Reuse OA `oi_liquidation_collector/backfill.py` fetch/pagination (already REST 5m).
- Add CLI with `--dry-run|--detect-gaps|--backfill-missing|--verify-only`, advisory lock, closed buckets only, no OI averaging.
- Target CH only; `source` stamped; idempotent inserts.
- Do **not** modify dirty `collector.py`/`writer.py` unless absolutely required (prefer existing `AllowlistedWriter`).

### B2 Public trades

- Do **not** claim auto-restart backfill.
- Add job wrapper invoking existing `run_public_trades_7d_backfill.py` argv-allowlist.
- Gap detect + confirm dialog; disabled in UI until READY gate.
- Avoid editing dirty SG sources; if completion of `gap_fill` path requires those files → STOP.

### B3–B5 Health + dashboard

- New `dashboard/collector_health/` service (read-only process+DB+API evidence).
- Endpoints under `/api/collector-health*` + backfill job APIs.
- Section on `/stoch-signale` — `stoch_signale.html/js` currently clean in git status.
- CSRF + same-origin + `require_auth`; no restart/kill buttons.

### B6–B7 / C–D

- Tests listed in user brief; no prod backfill; atomic commits of **task files only**; no push.

## Freshness policy to encode

| Collector | HEALTHY requires |
|-----------|------------------|
| Public trades | process + WS + DB max advancing + lag&lt;30s + coverage 51/51 + insert_failures=0; drops&gt;0 → DEGRADED |
| OI/Liq | process + health heartbeat &lt;60s + oi5s age &lt;60s (OI value may be flat) |
| OI 5m hist | N/A continuous; gap_count for closed buckets |
| Full OB | process + health connected + raw write progress |
| Liquidations | heartbeat/WS; zero rows ≠ STALE |

## Explicit non-goals until approval

- No collector/dashboard restart.
- No production OI/PT backfill.
- No commit/push in Phase A (this document is plan only).
