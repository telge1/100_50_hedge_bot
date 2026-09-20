# Leakage & Overfitting Prevention Contract

1. Features ≤ decision_t event-time; outcomes > decision_t.
2. No threshold tuning on test or on full sample without nested freeze.
3. Chronological splits only; no shuffle of adjacent seconds.
4. Walk-forward: Discover → Validate → Test (e.g. days D1–Dk / Dk+1–Dm / Dm+1–Dn).
5. Purge/embargo overlapping episodes (embargo ≥ max horizon used in labels).
6. Deduplicate highly overlapping episode starts (min spacing rule).
7. DOGE is not independent N if same UTC windows; report effective sample after overlap discount.
8. Document every excluded GAP/PARTIAL window and reason.
9. No cherry-pick of spectacular charts into discovery set without registry.
10. Prefix parity test: features(t) identical when built on data truncated at t vs full day file truncated.
11. Research rematerialization after t must not rewrite past feature rows silently — version build_id.
