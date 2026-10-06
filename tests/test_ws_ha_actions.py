"""Exercise WebSocket actions through the real HA REST request builder."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from src.inverter_dashboard import ha_client, websocket_handler


@pytest.fixture
def ha_actions(monkeypatch):
    monkeypatch.setattr(ha_client, "_configured", True)
    monkeypatch.setattr(ha_client, "_direct", True)
    monkeypatch.setattr(ha_client, "_url", "http://ha.test")
    monkeypatch.setattr(ha_client, "_token", "test-token")
    monkeypatch.setattr(
        ha_client,
        "_filtered_entities",
        {
            "numbers": ["number.limit", "input_number.helper"],
            "covers": ["cover.blind"],
            "media_players": ["media_player.radio"],
            "scenes": ["scene.evening"],
        },
    )
    monkeypatch.setattr(ha_client, "_boolean_entities", {})
    monkeypatch.setattr(ha_client, "_switch_entities", {"laundry_start": "button.washer_start"})
    monkeypatch.setattr(
        ha_client, "fetch_states_once", AsyncMock(return_value={"ha_direct_connected": True})
    )
    monkeypatch.setattr(websocket_handler, "broadcast_state", AsyncMock())
    mqtt_publish = AsyncMock()
    monkeypatch.setattr(websocket_handler, "mqtt_publish", mqtt_publish)
    monkeypatch.setattr(ha_client, "_overlay", {})
    monkeypatch.setattr(ha_client, "_overlay_observed_at", None)
    monkeypatch.setattr(ha_client, "_overlay_observed_monotonic", None)
    ha_client.replace_overlay(
        {
            "ha_direct_connected": True,
            "ha_filtered": {
                "numbers": [
                    {"entity_id": entity, "min": 0, "max": 100}
                    for entity in ("number.limit", "input_number.helper")
                ],
                "covers": [{"entity_id": "cover.blind", "position": 50}],
                "media_players": [{"entity_id": "media_player.radio", "state": "playing"}],
                "scenes": [{"entity_id": "scene.evening"}],
            },
        }
    )
    return mqtt_publish


@pytest.mark.parametrize(
    "action,entity,params,path",
    [
        ("number_set", "number.limit", {"value": 12.5}, "number/set_value"),
        ("number_set", "input_number.helper", {"value": 0}, "input_number/set_value"),
        ("set_cover_position", "cover.blind", {"position": 0}, "cover/set_cover_position"),
        ("media_player", "media_player.radio", {"mp_action": "play"}, "media_player/media_play"),
        ("media_player", "media_player.radio", {"mp_action": "pause"}, "media_player/media_pause"),
        ("media_player", "media_player.radio", {"mp_action": "stop"}, "media_player/media_stop"),
        ("scene_activate", "scene.evening", {}, "scene/turn_on"),
        ("press", "button.washer_start", {}, "button/press"),
        ("toggle", "button.washer_start", {}, "button/press"),
    ],
)
async def test_dispatch_posts_configured_ha_action(
    ha_actions, monkeypatch, *, action, entity, params, path
):
    requests = []

    def request_handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(request_handler)) as client:
        monkeypatch.setattr(ha_client, "_http_client", client)
        await websocket_handler._dispatch_action(action, {"entity": entity, **params}, None)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"http://ha.test/api/services/{path}"
    assert request.headers["Authorization"] == "Bearer test-token"
    expected = {
        "entity_id": entity,
        **{key: value for key, value in params.items() if key != "mp_action"},
    }
    assert json.loads(request.content) == expected
    ha_actions.assert_not_called()


@pytest.mark.parametrize(
    "action,entity,params",
    [
        ("number_set", "number.unconfigured", {"value": 1}),
        ("number_set", "number.limit", {"value": "1"}),
        ("number_set", "number.limit", {"value": True}),
        ("number_set", "number.limit", {"value": float("nan")}),
        ("number_set", "number.limit", {"value": float("inf")}),
        ("number_set", "number.limit", {}),
        ("set_cover_position", "cover.blind", {"position": -1}),
        ("set_cover_position", "cover.blind", {"position": 101}),
        ("set_cover_position", "cover.blind", {"position": 0.5}),
        ("set_cover_position", "cover.blind", {"position": True}),
        ("media_player", "media_player.radio", {"mp_action": "delete"}),
        ("media_player", "media_player.radio", {"mp_action": []}),
        ("scene_activate", "scene.unconfigured", {}),
        ("number_set", "input_boolean.only_charging", {"value": 1}),
        ("press", "button.unconfigured", {}),
    ],
)
async def test_rejected_controls_never_publish(ha_actions, monkeypatch, action, entity, params):
    request = AsyncMock()
    monkeypatch.setattr(ha_client, "_ha_request", request)
    with pytest.raises(ValueError):
        await websocket_handler._dispatch_action(action, {"entity": entity, **params}, None)
    request.assert_not_called()
    ha_actions.assert_not_called()


async def test_disabled_direct_ha_rejects_rich_controls(ha_actions, monkeypatch):
    monkeypatch.setattr(ha_client, "_direct", False)
    request = AsyncMock()
    monkeypatch.setattr(ha_client, "_ha_request", request)
    with pytest.raises(ValueError):
        await websocket_handler._dispatch_action(
            "scene_activate", {"entity": "scene.evening"}, None
        )
    request.assert_not_called()
    ha_actions.assert_not_called()


async def test_failed_direct_action_does_not_fall_back_to_daemon(ha_actions, monkeypatch):
    monkeypatch.setattr(ha_client, "_ha_request", AsyncMock(return_value=httpx.Response(503)))
    with pytest.raises(RuntimeError):
        await websocket_handler._dispatch_action("press", {"entity": "button.washer_start"}, None)
    ha_actions.assert_not_called()


@pytest.mark.parametrize("case", ["offline", "stale", "unobserved", "missing_entity"])
async def test_rich_actions_fail_closed_on_stale_or_missing_ha_state(ha_actions, monkeypatch, case):
    import time

    if case == "offline":
        ha_client.replace_overlay({"ha_direct_connected": False})
    elif case == "stale":
        monkeypatch.setattr(ha_client, "_overlay_observed_monotonic", time.monotonic() - 31)
    elif case == "unobserved":
        monkeypatch.setattr(ha_client, "_overlay_observed_monotonic", None)
    else:
        ha_client.replace_overlay({"ha_direct_connected": True, "ha_filtered": {}})
    request = AsyncMock()
    monkeypatch.setattr(ha_client, "_ha_request", request)
    with pytest.raises(ValueError):
        await websocket_handler._dispatch_action(
            "scene_activate", {"entity": "scene.evening"}, None
        )
    request.assert_not_called()


async def test_number_range_is_revalidated_against_current_metadata(ha_actions, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(ha_client, "_ha_request", request)
    with pytest.raises(ValueError, match="range"):
        await websocket_handler._dispatch_action(
            "number_set", {"entity": "number.limit", "value": 101}, None
        )
    request.assert_not_called()
