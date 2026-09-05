# ROOT_CAUSE — meta.json.tmp race

## Exact cause

`DurableSpool._persist_meta()` used a **fixed** temp path:

```text
meta.json.tmp
```

Callers on **different threads**:

1. **Append path** — asyncio event-loop thread via `AllowlistedWriter.enqueue()` → `spool.append_many()` → `_persist_meta()`
2. **Ack path** — writer worker via `asyncio.to_thread(_insert_sync)` → `spool.ack_through()` → `_persist_meta()`
3. Health `unacked_stats()` only reads (no meta write) but can interleave with locked section after fix

No lock around meta commit. Classic lost-temp race:

1. Thread A writes/fsyncs `meta.json.tmp`
2. Thread B writes/fsyncs same path (truncates/overwrites)
3. Thread B `os.replace(tmp, meta.json)` succeeds → tmp gone
4. Thread A `os.replace` → **ENOENT** (`[Errno 2]`)

## Secondary bugs

1. **Ack after successful insert inside same try** — meta ENOENT classified as insert failure → **re-insert** → MergeTree physical duplicates for `open_interest_events`.
2. **SpoolMeta/OSError on append** bubbled to collector reconnect loop instead of fail-fast.
3. Meta lacked generation / prev recovery / directory fsync / unique temps.

## Ack regression risk (pre-fix)

Unlocked concurrent persist could write stale `(last_acked_seq, next_seq)` snapshots, theoretically moving ack backwards or shrinking `next_seq`. Live meta currently looks coherent with segments, but race made this unsafe.

## Fix (offline)

- `threading.RLock` around append/ack/meta/close/stats
- Unique temp per commit: `meta.json.tmp.<pid>.<tid>.<gen>.<uuid>`
- File fsync + directory fsync
- Monotone `generation`
- Ack never decreases under lock
- `meta.json.prev` written **after** successful publish (no missing-meta window)
- Corrupt meta → prev + segment seq reconcile; else fail-closed
- Orphan `meta.json.tmp*` quarantined on open (never delete unacked segments)
- `SpoolMetaError` fail-fast in collector; writer acks **after** insert with ack-only retries (no re-insert)
