# Halloween 2026 — Field Notes

Observations worth keeping for future deployments, collected while preparing
the 2026 event. The 2025 layout document
([halloween-2025-structure.md](./halloween-2025-structure.md)) is kept as a
snapshot of the 2025 launch and is not updated here.

## Launching the stack

- Start the broker and dashboard with
  `docker-compose.yml` + `docker-compose.dashboard.yml`; the dashboard is at
  <http://localhost:8050>. `docker-compose.dev.yml` only adds the `mqtt_test`
  integration-test service and does not start the dashboard.
- The Tesla/Hue/Nest backend runs in the `worker_host` container
  (`docker-compose.workers.yml`). Set `WORKER_PROPS_ALLOW=tesla_hue_nest` in
  `config/worker_host.env` to load only that prop.
- See the README for full PowerShell launch commands.

## Findings (2026-10-24)

### Hue: wrong import name for `phue2`
- The `phue2` pip package installs the Python module `phue`, not `phue2`.
  `from phue2 import ...` fails with `No module named 'phue2'` even though the
  package is installed (rebuilding the image does not help).
- Fixed by importing from `phue` in both `tesla_hue_nest` and
  `thriller_hue_nest` `backend/hue_API.py`.

### Hue: VPN route hijacks the bridge subnet
- With Cisco AnyConnect connected, traffic for the bridge subnet
  (`10.67.1.0/24`) was routed into the VPN virtual adapter (lower route
  metric) instead of Wi-Fi, so the bridge was unreachable from Windows and
  from containers (`POST request timed out`).
- Disconnecting the VPN restored access. Check with
  `Find-NetRoute -RemoteIPAddress <bridge-ip>`.

### Hue: bridge registration
- First connection from a new container requires pressing the bridge's
  physical link button (error 101 `link button not pressed`) within about 30
  seconds of the attempt.
- The key is stored at `config/secrets/hue/python_hue` (git-ignored), bind-mounted
  into the worker at `/hue` and selected via `HUE_CONFIG_FILE`, so it survives
  rebuilds and container recreation. To register a new key, press the bridge
  link button, then within ~30 s run:
  `docker exec halloween_worker_host python -c "import os; from phue import Bridge; Bridge('10.67.1.54', config_file_path=os.environ['HUE_CONFIG_FILE'])"`

### Tesla
- Worker startup wakes the car, then checks trunk state using the `door_lock`
  command, which can lock the doors; it also closes the trunk if it detects it
  open. Make sure the car is parked and the trunk area is clear before
  starting the worker.
- Worker telemetry for the trunk reads "Closed" and does not reflect a trunk
  opened outside the worker (e.g. by a direct API call or by hand).
- Dashboard trunk buttons publish `{"action": "tesla", "args": ...}` and do
  reach `do_tesla` (the base worker dispatches `do_<action>` by name).
- Bug fixed: "Close Trunk" failed with `KeyError: 'string'` because the
  MyTeslaMate API returns failure text under `reason`, not `string`
  (`get_trunk_state` in `tesla_API.py`). Verified: restart with the trunk open
  now closes it.
- Stale state: the worker deliberately caches `trunk_open` (no read-only trunk
  query exists; checking uses `door_lock`, which is slow and can lock the car).
  If the trunk is moved outside the worker, the dashboard keeps showing Open and
  pressing Open is a no-op. Use the dashboard **Resync Tesla** button (publishes
  `{"action":"tesla","args":"resync"}`), which re-reads the real state via
  `door_lock` (may lock the doors; takes a moment). A failed open/close also
  triggers an automatic resync.
- Handler exceptions are published to MQTT `halloween/<prop>/status/error` and
  now also printed in `docker logs`. To watch MQTT:
  `docker exec halloween_mqtt sh -c 'mosquitto_sub -h localhost -u "$MQTT_ADMIN_USER" -P "$MQTT_ADMIN_PW" -v -t "halloween/tesla_hue_nest/status/#"'`

### Nest / Chromecast
- With the speakers offline, discovery reports `Connected to 0 Chromecast
  devices`. Re-test once they are online.

## Open items for 2026
- [ ] Re-test Nest speaker discovery with speakers online.
- [ ] Test the dashboard Hue buttons.
- [x] Resync the cached trunk state (dashboard "Resync Tesla" button).
- [ ] Test the ESP32 coffin jumper (Wi-Fi/MQTT).
- [ ] Persist the Hue bridge key across container recreation.
- [ ] Test the ESP32 coffin jumper Wi-Fi/MQTT connection.
