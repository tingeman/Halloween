from __future__ import annotations
import threading
from typing import Callable, List
import paho.mqtt.client as mqtt

from .base import MqttMessage

class ThreadedMqtt:
    """
    Simple thread-based MQTT loop.
    Workers are async, but paho is most reliable in its threaded loop.
    We fan-out messages to handlers; workers can attach a handler.
    """
    def __init__(self, host: str, port: int, username: str | None, password: str | None, client_id: str, lwt_topic: str | None = None):
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            self._client.username_pw_set(username=username, password=password)
        if lwt_topic:
            self._client.will_set(lwt_topic, payload="offline", qos=1, retain=True)

        self._handlers: List[Callable[[MqttMessage], None]] = []
        self._subs: dict[str, int] = {}
        self._availability: dict[str, str | bytes] = {}

        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self._connected_evt = threading.Event()

        self.host = host
        self.port = port
        self.lwt_topic = lwt_topic

    # Public API for workers/host
    def publish(self, topic: str, payload: str | bytes, qos: int = 0, retain: bool = False) -> None:
        if retain and topic.endswith("/availability"):
            self._availability[topic] = payload
        self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self._subs[topic] = qos
        self._client.subscribe(topic, qos=qos)

    def add_message_handler(self, handler: Callable[[MqttMessage], None]) -> None:
        self._handlers.append(handler)

    # Lifecycle
    def connect_and_loop(self) -> None:
        self._client.connect(self.host, self.port, keepalive=30)
        thread = threading.Thread(target=self._client.loop_forever, daemon=True)
        thread.start()
        self._connected_evt.wait(timeout=10)

        # Mark online
        if self.lwt_topic:
            self.publish(self.lwt_topic, "online", qos=1, retain=True)

    def _on_connect(self, client, userdata, flags, rc):
        # clean_session=True: the broker forgets us on restart/reconnect, so redo subscriptions
        for topic, qos in list(self._subs.items()):
            client.subscribe(topic, qos=qos)
        # A restarted broker may have lost retained messages (or restored stale ones)
        for topic, payload in list(self._availability.items()):
            client.publish(topic, payload, qos=1, retain=True)
        self._connected_evt.set()

    def _on_message(self, client, userdata, message):
        msg = MqttMessage(topic=message.topic, payload=message.payload, qos=message.qos, retain=message.retain)
        for h in list(self._handlers):
            try:
                h(msg)
            except Exception:
                # don't crash host on a bad handler
                pass
