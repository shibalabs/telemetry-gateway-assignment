from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from telemetry_gateway.api import QUEUE_LIMIT_VARIABLE, create_app
from telemetry_gateway.realtime import DEFAULT_CLIENT_QUEUE_LIMIT


def test_health_and_dashboard_are_local(tmp_path) -> None:
    app = create_app(str(tmp_path / "gateway.db"))
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Local Telemetry Dashboard" in dashboard.text


def test_the_websocket_buffer_limit_is_configurable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(QUEUE_LIMIT_VARIABLE, "5")
    app = create_app(str(tmp_path / "gateway.db"))

    assert app.state.hub.queue_limit == 5


@pytest.mark.parametrize("value", ["", "0", "-3", "not-a-number"])
def test_an_unusable_buffer_limit_falls_back_to_the_default(
    tmp_path, monkeypatch, value: str
) -> None:
    """A bad setting must not disable the protection or crash startup."""
    monkeypatch.setenv(QUEUE_LIMIT_VARIABLE, value)
    app = create_app(str(tmp_path / "gateway.db"))

    assert app.state.hub.queue_limit == DEFAULT_CLIENT_QUEUE_LIMIT


def test_an_explicit_buffer_limit_overrides_the_environment(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(QUEUE_LIMIT_VARIABLE, "5")
    app = create_app(str(tmp_path / "gateway.db"), websocket_queue_limit=12)

    assert app.state.hub.queue_limit == 12


def test_rejects_telemetry_for_an_unknown_boot(tmp_path) -> None:
    app = create_app(str(tmp_path / "gateway.db"))
    with TestClient(app) as client:
        response = client.post(
            "/api/telemetry",
            json={
                "deviceId": "device-01",
                "bootId": "unknown-boot",
                "sequence": 1,
                "deviceTime": "2026-08-12T09:00:00Z",
                "metric": "temperature",
                "value": 22,
            },
        )

        assert response.status_code == 409
        assert response.json() == {"error": "unknown_boot"}


def test_registers_boot_ingests_and_lists_state(tmp_path) -> None:
    app = create_app(
        str(tmp_path / "gateway.db"),
        now=lambda: datetime(2026, 8, 12, 9, 0, 1, tzinfo=timezone.utc),
    )
    with TestClient(app) as client:
        boot = client.post(
            "/api/boots", json={"deviceId": "device-01", "bootId": "boot-a"}
        )
        assert boot.status_code == 201
        assert boot.json()["generation"] == 1

        ingestion = client.post(
            "/api/telemetry",
            json={
                "deviceId": "device-01",
                "bootId": "boot-a",
                "sequence": 1,
                "deviceTime": "2026-08-12T09:00:00Z",
                "metric": "temperature",
                "value": 21.4,
            },
        )
        assert ingestion.status_code == 200
        assert ingestion.json()["currentChanged"] is True

        devices = client.get("/api/devices")
        assert devices.status_code == 200
        assert len(devices.json()["devices"]) == 1
        assert devices.json()["devices"][0]["value"] == 21.4
