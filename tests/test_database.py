from telemetry_gateway.database import TelemetryStore
from telemetry_gateway.models import BootRegistrationInput, TelemetryInput


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


def test_registers_a_boot_idempotently() -> None:
    store = TelemetryStore(":memory:")
    try:
        event = BootRegistrationInput(deviceId="device-01", bootId="boot-a")

        first = store.register_boot(event)
        second = store.register_boot(event)

        assert first.to_api() == {
            "deviceId": "device-01",
            "bootId": "boot-a",
            "generation": 1,
            "created": True,
        }
        assert second.to_api() == {
            "deviceId": "device-01",
            "bootId": "boot-a",
            "generation": 1,
            "created": False,
        }
    finally:
        store.close()


def test_stores_a_basic_event_and_calculates_current_state() -> None:
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))

        result = store.ingest(telemetry(), "2026-08-12T09:00:01+00:00")

        assert result.duplicate is False
        assert result.current_changed is True
        assert store.list_current_states()[0].to_api() == {
            "deviceId": "device-01",
            "bootId": "boot-a",
            "generation": 1,
            "sequence": 1,
            "deviceTime": "2026-08-12T09:00:00+00:00",
            "receivedAt": "2026-08-12T09:00:01+00:00",
            "metric": "temperature",
            "value": 21.4,
        }
        assert len(store.list_events(10)) == 1
    finally:
        store.close()


def test_repeated_event_from_same_boot_is_a_duplicate() -> None:
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        store.ingest(telemetry(), "2026-08-12T09:00:01+00:00")

        duplicate = store.ingest(telemetry(), "2026-08-12T09:00:02+00:00")

        assert duplicate.to_api() == {
            "accepted": True,
            "duplicate": True,
            "currentChanged": False,
        }
        assert len(store.list_events(10)) == 1
    finally:
        store.close()


def test_redelivering_an_event_many_times_stays_idempotent() -> None:
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        first_event = telemetry(sequence=1, value=21.4)
        second_event = telemetry(
            sequence=2, value=25.0, deviceTime="2026-08-12T09:00:02+00:00"
        )
        store.ingest(first_event, "2026-08-12T09:00:01+00:00")
        store.ingest(second_event, "2026-08-12T09:00:02+00:00")

        # The transport is at-least-once: replay both events repeatedly.
        for attempt in range(5):
            first = store.ingest(first_event, f"2026-08-12T09:01:0{attempt}+00:00")
            second = store.ingest(second_event, f"2026-08-12T09:02:0{attempt}+00:00")
            assert first.duplicate is True
            assert first.current_changed is False
            assert first.state is None
            assert second.duplicate is True
            assert second.current_changed is False

        # One raw row per logical event, and current state never moved.
        assert len(store.list_events(50)) == 2
        current = store.list_current_states()
        assert len(current) == 1
        assert current[0].sequence == 2
        assert current[0].value == 25.0
        assert current[0].received_at == "2026-08-12T09:00:02+00:00"
    finally:
        store.close()


def test_a_new_boot_may_restart_the_sequence_without_being_a_duplicate() -> None:
    """sequence restarts at 1 per boot, so (boot, sequence) pairs must stay distinct."""
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        store.ingest(
            telemetry(bootId="boot-a", sequence=1, value=21.4),
            "2026-08-12T09:00:01+00:00",
        )

        second_boot = store.register_boot(
            BootRegistrationInput(deviceId="device-01", bootId="boot-b")
        )
        after_restart = store.ingest(
            telemetry(
                bootId="boot-b",
                sequence=1,
                value=30.5,
                deviceTime="2026-08-12T09:05:00+00:00",
            ),
            "2026-08-12T09:05:01+00:00",
        )

        assert second_boot.generation == 2
        assert after_restart.duplicate is False
        assert after_restart.current_changed is True
        assert len(store.list_events(10)) == 2

        current = store.list_current_states()[0]
        assert current.boot_id == "boot-b"
        assert current.generation == 2
        assert current.sequence == 1
        assert current.value == 30.5
    finally:
        store.close()


def test_boots_of_different_devices_do_not_share_a_generation_counter() -> None:
    store = TelemetryStore(":memory:")
    try:
        first = store.register_boot(
            BootRegistrationInput(deviceId="device-01", bootId="boot-a")
        )
        second = store.register_boot(
            BootRegistrationInput(deviceId="device-02", bootId="boot-a")
        )
        third = store.register_boot(
            BootRegistrationInput(deviceId="device-01", bootId="boot-b")
        )

        assert first.generation == 1
        assert second.generation == 1
        assert third.generation == 2
    finally:
        store.close()


def test_reregistering_a_boot_keeps_the_original_generation() -> None:
    """A device retrying registration must not be handed a new generation."""
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-b"))

        repeat = store.register_boot(
            BootRegistrationInput(deviceId="device-01", bootId="boot-a")
        )
        newest = store.register_boot(
            BootRegistrationInput(deviceId="device-01", bootId="boot-c")
        )

        assert repeat.created is False
        assert repeat.generation == 1
        # A genuinely new boot still outranks every earlier boot.
        assert newest.created is True
        assert newest.generation == 3
    finally:
        store.close()
