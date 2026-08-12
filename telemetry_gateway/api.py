from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from telemetry_gateway.database import TelemetryRepository, TelemetryStore
from telemetry_gateway.errors import UnknownBootError
from telemetry_gateway.models import BootRegistrationInput, TelemetryInput
from telemetry_gateway.realtime import DEFAULT_CLIENT_QUEUE_LIMIT, RealtimeHub
from telemetry_gateway.service import TelemetryService

STATIC_DIR = Path(__file__).with_name("static")
QUEUE_LIMIT_VARIABLE = "WS_CLIENT_QUEUE_LIMIT"


def _configured_queue_limit() -> int:
    """Per-client websocket buffer limit, overridable for local experiments."""
    raw = os.environ.get(QUEUE_LIMIT_VARIABLE)
    if not raw:
        return DEFAULT_CLIENT_QUEUE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CLIENT_QUEUE_LIMIT
    return value if value >= 1 else DEFAULT_CLIENT_QUEUE_LIMIT


def create_app(
    database_path: str = "data/telemetry.db",
    repository: TelemetryRepository | None = None,
    now: Callable[[], datetime] | None = None,
    websocket_queue_limit: int | None = None,
) -> FastAPI:
    store = repository or TelemetryStore(database_path)
    hub = RealtimeHub(queue_limit=websocket_queue_limit or _configured_queue_limit())
    service = TelemetryService(store, hub, now=now)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await hub.shutdown()
        close = getattr(store, "close", None)
        if callable(close):
            close()

    app = FastAPI(title="Local Telemetry Gateway", lifespan=lifespan)
    app.state.repository = store
    app.state.hub = hub
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.exception_handler(UnknownBootError)
    async def unknown_boot_handler(_request, error: UnknownBootError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": error.code})

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        try:
            ready = store.ping()
        except Exception:
            ready = False
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
        )

    @app.post("/api/boots")
    async def register_boot(event: BootRegistrationInput) -> JSONResponse:
        result = service.register_boot(event)
        return JSONResponse(
            status_code=201 if result.created else 200,
            content=result.to_api(),
        )

    @app.post("/api/telemetry")
    async def ingest(event: TelemetryInput) -> dict:
        return (await service.ingest(event)).to_api()

    @app.get("/api/devices")
    async def list_devices() -> dict:
        return {"devices": [state.to_api() for state in store.list_current_states()]}

    @app.get("/api/events")
    async def list_events(limit: int = Query(default=100, ge=1, le=500)) -> dict:
        return {"events": [event.to_api() for event in store.list_events(limit)]}

    @app.websocket("/ws")
    async def websocket_endpoint(client: WebSocket) -> None:
        await hub.connect(client)
        try:
            # The dashboard sends nothing; this loop only detects the close.
            while True:
                await client.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await hub.disconnect(client)

    return app
