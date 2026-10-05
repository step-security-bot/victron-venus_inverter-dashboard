"""Real HTTPX request failures must invalidate the complete HA poll overlay."""

import asyncio
from copy import deepcopy

import httpx
import pytest

from src.inverter_dashboard import ha_client

DISCONNECTED = {"booleans": {}, "ha_direct_connected": False}


@pytest.fixture
def ha_transport(monkeypatch):
    """Use real AsyncClient/MockTransport, without network access or mocked readers."""
    monkeypatch.setattr(ha_client, "_configured", True)
    monkeypatch.setattr(ha_client, "_direct", True)
    monkeypatch.setattr(ha_client, "_url", "http://ha.invalid")
    monkeypatch.setattr(ha_client, "_token", "test-token")
    for name in (
        "_boolean_entities",
        "_switch_entities",
        "_appliance_entities",
        "_sensor_entities",
        "_filtered_entities",
    ):
        monkeypatch.setattr(ha_client, name, {})
    monkeypatch.setattr(ha_client, "_overlay", {})
    client_type = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            ha_client.httpx,
            "AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
        )

    return install


def configure_reader(monkeypatch, reader, entities):
    if reader == "state":
        monkeypatch.setattr(ha_client, "_switch_entities", {e.split(".")[1]: e for e in entities})
    else:
        monkeypatch.setattr(ha_client, "_filtered_entities", {"sensors": entities})


def state_response(entity):
    return httpx.Response(200, json={"entity_id": entity, "state": "on", "attributes": {}})


@pytest.mark.parametrize("reader", ["state", "full-state"])
@pytest.mark.parametrize("failure", ["connect", "timeout", 401, 403, 500, 503])
@pytest.mark.parametrize("after_partial", [False, True])
async def test_poll_failure_discards_partial_overlay_and_stops_requests(
    ha_transport, monkeypatch, reader, failure, after_partial
):
    entities = ["switch.home_first", "switch.home_failed", "switch.home_unvisited"]
    configure_reader(monkeypatch, reader, entities if after_partial else entities[1:])
    requested = []

    def handler(request):
        entity = request.url.path.rsplit("/", 1)[1]
        requested.append(entity)
        assert request.headers["Authorization"] == "Bearer test-token"
        if entity == "switch.home_failed":
            if failure == "connect":
                raise httpx.ConnectError("HA offline", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("HA timed out", request=request)
            return httpx.Response(failure)
        return state_response(entity)

    ha_transport(handler)
    assert await ha_client.fetch_states_once() == DISCONNECTED
    assert requested == (entities[:2] if after_partial else entities[1:2])


@pytest.mark.parametrize("reader", ["state", "full-state"])
async def test_missing_entity_keeps_poll_connected_and_continues(ha_transport, monkeypatch, reader):
    entities = ["switch.home_missing", "switch.home_light"]
    configure_reader(monkeypatch, reader, entities)
    requested = []

    def handler(request):
        entity = request.url.path.rsplit("/", 1)[1]
        requested.append(entity)
        return httpx.Response(404) if entity == entities[0] else state_response(entity)

    ha_transport(handler)
    overlay = await ha_client.fetch_states_once()
    assert overlay["ha_direct_connected"] is True
    assert requested == entities
    if reader == "state":
        assert overlay["home_missing"] is None
        assert overlay["home_light"] is True
    else:
        assert [doc["entity_id"] for doc in overlay["ha_filtered"]["sensors"]] == entities[1:]


@pytest.mark.parametrize("reader", ["state", "full-state"])
async def test_poll_recovers_and_preserves_cerbo_ownership(ha_transport, monkeypatch, reader):
    configure_reader(monkeypatch, reader, ["switch.home_light"])
    monkeypatch.setattr(
        ha_client, "_boolean_entities", {"only_charging": "input_boolean.only_charging"}
    )
    monkeypatch.setattr(ha_client, "_sensor_entities", {"battery_soc": "sensor.old_soc"})
    base = {"booleans": {"only_charging": True}, "battery_soc": 42.5}
    original = deepcopy(base)
    requested = []
    healthy = False

    def handler(request):
        entity = request.url.path.rsplit("/", 1)[1]
        requested.append(entity)
        return state_response(entity) if healthy else httpx.Response(503)

    ha_transport(handler)
    for healthy in (False, True):
        overlay = await ha_client.fetch_states_once()
        assert overlay["ha_direct_connected"] is healthy
        ha_client.replace_overlay(overlay)
        merged = ha_client.merge_overlay(base)
        assert merged["ha_direct_connected"] is healthy
        expected_booleans = dict(original["booleans"])
        if reader == "state":
            expected_booleans["home_light"] = True if healthy else None
        assert merged["booleans"] == expected_booleans
        assert merged["battery_soc"] == original["battery_soc"]
        assert base == original
        if healthy:
            if reader == "state":
                assert merged["home_light"] is True
            else:
                assert merged["ha_filtered"]["sensors"][0]["entity_id"] == "switch.home_light"
    assert requested == ["switch.home_light", "switch.home_light"]


@pytest.mark.parametrize("reader", ["state", "full-state"])
async def test_poll_cancellation_propagates_without_publishing_partial_state(
    ha_transport, monkeypatch, reader
):
    configure_reader(monkeypatch, reader, ["switch.home_first", "switch.home_waiting"])
    entered = asyncio.Event()
    released = asyncio.Event()
    previous = {"booleans": {}, "home_light": True, "ha_direct_connected": True}
    ha_client.replace_overlay(previous)

    async def handler(request):
        if request.url.path.endswith("home_first"):
            return state_response("switch.home_first")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    ha_transport(handler)
    task = asyncio.create_task(ha_client.fetch_states_once())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released.is_set()
        assert ha_client._overlay == previous
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
