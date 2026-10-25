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

The action ends when the DFPlayer stops playing (polled every loop). Then the
solenoid goes LOW, the player is stopped, and the state returns to `armed`
(or `blocked`). `stop_action` ends it early.

So the solenoid is on for the length of the sound track.

### Triggers
PIR motion and the MQTT `trigger` command go through one function,
`request_trigger()`, so they obey the same rules. A trigger is **rejected** when:
- an action ran less than `ACTION_LOCKOUT_MS` (7 s) ago,
- the state is already `action`,
- the prop is blocked.

On acceptance the trigger counter increases and the PIR is silenced for
`ACTION_LOCKOUT_MS` to prevent re-triggering.

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
| `blocked` | Triggers rejected (PIR and remote) |
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
Shows firmware version, state, trigger count, PIR (`Motion!`/`---`), blocked
flag and broker uptime (from `halloween/broker/uptime`).

## MQTT interface

Base topic: `halloween/esp32-coffin-jumper-01` (hard-coded in `main.py` as
`BASE`; the dashboard uses the same string).

| Topic | Direction | Payload |
|---|---|---|
| `<base>/availability` | device -> | `online` / `offline` (retained, LWT) |
| `<base>/state` | device -> | state name (retained) |
| `<base>/telemetry` | device -> | JSON, every 5 s |
| `<base>/cmd` | -> device | plain text or JSON (below) |
| `halloween/broker/uptime` | -> device | JSON with `uptime_s` |

Telemetry fields: `fw`, `uptime_s`, `triggers`, `pir`, `blocked`, `vol`,
`rssi`, `solenoid_pin_state`, `solenoid_power`. (`relay` and `relay_pin_state`
are legacy placeholders and always `---`.)

### Commands
Send either a bare word (`trigger`) or JSON `{"action": "...", "params": {...}}`.

| Action | Params | Effect |
|---|---|---|
| `trigger` | | Start an action (subject to the trigger rules above) |
| `stop_action` | | End a running action |
| `block` / `unblock` | | Stop any action and reject triggers / allow them again |
| `arm` | | Set state `armed` if not blocked |
| `reset` | | Zero the trigger counter, unblock, set `armed` |
| `play_music` | `volume` (0-30), `track` (default 1) | Play a track from the root |
| `set_volume` | `volume` (0-30) | Set DFPlayer volume |
| `pause_music` / `resume_music` | | Pause or resume playback |
| `stop_music` | | Stop playback |

Quick test (uses the broker container's admin credentials from its environment):

```powershell
docker exec halloween_mqtt sh -c 'mosquitto_pub -h localhost -u "$MQTT_ADMIN_USER" -P "$MQTT_ADMIN_PW" -t halloween/esp32-coffin-jumper-01/cmd -m trigger'
```

## Dashboard

The widget ([../plugin/page.py](../plugin/page.py)) shows availability and
state badges and telemetry, with buttons for Trigger, Stop Action,
Block/Unblock, Reset, Play, Stop playing and Set Volume (volume input).
Grey/offline means no `availability=online` has been received.

## Safeguards and logging

- **Hardware watchdog** (60 s): the board reboots if the main loop stalls.
- **Guarded loop**: an exception in one iteration is logged and the solenoid is forced off. After 10 consecutive errors the board logs and reboots.
- **Action failsafe**: an action is force-stopped after 60 s, so the solenoid cannot stay on if the DFPlayer reports "playing" forever.
- **Crash log**: errors are appended to `crash.log` on the board (capped at about 4 KB). Read it with `mpremote connect COM8 fs cat crash.log`.
- **MQTT**: errors are published to `<base>/log`. A crash log left from the previous boot is published once after reconnecting, then deleted. Telemetry includes `reset_cause` and `loop_errors`.
- **After using `mpremote`**: connecting interrupts the running program and does not restart it, so the prop goes offline. Run `mpremote connect COM8 reset` (or power-cycle) afterwards, and don't leave a REPL open.

## Known limitations
- Hard-coded base topic and client id mean one device per broker; a second
  coffin needs `BASE` and `secrets.CLIENT_ID` changed (and the plugin topics).
- The dashboard's volume field is sent with `play_music` and `set_volume`; the
  firmware ignores `params` for `trigger` and `reset`.
- The solenoid has no timeout of its own; it stays on until the sound ends or
  `stop_action`/`block` runs. If the DFPlayer never reports playing (for
  example no SD card or a missing track 1), the action ends on the next loop.
- `unblock`/`arm` return to `armed` even if a track is playing.
- Changing the solenoid duration independent of the track length is not
  implemented.

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
