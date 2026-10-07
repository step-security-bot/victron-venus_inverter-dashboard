"""Vehicle power ownership, availability and public serialization contracts."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from inverter_dashboard import config, gateway, ha_client
from inverter_dashboard import websocket_handler as ws
from inverter_dashboard.server import MqttState

PORTAL = "site"
VEHICLE = "0"
CHARGER = "40"
CHARGER_WATTS = 7400


def _envelope(value):
    return json.dumps({"value": value}).encode()


@contextmanager
def _preserve_gateway_selection():
    """Neutralize gateway selection, then restore source and dual-path."""
    previous_source = gateway.active_source()
    previous_dual = gateway.dual_path_enabled()
    try:
        gateway.set_active_source("none")
        yield
    finally:
        gateway.set_active_source(previous_source, dual_path=previous_dual)


@pytest.fixture
def pinned(monkeypatch):
    """Pin portal, vehicle 0 and charger 40 before MqttState reads them."""
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", PORTAL)
    monkeypatch.setattr(config, "EV_INSTANCE", 0)
    monkeypatch.setattr(config, "EVCHARGER_INSTANCE", 40)
    monkeypatch.setattr(gateway, "_active_source", gateway._active_source)
    monkeypatch.setattr(gateway, "_dual_path", gateway._dual_path)
    with _preserve_gateway_selection():
        yield MqttState()


def _bind_payload(monkeypatch, state, transport):
    monkeypatch.setattr(ws, "_ui_settings", {})
    monkeypatch.setattr(
        ws,
        "_state",
        {
            "latest_version": None,
            "mqtt_state": state,
            "app_state": SimpleNamespace(
                data_source=transport,
                mqtt_connected=transport == "mqtt",
                gateway_connected=transport == "igw",
                mqtt_client=None,
                mqtt_state=state,
            ),
        },
    )
    gateway.set_active_source(transport, dual_path=gateway.dual_path_enabled())


async def _deliver(state, snapshot, transport):
    if transport == "igw":
        gateway.apply_snapshot(state, snapshot)
        return
    for kind, leaves in snapshot.items():
        for path, value in leaves.items():
            await state.on_message(f"N/{PORTAL}/{kind}/{path}", _envelope(value))


def _sample(vehicle_watts, charger_watts=CHARGER_WATTS):
    return {
        "ev": {f"{VEHICLE}/Ac/Power": vehicle_watts},
        "evcharger": {f"{CHARGER}/Ac/Power": charger_watts},
    }


def _assert_split(payload, vehicle_watts):
    assert "car_charging_power" in payload
    assert payload["car_charging_power"] == vehicle_watts
    assert payload["ev_power"] == vehicle_watts
    assert payload["ev_charging_power"] == CHARGER_WATTS
    assert payload["ev_charging_kw"] == 7.4
    assert payload.get("car_soc") is None


def _assert_vehicle_availability(payload, available):
    health = payload["telemetry_available"]
    assert health["car_charging_power"] is available
    assert health["ev_power"] is available


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
@pytest.mark.parametrize("vehicle_watts", [0, 1500])
async def test_ev1_selected_vehicle_watts_survive_public_serializer(
    pinned, monkeypatch, transport, vehicle_watts
):
    await _deliver(pinned, _sample(vehicle_watts), transport)
    _assert_split(pinned.current_state, vehicle_watts)
    _assert_vehicle_availability(pinned.current_state, True)
    _bind_payload(monkeypatch, pinned, transport)
    published = ws.build_payload()
    _assert_split(published, vehicle_watts)
    _assert_vehicle_availability(published, True)


def test_ev2_serializer_keeps_zero_and_explicit_null_but_omits_unset():
    assert (
        ws.InverterState(car_charging_power=0).model_dump(exclude_unset=True)["car_charging_power"]
        == 0
    )
    assert (
        ws.InverterState(car_charging_power=None).model_dump(exclude_unset=True)[
            "car_charging_power"
        ]
        is None
    )
    assert "car_charging_power" not in ws.InverterState().model_dump(exclude_unset=True)
    dumped = ws.InverterState(car_charging_power=0, not_a_public_field=1).model_dump(
        exclude_unset=True
    )
    assert dumped["car_charging_power"] == 0
    assert "not_a_public_field" not in dumped


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
@pytest.mark.parametrize("connected", [None, 0])
async def test_ev3_null_and_disconnected_first_claim_are_explicit(
    pinned, monkeypatch, transport, connected
):
    leaves = {f"{VEHICLE}/Ac/Power": None}
    if connected is not None:
        leaves[f"{VEHICLE}/Connected"] = connected
    await _deliver(pinned, {"ev": leaves}, transport)
    assert pinned.current_state["car_charging_power"] is None
    assert pinned.current_state["ev_power"] is None
    _assert_vehicle_availability(pinned.current_state, False)
    _bind_payload(monkeypatch, pinned, transport)
    payload = ws.build_payload()
    assert payload["car_charging_power"] is None
    assert payload["ev_power"] is None
    _assert_vehicle_availability(payload, False)


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_ev4_invalidation_and_recovery_ignore_daemon_mirror(pinned, monkeypatch, transport):
    await _deliver(pinned, _sample(1500), transport)
    await _deliver(pinned, _sample(None), transport)
    pinned._merge_daemon_state({"car_charging_power": 8000, "ev_power": 9000})
    monkeypatch.setattr(ha_client, "is_direct_mode", lambda: True)
    monkeypatch.setattr(ha_client, "_sensor_entities", {"car_charging_power": "sensor.car_power"})
    monkeypatch.setattr(
        ha_client,
        "_overlay",
        {"ha_direct_connected": True, "car_charging_power": 8000},
    )
    merged = ha_client.merge_overlay(pinned.get_state())
    assert merged["car_charging_power"] is None
    assert merged["ev_power"] is None
    assert merged["ev_charging_power"] == CHARGER_WATTS
    assert merged["ev_charging_kw"] == 7.4
    _bind_payload(monkeypatch, pinned, transport)
    _assert_vehicle_availability(pinned.current_state, False)
    _assert_vehicle_availability(ws.build_payload(), False)
    await _deliver(pinned, _sample(0), transport)
    assert pinned.current_state["car_charging_power"] == 0
    assert pinned.current_state["ev_power"] == 0
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    assert pinned.current_state["ev_charging_kw"] == 7.4
    _assert_vehicle_availability(pinned.current_state, True)
    _assert_vehicle_availability(ws.build_payload(), True)


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_ev5_pin_zero_ignores_other_instance_until_selected_disconnect(
    pinned, monkeypatch, transport
):
    # IGW snapshots are complete replacements, so instance 0 stays in each one.
    base = {
        "ev": {f"{VEHICLE}/Ac/Power": 1500, "7/Ac/Power": 3200},
        "evcharger": {f"{CHARGER}/Ac/Power": CHARGER_WATTS},
    }
    await _deliver(pinned, base, transport)
    assert pinned.current_state["car_charging_power"] == 1500
    updated = {
        "ev": {f"{VEHICLE}/Ac/Power": 1500, "7/Ac/Power": 3300},
        "evcharger": {f"{CHARGER}/Ac/Power": CHARGER_WATTS},
    }
    cleared = {
        "ev": {f"{VEHICLE}/Ac/Power": 1500, "7/Ac/Power": None},
        "evcharger": {f"{CHARGER}/Ac/Power": CHARGER_WATTS},
    }
    disconnected = {
        "ev": {
            f"{VEHICLE}/Ac/Power": 1500,
            f"{VEHICLE}/Connected": 0,
            "7/Ac/Power": 3200,
        },
        "evcharger": {f"{CHARGER}/Ac/Power": CHARGER_WATTS},
    }
    await _deliver(pinned, updated, transport)
    await _deliver(pinned, cleared, transport)
    assert pinned.current_state["car_charging_power"] == 1500
    assert pinned.current_state["ev_power"] == 1500
    _assert_vehicle_availability(pinned.current_state, True)
    _bind_payload(monkeypatch, pinned, transport)
    _assert_vehicle_availability(ws.build_payload(), True)
    await _deliver(pinned, disconnected, transport)
    assert pinned.current_state["car_charging_power"] is None
    assert pinned.current_state["ev_power"] is None
    assert pinned.current_state["car_charging_power"] != 3200
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    _assert_vehicle_availability(pinned.current_state, False)
    _assert_vehicle_availability(ws.build_payload(), False)


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_ev6_removal_clears_vehicle_aliases_without_erasing_charger(
    pinned, monkeypatch, transport
):
    await _deliver(pinned, _sample(1500), transport)
    if transport == "mqtt":
        await pinned.on_message(f"N/{PORTAL}/ev/{VEHICLE}/Ac/Power", b"")
    else:
        gateway.apply_snapshot(pinned, {"evcharger": {f"{CHARGER}/Ac/Power": CHARGER_WATTS}})
    assert pinned.current_state["car_charging_power"] is None
    assert pinned.current_state["ev_power"] is None
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    _bind_payload(monkeypatch, pinned, transport)
    payload = ws.build_payload()
    assert payload["car_charging_power"] is None
    assert payload["ev_power"] is None
    _assert_vehicle_availability(pinned.current_state, False)
    _assert_vehicle_availability(payload, False)
    await _deliver(pinned, _sample(0), transport)
    assert pinned.current_state["car_charging_power"] == 0
    assert pinned.current_state["ev_power"] == 0
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    assert pinned.current_state["ev_charging_kw"] == 7.4
    _assert_vehicle_availability(pinned.current_state, True)
    _assert_vehicle_availability(ws.build_payload(), True)


def test_ev6_transport_loss_clears_claimed_vehicle_power(pinned, monkeypatch):
    """Reducer clearing only. This does not validate a transport callback."""
    gateway.apply_snapshot(pinned, _sample(1500))
    pinned.clear_cerbo_state()
    assert pinned.current_state["car_charging_power"] is None
    assert pinned.current_state["ev_power"] is None
    _assert_vehicle_availability(pinned.current_state, False)
    _bind_payload(monkeypatch, pinned, "igw")
    payload = ws.build_payload()
    assert payload["car_charging_power"] is None
    assert payload["ev_power"] is None
    _assert_vehicle_availability(payload, False)
    gateway.apply_snapshot(pinned, _sample(0))
    assert pinned.current_state["car_charging_power"] == 0
    assert pinned.current_state["ev_power"] == 0
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    assert pinned.current_state["ev_charging_kw"] == 7.4
    _assert_vehicle_availability(pinned.current_state, True)
    _assert_vehicle_availability(ws.build_payload(), True)


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
@pytest.mark.parametrize("connected", [True, False])
async def test_ev7_daemon_and_ha_cannot_own_car_reading(pinned, monkeypatch, transport, connected):
    pinned._merge_daemon_state(
        {"car_charging_power": 8000, "ev_power": 9000, "booleans": {"no_feed": False}}
    )
    assert pinned.current_state.get("car_charging_power") is None
    assert pinned.current_state.get("ev_power") is None
    assert pinned.current_state["booleans"]["no_feed"] is False
    await _deliver(pinned, _sample(1500), transport)
    _assert_vehicle_availability(pinned.current_state, True)
    pinned._merge_daemon_state({"car_charging_power": 8000, "ev_power": 9000})
    await _deliver(pinned, _sample(None), transport)
    pinned._merge_daemon_state({"car_charging_power": 8000, "ev_power": 9000})
    assert pinned.current_state["car_charging_power"] is None
    assert pinned.current_state["ev_power"] is None
    monkeypatch.setattr(ha_client, "_configured", True)
    monkeypatch.setattr(ha_client, "_direct", True)
    monkeypatch.setattr(ha_client, "_sensor_entities", {"car_charging_power": "sensor.car_power"})
    monkeypatch.setattr(
        ha_client,
        "_appliance_entities",
        {"washer_time": "sensor.washer_remaining"},
    )
    monkeypatch.setattr(
        ha_client,
        "_overlay",
        {
            "ha_direct_connected": connected,
            "car_charging_power": 8000,
            "washer_time": 5400,
        },
    )
    assert ha_client._ha_owns_field("car_charging_power", "sensor.car_power") is False
    assert ha_client._ha_owns_field("washer_time", "sensor.washer_remaining") is True
    merged = ha_client.merge_overlay(pinned.get_state())
    assert merged["car_charging_power"] is None
    assert merged["ev_power"] is None
    assert merged["washer_time"] == (5400 if connected else 0)
    _bind_payload(monkeypatch, pinned, transport)
    payload = ws.build_payload()
    assert payload["car_charging_power"] is None
    assert payload["ev_power"] is None
    assert payload["ev_charging_power"] == CHARGER_WATTS
    assert payload["washer_time"] == (5400 if connected else 0)
    _assert_vehicle_availability(pinned.current_state, False)
    _assert_vehicle_availability(payload, False)


@pytest.mark.parametrize(
    "payload",
    [b"not json", b'{"x": 1}', b'{"value": "1500"}', b'{"value": true}', b'{"value": NaN}'],
)
async def test_ev8_malformed_mqtt_does_not_invent_or_clear(pinned, monkeypatch, payload):
    await pinned.on_message(f"N/{PORTAL}/ev/{VEHICLE}/Ac/Power", payload)
    assert "car_charging_power" not in pinned.current_state
    await pinned.on_message(f"N/{PORTAL}/ev/{VEHICLE}/Ac/Power", _envelope(1500))
    await pinned.on_message(f"N/{PORTAL}/evcharger/{CHARGER}/Ac/Power", _envelope(CHARGER_WATTS))
    await pinned.on_message(f"N/{PORTAL}/ev/{VEHICLE}/Ac/Power", payload)
    assert pinned.current_state["car_charging_power"] == 1500
    assert pinned.current_state["ev_power"] == 1500
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    _assert_vehicle_availability(pinned.current_state, True)
    await pinned.on_message(f"N/{PORTAL}/ev/{VEHICLE}/Ac/Power", _envelope(None))
    assert pinned.current_state["car_charging_power"] is None
    assert pinned.current_state["ev_power"] is None
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    _assert_vehicle_availability(pinned.current_state, False)
    _bind_payload(monkeypatch, pinned, "mqtt")
    _assert_vehicle_availability(ws.build_payload(), False)


def test_ev8_igw_rejected_leaf_is_replacement_not_mqtt_hold(pinned, monkeypatch):
    gateway.apply_snapshot(pinned, _sample(1500))
    gateway.apply_snapshot(
        pinned,
        {
            "ev": {f"{VEHICLE}/Ac/Power": "not-a-number", f"{VEHICLE}/CustomName": "Vehicle"},
            "evcharger": {f"{CHARGER}/Ac/Power": CHARGER_WATTS},
        },
    )
    assert pinned.current_state["car_charging_power"] is None
    assert pinned.current_state["ev_power"] is None
    assert pinned.current_state["ev_charging_power"] == CHARGER_WATTS
    _assert_vehicle_availability(pinned.current_state, False)
    _bind_payload(monkeypatch, pinned, "igw")
    _assert_vehicle_availability(ws.build_payload(), False)


@pytest.mark.parametrize("source", ["mqtt", "igw"])
def test_pinned_gateway_selection_restores_dual_path(source):
    previous_source = gateway.active_source()
    previous_dual = gateway._dual_path
    try:
        gateway.set_active_source(source, dual_path=True)
        try:
            with _preserve_gateway_selection():
                assert gateway.active_source() == "none"
                assert gateway.dual_path_enabled() is False
                raise RuntimeError("selection setup failed")
        except RuntimeError as exc:
            assert str(exc) == "selection setup failed"
        assert gateway.active_source() == source
        assert gateway.dual_path_enabled() is True
    finally:
        gateway._active_source = previous_source
        gateway._dual_path = previous_dual
