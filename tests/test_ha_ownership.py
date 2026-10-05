"""HA reconnects and old mirror configuration cannot overwrite Cerbo state."""

from copy import deepcopy

import pytest

from src.inverter_dashboard import ha_client


@pytest.fixture
def configured_ha(monkeypatch):
    monkeypatch.setattr(ha_client, "_configured", True)
    monkeypatch.setattr(ha_client, "_direct", True)
    monkeypatch.setattr(
        ha_client,
        "_boolean_entities",
        {
            "only_charging": "input_boolean.only_charging",
            "legacy_alias": "input_boolean.no_feed",
            "holiday": "input_boolean.holiday",
        },
    )
    monkeypatch.setattr(
        ha_client,
        "_switch_entities",
        {
            "pump_switch": "switch.old_pump",
            "home_light": "light.kitchen",
        },
    )
    monkeypatch.setattr(
        ha_client,
        "_sensor_entities",
        {
            "battery_soc": "sensor.old_soc",
            "water_level": "sensor.old_water",
            "loads": "sensor.old_loads",
            "car_soc": "sensor.old_car_soc",
            "room_temperature": "sensor.room_temperature",
        },
    )
    monkeypatch.setattr(
        ha_client,
        "_appliance_entities",
        {
            "washer_time": "sensor.washer_remaining",
            "washer_power": "binary_sensor.washer_running",
        },
    )
    monkeypatch.setattr(ha_client, "_filtered_entities", {})


@pytest.mark.parametrize("connected", [True, False])
def test_cerbo_and_controller_survive_ha_overlay(configured_ha, monkeypatch, connected):
    base = {
        "booleans": {"only_charging": True, "no_feed": False, "holiday": True},
        "battery_soc": 0.0,
        "water_level": 0.5,
        "pump_switch": False,
        "car_soc": 0.0,
        "loads": {"22": 130.0},
        "daily_stats": {"produced_today": 5.2},
    }
    original = deepcopy(base)
    monkeypatch.setattr(
        ha_client,
        "_overlay",
        {
            "ha_direct_connected": connected,
            "booleans": {"only_charging": False, "no_feed": True, "holiday": False},
            "battery_soc": 100,
            "water_level": 99,
            "pump_switch": True,
            "car_soc": 99,
            "loads": {"old": 0},
            "home_light": True,
            "washer_time": 5400,
            "washer_power": True,
            "room_temperature": 21.5,
        },
    )

    merged = ha_client.merge_overlay(base)

    for key in ("battery_soc", "water_level", "pump_switch", "car_soc", "loads", "daily_stats"):
        assert merged[key] == original[key]
    assert merged["booleans"] == {
        "only_charging": True,
        "no_feed": False,
        "holiday": False,
        "home_light": True if connected else None,
    }
    assert "legacy_alias" not in merged["booleans"]
    assert merged["home_light"] is connected
    assert merged["washer_time"] == (5400 if connected else 0)
    assert merged["washer_power"] is connected
    assert base == original


async def test_poll_skips_old_cerbo_mirrors_but_keeps_appliances(configured_ha, monkeypatch):
    requested = []
    states = {
        "input_boolean.holiday": "on",
        "light.kitchen": "off",
        "sensor.washer_remaining": "01:30:00",
        "binary_sensor.washer_running": "on",
        "sensor.room_temperature": "21.5",
    }

    async def get_state(client, headers, entity):
        requested.append(entity)
        return states[entity]

    monkeypatch.setattr(ha_client, "_get_state", get_state)
    overlay = await ha_client.fetch_states_once()
    assert set(requested) == set(states)
    assert overlay["booleans"] == {"holiday": True}
    assert overlay["washer_time"] == 5400
    assert overlay["washer_power"] is True
    assert overlay["room_temperature"] == 21.5
    assert not ha_client.is_toggle_allowed("input_boolean.only_charging")
    assert not ha_client.is_toggle_allowed("input_boolean.no_feed")
    assert not ha_client.is_toggle_allowed("switch.old_pump")
    assert ha_client.is_toggle_allowed("light.kitchen")


def test_ha_outage_does_not_create_unknown_controller_flags(configured_ha, monkeypatch):
    monkeypatch.setattr(ha_client, "_overlay", {"ha_direct_connected": False})
    assert ha_client.merge_overlay({})["booleans"] == {"holiday": False, "home_light": None}
