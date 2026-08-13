"""The service publishes only after a successful commit that changed state.

docs/runtime-contract.md: "Publish a realtime message only after a successful
commit and only when current state changed. A failed transaction must not
produce a successful realtime update."
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from telemetry_gateway.models import (
    BootRegistrationResult,
    DeviceState,
    IngestResult,
    TelemetryInput,
)
from telemetry_gateway.service import TelemetryService

STATE = DeviceState(
    device_id="device-01",
    boot_id="boot-a",
    generation=1,
    sequence=1,
    device_time="2026-08-12T09:00:00+00:00",
    received_at="2026-08-12T09:00:01+00:00",
    metric="temperature",
    value=21.4,
)


class FakeRepository:
    """Records the order of persistence and publication side effects."""

    def __init__(self, result: IngestResult | None = None, error: Exception | None = None):
        self._result = result or IngestResult(False, True, STATE)
        self._error = error
        self.ingest_calls = 0
        self.journal: list[str] = []

    def register_boot(self, _event):
        return BootRegistrationResult("device-01", "boot-a", 1, True)

    def ingest(self, _event, _received_at):
        self.ingest_calls += 1
        self.journal.append("commit")
        if self._error is not None:
            raise self._error
        return self._result

    def list_current_states(self):
        return []

    def list_events(self, _limit):
        return []

    def ping(self):
        return True


class RecordingPublisher:
    def __init__(self, journal: list[str] | None = None) -> None:
        self.states: list[DeviceState] = []
        self._journal = journal if journal is not None else []

    async def publish(self, state: DeviceState) -> None:
        self._journal.append("publish")
        self.states.append(state)


def build(result=None, error=None):
    repository = FakeRepository(result, error)
    publisher = RecordingPublisher(repository.journal)
    service = TelemetryService(
        repository,
        publisher,
        now=lambda: datetime(2026, 8, 12, 9, 0, 1, tzinfo=timezone.utc),
    )
    return repository, publisher, service


def event(**overrides) -> TelemetryInput:
    values = {
        "deviceId": "device-01",
        "bootId": "boot-a",
        "sequence": 1,
        "deviceTime": "2026-08-12T09:00:00Z",
        "metric": "temperature",
        "value": 21.4,
    }
    values.update(overrides)
    return TelemetryInput.model_validate(values)


def test_publishes_the_committed_state_after_the_transaction() -> None:
    repository, publisher, service = build()

    result = asyncio.run(service.ingest(event()))

    assert result.current_changed is True
    assert publisher.states == [STATE]
    assert repository.ingest_calls == 1
    # The commit strictly precedes the announcement.
    assert repository.journal == ["commit", "publish"]


def test_publishes_the_persisted_state_not_the_request() -> None:
    """The message carries what the database recorded, including its generation."""
    committed = DeviceState(
        device_id="device-01",
        boot_id="boot-a",
        generation=4,
        sequence=12,
        device_time="2026-08-12T09:00:00+00:00",
        received_at="2026-08-12T09:00:01+00:00",
        metric="temperature",
        value=30.0,
    )
    _, publisher, service = build(IngestResult(False, True, committed))

    asyncio.run(service.ingest(event(sequence=12)))

    assert publisher.states == [committed]
    assert publisher.states[0].generation == 4


def test_a_duplicate_publishes_nothing() -> None:
    repository, publisher, service = build(IngestResult(True, False, None))

    result = asyncio.run(service.ingest(event()))

    assert result.duplicate is True
    assert result.current_changed is False
    assert publisher.states == []
    assert repository.journal == ["commit"]


def test_an_event_that_did_not_change_state_publishes_nothing() -> None:
    """A delayed or superseded event commits to history but announces nothing."""
    _, publisher, service = build(IngestResult(False, False, None))

    result = asyncio.run(service.ingest(event()))

    assert result.current_changed is False
    assert publisher.states == []


def test_a_failed_transaction_publishes_nothing() -> None:
    _, publisher, service = build(error=RuntimeError("disk failure"))

    with pytest.raises(RuntimeError):
        asyncio.run(service.ingest(event()))

    assert publisher.states == []


def test_boot_registration_is_delegated_to_the_repository() -> None:
    _, publisher, service = build()

    from telemetry_gateway.models import BootRegistrationInput

    result = service.register_boot(
        BootRegistrationInput(deviceId="device-01", bootId="boot-a")
    )

    assert result.generation == 1
    assert publisher.states == []
