"""Coffin jumper demo firmware helpers.

This module provides a small, test-oriented firmware helper for an ESP32
based prop. It implements Wi‑Fi connection, an SSD1306-driven OLED display
helper, and a lightweight MQTT client wrapper for publishing availability,
state and telemetry. It is intentionally small and synchronous to be easy to
run on constrained MicroPython builds.

Key behaviors covered:
- Connect to Wi‑Fi and optionally install missing libs via mip
- Render a small status display on an SSD1306 OLED
- Publish availability/state/telemetry to configured MQTT topics
- Configure an LWT and react to broker uptime messages

This file is intended for use in automated tests and local demos; it keeps
robustness simple (try/except and short reconnect paths) rather than providing
a full production-grade MQTT client.
"""

import time, json, sys, os

try:
    # MicroPython-specific modules
    import network, machine, ubinascii  # type: ignore
    from machine import Pin, I2C  # type: ignore
    from umqtt.simple import MQTTClient  # type: ignore
    import secrets  # device secrets on the target
    import dfplayer  # MANUAL INSTALL NEEDED! DOESN'T COME WITH MIP
    from pir_hcsr501 import PIRLatch
    import ntptime  # type: ignore
except Exception:
    # Provide fallbacks so the module can be imported on CPython for tests.
    network = None
    machine = None
    ubinascii = None
    Pin = object
    I2C = object
    MQTTClient = None
    try:
        import secrets  # may exist in repo for testing
    except Exception:
        secrets = None

# ---- Constants ----
FW_VERSION = "1.4.0"   # 1.4.0: cooldown state, single "playing" state; 1.3.0: physical-only block and play counter
TELEMETRY_INTERVAL_MS = 5000
MAX_RECONNECT_INTERVAL = 5 * 60 * 1000  # 5 minutes max backoff (given in ms)
ACTION_FOLDER = 0
ACTION_TRACK = 1

# ---- Volume ----
DEFAULT_VOLUME = 20      # DFPlayer volume at boot (0-30); the global limit scales this
MAX_VOLUME = 30
VOLUME_LIMIT_FILE = "volume_limit.json"   # last global limit, so it survives reboots offline
BLOCK_FILE = "blocked.json"              # safety block, so it survives reboots
PLAY_COUNT_FILE = "play_count.json"
ACTION_TIMING_FILE = "action_timing.json"
MAX_CONFIGURABLE_ACTION_MS = 10 * 60 * 1000
MAX_SOLENOID_DELAY_MS = 10 * 1000

PIR_LOCKOUT_MS = 5000    # Minimum ms between triggers (sensor-level default)
PIR_DEBOUNCE_MS= 300     # Debounce time for PIR edges (minimum time between edges)
PIR_WARMUP_MS  = 5000    # Ignore PIR edges for this long after boot

# Cooldown: after an action ends the prop stays in "cooldown" this long before it re-arms.
# Stored/telemetry key stays "action_lockout_ms" for compatibility.
ACTION_LOCKOUT_MS = 30000

# ---- Safeguards ----
WDT_TIMEOUT_MS = 60 * 1000       # hardware watchdog: reboot if the loop stalls this long
ACTION_MAX_MS = 60 * 1000        # failsafe: force-stop an action (solenoid off) after this long
ACTION_MIN_MS = 5000             # minimum solenoid-on time for an action
MAX_CONSECUTIVE_ERRORS = 10      # reboot after this many loop errors in a row
CRASH_LOG = "crash.log"          # on-device error log (kept small)
CRASH_LOG_MAX_BYTES = 4096

# OLED dimensions (set to 32 if your display is 128x32)
OLED_W = 128
OLED_H = 64
OLED_ADDR = 0x3C

# ---- Pins ----
PIN_PIR        = 4       # PIR output (3.3V logic!) 
PIN_SOLENOID   = 2       # MOSFET gate         (Connect to GPIO2)
UART_NUM       = 2       # DFPlayer UART
UART_TX        = 17      # ESP32 TX -> DF RX   (Updated per board)
UART_RX        = 16      # ESP32 RX <- DF TX   (Updated per board)
I2C_SDA        = 21
I2C_SCL        = 22


# ---- Topics ----
BASE = b"halloween/esp32-coffin-jumper-01"
T_AVAIL = BASE + b"/availability"
T_STATE = BASE + b"/state"
T_TEL   = BASE + b"/telemetry"
T_PLAY_COUNT = BASE + b"/telemetry/playback/PlayCount"
T_CMD   = BASE + b"/cmd"
T_LOG   = BASE + b"/log"
T_ACK   = BASE + b"/ack"                 # {"id", "action", "ok", "reason"} per command carrying an id
T_BLOCKED = BASE + b"/state/blocked"     # retained "1"/"0", same flag the workers publish
CMD_ID_HISTORY = 20                      # recent command ids kept to drop QoS 1 duplicates
T_BROKER_UP = b"halloween/broker/uptime"
T_VOL_LIMIT = b"halloween/broadcast/volume_limit"   # retained, JSON {"scale": 0..1}

# ---- Timezone (adjust when DST changes) ----
TZ_OFFSET_HOURS = 2     # Denmark: 1 (CET winter), 2 (CEST summer)
TZ_NAME = "CET/CEST"

# ----------------------------
# Utilities (stateless helpers)
# ----------------------------
def ensure_lib(modname: str, mip_name: object = None) -> object:
    """Ensure a Python module is available, installing it via mip if needed.

    This helper attempts to import ``modname``. If the import fails it will
    try to install the module using ``mip`` (MicroPython package manager) and
    then re-import it. In a constrained device this keeps optional helpers
    installable at runtime.

    Args:
        modname (str): Module name to import.
        mip_name (str|None): Optional package name for mip if it differs.

    Returns:
        module: The imported module object.
    """
    try:
        mod = __import__(modname)
        print("Lib OK:", modname)
        return mod
    except ImportError:
        print("Installing:", mip_name or modname)
        import mip
        mip.install(mip_name or modname)
        mod = __import__(modname)
        print("Installed:", modname)
        return mod

def wrap(text: str, max_chars: int) -> list:
    """Simple word-wrap helper.

    Splits ``text`` into a list of lines where each line is at most
    ``max_chars`` characters. This is intended for rendering short strings
    on the OLED.

    Args:
        text (str): Input text to wrap.
        max_chars (int): Maximum characters per line.

    Returns:
        list[str]: Lines of wrapped text.
    """
    out, line = [], ""
    for w in text.split():
        if len(line) + (1 if line else 0) + len(w) <= max_chars:
            line = (line + " " + w) if line else w
        else:
            out.append(line)
            line = w
    if line:
        out.append(line)
    return out

def draw_center(oled_obj: object, text: str, y: int) -> None:
    """Draw ``text`` centered horizontally at vertical position ``y``.

    Args:
        oled_obj: SSD1306-like object with a ``text`` method.
        text (str): Text to draw.
        y (int): Vertical pixel coordinate.
    """
    x = (OLED_W - len(text) * 6) // 2
    if x < 0:
        x = 0
    oled_obj.text(text, x, y)

def format_uptime(seconds: int) -> str:
    """Format an uptime in seconds to a human-readable string.

    Args:
        seconds (int): Uptime in seconds.

    Returns:
        str: Formatted uptime string, e.g. "1h 23m 45s".
    """
    hours = seconds // 3600
    rem = seconds % 3600
    mins = rem // 60
    secs = rem % 60
    return f"{hours}h {mins}m {secs}s"

# ----------------------------
# Main Prop Class
# ----------------------------
class CoffinProp:
    """A class to encapsulate the coffin prop's state and logic."""

    def __init__(self, debug=False):
        """Initialize the prop, its state, and component placeholders."""
        self._debug = debug
        
        # State
        self.state = "booting"
        self.is_blocked = self._load_blocked()   # safety lock: prevents solenoid activation
        self.is_armed = False     # armed: motion (PIR) triggers the action; always starts stopped
        self.is_paused = False    # a running action is paused (music held, solenoid off)
        self.play_count = self._load_play_count()
        self._seen_cmd_ids = []
        self.volume = DEFAULT_VOLUME          # requested volume; plays at min(volume, cap)
        self.volume_limit = self._load_volume_limit()
        (self.action_min_ms, self.action_max_ms, self.action_lockout_ms,
         self.solenoid_delay_ms) = self._load_action_timing()
        self.broker_uptime_str = "—"
        self._last_action_start_ms = None
        self._in_action = False          # the pre-programmed action (not just music) is running
        self._cooldown_until_ms = None   # set while in the "cooldown" state
        self._last_cooldown_pub_ms = 0
        self._solenoid_due_ms = None
        self._solenoid_started_ms = None
        self.wdt = None
        self._consecutive_errors = 0
        self._log_pending = None   # crash log text found at boot, published once MQTT is up
        self.reset_cause = None
        self._next_wifi_reconnect_ms = 0
        self._wifi_reconnect_attempts = 0
        self._next_mqtt_reconnect_ms = 0
        self._mqtt_reconnect_attempts = 0

        # Components
        self.wlan = None
        self.mqtt = None
        self.oled = None
        self.pir_latch = None
        self.solenoid_pin = None
        self.dfp = None

    # ----------------------------
    # Safeguards: logging, watchdog, failsafe
    # ----------------------------
    def _feed_wdt(self):
        """Feed the hardware watchdog (no-op if it is not running)."""
        if self.wdt:
            try:
                self.wdt.feed()
            except Exception:
                pass

    def _solenoid_off(self):
        """Force the solenoid off; safe to call at any time."""
        try:
            if self.solenoid_pin is not None:
                self.solenoid_pin.value(0)
        except Exception:
            pass

    def log_error(self, where: str, exc: Exception):
        """Print the traceback, append it to crash.log, and publish it if MQTT is up."""
        try:
            print("ERROR in {}: {!r}".format(where, exc))
            sys.print_exception(exc)
        except Exception:
            pass
        text = "[up {}s] {}: {!r}".format(time.ticks_ms() // 1000, where, exc)
        try:
            try:
                size = os.stat(CRASH_LOG)[6]
            except OSError:
                size = 0
            mode = "w" if size > CRASH_LOG_MAX_BYTES else "a"   # start over rather than fill flash
            with open(CRASH_LOG, mode) as f:
                f.write(text + "\n")
                sys.print_exception(exc, f)
        except Exception as e:
            print("Crash log write failed:", e)
        self._publish(T_LOG, text)

    def _load_boot_log(self):
        """Note why we booted and keep the previous crash log to publish once online."""
        try:
            self.reset_cause = machine.reset_cause()
            print("Reset cause:", self.reset_cause)
        except Exception:
            pass
        try:
            with open(CRASH_LOG) as f:
                data = f.read()
            if data:
                self._log_pending = data[-1500:]
                print("Previous crash log found:\n" + data)
        except OSError:
            pass

    def _publish_boot_log(self):
        """Publish the stored crash log (if any) after MQTT connects, then clear it."""
        if self._log_pending and self.mqtt:
            self._publish(T_LOG, "previous crash log: " + self._log_pending)
            self._log_pending = None
            try:
                os.remove(CRASH_LOG)
            except OSError:
                pass

    def _action_failsafe(self, now):
        """Force-stop an action that has run longer than ACTION_MAX_MS."""
        if (self._in_action and not self.is_paused and self._last_action_start_ms is not None
                and time.ticks_diff(now, self._last_action_start_ms) > self.action_max_ms):
            print("ACTION: exceeded configured maximum duration, forcing stop")
            self._publish(T_LOG, "action exceeded configured max duration, forced stop")
            self._stop_action(cooldown=True)

    # ----------------------------
    # Initialization
    # ----------------------------
    def _init_wifi(self) -> bool:
        """Connect the board to a Wi‑Fi access point.
        
        Returns:
            bool: True on success, False on failure.
        """
        if not secrets:
            print("Wi-Fi: secrets.py not found, cannot connect.")
            return False
        
        print("Wi-Fi: connecting to", secrets.WIFI_SSID)
        self.wlan = network.WLAN(network.STA_IF)
        if not self.wlan.active():
            self.wlan.active(True)
        
        if not self.wlan.isconnected():
            self.wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
            t0 = time.ticks_ms()
            while not self.wlan.isconnected():
                if time.ticks_diff(time.ticks_ms(), t0) > 20 * 1000:
                    print("Wi-Fi: connect timeout")
                    self.wlan = None
                    return False
                time.sleep(0.2)
                self._feed_wdt()

        print("Wi-Fi: connected", self.wlan.ifconfig())
        return True

    def _init_get_ntp_time(self):
        # 4. Sync NTP if online
        if self.wlan:
            try:
                ntptime.settime()
                print("NTP: synced")
            except Exception as e:
                print("NTP error:", e)

    def _init_peripherals(self):
        """Initialize hardware peripherals (OLED, PIR, DFPlayer)."""
        # PIR Latch
        try:
            self.pir_latch = PIRLatch(
                PIN_PIR, 
                hold_ms=PIR_LOCKOUT_MS, 
                debounce_ms=PIR_DEBOUNCE_MS, 
                warmup_ms=PIR_WARMUP_MS
            )
            print("PIR: Initialized")
        except Exception as e:
            print("PIR: Failed to initialize:", e)

        # OLED Display
        try:
            ssd1306 = ensure_lib("ssd1306")
            try:
                i2c = I2C(0, sda=machine.Pin(I2C_SDA), scl=machine.Pin(I2C_SCL), freq=400_000)
            except TypeError:
                i2c = I2C(1, sda=machine.Pin(I2C_SDA), scl=machine.Pin(I2C_SCL), freq=400_000)
            self.oled = ssd1306.SSD1306_I2C(OLED_W, OLED_H, i2c, addr=OLED_ADDR)
            print("OLED: Initialized")
        except Exception as e:
            print("OLED: Failed to initialize:", e)

        # DFPlayer
        try:
            self.dfp = dfplayer.DFPlayer(uart_id=UART_NUM, tx_pin_id=UART_TX, rx_pin_id=UART_RX)
            self.dfp.volume(self.effective_volume())
            print("DFPlayer: Initialized")
        except Exception as e:
            print("DFPlayer: Failed to initialize:", e)

        # Solenoid Pin (as output to drive MOSFET gate)
        try:
            self.solenoid_pin = machine.Pin(PIN_SOLENOID, machine.Pin.OUT)
            self.solenoid_pin.value(0)  # Ensure it's off initially
            print("Solenoid Pin: Initialized and set to LOW")
        except Exception as e:
            print("Solenoid Pin: Failed to initialize:", e)

    def _init_mqtt(self):
        """Create and connect the MQTT client."""
        if not self.wlan or not self.wlan.isconnected() or not secrets:
            print("MQTT: No Wi-Fi or secrets, skipping connection.")
            return

        try:
            self.mqtt = MQTTClient(
                client_id=secrets.CLIENT_ID,
                server=secrets.MQTT_HOST,
                port=secrets.MQTT_PORT,
                user=secrets.MQTT_USER,
                password=secrets.MQTT_PASSWORD,
                keepalive=30,
            )
            self.mqtt.set_last_will(T_AVAIL, b"offline", retain=True, qos=0)
            self.mqtt.set_callback(self._on_mqtt_message)
            self.mqtt.connect()
            self.mqtt.subscribe(T_CMD, 1)
            self.mqtt.subscribe(T_BROKER_UP)
            self.mqtt.subscribe(T_VOL_LIMIT)
            self._birth()
            self.set_state(self._rest_state())
            print("MQTT: Connected and configured")
            self._mqtt_reconnect_attempts = 0  # Reset on success
        except Exception as e:
            print("MQTT: Failed to connect:", e)
            if self.mqtt:
                try: self.mqtt.disconnect()
                except: pass
            self.mqtt = None

    # ----------------------------
    # Core Logic & State
    # ----------------------------
    def _rest_state(self) -> str:
        """State when no action runs; blocking is a separate flag."""
        if self._cooldown_until_ms is not None:
            return "cooldown"
        return "armed" if self.is_armed else "stopped"

    def set_state(self, new_state: str):
        """Update the local state and publish it via MQTT if connected."""
        self.state = new_state
        if self.mqtt:
            self._publish(T_STATE, new_state, retain=True)
        self.render_display()

    def _birth(self):
        """Mark this device as online and set the initial state."""
        if self.mqtt:
            self._publish(T_AVAIL, b"online", retain=True)
            self._publish_boot_log()
            self._publish_blocked()
            self._publish_play_count()

    def _start_action(self):
        """Perform the pre-programmed action."""
        print("ACTION: Starting...")
        self.play_track(folder=ACTION_FOLDER, track=ACTION_TRACK)
        self._last_action_start_ms = time.ticks_ms()
        self._solenoid_due_ms = (
            time.ticks_add(self._last_action_start_ms, self.solenoid_delay_ms)
            if not self.is_blocked else None
        )
        self._solenoid_started_ms = None
        self._in_action = True
        self._cooldown_until_ms = None
        if self.solenoid_delay_ms == 0 and not self.is_blocked:
            if self.solenoid_pin is not None:
                self.solenoid_pin.value(1)
                self._solenoid_started_ms = self._last_action_start_ms
        elif self.solenoid_delay_ms > 0 and not self.is_blocked:
            print("Solenoid activation delayed by {} ms".format(self.solenoid_delay_ms))
        self.set_state("playing")
        self.play_count += 1
        self._save_play_count()
        self._publish_play_count()
        print("ACTION started.")

    def _stop_action(self, cooldown=False):
        """Clean up after the action. cooldown=True (natural end) enters the cooldown state."""
        print("ACTION: Stopping...")
        self._in_action = False
        self._cooldown_until_ms = (
            time.ticks_add(time.ticks_ms(), self.action_lockout_ms)
            if cooldown and self.action_lockout_ms > 0 else None
        )
        self._last_cooldown_pub_ms = time.ticks_ms()
        if self.solenoid_pin:
            self.solenoid_pin.value(0)  # Deactivate solenoid
        self._solenoid_due_ms = None
        self._solenoid_started_ms = None
        
        # ensure the player is stopped
        if self.dfp:
            self.dfp.stop()

        self.set_state(self._rest_state())
        if self._cooldown_until_ms is not None:
            self._telemetry()
        print("ACTION: Stopped and state reset.")

    def _check_cooldown(self, now):
        """Leave the cooldown state when its time is up; refresh the countdown once a second."""
        if self._cooldown_until_ms is None:
            return
        if time.ticks_diff(now, self._cooldown_until_ms) >= 0:
            self._cooldown_until_ms = None
            if self.state == "cooldown":
                self.set_state(self._rest_state())
            self._telemetry()
        elif time.ticks_diff(now, self._last_cooldown_pub_ms) >= 1000:
            self._last_cooldown_pub_ms = now
            self._telemetry()

    def _check_action_status(self, now=None):
        """If an action is running, check if it has completed."""
        if not self._in_action or self.is_paused:
            return
        if now is None:
            now = time.ticks_ms()

        action_is_ongoing = False
        try:
            # Condition 1: DFPlayer is playing
            if self.dfp and self.dfp.is_playing():
                action_is_ongoing = True
            
            # Future condition 2: Check another sensor (e.g., PIR)
            # if self.some_other_sensor.is_active():
            #     action_is_ongoing = True

        except Exception as e:
            print("Error checking action status:", e)
            self.set_state("IO Error")
            # If there's an error, we should probably stop the action
            action_is_ongoing = False

        if (action_is_ongoing and not self.is_blocked and self._solenoid_due_ms is not None
                and time.ticks_diff(now, self._solenoid_due_ms) >= 0):
            if self.solenoid_pin is not None:
                self.solenoid_pin.value(1)
                self._solenoid_started_ms = now
            self._solenoid_due_ms = None

        min_time_remaining = (
            self._solenoid_started_ms is not None
            and time.ticks_diff(now, self._solenoid_started_ms) < self.action_min_ms
        )
        if not action_is_ongoing and not min_time_remaining:
            self._stop_action(cooldown=True)

    def _process_triggers(self, now):
        """Check trigger conditions and start an action if appropriate.

        Uses timestamp-based lockout instead of blocking sleep so the main loop
        can continue processing MQTT, display, etc.
        """
        if not self.pir_latch:
            return

        # If PIR has a pending edge, funnel it through request_trigger
        if self.pir_latch.pending():
            accepted = self.request_trigger(now=now, source="pir")
            if not accepted:
                # still consume a small debounce so pending doesn't immediately reappear
                try:
                    self.pir_latch.consume_for(PIR_DEBOUNCE_MS)
                except Exception:
                    pass
                
    def request_trigger(self, now=None, source="remote") -> bool:
        """Attempt to start an action triggered from 'source' ('pir'|'remote'|'mqtt').

        Returns True if an action started, False if the request was rejected.
        This centralizes lockout and state checks so all trigger paths behave the same.
        """
        if now is None:
            now = time.ticks_ms()

        # No new action while one runs or during the cooldown
        if self._in_action or self._cooldown_until_ms is not None:
            return False

        # Motion only counts while armed and not paused; a manual Trigger always may
        if source == "pir" and (not self.is_armed or self.is_paused):
            return False

        # A blocked prop still plays its sound; only solenoid activation is inhibited.
        self._start_action()

        # Quiet the PIR to avoid immediate re-triggering:
        try:
            if self.pir_latch:
                if source == "pir":
                    # # short debounce for PIR-originated edge
                    # self.pir_latch.consume_for(PIR_DEBOUNCE_MS)
                    # Currently we use the same ACTION_LOCKOUT_MS for simplicity
                    self.pir_latch.consume_for(self.action_lockout_ms)
                else:
                    # remote trigger: keep PIR quiet for the action lockout to avoid
                    # re-triggering while action runs
                    self.pir_latch.consume_for(self.action_lockout_ms)
        except Exception:
            pass

        return True


    # ----------------------------
    # Display & MQTT Helpers
    # ----------------------------
    def render_display(self):
        """Render the status screen on the OLED, if available."""
        if not self.oled:
            return
        
        self.oled.fill(0)
        self.oled.text("Coffin v{}".format(FW_VERSION), 0, 0)
        self.oled.text("State: {}".format(self.state[:10]), 0, 8)
        self.oled.text("Plays: {}".format(self.play_count), 0, 16)
        
        pir_status = "---"
        if self.pir_latch:
            pir_status = "Motion!" if self.pir_latch.active() else "---"
        self.oled.text("PIR: {}".format(pir_status), 0, 24)
        
        self.oled.text("Blk: {}".format("Y" if self.is_blocked else "N"), 0, 32)
        
        up = self.broker_uptime_str
        if len(up) > 18:
            up = up[:18]
        self.oled.text("Up: {}".format(up), 0, 40)
        
        self.oled.show()

    def _publish(self, topic: bytes, payload, retain: bool = False):
        """Publish a payload to a topic with light normalization."""
        if not self.mqtt:
            return
        
        try:
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload)
            if isinstance(payload, str):
                payload = payload.encode()

            if self._debug:
                try:
                    print("MQTT TX:", topic, payload)
                except Exception:
                    pass
            
            self.mqtt.publish(topic, payload, retain=retain, qos=0)
        except Exception as e:
            print("MQTT: Publish failed:", e)

    def _telemetry(self):
        """Publish a small telemetry JSON payload."""
        tel = {
            "fw": FW_VERSION,
            "uptime_s": time.ticks_ms() // 1000,
            "action_min_ms": self.action_min_ms,
            "action_max_ms": self.action_max_ms,
            "action_lockout_ms": self.action_lockout_ms,
            "cooldown_s": (max(0, time.ticks_diff(self._cooldown_until_ms, time.ticks_ms()) // 1000 + 1)
                           if self._cooldown_until_ms is not None else 0),
            "solenoid_delay_ms": self.solenoid_delay_ms,
            "play_count": self.play_count,
            "pir": "---",
            "relay": "---",
            "relay_pin_state": "---",
            "blocked": self.is_blocked,
            "armed": self.is_armed,
            "paused": self.is_paused,
            "vol": self.volume,
            "vol_effective": self.effective_volume(),
            "vol_limit": "off" if self.volume_limit >= 1.0 else "{}%".format(round(self.volume_limit * 100)),
            "rssi": None,
            "reset_cause": self.reset_cause,
            "loop_errors": self._consecutive_errors,
        }
        if self.pir_latch:
            tel["pir"] = "Motion!" if self.pir_latch.active() else "---"

        if self.solenoid_pin is not None:
            # relay is really a MOSFET, driven by SOLENOID pin
            tel["solenoid_pin_state"] = "High" if self.solenoid_pin.value() else "Low"
            tel["solenoid_power"] = "On" if self.solenoid_pin.value() else "Off"

        if self.wlan and hasattr(self.wlan, "status"):
            tel["rssi"] = self.wlan.status('rssi')
        
        self._publish(T_TEL, tel, retain=False)

    # ----------------------------
    # MQTT Message Handlers
    # ----------------------------
    def _on_mqtt_message(self, topic: bytes, msg: bytes):
        """Callback for incoming MQTT messages."""
        try:
            t = topic.decode()
        except:
            t = str(topic)

        if self._debug:
            try:
                print("MQTT RX:", t, msg)
            except Exception:
                pass

        if t == T_CMD.decode():
            self._on_cmd(msg)
        elif t == T_BROKER_UP.decode():
            self._on_broker_uptime(msg)
        elif t == T_VOL_LIMIT.decode():
            self._on_volume_limit(msg)

    def _on_volume_limit(self, msg: bytes):
        """Apply the global volume limit (same semantics as halloween_common.volume_limit)."""
        scale = 1.0
        try:
            obj = json.loads(msg.decode())
            scale = float(obj.get("scale", 1.0) if isinstance(obj, dict) else obj)
        except Exception:
            print("Bad volume limit payload:", msg)
        scale = min(max(scale, 0.0), 1.0)
        if scale != self.volume_limit:
            self.volume_limit = scale
            self._save_volume_limit()
        print("Volume limit:", scale)
        if self.dfp:
            try:
                self.dfp.volume(self.effective_volume())
            except Exception as e:
                print("Error applying volume limit:", e)

    def _on_broker_uptime(self, msg: bytes):
        """Handle incoming broker uptime messages."""
        try:
            msg_json = json.loads(msg.decode())
            if self._debug:
                print("Broker uptime payload JSON:", msg_json)

            raw = msg_json.get('uptime_s')
            broker_uptime_seconds = int(float(raw)) if raw is not None else 0
            self.broker_uptime_str = format_uptime(broker_uptime_seconds)

            if self._debug:
                print("Broker uptime str:", self.broker_uptime_str)
        except Exception as e:
            print("Failed to parse broker uptime payload:", msg, "Error:", e)
            self.broker_uptime_str = str(msg)
        
        self.render_display()

    def _on_cmd(self, msg: bytes):
        """Handle incoming command messages."""
        try:
            s = msg.decode()
        except:
            s = str(msg)

        action, params, cmd_id = None, {}, None
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                action = obj.get("action")
                # "params" is the coffin's native key; "args" is the shared worker contract
                params = obj.get("params", obj.get("args", {}))
                cmd_id = obj.get("id")
            except Exception:
                pass
        else:
            action = s.strip().lower()
        if not isinstance(params, dict):
            params = {}

        # QoS 1 can deliver a command twice: answer a repeat without running it again
        if cmd_id is not None and cmd_id in self._seen_cmd_ids:
            self._ack(cmd_id, action, True, "duplicate")
            return

        print("Command action:", action)
        try:
            ok, reason = self._run_cmd(action, params)
        except Exception as e:
            ok, reason = False, "error: {}".format(e)
            self.log_error("command " + str(action), e)
        if cmd_id is not None:
            self._seen_cmd_ids.append(cmd_id)
            del self._seen_cmd_ids[:-CMD_ID_HISTORY]
            self._ack(cmd_id, action, ok, reason)

    def _ack(self, cmd_id, action, ok, reason=None):
        """Tell the dashboard what happened to a command."""
        self._publish(T_ACK, {"id": cmd_id, "action": action, "ok": bool(ok), "reason": reason})

    def _publish_blocked(self):
        """Retained flag the dashboard shows as the safety-block badge."""
        self._publish(T_BLOCKED, "1" if self.is_blocked else "0", retain=True)

    def _publish_play_count(self):
        self._publish(T_PLAY_COUNT, str(self.play_count), retain=True)

    def _run_cmd(self, action, params):
        """Run one command; returns (ok, reason)."""
        result = (True, None)
        if not action:
            return False, "empty or invalid command"

        # block / unblock inhibit only the physical solenoid action (see README).
        if action == "block":
            self.is_blocked = True
            self._save_blocked()
            self._solenoid_due_ms = None
            self._solenoid_started_ms = None
            if self.solenoid_pin:
                self.solenoid_pin.value(0)
            if self.state not in ("playing", "paused"):
                self.set_state(self._rest_state())
        elif action == "unblock":
            self.is_blocked = False
            self._save_blocked()
            if self.state not in ("playing", "paused"):
                self.set_state(self._rest_state())
        elif action == "reset_counter":
            self.play_count = 0
            if not self._save_play_count():
                result = (False, "could not persist play counter")
            self._publish_play_count()
        elif action == "arm":
            self.is_armed = True
            if self.is_paused:                    # arming ends a paused action
                self.is_paused = False
                self._stop_action()
            elif self.state != "playing":
                self.set_state(self._rest_state())
        elif action == "stop":
            # Disarm: end anything running and ignore motion until armed again
            self.is_armed = False
            self.is_paused = False
            self._stop_action()
        elif action == "pause":
            if self.state == "playing" and not self.is_paused:
                self.is_paused = True
                if self.solenoid_pin:
                    self.solenoid_pin.value(0)
                self.pause()
                self.set_state("paused")
        elif action == "trigger":
            if self.is_paused:                    # Trigger resumes a paused action
                self.is_paused = False
                self.resume()
                if self._in_action:
                    self._last_action_start_ms = time.ticks_ms()   # failsafe counts from resume
            else:
                now = time.ticks_ms()
                accepted = self.request_trigger(now=now, source="mqtt")
                if not accepted:
                    print("Remote trigger rejected (lockout/state).")
                    result = (False, "trigger rejected (cooldown or already running)")
        elif action == "stop_action":
            if self._in_action:
                self._stop_action()
        elif action == "play_music":
            vol = params.get("volume")
            if vol is not None:
                self.set_volume(vol)
            track = params.get("track", 1)
            self.play_track(folder=0, track=track)
        elif action == "set_volume":
            vol = params.get("volume")
            if vol is None:
                result = (False, "missing volume")
            elif not self.set_volume(vol):
                result = (False, "volume must be 0-30 and the player available")
        elif action == "set_action_timing":
            result = self._set_action_timing(params)
        elif action == "pause_music":
            self.pause()
        elif action == "resume_music":
            self.resume()
        elif action == "stop_music":
            self.stop()
        else:
            result = (False, "unknown action: {}".format(action))

        # Report the new settings at once, so the dashboard need not wait for the next interval
        self._publish_blocked()
        self._telemetry()
        self._last_tel = time.ticks_ms()
        return result

    # ----------------------------
    # DFPlayer Methods
    # ----------------------------
    def volume_cap(self) -> int:
        """Highest allowed volume: no limit at 100%, else limit * DEFAULT_VOLUME."""
        if self.volume_limit >= 1.0:
            return MAX_VOLUME
        return int(round(DEFAULT_VOLUME * self.volume_limit))

    def effective_volume(self) -> int:
        return min(self.volume, self.volume_cap())

    def _load_volume_limit(self) -> float:
        try:
            with open(VOLUME_LIMIT_FILE) as f:
                return min(max(float(json.loads(f.read()).get("scale", 1.0)), 0.0), 1.0)
        except Exception:
            return 1.0

    def _load_action_timing(self):
        defaults = (ACTION_MIN_MS, ACTION_MAX_MS, ACTION_LOCKOUT_MS, 0)
        try:
            with open(ACTION_TIMING_FILE) as f:
                values = json.loads(f.read())
            minimum = int(values["action_min_ms"])
            maximum = int(values["action_max_ms"])
            lockout = int(values["action_lockout_ms"])
            delay = int(values.get("solenoid_delay_ms", 0))
            if (minimum < 0 or maximum < 1 or lockout < 0
                    or maximum < minimum
                    or max(minimum, maximum, lockout) > MAX_CONFIGURABLE_ACTION_MS):
                raise ValueError("stored action timing values are out of range")
            if not 0 <= delay <= MAX_SOLENOID_DELAY_MS:
                raise ValueError("stored solenoid delay is out of range")
            return minimum, maximum, lockout, delay
        except OSError as e:
            if not e.args or e.args[0] != 2:
                print("Could not read action timing settings; using firmware defaults:", e)
            return defaults
        except Exception as e:
            print("Invalid action timing settings; using firmware defaults:", e)
            return defaults

    def _set_action_timing(self, params):
        try:
            raw_minimum = params["action_min_ms"]
            raw_maximum = params["action_max_ms"]
            raw_lockout = params["action_lockout_ms"]
            raw_delay = params.get("solenoid_delay_ms", self.solenoid_delay_ms)
            if any(isinstance(value, bool) for value in
                   (raw_minimum, raw_maximum, raw_lockout, raw_delay)):
                raise ValueError("values must be integer milliseconds")
            minimum = int(raw_minimum)
            maximum = int(raw_maximum)
            lockout = int(raw_lockout)
            delay = int(raw_delay)
            if any(value != int_value for value, int_value in (
                    (raw_minimum, minimum), (raw_maximum, maximum),
                    (raw_lockout, lockout), (raw_delay, delay))):
                raise ValueError("values must be integer milliseconds")
            if (minimum < 0 or maximum < 1 or lockout < 0
                    or maximum < minimum
                    or max(minimum, maximum, lockout) > MAX_CONFIGURABLE_ACTION_MS):
                raise ValueError("values must be 0..600000 ms, with max >= min and max > 0")
            if not 0 <= delay <= MAX_SOLENOID_DELAY_MS:
                raise ValueError("solenoid delay must be 0..10000 ms")
        except (KeyError, TypeError, ValueError) as e:
            print("Rejected action timing settings:", e)
            return False, "rejected timing: {}".format(e)

        try:
            with open(ACTION_TIMING_FILE, "w") as f:
                f.write(json.dumps({
                    "action_min_ms": minimum,
                    "action_max_ms": maximum,
                    "action_lockout_ms": lockout,
                    "solenoid_delay_ms": delay,
                }))
        except Exception as e:
            print("Could not save action timing settings:", e)

        self.action_min_ms = minimum
        self.action_max_ms = maximum
        self.action_lockout_ms = lockout
        self.solenoid_delay_ms = delay
        print("Action timing updated:", minimum, maximum, lockout, delay)
        return True, None

    def _load_blocked(self) -> bool:
        try:
            with open(BLOCK_FILE) as f:
                return bool(json.loads(f.read()).get("blocked", False))
        except Exception:
            return False

    def _load_play_count(self) -> int:
        try:
            with open(PLAY_COUNT_FILE) as f:
                count = json.loads(f.read()).get("play_count", 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("invalid play counter")
            return count
        except OSError:
            return 0
        except Exception as e:
            print("Could not load play counter:", e)
            return 0

    def _save_play_count(self) -> bool:
        try:
            with open(PLAY_COUNT_FILE, "w") as f:
                f.write(json.dumps({"play_count": self.play_count}))
            return True
        except Exception as e:
            print("Could not save play counter:", e)
            return False

    def _save_blocked(self):
        try:
            with open(BLOCK_FILE, "w") as f:
                f.write(json.dumps({"blocked": self.is_blocked}))
        except Exception as e:
            print("Could not save block state:", e)

    def _save_volume_limit(self):
        try:
            with open(VOLUME_LIMIT_FILE, "w") as f:
                f.write(json.dumps({"scale": self.volume_limit}))
        except Exception as e:
            print("Could not save volume limit:", e)

    def set_volume(self, vol: int) -> bool:
        """Set the requested DFPlayer volume (0-30); plays at most volume_cap()."""
        if not self.dfp: return False
        try:
            vol = int(vol)
            if 0 <= vol <= MAX_VOLUME:
                self.volume = vol
                self.dfp.volume(self.effective_volume())
                return True
            print("Volume out of range (0-30):", vol)
        except Exception as e:
            print("Error setting volume:", e)
        return False

    def play_track(self, folder: int = 0, track: int = 1):
        """Play a specific track on the DFPlayer."""
        if not self.dfp: return
        try:
            if track >= 1:
                if folder == 0:
                    print("Playing track {} from root".format(track))
                    self.dfp.play_from_root(track)
                else:
                    print("Playing track {} from folder {}".format(track, folder))
                    self.dfp.play(folder, track)
                #self.dfp.send_cmd(3, folder, track)
                self.set_state("playing")
            else:
                print("Track number must be >= 1:", track)
        except Exception as e:
            print("Error playing track:", e)

    def pause(self):
        """Pause playback on the DFPlayer."""
        if not self.dfp: return
        try:
            self.dfp.pause()
        except Exception as e:
            print("Error pausing track:", e)

    def resume(self):
        """Resume playback on the DFPlayer."""
        if not self.dfp: return
        try:
            self.dfp.resume()
            self.set_state("playing")
        except Exception as e:
            print("Error resuming track:", e)

    def stop(self):
        """Stop playback on the DFPlayer."""
        if not self.dfp: return
        try:
            self.dfp.stop()
            self.set_state(self._rest_state())
        except Exception as e:
            print("Error stopping track:", e)

    # ----------------------------
    # Reconnection Backoff
    # ----------------------------
    def _get_backoff_delay_ms(self, attempts: int) -> int:
        """Calculate exponential backoff delay with a cap."""
        # Exponential backoff: 2s, 4s, 8s, 16s, ..., up to 5 mins
        delay = min(MAX_RECONNECT_INTERVAL, (2 ** attempts) * 1000)
        return delay

    # ----------------------------
    # Other Helpers
    # ----------------------------

    def sleep(self, duration: float, verbose: bool = False):
        """Sleep for a given duration in seconds.
        If verbose is true, print a countdown message every 1 second.
        """
        if verbose:
            for remaining in range(int(duration), 0, -1):
                print(f"Sleeping... {remaining} seconds remaining")
                time.sleep(1)
            if duration % 1:
                time.sleep(duration % 1) # Sleep the fractional part
        else:
            time.sleep(duration)

    # ----------------------------
    # Main Loop
    # ----------------------------
    def run(self):
        """Main entrypoint for the device firmware."""
        # 1. Initialize peripherals
        self._load_boot_log()
        self._init_peripherals()

        # Start the watchdog once the (slow, one-off) peripheral setup is done
        try:
            self.wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)
            print("Watchdog: started ({} s)".format(WDT_TIMEOUT_MS // 1000))
        except Exception as e:
            print("Watchdog: not available:", e)

        # 2. Splash screen
        if self.oled:
            self.oled.fill(0)
            self.oled.text("ESP32 + Coffin", 0, 0)
            self.oled.text("Connecting...", 0, 16)
            self.oled.show()

        # 3. Connect to Wi-Fi
        self._init_wifi()
        self._init_get_ntp_time()
        if self.oled:
            ip = self.wlan.ifconfig()[0] if self.wlan else "Offline"
            self.oled.fill(0)
            self.oled.text("ESP32 + Coffin", 0, 0)
            self.oled.text("IP: " + ip, 0, 8)
            self.oled.show()

        # 4. Connect to MQTT if online
        self._init_mqtt()
        self.set_state(self._rest_state())

        # 5. Main loop, guarded: one bad iteration is logged, not fatal
        self._last_tel = 0
        while True:
            try:
                self._step(time.ticks_ms())
                self._consecutive_errors = 0
            except Exception as e:
                self._consecutive_errors += 1
                self._solenoid_off()
                self.log_error("main loop", e)
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise   # main() logs and reboots
                time.sleep(0.5)
            self._feed_wdt()
            time.sleep(0.1)

    def _step(self, now):
        """One iteration of the main loop."""
        if True:
            # --- Handle Wi-Fi and MQTT Connections with Backoff ---
            # 1. Check Wi-Fi status
            if not self.wlan or not self.wlan.isconnected():
                if self.mqtt:
                    try: self.mqtt.disconnect()
                    except: pass
                    self.mqtt = None
                    print("Wi-Fi: Connection lost, MQTT disconnected.")
                
                if time.ticks_diff(now, self._next_wifi_reconnect_ms) >= 0:
                    print("Wi-Fi: Attempting to reconnect...")
                    if self._init_wifi():
                        print("Wi-Fi: Reconnected.")
                        self._wifi_reconnect_attempts = 0
                        self._next_mqtt_reconnect_ms = now # Try MQTT connect right away
                        self._init_get_ntp_time()
                    else:
                        self._wifi_reconnect_attempts += 1
                        delay = self._get_backoff_delay_ms(self._wifi_reconnect_attempts)
                        self._next_wifi_reconnect_ms = time.ticks_add(now, delay)
                        print(f"Wi-Fi: Reconnect failed. Retrying in {delay//1000}s.")
                 
            # 2. Check MQTT status (only if Wi-Fi is up)
            elif not self.mqtt:
                if time.ticks_diff(now, self._next_mqtt_reconnect_ms) >= 0:
                    print("MQTT: Attempting to connect...")
                    self._init_mqtt()
                    if not self.mqtt:
                        self._mqtt_reconnect_attempts += 1
                        delay = self._get_backoff_delay_ms(self._mqtt_reconnect_attempts)
                        self._next_mqtt_reconnect_ms = time.ticks_add(now, delay)
                        print(f"MQTT: Connection failed. Retrying in {delay//1000}s.")

            # 3. Pump MQTT messages if connected
            if self.mqtt:
                try:
                    self.mqtt.check_msg()
                except Exception as e:
                    print(f"MQTT: check_msg error: {e}. Disconnecting.")
                    try: self.mqtt.disconnect()
                    except: pass
                    self.mqtt = None
                    self._next_mqtt_reconnect_ms = now # Schedule immediate reconnect attempt

            # --- Core Prop Logic ---
            self._action_failsafe(now)

            # Check and process current action status
            self._check_action_status(now)
            self._check_cooldown(now)

            # Process triggers (PIR sensor)
            self._process_triggers(now)

            # Periodic telemetry
            if self.mqtt and time.ticks_diff(now, self._last_tel) > TELEMETRY_INTERVAL_MS:
                self._telemetry()
                self._last_tel = now
                print ("Telemetry sent.")

            self.render_display()

# ----------------------------
# Entrypoint
# ----------------------------
def main():
    """Instantiates and runs the prop; on an unrecoverable error, log and reboot."""
    debug = secrets.DEBUG if hasattr(secrets, "DEBUG") else False
    prop = CoffinProp(debug=debug)
    try:
        prop.run()
    except Exception as e:   # KeyboardInterrupt (Ctrl-C from mpremote) is not caught on purpose
        prop._solenoid_off()
        prop.log_error("fatal", e)
        time.sleep(2)
        machine.reset()

# Entry
if __name__ == "__main__":
    # Run real firmware loop on MicroPython; otherwise execute lightweight
    # helper checks suitable for CPython host environments.
    import sys
    impl = getattr(sys, "implementation", None)
    name = getattr(impl, "name", "") if impl is not None else ""
    if name == "micropython":
        main()
    else:
        # CPython-only quick unit-style checks for pure helpers.
        print("Running helper checks (host CPython)...")

        # wrap() behavior
        assert wrap("hello world test", 6) == ["hello", "world", "test"]

        # draw_center() with a dummy OLED
        class DummyOLED:
            def __init__(self):
                self.calls = []
            def text(self, text, x, y):
                self.calls.append((text, x, y))
            def fill(self, v):
                pass
            def show(self):
                pass

        d = DummyOLED()
        draw_center(d, "OK", 10)
        assert any(c[0] == "OK" for c in d.calls), "draw_center did not call text()"

        # _publish() serialisation test
        class DummyMQ:
            def __init__(self):
                self.publ = []
            def publish(self, topic, payload, retain=False, qos=0):
                self.publ.append((topic, payload, retain, qos))

        prop = CoffinProp(debug=False)
        prop.mqtt = DummyMQ()
        prop._publish(b'topic', {'a': 1}, retain=True)
        assert prop.mqtt.publ, "_publish did not invoke underlying publish()"
        payload = prop.mqtt.publ[0][1]
        assert isinstance(payload, (bytes, bytearray)) and payload.startswith(b'{') and b'"a"' in payload

        # ensure_lib should at least import stdlib json on CPython
        mod = ensure_lib('json')
        import json as _json
        assert mod is _json

        print("All helper checks passed.")
