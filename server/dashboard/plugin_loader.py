# server/dashboard/plugin_loader.py
import importlib.util
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Dict, Any, Tuple
import types

# Toggle verbose tracebacks by setting ENV: DASHBOARD_DEBUG=1
import os
DEBUG = os.getenv("DASHBOARD_DEBUG", "0") == "1"

def _csv_set(envname: str) -> set[str]:
    v = os.getenv(envname, "")
    return {s.strip() for s in v.split(",") if s.strip()}

DISABLE_ALL_BUILTINS = os.getenv("PLUGIN_DISABLE_ALL_BUILTINS", "0") == "1"
BUILTINS_ALLOW = _csv_set("PLUGIN_BUILTINS_ALLOW")      # e.g. "system_status"
BUILTINS_DISABLE = _csv_set("PLUGIN_BUILTINS_DISABLE")  # e.g. "example_plugin"

DISABLE_ALL_PROPS = os.getenv("PLUGIN_DISABLE_ALL_PROPS", "0") == "1"
PROPS_ALLOW = _csv_set("PLUGIN_PROPS_ALLOW")      # e.g. "tesla_hue_nest,thriller_hue_nest"
PROPS_DISABLE = _csv_set("PLUGIN_PROPS_DISABLE")  # e.g. "example_prop"


@dataclass
class PluginDesc:
    name: str
    layout: Callable[[], Any]
    register: Callable[[Any, Dict[str, Any]], None]
    zone: str  # "card" | "topbar"
    instance: Any = None  # Plugin instance (class plugins) or the module


def _load_module_from_path(name: str, file_path: str):
    """Load a Python module from an arbitrary file path with a stable module name."""
    spec = importlib.util.spec_from_file_location(name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _validate_plugin(module, origin: Path) -> Tuple[PluginDesc | None, List[str]]:
    """Validate required symbols and types; return (PluginDesc or None, errors)."""
    errors: List[str] = []
    # Required attributes
    required = ["name", "layout", "register_callbacks"]
    missing = [attr for attr in required if not hasattr(module, attr)]
    if missing:
        errors.append(f"Missing required attribute(s): {', '.join(missing)}")

    # Early exit if missing
    if errors:
        return None, errors

    name = getattr(module, "name")
    layout = getattr(module, "layout")
    register = getattr(module, "register_callbacks")
    zone = getattr(module, "zone", "card")

    # Type checks
    if not isinstance(name, str) or not name.strip():
        errors.append("Attribute 'name' must be a non-empty string.")
    if not callable(layout):
        errors.append("Attribute 'layout' must be a callable returning a Dash component.")
    if not callable(register):
        errors.append("Attribute 'register_callbacks' must be a callable (app, services) -> None.")
    if zone not in ("card", "topbar"):
        errors.append("Attribute 'zone' must be 'card' or 'topbar' if provided.")

    if errors:
        return None, errors

    return PluginDesc(name=name, layout=layout, register=register, zone=zone, instance=getattr(module, "instance", module)), []


def _try_load_one(module_name: str, file_path: Path) -> PluginDesc | None:
    try:
        m = _load_module_from_path(module_name, str(file_path))
    except Exception as e:
        print(f"[plugin_loader] Import failed for {file_path}: {e}")
        if DEBUG:
            traceback.print_exc()
        return None

    # Backwards-compatible support: if the module exposes a `Plugin` class,
    # instantiate it (no-arg constructor expected) and validate the instance
    # instead of the raw module. This lets plugin authors provide class-based
    # plugins while keeping the old module-level API working.
    plugin_target = m
    if hasattr(m, "Plugin"):
        PluginCls = getattr(m, "Plugin")
        try:
            inst = PluginCls()
        except Exception as e:
            print(f"[plugin_loader] Failed to instantiate Plugin from {file_path}: {e}")
            if DEBUG:
                traceback.print_exc()
            return None

        # Build a simple namespace that mimics the module attributes checked
        # by _validate_plugin so we can reuse the existing validation logic.
        ns = types.SimpleNamespace()
        # Prefer instance attributes, fall back to module-level attributes
        ns.name = getattr(inst, "name", getattr(m, "name", None))
        # layout may be an instance method (callable) or attribute
        ns.layout = getattr(inst, "layout", getattr(m, "layout", None))
        # allow both register or register_callbacks method names
        ns.register_callbacks = getattr(inst, "register", getattr(inst, "register_callbacks", getattr(m, "register_callbacks", None)))
        ns.zone = getattr(inst, "zone", getattr(m, "zone", "card"))
        ns.instance = inst

        plugin_target = ns

    desc, errors = _validate_plugin(plugin_target, file_path)
    if errors:
        print(f"[plugin_loader] {file_path} is not a valid plugin:")
        for err in errors:
            print(f"  - {err}")
        # Optional hint: show what attributes actually exist
        if DEBUG:
            have = sorted([a for a in dir(m) if not a.startswith('_')])
            print(f"  Available attributes: {have}")
        return None

    print(f"[plugin_loader] Loaded plugin '{desc.name}' from {file_path} (zone={desc.zone})")
    return desc


def _builtin_enabled(name: str) -> bool:
    """Check if builtin plugin is enabled via environment variables."""
    # Precedence: disable-all → allow-list (if set) → disable-list
    if DISABLE_ALL_BUILTINS:
        return False
    if BUILTINS_ALLOW:
        return name in BUILTINS_ALLOW
    if BUILTINS_DISABLE:
        return name not in BUILTINS_DISABLE
    return True


def _prop_enabled(name: str) -> bool:
    """Check if prop plugin is enabled via environment variables."""
    # Precedence: disable-all → allow-list (if set) → disable-list
    if DISABLE_ALL_PROPS:
        return False
    if PROPS_ALLOW:
        return name in PROPS_ALLOW
    if PROPS_DISABLE:
        return name not in PROPS_DISABLE
    return True


def discover_plugins(props_root: str, builtins_root: str) -> List[Dict]:
    """
    Discover plugins from:
      - built-ins: <builtins_root>/*.py
      - props:     <props_root>/*/plugin/page.py
    Returns a list of dicts as expected by app.py:
      { "name", "layout", "register", "zone" }
    """
    plugins: List[Dict] = []

    # Built-in plugins
    for f in sorted(Path(builtins_root).glob("*.py")):
        plugin_name = f.stem
        if not _builtin_enabled(plugin_name):
            print(f"[plugin_loader] Skipping builtin plugin '{plugin_name}' (disabled by env)")
            continue
        desc = _try_load_one(f"builtin_{plugin_name}", f)
        if desc:
            plugins.append({"name": desc.name, "layout": desc.layout, "register": desc.register, "zone": desc.zone, "instance": desc.instance})

    # Prop plugins
    for page in sorted(Path(props_root).glob("*/plugin/page.py")):
        prop_name = page.parent.parent.name
        if not _prop_enabled(prop_name):
            print(f"[plugin_loader] Skipping prop plugin '{prop_name}' (disabled by env)")
            continue
        mod_name = f"plugin_{prop_name}"
        desc = _try_load_one(mod_name, page)
        if desc:
            plugins.append({"name": desc.name, "layout": desc.layout, "register": desc.register, "zone": desc.zone, "instance": desc.instance})

    if not plugins:
        print("[plugin_loader] No plugins found. Expected /app/builtin_plugins/*.py or /opt/props/*/plugin/page.py")

    return plugins
