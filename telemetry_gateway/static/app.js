const states = new Map();
const grid = document.querySelector('#grid');
const empty = document.querySelector('#empty');
const status = document.querySelector('#connection-status');
const errorBox = document.querySelector('#error');
const INITIAL_RETRY_MS = 1000;
const MAX_RETRY_MS = 15000;
let stopped = false;
let retryTimer;
let retryDelay = INITIAL_RETRY_MS;
let socket;

function stateKey(state) {
  return `${state.deviceId}:${state.metric}`;
}

// Recency follows docs/protocol.md: the higher server-assigned boot generation
// wins, and sequence breaks ties inside one boot. deviceTime is diagnostic only
// and is never used to order state.
function isNewer(candidate, existing) {
  if (!existing) {
    return true;
  }
  if (candidate.generation !== existing.generation) {
    return candidate.generation > existing.generation;
  }
  return candidate.sequence > existing.sequence;
}

function applyState(state) {
  const key = stateKey(state);
  if (!isNewer(state, states.get(key))) {
    return false;
  }
  states.set(key, state);
  return true;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function render() {
  const ordered = [...states.values()].sort(
    (left, right) =>
      left.deviceId.localeCompare(right.deviceId) ||
      left.metric.localeCompare(right.metric)
  );

  empty.classList.toggle('hidden', ordered.length > 0);
  grid.innerHTML = ordered
    .map(
      (state) => `
        <article>
          <div class="card-title">
            <h2>${escapeHtml(state.deviceId)}</h2>
            <span>${escapeHtml(state.metric)}</span>
          </div>
          <strong>${Number(state.value).toFixed(2)}</strong>
          <dl>
            <div><dt>Generation</dt><dd>${state.generation}</dd></div>
            <div><dt>Sequence</dt><dd>${state.sequence}</dd></div>
            <div><dt>Boot</dt><dd title="${escapeHtml(state.bootId)}">${escapeHtml(state.bootId.slice(0, 8))}</dd></div>
            <div><dt>Received</dt><dd>${new Date(state.receivedAt).toLocaleTimeString()}</dd></div>
          </dl>
        </article>
      `
    )
    .join('');
}

function setError(message) {
  errorBox.textContent = message || '';
  errorBox.classList.toggle('hidden', !message);
}

async function loadSnapshot() {
  const response = await fetch('/api/devices', { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`Snapshot request failed with ${response.status}.`);
  }

  const body = await response.json();

  // The snapshot is authoritative, so entries it no longer lists are dropped.
  // A message that arrived while the request was in flight can still be newer
  // than the row it returned, so those are kept rather than overwritten.
  const merged = new Map();
  for (const state of body.devices) {
    const live = states.get(stateKey(state));
    merged.set(stateKey(state), isNewer(state, live) ? state : live);
  }
  states.clear();
  for (const [key, state] of merged) {
    states.set(key, state);
  }
  render();
}

function connect() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

  socket.addEventListener('open', () => {
    status.textContent = 'Realtime connected';
    status.className = 'status online';
    setError('');
    retryDelay = INITIAL_RETRY_MS;

    // Messages are not replayed, so anything published while the socket was
    // down is missing. The snapshot is refetched after every successful
    // connection, not only the first one, to restore authoritative state.
    loadSnapshot().catch((error) => setError(error.message));
  });

  socket.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message?.type !== 'device.state.changed' || !message.data) {
      return;
    }
    // A message can be older than the snapshot that raced it, so it is applied
    // only when it is genuinely newer.
    if (applyState(message.data)) {
      render();
    }
  });

  socket.addEventListener('error', () => {
    setError('Realtime connection failed.');
  });

  socket.addEventListener('close', () => {
    status.textContent = 'Realtime disconnected';
    status.className = 'status offline';
    if (stopped) {
      return;
    }
    // Back off so a server that drops this client as slow is not immediately
    // hammered by it again.
    window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS);
  });
}

connect();

window.addEventListener('beforeunload', () => {
  stopped = true;
  window.clearTimeout(retryTimer);
  socket?.close();
});
