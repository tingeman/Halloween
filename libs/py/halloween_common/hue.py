"""Shared Philips Hue access for all props.

All props in the worker host run in one process and talk to the same bridge and
motion sensors. This module gives them:

- one ``phue`` Bridge per bridge IP (``get_bridge``), and
- a shared sensor-state cache (``HueSensor.refresh``): a sensor is fetched from
  the bridge at most once per ``SENSOR_MIN_INTERVAL`` seconds no matter how many
  props or threads poll it. The Hue bridge only handles ~10 requests/s in total.

Set ``HUE_SENSOR_MIN_INTERVAL`` (seconds, default 0.25) to change the interval.
"""
import colorsys
import os
import random
import threading
import time

from phue import Bridge as _Bridge, PhueRegistrationException

SENSOR_MIN_INTERVAL = float(os.environ.get("HUE_SENSOR_MIN_INTERVAL", "0.25"))

_bridges: dict[str, _Bridge] = {}
_bridges_lock = threading.Lock()


def get_bridge(bridge_ip):
    """Return the shared phue Bridge for this IP, creating it on first use."""
    with _bridges_lock:
        bridge = _bridges.get(bridge_ip)
        if bridge is None:
            # HUE_CONFIG_FILE lets the registration key live on a persistent volume (default: ~/.python_hue)
            bridge = _Bridge(bridge_ip, config_file_path=os.environ.get("HUE_CONFIG_FILE"))
            _bridges[bridge_ip] = bridge
        return bridge


Bridge = get_bridge


class _SensorCacheEntry:
    def __init__(self):
        self.lock = threading.Lock()
        self.fetched_at = None      # monotonic time of the last fetch attempt
        self.state = None
        self.error = None


_sensor_cache: dict[tuple, _SensorCacheEntry] = {}
_sensor_cache_lock = threading.Lock()


def get_sensor_state(bridge_ip, sensor_id, max_age=None):
    """Return the sensor's ``state`` dict, fetching from the bridge at most once per max_age seconds.

    Concurrent callers wait for a single fetch and share its result. A failed
    fetch is also remembered for the interval and re-raised to every caller in it,
    so an unresponsive bridge is not hammered.
    """
    max_age = SENSOR_MIN_INTERVAL if max_age is None else max_age
    key = (bridge_ip, sensor_id)
    with _sensor_cache_lock:
        entry = _sensor_cache.setdefault(key, _SensorCacheEntry())
    with entry.lock:
        now = time.monotonic()
        if entry.fetched_at is None or now - entry.fetched_at >= max_age:
            entry.fetched_at = now
            try:
                entry.state = get_bridge(bridge_ip).get_sensor(sensor_id=sensor_id)["state"]
                entry.error = None
            except Exception as e:
                entry.error = e
        if entry.error is not None:
            raise entry.error
        return entry.state


class HueBridge:
    def __init__(self, bridge_ip):
        self.bridge_ip = bridge_ip
        self.b = get_bridge(bridge_ip)


class HueLights:

    commands = {'red':         {'transitiontime': 70, 'on':  True, 'bri' : 90, 'hue': 65535, 'sat': 255},
                'yellow':      {'transitiontime': 70, 'on':  True, 'bri':  58, 'hue': 13539, 'sat': 252},
                'pink':        {'transitiontime': 70, 'on':  True, 'bri':  58, 'hue': 58294, 'sat': 253},
                'purple':      {'transitiontime': 70, 'on':  True, 'bri': 136, 'hue': 58294, 'sat': 253},
                'normal':      {'transitiontime': 70, 'on':  True, 'bri': 144, 'hue': 13524, 'sat': 200},
                'normal_lstr': {'transitiontime': 70, 'on':  True, 'bri': 144, 'hue':  7676, 'sat': 199},
                'dimmed':      {'transitiontime': 70, 'on':  True, 'bri':   0},
                'off':         {'transitiontime':  0, 'on': False},
                }

    disco_cmds = ['red', 'red', 'red', 'red', 'red', 'yellow', 'yellow', 'yellow', 'pink', 'purple']
    disco_durations =  [1,2,3,4]   # in seconds
    disco_brightness = [0, 0, 0, 50, 50, 50, 100, 150]

    def __init__(self, bridge_ip, light_pattern):
        self.run_disco = False
        self.bridge_ip = bridge_ip
        self.b = get_bridge(bridge_ip)

        # get lights
        lights = self.b.get_light_objects('name')

        # Get the appropriate lights (only include reachable ones)
        self.lights = {}
        self.lights_uids = []
        light_pattern = light_pattern.lower()

        unreachable_lights = []
        for light_name in lights.keys():
            if light_pattern in light_name.lower():
                light = lights[light_name]
                # Check if light is reachable before adding it
                if light.reachable:
                    self.lights[light_name] = light
                else:
                    unreachable_lights.append(light_name)

        # Report unreachable lights that matched the pattern
        if unreachable_lights:
            print(f"Found {len(unreachable_lights)} unreachable light(s) matching pattern '{light_pattern}': {', '.join(unreachable_lights)}")

        self.lights_uids = [l.light_id for l in self.lights.values()]
        self.lights_types = [l.type for l in self.lights.values()]
        self.list_lights()


    def __len__(self) -> int:
        return len(self.lights)

    def lights_off(self, transitiontime=50):
        self.disco_on = False
        cmd = self.commands['dimmed']
        cmd['transitiontime'] = transitiontime
        self.b.set_light(self.lights_uids, cmd)

        timer = threading.Timer(transitiontime/10., self.b.set_light, args=[self.lights_uids, self.commands['off']])
        timer.start()

    def send_command(self, cmd=None, uids=None,  **kwargs):
        if uids is None:
            uids = self.lights_uids

        if cmd is not None:
            cmd_dict = self.commands[cmd]
            cmd_dict.update(**kwargs)
        else:
            cmd_dict = kwargs

        if cmd == 'off':
            self.disco_on = False

        bw_uids = []
        color_uids = []

        for id, uid in enumerate(uids):
            if 'color' in self.lights_types[id]:
                color_uids.append(uid)
            else:
                bw_uids.append(uid)

        if len(color_uids)>0:
            self.b.set_light(color_uids, cmd_dict)
        if len(bw_uids)>0:
            # Remove color keys for bw lights
            bw_cmd = {k: v for k, v in cmd_dict.items() if k not in ['hue', 'sat']}
            self.b.set_light(bw_uids, bw_cmd)

    def list_lights(self):
        for id, (n, l) in enumerate(self.lights.items()):
            try:
                print("{0})  {1:30}     'on': {2:>5s}, 'bri': {3:>3d}, 'hue': {4:>5d}, 'sat': {5:>3d}".format(id, n, (lambda x: 'True' if x else 'False')(l.on), l.brightness, l.hue, l.saturation))
            except Exception as e:
                print("{0})  {1:30}     'on': {2:>5s}, 'bri': {3:>3d}".format(id, n, (lambda x: 'True' if x else 'False')(l.on), l.brightness))

    def start_disco(self, uids=None):
        self.disco_on = True

        if uids is None:
            uids = self.lights_uids

        for uid in uids:
            self._disco_callback(uid)

    def _disco_callback(self, uid):
        if not self.disco_on:
            print('stopping disco (uid: {0})'.format(uid))
            return     # return without scheduling new

        rcmd = random.choice(self.disco_cmds)
        rtransition = random.choice(self.disco_durations)
        rbri = random.choice(self.disco_brightness)

        self.send_command(uids=[uid], cmd=rcmd, transitiontime=rtransition*5, bri=rbri)

        # schedule next light change
        timer = threading.Timer(rtransition, self._disco_callback, args=[uid])
        timer.start()


class HueSensor:
    """A Hue motion sensor found by (partial, case-insensitive) name.

    ``refresh()`` is cheap to call often: readings come from the shared cache.
    """

    def __init__(self, bridge_ip, sensor_pattern):
        self.bridge_ip = bridge_ip
        self.b = get_bridge(bridge_ip)

        sensors = self.b.get_sensor_objects('name')
        sensor_pattern = sensor_pattern.lower()

        self.sensor = None
        self.name = ""
        for sname in sensors.keys():
            if sensor_pattern in sname.lower():
                print('{0}'.format(sname))
                self.sensor = sensors[sname]
                self.name = self.sensor.name
                break

        self.refresh()

    def refresh(self):
        self.sensor_state = get_sensor_state(self.bridge_ip, self.sensor.sensor_id)
        self.presence = self.sensor_state.get('presence', False)
        self.updated = self.sensor_state['lastupdated']

    def has_presence(self):
        return self.sensor_state.get('presence', False)
