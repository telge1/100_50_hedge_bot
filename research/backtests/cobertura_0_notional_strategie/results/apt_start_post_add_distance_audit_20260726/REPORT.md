# APT Start / Post-Add Distance Guard Audit

**Decision: `APT_DISTANCE_GUARDS_RECOVERY_FOUND`**

Baseline fingerprint OK: **True**

## Answers

1. First recovering start distance: **0.06** (`2026-01-19T00:05:00+00:00` @ `1.6447`)
2. Start candles by threshold:
   - 5% → `2026-01-19T00:00:00+00:00` @ `1.7223` (dist=0.0522, recovered=False)
   - 6% → `2026-01-19T00:05:00+00:00` @ `1.6447` (dist=0.0818, recovered=True)
   - 7% → `2026-01-19T00:05:00+00:00` @ `1.6447` (dist=0.0818, recovered=True)
   - 8% → `2026-01-19T00:05:00+00:00` @ `1.6447` (dist=0.0818, recovered=True)
   - 9% → `2026-01-19T00:15:00+00:00` @ `1.6161` (dist=0.0930, recovered=False)
   - 10% → `2026-01-19T10:50:00+00:00` @ `1.5969` (dist=0.1005, recovered=False)
   - 11% → `2026-01-20T08:35:00+00:00` @ `1.5688` (dist=0.1117, recovered=True)
   - 12% → `2026-01-20T17:50:00+00:00` @ `1.5469` (dist=0.1204, recovered=True)
3. Hypothesis 8–10% band: **False** (first=0.06)
4. Start-guard alone sufficient: **True** (n_recovered=5)
5. Post-add-guard alone sufficient: **False** (n_recovered=0)
6. Combination improves over start-only: **False** (combined_recovered=25, start_only=5; combined recoveries reuse the same 00:05 start path — post-add did not change PnL/overlay vs start-only)
7. Best post-add rally buffer among recovered combined/post-only:
   - `start_06__post_03` rally=`0.05963302752293563` min_post_dist=`0.10179290245734698`
8. Best `start_06__post_03` scaled=`0` skipped=`0`
9. Best max_overlay_qty=`474.184` max_gross=`1101.4820136` (baseline overlay=474.184)
10. Best overlay_be_closes=`7` vs baseline `16`
11. Earliest robust candidate: **`start_06__post_03`** start=`2026-01-19T00:05:00+00:00` @ `1.6447` proj_avg=`1.791289264225859` econ=`21.858019294808667` bars=`5187`
12. scale_down vs skip: scale_down recovered=`True` econ=`21.858019294808667`; skip recovered=`True` econ=`21.858019294808667`
13. Transfer recommendation: Prefer start-distance guard first (threshold ≥6% on APT selects ~8.2% realized start distance at 00:05). Post-add scale_down was inactive on the winning path; transfer only after multi-blocker confirmation.

Note: Configured thresholds 9% and 10% are **non-monotonic** — they pick later starts that fail while 6–8% and 11–12% recover.

Decision: `APT_DISTANCE_GUARDS_RECOVERY_FOUND`

