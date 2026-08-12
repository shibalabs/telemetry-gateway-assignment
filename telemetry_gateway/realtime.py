from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from telemetry_gateway.models import DeviceState

logger = logging.getLogger(__name__)

# How many pending messages a single dashboard may fall behind before it is
# dropped. The queue is the only per-client buffer, so this bounds the memory a
# broken or throttled client can cost the gateway.
DEFAULT_CLIENT_QUEUE_LIMIT = 64

# Websocket close code 1013 "try again later": the client is invited to
# reconnect, and on reconnection the dashboard refetches the snapshot.
SLOW_CLIENT_CLOSE_CODE = 1013

# A dropped client's socket may itself be unresponsive, so closing it is not
# allowed to hang forever.
CLOSE_TIMEOUT_SECONDS = 5.0


class StatePublisher(Protocol):
    async def publish(self, state: DeviceState) -> None: ...


class WebSocketLike(Protocol):
    async def accept(self) -> None: ...

    async def send_json(self, message: Any) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


class _ClientChannel:
    """One dashboard connection, fed through a bounded queue by its own task.

    Sending happens in a dedicated task rather than in the publisher's call
    stack. A client that cannot keep up fills its queue and is dropped; it can
    never stall the ingest path or another client.
    """

    def __init__(self, websocket: WebSocketLike, queue_limit: int) -> None:
        self._websocket = websocket
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_limit)
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def websocket(self) -> WebSocketLike:
        return self._websocket

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        self._task = asyncio.create_task(self._deliver())

    def offer(self, message: dict[str, Any]) -> bool:
        """Queue a message without blocking. False means the client is too slow."""
        if self._closed:
            return False
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

    async def _deliver(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._websocket.send_json(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The peer is gone or the transport failed. The endpoint's
                # receive loop performs the registry cleanup.
                return

    async def close(self, code: int = 1000) -> None:
        """Stop delivery and release the socket. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True

        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Drop anything still queued so the buffer cannot outlive the client.
        while not self._queue.empty():
            self._queue.get_nowait()

        try:
            await asyncio.wait_for(
                self._websocket.close(code=code), timeout=CLOSE_TIMEOUT_SECONDS
            )
        except Exception:
            pass


class RealtimeHub:
    """Fan-out of current-state changes to connected dashboards.

    docs/runtime-contract.md requires the server to isolate clients from each
    other, bound the memory of a client that cannot receive fast enough, drop it
    once a configured buffer limit is exceeded, and keep serving healthy clients
    throughout.
    """

    def __init__(self, queue_limit: int = DEFAULT_CLIENT_QUEUE_LIMIT) -> None:
        if queue_limit < 1:
            raise ValueError("queue_limit must be at least 1")
        self._queue_limit = queue_limit
        self._channels: dict[Any, _ClientChannel] = {}
        self._closing: set[asyncio.Task[None]] = set()
        self.dropped_clients = 0

    async def connect(self, client: WebSocketLike) -> None:
        await client.accept()
        channel = _ClientChannel(client, self._queue_limit)
        self._channels[client] = channel
        channel.start()

    async def disconnect(self, client: WebSocketLike) -> None:
        channel = self._channels.pop(client, None)
        if channel is not None:
            await channel.close()

    async def publish(self, state: DeviceState) -> None:
        """Hand the message to every client's queue. Never waits on a socket."""
        message = {"type": "device.state.changed", "data": state.to_api()}
        for client, channel in tuple(self._channels.items()):
            if channel.offer(message):
                continue

            # This client is more than queue_limit messages behind. Drop it
            # instead of buffering without bound; it will reconnect and refetch
            # the authoritative snapshot.
            self._channels.pop(client, None)
            self.dropped_clients += 1
            logger.warning(
                "Dropping a websocket client that fell more than %d messages behind.",
                self._queue_limit,
            )
            self._close_in_background(channel)

    def _close_in_background(self, channel: _ClientChannel) -> None:
        """Close without blocking the publisher on an unresponsive socket."""
        task = asyncio.create_task(channel.close(code=SLOW_CLIENT_CLOSE_CODE))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def shutdown(self) -> None:
        channels = tuple(self._channels.values())
        self._channels.clear()
        for channel in channels:
            await channel.close()

    @property
    def size(self) -> int:
        return len(self._channels)

    @property
    def queue_limit(self) -> int:
        return self._queue_limit

    def pending_for(self, client: WebSocketLike) -> int:
        """Queued-but-unsent messages for one client. Used by tests and health."""
        channel = self._channels.get(client)
        return channel.pending if channel is not None else 0
