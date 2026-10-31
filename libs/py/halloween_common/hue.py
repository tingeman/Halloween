"""Shared Philips Hue access for all props.

All props in the worker host run in one process and talk to the same bridge.
The bridge only handles ~10 requests/s in total, so everything that talks to a
bridge goes through one ``HueHub`` per bridge IP:

- **Sensors**: one background thread reads ``GET /sensors`` (all sensors in a
  single request) every ``HUE_SENSOR_INTERVAL`` seconds. ``HueSensor.refresh()``
  only reads that snapshot, so it never waits on the network, and the bridge
  load does not grow with the number of props or sensors.
- **Inventory**: lights and sensors (names, types, capabilities) are read with
  one ``GET /lights`` + ``GET /sensors``, at start-up, on ``HueLights.resolve()``
  (the props' "hue connect" action) and every ``INVENTORY_INTERVAL`` seconds.
- **Light commands**: queued and sent by one writer thread, at most
  ``HUE_MAX_WRITES_PER_S`` per second. Commands for a light that has not been
  sent yet are merged (the newest values win), so a slow bridge never builds up
  a backlog. A delayed command (the "off" at the end of a fade) is cancelled by
  any newer command for that light.

``HueLights`` and ``HueSensor`` objects belong to one prop and keep their own
state; the presets in ``PRESETS`` are read-only. Light names are matched exactly
(ignoring case). Sensor names are matched exactly first, then as a substring, and
only motion (presence) sensors are considered.

Transition times follow the Hue API: tenths of a second.
"""
import os
import random
import threading
import time
from types import MappingProxyType

import httpx
from phue import Bridge as _Bridge, PhueRegistrationException  # noqa: F401  (re-exported)

SENSOR_INTERVAL = float(os.environ.get("HUE_SENSOR_INTERVAL", "0.5"))
STALE_AFTER = float(os.environ.get("HUE_STALE_AFTER", "5"))
MAX_WRITES_PER_S = float(os.environ.get("HUE_MAX_WRITES_PER_S", "8"))
INVENTORY_INTERVAL = 300.0
MAX_POLL_BACKOFF = 5.0


def _frozen(**kw):
    return MappingProxyType(kw)


# Read-only: build a new dict from a preset before changing anything.
PRESETS = MappingProxyType({
    'red':         _frozen(transitiontime=70, on=True, bri=90,  hue=65535, sat=255),
    'yellow':      _frozen(transitiontime=70, on=True, bri=58,  hue=13539, sat=252),
    'pink':        _frozen(transitiontime=70, on=True, bri=58,  hue=58294, sat=253),
    'purple':      _frozen(transitiontime=70, on=True, bri=136, hue=58294, sat=253),
    'green':       _frozen(transitiontime=70, on=True, bri=120, hue=25500, sat=254),
    'blue':        _frozen(transitiontime=70, on=True, bri=120, hue=46920, sat=254),
    'normal':      _frozen(transitiontime=70, on=True, bri=144, hue=13524, sat=200),
    'normal_lstr': _frozen(transitiontime=70, on=True, bri=144, hue=7676,  sat=199),
    'dimmed':      _frozen(transitiontime=70, on=True, bri=0),
    'off':         _frozen(transitiontime=0,  on=False),
})


def _retry(fn, attempts=3, delay=1.5):
    """Call fn, retrying on bridge errors such as request timeouts."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if attempt == attempts - 1:
                raise
            print(f"[hue] request failed ({e}); retrying...")
            time.sleep(delay)


def _by_id(data):
    return {int(k): v for k, v in (data or {}).items()}


def _norm(name):
    return (name or "").strip().lower()


class HueHub:
    """Everything that talks to one bridge. Get the shared one with ``HueHub.get(ip)``."""

    _hubs: dict = {}
    _hubs_lock = threading.Lock()

    @classmethod
    def get(cls, bridge_ip):
        with cls._hubs_lock:
            hub = cls._hubs.get(bridge_ip)
            if hub is None:
                hub = cls(bridge_ip)      # not stored if it fails, so a later call retries
                cls._hubs[bridge_ip] = hub
            return hub

    def __init__(self, bridge_ip, bridge=None, start=True):
        self.ip = bridge_ip
        if bridge is None:
            # One pooled HTTP client with short timeouts, shared by every request to this bridge
            client = httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0))
            # HUE_CONFIG_FILE lets the registration key live on a persistent volume (default: ~/.python_hue)
            bridge = _Bridge(bridge_ip, config_file_path=os.environ.get("HUE_CONFIG_FILE"), http_client=client)
        self.bridge = bridge

        self._lock = threading.Lock()   # guards the snapshots and light claims
        self.lights: dict[int, dict] = {}
        self.sensors: dict[int, dict] = {}
        self.sensors_at = 0.0           # monotonic time of the last successful sensor read
        self.inventory_at = 0.0
        self.last_error = None
        self._claims: dict[int, str] = {}

        self._cv = threading.Condition()   # guards the write queue
        self._pending: dict[int, dict] = {}            # light id -> command to send now
        self._scheduled: dict[int, tuple] = {}         # light id -> (due time, command)
        self._in_flight = 0

        _retry(self.refresh_inventory, attempts=4, delay=2.0)
        if start:
            threading.Thread(target=self._poll_loop, daemon=True, name=f"hue-poll-{bridge_ip}").start()
            threading.Thread(target=self._write_loop, daemon=True, name=f"hue-write-{bridge_ip}").start()

    # ------------------------------------------------------------ reading
    def _get(self, resource):
        return _by_id(self.bridge.request("GET", f"/api/{self.bridge.username}/{resource}"))

    def refresh_inventory(self):
        """Re-read all lights and sensors (names, capabilities and sensor states)."""
        lights = self._get("lights")
        sensors = self._get("sensors")
        now = time.monotonic()
        with self._lock:
            self.lights, self.inventory_at = lights, now
            self.sensors, self.sensors_at = sensors, now

    def poll_sensors(self):
        sensors = self._get("sensors")
        with self._lock:
            self.sensors, self.sensors_at = sensors, time.monotonic()

    def _poll_loop(self):
        delay = SENSOR_INTERVAL
        while True:
            try:
                if time.monotonic() - self.inventory_at > INVENTORY_INTERVAL:
                    self.refresh_inventory()
                else:
                    self.poll_sensors()
                if self.last_error is not None:
                    print(f"[hue] bridge {self.ip} is responding again")
                self.last_error = None
                delay = SENSOR_INTERVAL
            except Exception as e:
                if self.last_error is None:
                    print(f"[hue] reading sensors from bridge {self.ip} failed: {e}")
                self.last_error = e
                delay = min(delay * 2, MAX_POLL_BACKOFF)
            time.sleep(delay)

    def sensor_state(self, sensor_id):
        """Return ``(state dict, age in seconds)`` from the last sensor read. Never does I/O."""
        with self._lock:
            sensor = self.sensors.get(sensor_id)
            age = time.monotonic() - self.sensors_at
        return (sensor or {}).get("state", {}), age

    def find_sensor(self, name):
        """Return ``(id, name)`` of the motion sensor called ``name``, or None.

        An exact (case-insensitive) name wins; otherwise the first motion sensor whose
        name contains ``name``. Only sensors that report ``presence`` are considered.
        """
        wanted = _norm(name)
        with self._lock:
            motion = [(sid, s.get("name", "")) for sid, s in sorted(self.sensors.items())
                      if "presence" in s.get("state", {})]
        for sid, sname in motion:
            if _norm(sname) == wanted:
                return sid, sname
        for sid, sname in motion:
            if wanted in _norm(sname):
                return sid, sname
        return None

    def find_lights(self, names):
        """Return ``(ids, missing names)`` for lights whose names match exactly (ignoring case)."""
        ids, missing = [], []
        with self._lock:
            lights = sorted(self.lights.items())
        for name in names:
            matches = [lid for lid, light in lights if _norm(light.get("name")) == _norm(name)]
            if matches:
                ids.extend(lid for lid in matches if lid not in ids)
            else:
                missing.append(name)
        return ids, missing

    def light_info(self, light_id):
        return self.lights.get(light_id, {})

    def claim(self, owner, light_ids):
        """Record which prop uses which lights, and warn when two props share one."""
        conflicts = []
        with self._lock:
            for lid in [lid for lid, o in self._claims.items() if o == owner]:
                del self._claims[lid]
            for lid in light_ids:
                other = self._claims.get(lid)
                if other is not None and other != owner:
                    conflicts.append(f"{self.light_info(lid).get('name', lid)} (also used by {other})")
                self._claims[lid] = owner
        if conflicts:
            print(f"[hue] WARNING: {owner} shares lights with another prop: {', '.join(conflicts)}")

    # ------------------------------------------------------------ writing
    def submit(self, light_id, cmd, delay=0.0):
        """Queue a state command for one light.

        A command without delay is merged into any unsent command for the light and
        cancels its delayed command. A delayed command replaces the earlier delayed one.
        """
        with self._cv:
            if delay > 0:
                self._scheduled[light_id] = (time.monotonic() + delay, dict(cmd))
            else:
                self._scheduled.pop(light_id, None)
                self._pending.setdefault(light_id, {}).update(cmd)
            self._cv.notify_all()

    def _next_due(self):
        """With ``_cv`` held: pop the next command to send, or say how long to wait."""
        now = time.monotonic()
        for lid, (due, cmd) in list(self._scheduled.items()):
            if due <= now:
                del self._scheduled[lid]
                self._pending.setdefault(lid, {}).update(cmd)
        if self._pending:
            lid = next(iter(self._pending))
            return lid, self._pending.pop(lid), None
        wait = min((due for due, _ in self._scheduled.values()), default=None)
        return None, None, (None if wait is None else max(0.0, wait - now))

    def _write_loop(self):
        interval = 1.0 / MAX_WRITES_PER_S
        while True:
            with self._cv:
                while True:
                    lid, cmd, wait = self._next_due()
                    if lid is not None:
                        self._in_flight += 1
                        break
                    self._cv.wait(timeout=wait)
            try:
                self.write_light(lid, cmd)
            finally:
                with self._cv:
                    self._in_flight -= 1
                    self._cv.notify_all()
            time.sleep(interval)

    def _fit(self, light_id, cmd):
        """Drop settings the light does not support (colour on a white bulb, dimming on a plug)."""
        state = self.light_info(light_id).get("state", {})
        out = {}
        for key, value in cmd.items():
            if key == "on" or key in state or (key == "transitiontime" and "bri" in state):
                out[key] = value
        return out

    def write_light(self, light_id, cmd):
        payload = self._fit(light_id, cmd)
        if not payload:
            return
        try:
            self.bridge.request("PUT", f"/api/{self.bridge.username}/lights/{light_id}/state", payload)
        except Exception as e:   # one light's error must not stop the writer
            print(f"[hue] command to light {self.light_info(light_id).get('name', light_id)} failed: {e}")

    def flush(self, timeout=3.0):
        """Wait until every queued command (not the delayed ones) has been sent."""
        end = time.monotonic() + timeout
        with self._cv:
            while self._pending or self._in_flight:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True


class HueLights:
    """The lights of one prop, found by exact name (ignoring case).

    ``light_names`` is one name or a list of names (a prop's ``HUE_SPOT_LIGHT``).
    ``owner`` names the prop in warnings about lights shared between props.
    """

    disco_cmds = ['red', 'red', 'red', 'red', 'red', 'yellow', 'yellow', 'yellow', 'pink', 'purple']
    disco_durations = [1, 2, 3, 4]   # seconds
    disco_brightness = [0, 0, 0, 50, 50, 50, 100, 150]

    def __init__(self, bridge_ip, light_names, owner=None):
        self.bridge_ip = bridge_ip
        self.hub = HueHub.get(bridge_ip)
        self.names = [light_names] if isinstance(light_names, str) else list(light_names)
        self.owner = owner or f"lights-{id(self):x}"
        self._lock = threading.Lock()   # orders disco commands against stop_disco()
        self._disco_stop = None
        self.lights_uids: list[int] = []
        self.lights: dict[str, int] = {}
        self.missing: list[str] = []
        self.resolve(refresh=False)    # the hub has just read the inventory

    def resolve(self, refresh=True):
        """Look up the configured names again (picks up renamed or new lights)."""
        if refresh:
            _retry(self.hub.refresh_inventory)
        ids, missing = self.hub.find_lights(self.names)
        self.lights_uids, self.missing = ids, missing
        self.lights = {self.hub.light_info(lid).get("name", str(lid)): lid for lid in ids}
        self.hub.claim(self.owner, ids)
        if missing:
            print(f"[hue] {self.owner}: no light named {', '.join(repr(n) for n in missing)} on bridge {self.bridge_ip}")
        self.list_lights()
        if not ids:
            raise LookupError(f"No Hue light named {', '.join(repr(n) for n in self.names)} on bridge {self.bridge_ip}")

    def __len__(self) -> int:
        return len(self.lights_uids)

    def list_lights(self):
        for name, lid in self.lights.items():
            state = self.hub.light_info(lid).get("state", {})
            reach = "" if state.get("reachable", True) else "   (UNREACHABLE)"
            print(f"[hue] {self.owner}: light {lid:>3} {name:30} on: {state.get('on')}, bri: {state.get('bri')}{reach}")

    def send_command(self, cmd=None, uids=None, **kwargs):
        """Send a preset (``cmd``) and/or raw settings (``bri=…, transitiontime=…``) to the lights."""
        cmd_dict = dict(PRESETS[cmd]) if cmd is not None else {}
        cmd_dict.update(kwargs)
        if cmd == 'off':
            self.stop_disco()
        for lid in (self.lights_uids if uids is None else uids):
            self.hub.submit(lid, cmd_dict)

    def lights_off(self, transitiontime=50):
        """Fade to dark over ``transitiontime`` (1/10 s), then switch off."""
        self.stop_disco()
        dimmed = dict(PRESETS['dimmed'], transitiontime=transitiontime)
        for lid in self.lights_uids:
            self.hub.submit(lid, dimmed)
            self.hub.submit(lid, PRESETS['off'], delay=transitiontime / 10.0)

    def flush(self, timeout=3.0):
        return self.hub.flush(timeout)

    # ------------------------------------------------------------ disco
    @property
    def disco_on(self):
        return self._disco_stop is not None

    @disco_on.setter
    def disco_on(self, value):
        if value:
            self.start_disco()
        else:
            self.stop_disco()

    def start_disco(self, uids=None):
        with self._lock:
            if self._disco_stop is not None:
                return
            stop = self._disco_stop = threading.Event()
        ids = list(self.lights_uids if uids is None else uids)
        threading.Thread(target=self._disco_loop, args=(stop, ids), daemon=True,
                         name=f"hue-disco-{self.owner}").start()

    def stop_disco(self):
        # Under the lock, so no disco command can be queued after this returns
        with self._lock:
            if self._disco_stop is not None:
                self._disco_stop.set()
                self._disco_stop = None

    def _disco_loop(self, stop, ids):
        next_change = {lid: 0.0 for lid in ids}
        while ids and not stop.is_set():
            now = time.monotonic()
            for lid, due in next_change.items():
                if due > now:
                    continue
                duration = random.choice(self.disco_durations)
                cmd = dict(PRESETS[random.choice(self.disco_cmds)],
                           transitiontime=duration * 5, bri=random.choice(self.disco_brightness))
                with self._lock:
                    if stop.is_set():
                        return
                    self.hub.submit(lid, cmd)
                next_change[lid] = now + duration
            stop.wait(max(0.05, min(next_change.values()) - time.monotonic()))


class HueSensor:
    """A Hue motion sensor. ``refresh()`` reads the shared snapshot and never waits on the bridge."""

    def __init__(self, bridge_ip, name):
        self.bridge_ip = bridge_ip
        self.hub = HueHub.get(bridge_ip)
        found = self.hub.find_sensor(name)
        if found is None:
            raise LookupError(f"No Hue motion sensor named {name!r} on bridge {bridge_ip}")
        self.sensor_id, self.name = found
        print(f"[hue] motion sensor {name!r} -> {self.name} (id {self.sensor_id})")
        self.sensor_state, self.presence, self.updated = {}, False, None
        self.refresh()

    def refresh(self):
        state, age = self.hub.sensor_state(self.sensor_id)
        if age > STALE_AFTER:
            raise TimeoutError(f"Hue sensor data is {age:.0f} s old (bridge {self.bridge_ip}: {self.hub.last_error})")
        if not state:
            raise LookupError(f"Hue sensor {self.name} (id {self.sensor_id}) is no longer on the bridge")
        self.sensor_state = state
        self.presence = bool(state.get("presence", False))
        self.updated = state.get("lastupdated")

    def has_presence(self):
        return self.presence


def connect_sensors(bridge_ip, names):
    """Create a HueSensor per name. Returns ``(sensors, errors)``; one bad name does not stop the rest."""
    sensors, errors = [], []
    for name in names:
        try:
            sensors.append(HueSensor(bridge_ip, name))
        except Exception as e:
            print(f"[hue] {e}")
            errors.append(f"{name}: {e}")
    return sensors, errors
