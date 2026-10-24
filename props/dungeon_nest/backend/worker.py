"""Dungeon Nest worker.

Motion (Hue sensors) starts the dungeon soundscape on the dungeon Nest speaker.
Every motion reading while playing pushes the stop deadline PLAY_DURATION seconds
into the future. When the deadline passes, the sound fades out. If the track ends
before the deadline it is restarted.
"""
from __future__ import annotations
import asyncio
import json
import threading
import time
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings

from halloween_common.media import resolve_media_url
from worker_host.base import BaseWorker

from .chromecast_API import ChromecastGroup
from .hue_API import HueSensor

PROP_ID = "dungeon_nest"
RESTART_GRACE_S = 15  # don't judge "track ended" until this long after (re)start


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
    AUTO_ARM: bool = True
    SENSOR_POLL_INTERVAL: float = 1.0
    TELEMETRY_INTERVAL: int = 10
    tick_interval: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def _resolve_media_urls(self):
        for dev in self.CHROMECAST_DUNGEON_GROUP:
            dev.url = resolve_media_url(dev.url, self.MEDIA_BASE_URL)
        return self


class Worker(BaseWorker):
    NAME = PROP_ID

    def __init__(self, prop_id, mqtt_client, config: ConfigModel | None = None):
        super().__init__(prop_id, mqtt_client, config)
        self.group: ChromecastGroup | None = None
        self.sensors: list[HueSensor] = []
        self.state = "stopped"          # stopped | armed | playing | paused
        self.paused_remaining = 0.0
        self.deadline = 0.0             # monotonic time at which playing stops
        self.last_start = 0.0
        self.play_count = 0
        self.last_telemetry = 0.0
        self._lock = threading.RLock()  # serialises speaker access (poll thread vs commands)

    # ----------------------------- lifecycle -----------------------------
    async def start(self) -> None:
        await super().start()
        await asyncio.to_thread(self._connect_chromecast)
        await asyncio.to_thread(self._connect_sensors)
        self._set_state("armed" if self.config.AUTO_ARM else "stopped")
        self.spawn(self._ticker())
        self.spawn(self._poll_loop())

    async def do_exit(self, arg) -> None:
        await asyncio.to_thread(self._stop_sound)
        await self.stop()

    def _set_state(self, state: str) -> None:
        self.state = state
        self.publish_state(state, retain=True)

    # ----------------------------- connections ---------------------------
    def _urls(self):
        return [d.url for d in self.config.CHROMECAST_DUNGEON_GROUP]

    def _connect_chromecast(self) -> None:
        with self._lock:
            try:
                devs = self.config.CHROMECAST_DUNGEON_GROUP
                self.telemetry("speakers/Status", "Connecting to Chromecast...")
                self.group = ChromecastGroup(
                    host_list=[d.IP for d in devs],
                    volumes=[d.volume for d in devs],
                )
                n = len(self.group.chromecasts)
                self.telemetry("speakers/Status", f"Connected to {n} Chromecast devices.")
                self.telemetry("speakers/SpeakersCount", n, retain=True)
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

    def _have_speakers(self) -> bool:
        return self.group is not None and not self.group.is_empty()

    # ----------------------------- sound control -------------------------
    def _load_and_play(self) -> None:
        with self._lock:
            if not self._have_speakers():
                print(f"[{PROP_ID}] No speakers available.")
                return
            self.group.load_media(url_list=self._urls(), autoplay=False)
            time.sleep(1)
            self.group.play()
            self.last_start = time.monotonic()

    def _start_sound(self) -> None:
        """Start the looping action (or just extend it when already playing)."""
        now = time.monotonic()
        self.deadline = now + self.config.PLAY_DURATION
        if self.state == "playing":
            return
        self.play_count += 1
        print(f"[{PROP_ID}] Starting soundscape (play #{self.play_count}).")
        self._set_state("playing")
        try:
            self._load_and_play()
        except Exception as e:
            print(f"[{PROP_ID}] Failed to start sound: {e}")
            self.telemetry("error", f"Failed to start sound: {e}")

    def _pause_sound(self) -> None:
        if self.state != "playing":
            return
        self.paused_remaining = max(0.0, self.deadline - time.monotonic())
        self._set_state("paused")
        with self._lock:
            if self._have_speakers():
                try:
                    self.group.pause()
                except Exception as e:
                    self.telemetry("error", f"Failed to pause sound: {e}")

    def _resume_sound(self) -> None:
        self.deadline = time.monotonic() + self.paused_remaining
        self.last_start = time.monotonic()
        self._set_state("playing")
        with self._lock:
            if self._have_speakers():
                try:
                    self.group.play()
                except Exception as e:
                    self.telemetry("error", f"Failed to resume sound: {e}")

    def _stop_sound(self, fade: bool = False, new_state: str = "stopped") -> None:
        self._set_state(new_state)
        with self._lock:
            if not self._have_speakers():
                return
            try:
                if fade:
                    self.group.fade_to_stop()
                else:
                    self.group.stop()
            except Exception as e:
                print(f"[{PROP_ID}] Failed to stop sound: {e}")
                self.telemetry("error", f"Failed to stop sound: {e}")

    def _speaker_stop(self, fade: bool = False) -> None:
        """Nest-level stop: ends a running action but keeps the arm state."""
        keep = self.state if self.state in ("stopped", "armed") else "armed"
        self._stop_sound(fade, keep)

    def _play_once(self) -> None:
        """Nest-level play: plays the track once, no timer, no state change."""
        try:
            self._load_and_play()
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
            await asyncio.sleep(self.config.SENSOR_POLL_INTERVAL)

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
        now = time.monotonic()
        if self.state in ("armed", "playing") and self._motion_detected():
            self._start_sound()

        if self.state == "playing":
            if now >= self.deadline:
                print(f"[{PROP_ID}] Play time elapsed, fading out.")
                self._stop_sound(fade=True, new_state="armed")
            elif now - self.last_start > RESTART_GRACE_S and self._have_speakers():
                try:
                    with self._lock:
                        self.group.refresh()
                        ended = self.group.state() in ("STOPPED", "UNKNOWN")
                    if ended:
                        print(f"[{PROP_ID}] Track ended before deadline, restarting.")
                        self._load_and_play()
                except Exception as e:
                    self.telemetry("error", f"Restart check failed: {e}")

        if now - self.last_telemetry > self.config.TELEMETRY_INTERVAL:
            self.last_telemetry = now
            self._send_telemetry(now)

    def _send_telemetry(self, now: float) -> None:
        if self.state == "playing":
            remaining = max(0, int(self.deadline - now))
        elif self.state == "paused":
            remaining = int(self.paused_remaining)
        else:
            remaining = 0
        self.telemetry("playback/RemainingSeconds", remaining, retain=True)
        self.telemetry("playback/PlayCount", self.play_count, retain=True)
        try:
            speaker = self.group.state() if self._have_speakers() else "No Chromecast devices"
        except Exception:
            speaker = "unknown"
        self.telemetry("speakers/State", speaker, retain=True)

    # ----------------------------- commands ------------------------------
    async def do_arm(self, arg) -> None:
        if self.state in ("stopped", "paused"):
            if self.state == "paused":
                await asyncio.to_thread(self._stop_sound, False, "armed")
            else:
                self._set_state("armed")

    async def do_play(self, arg) -> None:
        """Prop-level play: start the looping action, or resume when paused."""
        if self.state == "paused":
            await asyncio.to_thread(self._resume_sound)
        else:
            await asyncio.to_thread(self._start_sound)

    async def do_pause(self, arg) -> None:
        await asyncio.to_thread(self._pause_sound)

    async def do_stop(self, arg) -> None:
        await asyncio.to_thread(self._stop_sound)

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
                await asyncio.to_thread(self._connect_chromecast)
                await asyncio.to_thread(self._connect_sensors)
                return
            if action == "play":
                # Nest-level play: the track once; the prop-level Play runs the timed loop
                await asyncio.to_thread(self._play_once)
            elif action == "stop":
                await asyncio.to_thread(self._speaker_stop)
            elif action == "fade_to_stop":
                await asyncio.to_thread(self._speaker_stop, True)
            elif action in ("volume_up", "volume_down") and self._have_speakers():
                fn = self.group.volume_up if action == "volume_up" else self.group.volume_down
                await asyncio.to_thread(fn)
            else:
                print(f"[{PROP_ID}] Unknown or unavailable chromecast action: {action}")
        elif isinstance(arg, dict) and "volume" in arg:
            if self._have_speakers():
                await asyncio.to_thread(self.group.set_volume, float(arg["volume"]))
        else:
            print(f"[{PROP_ID}] Invalid chromecast argument: {arg}")

        if self._have_speakers():
            await asyncio.to_thread(self.group.refresh)
            self.telemetry("speakers/State", self.group.state(), retain=True)
