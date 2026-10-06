"""Desktop override/tariff commands require current authoritative support."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from inverter_dashboard import controller_commands as commands
from inverter_dashboard import gateway, server
from inverter_dashboard import websocket_handler as ws

OVERRIDE = {"value": None, "last_error": None, "request_id": None}
TARIFF = {"writable": True, "revision": "a" * 64}


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(gateway, "_active_source", "mqtt")
    monkeypatch.setattr(gateway, "_source_generation", 0)
    monkeypatch.setattr(gateway, "prefer_gateway", lambda: False)
    ms = server.MqttState()
    ms._merge_daemon_state(
        {
            "setpoint_override": OVERRIDE,
            "dry_run": True,
            "ui_config": {"electricity_tariff_status": TARIFF},
        }
    )
    client = SimpleNamespace(publish=AsyncMock())
    app = SimpleNamespace(
        mqtt_state=ms,
        mqtt_client=client,
        mqtt_connected=True,
        gateway_connected=False,
        data_source="mqtt",
    )
    monkeypatch.setitem(ws._state, "mqtt_state", ms)
    monkeypatch.setitem(ws._state, "app_state", app)
    return ms, app, client


@pytest.mark.parametrize("value", [None, 0, -350, -(2**31), 2**31 - 1])
async def test_override_single_explicit_send_even_dry_no_optimistic_status(runtime, value):
    ms, _, client = runtime

    async def acknowledge(_topic, payload, **_kwargs):
        assert ms.get_state()["setpoint_override"] == OVERRIDE
        body = json.loads(payload)
        await ms.on_message(
            "inverter/setpoint_override", json.dumps({**body, "last_error": None}).encode()
        )

    client.publish.side_effect = acknowledge
    await ws._dispatch_action(
        "set_setpoint_override",
        {"action": "set_setpoint_override", "value": value, "request_id": "override-1"},
        client,
    )
    client.publish.assert_awaited_once_with(
        "inverter/cmd/setpoint_override",
        commands.encode_body({"value": value, "request_id": "override-1"}),
        qos=0,
        retain=False,
    )
    assert ms.get_state()["setpoint_override"] == {
        "value": value,
        "request_id": "override-1",
        "last_error": None,
    }


@pytest.mark.parametrize("value", [True, 1.0, "1", 2**31, -(2**31) - 1, [], float("nan")])
async def test_override_invalid_values_never_publish(runtime, value):
    with pytest.raises(ValueError):
        await ws._dispatch_action(
            "set_setpoint_override", {"value": value, "request_id": "id"}, runtime[2]
        )
    runtime[2].publish.assert_not_awaited()


@pytest.mark.parametrize("case", ["retained", "stale", "future", "offline", "missing", "malformed"])
async def test_override_no_fresh_supported_status_no_write(runtime, case):
    ms, app, client = runtime
    if case == "retained":
        await ms.on_message(
            "inverter/setpoint_override", json.dumps(OVERRIDE).encode(), retained=True
        )
    elif case in ("stale", "future"):
        ms._setpoint_override_observed_at = time.time() + (10 if case == "future" else -31)
    elif case == "offline":
        app.mqtt_connected = False
    else:
        await ms.on_message(
            "inverter/setpoint_override",
            json.dumps(None if case == "missing" else {"value": 0}).encode(),
        )
    assert not ws.build_payload()["setpoint_override_controls_available"]
    with pytest.raises(ValueError):
        await ws._dispatch_action(
            "set_setpoint_override", {"value": None, "request_id": "id"}, client
        )
    client.publish.assert_not_awaited()


@pytest.mark.parametrize("case", ["offline", "state", "client", "generation"])
async def test_queued_override_rechecks_owner_immediately_before_write(runtime, monkeypatch, case):
    ms, app, client = runtime

    def change():
        if case == "offline":
            app.mqtt_connected = False
        elif case == "state":
            monkeypatch.setitem(ws._state, "mqtt_state", server.MqttState())
        elif case == "client":
            app.mqtt_client = SimpleNamespace(publish=AsyncMock())
        else:
            gateway.invalidate_ess_commands()

    asyncio.get_running_loop().call_soon(change)
    with pytest.raises(ValueError):
        await ws._dispatch_action("set_setpoint_override", {"value": 1, "request_id": "id"}, client)
    client.publish.assert_not_awaited()
    assert ms.get_state()["setpoint_override"] == OVERRIDE


async def test_retained_support_cannot_forge_server_freshness(runtime):
    ms, _, client = runtime
    await ms.on_message(
        "inverter/state",
        json.dumps(
            {
                "setpoint_override": OVERRIDE,
                "setpoint_override_observed_at": time.time(),
                "setpoint_override_controls_available": True,
            }
        ).encode(),
        retained=True,
    )
    assert ms.get_state()["setpoint_override_observed_at"] is None
    with pytest.raises(ValueError):
        await ws._dispatch_action("set_setpoint_override", {"value": 1, "request_id": "id"}, client)
    await ms.on_message("inverter/state", json.dumps({"setpoint_override": OVERRIDE}).encode())
    assert ws.build_payload()["setpoint_override_controls_available"]


async def test_tariff_revision_and_capability_checked_before_send(runtime):
    ms, _, client = runtime
    body = {"plan": None, "revision": "b" * 64, "request_id": "tariff"}
    with pytest.raises(ValueError, match="changed"):
        await ws._dispatch_action("electricity_tariff", body, client)
    client.publish.assert_not_awaited()
    body["revision"] = TARIFF["revision"]
    await ws._dispatch_action("electricity_tariff", body, client)
    client.publish.assert_awaited_once_with(
        "inverter/cmd/electricity_tariff", commands.encode_body(body), qos=0, retain=False
    )
    assert ms.get_state()["ui_config"]["electricity_tariff_status"] == TARIFF


@pytest.mark.parametrize(
    "patch",
    [
        {"revision": "A" * 64},
        {"request_id": "bad/id"},
        {"plan": []},
        {"plan": {"x": float("nan")}},
        {"plan": {"x": "x" * 100000}},
        {"extra": 1},
    ],
)
def test_tariff_rejects_invalid_bounded_envelope(patch):
    with pytest.raises(ValueError):
        commands.validate_tariff({"plan": None, "revision": "a" * 64, "request_id": "id", **patch})


async def test_gateway_override_checks_fresh_snapshot_and_post_entry(runtime, monkeypatch):
    ms, app, client = runtime
    monkeypatch.setattr(gateway, "prefer_gateway", lambda: True)
    app.data_source = "igw"
    app.gateway_connected = True
    ms.gateway_capabilities = {"setpoint_override": True}
    snapshot = {
        "capabilities": {"setpoint_override": True},
        "inverter": {"setpoint_override": OVERRIDE},
    }
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    monkeypatch.setattr(gateway, "_new_gateway_client", lambda: fake)
    monkeypatch.setattr(gateway, "fetch_snapshot", AsyncMock(return_value=snapshot))
    posts = []

    async def changed_before_post(_name, _body, *, expected_generation, before_send):
        assert expected_generation == gateway.source_generation()
        app.gateway_connected = False
        before_send()
        posts.append(_body)

    monkeypatch.setattr(gateway, "post_command", changed_before_post)
    with pytest.raises(ValueError):
        await ws._dispatch_action("set_setpoint_override", {"value": 1, "request_id": "id"}, client)
    assert not posts
    client.publish.assert_not_awaited()


async def test_override_status_subscription_and_retirement(runtime):
    ms, _, client = runtime
    client.subscribe = AsyncMock()
    await server._subscribe_topics(client, "test-portal")
    assert any(
        call.args == ("inverter/setpoint_override",) for call in client.subscribe.await_args_list
    )
    await ms.on_message(
        "inverter/setpoint_override", b'{"value":-75,"last_error":null,"request_id":"ack"}'
    )
    assert ms.get_state()["setpoint_override"]["request_id"] == "ack"
    ms.clear_daemon_state()
    assert ms.get_state()["setpoint_override"] is None
    assert ms.get_state()["setpoint_override_observed_at"] is None


@pytest.mark.parametrize("case", ["missing", "wrong_id", "wrong_value", "error"])
async def test_override_never_accepts_wrong_or_missing_ack_or_resends(runtime, monkeypatch, case):
    ms, _, client = runtime
    monkeypatch.setattr(ws, "OVERRIDE_TIMEOUT_SECONDS", 0.03)

    async def response(_topic, _payload, **_kwargs):
        if case == "missing":
            return
        status = {"value": None, "request_id": "stop", "last_error": None}
        if case == "wrong_id":
            status["request_id"] = "old"
        elif case == "wrong_value":
            status["value"] = 1
        else:
            status["last_error"] = "rejected"
        await ms.on_message("inverter/setpoint_override", json.dumps(status).encode())

    client.publish.side_effect = response
    with pytest.raises((TimeoutError, ValueError)):
        await ws._dispatch_action(
            "set_setpoint_override", {"value": None, "request_id": "stop"}, client
        )
    client.publish.assert_awaited_once()


async def test_override_total_deadline_covers_gateway_preflight_no_post(runtime, monkeypatch):
    ms, app, client = runtime
    monkeypatch.setattr(gateway, "prefer_gateway", lambda: True)
    app.data_source = "igw"
    app.gateway_connected = True
    ms.gateway_capabilities = {"setpoint_override": True}
    fake = AsyncMock()
    fake.__aenter__.return_value = fake
    monkeypatch.setattr(gateway, "_new_gateway_client", lambda: fake)

    async def slow(_client):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(gateway, "fetch_snapshot", slow)
    post = AsyncMock()
    monkeypatch.setattr(gateway, "post_command", post)
    monkeypatch.setattr(ws, "OVERRIDE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        await ws._dispatch_action(
            "set_setpoint_override", {"value": None, "request_id": "id"}, client
        )
    post.assert_not_awaited()


@pytest.mark.parametrize("case", ["offline", "client_swap", "source_swap", "expired"])
async def test_legacy_header_actions_require_current_controller_transport(runtime, case):
    ms, app, client = runtime
    if case == "offline":
        app.mqtt_connected = False
    elif case == "client_swap":
        app.mqtt_client = SimpleNamespace(publish=AsyncMock())
    elif case == "source_swap":
        app.data_source = "igw"
    else:
        ms._daemon_received_at = time.monotonic() - 121
    with pytest.raises(ValueError):
        await ws._dispatch_action("toggle", {"entity": "only_charging", "state": True}, client)
    client.publish.assert_not_awaited()


async def test_generic_command_result_is_separate_from_state_and_failure_safe(monkeypatch):
    frames = []

    async def send(_socket, body):
        frames.append(json.loads(body))

    monkeypatch.setattr(ws, "_send_state", send)
    await ws._command_reply(None, "toggle", "id")
    await ws._command_reply(None, "toggle", "id", failed=True)
    await ws._command_reply(None, "toggle", "bad/id", failed=True)
    assert frames[0] == {
        "type": "command_result",
        "action": "toggle",
        "request_id": "id",
        "status": "accepted",
    }
    assert frames[1]["type"] == "command_error" and frames[1]["request_id"] == "id"
    assert len(frames) == 2


@pytest.mark.parametrize("case", ["retained", "expired", "future"])
async def test_retained_or_stale_controller_can_display_but_cannot_control(runtime, case):
    ms, _, client = runtime
    if case == "retained":
        ms._merge_daemon_state({"dry_run": False}, retained=True)
    else:
        ms._controller_command_received_at = time.monotonic() + (1 if case == "future" else -31)
    assert ms.controller_available()
    assert not ws.build_payload()["controller_controls_available"]
    with pytest.raises(ValueError):
        await ws._dispatch_action("dry_run", {"value": True}, client)
    client.publish.assert_not_awaited()


@pytest.mark.parametrize("observation", ["none", "retained", "fresh"])
async def test_override_reused_id_requires_new_nonretained_receipt(
    runtime, monkeypatch, observation
):
    ms, _, client = runtime
    matching = {"value": None, "last_error": None, "request_id": "reused"}
    ms._merge_daemon_state({"setpoint_override": matching})
    monkeypatch.setattr(ws, "OVERRIDE_TIMEOUT_SECONDS", 0.02)

    async def publish(*_args, **_kwargs):
        if observation != "none":
            await ms.on_message(
                "inverter/setpoint_override",
                json.dumps(matching).encode(),
                retained=observation == "retained",
            )

    client.publish.side_effect = publish
    if observation == "fresh":
        await ws._dispatch_action(
            "set_setpoint_override", {"value": None, "request_id": "reused"}, client
        )
    else:
        with pytest.raises((ValueError, TimeoutError)):
            await ws._dispatch_action(
                "set_setpoint_override", {"value": None, "request_id": "reused"}, client
            )
    client.publish.assert_awaited_once()


async def test_tariff_actual_wire_bytes_match_validated_utf8_budget(runtime, monkeypatch):
    import httpx

    ms, _, client = runtime
    body = {"request_id": "unicode", "revision": TARIFF["revision"], "plan": {"name": "é" * 20000}}
    expected = commands.encode_body(commands.validate_tariff(body)).encode("utf-8")
    assert len(expected) < 100_000 < len(json.dumps(body).encode())
    await ws._dispatch_action("electricity_tariff", body, client)
    assert client.publish.await_args.args[1].encode("utf-8") == expected
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(gateway.config, "GATEWAY_URL", "https://gateway.example")
    monkeypatch.setattr(gateway, "build_headers", dict)
    monkeypatch.setattr(
        gateway,
        "_new_gateway_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await gateway.post_command("electricity_tariff", body)
    assert sent[0].content == expected
    assert sent[0].headers["Content-Type"] == "application/json"
    assert ms.get_state()["ui_config"]["electricity_tariff_status"] == TARIFF


@pytest.mark.parametrize(
    "case", ["success", "old_client", "offline", "changed_portal", "generation"]
)
async def test_notification_ack_uses_current_native_owner_without_controller(runtime, case):
    ms, app, client = runtime
    ms.clear_daemon_state()
    ms._portal_id = "site"
    assert not ms.controller_available()

    def change():
        if case == "old_client":
            app.mqtt_client = SimpleNamespace(publish=AsyncMock())
        elif case == "offline":
            app.mqtt_connected = False
        elif case == "changed_portal":
            ms._portal_id = "other"
        elif case == "generation":
            gateway.invalidate_ess_commands()

    asyncio.get_running_loop().call_soon(change)
    if case == "success":
        await ws._acknowledge_victron_on_cerbo(None, client)
        client.publish.assert_awaited_once_with(
            "W/site/platform/0/Notifications/AcknowledgeAll", '{"value":1}', qos=0, retain=False
        )
    else:
        with pytest.raises(ValueError):
            await ws._acknowledge_victron_on_cerbo(None, client)
        client.publish.assert_not_awaited()


@pytest.mark.parametrize("native_id", ["victron-platform-0-1", "victron-battery-1-HighVoltage"])
async def test_notification_gateway_entry_swap_never_posts_local_dismiss_stays_separate(
    runtime, monkeypatch, native_id
):
    ms, app, client = runtime
    ms.clear_daemon_state()
    app.data_source = "igw"
    app.gateway_connected = True
    monkeypatch.setattr(gateway, "prefer_gateway", lambda: True)
    monkeypatch.setattr(gateway.config, "GATEWAY_URL", "https://gateway.example")
    monkeypatch.setattr(gateway, "build_headers", dict)
    fake = AsyncMock()

    async def enter():
        gateway.invalidate_ess_commands()
        return fake

    fake.__aenter__.side_effect = enter
    monkeypatch.setattr(gateway, "_new_gateway_client", lambda: fake)
    broadcast = AsyncMock()
    monkeypatch.setattr(ws, "broadcast_state", broadcast)
    ms.push_notification({"id": native_id, "title": "Fixture", "body": "", "level": "warning"})
    with pytest.raises(ValueError):
        await ws._dispatch_action("dismiss_notification", {"id": native_id}, client)
    fake.post.assert_not_awaited()
    assert not ms.get_notifications()
    broadcast.assert_awaited_once()


async def test_native_ack_timeout_propagates_without_retry(runtime, monkeypatch):
    ms, _, client = runtime
    ms.clear_daemon_state()
    ms._portal_id = "site"
    monkeypatch.setattr(ws, "NOTIFICATION_COMMAND_TIMEOUT_SECONDS", 0.01)

    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(0.1)

    client.publish.side_effect = stalled
    with pytest.raises(TimeoutError):
        await ws._acknowledge_victron_on_cerbo(None, client)
    client.publish.assert_awaited_once()
