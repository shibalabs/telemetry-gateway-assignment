/**
 * Dashboard recovery tests.
 *
 * docs/runtime-contract.md requires the dashboard to fetch the current-state
 * snapshot when it starts, fetch it again after every successful WebSocket
 * reconnection, and treat incoming messages as updates to that snapshot.
 *
 * Run with:  node --test tests/
 * These exercise the real telemetry_gateway/static/app.js against a minimal
 * DOM, WebSocket and fetch harness, so no browser or dependency is needed.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';
import test from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const APP_SOURCE = readFileSync(
  join(here, '..', 'telemetry_gateway', 'static', 'app.js'),
  'utf8'
);

function element() {
  const classes = new Set();
  return {
    textContent: '',
    innerHTML: '',
    className: '',
    classList: {
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
      contains: (name) => classes.has(name),
    },
  };
}

/** Boots app.js with stubbed browser globals and hands back its internals. */
function loadDashboard({ snapshots = [[]] } = {}) {
  const nodes = {
    '#grid': element(),
    '#empty': element(),
    '#connection-status': element(),
    '#error': element(),
  };

  const sockets = [];
  const timers = [];
  const fetchCalls = [];
  const pendingSnapshots = [...snapshots];
  let lastSnapshot = snapshots[snapshots.length - 1] ?? [];

  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.listeners = new Map();
      this.closed = false;
      sockets.push(this);
    }
    addEventListener(type, handler) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(handler);
    }
    close() {
      this.closed = true;
    }
    dispatch(type, event) {
      for (const handler of this.listeners.get(type) ?? []) handler(event);
    }
  }

  const win = {
    location: { protocol: 'http:', host: '127.0.0.1:3000' },
    setTimeout(callback, delay) {
      timers.push({ callback, delay });
      return timers.length;
    },
    clearTimeout() {},
    addEventListener() {},
  };

  async function fakeFetch(url, options) {
    fetchCalls.push({ url, options });
    if (pendingSnapshots.length > 0) lastSnapshot = pendingSnapshots.shift();
    const devices = lastSnapshot;
    return {
      ok: true,
      status: 200,
      json: async () => ({ devices }),
    };
  }

  // app.js is a plain browser script, so it is evaluated with the browser
  // globals supplied as parameters and its internals returned. The only
  // interpolated text is this repository's own source file, read from disk;
  // no external or user-supplied input reaches here.
  const factory = new Function(
    'window',
    'document',
    'WebSocket',
    'fetch',
    `${APP_SOURCE}
     return { isNewer, applyState, states, loadSnapshot, connect, render };`
  );

  const app = factory(
    win,
    { querySelector: (selector) => nodes[selector] },
    FakeWebSocket,
    fakeFetch
  );

  return { app, nodes, sockets, timers, fetchCalls };
}

function reading(overrides = {}) {
  return {
    deviceId: 'device-01',
    metric: 'temperature',
    bootId: 'boot-a',
    generation: 1,
    sequence: 1,
    deviceTime: '2026-08-12T09:00:00+00:00',
    receivedAt: '2026-08-12T09:00:01+00:00',
    value: 21.4,
    ...overrides,
  };
}

/** Let queued promise callbacks run. */
const flush = () => new Promise((resolve) => setImmediate(resolve));

function message(data) {
  return { data: JSON.stringify({ type: 'device.state.changed', data }) };
}

function currentValue(app, key = 'device-01:temperature') {
  return app.states.get(key)?.value;
}

test('the snapshot is fetched when the dashboard starts', async () => {
  const { sockets, fetchCalls } = loadDashboard({
    snapshots: [[reading({ value: 21.4 })]],
  });

  sockets[0].dispatch('open');
  await flush();

  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, '/api/devices');
});

test('the snapshot is fetched again after every reconnection', async () => {
  const { app, sockets, timers, fetchCalls } = loadDashboard({
    snapshots: [[reading({ sequence: 1, value: 21.4 })]],
  });

  sockets[0].dispatch('open');
  await flush();
  assert.equal(fetchCalls.length, 1);

  // The connection drops and the dashboard reconnects.
  sockets[0].dispatch('close');
  assert.equal(timers.length, 1, 'a reconnect was scheduled');
  timers[0].callback();
  assert.equal(sockets.length, 2, 'a new socket was opened');

  sockets[1].dispatch('open');
  await flush();

  assert.equal(fetchCalls.length, 2, 'the snapshot is refetched on reconnect');
  assert.equal(currentValue(app), 21.4);
});

test('state missed while disconnected is restored from the snapshot', async () => {
  // While the socket is down the device reboots and reports a new value.
  const { app, sockets, timers } = loadDashboard({
    snapshots: [
      [reading({ generation: 1, sequence: 3, value: 21.4 })],
      [reading({ bootId: 'boot-b', generation: 2, sequence: 1, value: 30.5 })],
    ],
  });

  sockets[0].dispatch('open');
  await flush();
  assert.equal(currentValue(app), 21.4);

  sockets[0].dispatch('close');
  timers[0].callback();
  sockets[1].dispatch('open');
  await flush();

  assert.equal(currentValue(app), 30.5, 'the missed reboot is recovered');
  assert.equal(app.states.get('device-01:temperature').generation, 2);
});

test('a live message updates the snapshot it arrives after', async () => {
  const { app, sockets } = loadDashboard({
    snapshots: [[reading({ sequence: 1, value: 21.4 })]],
  });

  sockets[0].dispatch('open');
  await flush();
  sockets[0].dispatch('message', message(reading({ sequence: 2, value: 22.9 })));

  assert.equal(currentValue(app), 22.9);
});

test('a stale message never moves current state backward', async () => {
  const { app, sockets } = loadDashboard({
    snapshots: [[reading({ generation: 2, sequence: 5, value: 25.0 })]],
  });
  sockets[0].dispatch('open');
  await flush();

  // A lower sequence in the same boot, and an older boot with a much higher
  // sequence and a future clock: neither is newer.
  sockets[0].dispatch(
    'message',
    message(reading({ generation: 2, sequence: 4, value: 11.0 }))
  );
  sockets[0].dispatch(
    'message',
    message(
      reading({
        bootId: 'boot-old',
        generation: 1,
        sequence: 99,
        deviceTime: '2099-01-01T00:00:00+00:00',
        value: 99.0,
      })
    )
  );

  assert.equal(currentValue(app), 25.0);
});

test('a snapshot does not overwrite a newer message that raced it', async () => {
  const { app, sockets } = loadDashboard({
    snapshots: [[reading({ sequence: 1, value: 21.4 })]],
  });
  sockets[0].dispatch('open');
  await flush();

  // A newer reading arrives, then a reconnect refetches a snapshot that was
  // taken before it.
  sockets[0].dispatch('message', message(reading({ sequence: 9, value: 40.0 })));
  await app.loadSnapshot();

  assert.equal(currentValue(app), 40.0);
});

test('the snapshot drops entries the server no longer reports', async () => {
  const { app, sockets, timers } = loadDashboard({
    snapshots: [
      [reading({ deviceId: 'device-01' }), reading({ deviceId: 'device-02' })],
      [reading({ deviceId: 'device-01' })],
    ],
  });

  sockets[0].dispatch('open');
  await flush();
  assert.equal(app.states.size, 2);

  sockets[0].dispatch('close');
  timers[0].callback();
  sockets[1].dispatch('open');
  await flush();

  assert.equal(app.states.size, 1);
  assert.ok(app.states.has('device-01:temperature'));
});

test('reconnection backs off instead of retrying at a fixed rate', async () => {
  const { sockets, timers } = loadDashboard();

  sockets[0].dispatch('close');
  timers[0].callback();
  sockets[1].dispatch('close');

  assert.equal(timers.length, 2);
  assert.ok(
    timers[1].delay > timers[0].delay,
    `expected backoff, got ${timers[0].delay} then ${timers[1].delay}`
  );
});

test('a successful connection resets the backoff', async () => {
  const { sockets, timers } = loadDashboard();

  sockets[0].dispatch('close');
  const firstDelay = timers[0].delay;
  timers[0].callback();

  sockets[1].dispatch('open');
  await flush();
  sockets[1].dispatch('close');

  assert.equal(timers[1].delay, firstDelay);
});

test('malformed and unrelated messages are ignored', async () => {
  const { app, sockets } = loadDashboard({
    snapshots: [[reading({ value: 21.4 })]],
  });
  sockets[0].dispatch('open');
  await flush();

  sockets[0].dispatch('message', { data: 'not json' });
  sockets[0].dispatch('message', { data: JSON.stringify({ type: 'other' }) });
  sockets[0].dispatch('message', { data: JSON.stringify({ type: 'device.state.changed' }) });

  assert.equal(currentValue(app), 21.4);
});

test('isNewer follows the protocol ordering rules', () => {
  const { app } = loadDashboard();
  const base = reading({ generation: 2, sequence: 5 });

  assert.equal(app.isNewer(base, undefined), true);
  assert.equal(app.isNewer(reading({ generation: 3, sequence: 1 }), base), true);
  assert.equal(app.isNewer(reading({ generation: 2, sequence: 6 }), base), true);
  assert.equal(app.isNewer(reading({ generation: 1, sequence: 99 }), base), false);
  assert.equal(app.isNewer(reading({ generation: 2, sequence: 5 }), base), false);
  assert.equal(app.isNewer(reading({ generation: 2, sequence: 4 }), base), false);
});
