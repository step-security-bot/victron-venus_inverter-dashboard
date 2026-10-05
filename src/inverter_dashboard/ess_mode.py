"""Explicit, correlated ESS selection; controller telemetry owns permission."""

import math
import re
import time
from typing import Any

MODES = frozenset(
    {
        "off",
        "on",
        "optimized_with_battery_life",
        "optimized_without_battery_life",
        "keep_batteries_charged",
        "external_control",
    }
)
SERVER_FIELDS = frozenset({"ess_mode_observed_at", "ess_mode_controls_available"})


def validate_selection(body: Any) -> dict[str, str]:
    if (
        not isinstance(body, dict)
        or set(body) != {"mode", "request_id"}
        or not isinstance(body["mode"], str)
        or body["mode"] not in MODES
        or not isinstance(body["request_id"], str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", body["request_id"]) is None
    ):
        raise ValueError("Invalid ESS mode selection")
    return body


def telemetry_ready(mode: Any, observed_at: Any, dry_run: Any) -> bool:
    return (
        isinstance(mode, dict)
        and mode.get("selection_supported") is True
        and type(observed_at) in (int, float)
        and math.isfinite(observed_at)
        and 0 <= time.time() - observed_at <= 30
        and dry_run is False
    )


def validate_gateway_snapshot(snapshot: dict[str, Any]) -> None:
    capabilities = snapshot.get("capabilities")
    controller = snapshot.get("inverter")
    if not isinstance(capabilities, dict) or capabilities.get("set_ess_mode") is not True:
        raise ValueError("Update inverter-gateway to enable ESS mode selection")
    if not isinstance(controller, dict) or not telemetry_ready(
        controller.get("ess_mode"), time.time(), controller.get("dry_run")
    ):
        raise ValueError("Wait for live supported ESS telemetry with dry run disabled")
