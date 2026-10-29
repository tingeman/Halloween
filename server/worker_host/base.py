from __future__ import annotations
import asyncio, json, os, re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Iterable, Union
import inspect
from pydantic import BaseModel

from halloween_common import volume_limit

@dataclass
class MqttMessage:
    topic: str
    payload: bytes
    qos: int
    retain: bool

class MqttClientProto(Protocol):
    def publish(self, topic: str, payload: str | bytes, qos: int = 0, retain: bool = False) -> None: ...
    def subscribe(self, topic: str, qos: int = 0) -> None: ...
    def add_message_handler(self, handler: Callable[[MqttMessage], None]) -> None: ...

class BaseWorker:
    """
    Class-based contract for prop backends.
    Control topic: halloween/<prop_id>/cmd   (action comes from payload)
    Telemetry:     halloween/<prop_id>/telemetry/<key>
    Status:        halloween/<prop_id>/status/<key>
    State:         halloween/<prop_id>/state
    Availability:  halloween/<prop_id>/availability
    Broadcast:     halloween/broadcast/<key>  (all workers; see on_broadcast)
    Block flag:    halloween/<prop_id>/state/blocked  ("1"/"0", retained; do_block / do_unblock)

    Volume limit:  the retained halloween/broadcast/volume_limit message sets
                   self.volume_scale and is applied to every object returned by
                   volume_targets() (anything with set_volume_limit(scale)).
                   Workers call apply_volume_limit() after (re)creating them.
    """
    def __init__(self, prop_id: str, mqtt: MqttClientProto, config: Union[dict[str, Any], BaseModel, None] = None):
        self.prop_id = prop_id
        self.mqtt = mqtt
        self.config = config or {}
        self._tasks: list[asyncio.Task] = []
        self.volume_scale = volume_limit.NO_LIMIT
        # Safety block (see do_block): what it blocks is up to each worker
        self.blocked = False
        self._seen_cmd_ids: list[str] = []

    # --- Lifecycle ---
    async def start(self) -> None:
        self.mqtt.subscribe(f"halloween/{self.prop_id}/cmd", qos=1)
        self.mqtt.subscribe(volume_limit.TOPIC, qos=1)
        # Retained block flag, so a block survives worker restarts
        self.mqtt.subscribe(self._t_blocked, qos=1)
        # Publish a simple availability LWT topic (no 'status' segment) so the
        # dashboard and other consumers can subscribe to `halloween/<prop_id>/availability`.
        self.mqtt.publish(f"halloween/{self.prop_id}/availability", "online", qos=1, retain=True)
        # announce state
        self.publish_state("started", qos=1, retain=True)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self.mqtt.publish(f"halloween/{self.prop_id}/availability", "offline", qos=1, retain=True)
        # announce state
        self.publish_state("stopped", qos=1, retain=True)

    # --- Message dispatch (parse action from payload) ---
    async def on_message(self, msg: MqttMessage) -> None:
        if msg.topic.startswith("halloween/broadcast/"):
            await self.on_broadcast(msg)
            return
        if msg.topic == self._t_blocked:
            self.blocked = msg.payload.decode("utf-8", "replace").strip().lower() in ("1", "true", "on", "blocked")
            return
        if msg.topic != f"halloween/{self.prop_id}/cmd":
            return
        action, arg = self._parse_cmd_payload(msg.payload)
        cmd_id = self._cmd_id(msg.payload)
        if cmd_id is not None and cmd_id in self._seen_cmd_ids:
            # QoS 1 can deliver a command twice: answer again, don't run it again
            self._ack(cmd_id, action, True, "duplicate")
            return
        if not action:
            self.publish_status("warn", "Empty/invalid command payload")
            self._ack(cmd_id, action, False, "empty or invalid command")
            return
        if cmd_id is not None:
            self._seen_cmd_ids.append(cmd_id)
            del self._seen_cmd_ids[:-20]
        handler_name = f"do_{action.replace('/', '_')}"
        if hasattr(self, handler_name):
            handler = getattr(self, handler_name)
            try:
                # If handler is async, await it; if it's sync, run in thread to avoid blocking loop
                if inspect.iscoroutinefunction(handler):
                    await handler(arg)
                else:
                    await asyncio.to_thread(handler, arg)
                self._ack(cmd_id, action, True)
            except Exception as e:
                print(f"[{self.prop_id}] {handler_name} failed: {type(e).__name__}: {e}", flush=True)
                self.publish_status("error", f"{type(e).__name__}: {e}")
                self._ack(cmd_id, action, False, f"{type(e).__name__}: {e}")
        elif hasattr(self, "do_command"):
            handler = getattr(self, "do_command")
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(action, arg)
                else:
                    await asyncio.to_thread(handler, action, arg)
                self._ack(cmd_id, action, True)
            except Exception as e:
                print(f"[{self.prop_id}] do_command({action}) failed: {type(e).__name__}: {e}", flush=True)
                self.publish_status("error", f"{type(e).__name__}: {e}")
                self._ack(cmd_id, action, False, f"{type(e).__name__}: {e}")
        else:
            self.publish_status("warn", f"Unknown action: {action}")
            self._ack(cmd_id, action, False, f"unknown action: {action}")

    # --- Command acknowledgements (same contract as the coffin firmware) ---
    @staticmethod
    def _cmd_id(payload: bytes) -> Optional[str]:
        """The optional "id" of a JSON command, echoed back in the ack."""
        try:
            obj = json.loads(payload.decode("utf-8", errors="ignore"))
        except Exception:
            return None
        if isinstance(obj, dict) and obj.get("id") is not None:
            return str(obj["id"])
        return None

    def _ack(self, cmd_id: Optional[str], action: Optional[str], ok: bool, reason: Optional[str] = None) -> None:
        """Publish {"id", "action", "ok", "reason"} on halloween/<prop_id>/ack (only for commands with an id)."""
        if cmd_id is None:
            return
        self.mqtt.publish(f"halloween/{self.prop_id}/ack",
                          json.dumps({"id": cmd_id, "action": action, "ok": ok, "reason": reason}), qos=1)

    # --- Safety block (same for every worker) ---
    @property
    def _t_blocked(self) -> str:
        return f"halloween/{self.prop_id}/state/blocked"

    async def do_block(self, arg) -> None:
        """Set the retained safety block; workers check self.blocked before risky actions."""
        self.blocked = True
        self.mqtt.publish(self._t_blocked, "1", qos=1, retain=True)

    async def do_unblock(self, arg) -> None:
        self.blocked = False
        self.mqtt.publish(self._t_blocked, "0", qos=1, retain=True)

    # --- Broadcasts (same for every worker) ---
    async def on_broadcast(self, msg: MqttMessage) -> None:
        if msg.topic == volume_limit.TOPIC:
            scale = volume_limit.parse(msg.payload)
            if getattr(self, "_volume_limit_seen", False) and scale == self.volume_scale:
                return  # repeat of the current limit: nothing to do
            self._volume_limit_seen = True
            self.volume_scale = scale
            await asyncio.to_thread(self.apply_volume_limit)

    def volume_targets(self) -> list:
        """Objects the volume limit applies to (override, e.g. [self.group])."""
        return []

    def apply_volume_limit(self) -> None:
        """Push self.volume_scale to every volume target. Safe to call any time."""
        for target in self.volume_targets():
            if target is not None and hasattr(target, "set_volume_limit"):
                try:
                    target.set_volume_limit(self.volume_scale)
                except Exception as e:
                    print(f"[{self.prop_id}] applying volume limit failed: {type(e).__name__}: {e}", flush=True)
        self.telemetry("volume/Limit", volume_limit.label(self.volume_scale), retain=True)

    # --- Helpers for workers ---
    def spawn(self, coro) -> None:
        self._tasks.append(asyncio.create_task(coro))

    def resolve_config_var(self, key: str, default: Any = None) -> Any:
        """
        Resolves a configuration value from the worker's config dictionary.

        This method provides a hierarchical lookup:
        1. It retrieves the value for the given key from `self.config`.
        2. If the value is a string in the format `${ENV_VAR_NAME}`, it
           attempts to resolve it from the environment variables.
        3. If the key is not found in the config, or if the corresponding
           environment variable is not set, it returns the provided `default`.

        Args:
            key: The configuration key to look up.
            default: The value to return if the key or environment variable
                     is not found.

        Returns:
            The resolved configuration value.
        """
        # Support `self.config` being either a dict or a pydantic BaseModel
        value = None
        try:
            if isinstance(self.config, dict):
                value = self.config.get(key)
            elif isinstance(self.config, BaseModel):
                # Prefer attribute access (fast) to avoid serializing the whole
                # pydantic model on every lookup. Fall back to model_dump() if
                # the attribute is not present (or the model uses aliases).
                value = getattr(self.config, key, None)
                if value is None and hasattr(self.config, 'model_dump'):
                    try:
                        dd = self.config.model_dump()
                        value = dd.get(key, None)
                    except Exception:
                        value = None
            else:
                # Generic mapping-like attempt
                try:
                    value = self.config.get(key)  # type: ignore[attr-defined]
                except Exception:
                    value = getattr(self.config, key, None)
        except Exception:
            value = None

        if value is None:
            return default

        if isinstance(value, str):
            # Check for ${VAR_NAME} pattern
            match = re.match(r'^\$\{(.+)\}$', value)
            if match:
                env_var_name = match.group(1)
                env_value = os.getenv(env_var_name)
                # Return the environment value if it exists, otherwise the default
                return env_value if env_value is not None else default
        
        # If it's not a placeholder string, return the value directly
        return value

    def telemetry(self, key: str, value: Any, qos: int = 0, retain: bool = False) -> None:
        self.mqtt.publish(f"halloween/{self.prop_id}/telemetry/{key}", str(value), qos=qos, retain=retain)
        print(f"telemetry: halloween/{self.prop_id}/telemetry/{key}={value}")

    def publish_status(self, key: str, value: Any, qos: int = 0, retain: bool = False) -> None:
        self.mqtt.publish(f"halloween/{self.prop_id}/status/{key}", str(value), qos=qos, retain=retain)

    def publish_state(self, state: str, qos: int = 0, retain: bool = False) -> None:
        """Publish a simple textual state for the worker under
        `halloween/<prop_id>/state`.

        Args:
            state: short textual state (e.g. 'started', 'stopped', 'playing')
            qos: MQTT QoS
            retain: whether to retain the state message
        """
        try:
            self.mqtt.publish(f"halloween/{self.prop_id}/state", str(state), qos=qos, retain=retain)
        except Exception:
            # Ensure worker doesn't crash due to publish errors
            print(f"[worker_host] failed to publish state for {self.prop_id}: {state}")

    def command(self, target_prop: str, action: str, args: Any | None = None, *, qos: int = 1):
        """
        Send a command to another worker/prop firmware:
        topic: halloween/<target_prop>/cmd
        payload: JSON {"action": ..., "args": ...}
        """
        payload = json.dumps({"action": action, "args": args})
        self.mqtt.publish(f"halloween/{target_prop}/cmd", payload, qos=qos)

    def broadcast(self, targets: Iterable[str], action: str, args: Any | None = None, *, qos: int = 1):
        """Send the same command to many props."""
        for t in targets:
            self.command(t, action, args, qos=qos)


    @staticmethod
    def _parse_cmd_payload(payload: bytes) -> tuple[Optional[str], Optional[str]]:
        """
        Parse an MQTT command payload into an (action, arg) pair.

        Supported payload shapes:
        - JSON object with an `action` key and optional `args` key, e.g.
          {"action": "arm", "args": "now"}
          - If `args` is a dict or list, it will be returned as a JSON-encoded
            string to keep the return type simple.
          - If `args` is a scalar, it will be coerced to str.
        - Plain text: a single word `action` or `action arg...` where the first
          space separates the action and the argument (argument may contain spaces).

        Behavior and return value:
        - Returns a tuple (action, arg) where `action` is a non-empty string
          representing the command name and `arg` is either a string or None.
        - If the payload is empty or cannot be parsed, returns (None, None).

        Examples:
        - b'{"action":"arm"}' -> ("arm", None)
        - b'{"action":"play","args":{"url":"/x.mp3"}}' -> ("play", '{"url":"/x.mp3"}')
        - b"stop now" -> ("stop", "now")
        - b"reboot" -> ("reboot", None)

        Args:
            payload: Raw MQTT message payload as bytes.

        Returns:
            Tuple[action, arg] where action is the command name (or None on
            failure) and arg is a string argument (or None).
        """
        if not payload:
            return None, None
        p = payload.decode("utf-8", errors="ignore").strip()
        if not p:
            return None, None
        # Try JSON first
        try:
            obj = json.loads(p)
            if isinstance(obj, dict) and "action" in obj:
                action = str(obj["action"]).strip()
                arg = obj.get("args")
                if isinstance(arg, (dict, list)):
                    arg = json.dumps(arg)
                elif arg is not None:
                    arg = str(arg)
                return action, arg
        except Exception:
            pass
        # Plain text: "action arg..."
        if " " in p:
            action, arg = p.split(" ", 1)
            return action.strip(), arg.strip()
        return p, None
