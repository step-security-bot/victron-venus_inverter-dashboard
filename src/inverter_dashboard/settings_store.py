"""
Dashboard settings persistence (JSON file next to local_config).

Covers runtime-editable UI settings (section visibility + camera topic)
and connection overrides (MQTT host/port/credentials, HA URL/token) that
take effect at next startup — env < dashboard_settings.json < CLI flag.
Secrets are stored plaintext in the settings file (same trust level as
local_config.py); GET /api/settings masks them.
"""

import json
import logging
import os
import tempfile
from typing import Any

from . import config
from .config import CAMERA_TOPIC

logger = logging.getLogger(__name__)

# Keys the dashboard may read/write; everything else is rejected.
ALLOWED_KEYS = {
    "camera_topic": str,
    "show_daily_stats": bool,
    "show_header_toggles": bool,
    "show_ha_sensors": bool,
    "show_ha_numbers": bool,
    "show_batteries": bool,
    "show_solar_production": bool,
    "show_active_loads": bool,
    "show_ev": bool,
    "show_washer": bool,
    "show_dryer": bool,
    "show_dishwasher": bool,
    "show_home_section": bool,
    "show_ha_covers": bool,
    "show_ha_media": bool,
    "show_ha_scenes": bool,
    "show_ha_weather": bool,
    # Connection overrides (applied at startup; restart required)
    "mqtt_host": str,
    "mqtt_port": int,
    "mqtt_username": str,
    "mqtt_password": str,
    "ha_url": str,
    "ha_token": str,
}

# Settings that must be masked in API responses.
SECRET_KEYS = ("mqtt_password", "ha_token")

DEFAULTS: dict[str, Any] = {
    "camera_topic": CAMERA_TOPIC,
    "show_daily_stats": True,
    "show_header_toggles": True,
    "show_ha_sensors": True,
    "show_ha_numbers": True,
    "show_batteries": True,
    "show_solar_production": True,
    "show_active_loads": True,
    "show_ev": True,
    "show_washer": True,
    "show_dryer": True,
    "show_dishwasher": True,
    "show_home_section": True,
    "show_ha_covers": True,
    "show_ha_media": True,
    "show_ha_scenes": True,
    "show_ha_weather": True,
    "mqtt_host": config.MQTT_HOST,
    "mqtt_port": config.MQTT_PORT,
    "mqtt_username": config.MQTT_USERNAME,
    "mqtt_password": "",
    "ha_url": "",
    "ha_token": "",
}

# Connection keys → config module attribute (mqtt_*) or ha_client override (ha_*)
_CONFIG_ATTRS = {
    "mqtt_host": "MQTT_HOST",
    "mqtt_port": "MQTT_PORT",
    "mqtt_username": "MQTT_USERNAME",
    "mqtt_password": "MQTT_PASSWORD",
}


def settings_path() -> str:
    """dashboard_settings.json lives where local_config.py is looked up."""
    override = os.environ.get("INVERTER_DASHBOARD_SETTINGS_FILE", "").strip()
    if override:
        return os.path.abspath(override)
    env_dir = os.environ.get("INVERTER_DASHBOARD_CONFIG", "").strip()
    if env_dir:
        return os.path.join(env_dir, "dashboard_settings.json")
    repo_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(repo_root, "dashboard_settings.json")


def load_settings(mask_secrets: bool = False) -> dict[str, Any]:
    """Defaults overlaid with the persisted file (unknown/invalid entries ignored).

    mask_secrets=True replaces secret values with "***" (set vs empty only).
    """
    out = dict(DEFAULTS)
    try:
        with open(settings_path(), encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            for k, typ in ALLOWED_KEYS.items():
                if k in stored and _valid_setting(k, stored[k], typ):
                    out[k] = stored[k]
    except (OSError, json.JSONDecodeError):
        pass
    if mask_secrets:
        for k in SECRET_KEYS:
            out[k] = "***" if out.get(k) else ""
    return out


def apply_connection_overrides() -> int:
    """Apply persisted connection overrides to config + ha_client at startup.

    Only non-empty stored values win over env defaults. Returns count applied.
    """
    from . import ha_client

    applied = 0
    for key, attr in _CONFIG_ATTRS.items():
        val = load_settings().get(key)
        if val not in (None, "") and val != DEFAULTS.get(key):
            setattr(config, attr, val)
            applied += 1
    ha_url = load_settings().get("ha_url")
    ha_token = load_settings().get("ha_token")
    if ha_url or ha_token:
        ha_client.override_credentials(url=ha_url or None, token=ha_token or None)
        applied += 1
    if applied:
        logger.info("Applied %d connection override(s) from settings file", applied)
    return applied


def _valid_setting(key: str, value: Any, expected_type: type) -> bool:
    if not isinstance(value, expected_type):
        return False
    if key == "mqtt_port":
        return not isinstance(value, bool) and 1 <= value <= 65535
    return not (key in SECRET_KEYS and value == "***")


def _sync_settings_directory(parent: str) -> None:
    """Persist the rename on POSIX; Windows does not expose directory fsync."""
    if os.name != "posix":
        return
    directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_private_settings(merged: dict[str, Any]) -> None:
    path = settings_path()
    parent = os.path.dirname(path)
    if os.environ.get("INVERTER_DASHBOARD_SETTINGS_FILE", "").strip():
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".dashboard-settings-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(merged, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_settings_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate patch against ALLOWED_KEYS, merge-write, return new settings.

    Raises ValueError on unknown keys or wrong types.
    """
    clean: dict[str, Any] = {}
    for k, v in patch.items():
        if k not in ALLOWED_KEYS:
            raise ValueError(f"unknown setting: {k}")
        if k in SECRET_KEYS and v == "***":
            continue  # Round-tripping the masked API value preserves the secret.
        if not _valid_setting(k, v, ALLOWED_KEYS[k]):
            # API contract: bad type is a 400 (ValueError), not a TypeError
            raise ValueError(f"setting {k} must be {ALLOWED_KEYS[k].__name__}")
        clean[k] = v

    merged = load_settings()
    merged.update(clean)
    _write_private_settings(merged)
    logger.info("Saved settings: %s", sorted(clean))
    return merged
