"""Coffin Jumper plugin (class-based)

The ESP32 publishes one JSON telemetry object on <base>/telemetry and takes
commands as {"action": ..., "params": {...}} on <base>/cmd. Volumes are the
DFPlayer scale 0-30.
"""
import json

from plugin_base import PropPlugin


class Plugin(PropPlugin):
    name = "Coffin Jumper"
    PROP_ID = "esp32-coffin-jumper-01"
    PREFIX = "cj"
    ACCENT = "#ffb547"
    # All stop disarms it like the other props: "stop" ends the action and music
    STOP_COMMANDS = [{"action": "stop"}, {"action": "stop_music"}]

    @property
    def T_TELEMETRY(self):
        return f"{self.T_BASE}/telemetry"

    def parse_telemetry(self, topic, payload: bytes) -> dict:
        data = json.loads(payload.decode())
        return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {"telemetry": data}

    def controls(self):
        volume = [(self.cid("volume"), "value")]

        def with_volume(action):
            def build(vol):
                cmd = {"action": action}
                if vol is not None:
                    cmd["params"] = {"volume": int(vol)}
                return cmd
            return build

        return [
            # Same four controls as the other props (Trigger fires the solenoid, so it confirms)
            self.state_buttons(arm={"action": "arm"}, trigger=with_volume("trigger"),
                               pause={"action": "pause"}, stop={"action": "stop"},
                               trigger_state=volume, confirm_trigger=True),
            # Extra for the coffin: a safety lock that refuses every physical action
            self.row("Safety",
                     self.button("Block", self.cid("block"), {"action": "block"}, "warning",
                                 title="Lock: nothing moves or plays, from any source, until Unblock"),
                     self.button("Unblock", self.cid("unblock"), {"action": "unblock"},
                                 title="Lift the lock (back to armed or stopped)"),
                     self.button("Reset", self.cid("reset"), with_volume("reset"), state=volume,
                                 title="Clear the trigger count, unblock and arm")),
            self.row("Music",
                     self.button("Play", self.cid("music-play"), with_volume("play_music"), state=volume),
                     self.button("Stop", self.cid("music-stop"), {"action": "stop_music"}),
                     self.volume_slider(self.cid("volume"), with_volume("set_volume"),
                                        min=0, max=30, step=1, value=20)),
        ]
