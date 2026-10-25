"""Global volume limit shared by the dashboard, workers and firmware.

The dashboard publishes the limit as a retained message on TOPIC, so every
prop picks it up on (re)connect and the broker keeps it across reboots
(mosquitto persistence is on).

Payload: JSON {"scale": 0.3}. Plain numbers ("0.3") and percentages ("30%")
are accepted too.

Semantics, identical for every prop:
- scale >= 1.0 means no limit (props behave as before).
- scale < 1.0 caps every speaker at scale * <that speaker's default volume>.
  Requests above the cap are lowered to the cap; requests below it are kept.
"""
from __future__ import annotations

import json

TOPIC = "halloween/broadcast/volume_limit"
NO_LIMIT = 1.0


def parse(payload: bytes | str | None) -> float:
    """Return the scale (0..1) from a payload; malformed input means no limit."""
    if payload is None:
        return NO_LIMIT
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    text = text.strip()
    if not text:
        return NO_LIMIT
    try:
        obj = json.loads(text)
        value = obj.get("scale", NO_LIMIT) if isinstance(obj, dict) else obj
        scale = float(value)
    except (ValueError, TypeError):
        try:
            scale = float(text.rstrip("%")) / 100.0 if text.endswith("%") else float(text)
        except ValueError:
            return NO_LIMIT
    return min(max(scale, 0.0), NO_LIMIT)


def encode(scale: float) -> str:
    return json.dumps({"scale": round(min(max(float(scale), 0.0), NO_LIMIT), 3)})


def cap(default: float, scale: float, ceiling: float = 1.0) -> float:
    """Highest volume allowed for a speaker whose default volume is `default`."""
    if scale >= NO_LIMIT:
        return ceiling
    return max(0.0, default * scale)


def clamp(requested: float, default: float, scale: float, ceiling: float = 1.0) -> float:
    return min(requested, cap(default, scale, ceiling))


def label(scale: float) -> str:
    return "off" if scale >= NO_LIMIT else f"{round(scale * 100)}%"
