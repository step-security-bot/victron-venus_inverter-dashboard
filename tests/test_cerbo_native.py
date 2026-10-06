"""Native-only, mixed-source and lifecycle regression scenarios."""

import asyncio
import json

import pytest

from inverter_dashboard import config, server
from inverter_dashboard.server import MqttState, _keepalive_loop, _subscribe_topics


def sample(ms, kind, path, value, instance="0"):
    return ms._handle_cerbo_device(
        f"N/site/{kind}/{instance}/{path}", json.dumps({"value": value}).encode()
    )


@pytest.fixture
def ms(monkeypatch):
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", "site")
    monkeypatch.setattr(config, "EV_INSTANCE", 22)
    monkeypatch.setattr(config, "EVCHARGER_INSTANCE", 40)
    return MqttState()


def test_native_battery_soc_uses_voltage_and_system_power_priority(ms):
    sample(ms, "battery", "ProductName", "48 V LiFePO4", "512")
    sample(ms, "battery", "Soc", 62, "512")
    sample(ms, "battery", "Dc/0/Voltage", 52, "512")
    sample(ms, "battery", "Dc/0/Power", -250, "512")
    assert ms.current_state.get("battery_soc") is None
    sample(ms, "system", "Dc/Battery/Voltage", 47.2)
    sample(ms, "system", "Dc/Battery/Soc", 77)
    sample(ms, "system", "Dc/Battery/Power", 0)
    sample(ms, "battery", "Soc", 63, "512")
    assert ms.current_state["battery_soc"] == 50
    assert ms.current_state["battery_power"] == ms.current_state["bp"] == 0
    sample(ms, "system", "Dc/Battery/Voltage", None)
    assert ms.current_state["battery_soc"] is None
    assert ms.current_state["telemetry_available"]["battery_soc"] is False


def test_metadata_does_not_claim_other_legacy_measurements(ms):
    ms._merge_daemon_state({"g1": 12, "battery_soc": 48, "mppt_total": 55, "car_soc": 10})
    sample(ms, "system", "Serial", "site")
    sample(ms, "battery", "ProductName", "Battery", "512")
    sample(ms, "solarcharger", "CustomName", "Roof", "290")
    sample(ms, "ev", "Ac/Power", 1200, str(config.EV_INSTANCE))
    ms._merge_daemon_state({"g1": 13, "battery_soc": 49, "mppt_total": 56, "car_soc": 11})
    assert ms.current_state["g1"] == 13
    assert ms.current_state.get("battery_soc") is None
    assert ms.current_state["mppt_total"] == 56
    assert ms.current_state.get("car_soc") is None


def test_null_before_first_native_number_invalidates_legacy(ms):
    ms._merge_daemon_state({"battery_soc": 99})
    sample(ms, "system", "Dc/Battery/Soc", None)
    ms._merge_daemon_state({"battery_soc": 98, "daily_stats": {"solar": 3}})
    assert ms.current_state["battery_soc"] is None
    assert ms.current_state["daily_stats"] == {"solar": 3}


@pytest.mark.parametrize(
    "payload",
    [
        b"garbage",
        b"[]",
        b"{}",
        b'{"value":true}',
        b'{"value":"45"}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e999}',
    ],
)
def test_bad_numbers_do_not_destroy_previous_sample(ms, payload):
    sample(ms, "system", "Ac/Grid/L1/Power", 123)
    assert ms._handle_cerbo_device("N/site/system/0/Ac/Grid/L1/Power", payload) is False
    assert ms.current_state["gt"] == 123


def test_three_phase_and_single_phase_totals_update_every_tick(ms):
    sample(ms, "system", "Ac/ActiveIn/Source", 1)
    sample(ms, "vebus", "Ac/ActiveIn/L1/P", 20, "276")
    sample(ms, "vebus", "Ac/Out/L1/P", 100, "276")
    assert ms.current_state["gt"] == 20
    assert ms.current_state.get("tt") is None
    sample(ms, "vebus", "Ac/ActiveIn/L1/P", 25, "276")
    sample(ms, "vebus", "Ac/Out/L1/P", 110, "276")
    assert ms.current_state["gt"] == 25
    assert ms.current_state.get("tt") is None
    for phase, grid, load in ((1, 5, 20), (2, 6, 30), (3, -3, 40)):
        sample(ms, "system", f"Ac/Grid/L{phase}/Power", grid)
        sample(ms, "system", f"Ac/Consumption/L{phase}/Power", load)
    sample(ms, "vebus", "Ac/ActiveIn/L1/P", 2000, "276")
    assert ms.current_state["g3"] == -3
    assert ms.current_state["t3"] == 40
    assert ms.current_state["gt"] == 8
    assert ms.current_state["tt"] == 90
    sample(ms, "system", "Ac/Grid/L1/Power", 0)
    assert ms.current_state["gt"] == 3


def test_grid_meter_fallback_and_disconnected_vebus(ms):
    sample(ms, "system", "Ac/ActiveIn/Source", 1)
    sample(ms, "vebus", "Ac/ActiveIn/L1/P", 900, "276")
    sample(ms, "grid", "Ac/L1/Power", 20, "30")
    assert ms.current_state["gt"] == 20
    sample(ms, "system", "Ac/Grid/L1/Power", 0)
    assert ms.current_state["gt"] == 0
    sample(ms, "system", "Ac/Grid/L1/Power", None)
    assert ms.current_state["gt"] is None  # current systemcalc invalidation is authoritative
    ms._handle_cerbo_device("N/site/grid/30", b"")
    assert ms.current_state["gt"] is None
    ms._handle_cerbo_device("N/site/system/0", b"")
    sample(ms, "system", "Ac/ActiveIn/Source", 1)
    assert ms.current_state["gt"] == 900
    sample(ms, "vebus", "Ac/ActiveIn/Connected", 0, "276")
    assert ms.current_state["gt"] is None
    assert ms.current_state["grid_available"] is False
    assert ms.current_state["telemetry_available"]["grid_available"] is True


def test_generator_input_is_not_counted_as_grid(ms):
    sample(ms, "system", "Ac/ActiveIn/Source", 2)
    sample(ms, "vebus", "Ac/ActiveIn/L1/P", 1200, "276")
    assert ms.current_state.get("gt") is None


def test_consumption_falls_back_to_input_plus_output(ms):
    sample(ms, "system", "Ac/ConsumptionOnInput/L1/Power", 70)
    sample(ms, "system", "Ac/ConsumptionOnOutput/L1/Power", 40)
    assert ms.current_state["tt"] == 110
    sample(ms, "system", "Ac/Consumption/L1/Power", 0)
    assert ms.current_state["tt"] == 0


def test_ac_pv_aggregate_wins_and_phases_are_summed(ms):
    for phase, watts in ((1, 100), (2, 200), (3, 300)):
        sample(ms, "pvinverter", f"Ac/L{phase}/Power", watts, "42")
    assert ms.current_state["pv_inverter_total"] == 600
    sample(ms, "pvinverter", "Ac/Power", 0, "42")
    sample(ms, "pvinverter", "Ac/L2/Power", 999, "42")
    assert ms.current_state["pv_inverter_total"] == 0
    sample(ms, "pvinverter", "Ac/Power", None, "42")
    assert ms.current_state["pv_inverter_total"] == 1399


def test_native_solar_aggregates_no_double_count_or_stale_legacy_component(ms):
    ms._merge_daemon_state({"mppt_total": 9000, "solar_total": 10000})
    sample(ms, "pvinverter", "Ac/Power", 100, "42")
    assert ms.current_state["solar_total"] == 100
    sample(ms, "solarcharger", "Dc/0/Voltage", 50, "290")
    sample(ms, "solarcharger", "Dc/0/Current", 10, "290")
    assert ms.current_state["solar_total"] == 600
    sample(ms, "solarcharger", "Yield/Power", 0, "290")
    assert ms.current_state["mppt_total"] == 0
    sample(ms, "system", "Dc/Pv/Power", 700)
    sample(ms, "system", "Ac/PvOnGrid/L1/Power", 150)
    sample(ms, "system", "Ac/PvOnOutput/L3/Power", 50)
    assert ms.current_state["mppt_total"] == 700
    assert ms.current_state["pv_inverter_total"] == 200
    assert ms.current_state["solar_total"] == 900


def test_acload_phases_and_duplicate_names_are_stable(ms):
    sample(ms, "acload", "CustomName", "Heater", "81")
    sample(ms, "acload", "ProductName", "Product", "81")
    sample(ms, "acload", "Ac/L1/Power", 10, "81")
    sample(ms, "acload", "Ac/L2/Power", 20, "81")
    sample(ms, "acload", "Ac/L3/Power", 30, "81")
    sample(ms, "acload", "CustomName", "Heater", "82")
    sample(ms, "acload", "Ac/Power", 0, "82")
    assert ms.current_state["loads"] == {"81": 60, "82": 0}
    sample(ms, "acload", "Ac/Power", 40, "81")
    sample(ms, "acload", "Ac/L1/Power", 500, "81")
    assert ms.current_state["loads"]["81"] == 40
    sample(ms, "acload", "CustomName", "", "81")
    assert ms.current_state["loads"]["81"] == 40
    assert ms.current_state["load_names"]["81"] == "Product"


def test_battery_diagnostics_and_custom_name_priority(ms):
    sample(ms, "battery", "CustomName", "House battery", "512")
    sample(ms, "battery", "ProductName", "Generic BMS", "512")
    sample(ms, "battery", "Dc/0/Temperature", 24, "512")
    sample(ms, "battery", "TimeToGo", 7260, "512")
    sample(ms, "battery", "System/MinCellVoltage", 3.1, "512")
    sample(ms, "battery", "System/MinVoltageCellId", "C4", "512")
    battery = ms.current_state["batteries"][0]
    assert battery["name"] == "House battery"
    assert battery["temperature"] == 24
    assert battery["time_to_go"] == "2h 01m"
    assert battery["min_cell_voltage"] == 3.1
    assert battery["min_voltage_cell_id"] == "C4"


def test_clear_on_disconnect_or_device_removal_never_revives_daemon(ms):
    ms._merge_daemon_state({"battery_power": 80, "version": "controller"})
    sample(ms, "battery", "Dc/0/Power", 90, "512")
    ms._handle_cerbo_device("N/site/battery/512", b"")
    assert ms.current_state["battery_power"] is None
    assert ms.current_state["batteries"] == []
    ms._merge_daemon_state({"battery_power": 100})
    assert ms.current_state["battery_power"] is None
    sample(ms, "battery", "Dc/0/Power", 110, "512")
    ms.clear_cerbo_state()
    assert ms.current_state["battery_power"] is None
    assert ms.current_state["version"] == "controller"
    sample(ms, "battery", "Dc/0/Power", 120, "512")
    assert ms.current_state["battery_power"] == 120


def test_water_percent_units_modes_and_independent_ev_fields(ms):
    sample(ms, "tank", "Level", 0.42, str(config.WATER_TANK_INSTANCE))
    sample(ms, "pump", "Mode", 0, str(config.WATER_PUMP_INSTANCE))
    sample(ms, "pump", "State", 0, str(config.WATER_PUMP_INSTANCE))
    sample(ms, "pump", "Mode", 2, str(config.WATER_VALVE_INSTANCE))
    sample(ms, "evcharger", "Ac/Power", 7200, str(config.EVCHARGER_INSTANCE))
    ms._merge_daemon_state({"water_valve": True, "car_soc": 35})
    assert ms.current_state["water_level"] == 0.42
    assert ms.current_state["pump_mode"] == 0
    assert ms.current_state["pump_switch"] is False
    assert ms.current_state["water_valve_mode"] == 2
    assert ms.current_state.get("water_valve") is None
    assert ms.current_state["ev_charging_kw"] == pytest.approx(7.2)
    assert ms.current_state.get("car_soc") is None


class Broker:
    """Record native subscriptions and publishes without a network connection."""

    def __init__(self):
        self.events = []

    async def subscribe(self, topic):
        self.events.append(("subscribe", topic))

    async def publish(self, topic, payload, qos=0):
        self.events.append(("publish", topic, payload))


@pytest.mark.asyncio
async def test_configured_portal_subscribes_before_bootstrap_and_scopes_every_filter(ms):
    broker = Broker()
    await _subscribe_topics(broker)
    assert broker.events[-1] == ("publish", "R/site/keepalive", "")
    native = [e[1] for e in broker.events if e[0] == "subscribe" and e[1].startswith("N/")]
    assert all(topic.startswith("N/site/") for topic in native)
    for kind in (
        "system",
        "grid",
        "battery",
        "solarcharger",
        "pvinverter",
        "acload",
        "tank",
        "pump",
        "ev",
        "evcharger",
        "vebus",
    ):
        assert f"N/site/{kind}/+/#" in native


@pytest.mark.asyncio
async def test_native_discovery_bootstraps_without_controller(monkeypatch):
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", "")
    ms = MqttState()
    broker = Broker()
    await _subscribe_topics(broker)
    assert not any(e[0] == "publish" for e in broker.events)
    assert ("subscribe", "N/+/system/+/Serial") in broker.events

    async def discovered(portal):
        await _subscribe_topics(broker, portal)

    ms.set_portal_callback(discovered)
    await ms.on_message("N/site/system/0/Serial", b'{"value":"site"}')
    assert ms._portal_id == "site"
    assert broker.events[-1] == ("publish", "R/site/keepalive", "")
    await ms.on_message("N/site/ev/22/Soc", b'{"value":72}')
    assert ms.current_state["car_soc"] == 72
    await ms.on_message("inverter/portal", b"foreign")
    await ms.on_message("N/foreign/system/0/Ac/Grid/L1/Power", b'{"value":999}')
    assert ms._portal_id == "site"
    assert ms.current_state.get("gt") is None


@pytest.mark.asyncio
async def test_configured_portal_filters_all_native_domains(ms):
    for topic in (
        "N/foreign/battery/512/Soc",
        "N/foreign/acload/81/Ac/Power",
        "N/foreign/pvinverter/20/Ac/Power",
        "N/foreign/battery/512/Alarms/HighVoltage",
    ):
        await ms.on_message(topic, b'{"value":2}')
    await ms.on_message("inverter/portal", b"foreign")
    assert ms.current_state == {}
    assert ms.notifications == []
    assert ms._portal_id == "site"


@pytest.mark.asyncio
async def test_regular_keepalive_suppresses_full_republish(monkeypatch):
    broker = Broker()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await _keepalive_loop(broker, lambda: "site")
    assert sleeps == [20, 20]
    assert 0 < server.KEEPALIVE_INTERVAL_SECS < 30
    assert broker.events == [
        ("publish", "R/site/keepalive", '{"keepalive-options":["suppress-republish"]}'),
        *[("publish", topic, "") for topic in server._water_read_topics("site")],
    ]
    assert all(event[1].startswith("R/site/") for event in broker.events)


@pytest.mark.asyncio
async def test_modern_and_legacy_alarm_names(ms):
    await ms.on_message("N/site/battery/512/Alarms/HighCellVoltage", b'{"value":2}')
    notification = ms.notifications[0]
    assert notification["title"] == "Battery 512"
    assert notification["body"] == "High Cell Voltage: Alarm"
    await ms.on_message("N/site/battery_513/Alarms/LowVoltage", b'{"value":1}')
    assert any(n["body"] == "Low Voltage: Warning" for n in ms.notifications)


@pytest.mark.parametrize("source", [None, 2, 240])
def test_unknown_or_generator_source_does_not_assume_grid(ms, source):
    if source is not None:
        sample(ms, "system", "Ac/ActiveIn/Source", source)
    sample(ms, "vebus", "Ac/ActiveIn/L1/P", 300, "276")
    sample(ms, "vebus", "Ac/Out/L1/P", 400, "276")
    assert ms.current_state.get("gt") is None
    assert ms.current_state.get("tt") is None


def test_selected_battery_reported_soc_cannot_override_voltage_formula(ms):
    sample(ms, "battery", "Soc", 80, "1")
    sample(ms, "battery", "CustomName", "SmartShunt", "1")
    sample(ms, "battery", "Soc", 20, "2")
    assert ms.current_state.get("battery_soc") is None
    sample(ms, "system", "Dc/Battery/Instance", 2)
    sample(ms, "system", "Dc/Battery/Voltage", 47.2)
    assert ms.current_state["battery_soc"] == 50
    sample(ms, "system", "Dc/Battery/Soc", 0)
    sample(ms, "battery", "Dc/0/Voltage", 51.2, "1")
    assert ms.current_state["battery_soc"] == 78


@pytest.mark.asyncio
async def test_failed_discovery_bootstrap_can_retry(monkeypatch):
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", "")
    ms = MqttState()
    attempts = []

    async def callback(portal):
        attempts.append(portal)
        if len(attempts) == 1:
            raise RuntimeError("transient subscribe failure")

    ms.set_portal_callback(callback)
    await ms.on_message("N/site/system/0/Serial", b'{"value":"site"}')
    assert ms._portal_id == ""
    await ms.on_message("N/site/system/0/Serial", b'{"value":"site"}')
    assert ms._portal_id == "site"
    assert attempts == ["site", "site"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b'{"value":null}', b'{"value":true}', b'{"value":NaN}'])
async def test_unknown_native_value_cannot_select_portal(monkeypatch, payload):
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", "")
    ms = MqttState()
    await ms.on_message("N/foreign/system/0/Serial", payload)
    assert ms._portal_id == ""


@pytest.mark.asyncio
async def test_empty_leaf_removes_entire_service_without_reviving_legacy(ms):
    ms._merge_daemon_state({"battery_soc": 80, "battery_voltage": 50})
    sample(ms, "battery", "CustomName", "House battery", "512")
    sample(ms, "battery", "Dc/0/Voltage", 52, "512")
    sample(ms, "battery", "Soc", 72, "512")
    assert ms.current_state["batteries"][0]["voltage"] == 52

    # A JSON null means only SoC is unknown; the battery still exists.
    await ms.on_message("N/site/battery/512/Soc", b'{"value":null}')
    assert ms.current_state["batteries"][0]["voltage"] == 52
    assert ms.current_state["battery_soc"] is None

    # Empty bytes on any leaf announce service removal, before the remaining
    # empty notifications arrive. All its old measurements disappear together.
    await ms.on_message("N/site/battery/512/Soc", b"")
    assert ms.current_state["batteries"] == []
    assert ms.current_state["battery_voltage"] is None
    assert ms.current_state["telemetry_available"]["battery_voltage"] is False
    assert ms._cerbo_devices.get("battery") == {}
    await ms.on_message("N/site/battery/512/Dc/0/Voltage", b"")
    ms._merge_daemon_state({"battery_voltage": 51, "battery_soc": 79})
    assert ms.current_state["batteries"] == []
    assert ms.current_state["battery_voltage"] is None
    assert ms.current_state["battery_soc"] is None


@pytest.mark.parametrize("portal", ["", "site/other", "+", "#", "a b", "site\x00"])
def test_water_read_topics_reject_invalid_portal(portal):
    assert server._water_read_topics(portal) == []


def test_water_read_topics_only_configured_deduplicated_targets(monkeypatch):
    monkeypatch.setattr(config, "WATER_PUMP_INSTANCE", 7)
    monkeypatch.setattr(config, "WATER_VALVE_INSTANCE", 7)
    assert server._water_read_topics("site") == ["R/site/pump/7/Mode"]
    monkeypatch.setattr(config, "WATER_VALVE_INSTANCE", -1)
    assert server._water_read_topics("site") == ["R/site/pump/7/Mode"]
    monkeypatch.setattr(config, "WATER_PUMP_INSTANCE", True)
    assert server._water_read_topics("site") == []


async def test_source_retirement_after_keepalive_prevents_water_reads(monkeypatch):
    portal = ["site"]
    broker = Broker()
    original = broker.publish

    async def publish(topic, payload, **kwargs):
        await original(topic, payload, **kwargs)
        portal[0] = ""

    broker.publish = publish
    count = [0]

    async def sleep(_):
        count[0] += 1
        if count[0] == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await _keepalive_loop(broker, lambda: portal[0])
    assert len(broker.events) == 1
    assert broker.events[0][1] == "R/site/keepalive"
