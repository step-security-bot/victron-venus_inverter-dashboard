# Inverter Dashboard

[![Docker Hub](https://img.shields.io/docker/v/alvit/inverter-dashboard?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/alvit/inverter-dashboard)
[![Docker Pulls](https://img.shields.io/docker/pulls/alvit/inverter-dashboard?label=Docker%20Pulls&logo=docker)](https://hub.docker.com/r/alvit/inverter-dashboard)
[![CI](https://github.com/victron-venus/inverter-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/victron-venus/inverter-dashboard/actions/workflows/ci.yml)
[![CodeQL](https://github.com/victron-venus/inverter-dashboard/actions/workflows/codeql.yml/badge.svg)](https://github.com/victron-venus/inverter-dashboard/actions/workflows/codeql.yml)
[![Trivy](https://github.com/victron-venus/inverter-dashboard/actions/workflows/trivy-fs.yml/badge.svg)](https://github.com/victron-venus/inverter-dashboard/actions/workflows/trivy-fs.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub last commit](https://img.shields.io/github/last-commit/victron-venus/inverter-dashboard)](https://github.com/victron-venus/inverter-dashboard/commits/main)
[![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)](https://github.com/victron-venus/inverter-dashboard/graphs/commit-activity)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Real-time web dashboard for monitoring Victron inverter systems directly through Cerbo GX MQTT or inverter-gateway. [inverter-control](https://github.com/victron-venus/inverter-control) supplies control policy, history and forecasts when available; it is not required to relay live telemetry.

> **Dashboard options:** For Cerbo GX deployments, [**inverter-dashboard-go**](https://github.com/victron-venus/inverter-dashboard-go) is the recommended primary (single binary, low footprint). **This repo** targets Docker/NAS installs (`alvit/inverter-dashboard`). For a native app, see [**inverter-desktop**](https://github.com/victron-venus/inverter-desktop).

---

## Project Role

**This dashboard targets Docker, NAS and general-purpose servers.** The projects share the Vue interface and direct Cerbo telemetry contract:

| Use Case | Recommended |
|----------|-------------|
| Cerbo GX / embedded | [inverter-dashboard-go](https://github.com/victron-venus/inverter-dashboard-go) — single binary, minimal footprint |
| Docker / NAS | [inverter-dashboard](https://github.com/victron-venus/inverter-dashboard) — this repo (`alvit/inverter-dashboard` on Docker Hub) |
| Native desktop/mobile | [inverter-desktop](https://github.com/victron-venus/inverter-desktop) — Rust/Tauri app with offline support |
| Building custom dashboards | [inverter-dashboard-vue](https://github.com/victron-venus/inverter-dashboard-vue) — shared Vue 3 component library |

![Inverter Dashboard](images/Screenshot.png)

## Architecture

```mermaid
flowchart TD
    subgraph Venus["Venus OS (Cerbo GX)"]
        INV["inverter-control"]
        DP["dbus-pump (water)"]
        MQTT["MQTT Broker"]
    end

    subgraph Dashboard["Web Dashboard"]
        WS["WebSocket Server<br/>:8080"]
        UI["Web UI<br/>HTML/CSS/JS"]
    end

    subgraph Clients["Clients"]
        BROWSER["Browser"]
        MOBILE["Mobile"]
    end

    INV -->|"inverter/state (slim: daily_stats, booleans, …)"| MQTT
    CERBO["Cerbo Venus MQTT<br/>system/battery/MPPT/vebus/acload"] -->|N/… live tiles| MQTT
    DP -.->|"N/&lt;portal&gt;/tank/… /pump/…"| MQTT
    MQTT -.->|subscribe| WS
    WS -->|push state| UI
    UI --> BROWSER & MOBILE
```

Live grid / consumption / battery / solar / loads / setpoint come from **Cerbo
Venus MQTT** (same path as [inverter-desktop](https://github.com/victron-venus/inverter-desktop))
**and/or** remote **[inverter-gateway](https://github.com/victron-venus/inverter-gateway)**
(`GATEWAY_ENABLED` + `GATEWAY_URL` → poll `/v1/snapshot`).

IGW connections require a verified HTTPS origin, for example
`https://gateway.example.com:9151` for native HTTPS. Snapshot and command requests
reject redirects, so configure the final URL directly. Native HTTPS accepts the
gateway bearer token alone; Cloudflare Access credentials are optional and must
be supplied as a complete pair when used. Private CA bundles can be supplied via
`SSL_CERT_FILE` or `SSL_CERT_DIR` while retaining certificate and hostname checks.

**Data-source precedence** (exclusive live path, desktop-aligned):

1. Only `MQTT_HOST` → Cerbo MQTT client.
2. Only IGW (`GATEWAY_ENABLED` + `GATEWAY_URL`) → snapshot poller.
3. Both configured → TCP-probe MQTT; if the broker responds, use MQTT;
   otherwise use IGW. Dual-path may fail over MQTT→IGW and later recover
   when MQTT is reachable again.
4. Neither → no live telemetry source.

Production on `worker-1` clears `MQTT_HOST` so IGW stays primary (one Cerbo MQTT
client on Synology). Local/dev and optional ConfigMaps may set both.
`inverter/state` still carries daemon-only extras (`daily_stats`,
`solar_forecast`, flags, …) when MQTT is used; IGW mode maps Cerbo live tiles
only (daemon extras need HA overlay or a separate source).

### Direct Cerbo telemetry

Point `MQTT_HOST` at the Cerbo broker and set `CERBO_PORTAL_ID` to the GX's VRM
portal ID (shown in its VRM settings). Local MQTT access must be enabled on the GX.
The dashboard subscribes to that portal's native topics before publishing an empty
`R/<portal>/keepalive` to request a full refresh. Further keepalives run every
45 seconds with `suppress-republish`, so an unchanged system does not repeatedly
send its entire tree. Reconnects invalidate old live observations and request a
fresh tree.

When the portal is omitted, the dashboard can learn it from native `system/.../Serial`,
heartbeat or keepalive notifications, or legacy `inverter/portal`. Modern Venus
MQTT notifications are not retained, so a silent broker cannot be discovered
reliably until another client starts notifications. **Configure `CERBO_PORTAL_ID`
for unattended startup**, and whenever the broker's ACL requires portal-scoped
subscriptions. A selected portal stays fixed; another portal's notifications
cannot mix into this dashboard.

The same reducer handles MQTT notifications and complete IGW snapshots:

- Grid and consumption support L1, L2 and L3; systemcalc is preferred, with grid
  meter and connected VE.Bus input fallbacks when native source identity is mains
  or shore power. Generator input is not grid. VE.Bus output alone is not total
  consumption; systemcalc consumption is required to account for the site topology.
- Main Battery SoC follows inverter-desktop: `round(clamp((V - 40) / (54.4 - 40) * 100, 0, 100))`,
  with half-up rounding. Valid SmartShunt voltage takes priority over
  `system/0/Dc/Battery/Voltage`; without either, the main percentage is unknown.
  Reported BMS/shunt SoC counters remain on their actual device tiles and never
  replace the main percentage. Device tiles include identity, temperature, time
  remaining and cell-voltage diagnostics; no synthetic Bank device is added.
- Solar combines DC MPPT and AC PV, without counting device and system totals twice.
  Published totals, including zero, take priority over summed phase powers.
- AC loads are native `acload` readings keyed by instance in `loads`, with
  independent display names in `load_names`. Renames and duplicate names cannot
  change device identity or combine unrelated loads. Zero and signed watts are preserved.
- Null values, service removal and snapshot omissions invalidate affected readings.
  Missing `Connected` is accepted for older producers; only numeric `1` is
  connected when the leaf exists. A null, invalid or other numeric value makes
  the device unavailable, including its controls.
  Invalid JSON and nonnumeric measurements are ignored. `telemetry_available`
  distinguishes missing readings from measured zero; stale controller mirrors
  cannot restore a native reading that became unavailable.

### Water and EV

Water data comes from [dbus-pump](https://github.com/victron-venus/dbus-pump) through
native `tank` and `pump` topics. `Level` is a percentage, including values below
1%; pump `State`/`Status` and `Mode` are read directly. Configure
`WATER_TANK_INSTANCE`, `WATER_PUMP_INSTANCE` and `WATER_VALVE_INSTANCE` to match
that bridge (defaults 21/1/2).

With direct Cerbo MQTT connected, the dashboard can set the configured pump or
valve to Auto (0), forced on (1), or forced off (2) through its native writable
`W/<portal>/pump/<instance>/Mode` path. Controls are enabled only after that
device's Mode is available; the displayed state changes when Cerbo confirms it.
Writes use QoS 0 and are nonretained, avoiding broker redelivery of overrides.
IGW supports the same controls when its snapshot advertises
`capabilities.water_mode: true`: the dashboard posts
`/v1/commands/water_mode` with `{instance, mode}` using the configured write token.
The gateway requires a connected broker and an observed valid Mode for the exact
native pump; inverter-control state is not required. Older gateways keep Water
read-only. `water_pump_controls_available` and `water_valve_controls_available`
report each target separately. Explicit instance `0` is valid. Commands are not
retried, and failures never optimistically change the displayed state.
Home Assistant switches and controller commands are not used for these overrides;
automation continues to run in dbus-pump. Public views remain read-only.

Vehicle SoC and power come directly from native `ev` services, and wallbox power
from `evcharger`. Each defaults to automatic discovery: the lowest connected
numeric instance with usable telemetry wins, independently of message order.
Set `EV_INSTANCE` or `EVCHARGER_INSTANCE` to pin a specific instance, including 0;
an unavailable pinned device never falls back to another instance. Leave these
variables unset or set them to `auto` to resume discovery. Legacy vehicle SoC
published under `evcharger` is supported as a fallback. Vehicle power and
`ev_charging_power` are watts; `ev_charging_kw` is kilowatts. Zero is a valid
reading. Null leaves, removed services and disconnected devices invalidate old
values. `discovered_water_ev` includes device identities and available SoC/power.
These fields work identically through native MQTT and IGW snapshots, without HA.

ESS status comes from `inverter/state.ess_mode` until native settings are observed.
Native `Settings/CGwacs/Hub4Mode` and `Settings/CGwacs/BatteryLife/State` then take
precedence. Optimized mode remains unknown until both settings are available.

## Home Assistant integration

`inverter/state` remains the source of controller-specific fields such as
`daily_stats`, `solar_forecast`, `ess_mode`, `booleans`, `dry_run`, limits and uptime.
The slim controller no longer forwards every appliance or HA entity. Configure
`HA_APPLIANCE_ENTITIES` in `local_config.py` to read the desired appliances directly
from HA. `HA_DIRECT_CONTROLS=True` additionally enables explicitly configured HA
switches and buttons. Use only entities owned by HA; controller policy flags and
native Cerbo power, battery, EV and water readings keep their own sources.

HA is optional for native monitoring and the seven inverter-control flags:
`only_charging`, `no_feed`, `house_support`, `charge_battery`,
`do_not_supply_charger`, `set_limit_to_ev_charger`, and `minimize_charging`.
Their values and header metadata come from the controller, not HA mirrors.
Unknown flags are unavailable rather than false. Direct MQTT accepts slim
controller updates; new IGW snapshots carry the complete retained object as
`inverter`, including controller history, forecasts and UI metadata. An omitted
`inverter` field from an older gateway preserves prior observations; explicit
null clears them. Controller observations expire after 120 seconds without a
controller update on direct MQTT; IGW enforces the same expiry at its broker.

IGW forwards only whitelisted controller actions: a known flag with an explicit
on/off state, `dry_run` with an explicit boolean value, and `ess_mode` with an
empty object (the controller's existing ESS toggle). Failed commands are reported
without retries. Other controller actions require direct MQTT; native Water uses
its separate capability-gated gateway endpoint described above. Production IGW-only deployments
must keep `MQTT_HOST=""`; discovery does not enable another transport.

The HTTP and WebSocket payloads expose the actual `data_source` (`mqtt` or `igw`),
`mqtt_connected`, `gateway_connected`, and selected-source `native_connected`.
IGW success never marks the dashboard's direct MQTT connection as connected.
IGW `/v1/snapshot` returns 503 when its broker is disconnected or not ready, so a
successful snapshot establishes broker readiness at receipt time.

`telemetry` reports `source`, `observed_at` (epoch milliseconds),
`timestamp_source: "local_receipt"`, and `quality` (`unknown`, `live`, or `stale`).
A valid native MQTT notification or successful complete IGW snapshot advances
this receipt timestamp; controller/HA updates do not. Quality is unknown before
an observation and stale after 120 seconds or a transport disconnect. For IGW this
is snapshot receipt freshness, not proof that every sensor was sampled recently;
upstream currently supplies no measurement timestamp. Readings keep their separate
`telemetry_available` flags, and sensor values are not erased merely for being unchanged.

## Deploy to k3s (node `worker-1`)

Python / multi-arch image for NAS and k3s (prefer this over `inverter-dashboard-go` for cluster workers).

Portable examples and the local configuration workflow are in
[`deploy/k3s/`](deploy/k3s/). Copy the manifests into `.local-private/` and set your
worker, gateway URL, portal identifier and ingress host there before deployment.
The example worker is `worker-1`; the namespace is `inverter-dashboard`.

Create real Secrets out-of-band and apply your configured local copy as described
in the runbook. The placeholder Secret is excluded from Kustomize resources.

- Image: `alvit/inverter-dashboard` on Docker Hub. Registry publication promotes approved stable OCI assets; see the [operator runbook](docs/release-workflow.md).
- For IGW-only deployments, use your gateway endpoint and `MQTT_HOST=""`.
  Local/dev can use `MQTT_HOST`, or configure both paths for MQTT-first fallback.
  Set `CERBO_PORTAL_ID` locally for native MQTT bootstrap and configure water/EV instances separately.
- The public Ingress host is a documentation placeholder; configure your own DNS and TLS locally.

## Configuration Reference

In `local_config.py` (created via [`scripts/init-config.sh`](scripts/init-config.sh)):

```python
# Default: MQTT-only mode (recommended)
HA_DIRECT_CONTROLS = False

# Direct polling mode (diagnostic only — not recommended)
HA_DIRECT_CONTROLS = True
HA_URL = "https://homeassistant.local:8123"
HA_TOKEN = "REPLACE_WITH_LONG_LIVED_ACCESS_TOKEN"

# HA entity mappings (used by both modes)
HA_BOOLEAN_ENTITIES = {
    "only_charging": "input_boolean.only_charging",
}
HA_SWITCH_ENTITIES = {
    "home_no_feed": "input_boolean.no_feed",
    "home_house_support": "input_boolean.house_support",
}
```

---

<!-- ci-release-process:start -->
## Release process

See the [release strategy](RELEASING.md) for validation, nightly, beta, RC and stable promotion rules, and the [operator runbook](docs/release-workflow.md) for local commands.
<!-- ci-release-process:end -->

### Run a standalone dashboard

Download a ZIP and its matching `.sha256` file from the
[latest stable release](https://github.com/victron-venus/inverter-dashboard/releases/latest):

- Linux x86_64: `inverter-dashboard-linux-x86_64.zip` (built on Ubuntu 24.04).
- macOS Intel: `inverter-dashboard-macos-x86_64.zip`.
- macOS Apple Silicon: `inverter-dashboard-macos-arm64.zip`.
- Windows x86_64: `inverter-dashboard-windows-x86_64.zip`.

The executable includes Python, its runtime dependencies, and the Vue web
interface. You do not need a source checkout, Python installation, or frontend
build. Extract the archive into a directory where you keep dashboard settings.

On Linux or macOS, make the extracted file executable and start it with your
Cerbo's reachable address in place of `CERBO_IP`:

```sh
chmod +x inverter-dashboard
./inverter-dashboard --mqtt-host CERBO_IP --port 8080
```

On Windows, open PowerShell in the extracted directory:

```powershell
.\inverter-dashboard.exe --mqtt-host CERBO_IP --port 8080
```

Open `http://127.0.0.1:8080` in your browser. The web server binds to loopback by
default. Existing MQTT, gateway, and authentication settings also apply to the
standalone executable; set `DASHBOARD_SECRET` before enabling remote access with
`HOST`. Use `--help` to see the available command-line options.

To verify a download, keep the ZIP beside its checksum file and use
`sha256sum --check <archive>.sha256` on Linux or
`shasum -a 256 --check <archive>.sha256` on macOS. On Windows, compare
`Get-FileHash <archive>.zip -Algorithm SHA256` with the matching checksum file.

---

## Completed Features

- ✅ **Release packaging**: Candidate artifacts and checksums; see the [release strategy](RELEASING.md).
- ✅ **Async MQTT Migration**: Refactored `mqtt_handler.py` to use `aiomqtt` (asyncio wrapper for paho-mqtt) for non-blocking I/O in the FastAPI event loop
- ✅ **Ultra-Slim Multi-Arch Docker Image**: Refactored `Dockerfile` using `uv` (fast Python package installer) and multi-stage builds to reduce image size to ~40MB (achieved 84MB from 149MB - further reduction needs distroless/scratch base)
- ✅ **Static Vue Asset Mounting**: Added FastAPI StaticFiles mounting route for `inverter-dashboard-vue` compiled dist assets

---

## Features

- Real-time power monitoring (Grid, Solar, Battery, Consumption)
- Interactive controls via WebSocket
- Live power charts with ECharts
- EV charging status
- Water system monitoring (dbus-pump via Cerbo MQTT)
- Home automation controls
- Mobile-friendly responsive UI

## Quick Start

### Docker (Recommended)

```bash
docker run -d \
  --name inverter-dashboard \
  -p 8080:8080 \
  -e MQTT_HOST=192.0.2.10 \
  alvit/inverter-dashboard:latest
```

### Docker Compose

```yaml
version: '3.8'
services:
  dashboard:
    image: alvit/inverter-dashboard:latest
    ports:
      - "8080:8080"
    environment:
      - MQTT_HOST=192.0.2.10
      - MQTT_PORT=1883
    restart: unless-stopped
```

### Portainer Stack

See [portainer-stack.yml](portainer-stack.yml) for Portainer deployment. Set
`MQTT_HOST` to your broker through a local environment file or the deployment UI.
The checked-in `mqtt.example.com` default and documentation addresses are
placeholders; keep real broker addresses out of commits.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `MQTT_HOST` | `Cerbo` | Cerbo/LAN MQTT broker. Empty/`""` disables MQTT (IGW-only). May coexist with IGW. |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `GATEWAY_ENABLED` | `false` | Enable remote inverter-gateway snapshot polling (coexists with MQTT; see precedence above) |
| `GATEWAY_URL` | _(empty)_ | HTTPS IGW origin, e.g. `https://gateway.example.com:9151`; HTTP, URL credentials, path prefixes, queries and fragments are rejected |
| `GATEWAY_ACCESS_CLIENT_ID` | _(empty)_ | Optional Cloudflare Access service-token client id; set both Access fields or neither |
| `GATEWAY_ACCESS_CLIENT_SECRET` | _(empty)_ | Optional Cloudflare Access service-token client secret; leave both empty for native HTTPS |
| `GATEWAY_API_TOKEN` | _(empty)_ | Bearer token (`Authorization: Bearer …`) matching gateway `GATEWAY_API_TOKEN` |
| `GATEWAY_POLL_INTERVAL` | `2` | Seconds between `/v1/snapshot` polls |
| `WEB_PORT` | `8080` | Web server port (inside the container) |
| `INVERTER_DASHBOARD_CONFIG` | `/app/config` | Host folder mounted read-only: `local_config.py` and optional TLS files |
| `CERBO_PORTAL_ID` | *(empty)* | VRM portal ID for scoped native subscriptions and immediate bootstrap. Required on a silent broker; otherwise passive discovery is available. |
| `WATER_TANK_INSTANCE` / `WATER_PUMP_INSTANCE` / `WATER_VALVE_INSTANCE` | `21` / `1` / `2` | D-Bus device instances on the GX (must match dbus-pump) |

### Secrets (`local_config.py`) + optional HTTPS

Committed template only: [`local_config.example.py`](local_config.example.py). Your real file is **`local_config.py`** in the **repository root** (next to `server.py`) — **gitignored** (never push). There is no separate `config/` folder in the repo.

If Cerbo **inverter-control** uses **`MQTT_SLIM_STATE`** (slim `inverter/state`), dishwasher/washer/dryer fields are omitted from MQTT — add **`HA_APPLIANCE_ENTITIES`** in **`local_config.py`** so the dashboard polls those sensors from Home Assistant (same keys as full MQTT state).

**Synology NAS (deploy path used in this repo):**

| Location | Purpose |
|---------|---------|
| `/volume1/docker/inverter-dashboard/config` | **On the NAS host only:** a folder that is **bind-mounted** read-only into the container as **`/app/config`**. Put **`local_config.py`** here together with optional **`dashboard.crt`** / **`dashboard.key`**. The folder name on disk is convention (matches [`docker-compose.yml`](docker-compose.yml) / [`portainer-stack.yml`](portainer-stack.yml)); it is **not** a `config/` directory inside the Git clone. |

- **After clone (any machine):** `./scripts/init-config.sh` creates **`./local_config.py`** from the example; fill in **`HA_TOKEN`** / **`HA_URL`** (typically the same long-lived token as inverter-control **`secrets.py`**).
- **`postinstall.sh`** (in repo root): run **on your Mac/PC** (not on the NAS). Put **`Host synology`** (user, hostname, keys) in **`~/.ssh/config`**, then simply **`./postinstall.sh`** — it runs **`ssh synology`** by default (override with **`SYNOLOGY_SSH`** only if you use another alias).

  Expects **passwordless `sudo`** on the NAS for **`docker` / `docker compose`** and for writing under **`/volume1/docker/...`**. Files are pushed with **`ssh` + stdin** (not `scp`), so it still works if Synology has disabled the SFTP/SCP subsystem. Then **`sudo install`** from a temp dir. Env: **`SYNOLOGY_SSH`**, **`REMOTE_BASE`**, **`SOURCE_CONFIG`** (defaults to **repo root** next to **`postinstall.sh`**), **`STACK_FILE`**, **`DOCKER`** (default **`sudo /usr/local/bin/docker`** — under **`sudo`** DSM often has no **`docker`** in **`PATH`**). On **macOS**, if **`dashboard.crt`** exists next to **`postinstall.sh`** or under **`.certs/`**, the script imports it as trusted when missing: tries **System** keychain (`System.keychain-db` / `System.keychain`), then **login** keychain if needed (**`SKIP_MAC_TRUST=1`** to skip).

If **both** cert files exist in that folder, the **entrypoint enables HTTPS on the same port** as HTTP would use; otherwise HTTP only.

### Command Line Arguments

```bash
python server.py --mqtt-host 192.0.2.10 --mqtt-port 1883 --port 8080
```

### HTTPS (why you still see `http://`)

By default the app and the published Docker image listen on **plain HTTP** (port `8080`). Nothing is wrong with your deploy — TLS is not enabled unless you add it.

**Choose one approach:**

1. **Reverse proxy (recommended for production / LAN DNS)**
   Put **Caddy**, **Traefik**, or **nginx** in front of the container on port **443**, terminate Let’s Encrypt (or your certs) there, and proxy to `http://inverter-dashboard:8080`. You open `https://dashboard.example.com` in the browser; the container keeps HTTP internally.

2. **TLS inside the Python app** (good for quick tests / single host)

   **Docker (recommended layout):** mount your host config folder to **`/app/config`**, put **`dashboard.crt`** and **`dashboard.key`** next to **`local_config.py`**. The entrypoint detects both files and passes **`--ssl-cert`** / **`--ssl-key`** automatically on the **same** port as without TLS (default 8080). Map ports e.g. `"8443:8080"` if you want HTTPS on 8443 externally.

   Generate certs (repo includes a helper):

   ```bash
   ./scripts/ssl-local-deploy.sh
   # Optional: TLS_CN=myhost.local ./scripts/ssl-local-deploy.sh
   ```

   The helper includes **Subject Alternative Name (SAN)** entries (`TLS_CN`, `localhost`, `127.0.0.1`). Browsers require SAN for HTTPS hostname checks; an old cert with **CN-only** can still show “not private” even after trusting — regenerate, copy the new **`dashboard.crt`** / **`dashboard.key`** to the NAS folder that is mounted at **`/app/config`**, redeploy, then trust again (remove the previous cert from Keychain Access if needed).

   Trust the cert on your Mac (`postinstall.sh` does this automatically; the helper also prints `security add-trusted-cert`).

   **Local run (paths arbitrary):**

   ```bash
   python server.py --mqtt-host … --port 8443 \
     --ssl-cert .certs/dashboard.crt --ssl-key .certs/dashboard.key
   ```

   **Docker Compose / Portainer:** host config path (Synology):

   ```yaml
   volumes:
     - /volume1/docker/inverter-dashboard/config:/app/config:ro
   ```

   On a dev PC without `/volume1`, comment out this volume or bind a local folder (e.g. repo root or any directory that contains **`local_config.py`** and optional TLS files) to **`/app/config`** instead.

   If you omit **`dashboard.crt`** / **`dashboard.key`** on the host, the app stays on HTTP.

   The image **`HEALTHCHECK`** uses **`scripts/docker_healthcheck.py`**, which calls **`/api/state`** over HTTP or HTTPS depending on whether `dashboard.crt` + `dashboard.key` exist in the config directory.

3. **`mkcert`** — alternative to OpenSSL for local dev trust; still point the app at the generated `.pem` paths with `--ssl-cert` / `--ssl-key`.

## MQTT Topics

### Subscribed (incoming data)

- `N/<portal>/{system,grid,battery,solarcharger,pvinverter,vebus,acload}/+/#` — native energy measurements and device identity
- `N/<portal>/{tank,pump,ev,evcharger}/+/#` — water and EV measurements
- `N/<portal>/platform/+/Notifications/#` and native `Alarms/#` — Victron notifications
- `inverter/state` — optional controller policy, history, forecasts and legacy compatibility
- `inverter/portal` — optional legacy portal discovery
- `inverter/notifications` — controller notification events
- `CAMERA_TOPIC` — optional configured camera events

The dashboard sends read-only `R/<portal>/keepalive` requests to maintain native
notifications. Control actions remain separate from telemetry subscription.

### Published (commands)
- `W/<portal>/pump/<configured-instance>/Mode` — explicit water Auto/on/off overrides (`{"value":0|1|2}`), direct MQTT or the IGW native water endpoint
- `inverter/cmd/toggle` - Toggle boolean entities
- `inverter/cmd/press` - Press button entities
- `inverter/cmd/setpoint` - Set power setpoint
- `inverter/cmd/dry_run` - Toggle dry run mode
- `inverter/cmd/limits` - Set power limits
- `inverter/cmd/ess_mode` - Toggle ESS mode
- `inverter/cmd/loop_interval` - Set control loop interval

## Expected State Format

```json
{
  "gt": 150,
  "g1": 100,
  "g2": 50,
  "tt": 2500,
  "t1": 1500,
  "t2": 1000,
  "solar_total": 3500,
  "battery_soc": 85,
  "battery_power": -500,
  "battery_voltage": 52.4,
  "setpoint": 0,
  "inverter_state": "Inverting",
  "dry_run": false,
  "ess_mode": {
    "mode_name": "Optimized (with BatteryLife)",
    "is_external": false
  },
  "booleans": {
    "auto_mode": true,
    "ev_boost": false
  },
  "daily_stats": {
    "produced_today": 25.5,
    "produced_dollars": 7.65,
    "grid_kwh": 2.3
  }
}
```


## Development

### Local Setup

```bash
# Clone repository
git clone https://github.com/victron-venus/inverter-dashboard.git
cd inverter-dashboard

# Install the exact committed runtime and test dependencies (requires uv)
uv sync --locked --extra test

# Optional: ./local_config.py for Home Assistant direct mode (gitignored)
./scripts/init-config.sh

# Run
uv run --locked inverter-dashboard --mqtt-host your-mqtt-broker
```

### Build a Native Binary

Run `./build_binaries.sh --local` (or `python3 scripts/release.py package`) to
build and smoke-test a binary for the current OS and architecture. The same
committed `uv.lock` and packaging dependency group are used by local and hosted
builds. Archives and checksums are written to `release-output/`. Multi-platform
release binaries are built on the corresponding native GitHub Actions runners;
PyInstaller does not cross-compile them from a single host.

### Build Docker Image Locally

```bash
docker build -t inverter-dashboard .
docker run -p 8080:8080 -e MQTT_HOST=192.0.2.10 inverter-dashboard
```

## Multi-Architecture Support

Docker images are built for:
- `linux/amd64` (x86_64)
- `linux/arm64` (Raspberry Pi 4, Apple Silicon, etc.)

## Documentation

- [System Architecture](./.github/docs/system-architecture.md) - Data flow diagrams, runbook

## Related Projects

- [inverter-dashboard-vue](https://github.com/victron-venus/inverter-dashboard-vue) — shared frontend consumed by this backend.
- [inverter-dashboard-go](https://github.com/victron-venus/inverter-dashboard-go) — Go backend packaged as a single binary.
- [inverter-desktop](https://github.com/victron-venus/inverter-desktop) — native Tauri client for the same Cerbo telemetry.
- [inverter-control](https://github.com/victron-venus/inverter-control) — ESS controller and correlated command protocol.
- [dbus-ev](https://github.com/victron-venus/dbus-ev) — maintained vehicle and optional Mercedes charger telemetry.
- [dbus-pump](https://github.com/victron-venus/dbus-pump) — water tank, pump and valve services consumed through Cerbo MQTT.
- [dbus-emporia-vue](https://github.com/victron-venus/dbus-emporia-vue) — AC-load telemetry and tariff data for the configured Emporia channels.
- [inverter-web-vitrine](https://github.com/victron-venus/inverter-web-vitrine) — separate read-only public status page backed by an authenticated gateway.

Browse the [public project catalog](https://victron-venus.github.io/.github/projects.html)
for other Venus OS packages and companion tools. Each project documents its own
installation, compatibility and release requirements.


## Author

Created by [@4alvit](https://github.com/4alvit)

## License

MIT License - see [LICENSE](LICENSE).

---

**Note:** This is a community project and is not affiliated with Victron Energy.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature-name`)
3. Commit your changes
4. Push to the branch (`git push origin feature-name`)
5. Create a Pull Request

## Support

For issues specific to:
- **MQTT connectivity**: Check broker reachability and topic subscriptions
- **WebSocket errors**: Verify port accessibility and firewall settings
- **Home Assistant integration**: Validate token and entity availability
- **Docker deployment**: Review container logs and volume mounts
- **This project**: Open an issue in this repository


## Opt-in system notifications

The dashboard can deliver encrypted Web Push notifications without an open tab.
Enable `WEB_PUSH_ENABLED=true` and set `WEB_PUSH_DATA_DIR` to a dedicated persistent
private directory. `WEB_PUSH_SUBJECT` defaults to this project's HTTPS repository
URL; a contact `mailto:` URI is also accepted. Missing directory configuration is
an explicit configuration error. An inaccessible, locked or corrupt store instead
reports Web Push unavailable while normal telemetry continues. It never resets a
store or silently generates a replacement VAPID key for an existing store.

Use a single application process/replica with a persistent local filesystem and
Recreate rollout strategy. The directory is mode 0700 and store/lock files mode 0600;
an exclusive process lock prevents duplicate senders. Preserve this directory
across upgrades, including the VAPID identity and browser subscriptions. Do not
share it over a network filesystem or expose it through static file serving.
The server bounds subscribers to 64, dedupe entries to 4096 and pending deliveries
to 1024. Deleting a subscription or updating preferences discards its pending work.

In notification settings, **Enable** requires a direct browser permission gesture
and confirmation that the server registered the subscription. The explicit test
button reports **queued**, not delivered. Notifications require a secure context
and a browser/platform that supports Web Push; iPhone support requires using an
installed Home Screen web app. Browser permission and OS delivery remain under
the user's control. Existing in-app warning banners remain independent.

Native warnings retain the original Victron/control occurrence time. Current
alerts are silently primed on first connection and reconnect; unknown, old or
future timestamps do not create new system notifications. Fresh selected charger
power transitions (>10W), pump/valve states (0 or 1) and actual native battery SoC
crossing 20% can also notify after a baseline in the same connection. Prior samples
expire after 30 seconds. MQTT retained samples clear these baselines and an initial
10 second hydration period is silent. Voltage-derived SoC and near-zero grid watts
never create notification events. Closed connection epochs cancel their queued
and in-flight work; provider delivery already accepted cannot be recalled.

All notification APIs use the existing dashboard secret policy. Mutations require
JSON and a same-origin HTTPS Origin matching the ingress-preserved Host; forwarded
host/proto values cannot grant access. The browser sends its dashboard token only
in the Authorization header and subscription capability only in the JSON body.
No endpoint, encryption key or private VAPID material is returned in diagnostics.
Outbound delivery uses [pywebpush](https://pypi.org/project/pywebpush/2.5.0/) for
standard encryption and VAPID, a public-IP-pinned HTTPS transport with verified
TLS, no proxy/redirects, and a10 second timeout. Supported provider authorities are
`fcm.googleapis.com`, `updates.push.services.mozilla.com`, named hosts under
`.push.apple.com`, and named hosts under `.notify.windows.com`; unknown providers
are rejected. Each event expires 300 seconds after its source time, has at most
three delivery attempts, and 404/410 removes the expired subscription.

The root service worker, web manifest and notification icon are narrowly public
static resources with no-cache headers. The worker has no fetch handler, command
actions or asset cache. A notification click opens the dashboard root and does
not acknowledge an alarm or control any device.

Server-side Web Push is supported on Linux and macOS Apple Silicon hosts with a
private Unix store. On Windows and Intel macOS, its status API explicitly reports
`unsupported_platform` and the regular dashboard, telemetry and in-app banners
remain available. Intel macOS packages omit the optional sender dependencies:
current cryptography releases no longer publish Intel macOS wheels, and the
dashboard keeps its secure dependency version and `--no-build` packaging policy.
Browsers on Windows and Intel macOS can still receive Web Push from a supported
server such as the Linux deployment in k3s.

The only dependency without an official wheel is `http-ece==1.2.1`. Its reviewed
pure-Python wheel and original source archive are under `vendor/http-ece/` with
MIT license, exact source/runtime hashes and provenance. `uv.lock` selects that
wheel without weakening `--no-build`. To reproduce it offline, download the
hash-pinned build tool wheels listed in `upstream-inputs.json` into a wheelhouse,
then use the resolved Python 3.12 executable (3.12.14 was used for qualification):

```sh
python3.12 scripts/rebuild_http_ece_wheel.py --wheelhouse /path/to/wheelhouse --output-name rebuilt
```

The wheel is written beneath `build/vendor-wheels/rebuilt/`; output names cannot
contain path separators or traverse symlinks.

The build fixes `SOURCE_DATE_EPOCH`, installs only hash-checked offline tools,
and compares every packaged runtime Python file with the upstream source. The
upstream MIT license, omitted from its sdist, is included as wheel metadata.


Dashboard UI preferences can use a durable file independent of read-only
connection configuration: set `INVERTER_DASHBOARD_SETTINGS_FILE` to an absolute
path such as `/var/lib/inverter-dashboard/settings/dashboard_settings.json`.
The default location beside `local_config.py` is unchanged. New explicit parent
directories use mode `0700`; writes use a private `0600` temporary file and atomic
replacement, followed by a directory sync on POSIX systems. Masked secret values returned by the settings API preserve existing
credentials when submitted unchanged. Settings mutations require JSON and reject
cross-origin browser requests; native clients retain the existing authentication.

Controller commands require a current connection and fresh non-retained support.
Native water Mode is refreshed with bounded read requests for the configured
pump and valve every 20 seconds. Setpoint Override is a controller-owned
persistent override, including an explicit stop (`null`), and is independent of
DRY mode. The dashboard sends once, requires matching request ID and exact value
with no error within one five-second deadline, and never retries on a replacement
connection. Controller tariff editing preserves revision-based concurrency and
requires authoritative acknowledgement before the editor reports a saved plan.
