"""Controller and native EV contracts shared by direct MQTT and IGW."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from inverter_dashboard import config, gateway, ha_client, server
from inverter_dashboard import websocket_handler as ws
from inverter_dashboard.server import MqttState


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", "site")
    monkeypatch.setattr(config, "EV_INSTANCE", None)
    monkeypatch.setattr(config, "EVCHARGER_INSTANCE", None)
    gateway.set_active_source("mqtt")
    yield MqttState()
    gateway.set_active_source("none")


async def deliver(state, snapshot, transport):
    if transport == "igw":
        gateway.apply_snapshot(state, snapshot)
        return
    for kind, leaves in snapshot.items():
        if kind == "inverter":
            await state.on_message("inverter/state", json.dumps(leaves).encode())
        else:
            for path, value in leaves.items():
                await state.on_message(
                    f"N/site/{kind}/{path}", json.dumps({"value": value}).encode()
                )


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
@pytest.mark.parametrize("controller_first", [True, False])
async def test_controller_and_native_ev_survive_either_arrival_order(
    state, transport, controller_first, monkeypatch
):
    controller = {
        "inverter": {
            "booleans": {"only_charging": True, "no_feed": False},
            "ess_mode": {"mode_name": "Old controller mirror"},
            "car_soc": 1,
            "ev_power": 2,
            "ui_config": {
                "header_toggles": [{"entity": "only_charging", "label": "Only Charging"}]
            },
        }
    }
    native = {
        "ev": {"72/CustomName": "Vehicle", "72/Soc": 81, "72/Ac/Power": 0},
        "evcharger": {"91/CustomName": "Wallbox", "91/Ac/Power": 7400},
        "settings": {"0/Settings/CGwacs/Hub4Mode": 3},
    }
    if transport == "mqtt":
        for part in [controller, native] if controller_first else [native, controller]:
            await deliver(state, part, transport)
    else:
        # Consecutive complete snapshots and leaf ordering use the same reducer.
        await deliver(
            state,
            {**native, **controller} if controller_first else {**controller, **native},
            transport,
        )
    monkeypatch.setattr(ha_client, "is_direct_mode", lambda: False)
    monkeypatch.setitem(ws._state, "mqtt_state", state)
    payload = ws.build_payload()
    assert payload["booleans"] == {"only_charging": True, "no_feed": False}
    assert payload["controller_controls_available"] is True
    assert payload["ui_config"]["header_toggles"][0]["entity"] == "only_charging"
    assert payload["ess_mode"]["mode_name"] == "External control"
    assert payload["car_soc"] == 81
    assert payload["ev_power"] == 0
    assert payload["ev_charging_kw"] == 7.4
    assert payload["ev_charging_power"] == 7400
    assert payload["ev_present"] is True
    assert payload["evcharger_present"] is True
    assert [d["instance"] for d in payload["discovered_water_ev"]] == [72, 91]


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
@pytest.mark.parametrize("reverse", [True, False])
async def test_auto_selection_is_numeric_and_order_independent(state, transport, reverse):
    items = [("90/Soc", 90), ("12/Soc", 12), ("2/CustomName", "Metadata only")]
    if reverse:
        items.reverse()
    await deliver(state, {"ev": dict(items)}, transport)
    assert state.current_state["car_soc"] == 12
    assert [d["instance"] for d in state.current_state["discovered_water_ev"]] == [2, 12, 90]


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_explicit_zero_pin_does_not_fall_back_to_another_car(state, transport, monkeypatch):
    monkeypatch.setattr(config, "EV_INSTANCE", 0)
    await deliver(state, {"ev": {"0/Soc": 30, "8/Soc": 80}, "evcharger": {"40/Soc": 60}}, transport)
    assert state.current_state["car_soc"] == 30
    await deliver(
        state, {"ev": {"0/Connected": 0, "8/Soc": 81}, "evcharger": {"40/Soc": 61}}, transport
    )
    assert state.current_state["car_soc"] is None
    assert state.current_state["ev_present"] is False


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_legacy_evcharger_soc_and_null_invalidation(state, transport):
    await deliver(state, {"evcharger": {"88/Soc": 0, "88/Ac/Power": 0}}, transport)
    assert state.current_state["car_soc"] == 0
    assert state.current_state["ev_charging_kw"] == 0
    await deliver(state, {"evcharger": {"88/Soc": None, "88/Ac/Power": None}}, transport)
    state._merge_daemon_state({"car_soc": 99, "ev_charging_kw": 99})
    assert state.current_state["car_soc"] is None
    assert state.current_state["ev_charging_kw"] is None


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_removed_devices_clear_inventory_and_readings(state, transport):
    await deliver(state, {"ev": {"12/Soc": 44}, "evcharger": {"77/Ac/Power": 6200}}, transport)
    if transport == "igw":
        gateway.apply_snapshot(state, {})
    else:
        await state.on_message("N/site/ev/12/Soc", b"")
        await state.on_message("N/site/evcharger/77/Ac/Power", b"")
    assert state.current_state["car_soc"] is None
    assert state.current_state["ev_charging_kw"] is None
    assert state.current_state["ev_present"] is False
    assert state.current_state["discovered_water_ev"] == []


def test_boolean_partial_updates_and_unknown_are_not_false(state):
    state._merge_daemon_state({"booleans": {"only_charging": "on", "no_feed": False}})
    state._merge_daemon_state({"booleans": {"only_charging": None, "house_support": "invalid"}})
    assert state.current_state["booleans"] == {
        "only_charging": None,
        "no_feed": False,
        "house_support": None,
    }
    assert ws.InverterState(**state.current_state).model_dump()["booleans"]["only_charging"] is None


@pytest.mark.parametrize("malformed", [None, [], "true", 1])
def test_invalid_boolean_map_is_unknown_and_serializable(state, malformed):
    state._merge_daemon_state({"booleans": {"only_charging": True}})
    state._merge_daemon_state({"booleans": malformed})
    assert ws.InverterState(**state.current_state).booleans is None


@pytest.mark.parametrize("connected", [True, False])
def test_ha_mirrors_cannot_override_controller_flags(state, monkeypatch, connected):
    state._merge_daemon_state({"booleans": {"only_charging": True, "no_feed": None}})
    monkeypatch.setattr(ha_client, "is_direct_mode", lambda: True)
    monkeypatch.setattr(
        ha_client,
        "_boolean_entities",
        {
            "only_charging": "input_boolean.only_charging",
            "no_feed": "input_boolean.no_feed",
        },
    )
    monkeypatch.setattr(
        ha_client,
        "_overlay",
        {
            "ha_direct_connected": connected,
            "booleans": {"only_charging": False, "no_feed": False},
        },
    )
    assert ha_client.merge_overlay(state.get_state())["booleans"] == {
        "only_charging": True,
        "no_feed": None,
    }


def test_legacy_snapshot_does_not_clear_controller_but_explicit_null_does(state):
    state._merge_daemon_state({"booleans": {"only_charging": True}})
    gateway.apply_snapshot(state, {})
    assert state.current_state["booleans"]["only_charging"] is True
    gateway.apply_snapshot(state, {"inverter": None})
    assert state.current_state["booleans"] is None
    assert state.controller_available() is False


def test_complete_gateway_controller_snapshot_removes_absent_flags_and_metadata(state):
    gateway.apply_snapshot(
        state,
        {
            "inverter": {
                "booleans": {"only_charging": True, "no_feed": True},
                "ui_config": {"header_toggles": []},
            }
        },
    )
    gateway.apply_snapshot(state, {"inverter": {"booleans": {"only_charging": None}}})
    assert state.current_state["booleans"] == {"only_charging": None}
    assert state.current_state["ui_config"] is None
    gateway.apply_snapshot(state, {"inverter": {}})
    assert state.current_state["booleans"] is None


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_optimized_ess_requires_battery_life_reading(state, transport):
    await deliver(state, {"settings": {"0/Settings/CGwacs/Hub4Mode": 1}}, transport)
    assert state.current_state["ess_mode"] is None
    await deliver(
        state,
        {
            "settings": {
                "0/Settings/CGwacs/Hub4Mode": 1,
                "0/Settings/CGwacs/BatteryLife/State": 9,
            }
        },
        transport,
    )
    assert state.current_state["ess_mode"]["mode_name"] == "Keep batteries charged"
    await deliver(
        state,
        {
            "settings": {
                "0/Settings/CGwacs/Hub4Mode": 1,
                "0/Settings/CGwacs/BatteryLife/State": None,
            }
        },
        transport,
    )
    assert state.current_state["ess_mode"] is None


def test_controller_timeout_is_not_refreshed_by_native_telemetry(state, monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 100)
    state._merge_daemon_state({"booleans": {"only_charging": True}})
    monkeypatch.setattr(server.time, "monotonic", lambda: 221)
    state._handle_cerbo_device("N/site/ev/72/Soc", b'{"value":81}')
    assert state.get_state()["booleans"] is None
    assert state.current_state["car_soc"] == 81


@pytest.mark.parametrize("payload", [b"", b"null"])
async def test_retained_clear_invalidates_controller(state, payload):
    await state.on_message("inverter/state", b'{"booleans":{"only_charging":true}}')
    await state.on_message("inverter/state", payload)
    assert state.current_state["booleans"] is None
    assert state.controller_available() is False


async def test_gateway_only_controls_use_whitelist_without_mqtt_or_ha(state, monkeypatch):
    gateway.set_active_source("igw")
    post = AsyncMock()
    monkeypatch.setattr(gateway, "post_command", post)
    await ws._dispatch_action(
        "toggle", {"entity": "input_boolean.only_charging", "state": False}, None
    )
    await ws._dispatch_action("ess_mode", {}, None)
    await ws._dispatch_action("dry_run", {"value": True}, None)
    assert [call.args for call in post.await_args_list] == [
        ("toggle", {"entity": "only_charging", "state": "off"}),
        ("ess_mode", {}),
        ("dry_run", {"value": True}),
    ]
    with pytest.raises(ValueError):
        await ws._dispatch_action("toggle", {"entity": "only_charging"}, None)
    with pytest.raises(ValueError):
        await ws._dispatch_action("toggle", {"entity": "switch.boiler", "state": True}, None)
    with pytest.raises(ValueError):
        await ws._dispatch_action("setpoint", {"value": 100}, None)
    assert post.await_count == 3


async def test_gateway_failure_is_reported_without_retry(state, monkeypatch):
    gateway.set_active_source("igw")
    post = AsyncMock(side_effect=RuntimeError("controller unavailable"))
    monkeypatch.setattr(gateway, "post_command", post)
    with pytest.raises(RuntimeError, match="controller unavailable"):
        await ws._dispatch_action("ess_mode", {}, None)
    post.assert_awaited_once()


def test_ev_config_defaults_auto_and_explicit_instance():
    assert config.Config(_env_file=None).EV_INSTANCE is None
    assert config.Config(_env_file=None, EV_INSTANCE="auto").EV_INSTANCE is None
    assert config.Config(_env_file=None, EV_INSTANCE="0").EV_INSTANCE == 0
    with pytest.raises(ValueError):
        config.Config(_env_file=None, EV_INSTANCE=-1)


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_daemon_ev_mirrors_cannot_supply_first_native_reading(state, transport):
    mirrors = {
        "car_soc": 99,
        "ev_power": 9000,
        "car_charging_power": 8000,
        "ev_charging_kw": 9,
        "ev_charging_power": 9000,
        "ev_present": True,
        "evcharger_present": True,
        "discovered_water_ev": [{"kind": "ev", "instance": 999}],
    }
    await deliver(state, {"inverter": {**mirrors, "booleans": {"no_feed": False}}}, transport)
    assert all(state.current_state.get(key) is None for key in mirrors)
    assert state.current_state["booleans"]["no_feed"] is False
    await deliver(state, {"ev": {"72/Soc": 0}}, transport)
    assert state.current_state["car_soc"] == 0
    assert state.current_state.get("ev_power") is None


@pytest.mark.parametrize("entity", ["switch.no_feed", "binary_sensor.no_feed", "sensor.no_feed"])
def test_controller_key_recognition_does_not_claim_other_ha_domains(entity):
    assert ws._control_flag_key(entity) is None
    assert ha_client._ha_owns_field("independent_relay", entity)
    assert ws._control_flag_key("no_feed") == "no_feed"
    assert ws._control_flag_key("input_boolean.no_feed") == "no_feed"


async def test_configured_same_name_ha_switch_uses_ha_without_controller(state, monkeypatch):
    monkeypatch.setattr(ha_client, "_configured", True)
    monkeypatch.setattr(ha_client, "_direct", True)
    monkeypatch.setattr(ha_client, "_switch_entities", {"independent_relay": "switch.no_feed"})
    toggle = AsyncMock(return_value=True)
    publish = AsyncMock()
    monkeypatch.setattr(ha_client, "toggle_entity", toggle)
    monkeypatch.setattr(ha_client, "fetch_states_once", AsyncMock(return_value={}))
    monkeypatch.setattr(ws, "mqtt_publish", publish)
    monkeypatch.setattr(ws, "broadcast_state", AsyncMock())
    await ws._dispatch_action("toggle", {"entity": "switch.no_feed"}, None)
    toggle.assert_awaited_once_with("switch.no_feed")
    publish.assert_not_awaited()


async def test_gateway_connection_loss_immediately_clears_controller_state(monkeypatch):
    app = SimpleNamespace(mqtt_tasks=[], gateway_connected=True)
    monkeypatch.setattr(server, "_app_state", app)
    monkeypatch.setattr(ws, "set_mqtt_state", lambda value: None)
    broadcast = AsyncMock()
    monkeypatch.setattr(ws, "broadcast_state", broadcast)

    async def poll(app_state, apply_snapshot, status_emit, *, is_current):
        assert is_current()
        await apply_snapshot({"inverter": {"booleans": {"only_charging": True}}})
        assert app_state.mqtt_state.controller_available()
        app_state.gateway_connected = False
        await status_emit()
        assert not app_state.mqtt_state.controller_available()
        assert app_state.mqtt_state.current_state["booleans"] is None

    monkeypatch.setattr(gateway, "gateway_poll_loop", poll)
    server._start_gateway_client()
    await app.mqtt_tasks[0]
    assert broadcast.await_count == 2
