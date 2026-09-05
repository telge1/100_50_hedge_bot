# Full-OB Signal Inventory + Market Profile Contract v1

**Verdict:** `FULL_OB_SIGNAL_AND_PROFILE_CONTRACT_PROVEN`

**Generated (UTC):** 2026-09-04 (read-only audit)

**Safety:** Collector PID `1565672` and OI PID `147111` left untouched; no DB writes; no code changes; no `.tmp` mutation; no full-OB replay.

---

## Phase A — LIVE vs WORKTREE

| Item | Value |
|------|-------|
| Collector PID | `1565672` |
| Start | 2026-09-04 10:03:01 local (UTC+3) = **2026-09-04T08:03:01Z** |
| Command | `raw-archive-only --symbols BTCUSDT,DOGEUSDT` (+ confirm + health-file) |
| Active symbols | BTCUSDT, DOGEUSDT |
| Full-OB topics (confirmed) | `orderbook.full.BTCUSDT`, `orderbook.full.DOGEUSDT` |
| Worktree | `/home/telgenbuescher/projects/orderbook_analyse` |
| Branch / HEAD | `feature/strategy-lab-phase1` / `bcf13edb…` |
| OI PID | `147111` (unchanged since 2026-08-18) |

### LIVE_PROCESS_LOGIC vs CURRENT_WORKTREE_LOGIC

| Logic surface | Identical? |
|---------------|------------|
| `profiles.py` / `watcher.py` / `config.py` (signal + profile) | **Yes for live** — mtimes **before** process start; live memory matches those files |
| Resync checkpoint (`continuity_contract.py`, newer `manager.py`, `on_demand_full.py`, …) | **No** — on disk **after** process start; **not** loaded by PID 1565672 |

Do **not** claim the live collector runs the post-start resync-checkpoint code.

FR env present in the live process (non-secret): enable=true, symbols=BTCUSDT,DOGEUSDT, root under `data/orderbook_raw_shadow/full_ob_edge_flight_recorder`. Profile window / poll / bps thresholds **unset** → code defaults **30 / 20 / 50 / 20 / 75**.

---

## Phase B/C — Signal inventory (manifest-verified)

Sources: every finalized `event_manifest.json` + `profile_context.json` under the FR root (7 events). Markers (`EDGE_RETOUCH`, `EXTENSION`, …) are **intra-event**, not separate genuine signals.

### Classification

| Class | Count | Notes |
|-------|------:|-------|
| **GENUINE_CROSS_IN** | **5** | `CROSS_IN` + `REAL_CROSS_IN` + `edge_entry_crossed=true` |
| BOOTSTRAP_ALREADY_IN_EDGE_ZONE | 2 | Persistent capture from early pilot; **not** genuine |
| EDGE_RETOUCH / EXTENSION / REARM / EVENT_END | n/a as rows | Markers / lifecycle only |
| SYNTHETIC_TEST | 0 | None found |

### Genuine BTCUSDT (3)

| UTC | Local (UTC+3) | Event ID | Side | Edge | Edge px | Mid | Dist bps |
|-----|---------------|----------|------|------|---------|-----|----------|
| 2026-09-03T21:36:31.167060Z | 2026-09-04T00:36:31+03 | `…213631Z_5ddf5e6415` | UPPER | TPO_VAH | 81610 | 81768.55 | 19.39 |
| 2026-09-04T00:42:00.551317Z | 2026-09-04T03:42:00+03 | `…004200Z_0c13abdcf9` | UPPER | TPO_VAH | 81225 | 81386.65 | 19.86 |
| 2026-09-04T08:05:34.762483Z | 2026-09-04T11:05:34+03 | `…080534Z_1fd9a66d36` | LOWER | TPO_VAL | 80635 | 80474.15 | 19.99 |

### Genuine DOGEUSDT (2)

| UTC | Local (UTC+3) | Event ID | Side | Edge | Edge px | Mid | Dist bps |
|-----|---------------|----------|------|------|---------|-----|----------|
| 2026-09-03T23:30:08.359952Z | 2026-09-04T02:30:08+03 | `…233008Z_f0df8d9b04` | UPPER | TPO_VAH | 0.087695 | 0.087735 | 4.56 |
| 2026-09-04T08:05:51.864878Z | 2026-09-04T11:05:51+03 | `…080551Z_2c38905508` | LOWER | TPO_VAL | 0.08707 | 0.086915 | 17.83 |

### Bootstrap (not genuine)

| UTC | Symbol | Event ID |
|-----|--------|----------|
| 2026-09-03T19:52:09.836148Z | BTCUSDT | `…195209Z_e8cb0f6198` |
| 2026-09-03T19:52:09.836148Z | DOGEUSDT | `…195209Z_dc0458f57a` |

Full columns: `signals.csv` / `signals.json`.

All listed events: `research_eligible=false`. Open events (08:05Z BTC/DOGE) still `FIGHT_ACTIVE` under the live collector. Fields `continuous_capture` / `replayable_by_epochs` absent on these manifests (resync contract not in live process).

---

## Phase D — Profile → signal code path

```text
ClickHouseCompletedProfileProvider.load  (profiles.py)
  → last_completed_window(now, window_minutes=30)   # UTC-aligned
  → build_profile(...) Volume VA (POC/VAH/VAL)
  → optional TPO 1m presence from public trades; else volume_proxy_fallback
  → EdgeLevel set: TPO_*/VOL_*/OUTER_*/INNER_*
FullObEdgeFlightRecorder.poll_profiles  (manager.py, every profile_poll_sec=20)
  → EdgeWatcher.set_edges  (frozen during capture → PROFILE_UPDATE_DURING_CAPTURE)
EdgeWatcher.evaluate  (watcher.py)
  → nearest edge by bps; zone IN if dist ≤ capture_bps (20)
  → CROSS_IN when entering IN from non-IN (after outside/rearm rules)
CapturePlan + EventWriter  (capture_plan.py / event_writer.py)
  → event_manifest + profile_context.json (edge_price_at_trigger frozen)
```

Wired in `collector.py` when FR enabled: `ClickHouseCompletedProfileProvider(..., window_minutes=fr_settings.profile_window_minutes)`.

### Answers

**1. Profilart**

- Volume Profile value area is always computed.
- TPO marks attempted from trades; **all live `profile_context.json` show `tpo_source=volume_proxy_fallback`** → `TPO_VAH/VAL` equal volume VA.
- OUTER = max/min of TPO vs VOL; watcher does **not** prefer OUTER — it picks **nearest** among all edges. With fallback, TPO==VOL so OUTER/INNER coincide.
- Edges **frozen** at event start; live poll updates deferred while capturing.

**2. Profilfenster**

- **Rolling last-completed 30m UTC window**, not classic daily/RTH session.
- `ProfileWindow.anchor_mode="composite"` labels the builder window.
- Example for 08:05Z signal: session `[07:30Z, 08:00Z)`, cutoff `08:00Z`.
- Causal: `trades_strictly_before_cutoff` / `completed_window_only` — only data before cutoff, hence before signal.

**3. TPO-Bracket-Dauer**

- **Not 30m brackets.** Intended TPO marks: **1-minute** presence (`tpo_bracket_presence_1m_causal`).
- `bracket_minutes` in meta = **`profile_window_minutes` (30)** = lookback window length.
- Config: `OB_V3_FULL_OB_FR_PROFILE_WINDOW_MIN` default 30; live unset → 30.
- Independent of candle timeframe.

**4. Aktualisierung**

- Profile poll default **20s**.
- During event: edges frozen; pending profile tagged `PROFILE_UPDATE_DURING_CAPTURE`.
- Frozen price in event: `edge_price_at_trigger` / `frozen_edges`.

**5. Triggerzone (proven defaults / live)**

| Value | Role |
|------:|------|
| **20 bps** | Entry / zone `IN` (`capture_distance_bps`); CROSS_IN threshold |
| **50 bps** | Arm / approach keep-alive (`arm_distance_bps`); **not** CROSS_IN |
| **75 bps** | Zone `OUT` / rearm distance (`disarm_distance_bps`) |
| UPPER/LOWER | From nearest `*VAH` → UPPER, `*VAL` → LOWER |
| Bootstrap | Start already `IN` without prior outside → observe only (current code refuses persistent capture; two early events still on disk) |

---

## Phase E — Pflichtantwort

**Welche Market-Profile-Timeframe erzeugt aktuell das Live-Signal?**

```text
Profilart:           Volume Profile VA (+ TPO 1m marks when available;
                     live signals: volume_proxy_fallback ⇒ TPO_* = VOL_*)
Profilfenster:       Last completed 30-minute UTC-aligned rolling window
TPO-Bracket:         1m presence when TPO path works; live used volume fallback
                     (30m is the window, NOT the TPO bracket)
Kerzen-Timeframe:    keine Abhängigkeit
Session:             UTC clock brackets (z.B. 07:30–08:00), kein RTH-Session-Profil
Kausaler Cutoff:     ja (Fensterende; nur Trades/Vol vor Cutoff)
Aktualisierung:      Poll 20s; Kante bei Event-Start eingefroren
Signalrelevante Kante: nearest TPO_VAH/TPO_VAL (≡ VOL bei Fallback), Entry ≤20 bps
```

Live vs Worktree for **this** contract: profile/watcher **identical** (loaded at start). Resync path on disk **differs** from live.

---

## Artefakte

- `REPORT.md` (this file)
- `signals.csv` / `signals.json`
- `profile_signal_contract.json`
- `code_path_audit.md`
- `source_manifest.json`
