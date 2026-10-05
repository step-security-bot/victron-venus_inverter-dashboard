"""
Site Configuration Example

Copy this file to local_config.py and fill in your values.
local_config.py is NOT tracked by git.

Docker: place **local_config.py** on the host inside the folder you mount at **/app/config**
(set **INVERTER_DASHBOARD_CONFIG=/app/config** in compose — that path is the mount point, not a repo subfolder).
Local dev: run **scripts/init-config.sh** to create **./local_config.py** next to **server.py**, or copy this file manually.

When HA_DIRECT_CONTROLS is True, the dashboard reads toggle/switch states and
sends toggle commands directly to Home Assistant instead of relying on Cerbo MQTT
state from inverter-control. That keeps MQTT traffic smaller and lets you change
entities here without touching inverter-control.

When inverter-control uses MQTT_SLIM_STATE=True, dishwasher/washer/dryer fields are
not published on inverter/state — set HA_APPLIANCE_ENTITIES (below) so the dashboard
polls those sensors from HA REST (same entity IDs as in inverter-control site_config).
"""

# ---------------------------------------------------------------------------
# Connection
# Native energy connection is configured through environment variables:
# MQTT_HOST=Cerbo
# CERBO_PORTAL_ID=<VRM portal ID shown in GX settings>
# A silent modern Venus broker requires this ID to start notifications.
# ---------------------------------------------------------------------------
HA_URL = "http://homeassistant.local:8123"
HA_TOKEN = "REPLACE_WITH_LONG_LIVED_ACCESS_TOKEN"

# Master switch: use HA REST for switches/booleans below (recommended with MQTT_SLIM on Cerbo).
# When True, dashboard does not fall back to MQTT/Cerbo for those entity states if HA REST is down.
HA_DIRECT_CONTROLS = True

# Poll interval for HA states (seconds). Increase on slow hosts.
HA_POLL_INTERVAL_SEC = 12

# ---------------------------------------------------------------------------
# Entities — keys are WebSocket/MQTT state field names (booleans for switches/lights).
# Home card buttons are built automatically from HA_SWITCH_ENTITIES (+ optional HA_SWITCH_LABELS).
# ---------------------------------------------------------------------------
# Header mode toggles (input_boolean.*)
HA_BOOLEAN_ENTITIES = {
    "only_charging": "input_boolean.only_charging",
    "no_feed": "input_boolean.no_feed",
    "house_support": "input_boolean.house_support",
    "charge_battery": "input_boolean.charge_battery",
    "do_not_supply_charger": "input_boolean.do_not_supply_charger",
    "set_limit_to_ev_charger": "input_boolean.set_limit_to_ev_charger",
    "minimize_charging": "input_boolean.minimize_charging",
}

# Home section buttons — keys are WebSocket state keys (bool).
# Each value may be:
#   - entity id string: "switch.foo"
#   - (entity_id, "Short label") tuple
#   - dict: {"entity": "switch.example", "label": "Short", "order": 0, "enabled": True}
#     (keys "short" / "name" also work for label)
# Supported domains include switch.* and light.* (toggle via HA REST when HA_DIRECT_CONTROLS).
# Empty means no Home buttons. Equal orders preserve declaration order; disabled
# entries are neither displayed nor polled nor permitted as Home toggle targets.
HA_SWITCH_ENTITIES = {}

# Optional extra overrides if you still use plain string values above (HA_SWITCH_LABELS wins over embedded labels).
HA_SWITCH_LABELS = {}

# Dishwasher/washer/dryer telemetry — same dashboard keys as inverter/state would carry without MQTT_SLIM.
# Optional washer_power/dryer_power entries: use switch.* or sensor.* (>1 W counts as running).
HA_APPLIANCE_ENTITIES = {
    "dishwasher_running": "binary_sensor.dishwasher_running",
    "dishwasher_duration": "sensor.dishwasher_duration",
    "washer_time": "sensor.washer_remaining_time",
    "dryer_time": "sensor.dryer_remaining_time",
    # "washer_power": "sensor.washer_power_estimate",
    # "dryer_power": "sensor.dryer_power_estimate",
}

# Water card (optional — leave empty strings to skip)
# Water card — dbus-pump via Cerbo MQTT; no HA entities.

# EV card (optional — select the matching native device instances)
# EV data comes from dbus-ev (vehicle) and dbus-evcharger (wallbox) via
# Cerbo MQTT — no Home Assistant involved.
#
# Set CERBO_PORTAL_ID in the environment for reliable native MQTT bootstrap.
# Empty portal enables passive discovery; it does not disable EV or water.
# EV_INSTANCE (vehicle, dbus-ev) and EVCHARGER_INSTANCE (wallbox, dbus-evcharger)
# must match the respective local_config.py DEVICE_INSTANCE values.
#   EV_INSTANCE = 22       # vehicle Soc + Ac/Power
#   EVCHARGER_INSTANCE = 40  # wallbox Ac/Power -> ev_charging_kw


# ---------------------------------------------------------------------------
# Sensor entities — keys are WebSocket/MQTT state field names (numeric).
# These are fetched from HA REST and override the corresponding MQTT values when HA is connected.
# NOTE: Most fields now come from inverter-control MQTT (D-Bus sourced):
#   gt, g1, g2, tt, t1, t2, solar_total, mppt_total
#   battery_soc, battery_power, battery_voltage, battery_current
#   setpoint, daily_stats, mppt_chargers, batteries, mppt_individual,
#   pv_inverters (AC PV inverters of any vendor)
#   loads (Vue circuits via D-Bus acload)
#   ev_charging_kw, ev_power, car_soc (EV — dbus-ev / dbus-evcharger via Cerbo MQTT)
#   water_level, water_valve, pump_switch (water — dbus-pump via Cerbo MQTT)
# Only configure fields NOT available from the above sources:
#   - appliance timers (dishwasher, washer, dryer) when inverter-control uses MQTT_SLIM_STATE
# Example:
# HA_SENSOR_ENTITIES = {
#     "water_level": "sensor.water_tank_level",  # only if you also use HA instead of dbus-pump
# }
HA_SENSOR_ENTITIES = {}

# Rich display entities for the HA entity cards (covers/media/scenes/weather/numbers/sensors).
# Labels come from HA friendly_name. All keys optional.
# Example:
# HA_FILTERED_ENTITIES = {
#     "covers": ["cover.living_room_blinds"],
#     "media_players": ["media_player.kitchen_speaker"],
#     "scenes": ["scene.movie_night"],
#     "numbers": ["number.ev_charger_current"],
#     "sensors": ["sensor.outdoor_temperature"],
#     "weather": "weather.home",
# }
HA_FILTERED_ENTITIES = {}
