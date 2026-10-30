import threading
import time
import types

import pytest

pytest.importorskip("phue")
from halloween_common import hue  # noqa: E402


class FakeBridge:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def get_sensor(self, sensor_id):
        self.calls += 1
        time.sleep(0.05)
        if self.fail:
            raise OSError("GET request timed out")
        return {"state": {"presence": True, "lastupdated": "t"}}


@pytest.fixture
def bridge(monkeypatch):
    b = FakeBridge()
    monkeypatch.setitem(hue._bridges, "1.2.3.4", b)
    hue._sensor_cache.clear()
    return b


def test_concurrent_callers_share_one_fetch(bridge):
    threads = [threading.Thread(target=hue.get_sensor_state, args=("1.2.3.4", 7)) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert bridge.calls == 1


def test_refetch_after_interval(bridge):
    hue.get_sensor_state("1.2.3.4", 7, max_age=0.1)
    hue.get_sensor_state("1.2.3.4", 7, max_age=0.1)
    assert bridge.calls == 1
    time.sleep(0.15)
    hue.get_sensor_state("1.2.3.4", 7, max_age=0.1)
    assert bridge.calls == 2


def test_failure_is_shared_within_interval(bridge):
    bridge.fail = True
    for _ in range(3):
        with pytest.raises(OSError):
            hue.get_sensor_state("1.2.3.4", 7, max_age=1)
    assert bridge.calls == 1


def test_sensors_cached_independently(bridge):
    hue.get_sensor_state("1.2.3.4", 7)
    hue.get_sensor_state("1.2.3.4", 8)
    assert bridge.calls == 2
