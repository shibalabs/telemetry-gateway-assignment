"""Current state must be ordered by (generation, sequence), never by deviceTime.

docs/protocol.md: "deviceTime is diagnostic metadata only. Device clocks can be
early, late, or far in the future. Do not use deviceTime to decide current state."
"""

from __future__ import annotations

import pytest

from telemetry_gateway.database import TelemetryStore
from telemetry_gateway.models import BootRegistrationInput, TelemetryInput

FAR_FUTURE = "2099-01-01T00:00:00+00:00"
FAR_PAST = "1999-01-01T00:00:00+00:00"


@pytest.fixture()
def store():
    store = TelemetryStore(":memory:")
    try:
        yield store
    finally:
        store.close()


def telemetry(**overrides) -> TelemetryInput:
    values = {
        "deviceId": "device-01",
        "bootId": "boot-a",
        "sequence": 1,
        "deviceTime": "2026-08-12T09:00:00+00:00",
        "metric": "temperature",
        "value": 21.4,
    }
    values.update(overrides)
    return TelemetryInput.model_validate(values)


def register(store: TelemetryStore, boot_id: str) -> int:
    return store.register_boot(
        BootRegistrationInput(deviceId="device-01", bootId=boot_id)
    ).generation


def current(store: TelemetryStore):
    states = store.list_current_states()
    assert len(states) == 1
    return states[0]


def test_a_delayed_event_does_not_move_current_state_backward(store) -> None:
    """A late arrival with a lower sequence stays in history but not in state."""
    register(store, "boot-a")
    store.ingest(telemetry(sequence=5, value=25.0), "2026-08-12T09:00:05+00:00")

    delayed = store.ingest(
        telemetry(sequence=2, value=10.0), "2026-08-12T09:00:09+00:00"
    )

    assert delayed.duplicate is False  # It is a new logical event...
    assert delayed.current_changed is False  # ...but it is not the newest.
    assert delayed.state is None
    assert current(store).sequence == 5
    assert current(store).value == 25.0
    # The raw audit history keeps the delayed event.
    assert len(store.list_events(10)) == 2


def test_a_future_device_clock_does_not_pin_current_state(store) -> None:
    """An event stamped in 2099 must not block later events from the same boot."""
    register(store, "boot-a")
    store.ingest(
        telemetry(sequence=1, deviceTime=FAR_FUTURE, value=99.0),
        "2026-08-12T09:00:01+00:00",
    )

    later = store.ingest(
        telemetry(sequence=2, deviceTime="2026-08-12T09:00:02+00:00", value=21.0),
        "2026-08-12T09:00:02+00:00",
    )

    assert later.current_changed is True
    assert current(store).sequence == 2
    assert current(store).value == 21.0


def test_a_backdated_device_clock_still_advances_current_state(store) -> None:
    """A device whose clock jumps backward keeps producing authoritative state."""
    register(store, "boot-a")
    store.ingest(
        telemetry(sequence=1, deviceTime="2026-08-12T09:00:00+00:00", value=21.0),
        "2026-08-12T09:00:01+00:00",
    )

    skewed = store.ingest(
        telemetry(sequence=2, deviceTime=FAR_PAST, value=22.5),
        "2026-08-12T09:00:02+00:00",
    )

    assert skewed.current_changed is True
    assert current(store).sequence == 2
    assert current(store).value == 22.5
    assert current(store).device_time == FAR_PAST  # Kept as diagnostics.


def test_an_older_boot_never_replaces_state_from_a_newer_boot(store) -> None:
    """Generation wins over sequence: boot-a seq 9 loses to boot-b seq 1."""
    register(store, "boot-a")
    assert register(store, "boot-b") == 2
    store.ingest(
        telemetry(bootId="boot-b", sequence=1, value=30.0), "2026-08-12T09:05:00+00:00"
    )

    straggler = store.ingest(
        telemetry(
            bootId="boot-a", sequence=9, deviceTime=FAR_FUTURE, value=11.0
        ),
        "2026-08-12T09:05:01+00:00",
    )

    assert straggler.duplicate is False
    assert straggler.current_changed is False
    assert current(store).boot_id == "boot-b"
    assert current(store).generation == 2
    assert current(store).value == 30.0
    assert len(store.list_events(10)) == 2


def test_a_newer_boot_replaces_state_even_with_a_lower_sequence(store) -> None:
    register(store, "boot-a")
    store.ingest(telemetry(bootId="boot-a", sequence=7, value=21.0), "t1")
    register(store, "boot-b")

    restart = store.ingest(
        telemetry(bootId="boot-b", sequence=1, deviceTime=FAR_PAST, value=30.0), "t2"
    )

    assert restart.current_changed is True
    assert current(store).generation == 2
    assert current(store).sequence == 1
    assert current(store).value == 30.0


def test_bootid_text_ordering_carries_no_meaning(store) -> None:
    """bootId is opaque: a lexically smaller id can still be the newer boot."""
    store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="zzz-first"))
    store.ingest(
        telemetry(bootId="zzz-first", sequence=1, value=21.0),
        "2026-08-12T09:00:01+00:00",
    )
    store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="aaa-second"))

    newer = store.ingest(
        telemetry(bootId="aaa-second", sequence=1, value=30.0),
        "2026-08-12T09:05:01+00:00",
    )

    assert newer.current_changed is True
    assert current(store).boot_id == "aaa-second"
    assert current(store).generation == 2


def test_out_of_order_arrival_converges_on_the_newest_event(store) -> None:
    """Whatever the arrival order, the highest (generation, sequence) wins."""
    register(store, "boot-a")
    arrivals = [4, 1, 7, 3, 6, 2, 5]
    for sequence in arrivals:
        store.ingest(
            telemetry(sequence=sequence, value=float(sequence)),
            f"2026-08-12T09:00:{sequence:02d}+00:00",
        )

    assert current(store).sequence == 7
    assert current(store).value == 7.0
    assert len(store.list_events(20)) == len(arrivals)


def test_metrics_of_one_device_hold_independent_current_state(store) -> None:
    register(store, "boot-a")
    store.ingest(telemetry(sequence=1, metric="temperature", value=21.0), "t1")
    store.ingest(telemetry(sequence=2, metric="humidity", value=55.0), "t2")

    states = {state.metric: state for state in store.list_current_states()}

    assert states["temperature"].value == 21.0
    assert states["humidity"].value == 55.0
