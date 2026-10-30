"""Dungeon Nest worker.

Motion (Hue sensors) starts the dungeon soundscape on the dungeon Nest speaker.
Every motion reading while playing pushes the stop deadline PLAY_DURATION seconds
into the future, and the track is restarted if it ends before the deadline.
When the deadline passes the sound fades out, followed by a cooldown of
WAIT_BETWEEN_PLAYS seconds.

The prop is a state machine like tesla_hue_nest and thriller_hue_nest:
Stopped, Arming, Waiting (published as "armed"), Playing, Paused, FadeOut and
Cooldown. All state logic runs in one worker thread; commands are queued to it.
"""
from __future__ import annotations
import asyncio
import json
import queue
import threading
import time
from abc import ABC
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings

from halloween_common.media import resolve_media_url
from worker_host.base import BaseWorker

from .chromecast_API import ChromecastGroup
from .hue_API import HueSensor

PROP_ID = "dungeon_nest"
RESTART_GRACE_S = 15   # don't judge "track ended" until this long after (re)start
TRACK_CHECK_S = 2      # how often a running track is checked for having ended
FADE_OUT_S = 5         # time given to the speaker to fade out
LOOP_SLEEP_S = 0.2     # state machine tick


class ChromecastDevice(BaseModel):
    name: Optional[str] = "ChromeCast Device"
    IP: str
    volume: float = 0.5
    url: str
    repeat: bool = False


class ConfigModel(BaseSettings):
    MEDIA_BASE_URL: str = ""
    HUE_BRIDGE_IP: str = Field(...)
    MOTION_SENSORS: List[str] = []
    CHROMECAST_DUNGEON_GROUP: List[ChromecastDevice]
    PLAY_DURATION: int = 300
    WAIT_BETWEEN_PLAYS: int = 30
    AUTO_ARM: bool = False
    SENSOR_POLL_INTERVAL: float = 1.0
    TELEMETRY_INTERVAL: int = 10
    tick_interval: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def _resolve_media_urls(self):
        for dev in self.CHROMECAST_DUNGEON_GROUP:
            dev.url = resolve_media_url(dev.url, self.MEDIA_BASE_URL)
        return self


# Published state names shared by every prop: WaitingState (ready for motion) is reported as "armed".
_PUBLIC_STATE_NAMES = {"waiting": "armed"}


def _state_name(state_class) -> str:
    name = state_class.__name__.replace("State", "").lower()
    return _PUBLIC_STATE_NAMES.get(name, name)


# ============================ STATE CLASSES ============================
class State(ABC):
    """Base class of the state machine. `context` is the Worker."""

    def __init__(self, context):
        pass

    def on_enter(self, context):
        """Blocking entry work, run once in the worker thread before handle()."""
        return None

    def handle(self, context):
        """Runs on every tick."""
        pass

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")

    def on_motion(self, context):
        pass


class StoppedState(State):
    """Idle: motion is ignored. Play starts a show by hand; Arm makes the prop react to motion."""

    def __init__(self, context):
        print("Dungeon is stopped. Waiting for input...")
        self.needs_stop = context.current_state is not None   # nothing to stop on the initial state

    def on_enter(self, context):
        if self.needs_stop:
            context.stop_sound()

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'arm':
                context.set_state(ArmingState)
            case 'play':
                context.set_state(PlayingState)


class ArmingState(State):
    """Makes sure the speakers are connected, then goes on to WaitingState."""

    def __init__(self, context):
        print("Dungeon is arming...")

    def on_enter(self, context):
        if not context.have_speakers():
            context.connect_chromecast()
        context.set_state(WaitingState)

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        if action == 'stop':
            context.set_state(StoppedState)


class WaitingState(State):
    """Armed: motion starts the soundscape."""

    def __init__(self, context):
        print("Dungeon is waiting for motion...")

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'stop':
                context.set_state(StoppedState)
            case 'play':
                context.set_state(PlayingState)

    def on_motion(self, context):
        context.set_state(PlayingState)


class PlayingState(State):
    """Loops the soundscape. Motion pushes the stop deadline PLAY_DURATION seconds ahead."""

    def __init__(self, context):
        # Remember where to return after the show and its cooldown
        if isinstance(context.current_state, StoppedState):
            context.after_play_state = StoppedState
        elif isinstance(context.current_state, WaitingState):
            context.after_play_state = ArmingState
        now = time.monotonic()
        self.duration = context.config.PLAY_DURATION
        self.deadline = now + self.duration
        self.last_start = now
        self.last_check = now
        self._entered = False

    def on_enter(self, context):
        if self._entered:
            return
        self._entered = True
        try:
            started = context.load_and_play()
        except Exception as e:
            print(f"[{PROP_ID}] Failed to start sound: {e}")
            context.telemetry("error", f"Failed to start sound: {e}")
            started = False
        if not started:
            context.set_state(WaitingState if context.after_play_state is ArmingState else StoppedState)
            return
        now = time.monotonic()
        self.deadline = now + self.duration
        self.last_start = now
        context.record_activation()
        print(f"[{PROP_ID}] Starting soundscape (play #{context.activation_count}).")

    def handle(self, context):
        now = time.monotonic()
        if now >= self.deadline:
            print(f"[{PROP_ID}] Play time elapsed, fading out.")
            context.set_state(FadeOutState)
            return
        if now - self.last_start > RESTART_GRACE_S and now - self.last_check >= TRACK_CHECK_S and context.have_speakers():
            self.last_check = now
            try:
                if context.track_ended():
                    print(f"[{PROP_ID}] Track ended before deadline, restarting.")
                    context.load_and_play()
                    self.last_start = time.monotonic()
            except Exception as e:
                context.telemetry("error", f"Restart check failed: {e}")

    def on_motion(self, context):
        self.deadline = time.monotonic() + self.duration

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'stop':
                context.set_state(StoppedState)
            case 'pause':
                context.set_state(PausedState)
            case 'fade':
                context.set_state(FadeOutState)
            case 'play':
                self.on_motion(context)


class PausedState(State):
    """Freezes the show: the sound is paused and the deadline stops running. Motion is ignored."""

    def __init__(self, context):
        print("Pausing...")
        current = context.current_state
        self.resume_to = current if isinstance(current, PlayingState) else None
        self.paused_at = time.monotonic()

    def on_enter(self, context):
        context.pause_sound()

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'play' | 'resume':
                self._resume(context)
            case 'stop':
                context.set_state(StoppedState)
            case 'fade':
                context.set_state(FadeOutState)
            case 'arm':
                context.stop_sound()
                context.set_state(ArmingState)

    def _resume(self, context):
        if self.resume_to is None:
            context.set_state(PlayingState)
            return
        now = time.monotonic()
        self.resume_to.deadline += now - self.paused_at   # the deadline did not run while paused
        self.resume_to.last_start = now                   # give the speaker time to report PLAYING
        context.play_sound()
        context.restore_state(self.resume_to)


class FadeOutState(State):
    """Fades the sound out, then starts the cooldown."""

    def __init__(self, context):
        print("Fading out...")
        self.end_time = time.monotonic() + FADE_OUT_S

    def on_enter(self, context):
        context.stop_sound(fade=True)

    def handle(self, context):
        if time.monotonic() >= self.end_time:
            context.set_state(CooldownState)

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        if action == 'stop':
            context.set_state(StoppedState)


class CooldownState(State):
    """
    Ignores motion for WAIT_BETWEEN_PLAYS seconds after a show (a manual play skips the rest),
    then returns to the state before the show: ArmingState if it was armed,
    StoppedState if the show was started by hand. The remaining time is
    published as the timers/Cooldown telemetry.
    """

    def __init__(self, context):
        self.duration = context.config.WAIT_BETWEEN_PLAYS
        print(f"Entering cooldown state for {self.duration} seconds...")
        self.cooldown_end_time = time.monotonic() + self.duration
        self._shown = None

    def _publish_remaining(self, context, remaining):
        if remaining != self._shown:
            self._shown = remaining
            context.telemetry("timers/Cooldown", f"{remaining} s" if remaining else "-", retain=True)

    def handle(self, context):
        remaining = max(0, int(self.cooldown_end_time - time.monotonic() + 0.999))
        self._publish_remaining(context, remaining)
        if remaining == 0:
            print("Cooldown finished.")
            context.set_state(context.after_play_state)

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        if action == 'stop':
            self._publish_remaining(context, 0)
            context.set_state(StoppedState)
        elif action == 'play':
            # A manual play skips the rest of the cooldown and keeps the state to return to
            self._publish_remaining(context, 0)
            context.set_state(PlayingState)


# ================================ WORKER ================================
class Worker(BaseWorker):
    NAME = PROP_ID

    def __init__(self, prop_id, mqtt_client, config: ConfigModel | None = None):
        super().__init__(prop_id, mqtt_client, config)
        self.group: ChromecastGroup | None = None
        self.sensors: list[HueSensor] = []
        self.current_state: State | None = None
        self.command_queue: queue.Queue = queue.Queue()
        self.after_play_state = ArmingState   # where the cooldown returns to (see PlayingState)
        self._state_needs_enter = False
        self.last_telemetry = 0.0
        self.last_sensor_poll = 0.0
        self._lock = threading.RLock()  # serialises speaker access (worker thread vs connect/play-once)

    # ----------------------------- lifecycle -----------------------------
    async def start(self) -> None:
        await super().start()
        await asyncio.to_thread(self.connect_chromecast)
        await asyncio.to_thread(self._connect_sensors)
        self.set_state(ArmingState if self.config.AUTO_ARM else StoppedState)
        self.spawn(self._ticker())
        self.spawn(self._poll_loop())

    async def do_exit(self, arg) -> None:
        await asyncio.to_thread(self.stop_sound)
        await self.stop()

    # ----------------------------- state machine -------------------------
    def set_state(self, new_state_class):
        """Transition to a new state (its on_enter runs in the worker thread) and publish it."""
        if self.current_state and self.current_state.__class__ == new_state_class:
            return
        state_name = _state_name(new_state_class)
        print(f"Transitioning to state: {state_name}")
        self.current_state = new_state_class(self)
        self._state_needs_enter = True
        self.publish_state(state_name, retain=True)  # retained: dashboards opened later see it

    def restore_state(self, state_obj):
        """Return to an existing state instance without on_enter (resume after pause)."""
        self.current_state = state_obj
        self._state_needs_enter = False
        self.publish_state(_state_name(state_obj.__class__), retain=True)

    def _handle_command(self, action: str, arg) -> None:
        if action in ("nest_stop", "nest_fade_stop"):
            self._nest_stop(fade=action == "nest_fade_stop")
        elif self.current_state is not None:
            self.current_state.on_action(self, action, arg)

    def _nest_stop(self, fade: bool) -> None:
        """Nest-level stop: ends a running show (into the cooldown) but keeps the arm state."""
        if isinstance(self.current_state, (PlayingState, PausedState, FadeOutState)):
            self.stop_sound(fade=fade)
            self.set_state(CooldownState)
        else:
            self.stop_sound(fade=fade)

    # ----------------------------- connections ---------------------------
    def _urls(self):
        return [d.url for d in self.config.CHROMECAST_DUNGEON_GROUP]

    def connect_chromecast(self) -> None:
        with self._lock:
            try:
                devs = self.config.CHROMECAST_DUNGEON_GROUP
                self.telemetry("speakers/Status", "Connecting to Chromecast...")
                self.group = ChromecastGroup(
                    host_list=[d.IP for d in devs],
                    volumes=[d.volume for d in devs],
                    volume_limit_scale=self.volume_scale,
                )
                n = len(self.group.chromecasts)
                self.telemetry("speakers/Status", f"Connected to {n} Chromecast devices.")
                self.telemetry("speakers/SpeakersCount", n, retain=True)
                self.apply_volume_limit()
            except Exception as e:
                print(f"[{PROP_ID}] Chromecast init failed: {e}")
                self.telemetry("speakers/Status", f"Chromecast initialization failed: {e}")
                self.telemetry("speakers/SpeakersCount", 0, retain=True)
                self.group = None

    def _connect_sensors(self) -> None:
        try:
            self.telemetry("hue/state", f"Connecting to Hue motion sensors (bridge: {self.config.HUE_BRIDGE_IP})...")
            self.sensors = [HueSensor(self.config.HUE_BRIDGE_IP, n) for n in self.config.MOTION_SENSORS]
            self.telemetry("hue/state", f"Connected to {len(self.sensors)} Hue motion sensors.")
            self.telemetry("hue/SensorsCount", len(self.sensors), retain=True)
        except Exception as e:
            print(f"[{PROP_ID}] Hue sensor init failed: {e}")
            self.telemetry("hue/state", f"Hue motion sensor initialization failed: {e}")
            self.telemetry("hue/SensorsCount", 0, retain=True)
            self.sensors = []

    def have_speakers(self) -> bool:
        return self.group is not None and not self.group.is_empty()

    def volume_targets(self) -> list:
        return [self.group]

    def apply_volume_limit(self) -> None:
        with self._lock:  # serialise with the worker thread's speaker access
            super().apply_volume_limit()

    # ----------------------------- sound control -------------------------
    def load_and_play(self) -> bool:
        with self._lock:
            if not self.have_speakers():
                print(f"[{PROP_ID}] No speakers available.")
                return False
            self.group.load_media(url_list=self._urls(), autoplay=False)
            time.sleep(1)
            self.group.play()
            return True

    def track_ended(self) -> bool:
        with self._lock:
            self.group.refresh()
            return self.group.state() in ("STOPPED", "UNKNOWN")

    def _speaker_call(self, name: str, what: str) -> None:
        with self._lock:
            if not self.have_speakers():
                return
            try:
                getattr(self.group, name)()
            except Exception as e:
                print(f"[{PROP_ID}] Failed to {what} sound: {e}")
                self.telemetry("error", f"Failed to {what} sound: {e}")

    def play_sound(self) -> None:
        self._speaker_call("play", "resume")

    def pause_sound(self) -> None:
        self._speaker_call("pause", "pause")

    def stop_sound(self, fade: bool = False) -> None:
        self._speaker_call("fade_to_stop" if fade else "stop", "stop")

    def _play_once(self) -> None:
        """Nest-level play: plays the track once, no timer, no state change."""
        try:
            self.load_and_play()
        except Exception as e:
            self.telemetry("error", f"Failed to play: {e}")

    # ----------------------------- poll loop -----------------------------
    async def _ticker(self):
        i = 0
        while True:
            self.telemetry("tick", i)
            i += 1
            await asyncio.sleep(int(self.config.tick_interval))

    async def _poll_loop(self):
        while True:
            try:
                await asyncio.to_thread(self._tick)
            except Exception as e:
                self.telemetry("error", f"tick failed: {e}")
            await asyncio.sleep(LOOP_SLEEP_S)

    def _motion_detected(self) -> bool:
        detected = False
        for s in self.sensors:
            try:
                s.refresh()
            except Exception as e:
                self.telemetry("sensor/error", f"Sensor refresh failed: {e}")
                continue
            if getattr(s, "presence", False):
                detected = True
        return detected

    def _tick(self) -> None:
        while True:
            try:
                action, arg = self.command_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_command(action, arg)
            except Exception as e:
                self.telemetry("error", f"Command handler error: {e}")

        if self._state_needs_enter and self.current_state is not None:
            self._state_needs_enter = False   # cleared first: on_enter may itself change state
            try:
                self.current_state.on_enter(self)
            except Exception as e:
                self.telemetry("error", f"State on_enter error: {e}")

        if self.current_state is not None:
            try:
                self.current_state.handle(self)
            except Exception as e:
                self.telemetry("error", f"State handle error: {e}")

        now = time.monotonic()
        if now - self.last_sensor_poll >= self.config.SENSOR_POLL_INTERVAL:
            self.last_sensor_poll = now
            if self.sensors and self._motion_detected() and self.current_state is not None:
                self.current_state.on_motion(self)

        if now - self.last_telemetry > self.config.TELEMETRY_INTERVAL:
            self.last_telemetry = now
            self._send_telemetry(now)

    def _remaining_seconds(self, now: float) -> int:
        state = self.current_state
        if isinstance(state, PlayingState):
            return max(0, int(state.deadline - now))
        if isinstance(state, PausedState) and state.resume_to is not None:
            return max(0, int(state.resume_to.deadline - state.paused_at))
        return 0

    def _send_telemetry(self, now: float) -> None:
        self.telemetry("playback/RemainingSeconds", self._remaining_seconds(now), retain=True)
        self.telemetry("playback/PlayCount", self.activation_count, retain=True)
        try:
            speaker = self.group.state() if self.have_speakers() else "No Chromecast devices"
        except Exception:
            speaker = "unknown"
        self.telemetry("speakers/State", speaker, retain=True)

    # ----------------------------- commands ------------------------------
    async def do_arm(self, arg) -> None:
        self.command_queue.put(("arm", arg))

    async def do_play(self, arg) -> None:
        """Prop-level play: start the looping show, or resume when paused."""
        self.command_queue.put(("play", arg))

    async def do_pause(self, arg) -> None:
        self.command_queue.put(("pause", arg))

    async def do_stop(self, arg) -> None:
        self.command_queue.put(("stop", arg))

    async def do_chromecast(self, arg) -> None:
        if isinstance(arg, str):
            try:
                parsed = json.loads(arg)
                if isinstance(parsed, dict):
                    arg = parsed
            except (json.JSONDecodeError, ValueError):
                pass

        if isinstance(arg, str):
            action = arg.lower()
            if action == "connect":
                await asyncio.to_thread(self.connect_chromecast)
                await asyncio.to_thread(self._connect_sensors)
                return
            if action == "play":
                # Nest-level play: the track once; the prop-level Play runs the timed loop
                await asyncio.to_thread(self._play_once)
            elif action == "stop":
                self.command_queue.put(("nest_stop", None))
            elif action == "fade_to_stop":
                self.command_queue.put(("nest_fade_stop", None))
            elif action in ("volume_up", "volume_down") and self.have_speakers():
                fn = self.group.volume_up if action == "volume_up" else self.group.volume_down
                await asyncio.to_thread(fn)
            elif action == "status" and self.have_speakers():
                for key, line in self.group.describe().items():
                    self.telemetry(f"speakers/{key}", line)
            else:
                print(f"[{PROP_ID}] Unknown or unavailable chromecast action: {action}")
        elif isinstance(arg, dict) and "volume" in arg:
            if self.have_speakers():
                await asyncio.to_thread(self.group.set_volume, float(arg["volume"]))
        else:
            print(f"[{PROP_ID}] Invalid chromecast argument: {arg}")

        if self.have_speakers():
            await asyncio.to_thread(self.group.refresh)
            self.telemetry("speakers/State", self.group.state(), retain=True)
