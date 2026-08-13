# Engineering decisions

Six defects were repaired, one per pull request, each merged into `main` with
its own tests. A seventh pull request added a runtime dependency that the
WebSocket contract turned out to require.

## Invariants identified

These are the properties the code must hold. Each one is asserted by at least
one test.

**Identity and history**

1. A logical event is `(deviceId, bootId, sequence)`. The raw audit table holds
   at most one row per logical event.
2. Redelivering a logical event any number of times changes nothing: no second
   audit row, no state change, no realtime message. The response reports
   `duplicate: true`.
3. `sequence` restarts at 1 for each boot, so `(bootId, sequence)` pairs from
   different boots are distinct events and must both be recorded.
4. Raw history is append-only. An event that loses the recency comparison is
   still recorded; only `current_state` is left alone.
5. Telemetry for an unregistered boot is rejected with `409 unknown_boot` and
   leaves no trace in history or state.

**Boot generations**

6. `generation` is server-assigned, strictly increasing per device, and unique
   within a device.
7. Re-registering an existing `(deviceId, bootId)` returns the original
   generation and never allocates a new one.
8. Generations are per device: two devices independently start at 1.
9. `bootId` is opaque. Its text ordering carries no meaning.

**Recency**

10. For one `(deviceId, metric)`, the newer event is the one with the higher
    `generation`; ties are broken by higher `sequence`.
11. `deviceTime` never participates in a recency decision. It is recorded and
    returned as diagnostics only.
12. Current state never moves backward, whatever order requests arrive in.

**Persistence and publication**

13. The database is the source of truth. A realtime message is published only
    after a successful commit, and only when current state actually changed.
14. A failed transaction publishes nothing.
15. The published payload is the row the database committed, so its
    `generation` and `receivedAt` match `/api/devices`.

**Realtime delivery**

16. One client's behaviour cannot delay or block another, or the ingest path.
17. Memory held for a client that cannot keep up is bounded.
18. A client past the configured buffer limit is dropped, not buffered.
19. `/api/devices` is sufficient to reconstruct authoritative state at any
    time, because messages are never replayed.

## Incidents fixed

### 1. A restarted device went silent — PR #1

`telemetry_events` declared `UNIQUE (device_id, sequence)`, but the protocol's
logical event identity is `(deviceId, bootId, sequence)` and `sequence` restarts
at 1 for every boot. Every event from a new boot therefore collided with the
previous boot's row and was discarded as a duplicate. A restarted device stayed
invisible until its sequence climbed past the previous boot's high-water mark —
the more readings a device had sent before rebooting, the longer it stayed dark.

Migration `002` rebuilds the table on `UNIQUE (device_id, boot_id, sequence)`.
Because SQLite cannot drop a UNIQUE constraint in place, the table is copied to
a new one and renamed; every row keeps its original `id`, so the audit history
is preserved exactly.

### 2. Wrong clocks and late packets corrupted current state — PR #2

The `current_state` upsert guarded on
`excluded.device_time > current_state.device_time`. `deviceTime` is supplied by
the device, which the protocol says may be early, late, or far in the future.
Two failures followed. A device whose clock read 2099 pinned current state
permanently: every subsequent genuine reading compared as older and was dropped.
A device whose clock ran slow, or an event delayed in transit, could overwrite
newer state.

The guard now compares `(generation, sequence)`, the ordering the protocol
defines. `deviceTime` is still stored and returned, but never decides anything.

### 3. The socket announced state the database never accepted — PR #3

`TelemetryService.ingest` called `preview_state` and published **before**
writing anything:

```python
state = self._repository.preview_state(event, received_at)
await self._publisher.publish(state)
return self._repository.ingest(event, received_at)
```

So every duplicate re-announced a change, every superseded event announced a
value the database had correctly refused, and a failed transaction still
produced a successful realtime update. The announced payload was the request
rather than the committed row, so its `generation` could disagree with
`/api/devices`.

The transaction now completes first, and a message is published only when the
commit reports that current state changed. `preview_state` had no remaining
caller and was removed.

### 4. One stalled dashboard froze the whole gateway — PR #4

`RealtimeHub.publish` awaited `send_json` for each client in turn, inside the
telemetry request's call stack. A single client that stopped reading was enough
to stall everything: the awaiting send never completed, the `POST /api/telemetry`
that triggered it never returned, and clients later in the iteration received
nothing. The only buffer was the transport's, which grows without bound.

Reproduced before fixing — with the original hub, `publish` never returned:

```
OLD HUB: publish BLOCKED on the stalled client (timed out)
```

Each connection now owns a bounded `asyncio.Queue` and its own delivery task.
`publish` only calls `put_nowait`, so it never waits on a socket. A client that
falls more than `WS_CLIENT_QUEUE_LIMIT` messages behind is removed from the
registry and closed in a background task with code `1013` ("try again later"),
and its queue is drained. Closing is bounded by a timeout, because a dropped
client's socket may itself be unresponsive.

### 5. Reconnecting left the dashboard showing stale values — PR #5

`loadSnapshot()` ran once at page load and never again. Since messages are not
replayed, everything published while a connection was down was simply lost: a
device that rebooted during the gap appeared to keep reporting its old boot
forever.

The snapshot is now fetched on every `open` event. Incoming messages are applied
through the same `(generation, sequence)` rule as the server, so a message that
raced an in-flight snapshot no longer loses to the older row, and a stale
message cannot move a card backward. Reconnection backs off from 1s to 15s so a
client the server just dropped does not immediately return at the same rate.

### 6. The WebSocket endpoint returned 404 in the real runtime — PR #6

Found by probing the running server rather than by a test. `requirements.txt`
pinned `uvicorn` with no WebSocket protocol library, and uvicorn ships none:

```
WARNING:  No supported WebSocket library detected.
INFO:     127.0.0.1:7351 - "GET /ws HTTP/1.1" 404 Not Found
```

The realtime channel was dead whenever the app actually ran. The test suite
missed it because Starlette's `TestClient` implements the WebSocket transport
itself and never goes through uvicorn — so every WebSocket test passed against
a server that could not accept a single real connection.

`websockets==17.0.1` was added. It is pure Python, MIT licensed and local, so
the local-only and no-paid-dependency constraints hold.

## Design choices and trade-offs

**Ordering in SQL, not in Python.** The recency comparison lives in the
`ON CONFLICT ... WHERE` clause, so the read, the decision and the write are one
atomic statement. A read-then-decide-then-write sequence in Python would need
the comparison to hold across statements. `rowcount` then tells the service
whether state changed, which is the same fact the realtime decision needs.

**A new migration rather than editing migration 001.** Editing an applied
migration would silently diverge from any database already on disk. `002`
rebuilds the table instead, which costs a table copy on first start but keeps
existing installs correct and preserves history, as required.

**Drop slow clients rather than shed or coalesce messages.** The runtime
contract explicitly permits dropping past a buffer limit. Dropping is safe
precisely because the socket is not the source of truth: the client reconnects
and refetches. Coalescing per `(device, metric)` would have kept slow clients
alive with less data, but adds a staleness policy for no required benefit.

**Merge the snapshot instead of replacing the map.** A reconnect's snapshot can
be older than a message already applied. Entries the snapshot omits are dropped
(it is authoritative about which devices exist), but an entry the client already
holds is kept when it is strictly newer by `(generation, sequence)`.

**Dashboard tests on Node's built-in runner.** Half of problem area 6 lives in
`app.js`, so it needed real tests. `node --test` needs no packages and no
browser; `app.js` is evaluated with stubbed `document`, `window`, `WebSocket`
and `fetch`. `scripts/check.sh` skips them when Node is absent, so the Python
checks remain the baseline.

**One pull request per problem area.** Each branch is independently green. Where
a test would have depended on a later fix, it was scoped to the behaviour its
own PR owns rather than left failing.

## Schema and API compatibility

**Schema.** Migration `002` changes only the uniqueness constraint on
`telemetry_events`; columns, types and `id` values are unchanged. Applying it to
a populated pre-fix database was tested directly: 3 rows in, 3 rows out, same
values, current state intact, and a restart at `sequence 1` then accepted. The
new key is strictly weaker than the old one, so no existing row can violate it
and the migration cannot fail on real data. It is forward-only; there is no down
migration.

**API.** No request or response shape changed, and no endpoint was added or
removed. Status codes are unchanged.

Two behaviours visibly change, both of them the point of the exercise:

- A duplicate now returns `duplicate: true` where a post-restart event used to
  be misreported as one. Clients keyed on that flag see fewer false duplicates.
- `currentChanged` is now `false` for delayed and superseded events that
  previously flipped it to `true`. This is the documented contract.

`POST /api/telemetry` includes an extra `state` object in its response when
current state changed. That predates this work and is left in place; it is
additive and documented consumers ignore unknown keys.

`preview_state` was removed from the repository protocol. It is an internal
seam with no HTTP surface.

The only runtime requirement added is the `websockets` package.

## Verification

- 57 Python tests and 11 dashboard tests, all passing.
- Every fix was checked against the unfixed code first. The ordering tests: 6 of
  8 failed before PR #2. The hub: `publish` provably blocked forever.
- Chaos simulator, 4 devices, 25 seconds: 295 accepted changes, 45 duplicates
  correctly refused, 35 stale events accepted into history without moving state,
  14 device restarts, 0 errors, 0 unexpected 409s.
- Direct invariant audit of the resulting database: 0 duplicate logical events,
  0 rows where `current_state` disagreed with the newest event, 0 generation
  collisions, 0 orphaned rows, 330 raw events retained across 18 boots.
- Live slow-client test against the running server: 12,000 events posted while a
  client that never reads was connected. No request blocked, the stalled client
  was dropped with the configured warning, and the healthy client received every
  published message.
- Legacy database upgrade path exercised end to end.

## Remaining risks and incomplete work

**Bounded, but not only by the queue.** The per-client queue caps what the
application buffers, but the socket transport keeps its own buffer underneath.
In the live test the stalled client absorbed several hundred kilobytes before
backpressure reached the queue: at 1,500 messages it had not yet been dropped,
by 12,000 it had. Memory is bounded, but the true bound is the transport's
high-water mark plus `WS_CLIENT_QUEUE_LIMIT` messages, and lowering the limit
does not shrink the transport's share. Deliberately left as is — bounding that
would mean reaching into uvicorn's internals.

**Repository calls block the event loop.** `TelemetryStore` is synchronous and
serialises every operation on one connection behind an `RLock`, called directly
from async handlers. For a local gateway with a handful of devices this is fine
and keeps the transactional reasoning simple, but throughput is capped by one
writer and a slow query would stall the loop. A thread executor would be the
next step if device counts grew.

**Restart-time races are not covered by a test.** A boot registration and its
first telemetry arriving concurrently are serialised by the store's lock, so the
invariants hold, but the concurrent path is argued rather than tested. The
`asyncio` tests drive the hub deterministically rather than under real
contention.

**The dashboard is tested through a stub, not a browser.** The harness covers
`app.js`'s logic — snapshot on reconnect, merge rules, backoff, message
filtering — but not rendering or real browser event timing. A browser check of
the running dashboard was not possible in this environment.

**`INSERT OR IGNORE` treats a same-identity event with a different payload as a
duplicate.** The protocol states a device does not reuse a sequence for two
metrics, so this cannot arise from a conforming device, and it is the correct
response to redelivery. A misbehaving device would be silently ignored rather
than reported.

**No down migration**, and the `002` rebuild copies the whole table on first
start. On a large local database that is a one-off pause at startup.
