# MealVue Job Assignment

Repair a local environmental monitoring gateway. Remote sensor devices send temperature readings to a browser dashboard.

## Assignment

### Time

Submit your work within 48 hours after you receive this assignment.

Plan to spend about three hours on the work. Required AI use should make this scope achievable in that time.

A focused partial solution with clear risk analysis is better than a large rewrite that you cannot explain.

### Objective

Repair the system so that it follows the protocol and runtime contracts in `docs/`.

The assignment covers six problem areas:

1. Make duplicate event delivery idempotent.
2. Keep events from separate device boots distinct.
3. Prevent delayed events and incorrect device clocks from moving current state backward.
4. Publish state changes only after a successful database transaction.
5. Prevent a slow WebSocket client from blocking healthy clients or causing unbounded memory use.
6. Restore authoritative current state after a dashboard reconnects.

Add focused tests for each behavior that you change.

### Work and submission

1. Fork this repository.
2. Detach your fork from the fork network before you create pull requests.
3. Use pull requests to implement and review your fixes.
4. Merge your completed pull requests into `main`.
5. Keep normal, clear Git commits.
6. Complete `DECISIONS.md` and `AI_USAGE.md`.
7. Send the repository URL and final commit SHA through Indeed chat.

Do not open a pull request against the starter repository.

### AI use

You must use an AI coding tool during this assignment. You remain responsible for every submitted change.

In `AI_USAGE.md`, record:

- The AI tools that you used
- Important prompts or prompt summaries
- Incorrect or unsuitable output that you rejected or corrected
- The checks that you used to verify AI-generated changes

### Engineering constraints

- Keep all runtime components on one local machine.
- Do not add cloud services or paid dependencies.
- Do not replace the application or its framework.
- Preserve the raw telemetry audit history.
- Do not delete the local database on each start.
- Keep API behavior compatible unless you document a necessary change.

### Deliverables

Submit working code with focused tests.

In `DECISIONS.md`, describe:

- The invariants that you identified
- The incidents that you fixed
- Important design choices and trade-offs
- Schema or API compatibility concerns
- Remaining risks or incomplete work

### Evaluation

The evaluation is behavior-based. You can choose the internal architecture.

We will assess:

- System and data-model reasoning
- Correctness during failures and message reordering
- Tests and debugging method
- Scope control and maintainability
- Risk prioritization
- Your ability to direct and verify AI-generated work

## Notes

Read these files before you edit code:

1. [`docs/protocol.md`](docs/protocol.md)
2. [`docs/runtime-contract.md`](docs/runtime-contract.md)
3. [`docs/api.md`](docs/api.md)

The included tests cover only the basic path. Passing them does not prove that you fixed all six problem areas.

Do not deploy the application. No cloud account, paid API, remote database, or physical device is required.

## Getting started

The repository contains:

- A Python FastAPI HTTP and WebSocket service
- A local SQLite database with versioned migrations
- A browser dashboard served by the application
- A configurable local device simulator
- Unit and API tests

### Requirements

- Python 3.11 or newer

### Install

Linux and macOS:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### Run the application

```bash
python -m telemetry_gateway
```

Open `http://127.0.0.1:3000`.

### Run the simulator

Run this command in another terminal:

```bash
python simulator.py --devices 4
```

Use chaos mode to add duplicate, delayed, restart, and clock-skew events:

```bash
python simulator.py --devices 4 --chaos
```

### Run all local checks

```bash
./scripts/check.sh
```

The equivalent cross-platform commands are:

```bash
python -m compileall -q telemetry_gateway simulator.py tests
python -m pytest
node --test tests/dashboard.test.mjs
```

The dashboard tests run the real `static/app.js` against a stubbed DOM on
Node's built-in test runner. They need no packages, and `scripts/check.sh`
skips them when Node is not on `PATH`.

### Local endpoints

- Application and dashboard: `http://127.0.0.1:3000`
- WebSocket: `ws://127.0.0.1:3000/ws`
- Liveness: `http://127.0.0.1:3000/health/live`
- Readiness: `http://127.0.0.1:3000/health/ready`

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Listen address. |
| `PORT` | `3000` | Listen port. |
| `DATA_FILE` | `data/telemetry.db` | SQLite database path. Migrated in place on start; never recreated. |
| `WS_CLIENT_QUEUE_LIMIT` | `64` | Messages a single dashboard may fall behind before it is dropped. |
