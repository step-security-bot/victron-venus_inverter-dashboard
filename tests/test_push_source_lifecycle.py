"""Late source callbacks must not mutate or revive a replacement push epoch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from inverter_dashboard import gateway, server


async def test_gateway_captured_callbacks_reject_replaced_owner_and_generation(monkeypatch):
    owner = server.AppState()
    monkeypatch.setattr(server, "_app_state", owner)
    monkeypatch.setattr(server.websocket_handler, "set_mqtt_state", Mock())
    broadcast, observer, service = AsyncMock(), Mock(), Mock()
    monkeypatch.setattr(server.websocket_handler, "broadcast_state", broadcast)
    monkeypatch.setattr(server, "_push_observer", observer)
    monkeypatch.setattr(server, "_push_service", service)
    callbacks = []

    async def poll(_owner, apply, status, *, is_current):
        callbacks.append((apply, status, is_current))

    monkeypatch.setattr(gateway, "gateway_poll_loop", poll)
    server._start_gateway_client()
    await asyncio.wait_for(asyncio.gather(owner.mqtt_tasks[-1]), 1)
    original = owner.mqtt_state
    old_apply, old_status, current = callbacks[0]
    await old_apply({"inverter": {"marker": "first"}})
    observer.snapshot.assert_called_once_with(original)
    observer.reset_mock()
    broadcast.reset_mock()
    # Even reuse of the original state object cannot revive its closed generation.
    owner.source_generation += 1
    await old_apply({"inverter": {"marker": "late"}})
    await old_status()
    assert original.current_state["marker"] == "first"
    assert not current()
    assert not observer.snapshot.called and not broadcast.called
    # A replacement of the same transport also rejects the former callbacks.
    server._start_gateway_client()
    await asyncio.wait_for(asyncio.gather(owner.mqtt_tasks[-1]), 1)
    replacement = owner.mqtt_state
    replacement.current_state["marker"] = "replacement"
    await old_apply({"inverter": {"marker": "late"}})
    await old_status()
    assert replacement.current_state["marker"] == "replacement"
    assert not service.disconnect.called and not observer.snapshot.called


@pytest.mark.parametrize("failure", [False, True])
async def test_gateway_late_fetch_cannot_change_connection_flags(monkeypatch, failure):
    owner = server.AppState(mqtt_connected=True, gateway_connected=False)
    current = True

    async def fetch(_client):
        nonlocal current
        current = False
        if failure:
            raise RuntimeError("old fetch")
        return {"inverter": {"marker": "late"}}

    monkeypatch.setattr(gateway, "fetch_snapshot", fetch)
    apply, status = AsyncMock(), AsyncMock()
    await gateway.gateway_poll_loop(owner, apply, status, is_current=lambda: current)
    assert owner.mqtt_connected and not owner.gateway_connected
    assert owner.gateway_polls == owner.gateway_errors == 0
    apply.assert_not_called()
    status.assert_not_called()


async def test_mqtt_late_message_and_cleanup_do_not_touch_replacement_state(monkeypatch):
    owner = server.AppState()
    monkeypatch.setattr(server, "_app_state", owner)
    monkeypatch.setattr(server.websocket_handler, "set_mqtt_state", Mock())
    monkeypatch.setattr(server.websocket_handler, "broadcast_state", AsyncMock())
    observer, service = Mock(), Mock()
    monkeypatch.setattr(server, "_push_observer", observer)
    monkeypatch.setattr(server, "_push_service", service)
    monkeypatch.setattr(server, "_subscribe_topics", AsyncMock())
    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        """Controlled MQTT session with no broker or device commands."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        @property
        def messages(self):
            async def messages():
                entered.set()
                await release.wait()
                yield SimpleNamespace(
                    topic=SimpleNamespace(value="inverter/state"),
                    payload=b'{"marker":"old"}',
                    retain=False,
                )

            return messages()

    monkeypatch.setattr(server, "_make_mqtt_client", Client)
    server._start_mqtt_client()
    original = owner.mqtt_state
    original.on_message = AsyncMock()
    task = owner.mqtt_tasks[0]
    await entered.wait()
    replacement = server.MqttState()
    replacement.current_state["marker"] = "replacement"
    owner.source_generation += 1
    owner.data_source, owner.mqtt_state = "igw", replacement
    owner.gateway_connected, owner.mqtt_connected = True, False
    release.set()
    await asyncio.wait_for(asyncio.gather(task), 1)
    original.on_message.assert_not_called()
    observer.mqtt.assert_not_called()
    service.disconnect.assert_not_called()
    assert replacement.current_state["marker"] == "replacement"
    assert owner.gateway_connected and not owner.mqtt_connected


async def test_mqtt_source_switch_during_message_await_skips_observer(monkeypatch):
    owner = server.AppState()
    monkeypatch.setattr(server, "_app_state", owner)
    monkeypatch.setattr(server.websocket_handler, "set_mqtt_state", Mock())
    monkeypatch.setattr(server.websocket_handler, "broadcast_state", AsyncMock())
    observer = Mock()
    monkeypatch.setattr(server, "_push_observer", observer)
    monkeypatch.setattr(server, "_push_service", Mock())
    monkeypatch.setattr(server, "_subscribe_topics", AsyncMock())
    replacement = server.MqttState()

    class Client:
        """Controlled MQTT session with no broker or device commands."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        @property
        def messages(self):
            async def messages():
                yield SimpleNamespace(
                    topic=SimpleNamespace(value="inverter/state"), payload=b"{}", retain=False
                )

            return messages()

    async def delayed_apply(_self, *_args, **_kwargs):
        owner.source_generation += 1
        owner.mqtt_state, owner.data_source = replacement, "igw"
        await asyncio.sleep(0)

    monkeypatch.setattr(server.MqttState, "on_message", delayed_apply)
    monkeypatch.setattr(server, "_make_mqtt_client", Client)
    server._start_mqtt_client()
    await asyncio.wait_for(asyncio.gather(owner.mqtt_tasks[0]), 1)
    observer.mqtt.assert_not_called()
    assert owner.mqtt_state is replacement


async def test_push_lifecycle_is_opt_in_and_joins_workers_without_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(server.config, "WEB_PUSH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server.ha_client, "load_config", lambda: None)
    monkeypatch.setattr(server.settings_store, "apply_connection_overrides", lambda: None)
    monkeypatch.setattr(server.settings_store, "load_settings", dict)
    monkeypatch.setattr(server, "_select_and_start_data_source", AsyncMock())
    monkeypatch.setattr(server, "_start_ha_polling", lambda: None)
    monkeypatch.setattr(server, "_start_version_check", lambda: None)
    monkeypatch.setattr(server, "_app_state", server.AppState())
    async with server.lifespan(server.app):
        service = server._push_service
        workers = list(service.workers)
        assert service.available and len(workers) == 2
    assert server._push_service is None and server._push_observer is None
    assert all(task.done() for task in workers)


@pytest.mark.parametrize(
    "name,mime",
    [
        ("notifications-sw.js", "application/javascript"),
        ("manifest.webmanifest", "application/manifest+json"),
        ("notification-icon.svg", "image/svg+xml"),
    ],
)
async def test_exact_root_notification_assets_are_public_and_not_cached(
    tmp_path, monkeypatch, name, mime
):
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setattr(server, "DASHBOARD_SECRET", "test-secret")
    monkeypatch.setattr(server, "_resolve_spa_root", lambda: tmp_path)
    (tmp_path / name).write_text("inert test asset")
    async with AsyncClient(
        transport=ASGITransport(app=server.app), base_url="https://dashboard.test"
    ) as client:
        response = await client.get("/" + name)
        assert response.status_code == 200
        assert response.text == "inert test asset"
        assert response.headers["content-type"].startswith(mime)
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["service-worker-allowed"] == "/"
        assert (await client.get("/api/notifications/status")).status_code == 401


async def test_corrupt_push_store_preserves_telemetry_startup_and_reports_unavailable(
    tmp_path, monkeypatch
):
    from httpx import ASGITransport, AsyncClient

    path = tmp_path / "push.sqlite3"
    path.write_bytes(b"corrupt database never reset")
    monkeypatch.setattr(server.config, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(server.config, "WEB_PUSH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server.ha_client, "load_config", lambda: None)
    monkeypatch.setattr(server.settings_store, "apply_connection_overrides", lambda: None)
    monkeypatch.setattr(server.settings_store, "load_settings", dict)
    initialize = AsyncMock()
    monkeypatch.setattr(server, "_select_and_start_data_source", initialize)
    monkeypatch.setattr(server, "_start_ha_polling", lambda: None)
    monkeypatch.setattr(server, "_start_version_check", lambda: None)
    monkeypatch.setattr(server, "_app_state", server.AppState())
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    async with server.lifespan(server.app):
        initialize.assert_awaited_once()
        assert not server._push_service.workers
        async with AsyncClient(
            transport=ASGITransport(app=server.app), base_url="https://dashboard.test"
        ) as client:
            status = (await client.get("/api/notifications/status")).json()
        assert status["enabled"] is True and status["available"] is False
        assert status["reason"] == "storage_unavailable" and status["publicKey"] is None
    assert path.read_bytes() == b"corrupt database never reset"


async def test_real_mqtt_loop_passes_actual_payload_to_independent_push_observer(
    tmp_path, monkeypatch
):
    from inverter_dashboard.push_events import DEFAULT_PREFERENCES
    from inverter_dashboard.push_observer import PushObserver
    from inverter_dashboard.push_service import PushService
    from tests.test_push_transport import receiver

    clock = [1_800_000_000.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    monkeypatch.setattr(server.config, "CERBO_PORTAL_ID", "site")
    monkeypatch.setattr(server.config, "WATER_PUMP_INSTANCE", 1)
    monkeypatch.setattr(server, "_app_state", server.AppState())
    monkeypatch.setattr(server.websocket_handler, "set_mqtt_state", Mock())
    monkeypatch.setattr(server.websocket_handler, "broadcast_state", AsyncMock())
    monkeypatch.setattr(server, "_subscribe_topics", AsyncMock())
    service = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    service.register(receiver()[2], dict(DEFAULT_PREFERENCES))
    monkeypatch.setattr(server, "_push_service", service)
    monkeypatch.setattr(server, "_push_observer", PushObserver(service))
    observed = asyncio.Event()

    class Client:
        """Two real source messages, with no network, broker or WebSocket client."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        @property
        def messages(self):
            async def messages():
                clock[0] += 11
                yield SimpleNamespace(
                    topic=SimpleNamespace(value="N/site/pump/1/State"),
                    payload=b'{"value":0}',
                    retain=False,
                )
                clock[0] += 1
                yield SimpleNamespace(
                    topic=SimpleNamespace(value="N/site/pump/1/State"),
                    payload=b'{"value":1}',
                    retain=False,
                )
                observed.set()
                await asyncio.Event().wait()

            return messages()

    monkeypatch.setattr(server, "_make_mqtt_client", Client)
    server._start_mqtt_client()
    task = server._app_state.mqtt_tasks[0]
    try:
        await asyncio.wait_for(observed.wait(), 1)
        delivery = service.store.next_delivery(clock[0])
        assert delivery is not None and delivery["payload"]["title"] == "Water pump on"
        assert delivery["payload"]["sourceTimestampMs"] == int(clock[0] * 1000)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


async def test_source_retirement_wakes_reconnect_backoff_without_waiting(monkeypatch):
    owner = server.AppState()
    monkeypatch.setattr(server, "_app_state", owner)
    monkeypatch.setattr(server.websocket_handler, "set_mqtt_state", Mock())
    _, _, current, stopped = server._new_source_state("mqtt")
    waiting = asyncio.create_task(server._wait_for_source_retry(stopped, 60))
    await asyncio.sleep(0)
    assert not waiting.done()
    server._retire_source(owner)
    await asyncio.wait_for(asyncio.gather(waiting), 1)
    assert stopped.is_set() and not current()
