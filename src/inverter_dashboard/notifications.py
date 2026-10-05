"""Alert banner notifications — same schema as inverter-desktop / alert-mqtt-bridge.

Schema: ``{id, level, title, body, source, ts}``

Sources:
- MQTT ``inverter/notifications`` (inverter-control, Grafana alert-mqtt-bridge)
- Venus-platform GUIv2 slots ``N/<portal>/platform/<inst>/Notifications/<slot>/*``
  (LAN MQTT or IGW snapshot ``platform`` leaves)
- Victron ``Alarms/*`` fallback when no platform Notifications have been seen
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

PLATFORM_FIELDS = frozenset(
    {
        "Description",
        "DeviceName",
        "Service",
        "DateTime",
        "Type",
        "Active",
        "Acknowledged",
        "Silenced",
    }
)


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("1", "true"):
            return True
        if t in ("0", "false"):
            return False
    return None


def _as_i64(v: Any) -> int | None:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v.strip()))
        except ValueError:
            return None
    return None


def _as_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        t = v.strip()
        return t or None
    if isinstance(v, (int, float, bool)):
        return str(v)
    return None


def _event_datetime(value: Any) -> int | None:
    """Positive, integral Venus Unix seconds; unknown values never become receipt time."""
    if isinstance(value, bool):
        return None
    try:
        # A near-integer string must not round into a different valid event and
        # clear its dismissal. Validate it exactly before converting to seconds.
        numeric = Decimal(value)
        if (
            not numeric.is_finite()
            or numeric <= 0
            or numeric > 253_402_300_799  # 9999-12-31T23:59:59Z, RFC3339's upper bound
            or numeric != numeric.to_integral_value()
        ):
            return None
        seconds = int(numeric)
        # Also reject dates outside the RFC3339 range supported by our clients.
        datetime.fromtimestamp(seconds, tz=UTC)
        return seconds
    except (InvalidOperation, TypeError, ValueError, OverflowError, OSError):
        return None


def normalize_notification(data: Any) -> dict[str, str] | None:
    """Coerce a dict into the shared banner schema; return None if unusable."""
    if not isinstance(data, dict):
        return None
    nid = str(data.get("id") or "").strip()
    title = str(data.get("title") or "").strip()
    if not nid and not title:
        return None
    level = str(data.get("level") or "info").strip().lower()
    if level not in ("info", "warning", "alarm"):
        level = "info"
    return {
        "id": nid or title,
        "level": level,
        "title": title or nid,
        "body": str(data.get("body") or ""),
        "source": str(data.get("source") or "inverter-control"),
        "ts": str(data.get("ts") or ""),
    }


def parse_platform_leaf_key(key: str) -> tuple[str, int, str] | None:
    """Parse ``<inst>/Notifications/<slot>/<Field>`` → (inst, slot, field)."""
    parts = key.split("/")
    if len(parts) != 4 or parts[1] != "Notifications":
        return None
    inst, _, slot_s, field = parts
    if field not in PLATFORM_FIELDS:
        return None
    try:
        slot = int(slot_s)
    except ValueError:
        return None
    if slot < 0 or slot > 20:
        return None
    return inst, slot, field


def _slot_level(notif_type: int | None) -> str:
    # Venus: 0=Warning, 1=Alarm, 2=Info (desktop mqtt.rs)
    if notif_type == 0:
        return "warning"
    if notif_type == 2:
        return "info"
    return "alarm"


def _slot_should_show(slot: dict[str, Any]) -> bool:
    """Mirror desktop: cleared conditions still need acknowledgement in GUIv2."""
    if slot.get("user_dismissed") or slot.get("acknowledged"):
        return False
    desc = (slot.get("description") or "").strip()
    return bool(desc)


def _slot_to_notification(inst: str, slot_n: int, slot: dict[str, Any]) -> dict[str, str] | None:
    if not _slot_should_show(slot):
        return None
    title = (slot.get("description") or "Alarm").strip()
    body = (slot.get("device_name") or slot.get("service") or "").strip()
    ts = ""
    dt = slot.get("date_time")
    if isinstance(dt, int) and dt > 0:
        try:
            ts = datetime.fromtimestamp(dt, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            ts = ""
    return {
        "id": f"victron-platform-{inst}-{slot_n}",
        "level": _slot_level(
            slot.get("notif_type") if isinstance(slot.get("notif_type"), int) else None
        ),
        "title": title,
        "body": body,
        "source": "victron",
        "ts": ts,
    }


def apply_platform_field(
    slots: dict[tuple[str, int], dict[str, Any]], key: str, value: Any
) -> bool:
    """Update in-memory platform slots from one leaf. Returns True if map changed."""
    parsed = parse_platform_leaf_key(key)
    if not parsed:
        return False
    inst, slot_n, field = parsed
    entry = slots.setdefault(
        (inst, slot_n),
        {
            "description": None,
            "device_name": None,
            "service": None,
            "date_time": None,
            "last_valid_date_time": None,
            "notif_type": None,
            "active": None,
            "acknowledged": None,
            "silenced": None,
            "user_dismissed": False,
        },
    )
    before = dict(entry)
    if field == "Description":
        entry["description"] = _as_str(value)
    elif field == "DeviceName":
        entry["device_name"] = _as_str(value)
    elif field == "Service":
        entry["service"] = _as_str(value)
    elif field == "DateTime":
        next_dt = _event_datetime(value)
        if next_dt is not None:
            if next_dt != entry.get("last_valid_date_time"):
                entry["user_dismissed"] = False
            entry["last_valid_date_time"] = next_dt
        entry["date_time"] = next_dt
    elif field == "Type":
        entry["notif_type"] = _as_i64(value)
    elif field == "Active":
        entry["active"] = _as_bool(value)
        if entry["active"] is False and before.get("active") is True:
            entry["user_dismissed"] = False
    elif field == "Acknowledged":
        entry["acknowledged"] = _as_bool(value)
    elif field == "Silenced":
        entry["silenced"] = _as_bool(value)
    return entry != before


def notifications_from_platform_slots(
    slots: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for (inst, slot_n), slot in sorted(slots.items(), key=lambda x: (x[0][0], x[0][1])):
        n = _slot_to_notification(inst, slot_n, slot)
        if n:
            out.append(n)
    return out


def sync_platform_map(
    slots: dict[tuple[str, int], dict[str, Any]],
    platform_leaves: dict[str, Any],
) -> list[dict[str, str]]:
    """Merge IGW ``platform`` leaves into slots; preserve user_dismissed sticky bits.

    Desktop keeps ``user_dismissed`` until Active clears or DateTime changes so
    AcknowledgeAll latency / re-polls cannot resurrect a banner the user closed.
    """
    prev_dismissed = {
        key: bool(slot.get("user_dismissed"))
        for key, slot in slots.items()
        if slot.get("user_dismissed")
    }
    prev_dates = {key: slot.get("last_valid_date_time") for key, slot in slots.items()}
    slots.clear()
    for key, raw in platform_leaves.items():
        value = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
        apply_platform_field(slots, key, value)
    for key, slot in slots.items():
        if slot.get("date_time") is None:
            slot["last_valid_date_time"] = prev_dates.get(key)
        if not prev_dismissed.get(key):
            continue
        # Fresh event (DateTime changed) clears sticky dismiss — apply_platform_field
        # already resets on DateTime change when updating in place; after clear+rebuild
        # compare against the previous DateTime.
        if prev_dates.get(key) != slot.get("date_time") and slot.get("date_time") is not None:
            slot["user_dismissed"] = False
        else:
            slot["user_dismissed"] = True
    return notifications_from_platform_slots(slots)


def parse_alarm_leaf_key(service_bucket: str, key: str) -> tuple[str, str] | None:
    """Parse ``<inst>/Alarms/<Name>`` under battery/vebus → (service_id, alarm_name)."""
    parts = key.split("/")
    if len(parts) < 3 or parts[1] != "Alarms":
        return None
    inst = parts[0]
    alarm_name = "/".join(parts[2:])
    if not alarm_name:
        return None
    return f"{service_bucket}_{inst}", alarm_name


def alarm_notification(
    service_id: str,
    alarm_name: str,
    value: int,
    *,
    pretty_service,
    pretty_alarm,
) -> dict[str, str] | None:
    if value not in (1, 2):
        return None
    level = "alarm" if value == 2 else "warning"
    state_txt = "Alarm" if value == 2 else "Warning"
    return {
        "id": f"victron-alarm-{service_id}-{alarm_name}",
        "level": level,
        "title": pretty_service(service_id),
        "body": f"{pretty_alarm(alarm_name)}: {state_txt}",
        "source": "victron",
        "ts": "",
    }


def mqtt_sync_platform_from_snapshot(ms: Any, platform_leaves: dict[str, Any]) -> bool:
    """IGW: rebuild platform banners from snapshot ``platform`` map."""
    if not isinstance(platform_leaves, dict):
        platform_leaves = {}
    if platform_leaves:
        ms._platform_seen = True
    before = [n for n in ms.notifications if str(n.get("id", "")).startswith("victron-platform-")]
    visible = sync_platform_map(ms._platform_slots, platform_leaves)
    ms.notifications = [
        n for n in ms.notifications if not str(n.get("id", "")).startswith("victron-platform-")
    ]
    for n in visible:
        mqtt_upsert_notification(ms, n)
    after = [n for n in ms.notifications if str(n.get("id", "")).startswith("victron-platform-")]
    return before != after


def mqtt_sync_alarms_from_snapshot(
    ms: Any, snap: dict[str, Any], pretty_service, pretty_alarm
) -> bool:
    """IGW fallback: map battery/vebus Alarms/* leaves when platform unseen."""
    if ms._platform_seen:
        return False
    changed = False
    for bucket in ("battery", "vebus"):
        leaves = snap.get(bucket) or {}
        if not isinstance(leaves, dict):
            continue
        for key, raw in leaves.items():
            parsed = parse_alarm_leaf_key(bucket, key)
            if not parsed:
                continue
            service_id, alarm_name = parsed
            value_raw = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
            try:
                value = int(float(value_raw))
            except (TypeError, ValueError):
                value = 0
            topic = f"igw/{bucket}/{key}"
            prev = ms._alarm_values.get(topic, 0)
            if prev == value:
                continue
            ms._alarm_values[topic] = value
            nid = f"victron-alarm-{service_id}-{alarm_name}"
            if value not in (1, 2):
                changed = mqtt_remove_notification_id(ms, nid) or changed
                continue
            notif = alarm_notification(
                service_id,
                alarm_name,
                value,
                pretty_service=pretty_service,
                pretty_alarm=pretty_alarm,
            )
            if notif:
                ms.push_notification(notif)
                changed = True
    return changed


def mqtt_handle_platform_notification(ms: Any, topic: str, payload: bytes) -> bool:
    """LAN MQTT: N/<portal>/platform/<inst>/Notifications/<slot>/<Field>."""
    import json

    parts = topic.split("/")
    if len(parts) < 7 or parts[2] != "platform" or parts[4] != "Notifications":
        return False
    inst = parts[3]
    try:
        slot_n = int(parts[5])
    except ValueError:
        return False
    field = parts[6]
    key = f"{inst}/Notifications/{slot_n}/{field}"
    try:
        raw = json.loads(payload.decode(), parse_float=str if field == "DateTime" else float)
    except (ValueError, UnicodeDecodeError):
        return False
    value = raw.get("value") if isinstance(raw, dict) else raw
    ms._platform_seen = True
    before = [n for n in ms.notifications if str(n.get("id", "")).startswith("victron-platform-")]
    apply_platform_field(ms._platform_slots, key, value)
    visible = notifications_from_platform_slots(ms._platform_slots)
    ms.notifications = [
        n for n in ms.notifications if not str(n.get("id", "")).startswith("victron-platform-")
    ]
    for n in visible:
        mqtt_upsert_notification(ms, n)
    after = [n for n in ms.notifications if str(n.get("id", "")).startswith("victron-platform-")]
    # Fields arrive separately: DateTime/body updates must reach browsers even
    # when the slot's id and visibility have not changed.
    return before != after


def mqtt_dismiss_notification(ms: Any, nid: str) -> bool:
    """User dismissed banner (X). Sticky for non-platform ids until a fresh id."""
    if not nid:
        return False
    changed = mqtt_remove_notification_id(ms, nid)
    if nid.startswith("victron-platform-"):
        rest = nid.removeprefix("victron-platform-")
        parts = rest.rsplit("-", 1)
        if len(parts) == 2:
            inst, slot_s = parts[0], parts[1]
            try:
                slot_n = int(slot_s)
            except ValueError:
                slot_n = -1
            entry = ms._platform_slots.get((inst, slot_n))
            if entry is not None:
                entry["user_dismissed"] = True
                changed = True
        return changed
    ms._dismissed_ids.add(nid)
    if len(ms._dismissed_ids) > 200:
        ms._dismissed_ids = set(list(ms._dismissed_ids)[-200:])
    return changed


def mqtt_push_notification(ms: Any, data: Any) -> None:
    """Upsert a notification (MqttNotification shape — desktop / alert-bridge)."""
    notif = normalize_notification(data)
    if not notif:
        return
    if notif["id"] in ms._dismissed_ids:
        return
    mqtt_upsert_notification(ms, notif)


def mqtt_upsert_notification(ms: Any, notif: dict[str, str]) -> None:
    nid = notif["id"]
    for i, existing in enumerate(ms.notifications):
        if existing.get("id") == nid:
            ms.notifications[i] = notif
            return
    ms.notifications.append(notif)
    if len(ms.notifications) > ms.NOTIFICATIONS_MAX:
        ms.notifications = ms.notifications[-ms.NOTIFICATIONS_MAX :]


def mqtt_remove_notification_id(ms: Any, nid: str) -> bool:
    before = len(ms.notifications)
    ms.notifications = [n for n in ms.notifications if n.get("id") != nid]
    return len(ms.notifications) != before
