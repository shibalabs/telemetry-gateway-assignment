# AI usage record

## Tools used

- **Claude Code** (CLI, model Claude Opus 5) for the whole task: reading the
  specs, diagnosing the defects, writing the fixes and tests, and drafting
  `DECISIONS.md` and this file.

No other AI tool was used. Every change was reviewed before it was committed,
and every claim in `DECISIONS.md` is backed by a command that was actually run.

## Important prompts and prompt summaries

The session began with the assignment text and the repository URL, then followed
this sequence:

1. **"Read `docs/protocol.md`, `docs/runtime-contract.md` and `docs/api.md`
   first, then the source."** Reading the normative documents before the code
   mattered: two defects (the `deviceTime` ordering guard and the pre-commit
   publish) look like ordinary code until you have read the sentence they
   contradict.

2. **"Map each of the six problem areas to the specific line that causes it."**
   This produced the diagnosis: the `UNIQUE (device_id, sequence)` constraint in
   `migrations.py`, the `WHERE excluded.device_time > ...` clause in
   `database.py`, the `preview_state`-then-publish order in `service.py`, the
   sequential `await client.send_json(...)` loop in `realtime.py`, and the
   top-level one-shot `loadSnapshot()` in `app.js`.

3. **"One pull request per problem area, each independently green, with tests
   that fail against the unfixed code."**

4. **"Before claiming a fix works, reproduce the original failure."** This
   drove the check where the pre-fix hub is reconstructed and shown to block
   forever, and the reruns of each test file against the original source.

5. **"Run the real application with the chaos simulator and audit the resulting
   database against the invariants."** This is what turned up defect #6.

## Generated output rejected or corrected

Five substantive corrections were made to AI-generated work during this session.

1. **A test that hung the whole suite.** The first version of
   `tests/test_api_realtime.py` asserted "no message was published" with a
   helper that called `socket.receive_json()` and caught the exception. That
   call blocks forever when nothing is queued, and the run had to be killed
   after 300 seconds. Rewritten to assert absence by ordering instead: force a
   known state change, then require that the *next* message received is that
   marker. If the preceding operation had published, its message would arrive
   first.

2. **Tests that silently depended on a later fix.** Two tests written on the
   PR #1 branch asserted that current state advanced, which needs the PR #2
   ordering fix. They failed, and the fix was *not* to pull PR #2 forward but to
   scope each test to the behaviour its own PR owns, so every branch is
   independently green.

3. **An assertion against an invented response shape.** A test asserted
   `body == {"accepted": ..., "duplicate": ..., "currentChanged": ...}` by exact
   equality. `IngestResult.to_api` also emits a `state` key on a change — real
   pre-existing behaviour that `docs/api.md` does not show. Corrected to assert
   the documented fields rather than deleting a working feature to match a
   wrong test.

4. **An overstated memory-bound claim.** The unit tests showed the per-client
   queue never exceeding its limit, which made "memory is bounded by
   `WS_CLIENT_QUEUE_LIMIT`" look proven. Testing against the running server
   showed a stalled client absorbing several hundred kilobytes in the transport
   before backpressure reached the queue at all — not dropped at 1,500 messages,
   dropped by 12,000. The claim in `DECISIONS.md` was corrected to state the
   real bound, and the gap is listed as a residual risk.

5. **A security warning that needed a judgement, not a reflex.** A tooling hook
   flagged `new Function()` with an interpolated string in the dashboard test
   harness as a code-injection risk. The warning is correct in general; here the
   only interpolated text is the repository's own `app.js` read from disk in a
   test. Kept, with a comment recording why it is safe, rather than either
   ignoring the warning or abandoning the approach.

One assumption also proved wrong and is worth recording: the WebSocket tests all
passed, which was taken as evidence the socket worked. It only proved
Starlette's `TestClient` worked. The endpoint returned `404` in the real runtime,
and that was found by opening a raw socket to the running server — not by any
test.

## Verification performed

Nothing was accepted because it looked right. Each item below was run.

**Test suites**

- `python -m pytest` — 57 tests passing.
- `node --test tests/dashboard.test.mjs` — 11 tests passing.
- `python -m compileall -q telemetry_gateway simulator.py tests`.

**Each fix checked against the unfixed code**

- Ordering tests re-run with `database.py` reverted: 6 of 8 failed.
- Dashboard tests re-run against the original `app.js`: all failed, including
  the one that matters, "the snapshot is fetched again after every
  reconnection".
- The pre-fix hub was reconstructed and driven with a stalled client;
  `publish` never returned, confirming the blocking bug rather than assuming it.

**Whole-system behaviour**

- `python -m telemetry_gateway` with `simulator.py --devices 4 --chaos` for 25
  seconds: 295 accepted changes, 45 duplicates correctly refused, 35 stale
  events recorded without moving state, 14 restarts, 0 errors.
- The resulting database was audited directly against the invariants: 0
  duplicate logical events, 0 rows where `current_state` disagreed with the
  newest event by `(generation, sequence)`, 0 generation collisions, 0 orphaned
  rows, 330 events retained across 18 boots, schema at version 2 with the
  unique key on `(device_id, boot_id, sequence)`.

**Migration safety**

- A database was built with the *original* migration code, populated, then
  opened with the fixed gateway: 3 rows in, 3 rows out with identical values and
  ids, current state preserved, and a post-restart `sequence 1` event then
  accepted — the exact case the old key swallowed.

**Live WebSocket behaviour**

- Raw socket handshake against the running server, before and after PR #6:
  `404` then `101`.
- 12,000 events posted while a client that completes the handshake and never
  reads a byte was connected. No request blocked, the stalled client was dropped
  with the configured warning, the healthy client received every published
  message, and current state was correct afterwards.
