"""Unit tests for the global volume limit (no devices, no broker).

Run inside the worker image, which has pychromecast installed:
    docker run --rm -v <repo>:/repo -w /repo --entrypoint sh halloween/worker_host:latest \
      -c "pip install -q pytest && PYTHONPATH=/repo/libs/py:/repo/server pytest -q libs/py/tests/unit"
"""
import asyncio
import importlib.util
import json
import os
import types

import pytest

from halloween_common import volume_limit


# --- payload parsing / cap ---------------------------------------------------
@pytest.mark.parametrize("payload,expected", [
    (b'{"scale": 0.3}', 0.3), ("0.5", 0.5), ("30%", 0.3), (b"", 1.0),
    (None, 1.0), ("garbage", 1.0), ('{"scale": 7}', 1.0), ("-1", 0.0),
])
def test_parse(payload, expected):
    assert volume_limit.parse(payload) == pytest.approx(expected)


def test_cap_and_label():
    assert volume_limit.cap(0.5, 1.0) == 1.0          # no limit -> original behaviour
    assert volume_limit.cap(0.5, 0.3) == pytest.approx(0.15)
    assert volume_limit.clamp(0.1, 0.5, 0.3) == 0.1   # below the cap is kept
    assert volume_limit.label(1.0) == "off" and volume_limit.label(0.3) == "30%"
    assert volume_limit.parse(volume_limit.encode(0.25)) == 0.25


# --- ChromecastGroup ---------------------------------------------------------
class FakeCast:
    def __init__(self, host, level):
        self.cast_info = types.SimpleNamespace(host=host, friendly_name=f"Nest {host}")
        self.status = types.SimpleNamespace(volume_level=level)
        self.media_controller = types.SimpleNamespace(
            status=types.SimpleNamespace(player_state="IDLE"), update_status=lambda: None, play=lambda: None)
        self.sets = []

    def set_volume(self, v):
        self.sets.append(round(v, 3))
        self.status.volume_level = v


def make_group(defaults, scale=1.0):
    from halloween_common.chromecast import ChromecastGroup
    g = ChromecastGroup.__new__(ChromecastGroup)   # skip network discovery
    g.volume_limit = scale
    g.default_volumes = list(defaults)
    g.target_volumes = list(defaults)
    g.chromecasts = [FakeCast(f"10.0.0.{i}", d) for i, d in enumerate(defaults)]
    g._last_urls = {}                              # set by __init__, which __new__ skips
    return g


def test_group_limit_caps_relative_to_each_default():
    g = make_group([0.6, 0.4])
    g.set_volume_limit(0.5)
    assert [c.status.volume_level for c in g.chromecasts] == pytest.approx([0.3, 0.2])
    g.set_volume(0.9)                                # request above cap
    assert [c.status.volume_level for c in g.chromecasts] == pytest.approx([0.3, 0.2])
    g.set_volume(0.1)                                # below cap is honoured
    assert [c.status.volume_level for c in g.chromecasts] == pytest.approx([0.1, 0.1])


def test_group_volume_up_stops_at_cap_and_raising_limit_restores_target():
    g = make_group([0.5])
    g.set_volume_limit(0.4)                          # cap 0.2
    for _ in range(5):
        g.volume_up()
    assert g.chromecasts[0].status.volume_level == pytest.approx(0.2)
    g.set_volume(None)                               # back to default target (0.5)
    g.set_volume_limit(1.0)                          # lifting the limit restores it
    assert g.chromecasts[0].status.volume_level == pytest.approx(0.5)


def test_group_enforces_limit_on_refresh_and_play():
    g = make_group([0.5], scale=0.5)
    g.chromecasts[0].status.volume_level = 0.9       # changed outside the worker
    g.refresh()
    assert g.chromecasts[0].status.volume_level == pytest.approx(0.25)
    g.chromecasts[0].status.volume_level = 0.8
    g.play()
    assert g.chromecasts[0].status.volume_level == pytest.approx(0.25)
    assert "cap 0.25" in next(iter(g.describe().values()))


# --- BaseWorker broadcast handling ------------------------------------------
class FakeMqtt:
    def __init__(self):
        self.published, self.subscribed = [], []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def add_message_handler(self, h):
        pass


def test_base_worker_applies_broadcast_limit_to_targets():
    from worker_host.base import BaseWorker, MqttMessage

    applied = []

    class W(BaseWorker):
        def volume_targets(self):
            return [types.SimpleNamespace(set_volume_limit=applied.append), None]

    mqtt = FakeMqtt()
    w = W("p", mqtt)
    asyncio.run(w.start())
    assert volume_limit.TOPIC in mqtt.subscribed
    asyncio.run(w.on_message(MqttMessage(volume_limit.TOPIC, b'{"scale":0.3}', 1, True)))
    assert applied == [0.3] and w.volume_scale == 0.3
    assert ("halloween/p/telemetry/volume/Limit", "30%", True) in mqtt.published


# --- ESP32 coffin firmware (runs on CPython thanks to its import fallbacks) ---
def _load_firmware():
    path = os.path.join(os.path.dirname(__file__), "../../../../props/coffin_jumper/firmware/main.py")
    spec = importlib.util.spec_from_file_location("coffin_fw", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_firmware_volume_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                      # volume_limit.json goes here
    fw = _load_firmware()
    prop = fw.CoffinProp()
    sent = []
    prop.dfp = types.SimpleNamespace(volume=sent.append)
    assert prop.volume_cap() == fw.MAX_VOLUME        # no limit by default
    prop._on_volume_limit(b'{"scale": 0.3}')
    assert sent[-1] == 6                             # 30% of DEFAULT_VOLUME 20
    prop.set_volume(25)
    assert sent[-1] == 6 and prop.volume == 25
    prop.set_volume(4)
    assert sent[-1] == 4
    assert json.loads((tmp_path / "volume_limit.json").read_text())["scale"] == 0.3
    assert fw.CoffinProp().volume_limit == 0.3       # survives a "reboot"


def test_base_worker_block_flag_is_retained_and_restored():
    from worker_host.base import BaseWorker, MqttMessage

    mqtt = FakeMqtt()
    w = BaseWorker("p", mqtt)
    asyncio.run(w.start())
    assert "halloween/p/state/blocked" in mqtt.subscribed
    asyncio.run(w.on_message(MqttMessage("halloween/p/cmd", b"block", 1, False)))
    assert w.blocked and ("halloween/p/state/blocked", "1", True) in mqtt.published
    w2 = BaseWorker("p", FakeMqtt())          # restart: retained flag comes back
    asyncio.run(w2.on_message(MqttMessage("halloween/p/state/blocked", b"1", 1, True)))
    assert w2.blocked


def test_repeated_volume_limit_is_not_reapplied():
    from worker_host.base import BaseWorker, MqttMessage

    applied = []

    class W(BaseWorker):
        def volume_targets(self):
            return [types.SimpleNamespace(set_volume_limit=applied.append)]

    w = W("p", FakeMqtt())
    for _ in range(3):
        asyncio.run(w.on_message(MqttMessage(volume_limit.TOPIC, b'{"scale":0.25}', 1, True)))
    assert applied == [0.25]
