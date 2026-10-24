"""
Tesla/Hue/Nest Worker Dashboard Plugin

Displays status and provides controls for the tesla_hue_nest worker.
"""
import json
from dash import html, dcc, Input, Output, State

# Import BasePlugin from the standalone plugin_base package
from plugin_base import BasePlugin


class Plugin(BasePlugin):
    """Class-based plugin for the Tesla/Hue/Nest worker."""

    name = "Tesla/Hue/Nest"
    zone = "card"
    path = "/tesla_hue_nest_worker"

    # MQTT Topics
    PROP_ID = "tesla_hue_nest"
    T_CMD = f"halloween/{PROP_ID}/cmd"
    T_AVAIL = f"halloween/{PROP_ID}/availability"
    T_STATE = f"halloween/{PROP_ID}/state"
    T_TELEMETRY = f"halloween/{PROP_ID}/telemetry/#"

    def layout(self):
        """Return the Dash layout for the plugin."""
        return html.Div([
            html.H4([
                self.name,
                html.Span(id="thn-availability", className="ms-2", children=[
                    html.Span("unknown", className="badge bg-secondary text-white")
                ]),
                html.Span(id="thn-state", className="ms-2", children=[
                    html.Span("unknown", className="badge bg-secondary text-white")
                ]),
            ]),

            # Main state controls
            html.Div(className="d-flex gap-2 mb-2", children=[
                html.Button("Arm", id="thn-arm", n_clicks=0, className="btn btn-primary"),
                html.Button("Play", id="thn-play", n_clicks=0, className="btn btn-success"),
                html.Button("Stop", id="thn-stop", n_clicks=0, className="btn btn-danger"),
                html.Button("Pause", id="thn-pause", n_clicks=0, className="btn btn-warning", disabled=True), # Placeholder
            ]),

            # Tesla controls
            html.Div(className="d-flex gap-2 mb-2", children=[
                html.Label("Tesla:"),
                html.Button("Open Trunk", id="thn-tesla-open-trunk", n_clicks=0),
                html.Button("Close Trunk", id="thn-tesla-close-trunk", n_clicks=0),
                html.Button("Resync Tesla", id="thn-tesla-resync", n_clicks=0,
                            title="Re-read the real trunk state from the car (may lock the doors)"),
            ]),

            # Hue controls
            html.Div(className="d-flex gap-2 mb-2", children=[
                html.Label("Hue:"),
                html.Button("Toggle Disco", id="thn-hue-disco", n_clicks=0),
                html.Button("(Re)Connect", id="thn-hue-connect", n_clicks=0),
            ]),

            # Chromecast/Nest controls
            html.Div(className="d-flex flex-wrap gap-2 mb-2 align-items-center", children=[
                html.Label("Nest:"),
                html.Button("Play", id="thn-cc-play", n_clicks=0),
                html.Button("Stop", id="thn-cc-stop", n_clicks=0),
                html.Button("Fade to Stop", id="thn-cc-fade", n_clicks=0),
                html.Button("Vol Up", id="thn-cc-vol-up", n_clicks=0),
                html.Button("Vol Down", id="thn-cc-vol-down", n_clicks=0),
                html.Button("(Re)Connect", id="thn-cc-connect", n_clicks=0),
                dcc.Input(id="thn-cc-volume-val", type="number", min=0, max=1, step=0.1, value=0.5, style={"width": "5em"}),
                html.Button("Set Vol", id="thn-cc-set-vol", n_clicks=0),
                html.Button("Log status", id="thn-cc-log", n_clicks=0),
            ]),

            # Telemetry display
            html.Div(id="thn-telem", className="small"),
        ])

    def on_register(self, app, services):
        """Register callbacks and MQTT subscriptions."""
        # Subscribe to MQTT topics
        self.mqtt_subscribe(self.T_AVAIL, self._on_avail)
        self.mqtt_subscribe(self.T_STATE, self._on_state)
        self.mqtt_subscribe(self.T_TELEMETRY, self._on_telem)

        # Register periodic render callbacks
        app.callback(Output("thn-availability", "children"), Input(self._tick, "n_intervals"))(self._render_avail)
        app.callback(Output("thn-state", "children"), Input(self._tick, "n_intervals"))(self._render_state)
        app.callback(Output("thn-telem", "children"), Input(self._tick, "n_intervals"))(self._render_telem)

        # Register button callbacks
        self._register_button(app, "thn-arm", "arm")
        self._register_button(app, "thn-play", "play")
        self._register_button(app, "thn-stop", "stop")

        # Tesla
        self._register_button(app, "thn-tesla-open-trunk", {"action": "tesla", "args": "open_trunk"})
        self._register_button(app, "thn-tesla-close-trunk", {"action": "tesla", "args": "close_trunk"})
        self._register_button(app, "thn-tesla-resync", {"action": "tesla", "args": "resync"})

        # Hue
        self._register_button(app, "thn-hue-disco", {"action": "hue", "args": "disco"})
        # Hue reconnect
        self._register_button(app, "thn-hue-connect", {"action": "hue", "args": "connect"})

        # Chromecast
        self._register_button(app, "thn-cc-play", {"action": "chromecast", "args": "play"})
        self._register_button(app, "thn-cc-stop", {"action": "chromecast", "args": "stop"})
        self._register_button(app, "thn-cc-fade", {"action": "chromecast", "args": "fade_to_stop"})
        self._register_button(app, "thn-cc-vol-up", {"action": "chromecast", "args": "volume_up"})
        self._register_button(app, "thn-cc-vol-down", {"action": "chromecast", "args": "volume_down"})
        self._register_button(app, "thn-cc-connect", {"action": "chromecast", "args": "connect"})

        @app.callback(
            Output("thn-cc-set-vol", "n_clicks"),
            Input("thn-cc-set-vol", "n_clicks"),
            State("thn-cc-volume-val", "value"),
            prevent_initial_call=True,
        )
        def _set_volume(_, volume):
            if volume is not None:
                self.mqtt_publish(self.T_CMD, json.dumps({"action": "chromecast", "args": {"volume": float(volume)}}))
            return 0

    def _register_button(self, app, button_id, command):
        """Helper to register a simple button callback that sends an MQTT command."""
        @app.callback(
            Output(button_id, "n_clicks"),
            Input(button_id, "n_clicks"),
            prevent_initial_call=True,
        )
        def _handle_click(_):
            payload = command if isinstance(command, str) else json.dumps(command)
            self.mqtt_publish(self.T_CMD, payload)
            return 0

    # --- MQTT Handlers ---
    def _on_avail(self, topic, payload: bytes):
        self.cache["thn_avail"] = payload.decode("utf-8", "replace")

    def _on_state(self, topic, payload: bytes):
        self.cache["thn_state"] = payload.decode("utf-8", "replace")

    def _on_telem(self, topic, payload: bytes):
        # Parse topic: halloween/tesla_hue_nest/telemetry/category/key or halloween/tesla_hue_nest/telemetry/key
        parts = topic.split("/")
        value = payload.decode("utf-8", "replace")
        
        with self.cache.locked() as backing:
            if "thn_telem" not in backing:
                backing["thn_telem"] = {}
            
            # If topic has category/key structure (e.g., hue/Scene, speakers/Status)
            if len(parts) >= 5 and "/" in parts[-1]:
                # Already has category prefix in the key
                full_key = "/".join(parts[4:])  # Everything after telemetry/
                backing["thn_telem"][full_key] = value
            elif len(parts) >= 5:
                # Multi-part: category/key
                category = parts[-2] if len(parts) > 4 else ""
                key = parts[-1]
                if category:
                    full_key = f"{category}/{key}"
                else:
                    full_key = key
                backing["thn_telem"][full_key] = value
            else:
                # Single part key (e.g., tick)
                key = parts[-1]
                backing["thn_telem"][key] = value

    # --- Render Callbacks ---
    def _render_avail(self, _):
        """Render the availability badge."""
        avail = (self.cache.get("thn_avail") or "unknown").lower()
        if avail == "online":
            return html.Span("online", className="badge bg-success text-white")
        return html.Span(avail, className="badge bg-danger text-white")

    def _render_state(self, _):
        """Render the state badge."""
        state = (self.cache.get("thn_state") or "unknown").lower()
        color = "secondary"
        if state in ("armed", "waiting"):
            color = "success"
        elif state in ("playing", "arming"):
            color = "info"
        elif state in ("stopped", "cooldown", "fadeout"):
            color = "warning"
        elif state == "error":
            color = "danger"
        return html.Span(state, className=f"badge bg-{color} text-white")

    def _render_telem(self, _):
        """Render the telemetry table grouped by category."""
        telem = self.cache.get("thn_telem", {})
        if not telem:
            return html.Div("telemetry: —")
        
        # Group telemetry by category
        categories = {
            "tesla": {},
            "hue": {},
            "speakers": {},
            "other": {}
        }
        
        for key, value in telem.items():
            if "/" in key:
                category, subkey = key.split("/", 1)
                if category in categories:
                    categories[category][subkey] = value
                else:
                    categories["other"][key] = value
            else:
                categories["other"][key] = value
        
        # Build single table with rowspan for category labels
        rows = []
        
        # Tesla section
        if categories["tesla"]:
            items = sorted(categories["tesla"].items())
            for i, (key, value) in enumerate(items):
                if i == 0:
                    rows.append(html.Tr([
                        html.Td(html.Strong("Tesla:"), rowSpan=len(items), className="align-top", style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})
                    ]))
                else:
                    rows.append(html.Tr([html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}), html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})]))
        
        # Hue section
        if categories["hue"]:
            items = sorted(categories["hue"].items())
            for i, (key, value) in enumerate(items):
                if i == 0:
                    rows.append(html.Tr([
                        html.Td(html.Strong("Hue:"), rowSpan=len(items), className="align-top", style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})
                    ]))
                else:
                    rows.append(html.Tr([html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}), html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})]))
        
        # Speakers section
        if categories["speakers"]:
            items = sorted(categories["speakers"].items())
            for i, (key, value) in enumerate(items):
                if i == 0:
                    rows.append(html.Tr([
                        html.Td(html.Strong("Speakers:"), rowSpan=len(items), className="align-top", style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})
                    ]))
                else:
                    rows.append(html.Tr([html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}), html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})]))
        
        # Other section
        if categories["other"]:
            items = sorted(categories["other"].items())
            for i, (key, value) in enumerate(items):
                if i == 0:
                    rows.append(html.Tr([
                        html.Td(html.Strong("Other:"), rowSpan=len(items), className="align-top", style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}),
                        html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})
                    ]))
                else:
                    rows.append(html.Tr([html.Td(key, style={"width": "1%", "whiteSpace": "nowrap"}), html.Td(value, style={"wordBreak": "break-word", "maxWidth": "0"})]))
        
        if not rows:
            return html.Div("telemetry: —")
        
        return html.Table(className="table table-sm mb-0", style={"tableLayout": "auto"}, children=[html.Tbody(rows)])
