"""
Dungeon Nest Dashboard Plugin

Status and basic Nest controls for the dungeon_nest worker.
"""
from plugin_base import PropPlugin


class Plugin(PropPlugin):
    name = "Dungeon Nest"
    path = "/dungeon_nest_worker"

    PROP_ID = "dungeon_nest"
    PREFIX = "dungeon"
    ACCENT = "#7dff6a"

    def controls(self):
        return [
            self.state_buttons(),
            *self.nest_controls(),
        ]
