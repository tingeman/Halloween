# server/dashboard/jarvis/__init__.py
"""
Jarvis-style dashboard, served at /jarvis/ next to the classic dashboard.

It is a second Dash app on the same Flask server. It discovers its own plugin
instances (so component ids never clash with the classic view), shares the
MQTT connection, and keeps its own cache. Plugins are rendered unchanged;
the HUD look and the four layouts (grid / focus / ops / kiosk) are CSS on
top of the plugin markup.

Besides the plugins, the view listens read-only to availability, state,
command topics and the global volume limit for the systems overview and the
comms log.

Environment (all optional):
  JARVIS_DEFAULT_LAYOUT  grid | grid2 | focus | ops | kiosk (or 1-5); used until the
                         browser has picked one. ?layout=<name> in the URL wins.
  JARVIS_THEME           blue (default) or flame; also switchable in the settings menu.
  JARVIS_ACCENTS         1 to start with per-prop accent colours on (toggle in
                         the settings menu).
  JARVIS_SOUNDTRACK      file name in the media folder (MEDIA_DIR) or an http(s)
                         URL; played once when the view loads (reload or the ♫
                         button / key R restarts it). Empty = no soundtrack.
  JARVIS_SOUNDTRACK_VOLUME  0-1, default 0.6.
  MEDIA_DIR              where soundtrack files are served from (/opt/media).
"""
import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

from dash import Dash, html, dcc, Input, Output, State, ALL, ClientsideFunction
import dash_bootstrap_components as dbc
from flask import abort, send_from_directory

from halloween_common import volume_limit
from plugin_loader import discover_plugins

URL_BASE = "/jarvis/"
MEDIA_URL = "/media/"
LAYOUTS = [
    ("grid", "Grid", "All panes as equal tiles"),
    ("grid2", "Grid 2", "Systems rail plus every prop as a full-height column"),
    ("focus", "Focus", "One large pane, the rest in a side stack"),
    ("ops", "Ops", "Command rail with systems and comms log"),
    ("kiosk", "Kiosk", "Status only, no buttons, for a wall screen"),
]
LAYOUT_KEYS = [key for key, _, _ in LAYOUTS]
PALETTE = ["#ffb547", "#7dff6a", "#ff7a45", "#c38bff", "#4dd8ff", "#ff6fb5"]
ASSETS = Path(__file__).parent / "assets"
FONTS = (
    "https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700"
    "&family=Rajdhani:wght@500;600;700&family=Share+Tech+Mono&display=swap"
)
STALE_AFTER = float(os.getenv("PROP_STALE_AFTER", "60"))


def _env_flag(name, default=False):
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _default_layout():
    value = os.getenv("JARVIS_DEFAULT_LAYOUT", "grid").strip().lower()
    if value.isdigit() and 1 <= int(value) <= len(LAYOUT_KEYS):
        return LAYOUT_KEYS[int(value) - 1]
    return value if value in LAYOUT_KEYS else "grid"


def _soundtrack_url():
    track = os.getenv("JARVIS_SOUNDTRACK", "").strip()
    if not track or track.lower().startswith(("http://", "https://")):
        return track
    return MEDIA_URL + quote(track.lstrip("/"))


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
        topic = getattr(p.get("instance"), "T_AVAIL", None) or getattr(p.get("instance"), "T_CMD", None)
        if isinstance(topic, str) and topic.count("/") >= 2:
            names[topic.split("/")[1]] = p["name"]
    return names


def _register_media_route(server):
    media_dir = Path(os.getenv("MEDIA_DIR", "/opt/media"))

    @server.route(MEDIA_URL + "<path:name>")
    def _media(name):
        if not media_dir.is_dir():
            abort(404)
        return send_from_directory(media_dir, name, conditional=True)


def create_app(server, mqtt, classic_url="/classic/"):
    """Build the Jarvis Dash app on `server`, sharing the `mqtt` service."""
    log = deque(maxlen=80)          # (ts, kind, prop, text)
    systems = {}                    # prop id -> {"avail", "state", "changed"}
    lock = threading.Lock()         # MQTT thread writes, Dash threads read
    started = time.time()
    blocked_props = {}               # prop id -> safety block on (halloween/<id>/state/blocked)

    def _log(kind, prop, text):
        with lock:
            log.appendleft((time.time(), kind, prop, text))

    def _on_cmd(topic, payload):
        _log("tx", topic.split("/")[1], payload.decode("utf-8", "replace"))

    def _on_limit(topic, payload):
        _log("vol", "volume limit", volume_limit.label(volume_limit.parse(payload)))

    def _on_status(field, kind):
        def handler(topic, payload):
            prop = topic.split("/")[1]
            value = _status_text(payload, "status" if field == "avail" else "state")
            with lock:
                sys_ = systems.setdefault(prop, {"avail": "unknown", "state": "unknown", "changed": 0})
                changed = sys_[field] != value
                if changed:
                    sys_[field], sys_["changed"] = value, time.time()
            if changed:
                _log(kind, prop, value)
        return handler

    plugins = discover_plugins("/opt/props", "/app/builtin_plugins")
    names = _prop_topic_names(plugins)
    _register_media_route(server)

    app = Dash(
        "jarvis",
        server=server,
        url_base_pathname=URL_BASE,
        assets_folder=str(ASSETS),
        external_stylesheets=[dbc.themes.BOOTSTRAP, FONTS],
        title="J.A.R.V.I.S. · Halloween",
        update_title=None,
    )
    app.config.suppress_callback_exceptions = True

    topbar = [p for p in plugins if p["zone"] == "topbar"]
    cards = [p for p in plugins if p["zone"] == "card"]
    n = max(len(cards), 1)
    cols = n if n <= 4 else math.ceil(n / 2)

    # MQTT prop id behind each pane, to match rail rows with availability/state
    pane_props = []
    for p in cards:
        topic = getattr(p.get("instance"), "T_AVAIL", None)
        pane_props.append(topic.split("/")[1] if isinstance(topic, str) and topic.count("/") >= 2 else None)

    def _accent(i, p):
        return getattr(p.get("instance"), "ACCENT", None) or PALETTE[i % len(PALETTE)]

    panes = [
        html.Section(
            id={"type": "jv-pane", "index": i},
            className="jv-pane",
            style={"--jv-prop-accent": _accent(i, p)},
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

    def _reactor(core=None, extra=""):
        rings = [html.Div(className="jv-ring r1"), html.Div(className="jv-ring r2"), html.Div(className="jv-ring r3")]
        return html.Div(className=f"jv-reactor {extra}".strip(), children=rings + [core or html.Div(className="jv-core")])

    def _brand(extra=""):
        return html.Div(className=f"jv-brand-text {extra}".strip(), children=[
            html.Div("J.A.R.V.I.S.", className="jv-title"),
            html.Div("Halloween orchestration · live", className="jv-subtitle"),
        ])

    soundtrack = _soundtrack_url()
    # The panel sits outside the header: the header's clip-path would cut it off
    settings = html.Div(className="jv-settings", children=[
        html.Button("⚙", id="jv-settings-btn", n_clicks=0, className="jv-settings-btn", title="Settings"),
    ])
    settings_panel = html.Div(id="jv-settings-panel", className="jv-settings-panel", children=[
            html.Div(className="jv-settings-head", children=[
                html.Div("Settings", className="jv-rail-head"),
                html.Button("✕", id="jv-settings-close", n_clicks=0, className="jv-settings-close",
                            title="Close (Esc)"),
            ]),
            html.Button("Theme", id="jv-set-theme", n_clicks=0, className="jv-toggle theme",
                        title="Blue or flame colour scheme"),
            html.Button("Prop accent colours", id="jv-set-accents", n_clicks=0, className="jv-toggle"),
            html.Button("Soundtrack", id="jv-set-sound", n_clicks=0, className="jv-toggle",
                        disabled=not soundtrack,
                        title=None if soundtrack else "Set JARVIS_SOUNDTRACK to enable"),
            html.Div("Keys: 1-5 layout · [ ] previous/next prop · C classic · M sound on/off · R restart track", className="jv-muted"),
    ])

    # Status bar under the top bar, only in layouts without the systems rail
    # (grid, kiosk): brand and one status chip per system.
    # Grid / Kiosk: the rail's brand + online gauge as a corner box next to the
    # top bar and the status bar
    brandbox = html.Div(className="jv-brandbox jv-rail-brand", children=[
        _reactor(html.Div(id="jv-reactor-count-2", className="jv-core-label", children="0/0"), "gauge"),
        html.Div([_brand(), html.Div("systems online", className="jv-rail-caption")]),
    ])
    statusbar = html.Div(className="jv-statusbar", children=[
        html.Div(id="jv-systems", className="jv-systems"),
    ])

    # Slim top bar: global inputs and view controls, identical in every layout
    # so they stay put when the view changes.
    header = html.Header(className="jv-header", children=[
        html.Div(className="jv-controls", children=[
            html.Div(className="jv-topbar", children=[p["layout"]() for p in topbar]),
            # Restarts the soundtrack from the beginning (handled in jarvis.js, key R)
            *([html.Button("♫", id="jv-sound-restart", className="jv-sound-btn",
                           title="Restart the soundtrack (key R)")] if soundtrack else []),
            html.Div(className="jv-switch", role="group", children=[
                html.Button(label, id=f"jv-l-{key}", n_clicks=0, title=f"{desc} (key {i + 1})",
                            className="jv-switch-btn")
                for i, (key, label, desc) in enumerate(LAYOUTS)
            ]),
            html.A("Classic", href=classic_url, className="jv-classic", title="Back to the original dashboard (key C)"),
            settings,
            html.Div(id="jv-clock", className="jv-clock"),   # filled by jarvis.js
        ]),
    ])

    # Systems rail (ops, focus): full height, brand on top, one row per prop
    # pane (click to focus it), other systems, comms log
    prop_rows = [
        html.Button(
            id={"type": "jv-focus-row", "index": i}, n_clicks=0, className="jv-proprow",
            title="Show this prop in Focus", style={"--jv-prop-accent": _accent(i, p)},
            children=html.Div(id={"type": "jv-row-body", "index": i}, className="jv-sysrow idle", children=[
                html.Span(className="jv-dot"),
                html.Div([html.Div(p["name"], className="jv-sysrow-name"),
                          html.Div("unknown", className="jv-sysrow-meta")]),
            ]),
        )
        for i, p in enumerate(cards)
    ]
    rail = html.Aside(className="jv-rail", children=[
        html.Div(className="jv-rail-brand", children=[
            _reactor(html.Div(id="jv-reactor-count", className="jv-core-label", children="0/0"), "gauge"),
            html.Div([_brand(), html.Div("systems online", className="jv-rail-caption")]),
        ]),
        html.Div("Props", className="jv-rail-head"),
        html.Div(className="jv-rail-systems", children=prop_rows),
        html.Div("Other systems", className="jv-rail-head"),
        html.Div(id="jv-rail-systems", className="jv-rail-systems"),
        html.Div("Comms log", className="jv-rail-head"),
        html.Div(id="jv-log", className="jv-log"),
    ])

    defaults = {
        "layout": _default_layout(),
        "theme": "flame" if os.getenv("JARVIS_THEME", "blue").strip().lower() == "flame" else "blue",
        "accents": _env_flag("JARVIS_ACCENTS"),
        "sound": bool(soundtrack),
        "classicUrl": classic_url,
    }

    app.layout = html.Div(
        id="jv-root",
        className=f"jv jv-layout-{defaults['layout']}" + (" jv-theme-flame" if defaults["theme"] == "flame" else ""),
        **{"data-bs-theme": "dark"},
        style={"--jv-n": n, "--jv-cols": cols, "--jv-side": max(n - 1, 1)},
        children=[
            dcc.Store(id="jv-prefs", storage_type="local"),
            dcc.Store(id="jv-defaults", data=defaults),
            dcc.Interval(id="global-tick", interval=1000, n_intervals=0),
            html.Audio(id="jv-audio", src=soundtrack or None, preload="auto",
                       **{"data-volume": os.getenv("JARVIS_SOUNDTRACK_VOLUME", "0.6")}),
            html.Div(className="jv-bg"),
            rail,
            header,
            statusbar,
            brandbox,
            settings_panel,
            html.Main(className="jv-main", children=[
                html.Div(className="jv-panes", children=panes or [
                    html.Div("No prop plugins loaded.", className="jv-empty")
                ]),
            ]),
            html.Footer(className="jv-footer", children=[
                html.Span("COMMS", className="jv-footer-tag"),
                html.Div(id="jv-ticker", className="jv-ticker"),
                html.Span("1-5 layout   C classic   M sound   R restart track", className="jv-keys"),
            ]),
        ],
    )

    # --- Plugins: own cache, shared MQTT ---------------------------------------
    services = {"mqtt": mqtt, "cache": {}, "app": app, "tick_id": "global-tick", "plugins": plugins}
    for p in plugins:
        try:
            p["register"](app, services)
        except Exception as e:
            print(f"[jarvis][plugin:{p['name']}] register_callbacks failed: {e}")

    mqtt.subscribe("halloween/+/availability", _on_status("avail", "avail"))
    mqtt.subscribe("halloween/+/state", _on_status("state", "state"))
    mqtt.subscribe("halloween/+/state/blocked", lambda t, p: blocked_props.__setitem__(
        t.split("/")[1], p.decode("utf-8", "replace").strip().lower() in ("1", "true", "on", "blocked")))
    mqtt.subscribe("halloween/+/cmd", _on_cmd)          # needs the dashboard ACL read on cmd
    mqtt.subscribe(volume_limit.TOPIC, _on_limit)
    # Signs of life for the stale check (MQTTService.last_live) from systems
    # without a prop card: the worker host uptime and any telemetry
    for topic in ("halloween/+/uptime", "halloween/+/telemetry", "halloween/+/telemetry/#"):
        mqtt.subscribe(topic, lambda t, p: None)

    # --- Layout, settings and soundtrack (clientside, persisted locally) ------
    toggles = ["jv-set-theme", "jv-set-accents", "jv-set-sound"]
    app.clientside_callback(
        ClientsideFunction("jarvis", "updatePrefs"),
        Output("jv-prefs", "data"),
        [Input(f"jv-l-{key}", "n_clicks") for key in LAYOUT_KEYS],
        Input({"type": "jv-focus", "index": ALL}, "n_clicks"),
        Input({"type": "jv-focus-row", "index": ALL}, "n_clicks"),
        [Input(t, "n_clicks") for t in toggles],
        State("jv-prefs", "data"),
        State("jv-defaults", "data"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        ClientsideFunction("jarvis", "applyPrefs"),
        Output("jv-root", "className"),
        Output({"type": "jv-pane", "index": ALL}, "className"),
        [Output(f"jv-l-{key}", "className") for key in LAYOUT_KEYS],
        [Output(t, "className") for t in toggles],
        Output({"type": "jv-focus-row", "index": ALL}, "className"),
        Input("jv-prefs", "data"),
        State("jv-defaults", "data"),
    )
    app.clientside_callback(
        ClientsideFunction("jarvis", "toggleSettings"),
        Output("jv-settings-panel", "className"),
        Input("jv-settings-btn", "n_clicks"),
        Input("jv-settings-close", "n_clicks"),   # also clicked by Esc / outside click (jarvis.js)
        State("jv-settings-panel", "className"),
        prevent_initial_call=True,
    )
    # The clock is driven by a browser timer aligned to whole seconds (jarvis.js),
    # not by global-tick, which drifts and bunches up behind server callbacks.

    # --- Systems overview, rail and comms log ---------------------------------
    def _level(prop, sys_):
        avail = sys_["avail"].lower()
        state = sys_["state"].lower()
        if avail in ("offline", "false", "down") or state in ("error", "fault", "io error"):
            return "bad"
        if state == "blocked" or blocked_props.get(prop):
            return "lock"       # online, but its safety block is on
        if avail in ("online", "true", "up"):
            last = mqtt.last_live(f"halloween/{prop}/") if hasattr(mqtt, "last_live") else None
            return "stale" if time.time() - max(last or 0, started) > STALE_AFTER else "ok"
        return "idle"

    def _label(prop):
        return names.get(prop) or prop.replace("_", " ").replace("-", " ").title()

    def _fmt_ts(ts):
        return time.strftime("%H:%M:%S", time.localtime(ts))

    @app.callback(
        Output("jv-systems", "children"),
        Output({"type": "jv-row-body", "index": ALL}, "children"),
        Output({"type": "jv-row-body", "index": ALL}, "className"),
        Output("jv-rail-systems", "children"),
        Output("jv-reactor-count", "children"),
        Output("jv-reactor-count-2", "children"),
        Output("jv-log", "children"),
        Output("jv-ticker", "children"),
        Input("global-tick", "n_intervals"),
    )
    def _render_overview(_):
        with lock:
            items = sorted(((k, dict(v)) for k, v in systems.items()), key=lambda kv: _label(kv[0]).lower())
            entries = list(log)
        chips, rows = [], []
        levels = {prop: _level(prop, s) for prop, s in items}
        status = dict(items)

        def meta(prop, sys_, lvl):
            return f"{sys_['avail']}{' (stale)' if lvl == 'stale' else ''} · {sys_['state']}"

        # One fixed rail row per prop pane (clickable); the rest go under "Other systems"
        row_children, row_classes = [], []
        for i, p in enumerate(cards):
            prop = pane_props[i]
            sys_ = status.get(prop, {"avail": "unknown", "state": "unknown", "changed": 0})
            lvl = levels.get(prop, "idle")
            row_classes.append(f"jv-sysrow {lvl}")
            row_children.append([
                html.Span(className="jv-dot"),
                html.Div([html.Div(p["name"], className="jv-sysrow-name"),
                          html.Div(meta(prop, sys_, lvl), className="jv-sysrow-meta")]),
                html.Span(_fmt_ts(sys_["changed"]) if sys_["changed"] else "", className="jv-sysrow-ts"),
            ])

        for prop, sys_ in items:
            lvl = levels[prop]
            shown = "stale" if lvl == "stale" else ("blocked" if lvl == "lock" else sys_["state"])
            chips.append(html.Div(className=f"jv-chip {lvl}", title=f"{prop}: {sys_['avail']} / {sys_['state']}", children=[
                html.Span(className="jv-dot"),
                html.Span(_label(prop), className="jv-chip-name"),
                html.Span(shown, className="jv-chip-state"),
            ]))
            if prop in pane_props:
                continue
            rows.append(html.Div(className=f"jv-sysrow {lvl}", children=[
                html.Span(className="jv-dot"),
                html.Div([
                    html.Div(_label(prop), className="jv-sysrow-name"),
                    html.Div(meta(prop, sys_, lvl), className="jv-sysrow-meta"),
                ]),
                html.Span(_fmt_ts(sys_["changed"]) if sys_["changed"] else "", className="jv-sysrow-ts"),
            ]))
        online = sum(1 for lvl in levels.values() if lvl in ("ok", "lock"))
        if not items:
            chips = [html.Div("Waiting for systems…", className="jv-chip idle")]
        if not rows:
            rows = [html.Div("None reporting.", className="jv-muted")]

        kinds = {"tx": "TX", "avail": "LNK", "state": "STA", "vol": "VOL"}
        log_rows = [
            html.Div(className=f"jv-logrow {kind}", children=[
                html.Span(_fmt_ts(ts), className="jv-log-ts"),
                html.Span(kinds.get(kind, kind.upper()), className="jv-log-kind"),
                html.Span(_label(prop), className="jv-log-prop"),
                html.Span(text, className="jv-log-text"),
            ])
            for ts, kind, prop, text in entries[:40]
        ] or [html.Div("Silence on the wire.", className="jv-muted")]
        ticker = [
            html.Span(className=f"jv-tick {kind}", children=f"{_fmt_ts(ts)} {_label(prop)} › {text}")
            for ts, kind, prop, text in entries[:6]
        ] or [html.Span("Standing by.", className="jv-tick")]
        count = f"{online}/{len(items)}"
        return chips, row_children, row_classes, rows, count, count, log_rows, ticker

    print(f"[jarvis] Jarvis dashboard mounted at {URL_BASE} with {len(cards)} pane(s); "
          f"default layout {defaults['layout']}, soundtrack {'on' if soundtrack else 'off'}")
    return app
