"""Transport-independent reducer for native Venus MQTT notifications.

Keep raw leaves so totals, fallbacks and null invalidation behave identically
for incremental MQTT messages and complete inverter-gateway snapshots.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from . import config

CERBO_KINDS = (
    "system",
    "grid",
    "battery",
    "solarcharger",
    "pvinverter",
    "vebus",
    "acload",
    "tank",
    "pump",
    "ev",
    "evcharger",
    "settings",
)
KEEPALIVE_INTERVAL_SECS = 20
NATIVE_FRESHNESS_SECS = 120
NATIVE_EV_KEYS = frozenset(
    {
        "car_soc",
        "ev_power",
        "car_charging_power",
        "ev_charging_kw",
        "ev_charging_power",
        "ev_present",
        "evcharger_present",
        "discovered_water_ev",
    }
)
NATIVE_SECTION_KEYS = NATIVE_EV_KEYS | frozenset(
    {
        "battery_soc",
        "loads",
        "load_names",
        "water_level",
        "water_valve",
        "pump_switch",
        "water_valve_mode",
        "pump_mode",
        "water_pump_mode",
    }
)
CERBO_OWNED_KEYS = frozenset(
    {
        "g1",
        "g2",
        "g3",
        "gt",
        "t1",
        "t2",
        "t3",
        "tt",
        "grid_available",
        "grid_l1_available",
        "grid_l2_available",
        "grid_l3_available",
        "bv",
        "bc",
        "bp",
        "battery_soc",
        "battery_power",
        "battery_voltage",
        "battery_current",
        "batteries",
        "solar_total",
        "pv_total",
        "mppt_total",
        "mppt_data",
        "mppt_individual",
        "mppt_chargers",
        "pv_inverter_total",
        "pv_inverter_individual",
        "pv_inverter_powers",
        "pv_inverters",
        "loads",
        "load_names",
        "setpoint",
        "inverter_state",
        "ev_power",
        "car_soc",
        "ev_charging_kw",
        "ev_charging_power",
        "ev_present",
        "evcharger_present",
        "discovered_water_ev",
        "ess_mode",
        "water_level",
        "water_valve",
        "pump_switch",
        "water_valve_mode",
        "pump_mode",
        "water_pump_mode",
    }
)
COLLECTION_DEFAULTS = {
    "batteries": [],
    "mppt_chargers": [],
    "mppt_data": [],
    "mppt_individual": [],
    "pv_inverters": [],
    "pv_inverter_individual": [],
    "pv_inverter_powers": [],
    "loads": {},
    "load_names": {},
    "discovered_water_ev": [],
    "ev_present": False,
    "evcharger_present": False,
}
ESS_PATHS = ("Settings/CGwacs/Hub4Mode", "Settings/CGwacs/BatteryLife/State")
INVERTER_STATES = {
    0: "Off",
    1: "Low Power",
    2: "Fault",
    3: "Bulk",
    4: "Absorption",
    5: "Float",
    6: "Storage",
    7: "Equalize",
    8: "Passthru",
    9: "Inverting",
    10: "Power assist",
    11: "Power supply",
    252: "External control",
}
BATTERY_PATHS = {
    "Soc": "soc",
    "Dc/0/Voltage": "voltage",
    "Dc/0/Current": "current",
    "Dc/0/Power": "power",
    "Dc/0/Temperature": "temperature",
    "System/MinCellVoltage": "min_cell_voltage",
    "System/MaxCellVoltage": "max_cell_voltage",
    "TimeToGo": "time_to_go_seconds",
    "ConsumedAmphours": "consumed_amphours",
    "InstalledCapacity": "capacity",
}


def number(value: Any) -> float | None:
    """MQTT numbers are JSON numbers; booleans, NaN and strings are not watts."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            value = float(value)
        except (OverflowError, ValueError):
            return None
        if math.isfinite(value):
            return value
    return None


def voltage_soc(voltage: float) -> int:
    """Desktop bank SoC: 40–54.4 V, clamped, with Rust's half-up rounding."""
    percent = min(100.0, max(0.0, (voltage - 40.0) / (54.4 - 40.0) * 100.0))
    return math.floor(percent + 0.5)


def parse_cerbo_payload(payload: bytes) -> Any:
    """Return the Venus ``value`` field, or None for malformed messages."""
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    return data.get("value") if isinstance(data, dict) else None


def state_from_current(amps: float) -> str:
    if amps > 0.5:
        return "Charging"
    if amps < -0.5:
        return "Discharging"
    return "Idle"


def _sort_key(instance: str) -> tuple[int, int | str]:
    return (0, int(instance)) if instance.isdigit() else (1, instance)


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _first_number(*values: Any) -> float | None:
    return next((v for value in values if (v := number(value)) is not None), None)


def _sum_known(values) -> float | None:
    known = [v for value in values if (v := number(value)) is not None]
    return number(sum(known)) if known else None


def _power(leaves: dict[str, Any], prefix: str = "Ac") -> float | None:
    """Prefer published total, including zero; otherwise sum distinct phases."""
    total = number(leaves.get(f"{prefix}/Power"))
    if total is not None:
        return total
    return _sum_known(leaves.get(f"{prefix}/L{i}/Power") for i in (1, 2, 3))


def _identity(instance: str, leaves: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"instance": instance}
    for key, val in (
        ("name", _text(leaves.get("CustomName")) or _text(leaves.get("ProductName"))),
        ("serial", _text(leaves.get("Serial"))),
    ):
        if val is not None:
            result[key] = val
    return result


class CerboOverlayMixin:
    """Native telemetry reducer used by the MQTT server and IGW snapshot path."""

    def _init_cerbo(self) -> None:
        self._cerbo_devices: dict[str, dict[str, dict[str, Any]]] = {}
        self._cerbo_claimed_keys: set[str] = set()
        self._cerbo_overlay: dict[str, Any] = {}
        self._native_observations: dict[str, tuple[float, float]] = {}
        self._native_last_emit: float | None = None
        self._water_mode_observations: dict[str, float] = {}
        self._native_invalidated: set[str] = set()

    def _note_native_observation(self, source: str) -> None:
        """Record transport receipt, not the sensor's measurement time."""
        self._native_observations[source] = (time.time(), time.monotonic())
        self._native_invalidated.discard(source)

    def native_telemetry(self, source: str, connected: bool) -> dict[str, Any]:
        """Describe native transport freshness independently of controller ticks."""
        observation = self._native_observations.get(source)
        quality = "unknown"
        if observation is not None:
            quality = (
                "live"
                if connected
                and source not in self._native_invalidated
                and time.monotonic() - observation[1] <= NATIVE_FRESHNESS_SECS
                else "stale"
            )
        return {
            "source": source,
            "observed_at": round(observation[0] * 1000) if observation is not None else None,
            "timestamp_source": "local_receipt",
            "quality": quality,
        }

    def clear_cerbo_state(self) -> None:
        """Invalidate observations after disconnect; await a fresh full publish."""
        self._cerbo_devices.clear()
        self._water_mode_observations.clear()
        self._native_invalidated.update(self._native_observations)
        self._apply_cerbo_overlays()

    def replace_cerbo_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Replace all native leaves from an IGW snapshot, including removals."""
        devices: dict[str, dict[str, dict[str, Any]]] = {}
        for kind in CERBO_KINDS:
            values = snapshot.get(kind)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if not isinstance(key, str) or "/" not in key:
                    continue
                instance, path = key.split("/", 1)
                if (
                    not instance
                    or not path
                    or not self._valid_path_value(kind, path, value)
                    or not self._known_path(kind, path)
                ):
                    continue
                devices.setdefault(kind, {}).setdefault(instance, {})[path] = value
                if value is None:
                    self._claim_invalid_leaf(kind, instance, path)
        self._cerbo_devices = devices
        self._water_mode_observations = {
            instance: time.monotonic()
            for instance, leaves in self._devices("pump")
            if number(leaves.get("Mode")) in (0, 1, 2)
        }
        self._note_native_observation("igw")
        self._apply_cerbo_overlays()

    @staticmethod
    def _valid_path_value(kind: str, path: str, value: Any) -> bool:
        # Invalid connection flags must invalidate an earlier connected device.
        if path == "Connected":
            return True
        if value is None or number(value) is not None:
            return True
        return isinstance(value, str) and path in (
            "CustomName",
            "ProductName",
            "Serial",
            "System/MinVoltageCellId",
            "System/MaxVoltageCellId",
            "BatteryService",
            "ActiveBatteryService",
            "AutoSelectedBatteryService",
        )

    def _handle_cerbo_device(self, topic: str, payload: bytes, *, retained: bool = False) -> bool:  # pylint: disable=too-many-return-statements
        parts = topic.split("/")
        if len(parts) < 4 or parts[0] != "N" or not parts[1] or not parts[3]:
            return False
        if self._portal_id and parts[1] != self._portal_id:
            return False
        kind, instance = parts[2], parts[3]
        if kind not in CERBO_KINDS:
            return False
        path = "/".join(parts[4:])
        if kind == "pump" and path == "Mode":
            # Any current-source report supersedes the old command authority;
            # only a valid nonretained Mode below can establish it again.
            self._water_mode_observations.pop(instance, None)
        if not payload:
            devices = self._cerbo_devices.get(kind, {})
            if instance not in devices:
                return False
            # dbus-flashmq clears each service leaf with an empty payload when
            # the service disappears. Remove it immediately on the first such
            # notification; JSON {"value": null} invalidates only one leaf.
            devices.pop(instance)
            if kind == "pump":
                self._water_mode_observations.pop(instance, None)
        else:
            if not path:
                return False
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                return False
            if not isinstance(data, dict) or "value" not in data:
                return False
            value = data["value"]
            if not self._valid_path_value(kind, path, value):
                return False
            if not self._known_path(kind, path):
                return False
            self._cerbo_devices.setdefault(kind, {}).setdefault(instance, {})[path] = value
            self._observe_water_mode(kind, instance, path, value, retained)
            if value is None:
                self._claim_invalid_leaf(kind, instance, path)
        before = dict(self.current_state)
        self._note_native_observation("mqtt")
        self._apply_cerbo_overlays()
        now = time.monotonic()
        # Refresh the footer for unchanged valid notifications, at most once
        # per second unless a measurement itself changed.
        changed = self.current_state != before
        if changed or self._native_last_emit is None or now - self._native_last_emit >= 1:
            self._native_last_emit = now
            return True
        return False

    def _observe_water_mode(self, kind, instance, path, value, retained) -> None:
        if kind != "pump":
            return
        if path == "Connected" and number(value) != 1:
            self._water_mode_observations.pop(instance, None)
        if path == "Mode":
            self._water_mode_observations.pop(instance, None)
            if not retained and number(value) in (0, 1, 2):
                self._water_mode_observations[instance] = time.monotonic()

    def water_mode_fresh(self, instance: str) -> bool:
        """Only an accepted Mode observation from this source permits a write."""
        observed = self._water_mode_observations.get(instance)
        return observed is not None and 0 <= time.monotonic() - observed <= 30

    def _claim_invalid_leaf(self, kind: str, instance: str, path: str) -> None:
        """An explicit unknown is authoritative even before the first sample."""
        keys: set[str] = set()
        selected_instances = {
            "tank": config.WATER_TANK_INSTANCE,
            "ev": config.EV_INSTANCE,
            "evcharger": config.EVCHARGER_INSTANCE,
        }
        if (
            kind in selected_instances
            and selected_instances[kind] is not None
            and instance != str(selected_instances[kind])
        ):
            return
        if kind in ("grid", "vebus"):
            for i in (1, 2, 3):
                if path in (f"Ac/L{i}/Power", f"Ac/ActiveIn/L{i}/Power", f"Ac/ActiveIn/L{i}/P"):
                    keys.update((f"g{i}", "gt"))
            if kind == "grid" and path == "Ac/Power":
                keys.add("gt")
        if kind == "pump":
            if instance == str(config.WATER_PUMP_INSTANCE):
                keys.update(
                    ("pump_mode", "water_pump_mode") if path == "Mode" else ("pump_switch",)
                )
            elif instance == str(config.WATER_VALVE_INSTANCE):
                keys.update(("water_valve_mode",) if path == "Mode" else ("water_valve",))
        if kind == "system":
            for i in (1, 2, 3):
                if path == f"Ac/Grid/L{i}/Power":
                    keys.update((f"g{i}", "gt"))
                if path in (
                    f"Ac/Consumption/L{i}/Power",
                    f"Ac/ConsumptionOnInput/L{i}/Power",
                    f"Ac/ConsumptionOnOutput/L{i}/Power",
                ):
                    keys.update((f"t{i}", "tt"))
            if path == "Dc/Pv/Power":
                keys.update(("mppt_total", "pv_total", "solar_total"))
            if path.startswith("Ac/PvOn") and path.endswith("/Power"):
                keys.update(("pv_inverter_total", "solar_total"))
        if kind in ("battery", "system"):
            if path == ("Dc/Battery/Voltage" if kind == "system" else "Dc/0/Voltage"):
                keys.add("battery_soc")
            prefix = "Dc/Battery/" if kind == "system" else "Dc/0/"
            for field, leaf, alias in (
                ("soc", "Soc", None),
                ("voltage", "Voltage", "bv"),
                ("current", "Current", "bc"),
                ("power", "Power", "bp"),
            ):
                expected = "Soc" if field == "soc" and kind == "battery" else prefix + leaf
                if path == expected:
                    keys.add(f"battery_{field}")
                    if alias:
                        keys.add(alias)
        if kind == "solarcharger" and path in ("Yield/Power", "Dc/0/Power"):
            keys.update(("mppt_total", "pv_total", "solar_total"))
        if kind == "pvinverter" and path.endswith("/Power"):
            keys.update(("pv_inverter_total", "solar_total"))
        if kind == "vebus":
            if path == "State":
                keys.add("inverter_state")
            if path.startswith("Hub4/") and path.endswith("/AcPowerSetpoint"):
                keys.add("setpoint")
        for service, leaf, fields in (
            ("ev", "Soc", ("car_soc",)),
            ("ev", "Ac/Power", ("ev_power",)),
            ("evcharger", "Ac/Power", ("ev_charging_kw", "ev_charging_power")),
            ("evcharger", "Soc", ("car_soc",)),
            ("tank", "Level", ("water_level",)),
        ):
            if kind == service and path == leaf:
                keys.update(fields)
        if kind == "settings" and path in ESS_PATHS:
            keys.add("ess_mode")
        self._cerbo_claimed_keys.update(keys)

    @staticmethod
    def _known_path(kind: str, path: str) -> bool:  # pylint: disable=too-many-return-statements
        if path in ("ProductName", "CustomName", "Serial", "Connected"):
            return True
        if kind == "battery":
            return path in BATTERY_PATHS or path in (
                "System/MinVoltageCellId",
                "System/MaxVoltageCellId",
                "State",
            )
        if kind == "system":
            return path.startswith(("Ac/", "Dc/Battery/", "Dc/Pv/")) or path in (
                "BatteryService",
                "ActiveBatteryService",
                "AutoSelectedBatteryService",
            )
        if kind == "solarcharger":
            return path in ("Yield/Power", "Dc/0/Power", "Dc/0/Voltage", "Dc/0/Current", "Pv/V")
        if kind in ("acload", "pvinverter", "grid"):
            return path.startswith("Ac/")
        if kind == "vebus":
            return path.startswith(("Ac/", "Hub4/", "Dc/")) or path in ("State", "Mode")
        if kind == "tank":
            return path == "Level"
        if kind == "pump":
            return path in ("State", "Status", "Mode")
        if kind == "settings":
            return path in ESS_PATHS
        return path in ("Soc", "Ac/Power")

    def _devices(self, kind: str):
        values = self._cerbo_devices.get(kind, {})
        return [
            (i, values[i])
            for i in sorted(values, key=_sort_key)
            if "Connected" not in values[i] or number(values[i]["Connected"]) == 1
        ]

    def _apply_cerbo_overlays(self) -> None:
        overlay: dict[str, Any] = {}
        systems = self._devices("system")
        system = dict(systems).get("0", systems[0][1] if systems else {})
        vebuses = self._devices("vebus")
        vebus = vebuses[0][1] if vebuses else {}
        self._apply_ac(overlay, system, vebus)
        self._apply_batteries(overlay, system)
        self._apply_solar(overlay, system)
        self._apply_loads(overlay)
        self._apply_ev_water(overlay)
        self._apply_ess(overlay)
        setpoints = [vebus.get(f"Hub4/L{i}/AcPowerSetpoint") for i in (1, 2, 3)]
        if (value := _sum_known(setpoints)) is not None:
            overlay["setpoint"] = value
        if (code := number(vebus.get("State"))) is not None:
            overlay["inverter_state"] = INVERTER_STATES.get(int(code), f"? ({int(code)})")

        # A formerly observed field stays unavailable after null/removal until a
        # native replacement arrives. Old controller mirrors must not revive it.
        self._cerbo_claimed_keys.update(overlay)
        for key in self._cerbo_claimed_keys:
            self.current_state[key] = overlay.get(key, COLLECTION_DEFAULTS.get(key))
        self._cerbo_overlay = overlay
        if self._cerbo_claimed_keys or self.current_state:
            self.current_state["telemetry_available"] = {
                key: self.current_state.get(key) is not None
                for key in CERBO_OWNED_KEYS
                if key not in COLLECTION_DEFAULTS
            }

    def _apply_ac(self, out, system, vebus) -> None:
        meters = self._devices("grid")
        grid = meters[0][1] if meters else {}
        # A disconnected AC input must not supply a stale grid fallback.
        input_source = number(vebus.get("Ac/ActiveIn/ActiveInput"))
        connected = number(vebus.get("Ac/ActiveIn/Connected"))
        use_input = (
            connected != 0
            and input_source != 240
            and number(system.get("Ac/ActiveIn/Source")) in (1, 3)
        )
        for i in (1, 2, 3):
            path = f"Ac/Grid/L{i}/Power"
            value = (
                number(system[path])
                if path in system
                else _first_number(
                    grid.get(f"Ac/L{i}/Power"),
                    vebus.get(f"Ac/ActiveIn/L{i}/P") if use_input else None,
                    vebus.get(f"Ac/ActiveIn/L{i}/Power") if use_input else None,
                )
            )
            if path in system or value is not None:
                out[f"grid_l{i}_available"] = value is not None
                out[f"g{i}"] = value
            consumption = _sum_known(
                [
                    system.get(f"Ac/ConsumptionOnInput/L{i}/Power"),
                    system.get(f"Ac/ConsumptionOnOutput/L{i}/Power"),
                ]
            )
            value = _first_number(
                system.get(f"Ac/Consumption/L{i}/Power"),
                consumption,
            )
            if value is not None:
                out[f"t{i}"] = value
        total = _sum_known(out.get(f"g{i}") for i in (1, 2, 3))
        # A meter aggregate is useful when no phase powers were published.
        if total is None and not any(f"Ac/Grid/L{i}/Power" in system for i in (1, 2, 3)):
            total = _power(grid)
        if total is not None:
            out["gt"] = total
            out["grid_available"] = True
        elif any(f"Ac/Grid/L{i}/Power" in system for i in (1, 2, 3)):
            out["grid_available"] = False
        elif connected is not None:
            out["grid_available"] = bool(connected)
        total = _sum_known(out.get(f"t{i}") for i in (1, 2, 3))
        if total is not None:
            out["tt"] = total

    def _apply_batteries(self, out, system) -> None:
        batteries = []
        for instance, leaves in self._devices("battery"):
            entry = _identity(instance, leaves)
            for path, key in BATTERY_PATHS.items():
                if (value := number(leaves.get(path))) is not None:
                    entry[key] = value
            for path, key in (
                ("System/MinVoltageCellId", "min_voltage_cell_id"),
                ("System/MaxVoltageCellId", "max_voltage_cell_id"),
            ):
                value = leaves.get(path)
                if value is not None and (isinstance(value, str) or number(value) is not None):
                    entry[key] = str(value)
            if "current" in entry:
                entry["state"] = state_from_current(entry["current"])
            if (
                "power" not in entry
                and "voltage" in entry
                and "current" in entry
                and (power := number(entry["voltage"] * entry["current"])) is not None
            ):
                entry["power"] = power
            if (seconds := entry.get("time_to_go_seconds")) is not None and seconds >= 0:
                minutes = int(seconds / 60)
                entry["time_to_go"] = f"{minutes // 60}h {minutes % 60:02d}m"
            if len(entry) > 1:
                batteries.append(entry)
        self._batteries = {b["instance"]: b for b in batteries}
        if batteries:
            out["batteries"] = batteries
        # Use the explicitly selected monitor, or the only battery service.
        # Device names cannot distinguish overlapping BMS and shunt readings.
        selected = batteries[0] if len(batteries) == 1 else {}
        if (instance := number(system.get("Dc/Battery/Instance"))) is not None:
            selected = self._batteries.get(str(int(instance)), {})
        for field, path in (
            ("voltage", "Voltage"),
            ("current", "Current"),
            ("power", "Power"),
        ):
            value = _first_number(system.get(f"Dc/Battery/{path}"), selected.get(field))
            if value is not None:
                out[f"battery_{field}"] = value
        # The shunt's reported SoC counter is not the main bank percentage.
        # Keep real device SoCs in batteries, and derive the bank tile locally.
        shunts = [entry for entry in batteries if "shunt" in entry.get("name", "").lower()]
        shunt = next((entry for entry in shunts if "voltage" in entry), shunts[0] if shunts else {})
        bank_voltage = _first_number(shunt.get("voltage"), system.get("Dc/Battery/Voltage"))
        if bank_voltage is not None:
            out["battery_soc"] = voltage_soc(bank_voltage)
            out["battery_voltage"] = bank_voltage
        if shunt:
            for field in ("current", "power"):
                if field in shunt:
                    out[f"battery_{field}"] = shunt[field]
        for field, alias in (("voltage", "bv"), ("current", "bc"), ("power", "bp")):
            if f"battery_{field}" in out:
                out[alias] = out[f"battery_{field}"]

    def _apply_solar(self, out, system) -> None:
        chargers = []
        for instance, leaves in self._devices("solarcharger"):
            entry = _identity(instance, leaves)
            for path, key in (("Pv/V", "pv_voltage"), ("Dc/0/Current", "current")):
                if (value := number(leaves.get(path))) is not None:
                    entry[key] = value
            voltage = number(leaves.get("Dc/0/Voltage"))
            current = number(leaves.get("Dc/0/Current"))
            product = voltage * current if voltage is not None and current is not None else None
            power = _first_number(leaves.get("Yield/Power"), leaves.get("Dc/0/Power"), product)
            if power is not None:
                entry["power"] = power
            if len(entry) > 1:
                chargers.append(entry)
        self._chargers = {c["instance"]: c for c in chargers}
        if chargers:
            out["mppt_chargers"] = chargers
            out["mppt_data"] = chargers
        powers = [c["power"] for c in chargers if "power" in c]
        mppt = _first_number(system.get("Dc/Pv/Power"), _sum_known(powers))
        if powers:
            out["mppt_individual"] = powers
        if mppt is not None:
            out["mppt_total"] = mppt
            out["pv_total"] = mppt
        inverters = []
        for instance, leaves in self._devices("pvinverter"):
            entry = _identity(instance, leaves)
            if (value := _power(leaves)) is not None:
                entry["power"] = value
            for path, key in (("Ac/L1/Voltage", "voltage"), ("Ac/L1/Current", "current")):
                if (value := number(leaves.get(path))) is not None:
                    entry[key] = value
            if len(entry) > 1:
                inverters.append(entry)
        self._pv_inverters = {p["instance"]: p for p in inverters}
        if inverters:
            out["pv_inverters"] = inverters
        powers = [p["power"] for p in inverters if "power" in p]
        if powers:
            out["pv_inverter_individual"] = powers
            out["pv_inverter_powers"] = powers
        ac_pv = _sum_known(
            system.get(f"Ac/PvOn{position}/L{i}/Power")
            for position in ("Grid", "Output", "Genset")
            for i in (1, 2, 3)
        )
        ac_pv = _first_number(ac_pv, _sum_known(powers))
        if ac_pv is not None:
            out["pv_inverter_total"] = ac_pv
        # Never add a stale legacy total to a native component.
        if (total := _sum_known((mppt, ac_pv))) is not None:
            out["solar_total"] = total

    def _apply_loads(self, out) -> None:
        loads = {}
        names = {}
        self._acload_powers = {}
        self._acload_names = {}
        self._acload_product_names = {}
        for instance, leaves in self._devices("acload"):
            power = _power(leaves)
            if power is None:
                continue
            name = _text(leaves.get("CustomName")) or _text(leaves.get("ProductName"))
            names[instance] = name or f"AC Load {instance}"
            loads[instance] = power
            self._acload_powers[instance] = power
            self._acload_names[instance] = names[instance]
        if loads:
            out["loads"] = loads
            out["load_names"] = names

    def _apply_ev_water(self, out) -> None:
        tank = dict(self._devices("tank")).get(str(config.WATER_TANK_INSTANCE), {})
        if (value := number(tank.get("Level"))) is not None:
            # Victron tank Level is percent, including legitimate 0..1%.
            out["water_level"] = value
        self._apply_ev(out)
        inventory = self._water_ev_inventory()
        if inventory or "discovered_water_ev" in self._cerbo_claimed_keys:
            out["discovered_water_ev"] = inventory
        self._apply_pumps(out)

    def _apply_ev(self, out) -> None:
        vehicle = self._selected_ev("ev", config.EV_INSTANCE)
        charger = self._selected_ev("evcharger", config.EVCHARGER_INSTANCE)
        if "Soc" in vehicle or "Soc" in charger:
            fallback_soc = charger.get("Soc") if config.EV_INSTANCE is None else None
            out["car_soc"] = _first_number(vehicle.get("Soc"), fallback_soc)
        if "Ac/Power" in vehicle:
            out["ev_power"] = number(vehicle["Ac/Power"])
        if "Ac/Power" in charger:
            power = number(charger["Ac/Power"])
            out["ev_charging_power"] = power
            out["ev_charging_kw"] = power / 1000 if power is not None else None
        for kind, selected in (("ev", vehicle), ("evcharger", charger)):
            if kind in self._cerbo_devices or f"{kind}_present" in self._cerbo_claimed_keys:
                out[f"{kind}_present"] = bool(selected)

    def _water_ev_inventory(self) -> list[dict[str, Any]]:
        inventory = []
        for kind in ("tank", "pump", "ev", "evcharger"):
            for instance, leaves in self._devices(kind):
                inventory.append(self._water_ev_device(kind, instance, leaves))
        return inventory

    @staticmethod
    def _water_ev_device(kind: str, instance: str, leaves: dict[str, Any]) -> dict[str, Any]:
        item = {"kind": kind, **_identity(instance, leaves)}
        item["instance"] = int(instance) if instance.isdigit() else instance
        for field, path in (("soc", "Soc"), ("power", "Ac/Power")):
            if (value := number(leaves.get(path))) is not None:
                item[field] = value
        return item

    def _apply_pumps(self, out) -> None:
        for instance, key, mode_key in (
            (config.WATER_VALVE_INSTANCE, "water_valve", "water_valve_mode"),
            (config.WATER_PUMP_INSTANCE, "pump_switch", "pump_mode"),
        ):
            leaves = dict(self._devices("pump")).get(str(instance), {})
            value = number(leaves.get("State" if "State" in leaves else "Status"))
            if value is not None:
                out[key] = value >= 0.5
            if (mode := number(leaves.get("Mode"))) in (0, 1, 2):
                out[mode_key] = int(mode)
                if mode_key == "pump_mode":
                    out["water_pump_mode"] = int(mode)

    def _selected_ev(self, kind: str, configured: int | None) -> dict[str, Any]:
        devices = self._devices(kind)
        if configured is not None:
            return dict(devices).get(str(configured), {})
        usable = [
            leaves
            for _, leaves in devices
            if any(number(leaves.get(path)) is not None for path in ("Soc", "Ac/Power"))
        ]
        if usable:
            return usable[0]
        if devices:
            return devices[0][1]
        return {}

    def _apply_ess(self, out) -> None:
        settings = self._devices("settings")
        leaves = dict(settings).get("0", settings[0][1] if settings else {})
        if not any(path in leaves for path in ESS_PATHS):
            return
        hub4, battery_life = (number(leaves.get(path)) for path in ESS_PATHS)
        if (
            hub4 is None
            or not hub4.is_integer()
            or (hub4 == 1 and (battery_life is None or not battery_life.is_integer()))
        ):
            out["ess_mode"] = None
            return
        mode = int(hub4)
        name = f"Unknown ({mode})"
        if mode == 3:
            name = "External control"
        elif mode == 1:
            if battery_life in (0, 10):
                name = "Optimized without BatteryLife"
            elif battery_life == 9:
                name = "Keep batteries charged"
            else:
                name = "Optimized (BatteryLife)"
        out["ess_mode"] = {
            "hub4_mode": mode,
            "battery_life_state": int(battery_life) if battery_life is not None else None,
            "mode_name": name,
            "is_external": mode == 3,
        }
