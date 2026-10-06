"""Epoch-bound notification detection, independent from banners and open tabs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

from .push_store import PushStore

DEFAULT_PREFERENCES = {"native": True, "ev": True, "water": True, "lowBattery": True}
MAX_AGE_SECONDS = 300
FUTURE_SKEW_SECONDS = 30
SAMPLE_FRESH_SECONDS = 30
MQTT_PRIMING_SECONDS = 10


def preferences(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_PREFERENCES):
        raise ValueError("Invalid notification preferences")
    if any(not isinstance(item, bool) for item in value.values()):
        raise ValueError("Invalid notification preferences")
    return dict(value)


def timestamp_ms(value: Any) -> int | None:
    """Source timestamps require explicit time zones; unknown stays unknown."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        milliseconds = stamp.timestamp() * 1000
        if not math.isfinite(milliseconds) or milliseconds <= 0:
            return None
        return int(milliseconds)
    except (ValueError, OverflowError, OSError):
        return None


def event_payload(
    kind: str, source: str, identifier: str, source_ms: int, now: float, *, title: str, body: str
) -> dict:
    identity = json.dumps(
        [kind, source, identifier, source_ms], separators=(",", ":"), ensure_ascii=False
    ).encode()
    payload = {
        "schemaVersion": 1,
        "eventKey": hashlib.sha256(identity).hexdigest(),
        "kind": kind,
        "source": source,
        "sourceTimestampMs": source_ms,
        "observedAtMs": int(now * 1000),
        "title": title[:120],
        "body": body[:1000],
        "url": "/",
    }
    while len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()) > 3072:
        payload["body"] = payload["body"][:-1]
    return payload


def validate_persisted_payload(value: Any) -> None:
    """Reject valid JSON with an invalid delivery shape before starting workers."""
    required = {
        "schemaVersion",
        "eventKey",
        "kind",
        "source",
        "sourceTimestampMs",
        "observedAtMs",
        "title",
        "body",
        "url",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Invalid persisted push payload")
    if (
        not isinstance(value["schemaVersion"], int)
        or value["schemaVersion"] != 1
        or isinstance(value["schemaVersion"], bool)
        or value["url"] != "/"
    ):
        raise ValueError("Invalid persisted push payload")
    kinds = {
        "native": {"victron", "system"},
        "ev": {"victron"},
        "water": {"victron"},
        "lowBattery": {"victron"},
        "test": {"system"},
    }
    if not isinstance(value["kind"], str) or value["source"] not in kinds.get(value["kind"], set()):
        raise ValueError("Invalid persisted push source")
    _validate_payload_fields(value)


def _validate_payload_fields(value: dict) -> None:
    for key, maximum in (("eventKey", 64), ("title", 120), ("body", 1000)):
        if not isinstance(value[key], str) or len(value[key]) > maximum:
            raise ValueError("Invalid persisted push text")
    if not re.fullmatch(r"[a-f0-9]{64}", value["eventKey"]):
        raise ValueError("Invalid persisted push identity")
    for key in ("sourceTimestampMs", "observedAtMs"):
        if (
            not isinstance(value[key], int)
            or isinstance(value[key], bool)
            or not 0 < value[key] <= 9_007_199_254_740_991
        ):
            raise ValueError("Invalid persisted push time")
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > 3072:
        raise ValueError("Invalid persisted push size")


class EventProcessor:
    """Prime silently at each connection, then consider only fresh transitions."""

    def __init__(self, store: PushStore):
        self.store = store
        self.epoch = ""
        self.started_at = 0.0
        self.native_primed = False
        self.unknown_primed: dict[str, tuple[str, str]] = {}
        self.samples: dict[str, tuple[Any, float]] = {}

    def reset(self, epoch: str, now: float) -> None:
        self.epoch = epoch
        self.started_at = now
        self.native_primed = False
        self.unknown_primed.clear()
        self.samples.clear()
        self.store.retire_epochs(epoch)

    def native(self, notifications: list[dict], now: float, *, prime: bool = False) -> list[dict]:
        output = []
        prime = prime or not self.native_primed
        present = {str(item.get("id", ""))[:200] for item in notifications[:100]}
        self.unknown_primed = {
            key: value for key, value in self.unknown_primed.items() if key in present
        }
        for notification in notifications[:100]:
            event = self._native_event(notification, now, prime)
            if event is not None:
                output.append(event)
        self.native_primed = True
        return output

    def _native_event(self, notification: dict, now: float, prime: bool) -> dict | None:
        identifier = str(notification.get("id", ""))[:200]
        occurrence = (str(notification.get("title", "")), str(notification.get("body", "")))
        source_ms = timestamp_ms(notification.get("ts"))
        if source_ms is None:
            if prime and identifier:
                self.unknown_primed[identifier] = occurrence
            return None
        source = "victron" if notification.get("source") == "victron" else "system"
        event = event_payload(
            "native",
            source,
            identifier,
            source_ms,
            now,
            title=str(notification.get("title", "")),
            body=str(notification.get("body", "")),
        )
        initial = self.unknown_primed.pop(identifier, None)
        hydrated = initial is not None and initial[0] == occurrence[0]
        if prime or hydrated:
            self.store.remember(event["eventKey"], now)
            return None
        if notification.get("level") not in ("warning", "alarm", "error"):
            return None
        source_time = source_ms / 1000
        if (
            source_time < self.started_at - FUTURE_SKEW_SECONDS
            or now - source_time > MAX_AGE_SECONDS
            or source_time - now > FUTURE_SKEW_SECONDS
        ):
            self.store.remember(event["eventKey"], now)
            return None
        return event

    def sample(
        self, kind: str, identifier: str, value: Any, now: float, *, prime: bool = False
    ) -> dict | None:
        """Caller supplies only a directly observed, selected native field."""
        key = f"{kind}:{identifier}"
        if value is None:
            self.samples.pop(key, None)
            return None
        previous = self.samples.get(key)
        self.samples[key] = (value, now)
        if prime or previous is None or not 0 <= now - previous[1] <= SAMPLE_FRESH_SECONDS:
            return None
        message = _sample_message(kind, identifier, previous[0], value)
        if message is None:
            return None
        title, body = message
        return event_payload(
            kind, "victron", identifier, int(now * 1000), now, title=title, body=body
        )


def _sample_message(kind: str, identifier: str, before: Any, value: Any) -> tuple[str, str] | None:
    if kind == "lowBattery":
        if not before > 20 >= value:
            return None
        title, body = "Low battery", f"Battery state of charge is {value:g}%"
    elif value == before:
        return None
    elif kind == "ev":
        title = "EV charging started" if value else "EV charging stopped"
        body = "Observed from the selected EV charger"
    elif kind == "water":
        label = "Water pump" if identifier.startswith("pump:") else "Water valve"
        title, body = f"{label} {'on' if value else 'off'}", "Observed from native device state"
    else:
        return None
    return title, body
