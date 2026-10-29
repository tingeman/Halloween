"""
Declarative controls for dashboard plugins.

`CommandUI` is a mixin for BasePlugin subclasses. Building the layout with
its helpers records what each control does; `register_controls(app)` then
wires every recorded control in one go, so all props behave the same:

- button(...)         publishes a command on click; confirm=True asks
                      "Confirm?" first and only sends on a second click
                      within CONFIRM_SECONDS (for things that move).
- volume_slider(...)  publishes a command when the slider is released.
- row(...)            a labelled line of controls.

A command is a payload (str or dict, dicts are JSON-encoded) or a callable
taking the current values of the `state` controls and returning one.
Published topic: self.T_CMD unless the control says otherwise.
"""
import json

from dash import html, dcc, Input, Output, State, no_update

CONFIRM_SECONDS = 3.0
VARIANTS = ("primary", "success", "danger", "warning")


def _payload(command):
    return command if isinstance(command, str) else json.dumps(command)


class CommandUI:
    """Mixin: record controls while building the layout, wire them on register."""

    def _controls(self):
        if not hasattr(self, "_ui_controls"):
            self._ui_controls = {}
        return self._ui_controls

    # --- layout helpers -----------------------------------------------------
    def button(self, label, cid, command, variant=None, confirm=False, title=None,
               state=(), topic=None):
        """A command button. `state` lists (component_id, prop) passed to a callable command."""
        cls = f"btn btn-sm btn-{variant}" if variant in VARIANTS else "btn btn-sm btn-outline-secondary"
        self._controls()[cid] = {
            "kind": "button", "label": label, "command": command, "confirm": confirm,
            "state": list(state), "topic": topic, "className": cls,
        }
        btn = html.Button(label, id=cid, n_clicks=0, className=cls, title=title)
        if not confirm:
            return btn
        return html.Span(style={"display": "contents"}, children=[
            btn, dcc.Store(id=f"{cid}-armed", storage_type="memory"),
            dcc.Store(id=f"{cid}-fire", storage_type="memory"),
        ])

    def volume_slider(self, cid, command, min=0.0, max=1.0, step=0.05, value=0.5,
                      label="Vol", topic=None, retain=False):
        """A slider that publishes `command(value)` when released."""
        self._controls()[cid] = {"kind": "slider", "command": command, "topic": topic, "retain": retain}
        return html.Div(className="hw-slider d-flex align-items-center gap-2", children=[
            html.Span(label, className="hw-slider-label small text-muted"),
            html.Div(style={"flex": "1 1 140px", "minWidth": "120px"}, children=dcc.Slider(
                id=cid, min=min, max=max, step=step, value=value, marks=None,
                updatemode="mouseup", allow_direct_input=False,
                tooltip={"placement": "bottom", "always_visible": False},
            )),
        ])

    @staticmethod
    def row(label, *children):
        # label=None: no label column; "" keeps an empty one so follow-up rows line up
        items = [] if label is None else [html.Label(f"{label}:" if label else "", className="hw-row-label")]
        items += list(children)
        return html.Div(className="hw-row d-flex flex-wrap gap-2 mb-2 align-items-center", children=items)

    # --- wiring -------------------------------------------------------------
    def register_controls(self, app):
        tick = getattr(self, "_tick", None)
        for cid, spec in self._controls().items():
            if spec["kind"] == "slider":
                self._wire_slider(app, cid, spec)
            elif spec["confirm"] and tick:
                self._wire_confirm_button(app, cid, spec, tick)
            else:
                self._wire_button(app, cid, spec)

    def _send(self, spec, *values):
        command = spec["command"]
        payload = command(*values) if callable(command) else command
        if payload is None:
            return
        if spec.get("retain"):
            self.mqtt_publish(spec["topic"] or self.T_CMD, _payload(payload), qos=1, retain=True)
            return
        if spec["topic"] is None and getattr(self, "ACKS", False):
            payload = self._track_command({"action": payload} if isinstance(payload, str) else payload)
        self.mqtt_publish(spec["topic"] or self.T_CMD, _payload(payload), qos=1)

    def _wire_button(self, app, cid, spec):
        # Brief red flash in the browser: the press is visible before the command is sent
        app.clientside_callback(
            f"""function(n) {{
                const el = document.getElementById({json.dumps(cid)});
                if (n && el) {{
                    el.classList.add("hw-sent");
                    setTimeout(() => el.classList.remove("hw-sent"), 800);
                }}
                return window.dash_clientside.no_update;
            }}""",
            Output(cid, "className"),
            Input(cid, "n_clicks"),
            prevent_initial_call=True,
        )

        @app.callback(
            Output(cid, "n_clicks"),
            Input(cid, "n_clicks"),
            *[State(i, p) for i, p in spec["state"]],
            prevent_initial_call=True,
        )
        def _click(_, *values):
            self._send(spec, *values)
            return 0

    def _wire_confirm_button(self, app, cid, spec, tick):
        # Arming and reverting run in the browser so no click can be lost to a
        # slow round trip; the server only sees the confirmed "fire".
        label, cls = json.dumps(spec["label"]), json.dumps(spec["className"])
        window_ms = int(CONFIRM_SECONDS * 1000)
        app.clientside_callback(
            f"""function(_, armedAt) {{
                const now = Date.now();
                if (armedAt && now - armedAt < {window_ms}) {{
                    return [{label}, {cls}, null, now];
                }}
                return ["Confirm?", {cls} + " hw-confirming", now, window.dash_clientside.no_update];
            }}""",
            Output(cid, "children"),
            Output(cid, "className"),
            Output(f"{cid}-armed", "data"),
            Output(f"{cid}-fire", "data"),
            Input(cid, "n_clicks"),
            State(f"{cid}-armed", "data"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            f"""function(_, armedAt) {{
                const nu = window.dash_clientside.no_update;
                if (!armedAt || Date.now() - armedAt < {window_ms}) return [nu, nu, nu];
                return [{label}, {cls}, null];
            }}""",
            Output(cid, "children", allow_duplicate=True),
            Output(cid, "className", allow_duplicate=True),
            Output(f"{cid}-armed", "data", allow_duplicate=True),
            Input(tick, "n_intervals"),
            State(f"{cid}-armed", "data"),
            prevent_initial_call=True,
        )

        @app.callback(
            Output(f"{cid}-fire", "clear_data"),
            Input(f"{cid}-fire", "data"),
            *[State(i, p) for i, p in spec["state"]],
            prevent_initial_call=True,
        )
        def _fire(fired_at, *values):
            if fired_at:
                self._send(spec, *values)
            return no_update

    def _wire_slider(self, app, cid, spec):
        @app.callback(
            Output(cid, "className"),
            Input(cid, "value"),
            prevent_initial_call=True,
        )
        def _released(value):
            if value is not None:
                self._send(spec, value)
            return no_update
