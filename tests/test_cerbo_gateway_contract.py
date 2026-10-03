"""The gateway and LAN transports must give the browser identical measurements."""

import json

import pytest
from fastapi import Response
from starlette.requests import Request

from inverter_dashboard import gateway, server, websocket_handler
from inverter_dashboard.server import MqttState


@pytest.mark.asyncio
async def test_gateway_matches_live_cerbo_and_preserves_zero():
    snapshot = {
        "system": {
            "0/Ac/Grid/L1/Power": 100,
            "0/Ac/Grid/L2/Power": 200,
            "0/Ac/Grid/L3/Power": -300,
            "0/Dc/Battery/Soc": 0,
            "0/Dc/Battery/Voltage": 40,
        },
        "solarcharger": {"2/Yield/Power": 0, "2/Dc/0/Power": 900},
        "pvinverter": {"3/Ac/Power": 0, "3/Ac/L1/Power": 800},
        "tank": {"21/Level": 0.5},
        "ev": {"22/Ac/Power": 3200},
        "evcharger": {"40/Ac/Power": 7200},
    }
    live, remote = MqttState(), MqttState()
    for kind, leaves in snapshot.items():
        for path, value in leaves.items():
            await live.on_message(f"N/site/{kind}/{path}", json.dumps({"value": value}).encode())
    gateway.apply_snapshot(remote, snapshot)
    for key, expected in {
        "gt": 0,
        "g3": -300,
        "battery_soc": 0,
        "mppt_total": 0,
        "pv_inverter_total": 0,
        "solar_total": 0,
        "water_level": 0.5,
        "ev_power": 3200,
        "ev_charging_kw": 7.2,
    }.items():
        assert live.current_state[key] == pytest.approx(expected), key
        assert remote.current_state[key] == pytest.approx(expected), key


def test_snapshot_removal_and_api_nulls_clear_previous_measurements(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    ms = MqttState()
    gateway.apply_snapshot(
        ms,
        {
            "system": {"0/Ac/Grid/L3/Power": 77, "0/Dc/Battery/Voltage": 40},
            "battery": {"1/Soc": 42},
        },
    )
    gateway.apply_snapshot(ms, {})
    monkeypatch.setitem(websocket_handler._state, "mqtt_state", ms)
    monkeypatch.setattr(websocket_handler.ha_client, "merge_overlay", lambda state: state)
    payload = websocket_handler.build_payload()
    assert payload["g3"] is None
    assert payload["battery_soc"] is None
    assert payload["telemetry_available"]["g3"] is False
    assert payload["batteries"] == []


@pytest.mark.asyncio
async def test_http_fallback_preserves_complete_websocket_contract(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    ms = MqttState()
    gateway.apply_snapshot(
        ms,
        {
            "system": {"0/Ac/Grid/L3/Power": 77, "0/Dc/Battery/Voltage": 40},
            "battery": {"1/Soc": 0},
            "ev": {"22/Ac/Power": 3200},
        },
    )
    monkeypatch.setattr(server._app_state, "mqtt_state", ms)
    monkeypatch.setitem(websocket_handler._state, "mqtt_state", ms)
    monkeypatch.setattr(websocket_handler.ha_client, "merge_overlay", lambda state: state)
    response = await server.api_state(Request({"type": "http", "headers": []}), Response())
    assert response["g3"] == 77
    assert response["ev_power"] == 3200
    assert response["battery_soc"] == 0
    assert response["batteries"][0]["soc"] == 0
    assert response["telemetry_available"]["battery_soc"] is True
