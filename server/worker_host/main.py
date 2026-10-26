from __future__ import annotations
import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Dict

from .base import BaseWorker, MqttMessage
from .loader import discover_workers, WorkerDesc
from .mqtt import ThreadedMqtt

PROPS_ROOT = Path("/app/props")  # bind-mounted repo
BUILTIN_ROOT = Path("/app/worker_host/builtin_workers")  # optional
WORKER_LWT = "halloween/worker_host/availability"
UPTIME_TOPIC = "halloween/worker_host/uptime"

def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise SystemExit(f"Missing required env var: {name}")
    return val or ""

def _load_config(desc: WorkerDesc) -> dict | None:
    if not desc.config_path:
        return None
    try:
        raw_config_text = desc.config_path.read_text(encoding="utf-8")
        
        # Use basic dict loading if no model is provided
        if not desc.config_model:
            if desc.config_path.suffix.lower() == ".json":
                return json.loads(raw_config_text)
            elif desc.config_path.suffix.lower() in (".yaml", ".yml"):
                import yaml
                return yaml.safe_load(raw_config_text) or {}
            else:
                print(f"[worker_host] Unknown config format for {desc.config_path}")
                return {}

        # If a Pydantic model is defined, use it to parse
        if desc.config_path.suffix.lower() in (".yaml", ".yml"):
            import yaml
            raw_dict = yaml.safe_load(raw_config_text) or {}
            # Let the worker's ConfigModel (now a BaseSettings subclass)
            # resolve missing values from the environment. Validate directly.
            return desc.config_model.model_validate(raw_dict)
        elif desc.config_path.suffix.lower() == ".json":
            return desc.config_model.model_validate_json(raw_config_text)
        else:
            print(f"[worker_host] Pydantic model provided, but config format for {desc.config_path} is not json or yaml.")
            return None

    except Exception as e:
        print(f"[worker_host] Failed to parse {desc.config_path} with model {desc.config_model}: {e}")
        return None

async def run():
    # MQTT
    host = env("MQTT_HOST", "broker")
    port = int(env("MQTT_PORT", "1883"))
    user = env("MQTT_WORKER_USER", "worker")
    pw   = env("MQTT_WORKER_PW", "")
    client_id = env("MQTT_CLIENT_ID", "worker_host")

    mqtt = ThreadedMqtt(
        host=host,
        port=port,
        username=user or None,
        password=pw or None,
        client_id=client_id,
        lwt_topic=WORKER_LWT,
    )
    mqtt.connect_and_loop()

    # Discover workers (class-only contract)
    discovered = discover_workers(PROPS_ROOT, BUILTIN_ROOT if BUILTIN_ROOT.exists() else None)
    if not discovered:
        mqtt.publish(f"halloween/{client_id}/status/warn", "No workers discovered", qos=0)

    # Helpful debug: list discovered workers (prop_id -> origin)
    print("[worker_host] Discovered workers:")
    for d in discovered:
        print(f"  - prop_id={d.prop_id} origin={d.origin}")

    # Fan-out setup
    loop = asyncio.get_running_loop()
    msg_queue: asyncio.Queue[MqttMessage] = asyncio.Queue()

    def on_any_message(msg: MqttMessage) -> None:
        loop.call_soon_threadsafe(msg_queue.put_nowait, msg)

    mqtt.add_message_handler(on_any_message)

    # Instantiate & start each worker
    instances: Dict[str, BaseWorker] = {}
    for desc in discovered:
        cfg = _load_config(desc)
        w = desc.cls(desc.prop_id, mqtt, cfg)
        await w.start()
        instances[desc.prop_id] = w

    # One subscription for all control topics; workers filter locally
    mqtt.subscribe("halloween/+/cmd", qos=1)

    # Heartbeat
    async def heartbeat():
        start = time.time()
        while True:
            mqtt.publish(UPTIME_TOPIC, str(int(time.time() - start)), qos=0)
            await asyncio.sleep(10)
    hb_task = asyncio.create_task(heartbeat())

    # Dispatcher: one queue and consumer per worker, so a slow handler in one
    # prop (speaker or car I/O) never holds up commands for the others.
    # Each worker still gets its own messages in order.
    worker_queues: Dict[str, asyncio.Queue] = {pid: asyncio.Queue() for pid in instances}

    async def consume(prop_id: str, w: BaseWorker):
        q = worker_queues[prop_id]
        while True:
            msg = await q.get()
            try:
                await w.on_message(msg)
            except Exception as e:
                print(f"[worker_host] {prop_id} on_message failed: {type(e).__name__}: {e}", flush=True)

    async def dispatcher():
        while True:
            msg = await msg_queue.get()
            parts = msg.topic.split("/", 3)
            if len(parts) >= 3 and parts[0] == "halloween":
                prop_id = parts[1]
                if prop_id == "broadcast":
                    # Broadcasts (e.g. the global volume limit) go to every worker
                    for q in worker_queues.values():
                        q.put_nowait(msg)
                elif prop_id in worker_queues:
                    worker_queues[prop_id].put_nowait(msg)
    consumer_tasks = [asyncio.create_task(consume(pid, w)) for pid, w in instances.items()]
    disp_task = asyncio.create_task(dispatcher())

    # Graceful shutdown
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()

    hb_task.cancel()
    disp_task.cancel()
    for t in consumer_tasks:
        t.cancel()
    for w in instances.values():
        try:
            await w.stop()
        except Exception:
            pass

def main():
    asyncio.run(run())

if __name__ == "__main__":
    print("[worker_host] Starting up...")
    main()
