"""Correlated controller commands and server-owned observation timestamps."""

import json
import math
import re
import time
from typing import Any

SERVER_FIELDS = frozenset(
    {
        "setpoint_override_observed_at",
        "setpoint_override_controls_available",
        "electricity_tariff_observed_at",
        "electricity_tariff_controls_available",
        "grid_backup_observed_at",
    }
)


def request_id_valid(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is not None


def int32_or_null(value: Any) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and -(2**31) <= value < 2**31
    )


def override_status(value: Any) -> dict[str, Any] | None:
    """Unknown support is distinct from an acknowledged inactive value:null."""
    if not isinstance(value, dict) or not {"value", "last_error", "request_id"} <= value.keys():
        return None
    if not int32_or_null(value["value"]):
        return None
    if any(
        value[key] is not None and not isinstance(value[key], str)
        for key in ("last_error", "request_id")
    ):
        return None
    return {key: value[key] for key in ("value", "last_error", "request_id")}


def fresh(observed_at: Any) -> bool:
    return (
        type(observed_at) in (int, float)
        and math.isfinite(observed_at)
        and 0 <= time.time() - observed_at <= 30
    )


def validate_override(body: Any) -> dict[str, Any]:
    if (
        not isinstance(body, dict)
        or set(body) != {"value", "request_id"}
        or not int32_or_null(body["value"])
        or not request_id_valid(body["request_id"])
    ):
        raise ValueError("Setpoint override requires an int32 value or null and request ID")
    return body


def encode_body(body: dict[str, Any]) -> str:
    """Use the exact UTF-8 representation for both wire transports and limits."""
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def validate_tariff(body: Any) -> dict[str, Any]:
    if (
        not isinstance(body, dict)
        or set(body) != {"plan", "revision", "request_id"}
        or not request_id_valid(body["request_id"])
        or not isinstance(body["revision"], str)
        or re.fullmatch(r"[a-f0-9]{64}", body["revision"]) is None
    ):
        raise ValueError("Invalid controller tariff command")
    if body["plan"] is not None and not isinstance(body["plan"], dict):
        raise ValueError("Controller tariff plan must be an object or null")
    try:
        encoded = encode_body(body).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Invalid controller tariff JSON") from error
    if len(encoded) > 100_000:
        raise ValueError("Controller tariff command exceeds 100000 bytes")
    return body


def tariff_status(state: dict[str, Any]) -> dict[str, Any]:
    ui = state.get("ui_config")
    status = ui.get("electricity_tariff_status") if isinstance(ui, dict) else None
    return status if isinstance(status, dict) else {}


def observe(state, incoming: dict[str, Any], *, retained: bool) -> None:
    """Only controller observations renew command support; native/HA ticks cannot."""
    observed = None if retained else time.time()
    if "setpoint_override" in incoming:
        status = override_status(incoming["setpoint_override"])
        state.current_state["setpoint_override"] = status
        state._setpoint_override_observed_at = observed if status is not None else None
        if status is not None and not retained:
            state._override_observation_sequence += 1
    if "ui_config" in incoming:
        state._electricity_tariff_observed_at = observed
    if "grid_backup" in incoming:
        backup = incoming["grid_backup"]
        measured = backup.get("measurement_time") if isinstance(backup, dict) else None
        state.current_state["grid_backup_observed_at"] = (
            measured if type(measured) in (int, float) and math.isfinite(measured) else None
        )
