# Halloween 2025

A modular, MQTT-based Halloween automation system with web dashboard, prop workers, and ESP32 firmware support.

## 🎃 Overview

This system orchestrates multiple Halloween props (animatronics, lights, sound effects) through:

- **MQTT Broker** (Mosquitto) for message passing
- **Web Dashboard** (Plotly Dash) for monitoring and control
- **Worker Host** for prop backend logic
- **ESP32 Firmware** (MicroPython) for physical prop control

### Architecture Goals

- **Single place for orchestration** - All Docker Compose files in `infra/compose/`
- **Self-contained props** - Each prop contains plugin + backend + firmware + docs under `props/<prop>/`
- **Generic server hosts** - Dashboard and worker hosts discover and load prop bundles at runtime
- **Shared libraries** - Common code in `libs/` (Python for server/workers; MicroPython for firmware)
- **Runtime discovery** - No hardcoded prop lists; services auto-discover available props from filesystem

## 🚀 Quick Start

### Prerequisites

- Docker & Docker Compose
- (Optional) Python 3.11+ for local development

### 1. Configure Secrets

Create the secrets directory and configuration files:

```bash
# Create secrets directory
mkdir -p config/secrets

# Copy example files
cp config/dashboard.env.example config/dashboard.env

# Create MQTT user credentials
cat > config/secrets/mqtt_users.env << EOF
MQTT_ADMIN_USER=admin
MQTT_ADMIN_PW=your_secure_password
MQTT_DASHBOARD_USER=dashboard
MQTT_DASHBOARD_PW=your_dashboard_password
MQTT_WORKER_USER=worker
MQTT_WORKER_PW=your_worker_password
EOF

# (Optional) Add Tesla credentials if using tesla_hue_nest prop
cat > config/secrets/tesla.env << EOF
TESLA_AUTH_TOKEN=your_tesla_token
VEHICLE_TAG=your_vehicle_name
EOF
```

### 2. Launch the System

Run Compose commands from the repository root. On Windows PowerShell, use
backticks for line continuation.

#### Full Stack (All Services)

```powershell
docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.workers.yml `
  -f infra/compose/docker-compose.dashboard.yml `
  -f infra/compose/docker-compose.media.yml `
  up --build
```

To launch just the broker and dashboard, use:

```powershell
docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.dashboard.yml `
  up --build
```

`docker-compose.dev.yml` is not required to run the dashboard; it only adds
the `mqtt_test` integration-test service. Add it only when you intend to run
that test service.

When relaunching, it may be necessary to stop and remove all containers first, to trigger full rebuild:

```powershell
docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.workers.yml `
  -f infra/compose/docker-compose.dashboard.yml `
  -f infra/compose/docker-compose.media.yml `
  stop

docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.workers.yml `
  -f infra/compose/docker-compose.dashboard.yml `
  -f infra/compose/docker-compose.media.yml `
  rm

docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.workers.yml `
  -f infra/compose/docker-compose.dashboard.yml `
  -f infra/compose/docker-compose.media.yml `
  up --build
```

#### Individual Services

**MQTT Broker Only:**

```powershell
docker compose -f infra/compose/docker-compose.yml up --build
```

**Dashboard Only:**

```powershell
docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.dashboard.yml `
  up --build
```

**Workers Only:**

```powershell
docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.workers.yml `
  up --build
```

### 3. Access the Dashboard

Once the dashboard container is running, open **<http://localhost:8050>**.
If it does not load, check the Compose service status and dashboard logs:

```powershell
docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.dashboard.yml ps
docker compose -f infra/compose/docker-compose.yml `
  -f infra/compose/docker-compose.dashboard.yml logs dashboard
```

#### Jarvis view

A second, HUD-style view of the same plugins runs at
**<http://localhost:8050/jarvis/>**. Switch between the views with the
**Jarvis view** button in the classic navbar and **Classic view** in the Jarvis
header (or press `C`). Both views send the same MQTT commands.

The Jarvis view has three layouts, switchable live with the header buttons or
keys `1` `2` `3`; the choice is remembered per browser:

- **Grid**: all prop panes as equal tiles on one screen.
- **Focus**: one large pane, the others stacked compactly on the right
  (telemetry hidden). Click `⤢` on a pane to focus it.
- **Ops**: a command rail with systems online, per-prop status and a comms log
  of commands sent from this view, plus the panes in a two-column board.

Code: `server/dashboard/jarvis/` (app in `__init__.py`, look and layouts in
`assets/jarvis.css`). Plugins need no changes; the view restyles their markup.
Proposed behaviour changes are tracked in
[docs/dashboard-roadmap.md](docs/dashboard-roadmap.md).

## 📁 Project Structure

```text
halloween-2025/
├── config/                      # Configuration files
│   ├── dashboard.env            # Dashboard settings
│   ├── worker_host.env          # Worker settings
│   └── secrets/                 # Git-ignored credentials
│       ├── mqtt_users.env       # MQTT authentication
│       └── tesla.env            # Tesla API token (optional)
│
├── infra/compose/               # Docker Compose files
│   ├── docker-compose.yml       # Base: MQTT broker
│   ├── docker-compose.dashboard.yml  # Dashboard service
│   ├── docker-compose.workers.yml    # Worker host service
│   ├── docker-compose.media.yml      # Media server (optional)
│   ├── docker-compose.dev.yml        # Development overrides
│   └── docker-compose.prod.yml       # Production overrides
│
├── server/                      # Server components
│   ├── broker/                  # MQTT broker config
│   ├── dashboard/               # Web dashboard
│   └── worker_host/             # Worker runtime
│
├── props/                       # Individual props (self-contained)
│   ├── coffin_jumper/
│   │   ├── firmware/            # ESP32 MicroPython code
│   │   ├── plugin/              # Dashboard plugin
│   │   └── backend/             # Worker logic
│   ├── tesla_hue_nest/          # Tesla trunk + Hue lights + Nest speakers
│   ├── thriller_hue_nest/       # Hue lights + Nest speakers
│   └── example_prop/            # Template for new props
│
└── libs/                        # Shared libraries
    ├── py/                      # Python libs (server-side)
    │   └── halloween_common/    # MQTT topics, schemas
    └── micropython/             # MicroPython libs (ESP32)
        └── mp_common/           # WiFi, MQTT helpers
```

## 🎯 Available Props

### Prop Anatomy

Each prop is self-contained with three components:

1. **Plugin** (`plugin/page.py`) - Dashboard UI card for monitoring and control
2. **Backend** (`backend/worker.py`) - Headless logic that handles MQTT messages, timers, integrations
3. **Firmware** (`firmware/`) - Optional ESP32 MicroPython code for physical control

All props document their MQTT contract in `props/<prop>/topics.md`.

### MQTT Contract Convention

Props follow a standard topic structure (enforced by `libs/py/halloween_common`):

- `halloween/<prop_id>/cmd` - Commands (JSON or text)
- `halloween/<prop_id>/state` - Current state machine state
- `halloween/<prop_id>/status/<key>` - Status messages (info, warn, error)
- `halloween/<prop_id>/telemetry/<category>/<key>` - Sensor data, counters, timings
- `halloween/<prop_id>/availability` - Online/offline status (LWT)

Shared schemas are defined in `libs/py/halloween_common/schemas.py` using Pydantic.

### Currently Active

- **tesla_hue_nest** - Coordinates Tesla trunk, Hue lights, and Nest speakers
- **thriller_hue_nest** - Synchronizes Hue lights with Thriller audio on Nest speakers
- **coffin_jumper** - ESP32-based animatronic with PIR sensor and DFPlayer audio

### Enabling/Disabling Props

Edit `config/dashboard.env`:

```bash
# Option 1: Allow only specific props (recommended)
PLUGIN_PROPS_ALLOW=tesla_hue_nest,thriller_hue_nest,coffin_jumper

# Option 2: Disable specific props
PLUGIN_PROPS_DISABLE=example_prop

# Option 3: Disable all prop plugins
# PLUGIN_DISABLE_ALL_PROPS=1
```

## 🔧 Development

### How Plugins & Workers Are Auto-Discovered

The system uses **runtime discovery** - no hardcoded lists of props.

#### Dashboard Plugin Discovery

The dashboard scans `/opt/props/*/plugin/page.py` and `/app/builtin_plugins/*.py` at startup. Each plugin must define a `class Plugin(BasePlugin)` with:

**Class attributes:**

- `name: str` - Display name for the plugin
- `zone: str` - Optional; either `"card"` (default) or `"topbar"`

**Methods:**

- `layout(self) -> Component` - Returns a Dash component for the UI
- `on_register(self, app, services) -> None` - Registers callbacks and subscriptions

The `BasePlugin` class provides:

- `self.cache` - Thread-safe dict-like cache (use `self.cache[key] = value`)
- `self.cache_get(key, default)` - Safe cache retrieval
- `self.cache_set(key, value)` - Safe cache storage
- `self.mqtt_publish(topic, payload, **kwargs)` - MQTT publish helper
- `self.mqtt_subscribe(topic, callback)` - MQTT subscribe helper

The `services` dict passed to `on_register()` contains:

- `mqtt` - MQTT client instance (wrapped by BasePlugin as helper methods)
- `cache` - Raw shared cache dict (wrapped by BasePlugin as `self.cache`)
- `app` - Dash app instance (for direct callback registration)
- `tick_id` - Global interval component ID (for periodic updates)

#### Worker Backend Discovery

The worker host scans `/app/props/*/backend/worker.py` and `/app/worker_host/builtin_workers/*.py` at startup. Each backend must define a `class Worker(BaseWorker)` with:

**Constructor signature:**

```python
def __init__(self, prop_id: str, mqtt: MqttClientProto, config: dict | BaseModel | None = None)
```

**Optional module-level attributes:**

- `PROP_ID = "my_prop"` - Explicit prop ID (defaults to folder name if not specified)
- `class ConfigModel(BaseModel)` - Pydantic model for validating `config.yaml`

**Methods to implement:**

- `async def start(self) -> None` - Called when worker starts (subscribe to topics, set up timers)
- `async def do_<action>(self, arg: str | dict | None) -> None` - Command handlers (e.g., `do_arm`, `do_play`)

The `BaseWorker` class provides:

- `self.prop_id` - Unique identifier for the prop
- `self.mqtt` - MQTT client (publish, subscribe)
- `self.config` - Parsed config (dict or Pydantic model)
- `self.telemetry(key, value, ...)` - Publish telemetry
- `self.publish_state(state, ...)` - Publish state changes
- `self.publish_status(key, value, ...)` - Publish status messages
- `self.command(target_prop, action, args)` - Send commands to other props
- `self.resolve_config_var(key, default)` - Get config with environment variable substitution

**Command dispatching:**
Messages on `halloween/<prop_id>/cmd` are automatically dispatched to `do_<action>()` methods based on the payload:

- Text payload `"arm"` → calls `do_arm(None)`
- JSON `{"action": "play"}` → calls `do_play(None)`
- JSON `{"action": "chromecast", "args": {"volume": 0.5}}` → calls `do_chromecast({"volume": 0.5})`

### Adding a New Prop

1. Create prop directory structure:

   ```bash
   mkdir -p props/my_prop/{firmware,plugin,backend}
   ```

2. Implement components:

   - `plugin/page.py` - Dashboard UI (must define `class Plugin(BasePlugin)`)
   - `backend/worker.py` - Server logic (must define `class Worker(BaseWorker)`)
   - `firmware/main.py` - ESP32 code (optional)

3. Document MQTT contract:

   - Create `props/my_prop/topics.md`

4. Restart services:

   ```bash
   docker compose -f docker-compose.yml \
                  -f docker-compose.workers.yml \
                  -f docker-compose.dashboard.yml \
                  up --build
   ```

   Often it is necessary to remove the old containers to trigger a complete rebuild:

   ```bash
   docker compose -f docker-compose.yml \
                  -f docker-compose.workers.yml \
                  -f docker-compose.dashboard.yml \
                  rm

   docker compose -f docker-compose.yml \
                  -f docker-compose.workers.yml \
                  -f docker-compose.dashboard.yml \
                  up --build
   ```

The new prop will be auto-discovered and loaded!

### ESP32 Firmware Development

***[Please check information about ESP32 usage in this section before use]***

Props with physical ESP32 hardware use MicroPython firmware located in `props/<prop>/firmware/`.

#### Shared MicroPython Libraries

Common MicroPython utilities are in `libs/micropython/mp_common/`:

- `wifi.py` - WiFi connection with retry/backoff
- `mqtt.py` - Lightweight MQTT pub/sub
- `msgpack_json.py` - Tiny serialization helpers
- `util.py` - Debounce, scheduler, safe reboot utilities

These libraries are **not automatically deployed**. You must copy them into your prop's firmware directory before uploading to the ESP32.

#### Deploying Firmware to ESP32

Example for `coffin_jumper` prop:

```powershell
# PowerShell (Windows)
cd props/coffin_jumper/firmware
.\copy_to_esp32.ps1 COM3

# Or using mpremote directly
mpremote connect COM3 fs cp *.py :
```

The `copy_to_esp32.ps1` script uploads all `.py` files in the firmware directory to the ESP32.

**Note:** If your firmware uses `mp_common` utilities, you must copy them to your firmware directory first:

```bash
# Copy shared libraries to your prop's firmware
cp libs/micropython/mp_common/*.py props/my_prop/firmware/
```

### Prop Configuration

Props can use optional configuration files:

- `backend/config.yaml` or `backend/config.yml` or `backend/config.json`
- Define `class ConfigModel(BaseModel)` in `worker.py` for validation
- Access via `self.config` in worker class
- Use `${ENV_VAR}` syntax for environment variable substitution

Example `config.yaml`:

```yaml
hue_bridge_ip: "192.168.1.100"
api_token: "${HUE_API_TOKEN}"  # Resolved from environment
volume_default: 0.5
```

## 📡 MQTT Topics

All communication follows this convention:

```text
halloween/<prop_id>/cmd                    # Commands (JSON or text)
halloween/<prop_id>/state                  # Current state
halloween/<prop_id>/availability           # online/offline
halloween/<prop_id>/telemetry/<category>/<key>  # Sensor data, counters
```

### Example: Control a Prop

```bash
# Arm the tesla_hue_nest prop
mosquitto_pub -h localhost -t halloween/tesla_hue_nest/cmd -m 'arm'

# Play with JSON command
mosquitto_pub -h localhost -t halloween/tesla_hue_nest/cmd \
  -m '{"action": "play"}'

# Adjust volume
mosquitto_pub -h localhost -t halloween/tesla_hue_nest/cmd \
  -m '{"action": "chromecast", "args": {"volume": 0.7}}'
```

### Monitor Telemetry

```bash
# All telemetry
mosquitto_sub -h localhost -v -t 'halloween/+/telemetry/#'

# Specific prop
mosquitto_sub -h localhost -v -t 'halloween/tesla_hue_nest/#'
```

## 🐛 Troubleshooting

### Speakers Silent After an IP Change

Speakers fetch audio from the media server (port 8000 on the host PC). If Play does nothing, the speaker status shows `idle_reason: ERROR` and the media server log (`docker logs halloween_media_server`) has no request from the speaker, the Windows firewall is probably blocking inbound port 8000 (Wi-Fi is often profiled "Public"). Allow it once, in an Administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Halloween media server" -Direction Inbound -Protocol TCP -LocalPort 8000 -RemoteAddress 10.67.1.0/24 -Action Allow -Profile Any
```


Media URLs are built from `MEDIA_BASE_URL` in the prop's `props/<prop>/backend/config.yml` (the media host's IP, port 8000). Update it in each prop that plays media, then `docker restart halloween_worker_host` (props are bind-mounted, so no rebuild is needed). See `halloween-2026-notes.md` for where each kind of setting lives.

### Tesla Trunk State Out of Sync

The worker caches the trunk state (no read-only query exists, and checking is slow). If the trunk is moved outside the worker, press **Resync Tesla** on the dashboard. This re-reads the real state through a `door_lock` command, so it may lock the doors. Handler errors appear in `docker logs halloween_worker_host` and on the MQTT topic `halloween/<prop>/status/error`.

### Dashboard Not Loading Plugins

Check environment variables in `config/dashboard.env`:

```bash
docker compose logs dashboard
```

Look for plugin discovery messages like:

```text
[plugin_loader] Loaded plugin 'Tesla Hue Nest' from /opt/props/tesla_hue_nest/plugin/page.py (zone=card)
[plugin_loader] Skipping prop plugin 'example_prop' (disabled by env)
```

Verify that:

- Props exist in `/opt/props/` (volume mount working)
- `PLUGIN_PROPS_ALLOW` or `PLUGIN_PROPS_DISABLE` is set correctly
- Plugin files expose required attributes: `name`, `layout`, `register_callbacks`

### Worker Not Starting

Check MQTT connection:

```bash
docker compose logs mqtt worker_host
```

Look for worker discovery messages:

```text
[worker_host] Discovered workers:
  - prop_id=tesla_hue_nest origin=/app/props/tesla_hue_nest/backend/worker.py
```

Verify:

- MQTT credentials in `config/secrets/mqtt_users.env` are correct
- Worker files define `class Worker(BaseWorker)`
- Props are not disabled via `WORKER_PROPS_DISABLE` or `WORKER_PROPS_ALLOW`

### Volume Controls Not Working

Ensure chromecast is connected:

1. Click "(Re)Connect" button in dashboard
2. Check worker logs for connection status
3. Verify network access to Nest speakers

### Missing Python Dependencies

If a prop's backend requires additional packages:

1. Add them to `props/<prop>/backend/requirements.txt`
2. Update `server/worker_host/Dockerfile` to install prop requirements
3. Rebuild: `docker compose build worker_host`

Or add to combined requirements:

```bash
# Add to combined-requirements.txt at repo root
echo "new-package>=1.0.0" >> combined-requirements.txt
docker compose build
```

### Secrets Not Loading

Ensure files exist and are referenced in compose files:

```bash
# Verify secrets exist
ls -la config/secrets/

# Check compose env_file references
cat infra/compose/docker-compose.workers.yml | grep env_file
```

Environment variable format in secrets files:

```bash
# config/secrets/mqtt_users.env
MQTT_ADMIN_USER=admin
MQTT_ADMIN_PW=your_password
```

### Rebuild Everything

## 📚 Documentation

- **Dashboard Plugins**: `server/dashboard/README.md`
- **Workers**: `server/worker_host/WORKERS.md`
- **Architecture** (2025 snapshot): `halloween-2025-structure.md`
- **2026 field notes**: `halloween-2026-notes.md`
- **Individual Props**: See `props/<prop_name>/topics.md`

## ❓ FAQ

### Why not put plugins directly under `server/dashboard/plugins`?

**Portability.** With self-contained props, each prop "carries" its own UI, backend logic, and firmware in one place. The dashboard and worker host just **discover** them at runtime. This makes it easy to add/remove props without modifying server code.

### Can I run a prop's backend in its own container?

**Yes.** You can create a dedicated service in Docker Compose that builds and runs a specific prop's backend. The default pattern uses a single `worker_host` container that loads multiple backends to reduce container sprawl, but you can customize this for specific needs (e.g., isolation, different resource limits).

### Where do integrations (Tesla/Hue/Nest APIs) live?

Keep vendor-specific logic inside the prop's backend (e.g., `props/tesla_hue_nest/backend/`). If multiple props need the same integration, you can:

- Move shared client code to `libs/py/halloween_common/`
- Access it via imports in worker backends
- Keep access tokens/secrets in `config/secrets/`

### How do I disable a specific prop without deleting it?

Use environment variables in `config/dashboard.env`:

```bash
# Disable specific props
PLUGIN_PROPS_DISABLE=example_prop,old_prop

# Or allow only specific props
PLUGIN_PROPS_ALLOW=tesla_hue_nest,thriller_hue_nest
```

The same variables work for workers by setting them in `config/worker_host.env`.

### Can props share state or communicate with each other?

Yes, through MQTT. Props can:

- Subscribe to other props' telemetry topics: `halloween/<other_prop>/telemetry/#`
- Send commands to other props: publish to `halloween/<other_prop>/cmd`
- Use shared MQTT topics for coordination

The dashboard `services.cache` dict also allows plugins to share state, though this is dashboard-only and not visible to workers.

## 👨‍💻 Typical Developer Workflow

1. **Add a new prop** under `props/my_prop/` with firmware, plugin, and backend
2. **Implement using shared libraries**:
   - Plugins import from `plugin_base` (SafeCache, BasePlugin)
   - Workers import from `halloween_common` (schemas, MQTT helpers)
   - Firmware copies from `libs/micropython/mp_common/`
3. **Document MQTT contract** in `props/my_prop/topics.md`
4. **Run locally with dev compose**:

   ```bash
   docker compose -f infra/compose/docker-compose.yml \
                  -f infra/compose/docker-compose.workers.yml \
                  -f infra/compose/docker-compose.dashboard.yml \
                  up --build
   ```

5. **Iterate**: Props are bind-mounted, so code changes are reflected on container restart
6. **Test**: Dashboard auto-loads plugin, worker host auto-loads backend
7. **Deploy**: Use same compose files or bake props into images for production

## 🧪 Running Integration Tests

The `docker-compose.dev.yml` file provides a test environment for running pytest integration tests, currently focused on MQTT broker setup validation:

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.dev.yml \
               up --build
```

This launches an `mqtt_test` service that:

- Installs test dependencies from `libs/py[test]`
- Runs integration tests in `libs/py/tests/integration/mqtt`
- Validates MQTT authentication and connectivity
- Exits after completion

The test results are displayed in the container logs.

## 🎬 2025 Experiences & Learnings

### What Worked Well

- ✅ Web dashboard was significantly better than terminal-based control
- ✅ Modular prop architecture made adding/removing features easy
- ✅ MQTT message bus enabled clean separation of concerns

### Issues Encountered

- ⚠️ Volume settings not persistent (props reset to default volume on each play)
- ⚠️ Stop button should reset cooldown timers for immediate re-arm
- ⚠️ Tesla trunk sometimes out of sync with Hue lights
- ⚠️ Trunk occasionally opened and closed quickly
- ⚠️ Last time attempted, the trunk did not close at end of play, and dashboard buttons would not acutate it
- ⚠️ No direct "play" trigger (required arm + motion sensor)
- ⚠️ Doungeon soundscape not implemented

### TODO

- [ ] Implement dungeon soundscape based on thriller_hue_nest (we don't need hue operation, so simplify)
- [ ] Implement persistent volume settings across container restarts
- [ ] Move hue, chromecast and tesla integration apis to common folder and ensure import from there
- [ ] Add cooldown timer reset on stop command
- [ ] Add direct play mode (bypass motion sensors)
- [ ] Remote config editing from dashboard
- [ ] Prop removal/relaunching from dashboard
- [ ] Tesla state machine intensive testing and debugging
- [x] Persistent Hue bridge key storage (`config/secrets/hue/`, see `halloween-2026-notes.md`)
- [ ] Per-prop documentation page (`props/<prop>/README.md`): purpose, hardware, config.yml settings (Hue IP, media URL, Chromecast IPs), secrets, MQTT topics, dashboard controls, troubleshooting
- [ ] Fire counter and Tesla open counter (persistent)
- [ ] Mounted config volumes with reload capability

## 📄 License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

## 🤝 Contributing

1. Create a new prop under `props/my_prop/`
2. Follow the self-contained structure (firmware/plugin/backend)
3. Document MQTT topics in `topics.md`
4. Test with development compose setup
5. Submit pull request

---

## Happy Haunting! 🎃👻
