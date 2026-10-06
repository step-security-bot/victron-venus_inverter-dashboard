"""HTTP and WebSocket snapshots report source health, including disconnection."""

from types import SimpleNamespace

import pytest

from src.inverter_dashboard import ha_client, websocket_handler


@pytest.fixture
def transport_context(monkeypatch):
    mqtt = SimpleNamespace(
        get_state=lambda: {"mqtt_connected": True, "gateway_connected": True},
        get_notifications=list,
        controller_available=lambda: False,
        controller_commands_available=lambda: False,
        camera_event=None,
    )
    monkeypatch.setitem(websocket_handler._state, "mqtt_state", mqtt)
    monkeypatch.setattr(ha_client, "merge_overlay", lambda value: value)
    monkeypatch.setattr(ha_client, "ui_config_patch", dict)
    monkeypatch.setattr(websocket_handler, "_can_control_water", lambda *_: False)


@pytest.mark.parametrize(
    "source,mqtt_connected,gateway_connected",
    [("mqtt", True, False), ("mqtt", False, False), ("igw", False, True), ("igw", False, False)],
)
def test_payload_reports_current_source_health(
    transport_context, monkeypatch, source, mqtt_connected, gateway_connected
):
    app = SimpleNamespace(
        data_source=source,
        mqtt_connected=mqtt_connected,
        gateway_connected=gateway_connected,
    )
    monkeypatch.setitem(websocket_handler._state, "app_state", app)
    payload = websocket_handler.build_payload()
    assert payload["data_source"] == source
    assert payload["mqtt_connected"] is mqtt_connected
    assert payload["gateway_connected"] is gateway_connected
    assert payload["native_connected"] is (
        mqtt_connected if source == "mqtt" else gateway_connected
    )
    app.mqtt_connected = False
    app.gateway_connected = False
    disconnected = websocket_handler.build_payload()
    assert disconnected["mqtt_connected"] is False
    assert disconnected["gateway_connected"] is False
    assert disconnected["native_connected"] is False


@pytest.mark.parametrize("app", [None, SimpleNamespace()])
def test_payload_does_not_invent_missing_transport_status(transport_context, monkeypatch, app):
    monkeypatch.setitem(websocket_handler._state, "app_state", app)
    payload = websocket_handler.build_payload()
    assert not {"data_source", "mqtt_connected", "gateway_connected"} & payload.keys()
