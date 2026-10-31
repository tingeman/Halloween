# server/worker_host/builtin_workers/heartbeat.py
from __future__ import annotations
import asyncio
import json
import queue
import time
from abc import ABC, abstractmethod 
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings
from typing import List

from halloween_common.media import resolve_media_url
from worker_host.base import BaseWorker

# Import third-party libraries
from .chromecast_API import *
from .hue_API import *

PROP_ID = "thriller_hue_nest"


# ============================ CONFIG MODEL ============================
class ChromecastDevice(BaseModel):
    name: Optional[str] = 'ChromeCast Device'
    IP: str
    volume: float = 0.5
    url: str
    repeat: bool = False

class ConfigModel(BaseSettings):
    # Per-prop value, set in the prop's config.yml; bare media file names resolve against it
    MEDIA_BASE_URL: str = ""
    HUE_BRIDGE_IP: str = Field(..., env="HUE_BRIDGE_IP")
    MOTION_SENSORS: List[str]
    CHROMECAST_THRILLER_GROUP: List[ChromecastDevice]
    USE_TESLA: bool = False
    WAIT_BETWEEN_PLAYS: int = 480
    MOTION_TIMEOUT: int = 180
    HUE_SPOT_LIGHT: str = "Halloween Spot"
    HUE_PLAYING_SCENE: str = "disco"  # Options: 'disco' or color name ('red', 'yellow', 'pink', 'purple', 'normal', etc.)
    TELEMETRY_INTERVAL: int = 10  # How often to send telemetry updates (seconds)
    tick_interval: int = 5

    # Use BaseSettings' model_config to control env file behavior. Docker
    # already supplies environment variables via docker-compose env_file,
    # so explicit env_file is optional. Keep env_file here for local dev.
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # ignore extra keys from the raw YAML to be lenient
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _resolve_media_urls(self):
        for dev in self.CHROMECAST_THRILLER_GROUP:
            dev.url = resolve_media_url(dev.url, self.MEDIA_BASE_URL)
        return self


# Published state names shared by every prop: WaitingState (ready for motion)
# is reported as "armed", like the Dungeon and Coffin props.
_PUBLIC_STATE_NAMES = {"waiting": "armed"}


def _state_name(state_class) -> str:
    name = state_class.__name__.replace("State", "").lower()
    return _PUBLIC_STATE_NAMES.get(name, name)


# ============================ STATE CLASSES ============================
class State(ABC):
    """
    Abstract base class for a state in the state machine.

    This class defines the interface for all concrete states. It ensures that
    any state implementation provides a `handle` method. It also provides
    default, overridable handlers for actions and motion events.
    """
    def __init__(self, context):
        """Initializes the state."""
        pass 

    def on_enter(self, context):
        """Optional entry hook called once (in the worker sync thread)

        Implement blocking initialization here if the state requires it.
        This will be invoked by the worker before the first call to
        `handle()` for the new state, ensuring initialization completes on
        the synchronous thread.
        """
        return None

    @abstractmethod
    def handle(self, context):
        """
        Executes the primary logic for the state on each tick of the poll loop.

        This method must be implemented by all concrete state subclasses.

        Args:
            context: The main Worker instance, providing access to shared resources.
        """
        pass

    def on_action(self, context, action, arg):
        """
        Handles a command received from the MQTT command queue.

        This method can be overridden by subclasses to define state-specific
        command handling.

        Args:
            context: The main Worker instance.
            action: The command action string (e.g., 'arm', 'stop').
            arg: An optional argument string for the command.
        """
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")

    def on_motion(self, context):
        """
        Handles a motion detection event.

        This method can be overridden by subclasses to define state-specific
        responses to motion.

        Args:
            context: The main Worker instance.
        """
        # Which sensors triggered the event can be accessed from 
        # context.motion_listener.triggered_sensors
        pass
        #print(f"{self.__class__.__name__}::Motion detected.")


class StoppedState(State):
    """
    Represents the idle state of the system.

    In this state, all prop activities are ceased (lights off, trunk closed,
    media stopped). The worker waits for an 'arm' command to begin the sequence.
    """
    def __init__(self, context):
        """Initializes the stopped state, ensuring a clean shutdown of all components."""
        print("System is stopped. Waiting for input...")
        self.stopped = False

    def handle(self, context):
        """
        Ensures all prop components are in their off/idle state.

        This method runs once upon entering the state and then does nothing
        further, preventing repeated "off" commands.
        """
        if not self.stopped:
            context.hue_manager.lights.disco_on = False
            context.hue_manager.lights.send_command('purple', transitiontime=1)

            if context.cc_thriller_group is not None and not context.cc_thriller_group.is_empty():
                context.cc_thriller_group.stop()

            context.hue_manager.lights_off(transitiontime=1)
            self.stopped = True

    def on_action(self, context, action, arg):
        """Handles user commands while in the stopped state."""
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'disco':
                context.hue_manager.toggle_disco()
            case 'arm':
                context.play_after_arm = False
                context.set_state(ArmingState)
            case 'play':
                # Media is only loaded while arming: arm, then play at once, and return here after the cooldown
                context.play_after_arm = True
                context.after_play_state = StoppedState
                context.set_state(ArmingState)
            case 'volume_up':
                if not context.cc_thriller_group.is_empty():
                    context.cc_thriller_group.volume_up()
            case 'volume_down':
                if not context.cc_thriller_group.is_empty():
                    context.cc_thriller_group.volume_down()


class ArmingState(State):
    """
    Prepares the system for operation.

    This state loads the necessary media onto the Chromecast devices and then
    immediately transitions to the WaitingState.
    """
    def __init__(self, context):
        """Initializes the arming state."""
        print("System is arming...")

    def on_enter(self, context):
        """
        Loads media onto the Chromecast group and transitions to WaitingState.
        """
        # Load thriller sound track if possible
        if context.cc_thriller_group.is_empty():
            print('No Thriller chromecast found, cannot play media.')
        else:
            url_list = filter_url_list(context.config.CHROMECAST_THRILLER_GROUP)
            # Prefer background loader which guarantees paused-on-ready
            if hasattr(context.cc_thriller_group, 'load_media_bg'):
                try:
                    task = context.cc_thriller_group.load_media_bg(url_list=url_list, autoplay=False)
                    # wait synchronously in the sync-thread for readiness
                    task.join()
                    print('Tesla media loaded (background) and ready...')
                except Exception as e:
                    print(f'Background load failed: {e}; falling back to blocking load')
                    context.cc_thriller_group.load_media(url_list=url_list)
            else:
                context.cc_thriller_group.load_media(url_list=url_list)
                print('Tesla media loaded and ready...')

        if context.play_after_arm:
            context.play_after_arm = False
            context.set_state(PlayingState)
        else:
            context.set_state(WaitingState)

    def handle(self, context):
        """
        Runs every poll tick. No actions needed in this state.
        """
        pass

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'wait':
                context.set_state(WaitingState)

class WaitingState(State):
    """
    Waits for motion to be detected.

    This state is the main "armed and ready" mode. It periodically checks
    the status of the Chromecast devices to ensure they are ready. If motion
    is detected, it transitions to the PlayingState.
    """
    def __init__(self, context):
        """Initializes the waiting state."""
        print("System is waiting for motion...")
        self.check_interval = 60  # Check interval in seconds
        self.last_check_time = time.monotonic()

    def handle(self, context):
        """
        Periodically checks Chromecast status to prevent timeouts or errors.
        """
        current_time = time.monotonic()
        if current_time - self.last_check_time > self.check_interval:
            context.cc_thriller_group.refresh()
            if context.cc_thriller_group.any_unknown():
                print(f"{self.__class__.__name__}::Unknown chromecast state detected. Rearming...")
                context.set_state(ArmingState)
                return
            self.last_check_time = current_time
            print(f"{self.__class__.__name__}::Checked chromecast state, all good!")

    def on_action(self, context, action, arg):
        """Handles user commands while waiting for motion."""
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'stop':
                context.set_state(StoppedState)
            case 'play':
                context.set_state(PlayingState)
    
    def on_motion(self, context):
        """Transitions to the PlayingState upon motion detection."""
        context.set_state(PlayingState)


class CooldownState(State):
    """
    Enforces a delay between prop activations.

    Entered after a show has finished (FadeOutState). Motion is ignored for WAIT_BETWEEN_PLAYS seconds (a manual play skips the rest), then the prop returns to the
    state it was in before the show: ArmingState if it was armed,
    StoppedState if the show was started manually without being armed.
    The remaining time is published as the timers/Cooldown telemetry.
    """
    def __init__(self, context):
        """Initializes the cooldown timer."""
        self.duration = context.config.WAIT_BETWEEN_PLAYS
        print(f"Entering cooldown state for {self.duration} seconds...")
        self.cooldown_end_time = time.monotonic() + self.duration
        self._shown = None

    def _publish_remaining(self, context, remaining):
        if remaining != self._shown:
            self._shown = remaining
            context.telemetry("timers/Cooldown", f"{remaining} s" if remaining else "-", retain=True)

    def handle(self, context):
        """
        Counts down and transitions back when the cooldown period has elapsed.
        """
        remaining = max(0, int(self.cooldown_end_time - time.monotonic() + 0.999))
        self._publish_remaining(context, remaining)
        if remaining == 0:
            print("Cooldown finished.")
            context.set_state(context.after_play_state)

    def on_action(self, context, action, arg):
        """Allows the prop to be stopped manually during cooldown."""
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        if action == 'stop':
            self._publish_remaining(context, 0)
            context.set_state(StoppedState)
        elif action == 'play':
            # A manual play skips the rest of the cooldown; the media is reloaded via ArmingState
            self._publish_remaining(context, 0)
            context.play_after_arm = True
            context.set_state(ArmingState)


class PlayingState(State):
    """
    Executes the main prop sequence.

    This state plays media, controls lights, and opens the Tesla trunk. It
    includes a timeout to transition to FadeOutState if motion is no longer
    detected.
    """
    def __init__(self, context):
        """Initialize lightweight state variables for the playing sequence.

        Heavy I/O (device commands) are performed in on_enter(), which will be
        executed in the worker's synchronous thread before handle() is called.
        """
        self.timeout_interval = context.config.MOTION_TIMEOUT  # Timeout interval in seconds
        self.last_motion_time = time.monotonic()
        # Remember where to return after the show and its cooldown
        if isinstance(context.current_state, StoppedState):
            context.after_play_state = StoppedState
        elif isinstance(context.current_state, WaitingState):
            context.after_play_state = ArmingState
        # Flag to guard repeated on_enter execution if needed
        self._entered = False

    def on_enter(self, context):
        """Perform blocking initialization (safe in sync thread):

        - start media playback
        - send hue commands
        """
        if self._entered:
            return
        self._entered = True

        print("System is playing...")
        context.record_activation()
        # Hue scene control (safe to call even if null manager)
        try:
            if context.config.HUE_PLAYING_SCENE.lower() == 'disco':
                if not context.hue_manager.lights.disco_on:
                    context.hue_manager.lights.start_disco()
                    print("Starting disco mode...")
            else:
                # Send static color command
                context.hue_manager.send_command(context.config.HUE_PLAYING_SCENE)
                print(f"Setting lights to {context.config.HUE_PLAYING_SCENE}...")
        except Exception as e:
            print(f"Failed to set hue scene: {e}")

        if not context.cc_thriller_group.is_empty():
            try:
                context.cc_thriller_group.play()
            except Exception:
                pass


    def handle(self, context):
        """
        Monitors for inactivity timeout and media completion.

        If no motion is detected for the timeout duration, or if the media
        finishes playing, it transitions to the FadeOutState.
        """
        current_time = time.monotonic()
        if current_time < getattr(self, 'resume_grace_until', 0):
            return  # just resumed from pause; give the speakers time to report PLAYING

        if len(context.motion_sensors) > 0:
            if current_time - self.last_motion_time > self.timeout_interval:
                # Trigger timeout if enough time has passed since the last motion detection
                print("Timeout triggered due to inactivity.")
                context.set_state(FadeOutState)
                return 
        
            print(f"{self.__class__.__name__}::Time since last motion: {current_time - self.last_motion_time:.1f} s")
        
        if time.monotonic() - self.last_motion_time > 5:   # WAIT minimum 5 seconds before checking playing state
            if not context.cc_thriller_group.is_empty():
                context.cc_thriller_group.refresh()
                if not context.cc_thriller_group.any_playing():
                    print("Soundtrack stopped playing...")
                    context.set_state(FadeOutState)
                    return

    def on_action(self, context, action, arg):
        """Handles user commands during the playing sequence."""
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'stop':
                context.set_state(StoppedState)
            case 'pause':
                context.set_state(PausedState)
            case 'fade':
                context.set_state(FadeOutState)
            case 'disco':
                context.hue_manager.toggle_disco()
            case 'volume_up':
                if not context.cc_thriller_group.is_empty():
                    context.cc_thriller_group.volume_up()
            case 'volume_down':
                if not context.cc_thriller_group.is_empty():
                    context.cc_thriller_group.volume_down()

    def on_motion(self, context):
        """Resets the inactivity timer upon motion detection."""
        self.last_motion_time = time.monotonic()


class PausedState(State):
    """
    Freezes the show: the music is paused and the motion timeout stops
    counting, while the lights are left as they are.

    play resumes the same show where it was, without re-running its start
    (no new trunk or Hue commands, no new play count, no cooldown check).
    Motion is ignored while paused.
    """
    def __init__(self, context):
        print("Pausing...")
        current = context.current_state
        self.resume_to = current if isinstance(current, PlayingState) else None
        self.paused_at = time.monotonic()

    def on_enter(self, context):
        if not context.cc_thriller_group.is_empty():
            try:
                context.cc_thriller_group.pause()
            except Exception as e:
                print(f"Failed to pause media: {e}")

    def handle(self, context):
        pass

    def on_action(self, context, action, arg):
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'play' | 'resume':
                self._resume(context)
            case 'stop':
                context.set_state(StoppedState)
            case 'fade':
                context.set_state(FadeOutState)
            case 'disco':
                context.hue_manager.toggle_disco()
            case 'volume_up':
                if not context.cc_thriller_group.is_empty():
                    context.cc_thriller_group.volume_up()
            case 'volume_down':
                if not context.cc_thriller_group.is_empty():
                    context.cc_thriller_group.volume_down()

    def _resume(self, context):
        if self.resume_to is None:
            context.set_state(PlayingState)
            return
        paused_for = time.monotonic() - self.paused_at
        self.resume_to.last_motion_time += paused_for   # the timeout did not run while paused
        self.resume_to.resume_grace_until = time.monotonic() + 5
        if not context.cc_thriller_group.is_empty():
            try:
                context.cc_thriller_group.play()
            except Exception as e:
                print(f"Failed to resume media: {e}")
        context.restore_state(self.resume_to)


class FadeOutState(State):
    """
    Gracefully ends the prop sequence.

    This state fades out the media volume, closes the Tesla trunk, and fades
    the lights to off, then enters the cooldown before the prop is re-armed.
    """
    def __init__(self, context):
        """Initializes the fade-out sequence."""
        print("Fading out...")
        self.transition_time = 10 # seconds for lights to fade
        self.next_state_time = time.monotonic() + self.transition_time

        context.hue_manager.lights.disco_on = False
        context.hue_manager.lights.send_command('purple', transitiontime=self.transition_time * 10) # phue uses 1/10s

        if context.cc_thriller_group is not None and not context.cc_thriller_group.is_empty():
            context.cc_thriller_group.fade_to_stop()

        context.hue_manager.lights_off(transitiontime=self.transition_time)

    def handle(self, context):
        """
        Waits for the fade-out transition to complete, then starts the cooldown.
        """
        # This state's work is done in __init__. We just wait here.
        if time.monotonic() >= self.next_state_time:
            context.set_state(CooldownState)

    def on_action(self, context, action, arg):
        """Allows the prop to be stopped manually during fade-out."""
        print(f"{self.__class__.__name__}::action passed: {action}, arg: {arg}")
        match action:
            case 'stop':
                context.set_state(StoppedState)


# ============================ HUE MANAGER ============================
class HueManager:
    """
    A wrapper for the HueLights object to provide a simplified API.

    This class abstracts the details of sending commands to the Hue bridge,
    managing the disco mode, and turning lights off.
    """
    def __init__(self, lights):
        """
        Initializes the HueManager.

        Args:
            lights: An initialized HueLights object.
        """
        self.lights = lights

    def toggle_disco(self):
        """Toggles the disco mode on or off."""
        if self.lights.disco_on:
            self.lights.disco_on = False
            self.lights.send_command('red')
            print('Stopping disco...')
        else:
            self.lights.start_disco()
            print('Starting disco...')

    def send_command(self, command):
        """Sends a raw command to the Hue lights."""
        self.lights.send_command(command)

    def lights_off(self, transitiontime=0):
        """
        Turns the lights off.

        Args:
            transitiontime: The time in seconds for the lights to fade out.
        """
        self.lights.lights_off(transitiontime=transitiontime)


# ======= Resiliency helpers: dummy subsystems used when hardware is unavailable ======
class _DummyLights:
    def __init__(self):
        self.disco_on = False
    def send_command(self, *a, **k):
        return None
    def lights_off(self, *a, **k):
        return None
    def start_disco(self):
        self.disco_on = True


class _NullHueManager:
    def __init__(self):
        self.lights = _DummyLights()
    def toggle_disco(self):
        return None
    def send_command(self, *a, **k):
        return None
    def lights_off(self, *a, **k):
        return None


class _DummyChromecastGroup:
    def __init__(self):
        self.chromecasts = []
    def is_empty(self):
        return True
    def load_media(self, *a, **k):
        return None
    def load_media_bg(self, *a, **k):
        """Return a dummy background-task compatible object for environments
        where ChromeCast devices are not available.

        The returned object exposes cancel(), join(timeout=None) and
        as_future(loop=None) so callers can rely on the same API as the
        real MediaLoadTask.
        """
        class _DummyTask:
            def cancel(self):
                return None
            def join(self, timeout=None):
                return []
            def as_future(self, loop=None):
                if loop is None:
                    loop = asyncio.get_event_loop()
                fut = loop.create_future()
                loop.call_soon_threadsafe(fut.set_result, [])
                return fut

        return _DummyTask()
    def stop(self):
        return None
    def play(self):
        return None
    def refresh(self):
        return None
    def any_unknown(self):
        return False
    def any_playing(self):
        return False
    def volume_up(self):
        return None
    def volume_down(self):
        return None
    def fade_to_stop(self):
        return None
    def state(self):
        return "No Chromecast devices"


# ============================ UTILS ============================
# Utility Functions
def _get_field_from_item(item, field):
    """Return field value from dict-like or object-like item."""
    try:
        # dict-like
        if isinstance(item, dict):
            return item.get(field)
        # pydantic BaseModel or simple object
        return getattr(item, field, None)
    except Exception:
        return None

def filter_ip_list(list_of_items):
    """Extract IP addresses from list of dicts or objects (ChromecastDevice)."""
    return [_get_field_from_item(it, 'IP') for it in list_of_items]

def filter_url_list(list_of_items):
    """Extract URLs from list of dicts or objects."""
    return [_get_field_from_item(it, 'url') for it in list_of_items]

def filter_volume_list(list_of_items):
    """Extract volume values from list of dicts or objects."""
    return [_get_field_from_item(it, 'volume') for it in list_of_items]

def filter_repeat_list(list_of_items):
    """Extract repeat flags from list of dicts or objects."""
    return [_get_field_from_item(it, 'repeat') for it in list_of_items]

def connect_hue_lights(bridge_ip, light_name):
    """
    Connects to the Hue bridge and returns a HueLights object.

    Args:
        bridge_ip: The IP address of the Hue bridge.
        light_name: The name of the light or group to control.

    Returns:
        An initialized HueLights object.
    """
    print(f"connecting...")    
    lights = HueLights(bridge_ip, light_name)
    print(f"sending command off...")
    lights.send_command('off')
    print(f"command off sent")
    return lights

# ============================ WORKER CLASS ============================

class Worker(BaseWorker):
    """
    Orchestrates the Hue/Nest prop.

    This worker manages a state machine that controls interactions between Phillips Hue 
    sensors, Philips Hue lights, and Google Chromecast devices. It listens
    for MQTT commands to start, stop, and control the prop sequence, and it
    responds to motion sensor events.
    """

    NAME = "thriller_hue_nest"

    def __init__(self, prop_id, mqtt_client, config: ConfigModel | None = None):
        """
        Initializes the worker, loading configuration and setting up state variables.

        This constructor is non-blocking. All I/O-bound initializations for
        external services are deferred to the `start` method.

        Args:
            prop_id: The unique identifier for this prop.
            mqtt_client: The MQTT client instance for communication.
            config: A Pydantic model of the configuration options for the worker,
                    loaded from the prop's config.yml file.
        """
        super().__init__(prop_id, mqtt_client, config)
        
        self.current_state = None # Will hold the state object instance
        self.command_queue = queue.Queue() # Will hold incoming commands

        # State Machine Variables
        self.after_play_state = ArmingState   # where the cooldown returns to (see PlayingState)
        self.play_after_arm = False           # manual play from Stopped: ArmingState goes on to PlayingState

        self.hue_lights = []
        self.hue_manager = _NullHueManager()  # Default to null manager
        self.motion_sensors = []
        self.cc_thriller_group = _DummyChromecastGroup()  # Default to dummy group
        # When set_state is called we set this flag; the sync thread will
        # call the state's on_enter() exactly once before calling handle().
        self._state_needs_enter = False
        # Timer for periodic telemetry collection
        self.last_telemetry_time = time.monotonic()
        # Queue for telemetry work that must run in the sync thread
        # (chromecast.state(), tesla queries, etc. may block and must not
        # run on the asyncio event loop). Use a Queue so both sync and async
        # callers can enqueue work safely.
        self._telemetry_queue = queue.Queue()
        # Flag flipped while _run_sync_tasks is executing to allow
        # set_state() to perform telemetry immediately when called from
        # the sync thread.
        self._in_sync_thread = False


    # --- NEW: State Management Method ---
    def set_state(self, new_state_class):
        """
        Transitions the worker to a new state and notifies via MQTT.

        This method is the central point for all state changes. It creates an
        instance of the new state class, updates the worker's current state,
        and publishes the new state name to the status topic.

        Args:
            new_state_class: The class of the state to transition to (e.g., `StoppedState`).
        """
        if self.current_state and self.current_state.__class__ == new_state_class:
            return # Already in this state

        state_name = _state_name(new_state_class)
        print(f"Transitioning to state: {state_name}")
        
        # Create an instance of the new state, passing self as the context
        self.current_state = new_state_class(self)
        # Mark that the state's on_enter() needs to run in the sync thread
        self._state_needs_enter = True

        self.publish_state(state_name, retain=True)  # retained: dashboards opened later see it

    def restore_state(self, state_obj):
        """Return to an existing state instance without on_enter (resume after pause)."""
        self.current_state = state_obj
        self._state_needs_enter = False
        self.publish_state(_state_name(state_obj.__class__), retain=True)


    # --- NEW: Async Command Bridge ---
    async def do_command(self, action: str | None, arg: str | None):
        """
        Receives a command from MQTT and puts it on the thread-safe queue.

        This method serves as the bridge between the asynchronous MQTT message
        handler and the synchronous state machine. It queues commands to be
        processed by the main poll loop.

        Args:
            action: The command action string (e.g., 'arm', 'stop').
            arg: An optional argument string for the command.
        """
        if action:
            print(f"Queueing command: {action}({arg})")
            self.command_queue.put((action, arg))
            print(f"Queue size is now {self.command_queue.qsize()}")
            print(f"Command queue contents: {list(self.command_queue.queue)}")

    async def _init_integrations(self):
        """
        Initializes connections to all external services (Hue, Tesla, Chromecast).

        This method runs all blocking I/O calls in a separate thread to avoid
        freezing the main asyncio event loop during startup.

        Returns:
            True if all initializations succeed, False otherwise.
        """
        
        try:
            # Use to_thread for all blocking initializations
            await asyncio.to_thread(self._connect_blocking_services)
            
            # Set initial state only after connections are successful
            self.set_state(StoppedState)

        except Exception as e:
            print(f"FATAL: ERROR initializing integrations: {e}")
            # Publish an error state so the dashboard can reflect initialization failure
            self.publish_state("error", qos=1, retain=True)
            # Do not start the poll loop if init fails
            return False
        return True

    def _connect_blocking_services(self):
        """
        Contains the blocking I/O calls for service initialization.

        This method is executed in a separate thread via `asyncio.to_thread`
        to prevent blocking the main worker loop.
        """
        # Run the three sub-connectors. Each handles its own errors and
        # leaves a sane fallback (dummy) in place so the worker can continue.
        self._connect_chromecast()
        self._connect_hue()

    def _connect_chromecast(self):
        """Blocking init for Chromecast devices. Safe to call from a thread.

        Sets `self.cc_thriller_group` to a real ChromecastGroup on success or a
        `_DummyChromecastGroup` on failure.
        """
        try:
            print('Connecting to sound system...')
            self.telemetry("speakers/Status", "Connecting to Chromecast...")
            print(f"Chromecast config: {self.config.CHROMECAST_THRILLER_GROUP}")
            self.cc_thriller_group = ChromecastGroup(
                host_list=filter_ip_list(self.config.CHROMECAST_THRILLER_GROUP),
                volumes=filter_volume_list(self.config.CHROMECAST_THRILLER_GROUP),
                volume_limit_scale=self.volume_scale,
            )
            self.telemetry("speakers/Status", f"Connected to {len(self.cc_thriller_group.chromecasts)} Chromecast devices.")
            print(f"Connected to {len(self.cc_thriller_group.chromecasts)} Chromecast devices.")
            print(f"Chromecast group: {self.cc_thriller_group}")
            self.telemetry("speakers/SpeakersCount", len(self.cc_thriller_group.chromecasts), retain=True)
            self.apply_volume_limit()
        except Exception as e:
            print(f"[thriller_hue_nest] Chromecast init failed: {e}. Continuing without sound system.")
            self.telemetry("speakers/Status", f"Chromecast initialization failed: {e}")
            self.telemetry("speakers/SpeakersCount", 0, retain=True)
            self.cc_thriller_group = _DummyChromecastGroup()


    def _connect_hue(self):
        """Blocking init for the Philips Hue bridge and motion sensors.

        On success sets `self.hue_lights`, `self.hue_manager`, and
        `self.motion_sensors`. On failure sets `self.hue_manager` to a
        `_NullHueManager` and `self.motion_sensors` to an empty list.
        """
        exceptions_raised = {'lights': None, 'sensors': None}
        
        try:
            print('Connecting to Hue Lights...')
            self.telemetry("hue/state", f"Connecting to Hue lights... (bridge: {self.config.HUE_BRIDGE_IP})")
            self.hue_lights = connect_hue_lights(self.config.HUE_BRIDGE_IP, self.config.HUE_SPOT_LIGHT)
            self.hue_manager = HueManager(self.hue_lights)
            print(f"Connected to Hue lights: {self.hue_lights}")
            print(f"Connected to Hue lights: {self.hue_lights.lights}")
            self.telemetry("hue/state", f"Connected to Hue lights: {self.config.HUE_SPOT_LIGHT}")
            self.telemetry("hue/LightsCount", len(self.hue_lights), retain=True)
        except Exception as e:
            print(f"[thriller_hue_nest] Hue lights init failed: {e}. Continuing without lights.")
            # print the traceback for debugging
            import traceback
            traceback.print_exc()
        
            self.telemetry("hue/state", f"Hue lights initialization failed: {e}")
            self.telemetry("hue/LightsCount", 0, retain=True)
            self.hue_manager = _NullHueManager()
            self.hue_lights = []
            exceptions_raised['lights'] = e

        try:
            print('Connecting to Hue Motion Sensors...')
            self.telemetry("hue/state", "Connecting to Hue motion sensors...")
            self.motion_sensors = [HueSensor(self.config.HUE_BRIDGE_IP, name) for name in self.config.MOTION_SENSORS]
            self.telemetry("hue/state", f"Connected to {len(self.motion_sensors)} Hue motion sensors.")
            self.telemetry("hue/SensorsCount", len(self.motion_sensors), retain=True)
        except Exception as e:
            print(f"[thriller_hue_nest] Hue motion sensor init failed: {e}. Continuing without motion sensors.")
            self.telemetry("hue/state", f"Hue motion sensor initialization failed: {e}")
            self.telemetry("hue/SensorsCount", 0, retain=True)
            self.motion_sensors = []
            exceptions_raised['sensors'] = e

        if exceptions_raised['lights'] is None or exceptions_raised['sensors'] is None:
            out_str = f"Connected; {len(self.motion_sensors)} motion sensors; {len(self.hue_lights)} lights."
        else:
            out_str = "Failed to connect to Hue lights and motion sensors. Exceptions: " + ", ".join(
                [f"{k}: {v}" for k, v in exceptions_raised.items() if v is not None]
            )
        print(out_str)
        self.telemetry("hue/Status", out_str, retain=True)

    async def start(self) -> None:
        """
        Starts the worker's main operations.

        This method is called by the worker_host. It initializes all external
        integrations and, upon success, spawns the background tasks for the
        ticker and the main polling loop.
        """
        await super().start()

        # --- Initialize integrations first ---
        init_success = await self._init_integrations()
        if not init_success:
            print("FATAL: Initialization failed.")
            return # Stop if initialization failed

        # Spawn ticker method
        self.spawn(self._ticker())

        # Spawn the async poll loop as a managed task
        self.spawn(self._poll_loop())

    async def do_exit(self, arg: str | None) -> None:
        """
        Gracefully stops the worker and cleans up resources.

        This method is triggered by an 'exit' command via MQTT. It runs the
        synchronous cleanup logic and then stops all background tasks.

        Args:
            arg: An optional argument string (not used).
        """
        print("Received exit command. Shutting down worker...")
        
        # Run the synchronous cleanup logic in a thread
        await asyncio.to_thread(self._cleanup_sync)
        
        # The BaseWorker's stop() method will cancel all spawned tasks
        await self.stop()
        print("Worker has been stopped.")

    def _cleanup_sync(self):
        """
        Contains the blocking I/O calls for resource cleanup.

        This method is executed in a separate thread to ensure that shutting
        down services does not block the main loop.
        """
        print("Cleaning up resources...")
        if hasattr(self, 'hue_manager'):
            self.hue_manager.lights_off()
            self.telemetry("hue/state", "Off", retain=True)
        if hasattr(self, 'cc_thriller_group') and self.cc_thriller_group is not None:
            self.cc_thriller_group.stop()
            self.telemetry("speakers/State", "Stopped", retain=True)

    async def _poll_loop(self):
        """
        The main background loop for the worker.

        This loop continuously processes commands from the queue and delegates
        state logic and motion detection to the synchronous `_run_sync_tasks`
        method, which runs in a separate thread to avoid blocking the asyncio
        event loop.
        """
        while True:
            # --- Process Command Queue ---
            try:
                # Check for a command without blocking
                # The queue contains (action, arg) tuples
                action, arg = self.command_queue.get_nowait()
            except queue.Empty:
                action = None
                arg = None

            if action is not None:
                try:
                    self._handle_command(action, arg)
                except Exception as e:
                    # Do not let command handler exceptions kill the poll loop
                    self.telemetry("error", f"Command handler error: {e}")

            # --- Run Synchronous, Blocking Code in a Thread ---
            # This prevents your API calls (Hue, Tesla) from freezing the event loop.
            try:
                await asyncio.to_thread(self._run_sync_tasks)
            except Exception as e:
                # Log and continue; do not kill the main loop
                self.telemetry("error", f"_run_sync_tasks failed: {e}")

            # Use the non-blocking sleep
            await asyncio.sleep(0.2)
            

    def _run_sync_tasks(self):
        """

        Executes all synchronous, blocking tasks for the worker.

        This includes running the current state's `handle` method and polling
        the motion sensors. It is designed to be called via `asyncio.to_thread`
        from the main async poll loop.
        """

        # If a new state requires entry initialization, run it now (sync thread)
        if getattr(self, '_state_needs_enter', False) and self.current_state is not None:
            # Clear first: on_enter may call set_state itself, and that new state must get its own on_enter
            self._state_needs_enter = False
            try:
                self.current_state.on_enter(self)
                self._collect_and_send_telemetry()
                self.last_telemetry_time = time.monotonic()  # Reset timer after state transition
            except Exception as e:
                self.telemetry("error", f"State on_enter error: {e}")

        # Delegate the main work to the current state's handle method
        if self.current_state:
            try:
                self.current_state.handle(self)
            except Exception as e:
                self.telemetry("error", f"State handle error: {e}")

        # Periodic telemetry collection
        current_time = time.monotonic()
        if current_time - self.last_telemetry_time > self.config.TELEMETRY_INTERVAL:
            try:
                self._collect_and_send_telemetry()
            except Exception as e:
                self.telemetry("error", f"Telemetry collection error: {e}")
            finally:
                self.last_telemetry_time = current_time

        # Poll for motion and delegate
        motion_detected = False
        for sensor in self.motion_sensors: # Corrected variable name from your __init__
            try:
                sensor.refresh()
            except Exception as e:
                self.telemetry("sensor/error", f"Sensor refresh failed: {e}")
                continue
            if getattr(sensor, 'presence', False):
                motion_detected = True
                break 
        
        if motion_detected:
            if self.current_state:
                self.current_state.on_motion(self)


    def _collect_and_send_telemetry(self):
        """
        Collect telemetry values that may block (chromecast.state(), tesla
        attributes, etc.) and send them via the worker's telemetry() helper.

        This method must be executed in the sync thread.
        """
        # Re-assert the retained state so a dropped/reordered publish cannot leave the UI stale
        if self.current_state is not None:
            self.publish_state(_state_name(self.current_state.__class__), retain=True)
        try:
            # Hue scene
            try:
                hue_scene = "Disco" if getattr(self.hue_manager.lights, 'disco_on', False) else "Off"
            except Exception:
                hue_scene = "Unknown"
            self.telemetry("hue/Scene", hue_scene, retain=True)

            # Speaker/Chromecast player state (may block)
            try:
                speaker_state = self.cc_thriller_group.state()
            except Exception:
                speaker_state = "unknown"
            self.telemetry("speakers/State", speaker_state, retain=True)
        except Exception as e:
            # Ensure telemetry collection doesn't raise in the sync thread
            print(f"Telemetry collection failed: {e}")


    def _handle_command(self, action: str, arg: str | None):
        """
        Dispatches a queued command to the current state's `on_action` handler.

        Args:
            action: The command action string.
            arg: An optional argument string for the command.
        """
        print(f"Handling command: {action}({arg})")
        if action and self.current_state and hasattr(self.current_state, 'on_action'):
            # Call the state's on_action method
            self.current_state.on_action(self, action, arg)
        else:
            print(f"Warning: No handler for action '{action}' or state has no on_action method.")


    async def _ticker(self):
        """
        A background task that periodically publishes a telemetry tick.

        This serves as a simple heartbeat to indicate that the worker is alive
        and running.
        """
        i = 0
        interval = int(self.config.tick_interval)
        try:
          while True:
              self.telemetry("tick", i)
              i += 1
              await asyncio.sleep(interval)
        except asyncio.CancelledError:
          # Optional: cleanup hardware, close files, etc.
          raise

    async def do_hue(self, arg: str | dict | None):
        """
        Handles the 'hue' command to perform Hue light actions.

        Args:
            arg: A string or dictionary specifying the Hue action to perform.
        """
        if self.hue_manager is None:
            print("Hue manager instance is not available.")
            return

        if isinstance(arg, str):
            action = arg.lower()
            match action:
                case 'disco':
                    self.hue_manager.toggle_disco()
                case 'off':
                    self.hue_manager.lights_off()
                case 'purple':
                    self.hue_manager.send_command('purple')
                case 'red':
                    self.hue_manager.send_command('red')
                case 'green':
                    self.hue_manager.send_command('green')
                case 'blue':
                    self.hue_manager.send_command('blue')
                case 'connect':
                    # call the _connect_hue method to reconnect
                    await asyncio.to_thread(self._connect_hue)
                case _:
                    print(f"Unknown Hue action: {action}")

        elif isinstance(arg, dict):
            # Handle dictionary-based commands if needed
            print("Dictionary-based Hue commands are not implemented.")
        else:
            print("Invalid argument for Hue command.")

        self.telemetry("hue/state", "Disco" if self.hue_manager.lights.disco_on else "Off", retain=True)

    def volume_targets(self) -> list:
        return [self.cc_thriller_group]

    async def do_chromecast(self, arg: str | dict | None):
        """
        Handles the 'chromecast' command to perform Chromecast actions.

        Args:
            arg: A string or dictionary specifying the Chromecast action to perform.
        """
        
        # Try to parse JSON string to dict
        if isinstance(arg, str):
            try:
                parsed = json.loads(arg)
                if isinstance(parsed, dict):
                    arg = parsed
            except (json.JSONDecodeError, ValueError):
                pass  # Keep as string if not valid JSON
        
        if isinstance(arg, str):
            action = arg.lower()
            if action == 'connect':
                # call the _connect_chromecast method to reconnect
                await asyncio.to_thread(self._connect_chromecast)
                return
        
        if self.cc_thriller_group is None or self.cc_thriller_group.is_empty():
            print("Chromecast group is not available.")
            return

        print(f"do_chromecast called with arg: {arg}")

        # Always refresh status first
        await asyncio.to_thread(self.cc_thriller_group.refresh)
        for cast in self.cc_thriller_group.chromecasts:
            try:
                print(f"Device: {cast.cast_info.friendly_name} at {cast.cast_info.host}: {cast.media_controller.status}")
            except Exception as e:
                print(f"Error updating status for {cast}")
                print(f"Exception: {e}")

        if isinstance(arg, str):
            action = arg.lower()
            match action:
                case 'play':
                    url_list = filter_url_list(self.config.CHROMECAST_THRILLER_GROUP)
                    # Prefer the non-blocking background loader which guarantees the
                    # track is PAUSED when ready (unless autoplay=True). This avoids
                    # the race between load_media() and a subsequent play() call.
                    if hasattr(self.cc_thriller_group, 'load_media_bg'):
                        try:
                            task = self.cc_thriller_group.load_media_bg(url_list=url_list, autoplay=False)
                            # Await the asyncio.Future bridge so we resume after
                            # the group is ready (and paused) for playback.
                            await task.as_future()
                        except Exception as e:
                            print(f"Chromecast background load failed: {e}")
                            # Fallback to the legacy blocking load
                            await asyncio.to_thread(self.cc_thriller_group.load_media, url_list=url_list)
                    else:
                        # Older / dummy groups may not implement the new API
                        await asyncio.to_thread(self.cc_thriller_group.load_media, url_list=url_list)

                    # wait a moment for status to update
                    await asyncio.sleep(1)

                    # Now explicitly start playback
                    await asyncio.to_thread(self.cc_thriller_group.play)

                case 'stop':
                    await asyncio.to_thread(self.cc_thriller_group.stop)
                case 'volume_up':
                    await asyncio.to_thread(self.cc_thriller_group.volume_up)
                case 'volume_down':
                    await asyncio.to_thread(self.cc_thriller_group.volume_down)
                case 'fade_to_stop':
                    await asyncio.to_thread(self.cc_thriller_group.fade_to_stop)
                case 'connect':
                    # call the _connect_chromecast method to reconnect
                    await asyncio.to_thread(self._connect_chromecast)
                case 'status':
                    for key, line in self.cc_thriller_group.describe().items():
                        self.telemetry(f"speakers/{key}", line)
                case _:
                    print(f"Unknown Chromecast action: {action}")
        elif isinstance(arg, dict):
            # Handle dictionary-based commands (e.g., volume_set with volume parameter)
            if 'volume' in arg:
                volume = float(arg['volume'])
                print(f"Setting chromecast volume to {volume}")
                await asyncio.to_thread(self.cc_thriller_group.set_volume, volume)
            else:
                print(f"Unknown dictionary-based Chromecast command: {arg}")
        else:
            print("Invalid argument for Chromecast command.")
        
        # Always refresh status first
        await asyncio.to_thread(self.cc_thriller_group.refresh)
        for cast in self.cc_thriller_group.chromecasts:
            try:
                print(f"Device: {cast.cast_info.friendly_name} at {cast.cast_info.host}: {cast.media_controller.status}")
            except Exception as e:
                print(f"Error updating status for {cast}")
                print(f"Exception: {e}")

        self.telemetry("speakers/State", self.cc_thriller_group.state(), retain=True)
                       
