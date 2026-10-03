"""Known slim-controller policy/status fields survive both browser transports."""

import json

import pytest
from fastapi import Response
from starlette.requests import Request

from inverter_dashboard import server, websocket_handler
from inverter_dashboard.server import MqttState


@pytest.mark.asyncio
async def test_slim_controller_metadata_survives_websocket_and_http_payloads(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    ms = MqttState()
    monkeypatch.setattr(server._app_state, "mqtt_state", ms)
    monkeypatch.setitem(websocket_handler._state, "mqtt_state", ms)
    monkeypatch.setattr(websocket_handler.ha_client, "merge_overlay", lambda state: state)
    controller = {
        "limits": {"min": -2300, "max": 2250},
        "loop_interval": 0.3,
        "dvcc_limits": {
            "ccl": 0.0,
            "dcl": 75.0,
            "ccl_reason": "soc_100",
            "dcl_reason": "soc_ok",
            "cvl": 54.4,
            "max_cell_voltage": 3.4,
            "min_cell_id": "C2",
            "min_temp": None,
        },
        "perf": {
            "cycle_ms": {"p50": 1.3, "p99": 10.5, "missed_deadlines": 0},
            "stage_ms": {"write": {"p50": 0.8}},
            "cpu_percent": None,
            "signals_healthy": True,
            "dbus_subprocess_calls": 0,
        },
        "grid_control_valid": False,
        "grid_control_reason": "Meter reading expired",
        "grid_loss_state": "holding",
        "grid_loss_hold_seconds": 300.0,
        "grid_loss_elapsed": 0.0,
        "grid_loss_remaining": 300.0,
        "grid_loss_zero_applied": False,
    }
    await ms.on_message(
        "inverter/state",
        json.dumps({**controller, "grid_loss_internal": "not public"}).encode(),
    )
    for payload in (
        websocket_handler.build_payload(),
        await server.api_state(Request({"type": "http", "headers": []}), Response()),
    ):
        for key, expected in controller.items():
            assert payload[key] == expected, key
        assert "grid_loss_internal" not in payload


@pytest.mark.asyncio
async def test_recovery_nulls_clear_previous_controller_status(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    ms = MqttState()
    monkeypatch.setattr(server._app_state, "mqtt_state", ms)
    monkeypatch.setitem(websocket_handler._state, "mqtt_state", ms)
    monkeypatch.setattr(websocket_handler.ha_client, "merge_overlay", lambda state: state)
    ms._merge_daemon_state(
        {
            "grid_control_reason": "Meter unavailable",
            "grid_loss_elapsed": 20,
            "grid_loss_remaining": 0,
            "dvcc_limits": {"ccl": 0},
        }
    )
    recovery = {
        "grid_control_valid": True,
        "grid_control_reason": None,
        "grid_loss_state": "normal",
        "grid_loss_hold_seconds": None,
        "grid_loss_elapsed": None,
        "grid_loss_remaining": None,
        "grid_loss_zero_applied": False,
        "dvcc_limits": None,
    }
    await ms.on_message("inverter/state", json.dumps(recovery).encode())
    for payload in (
        websocket_handler.build_payload(),
        await server.api_state(Request({"type": "http", "headers": []}), Response()),
    ):
        for key, expected in recovery.items():
            assert payload[key] == expected, key
