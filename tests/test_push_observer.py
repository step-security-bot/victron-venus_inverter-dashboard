"""Process actual source messages without a browser and never infer approximate events."""

import json

import pytest

from inverter_dashboard import config, gateway, push_observer, server
from inverter_dashboard.push_events import DEFAULT_PREFERENCES
from inverter_dashboard.push_observer import PushObserver, selected_samples
from inverter_dashboard.push_service import PushService
from tests.test_push_transport import receiver

NOW = 1_800_000_000.0


@pytest.fixture
def observed(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(push_observer.time, "time", lambda: clock[0])
    monkeypatch.setattr(config, "WATER_PUMP_INSTANCE", 1)
    monkeypatch.setattr(config, "WATER_VALVE_INSTANCE", 2)
    monkeypatch.setattr(config, "EVCHARGER_INSTANCE", 40)
    service = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    service.register(receiver()[2], dict(DEFAULT_PREFERENCES))
    ms = server.MqttState()
    ms._portal_id = "site"
    observer = PushObserver(service)
    yield service, observer, ms, clock
    service.store.close()


def take(service, now):
    result = []
    while item := service.store.next_delivery(now):
        result.append(item["payload"])
        service.store.finish(item)
    return result


async def feed(observed, path, value, *, retained=False, empty=False):
    _, observer, ms, _ = observed
    topic = f"N/site/{path}"
    await ms.on_message(
        topic, b"" if empty else json.dumps({"value": value}).encode(), retained=retained
    )
    observer.mqtt(
        ms,
        topic,
        retained=retained,
        payload=b"" if empty else json.dumps({"value": value}).encode(),
    )


async def test_mqtt_retained_unknown_and_removed_service_require_new_baselines(observed):
    service, _, ms, clock = observed
    service.connect("mqtt", ms)
    clock[0] += 11
    await feed(observed, "pump/1/State", 0, retained=True)
    await feed(observed, "pump/1/State", 1)
    assert take(service, clock[0]) == []
    clock[0] += 1
    await feed(observed, "pump/1/State", 0)
    assert [event["title"] for event in take(service, clock[0])] == ["Water pump off"]
    await feed(observed, "pump/1", None, empty=True)
    clock[0] += 1
    await feed(observed, "pump/1/State", 1)
    assert take(service, clock[0]) == []
    await feed(observed, "pump/1/State", None)
    await feed(observed, "pump/1/State", 0)
    assert take(service, clock[0]) == []
    await feed(observed, "pump/1/Connected", 0)
    await feed(observed, "pump/1/State", 1)
    await feed(observed, "pump/1/Connected", 1)
    assert take(service, clock[0]) == []
    await feed(observed, "pump/1/State", 1)
    assert take(service, clock[0]) == []


async def test_fresh_source_samples_notify_without_any_websocket_client(observed):
    service, _, ms, clock = observed
    service.connect("mqtt", ms)
    clock[0] += 11
    for path, value in (
        ("evcharger/40/Ac/Power", 0),
        ("pump/2/State", 0),
        ("system/0/Dc/Battery/Soc", 21),
    ):
        await feed(observed, path, value)
    assert take(service, clock[0]) == []
    clock[0] += 1
    for path, value in (
        ("evcharger/40/Ac/Power", 20),
        ("pump/2/State", 1),
        ("system/0/Dc/Battery/Soc", 20),
    ):
        await feed(observed, path, value)
    events = take(service, clock[0])
    assert {item["kind"] for item in events} == {"ev", "water", "lowBattery"}
    assert all(item["sourceTimestampMs"] == int(clock[0] * 1000) for item in events)
    service.disconnect()
    await feed(observed, "pump/2/State", 0)
    assert take(service, clock[0]) == []


def test_igw_first_complete_snapshot_and_reconnect_are_silent(observed):
    service, observer, ms, clock = observed
    initial = {"pump": {"1/State": 0}, "evcharger": {"40/Ac/Power": 0}}
    gateway.apply_snapshot(ms, initial)
    observer.snapshot(ms)
    assert take(service, clock[0]) == []
    clock[0] += 2
    gateway.apply_snapshot(ms, {"pump": {"1/State": 1}, "evcharger": {"40/Ac/Power": 7000}})
    observer.snapshot(ms)
    assert {item["kind"] for item in take(service, clock[0])} == {"water", "ev"}
    service.disconnect("igw", ms)
    gateway.apply_snapshot(ms, initial)
    observer.snapshot(ms)
    assert take(service, clock[0]) == []
    clock[0] += 31
    gateway.apply_snapshot(ms, {"pump": {"1/State": 1}})
    observer.snapshot(ms)
    assert take(service, clock[0]) == []


def test_true_soc_uses_explicit_battery_selection_and_never_voltage_estimate(observed):
    _, _, ms, _ = observed
    gateway.apply_snapshot(
        ms,
        {
            "battery": {"1/Dc/0/Voltage": 48},
            "inverter": {"battery_soc": 10, "grid_available": False},
        },
    )
    assert selected_samples(ms)["lowBattery"].value is None
    gateway.apply_snapshot(ms, {"battery": {"1/Soc": 19}})
    assert selected_samples(ms)["lowBattery"].identifier == "battery:1"
    gateway.apply_snapshot(ms, {"battery": {"1/Soc": 19, "2/Soc": 80}})
    assert "lowBattery" not in selected_samples(ms)
    gateway.apply_snapshot(
        ms, {"system": {"0/Dc/Battery/Instance": 2}, "battery": {"1/Soc": 19, "2/Soc": 80}}
    )
    assert selected_samples(ms)["lowBattery"].value == 80
    gateway.apply_snapshot(ms, {"system": {"0/Dc/Battery/Soc": 42}, "battery": {"1/Soc": 19}})
    assert selected_samples(ms)["lowBattery"].value == 42
    assert "grid" not in selected_samples(ms)


async def test_only_selected_native_leaf_changes_can_generate_synthetic_events(observed):
    service, _, ms, clock = observed
    service.connect("mqtt", ms)
    clock[0] += 11
    await feed(observed, "evcharger/40/Ac/Power", 0)
    await feed(observed, "evcharger/41/Ac/Power", 7000)
    await feed(observed, "pump/1/State", 2)
    await feed(observed, "pump/1/State", 1)
    await ms.on_message(
        "inverter/state", b'{"battery_soc":1,"ev_power":7000,"grid_available":false}'
    )
    assert take(service, clock[0]) == []


async def test_new_native_description_datetime_type_order_waits_for_real_severity(observed):
    service, _, ms, clock = observed
    service.connect("mqtt", ms)
    clock[0] += 11
    root = "platform/0/Notifications/1"
    await feed(observed, root + "/Description", "Informational condition")
    await feed(observed, root + "/DateTime", int(clock[0]))
    assert take(service, clock[0]) == []
    await feed(observed, root + "/Type", 2)
    assert take(service, clock[0]) == []
    # A separate new alarm arriving Description -> DateTime -> Type must notify.
    root = "platform/0/Notifications/2"
    await feed(observed, root + "/Description", "Real warning")
    await feed(observed, root + "/DateTime", int(clock[0]))
    await feed(observed, root + "/Type", 0)
    result = take(service, clock[0])
    assert [event["title"] for event in result] == ["Real warning"]
    assert result[0]["sourceTimestampMs"] == int(clock[0] * 1000)


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b'{"value":NaN}', b'{"value":true}', b'{"value":"0"}', b'{"unrelated":0}'],
)
async def test_invalid_mqtt_packet_cannot_refresh_cached_synthetic_baseline(observed, payload):
    service, observer, ms, clock = observed
    service.connect("mqtt", ms)
    clock[0] += 11
    await feed(observed, "system/0/Dc/Battery/Soc", 30)
    clock[0] += 31
    topic = "N/site/system/0/Dc/Battery/Soc"
    await ms.on_message(topic, payload)
    observer.mqtt(ms, topic, retained=False, payload=payload)
    clock[0] += 1
    await feed(observed, "system/0/Dc/Battery/Soc", 19)
    assert take(service, clock[0]) == []


async def test_initial_partial_native_late_time_type_and_device_name_hydration_is_silent(observed):
    service, _, ms, clock = observed
    service.connect("mqtt", ms)
    path = "platform/0/Notifications/1"
    await feed(observed, path + "/Description", "Initial unknown warning")
    clock[0] += 15
    await feed(observed, path + "/DeviceName", "Later hydrated name")
    await feed(observed, path + "/DateTime", int(clock[0]))
    await feed(observed, path + "/Type", 1)
    assert take(service, clock[0]) == []
    clock[0] += 1
    await feed(observed, path + "/DateTime", int(clock[0]))
    assert [event["title"] for event in take(service, clock[0])] == ["Initial unknown warning"]


@pytest.mark.parametrize("bad_type", [None, True, False, "0", "1", 0.5, 4])
@pytest.mark.parametrize("source", ["mqtt", "igw"])
async def test_malformed_platform_type_keeps_banner_policy_but_never_pushes(
    observed, bad_type, source
):
    service, observer, ms, clock = observed
    if source == "mqtt":
        service.connect("mqtt", ms)
    else:
        gateway.apply_snapshot(ms, {})
        observer.snapshot(ms)
    clock[0] += 11
    leaves = {
        "0/Notifications/1/Description": "Unclassified event",
        "0/Notifications/1/DateTime": int(clock[0]),
        "0/Notifications/1/Type": bad_type,
    }
    if source == "mqtt":
        for path, value in leaves.items():
            await feed(observed, "platform/" + path, value)
    else:
        gateway.apply_snapshot(ms, {"platform": leaves})
        observer.snapshot(ms)
    assert ms.get_notifications()  # Existing banner behavior is deliberately unchanged.
    assert take(service, clock[0]) == []
