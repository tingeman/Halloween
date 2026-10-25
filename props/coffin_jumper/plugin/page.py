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
    STOP_COMMANDS = [{"action": "stop_action"}, {"action": "stop_music"}]

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
            self.row("Action",
                     self.button("Trigger", self.cid("trigger"), with_volume("trigger"), "danger",
                                 confirm=True, state=volume),
                     self.button("Stop Action", self.cid("stop-action"), {"action": "stop_action"}),
                     self.button("Block", self.cid("block"), {"action": "block"}, "warning"),
                     self.button("Unblock", self.cid("unblock"), {"action": "unblock"}),
                     self.button("Reset", self.cid("reset"), with_volume("reset"), state=volume)),
            self.row("Music",
                     self.button("Play", self.cid("play"), with_volume("play_music"), state=volume),
                     self.button("Stop playing", self.cid("stop"), {"action": "stop_music"}),
                     self.volume_slider(self.cid("volume"), with_volume("set_volume"),
                                        min=0, max=30, step=1, value=20)),
        ]
