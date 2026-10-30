# Coffin Jumper

An ESP32 (MicroPython) prop. When a PIR sensor sees motion, or a remote
trigger arrives over MQTT, it plays a sound from a DFPlayer Mini and fires a
solenoid (via a MOSFET) that makes something "jump" in the coffin. It reports
its status to the broker and can be controlled from the dashboard.

There is **no backend worker** for this prop. All logic runs on the ESP32; the
dashboard plugin ([../plugin/page.py](../plugin/page.py)) only shows state and
publishes commands. The prop is therefore independent of `worker_host` and
`WORKER_PROPS_ALLOW`.

```text
 PIR ──► ESP32 ──UART──► DFPlayer Mini ──► speaker
            │ └─GPIO──► MOSFET ──► solenoid
            │ └─I2C───► SSD1306 OLED
            └── Wi-Fi ──► MQTT broker ◄── Dashboard plugin
```

## Hardware and pins

| Function | Pin | Notes |
|---|---|---|
| PIR (HC-SR501) output | GPIO 4 | 3.3 V logic only; internal pull-down; rising-edge IRQ |
| Solenoid MOSFET gate | GPIO 2 | HIGH = solenoid on; forced LOW at boot |
| DFPlayer UART (UART 2) | TX GPIO 17, RX GPIO 16 | 9600 baud; ESP32 TX -> DFPlayer RX |
| OLED SSD1306 (I2C) | SDA GPIO 21, SCL GPIO 22 | 128x64, address 0x3C |

Full wiring, parts list and schematic: [../hardware/WIRING.md](../hardware/WIRING.md).

Constants are at the top of [main.py](main.py): pins, OLED size, timings and
the timezone offset (`TZ_OFFSET_HOURS`; adjust for DST).

## Files

| File | Purpose |
|---|---|
| [main.py](main.py) | `CoffinProp` class and main loop |
| [dfplayer.py](dfplayer.py) | DFPlayer Mini UART driver (manual install; not available via `mip`) |
| [pir_hcsr501.py](pir_hcsr501.py) | `PIRLatch`: IRQ-based PIR with warm-up, debounce and lockout |
| [boot.py](boot.py) | Runs `gc.collect()` at boot |
| `secrets.py` | Wi-Fi and MQTT credentials (git-ignored; see `secrets.py.example`) |
| [copy_to_esp32.ps1](copy_to_esp32.ps1) | Copies the `.py` files to the board |

`ssd1306` is installed on first boot via `mip` if missing (needs network).
`umqtt.simple` and `ntptime` are expected in the MicroPython build.

## Behavior

### Startup
1. Initialize peripherals: PIR, OLED, DFPlayer (volume 20), solenoid pin (LOW).
   A failed peripheral is logged and skipped; the prop keeps running.
2. Show a splash screen, connect to Wi-Fi (20 s timeout), sync the clock via NTP.
3. Show the IP address, connect to MQTT, subscribe to `cmd` and
   `halloween/broker/uptime`, publish `availability=online`, then state `armed`.

### Action (the "jump")
Started by an accepted trigger:
1. Play track 1 from the DFPlayer root (`ACTION_FOLDER=0`, `ACTION_TRACK=1`).
2. Set the solenoid pin HIGH.
3. State becomes `action`.

The action ends when the DFPlayer stops playing and the configured minimum
solenoid-on time has elapsed. Then the solenoid goes LOW, the player is
stopped, and the state returns to `armed` (or `stopped`). The configured
maximum duration is a failsafe; `stop_action` ends the action early.

The solenoid stays on through the sound track, subject to the configured
minimum-on time and maximum-duration failsafe.

### Triggers
PIR motion and the MQTT `trigger` command go through one function,
`request_trigger()`, so they obey the same rules. A trigger is **rejected** when:
- an action ran less than the configured `ACTION_LOCKOUT_MS` (7 s by default) ago,
- the state is already `action`.

Each started action increments the persistent play counter. Rejected triggers
do not. The PIR is silenced for `ACTION_LOCKOUT_MS` to prevent re-triggering.
When blocked, the sound still plays, but the solenoid is not activated.

PIR handling ([pir_hcsr501.py](pir_hcsr501.py)): rising-edge IRQ, ignored for
`PIR_WARMUP_MS` (5 s) after boot, edges closer than `PIR_DEBOUNCE_MS` (300 ms)
are dropped, and `pending()` returns True once per motion edge.

### States
Published (retained) to `state` and shown on the OLED:

| State | Meaning |
|---|---|
| `booting` | Initial, before MQTT |
| `idle` | Just announced online |
| `armed` | Waiting for a trigger |
| `playing` | Music playing outside of an action (`play_music`) |
| `action` | Jump in progress (sound plus solenoid) |
| `IO Error` | DFPlayer status check failed |

### Connectivity
- Availability: `online` retained on connect; MQTT last-will sets `offline`.
- Wi-Fi loss disconnects MQTT and reconnects with exponential backoff
  (2 s doubling, capped at 5 min). MQTT reconnects the same way.
- Telemetry every 5 s (not retained).
- The loop runs every 100 ms; the PIR and local logic keep working offline.

### OLED
Shows firmware version, state, persistent play count, PIR (`Motion!`/`---`), blocked
flag and broker uptime (from `halloween/broker/uptime`).

## MQTT interface

Base topic: `halloween/esp32-coffin-jumper-01` (hard-coded in `main.py` as
`BASE`; the dashboard uses the same string).

| Topic | Direction | Payload |
|---|---|---|
| `<base>/availability` | device -> | `online` / `offline` (retained, LWT) |
| `<base>/state` | device -> | state name (retained) |
| `<base>/telemetry` | device -> | JSON, every 5 s |
| `<base>/telemetry/playback/PlayCount` | device -> | Persistent play count (retained) |
| `<base>/cmd` | -> device | plain text or JSON (below) |
| `halloween/broker/uptime` | -> device | JSON with `uptime_s` |

Telemetry fields: `fw`, `uptime_s`, `play_count`, `pir`, `blocked`, `vol`,
`rssi`, `solenoid_pin_state`, `solenoid_power`. (`relay` and `relay_pin_state`
are legacy placeholders and always `---`.)

### Commands
Send either a bare word (`trigger`) or JSON `{"action": "...", "params": {...}}`.

| Action | Params | Effect |
|---|---|---|
| `trigger` | | Start an action (subject to the trigger rules above) |
| `stop_action` | | End a running action |
| `block` / `unblock` | | Prevent / allow solenoid activation; sound and triggers continue |
| `arm` | | Arm motion triggers |
| `reset_counter` | | Zero the persistent play counter |
| `play_music` | `volume` (0-30), `track` (default 1) | Play a track from the root |
| `set_volume` | `volume` (0-30) | Set DFPlayer volume |
| `set_action_timing` | `action_min_ms`, `action_max_ms`, `action_lockout_ms`, `solenoid_delay_ms` | Update the action timing values |
| `pause_music` / `resume_music` | | Pause or resume playback |
| `stop_music` | | Stop playback |

The dashboard's Action timing fields are shown in seconds and send all four
values as milliseconds. Minimum, maximum, and lockout fields use whole-second
steps; solenoid delay uses 0.1-second steps. The firmware accepts action timing
values from 0 to 600000 ms, requires `action_max_ms` to be greater than zero
and at least `action_min_ms`, and limits `solenoid_delay_ms` to 0..10000 ms.
Accepted values are saved in `action_timing.json` so they survive a reboot. If
no valid saved configuration exists, it uses the firmware defaults (minimum
5000 ms, maximum 60000 ms, lockout 7000 ms, and no solenoid delay). Current
settings are included in telemetry.

Commands may carry an `"id"`; the firmware then answers on `<base>/ack` with
`{"id", "action", "ok", "reason"}` (a repeated id is acknowledged as
`duplicate` and not run again). `cmd` is subscribed at QoS 1. After every
command the firmware publishes telemetry (so `vol` and the timing values are
current) and the retained `<base>/state/blocked` flag (`1`/`0`). The dashboard
slider follows the reported `vol`, and shows rejected/unanswered commands next
to the badges. `args` is the shared command key (same as the worker props);
`params` is still accepted.

Quick test (uses the broker container's admin credentials from its environment):

```powershell
docker exec halloween_mqtt sh -c 'mosquitto_pub -h localhost -u "$MQTT_ADMIN_USER" -P "$MQTT_ADMIN_PW" -t halloween/esp32-coffin-jumper-01/cmd -m trigger'
```

## Dashboard

The widget ([../plugin/page.py](../plugin/page.py)) shows availability and
state badges and telemetry, with buttons for Trigger, Stop Action,
Block/Unblock, Reset, Play, Stop playing and Set Volume (volume input). Its
Action timing fields configure the minimum solenoid-on time, maximum action
duration, retrigger lockout, and delay between the DFPlayer play command and
solenoid activation.
Grey/offline means no `availability=online` has been received.

## Safeguards and logging

- **Hardware watchdog** (60 s): the board reboots if the main loop stalls.
- **Guarded loop**: an exception in one iteration is logged and the solenoid is forced off. After 10 consecutive errors the board logs and reboots.
- **Action failsafe**: an action is force-stopped after the configured maximum duration (60 s by default), so the solenoid cannot stay on if the DFPlayer reports "playing" forever.
- **Crash log**: errors are appended to `crash.log` on the board (capped at about 4 KB). Read it with `mpremote connect COM8 fs cat crash.log`.
- **MQTT**: errors are published to `<base>/log`. A crash log left from the previous boot is published once after reconnecting, then deleted. Telemetry includes `reset_cause` and `loop_errors`.
- **After using `mpremote`**: connecting interrupts the running program and does not restart it, so the prop goes offline. Run `mpremote connect COM8 reset` (or power-cycle) afterwards, and don't leave a REPL open.

## Known limitations
- Hard-coded base topic and client id mean one device per broker; a second
  coffin needs `BASE` and `secrets.CLIENT_ID` changed (and the plugin topics).
- The dashboard's volume field is sent with `play_music` and `set_volume`; the
  firmware ignores `params` for `trigger` and `reset_counter`.
- The solenoid has no timeout of its own; it stays on until the sound ends or
  `stop_action`/`block` runs while unblocked. If the DFPlayer never reports playing (for
  example no SD card or a missing track 1), the action ends on the next loop.
- `unblock`/`arm` return to `armed` even if a track is playing.
- The minimum-on setting keeps the solenoid on after an early track end until
  the minimum duration has elapsed. The maximum setting can stop the solenoid
  before a longer track finishes.

## Setup and deployment

1. Create `secrets.py` next to `main.py` (copy `secrets.py.example`):

   ```python
   WIFI_SSID     = "MySSID"
   WIFI_PASSWORD = "..."
   MQTT_HOST     = "10.67.1.115"      # IP of the PC running the broker
   MQTT_PORT     = 1883
   MQTT_USER     = "device"
   MQTT_PASSWORD = "..."              # must match the broker's `device` user
   CLIENT_ID     = "esp32-coffin-jumper-01"
   # DEBUG = True                     # optional: log MQTT traffic
   ```

   `MQTT_HOST` changes with the network and PC. Keep one block per site and
   comment out the inactive one.

2. Install `mpremote` (for example `pip install mpremote` in the
   `halloween2026` conda env) and find the board:

   ```powershell
   mpremote connect list
   ```

   A CH340 board shows as `1a86:7523`. Use that COM port below (COM8 here).

3. Copy the files. From this folder:

   ```powershell
   .\copy_to_esp32.ps1 COM8
   ```

   Or one file: `mpremote connect COM8 fs cp secrets.py :`

4. Reset the board, then watch the console:

   ```powershell
   mpremote connect COM8 reset
   mpremote connect COM8 repl
   ```

   Expected: `Wi-Fi: connected`, `MQTT: Connected and configured`, then
   `Telemetry sent.` every 5 s. The dashboard widget should show online.

If the board never appears on the broker, the Windows firewall may be blocking
inbound TCP 1883 (see the repository README, Troubleshooting).

Tips:
- `mpremote` can't open a port that is in use (REPL open, Thonny, another
  `mpremote`). Close the other session first.
- Run without writing to flash: `mpremote connect COM8 run main.py`.
- Remove a file: `mpremote connect COM8 fs rm main.py`.
- `python main.py` on a desktop only runs small helper self-checks (`wrap`,
  `draw_center`, `_publish`); it does not touch hardware.

## Controls and states (2026)

Same four controls as the other props, plus a safety lock:

| Command | Effect | State |
|---------|--------|-------|
| `arm` | Enable motion triggers; ends a paused action | `armed` |
| `trigger` | Fire the action by hand (also when stopped); resumes a paused action | `action` |
| `pause` | Hold a running action: solenoid off, music paused, motion ignored | `paused` |
| `stop` | Disarm: end the action and music, ignore motion until `arm` | `stopped` |
| `block` | Inhibit solenoid activation; trigger sounds still play | state unchanged; `state/blocked` = `1` |
| `unblock` | Allow solenoid activation again | |
| `reset_counter` | Clear the persistent play counter without changing prop state | |

The dashboard's All stop sends `stop` (and `stop_music`). The board boots stopped (disarmed), so
motion is ignored until it is armed from the dashboard. A block is stored in `blocked.json` on the
board and survives reboots. The play counter is stored in `play_count.json` and is reset from the
JARVIS Settings panel's **Reset all play counters** button.
