# server/dashboard/mqtt_service.py
import os
import json
import ssl
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt


# ---- Types -----------------------------------------------------------------

OnMessage = Callable[[str, bytes], None]


@dataclass
class TLSConfig:
    ca_certs: Optional[str] = None
    certfile: Optional[str] = None
    keyfile: Optional[str] = None
    insecure: bool = False  # allow self-signed (sets CERT_NONE)


@dataclass
class WillConfig:
    topic: str
    payload: bytes | str
    qos: int = 0
    retain: bool = False


# ---- Helper: MQTT topic filter match (+ / #) --------------------------------

def _topic_matches(filter_str: str, topic: str) -> bool:
    """
    Minimal MQTT topic match supporting '+' (single level) and '#' (multi-level).
    """
    if filter_str == topic:
        return True

    f_levels = filter_str.split('/')
    t_levels = topic.split('/')

    i = 0
    for i in range(len(f_levels)):
        f = f_levels[i]

        # Multi-level wildcard (#) must be last in filter
        if f == '#':
            return i == len(f_levels) - 1

        if i >= len(t_levels):
            return False

        if f == '+':
            continue  # matches exactly one level

        if f != t_levels[i]:
            return False

    # All filter levels consumed: match only if topic has no extra levels
    return len(t_levels) == len(f_levels)


# ---- Service ---------------------------------------------------------------

class MQTTService:
    """
    Thin, thread-safe wrapper over paho-mqtt:
      - single client with loop_forever() in a daemon thread
      - multiple callbacks per subscription filter (topic or wildcard)
      - re-subscribes on reconnect
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "dashboard",
        keepalive: int = 60,
        will: Optional[WillConfig] = None,
        tls: Optional[TLSConfig] = None,
        protocol: int = mqtt.MQTTv311,  # good default for many brokers
        clean_session: bool = True,
    ):
        self._host = host
        self._port = port
        self._keepalive = keepalive

        # paho-mqtt 2.x: classic callback API still works with these kwargs
        self._client = mqtt.Client(client_id=client_id, clean_session=clean_session, protocol=protocol)

        if username:
            self._client.username_pw_set(username, password)

        if tls:
            ctx = ssl.create_default_context(cafile=tls.ca_certs) if tls.ca_certs else ssl.create_default_context()
            if tls.certfile and tls.keyfile:
                ctx.load_cert_chain(certfile=tls.certfile, keyfile=tls.keyfile)
            if tls.insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._client.tls_set_context(ctx)

        if will:
            self._client.will_set(will.topic, payload=will.payload, qos=will.qos, retain=will.retain)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._debug = os.getenv("DASHBOARD_DEBUG", "0") == "1"
        self._client.on_log = self._on_log if self._debug else None

        # Subscriptions: filter -> list[callback]
        self._subs: Dict[str, List[OnMessage]] = defaultdict(list)
        # topic -> time of the last non-retained ("live") message
        self._live: Dict[str, float] = {}

        self._lock = threading.RLock()
        self._connected_evt = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None

    # -- Lifecycle -----------------------------------------------------------

    def connect(self, wait_timeout: float = 10.0):
        """Start the network loop in a background thread and wait for initial connect."""
        def _loop():
            # Will auto-retry first connection when loop_forever is used with paho>=2.0
            self._client.connect(self._host, self._port, self._keepalive)
            self._client.loop_forever(retry_first_connection=True)

        self._loop_thread = threading.Thread(target=_loop, name="mqtt-loop", daemon=True)
        self._loop_thread.start()
        self._connected_evt.wait(timeout=wait_timeout)

    def disconnect(self):
        try:
            self._client.disconnect()
        except Exception:
            pass

    # -- Pub/Sub ------------------------------------------------------------

    def subscribe(self, topic_filter: str, callback: OnMessage, qos: int = 0):
        """
        Register a callback for a topic filter (may include '+' or '#').
        Multiple callbacks per filter are allowed.
        """
        with self._lock:
            self._subs[topic_filter].append(callback)
            if self._connected_evt.is_set():
                # Subscribe (idempotent from the broker perspective)
                self._client.subscribe(topic_filter, qos=qos)

    def unsubscribe(self, topic_filter: str, callback: Optional[OnMessage] = None):
        """Remove a callback for a filter, or the entire filter if callback is None."""
        with self._lock:
            if topic_filter not in self._subs:
                return
            if callback is None:
                del self._subs[topic_filter]
                if self._connected_evt.is_set():
                    self._client.unsubscribe(topic_filter)
            else:
                lst = self._subs[topic_filter]
                try:
                    lst.remove(callback)
                except ValueError:
                    pass
                if not lst:
                    del self._subs[topic_filter]
                    if self._connected_evt.is_set():
                        self._client.unsubscribe(topic_filter)

    def last_live(self, prefix: str) -> Optional[float]:
        """Time of the newest non-retained message on a topic starting with `prefix`."""
        with self._lock:
            times = [t for topic, t in self._live.items() if topic.startswith(prefix)]
        return max(times) if times else None

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False):
        self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def publish_json(self, topic: str, obj, qos: int = 0, retain: bool = False):
        self.publish(topic, json.dumps(obj, separators=(",", ":")), qos=qos, retain=retain)

    # -- paho callbacks ------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if self._debug:
            print(f"[MQTTService] on_connect rc={rc}")
        if rc == mqtt.MQTT_ERR_SUCCESS or rc == 0:
            self._connected_evt.set()
            # Re-subscribe all known filters
            with self._lock:
                for f in self._subs.keys():
                    if self._debug:
                        print(f"[MQTTService] (re)subscribe: {f}")
                    client.subscribe(f, qos=0)
        else:
            # connection failed; leave event unset so callers can detect
            pass

    def _on_disconnect(self, client, userdata, rc):
        # Clear connected flag so future subscribe() calls know they must defer
        if self._debug:
            print(f"[MQTTService] on_disconnect rc={rc}")
        self._connected_evt.clear()

    def _on_message(self, client, userdata, msg):
        # Collect relevant handlers by matching the message topic against each filter.
        if self._debug:
            print(f"[MQTTService] msg on {msg.topic} -> {msg.payload[:64]!r}")

        to_call: List[OnMessage] = []
        with self._lock:
            # commands are what we (or others) send, not a sign of life from the prop
            if not msg.retain and not msg.topic.endswith("/cmd"):
                self._live[msg.topic] = time.time()
            for f, handlers in self._subs.items():
                if _topic_matches(f, msg.topic):
                    to_call.extend(handlers)

        if not to_call:
            return

        for cb in to_call:
            try:
                cb(msg.topic, msg.payload)
            except Exception as e:
                print(f"[MQTTService] callback error on {msg.topic}: {e}")

    def _on_log(self, client, userdata, level, buf):
        if self._debug:
            print(f"[MQTTService][paho] {buf}")