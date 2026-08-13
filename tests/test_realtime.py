"""A slow websocket client must not block healthy ones or grow without bound.

docs/runtime-contract.md requires the server to isolate clients from each other,
keep memory bounded for a client that cannot receive data fast enough, drop that
client once the configured buffer limit is exceeded, and keep serving the rest.
"""

from __future__ import annotations

import asyncio

import pytest

from telemetry_gateway.models import DeviceState
from telemetry_gateway.realtime import RealtimeHub


def state(sequence: int) -> DeviceState:
    return DeviceState(
        device_id="device-01",
        boot_id="boot-a",
        generation=1,
        sequence=sequence,
        device_time="2026-08-12T09:00:00+00:00",
        received_at="2026-08-12T09:00:01+00:00",
        metric="temperature",
        value=float(sequence),
    )


class FakeClient:
    """A dashboard connection whose send speed the test controls."""

    def __init__(self, blocked: bool = False) -> None:
        self.received: list[dict] = []
        self.accepted = False
        self.close_code: int | None = None
        self._gate = asyncio.Event()
        if not blocked:
            self._gate.set()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, message: dict) -> None:
        await self._gate.wait()
        self.received.append(message)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    def release(self) -> None:
        self._gate.set()


async def settle(times: int = 6) -> None:
    """Let the per-client delivery tasks run."""
    for _ in range(times):
        await asyncio.sleep(0)


@pytest.mark.parametrize("limit", [1, 4, 64])
def test_publish_never_waits_on_a_stalled_client(limit: int) -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=limit)
        stalled = FakeClient(blocked=True)
        await hub.connect(stalled)

        # Far more messages than the buffer can hold. If publish awaited the
        # socket, this would deadlock instead of returning.
        for sequence in range(limit * 5):
            await asyncio.wait_for(hub.publish(state(sequence)), timeout=2)

        assert stalled.received == []

    asyncio.run(scenario())


def test_a_stalled_client_does_not_delay_a_healthy_one() -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=8)
        stalled = FakeClient(blocked=True)
        healthy = FakeClient()
        await hub.connect(stalled)
        await hub.connect(healthy)

        for sequence in range(3):
            await hub.publish(state(sequence))
        await settle()

        assert [m["data"]["sequence"] for m in healthy.received] == [0, 1, 2]
        assert stalled.received == []

    asyncio.run(scenario())


def test_a_client_that_exceeds_the_buffer_limit_is_dropped() -> None:
    async def scenario() -> None:
        limit = 4
        hub = RealtimeHub(queue_limit=limit)
        stalled = FakeClient(blocked=True)
        await hub.connect(stalled)
        assert hub.size == 1

        for sequence in range(limit * 4):
            await hub.publish(state(sequence))
            await settle(2)

        assert hub.size == 0
        assert hub.dropped_clients == 1

        # The socket is closed with "try again later" so the dashboard
        # reconnects and refetches the authoritative snapshot.
        stalled.release()
        await settle()
        assert stalled.close_code == 1013

    asyncio.run(scenario())


def test_memory_stays_bounded_while_a_client_is_stalled() -> None:
    """The queue is the only per-client buffer, and it never exceeds its limit."""

    async def scenario() -> None:
        limit = 4
        hub = RealtimeHub(queue_limit=limit)
        stalled = FakeClient(blocked=True)
        await hub.connect(stalled)

        high_water = 0
        for sequence in range(200):
            await hub.publish(state(sequence))
            high_water = max(high_water, hub.pending_for(stalled))

        assert high_water <= limit
        assert hub.size == 0  # Dropped rather than buffered.

    asyncio.run(scenario())


def test_healthy_clients_keep_receiving_after_a_slow_client_is_dropped() -> None:
    async def scenario() -> None:
        limit = 2
        hub = RealtimeHub(queue_limit=limit)
        stalled = FakeClient(blocked=True)
        healthy = FakeClient()
        await hub.connect(stalled)
        await hub.connect(healthy)

        for sequence in range(20):
            await hub.publish(state(sequence))
            await settle(2)

        assert hub.size == 1  # Only the healthy client remains.
        assert hub.dropped_clients == 1
        assert [m["data"]["sequence"] for m in healthy.received] == list(range(20))

        # And it still receives everything published afterwards.
        await hub.publish(state(99))
        await settle()
        assert healthy.received[-1]["data"]["sequence"] == 99

    asyncio.run(scenario())


def test_a_failing_socket_does_not_break_other_clients() -> None:
    class BrokenClient(FakeClient):
        async def send_json(self, message: dict) -> None:
            raise ConnectionResetError("peer vanished")

    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=8)
        broken = BrokenClient()
        healthy = FakeClient()
        await hub.connect(broken)
        await hub.connect(healthy)

        for sequence in range(5):
            await hub.publish(state(sequence))
        await settle()

        assert [m["data"]["sequence"] for m in healthy.received] == [0, 1, 2, 3, 4]

    asyncio.run(scenario())


def test_a_disconnected_client_stops_receiving() -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=8)
        client = FakeClient()
        await hub.connect(client)
        await hub.publish(state(1))
        await settle()

        await hub.disconnect(client)
        await hub.publish(state(2))
        await settle()

        assert hub.size == 0
        assert [m["data"]["sequence"] for m in client.received] == [1]

    asyncio.run(scenario())


def test_disconnect_is_safe_to_repeat() -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=8)
        client = FakeClient()
        await hub.connect(client)

        await hub.disconnect(client)
        await hub.disconnect(client)

        assert hub.size == 0

    asyncio.run(scenario())


def test_shutdown_releases_every_client() -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=8)
        clients = [FakeClient() for _ in range(3)]
        for client in clients:
            await hub.connect(client)

        await hub.shutdown()

        assert hub.size == 0
        assert all(client.close_code is not None for client in clients)

    asyncio.run(scenario())


def test_the_hub_rejects_a_meaningless_buffer_limit() -> None:
    with pytest.raises(ValueError):
        RealtimeHub(queue_limit=0)


def test_the_publisher_accepts_a_client_before_delivering() -> None:
    async def scenario() -> None:
        hub = RealtimeHub()
        client = FakeClient()

        await hub.connect(client)

        assert client.accepted is True
        assert hub.size == 1
        assert hub.queue_limit >= 1

    asyncio.run(scenario())
