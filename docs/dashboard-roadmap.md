# Dashboard roadmap (awaiting approval)

Proposed changes that would alter how props are driven from the dashboard.
None of these are implemented. Each needs Thomas's go before work starts.

The Jarvis view itself (branch `develop/2026/agent-jarvis-dashboard`) only
changes presentation: it renders the existing plugins unchanged and sends
exactly the same MQTT commands as the classic view.

## Proposed behaviour changes

| # | Prop / area | Proposal | Why |
|---|-------------|----------|-----|
| 1 | Tesla, Coffin Jumper | Two-step confirm ("press again within 3 s") on **Open Trunk**, **Close Trunk**, **Resync Tesla** and **Trigger**. | These move physical things (and Resync may lock the car). One stray click on a touch screen should not do that. |
| 2 | All props | Global **All stop** button in the Jarvis header that sends `stop` to every worker prop and `stop_action` + `stop_music` to the Coffin Jumper. | One place to silence everything if a scare goes wrong. |
| 3 | Tesla, Thriller | The **Log status** button (`thn-cc-log`, `thriller-cc-log`) has no callback, so pressing it does nothing. Either wire it to a `{"action": "chromecast", "args": "status"}` command (needs a matching worker handler) or remove it. | Dead button. |
| 4 | Tesla, Thriller | **Pause** is shown but permanently disabled. Implement pause like Dungeon Nest, or hide it. | Placeholder clutter. |
| 5 | Coffin Jumper | Replace the single **Block/Unblock** toggle (which decides from the cached state) with explicit **Block** and **Unblock** buttons. | If the cached state is stale, the toggle sends the opposite of what the label shows. |
| 6 | All Nest props | Replace the number box + **Set Vol** with a slider that sends on release. Payload stays `{"action": "chromecast", "args": {"volume": x}}`. | Faster and harder to mistype (e.g. `5` instead of `0.5`). |
| 7 | Dashboard / broker | Mark a prop as **stale** when its retained `availability` says online but no telemetry has arrived for N seconds. | Retained "online" survives a worker crash or container stop; the dashboard currently shows stopped props as online. |
| 8 | Broker ACL | Give the `dashboard` user read access to `halloween/+/cmd`, so the Jarvis comms log shows commands from every source (classic view, scripts), not just the Jarvis view. | Complete audit trail during the night. |

## Dashboard-only ideas (no prop behaviour change)

These could be done without approval, but are listed so they can be weighed together.

- `DASHBOARD_DEFAULT_VIEW=jarvis` env setting to open the Jarvis view at `/`.
- Per-prop accent colours in the Jarvis view (e.g. Tesla red, Thriller purple).
- A fourth "Kiosk" layout for a wall screen: status only, no buttons.
