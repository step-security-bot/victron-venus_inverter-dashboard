"""
Configuration for Inverter Dashboard using pydantic-settings
"""

from urllib.parse import urlsplit

import httpx
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .push_subject import validate_push_subject


def validate_gateway_url(value: str) -> str:
    """Accept only an unambiguous HTTPS origin before attaching IGW credentials."""
    error = "GATEWAY_URL must be an HTTPS origin without userinfo, path, query, or fragment"
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(error)
    if any(character in value for character in ("\\", "?", "#", "@")):
        raise ValueError(error)
    try:
        url = httpx.URL(value)
        raw_path = urlsplit(value).path
    except (httpx.InvalidURL, ValueError):
        raise ValueError(error) from None
    if url.scheme != "https" or not url.is_absolute_url or not url.host:
        raise ValueError(error)
    if (
        url.userinfo
        or raw_path not in ("", "/")
        or (url.port is not None and not 1 <= url.port <= 65535)
    ):
        raise ValueError(error)
    return str(url).rstrip("/")


def validate_gateway_access_pair(client_id: str, client_secret: str) -> tuple[str, str]:
    """Native HTTPS needs no Access pair; public Access requires both fields."""
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if bool(client_id) != bool(client_secret):
        raise ValueError(
            "GATEWAY_ACCESS_CLIENT_ID and GATEWAY_ACCESS_CLIENT_SECRET must be set together"
        )
    return client_id, client_secret


class Config(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    # MQTT settings (Cerbo-direct / local-dev). Set "" for IGW-only (mp).
    # May coexist with GATEWAY_*; see gateway.choose_startup_source precedence.
    MQTT_HOST: str = "Cerbo"
    MQTT_PORT: int = 1883
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""
    MQTT_TLS: bool = False
    MQTT_CA_CERT: str = ""

    # MQTT auto-reconnect backoff (seconds)
    MQTT_RECONNECT_MIN: float = 1.0
    MQTT_RECONNECT_MAX: float = 60.0

    # Remote inverter-gateway (IGW) — same pattern as inverter-desktop.
    # When enabled with GATEWAY_URL, snapshot polling is available. If MQTT_HOST
    # is also set, MQTT is preferred when reachable (dual-path failover to IGW).
    GATEWAY_ENABLED: bool = False
    GATEWAY_URL: str = ""
    GATEWAY_ACCESS_CLIENT_ID: str = ""
    GATEWAY_ACCESS_CLIENT_SECRET: str = ""
    GATEWAY_API_TOKEN: str = ""
    GATEWAY_POLL_INTERVAL: float = 2.0

    @field_validator("GATEWAY_URL")
    @classmethod
    def _validate_gateway_url(cls, value: str) -> str:
        return validate_gateway_url(value) if value else ""

    @model_validator(mode="after")
    def _validate_gateway_settings(self):
        if self.GATEWAY_ENABLED and not self.GATEWAY_URL:
            raise ValueError("GATEWAY_ENABLED requires an HTTPS GATEWAY_URL")
        validate_gateway_access_pair(
            self.GATEWAY_ACCESS_CLIENT_ID, self.GATEWAY_ACCESS_CLIENT_SECRET
        )
        return self

    # Web server settings
    HOST: str = "127.0.0.1"
    WEB_PORT: int = 8080

    # Dashboard authentication — REQUIRED. Set via DASHBOARD_SECRET env var.
    # Protects WebSocket commands and /api/* management endpoints.
    # Generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    DASHBOARD_SECRET: str = ""

    # Opt-in Web Push. State must live on a dedicated persistent private volume.
    WEB_PUSH_ENABLED: bool = False
    WEB_PUSH_DATA_DIR: str = ""
    WEB_PUSH_SUBJECT: str = "https://github.com/victron-venus/inverter-dashboard"

    @model_validator(mode="after")
    def _validate_push_settings(self):
        if self.WEB_PUSH_ENABLED and not self.WEB_PUSH_DATA_DIR:
            raise ValueError("WEB_PUSH_ENABLED requires WEB_PUSH_DATA_DIR")
        validate_push_subject(self.WEB_PUSH_SUBJECT)
        return self

    # Self-update settings
    SELF_UPDATE_ENABLED: bool = False
    UPDATE_PIN: str = ""

    # Default inverter limits (used by websocket_handler)
    DEFAULT_POWER_MIN: int = -2300
    DEFAULT_POWER_MAX: int = 2250
    DEFAULT_LOOP_INTERVAL: float = 0.33

    # Console lines kept in memory
    CONSOLE_MAX_LINES: int = 50
    CONSOLE_SEND_LINES: int = 20

    # HA poll timeout
    HA_POLL_TIMEOUT: float = 20.0
    HA_REQUEST_TIMEOUT: float = 15.0

    # Native Cerbo telemetry. Set portal ID for reliable startup on a silent broker.
    # Empty enables passive discovery from native notifications or inverter/portal.
    # Water system — dbus-pump via Cerbo MQTT.
    # Instances must match dbus-pump's local_config.py.
    CERBO_PORTAL_ID: str = ""
    WATER_TANK_INSTANCE: int = 21
    WATER_PUMP_INSTANCE: int = 1
    WATER_VALVE_INSTANCE: int = 2

    # EV system — dbus-ev / dbus-evcharger on the configured or discovered portal.
    # Unset instances select the lowest connected device with usable telemetry.
    # An explicit instance pins that device, including when it is unavailable.
    EV_INSTANCE: int | None = None
    EVCHARGER_INSTANCE: int | None = None

    @field_validator("EV_INSTANCE", "EVCHARGER_INSTANCE", mode="before")
    @classmethod
    def _validate_ev_instance(cls, value):
        if value is None or value in ("", "auto"):
            return None
        if isinstance(value, bool) or float(value) < 0:
            raise ValueError("EV instance must be a non-negative integer or auto")
        return value

    # Camera events — Frigate MQTT topic (empty disables camera monitoring).
    CAMERA_TOPIC: str = ""

    # GitHub repository for updates
    GITHUB_REPO: str = "victron-venus/inverter-dashboard"

    @property
    def GITHUB_RAW_URL(self) -> str:
        return f"https://raw.githubusercontent.com/{self.GITHUB_REPO}/main"

    @field_validator(
        "SELF_UPDATE_ENABLED", "MQTT_TLS", "GATEWAY_ENABLED", "WEB_PUSH_ENABLED", mode="before"
    )
    @classmethod
    def _parse_bool(cls, v: str | bool) -> bool:
        if isinstance(v, bool):
            return v
        return v.lower() in ("1", "true", "yes")


config = Config()

# Module-level exports for backward compatibility
MQTT_HOST = config.MQTT_HOST
MQTT_PORT = config.MQTT_PORT
MQTT_USERNAME = config.MQTT_USERNAME
MQTT_PASSWORD = config.MQTT_PASSWORD
MQTT_TLS = config.MQTT_TLS
MQTT_CA_CERT = config.MQTT_CA_CERT
MQTT_RECONNECT_MIN = config.MQTT_RECONNECT_MIN
MQTT_RECONNECT_MAX = config.MQTT_RECONNECT_MAX
GATEWAY_ENABLED = config.GATEWAY_ENABLED
GATEWAY_URL = config.GATEWAY_URL
GATEWAY_ACCESS_CLIENT_ID = config.GATEWAY_ACCESS_CLIENT_ID
GATEWAY_ACCESS_CLIENT_SECRET = config.GATEWAY_ACCESS_CLIENT_SECRET
GATEWAY_API_TOKEN = config.GATEWAY_API_TOKEN
GATEWAY_POLL_INTERVAL = config.GATEWAY_POLL_INTERVAL
HOST = config.HOST
WEB_PORT = config.WEB_PORT
DASHBOARD_SECRET = config.DASHBOARD_SECRET
WEB_PUSH_ENABLED = config.WEB_PUSH_ENABLED
WEB_PUSH_DATA_DIR = config.WEB_PUSH_DATA_DIR
WEB_PUSH_SUBJECT = config.WEB_PUSH_SUBJECT
SELF_UPDATE_ENABLED = config.SELF_UPDATE_ENABLED
UPDATE_PIN = config.UPDATE_PIN
DEFAULT_POWER_MIN = config.DEFAULT_POWER_MIN
DEFAULT_POWER_MAX = config.DEFAULT_POWER_MAX
DEFAULT_LOOP_INTERVAL = config.DEFAULT_LOOP_INTERVAL
CONSOLE_MAX_LINES = config.CONSOLE_MAX_LINES
CONSOLE_SEND_LINES = config.CONSOLE_SEND_LINES
HA_POLL_TIMEOUT = config.HA_POLL_TIMEOUT
HA_REQUEST_TIMEOUT = config.HA_REQUEST_TIMEOUT
GITHUB_REPO = config.GITHUB_REPO
GITHUB_RAW_URL = config.GITHUB_RAW_URL
CERBO_PORTAL_ID = config.CERBO_PORTAL_ID
WATER_TANK_INSTANCE = config.WATER_TANK_INSTANCE
WATER_PUMP_INSTANCE = config.WATER_PUMP_INSTANCE
WATER_VALVE_INSTANCE = config.WATER_VALVE_INSTANCE
EV_INSTANCE = config.EV_INSTANCE
EVCHARGER_INSTANCE = config.EVCHARGER_INSTANCE
CAMERA_TOPIC = config.CAMERA_TOPIC
