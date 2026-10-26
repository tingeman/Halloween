"""
Thriller/Hue/Nest Worker Dashboard Plugin

Displays status and provides controls for the thriller_hue_nest worker.
"""
from plugin_base import PropPlugin


class Plugin(PropPlugin):
    """Class-based plugin for the Thriller/Hue/Nest worker."""

    name = "Thriller/Hue/Nest"
    path = "/thriller_hue_nest_worker"

    PROP_ID = "thriller_hue_nest"
    PREFIX = "thriller"
    ACCENT = "#c38bff"

    def controls(self):
        return [
            self.state_buttons(),   # pause freezes the music; trunk and lights stay
            *self.hue_controls(),
            *self.nest_controls(),
        ]
