# ABSCHLUSSBERICHT — Full OB Collector Recovery Live v1

## Verdict

```text
FULL_OB_LIVE_VALIDATION_FAILED
```

`DESTRUCTIVE_ACTIONS_EXECUTED=false`

## 1. Bewiesene Stop-Ursache

**`PROCESS_CRASH`** — `IndexError: pop from empty list` in `nested_profile_signal._evict_if_needed`
(eviction expired profiles but did not remove them from `_profiles[sym]`). No systemd → no auto-restart.
Offline fix: `by_sym.pop(victim.profile_id, None)` + regression test (84 pytest passed).

## 2. Alte / neue PID

- Alt: **1692334** (tot)
- Neu: **1810262** (lebend, single instance)

## 3. Startmechanismus

`nohup` via `orderbook_analyse/scripts/start_orderbook_v3_raw_archive_btc_doge.sh`
(kein systemd-User-Service). Genau ein Restart.

## 4. Instanzzahl

**1** Collector (`SINGLE_COLLECTOR_INSTANCE=true`)

## 5. BTC-/DOGE-Topics

`orderbook.full.BTCUSDT` + `orderbook.full.DOGEUSDT` confirmed (on-demand depth=0 leases during smoke).
Permanent: `orderbook.200.*`.

## 6. Book-Levelzahlen (T+20)

- BTC: **38232** bids / **27082** asks (≫1000)
- DOGE: **5056** / **14201** (≫1000)

## 7. u-/seq-Fortschritt

- BTC u: 4557367 → 4563372
- DOGE u: 4557054 → 4563059
- seq monoton steigend beide Symbole

## 8. Gaps / Reconnects

Source-Gaps **0**; Reconnects **0** während Smoke.

## 9. Queue-Drops

**0**

## 10. Writer-Fehler

**0**

## 11. Ringbuffer-Abdeckung

**nicht beobachtbar** — Flight Recorder im Prozess **disabled** (`OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=false`
durch Startscript-Default vor Dotenv). Kein zweiter Restart erlaubt.

## 12. Bootstrap-/Signalstatus

- `BOOTSTRAP_FILE_CREATED=false`
- Kein `GENUINE_CROSS_IN` / keine Capture-Datei
- Watcher/Signal-Registry inaktiv (FR off)

## 13. Socket-Status

- Path: `/run/user/1000/orderbook_ob1000.sock`
- `depth=0` uncapped, Timeouts 0, Book not crossed
- Full-Book-Lease aktiv während Smoke

## 14. OB1000-/OB200-Status

Beide Regressionen **PASS** (OB200 permanent 200/200; OB1000 acquire ok).

## 15. Alte `.tmp` / Recovery

6 orphan `*.tmp` in 2 Events; bit-identische Kopien; markiert `INCOMPLETE_AT_PROCESS_STOP`;
`research_eligible=false`; Originale unverändert.

## 16. Ressourcen

MemAvailable ~14.1 GB; Disk free ~377 GB; Collector RSS ~91.63 MB.

## 17. Andere PIDs unverändert

OI/Liq **1795773**, PT **1661773**, Dashboard **1780509** — alle weiterhin alive.

## 18. Full-OB-Importer

Weiterhin **deaktiviert / nicht laufend**. Keine offenen `.tmp` importiert.

## 19. Destructive Actions

`DESTRUCTIVE_ACTIONS_EXECUTED=false`

## Warum nicht CAPTURE_READY

Hard Book/Socket-Gates grün, aber Capture-Stack (FR/Watcher/Ringbuffer/Signal-Registry) nicht aktiv.
Startscript-Fix für FR-`.env`-Übernahme ist offline eingespielt; **nächster** kontrollierter Restart
(außerhalb dieses Auftrags) nötig für `FULL_OB_COLLECTOR_LIVE_HEALTHY_CAPTURE_READY`.
