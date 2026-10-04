"""Health probes and normal shutdown stay independent of optional data sources."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from inverter_dashboard import server


def test_liveness_does_not_read_telemetry_or_require_dashboard_secret(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "configured-secret")
    state = Mock()
    state.get_state.side_effect = AssertionError("liveness read telemetry")
    monkeypatch.setattr(server._app_state, "mqtt_state", state)
    response = TestClient(server.app).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body_failure", [False, True])
async def test_lifespan_joins_all_children_and_clears_client(monkeypatch, body_failure):
    stopped = []

    async def worker(name):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(name)

    children = [asyncio.create_task(worker(name)) for name in ("ha", "mqtt", "version")]
    await asyncio.sleep(0)
    monkeypatch.setattr(server.ha_client, "load_config", lambda: None)
    monkeypatch.setattr(server.settings_store, "apply_connection_overrides", lambda: None)
    monkeypatch.setattr(server.settings_store, "load_settings", dict)
    monkeypatch.setattr(server, "_select_and_start_data_source", AsyncMock())
    monkeypatch.setattr(server, "_start_ha_polling", lambda: children[0])
    monkeypatch.setattr(server, "_start_version_check", lambda: children[2])
    monkeypatch.setattr(server._app_state, "mqtt_tasks", [children[1]])
    monkeypatch.setattr(server._app_state, "mqtt_client", object())
    monkeypatch.setattr(server._app_state, "mqtt_connected", True)

    async def run():
        async with server.lifespan(server.app):
            if body_failure:
                raise RuntimeError("application failure")

    if body_failure:
        with pytest.raises(RuntimeError, match="application failure"):
            await run()
    else:
        await run()

    assert sorted(stopped) == ["ha", "mqtt", "version"]
    assert all(task.cancelled() for task in children)
    assert not server._app_state.mqtt_tasks
    assert server._app_state.mqtt_client is None
    assert not server._app_state.mqtt_connected
