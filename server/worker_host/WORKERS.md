# 🧠 Worker Host and Prop Workers

The **Worker Host** is a lightweight runtime service that manages backend logic for all props in the Halloween project.  
It loads individual **worker classes** — one per prop — and bridges them to the MQTT message bus.

---

## 🎯 Purpose

Each **prop** (e.g. `coffin_jumper`, `grave_shaker`, `fog_machine`) may have:

```
props/<prop_name>/
├── firmware/    → Code running on the prop hardware (ESP32 etc.)
├── plugin/      → Dashboard plugin for visualizing or controlling the prop
└── backend/     → Python worker logic (runs on the server)
```

The `worker_host` container:
- Automatically **discovers** all workers under `props/*/backend/worker.py`
- Optionally loads **built-in workers** (e.g. `heartbeat`) for testing or utilities
- Connects them to the MQTT broker
- Dispatches and **publishes commands** between props and other workers
- Handles telemetry and status topics for all workers

This design keeps each prop **self-contained** while allowing inter‑prop coordination.

`BaseWorker` owns the persistent activation counter. Prop workers call
`record_activation()` only when a main-prop play starts; the shared
`reset_counter` command clears it. Counts are published as retained
`playback/PlayCount` telemetry and stored under `WORKER_STATE_DIR`. The worker
Compose service mounts a named volume there so counts survive container
recreation.

---

## 🧩 Worker Concept

A **Worker** is a Python class that inherits from `BaseWorker` and exposes methods for:

| Category | Example | MQTT topic |
|-----------|----------|-------------|
| Control commands (subscribe) | `do_ping()` | `halloween/<prop_id>/cmd` |
| Control commands (publish) | `self.command("fogger", "start")` | `halloween/fogger/cmd` |
| Status updates | `status("availability", "online")` | `halloween/<prop_id>/status/#` |
| Telemetry (data) | `telemetry("tick", 0)` | `halloween/<prop_id>/telemetry/#` |

Each worker runs **inside the `worker_host` container**, not on the prop hardware.  
Workers can both **receive** and **send** commands, allowing central coordination between props.

---

## 🏗️ Project Structure

```
server/
  worker_host/
    Dockerfile
    pyproject.toml
    WORKERS.md              ← this file
    builtin_workers/
      heartbeat.py          ← example built-in worker
    worker_host/
      base.py
      loader.py
      main.py
      mqtt.py
props/
  example_prop/
    backend/
      worker.py             ← example prop worker
      config.json           ← optional config
```

---

## ⚙️ How the Worker Host Works

1. On startup, `worker_host` connects to the MQTT broker.
2. It scans for:
   - Built-in workers under `server/worker_host/builtin_workers/*.py`
   - Prop-specific workers under `props/*/backend/worker.py`
3. Each worker is instantiated and started.
4. MQTT messages to `halloween/<prop_id>/cmd` are routed to the right worker.
5. Workers may **publish commands** to other props or firmware using `self.command()`.
6. The worker can publish telemetry and status updates back over MQTT.

---

## 🧱 Defining a Worker

A worker file must define a **class named `Worker`** that inherits from `BaseWorker`.

Example: `props/example_prop/backend/worker.py`
```python
from __future__ import annotations
import asyncio
from worker_host.base import BaseWorker

class Worker(BaseWorker):
    """Example prop backend demonstrating telemetry and commands."""

    async def start(self) -> None:
        # Initialize and start background tasks
        await super().start()
        self.spawn(self._ticker())

    async def _ticker(self):
        """Publish a counter every few seconds."""
        i = 0
        interval = int(self.config.get("tick_interval", 5))
        try:
          while True:
              self.telemetry("tick", i)
              i += 1
              await asyncio.sleep(interval)
        except asyncio.CancelledError:
          # Optional: cleanup hardware, close files, etc.
          raise

    async def do_ping(self, arg: str | None):
        """Respond to 'ping' command."""
        self.telemetry("pong", arg or "ok")

    async def do_fire(self, arg: str | None):
        """Example of commanding another prop."""
        # Send a command to the fogger prop
        self.command("fogger", "start", arg)
```

### 🔁 About `spawn()`

`spawn(coro)` is a convenience that starts a background **async task** tied to the worker’s lifecycle.

- **Start:** When you call `self.spawn(coro)`, the coroutine is scheduled immediately with `asyncio.create_task(...)` and tracked internally.
- **Run:** The task runs **concurrently** with message handling. Use it for loops, timers, polling I/O, sensor reading, retry logic, etc.
- **Stop:** On shutdown, `BaseWorker.stop()` **cancels** all spawned tasks (`task.cancel()`), then publishes the worker’s availability = `offline`.
- **Your responsibility:** Inside long‑running tasks, handle cancellation cleanly so shutdowns are fast and safe:

```python
async def _ticker(self):
    try:
        while True:
            self.telemetry("tick", "...")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        # Optional: cleanup hardware, close files, etc.
        raise
```

**Tips**  
- Use **`spawn` for non-blocking background work**. Never `await` a never-ending loop in `start()`—that would block startup.  
- If your task can raise, catch and report within the task (e.g., `status("error", ...)`) to avoid silent failures.  
- Prefer short sleeps inside loops so cancellation is responsive.

---

## 🧮 Configuration Files

Each worker can have an optional **configuration file** placed next to `worker.py`.  
Supported formats are `config.json`, `config.yaml`, or `config.yml`.

When present, the configuration file is automatically read by the `worker_host` at startup and passed to the worker instance.  
Inside the worker, all configuration values are available through a dictionary named **`self.config`**.

Example:

**`props/example_prop/backend/config.json`**
```json
{
  "tick_interval": 3,
  "enabled": true,
  "targets": ["fogger", "coffin_jumper"]
}
```

**Access inside the worker**
```python
interval = self.config.get("tick_interval", 5)
targets = self.config.get("targets", [])
if self.config.get("enabled", True):
    self.broadcast(targets, "set_mode", "armed")
```

You can use these settings to control behaviour such as timing, limits, or feature toggles.  
If no config file is present, `self.config` will be an empty dictionary (`{}`).  
If the file cannot be parsed, the worker still loads, and a warning is printed to the logs.

**Tips:**
- Use flat keys when possible (e.g. `tick_interval`, `max_speed`).
- JSON is parsed natively; YAML requires the `pyyaml` package.
- Always use `.get(key, default)` to avoid `KeyError`.
- You can document configuration options in a `config.example.json` file.

---

## 🔄 Publishing Commands from Workers

Workers can publish commands to other props or workers.  
This makes it possible to create **coordinator** or **supervisor** workers that control several props at once.

Two helper methods are available in `BaseWorker`:

```python
self.command(target_prop, action, args=None, qos=1)
self.broadcast([target1, target2], action, args=None, qos=1)
```

### Example
```python
# Send a single command
self.command("coffin_jumper", "set_mode", "armed")

# Broadcast to several props
targets = ["fogger", "grave_shaker"]
self.broadcast(targets, "set_mode", "armed")
```

These publish MQTT messages to:
```
halloween/<target_prop>/cmd
```
with a payload such as:
```json
{"action": "set_mode", "args": "armed"}
```

This mechanism works for both **other workers** and **prop firmware**, as long as they subscribe to the same command topic.

---

## 🧰 Built-in Workers

The folder `server/worker_host/builtin_workers/` can contain reusable or testing workers.  
One is included by default:

### `heartbeat.py`
```python
from __future__ import annotations
import asyncio
from worker_host.base import BaseWorker

class Worker(BaseWorker):
    """
    Built-in demo worker.
    - Publishes a tick counter every few seconds.
    - Responds to commands on halloween/heartbeat/cmd
    """

    async def start(self) -> None:
        await super().start()
        self.spawn(self._ticker())

    async def _ticker(self):
        i = 0
        while True:
            self.telemetry("tick", i)
            i += 1
            await asyncio.sleep(self.config.get("tick_interval", 5))

    async def do_ping(self, arg: str | None):
        self.telemetry("pong", arg or "ok")

    async def do_set_mode(self, arg: str | None):
        mode = (arg or "").strip() or "idle"
        self.telemetry("mode", mode)

    async def do_fire(self, arg: str | None):
        ms = int(arg or "500")
        self.telemetry("event", f"fire start {ms}ms")
        await asyncio.sleep(ms / 1000)
        self.telemetry("event", "fire end")
```

---

## 🚀 Testing the Example Worker

When you start the `worker_host` container, it loads the built-in `heartbeat` worker by default.

### Verify it's running
```bash
mosquitto_sub -h localhost -v -t 'halloween/heartbeat/#'
```
You should see:
```
halloween/heartbeat/status/availability online
halloween/heartbeat/telemetry/tick 0
halloween/heartbeat/telemetry/tick 1
```

### Send commands
```bash
# Simple text commands
mosquitto_pub -h localhost -t 'halloween/heartbeat/cmd' -m 'ping hello'
mosquitto_pub -h localhost -t 'halloween/heartbeat/cmd' -m 'set_mode armed'
mosquitto_pub -h localhost -t 'halloween/heartbeat/cmd' -m 'fire 750'

# JSON command format
mosquitto_pub -h localhost -t 'halloween/heartbeat/cmd' -m '{"action": "ping", "args": "hello"}'
```

Observe responses:
```
halloween/heartbeat/telemetry/pong hello
halloween/heartbeat/telemetry/mode armed
halloween/heartbeat/telemetry/event fire start 750ms
```

---

## 🔧 Enabling or Disabling Built-in Workers

You can control which built-ins are loaded using environment variables.

| Variable | Description | Example |
|-----------|--------------|----------|
| `WORKER_DISABLE_ALL_BUILTINS` | Disable all built-ins | `1` |
| `WORKER_BUILTINS_DISABLE` | Comma-separated names to skip | `heartbeat,smoketest` |
| `WORKER_BUILTINS_ALLOW` | Only load specific built-ins | `heartbeat` |

**Precedence:**  
`WORKER_DISABLE_ALL_BUILTINS` → `WORKER_BUILTINS_ALLOW` → `WORKER_BUILTINS_DISABLE`

### Example in Docker Compose
```yaml
  worker_host:
    build:
      context: .
      dockerfile: server/worker_host/Dockerfile
    environment:
      MQTT_HOST: broker
      WORKER_BUILTINS_DISABLE: "heartbeat"
```

---

## 🧩 Topics Summary

| Type | Direction | Example topic | Notes |
|------|------------|----------------|--------|
| Control command | ↔ | `halloween/<prop_id>/cmd` | Workers can both **subscribe** and **publish** commands |
| Telemetry | ← worker | `halloween/<prop_id>/telemetry/<key>` | Arbitrary key/value pairs |
| Status | ← worker | `halloween/<prop_id>/status/<key>` | Human-readable messages |
| Availability | ← worker | `halloween/<prop_id>/status/availability` | `"online"` / `"offline"` |
| Worker host uptime | ← host | `halloween/worker_host/uptime` | Seconds since start |

---

## ✅ Summary

- Workers encapsulate **prop-specific logic** running inside `worker_host`.
- They talk via MQTT using clear topic conventions.
- Each worker is a **Python class** in its prop’s `backend/` folder.
- Any `config.json` or similar file is parsed automatically, and its contents are accessible inside the worker as `self.config`.
- Workers can **both receive and send commands** via MQTT, enabling coordination between props and centralized control.
- Use `spawn()` for long‑running background tasks that are automatically cancelled on shutdown.
- The host can also run **built-in** workers for testing or global features.
- Built-ins can be easily **disabled in production** using environment variables.

This modular design keeps the system clean:
- **Hardware code** lives under `firmware/`
- **Dashboard UI** under `plugin/`
- **Server logic** under `backend/`

All pieces are connected through MQTT, giving a fully distributed but maintainable setup.

---
