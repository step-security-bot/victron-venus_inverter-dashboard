"""
Optional Home Assistant REST client for inverter-dashboard.

Reads local_config.py when present; if HA_DIRECT_CONTROLS is False or file missing,
all UI state for switches comes from MQTT (inverter-control) only.

When HA_DIRECT_CONTROLS is True, configured home devices and appliances use HA REST.
Controller flags and Cerbo telemetry always keep their MQTT source, including during
HA outages. HA mirrors from older configurations cannot replace these values.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx

from .cerbo import CERBO_OWNED_KEYS
from .config import HA_POLL_TIMEOUT, HA_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

_configured = False
_url = ""
_token = ""
_direct = False
_poll_interval = 12.0

_boolean_entities: dict[str, str] = {}
_switch_entities: dict[str, str] = {}
_switch_labels: dict[str, str] = {}
# Dashboard keys -> HA entity IDs (washer/dishwasher telemetry omitted from MQTT when inverter-control uses MQTT_SLIM_STATE)
_appliance_entities: dict[str, str] = {}
_sensor_entities: dict[str, Any] = {}

# Rich display entities (local_config.HA_FILTERED_ENTITIES): covers/media_players/scenes/numbers/sensors lists + weather entity
_filtered_entities: dict[str, Any] = {}

# Latest overlay merged into WebSocket payloads (replaced wholesale on each HA poll)
_overlay: dict[str, Any] = {
    "booleans": {},
    "ha_direct_connected": False,
}

# Reusable async HTTP client for HA requests
_http_client: httpx.AsyncClient | None = None

CONTROL_FLAG_KEYS = frozenset(
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
_MQTT_OWNED_KEYS = (
    CERBO_OWNED_KEYS
    | CONTROL_FLAG_KEYS
    | frozenset(
        {
            "booleans",
            "features",
            "ess_mode",
            "ess_mode_observed_at",
            "ess_mode_controls_available",
            "dry_run",
            "daily_stats",
            "solar_forecast",
            "limits",
            "loop_interval",
            "filtered_gt",
            "dvcc_limits",
            "perf",
            "ui_config",
            "version",
            "uptime",
            "grid_control_valid",
            "grid_control_reason",
            "water_pump_mode",
            "water_valve_mode",
            "car_charging_power",
            "ev_charging_power",
            "ev_present",
            "evcharger_present",
            "discovered_water_ev",
            "g3",
            "t3",
        }
    )
)


def _ha_owns_field(key: str, entity: str = "") -> bool:
    """Legacy HA aliases must not claim controller flags or live Cerbo fields."""
    return (
        isinstance(entity, str)
        and key not in _MQTT_OWNED_KEYS
        and entity.strip().removeprefix("input_boolean.") not in CONTROL_FLAG_KEYS
    )


def _ha_fields(entities: dict[str, str]):
    return ((key, entity) for key, entity in entities.items() if _ha_owns_field(key, entity))


def _prepend_local_config_import_path() -> None:
    """Load local_config from INVERTER_DASHBOARD_CONFIG (Docker mount) or app directory."""
    # Walk up from package dir to repo root (where local_config.py lives in dev/Docker)
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.normpath(os.path.join(pkg_dir, "..", ".."))

    candidates = []
    env_dir = os.environ.get("INVERTER_DASHBOARD_CONFIG", "").strip()
    if env_dir:
        candidates.append(env_dir)
    candidates.append(repo_root)
    candidates.append(pkg_dir)
    for d in candidates:
        if not d:
            continue
        path_py = os.path.join(d, "local_config.py")
        if os.path.isfile(path_py):
            if d not in sys.path:
                sys.path.insert(0, d)
            logger.info("Using local_config.py from %s", d)
            return


def _switch_entity_from_sequence(val: tuple | list) -> tuple[str | None, str | None]:
    """Extract (entity, label) from a (entity, label)-style tuple/list value."""
    entity = str(val[0]).strip() if len(val) >= 1 and val[0] else None
    label = str(val[1]).strip() if len(val) >= 2 and val[1] else None
    return entity, label


def _switch_entity_from_dict(val: dict) -> tuple[str | None, str | None]:
    """Extract (entity, label) from a dict-style value."""
    if val.get("enabled") is False:
        return None, None
    eid = val.get("entity") or val.get("id") or val.get("entity_id")
    lab = val.get("label") or val.get("short") or val.get("name")
    entity = str(eid).strip() if eid else None
    label = str(lab).strip() if lab else None
    return entity, label


def _switch_entity_from_value(val: Any) -> tuple[str | None, str | None]:
    """Normalize a single HA_SWITCH_ENTITIES value to (entity, label)."""
    if isinstance(val, str):
        return val.strip(), None
    if isinstance(val, (tuple, list)):
        return _switch_entity_from_sequence(val)
    if isinstance(val, dict):
        return _switch_entity_from_dict(val)
    return None, None


def _parse_ha_switch_entities(raw: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Parse local_config.HA_SWITCH_ENTITIES: value may be entity_id str, (entity, label), or dict."""
    entities: dict[str, str] = {}
    embedded_labels: dict[str, str] = {}
    if not raw or not isinstance(raw, dict):
        return entities, embedded_labels

    def order(item):
        value = item[1]
        rank = value.get("order", 0) if isinstance(value, dict) else 0
        return rank if isinstance(rank, int) and not isinstance(rank, bool) else 0

    # Python's stable sort retains configuration order when ranks are equal.
    for state_key, val in sorted(raw.items(), key=order):
        if not isinstance(state_key, str) or not state_key:
            continue
        entity, label = _switch_entity_from_value(val)
        if entity:
            entities[state_key] = entity
        if label:
            embedded_labels[state_key] = label
    return entities, embedded_labels


def _sensor_state_to_seconds(raw: str | None) -> int:
    """Parse HA sensor state to seconds for dashboard timers (matches inverter-control numeric / HH:MM:SS)."""
    if raw in (None, "unavailable", "unknown", "None", ""):
        return 0
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        pass
    parts = str(raw).split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return m * 60 + s
    except (ValueError, TypeError):
        pass
    return 0


def _boolish(raw: str | None) -> bool:
    if raw is None:
        return False
    return str(raw).lower() in ("on", "true", "yes", "1")


def _parse_numeric_state(raw: str | None) -> float | None:
    """Parse HA state string to a float, returns None if unavailable/unknown."""
    if raw in (None, "unavailable", "unknown", "None", ""):
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _appliance_field_value(state_key: str, entity_id: str, raw: str | None) -> Any:
    """Map HA state string to dashboard type (bool vs seconds)."""
    domain = entity_id.split(".")[0]
    if domain in ("binary_sensor", "switch", "light", "input_boolean"):
        return _boolish(raw)
    if domain == "sensor":
        if state_key.endswith("_power"):
            try:
                return float(raw or 0) > 1.0
            except (ValueError, TypeError):
                return _boolish(raw)
        return _sensor_state_to_seconds(raw)
    return False


def _appliance_fallback(state_key: str) -> Any:
    if state_key.endswith(("_time", "_duration")):
        return 0
    return False


def load_config():
    """Import local_config if present (see local_config.example.py)."""
    global _configured, _url, _token, _direct, _poll_interval
    global _boolean_entities, _switch_entities, _switch_labels
    global _appliance_entities
    global _sensor_entities
    global _filtered_entities

    _prepend_local_config_import_path()

    try:
        import local_config as sc  # type: ignore
    except ImportError:
        _configured = False
        _boolean_entities = {}
        _switch_entities = {}
        _switch_labels = {}
        _appliance_entities = {}
        _filtered_entities = {}
        logger.info("local_config.py not found — switch state from MQTT only")
        return

    _url = (getattr(sc, "HA_URL", "") or "").rstrip("/")
    _token = getattr(sc, "HA_TOKEN", "") or ""
    _direct = bool(getattr(sc, "HA_DIRECT_CONTROLS", False))
    _poll_interval = float(getattr(sc, "HA_POLL_INTERVAL_SEC", 12))

    _boolean_entities = dict(getattr(sc, "HA_BOOLEAN_ENTITIES", {}) or {})
    _sw_raw = getattr(sc, "HA_SWITCH_ENTITIES", {}) or {}
    _parsed_ent, _embedded_lab = _parse_ha_switch_entities(_sw_raw)
    _switch_entities = _parsed_ent
    _manual_lab = dict(getattr(sc, "HA_SWITCH_LABELS", {}) or {})
    _switch_labels = {**_embedded_lab, **_manual_lab}
    _appliance_entities = dict(getattr(sc, "HA_APPLIANCE_ENTITIES", {}) or {})
    _sensor_entities = dict(getattr(sc, "HA_SENSOR_ENTITIES", {}) or {})
    _filtered_entities = dict(getattr(sc, "HA_FILTERED_ENTITIES", {}) or {})

    _configured = bool(_url and _token and _token != "REPLACE_WITH_LONG_LIVED_ACCESS_TOKEN")
    if _direct and not _configured:
        logger.warning(
            "HA_DIRECT_CONTROLS enabled but HA_URL/HA_TOKEN not set — falling back to MQTT"
        )


def override_credentials(url: str | None = None, token: str | None = None) -> None:
    """Startup connection overrides from the settings file (win over local_config)."""
    global _url, _token, _configured
    if url:
        _url = url.rstrip("/")
    if token:
        _token = token
    _configured = bool(_url and _token and _token != "REPLACE_WITH_LONG_LIVED_ACCESS_TOKEN")


def is_direct_mode() -> bool:
    return _configured and _direct


def _default_switch_label(state_key: str) -> str:
    """Human-readable label from state_key when HA_SWITCH_LABELS has no override."""
    s = state_key
    s = s.removeprefix("home_")
    return s.replace("_", " ").upper()


def home_buttons_ui() -> list[dict[str, Any]]:
    """Home card buttons: one row per HA_SWITCH_ENTITIES entry (order preserved)."""
    rows = []
    for state_key, entity_id in _ha_fields(_switch_entities):
        label = _switch_labels.get(state_key) or _default_switch_label(state_key)
        btn_id = state_key.replace("_", "-")
        rows.append(
            {
                "id": btn_id,
                "label": label,
                "entity": entity_id,
                "state_key": state_key,
            }
        )
    return rows


def ui_config_patch() -> dict[str, Any]:
    """Partial ui_config from local_config (merged into WebSocket payloads)."""
    # An empty operator list must also clear legacy buttons in upstream MQTT state.
    return {"home_buttons": home_buttons_ui()}


def _home_switch_state(raw: str | None) -> bool | None:
    """Unknown or unavailable Home state is not an observed off state."""
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("on", "true", "yes", "1"):
            return True
        if value in ("off", "false", "no", "0"):
            return False
    return None


def is_toggle_allowed(entity_id: str) -> bool:
    """Only entity IDs listed in local_config may be toggled from the dashboard."""
    if not entity_id or not _configured:
        return False
    allowed = {entity for _, entity in _ha_fields(_boolean_entities)} | {
        entity for _, entity in _ha_fields(_switch_entities)
    }
    return entity_id in allowed


def replace_overlay(data: dict[str, Any]) -> None:
    """Replace HA overlay (used after successful poll or toggle refresh)."""
    global _overlay
    _overlay = data


def _ha_headers() -> dict:
    """Common headers for HA REST API calls."""
    return {
        "Authorization": f"Bearer {_token}",
        "Content-Type": "application/json",
    }


def _get_http_client() -> httpx.AsyncClient:
    """Get or create a shared async HTTP client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=HA_REQUEST_TIMEOUT)
    return _http_client


async def _ha_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
) -> httpx.Response | None:
    """Single helper for all HA REST calls. Returns response or None on error."""
    if not _configured:
        return None
    client = _get_http_client()
    try:
        async with asyncio.timeout(HA_REQUEST_TIMEOUT):
            resp = await client.request(
                method,
                f"{_url}{path}",
                headers=_ha_headers(),
                json=json_body,
            )
        return resp
    except (httpx.HTTPError, TimeoutError):
        logger.exception("HA request %s %s failed", method, path)
        return None


async def _get_state(client: httpx.AsyncClient, headers: dict, entity_id: str) -> str | None:
    """Read state; propagate connection failures so the entire poll is invalidated."""
    safe = quote(entity_id, safe="")
    r = await client.get(f"{_url}/api/states/{safe}", headers=headers)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if r.status_code != 200:
        return None
    try:
        data = r.json()
        return data.get("state")
    except json.JSONDecodeError:
        return None


async def _get_full_state(client: httpx.AsyncClient, headers: dict, entity_id: str) -> dict | None:
    """Read full state; a missing entity is optional, but a failed poll is not."""
    safe = quote(entity_id, safe="")
    r = await client.get(f"{_url}/api/states/{safe}", headers=headers)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if r.status_code != 200:
        return None
    try:
        doc = r.json()
        return doc if isinstance(doc, dict) and "entity_id" in doc else None
    except json.JSONDecodeError:
        return None


def _friendly_name(doc: dict) -> str:
    attrs = doc.get("attributes") or {}
    name = attrs.get("friendly_name") or doc.get("entity_id") or ""
    return str(name)


def _cover_display(doc: dict) -> dict[str, Any]:
    attrs = doc.get("attributes") or {}
    pos = attrs.get("current_position")
    if not isinstance(pos, (int, float)):
        pos = 100 if doc.get("state") == "open" else 0
    return {"entity_id": doc["entity_id"], "name": _friendly_name(doc), "position": int(pos)}


def _number_display(doc: dict) -> dict[str, Any] | None:
    value = _parse_numeric_state(doc.get("state"))
    if value is None:
        return None
    attrs = doc.get("attributes") or {}
    return {
        "entity_id": doc["entity_id"],
        "name": _friendly_name(doc),
        "value": value,
        "min": attrs.get("min", 0),
        "max": attrs.get("max", 100),
        "step": attrs.get("step", 1),
        "unit": attrs.get("unit_of_measurement") or "",
    }


def _sensor_display(doc: dict) -> dict[str, Any]:
    attrs = doc.get("attributes") or {}
    return {
        "entity_id": doc["entity_id"],
        "name": _friendly_name(doc),
        "state": str(doc.get("state") or ""),
        "unit": attrs.get("unit_of_measurement") or "",
    }


def _weather_display(doc: dict) -> dict[str, Any]:
    attrs = doc.get("attributes") or {}
    return {
        "entity_id": doc["entity_id"],
        "name": _friendly_name(doc),
        "state": str(doc.get("state") or ""),
        "temperature": attrs.get("temperature"),
        "unit": attrs.get("temperature_unit") or "",
        "forecast": attrs.get("forecast") or [],
    }


def _filtered_skeleton() -> dict[str, Any]:
    return {
        "sensors": [],
        "numbers": [],
        "covers": [],
        "media_players": [],
        "scenes": [],
        "weather": None,
    }


def build_filtered_displays(docs: dict[str, dict | None], cfg: dict[str, Any]) -> dict[str, Any]:
    """Map fetched HA state docs to the HaFilteredData display shape (pure; testable)."""
    out = _filtered_skeleton()
    for eid in cfg.get("sensors") or []:
        doc = docs.get(eid)
        if doc:
            out["sensors"].append(_sensor_display(doc))
    for eid in cfg.get("numbers") or []:
        doc = docs.get(eid)
        if doc:
            disp = _number_display(doc)
            if disp:
                out["numbers"].append(disp)
    for eid in cfg.get("covers") or []:
        doc = docs.get(eid)
        if doc:
            out["covers"].append(_cover_display(doc))
    for eid in cfg.get("media_players") or []:
        doc = docs.get(eid)
        if doc:
            out["media_players"].append(
                {
                    "entity_id": doc["entity_id"],
                    "name": _friendly_name(doc),
                    "state": str(doc.get("state") or ""),
                }
            )
    for eid in cfg.get("scenes") or []:
        doc = docs.get(eid)
        if doc:
            out["scenes"].append({"entity_id": doc["entity_id"], "name": _friendly_name(doc)})
    weather_eid = cfg.get("weather")
    wdoc = docs.get(weather_eid) if weather_eid else None
    if wdoc:
        out["weather"] = _weather_display(wdoc)
    return out


async def _fetch_filtered(client: httpx.AsyncClient, headers: dict) -> dict[str, Any]:
    """Fetch every configured rich-entity state doc and map it for the UI."""
    wanted: list[str] = []
    for key in ("sensors", "numbers", "covers", "media_players", "scenes"):
        wanted.extend(e for e in (_filtered_entities.get(key) or []) if e not in wanted)
    weather_eid = _filtered_entities.get("weather")
    if weather_eid and weather_eid not in wanted:
        wanted.append(weather_eid)

    docs: dict[str, dict | None] = {}
    for eid in wanted:
        docs[eid] = await _get_full_state(client, headers, eid)
    return build_filtered_displays(docs, _filtered_entities)


async def fetch_states_once() -> dict[str, Any]:
    """Fetch all configured entities; returns overlay dict for merging into MQTT state."""
    if not is_direct_mode():
        return {}

    headers = _ha_headers()
    out: dict[str, Any] = {"booleans": {}}

    try:
        async with httpx.AsyncClient(timeout=HA_POLL_TIMEOUT) as client:
            booleans = {}
            for key, eid in _ha_fields(_boolean_entities):
                st = await _get_state(client, headers, eid)
                booleans[key] = st == "on"
            out["booleans"] = booleans

            for key, eid in _ha_fields(_switch_entities):
                st = await _get_state(client, headers, eid)
                out[key] = _home_switch_state(st)

            for key, eid in _ha_fields(_appliance_entities):
                st = await _get_state(client, headers, eid)
                out[key] = _appliance_field_value(key, eid, st)

            out["ha_direct_connected"] = True

            for key, eid in _ha_fields(_sensor_entities):
                st = await _get_state(client, headers, eid)
                if st is not None:
                    val = _parse_numeric_state(st)
                    if val is not None:
                        out[key] = val
                    else:
                        out[key] = None
                else:
                    out[key] = None

            if _filtered_entities:
                out["ha_filtered"] = await _fetch_filtered(client, headers)
            return out

    except httpx.HTTPError as e:
        logger.warning("HA poll failed: %s", e)
        return {"booleans": {}, "ha_direct_connected": False}


def _apply_connected_overlay(merged: dict[str, Any], o: dict[str, Any]) -> None:
    """Fill merged dashboard state from a live HA overlay."""
    booleans = dict(merged.get("booleans") or {})
    overlay_booleans = o.get("booleans") or {}
    for k, _ in _ha_fields(_boolean_entities):
        booleans[k] = bool(overlay_booleans.get(k))
    merged["booleans"] = booleans
    for k, _ in _ha_fields(_switch_entities):
        merged[k] = bool(o.get(k))
        # Dynamic Home keys survive the state schema through its booleans map.
        booleans[k] = o.get(k) if isinstance(o.get(k), bool) else None
    for k, _ in _ha_fields(_appliance_entities):
        if k in o:
            merged[k] = o[k]
    for k, _ in _ha_fields(_sensor_entities):
        if k in o:
            merged[k] = o[k]
    if o.get("ha_filtered"):
        merged["ha_filtered"] = o["ha_filtered"]


def _apply_disconnected_overlay(merged: dict[str, Any]) -> None:
    """Fill merged dashboard state with safe defaults when HA is unreachable."""
    booleans = dict(merged.get("booleans") or {})
    for k, _ in _ha_fields(_boolean_entities):
        booleans[k] = False
    merged["booleans"] = booleans
    for k, _ in _ha_fields(_switch_entities):
        merged[k] = False
        booleans[k] = None
    for k, _ in _ha_fields(_appliance_entities):
        merged[k] = _appliance_fallback(k)


def merge_overlay(base: dict[str, Any]) -> dict[str, Any]:
    """Merge MQTT state with HA overlay; in direct mode HA-owned keys never fall back to MQTT."""
    merged = dict(base)
    merged.setdefault("booleans", {})
    if not is_direct_mode():
        booleans = dict(merged.get("booleans") or {})
        for key, _ in _ha_fields(_switch_entities):
            booleans[key] = None
        merged["booleans"] = booleans
        return merged

    o = _overlay
    connected = bool(o.get("ha_direct_connected"))
    merged["ha_direct_connected"] = connected

    if connected:
        _apply_connected_overlay(merged, o)
    else:
        _apply_disconnected_overlay(merged)

    return merged


async def ha_poll_loop():
    """Background task: refresh HA overlay periodically."""
    load_config()
    if not is_direct_mode():
        return

    while True:
        replace_overlay(await fetch_states_once())
        await asyncio.sleep(_poll_interval)


async def call_turn(entity_id: str, turn_on: bool) -> bool:
    """Explicit turn_on / turn_off."""
    if not _configured:
        return False

    domain = entity_id.split(".")[0]
    service = "turn_on" if turn_on else "turn_off"
    if domain not in ("input_boolean", "switch", "light"):
        return False

    resp = await _ha_request(
        "POST", f"/api/services/{domain}/{service}", json_body={"entity_id": entity_id}
    )
    return resp is not None and resp.status_code == 200


async def toggle_entity(entity_id: str) -> bool:
    """HA toggle service (no prior state required)."""
    if not _configured:
        return False

    domain = entity_id.split(".")[0]
    if domain == "input_boolean":
        svc = "input_boolean/toggle"
    elif domain == "switch":
        svc = "switch/toggle"
    elif domain == "light":
        svc = "light/toggle"
    else:
        return False

    resp = await _ha_request("POST", f"/api/services/{svc}", json_body={"entity_id": entity_id})
    return resp is not None and resp.status_code == 200


def domain_for_press(entity_id: str) -> str | None:
    if entity_id.startswith("button."):
        return "button"
    return None


async def press_entity(entity_id: str) -> bool:
    """Fire button.press."""
    resp = await _ha_request(
        "POST", "/api/services/button/press", json_body={"entity_id": entity_id}
    )
    return resp is not None and resp.status_code == 200


async def perform_action(action: str, entity: str, payload: dict[str, Any]) -> bool:
    """Dispatch configured rich HA controls through the direct HA connection."""
    if not is_direct_mode():
        raise ValueError("Direct Home Assistant controls are not enabled")
    if not isinstance(entity, str) or not entity or entity.rsplit(".", 1)[-1] in CONTROL_FLAG_KEYS:
        raise ValueError("A Home Assistant entity is required")
    domain = entity.split(".", 1)[0]
    body: dict[str, Any] = {"entity_id": entity}
    allowed = False
    service = ""
    if action == "number_set":
        allowed = domain in ("number", "input_number") and entity in _filtered_entities.get(
            "numbers", []
        )
        value = payload.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Number value must be finite")
        body["value"] = value
        service = "set_value"
    elif action == "set_cover_position":
        allowed = domain == "cover" and entity in _filtered_entities.get("covers", [])
        position = payload.get("position")
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or not math.isfinite(position)
            or not 0 <= position <= 100
            or int(position) != position
        ):
            raise ValueError("Cover position must be an integer from 0 to 100")
        body["position"] = int(position)
        service = "set_cover_position"
    elif action == "media_player":
        allowed = domain == "media_player" and entity in _filtered_entities.get("media_players", [])
        services = {"play": "media_play", "pause": "media_pause", "stop": "media_stop"}
        mp_action = payload.get("mp_action")
        service = services.get(mp_action, "") if isinstance(mp_action, str) else ""
        if not service:
            raise ValueError("Unsupported media action")
    elif action == "scene_activate":
        allowed = domain == "scene" and entity in _filtered_entities.get("scenes", [])
        service = "turn_on"
    if not allowed or not service:
        raise ValueError("Home Assistant action is not allowed for this entity")
    response = await _ha_request("POST", f"/api/services/{domain}/{service}", json_body=body)
    return response is not None and response.status_code == 200
