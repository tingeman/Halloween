# dungeon_nest

Dungeon exhibition soundscape. The motion prop in the dungeon runs on a constant
drive motor, so only the sound is controlled.

## Behaviour
States: `stopped` (ignores motion), `arming` (connecting speakers), `armed` (waiting for motion),
`playing`, `paused`, `fadeout`, `cooldown`.
`AUTO_ARM: true` starts the worker armed; the default is `false`, so it starts stopped until armed.

- Motion on any of the Hue sensors (same sensors as `thriller_hue_nest`) while armed
  starts `media/Halloween2023 - dungeon soundscape.mp3` on the dungeon Nest speaker, looping.
- While playing, every motion reading pushes the stop deadline `PLAY_DURATION`
  (300 s) into the future, so a retrigger resets the timeout.
- When the deadline passes the sound fades out (5 s), then the prop waits in `cooldown`
  for `WAIT_BETWEEN_PLAYS` (30 s) and returns to `armed` (or `stopped` if the play was
  started manually from `stopped`). Motion is ignored during the cooldown, but a manual Play
  is accepted and skips the rest of it.
- If the track ends before the deadline it is restarted.
- Nothing else is switched (no lights, no motor).

## Settings (`backend/config.yml`)
| Key | Meaning |
|---|---|
| `PLAY_DURATION` | Seconds of sound after the last motion |
| `WAIT_BETWEEN_PLAYS` | Cooldown seconds after a play before re-arming |
| `SENSOR_POLL_INTERVAL` | Seconds between sensor polls |
| `HUE_BRIDGE_IP`, `MOTION_SENSORS` | Hue bridge and sensors (names as in the Hue app) |
| `MEDIA_BASE_URL` | Set centrally in `config/worker_host.env` (media server as seen from the speaker) |
| `CHROMECAST_DUNGEON_GROUP` | Speaker `IP`, `volume` and `url` (bare file name or full URL) |

When the PC's IP changes, update `MEDIA_BASE_URL` in `config/worker_host.env` and recreate the worker host.

## Dashboard
State badge: `stopped`, `arming`, `armed`, `playing`, `paused`, `fadeout` or `cooldown`.
Prop buttons: Arm (react to motion), Play (start the 5 min looping action, or
resume when paused), Pause (pauses sound and freezes the timer), Stop (stops
sound and disarms). Nest buttons: Play (the track once, no timer), Stop, Fade to
Stop (end a running action, stay armed), volume, Set Vol, (Re)Connect
(reconnects the speaker and re-reads the Hue sensors).

## MQTT
Base `halloween/dungeon_nest/`: `cmd` (`arm`, `play`, `pause`, `stop`, or
`{"action":"chromecast","args":"play|stop|fade_to_stop|volume_up|volume_down|connect"}`,
`{"action":"chromecast","args":{"volume":0.5}}`), `state`,
`availability`, `telemetry/#`.

Enable via `WORKER_PROPS_ALLOW` in `config/worker_host.env`.
