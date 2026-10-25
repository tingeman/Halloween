"""Pause/resume for the Tesla and Thriller state machines (fakes only, no devices).

Run like test_volume_limit.py (inside the worker image), with /repo on PYTHONPATH.
"""
import importlib
import time
import types

import pytest


class FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))

    def subscribe(self, topic, qos=0):
        pass


class FakeGroup:
    def __init__(self):
        self.calls = []

    def is_empty(self):
        return False

    def __getattr__(self, name):              # pause, play, stop, refresh, ...
        return lambda *a, **k: self.calls.append(name)

    def any_playing(self):
        return True


class FakeCar:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):              # open_trunk, close_trunk, ...
        return lambda *a, **k: self.calls.append(name)


@pytest.mark.parametrize("prop,group_attr", [
    ("tesla_hue_nest", "cc_tesla_group"),
    ("thriller_hue_nest", "cc_thriller_group"),
])
def test_pause_freezes_music_and_resume_continues_same_show(prop, group_attr):
    mod = importlib.import_module(f"props.{prop}.backend.worker")
    w = mod.Worker(prop, FakeMqtt(), None)
    w.config = types.SimpleNamespace(WAIT_BETWEEN_PLAYS=0, MOTION_TIMEOUT=60, USE_TESLA=True)
    group, car = FakeGroup(), FakeCar()
    setattr(w, group_attr, group)
    w.tesla_car = car
    w.motion_sensors = [object()]

    playing = mod.PlayingState(w)
    playing._entered = True                   # show already started
    w.current_state = playing
    playing.last_motion_time = time.monotonic() - 20

    playing.on_action(w, "pause", None)
    paused = w.current_state
    assert isinstance(paused, mod.PausedState)
    paused.on_enter(w)
    assert group.calls == ["pause"]

    paused.on_motion(w)                       # motion is ignored while paused
    paused.handle(w)
    assert w.current_state is paused

    paused.paused_at -= 30                    # pretend we were paused for 30 s
    before = playing.last_motion_time
    paused.on_action(w, "play", None)
    assert w.current_state is playing         # same show, no restart
    assert w._state_needs_enter is False
    assert playing.last_motion_time == pytest.approx(before + 30, abs=0.5)
    assert group.calls == ["pause", "play"]
    assert car.calls == []                    # trunk untouched by pause/resume
    assert (f"halloween/{prop}/state", "playing") in w.mqtt.published

    playing.handle(w)                         # inside the resume grace: no fade-out
    assert w.current_state is playing


def test_stop_from_pause_goes_to_stopped():
    mod = importlib.import_module("props.tesla_hue_nest.backend.worker")
    w = mod.Worker("tesla_hue_nest", FakeMqtt(), None)
    w.config = types.SimpleNamespace(WAIT_BETWEEN_PLAYS=0, MOTION_TIMEOUT=60, USE_TESLA=True)
    w.cc_tesla_group = FakeGroup()
    w.current_state = mod.PlayingState(w)
    w.current_state.on_action(w, "pause", None)
    w.current_state.on_action(w, "stop", None)
    assert isinstance(w.current_state, mod.StoppedState)
