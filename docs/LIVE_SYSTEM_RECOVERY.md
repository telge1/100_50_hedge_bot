# Live system cross-repository recovery

Recover the Research / Multi-Coin dashboard **code** from Git. This document does **not** cover ClickHouse data, credentials, or running collectors.

## Repository pins (verified 2026-11-27)

| Role | Remote | Branch | Commit | Canonical directory |
|------|--------|--------|--------|---------------------|
| Spread Recovery (dashboard) | `git@github.com:telge1/100_50_hedge_bot.git` | `feature/ema-touch-structure-v2` | `8469f7bc5e717e79df8a66f0468061a3b47d206b` | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev` |
| Trading Research Platform (TRP) | `git@github.com:telge1/trading_research_platform.git` | `main` | `bf6b3a1afbcde2158ee9169802aad6c5d9c97a8b` | `/home/telgenbuescher/projects/trading_research_platform` |
| Pools / pattern + shared libs | `git@github.com:telge1/100_50_hedge_bot.git` | `pools-ob-delta-bot` | `35494db612b944c5cdf5c32be61cf40ca23c58f8` | `/home/telgenbuescher/projects/pools+ob+delta_bot` |

**`pool_pattern/` (production):** tracked on `pools-ob-delta-bot`; last dedicated change `a5c5c040` (*wip(pool-pattern): preserve current pattern research state*). Local `pool_pattern/` matches `origin/pools-ob-delta-bot` (no diff).

**LMV2 engine (production):** `trading_research_platform/indicators/live_micro_lld_v2/` — **12 files** on `origin/main` @ `bf6b3a1`. Depends only on `data.models` (already on GitHub) and internal LMV2 modules.

### Not secured in Git (local-only)

| Item | Location | Impact |
|------|----------|--------|
| `data/timeframes.py` (`1d` in `SUPPORTED_TIMEFRAMES`) | TRP working tree (modified, not committed) | `trp_import.load_trp()` only; **not** required for LMV2 PD1 engine |
| `indicators/liquidity_location/*` local edits | TRP | LLD V1 path; **not** imported by LMV2 engine |
| Pools `backtester/short/dashboard/*` dirty tree | pools workspace | **Not** used when Spread dashboard is canonical |
| `spread_recovery_hedge_short_dev/data/` | gitignored | **Market data** (Bybit archives), not Python |

## Python runtime

- **Version:** Python 3.12.2 (Spread `.venv`)
- **Venv:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv`
- **Install:** `pip install -r dashboard/requirements.txt` (plus optional `orderbook_analyse` per `requirements-symbol-onboarding.txt`)

## Productive `PYTHONPATH` (user unit — canonical for live)

From `~/.config/systemd/user/dashboard.service`:

```text
/home/telgenbuescher/projects/spread_recovery_hedge_short_dev
/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard
/home/telgenbuescher/projects/pools+ob+delta_bot
/home/telgenbuescher/projects/pools+ob+delta_bot/backtester/short
```

Also set:

```text
POOLS_OB_DELTA_BOT_ROOT=/home/telgenbuescher/projects/pools+ob+delta_bot
```

**TRP (not on `PYTHONPATH` in the unit file):** injected at runtime via:

1. `research_charts.trp_import.ensure_trp_on_path()` → `TRP_ROOT` in `dashboard/research_charts/boundary.py`:
   - `/home/telgenbuescher/projects/trading_research_platform`
2. `pool_pattern.market.ensure_paths()` (called from `short_pool_oracle_v1` on PD1 load) — prepends the same `TRP_ROOT` plus pools/backtester paths, `orderbook_analyse/src`, etc.

Optional env: `TRADING_RESEARCH_PLATFORM_ROOT` is used by **publisher tools** in pools; the dashboard boundary currently uses a **fixed** `TRP_ROOT` path.

### Import order (PD1 / multi-coin cold start)

1. `app.py` loads `pd1_landscape_viewer_v1.historical_window_v1`
2. `short_pool_oracle_v1` → `pool_pattern.market.ensure_paths()` → TRP on `sys.path`
3. `viewer_scope_v1` / `multicoin_pd1_scope_v1` → `historical_lmv2_pd_contract_v1` → `live_micro_lld_v2_ch_test` (Spread adapter, on Git)
4. Pool windows call TRP `indicators.live_micro_lld_v2.engine.run_live_micro_lld_v2` via adapter code

## Environment flags (user drop-ins)

Path: `~/.config/systemd/user/dashboard.service.d/`

| File | Variable |
|------|----------|
| (base unit) | `HISTORICAL_LMV2_BATCH_READER=1` |
| `mc4l-c-compact-batch.conf` | `PHASE5S_COMPACT_BATCH_LOADER=1` |
| `mc4r-c2-prefill-reuse.conf` | `PHASE5S_MEMBER_PREFILL_REUSE_ENABLED=1` |
| `mc4s-c2-low-copy.conf` | `PHASE5S_MEMBER_PREFILL_LOW_COPY_ENABLED=1` |
| `mc5j-direct-lookup.conf` | `PHASE5S_BATCH_PREFILL_DIRECT_LOOKUP=1` |
| `mc4o-2-pane-gzip.conf` | `RESEARCH_PANE_GZIP_ENABLED=1` |
| `ob-live-03-on-demand.conf` | `OB_V3_ON_DEMAND_ENABLE=true` |

See also: `dashboard/research_charts/PHASE5S_BATCH_ENV_FLAGS.md` (Spread repo).

## systemd vs. running process

| Source | State |
|--------|--------|
| `/etc/systemd/system/dashboard.service` | **inactive**; `PYTHONPATH` = Spread only (no pools, no TRP) |
| `~/.config/systemd/user/dashboard.service` | Full `PYTHONPATH` + `POOLS_OB_DELTA_BOT_ROOT`; drop-ins above |
| **Live process** (2026-11-27) | Started **manually** Fri 10:26 (`start_tf1d_local.sh` or equivalent), **not** bound to active system unit; `PYTHONPATH` matches **user** unit (includes pools). |

**Recovery implication:** Use the **user** unit definition (or equivalent env) for restarts; the system unit alone is **insufficient** (missing pools + TRP side effects).

## External services & data (not in Git)

- ClickHouse (candles, pool publish SSOT, LMV2 CH test tables as configured)
- MySQL / bot DB (if hedge features used)
- OB-V3 on-demand socket (`orderbook_analyse`, lease/collector)
- MC live **publishers** (ZEC/QNT/LIT/XAU, etc.) under pools `tools/`
- Secrets: dashboard auth, CH credentials, API keys (env files — **do not** commit)

## Controlled recovery steps

1. Clone the three repos at the commits above; checkout branches.
2. Create Spread venv; `pip install -r dashboard/requirements.txt`.
3. Deploy TRP to **`/home/telgenbuescher/projects/trading_research_platform`** @ `bf6b3a1` (or update `boundary.py` / `pool_pattern.market` hardcoded paths consistently).
4. Install user systemd unit + drop-ins **or** export the same `PYTHONPATH` / flags as `start_tf1d_local.sh`.
5. Restore ClickHouse + publisher state per ops runbooks (out of scope here).
6. **Verify code** (no live trading):

```bash
TRP_WT=<trp-worktree @ bf6b3a1>
SPREAD_WT=<spread-worktree @ 8469f7bc>
POOLS_WT=<pools-worktree @ 35494db6>
export PYTHONPATH="$TRP_WT:$SPREAD_WT/dashboard:$SPREAD_WT:$POOLS_WT:$POOLS_WT/backtester/short"
cd "$SPREAD_WT/dashboard"
.venv/bin/python -m pytest -q \
  tests/test_mc5e_viewer_symbols_v1.py \
  tests/test_mc4e_window_symbol_v1.py \
  research_charts/tests/test_mc4o_2_pane_gzip_middleware_v1.py \
  research_charts/tests/test_orderflow_price_impact_v1.py
cd "$TRP_WT" && .venv/bin/python -m pytest -q indicators/live_micro_lld_v2/tests/
```

7. Start dashboard only after CH/OB publishers are healthy (user action).

## Isolated cross-repo test record (2026-11-27)

Worktrees under `/tmp/cross_recovery_*` @ pinned commits only:

- `data.models` → `$TRP_WT/data/models.py`
- `indicators.live_micro_lld_v2.engine` → `$TRP_WT/indicators/live_micro_lld_v2/engine.py`
- `pool_pattern.market` → `$POOLS_WT/pool_pattern/market.py`
- Spread pytest: **27 passed** (mc5e, mc4e, gzip, orderflow)
- TRP LMV2 pytest: **25 passed**
- `test_mc_live_lifetime_resume_v1.py`: **10 passed** (same `PYTHONPATH`, run separately)

**Not verified in isolation:** full `import app` (MySQL/WS stack), end-to-end ClickHouse window build against production data.