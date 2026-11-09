# L1 V3N — Channel / Support / TP Architecture Audit (READ-ONLY)

**Scope:** Compare desired manual-channel Long logic with the **actual** L1 research code (V3J/V3K/V3L/V3M).  
**No code changes, no parameter tuning, no new backtests.**

**Modules reviewed:** `run_l1_pilot.py`, `support_line_selection.py`, `pilot_engine.py`, `pool_d2_metrics.py`, `pd1_exact_d2_adapter.py`, `channel_cycle.py`, `line_geometry.py` (probe), `config.py`.

---

## 1. Executive summary

| Theme | Finding |
|--------|---------|
| Support triggers | V3L (`INDEPENDENT_SUPPORT`) largely matches “independent support lines”; V3J (`DYNAMIC_SUPPORT`) still ties eligibility to legacy upper-main price comparison. |
| Channel pairing | **No explicit support↔upper pair model.** `channel_upper_at()` picks the **highest active** trend/hline above entry within **12 %** span — not “the upper line belonging to this support”. |
| TP with channel | `POOL_BOTTOM_NEAR_CHANNEL` or raw `CHANNEL_UPPER_LINE`; exit uses **fixed** channel line id from entry (`_channel_line`). |
| TP without channel | `max(lower_bound)` among D2-qualified bands — **farthest** qualified target, not nearest (V3M confirmed). |
| Variant C without channel | `C_CHANNEL_QUARTER_UNAVAILABLE` when no deckel — fail-closed by design in V3L. |
| Multiple channels | Overlapping uppers can coexist; picker may choose the **highest** line in window, not the inner/smaller channel partner. |

---

## 2. Desired logic vs code (reference)

See user spec §2: independent supports; Fall A (support + matching upper + D2); Fall B (support only → D2 TP); fixed 1.20 % SL; C1; ACTIVE canonical D2 only.

---

## 3. Support line recognition (code trace)

### 3.1 Load drawings

| Step | Location |
|------|-----------|
| Read `drawings.json` | `run_l1_pilot.build_pilot_context()` |
| Parse → `LineDef` | `line_from_drawing()` — trend/hline, `metadata.extend_right`, visible filter |
| Geometry | `line_geometry.LineDef.active_for_candle()`, `anchor_status()`, `price_at()` |

### 3.2 Modes (`support_line_selection.py`)

| Mode | Constant | Entry eligibility |
|------|----------|-------------------|
| V3J original | `LINE_SELECTION_DYNAMIC` | Diagonal trend; not upper-main id; `trend_support_from_above`; **must be below `UPPER_MAIN_CHANNEL_LINE_ID` price at touch** (`TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH`) |
| V3L | `LINE_SELECTION_INDEPENDENT` | Same touch/C1 rules; **no** legacy upper-main compare |
| Legacy pilot | `LINE_SELECTION_OLD` | Fixed `PILOT_ALLOWED_LOWER_TREND_IDS` allowlist |

**Functions:** `assess_long_touch_eligibility()` (`run_l1_pilot.py`), `trend_support_from_above()` (prior bar close above line ± `APPROACH_EPSILON_PCT`).

### 3.3 Touch / C1 / re-arm

- Touch zone: `in_contact_zone()` + `build_touch_events()` — 0.20 % zone, re-arm 0.30 % above line (`TOUCH_*` in `config.py` / pilot).
- Line inactive after finite segment: `build_touch_events()` skips bars where `active_for_candle` is false (V3J+).
- C1: `c1_confirm_indices()` — bullish bar after first touch, close above line; entry next bar open.
- Causality label: `line_causality_at_touch()` — `RETROSPECTIVE_DRAWING` until second anchor time passed.

### 3.4 Independent supports?

| Mode | Independent? |
|------|----------------|
| `INDEPENDENT_SUPPORT` (V3L) | **Yes** for trigger eligibility |
| `DYNAMIC_SUPPORT` (V3J) | **No** — hardcoded upper-main price gate |
| `certify_lower_parallel_trends()` | Research **diagnostic only** (roles in `l1_trend_line_roles.md`); not used for V3L triggers |

### 3.5 extend_right / anchors

- Finite segment: active only on `[t_lo, t_hi]` unless `extend_right` → `ON_EXTENDED_RAY`.
- After second anchor without extend: `AFTER_SECOND_ANCHOR` → inactive (no touches, no channel exit).
- **Important:** Upper-main compare in V3J uses `upper.price_at(touch)` even when upper is **inactive** (extrapolated ray beyond segment) — root cause V3K.

---

## 4. Channel partner assignment (critical)

### 4.1 Is there paired channel detection?

**No.** There is no data structure linking a support `drawing_id` to a “partner” upper line.

### 4.2 How is “upper deckel” chosen?

**Function:** `channel_upper_at(lines, t, entry, exclude_drawing_ids=…)` (`run_l1_pilot.py`)

Rules:

1. Only lines with `active_for_candle(t)` true (no extrapolation past anchor end unless `extend_right`).
2. `price_at(t) > entry` and `price_at(t) ≤ entry * (1 + MAX_CHANNEL_UPPER_SPAN_PCT/100)` (12 % cap).
3. Among **trends**, take **maximum price** (highest line); else among **hlines**, max price.
4. V3L `build_pilot_context` excludes **touch support id** from candidates; `apply_d2_gate_to_pending()` **does not** pass `exclude_drawing_ids` (re-pick may differ if support prices above entry).

**Not checked:**

- Parallelism / slope similarity to support line  
- Time overlap **between this support segment and upper segment** as a pair  
- “Inner vs outer channel” disambiguation  
- Drawing metadata (all `metadata` empty today)

### 4.3 Fixed upper-main id

- `UPPER_MAIN_CHANNEL_LINE_ID = d-22f8d9f720ce` used for: V3J eligibility, role report, `execute_variant` channel cycle **reference line** (`pilot_engine.upper_px` → `channel_line_at_bar(upper_line, …)`).
- **Not** the only deckel for TP: `channel_upper_at` may pick **any** active upper trend in span; in practice August entries often pick `d-22f8d9f720ce` while it remains **geometrically active**.

### 4.4 Can a foreign upper affect TP?

**Yes.** Any active trend/hline above entry within 12 % can become deckel and thus:

- Steer `POOL_BOTTOM_NEAR_CHANNEL` pool choice (pools with `lower_bound ≤ channel_upper` within `CHANNEL_NEAR_PCT` 2 % of channel),
- Or set `CHANNEL_UPPER_LINE` TP / dynamic exit line `_channel_line`.

If multiple uppers overlap, **highest price wins** — may be the **wider** channel, not the support’s local partner.

### 4.5 extend_right=false

When upper line ends at second anchor, `channel_line_at_bar()` returns `None` on later bars → **no dynamic CHANNEL_UPPER exit** after expiry; fixed pool TP from entry still used if set.

---

## 5. TP logic **with** channel deckel

**Pick path (execution truth):** `apply_d2_gate_to_pending()` → `analyze_d2_landscape()` (canonical D2 qualify) → `pick_tp(entry, qualified, channel_upper, channel_line_id)`.

### 5.1 Branch order in `pick_tp()`

1. If `channel_upper` set and `channel_upper > entry`:
   - **POOL_BOTTOM_NEAR_CHANNEL:** among qualified D2 rows with `lower_bound ≤ channel_upper` and within **2 %** of channel (measured from channel price), pick **max(lower_bound)** among those “near” pools.
   - Else if channel room ≥ 0.50 %: **CHANNEL_UPPER_LINE** at `channel_upper`, no pool id.
   - Else: block `NO_TP_CHANNEL_TOO_CLOSE`.
2. If no channel (or channel branch skipped): see §6.

### 5.2 Effective TP & exit

- `pool_tp_for_exit(pick)` — pool bottom for `POOL_BOTTOM_*` modes.
- `pilot_engine.effective_tp_price()` — min(pool_tp, main_upper_at_entry) for filters (variant B channel room uses `_main_upper_at_entry`).
- **`simulate_exit()`:** fixed `pool_tp` + optional **`channel_line` fixed at entry** (`_channel_line` from pick’s `tp_channel_line_id`). Each bar: if `channel_line_at_bar()` active, high ≥ line price → `CHANNEL_UPPER`; pool high ≥ pool_tp → `TP`; SL; same-bar conflict → `AMBIGUOUS`.

**Dynamic channel during trade:** Line object is frozen at entry; price follows geometry bar-by-bar via `channel_line_at_bar()` — **not** re-selected from `channel_upper_at()`. If line goes inactive, channel exit path stops; pool TP remains.

---

## 6. TP logic **without** channel (Fall B)

### 6.1 When is deckel absent?

`channel_upper_at()` returns `(None, None)` when no active line satisfies entry < price ≤ entry×1.12.

**Confirmed Fall 2:** Entry `2026-09-16 18:05 UTC`, support `d-1f70c9798935` — no upper candidates (September: `d-22f8d9f720ce` **inactive**).

### 6.2 Pool-only selection (current)

When `channel_upper is None`, `pick_tp()` (and D2 gate path):

```python
best = max(qualified, key=lambda m: float(m["lower_bound"]))
```

- **Highest `lower_bound`** among qualified canonical Upper-D2 bands (depth ≥ 2, dist ≥ 0.50 %).
- **Not** nearest (`min(lower_bound)`).
- Label: `POOL_BOTTOM_NO_CLEAR_CHANNEL` in execution (`apply_d2_gate`); V3L build path may label `UPPER_D2_POOL_ONLY` when `independent_support=True` — **overwritten** at gate apply.

### 6.3 V3M audit — Sep 16 entry (unchanged facts)

| Field | Value |
|-------|-------|
| Entry | 0.07962 @ 2026-09-16 18:05 UTC |
| Selected TP | 0.09123 (+14.58 %) |
| Nearest qualified D2 bottom | 0.08026 (+0.80 %) |
| Skipped nearer qualified bands | **56** |
| Exit | TP @ 0.09123 (5m replay matches) |

**Spec alignment:** User desired “sinnvolles” pool TP without channel was **not written** as `max(lower_bound)` in research docs; this is an **implementation artifact** from legacy `pick_tp` fallback. V3M: data/execution correct; **selection rule likely not aligned with intuitive “nächstes Ziel”.**

---

## 7. Variant C without channel

**File:** `pool_d2_metrics.analyze_d2_channel_filters(..., independent_no_channel=True)` (V3L).

- Channel quarters require `channel_upper > entry` and bands clipped into `[entry, channel_upper]`.
- Without deckel: `stagger_reason = C_CHANNEL_QUARTER_UNAVAILABLE`; `_d2_stagger_ok = False`.
- Nearness (B) can still use **all** D2 bands above entry (V3L extension).
- **Cannot** apply original “first channel quarter” definition without inventing a channel — fail-closed is intentional (V3L).

**Alternatives (describe only):** (a) keep C disabled without deckel; (b) redefine quarter as % of distance to pool TP (strategy decision); (c) separate variant C′ — requires product sign-off.

---

## 8. Three historical situations (read-only replay)

### Fall 1 — August `2026-08-26 15:10 UTC`, support `d-a6d9a05da2f1`

| Item | Result |
|------|--------|
| Support active at entry | Yes |
| `channel_upper_at` (excl. support) | **0.09179**, id **`d-22f8d9f720ce`** (only candidate in 12 % window) |
| V3L trade TP mode | `POOL_BOTTOM_NEAR_CHANNEL`, pool near upper |
| Exit | **CHANNEL_UPPER** @ ~0.08997 (channel hit before pool TP 0.09151) |
| Pairing | **Incidental overlap** with legacy upper-main segment (still active until 2026-09-03); **not** explicit pair logic |

### Fall 2 — September `2026-09-16 18:05 UTC`, support `d-1f70c9798935`

| Item | Result |
|------|--------|
| Channel deckel | **None** (upper-main inactive; no other active upper in span) |
| TP path | Pool-only via D2 gate → `max(lower_bound)` → 0.09123 |
| V3J | 0 trades (upper-main eligibility block — V3K) |
| V3L | Trade allowed |

### Fall 3 — Multiple channels in `drawings.json`

**Machine-readable pairing:** **None** (no metadata, no pairing API).

**Overlapping trends exist** (sample mid-overlap snapshots, 2026 drawings):

- Many pairs with `d-03350098db64`, `d-a6d9a05da2f1`, `d-22f8d9f720ce`, `d-ac60572bfc69`, etc., at different relative prices.
- At any entry instant, `channel_upper_at` returns **one** id — the **highest** qualifying line, which can be the **outer** August upper even when a **closer** upper trend also overlaps (risk of mis-attribution for TP).

**Cannot** assert which line the trader “meant” as partner without new rules or metadata.

---

## 9. Soll-Ist table

| Anforderung | Aktueller Code | Bewertung | Änderung? |
|------------|----------------|-----------|-----------|
| Neue Supportlinien automatisch erkennen | V3L `INDEPENDENT_SUPPORT` all diagonal trends (excl. upper-main trigger) | **PARTIALLY** (V3J noch blockiert) | Nur wenn V3J still relevant |
| Supportlinien unabhängig behandeln | V3L ja; V3J nein | **PARTIALLY** | Freigabe abhängig vom Produktmodus |
| Mehrere Kanäle unterscheiden | Keine Paarlogik | **MISSING** | Ja, falls Fall A verbindlich |
| Passenden Upper-Partner finden | `max(price)` in 12 % window | **MISSING** / **UNCLEAR** | Ja, wenn “Partner” ≠ “höchste Linie” |
| Upper-Linie + Pools für TP | `POOL_BOTTOM_NEAR_CHANNEL` / `CHANNEL_UPPER` | **ALREADY_CORRECT** *wenn* deckel stimmt | Deckel-Zuordnung evtl. |
| Ohne Upper nur Pools | Ja, D2 qualified | **PARTIALLY** | Auswahlregel max vs min |
| Pool-TP sinnvoll auswählen | `max(lower_bound)` | **MISSING** vs Nutzerintuition | Strategie + Freigabe |
| C-Staffelung ohne Kanal | `C_CHANNEL_QUARTER_UNAVAILABLE` | **ALREADY_CORRECT** (fail-closed) | Strategie für C′ |
| Keine Trades ohne Support | Touches nur auf gezeichneten Linien | **ALREADY_CORRECT** | — |
| Originalanker respektieren | `line_geometry` + `RETROSPECTIVE_DRAWING` | **ALREADY_CORRECT** | — |

---

## 10. Technical plan (no implementation)

### A. Already working (do not rewrite)

- Drawings load, 5m simulation, C1, SL 1.20 %, global one-long, D2 ACTIVE gate, `active_for_candle` / extend_right, retrospective labels.
- V3L independent **support eligibility**.
- Channel TP branches **given a chosen deckel price**.
- Variant C fail-closed without deckel.

### B. Proven gaps (evidence-based)

1. **No support↔upper channel pairing** — highest-line heuristic only.
2. **V3J DYNAMIC** legacy upper-main touch gate (V3K).
3. **Pool-only TP = `max(lower_bound)`** — skips nearer D2 targets (V3M).
4. **`apply_d2_gate_to_pending` / enrich** sometimes use fixed `upper_line` for cycle metrics while deckel comes from `channel_upper_at` — asymmetric references.
5. **Multi-channel confusion** when several actives lie in 12 % band.

### C. Open strategy decisions (need user sign-off)

1. Definition of “passender Upper-Partner” (time overlap? min distance above support? metadata? parallel slope tolerance?).
2. Without channel: **nearest** vs **farthest** vs **first substantial D2 zone** as TP.
3. Variant C without channel: disable vs new quarter definition vs pool-distance quarters.
4. Single runtime mode: V3L only vs keep V3J for regression.
5. Whether `d-22f8d9f720ce` remains special-cased anywhere after pairing exists.

### D. Minimal future touch points (if approved)

| File | Likely change |
|------|----------------|
| `run_l1_pilot.py` | `channel_upper_at` / new `channel_partner_for_support()`; `pick_tp` selection key (`min` vs `max`); align gate + build paths |
| `pool_d2_metrics.py` | `apply_d2_gate_to_pending` pass support id + independent flags consistently |
| `pilot_engine.py` | `effective_tp_price`, cycle upper_px source = entry deckel not legacy id |
| `support_line_selection.py` | Document single production mode |
| `config.py` | Only if new thresholds explicitly approved |
| Tests | Pairing + TP nearest + Sep regression |

**Avoid:** rewriting D2 adapter, SSOT, drawings.json, or A/B/C thresholds without separate approval.

---

## 11. Code path index

```
drawings.json
  → line_from_drawing / LineDef
  → build_touch_events → assess_long_touch_eligibility (mode)
  → c1_confirm_indices
  → channel_upper_at (deckel)
  → pools_at_confirm_open → ACTIVE members
  → apply_d2_gate_to_pending → analyze_d2_landscape → pick_tp
  → enrich_* / execute_variant → simulate_exit
```

---

## 12. STOP

READ-ONLY audit complete. Await explicit approval before any implementation.
