"""Source provenance/partial coverage remains honest across both transports."""

import json
from unittest.mock import AsyncMock

import pytest

from inverter_dashboard import config, gateway, ha_client, notifications, server, settings_store
from inverter_dashboard import websocket_handler as ws


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_system_grid_null_cannot_resurrect_cached_meter_phase(monkeypatch, transport):
    monkeypatch.setattr(config, "CERBO_PORTAL_ID", "site")
    ms = server.MqttState()
    snapshot = {
        "grid": {"40/Ac/L1/Power": 100, "40/Ac/L2/Power": 200},
        "system": {"0/Ac/Grid/L1/Power": 10, "0/Ac/Grid/L2/Power": None},
    }
    if transport == "igw":
        gateway.apply_snapshot(ms, snapshot)
    else:
        for kind, leaves in snapshot.items():
            for key, value in leaves.items():
                await ms.on_message(f"N/site/{kind}/{key}", json.dumps({"value": value}).encode())
    state = ms.get_state()
    assert state["g1"] == 10 and state["g2"] is None and state["gt"] == 10
    assert state["grid_l1_available"] is True and state["grid_l2_available"] is False
    monkeypatch.setitem(ws._state, "mqtt_state", ms)
    assert ws.build_payload()["grid_l2_available"] is False


@pytest.mark.parametrize("transport", ["mqtt", "igw"])
async def test_daily_grid_energy_and_backup_provenance_survive_without_synthetic_coverage(
    monkeypatch, transport
):
    ms = server.MqttState()
    grid_energy = {
        "source": "grid/40",
        "quality": "partial",
        "reason": "incomplete_day",
        "import_kwh": 1.5,
        "export_kwh": 0.3,
        "date": "2026-10-06",
        "timezone": "America/Los_Angeles",
    }
    backup = {
        "enabled": True,
        "available": True,
        "measurement_time": 1791312000.0,
        "age_seconds": 45,
        "power": 10,
    }
    controller = {
        "daily_stats": {"grid_energy": grid_energy},
        "grid_backup": backup,
        "grid_using_backup": True,
        "grid_backup_observed_at": 9999999999,
    }
    if transport == "mqtt":
        await ms.on_message("inverter/state", json.dumps(controller).encode())
    else:
        gateway.apply_snapshot(ms, {"inverter": controller})
    monkeypatch.setitem(ws._state, "mqtt_state", ms)
    monkeypatch.setattr(ha_client, "merge_overlay", lambda state: state)
    state = ws.build_payload()
    assert state["daily_stats"]["grid_energy"] == grid_energy
    assert state["grid_backup"] == backup
    assert state["grid_backup_observed_at"] == backup["measurement_time"]
    ms._merge_daemon_state({"uptime": 1})
    assert ms.get_state()["grid_backup_observed_at"] == backup["measurement_time"]
    ms.clear_daemon_state()
    assert ms.get_state()["grid_backup_observed_at"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "inf", "-inf"])
def test_invalid_notification_type_never_raises_or_becomes_alarm(value):
    assert notifications._as_i64(value) is None


def test_daily_stats_visibility_setting_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    settings_store.save_settings({"show_daily_stats": False})
    assert settings_store.load_settings()["show_daily_stats"] is False


def test_api_settings_never_returns_stored_credentials(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    settings_store.save_settings(
        {"mqtt_password": "private-mqtt-fixture", "ha_token": "private-ha-fixture"}
    )
    response = TestClient(server.app).post("/api/settings", json={"show_daily_stats": False})
    assert response.status_code == 200
    assert response.json()["settings"]["ha_token"] == "***"
    assert "private-" not in response.text
    assert settings_store.load_settings()["ha_token"] == "private-ha-fixture"


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "null",
        "https://testserver.evil",
        "https://user@testserver",
        "https://testserver/path",
        "https://testserver:bad",
    ],
)
def test_websocket_rejects_cross_origin_even_with_valid_secret(monkeypatch, origin):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(server, "DASHBOARD_SECRET", "secret")
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        TestClient(server.app).websocket_connect("/ws?token=secret", headers={"Origin": origin}),
    ):
        pass
    assert caught.value.code == 4403


@pytest.mark.parametrize(
    "origin", ["http://testserver", "https://testserver", "https://testserver:443"]
)
def test_websocket_origin_uses_actual_host_not_forwarded_authority(origin):
    from starlette.websockets import WebSocket

    socket = WebSocket(
        {
            "type": "websocket",
            "headers": [
                (b"host", b"testserver"),
                (b"origin", origin.encode()),
                (b"x-forwarded-host", b"evil.example"),
            ],
        },
        receive=AsyncMock(),
        send=AsyncMock(),
    )
    assert server._websocket_origin_allowed(socket)


@pytest.mark.parametrize(
    "key",
    [
        "show_daily_stats",
        "show_header_toggles",
        "show_ha_sensors",
        "show_ha_numbers",
        "show_batteries",
        "show_solar_production",
        "show_active_loads",
    ],
)
def test_desktop_visibility_keys_persist(key, tmp_path, monkeypatch):
    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    settings_store.save_settings({key: False})
    assert settings_store.load_settings()[key] is False


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Origin": "https://evil.example", "Content-Type": "text/plain"}, 403),
        ({"Origin": "https://evil.example", "Content-Type": "application/json"}, 403),
        (
            {
                "Origin": "https://testserver",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/json",
            },
            403,
        ),
        ({"Origin": "https://testserver", "Content-Type": "text/plain"}, 415),
    ],
)
def test_settings_cross_origin_or_plain_text_cannot_mutate_disk(
    tmp_path, monkeypatch, headers, code
):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    settings_store.save_settings({"show_daily_stats": True})
    from pathlib import Path

    path = Path(settings_store.settings_path())
    before = path.read_bytes()
    response = TestClient(server.app).post(
        "/api/settings", content='{"show_daily_stats":false}', headers=headers
    )
    assert response.status_code == code
    assert path.read_bytes() == before


def test_settings_failure_never_applies_runtime_or_reports_acceptance(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    before = ws.get_ui_settings()

    def failure(_patch):
        raise OSError("fixture unavailable volume")

    monkeypatch.setattr(settings_store, "save_settings", failure)
    response = TestClient(server.app, raise_server_exceptions=False).post(
        "/api/settings", json={"show_daily_stats": False}
    )
    assert response.status_code == 500
    assert ws.get_ui_settings() == before


@pytest.mark.parametrize("port", [True, False, 0, -1, 65536, 1883.0, "1883"])
def test_invalid_mqtt_port_does_not_replace_settings(tmp_path, monkeypatch, port):
    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    settings_store.save_settings({"mqtt_port": 1883})
    with pytest.raises(ValueError):
        settings_store.save_settings({"mqtt_port": port})
    assert settings_store.load_settings()["mqtt_port"] == 1883


def test_explicit_durable_settings_private_atomic_and_masked_preserving(tmp_path, monkeypatch):
    import stat

    path = tmp_path / "persistent" / "dashboard_settings.json"
    monkeypatch.setenv("INVERTER_DASHBOARD_SETTINGS_FILE", str(path))
    settings_store.save_settings({"ha_token": "private-fixture-token", "show_daily_stats": True})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    old = path.read_bytes()
    settings_store.save_settings({"ha_token": "***", "show_daily_stats": False})
    assert settings_store.load_settings()["ha_token"] == "private-fixture-token"
    assert settings_store.load_settings()["show_daily_stats"] is False
    assert path.read_bytes() != old
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr(
        settings_store.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("fixture"))
    )
    before = path.read_bytes()
    with pytest.raises(OSError):
        settings_store.save_settings({"show_daily_stats": True})
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.skipif(settings_store.os.name != "posix", reason="Directory fsync is POSIX-only")
def test_directory_sync_runs_after_replace_and_failure_is_reported(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv("INVERTER_DASHBOARD_SETTINGS_FILE", str(tmp_path / "settings.json"))
    calls = []
    real_fsync = settings_store.os.fsync
    real_replace = settings_store.os.replace

    def sync(fd):
        is_directory = stat.S_ISDIR(settings_store.os.fstat(fd).st_mode)
        calls.append("directory" if is_directory else "file")
        if is_directory:
            raise OSError("synthetic directory durability failure")
        real_fsync(fd)

    def replace(source, target):
        calls.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(settings_store.os, "fsync", sync)
    monkeypatch.setattr(settings_store.os, "replace", replace)
    with pytest.raises(OSError, match="durability"):
        settings_store.save_settings({"show_daily_stats": False})
    assert calls == ["file", "replace", "directory"]
    assert list(tmp_path.iterdir()) == [tmp_path / "settings.json"]


def test_directory_sync_skips_unsupported_windows_api(monkeypatch):
    monkeypatch.setattr(settings_store.os, "name", "nt")
    opened = []
    monkeypatch.setattr(settings_store.os, "open", lambda *_: opened.append(True))
    settings_store._sync_settings_directory("unused-fixture-parent")
    assert opened == []
