"""Arm / Trigger / Pause / Stop and Block on the coffin firmware (CPython, fakes only).

Firmware rules tested (props/coffin_jumper/firmware, 1.4.x):
- the prop always boots "stopped"; motion only triggers while "armed";
- one "playing" state for a running action, "paused" while held;
- block only inhibits the solenoid: sound and triggers continue, and blocking is
  a flag (``is_blocked``) next to the state, not a state of its own.
"""
import importlib.util
import json
import os
import sys
import time
import types

import pytest


@pytest.fixture
def prop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # MicroPython-only helpers the firmware uses
    if not hasattr(time, "ticks_ms"):
        monkeypatch.setattr(time, "ticks_ms", lambda: int(time.monotonic() * 1000), raising=False)
    if not hasattr(time, "ticks_diff"):
        monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    if not hasattr(time, "ticks_add"):
        monkeypatch.setattr(time, "ticks_add", lambda a, b: a + b, raising=False)
    if not hasattr(sys, "print_exception"):
        monkeypatch.setattr(sys, "print_exception", lambda exc, f=None: None, raising=False)
    path = os.path.join(os.path.dirname(__file__), "../../../../props/coffin_jumper/firmware/main.py")
    spec = importlib.util.spec_from_file_location("coffin_fw_states", os.path.abspath(path))
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    p = fw.CoffinProp()
    p.dfp_calls = []
    p.dfp = types.SimpleNamespace(**{n: (lambda n=n: (lambda *a: p.dfp_calls.append(n)))()
                                     for n in ("play_from_root", "play", "stop", "pause", "resume", "volume")})
    p.dfp.is_playing = lambda: True
    pin = types.SimpleNamespace(v=0)
    pin.value = lambda *a: (setattr(pin, "v", a[0]) if a else pin.v)
    p.solenoid_pin = pin
    p.render_display = lambda: None
    p.cmd = lambda action, **params: p._on_cmd(json.dumps({"action": action, "params": params}).encode())
    p.set_state(p._rest_state())
    return p


def test_boots_stopped_and_stop_disarms(prop):
    assert prop.state == "stopped" and not prop.is_armed
    assert prop.request_trigger(source="pir") is False      # motion ignored until armed
    prop.cmd("arm")
    assert prop.state == "armed" and prop.is_armed
    prop.cmd("stop")
    assert prop.state == "stopped" and not prop.is_armed
    assert prop.request_trigger(source="pir") is False


def test_manual_trigger_works_when_stopped(prop):
    assert prop.request_trigger(source="mqtt") is True
    assert prop.state == "playing" and prop.solenoid_pin.v == 1
    assert prop.dfp_calls[-1] == "play_from_root"
    assert prop.request_trigger(source="mqtt") is False      # not while one is running
    prop.cmd("stop_action")
    assert prop.state == "stopped"                            # back to where it was (disarmed)


def test_block_stops_only_the_solenoid(prop):
    prop.cmd("trigger")
    assert prop.state == "playing" and prop.solenoid_pin.v == 1
    prop.cmd("block")                                         # mid-action: solenoid off, show goes on
    assert prop.is_blocked and prop.state == "playing" and prop.solenoid_pin.v == 0
    prop.cmd("stop_action")
    assert prop.state == "stopped"

    prop.dfp_calls.clear()
    assert prop.request_trigger(source="mqtt") is True        # blocked: the sound still plays...
    assert prop.state == "playing" and prop.dfp_calls == ["play_from_root"]
    assert prop.solenoid_pin.v == 0                           # ...but the solenoid stays off
    prop.cmd("stop_action")
    prop.cmd("unblock")
    assert not prop.is_blocked and prop.state == "stopped"


def test_pause_holds_and_trigger_resumes(prop):
    prop.cmd("trigger")
    assert prop.state == "playing"
    prop.cmd("pause")
    assert prop.state == "paused" and prop.solenoid_pin.v == 0 and prop.dfp_calls[-1] == "pause"
    assert prop.request_trigger(source="pir") is False
    prop.cmd("trigger")
    assert prop.state == "playing" and prop.dfp_calls[-1] == "resume"


def test_arm_ends_a_paused_action(prop):
    prop.cmd("trigger")
    prop.cmd("pause")
    prop.cmd("arm")
    assert prop.state == "armed" and not prop.is_paused


def test_block_survives_a_reboot(prop):
    prop.cmd("block")
    again = type(prop)()                      # new instance = reboot, same flash (cwd)
    assert again.is_blocked is True and again._rest_state() == "stopped"   # a flag, not a state
    prop.cmd("unblock")
    assert type(prop)().is_blocked is False
