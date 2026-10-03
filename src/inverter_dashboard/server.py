#!/usr/bin/env python3
# pylint: disable=too-many-lines
"""
Remote Web Dashboard for Inverter Control

Live Cerbo tiles via LAN MQTT (local/dev) or remote inverter-gateway (IGW)
snapshot polling — same dual-transport idea as inverter-desktop. Serves the
dashboard over WebSocket/HTTP.
"""

import argparse
import asyncio
import fnmatch
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from aiomqtt import Client, MqttError, TLSParameters
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, gateway, ha_client, notifications, settings_store, websocket_handler
from .cerbo import (
    CERBO_KINDS,
    CERBO_OWNED_KEYS,
    KEEPALIVE_INTERVAL_SECS,
    NATIVE_SECTION_KEYS,
    CerboOverlayMixin,
    number,
)
from .config import DASHBOARD_SECRET, WEB_PORT
from .version import VERSION, SelfUpdateDisabled, check_latest_version, download_and_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _capitalize(s: str) -> str:
    return s[:1].upper() + s[1:] if s else ""


def _split_camel(s: str) -> list[str]:
    words: list[str] = []
    current = ""
    for ch in s:
        if ch.isupper() and current and current[-1].islower():
            words.append(current)
            current = ch
        elif ch in ("_", "-", " "):
            if current:
                words.append(current)
            current = ""
        else:
            current += ch
    if current:
        words.append(current)
    return words


def pretty_alarm_name(name: str) -> str:
    """'HighCellVoltage' / 'high_cell_voltage' -> 'High Cell Voltage'"""
    return " ".join(_capitalize(w) for w in _split_camel(name))


def pretty_service_name(service: str) -> str:
    """'battery_512' -> 'Battery 512', 'vebus' -> 'Vebus'"""
    name, _, inst = service.rpartition("_")
    if name and inst.isdigit():
        return f"{_capitalize(name)} {inst}"
    return _capitalize(service)


class MqttState(CerboOverlayMixin):
    """Encapsulated MQTT state."""

    NOTIFICATIONS_MAX = 100

    def __init__(self) -> None:
        self.current_state: dict[str, Any] = {}
        self.notifications: list[dict[str, Any]] = []
        # Active loads (Cerbo acload / dbus-emporia-vue): instance -> watts / names
        self._acload_names: dict[str, str] = {}
        self._acload_powers: dict[str, float] = {}
        self._acload_product_names: dict[str, str] = {}
        # Discovered PV inverters keyed by GX instance: {power, voltage, current, name}
        self._pv_inverters: dict[str, dict[str, Any]] = {}
        # Cerbo device maps (same durable sources as inverter-desktop)
        self._batteries: dict[str, dict[str, Any]] = {}
        self._chargers: dict[str, dict[str, Any]] = {}
        self._system: dict[str, dict[str, Any]] = {}
        self._vebus: dict[str, dict[str, Any]] = {}
        self._portal_id: str = config.CERBO_PORTAL_ID or ""
        self._init_cerbo()
        self.gateway_capabilities: dict[str, Any] = {}
        self._daemon_keys: set[str] = set()
        self._daemon_received_at: float | None = None
        self._alarm_values: dict[str, int] = {}
        # Venus-platform GUIv2 notification slots (desktop parity)
        self._platform_slots: dict[tuple[str, int], dict[str, Any]] = {}
        self._platform_seen: bool = False
        # Local dismiss sticky for non-platform ids (platform uses Cerbo ack)
        self._dismissed_ids: set[str] = set()
        self.camera_event: dict[str, Any] | None = None
        self._on_state_update: Callable | None = None
        # Optional: called when portal ID is discovered via inverter/portal
        self._on_portal: Callable | None = None

    def set_state_callback(self, callback: Callable) -> None:
        """Set callback to be called when state updates"""
        self._on_state_update = callback

    def set_portal_callback(self, callback: Callable) -> None:
        """Called when a portal is learned from native notifications or legacy discovery."""
        self._on_portal = callback

    async def _emit(self) -> None:
        if self._on_state_update:
            await self._on_state_update()

    def _merge_daemon_state(self, incoming: dict[str, Any]) -> None:
        """Non-destructive merge of slim inverter/state into current_state.

        EV, water, Active Loads and bank SoC always belong to native Cerbo. Other
        Cerbo-owned live tiles are never taken from the daemon once we have
        Cerbo overlays (or when the slim payload simply omits them). Missing
        keys must not clear previously known values — that caused Active Loads
        to flash then disappear.
        """
        self._daemon_received_at = time.monotonic()
        self._daemon_keys.update(incoming.keys() - NATIVE_SECTION_KEYS)
        for key, value in incoming.items():
            if key in NATIVE_SECTION_KEYS:
                continue
            if key in CERBO_OWNED_KEYS and self._cerbo_has_overlay(key):
                continue
            if key == "booleans":
                if not isinstance(value, dict):
                    self.current_state[key] = None
                    continue
                coerced = dict(self.current_state.get("booleans") or {})
                for bk, bv in value.items():
                    coerced[bk] = websocket_handler.control_boolean(bv)
                self.current_state[key] = coerced
                continue
            self.current_state[key] = value
        # Re-apply durable Cerbo maps so slim ticks cannot blank live tiles.
        self._apply_cerbo_overlays()

    def clear_daemon_state(self) -> None:
        """Invalidate controller observations without inventing disabled flags."""
        for key in self._daemon_keys:
            self.current_state[key] = None
        self._daemon_received_at = None
        self._apply_cerbo_overlays()

    def controller_available(self) -> bool:
        if self._daemon_received_at is None:
            return False
        if time.monotonic() - self._daemon_received_at > 120:
            self.clear_daemon_state()
            return False
        return True

    def _cerbo_has_overlay(self, key: str) -> bool:
        return key in self._cerbo_claimed_keys

    async def _discover_portal(self, portal: str) -> None:
        # Never let a foreign publisher override a configured/discovered site.
        if (
            self._portal_id
            or not portal
            or any(c in "/+#" or c.isspace() or ord(c) == 0 for c in portal)
        ):
            return
        if self._on_portal:
            await self._on_portal(portal)
        self._portal_id = portal
        logger.info("Discovered Cerbo portal ID: %s", portal)

    async def on_message(self, topic: str, payload: bytes) -> None:
        """Process incoming MQTT message"""
        try:
            if topic.startswith("N/"):
                parts = topic.split("/")
                if len(parts) < 3:
                    return
                # Discovery can come from system Serial, heartbeat, or an
                # already streaming native leaf. Selection then stays fixed.
                if (
                    not self._portal_id
                    and payload
                    and (len(parts) >= 5 or parts[2] in ("heartbeat", "keepalive"))
                ):
                    try:
                        discovery = json.loads(payload)
                    except (ValueError, UnicodeDecodeError):
                        return
                    value = discovery.get("value") if isinstance(discovery, dict) else None
                    if (isinstance(value, str) and value.strip()) or number(value) is not None:
                        await self._discover_portal(parts[1])
                if parts[1] != self._portal_id:
                    return
            if topic == "inverter/state":
                data = json.loads(payload.decode()) if payload else None
                if data is None:
                    self.clear_daemon_state()
                    await self._emit()
                if isinstance(data, dict):
                    self._merge_daemon_state(data)
                    await self._emit()

            elif topic == "inverter/portal":
                await self._discover_portal(payload.decode().strip().strip('"'))

            elif topic == "inverter/notifications":
                self.push_notification(json.loads(payload.decode()))
                if self._on_state_update:
                    await self._on_state_update()

            elif "/platform/" in topic and "/Notifications/" in topic:
                changed = self.handle_platform_notification(topic, payload)
                if changed and self._on_state_update:
                    await self._on_state_update()

            elif "/Alarms/" in topic:
                # Desktop suppresses raw Alarms once platform Notifications arrive.
                if self._platform_seen:
                    return
                changed = self.handle_alarm(topic, payload)
                if changed and self._on_state_update:
                    await self._on_state_update()

            elif config.CAMERA_TOPIC and fnmatch.fnmatch(
                topic, config.CAMERA_TOPIC.replace("+", "*")
            ):
                self.handle_camera_event(payload)
                if self.camera_event and self._on_state_update:
                    await self._on_state_update()

            elif topic.startswith("N/"):
                if self._handle_cerbo_device(topic, payload):
                    await self._emit()
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.exception("MQTT message parse error")
        except Exception:
            logger.exception("MQTT message error")

    def push_notification(self, data: Any) -> None:
        """Upsert a notification (MqttNotification shape — desktop / alert-bridge)."""
        notifications.mqtt_push_notification(self, data)

    def _upsert_notification(self, notif: dict[str, str]) -> None:
        notifications.mqtt_upsert_notification(self, notif)

    def _remove_notification_id(self, nid: str) -> bool:
        return notifications.mqtt_remove_notification_id(self, nid)

    def dismiss_notification(self, nid: str) -> bool:
        """User dismissed banner (X). Sticky for non-platform ids until a fresh id."""
        return notifications.mqtt_dismiss_notification(self, nid)

    def sync_platform_from_snapshot(self, platform_leaves: dict[str, Any]) -> bool:
        """IGW: rebuild platform banners from snapshot ``platform`` map."""
        return notifications.mqtt_sync_platform_from_snapshot(self, platform_leaves)

    def sync_alarms_from_snapshot(self, snap: dict[str, Any]) -> bool:
        """IGW fallback: map battery/vebus Alarms/* when platform unseen."""
        return notifications.mqtt_sync_alarms_from_snapshot(
            self, snap, pretty_service_name, pretty_alarm_name
        )

    def handle_platform_notification(self, topic: str, payload: bytes) -> bool:
        """LAN MQTT: N/<portal>/platform/<inst>/Notifications/<slot>/<Field>."""
        return notifications.mqtt_handle_platform_notification(self, topic, payload)

    def handle_alarm(self, topic: str, payload: bytes) -> bool:
        """Track a Victron alarm topic (value 0/1/2); emit/clear on transition.

        Returns True when the notification list changed.
        """
        try:
            val = json.loads(payload.decode()).get("value")
        except (ValueError, AttributeError):
            return False
        numeric = number(val)
        if val is not None and numeric is None:
            return False
        value = int(numeric) if numeric is not None else 0
        prev = self._alarm_values.get(topic, 0)
        if prev == value:
            return False
        self._alarm_values[topic] = value

        nid = f"victron-{topic}"
        if value not in (1, 2):
            # 0 = cleared: drop matching banner notifications
            before = len(self.notifications)
            self.notifications = [n for n in self.notifications if n["id"] != nid]
            return len(self.notifications) != before

        parts = topic.split("/")
        service = parts[2] if len(parts) > 4 else "device"
        if len(parts) > 5 and parts[3] != "Alarms":
            service = f"{service}_{parts[3]}"
        alarm_name = parts[-1]
        level = "alarm" if value == 2 else "warning"
        state_txt = "Alarm" if value == 2 else "Warning"
        self.push_notification(
            {
                "id": nid,
                "level": level,
                "title": pretty_service_name(service),
                "body": f"{pretty_alarm_name(alarm_name)}: {state_txt}",
                "source": "victron",
            }
        )
        return True

    def _handle_acload(self, topic: str, payload: bytes) -> bool:
        return self._handle_cerbo_device(topic, payload)

    def _handle_pvinverter(self, topic: str, payload: bytes) -> bool:
        return self._handle_cerbo_device(topic, payload)

    def handle_water(self, topic: str, payload: bytes) -> None:
        if self._portal_id:
            self._handle_cerbo_device(topic, payload)

    def handle_ev(self, topic: str, payload: bytes) -> None:
        if self._portal_id:
            self._handle_cerbo_device(topic, payload)

    def get_state(self) -> dict[str, Any]:
        """Get current state"""
        self.controller_available()
        return self.current_state

    def get_notifications(self) -> list[dict[str, Any]]:
        """Get notification list (inverter-control pushes + alarm transitions)."""
        return self.notifications

    def handle_camera_event(self, payload: bytes) -> None:
        """Store the latest camera event (desktop CameraEvent shape: {agent_name, video_url, timestamp})."""
        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = payload.decode(errors="replace")
        if isinstance(data, dict):
            self.camera_event = {
                "camera": str(data.get("agent_name") or "Camera"),
                "url": str(data.get("video_url") or ""),
                "ts": str(data.get("timestamp") or ""),
            }
        else:
            # Raw string payload treated as a direct stream/snapshot URL
            self.camera_event = {"camera": "Camera", "url": str(data), "ts": ""}


@dataclass
class AppState:
    """Application state container."""

    mqtt_state: MqttState | None = None
    mqtt_client: Client | None = None
    mqtt_tasks: list[asyncio.Task] = None
    mqtt_connected: bool = False
    mqtt_reconnects: int = 0
    # IGW (inverter-gateway) remote snapshot poller
    gateway_connected: bool = False
    gateway_polls: int = 0
    gateway_errors: int = 0
    data_source: str = "none"  # "mqtt" | "igw" | "none"

    def __post_init__(self):
        if self.mqtt_tasks is None:
            self.mqtt_tasks = []


# Module-level app state
_app_state = AppState()
websocket_handler.set_app_state(_app_state)


def _verify_secret(request: Request, token: str | None = None) -> None:
    """Verify DASHBOARD_SECRET against Authorization header or query param.

    Raises HTTPException(401/403) on failure.
    """
    if not DASHBOARD_SECRET:
        return

    if token and token == DASHBOARD_SECRET:
        return

    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == DASHBOARD_SECRET:
        return

    if not token and not auth:
        raise HTTPException(status_code=401, detail="missing secret")

    raise HTTPException(status_code=403, detail="invalid secret")


def _secret_authorized(request: Request, token: str | None = None) -> bool:
    """Return True when DASHBOARD_SECRET is unset or the request presents it."""
    if not DASHBOARD_SECRET:
        return True
    if token and token == DASHBOARD_SECRET:
        return True
    auth = request.headers.get("authorization", "")
    return bool(auth.startswith("Bearer ") and auth[7:] == DASHBOARD_SECRET)


def _make_mqtt_client() -> Client:
    """Create a fresh MQTT client from config (a closed client cannot be reused)."""
    # NB: tls_insecure must not be passed without an SSL context - paho raises
    # ValueError and the message-loop task would die silently at startup.
    client_kwargs: dict[str, Any] = {
        "hostname": config.MQTT_HOST,
        "port": config.MQTT_PORT,
        # Random suffix keeps the client ID unique so a second instance or a
        # stale broker session cannot kick this client off the broker.
        "identifier": f"inverter-dashboard-{os.urandom(3).hex()}",
        "username": config.MQTT_USERNAME,
        "password": config.MQTT_PASSWORD,
    }
    if config.MQTT_TLS:
        client_kwargs["tls_params"] = TLSParameters(ca_certs=config.MQTT_CA_CERT or None)
    return Client(**client_kwargs)


async def _subscribe_topics(client: Client, portal_id: str | None = None) -> None:
    """Subscribe first, then request a complete Venus publish for this session."""
    for topic in ("inverter/state", "inverter/portal", "inverter/notifications"):
        await client.subscribe(topic)
    if config.CAMERA_TOPIC:
        await client.subscribe(config.CAMERA_TOPIC)
    portal = portal_id or config.CERBO_PORTAL_ID
    if portal:
        await _subscribe_portal_topics(client, portal)
    else:
        # dbus-flashmq does not retain native telemetry. A silent broker needs
        # an explicitly configured portal; never publish a wildcard R/ topic.
        for topic in ("N/+/system/+/Serial", "N/+/heartbeat", "N/+/keepalive"):
            await client.subscribe(topic)
        logger.info(
            "Awaiting Cerbo portal discovery; configure CERBO_PORTAL_ID for a silent broker"
        )


async def _subscribe_portal_topics(client: Client, portal: str) -> None:
    """Use portal-scoped filters (also valid with VRM MQTT ACLs)."""
    for kind in CERBO_KINDS:
        await client.subscribe(f"N/{portal}/{kind}/+/#")
    await client.subscribe(f"N/{portal}/platform/+/Notifications/#")
    await client.subscribe(f"N/{portal}/+/Alarms/#")
    await client.subscribe(f"N/{portal}/+/+/Alarms/#")
    # Empty payload requests the full tree AFTER all subscriptions exist.
    await client.publish(f"R/{portal}/keepalive", "", qos=0)


async def _keepalive_loop(client: Client, portal_getter) -> None:
    """Maintain streaming without triggering full republishes every 45 seconds."""
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL_SECS)
        portal = portal_getter()
        if portal:
            try:
                await client.publish(
                    f"R/{portal}/keepalive", '{"keepalive-options":["suppress-republish"]}', qos=0
                )
            except MqttError as exc:
                logger.warning("Cerbo keepalive failed; retrying on next interval: %s", exc)


def _next_backoff(delay: float) -> float:
    """Double the reconnect delay, capped at MQTT_RECONNECT_MAX."""
    return min(delay * 2, config.MQTT_RECONNECT_MAX)


def _start_gateway_client():
    """Poll inverter-gateway /v1/snapshot (no Cerbo MQTT client)."""

    _app_state.mqtt_state = MqttState()
    _app_state.mqtt_client = None
    _app_state.data_source = "igw"
    _app_state.mqtt_state.set_state_callback(websocket_handler.broadcast_state)
    websocket_handler.set_mqtt_state(_app_state.mqtt_state)
    if config.CERBO_PORTAL_ID:
        _app_state.mqtt_state._portal_id = config.CERBO_PORTAL_ID

    async def _apply_and_emit(snap: dict[str, Any]) -> None:
        ms = _app_state.mqtt_state
        if ms is not None:
            gateway.apply_snapshot(ms, snap)
            await ms._emit()

    async def _status_and_emit() -> None:
        ms = _app_state.mqtt_state
        if ms is not None and not _app_state.gateway_connected:
            ms.clear_daemon_state()
        await websocket_handler.broadcast_state()

    task = asyncio.create_task(
        gateway.gateway_poll_loop(_app_state, _apply_and_emit, _status_and_emit)
    )
    _app_state.mqtt_tasks.append(task)


def _start_mqtt_client():
    """Start MQTT client connection and message loop with auto-reconnect."""
    _app_state.mqtt_state = MqttState()
    _app_state.mqtt_client = _make_mqtt_client()
    _app_state.data_source = "mqtt"
    _app_state.mqtt_state.set_state_callback(websocket_handler.broadcast_state)
    websocket_handler.set_mqtt_state(_app_state.mqtt_state)

    async def mqtt_connect_and_loop():
        delay = max(config.MQTT_RECONNECT_MIN, 0.1)
        while True:
            keepalive_task: asyncio.Task | None = None
            try:
                async with _app_state.mqtt_client:
                    _app_state.mqtt_connected = True
                    logger.info("Connected to MQTT broker")
                    ms = _app_state.mqtt_state
                    if ms is None:
                        raise RuntimeError("MQTT state not initialized")

                    async def _on_portal(portal: str) -> None:
                        client = _app_state.mqtt_client
                        if client is not None:
                            await _subscribe_portal_topics(client, portal)

                    ms.set_portal_callback(_on_portal)
                    ms.clear_daemon_state()
                    ms.clear_cerbo_state()
                    await _subscribe_topics(_app_state.mqtt_client, ms._portal_id or None)
                    logger.info("Subscribed to MQTT topics")
                    keepalive_task = asyncio.create_task(
                        _keepalive_loop(
                            _app_state.mqtt_client,
                            lambda: (
                                _app_state.mqtt_state._portal_id if _app_state.mqtt_state else ""
                            ),
                        )
                    )
                    delay = max(config.MQTT_RECONNECT_MIN, 0.1)
                    async for message in _app_state.mqtt_client.messages:
                        await ms.on_message(message.topic.value, message.payload)
            except asyncio.CancelledError:
                raise
            except MqttError:
                _app_state.mqtt_reconnects += 1
                logger.warning(
                    "MQTT connection lost — reconnecting in %.1fs (reconnect #%d)",
                    delay,
                    _app_state.mqtt_reconnects,
                )
                if gateway.dual_path_enabled() and gateway.gateway_configured():
                    await _failover_to_igw("MQTT connection lost")
                    return
            except Exception:  # pylint: disable=broad-except
                _app_state.mqtt_reconnects += 1
                logger.exception("Unexpected error in MQTT loop — retrying in %.1fs", delay)
                if gateway.dual_path_enabled() and gateway.gateway_configured():
                    await _failover_to_igw("MQTT loop error")
                    return
            finally:
                _app_state.mqtt_connected = False
                if _app_state.mqtt_state is not None:
                    _app_state.mqtt_state.clear_daemon_state()
                    _app_state.mqtt_state.clear_cerbo_state()
                    await _app_state.mqtt_state._emit()
                if keepalive_task is not None:
                    keepalive_task.cancel()
                    await asyncio.gather(keepalive_task, return_exceptions=True)
            await asyncio.sleep(delay)
            delay = _next_backoff(delay)
            _app_state.mqtt_client = _make_mqtt_client()

    mqtt_task = asyncio.create_task(mqtt_connect_and_loop())
    _app_state.mqtt_tasks.append(mqtt_task)


def _start_ha_polling():
    """Start HA polling task if direct mode enabled."""
    if ha_client.is_direct_mode():
        return asyncio.create_task(ha_client.ha_poll_loop())
    return None


def _start_version_check():
    """Start background version check task."""

    async def _bg_version_check():
        latest = await check_latest_version()
        if latest:
            websocket_handler.set_latest_version(latest)

    return asyncio.create_task(_bg_version_check())


async def _shutdown_tasks(ha_task):
    """Cancel and await all background tasks."""
    cancelled = False
    if ha_task:
        ha_task.cancel()
        try:
            await ha_task
        except asyncio.CancelledError:
            cancelled = True

    for task in _app_state.mqtt_tasks:
        task.cancel()
    if _app_state.mqtt_tasks:
        await asyncio.gather(*_app_state.mqtt_tasks, return_exceptions=True)

    if cancelled:
        raise asyncio.CancelledError


async def _shutdown_mqtt_client():
    """Clear MQTT client reference.

    The connection itself is closed by the message-loop task's ``async with``
    block when the task is cancelled (see ``_shutdown_tasks``).
    """
    _app_state.mqtt_connected = False
    _app_state.mqtt_client = None


async def _cancel_data_source_tasks() -> None:
    """Stop MQTT loop and/or IGW poller (exclusive switch).

    Skips ``asyncio.current_task()`` so a dual-path failover/recovery coroutine
    can replace sibling transports without cancelling itself.
    """
    current = asyncio.current_task()
    tasks = [t for t in _app_state.mqtt_tasks if t is not current]
    _app_state.mqtt_tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _app_state.mqtt_connected = False
    _app_state.gateway_connected = False
    _app_state.mqtt_client = None


async def _failover_to_igw(reason: str) -> None:
    """Dual-path: drop Cerbo MQTT and run IGW exclusively."""
    if not gateway.dual_path_enabled() or gateway.active_source() == "igw":
        return
    if not gateway.gateway_configured():
        return
    logger.warning("Failing over to IGW (%s)", reason)
    await _cancel_data_source_tasks()
    gateway.set_active_source("igw", dual_path=True)
    logger.info(
        "Data source: IGW (%s) — Cerbo MQTT unreachable / offline",
        config.GATEWAY_URL.rstrip("/"),
    )
    _start_gateway_client()
    _app_state.mqtt_tasks.append(asyncio.create_task(_mqtt_recovery_loop()))


async def _mqtt_connect_watchdog() -> None:
    """If dual-path MQTT never ConnAcks, fall over to IGW."""
    await asyncio.sleep(gateway.MQTT_CONNECT_WATCHDOG_SECS)
    if (
        gateway.dual_path_enabled()
        and gateway.active_source() == "mqtt"
        and not _app_state.mqtt_connected
    ):
        await _failover_to_igw("MQTT connect watchdog — no connection")


async def _mqtt_recovery_loop() -> None:
    """While on IGW in dual-path mode, periodically probe Cerbo MQTT to recover."""
    while gateway.dual_path_enabled() and gateway.active_source() == "igw":
        await asyncio.sleep(gateway.MQTT_RECOVERY_PROBE_SECS)
        if not gateway.mqtt_configured():
            continue
        if not await gateway.probe_mqtt_reachable():
            continue
        logger.info(
            "Cerbo MQTT reachable again (%s:%s) — recovering from IGW",
            config.MQTT_HOST,
            config.MQTT_PORT,
        )
        await _cancel_data_source_tasks()
        gateway.set_active_source("mqtt", dual_path=True)
        logger.info("Data source: Cerbo MQTT (%s:%s)", config.MQTT_HOST, config.MQTT_PORT)
        _start_mqtt_client()
        _app_state.mqtt_tasks.append(asyncio.create_task(_mqtt_connect_watchdog()))
        return


async def _select_and_start_data_source() -> None:
    """MQTT-first when reachable; IGW otherwise — mirrors inverter-desktop."""
    mqtt_ok = gateway.mqtt_configured()
    igw_ok = gateway.gateway_configured()
    dual = mqtt_ok and igw_ok
    mqtt_reachable = False
    if dual:
        mqtt_reachable = await gateway.probe_mqtt_reachable()
    elif mqtt_ok:
        # Single-transport MQTT: start client even if probe would fail (reconnect loop).
        mqtt_reachable = True

    startup = gateway.choose_startup_source(
        mqtt_configured=mqtt_ok,
        igw_configured=igw_ok,
        mqtt_reachable=mqtt_reachable if dual else mqtt_ok,
    )
    gateway.set_active_source(startup, dual_path=dual)

    if startup == "mqtt":
        logger.info(
            "Data source: Cerbo MQTT (%s:%s)%s",
            config.MQTT_HOST,
            config.MQTT_PORT,
            " (IGW available as fallback)" if dual else "",
        )
        _start_mqtt_client()
        if dual:
            _app_state.mqtt_tasks.append(asyncio.create_task(_mqtt_connect_watchdog()))
    elif startup == "igw":
        logger.info(
            "Data source: IGW (%s)%s",
            config.GATEWAY_URL.rstrip("/"),
            " — Cerbo MQTT unreachable" if dual else "",
        )
        _start_gateway_client()
        if dual:
            _app_state.mqtt_tasks.append(asyncio.create_task(_mqtt_recovery_loop()))
    else:
        logger.warning("No data source configured (set MQTT_HOST or GATEWAY_ENABLED+GATEWAY_URL)")
        gateway.set_active_source("none", dual_path=False)
        _app_state.mqtt_state = MqttState()
        websocket_handler.set_mqtt_state(_app_state.mqtt_state)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan handler"""
    # Startup
    ha_client.load_config()
    settings_store.apply_connection_overrides()  # file wins over env; CLI applied later wins over file
    websocket_handler.set_ui_settings(settings_store.load_settings())
    await _select_and_start_data_source()
    ha_task = _start_ha_polling()
    _start_version_check()

    yield

    # Shutdown
    await _shutdown_tasks(ha_task)
    await _shutdown_mqtt_client()


app = FastAPI(title="Inverter Dashboard", lifespan=lifespan)


def _spa_static_candidates() -> list[Path]:
    """Package-adjacent static/, plus Docker COPY at /app/src (non-editable install)."""
    candidates = [Path(__file__).parent / "static"]
    docker_static = Path("/app/src/inverter_dashboard/static")
    if docker_static not in candidates:
        candidates.append(docker_static)
    return candidates


def _resolve_spa_root() -> Path | None:
    """Prefer static/dist (export_dist.sh), else static/ (docker-publish image layout)."""
    for static_dir in _spa_static_candidates():
        dist_dir = static_dir / "dist"
        if (dist_dir / "index.html").is_file():
            return dist_dir
        if (static_dir / "index.html").is_file():
            return static_dir
    return None


# Mount Vue SPA assets if available (higher priority than fallback routes)
def _mount_vue_dist():
    """Mount SPA static files for both dist/ and flat static/ layouts."""
    spa_root = _resolve_spa_root()
    if spa_root is None:
        return
    # Legacy mount: some tooling expects assets under /static/...
    app.mount("/static", StaticFiles(directory=str(spa_root)), name="vue_dist")
    # Vite builds reference absolute /assets/... paths in index.html
    assets_dir = spa_root / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="vue_assets")
    logger.info("Mounted Vue SPA from %s", spa_root)


_mount_vue_dist()


# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, token: str | None = None):
    """Serve Vue SPA from static/dist or static/, or 404 if not built"""
    try:
        _verify_secret(request, token)
    except HTTPException as exc:
        return HTMLResponse(
            "<h1>Inverter Dashboard</h1>"
            f"<p>{exc.detail}. Append <code>?token=YOUR_SECRET</code> to the URL or send "
            "an <code>Authorization: Bearer</code> header.</p>",
            status_code=exc.status_code,
        )
    spa_root = _resolve_spa_root()
    if spa_root is not None:
        return (spa_root / "index.html").read_text()
    return HTMLResponse(
        "<h1>Inverter Dashboard</h1><p>Vue SPA not built. Run <code>npm run build</code> in inverter-dashboard-vue and copy dist/ to static/.</p>",
        status_code=404,
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates"""
    token = websocket.query_params.get("token")
    if DASHBOARD_SECRET and token != DASHBOARD_SECRET:
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket_handler.handle_websocket(websocket, _app_state)


@app.get(
    "/api/settings",
    responses={401: {"description": "Missing secret"}, 403: {"description": "Invalid secret"}},
)
async def api_settings_get(request: Request):
    """Current dashboard settings (section visibility, camera topic)."""
    _verify_secret(request)
    return {"ok": True, "settings": settings_store.load_settings(mask_secrets=True)}


@app.post(
    "/api/settings",
    responses={
        400: {"description": "Invalid settings"},
        401: {"description": "Missing secret"},
        403: {"description": "Invalid secret"},
    },
)
async def api_settings_post(request: Request):
    """Persist settings; section-visibility keys apply on next broadcast."""
    _verify_secret(request)
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        saved = settings_store.save_settings(patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    websocket_handler.set_ui_settings(saved)
    return {"ok": True, "settings": saved}


@app.get("/api/state")
async def api_state(request: Request, response: Response, token: str | None = None):
    """Health always; live Cerbo/IGW tiles only when authorized (or secret unset).

    Unauthenticated probes (Docker HEALTHCHECK) get connectivity fields only.
    SPA HTTP fallback must pass ?token= or Authorization when DASHBOARD_SECRET is set.
    """
    # The same URL can return health-only or authenticated live state.
    response.headers["Cache-Control"] = "no-store"
    raw = _app_state.mqtt_state.get_state() if _app_state.mqtt_state else {}
    health: dict[str, Any] = {
        "ok": True,
        "dashboard_version": VERSION,
        "control_version": raw.get("version"),
        "has_mqtt_state": bool(raw),
        "data_source": _app_state.data_source,
        "mqtt_connected": _app_state.mqtt_connected,
        "mqtt_reconnects": _app_state.mqtt_reconnects,
        "gateway_connected": _app_state.gateway_connected,
        "gateway_polls": _app_state.gateway_polls,
        "gateway_errors": _app_state.gateway_errors,
        "gateway_url": (config.GATEWAY_URL or "").rstrip("/") or None,
    }
    if not _secret_authorized(request, token):
        return health

    # Prefer the same filtered payload WS clients get (HA overlay + allowlist).
    live: dict[str, Any] = {}
    if _app_state.mqtt_state is not None:
        try:
            live = websocket_handler.build_payload()
        except Exception:  # pylint: disable=broad-except
            live = {}
    health["control_version"] = raw.get("version") or live.get("version")
    return {**live, **health}


@app.post(
    "/api/check-update",
    responses={401: {"description": "Missing secret"}, 403: {"description": "Invalid secret"}},
)
async def api_check_update(request: Request):
    """Check for updates"""
    _verify_secret(request)
    latest = await check_latest_version()
    if latest:
        websocket_handler.set_latest_version(latest)
    return {"current": VERSION, "latest": latest}


@app.post(
    "/api/update",
    responses={401: {"description": "Missing secret"}, 403: {"description": "Invalid secret"}},
)
async def api_update(request: Request):
    """Self-update: download latest from GitHub and restart"""
    _verify_secret(request)
    logger.info("Update requested...")

    try:
        success, result = download_and_update()
    except SelfUpdateDisabled:
        return JSONResponse(
            {"error": "self-update is disabled (set SELF_UPDATE_ENABLED=true)"}, status_code=403
        )

    if success:
        # Schedule restart via container supervisor (PID 1 reaps this process)
        asyncio.get_running_loop().call_later(1, lambda: os._exit(0))
        return {
            "status": "updated",
            "version": result,
            "message": f"Updated to v{result}, restarting...",
        }
    return JSONResponse({"error": result}, status_code=500)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Remote Web Dashboard for Inverter Control")
    parser.add_argument("--mqtt-host", default=None, help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=None, help="MQTT broker port")
    parser.add_argument("--port", type=int, default=WEB_PORT, help="Web server port")
    parser.add_argument("--ssl-cert", help="SSL certificate file")
    parser.add_argument("--ssl-key", help="SSL key file")
    args = parser.parse_args()

    # Update config: settings-file overrides already applied via lifespan;
    # explicit CLI flags win over both.
    if args.mqtt_host is not None:
        config.MQTT_HOST = args.mqtt_host
    if args.mqtt_port is not None:
        config.MQTT_PORT = args.mqtt_port

    proto = "https" if args.ssl_cert else "http"
    if not DASHBOARD_SECRET:
        logger.warning(
            "DASHBOARD_SECRET is not set — API endpoints are unprotected. "
            "Set DASHBOARD_SECRET env var for production use."
        )
    logger.info("Starting Remote Dashboard v%s", VERSION)
    logger.info("  MQTT: %s:%s", args.mqtt_host, args.mqtt_port)
    logger.info("  Web:  %s://%s:%s", proto, config.HOST, args.port)

    uvicorn.run(
        app,
        host=config.HOST,
        port=args.port,
        ssl_certfile=args.ssl_cert,
        ssl_keyfile=args.ssl_key,
        log_level="info",
    )


if __name__ == "__main__":
    main()
