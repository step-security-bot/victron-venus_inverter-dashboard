"""Tests for dashboard settings persistence and the /api/settings routes."""

import pytest

from inverter_dashboard import settings_store, websocket_handler
from inverter_dashboard.settings_store import DEFAULTS


@pytest.fixture(autouse=True)
def _isolated_file(tmp_path, monkeypatch):
    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    yield


class TestSettingsStore:
    """Tests for settings_store load/save."""

    def test_defaults_when_no_file(self):
        s = settings_store.load_settings()
        assert s == DEFAULTS
        assert s["show_ev"] is True

    def test_save_and_reload_roundtrip(self):
        saved = settings_store.save_settings({"show_ev": False})
        assert saved["show_ev"] is False
        assert settings_store.load_settings()["show_ev"] is False

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="unknown setting"):
            settings_store.save_settings({"not_a_setting": "x"})

    def test_connection_secrets_masked_on_load(self):
        settings_store.save_settings({"ha_token": "tok123", "mqtt_password": "pw"})
        masked = settings_store.load_settings(mask_secrets=True)
        assert masked["ha_token"] == "***"
        assert masked["mqtt_password"] == "***"
        raw = settings_store.load_settings()
        assert raw["ha_token"] == "tok123"

    def test_wrong_type_port_rejected(self):
        with pytest.raises(ValueError, match="must be int"):
            settings_store.save_settings({"mqtt_port": "1883"})

    def test_apply_connection_overrides(self):
        from inverter_dashboard import config as cfgmod

        settings_store.save_settings(
            {"mqtt_host": "broker.local", "mqtt_port": 8883, "ha_url": "http://ha:8123"}
        )
        # ha_client not configured → override_credentials just records url/token
        applied = settings_store.apply_connection_overrides()
        assert applied >= 1
        assert cfgmod.MQTT_HOST == "broker.local"
        assert cfgmod.MQTT_PORT == 8883

    def test_wrong_type_rejected(self):
        with pytest.raises(ValueError, match="must be bool"):
            settings_store.save_settings({"show_ev": "yes"})

    def test_partial_patch_merges(self):
        settings_store.save_settings({"camera_topic": "frigate/+/events"})
        s = settings_store.save_settings({"show_dryer": False})
        assert s["camera_topic"] == "frigate/+/events"
        assert s["show_dryer"] is False

    def test_invalid_json_file_ignored(self, tmp_path):
        (tmp_path / "dashboard_settings.json").write_text("{not json")
        assert settings_store.load_settings() == DEFAULTS


async def test_settings_routes_unauthorized(monkeypatch):
    """Routes require the bearer secret when DASHBOARD_SECRET is set."""
    from httpx import ASGITransport, AsyncClient

    from inverter_dashboard import server

    monkeypatch.setattr(server, "DASHBOARD_SECRET", "s3cret")
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://t") as client:
        assert (await client.get("/api/settings")).status_code == 401
        r = await client.post("/api/settings", json={"show_ev": False})
        assert r.status_code == 401
        # valid secret passes
        ok = await client.get("/api/settings", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


def test_set_ui_settings_filters_visibility_into_payload():
    websocket_handler.set_ui_settings({**DEFAULTS, "show_ev": False, "camera_topic": "x"})
    payload = websocket_handler._with_ui_config({})
    vis = payload["ui_config"]["settings"]
    assert vis["show_ev"] is False
    assert "camera_topic" not in vis  # visibility keys only


async def test_ws_set_settings_action_persists_and_applies():
    """WS 'set_settings' validates, persists, hot-applies."""
    import asyncio
    import json

    from inverter_dashboard import server

    saved = {}

    class FakeMqttClient:  # pylint: disable=too-few-public-methods
        """No-op MQTT client stub."""

        async def publish(self, topic, payload):  # unused for this action
            saved["published"] = (topic, payload)

    server.websocket_handler._ui_settings = {}
    from inverter_dashboard.websocket_handler import _dispatch_action

    await _dispatch_action("set_settings", {"show_ev": False}, FakeMqttClient())
    await asyncio.sleep(0)
    assert websocket_handler.get_ui_settings()["show_ev"] is False
    assert settings_store.load_settings()["show_ev"] is False

    # Invalid patches are rejected so correlated callers receive an error.
    with pytest.raises(ValueError):
        await _dispatch_action("set_settings", {"nope": True}, FakeMqttClient())
    assert "nope" not in websocket_handler.get_ui_settings()
    assert json.dumps(saved) is not None  # silence unused
