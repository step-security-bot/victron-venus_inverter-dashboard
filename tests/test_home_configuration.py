"""Home inventory and states come only from operator-selected HA entities."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from inverter_dashboard import ha_client, websocket_handler
from inverter_dashboard.server import MqttState


@pytest.fixture
def configured_home(monkeypatch):
    entries = {
        "home_last": {"entity": "switch.last", "label": "Last", "order": 2},
        "home_light": {"entity": "light.example", "name": "Laundry guard", "order": 1},
        "home_peer": {"entity": "switch.peer", "label": "Peer", "order": 1},
        "home_disabled": {"entity": "switch.disabled", "enabled": False},
    }
    entities, labels = ha_client._parse_ha_switch_entities(entries)
    for key, value in {
        "_configured": True,
        "_direct": True,
        "_switch_entities": entities,
        "_switch_labels": labels,
        "_boolean_entities": {},
        "_sensor_entities": {},
        "_appliance_entities": {},
        "_filtered_entities": {},
    }.items():
        monkeypatch.setattr(ha_client, key, value)
    return entities


async def test_configured_inventory_order_labels_and_disabled_polling(configured_home, monkeypatch):
    rows = ha_client.ui_config_patch()["home_buttons"]
    assert [row["state_key"] for row in rows] == ["home_light", "home_peer", "home_last"]
    assert rows[0]["label"] == "Laundry guard"
    assert not ha_client.is_toggle_allowed("switch.disabled")
    requested = []

    async def get_state(client, headers, entity):
        requested.append(entity)
        return "on"

    monkeypatch.setattr(ha_client, "_get_state", get_state)
    await ha_client.fetch_states_once()
    assert requested == list(configured_home.values())


@pytest.mark.parametrize(
    "state,expected",
    [("on", True), ("off", False), ("unknown", None), ("unavailable", None), (None, None)],
)
async def test_home_state_survives_payload_schema(configured_home, monkeypatch, state, expected):
    async def get_state(client, headers, entity):
        return state

    monkeypatch.setattr(ha_client, "_get_state", get_state)
    monkeypatch.setattr(ha_client, "_overlay", await ha_client.fetch_states_once())
    mqtt = MqttState()
    mqtt.current_state = {"booleans": {"home_light": True, "only_charging": True}}
    monkeypatch.setitem(websocket_handler._state, "mqtt_state", mqtt)
    payload = websocket_handler.build_payload()
    assert payload["booleans"]["home_light"] is expected
    assert payload["booleans"]["only_charging"] is True


@pytest.mark.parametrize("direct", [False, True])
def test_missing_home_connection_clears_stale_mqtt_state(configured_home, monkeypatch, direct):
    monkeypatch.setattr(ha_client, "_direct", direct)
    monkeypatch.setattr(ha_client, "_overlay", {"ha_direct_connected": False})
    base = {"booleans": {"home_light": True, "only_charging": True}}
    original = deepcopy(base)
    merged = ha_client.merge_overlay(base)
    assert merged["booleans"]["home_light"] is None
    assert merged["booleans"]["only_charging"] is True
    assert base == original


def test_empty_home_list_clears_upstream_buttons(monkeypatch):
    monkeypatch.setattr(ha_client, "_switch_entities", {})
    payload = websocket_handler._with_ui_config(
        {"ui_config": {"home_buttons": [{"entity": "switch.legacy"}]}}
    )
    assert payload["ui_config"]["home_buttons"] == []


def test_home_cannot_display_or_override_controller_fields(configured_home, monkeypatch):
    monkeypatch.setattr(
        ha_client,
        "_switch_entities",
        {
            "ess_mode_observed_at": "switch.bad_timestamp",
            "ess_mode_controls_available": "switch.bad_authority",
            "only_charging": "input_boolean.only_charging",
        },
    )
    assert ha_client.home_buttons_ui() == []
    assert not ha_client.is_toggle_allowed("switch.bad_authority")


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "action,entity",
    [
        ("toggle", "switch.disabled"),
        ("toggle", "light.removed"),
        ("toggle", "button.removed"),
        ("press", "button.removed"),
    ],
)
async def test_unconfigured_home_commands_never_fall_back_to_mqtt(
    configured_home, monkeypatch, direct, action, entity
):
    monkeypatch.setattr(ha_client, "_direct", direct)
    publish = AsyncMock()
    request = AsyncMock()
    monkeypatch.setattr(websocket_handler, "mqtt_publish", publish)
    monkeypatch.setattr(ha_client, "_ha_request", request)
    with pytest.raises(ValueError):
        await websocket_handler._dispatch_action(action, {"entity": entity}, None)
    publish.assert_not_called()
    request.assert_not_called()


async def test_home_authority_gate_preserves_native_flag_alias(configured_home, monkeypatch):
    monkeypatch.setattr(ha_client, "_direct", False)
    publish = AsyncMock()
    monkeypatch.setattr(websocket_handler, "mqtt_publish", publish)
    await websocket_handler._dispatch_action(
        "toggle", {"entity": "input_boolean.only_charging", "state": "on"}, None
    )
    publish.assert_awaited_once_with(None, "toggle", {"entity": "only_charging", "state": "on"})
