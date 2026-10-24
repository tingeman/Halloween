"""
Dungeon Nest Dashboard Plugin

Status and basic Nest controls for the dungeon_nest worker.
"""
import json
from dash import html, dcc, Input, Output, State

from plugin_base import BasePlugin


class Plugin(BasePlugin):
    name = "Dungeon Nest"
    zone = "card"
    path = "/dungeon_nest_worker"

    PROP_ID = "dungeon_nest"
    T_CMD = f"halloween/{PROP_ID}/cmd"
    T_AVAIL = f"halloween/{PROP_ID}/availability"
    T_STATE = f"halloween/{PROP_ID}/state"
    T_TELEMETRY = f"halloween/{PROP_ID}/telemetry/#"

    def layout(self):
        return html.Div([
            html.H4([
                self.name,
                html.Span(id="dungeon-availability", className="ms-2", children=[
                    html.Span("unknown", className="badge bg-secondary text-white")
                ]),
                html.Span(id="dungeon-state", className="ms-2", children=[
                    html.Span("unknown", className="badge bg-secondary text-white")
                ]),
            ]),

            html.Div(className="d-flex gap-2 mb-2", children=[
                html.Button("Arm", id="dungeon-arm", n_clicks=0, className="btn btn-primary"),
                html.Button("Play", id="dungeon-play", n_clicks=0, className="btn btn-success"),
                html.Button("Pause", id="dungeon-pause", n_clicks=0, className="btn btn-warning"),
                html.Button("Stop", id="dungeon-stop", n_clicks=0, className="btn btn-danger"),
            ]),

            html.Div(className="d-flex flex-wrap gap-2 mb-2 align-items-center", children=[
                html.Label("Nest:"),
                html.Button("Play", id="dungeon-cc-play", n_clicks=0),
                html.Button("Stop", id="dungeon-cc-stop", n_clicks=0),
                html.Button("Fade to Stop", id="dungeon-cc-fade", n_clicks=0),
                html.Button("Vol Up", id="dungeon-cc-vol-up", n_clicks=0),
                html.Button("Vol Down", id="dungeon-cc-vol-down", n_clicks=0),
                html.Button("(Re)Connect", id="dungeon-cc-connect", n_clicks=0),
                dcc.Input(id="dungeon-cc-volume-val", type="number", min=0, max=1, step=0.1, value=0.5, style={"width": "5em"}),
                html.Button("Set Vol", id="dungeon-cc-set-vol", n_clicks=0),
            ]),

            html.Div(id="dungeon-telem", className="small"),
        ])

    def on_register(self, app, services):
        self.mqtt_subscribe(self.T_AVAIL, self._on_avail)
        self.mqtt_subscribe(self.T_STATE, self._on_state)
        self.mqtt_subscribe(self.T_TELEMETRY, self._on_telem)

        app.callback(Output("dungeon-availability", "children"), Input(self._tick, "n_intervals"))(self._render_avail)
        app.callback(Output("dungeon-state", "children"), Input(self._tick, "n_intervals"))(self._render_state)
        app.callback(Output("dungeon-telem", "children"), Input(self._tick, "n_intervals"))(self._render_telem)

        self._register_button(app, "dungeon-arm", "arm")
        self._register_button(app, "dungeon-play", "play")
        self._register_button(app, "dungeon-pause", "pause")
        self._register_button(app, "dungeon-stop", "stop")
        self._register_button(app, "dungeon-cc-play", {"action": "chromecast", "args": "play"})
        self._register_button(app, "dungeon-cc-stop", {"action": "chromecast", "args": "stop"})
        self._register_button(app, "dungeon-cc-fade", {"action": "chromecast", "args": "fade_to_stop"})
        self._register_button(app, "dungeon-cc-vol-up", {"action": "chromecast", "args": "volume_up"})
        self._register_button(app, "dungeon-cc-vol-down", {"action": "chromecast", "args": "volume_down"})
        self._register_button(app, "dungeon-cc-connect", {"action": "chromecast", "args": "connect"})

        @app.callback(
            Output("dungeon-cc-set-vol", "n_clicks"),
            Input("dungeon-cc-set-vol", "n_clicks"),
            State("dungeon-cc-volume-val", "value"),
            prevent_initial_call=True,
        )
        def _set_volume(_, volume):
            if volume is not None:
                self.mqtt_publish(self.T_CMD, json.dumps({"action": "chromecast", "args": {"volume": float(volume)}}))
            return 0

    def _register_button(self, app, button_id, command):
        @app.callback(
            Output(button_id, "n_clicks"),
            Input(button_id, "n_clicks"),
            prevent_initial_call=True,
        )
        def _handle_click(_):
            payload = command if isinstance(command, str) else json.dumps(command)
            self.mqtt_publish(self.T_CMD, payload)
            return 0

    # --- MQTT handlers ---
    def _on_avail(self, topic, payload: bytes):
        self.cache["dungeon_avail"] = payload.decode("utf-8", "replace")

    def _on_state(self, topic, payload: bytes):
        self.cache["dungeon_state"] = payload.decode("utf-8", "replace")

    def _on_telem(self, topic, payload: bytes):
        # topic: halloween/dungeon_nest/telemetry/<key> or .../<category>/<key>
        key = "/".join(topic.split("/")[3:]).removeprefix("telemetry/")
        with self.cache.locked() as backing:
            backing.setdefault("dungeon_telem", {})[key] = payload.decode("utf-8", "replace")

    # --- Render ---
    def _render_avail(self, _):
        avail = (self.cache.get("dungeon_avail") or "unknown").lower()
        if avail == "online":
            return html.Span("online", className="badge bg-success text-white")
        return html.Span(avail, className="badge bg-danger text-white")

    def _render_state(self, _):
        state = (self.cache.get("dungeon_state") or "unknown").lower()
        color = {"armed": "success", "playing": "info", "paused": "warning", "stopped": "secondary", "error": "danger"}.get(state, "secondary")
        return html.Span(state, className=f"badge bg-{color} text-white")

    def _render_telem(self, _):
        telem = self.cache.get("dungeon_telem", {})
        if not telem:
            return html.Div("telemetry: —")
        cell = {"width": "1%", "whiteSpace": "nowrap"}
        rows = [html.Tr([html.Td(k, style=cell), html.Td(v)]) for k, v in sorted(telem.items())]
        return html.Table(className="table table-sm mb-0", children=[html.Tbody(rows)])
