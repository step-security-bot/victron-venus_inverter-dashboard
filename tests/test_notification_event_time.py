"""Source event times survive partial Venus republishes and MQTT/IGW reconnects."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from inverter_dashboard import gateway, server, websocket_handler

EVENT_SECONDS = 1_791_226_020
EVENT_TIME = "2026-10-05T18:47:00+00:00"
SLOT_ID = "victron-platform-0-1"


@pytest.fixture(params=["mqtt", "igw"])
def native_events(request, monkeypatch):
    monkeypatch.setattr(server.config, "CERBO_PORTAL_ID", "site")
    state = server.MqttState()
    leaves = {}

    async def publish(**fields):
        for field, value in fields.items():
            leaves[f"0/Notifications/1/{field}"] = {"value": value}
            if request.param == "mqtt":
                await state.on_message(
                    f"N/site/platform/0/Notifications/1/{field}",
                    json.dumps({"value": value}).encode(),
                )
        if request.param == "igw":
            gateway.apply_snapshot(state, {"platform": leaves})

    return state, publish


async def test_partial_old_replay_and_reconnect_keep_source_event_time(native_events, monkeypatch):
    state, publish = native_events
    monkeypatch.setitem(websocket_handler._state, "mqtt_state", state)
    await publish(Description="Internal failure", DeviceName="JBD Battery Chain 1")
    assert websocket_handler.build_payload()["notifications"][0]["ts"] == ""

    await publish(DateTime=EVENT_SECONDS)
    notification = websocket_handler.build_payload()["notifications"][0]
    assert notification["ts"] == EVENT_TIME
    received_at = datetime(2026, 10, 5, 20, 2, tzinfo=UTC)
    assert (received_at - datetime.fromisoformat(notification["ts"])).total_seconds() == 75 * 60

    # These are the same invalidation calls made when MQTT disconnects. The
    # subsequently republished slot is still the old source event.
    state.clear_daemon_state()
    state.clear_cerbo_state()
    await publish(Description="Internal failure", Active=True, Acknowledged=False)
    await publish(DateTime=EVENT_SECONDS)
    assert websocket_handler.build_payload()["notifications"][0]["ts"] == EVENT_TIME


async def test_late_datetime_is_broadcast_without_waiting_for_other_telemetry(monkeypatch):
    monkeypatch.setattr(server.config, "CERBO_PORTAL_ID", "site")
    state = server.MqttState()
    updates = []

    async def observe():
        updates.append([dict(n) for n in state.get_notifications()])

    state.set_state_callback(observe)
    base = "N/site/platform/0/Notifications/1"
    await state.on_message(f"{base}/Description", b'{"value":"Internal failure"}')
    assert updates[-1][0]["ts"] == ""
    await state.on_message(f"{base}/DateTime", json.dumps({"value": EVENT_SECONDS}).encode())
    assert len(updates) == 2
    assert updates[-1][0]["ts"] == EVENT_TIME
    await state.on_message(f"{base}/DateTime", json.dumps({"value": EVENT_SECONDS}).encode())
    assert len(updates) == 2  # Unchanged replays do not cause extra broadcasts.
    await state.on_message(f"{base}/DateTime", b'{"value":null}')
    assert updates[-1][0]["ts"] == ""
    assert len(updates) == 3


@pytest.mark.parametrize(
    "value",
    [
        None,
        "invalid",
        "",
        0,
        -1,
        True,
        False,
        1.5,
        "1.5",
        "1791226080.00000001",
        "1e999999999",
        "NaN",
        "Infinity",
        2**63 - 1,
        [],
        {},
    ],
)
async def test_invalid_datetime_is_unknown_and_does_not_keep_a_stale_time(native_events, value):
    state, publish = native_events
    await publish(Description="Internal failure", DateTime=EVENT_SECONDS)
    assert state.get_notifications()[0]["ts"] == EVENT_TIME
    await publish(DateTime=value)
    assert state.get_notifications()[0]["title"] == "Internal failure"
    assert state.get_notifications()[0]["ts"] == ""
    await publish(DateTime=EVENT_SECONDS)
    assert state.get_notifications()[0]["ts"] == EVENT_TIME


@pytest.mark.parametrize(
    "value",
    [EVENT_SECONDS, float(EVENT_SECONDS), str(EVENT_SECONDS), "1791226020.0", "1.79122602e9"],
)
async def test_integral_native_datetime_encodings_are_compatible(native_events, value):
    state, publish = native_events
    await publish(Description="Internal failure", DateTime=value)
    assert state.get_notifications()[0]["ts"] == EVENT_TIME


async def test_future_source_time_is_preserved(native_events):
    state, publish = native_events
    await publish(Description="Future clock", DateTime=4_102_444_800)
    assert state.get_notifications()[0]["ts"] == "2100-01-01T00:00:00+00:00"


async def test_incomplete_replay_does_not_resurrect_a_dismissed_event(native_events):
    state, publish = native_events
    await publish(Description="Internal failure", DateTime=EVENT_SECONDS)
    state.dismiss_notification(SLOT_ID)
    for value in (0, None, "invalid", "1791226080.00000001", EVENT_SECONDS):
        await publish(DateTime=value, DeviceName="JBD Battery Chain 1")
        assert state.get_notifications() == []
    # Only a different valid event time makes this a fresh slot event.
    await publish(DateTime=EVENT_SECONDS + 60)
    assert state.get_notifications()[0]["ts"] == "2026-10-05T18:48:00+00:00"
    await publish(Acknowledged=True)
    await publish(DateTime=EVENT_SECONDS + 120)
    assert state.get_notifications() == []


@pytest.mark.parametrize("timestamp", [None, "", "not-a-date", "2026-10-05T11:47:00-07:00"])
async def test_remote_notification_does_not_invent_or_rewrite_source_time(timestamp):
    state = server.MqttState()
    notification = {"id": "external", "title": "Alarm"}
    if timestamp is not None:
        notification["ts"] = timestamp
    for _ in range(2):  # Retained notification received again after reconnect.
        await state.on_message("inverter/notifications", json.dumps(notification).encode())
        assert state.get_notifications()[0]["ts"] == (timestamp or "")


async def test_raw_alarm_replays_have_unknown_event_time(monkeypatch):
    monkeypatch.setattr(server.config, "CERBO_PORTAL_ID", "site")
    for _ in range(2):
        # A new transport state cannot know when an already-active alarm began.
        mqtt = server.MqttState()
        await mqtt.on_message("N/site/battery/512/Alarms/HighVoltage", b'{"value":2}')
        assert mqtt.get_notifications()[0]["ts"] == ""
        igw = server.MqttState()
        gateway.apply_snapshot(igw, {"battery": {"512/Alarms/HighVoltage": 2}})
        assert igw.get_notifications()[0]["ts"] == ""


async def test_foreign_platform_time_cannot_modify_selected_portal_event(native_events):
    state, publish = native_events
    await publish(Description="Internal failure", DateTime=EVENT_SECONDS)
    await state.on_message("N/foreign/platform/0/Notifications/1/DateTime", b'{"value":1}')
    assert state.get_notifications()[0]["ts"] == EVENT_TIME


@pytest.mark.parametrize("transport", ["mqtt", "igw-flat", "igw-wrapped"])
@pytest.mark.parametrize("token", ["1791226080.00000001", "1.79122608000000001e9"])
async def test_fractional_json_wire_times_stay_unknown_and_cannot_undo_dismissal(
    monkeypatch, transport, token
):
    """JSON number decoding must not round a fractional event into a fresh integer."""
    monkeypatch.setattr(server.config, "CERBO_PORTAL_ID", "site")
    monkeypatch.setattr(gateway.config, "GATEWAY_URL", "https://gateway.example")
    monkeypatch.setattr(gateway, "build_headers", dict)
    state = server.MqttState()

    async def publish(raw_number):
        if transport == "mqtt":
            await state.on_message(
                "N/site/platform/0/Notifications/1/Description", b'{"value":"Internal failure"}'
            )
            await state.on_message(
                "N/site/platform/0/Notifications/1/DateTime",
                ('{"value":' + raw_number + "}").encode(),
            )
        else:
            raw_value = '{"value":' + raw_number + "}" if transport == "igw-wrapped" else raw_number
            response = (
                '{"platform":{"0/Notifications/1/Description":"Internal failure",'
                '"0/Notifications/1/DateTime":' + raw_value + "},"
                '"system":{"0/Dc/Battery/Voltage":51.25}}'
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, content=response))
            ) as client:
                snapshot = await gateway.fetch_snapshot(client)
            # Precision handling for DateTime must leave telemetry as JSON numbers.
            assert type(snapshot["system"]["0/Dc/Battery/Voltage"]) is float
            json.dumps(snapshot)
            gateway.apply_snapshot(state, snapshot)

    await publish(str(EVENT_SECONDS))
    assert state.get_notifications()[0]["ts"] == EVENT_TIME
    await publish(token)
    assert state.get_notifications()[0]["ts"] == ""
    await publish("1791226020.0")
    assert state.get_notifications()[0]["ts"] == EVENT_TIME
    state.dismiss_notification(SLOT_ID)
    await publish(token)
    assert state.get_notifications() == []
    await publish("1.79122602e9")
    assert state.get_notifications() == []
