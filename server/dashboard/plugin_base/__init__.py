"""Thin package wrapper for the plugin_base helpers.

Expose SafeCache, BasePlugin and the shared prop card (PropPlugin, CommandUI)
at package-level so plugins can `from plugin_base import PropPlugin`.
"""

from .plugin_base import SafeCache, BasePlugin
from .ui import CommandUI
from .prop_plugin import PropPlugin

__all__ = ["SafeCache", "BasePlugin", "CommandUI", "PropPlugin"]
