"""Arm / Trigger / Pause / Stop and Block on the coffin firmware (CPython, fakes only)."""
import importlib.util
import json
import os
import time
import types

import pytest


@pytest.fixture
def prop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    if not hasattr(time, "ticks_ms"):          # MicroPython time helpers
        monkeypatch.setattr(time, "ticks_ms", lambda: int(time.monotonic() * 1000), raising=False)
        monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
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


def test_stop_disarms_and_motion_is_ignored(prop):
    assert prop.state == "armed"
    prop.cmd("stop")
    assert prop.state == "stopped" and not prop.is_armed
    assert prop.request_trigger(source="pir") is False
    prop.cmd("arm")
    assert prop.state == "armed"


def test_manual_trigger_works_when_stopped_but_not_when_blocked(prop):
    prop.cmd("stop")
    assert prop.request_trigger(source="mqtt") is True
    assert prop.state == "action" and prop.solenoid_pin.v == 1
    prop.cmd("block")
    assert prop.state == "blocked" and prop.solenoid_pin.v == 0
    prop._last_action_start_ms = None
    assert prop.request_trigger(source="mqtt") is False
    prop.cmd("play_music")
    assert "play_from_root" not in prop.dfp_calls[-1:]
    prop.cmd("unblock")
    assert prop.state == "stopped"            # back to where it was (disarmed)


def test_pause_holds_and_trigger_resumes(prop):
    prop.cmd("trigger")
    assert prop.state == "action"
    prop.cmd("pause")
    assert prop.state == "paused" and prop.solenoid_pin.v == 0 and prop.dfp_calls[-1] == "pause"
    assert prop.request_trigger(source="pir") is False
    prop.cmd("trigger")
    assert prop.state == "action" and prop.dfp_calls[-1] == "resume"


def test_arm_ends_a_paused_action(prop):
    prop.cmd("trigger")
    prop.cmd("pause")
    prop.cmd("arm")
    assert prop.state == "armed" and not prop.is_paused


def test_block_survives_a_reboot(prop):
    prop.cmd("block")
    again = type(prop)()                      # new instance = reboot, same flash (cwd)
    assert again.is_blocked is True and again._rest_state() == "blocked"
    prop.cmd("unblock")
    assert type(prop)().is_blocked is False
