# ABSCHLUSSBERICHT — Full OB FR-enabled Live Restart v1

## Verdict

```text
FULL_OB_COLLECTOR_LIVE_HEALTHY_CAPTURE_READY
```

`DESTRUCTIVE_ACTIONS_EXECUTED=false`

## Report points

1. **Alte / neue PID:** 1810262 → **1817696**
2. **Effektiver FR-Env:** `OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=true` (`/proc` + startscript echo)
3. **Runtime-Recorder:** `full_ob_flight_recorder_enabled=true`
4. **Topics:** `orderbook.full.BTCUSDT` + `orderbook.full.DOGEUSDT` (keeper leases)
5. **Levels T+12:** BTC 38176/27231 · DOGE 5047/14209
6. **u/seq:** BTC u [4566695, 4570299] · DOGE u [4566381, 4569986] · seq monoton
7. **Ringbuffer:** T+10 ≥590s beide (BTC 600.0s, DOGE 600.1s); ~3000 msgs/symbol
8. **Watcher/Profil:** lifecycle IDLE; bootstrap `BOOTSTRAP_ALREADY_IN_EDGE_ZONE`; ACTIVE_PROFILE_WATCH_COUNT=0
9. **Signalstatus:** GENUINE_PARENT_SIGNAL_COUNT=0 (kein echtes Signal — ok); Registry aktiv
10. **Queue-Drops / Writer-Fehler:** 0 / 0
11. **Socket / OB1000 / OB200:** uncapped depth0 + Regression PASS
12. **Profil-Eviction-Crash:** 0 (kein IndexError)
13. **Ressourcen:** RSS ~255.79 MB; MemAvail ~13.58 GB; Disk ~377.05 GB
14. **Geschützte PIDs:** 1795773 / 1661773 / 1780509 unverändert alive
15. **Full-OB-Importer:** deaktiviert
16. **Destructive:** false

## Restart

- Mechanismus: `scripts/start_orderbook_v3_raw_archive_btc_doge.sh`
- Stop: SIGTERM (kein SIGKILL)
- Genau eine neue Instanz; `fr_enable=true`
