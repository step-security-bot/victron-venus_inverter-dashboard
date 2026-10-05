# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.26] - Unreleased

### Fixed
- Preserve native Victron event times through MQTT and gateway replays, including
  late timestamp fields. Missing or invalid event times remain unknown.
- Refresh notification ages while idle and after returning to the page, with the
  exact local date and timezone available. Replayed history keeps its source time.
- Keep dismissed alarms hidden when incomplete or invalid timestamps are replayed,
  and reject fractional timestamp strings without rounding them into fresh events.

## [1.8.19] - 2026-09-11

### Fixed
- Restore standalone binaries for Linux x86_64, macOS Intel, macOS Apple Silicon,
  and Windows x86_64 with their runtime dependencies and embedded Vue interface.
- Preserve the application version in frozen binaries and publish ZIP archives
  with SHA256 checksums for each platform.

## [1.8.17] - 2026-09-11

### Changed
- **`GET /api/state`**: include live Cerbo/IGW tiles (`gt`/`tt`/`battery_*`/`solar_total`/`loads`/…)
  alongside health/gateway counters so monitors and SPA HTTP fallback can verify non-zero
  data without a WebSocket.
- Pin Vue SPA download in docker-publish to **inverter-dashboard-vue v2.1.7** (HTTP `/api/state` poll when WS silent).

## [1.8.16] - 2026-09-11

### Fixed
- Pin Vue SPA download in docker-publish to **inverter-dashboard-vue v2.1.5** (fixes live `ReferenceError: prodY is not defined` from undeclared DailyStats computed in v2.1.4).
- Ship embedded static SPA assets from Vue **v2.1.5** (`index-Bjn2Wdgo.js`).

## [Unreleased]

### Fixed
- **Cerbo MQTT abandoned when IGW configured**: data-source selection now
  mirrors inverter-desktop — MQTT-first when `MQTT_HOST` is set and the broker
  accepts TCP; IGW when MQTT is missing/unreachable; dual-path failover and
  recovery. mp ConfigMap sets `MQTT_HOST=""` so production stays IGW-only.
  Both may be configured together for local/dev.

### Added
- **Alert banners (IGW + MQTT)**: Venus-platform GUIv2 notifications
  (`platform/.../Notifications/<slot>/*`) drive the same `{id,level,title,body,source,ts}`
  banners as inverter-desktop. IGW snapshot `platform` leaves work without Cerbo MQTT;
  LAN mode also subscribes to platform topics (Alarms fallback when platform unseen).
  Banner **X** dismisses locally and acknowledges on Cerbo via IGW
  `acknowledge_all_notifications` or MQTT `AcknowledgeAll`.

- **Inverter-gateway (IGW) transport**: optional remote live telemetry via
  `GET /v1/snapshot` (Cloudflare Access + bearer), same pattern as
  inverter-desktop. Env: `GATEWAY_ENABLED`, `GATEWAY_URL`,
  `GATEWAY_ACCESS_CLIENT_ID` / `GATEWAY_ACCESS_CLIENT_SECRET`,
  `GATEWAY_API_TOKEN`, `GATEWAY_POLL_INTERVAL`. Coexists with Cerbo MQTT when
  both are configured (MQTT-first if reachable).
- k3s ConfigMap supports a deployment-specific gateway endpoint with
  `MQTT_HOST=""` (IGW-only); gateway credentials come from Secret
  `inverter-dashboard-gateway`.

### Fixed
- **PV inverters not filling solar total**: discover `N/.../pvinverter/...` like
  desktop (CustomName/Serial/L1|L2), sum into `pv_inverter_total` /
  `solar_total`, and publish Cerbo keepalive immediately on connect.
- **Version shows "Web Dev"**: read `VERSION` from package-adjacent /
  `/app/VERSION` paths used by non-editable Docker installs (was falling back
  to `dev`).
- **Header control flags missing / wrong source**: toggle and state use Cerbo
  MQTT `inverter/state.booleans` bare keys (desktop parity), never HA
  `input_boolean` / `binary_sensor` mirrors.
- **Top daily status row**: refreshed Vue SPA includes DailyStats; row stays
  visible when yesterday or forecast has data even if today is still 0.
- **Console panel removed** from the bottom of the dashboard.

### Fixed
- **Live telemetry zeros after MQTT_SLIM_STATE**: dashboard now reads Cerbo Venus
  MQTT (`system` / `battery` / `solarcharger` / `vebus` / `acload` / `pvinverter`)
  for grid, consumption, bank SoC, solar, setpoint/mode, and active loads — the
  same durable sources as inverter-desktop — instead of relying on slim
  `inverter/state` for those tiles. Daily solar / battery-out from the daemon
  still work.
- **Active Loads blink-and-clear**: slim `inverter/state` no longer replaces
  `current_state` wholesale; Cerbo acload maps are re-applied after each daemon
  tick, and load power updates always refresh the UI.
- MQTT message loop never connected: passing `tls_insecure` without an SSL
  context made paho raise `ValueError`, killing the loop task silently at
  startup. TLS params are now only passed when `MQTT_TLS` is enabled.

### Changed
- k3s ConfigMap supports a locally configured Cerbo broker and portal identifier
  for portal keepalive and water/EV mapping.
- Subscribes to `inverter/portal` and publishes `R/<portal>/keepalive`.

### Added
- **EV system via Cerbo MQTT** — `dbus-ev` / `dbus-evcharger` integration:
  - New `CERBO_PORTAL_ID` config subscribes to EV topics:
    - `N/<portal>/ev/<instance>/Soc` → `car_soc` (%)
    - `N/<portal>/ev/<instance>/Ac/Power` → `ev_power` (W)
    - `N/<portal>/evcharger/<instance>/Ac/Power` → `ev_charging_kw` (kW)
  - Config keys: `EV_INSTANCE` (default 22) and `EVCHARGER_INSTANCE` (default 40)
  - `MqttState.handle_ev()` decodes topics into the state payload;
    subscriptions added in `_subscribe_topics()`
  - WebSocket model `InverterState` already exposes `ev_charging_kw`,
    `ev_power`, `car_soc` fields — no UI schema change needed
  - New test file `tests/test_server_ev.py` covers all decode paths and
    instance/portal gating

### Changed
- **Water system migrated from Home Assistant to dbus-pump via Cerbo MQTT** (no HA):
  - New `CERBO_PORTAL_ID` config (+ `WATER_TANK_INSTANCE` / `WATER_PUMP_INSTANCE` /
    `WATER_VALVE_INSTANCE`, defaults 21/1/2) subscribes to
  - `N/<portal>/tank/<N>/Level` and `N/<portal>/pump/<N>/State`
  - `HA_WATER_VALVE_ENTITY` / `HA_PUMP_SWITCH_ENTITY` removed from site/local config;
    water keys are no longer overlaid from HA or zeroed when HA is down
- **EV terminology clarified**: `ev_charging_kw` (kW, from evcharger) vs
  `ev_power` (W, from vehicle); neither comes from HA sensor entities anymore.
  Update `local_config.example.py` comments accordingly — removed the stale
  "no D-Bus standard" note since `dbus-ev`/`dbus-evcharger` now publish these.
