# server/dashboard/builtin_plugins/global_controls.py
"""
Global controls in the top bar of both dashboard views.

- Volume limit: publishes the retained halloween/broadcast/volume_limit.
  Every prop caps its speakers at limit * its own default volume; 100% means
  no limit. The broker keeps the value across restarts.
- All arm / All stop: send each loaded prop plugin's ARM_COMMANDS / STOP_COMMANDS
  to its command topic. All stop asks for a confirming second click.
"""
import json

from dash import html, Input, Output, State, no_update

from halloween_common import volume_limit
from plugin_base import BasePlugin, CommandUI

PREFIX = "global"


class Plugin(CommandUI, BasePlugin):
    name = "Global Controls"
    zone = "topbar"

    T_LIMIT = volume_limit.TOPIC

    def layout(self):
        self._ui_controls = {}
        return html.Div(className="hw-global d-flex align-items-center gap-2", children=[
            html.Div(className="hw-limit d-flex align-items-center gap-2", style={"minWidth": "230px"}, children=[
                html.Span("Vol limit", className="hw-limit-title small"),
                html.Div(style={"flex": "1 1 auto"}, children=self.volume_slider(
                    f"{PREFIX}-volume-limit", self._limit_payload, min=0, max=100, step=5, value=100,
                    label="", topic=self.T_LIMIT, retain=True)),
                html.Span("off", id=f"{PREFIX}-volume-limit-label", className="hw-limit-label badge bg-secondary"),
            ]),
            self.button("All arm", f"{PREFIX}-all-arm", self._all_arm, "warning",
                        title="Arm every prop"),
            self.button("All stop", f"{PREFIX}-all-stop", self._all_stop, "danger", confirm=True,
                        title="Stop every prop (asks for a second click)"),
        ])

    def _all_arm(self):
        return self._send_all("ARM_COMMANDS")

    def _all_stop(self):
        return self._send_all("STOP_COMMANDS")

    def _limit_payload(self, percent):
        scale = float(percent) / 100.0
        # The slider is also moved to mirror the broker (page load, other
        # browsers); only publish real changes, every publish makes each
        # worker re-apply the limit to its speakers.
        if abs(scale - self.cache.get("global_volume_limit", volume_limit.NO_LIMIT)) < 1e-6:
            return None
        return volume_limit.encode(scale)

    def _send_all(self, attr):
        for p in self._services.get("plugins", []):
            inst = p.get("instance")
            cmds = getattr(inst, attr, None)
            topic = getattr(inst, "T_CMD", None)
            if not cmds or not isinstance(topic, str):
                continue
            for cmd in cmds:
                self.mqtt_publish(topic, cmd if isinstance(cmd, str) else json.dumps(cmd), qos=1)
        return None  # everything already sent

    def on_register(self, app, services):
        self.mqtt_subscribe(self.T_LIMIT, lambda t, p: self.cache.set("global_volume_limit", volume_limit.parse(p)))
        self.register_controls(app)
        slider = f"{PREFIX}-volume-limit"

        # The slider is published retained, so mirror what the broker has
        @app.callback(
            Output(f"{PREFIX}-volume-limit-label", "children"),
            Output(f"{PREFIX}-volume-limit-label", "className"),
            Output(slider, "value"),
            Input(self._tick, "n_intervals"),
            State(slider, "value"),
        )
        def _render(_, current):
            scale = self.cache.get("global_volume_limit", volume_limit.NO_LIMIT)
            percent = round(scale * 100)
            cls = "hw-limit-label badge " + ("bg-secondary" if scale >= 1 else "bg-warning text-dark")
            return volume_limit.label(scale), cls, (no_update if current == percent else percent)
