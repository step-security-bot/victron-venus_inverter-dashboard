"""Read-only adapter from actual native leaves to notification observations."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from . import config
from .cerbo import number
from .push_events import MQTT_PRIMING_SECONDS
from .push_service import PushService

EV_POWER = "Ac/Power"


@dataclass(frozen=True)
class Sample:
    """Selected value and its exact native leaf, not a dashboard-derived approximation."""

    kind: str
    identifier: str
    value: Any
    leaf: tuple[str, str, str]


def _soc_sample(ms) -> Sample | None:
    systems = ms._devices("system")
    system_id, system = systems[0] if systems else ("0", {})
    actual = number(system.get("Dc/Battery/Soc"))
    if actual is not None:
        return Sample(
            "lowBattery",
            f"system:{system_id}",
            actual if 0 <= actual <= 100 else None,
            ("system", system_id, "Dc/Battery/Soc"),
        )
    batteries = ms._devices("battery")
    selected = batteries[0] if len(batteries) == 1 else None
    instance = number(system.get("Dc/Battery/Instance"))
    if instance is not None and instance.is_integer():
        selected = next((item for item in batteries if item[0] == str(int(instance))), None)
    if selected is None:
        return None
    identifier, leaves = selected
    actual = number(leaves.get("Soc"))
    return Sample(
        "lowBattery",
        f"battery:{identifier}",
        actual if actual is not None and 0 <= actual <= 100 else None,
        ("battery", identifier, "Soc"),
    )


def selected_samples(ms) -> dict[str, Sample]:
    samples = {}
    pumps = dict(ms._devices("pump"))
    for role, instance in (
        ("pump", config.WATER_PUMP_INSTANCE),
        ("valve", config.WATER_VALVE_INSTANCE),
    ):
        identifier = str(instance)
        leaves = pumps.get(identifier, {})
        field = "State" if "State" in leaves else "Status"
        raw = number(leaves.get(field))
        samples[role] = Sample(
            "water",
            f"{role}:{identifier}",
            bool(raw) if raw in (0, 1) else None,
            ("pump", identifier, field),
        )
    chargers = ms._devices("evcharger")
    selected = None
    if config.EVCHARGER_INSTANCE is not None:
        selected = next(
            (item for item in chargers if item[0] == str(config.EVCHARGER_INSTANCE)), None
        )
    else:
        selected = next(
            (item for item in chargers if number(item[1].get(EV_POWER)) is not None), None
        )
    if selected is not None:
        identifier, leaves = selected
        power = number(leaves.get(EV_POWER))
        samples["ev"] = Sample(
            "ev",
            f"evcharger:{identifier}",
            power > 10 if power is not None else None,
            ("evcharger", identifier, EV_POWER),
        )
    soc = _soc_sample(ms)
    if soc is not None:
        samples["lowBattery"] = soc
    return samples


def native_notifications(ms) -> list[dict]:
    """Push classification waits for Type; banner hydration keeps its own UI policy."""
    known = {
        f"victron-platform-{instance}-{slot}": (
            entry.get("notif_type") if entry.get("push_type_known") is True else None
        )
        for (instance, slot), entry in ms._platform_slots.items()
    }
    result = []
    for item in ms.get_notifications():
        identifier = item.get("id", "")
        if identifier.startswith("victron-platform-") and known.get(identifier) not in (0, 1, 2):
            item = {**item, "level": "unknown"}
        result.append(item)
    return result


class PushObserver:
    """MQTT retains prime nothing; IGW first full poll silently establishes a baseline."""

    def __init__(self, service: PushService):
        self.service = service
        self.selected: dict[str, Sample] = {}
        self.epoch = ""

    def _selection(self, ms) -> dict[str, Sample]:
        processor = self.service.processor
        if self.epoch != processor.epoch:
            self.selected.clear()
            self.epoch = processor.epoch
        samples = selected_samples(ms)
        for role, previous in self.selected.items():
            current = samples.get(role)
            if current is None or current.identifier != previous.identifier:
                processor.sample(previous.kind, previous.identifier, None, time.time())
        self.selected = samples
        return samples

    def snapshot(self, ms) -> None:
        if not self.service.available:
            return
        try:
            self._snapshot(ms)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            self.service.fail()

    def _snapshot(self, ms) -> None:
        prime = self.service.connect("igw", ms)
        now = time.time()
        for event in self.service.processor.native(native_notifications(ms), now, prime=prime):
            self.service.queue(event)
        for sample in self._selection(ms).values():
            self.service.queue(
                self.service.processor.sample(
                    sample.kind, sample.identifier, sample.value, now, prime=prime
                )
            )

    def mqtt(self, ms, topic: str, *, retained: bool, payload: bytes | None = None) -> None:
        if not self.service.available:
            return
        try:
            self._mqtt(ms, topic, retained=retained, payload=payload)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            self.service.fail()

    def _mqtt(self, ms, topic: str, *, retained: bool, payload: bytes | None) -> None:
        if self.service.connection != ("mqtt", ms):
            return
        now = time.time()
        processor = self.service.processor
        priming = now - processor.started_at < MQTT_PRIMING_SECONDS
        if topic == "inverter/notifications" or "/Notifications/" in topic or "/Alarms/" in topic:
            for event in processor.native(native_notifications(ms), now, prime=retained or priming):
                self.service.queue(event)
        parts = topic.split("/", 4)
        if len(parts) < 4 or parts[0] != "N" or parts[1] != ms._portal_id:
            return
        leaf = (parts[2], parts[3], parts[4] if len(parts) == 5 else "")
        # The state reducer can reject a packet and keep its earlier cached
        # leaf. Only this packet's valid numeric measurement refreshes a push
        # baseline; invalid/missing input clears it instead of rereading cache.
        try:
            incoming = json.loads(payload) if payload else None
        except (ValueError, TypeError):
            incoming = None
        valid_measurement = isinstance(incoming, dict) and number(incoming.get("value")) is not None
        for sample in self._selection(ms).values():
            self._mqtt_sample(sample, leaf, now, retained or not valid_measurement, priming)

    def _mqtt_sample(self, sample, leaf, now, clear_measurement, priming) -> None:
        processor = self.service.processor
        if sample.value is None or (leaf[:2] == sample.leaf[:2] and leaf[2] in ("", "Connected")):
            processor.sample(sample.kind, sample.identifier, None, now)
        elif leaf == sample.leaf:
            self.service.queue(
                processor.sample(
                    sample.kind,
                    sample.identifier,
                    None if clear_measurement else sample.value,
                    now,
                    prime=priming,
                )
            )
