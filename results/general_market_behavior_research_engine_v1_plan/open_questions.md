# Open Questions / Unproven Assumptions

1. Why so many Full-OB hours are `GAP` with gap_count=1 — boundary reconnect only or deeper holes? Need per-segment gap_marker audit (read-only) before trusting PARTIAL metrics.
2. Research DB rematerialization lag (trades/OI/liq/OB200 CH stop 2026-08-31) — schedule out of band; until then live FQNs required for Sep analysis.
3. Sep6 00:00–06:59Z public trades gap — archive availability for backfill TBD.
4. Exact ±ms trade↔book attribution accuracy on Bybit vs our receive delays — calibrate on COMPLETE hour.
5. Tick-size & bps band calibration for DOGE vs BTC — must differ.
6. Whether OB200 near-touch features proxy Full-OB well enough for P01–P04 — empirical compare on overlapping COMPLETE hours.
7. OI 5s vs REST 5m disagreement cases.
8. Effective independent sample size after embargo for 30m horizons on ~1–2 weeks data — likely **insufficient** for strong predictive claims; exploration OK.
9. Who/what issued SIGKILL + systemctl stop on live collector (ops) — process risk to trade continuity (monitoring exists now).
10. Flight-recorder public_trades placeholders — do not use FR trades as SoT.
