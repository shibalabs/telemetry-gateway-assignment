"""End-to-end checks that the socket only ever announces committed changes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from telemetry_gateway.api import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        str(tmp_path / "gateway.db"),
        now=lambda: datetime(2026, 8, 12, 9, 0, 1, tzinfo=timezone.utc),
    )
    with TestClient(app) as client:
        yield client


def register(client: TestClient, boot_id: str) -> int:
    response = client.post(
        "/api/boots", json={"deviceId": "device-01", "bootId": boot_id}
    )
    assert response.status_code in (200, 201)
    return response.json()["generation"]


def telemetry(client: TestClient, **overrides) -> dict:
    payload = {
        "deviceId": "device-01",
        "bootId": "boot-a",
        "sequence": 1,
        "deviceTime": "2026-08-12T09:00:00Z",
        "metric": "temperature",
        "value": 21.4,
    }
    payload.update(overrides)
    response = client.post("/api/telemetry", json=payload)
    assert response.status_code == 200
    return response.json()


MARKER_SEQUENCE = 900


def assert_nothing_was_announced(client: TestClient, socket) -> None:
    """Assert the socket is silent, by ordering rather than by waiting.

    receive_json blocks, so absence cannot be observed directly. Instead a
    known state change is forced: if the preceding operation had published a
    message, that message would be delivered ahead of this marker.
    """
    body = telemetry(client, sequence=MARKER_SEQUENCE, value=77.0)
    assert body["currentChanged"] is True

    marker = socket.receive_json()
    assert marker["data"]["sequence"] == MARKER_SEQUENCE
    assert marker["data"]["value"] == 77.0


def test_a_state_change_is_announced_once(client) -> None:
    register(client, "boot-a")
    with client.websocket_connect("/ws") as socket:
        body = telemetry(client, sequence=1, value=21.4)

        assert body["accepted"] is True
        assert body["duplicate"] is False
        assert body["currentChanged"] is True
        message = socket.receive_json()
        assert message["type"] == "device.state.changed"
        assert message["data"]["deviceId"] == "device-01"
        assert message["data"]["generation"] == 1
        assert message["data"]["sequence"] == 1
        assert message["data"]["value"] == 21.4
        assert message["data"]["receivedAt"] == "2026-08-12T09:00:01+00:00"


def test_a_duplicate_request_announces_nothing(client) -> None:
    register(client, "boot-a")
    with client.websocket_connect("/ws") as socket:
        telemetry(client, sequence=1)
        assert socket.receive_json()["data"]["sequence"] == 1

        body = telemetry(client, sequence=1)

        assert body == {"accepted": True, "duplicate": True, "currentChanged": False}
        assert_nothing_was_announced(client, socket)


def test_a_delayed_event_announces_nothing(client) -> None:
    register(client, "boot-a")
    with client.websocket_connect("/ws") as socket:
        telemetry(client, sequence=5, value=25.0)
        assert socket.receive_json()["data"]["sequence"] == 5

        body = telemetry(client, sequence=2, value=10.0, deviceTime="2099-01-01T00:00:00Z")

        assert body == {"accepted": True, "duplicate": False, "currentChanged": False}
        assert client.get("/api/devices").json()["devices"][0]["value"] == 25.0
        assert_nothing_was_announced(client, socket)


def test_an_unknown_boot_announces_nothing(client) -> None:
    register(client, "boot-a")
    with client.websocket_connect("/ws") as socket:
        response = client.post(
            "/api/telemetry",
            json={
                "deviceId": "device-01",
                "bootId": "never-registered",
                "sequence": 1,
                "deviceTime": "2026-08-12T09:00:00Z",
                "metric": "temperature",
                "value": 22.0,
            },
        )

        assert response.status_code == 409
        assert response.json() == {"error": "unknown_boot"}
        assert_nothing_was_announced(client, socket)


def test_a_rejected_event_leaves_no_trace_in_history(client) -> None:
    register(client, "boot-a")
    client.post(
        "/api/telemetry",
        json={
            "deviceId": "device-01",
            "bootId": "never-registered",
            "sequence": 1,
            "deviceTime": "2026-08-12T09:00:00Z",
            "metric": "temperature",
            "value": 22.0,
        },
    )

    assert client.get("/api/events").json()["events"] == []
    assert client.get("/api/devices").json()["devices"] == []


def test_the_snapshot_holds_state_published_while_no_client_was_connected(
    client,
) -> None:
    """Messages are not replayed, so /api/devices must carry the whole truth."""
    register(client, "boot-a")
    with client.websocket_connect("/ws"):
        telemetry(client, sequence=1, value=21.4)

    # Nothing is listening: these changes are announced to no one.
    telemetry(client, sequence=2, value=22.9)
    register(client, "boot-b")
    telemetry(client, bootId="boot-b", sequence=1, value=30.5)

    with client.websocket_connect("/ws"):
        snapshot = client.get("/api/devices").json()["devices"]

    assert len(snapshot) == 1
    assert snapshot[0]["value"] == 30.5
    assert snapshot[0]["bootId"] == "boot-b"
    assert snapshot[0]["generation"] == 2
    assert snapshot[0]["sequence"] == 1


def test_the_snapshot_survives_a_restart_of_the_process(tmp_path) -> None:
    """The local database is not reset on start, so state outlives the process."""
    database = str(tmp_path / "gateway.db")
    clock = lambda: datetime(2026, 8, 12, 9, 0, 1, tzinfo=timezone.utc)  # noqa: E731

    with TestClient(create_app(database, now=clock)) as first:
        register(first, "boot-a")
        telemetry(first, sequence=4, value=23.5)

    with TestClient(create_app(database, now=clock)) as second:
        snapshot = second.get("/api/devices").json()["devices"]
        history = second.get("/api/events").json()["events"]

    assert len(snapshot) == 1
    assert snapshot[0]["value"] == 23.5
    assert snapshot[0]["sequence"] == 4
    assert len(history) == 1


def test_a_device_restart_is_announced_to_a_connected_dashboard(client) -> None:
    register(client, "boot-a")
    with client.websocket_connect("/ws") as socket:
        telemetry(client, bootId="boot-a", sequence=9, value=21.0)
        assert socket.receive_json()["data"]["generation"] == 1

        assert register(client, "boot-b") == 2
        body = telemetry(client, bootId="boot-b", sequence=1, value=30.0)

        assert body["currentChanged"] is True
        message = socket.receive_json()
        assert message["data"]["bootId"] == "boot-b"
        assert message["data"]["generation"] == 2
        assert message["data"]["sequence"] == 1
        assert message["data"]["value"] == 30.0
