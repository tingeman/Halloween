"""
Tesla/Hue/Nest Worker Dashboard Plugin

Displays status and provides controls for the tesla_hue_nest worker.
"""
from plugin_base import PropPlugin


class Plugin(PropPlugin):
    """Class-based plugin for the Tesla/Hue/Nest worker."""

    name = "Tesla/Hue/Nest"
    path = "/tesla_hue_nest_worker"

    PROP_ID = "tesla_hue_nest"
    PREFIX = "thn"
    ACCENT = "#ff7a45"

    def controls(self):
        tesla = lambda args: {"action": "tesla", "args": args}  # noqa: E731
        # No Pause: the worker has no paused state (see docs/dashboard-roadmap.md)
        return [
            self.state_buttons(),
            # These move the car, so they ask for a second click
            self.row("Tesla",
                     self.button("Open Trunk", self.cid("tesla-open-trunk"), tesla("open_trunk"), confirm=True),
                     self.button("Close Trunk", self.cid("tesla-close-trunk"), tesla("close_trunk"), confirm=True),
                     self.button("Resync Tesla", self.cid("tesla-resync"), tesla("resync"), confirm=True,
                                 title="Re-read the real trunk state from the car (may lock the doors)")),
            *self.hue_controls(),
            *self.nest_controls(),
        ]
