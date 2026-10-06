"""Remote inverter-gateway (IGW) client — mirrors inverter-desktop gateway.rs.

Live Cerbo tiles come from either:
- Cerbo/LAN MQTT (``MQTT_HOST``), or
- remote inverter-gateway (``GATEWAY_ENABLED`` + ``GATEWAY_URL`` → ``GET /v1/snapshot``).

Precedence (same idea as inverter-desktop ``connectionPolicy``):
1. Only ``MQTT_HOST`` → Cerbo MQTT client.
2. Only IGW → snapshot poller.
3. Both configured → probe MQTT; if the broker accepts TCP, use MQTT,
   otherwise use IGW. Dual-path mode may fail over MQTT→IGW and later
   recover IGW→MQTT when the broker is reachable again.
4. Neither → no live data source.

mp production clears ``MQTT_HOST`` so IGW stays primary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx

from . import config, notifications

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 2.0
REQUEST_TIMEOUT_SECS = 25.0
MQTT_PROBE_TIMEOUT_SECS = 3.0
MQTT_CONNECT_WATCHDOG_SECS = 15.0
MQTT_RECOVERY_PROBE_SECS = 60.0

StartupSource = Literal["mqtt", "igw", "none"]

# Exclusive live path selected at startup (and updated on dual-path failover).
_active_source: StartupSource = "none"
_dual_path: bool = False
_source_generation = 0
_ess_command_tasks: set[asyncio.Task] = set()


def gateway_configured() -> bool:
    """True when remote IGW looks configured (enabled + URL)."""
    return bool(config.GATEWAY_ENABLED and (config.GATEWAY_URL or "").strip())


def mqtt_configured() -> bool:
    """True when a Cerbo/LAN MQTT host is configured."""
    return bool((config.MQTT_HOST or "").strip())


def set_active_source(source: StartupSource, *, dual_path: bool = False) -> None:
    """Record the exclusive live path (commands/ack follow this)."""
    global _active_source, _dual_path
    invalidate_ess_commands()
    _active_source = source
    _dual_path = dual_path and source in ("mqtt", "igw")


def active_source() -> StartupSource:
    """Currently selected exclusive data source."""
    return _active_source


def source_generation() -> int:
    return _source_generation


def invalidate_ess_commands() -> None:
    global _source_generation
    _source_generation += 1
    for task in tuple(_ess_command_tasks):
        task.cancel()


async def run_ess_selection(generation: int, operation: Callable[[], Awaitable[None]]) -> None:
    """Cancel an in-flight command check/write when its source is replaced."""
    if generation != _source_generation:
        raise ValueError("Connection changed before ESS selection")
    task = asyncio.create_task(operation())
    _ess_command_tasks.add(task)
    try:
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, asyncio.CancelledError) and generation != _source_generation:
            raise ValueError("Connection changed during ESS selection") from None
        if isinstance(result, BaseException):
            raise result
    finally:
        _ess_command_tasks.discard(task)


def dual_path_enabled() -> bool:
    """True when both MQTT and IGW were configured at selection time."""
    return _dual_path


def prefer_gateway() -> bool:
    """True when the live exclusive path is IGW (commands/ack via gateway).

    After startup selection this follows ``active_source()``. Before selection
    (tests / early imports) it is True only for IGW-only configs so a coexisting
    ``MQTT_HOST`` is not abandoned by default.
    """
    if _active_source != "none":
        return _active_source == "igw"
    return gateway_configured() and not mqtt_configured()


def choose_startup_source(
    *,
    mqtt_configured: bool,
    igw_configured: bool,
    mqtt_reachable: bool,
) -> StartupSource:
    """Choose the exclusive live path — mirrors desktop ``chooseStartupSource``."""
    if mqtt_configured and igw_configured:
        return "mqtt" if mqtt_reachable else "igw"
    if mqtt_configured:
        return "mqtt"
    if igw_configured:
        return "igw"
    return "none"


async def probe_mqtt_reachable(timeout: float = MQTT_PROBE_TIMEOUT_SECS) -> bool:
    """True when ``MQTT_HOST:MQTT_PORT`` accepts a TCP connection."""
    host = (config.MQTT_HOST or "").strip()
    if not host:
        return False
    port = int(config.MQTT_PORT or 1883)
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except Exception as e:  # pylint: disable=broad-except
        logger.debug("MQTT probe %s:%s failed: %s", host, port, e)
        return False
    try:
        writer.close()
        await writer.wait_closed()
    except Exception as close_err:  # pylint: disable=broad-except
        logger.debug("MQTT probe close %s:%s: %s", host, port, close_err)
    logger.debug("MQTT probe %s:%s ok", host, port)
    return True


def apply_snapshot(ms: Any, snap: dict[str, Any]) -> None:
    """Map IGW snapshot leaf maps into MqttState device maps + overlays.

    ``ms`` is an MqttState (duck-typed to avoid circular imports).
    """
    capabilities = snap.get("capabilities")
    ms.gateway_capabilities = capabilities if isinstance(capabilities, dict) else {}
    # Older gateways omit this field; omission must not erase a controller
    # observation. New gateways explicitly send null for absent/stale state.
    if "inverter" in snap:
        if isinstance(snap["inverter"], dict):
            # This is a complete retained controller object, not a slim tick.
            ms.clear_daemon_state()
            ms._merge_daemon_state(snap["inverter"])
        elif snap["inverter"] is None:
            ms.clear_daemon_state()
    ms.replace_cerbo_snapshot(snap)

    # Alert banners (desktop parity): Venus-platform GUIv2 slots from IGW
    # ``platform`` leaves; Alarms/* fallback when platform never seen.
    platform = snap.get("platform") or {}
    if hasattr(ms, "sync_platform_from_snapshot"):
        ms.sync_platform_from_snapshot(platform if isinstance(platform, dict) else {})
    if hasattr(ms, "sync_alarms_from_snapshot"):
        ms.sync_alarms_from_snapshot(snap)


def build_headers() -> dict[str, str]:
    """CF Access service-token + optional GATEWAY_API_TOKEN bearer."""
    config.validate_gateway_url(config.GATEWAY_URL)
    headers = {"User-Agent": "inverter-dashboard/gateway"}
    cid, csec = config.validate_gateway_access_pair(
        config.GATEWAY_ACCESS_CLIENT_ID or "", config.GATEWAY_ACCESS_CLIENT_SECRET or ""
    )
    if cid:
        headers["CF-Access-Client-Id"] = cid
    if csec:
        headers["CF-Access-Client-Secret"] = csec
    token = (config.GATEWAY_API_TOKEN or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_snapshot(client: httpx.AsyncClient) -> dict[str, Any]:
    """GET /v1/snapshot; raises httpx.HTTPStatusError on non-2xx."""
    base = config.validate_gateway_url(config.GATEWAY_URL)
    url = f"{base}/v1/snapshot"
    resp = await client.get(url, headers=build_headers(), follow_redirects=False)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise TypeError("gateway snapshot is not a JSON object")
    platform = data.get("platform")
    if isinstance(platform, dict):
        datetime_keys = []
        for key, raw in platform.items():
            parsed = notifications.parse_platform_leaf_key(key)
            value = raw.get("value") if isinstance(raw, dict) else raw
            if parsed and parsed[2] == "DateTime" and isinstance(value, float):
                datetime_keys.append(key)
        if datetime_keys:
            # Only source DateTime decimals need their exact wire text. Reparse
            # those leaves without rounding; all other telemetry stays ordinary
            # JSON numbers rather than introducing Decimal objects into state.
            exact_platform = resp.json(parse_float=str)["platform"]
            for key in datetime_keys:
                if isinstance(platform[key], dict):
                    platform[key]["value"] = exact_platform[key]["value"]
                else:
                    platform[key] = exact_platform[key]
    return data


async def post_command(
    name: str, body: dict[str, Any] | None = None, *, expected_generation: int | None = None
) -> None:
    """POST /v1/commands/{name} (whitelist only on the gateway)."""
    base = config.validate_gateway_url(config.GATEWAY_URL)
    url = f"{base}/v1/commands/{name.strip('/')}"
    headers = build_headers()
    async with _new_gateway_client() as client:
        if expected_generation is not None and expected_generation != _source_generation:
            raise ValueError("Connection changed before ESS selection")
        resp = await client.post(url, headers=headers, json=body or {}, follow_redirects=False)
        resp.raise_for_status()


def _new_gateway_client() -> httpx.AsyncClient:
    """Keep TLS verification and redirect refusal explicit for every IGW operation."""
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECS, verify=True, follow_redirects=False)


async def gateway_poll_loop(
    app_state, mqtt_state_emit, status_emit=None, *, is_current=lambda: True
) -> None:
    """Background poller: fetch snapshot → apply → emit.

    ``app_state`` is the server AppState duck-type (gateway_* / mqtt_connected).
    ``mqtt_state_emit`` is an awaitable callback after apply (usually ms._emit).
    ``status_emit`` broadcasts a connection loss without replacing the last snapshot.
    """
    delay = max(config.GATEWAY_POLL_INTERVAL, 0.5)
    logged_ok = False
    async with _new_gateway_client() as client:
        while is_current():
            try:
                snap = await fetch_snapshot(client)
                if not is_current():
                    return
                app_state.gateway_connected = True
                app_state.mqtt_connected = False
                await mqtt_state_emit(snap)
                if not is_current():
                    return
                app_state.gateway_polls += 1
                if not logged_ok:
                    logger.info(
                        "IGW connected to %s (polling /v1/snapshot every %.1fs)",
                        config.GATEWAY_URL.rstrip("/"),
                        delay,
                    )
                    logged_ok = True
            except asyncio.CancelledError:
                logger.info("IGW poller stopped")
                raise
            except Exception as e:  # pylint: disable=broad-except
                if not is_current():
                    return
                logged_ok = False
                await _report_gateway_failure(app_state, status_emit, e)
            await asyncio.sleep(delay)


async def _report_gateway_failure(app_state, status_emit, error) -> None:
    app_state.gateway_errors += 1
    was = app_state.gateway_connected
    app_state.gateway_connected = False
    app_state.mqtt_connected = False
    if was:
        logger.warning("IGW poll failed: %s", error)
        if status_emit is not None:
            await status_emit()
    else:
        logger.debug("IGW poll failed: %s", error)
