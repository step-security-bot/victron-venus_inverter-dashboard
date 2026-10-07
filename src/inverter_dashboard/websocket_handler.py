"""
WebSocket handler for real-time dashboard updates
"""

import asyncio
import json
import logging
from typing import Any

from aiomqtt import Client
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

from . import config, controller_commands, ess_mode, gateway, ha_client, settings_store
from .cerbo import number
from .config import DEFAULT_LOOP_INTERVAL, DEFAULT_POWER_MAX, DEFAULT_POWER_MIN
from .version import VERSION

logger = logging.getLogger(__name__)


def _gateway_command_body(action: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    body = payload or {}
    if action == "toggle":
        flag = _control_flag_key(body.get("entity"))
        value = control_boolean(body.get("state"))
        if flag is None or value is None:
            raise ValueError("Gateway toggles require a controller flag and explicit state")
        return {"entity": flag, "state": "on" if value else "off"}
    if action == "dry_run":
        if not isinstance(body.get("value"), bool):
            raise ValueError("Gateway dry run requires an explicit boolean value")
        return body
    if action == "ess_mode":
        if body:
            raise ValueError("ESS mode takes an empty command body")
        return body
    raise ValueError("This action is not supported by the gateway")


def _current_source(owner, generation: int, remote: bool) -> bool:
    return (
        owner is _state.get("mqtt_state")
        and generation == gateway.source_generation()
        and remote == gateway.prefer_gateway()
    )


def _transport_connected(remote: bool) -> bool:
    app = _state.get("app_state")
    return getattr(app, "data_source", None) == ("igw" if remote else "mqtt") and bool(
        getattr(app, "gateway_connected" if remote else "mqtt_connected", False)
    )


def _current_mqtt_client(client: Client | None) -> bool:
    return client is not None and client is getattr(_state.get("app_state"), "mqtt_client", None)


def _controller_transport_guard(client: Client | None):
    owner = _state.get("mqtt_state")
    remote = gateway.prefer_gateway()
    generation = gateway.source_generation()

    def current() -> None:
        client_matches = remote or _current_mqtt_client(client)
        if (
            not _current_source(owner, generation, remote)
            or owner is None
            or not owner.controller_commands_available()
            or not _transport_connected(remote)
            or not client_matches
        ):
            raise ValueError("Current controller transport is unavailable")

    current()
    return generation, remote, current


async def mqtt_publish(
    client: Client | None, action: str, payload: dict[str, Any] | None = None
) -> None:
    """Send once through the selected live controller; never silently accept loss."""
    generation, remote, current = _controller_transport_guard(client)

    async def send_checked() -> None:
        current()
        if remote:
            await gateway.post_command(
                action,
                _gateway_command_body(action, payload),
                expected_generation=generation,
                before_send=current,
            )
        else:
            topic = f"inverter/cmd/{action}"
            message = json.dumps(payload) if payload else ""
            await client.publish(topic, message, qos=0)

    await gateway.run_ess_selection(generation, send_checked)


# Connected WebSocket clients
ws_clients: set[WebSocket] = set()
_ws_write_locks: dict[WebSocket, asyncio.Lock] = {}
_broadcast_lock = asyncio.Lock()
WS_SEND_TIMEOUT_SECONDS = 2.0
WS_CLOSE_TIMEOUT_SECONDS = 1.0
OVERRIDE_TIMEOUT_SECONDS = 5.0
NOTIFICATION_COMMAND_TIMEOUT_SECONDS = 5.0


# Pydantic model for allowed state fields - replaces _STATE_ALLOWLIST
# All fields optional since MQTT may not send all at once
class InverterState(BaseModel):
    """Validated inverter state payload sent to WebSocket clients."""

    model_config = ConfigDict(extra="ignore")  # silently drop unknown fields

    # Grid
    gt: float | int | None = None
    g1: float | int | None = None
    g2: float | int | None = None
    g3: float | int | None = None

    # Consumption
    tt: float | int | None = None
    t1: float | int | None = None
    t2: float | int | None = None
    t3: float | int | None = None

    # False means unavailable, including an explicitly invalidated MQTT value.
    telemetry_available: dict[str, bool] | None = None
    grid_available: bool | None = None
    grid_l1_available: bool | None = None
    grid_l2_available: bool | None = None
    grid_l3_available: bool | None = None
    grid_backup: dict[str, Any] | None = None
    grid_using_backup: bool | None = None
    grid_backup_observed_at: float | None = None

    # Solar
    solar_total: float | int | None = None
    mppt_total: float | int | None = None
    pv_inverter_total: float | int | None = None

    # Battery
    battery_soc: float | int | None = None
    battery_power: float | int | None = None
    battery_voltage: float | int | None = None
    battery_current: float | int | None = None

    # Inverter
    setpoint: float | int | None = None
    inverter_state: str | None = None
    version: str | None = None

    # Dashboard
    dashboard_version: str | None = None
    latest_version: str | None = None
    uptime: float | int | None = None

    # HA
    ha_connected: bool | None = None
    ha_direct_connected: bool | None = None

    # Control
    setpoint_override: dict[str, Any] | None = None
    setpoint_override_observed_at: float | None = None
    electricity_tariff_observed_at: float | None = None
    dry_run: bool | str | None = None
    ess_mode: dict[str, Any] | None = None
    ess_mode_observed_at: float | None = None
    ess_mode_controls_available: bool = False
    limits: dict[str, float | int] | None = None
    loop_interval: float | int | None = None
    dvcc_limits: dict[str, Any] | None = None
    perf: dict[str, Any] | None = None

    # Controller/watchdog status remains in slim inverter/state. These are
    # policy diagnostics, separate from native grid measurement availability.
    grid_control_valid: bool | None = None
    grid_control_reason: str | None = None
    grid_loss_state: str | None = None
    grid_loss_hold_seconds: float | int | None = None
    grid_loss_elapsed: float | int | None = None
    grid_loss_remaining: float | int | None = None
    grid_loss_zero_applied: bool | None = None

    # Feature flags / derived
    booleans: dict[str, bool | None] | None = None
    features: dict[str, bool] | None = None
    mppt_individual: list[float | int] | None = None
    mppt_chargers: list[dict[str, Any]] | None = None
    # AC PV inverters of any vendor: [{name?, power, voltage?, current?}]
    pv_inverters: list[dict[str, Any]] | None = None
    batteries: list[dict[str, Any]] | None = None
    loads: dict[str, float | int] | None = None
    load_names: dict[str, str] | None = None
    ui_config: dict[str, Any] | None = None
    daily_stats: dict[str, Any] | None = None

    # Solar forecast computed upstream by inverter-control:
    # {date, generated_at, today_kwh, tomorrow_kwh}
    solar_forecast: dict[str, Any] | None = None

    # Rich HA entity displays from ha_client (HaFilteredData shape):
    # {sensors[], numbers[], covers[], media_players[], scenes[], weather}
    ha_filtered: dict[str, Any] | None = None

    # EV
    ev_charging_kw: float | int | None = None
    ev_power: float | int | None = None
    car_charging_power: float | int | None = None
    car_soc: float | int | None = None
    ev_charging_power: float | int | None = None
    ev_present: bool | None = None
    evcharger_present: bool | None = None
    discovered_water_ev: list[dict[str, Any]] | None = None

    # Water
    water_level: float | int | None = None
    water_valve: bool | str | None = None
    pump_switch: bool | str | None = None
    pump_mode: float | int | None = None
    water_pump_mode: float | int | None = None
    water_valve_mode: float | int | None = None

    # Appliances
    dishwasher_running: bool | None = None
    dishwasher_duration: float | int | None = None
    washer_time: float | int | None = None
    washer_power: float | int | bool | None = None
    dryer_time: float | int | None = None
    dryer_power: float | int | bool | None = None

    # Notifications (inverter-control pushes + Victron alarm transitions)
    notifications: list[dict[str, Any]] | None = None

    # Latest camera event (Frigate): {camera, url, ts}
    camera_event: dict[str, Any] | None = None


# Mutable module-level state (avoids global statements)
_state: dict[str, Any] = {"latest_version": None, "mqtt_state": None, "app_state": None}


def set_latest_version(version: str | None) -> None:
    """Update cached latest version"""
    _state["latest_version"] = version


def set_mqtt_state(mqtt_state):
    """Set the MqttState reference for state reads."""
    _state["mqtt_state"] = mqtt_state


def set_app_state(app_state) -> None:
    """Share current transport status without keeping a stale MQTT client."""
    _state["app_state"] = app_state


def _water_context():
    app_state = _state.get("app_state")
    mqtt_state = getattr(app_state, "mqtt_state", None)
    if mqtt_state is None:
        raise RuntimeError("Native water state is unavailable")
    if gateway.prefer_gateway():
        if getattr(app_state, "data_source", None) != "igw" or not getattr(
            app_state, "gateway_connected", False
        ):
            raise RuntimeError("The gateway connection is unavailable")
        if mqtt_state.gateway_capabilities.get("water_mode") is not True:
            raise RuntimeError("This gateway does not advertise native water mode control")
        return None, mqtt_state, None
    if getattr(app_state, "data_source", None) != "mqtt":
        raise RuntimeError("Direct Cerbo MQTT is not selected")
    if not getattr(app_state, "mqtt_connected", False) or app_state.mqtt_client is None:
        raise RuntimeError("Direct Cerbo MQTT is not connected")
    portal = getattr(mqtt_state, "_portal_id", "")
    if (
        not isinstance(portal, str)
        or not portal
        or any(c in "/+#\0" or c.isspace() for c in portal)
    ):
        raise RuntimeError("A valid Cerbo portal is required for water mode control")
    return app_state.mqtt_client, mqtt_state, portal


def _water_instance(which: str) -> int:
    instance = config.WATER_PUMP_INSTANCE if which == "pump" else config.WATER_VALVE_INSTANCE
    if isinstance(instance, bool) or not isinstance(instance, int) or instance < 0:
        raise RuntimeError("Water device instance is not configured")
    return instance


def _water_device_available(mqtt_state, which: str) -> bool:
    leaves = dict(mqtt_state._devices("pump")).get(str(_water_instance(which)), {})
    return number(leaves.get("Mode")) in (0, 1, 2) and mqtt_state.water_mode_fresh(
        str(_water_instance(which))
    )


def _can_control_water(which: str | None = None) -> bool:
    try:
        _, mqtt_state, _ = _water_context()
        return any(
            _water_device_available(mqtt_state, device)
            for device in ((which,) if which else ("pump", "valve"))
        )
    except RuntimeError:
        return False


def _validate_water_snapshot(snapshot: dict[str, Any], instance: int) -> None:
    caps, pumps = snapshot.get("capabilities"), snapshot.get("pump")
    if not isinstance(caps, dict) or caps.get("water_mode") is not True:
        raise ValueError("Current native water capability is unavailable")
    if not isinstance(pumps, dict) or number(pumps.get(f"{instance}/Mode")) not in (0, 1, 2):
        raise ValueError("Current native water mode is unavailable")
    if f"{instance}/Connected" in pumps and number(pumps[f"{instance}/Connected"]) != 1:
        raise ValueError("Current native water device is disconnected")


async def _set_water_mode(data: dict[str, Any], mqtt_client: Client | None) -> None:
    which, mode = data.get("which"), data.get("mode")
    if (
        which not in ("pump", "valve")
        or isinstance(mode, bool)
        or not isinstance(mode, int)
        or mode not in (0, 1, 2)
    ):
        raise ValueError("Water mode requires pump or valve and integer mode 0, 1 or 2")
    current_client, mqtt_state, portal = _water_context()
    if portal is not None and mqtt_client is not current_client:
        raise RuntimeError("The direct MQTT connection changed; retry the water action")
    instance = _water_instance(which)
    if not _water_device_available(mqtt_state, which):
        raise RuntimeError("The configured water device has no available native Mode")
    generation = gateway.source_generation()

    def current() -> None:
        live_client, live_state, live_portal = _water_context()
        if (
            generation != gateway.source_generation()
            or live_state is not mqtt_state
            or live_portal != portal
            or live_client is not current_client
            or not _water_device_available(live_state, which)
        ):
            raise RuntimeError("The water connection changed before dispatch")

    async def send_checked() -> None:
        current()
        if portal is None:
            async with gateway._new_gateway_client() as client:
                snapshot = await gateway.fetch_snapshot(client)
            current()
            _validate_water_snapshot(snapshot, instance)
            await gateway.post_command(
                "water_mode",
                {"instance": instance, "mode": mode},
                expected_generation=generation,
                before_send=current,
            )
        else:
            await current_client.publish(
                f"W/{portal}/pump/{instance}/Mode",
                json.dumps({"value": int(mode)}),
                qos=0,
                retain=False,
            )

    await gateway.run_ess_selection(generation, send_checked)


def build_payload() -> dict[str, Any]:
    """Build the canonical state payload sent to all WebSocket clients."""
    mqtt = _state["mqtt_state"]
    raw_state = ha_client.merge_overlay(mqtt.get_state())

    # Use Pydantic model to filter/validate - extra="ignore" drops unknown keys
    validated = InverterState(**raw_state)

    # Preserve explicit nulls so polling clients can clear invalidated values.
    filtered = validated.model_dump(exclude_unset=True)
    app_state = _state.get("app_state")
    transport = {
        key: getattr(app_state, key)
        for key in ("data_source", "mqtt_connected", "gateway_connected")
        if hasattr(app_state, key)
    }
    source = transport.get("data_source")
    if source in ("mqtt", "igw"):
        transport["native_connected"] = bool(
            transport.get("mqtt_connected" if source == "mqtt" else "gateway_connected")
        )
        if hasattr(mqtt, "native_telemetry"):
            transport["telemetry"] = mqtt.native_telemetry(source, transport["native_connected"])

    return _with_ui_config(
        {
            **filtered,
            **transport,
            "notifications": mqtt.get_notifications(),
            "camera_event": mqtt.camera_event,
            "dashboard_version": VERSION,
            "latest_version": _state["latest_version"],
            "water_controls_available": _can_control_water(),
            "water_pump_controls_available": _can_control_water("pump"),
            "water_valve_controls_available": _can_control_water("valve"),
            "controller_controls_available": _can_control_controller(),
            "ha_controls_available": ha_client.controls_available(),
            "ha_observed_at": ha_client._overlay_observed_at,
            "ess_mode_controls_available": _can_select_ess_mode(),
            "setpoint_override_controls_available": _can_controller_command("setpoint_override"),
            "electricity_tariff_controls_available": _can_controller_command("electricity_tariff"),
        }
    )


# Runtime-editable UI settings (dashboard_settings.json via /api/settings)
_ui_settings: dict[str, Any] = {}


def set_ui_settings(settings: dict[str, Any]) -> None:
    """Hot-apply saved settings to the broadcast pipeline."""
    global _ui_settings
    _ui_settings = dict(settings)


def get_ui_settings() -> dict[str, Any]:
    """Currently applied settings (defaults + persisted overrides)."""
    return dict(_ui_settings)


def _with_ui_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge local_config-derived ui_config (e.g. home_buttons) into payload."""
    patch = ha_client.ui_config_patch()
    out = dict(payload)
    uc = dict(out.get("ui_config") or {})
    if _ui_settings:
        uc["settings"] = {k: v for k, v in _ui_settings.items() if k.startswith("show_")}
    uc.update(patch)
    if uc:
        out["ui_config"] = uc
    return out


def _forget_client(websocket: WebSocket) -> None:
    ws_clients.discard(websocket)
    _ws_write_locks.pop(websocket, None)


async def _send_state(websocket: WebSocket, message: str) -> bool:
    """Serialize each peer's writes and bound transport backpressure."""
    if websocket not in ws_clients:
        return False
    lock = _ws_write_locks.setdefault(websocket, asyncio.Lock())
    async with lock:
        # An earlier queued write or the receive loop may have disconnected it.
        if websocket not in ws_clients:
            return False
        try:
            async with asyncio.timeout(WS_SEND_TIMEOUT_SECONDS):
                await websocket.send_text(message)
            return True
        except Exception:
            _forget_client(websocket)
            logger.warning("WebSocket state send failed; disconnecting client")
            try:
                async with asyncio.timeout(WS_CLOSE_TIMEOUT_SECONDS):
                    await websocket.close(code=1013)
            except Exception:
                logger.debug("WebSocket close did not complete after failed state send")
            return False


async def broadcast_state():
    """Send the same state to every peer without serial network waits."""
    # Apply backpressure to callers before allocating a payload or per-peer tasks.
    # Only one fan-out is active; initial frames share each peer's write lock.
    async with _broadcast_lock:
        clients = list(ws_clients)
        if not clients:
            return

        data = build_payload()
        message = json.dumps(data)
        async with asyncio.TaskGroup() as sends:
            for ws in clients:
                sends.create_task(_send_state(ws, message))


# Inverter-control flags published on Cerbo MQTT inverter/state.booleans
# (same set as inverter-desktop). Always toggle via MQTT with the bare key —
# never via HA input_boolean / binary_sensor mirrors.
_CONTROL_FLAG_KEYS = frozenset(
    {
        "only_charging",
        "no_feed",
        "house_support",
        "charge_battery",
        "do_not_supply_charger",
        "set_limit_to_ev_charger",
        "minimize_charging",
    }
)


def control_boolean(value: Any) -> bool | None:
    """Preserve unknown controller values rather than showing them as off."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "on"):
            return True
        if normalized in ("false", "0", "off"):
            return False
    if type(value) in (int, float) and value in (0, 1):
        return bool(value)
    return None


def _control_flag_key(entity: str | None) -> str | None:
    if not entity or not isinstance(entity, str):
        return None
    raw = entity.strip()
    if not raw:
        return None
    key = raw.removeprefix("input_boolean.")
    return key if key in _CONTROL_FLAG_KEYS else None


def _notification_portal(owner) -> str:
    return getattr(owner, "_portal_id", "") or config.CERBO_PORTAL_ID or ""


async def _native_notification_command(name: str, mqtt_client: Client | None) -> None:
    """Bind a single physical acknowledgement to its native source, not controller state."""
    owner = _state.get("mqtt_state")
    generation = gateway.source_generation()
    remote = gateway.prefer_gateway()
    portal = _notification_portal(owner)

    def current() -> None:
        if (
            owner is None
            or not _current_source(owner, generation, remote)
            or not _transport_connected(remote)
            or portal != _notification_portal(owner)
        ):
            raise ValueError("Native notification connection changed")
        if not remote and (
            not _current_mqtt_client(mqtt_client) or not _valid_notification_portal(portal)
        ):
            raise ValueError("Current native notification MQTT source is unavailable")

    current()

    async def send_checked() -> None:
        current()
        if remote:
            await gateway.post_command(
                name, {}, expected_generation=generation, before_send=current
            )
        elif name == "acknowledge_all_notifications":
            await mqtt_client.publish(
                f"W/{portal}/platform/0/Notifications/AcknowledgeAll",
                '{"value":1}',
                qos=0,
                retain=False,
            )
        else:
            raise ValueError("This native notification command requires the gateway")
        current()

    async with asyncio.timeout(NOTIFICATION_COMMAND_TIMEOUT_SECONDS):
        await gateway.run_ess_selection(generation, send_checked)
    current()


def _valid_notification_portal(portal: Any) -> bool:
    return (
        isinstance(portal, str)
        and bool(portal)
        and not any(c in "/+#" or c.isspace() or ord(c) < 32 for c in portal)
    )


async def _acknowledge_victron_on_cerbo(app_state, mqtt_client: Client | None) -> None:
    await _native_notification_command("acknowledge_all_notifications", mqtt_client)


async def _dismiss_native_notification(nid: str, mqtt_client: Client | None) -> None:
    if nid.startswith("victron-platform-"):
        await _acknowledge_victron_on_cerbo(None, mqtt_client)
    elif nid.startswith("victron-") and gateway.prefer_gateway():
        await _native_notification_command("silence_alarm", mqtt_client)


def _can_control_controller() -> bool:
    app = _state.get("app_state")
    try:
        _controller_transport_guard(getattr(app, "mqtt_client", None))
        return True
    except ValueError:
        return False


def _can_select_ess_mode() -> bool:
    ms = _state.get("mqtt_state")
    app = _state.get("app_state")
    if ms is None or not ms.controller_available():
        return False
    remote = gateway.prefer_gateway()
    if not getattr(app, "gateway_connected" if remote else "mqtt_connected", False):
        return False
    if remote and ms.gateway_capabilities.get("set_ess_mode") is not True:
        return False
    state = ms.get_state()
    return ess_mode.telemetry_ready(
        getattr(ms, "_controller_ess_mode", None),
        getattr(ms, "_ess_mode_observed_at", None),
        state.get("dry_run"),
    )


async def _select_ess_mode(data: dict[str, Any], mqtt_client: Client | None) -> None:
    body = ess_mode.validate_selection(
        {key: value for key, value in data.items() if key != "action"}
    )
    ms = _state.get("mqtt_state")
    generation = gateway.source_generation()
    remote = gateway.prefer_gateway()

    def current() -> None:
        if (
            not _current_source(ms, generation, remote)
            or not _transport_connected(remote)
            or not _can_select_ess_mode()
            or (not remote and not _current_mqtt_client(mqtt_client))
        ):
            raise ValueError("Connection or ESS capability changed before selection")

    current()

    async def send_checked() -> None:
        current()
        if remote:
            async with gateway._new_gateway_client() as client:
                snapshot = await gateway.fetch_snapshot(client)
                ess_mode.validate_gateway_snapshot(snapshot)
            current()
            await gateway.post_command(
                "set_ess_mode", body, expected_generation=generation, before_send=current
            )
        else:
            await mqtt_client.publish(
                "inverter/cmd/set_ess_mode", json.dumps(body), qos=0, retain=False
            )

    await gateway.run_ess_selection(generation, send_checked)


def _can_controller_command(name: str) -> bool:
    ms = _state.get("mqtt_state")
    remote = gateway.prefer_gateway()
    if (
        ms is None
        or not ms.controller_available()
        or not _transport_connected(remote)
        or (remote and ms.gateway_capabilities.get(name) is not True)
    ):
        return False
    state = ms.get_state()
    if name == "setpoint_override":
        return controller_commands.override_status(
            state.get(name)
        ) is not None and controller_commands.fresh(ms._setpoint_override_observed_at)
    return controller_commands.tariff_status(state).get(
        "writable"
    ) is True and controller_commands.fresh(ms._electricity_tariff_observed_at)


def _validate_controller_snapshot(name: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    capabilities = snapshot.get("capabilities")
    controller = snapshot.get("inverter")
    if (
        not isinstance(capabilities, dict)
        or capabilities.get(name) is not True
        or not isinstance(controller, dict)
    ):
        raise ValueError("Live gateway controller support is unavailable")
    if name == "setpoint_override":
        if controller_commands.override_status(controller.get(name)) is None:
            raise ValueError("Current setpoint override status is unavailable")
    elif controller_commands.tariff_status(controller).get("writable") is not True:
        raise ValueError("Controller tariff editing is unavailable")
    return controller


async def _send_controller_command(
    name: str, data: dict[str, Any], mqtt_client: Client | None
) -> None:
    body = {key: value for key, value in data.items() if key != "action"}
    body = (
        controller_commands.validate_override
        if name == "setpoint_override"
        else controller_commands.validate_tariff
    )(body)
    generation = gateway.source_generation()
    owner = _state.get("mqtt_state")
    remote = gateway.prefer_gateway()

    def check_current() -> None:
        if (
            not _current_source(owner, generation, remote)
            or not _can_controller_command(name)
            or (not remote and not _current_mqtt_client(mqtt_client))
        ):
            raise ValueError("Current controller connection or support is unavailable")
        if (
            name == "electricity_tariff"
            and controller_commands.tariff_status(owner.get_state()).get("revision")
            != body["revision"]
        ):
            raise ValueError("Controller tariff changed; reload before editing")

    check_current()

    async def send_checked() -> None:
        check_current()
        dispatched_sequence: int | None = None

        def before_dispatch() -> None:
            nonlocal dispatched_sequence
            check_current()
            dispatched_sequence = owner._override_observation_sequence

        if remote:
            async with gateway._new_gateway_client() as client:
                snapshot = await gateway.fetch_snapshot(client)
            controller = _validate_controller_snapshot(name, snapshot)
            if (
                name == "electricity_tariff"
                and controller_commands.tariff_status(controller).get("revision")
                != body["revision"]
            ):
                raise ValueError("Controller tariff changed; reload before editing")
            check_current()
            await gateway.post_command(
                name, body, expected_generation=generation, before_send=before_dispatch
            )
        else:
            before_dispatch()
            await mqtt_client.publish(
                f"inverter/cmd/{name}", controller_commands.encode_body(body), qos=0, retain=False
            )
        if name == "setpoint_override":
            if dispatched_sequence is None:
                raise ValueError("Override dispatch was not confirmed")
            await _wait_override_ack(owner, body, remote, check_current, dispatched_sequence)

    # One deadline includes DNS/TLS, preflight, the sole write and every ACK read.
    # Controller owns the persistent two-second writes; this client never resends.
    async with asyncio.timeout(OVERRIDE_TIMEOUT_SECONDS):
        await gateway.run_ess_selection(generation, send_checked)


async def _wait_override_ack(
    owner, body: dict[str, Any], remote: bool, check_current, dispatched_sequence: int
) -> None:
    while True:
        check_current()
        if remote:
            async with gateway._new_gateway_client() as client:
                snapshot = await gateway.fetch_snapshot(client)
            check_current()
            controller = _validate_controller_snapshot("setpoint_override", snapshot)
            controller_commands.observe(
                owner, {"setpoint_override": controller["setpoint_override"]}, retained=False
            )
            await broadcast_state()
            check_current()
        status = controller_commands.override_status(owner.get_state().get("setpoint_override"))
        if (
            owner._override_observation_sequence > dispatched_sequence
            and status
            and status["request_id"] == body["request_id"]
        ):
            if status["last_error"] is not None or status["value"] != body["value"]:
                raise ValueError("Controller did not confirm the requested override value")
            return
        await asyncio.sleep(0.05 if not remote else 0.25)


async def _dispatch_action(action: str, data: dict[str, Any], mqtt_client: Client):
    """Dispatch a single WebSocket action."""
    if action in ("set_setpoint_override", "electricity_tariff"):
        await _send_controller_command(
            "setpoint_override" if action == "set_setpoint_override" else action, data, mqtt_client
        )
    elif action == "set_ess_mode":
        await _select_ess_mode(data, mqtt_client)
    elif action == "water_mode":
        await _set_water_mode(data, mqtt_client)
    elif action in ("number_set", "set_cover_position", "media_player", "scene_activate"):
        if not await ha_client.perform_action(action, data.get("entity"), data):
            raise RuntimeError("Home Assistant action failed")
        fresh = await ha_client.fetch_states_once()
        if fresh.get("ha_direct_connected"):
            ha_client.replace_overlay(fresh)
        await broadcast_state()
    elif action == "toggle":
        entity = data.get("entity")
        if not isinstance(entity, str) or not entity:
            raise ValueError("Entity is required for toggle")
        flag = _control_flag_key(entity if isinstance(entity, str) else None)
        if flag:
            # Mirror desktop: Cerbo MQTT inverter/cmd/toggle with bare flag key.
            payload = {"entity": flag}
            if "state" in data:
                payload["state"] = data["state"]
            await mqtt_publish(mqtt_client, "toggle", payload)
            return
        if "." in entity and (
            not ha_client.is_direct_mode() or not ha_client.is_toggle_allowed(entity)
        ):
            raise ValueError("Home entity is not configured for direct Home Assistant control")
        if entity and ha_client.is_direct_mode() and ha_client.is_toggle_allowed(entity):
            if ha_client.domain_for_press(entity):
                succeeded = await ha_client.press_entity(entity)
            else:
                succeeded = await ha_client.toggle_entity(entity)
            if not succeeded:
                raise RuntimeError("Home Assistant action failed")
            fresh = await ha_client.fetch_states_once()
            if fresh.get("ha_direct_connected"):
                ha_client.replace_overlay(fresh)
            await broadcast_state()
            return
        await mqtt_publish(mqtt_client, "toggle", {"entity": entity})
    elif action == "press":
        entity = data.get("entity")
        if isinstance(entity, str) and "." in entity and not ha_client.is_direct_mode():
            raise ValueError("Home entity is not configured for direct Home Assistant control")
        if ha_client.is_direct_mode():
            if (
                not isinstance(entity, str)
                or not ha_client.is_toggle_allowed(entity)
                or ha_client.domain_for_press(entity) is None
            ):
                raise ValueError("Button is not configured for direct Home Assistant control")
            if not await ha_client.press_entity(entity):
                raise RuntimeError("Home Assistant button press failed")
            fresh = await ha_client.fetch_states_once()
            if fresh.get("ha_direct_connected"):
                ha_client.replace_overlay(fresh)
            await broadcast_state()
            return
        await mqtt_publish(mqtt_client, "press", {"entity": entity})
    elif action == "setpoint":
        await mqtt_publish(mqtt_client, "setpoint", {"value": data.get("value")})
    elif action == "dry_run":
        payload = {"value": data["value"]} if "value" in data else {}
        await mqtt_publish(mqtt_client, "dry_run", payload)
    elif action == "limits":
        await mqtt_publish(
            mqtt_client,
            "limits",
            {
                "min": data.get("min", DEFAULT_POWER_MIN),
                "max": data.get("max", DEFAULT_POWER_MAX),
            },
        )
    elif action == "ess_mode":
        await mqtt_publish(mqtt_client, "ess_mode", {})
    elif action == "loop_interval":
        await mqtt_publish(
            mqtt_client,
            "loop_interval",
            {"interval": data.get("interval", DEFAULT_LOOP_INTERVAL)},
        )
    elif action == "set_settings":
        try:
            saved = settings_store.save_settings(
                {key: value for key, value in data.items() if key not in ("action", "request_id")}
            )
        except ValueError:
            raise ValueError("Invalid settings patch") from None
        set_ui_settings(saved)
        await broadcast_state()
    elif action == "dismiss_notification":
        # Wired from Vue NotificationBanner X — same UX as inverter-desktop.
        nid = data.get("id")
        ms = _state.get("mqtt_state")
        if not isinstance(nid, str) or not nid or ms is None:
            return
        ms.dismiss_notification(nid)
        try:
            await _dismiss_native_notification(nid, mqtt_client)
        finally:
            # Local banner dismissal remains separate from physical ACK success.
            await broadcast_state()

    else:
        raise ValueError("Unsupported dashboard action")


async def _command_reply(
    websocket: WebSocket, action: str, request_id: Any, *, failed: bool = False
) -> None:
    if not controller_commands.request_id_valid(request_id):
        return
    payload = {
        "type": "command_error" if failed else "command_result",
        "action": action,
        "request_id": request_id,
    }
    if failed:
        payload["error"] = "Command was not confirmed; check the live connection and current state."
    else:
        payload["status"] = "accepted"
    await _send_state(websocket, json.dumps(payload))


async def handle_websocket(websocket: WebSocket, app_state):
    """Handle WebSocket connection.

    ``app_state`` is the server's AppState container; the MQTT client is read
    fresh for each action so commands survive reconnects (the aiomqtt client
    object is replaced on every reconnection).
    """
    await websocket.accept()
    ws_clients.add(websocket)
    logger.info("WebSocket client connected (%d total)", len(ws_clients))

    try:
        if not await _send_state(websocket, json.dumps(build_payload())):
            return

        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action:
                try:
                    if "request_id" in data and not controller_commands.request_id_valid(
                        data["request_id"]
                    ):
                        raise ValueError("Invalid command request ID")
                    await _dispatch_action(action, data, app_state.mqtt_client)
                    await _command_reply(websocket, action, data.get("request_id"))
                except Exception:
                    await _command_reply(websocket, action, data.get("request_id"), failed=True)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error")
    finally:
        _forget_client(websocket)
        logger.info("WebSocket client disconnected (%d remaining)", len(ws_clients))
