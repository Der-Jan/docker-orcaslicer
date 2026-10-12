# Printer panel

The image opens the live Selkies desktop inside a full-page frame and adds a
collapsible **Device** panel with Fluidd on the right. Its icon is OrcaSlicer's
`resources/images/tab_monitor_active.svg`, embedded unchanged in the wrapper. Pin it beside the desktop or drag its
left edge to resize it. Width and pin preference are stored in the browser.

Runtime assets live in `root/defaults/printer-panel/`; the saved-profile watcher
is `root/usr/local/libexec/orcaslicer-panel.py`. The `init-orcaslicer-panel`
oneshot runs after the base image has generated nginx and copied the Selkies
dashboard. It preserves that dashboard as `desktop.html`, installs the wrapper,
and includes a generated proxy configuration from `/run/orcaslicer-panel`.
`svc-orcaslicer-panel` updates the routes and metadata every two seconds. It
runs as root to reload nginx; OrcaSlicer continues to run as `abc`.

The selected profile's saved `print_host` and `printhost_apikey` are used.
The key is sent to Moonraker as an `X-Api-Key` header for HTTP and WebSocket
handshakes. It is never returned in browser config or metadata. Generated nginx
configuration containing the key is readable only by root. Browser forwarding
headers are removed; when Orca supplies a key, browser Authorization is replaced
by that key. Key changes trigger the same reload/reconnect as printer changes.
 No target address can
be supplied by the browser. Missing, ambiguous or unsupported addresses leave
the printer frame disconnected and show a status message. Current support is
for QIDI/Fluidd, with Moonraker on port 7125 and the `/webcam/` camera route.
IPv6 addresses, credentials embedded in URLs, and printer URLs containing paths
are unsupported. The panel follows `presets.machine` in `OrcaSlicer.conf`, not
unsaved UI selection. Profiles with inherited host settings must save an explicit
`print_host` in their selected user machine profile. The browser polls metadata
and replaces the printer iframe on changes to close its old WebSocket/camera.

The web page, API, WebSocket and camera use the existing nginx entrypoint. Keep
that entrypoint behind your normal authentication. No additional port is needed;
the printer only needs to be reachable from the container. Fluidd camera URLs
received over WebSocket also require a root `/webcam/` route; reverse proxies
that expose only a `SUBFOLDER` must route this camera path to the container too.

Build and recreate the container to enable the panel. A restart alone keeps the
old image. Save open Orca projects before recreating it. `desktop.html` remains
a direct link to Selkies for troubleshooting.

Validation: build the image, run it against fresh and existing `/config`, check
`[ls.io-init] done.`, then open its HTTPS endpoint. Confirm the desktop is live,
open the panel, verify Fluidd status and camera, pin/resize/close it, and check a
saved printer-profile change. Do not click printer controls during UI checks.
Local browser tooling, test configuration, logs and screenshots are ignored
under this directory rather than stored in `/tmp`.
