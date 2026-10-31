"""Unit tests for halloween_common.hue against a fake bridge (no network)."""
import threading
import time

import pytest

pytest.importorskip("phue")
from halloween_common import hue  # noqa: E402

IP = "10.0.0.1"


def _light(name, color=True, dimmable=True, reachable=True):
    state = {"on": False, "reachable": reachable}
    if dimmable:
        state["bri"] = 100
    if color:
        state.update(hue=0, sat=0)
    return {"name": name, "type": "Extended color light" if color else "Dimmable light", "state": state}


def _motion(name, presence=False):
    return {"name": name, "type": "ZLLPresence", "state": {"presence": presence, "lastupdated": "t"}}


class FakeBridge:
    username = "user"

    def __init__(self):
        self.lights = {
            "1": _light("Halloween Spot"),
            "2": _light("Halloween Spot Greeter"),
            "3": _light("Halloween Tesla", color=False),
            "4": _light("Halloween Plug", color=False, dimmable=False),
        }
        self.sensors = {
            "10": _motion("Halloween sensor"),
            "11": {"name": "Halloween light level", "type": "ZLLLightLevel", "state": {"lightlevel": 1}},
            "12": _motion("Hue outdoor motion sensor 1"),
            "13": _motion("Hue outdoor motion sensor 12"),
        }
        self.gets = 0
        self.puts = []
        self.fail_lights = set()
        self.lock = threading.Lock()

    def request(self, method, address, data=None):
        resource = address.split("/")[3]
        if method == "GET":
            self.gets += 1
            return dict(self.lights if resource == "lights" else self.sensors)
        light_id = int(address.split("/")[4])
        if light_id in self.fail_lights:
            raise hue.PhueRegistrationException(6, "parameter not available")   # any PhueException
        with self.lock:
            self.puts.append((light_id, data))
        return [{"success": {}}]


@pytest.fixture
def bridge():
    return FakeBridge()


@pytest.fixture
def hub(bridge, monkeypatch):
    """A hub without background threads, registered as the shared hub for IP."""
    h = hue.HueHub(IP, bridge=bridge, start=False)
    monkeypatch.setitem(hue.HueHub._hubs, IP, h)
    return h


def _drain(h):
    """Send everything that is due, the way the writer thread would."""
    while True:
        with h._cv:
            lid, cmd, _ = h._next_due()
        if lid is None:
            return
        h.write_light(lid, cmd)


# ------------------------------------------------------------------ presets
def test_presets_are_read_only_and_unchanged_by_commands(hub):
    before = dict(hue.PRESETS["purple"])
    lights = hue.HueLights(IP, "Halloween Spot")
    lights.send_command("purple", bri=0, transitiontime=100)
    lights.lights_off(transitiontime=30)
    assert dict(hue.PRESETS["purple"]) == before
    assert hue.PRESETS["dimmed"]["transitiontime"] == 70
    with pytest.raises(TypeError):
        hue.PRESETS["purple"]["bri"] = 0


def test_every_preset_the_workers_use_exists():
    for name in ["red", "yellow", "pink", "purple", "green", "blue", "normal", "normal_lstr", "dimmed", "off"]:
        assert name in hue.PRESETS


# ------------------------------------------------------------------ light names
def test_light_names_match_exactly_ignoring_case(hub):
    lights = hue.HueLights(IP, "halloween SPOT")
    assert lights.lights_uids == [1]            # not "Halloween Spot Greeter"
    assert list(lights.lights) == ["Halloween Spot"]


def test_list_of_names_and_missing_names(hub):
    lights = hue.HueLights(IP, ["Halloween Spot Greeter", "Halloween Tesla", "No Such Light"])
    assert lights.lights_uids == [2, 3]
    assert lights.missing == ["No Such Light"]


def test_no_matching_light_raises(hub):
    with pytest.raises(LookupError):
        hue.HueLights(IP, "Halloween")


def test_resolve_picks_up_renamed_light(hub, bridge):
    lights = hue.HueLights(IP, ["Halloween Spot Greeter", "Porch"])
    assert lights.missing == ["Porch"]
    bridge.lights["5"] = _light("Porch")
    lights.resolve()
    assert lights.lights_uids == [2, 5] and lights.missing == []


def test_shared_light_warning(hub, capsys):
    hue.HueLights(IP, "Halloween Spot", owner="thriller")
    hue.HueLights(IP, "Halloween Spot", owner="tesla")
    assert "shares lights" in capsys.readouterr().out


# ------------------------------------------------------------------ write queue
def test_unsent_commands_for_a_light_are_merged(hub, bridge):
    lights = hue.HueLights(IP, "Halloween Spot")
    for bri in range(50):
        lights.send_command("red", bri=bri)
    lights.send_command("purple")
    _drain(hub)
    assert len(bridge.puts) == 1
    assert bridge.puts[0][1]["hue"] == hue.PRESETS["purple"]["hue"]
    assert bridge.puts[0][1]["bri"] == hue.PRESETS["purple"]["bri"]


def test_lights_off_fades_then_switches_off(hub, bridge):
    lights = hue.HueLights(IP, "Halloween Spot")
    lights.lights_off(transitiontime=1)        # 0.1 s
    _drain(hub)
    assert bridge.puts == [(1, {"transitiontime": 1, "on": True, "bri": 0})]
    time.sleep(0.15)
    _drain(hub)
    assert bridge.puts[-1] == (1, {"transitiontime": 0, "on": False})


def test_new_command_cancels_pending_delayed_off(hub, bridge):
    lights = hue.HueLights(IP, "Halloween Spot")
    lights.lights_off(transitiontime=1)
    _drain(hub)
    lights.send_command("red")                 # a new show starts before the "off" is due
    time.sleep(0.15)
    _drain(hub)
    assert all(cmd.get("on") is True for _, cmd in bridge.puts)


def test_unsupported_settings_are_dropped(hub, bridge):
    lights = hue.HueLights(IP, ["Halloween Spot", "Halloween Tesla", "Halloween Plug"])
    lights.send_command("red")
    _drain(hub)
    sent = dict(bridge.puts)
    assert "hue" in sent[1] and "sat" in sent[1]
    assert "hue" not in sent[3] and "sat" not in sent[3] and sent[3]["bri"] == 90
    assert sent[4] == {"on": True}             # a plug: no dimming, no transition


def test_writer_thread_sends_and_survives_errors(bridge):
    h = hue.HueHub(IP, bridge=bridge, start=True)
    bridge.fail_lights = {1}
    h.submit(1, {"on": True})
    h.submit(2, {"on": True})
    assert h.flush(timeout=2)
    h.submit(3, {"on": True})
    assert h.flush(timeout=2)
    assert [lid for lid, _ in bridge.puts] == [2, 3]


# ------------------------------------------------------------------ disco
def test_stop_disco_stops_all_disco_commands(hub, bridge):
    lights = hue.HueLights(IP, ["Halloween Spot", "Halloween Spot Greeter"])
    lights.start_disco()
    time.sleep(0.1)
    assert lights.disco_on
    lights.disco_on = False                    # the workers stop the disco this way
    lights.send_command("purple")
    time.sleep(0.2)
    with hub._cv:
        pending = {lid: dict(cmd) for lid, cmd in hub._pending.items()}
    assert not lights.disco_on
    assert all(cmd["hue"] == hue.PRESETS["purple"]["hue"] and cmd["bri"] == 136 for cmd in pending.values())


def test_disco_state_is_per_prop(hub):
    a = hue.HueLights(IP, "Halloween Spot", owner="a")
    b = hue.HueLights(IP, "Halloween Tesla", owner="b")
    a.start_disco()
    assert a.disco_on and not b.disco_on
    a.stop_disco()


# ------------------------------------------------------------------ sensors
def test_sensor_exact_name_then_substring_motion_only(hub):
    assert hue.HueSensor(IP, "hue outdoor motion sensor 1").sensor_id == 12   # not "... 12"
    assert hue.HueSensor(IP, "halloween").sensor_id == 10                     # not the light-level sensor
    with pytest.raises(LookupError):
        hue.HueSensor(IP, "light level")


def test_sensor_refresh_reads_shared_snapshot_without_io(hub, bridge):
    sensors = [hue.HueSensor(IP, "halloween"), hue.HueSensor(IP, "Hue outdoor motion sensor 1")]
    bridge.sensors["10"] = _motion("Halloween sensor", presence=True)
    gets = bridge.gets
    for s in sensors:
        s.refresh()
    assert bridge.gets == gets and not sensors[0].presence
    hub.poll_sensors()                         # one GET serves every sensor and prop
    for s in sensors:
        s.refresh()
    assert bridge.gets == gets + 1
    assert sensors[0].presence and not sensors[1].presence


def test_stale_snapshot_raises(hub, monkeypatch):
    s = hue.HueSensor(IP, "halloween")
    hub.sensors_at -= hue.STALE_AFTER + 1
    with pytest.raises(TimeoutError):
        s.refresh()


def test_connect_sensors_skips_only_bad_names(hub):
    sensors, errors = hue.connect_sensors(IP, ["halloween", "nope", "Hue outdoor motion sensor 1"])
    assert [s.sensor_id for s in sensors] == [10, 12]
    assert len(errors) == 1 and errors[0].startswith("nope")
