# server/dashboard/builtin_plugins/broker_uptime.py
import json
from dash import html, Input, Output

name = "Broker Uptime"
zone = "topbar"

BADGE_ID = "uptime-badge"
TOPIC = "halloween/broker/uptime"  # you said Mosquitto publishes this

def layout():
    # Single element with fixed id; we'll only change its text and className
    return html.Span("Broker: —", id=BADGE_ID, className="badge bg-secondary")

def register_callbacks(app, services):
    mqtt  = services["mqtt"]
    cache = services["cache"]
    tick  = services["tick_id"]  # "global-tick"

    def _on_uptime(topic, payload: bytes):
        cache["broker_uptime_raw"] = payload.decode("utf-8", errors="replace")

    mqtt.subscribe(TOPIC, _on_uptime)

    @app.callback(
        Output(BADGE_ID, "children"),
        Output(BADGE_ID, "className"),
        Input(tick, "n_intervals"),
    )
    def _render(_):
        val = cache.get("broker_uptime_raw", "—")
        # Try to format seconds as HH:MM:SS if possible
        try:
            # Payload may be a bare number or JSON like {"uptime_s": 67}
            parsed = json.loads(val)
            secs = int(parsed["uptime_s"] if isinstance(parsed, dict) else parsed)
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            val = f"{h:02d}:{m:02d}:{s:02d}"
        except Exception:
            # keep val as-is if it's already a string like "1d 2h 3m"
            pass
        # Return ONLY text for children, and optionally adjust color
        color = "success" if val != "—" else "secondary"
        return f"Broker: {val}", f"badge bg-{color}"
