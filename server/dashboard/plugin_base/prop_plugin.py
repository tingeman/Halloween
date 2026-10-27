"""
PropPlugin: the standard dashboard card for a prop.

A prop plugin declares its topics and controls; everything else is shared:

    class Plugin(PropPlugin):
        name = "Dungeon Nest"
        PROP_ID = "dungeon_nest"        # topics live under halloween/<PROP_ID>/
        PREFIX = "dungeon"              # component id prefix
        ACCENT = "#7dff6a"              # optional, used by the Jarvis view

        def controls(self):
            return [
                self.row("", self.button("Arm", self.cid("arm"), "arm", "primary")),
                *self.nest_controls(),
            ]

Shared behaviour:
- availability / state badges (plain text or JSON payloads); availability
  shows "stale" when the prop claims online but has sent nothing live (only
  retained messages) for STALE_AFTER seconds
- telemetry table grouped by category (halloween/<id>/telemetry/<cat>/<key>)
- STOP_COMMANDS: what the global All stop sends to this prop
- nest_controls(): the same Nest speaker controls for every prop
"""
import json
import os
import time

from dash import html, dcc, Input, Output, State, no_update

from .plugin_base import BasePlugin
from .ui import CommandUI

STALE_AFTER = float(os.getenv("PROP_STALE_AFTER", "60"))


def _text(payload: bytes, field: str) -> str:
    text = payload.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get(field):
            return str(parsed[field])
    except Exception:
        pass
    return text


class PropPlugin(CommandUI, BasePlugin):
    zone = "card"
    PROP_ID = ""
    PREFIX = ""
    ACCENT = None
    STOP_COMMANDS = ["stop"]
    TELEMETRY_ORDER = ("tesla", "hue", "speakers", "volume", "playback")
    STATE_COLORS = {
        "armed": "success", "waiting": "success", "idle": "success", "ready": "success",
        "playing": "info", "arming": "info", "action": "primary", "jumping": "primary",
        "paused": "warning", "stopped": "warning", "cooldown": "warning", "fadeout": "warning",
        "blocked": "danger",
        "error": "danger", "fault": "danger", "io error": "danger",
    }

    # --- topics -------------------------------------------------------------
    @property
    def T_BASE(self):
        return f"halloween/{self.PROP_ID}"

    @property
    def T_CMD(self):
        return f"{self.T_BASE}/cmd"

    @property
    def T_AVAIL(self):
        return f"{self.T_BASE}/availability"

    @property
    def T_STATE(self):
        return f"{self.T_BASE}/state"

    @property
    def T_TELEMETRY(self):
        return f"{self.T_BASE}/telemetry/#"

    def cid(self, name):
        return f"{self.PREFIX}-{name}"

    def _key(self, name):
        return f"{self.PREFIX}_{name}"

    # --- to override ----------------------------------------------------------
    def controls(self):
        """Rows of controls between the header and the telemetry."""
        return []

    def parse_telemetry(self, topic, payload: bytes) -> dict:
        """Map one telemetry message to {"category/key": value}."""
        key = topic.split("/telemetry/", 1)[-1] if "/telemetry/" in topic else topic.rsplit("/", 1)[-1]
        return {key: payload.decode("utf-8", "replace")}

    # --- shared control groups -----------------------------------------------
    def nest_controls(self, log_status=True):
        cc = lambda args: {"action": "chromecast", "args": args}  # noqa: E731
        rows = [
            self.row("Nest",
                     self.button("Play", self.cid("cc-play"), cc("play")),
                     self.button("Stop", self.cid("cc-stop"), cc("stop")),
                     self.button("Fade to Stop", self.cid("cc-fade"), cc("fade_to_stop")),
                     self.button("(Re)Connect", self.cid("cc-connect"), cc("connect")),
                     *([self.button("Log status", self.cid("cc-log"), cc("status"),
                                    title="Publish per-speaker status to telemetry")] if log_status else [])),
            self.row("",
                     self.button("−", self.cid("cc-vol-down"), cc("volume_down"), title="Volume down"),
                     self.volume_slider(self.cid("cc-volume"), lambda v: cc({"volume": float(v)})),
                     self.button("+", self.cid("cc-vol-up"), cc("volume_up"), title="Volume up")),
        ]
        return rows

    def safety_controls(self, title="Safety block: refuse the risky actions until Unblock"):
        """Block / Unblock for workers that honour BaseWorker.blocked (survives restarts)."""
        return [self.row("Safety",
                         self.button("Block", self.cid("block"), "block", "danger", title=title),
                         self.button("Unblock", self.cid("unblock"), "unblock", title="Lift the safety block"))]

    def hue_controls(self):
        hue = lambda args: {"action": "hue", "args": args}  # noqa: E731
        return [self.row("Hue",
                         self.button("Toggle Disco", self.cid("hue-disco"), hue("disco")),
                         self.button("(Re)Connect", self.cid("hue-connect"), hue("connect")))]

    # The same four controls on every prop:
    #   Arm     ready for a (motion) trigger
    #   Trigger start a play by hand (resumes when paused)
    #   Pause   hold the running action
    #   Stop    leave the armed state (also what All stop sends by default)
    # Pass command overrides for props whose payloads differ (e.g. the coffin).
    def state_buttons(self, arm="arm", trigger="play", pause="pause", stop="stop",
                      trigger_state=(), confirm_trigger=False):
        return self.row(None,
                        self.button("Arm", self.cid("arm"), arm, "primary",
                                    title="Arm: ready for a trigger"),
                        self.button("Trigger", self.cid("play"), trigger, "success", confirm=confirm_trigger,
                                    state=trigger_state, title="Start a play by hand (resumes when paused)"),
                        self.button("Pause", self.cid("pause"), pause, "warning",
                                    title="Hold the running action"),
                        self.button("Stop", self.cid("stop"), stop, "danger",
                                    title="Stop and leave the armed state"))

    # --- layout ----------------------------------------------------------------
    def layout(self):
        badge = lambda text: html.Span(text, className="badge bg-secondary text-white")  # noqa: E731
        return html.Div(className="hw-prop", children=[
            html.H4([
                self.name,
                html.Span(id=self.cid("availability"), className="ms-2", children=[badge("unknown")]),
                html.Span(id=self.cid("state"), className="ms-2", children=[badge("unknown")]),
                html.Span(id=self.cid("blocked"), className="ms-2"),
            ]),
            # Controls and status side by side where there is room (Jarvis), stacked otherwise
            html.Div(className="hw-prop-body", children=[
                html.Div(className="hw-prop-controls", children=self.controls()),
                html.Div(className="hw-prop-status", children=[
                    html.Div(id=self.cid("telem"), className="small"),
                    dcc.Store(id=self.cid("sig"), storage_type="memory"),
                ]),
            ]),
        ])

    # --- register ---------------------------------------------------------------
    def on_register(self, app, services):
        self._started = time.time()
        self.mqtt_subscribe(self.T_AVAIL, lambda t, p: self.cache.set(self._key("avail"), _text(p, "status")))
        self.mqtt_subscribe(self.T_STATE, lambda t, p: self.cache.set(self._key("state"), _text(p, "state")))
        self.mqtt_subscribe(f"{self.T_BASE}/state/blocked", lambda t, p: self.cache.set(
            self._key("blocked"), p.decode("utf-8", "replace").strip().lower() in ("1", "true", "on", "blocked")))
        self.mqtt_subscribe(self.T_TELEMETRY, self._on_telem)

        # One request per prop per tick (separate callbacks per badge starve the
        # last props when the browser's request queue backs up). Only what
        # changed since this browser's last render is sent: re-sending the
        # telemetry tables every second made the badges lag.
        @app.callback(
            Output(self.cid("availability"), "children"),
            Output(self.cid("state"), "children"),
            Output(self.cid("blocked"), "children"),
            Output(self.cid("telem"), "children"),
            Output(self.cid("sig"), "data"),
            Input(self._tick, "n_intervals"),
            State(self.cid("sig"), "data"),
        )
        def _render(n, last):
            last = last or {}
            badges = (self.cache.get(self._key("avail")), self.is_stale(),
                      self.cache.get(self._key("state")), bool(self.cache.get(self._key("blocked"))))
            telem = dict(self.cache.get(self._key("telem"), {}))
            sig = {"badges": repr(badges), "telem": repr(sorted(telem.items(), key=lambda kv: kv[0]))}
            if sig["badges"] == last.get("badges"):
                avail = state = blocked = no_update
            else:
                avail, state = self._render_avail(n), self._render_state(n)
                blocked = html.Span("blocked", className="badge bg-danger text-white",
                                    title="Safety block is on") if badges[3] else None
            telem_out = no_update if sig["telem"] == last.get("telem") else self._render_telem(n)
            if avail is no_update and telem_out is no_update:
                return no_update, no_update, no_update, no_update, no_update
            return avail, state, blocked, telem_out, sig

        self.register_controls(app)

    def _on_telem(self, topic, payload: bytes):
        try:
            items = self.parse_telemetry(topic, payload)
        except Exception:
            items = {topic.rsplit("/", 1)[-1]: payload.decode("utf-8", "replace")}
        with self.cache.locked() as backing:
            backing.setdefault(self._key("telem"), {}).update(items)

    # --- render -------------------------------------------------------------------
    def is_stale(self) -> bool:
        last = self._mqtt.last_live(self.T_BASE + "/") if self._mqtt and hasattr(self._mqtt, "last_live") else None
        return time.time() - max(last or 0, self._started) > STALE_AFTER

    def _render_avail(self, _):
        avail = (self.cache.get(self._key("avail")) or "unknown").lower()
        if avail in ("online", "true", "up"):
            if self.is_stale():
                return html.Span("stale", className="badge bg-warning text-dark",
                                 title=f"Says online, but nothing live for over {int(STALE_AFTER)} s")
            return html.Span("online", className="badge bg-success text-white")
        if avail in ("offline", "false", "down"):
            return html.Span("offline", className="badge bg-danger text-white")
        return html.Span(avail, className="badge bg-secondary text-white")

    def _render_state(self, _):
        state = (self.cache.get(self._key("state")) or "unknown").lower()
        color = self.STATE_COLORS.get(state, "secondary")
        text = "text-dark" if color == "warning" else "text-white"
        return html.Span(state, className=f"badge bg-{color} {text}")

    def _render_telem(self, _):
        telem = dict(self.cache.get(self._key("telem"), {}))
        if not telem:
            return html.Div("telemetry: —")
        groups = {}
        for key, value in telem.items():
            cat, sub = key.split("/", 1) if "/" in key else ("other", key)
            groups.setdefault(cat, {})[sub] = value
        order = [c for c in self.TELEMETRY_ORDER if c in groups]
        order += sorted(c for c in groups if c not in order and c != "other")
        order += ["other"] if "other" in groups else []

        cell = {"width": "1%", "whiteSpace": "nowrap"}
        rows = []
        for cat in order:
            items = sorted(groups[cat].items())
            for i, (key, value) in enumerate(items):
                if isinstance(value, (dict, list)):
                    shown = html.Pre(json.dumps(value, indent=2, sort_keys=True), style={"whiteSpace": "pre-wrap", "margin": 0})
                else:
                    shown = str(value)
                tds = [html.Td(key, style=cell), html.Td(shown, style={"wordBreak": "break-word", "maxWidth": "0"})]
                if i == 0:
                    tds.insert(0, html.Td(html.Strong(f"{cat.capitalize()}:"), rowSpan=len(items),
                                          className="align-top", style=cell))
                rows.append(html.Tr(tds))
        return html.Table(className="table table-sm mb-0", style={"tableLayout": "auto"}, children=[html.Tbody(rows)])
