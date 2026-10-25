Dashboard plugin loader

This folder contains the small plugin loader used by the dashboard app. The
loader auto-discovers plugins from two locations:

- built-ins: `<builtins_root>/*.py` (app's builtin plugins)
- props: `<props_root>/*/plugin/page.py` (per-prop plugin modules)

Class-based plugin support
--------------------------

The loader supports two plugin styles for backwards compatibility:

1) Module-level API (legacy)
   - The module must expose:
     - `name` (str)
     - `layout()` (callable returning a Dash fragment)
     - `register_callbacks(app, services)` (callable that wires callbacks)
   - Optional: `zone` ("card" or "topbar").

2) Class-based API (new)
   - The module may export a `Plugin` class. The loader will try to
     instantiate `Plugin()` (no-argument constructor) and then read the
     following attributes or methods from the instance:
     - `name` (str)
     - `layout()` (callable)
     - `register(app, services)` or `register_callbacks(app, services)` (method)
     - optional `zone` (defaults to "card")
   - Example minimal plugin module:

```python
class Plugin:
    name = "My Plugin"
    zone = "card"

    def layout(self):
        # return Dash components
        ...

    def register(self, app, services):
        # wire MQTT subscriptions and Dash callbacks
        ...
```

Notes
-----
- The loader remains backwards compatible: existing module-level plugins
  continue to work without modification.
- Instantiation errors while creating `Plugin()` will cause the loader to
  skip the plugin and print an error; enable `DASHBOARD_DEBUG=1` for full
  tracebacks.
- Keep `Plugin.__init__()` cheap and side-effect free. The loader creates
  instances during discovery (before services like MQTT/cache are passed
  in).

- The loader runs discovery once per view (classic and Jarvis), so each view
  has its own plugin instances; `layout()` and `register()` are called once
  per instance.

Prop plugins: PropPlugin
------------------------

Prop cards should subclass `plugin_base.PropPlugin`; it provides the badges,
stale detection, telemetry table and shared controls, so every prop looks and
behaves the same in both views:

```python
from plugin_base import PropPlugin

class Plugin(PropPlugin):
    name = "Dungeon Nest"
    PROP_ID = "dungeon_nest"          # topics under halloween/<PROP_ID>/
    PREFIX = "dungeon"                # component id prefix, unique per prop
    ACCENT = "#7dff6a"                # Jarvis accent colour (optional)
    STOP_COMMANDS = ["stop"]          # what All stop sends (default)

    def controls(self):
        return [self.state_buttons(pause=True), *self.nest_controls()]
```

Control helpers (`plugin_base.ui.CommandUI`): `row(label, *children)`
(`label=None` for no label column), `button(label, cid, command, variant,
confirm=False, state=[(id, prop)])`, `volume_slider(cid, command, min, max,
step, value)`, plus `state_buttons()`, `nest_controls()`, `hue_controls()`.
A command is a str/dict payload or a callable taking the `state` values.
Controls are wired automatically in `register()`.

Override `parse_telemetry()` or `T_TELEMETRY` when a prop publishes telemetry
differently (see the Coffin Jumper plugin).
