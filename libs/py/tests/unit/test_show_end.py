"""When a Tesla/Thriller show ends (fakes only, no devices).

Run like test_pause_state.py, with the repo, server/ and libs/py on PYTHONPATH.
"""
import importlib
import time
import types

import pytest

from test_pause_state import FakeMqtt


class Recorder:
    """Records calls in one shared log so their order can be checked."""
    def __init__(self, name, log, playing=True):
        self.name, self.log, self.playing = name, log, playing

    def is_empty(self):
        return False

    def any_playing(self):
        return self.playing

    def __getattr__(self, attr):
        return lambda *a, **k: self.log.append((self.name, attr, k))


PROPS = [("tesla_hue_nest", "cc_tesla_group"), ("thriller_hue_nest", "cc_thriller_group")]


def _worker(prop, group_attr, log, playing):
    mod = importlib.import_module(f"props.{prop}.backend.worker")
    w = mod.Worker(prop, FakeMqtt(), None)
    w.config = types.SimpleNamespace(WAIT_BETWEEN_PLAYS=0, MOTION_TIMEOUT=60, USE_TESLA=True,
                                     HUE_PLAYING_SCENE="purple", MAX_SHOW_DURATION=0)
    setattr(w, group_attr, Recorder("music", log, playing))
    w.tesla_car = Recorder("car", log)
    w.hue_manager = mod.HueManager(Recorder("lights", log))
    w.motion_sensors = [object()]
    return mod, w


@pytest.mark.parametrize("prop,group_attr", PROPS)
def test_show_ends_when_song_ends_even_with_constant_motion(prop, group_attr):
    mod, w = _worker(prop, group_attr, [], playing=False)
    playing = mod.PlayingState(w)
    playing._entered = True
    w.current_state = playing
    playing.music_started_at = time.monotonic() - 30   # song started 30 s ago and has ended
    playing.on_motion(w)                               # people are still in front of the sensor
    playing.handle(w)                                  # first "not playing": not yet confirmed
    assert w.current_state is playing
    playing.not_playing_since -= mod.SONG_END_CONFIRM_S
    playing.handle(w)
    assert isinstance(w.current_state, mod.FadeOutState)


@pytest.mark.parametrize("prop,group_attr", PROPS)
def test_brief_not_playing_after_reconnect_keeps_show(prop, group_attr):
    log = []
    mod, w = _worker(prop, group_attr, log, playing=False)   # no media session just after reconnect
    playing = mod.PlayingState(w)
    playing._entered = True
    w.current_state = playing
    playing.music_started_at = time.monotonic() - 30
    playing.on_motion(w)
    playing.handle(w)
    getattr(w, group_attr).playing = True                    # 2 s later: playing again
    playing.handle(w)
    assert w.current_state is playing and playing.not_playing_since is None


@pytest.mark.parametrize("prop,group_attr", PROPS)
def test_no_fade_in_first_seconds_after_play(prop, group_attr):
    mod, w = _worker(prop, group_attr, [], playing=False)   # speaker not reporting PLAYING yet
    playing = mod.PlayingState(w)
    playing._entered = True
    w.current_state = playing
    playing.handle(w)
    assert w.current_state is playing


def test_tesla_fade_out_stops_music_and_lights_before_trunk_using_cached_state():
    log = []
    mod, w = _worker("tesla_hue_nest", "cc_tesla_group", log, playing=True)
    mod.FadeOutState(w)
    order = [(who, what) for who, what, _ in log if (who, what) in
             {("music", "fade_to_stop"), ("lights", "lights_off"), ("car", "close_trunk")}]
    assert order == [("music", "fade_to_stop"), ("lights", "lights_off"), ("car", "close_trunk")]
    assert [k for who, what, k in log if what == "close_trunk"] == [{"trunk_check": False}]


def test_tesla_stopped_uses_cached_trunk_state():
    log = []
    mod, w = _worker("tesla_hue_nest", "cc_tesla_group", log, playing=True)
    stopped = mod.StoppedState(w)
    stopped.handle(w)
    stopped.handle(w)                                   # runs once
    assert [k for who, what, k in log if what == "close_trunk"] == [{"trunk_check": False}]


class Unreachable(Recorder):
    """A speaker group whose connection is lost: every status update fails."""
    def refresh(self):
        raise ConnectionError("Chromecast 10.0.0.1:8009 is connecting...")

    def fade_to_stop(self):
        raise ConnectionError("Chromecast 10.0.0.1:8009 is connecting...")


def _playing(mod, w, started_ago):
    playing = mod.PlayingState(w)
    playing._entered = True
    w.current_state = playing
    playing.music_started_at = playing.show_started_at = time.monotonic() - started_ago
    return playing


@pytest.mark.parametrize("prop,group_attr", PROPS)
def test_unreachable_speaker_ends_show_after_grace(prop, group_attr):
    log = []
    mod, w = _worker(prop, group_attr, log, playing=True)
    setattr(w, group_attr, Unreachable("music", log))
    playing = _playing(mod, w, started_ago=30)
    playing.on_motion(w)                      # constant motion: the motion timeout never helps

    playing.handle(w)                         # connection just lost: keep the show going
    assert w.current_state is playing
    errors = [p for t, p in w.mqtt.published if t.endswith("/telemetry/error")]
    playing.handle(w)
    assert len([p for t, p in w.mqtt.published if t.endswith("/telemetry/error")]) == len(errors) == 1

    playing.speaker_error_since -= mod.SPEAKER_LOST_S + 1
    playing.handle(w)                         # still unreachable after the grace: end the show
    assert isinstance(w.current_state, mod.FadeOutState)
    # the failing fade-out of the music did not stop the lights (and trunk) from being handled
    assert ("lights", "lights_off") in [(who, what) for who, what, _ in log]


@pytest.mark.parametrize("prop,group_attr", PROPS)
def test_speaker_back_within_grace_keeps_show(prop, group_attr):
    log = []
    mod, w = _worker(prop, group_attr, log, playing=True)
    good = getattr(w, group_attr)
    setattr(w, group_attr, Unreachable("music", log))
    playing = _playing(mod, w, started_ago=30)
    playing.on_motion(w)
    playing.handle(w)
    setattr(w, group_attr, good)              # reconnected, still playing
    playing.handle(w)
    assert w.current_state is playing and playing.speaker_error_since is None


@pytest.mark.parametrize("prop,group_attr", PROPS)
def test_max_show_duration_ends_show(prop, group_attr):
    mod, w = _worker(prop, group_attr, [], playing=True)
    w.config.MAX_SHOW_DURATION = 100
    playing = _playing(mod, w, started_ago=50)
    playing.on_motion(w)
    playing.handle(w)
    assert w.current_state is playing
    playing.show_started_at -= 60              # now 110 s into the show, song still "playing"
    playing.handle(w)
    assert isinstance(w.current_state, mod.FadeOutState)


@pytest.mark.parametrize("prop,group_attr", PROPS)
def test_unreachable_speaker_does_not_block_arming_or_waiting(prop, group_attr):
    log = []
    mod, w = _worker(prop, group_attr, log, playing=True)
    w.config.CHROMECAST_TESLA_GROUP = w.config.CHROMECAST_THRILLER_GROUP = []
    w.play_after_arm = False

    class FailingLoad(Unreachable):
        def load_media_bg(self, *a, **k):
            raise ConnectionError("is connecting...")
        def load_media(self, *a, **k):
            raise ConnectionError("is connecting...")

    setattr(w, group_attr, FailingLoad("music", log))
    arming = mod.ArmingState(w)
    w.current_state = arming
    arming.on_enter(w)
    assert isinstance(w.current_state, mod.WaitingState)

    waiting = w.current_state
    waiting.last_check_time -= waiting.check_interval + 1
    waiting.handle(w)                          # the periodic check fails: re-arm instead of raising
    assert isinstance(w.current_state, mod.ArmingState)


def test_dungeon_cooldown_zero_rearms_at_once():
    mod = importlib.import_module("props.dungeon_nest.backend.worker")
    w = mod.Worker("dungeon_nest", FakeMqtt(), None)
    w.config = types.SimpleNamespace(WAIT_BETWEEN_PLAYS=0)
    w.after_play_state = mod.ArmingState
    cooldown = mod.CooldownState(w)
    w.current_state = cooldown
    cooldown.handle(w)
    assert isinstance(w.current_state, mod.ArmingState)


def test_group_stop_quits_app_when_stop_fails():
    pytest.importorskip("pychromecast")
    from halloween_common.chromecast import ChromecastGroup

    class Cast:
        def __init__(self, host, stop_works):
            self.cast_info = types.SimpleNamespace(host=host)
            self.calls = []
            def stop():
                self.calls.append("stop")
                if not stop_works:
                    raise RuntimeError("Failed to execute stop.")
            self.media_controller = types.SimpleNamespace(stop=stop)
        def quit_app(self):
            self.calls.append("quit_app")

    good, no_session = Cast("10.0.0.1", True), Cast("10.0.0.2", False)
    g = ChromecastGroup.__new__(ChromecastGroup)   # skip network discovery
    g.chromecasts = [good, no_session]
    g.stop()
    assert good.calls == ["stop"]
    assert no_session.calls == ["stop", "quit_app"]


def test_dungeon_restarts_track_only_after_two_ended_readings():
    mod = importlib.import_module("props.dungeon_nest.backend.worker")
    w = mod.Worker("dungeon_nest", FakeMqtt(), None)
    w.config = types.SimpleNamespace(PLAY_DURATION=300, WAIT_BETWEEN_PLAYS=0)
    readings, restarts = [True, False, True, True], []
    w.have_speakers = lambda: True
    w.track_ended = lambda: readings.pop(0)
    w.load_and_play = lambda: restarts.append(1) or True
    playing = mod.PlayingState(w)
    w.current_state = playing
    for _ in range(4):
        playing.last_start = playing.last_check = time.monotonic() - 60   # due for a check
        playing.handle(w)
    assert len(restarts) == 1    # the lone "ended" reading was ignored, the pair restarted once
