"""ESS writes require server-observed, live capability and a correlated request."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from inverter_dashboard import ess_mode, gateway, server
from inverter_dashboard import websocket_handler as ws

STATUS = {
    "selection_supported": True,
    "selected": "external_control",
    "vebus_mode": 3,
    "mode_name": "External control",
    "request_id": "previous",
    "error": "",
}


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(gateway, "_active_source", "mqtt")
    monkeypatch.setattr(gateway, "_dual_path", False)
    monkeypatch.setattr(gateway, "_source_generation", 0)
    ms = server.MqttState()
    ms._merge_daemon_state({"ess_mode": STATUS, "dry_run": False})
    client = SimpleNamespace(publish=AsyncMock())
    app = SimpleNamespace(
        mqtt_connected=True, gateway_connected=True, data_source="mqtt", mqtt_client=client
    )
    monkeypatch.setitem(ws._state, "mqtt_state", ms)
    monkeypatch.setitem(ws._state, "app_state", app)
    monkeypatch.setattr(gateway, "prefer_gateway", lambda: False)
    return ms, app, client


@pytest.mark.parametrize("mode", sorted(ess_mode.MODES))
async def test_explicit_selection_is_not_optimistic_and_preserves_request_id(runtime, mode):
    ms, _, client = runtime
    await ws._dispatch_action(
        "set_ess_mode",
        {"action": "set_ess_mode", "mode": mode, "request_id": "new-request"},
        client,
    )
    client.publish.assert_awaited_once_with(
        "inverter/cmd/set_ess_mode",
        json.dumps({"mode": mode, "request_id": "new-request"}),
        qos=0,
        retain=False,
    )
    assert ms.get_state()["ess_mode"] == STATUS


@pytest.mark.parametrize(
    "case", ["retained", "stale", "future", "unsupported", "dry", "unknown", "offline"]
)
async def test_rejected_selection_never_publishes(runtime, case):
    ms, app, client = runtime
    if case == "retained":
        await ms.on_message(
            "inverter/state",
            json.dumps(
                {
                    "ess_mode": STATUS,
                    "dry_run": False,
                    "ess_mode_observed_at": time.time(),
                    "ess_mode_controls_available": True,
                }
            ).encode(),
            retained=True,
        )
    elif case == "stale":
        ms._ess_mode_observed_at = time.time() - 31
    elif case == "future":
        ms._ess_mode_observed_at = time.time() + 10
    elif case == "unsupported":
        ms._controller_ess_mode["selection_supported"] = False
    elif case == "dry":
        ms.current_state["dry_run"] = True
    elif case == "unknown":
        ms.current_state["dry_run"] = None
    else:
        app.mqtt_connected = False
    with pytest.raises(ValueError):
        await ws._dispatch_action("set_ess_mode", {"mode": "off", "request_id": "request"}, client)
    client.publish.assert_not_awaited()
    assert not ws.build_payload()["ess_mode_controls_available"]


@pytest.mark.parametrize("case", ["dry", "unsupported", "offline", "client_swap", "state_swap"])
async def test_queued_telemetry_change_revalidates_at_mqtt_dispatch(runtime, monkeypatch, case):
    ms, app, client = runtime

    def change_before_dispatch():
        if case == "dry":
            ms.current_state["dry_run"] = True
        elif case == "unsupported":
            ms._controller_ess_mode["selection_supported"] = False
        elif case == "offline":
            app.mqtt_connected = False
        elif case == "client_swap":
            app.mqtt_client = SimpleNamespace(publish=AsyncMock())
        else:
            replacement = server.MqttState()
            replacement._merge_daemon_state({"ess_mode": STATUS, "dry_run": False})
            monkeypatch.setitem(ws._state, "mqtt_state", replacement)

    # The initial check runs now; the queued update runs before the child task writes.
    asyncio.get_running_loop().call_soon(change_before_dispatch)
    with pytest.raises(ValueError):
        await ws._dispatch_action("set_ess_mode", {"mode": "off", "request_id": "queued"}, client)
    client.publish.assert_not_awaited()
    app.mqtt_client.publish.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"mode": "bad", "request_id": "id"},
        {"mode": "off", "request_id": "bad/id"},
        {"mode": "off", "request_id": "x" * 129},
        {"mode": "off", "request_id": "id", "topic": "evil"},
        {"mode": [], "request_id": "id"},
    ],
)
def test_invalid_command_envelope(body):
    with pytest.raises(ValueError):
        ess_mode.validate_selection(body)


async def test_controller_selection_survives_native_overlay_and_clear(runtime):
    ms, _, _ = runtime
    gateway.apply_snapshot(
        ms,
        {
            "settings": {"0/Settings/CGwacs/Hub4Mode": 3},
            "inverter": {"ess_mode": STATUS, "dry_run": False},
        },
    )
    assert ws.build_payload()["ess_mode"] == STATUS
    assert ms.get_state()["ess_mode_observed_at"] is not None
    ms.clear_daemon_state()
    assert ms.get_state()["ess_mode_observed_at"] is None
    assert not ws.build_payload()["ess_mode_controls_available"]
    assert "request_id" not in ms.get_state()["ess_mode"]


@pytest.mark.parametrize(
    "case", ["ok", "gateway_unsupported", "controller_unsupported", "dry", "null", "changed_source"]
)
async def test_gateway_revalidates_fresh_snapshot_before_post(runtime, monkeypatch, case):
    ms, _, client = runtime
    monkeypatch.setattr(gateway, "prefer_gateway", lambda: True)
    ms.gateway_capabilities = {"set_ess_mode": True}
    snapshot = {
        "capabilities": {"set_ess_mode": True},
        "inverter": {"ess_mode": dict(STATUS), "dry_run": False},
    }
    if case == "gateway_unsupported":
        snapshot["capabilities"] = {}
    elif case == "controller_unsupported":
        snapshot["inverter"]["ess_mode"]["selection_supported"] = False
    elif case == "dry":
        snapshot["inverter"]["dry_run"] = True
    elif case == "null":
        snapshot["inverter"] = None

    async def fresh(_):
        if case == "changed_source":
            monkeypatch.setitem(ws._state, "mqtt_state", server.MqttState())
        return snapshot

    monkeypatch.setattr(gateway, "fetch_snapshot", fresh)
    post = AsyncMock()
    monkeypatch.setattr(gateway, "post_command", post)
    body = {"mode": "off", "request_id": "request"}
    if case == "ok":
        await ws._dispatch_action("set_ess_mode", body, client)
        post.assert_awaited_once_with(
            "set_ess_mode", body, expected_generation=gateway.source_generation()
        )
    else:
        with pytest.raises(ValueError):
            await ws._dispatch_action("set_ess_mode", body, client)
        post.assert_not_awaited()
    client.publish.assert_not_awaited()


async def test_gateway_roundtrip_cancels_slow_selection_without_post(runtime, monkeypatch):
    ms, _, client = runtime
    monkeypatch.setattr(gateway, "prefer_gateway", lambda: True)
    ms.gateway_capabilities = {"set_ess_mode": True}
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def delayed_snapshot(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
        # Even a transport swallowing cancellation cannot pass generation.
        return {
            "capabilities": {"set_ess_mode": True},
            "inverter": {"ess_mode": STATUS, "dry_run": False},
        }

    monkeypatch.setattr(gateway, "fetch_snapshot", delayed_snapshot)
    post = AsyncMock()
    monkeypatch.setattr(gateway, "post_command", post)
    task = asyncio.create_task(
        ws._dispatch_action("set_ess_mode", {"mode": "off", "request_id": "roundtrip"}, client)
    )
    await asyncio.wait_for(entered.wait(), 1)
    gateway.set_active_source("mqtt")
    gateway.set_active_source("igw")
    with pytest.raises(ValueError):
        await asyncio.wait_for(task, 1)
    assert cancelled.is_set()
    post.assert_not_awaited()


@pytest.mark.parametrize("source_replaced", [False, True])
async def test_selection_preserves_external_cancellation_and_cleans_child(runtime, source_replaced):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(gateway.run_ess_selection(gateway.source_generation(), operation))
    await asyncio.wait_for(entered.wait(), 1)
    if source_replaced:
        gateway.invalidate_ess_commands()
    else:
        task.cancel()
    error = ValueError if source_replaced else asyncio.CancelledError
    with pytest.raises(error):
        await asyncio.wait_for(task, 1)
    assert cleaned.is_set()
    assert not gateway._ess_command_tasks


async def test_selection_propagates_operation_failure(runtime):
    async def fail():
        raise RuntimeError("synthetic transport failure")

    with pytest.raises(RuntimeError, match="synthetic transport failure"):
        await gateway.run_ess_selection(gateway.source_generation(), fail)
    assert not gateway._ess_command_tasks


async def test_source_change_during_post_client_entry_never_posts(monkeypatch):
    monkeypatch.setattr(gateway, "_active_source", "igw")
    monkeypatch.setattr(gateway, "_dual_path", False)
    monkeypatch.setattr(gateway, "_source_generation", 0)
    generation = gateway.source_generation()
    post = AsyncMock()

    class Client:
        async def __aenter__(self):
            gateway.set_active_source("mqtt")
            gateway.set_active_source("igw")
            return SimpleNamespace(post=post)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(gateway, "_new_gateway_client", Client)
    monkeypatch.setattr(gateway.config, "GATEWAY_URL", "https://gateway.example")
    monkeypatch.setattr(gateway, "build_headers", dict)
    with pytest.raises(ValueError):
        await gateway.post_command(
            "set_ess_mode", {"mode": "off", "request_id": "id"}, expected_generation=generation
        )
    post.assert_not_awaited()
