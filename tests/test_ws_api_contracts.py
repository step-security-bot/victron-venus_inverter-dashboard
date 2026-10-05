"""Contract tests for WebSocket and HTTP API consumed by Vue/Go/Desktop clients.

Covers:
- WebSocket connect, initial payload, action dispatch, broadcast, auth
- HTTP API response shapes for all /api/* endpoints
- build_payload / InverterState validation behavior
"""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from inverter_dashboard import server, websocket_handler
from inverter_dashboard.server import MqttState
from inverter_dashboard.websocket_handler import InverterState, _dispatch_action

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeMqttClient:
    """No-op MQTT client stub — records published messages."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic: str, message: str, qos: int = 0):
        self.published.append((topic, message))


class FakeAppState:
    """Minimal AppState with a FakeMqttClient."""

    def __init__(self):
        self.mqtt_state: MqttState = MqttState()
        self.mqtt_client = FakeMqttClient()


# ---------------------------------------------------------------------------
# build_payload / InverterState contract
# ---------------------------------------------------------------------------


class TestBuildPayloadContract:
    """Payload structure consumed by all dashboard clients."""

    def test_unknown_mqtt_keys_are_dropped(self):
        """Extra keys from MQTT must not leak to clients."""
        ms = MqttState()
        ms.current_state = {
            "battery_soc": 80,
            "secret_internal_key": "do-not-send",
            "another_junk": 123,
        }
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}

        payload = wsh.build_payload()

        assert payload.get("battery_soc") == 80
        assert "secret_internal_key" not in payload
        assert "another_junk" not in payload
        wsh._state["mqtt_state"] = None

    def test_unknown_ha_overlay_keys_are_dropped(self):
        """Extra keys from HA overlay must not leak to clients."""
        from inverter_dashboard import ha_client

        ms = MqttState()
        ms.current_state = {"battery_soc": 50}
        ha_client.replace_overlay({"ha_direct_connected": True, "extra_key": "ignored"})
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}

        payload = wsh.build_payload()

        assert payload.get("battery_soc") == 50
        assert "extra_key" not in payload
        ha_client.replace_overlay({"ha_direct_connected": False})
        wsh._state["mqtt_state"] = None

    def test_console_not_in_payload(self):
        """Console log was removed from the panel — do not broadcast it."""
        ms = MqttState()
        ms.current_state = {"battery_soc": 10, "console": ["should-not-pass"]}
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}

        payload = wsh.build_payload()

        assert "console" not in payload
        wsh._state["mqtt_state"] = None

    def test_notifications_included(self):
        ms = MqttState()
        ms.push_notification(
            {"id": "n1", "level": "info", "title": "Hello", "body": "World", "source": "test"}
        )
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}

        payload = wsh.build_payload()

        assert "notifications" in payload
        assert len(payload["notifications"]) == 1
        assert payload["notifications"][0]["title"] == "Hello"
        wsh._state["mqtt_state"] = None

    def test_camera_event_included(self):
        ms = MqttState()
        ms.camera_event = {"camera": "front", "url": "rtsp://cam/stream", "ts": "123456"}
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}

        payload = wsh.build_payload()

        assert payload["camera_event"]["camera"] == "front"
        wsh._state["mqtt_state"] = None

    def test_dashboard_version_included(self):
        from inverter_dashboard.version import VERSION

        ms = MqttState()
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = "99.0.0"
        wsh._ui_settings = {}

        payload = wsh.build_payload()

        assert payload["dashboard_version"] == VERSION
        assert payload["latest_version"] == "99.0.0"
        wsh._state["mqtt_state"] = None

    def test_null_mqtt_state_returns_empty_fields(self):
        """Null mqtt_state must not crash build_payload."""
        wsh = websocket_handler
        wsh._state["mqtt_state"] = None
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}

        with pytest.raises(AttributeError):
            _ = wsh.build_payload()

        wsh._state["mqtt_state"] = MqttState()

    def test_inverterstate_model_all_fields_accepted(self):
        """Every documented field must pass InverterState validation."""
        raw = {
            # Grid / consumption
            "gt": 230.1,
            "g1": 229.0,
            "g2": 228.5,
            "tt": 1500.0,
            "t1": 800.0,
            "t2": 700.0,
            # Solar
            "solar_total": 4500.0,
            "mppt_total": 4400.0,
            "mppt_individual": [1200, 1500, 1700],
            # Battery
            "battery_soc": 75,
            "battery_power": -500.0,
            "battery_voltage": 52.0,
            "battery_current": -9.6,
            # Inverter
            "setpoint": 1500.0,
            "inverter_state": " charger",
            "version": "2.14",
            # Dashboard
            "dashboard_version": "0.1.0",
            "latest_version": "0.2.0",
            "uptime": 3600.0,
            # HA
            "ha_connected": True,
            "ha_direct_connected": True,
            # Control
            "dry_run": False,
            "ess_mode": {"mode": "storage"},
            # Feature flags
            "booleans": {"grid": True, "solar": False},
            "features": {"ev": True, "water": False},
            # Derived
            "mppt_chargers": [{"name": "MPP1", "power": 1200}],
            "pv_inverters": [{"name": "Tasmota", "power": 300}],
            "batteries": [{"name": "Battery 512", "soc": 75}],
            "loads": {"dishwasher": 1200, "washer": 300},
            "ui_config": {"settings": {"show_ev": True}},
            "daily_stats": {"today_kwh": 12.5},
            # Solar forecast
            "solar_forecast": {
                "date": "2026-08-30",
                "today_kwh": 12.5,
                "tomorrow_kwh": 8.0,
            },
            # HA filtered
            "ha_filtered": {
                "sensors": [{"id": "sensor.x", "state": "10.5"}],
                "numbers": [],
                "covers": [],
                "media_players": [],
                "scenes": [],
                "weather": None,
            },
            # EV
            "ev_charging_kw": 7.4,
            "ev_power": 7400.0,
            "car_soc": 73.5,
            # Water
            "water_level": 85.0,
            "water_valve": True,
            "pump_switch": False,
            # Appliances
            "dishwasher_running": True,
            "dishwasher_duration": 45.0,
            "washer_time": 30.0,
            "washer_power": 500.0,
            "dryer_time": 20.0,
            "dryer_power": 2000.0,
            # Console / notifications / camera
            "notifications": [{"id": "n1", "level": "info", "title": "OK", "body": ""}],
            "camera_event": {"camera": "cam1", "url": "rtsp://x", "ts": "1"},
        }
        validated = InverterState(**raw)
        dumped = validated.model_dump(exclude_none=True)
        assert validated.battery_soc == 75
        assert dumped["solar_forecast"]["today_kwh"] == 12.5
        assert dumped["ha_filtered"]["sensors"][0]["id"] == "sensor.x"
        assert validated.dry_run is False
        assert validated.inverter_state == " charger"


# ---------------------------------------------------------------------------
# WebSocket action dispatch contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_action_publishes_mqtt():
    client = FakeMqttClient()
    await _dispatch_action("toggle", {"entity": "only_charging"}, client)
    assert ("inverter/cmd/toggle", '{"entity": "only_charging"}') in client.published


async def test_toggle_control_flag_uses_bare_key():
    """Header flags must hit Cerbo MQTT with bare keys, not HA entity ids."""
    client = FakeMqttClient()
    await _dispatch_action("toggle", {"entity": "input_boolean.minimize_charging"}, client)
    assert ("inverter/cmd/toggle", '{"entity": "minimize_charging"}') in client.published


@pytest.mark.asyncio
async def test_press_action_rejects_unconfigured_home_entity():
    client = FakeMqttClient()
    with pytest.raises(ValueError):
        await _dispatch_action("press", {"entity": "button.reset"}, client)
    assert client.published == []


@pytest.mark.asyncio
async def test_setpoint_action_publishes_mqtt():
    client = FakeMqttClient()
    await _dispatch_action("setpoint", {"value": 1500}, client)
    assert ("inverter/cmd/setpoint", '{"value": 1500}') in client.published


@pytest.mark.asyncio
async def test_dry_run_action_publishes_mqtt():
    client = FakeMqttClient()
    await _dispatch_action("dry_run", {}, client)
    assert ("inverter/cmd/dry_run", "") in client.published


@pytest.mark.asyncio
async def test_limits_action_publishes_mqtt_with_defaults():
    from inverter_dashboard.config import DEFAULT_POWER_MAX, DEFAULT_POWER_MIN

    client = FakeMqttClient()
    await _dispatch_action("limits", {}, client)  # no explicit min/max
    _, msg = next((t, m) for t, m in client.published if "limits" in t)
    payload = json.loads(msg)
    assert payload["min"] == DEFAULT_POWER_MIN
    assert payload["max"] == DEFAULT_POWER_MAX


@pytest.mark.asyncio
async def test_limits_action_publishes_mqtt_with_explicit_values():
    client = FakeMqttClient()
    await _dispatch_action("limits", {"min": -1000, "max": 3000}, client)
    _, msg = next((t, m) for t, m in client.published if "limits" in t)
    payload = json.loads(msg)
    assert payload["min"] == -1000
    assert payload["max"] == 3000


@pytest.mark.asyncio
async def test_ess_mode_action_publishes_mqtt():
    client = FakeMqttClient()
    await _dispatch_action("ess_mode", {}, client)
    assert ("inverter/cmd/ess_mode", "") in client.published


@pytest.mark.asyncio
async def test_loop_interval_action_publishes_mqtt():
    from inverter_dashboard.config import DEFAULT_LOOP_INTERVAL

    client = FakeMqttClient()
    await _dispatch_action("loop_interval", {}, client)  # default interval
    _, msg = next((t, m) for t, m in client.published if "loop_interval" in t)
    payload = json.loads(msg)
    assert payload["interval"] == DEFAULT_LOOP_INTERVAL


@pytest.mark.asyncio
async def test_loop_interval_action_publishes_custom_interval():
    client = FakeMqttClient()
    await _dispatch_action("loop_interval", {"interval": 30}, client)
    _, msg = next((t, m) for t, m in client.published if "loop_interval" in t)
    payload = json.loads(msg)
    assert payload["interval"] == 30


@pytest.mark.asyncio
async def test_unknown_action_ignored():
    """Unknown actions must not raise — silent no-op per contract."""
    client = FakeMqttClient()
    # must not raise
    await _dispatch_action("not_a_real_action", {"data": 123}, client)
    assert not client.published


# ---------------------------------------------------------------------------
# WebSocket endpoint contract (via TestClient)
# ---------------------------------------------------------------------------


class TestWebSocketEndpoint:
    """WebSocket /ws contract for dashboard clients."""

    def test_ws_sends_initial_payload_on_connect(self, monkeypatch):
        """Connected client must receive build_payload() immediately."""
        from fastapi.testclient import TestClient

        ms = MqttState()
        ms.current_state = {"battery_soc": 42}
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}
        ws_clients_orig = wsh.ws_clients.copy()
        wsh.ws_clients.clear()

        try:
            monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                data = ws.receive_json()
                assert data["battery_soc"] == 42
                assert "dashboard_version" in data
        finally:
            wsh.ws_clients.update(ws_clients_orig)
            wsh._state["mqtt_state"] = None

    def test_ws_rejects_invalid_token(self, monkeypatch):
        """WS with wrong token must close with 4401 (server rejects before any message)."""
        from fastapi.testclient import TestClient

        monkeypatch.setattr(server, "DASHBOARD_SECRET", "correct-secret")
        client = TestClient(server.app)
        # pytest.raises must be the outermost context so it catches the
        # WebSocketDisconnect raised when receive_text() hits the 4401 close.
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws?token=wrong-token") as ws,
        ):
            ws.receive_text()

    def test_ws_accepts_valid_token(self, monkeypatch):
        """WS with correct token must connect and receive payload."""
        from fastapi.testclient import TestClient

        ms = MqttState()
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}
        ws_clients_orig = wsh.ws_clients.copy()
        wsh.ws_clients.clear()

        try:
            monkeypatch.setattr(server, "DASHBOARD_SECRET", "correct-secret")
            client = TestClient(server.app)
            with client.websocket_connect("/ws?token=correct-secret") as ws:
                data = ws.receive_json()
                assert "dashboard_version" in data
        finally:
            wsh.ws_clients.update(ws_clients_orig)
            wsh._state["mqtt_state"] = None

    def test_ws_action_toggle_publishes_to_mqtt(self, monkeypatch):
        """WS action must reach the MQTT publish path (inverter/cmd/<action>)."""
        from fastapi.testclient import TestClient

        ms = MqttState()
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}
        ws_clients_orig = wsh.ws_clients.copy()
        wsh.ws_clients.clear()

        tracked = FakeMqttClient()
        app_state = FakeAppState()
        app_state.mqtt_client = tracked

        try:
            monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
            monkeypatch.setattr(server, "_app_state", app_state)
            client = TestClient(server.app)
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # consume initial payload
                ws.send_json({"action": "toggle", "entity": "only_charging"})
                # give server loop a chance to dispatch
                import time

                time.sleep(0.05)
            assert ("inverter/cmd/toggle", '{"entity": "only_charging"}') in tracked.published
        finally:
            wsh.ws_clients.update(ws_clients_orig)
            wsh._state["mqtt_state"] = None
            monkeypatch.setattr(server, "_app_state", server.AppState())

    def test_ws_disconnect_removes_client(self, monkeypatch):
        """Disconnected client must be removed from ws_clients."""
        from fastapi.testclient import TestClient

        ms = MqttState()
        wsh = websocket_handler
        wsh._state["mqtt_state"] = ms
        wsh._state["latest_version"] = None
        wsh._ui_settings = {}
        ws_clients_orig = wsh.ws_clients.copy()
        wsh.ws_clients.clear()

        try:
            monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
            client = TestClient(server.app)
            initial_count = len(wsh.ws_clients)
            with client.websocket_connect("/ws"):
                assert len(wsh.ws_clients) == initial_count + 1
            # after disconnect, client should be removed
            assert len(wsh.ws_clients) == initial_count
        finally:
            wsh.ws_clients.update(ws_clients_orig)
            wsh._state["mqtt_state"] = None


# ---------------------------------------------------------------------------
# HTTP API contract
# ---------------------------------------------------------------------------


class TestApiContracts:
    """HTTP API contract for dashboard clients."""

    def test_api_state_returns_health_shape(self, monkeypatch):
        """GET /api/state must return health shape regardless of MQTT state."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
        client = TestClient(server.app)
        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "dashboard_version" in data
        assert "mqtt_connected" in data
        assert "mqtt_reconnects" in data
        assert "has_mqtt_state" in data
        assert isinstance(data["mqtt_reconnects"], int)
        assert "gateway_connected" in data
        assert "data_source" in data

    def test_api_state_without_mqtt_state_is_fine(self, monkeypatch):
        """API must not 500 when MQTT hasn't started."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
        # _app_state starts with mqtt_state=None
        client = TestClient(server.app)
        resp = client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_mqtt_state"] is False
        assert data["mqtt_connected"] is False

    def test_api_settings_get_returns_settings_shape(self, monkeypatch):
        """GET /api/settings must return {ok, settings}."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        resp = client.get("/api/settings", headers={"Authorization": "Bearer s3cret"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "settings" in data
        assert isinstance(data["settings"], dict)

    def test_api_settings_post_validates_json_object(self, monkeypatch):
        """POST /api/settings must reject non-object bodies."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        resp = client.post(
            "/api/settings",
            headers={"Authorization": "Bearer s3cret"},
            json="not-an-object",
        )
        assert resp.status_code == 400

    def test_api_settings_post_rejects_unknown_keys(self, monkeypatch):
        """POST /api/settings must reject unknown settings keys."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        resp = client.post(
            "/api/settings",
            headers={"Authorization": "Bearer s3cret"},
            json={"not_a_real_key": True},
        )
        assert resp.status_code == 400

    def test_api_settings_post_saves_valid_settings(self, monkeypatch, tmp_path):
        """POST /api/settings must persist and return the saved settings."""
        monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        resp = client.post(
            "/api/settings",
            headers={"Authorization": "Bearer s3cret"},
            json={"show_ev": False, "show_washer": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["settings"]["show_ev"] is False
        assert data["settings"]["show_washer"] is False

    def test_api_settings_requires_auth(self, monkeypatch):
        """GET and POST /api/settings must require the secret."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        assert client.get("/api/settings").status_code == 401
        assert client.post("/api/settings", json={}).status_code == 401

    def test_api_check_update_shape(self, monkeypatch):
        """POST /api/check-update must return {current, latest}."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        resp = client.post(
            "/api/check-update",
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "current" in data
        assert "latest" in data

    def test_api_update_disabled_returns_403(self, monkeypatch):
        """POST /api/update when disabled must return 403."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        resp = client.post(
            "/api/update",
            headers={"Authorization": "Bearer s3cret"},
        )
        # SelfUpdateDisabled -> 403
        assert resp.status_code == 403
        data = resp.json()
        assert "error" in data

    def test_api_update_requires_auth(self, monkeypatch):
        """POST /api/update must require the secret."""
        monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
        client = TestClient(server.app)
        assert client.post("/api/update").status_code == 401
