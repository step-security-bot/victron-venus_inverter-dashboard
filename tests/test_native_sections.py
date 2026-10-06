"""Native Loads, bank SoC and water controls across MQTT and IGW."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from inverter_dashboard import config, gateway, ha_client
from inverter_dashboard import websocket_handler as ws
from inverter_dashboard.cerbo import voltage_soc
from inverter_dashboard.server import MqttState

REAL_POST_COMMAND = gateway.post_command


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", "site")
    monkeypatch.setattr(config, "WATER_TANK_INSTANCE", 21)
    monkeypatch.setattr(config, "WATER_PUMP_INSTANCE", 7)
    monkeypatch.setattr(config, "WATER_VALVE_INSTANCE", 9)
    monkeypatch.setattr(ha_client, "is_direct_mode", lambda: False)
    gateway.set_active_source("mqtt")
    state = MqttState()
    monkeypatch.setitem(ws._state, "mqtt_state", state)
    monkeypatch.setitem(ws._state, "app_state", None)
    yield state
    gateway.set_active_source("none")


async def deliver(state, snapshot, transport):
    if transport == "igw":
        gateway.apply_snapshot(state, snapshot)
        return
    for kind, leaves in snapshot.items():
        for path, value in leaves.items():
            await state.on_message(f"N/site/{kind}/{path}", json.dumps({"value": value}).encode())


@pytest.mark.parametrize(
    "voltage,expected",
    [(30, 0), (40, 0), (40.936, 7), (47.2, 50), (51.2, 78), (54.4, 100), (60, 100)],
)
def test_bank_voltage_formula_matches_desktop(voltage, expected):
    assert voltage_soc(voltage) == expected


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_bank_soc_prefers_shunt_voltage_and_keeps_real_device_soc(state, transport):
    await deliver(
        state,
        {
            "battery": {
                "511/ProductName": "SmartShunt",
                "511/Dc/0/Voltage": 51.2,
                "511/Soc": 100,
                "40/CustomName": "Chain",
                "40/Dc/0/Voltage": 48,
                "40/Soc": 45,
            },
            "system": {"0/Dc/Battery/Voltage": 47.2, "0/Dc/Battery/Soc": 10},
        },
        transport,
    )
    state._merge_daemon_state({"battery_soc": 99, "loads": {"HA": 999}, "water_level": 99})
    payload = ws.build_payload()
    assert payload["battery_soc"] == 78
    assert payload["battery_voltage"] == 51.2
    assert {entry["instance"]: entry["soc"] for entry in payload["batteries"]} == {
        "40": 45,
        "511": 100,
    }
    assert len(payload["batteries"]) == 2
    assert not payload.get("loads")
    assert payload.get("water_level") is None


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_voltage_fallback_and_null_never_reuse_reported_soc(state, transport):
    await deliver(
        state, {"system": {"0/Dc/Battery/Voltage": 47.2, "0/Dc/Battery/Soc": 99}}, transport
    )
    assert state.current_state["battery_soc"] == 50
    await deliver(
        state, {"system": {"0/Dc/Battery/Voltage": None, "0/Dc/Battery/Soc": 99}}, transport
    )
    state._merge_daemon_state({"battery_soc": 90})
    assert state.current_state["battery_soc"] is None
    assert state.current_state["telemetry_available"]["battery_soc"] is False


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_active_loads_keep_instance_identity_and_names(state, transport):
    await deliver(
        state,
        {
            "acload": {
                "81/CustomName": "Heater",
                "81/Ac/L1/Power": 20,
                "81/Ac/L2/Power": 30,
                "82/CustomName": "Heater",
                "82/Ac/Power": 0,
            }
        },
        transport,
    )
    payload = ws.build_payload()
    assert payload["loads"] == {"81": 50, "82": 0}
    assert payload["load_names"] == {"81": "Heater", "82": "Heater"}
    if transport == "igw":
        gateway.apply_snapshot(state, {"acload": {"81/CustomName": "Renamed", "81/Ac/Power": 12}})
    else:
        await state.on_message("N/site/acload/82/Ac/Power", b"")
        await deliver(state, {"acload": {"81/CustomName": "Renamed", "81/Ac/Power": 12}}, transport)
    state._merge_daemon_state({"loads": {"stale": 800}, "load_names": {"81": "daemon"}})
    assert ws.build_payload()["loads"] == {"81": 12}
    assert ws.build_payload()["load_names"] == {"81": "Renamed"}


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_water_percent_states_and_modes_do_not_depend_on_ha(state, transport):
    await deliver(
        state,
        {
            "tank": {"21/Level": 0.5},
            "pump": {
                "7/State": 0.49,
                "7/Mode": 0,
                "9/State": 0.5,
                "9/Mode": 2,
            },
        },
        transport,
    )
    state._merge_daemon_state(
        {"water_level": 90, "pump_switch": True, "water_valve": False, "pump_mode": 1}
    )
    payload = ws.build_payload()
    assert payload["water_level"] == 0.5
    assert payload["pump_switch"] is False
    assert payload["water_valve"] is True
    assert payload["pump_mode"] == payload["water_pump_mode"] == 0
    assert payload["water_valve_mode"] == 2
    await deliver(
        state, {"tank": {"21/Level": None}, "pump": {"7/State": None, "7/Mode": None}}, transport
    )
    assert state.current_state["water_level"] is None
    assert state.current_state["pump_switch"] is None
    assert state.current_state["pump_mode"] is None
    assert state.current_state["water_pump_mode"] is None


@pytest.fixture
def igw_water(state, monkeypatch):
    gateway.set_active_source("igw")
    gateway.apply_snapshot(
        state,
        {
            "capabilities": {"water_mode": True},
            "pump": {
                "7/Mode": 0,
                "7/State": 0,
                "9/Mode": 2,
                "9/State": 0,
            },
        },
    )
    app = SimpleNamespace(
        mqtt_state=state,
        mqtt_client=None,
        data_source="igw",
        mqtt_connected=False,
        gateway_connected=True,
    )
    monkeypatch.setitem(ws._state, "app_state", app)

    def fresh_snapshot(_client):
        return {
            "capabilities": state.gateway_capabilities,
            "pump": {
                f"{instance}/{path}": value
                for instance, leaves in state._cerbo_devices.get("pump", {}).items()
                for path, value in leaves.items()
            },
        }

    monkeypatch.setattr(gateway, "fetch_snapshot", AsyncMock(side_effect=fresh_snapshot))
    post = AsyncMock()
    monkeypatch.setattr(gateway, "post_command", post)
    return app, post


@pytest.mark.parametrize("which,instance", [("pump", 7), ("valve", 9)])
@pytest.mark.parametrize("mode", [0, 1, 2])
async def test_gateway_water_writes_exact_native_target_without_controller(
    igw_water, which, instance, mode
):
    app, post = igw_water
    before = copy.deepcopy(app.mqtt_state.current_state)
    payload = ws.build_payload()
    assert payload["native_connected"] is True
    assert payload["mqtt_connected"] is False
    assert payload["gateway_connected"] is True
    assert payload["controller_controls_available"] is False
    assert payload["water_controls_available"] is True
    assert payload["water_pump_controls_available"] is True
    assert payload["water_valve_controls_available"] is True
    await ws._dispatch_action("water_mode", {"which": which, "mode": mode}, None)
    post.assert_awaited_once_with(
        "water_mode",
        {"instance": instance, "mode": mode},
        expected_generation=gateway.source_generation(),
        before_send=ANY,
    )
    assert app.mqtt_state.current_state == before


@pytest.mark.parametrize(
    "condition", ["legacy_gateway", "offline", "missing_mode", "disconnected_device"]
)
async def test_gateway_water_requires_capability_connection_and_native_mode(igw_water, condition):
    app, post = igw_water
    if condition == "legacy_gateway":
        gateway.apply_snapshot(app.mqtt_state, {"pump": {"7/Mode": 0}})
    elif condition == "offline":
        app.gateway_connected = False
    elif condition == "missing_mode":
        app.mqtt_state.replace_cerbo_snapshot({"pump": {"7/State": 1}})
    else:
        app.mqtt_state.replace_cerbo_snapshot({"pump": {"7/Mode": 0, "7/Connected": 0}})
    assert ws.build_payload()["water_pump_controls_available"] is False
    with pytest.raises(RuntimeError):
        await ws._dispatch_action("water_mode", {"which": "pump", "mode": 1}, None)
    post.assert_not_awaited()


async def test_gateway_water_failure_is_not_retried_or_optimistically_applied(igw_water):
    app, post = igw_water
    before = copy.deepcopy(app.mqtt_state.current_state)
    post.side_effect = RuntimeError("Native device is unavailable")
    with pytest.raises(RuntimeError, match="Native device"):
        await ws._dispatch_action("water_mode", {"which": "pump", "mode": 1}, None)
    post.assert_awaited_once()
    assert app.mqtt_state.current_state == before


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
@pytest.mark.parametrize("connected", [None, 0, -1, 2, 0.5, True, "1", [], {}])
async def test_invalid_connection_clears_native_devices(state, transport, connected):
    initial = {
        "battery": {"1/ProductName": "SmartShunt", "1/Dc/0/Voltage": 51.2},
        "system": {"0/Dc/Battery/Voltage": 47.2},
        "acload": {"81/Ac/Power": 50},
        "pump": {"7/Mode": 1, "7/State": 1},
    }
    await deliver(state, initial, transport)
    assert state.current_state["battery_soc"] == 78
    updated = copy.deepcopy(initial)
    for kind, instance in (("battery", 1), ("acload", 81), ("pump", 7)):
        updated[kind][f"{instance}/Connected"] = connected
    await deliver(state, updated, transport)
    assert state.current_state["battery_soc"] == 50
    assert state.current_state["batteries"] == []
    assert state.current_state["loads"] == {}
    assert state.current_state["pump_switch"] is None
    assert ws._water_device_available(state, "pump") is False


async def test_gateway_water_instance_zero_is_not_replaced_by_default(igw_water, monkeypatch):
    app, post = igw_water
    monkeypatch.setattr(config, "WATER_PUMP_INSTANCE", 0)
    gateway.apply_snapshot(
        app.mqtt_state, {"capabilities": {"water_mode": True}, "pump": {"0/Mode": 2}}
    )
    assert ws.build_payload()["water_pump_controls_available"] is True
    await ws._dispatch_action("water_mode", {"which": "pump", "mode": 0}, None)
    post.assert_awaited_once_with(
        "water_mode",
        {"instance": 0, "mode": 0},
        expected_generation=gateway.source_generation(),
        before_send=ANY,
    )


async def test_multiple_shunts_choose_first_valid_voltage(state):
    await deliver(
        state,
        {
            "battery": {
                "1/ProductName": "SmartShunt",
                "1/Dc/0/Voltage": None,
                "2/ProductName": "SmartShunt",
                "2/Dc/0/Voltage": 51.2,
            }
        },
        "mqtt",
    )
    assert state.current_state["battery_soc"] == 78


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_native_freshness_tracks_receipt_not_controller_or_sensor_age(
    state, monkeypatch, transport
):
    from inverter_dashboard import cerbo

    clock = [1000.0]
    monkeypatch.setattr(cerbo.time, "time", lambda: clock[0])
    monkeypatch.setattr(cerbo.time, "monotonic", lambda: clock[0])
    app = SimpleNamespace(
        data_source=transport,
        mqtt_connected=transport == "mqtt",
        gateway_connected=transport == "igw",
    )
    monkeypatch.setitem(ws._state, "app_state", app)
    assert ws.build_payload()["telemetry"] == {
        "source": transport,
        "observed_at": None,
        "quality": "unknown",
        "timestamp_source": "local_receipt",
    }
    await deliver(state, {"acload": {"81/Ac/Power": 0}}, transport)
    assert ws.build_payload()["telemetry"]["observed_at"] == 1000000
    assert ws.build_payload()["telemetry"]["quality"] == "live"
    clock[0] += 121
    state._merge_daemon_state({"battery_soc": 90, "version": "controller"})
    assert ws.build_payload()["telemetry"]["observed_at"] == 1000000
    assert ws.build_payload()["telemetry"]["quality"] == "stale"
    # Repeated valid values prove another receipt, not a new sensor measurement.
    await deliver(state, {"acload": {"81/Ac/Power": 0}}, transport)
    assert ws.build_payload()["telemetry"]["observed_at"] == 1121000
    assert ws.build_payload()["telemetry"]["quality"] == "live"
    app.mqtt_connected = app.gateway_connected = False
    assert ws.build_payload()["telemetry"]["quality"] == "stale"
    assert ws.build_payload()["native_connected"] is False


async def test_foreign_and_invalid_native_messages_do_not_refresh_freshness(state, monkeypatch):
    from inverter_dashboard import cerbo

    clock = [1000.0]
    monkeypatch.setattr(cerbo.time, "time", lambda: clock[0])
    monkeypatch.setattr(cerbo.time, "monotonic", lambda: clock[0])
    await deliver(state, {"acload": {"81/Ac/Power": 5}}, "mqtt")
    clock[0] += 121
    await state.on_message("N/other/acload/81/Ac/Power", b'{"value":20}')
    await state.on_message("N/site/acload/81/Ac/Power", b'{"value":"bad"}')
    assert state.native_telemetry("mqtt", True)["quality"] == "stale"
    assert state.native_telemetry("igw", True)["quality"] == "unknown"


async def test_unchanged_native_notification_refreshes_websocket_after_idle(state, monkeypatch):
    from inverter_dashboard import cerbo

    clock = [1000.0]
    monkeypatch.setattr(cerbo.time, "time", lambda: clock[0])
    monkeypatch.setattr(cerbo.time, "monotonic", lambda: clock[0])
    callback = AsyncMock()
    state.set_state_callback(callback)
    await deliver(state, {"acload": {"81/Ac/Power": 0}}, "mqtt")
    callback.reset_mock()
    await deliver(state, {"acload": {"81/Ac/Power": 0}}, "mqtt")
    callback.assert_not_awaited()
    clock[0] += 121
    await deliver(state, {"acload": {"81/Ac/Power": 0}}, "mqtt")
    callback.assert_awaited_once()


@pytest.mark.parametrize("ha_connected", [False, True])
async def test_ha_cannot_fill_or_override_native_sections(state, monkeypatch, ha_connected):
    monkeypatch.setattr(ha_client, "is_direct_mode", lambda: True)
    monkeypatch.setattr(
        ha_client,
        "_sensor_entities",
        {"battery_soc": "sensor.bank_soc", "water_level": "sensor.tank", "loads": "sensor.loads"},
    )
    monkeypatch.setattr(
        ha_client, "_switch_entities", {"pump_switch": "switch.pump", "water_valve": "switch.valve"}
    )
    monkeypatch.setattr(
        ha_client,
        "_overlay",
        {
            "ha_direct_connected": ha_connected,
            "battery_soc": 99,
            "water_level": 99,
            "loads": {"HA": 999},
            "pump_switch": True,
            "water_valve": True,
        },
    )
    payload = ws.build_payload()
    assert payload.get("battery_soc") is None
    assert payload.get("water_level") is None
    assert not payload.get("loads")
    assert payload.get("pump_switch") is None
    await deliver(
        state,
        {
            "system": {"0/Dc/Battery/Voltage": 47.2},
            "tank": {"21/Level": 0.5},
            "acload": {"81/Ac/Power": -20},
            "pump": {"7/State": 0, "9/State": 0},
        },
        "mqtt",
    )
    payload = ws.build_payload()
    assert payload["battery_soc"] == 50
    assert payload["water_level"] == 0.5
    assert payload["loads"] == {"81": -20}
    assert payload["pump_switch"] is payload["water_valve"] is False


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_reconnect_does_not_restore_pre_disconnect_freshness(state, transport):
    await deliver(state, {"acload": {"81/Ac/Power": 0}}, transport)
    before = state.native_telemetry(transport, True)
    assert before["quality"] == "live"
    state.clear_cerbo_state()
    after = state.native_telemetry(transport, True)
    assert after["quality"] == "stale"
    assert after["observed_at"] == before["observed_at"]
    await deliver(state, {"acload": {"81/Ac/Power": 0}}, transport)
    assert state.native_telemetry(transport, True)["quality"] == "live"


@pytest.mark.parametrize("case", ["retained", "expired", "future", "malformed", "retired"])
async def test_water_commands_require_current_nonretained_mode(state, case):
    client = SimpleNamespace(publish=AsyncMock())
    app = SimpleNamespace(
        mqtt_state=state,
        mqtt_client=client,
        data_source="mqtt",
        mqtt_connected=True,
        gateway_connected=False,
    )
    ws._state["app_state"] = app
    await state.on_message("N/site/pump/7/Mode", b'{"value":0}', retained=case == "retained")
    if case == "expired":
        state._water_mode_observations["7"] = time.monotonic() - 31
    elif case == "future":
        state._water_mode_observations["7"] = time.monotonic() + 1
    elif case == "malformed":
        await state.on_message("N/site/pump/7/Mode", b'{"value":"1"}')
    elif case == "retired":
        state.clear_cerbo_state()
    assert not ws.build_payload()["water_pump_controls_available"]
    with pytest.raises(RuntimeError):
        await ws._dispatch_action("water_mode", {"which": "pump", "mode": 1}, client)
    client.publish.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"[]",
        b"{}",
        b'{"value":"1"}',
        b'{"value":true}',
        b'{"value":null}',
        b'{"value":0.5}',
        b'{"value":3}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
    ],
)
async def test_invalid_current_water_mode_revokes_until_valid_observation(state, payload):
    await state.on_message("N/site/pump/7/Mode", b'{"value":0}')
    assert state.water_mode_fresh("7")
    # A foreign source cannot revoke this source's authority.
    await state.on_message("N/other-site/pump/7/Mode", payload)
    assert state.water_mode_fresh("7")
    await state.on_message("N/site/pump/9/Mode", payload)
    assert state.water_mode_fresh("7")
    await state.on_message("N/site/pump/7/State", payload)
    assert state.water_mode_fresh("7")
    await state.on_message("N/site/pump/7/Mode", payload)
    assert not state.water_mode_fresh("7")
    await state.on_message("N/site/pump/7/Mode", b'{"value":1}', retained=True)
    assert not state.water_mode_fresh("7")
    await state.on_message("N/site/pump/7/Mode", b'{"value":1}')
    assert state.water_mode_fresh("7")


@pytest.mark.parametrize(
    "case",
    [
        "source_roundtrip",
        "device_disconnected",
        "mode_removed",
        "capability_withdrawn",
        "client_entry_offline",
    ],
)
async def test_water_gateway_revalidates_current_context_before_post(igw_water, monkeypatch, case):
    app, post = igw_water
    snapshot = {"capabilities": {"water_mode": True}, "pump": {"7/Mode": 0}}

    async def fresh(_client):
        if case == "source_roundtrip":
            gateway.set_active_source("mqtt")
            gateway.set_active_source("igw")
            await asyncio.sleep(0)
        elif case == "device_disconnected":
            snapshot["pump"]["7/Connected"] = 0
        elif case == "mode_removed":
            snapshot["pump"].clear()
        elif case == "capability_withdrawn":
            snapshot["capabilities"].clear()
        return snapshot

    monkeypatch.setattr(gateway, "fetch_snapshot", fresh)
    if case == "client_entry_offline":
        # The real POST helper checks after HTTP client entry, before its POST.
        from inverter_dashboard import config as cfg

        monkeypatch.setattr(cfg, "GATEWAY_URL", "https://gateway.example")
        fake = AsyncMock()
        count = [0]

        async def enter():
            count[0] += 1
            if count[0] == 2:
                app.gateway_connected = False
            return fake

        fake.__aenter__.side_effect = enter
        monkeypatch.setattr(gateway, "_new_gateway_client", lambda: fake)
        # Retrieve implementation rather than the fixture's dispatch mock.
        monkeypatch.setattr(gateway, "post_command", REAL_POST_COMMAND)
        with pytest.raises(RuntimeError):
            await ws._dispatch_action("water_mode", {"which": "pump", "mode": 1}, None)
        fake.post.assert_not_awaited()
    else:
        with pytest.raises((RuntimeError, ValueError, asyncio.CancelledError)):
            await ws._dispatch_action("water_mode", {"which": "pump", "mode": 1}, None)
        post.assert_not_awaited()
