# server/dashboard/jarvis/__init__.py
"""
Jarvis-style dashboard, served at /jarvis/ next to the classic dashboard at /.

It is a second Dash app on the same Flask server. It discovers its own plugin
instances (so component ids never clash with the classic view), shares the
MQTT connection, and keeps its own cache. Plugins are rendered unchanged;
the HUD look and the three layouts (grid / focus / ops) are pure CSS on top
of the plugin markup.

Besides the plugins, the view listens read-only to
halloween/+/availability and halloween/+/state for the systems overview and
logs every command a plugin publishes from this view in the comms log.
"""
import json
import math
import threading
import time
from collections import deque
from pathlib import Path

from dash import Dash, html, dcc, Input, Output, State, ALL, ClientsideFunction
import dash_bootstrap_components as dbc

from plugin_loader import discover_plugins

URL_BASE = "/jarvis/"
LAYOUTS = [
    ("grid", "Grid", "All panes as equal tiles"),
    ("focus", "Focus", "One large pane, the rest in a side stack"),
    ("ops", "Ops", "Command rail with systems and comms log"),
]
ASSETS = Path(__file__).parent / "assets"
FONTS = (
    "https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700"
    "&family=Rajdhani:wght@500;600;700&family=Share+Tech+Mono&display=swap"
)


class _LoggingMQTT:
    """Delegates to the shared MQTTService and records every publish."""

    def __init__(self, mqtt, on_publish):
        self._mqtt = mqtt
        self._on_publish = on_publish

    def publish(self, topic, payload, *args, **kwargs):
        self._on_publish(topic, payload)
        return self._mqtt.publish(topic, payload, *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._mqtt, item)


def _status_text(payload: bytes, field: str) -> str:
    """Plain text payload, or the given field of a JSON object payload."""
    text = payload.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get(field):
            return str(parsed[field])
    except Exception:
        pass
    return text


def _prop_topic_names(plugins) -> dict:
    """Map MQTT prop id -> plugin display name, read from the plugin's topics."""
    names = {}
    for p in plugins:
        inst = getattr(p["layout"], "__self__", None)
        topic = getattr(inst, "T_AVAIL", None) or getattr(inst, "T_CMD", None)
        if isinstance(topic, str) and topic.count("/") >= 2:
            names[topic.split("/")[1]] = p["name"]
    return names


def create_app(server, mqtt):
    """Build the Jarvis Dash app on `server`, sharing the `mqtt` service."""
    log = deque(maxlen=80)          # (ts, kind, prop, text)
    systems = {}                    # prop id -> {"avail", "state", "changed"}
    lock = threading.Lock()         # MQTT thread writes, Dash threads read

    def _log(kind, prop, text):
        with lock:
            log.appendleft((time.time(), kind, prop, text))

    def _on_publish(topic, payload):
        parts = topic.split("/")
        prop = parts[1] if len(parts) > 1 else topic
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        _log("tx", prop, str(payload))

    def _on_avail(topic, payload):
        prop = topic.split("/")[1]
        value = _status_text(payload, "status")
        with lock:
            sys_ = systems.setdefault(prop, {"avail": "unknown", "state": "unknown", "changed": 0})
            changed = sys_["avail"] != value
            if changed:
                sys_["avail"], sys_["changed"] = value, time.time()
        if changed:
            _log("avail", prop, value)

    def _on_state(topic, payload):
        prop = topic.split("/")[1]
        value = _status_text(payload, "state")
        with lock:
            sys_ = systems.setdefault(prop, {"avail": "unknown", "state": "unknown", "changed": 0})
            changed = sys_["state"] != value
            if changed:
                sys_["state"], sys_["changed"] = value, time.time()
        if changed:
            _log("state", prop, value)

    plugins = discover_plugins("/opt/props", "/app/builtin_plugins")
    names = _prop_topic_names(plugins)

    app = Dash(
        "jarvis",
        server=server,
        url_base_pathname=URL_BASE,
        assets_folder=str(ASSETS),
        external_stylesheets=[
            dbc.themes.BOOTSTRAP,
            FONTS,
        ],
        title="J.A.R.V.I.S. · Halloween",
        update_title=None,
    )
    app.config.suppress_callback_exceptions = True

    topbar = [p for p in plugins if p["zone"] == "topbar"]
    cards = [p for p in plugins if p["zone"] == "card"]
    n = max(len(cards), 1)
    cols = n if n <= 4 else math.ceil(n / 2)

    panes = [
        html.Section(
            id={"type": "jv-pane", "index": i},
            className="jv-pane",
            children=[
                html.Div(className="jv-pane-tag", children=[
                    html.Span(f"SYS-{i + 1:02d}"),
                    html.Button("⤢", id={"type": "jv-focus", "index": i}, n_clicks=0,
                                className="jv-focus-btn", title="Focus this pane"),
                ]),
                html.Div(className="jv-pane-body", children=p["layout"]()),
            ],
        )
        for i, p in enumerate(cards)
    ]

    reactor = html.Div(className="jv-reactor", children=[
        html.Div(className="jv-ring r1"), html.Div(className="jv-ring r2"),
        html.Div(className="jv-ring r3"), html.Div(className="jv-core"),
    ])

    header = html.Header(className="jv-header", children=[
        html.Div(className="jv-brand", children=[
            reactor,
            html.Div([
                html.Div("J.A.R.V.I.S.", className="jv-title"),
                html.Div("Halloween orchestration · live", className="jv-subtitle"),
            ]),
        ]),
        html.Div(id="jv-systems", className="jv-systems"),
        html.Div(className="jv-controls", children=[
            html.Div(className="jv-topbar", children=[p["layout"]() for p in topbar]),
            html.Div(className="jv-switch", role="group", children=[
                html.Button(label, id=f"jv-l-{key}", n_clicks=0, title=f"{desc} (key {i + 1})",
                            className="jv-switch-btn")
                for i, (key, label, desc) in enumerate(LAYOUTS)
            ]),
            html.A("Classic view", href="/", className="jv-classic", title="Back to the original dashboard (key C)"),
            html.Div(id="jv-clock", className="jv-clock", children="--:--:--"),
        ]),
    ])

    rail = html.Aside(className="jv-rail", children=[
        html.Div(className="jv-rail-reactor", children=[
            html.Div(className="jv-reactor big", children=[
                html.Div(className="jv-ring r1"), html.Div(className="jv-ring r2"),
                html.Div(className="jv-ring r3"),
                html.Div(id="jv-reactor-count", className="jv-core-label", children="0/0"),
            ]),
            html.Div("Systems online", className="jv-rail-caption"),
        ]),
        html.Div("Systems", className="jv-rail-head"),
        html.Div(id="jv-rail-systems", className="jv-rail-systems"),
        html.Div("Comms log", className="jv-rail-head"),
        html.Div(id="jv-log", className="jv-log"),
    ])

    app.layout = html.Div(
        id="jv-root",
        className="jv jv-layout-grid",
        **{"data-bs-theme": "dark"},
        style={"--jv-n": n, "--jv-cols": cols, "--jv-side": max(n - 1, 1)},
        children=[
            dcc.Store(id="jv-prefs", storage_type="local"),
            dcc.Interval(id="global-tick", interval=1000, n_intervals=0),
            html.Div(className="jv-bg"),
            header,
            html.Main(className="jv-main", children=[
                rail,
                html.Div(className="jv-panes", children=panes or [
                    html.Div("No prop plugins loaded.", className="jv-empty")
                ]),
            ]),
            html.Footer(className="jv-footer", children=[
                html.Span("COMMS", className="jv-footer-tag"),
                html.Div(id="jv-ticker", className="jv-ticker"),
                html.Span("1 · 2 · 3 switch layout   C classic", className="jv-keys"),
            ]),
        ],
    )

    # --- Shared services: own cache, MQTT that logs publishes -------------------
    services = {"mqtt": _LoggingMQTT(mqtt, _on_publish), "cache": {}, "app": app, "tick_id": "global-tick"}
    for p in plugins:
        try:
            p["register"](app, services)
        except Exception as e:
            print(f"[jarvis][plugin:{p['name']}] register_callbacks failed: {e}")

    mqtt.subscribe("halloween/+/availability", _on_avail)
    mqtt.subscribe("halloween/+/state", _on_state)

    # --- Layout switching (clientside, persisted in localStorage) -------------
    app.clientside_callback(
        ClientsideFunction("jarvis", "updatePrefs"),
        Output("jv-prefs", "data"),
        [Input(f"jv-l-{key}", "n_clicks") for key, _, _ in LAYOUTS],
        Input({"type": "jv-focus", "index": ALL}, "n_clicks"),
        State("jv-prefs", "data"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        ClientsideFunction("jarvis", "applyPrefs"),
        Output("jv-root", "className"),
        Output({"type": "jv-pane", "index": ALL}, "className"),
        [Output(f"jv-l-{key}", "className") for key, _, _ in LAYOUTS],
        Input("jv-prefs", "data"),
    )
    app.clientside_callback(
        ClientsideFunction("jarvis", "clock"),
        Output("jv-clock", "children"),
        Input("global-tick", "n_intervals"),
    )

    # --- Systems overview, rail and comms log ---------------------------------
    def _level(sys_):
        avail = sys_["avail"].lower()
        state = sys_["state"].lower()
        if avail in ("offline", "false", "down") or state in ("error", "fault", "io error"):
            return "bad"
        if avail in ("online", "true", "up"):
            return "ok"
        return "idle"

    def _label(prop):
        return names.get(prop) or prop.replace("_", " ").replace("-", " ").title()

    def _fmt_ts(ts):
        return time.strftime("%H:%M:%S", time.localtime(ts))

    @app.callback(
        Output("jv-systems", "children"),
        Output("jv-rail-systems", "children"),
        Output("jv-reactor-count", "children"),
        Output("jv-log", "children"),
        Output("jv-ticker", "children"),
        Input("global-tick", "n_intervals"),
    )
    def _render_overview(_):
        with lock:
            items = sorted(((k, dict(v)) for k, v in systems.items()), key=lambda kv: _label(kv[0]).lower())
            entries = list(log)
        chips, rows = [], []
        for prop, sys_ in items:
            lvl = _level(sys_)
            chips.append(html.Div(className=f"jv-chip {lvl}", title=f"{prop}: {sys_['avail']} / {sys_['state']}", children=[
                html.Span(className="jv-dot"),
                html.Span(_label(prop), className="jv-chip-name"),
                html.Span(sys_["state"], className="jv-chip-state"),
            ]))
            rows.append(html.Div(className=f"jv-sysrow {lvl}", children=[
                html.Span(className="jv-dot"),
                html.Div([
                    html.Div(_label(prop), className="jv-sysrow-name"),
                    html.Div(f"{sys_['avail']} · {sys_['state']}", className="jv-sysrow-meta"),
                ]),
                html.Span(_fmt_ts(sys_["changed"]) if sys_["changed"] else "", className="jv-sysrow-ts"),
            ]))
        online = sum(1 for _, s in items if _level(s) == "ok")
        if not items:
            chips = [html.Div("Waiting for systems…", className="jv-chip idle")]
            rows = [html.Div("No availability messages yet.", className="jv-muted")]

        log_rows = [
            html.Div(className=f"jv-logrow {kind}", children=[
                html.Span(_fmt_ts(ts), className="jv-log-ts"),
                html.Span({"tx": "TX", "avail": "LNK", "state": "STA"}[kind], className="jv-log-kind"),
                html.Span(_label(prop), className="jv-log-prop"),
                html.Span(text, className="jv-log-text"),
            ])
            for ts, kind, prop, text in entries[:40]
        ] or [html.Div("Silence on the wire.", className="jv-muted")]
        ticker = [
            html.Span(className=f"jv-tick {kind}", children=f"{_fmt_ts(ts)} {_label(prop)} › {text}")
            for ts, kind, prop, text in entries[:6]
        ] or [html.Span("Standing by.", className="jv-tick")]
        return chips, rows, f"{online}/{len(items)}", log_rows, ticker

    print(f"[jarvis] Jarvis dashboard mounted at {URL_BASE} with {len(cards)} pane(s)")
    return app
