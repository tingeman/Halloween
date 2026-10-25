# Dashboard roadmap

Branch: `develop/2026/agent-jarvis-dashboard`. Items 1-8 were approved by
Thomas on 2026-10-25 and are implemented; open points are at the bottom.

## Implemented

| # | Area | What was done |
|---|------|---------------|
| 1 | Tesla, Coffin Jumper | **Open Trunk**, **Close Trunk**, **Resync Tesla** and **Trigger** ask "Confirm?" and only send on a second click within 3 s (`confirm=True` on any button). |
| 2 | All props | **All stop** in the top bar of both views (double click). Sends each plugin's `STOP_COMMANDS` (`stop` by default; Coffin: `stop_action` + `stop_music`). Tesla's stop is its normal Stop, which also closes the trunk. |
| 3 | Nest props | **Log status** now sends `{"action": "chromecast", "args": "status"}`; Dungeon, Tesla and Thriller workers publish one `speakers/<name>` telemetry line per speaker (state, volume, cap). |
| 4 | Tesla, Thriller | **Pause** works (decided 2026-10-25: freeze the music, leave the trunk open). New `PausedState`: pauses the speakers, freezes the motion timeout, ignores motion, leaves trunk and lights alone. **Play** resumes the same show where it was (no new trunk or Hue commands, no new play count); **Stop** and fade work as usual. |
| 5 | Coffin Jumper | Explicit **Block** and **Unblock** buttons replace the cached-state toggle. |
| 6 | All Nest props + Coffin | Volume number box + Set Vol replaced by a slider that sends on release (Nest 0-1, Coffin 0-30). Vol −/+ kept beside it. |
| 7 | Dashboard | A prop that says online but sends nothing live (retained messages do not count) for `PROP_STALE_AFTER` s (60) shows **stale** (amber) in both views. |
| 8 | Broker ACL | Dashboard may read `halloween/+/cmd`; the Jarvis comms log shows commands from every source. |
| + | All props | **Global volume limit** (see below). |
| + | Jarvis | Flame theme (red/orange with flickering flame glow), Blue/Flame toggle in the ⚙ menu, `JARVIS_THEME` default. Focus (vertical): rail, the selected prop full height in the middle at Ops control sizes, small cards for the other props on the right with their main action row only. |
| + | Jarvis | `DASHBOARD_DEFAULT_VIEW`, `JARVIS_DEFAULT_LAYOUT`, **Kiosk** layout (key 4), per-prop accent colours (⚙ menu or `JARVIS_ACCENTS`), soundtrack (`JARVIS_SOUNDTRACK`). |

### Global volume limit

- Top-bar slider in both views, 0-100% in steps of 5. Published retained on
  `halloween/broadcast/volume_limit` as `{"scale": 0.3}`; the broker persists it.
- Rule (same for every prop, `libs/py/halloween_common/volume_limit.py`):
  100% = no limit; below that every speaker is capped at
  `scale × its own default volume`. Requests under the cap are kept; lifting
  the limit restores what was asked for.
- Nest speakers: enforced in the shared `halloween_common/chromecast.py`
  (the three identical `chromecast_API.py` copies now import it). Every
  volume path goes through the cap, and `refresh()`/`play()` pull a speaker
  back down if it was raised elsewhere (e.g. the Google Home app).
- Workers: `BaseWorker` subscribes to the limit and applies it to whatever
  `volume_targets()` returns. A new prop only needs `volume_targets()` and a
  call to `apply_volume_limit()` after it (re)connects its speakers.
- ESP32 coffin: firmware subscribes to the limit, caps the DFPlayer at
  `scale × DEFAULT_VOLUME` (20) and keeps the last value in
  `volume_limit.json` on flash, so it holds even if it boots without the broker.
- Each worker reports `volume/Limit` in its telemetry; the coffin reports
  `vol_limit` and `vol_effective`.

### Adding a prop plugin

Subclass `PropPlugin` (`server/dashboard/plugin_base/prop_plugin.py`), set
`PROP_ID`, `PREFIX`, optional `ACCENT` and `STOP_COMMANDS`, and return rows
from `controls()` built with `self.row()`, `self.button()`,
`self.volume_slider()`, `self.nest_controls()`, `self.hue_controls()` and
`self.state_buttons()`. Badges, stale detection, telemetry table, All stop,
accents and every Jarvis layout then work without further code.

## Needs you before it is live

- **Rebuild and restart**: dashboard image and worker host (shared library,
  new topics) and **restart the broker** so the new ACL loads.
- **Flash the coffin ESP32** (`props/coffin_jumper/firmware/main.py`) or it
  ignores the volume limit. Until then the dashboard still sends its volume.
- **Soundtrack file**: put the track in `media/` and set `JARVIS_SOUNDTRACK`.

## Open points

| Item | Question |
|------|----------|
| Volume slider position | Sliders start at 0.5 (Nest) / 20 (Coffin); they do not read the speaker's current volume. Could follow telemetry if workers publish volume. |
| Coffin "fires" line | The old "fires: —" line was removed: the firmware publishes `triggers`, which is in the telemetry table. |
