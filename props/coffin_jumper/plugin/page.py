"""Coffin Jumper plugin (class-based)

The ESP32 publishes one JSON telemetry object on <base>/telemetry and takes
commands as {"action": ..., "params": {...}} on <base>/cmd. Volumes are the
DFPlayer scale 0-30.
"""
import json

from dash import dcc, html, Input, Output, State, no_update

from plugin_base import PropPlugin


class Plugin(PropPlugin):
    name = "Coffin Jumper"
    PROP_ID = "esp32-coffin-jumper-01"
    PREFIX = "cj"
    ACCENT = "#ffb547"
    ACKS = True  # firmware answers each command on <base>/ack
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

        def set_volume(vol):
            # The slider is also moved to mirror the prop; only send real changes
            if vol is None or vol == self.cache.get(self._key("telem"), {}).get("vol"):
                return None
            return {"action": "set_volume", "params": {"volume": int(vol)}}

        timing_inputs = [
            (self.cid("action-min-s"), "value"),
            (self.cid("action-max-s"), "value"),
            (self.cid("action-lockout-s"), "value"),
            (self.cid("solenoid-delay-s"), "value"),
        ]

        def set_action_timing(minimum, maximum, lockout, delay):
            if any(value is None for value in (minimum, maximum, lockout, delay)):
                return None
            return {
                "action": "set_action_timing",
                "params": {
                    "action_min_ms": int(round(float(minimum) * 1000)),
                    "action_max_ms": int(round(float(maximum) * 1000)),
                    "action_lockout_ms": int(round(float(lockout) * 1000)),
                    "solenoid_delay_ms": int(round(float(delay) * 1000)),
                },
            }

        timing_input_style = {
            "width": "6ch",
            "minWidth": "6ch",
            "maxWidth": "6ch",
            "flex": "0 0 6ch",
            "boxSizing": "border-box",
        }

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
                     self.volume_slider(self.cid("volume"), set_volume,
                                        min=0, max=30, step=1, value=20),
                     dcc.Store(id=self.cid("volume-seen"), storage_type="memory")),
            self.row("Action timing",
                     html.Label("Min on (s)"),
                     dcc.Input(id=self.cid("action-min-s"), type="number", min=0, max=600,
                               step=1, value=5, debounce=True, style=timing_input_style),
                     html.Label("Max (s)"),
                     dcc.Input(id=self.cid("action-max-s"), type="number", min=1, max=600,
                               step=1, value=60, debounce=True, style=timing_input_style),
                     html.Label("Lockout (s)"),
                     dcc.Input(id=self.cid("action-lockout-s"), type="number", min=0, max=600,
                               step=1, value=7, debounce=True, style=timing_input_style),
                     html.Label("Solenoid delay (s)"),
                     dcc.Input(id=self.cid("solenoid-delay-s"), type="number", min=0, max=10,
                               step=0.1, value=0, debounce=True, style=timing_input_style),
                     self.button("Apply", self.cid("action-timing-apply"), set_action_timing,
                                 state=timing_inputs),
                     dcc.Store(id=self.cid("action-timing-loaded"), storage_type="memory")),
        ]

    def on_register(self, app, services):
        super().on_register(app, services)
        if not self._tick:
            return

        @app.callback(
            Output(self.cid("action-min-s"), "value"),
            Output(self.cid("action-max-s"), "value"),
            Output(self.cid("action-lockout-s"), "value"),
            Output(self.cid("solenoid-delay-s"), "value"),
            Output(self.cid("action-timing-loaded"), "data"),
            Input(self._tick, "n_intervals"),
            State(self.cid("action-timing-loaded"), "data"),
            prevent_initial_call=True,
        )
        def load_action_timing(_, loaded):
            if loaded:
                return no_update, no_update, no_update, no_update, no_update
            telemetry = self.cache.get(self._key("telem"), {})
            keys = ("action_min_ms", "action_max_ms", "action_lockout_ms")
            if not all(key in telemetry for key in keys):
                return no_update, no_update, no_update, no_update, no_update
            values = [round(int(telemetry[key]) / 1000, 1) for key in keys]
            values.append(round(int(telemetry.get("solenoid_delay_ms", 0)) / 1000, 1))
            return tuple(values) + (True,)

        # Follow the volume the prop reports (it resets on a restart); only act when
        # the reported value changes, so a drag in progress is not overridden.
        @app.callback(
            Output(self.cid("volume"), "value"),
            Output(self.cid("volume-seen"), "data"),
            Input(self._tick, "n_intervals"),
            State(self.cid("volume"), "value"),
            State(self.cid("volume-seen"), "data"),
            prevent_initial_call=True,
        )
        def sync_volume(_, current, seen):
            reported = self.cache.get(self._key("telem"), {}).get("vol")
            if not isinstance(reported, (int, float)) or reported == seen:
                return no_update, no_update
            return (no_update if reported == current else int(reported)), reported
