"""Tests for root-page authentication and MQTT reconnect bookkeeping."""

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from inverter_dashboard import config as cfg
from inverter_dashboard import server


@pytest.fixture
def client(monkeypatch):
    """TestClient with DASHBOARD_SECRET set (lifespan not started: no MQTT tasks)."""
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
    return TestClient(server.app)


def test_index_requires_secret(client):
    resp = client.get("/")
    assert resp.status_code == 401


def test_index_rejects_wrong_credentials(client):
    assert client.get("/", params={"token": "nope"}).status_code == 403
    assert client.get("/", headers={"Authorization": "Bearer nope"}).status_code == 403


def test_index_accepts_valid_credentials(client):
    # Repo ships flat static/index.html (docker-publish layout); authorized
    # requests must pass auth and serve the SPA (or a non-auth error).
    for resp in (
        client.get("/", params={"token": "s3cret"}),
        client.get("/", headers={"Authorization": "Bearer s3cret"}),
    ):
        assert resp.status_code not in (401, 403)
        if resp.status_code == 200:
            assert 'id="app"' in resp.text


def test_index_open_when_no_secret_configured(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    resp = TestClient(server.app).get("/")
    assert resp.status_code not in (401, 403)


def test_embedded_spa_asset_references_are_served(client):
    response = client.get("/", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
    assert assets
    assert any(path.endswith(".js") for path in assets)
    assert any(path.endswith(".css") for path in assets)
    for path in assets:
        asset = client.get(path)
        assert asset.status_code == 200, path
        assert asset.content, path
        expected_type = "javascript" if path.endswith(".js") else "text/css"
        assert expected_type in asset.headers["content-type"], path


def test_embedded_spa_matches_recorded_source_build():
    """Partial or stale asset copies must not pass the package contract."""
    root = server._resolve_spa_root()
    assert root is not None
    metadata = json.loads((root / "source-info.json").read_text())
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["source_commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", metadata["package_lock_sha256"])
    expected = {item["path"] for item in metadata["files"]}
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert actual == expected | {"source-info.json"}
    for item in metadata["files"]:
        content = (root / item["path"]).read_bytes()
        assert len(content) == item["size"], item["path"]
        assert hashlib.sha256(content).hexdigest() == item["sha256"], item["path"]


def test_api_state_reports_mqtt_health(client):
    data = client.get("/api/state").json()
    assert data["mqtt_connected"] is False
    assert data["mqtt_reconnects"] == 0


def test_next_backoff_doubles_and_caps(monkeypatch):
    monkeypatch.setattr(cfg, "MQTT_RECONNECT_MAX", 10.0)
    assert server._next_backoff(1.0) == 2.0
    assert server._next_backoff(5.0) == 10.0
    assert server._next_backoff(50.0) == 10.0


def test_solar_forecast_passthrough():
    from inverter_dashboard import websocket_handler as wsh

    wsh._state["mqtt_state"] = server.MqttState()
    wsh._state["mqtt_state"].current_state = {
        "solar_forecast": {"date": "2026-08-23", "today_kwh": 12.5, "tomorrow_kwh": 9.1}
    }
    payload = wsh.build_payload()
    assert payload["solar_forecast"]["today_kwh"] == 12.5
    wsh._state["mqtt_state"] = None


async def test_shutdown_mqtt_client_clears_state():
    server._app_state.mqtt_connected = True
    server._app_state.mqtt_client = object()
    await server._shutdown_mqtt_client()
    assert server._app_state.mqtt_client is None
    assert server._app_state.mqtt_connected is False


async def test_mqtt_loop_reconnects_after_broker_error(monkeypatch):
    """Broker death mid-session must not kill the loop: next attempt connects."""
    import asyncio

    from aiomqtt import MqttError

    attempts = {"n": 0}

    class FakeClient:
        """Minimal aiomqtt.Client stand-in: first session dies, second idles."""

        def __init__(self):
            attempts["n"] += 1
            self._n = attempts["n"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def subscribe(self, *args, **kwargs):
            return None

        @property
        def messages(self):
            async def gen():
                if self._n == 1:
                    raise MqttError("broker died")
                await asyncio.sleep(30)
                yield b""

            return gen()

    monkeypatch.setattr(cfg, "MQTT_RECONNECT_MIN", 0.01)
    monkeypatch.setattr(cfg, "MQTT_RECONNECT_MAX", 0.02)
    monkeypatch.setattr(server, "_make_mqtt_client", FakeClient)

    old_tasks = list(server._app_state.mqtt_tasks)
    server._app_state.mqtt_tasks.clear()
    task = None
    try:
        server._start_mqtt_client()
        task = server._app_state.mqtt_tasks[0]
        for _ in range(200):
            if server._app_state.mqtt_reconnects >= 1 and server._app_state.mqtt_connected:
                break
            await asyncio.sleep(0.02)
        assert server._app_state.mqtt_reconnects == 1
        assert server._app_state.mqtt_connected is True
        assert attempts["n"] == 2  # fresh client object per attempt
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server._app_state.mqtt_tasks.clear()
        server._app_state.mqtt_tasks.extend(old_tasks)
        server._app_state.mqtt_connected = False
        server._app_state.mqtt_client = None


@pytest.mark.parametrize(
    "credentials",
    [{}, {"params": {"token": "wrong"}}, {"headers": {"Authorization": "Bearer wrong"}}],
)
def test_api_state_unauthenticated_omits_live_tiles(monkeypatch, credentials):
    """INVDASH-1: with secret set, unauthenticated /api/state is health-only."""
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")

    class _Mqtt:
        def get_state(self):
            return {"version": "9.9.9", "setpoint": 1234, "ess_mode": "Optimized"}

    monkeypatch.setattr(server._app_state, "mqtt_state", _Mqtt())

    def _fake_payload():
        pytest.fail("Unauthenticated health probes must not build the live payload")

    monkeypatch.setattr(server.websocket_handler, "build_payload", _fake_payload)
    client = TestClient(server.app)
    response = client.get("/api/state", **credentials)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["ok"] is True
    assert data["mqtt_connected"] is False
    assert "setpoint" not in data
    assert "ess_mode" not in data
    assert "notifications" not in data


@pytest.mark.parametrize(
    ("secret", "credentials"),
    [
        ("s3cret", {"headers": {"Authorization": "Bearer s3cret"}}),
        ("a&b+c", {"params": {"token": "a&b+c"}}),
        ("", {}),
    ],
)
def test_api_state_authenticated_includes_live_tiles(monkeypatch, secret, credentials):
    """Authorized /api/state still merges the live WS payload."""
    monkeypatch.setattr(server, "DASHBOARD_SECRET", secret)

    class _Mqtt:
        def get_state(self):
            return {"version": "9.9.9"}

    monkeypatch.setattr(server._app_state, "mqtt_state", _Mqtt())
    monkeypatch.setattr(
        server.websocket_handler,
        "build_payload",
        lambda: {"setpoint": 42, "ess_mode": "KeepBatteriesCharged", "grid_l2_available": False},
    )
    client = TestClient(server.app)
    response = client.get("/api/state", **credentials)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["ok"] is True
    assert data["setpoint"] == 42
    assert data["ess_mode"] == "KeepBatteriesCharged"
    assert data["grid_l2_available"] is False
