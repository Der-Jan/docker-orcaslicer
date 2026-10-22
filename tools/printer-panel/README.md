# Printer panel

Follow the repository's [UI requirements](../UI-GUIDELINES.md) for every change:
all custom user-facing UI and backend messages must be in English.

The image opens the live Selkies desktop inside a full-page frame and adds a
collapsible **Device** panel with Fluidd on the right. Its icon is OrcaSlicer's
`resources/images/tab_monitor_active.svg`, embedded unchanged in the wrapper. Pin it beside the desktop or drag its
left edge to resize it. Width and pin preference are stored in the browser.

The embedded QIDI camera's fullscreen button opens a camera-only view inside
the Device panel. Orca remains visible beside it; the browser does not enter
fullscreen. The image preserves its aspect ratio. **Back to Device**, the
camera button, or Escape returns to the previous Fluidd view without reloading
it or replacing the camera stream. This avoids QIDI's site-root navigation
opening the Selkies desktop inside the printer frame. The handler is attached
again after profile changes.

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

## Nextcloud account connection

The separate Nextcloud edge button opens a native panel for Login Flow v2,
without embedding Nextcloud. Enter the server's HTTPS base URL and approve the
login in the tab opened by the button (or use the fallback link).
`svc-orcaslicer-nextcloud` runs as `abc`, listening only on `127.0.0.1:8093`;
nginx forwards `printer-panel/nextcloud/` behind the existing browser entrypoint.
Mutating requests require JSON and a custom header; no CORS access is granted.

Polling happens in the backend for at most twenty minutes. Only the login URL
is returned to the browser; the poll token remains in memory. The returned app
password is persisted with mode `0600` inside the mode `0700` directory
`/data/orcaslicer/nextcloud/`. The service does not follow HTTP
redirects; returned endpoints must stay under the configured HTTPS origin and
base path. Both the container and browser must reach the configured server.
Nextcloud's login API must not require an additional reverse-proxy login from
the container. Normal Nextcloud login, including SSO and 2FA, stays in the browser.

Disconnect attempts to revoke the app password and removes its local copy.
If revocation fails, the UI asks for manual revocation in Nextcloud's security
settings. A connected status means credentials were obtained, not that a live
WebDAV health check has passed. A full cloud file/3D browser is not included yet.

After connecting, a folder picker lists direct child folders through WebDAV
`PROPFIND` with `Depth: 1`. The OCS user endpoint resolves the account UID;
the login name may be an email address and is not assumed to be the WebDAV UID.
Navigate existing folders, return with the up arrow, and select the current
folder with **Use this folder**. Selection is validated against WebDAV
before the relative path is stored in `credentials.json`. Existing connections
without a folder automatically show the picker. Folder selection persists
across restarts and does not transfer, delete, or create remote files.

Run `PYTHONDONTWRITEBYTECODE=1 python3 tools/printer-panel/test-nextcloud.py`
to check polling, cancellation, revocation, persistence, URL restrictions,
secret-free status, and the HTTP mutation guard with a simulated Nextcloud.

## Synchronization

The same supervised Nextcloud backend runs one bisync worker using rclone's
current official release, installed with SHA256 verification in both images.
Start/stop is explicit and persistent. A 30-second interval and **Sync now**
trigger serialized runs; a filesystem lock also guards against a second worker.
**Sync now** requests an immediate run without enabling a stopped sync or bypassing
project protection. The UI acknowledges the request immediately and displays
requested, preparing, checking, transferring, waiting, idle, error and stopping
states. Live transfer counts, bytes and speed come from rclone's JSON statistics;
an indeterminate bar is used until a transfer total is known. Waiting states
show why the sync cannot run. A completed check updates the last-success time
even if no files needed changing. Statistics are read from the existing child
process output; no rclone control port is exposed.
The app password is passed to `rclone obscure` on stdin and supplied to the
WebDAV remote through its child environment, never through command arguments
or browser responses. No additional credential config file is generated.

The selected dedicated remote subfolder mirrors `/config/Projects`. Root-folder
sync is disabled. The first resync merges both sides, preserving remote versions
replaced by the merge under the account's `.orcaslicer-sync-history/` folder.
Later conflicts retain local/cloud numbered filenames with their original
extensions. Deletions propagate both ways. The hidden `.orcaslicer-sync-check`
file guards root access and prevents an empty project folder from failing
bisync's empty-listing check; never remove it. Baselines and recovery data live
in the private `bisync/<identity>/` directory, separately for each server,
account, remote folder and filter set. No temporary file-content copies or
application upload queue are used. Rclone's own temporary download files are
excluded (`*.partial`), along with `*.part`, `*.tmp` and `*.new`. Local files changing during transfer remain
subject to rclone's normal source-update checks.

Open saved Orca projects pause the whole sync, including uploads, until a new
project is opened or Orca exits. Missing/ambiguous live plugin status also pauses
sync. This is intentionally conservative: it does not try to download into an
open project or dynamically change bisync filters. Interruptions use SIGINT and
rclone recovery, with forced termination after a grace period. Critical bisync
lockouts are shown rather than silently issuing a destructive resync. Inspect
`bisync/<identity>/sync.log` to diagnose them; automatic manual-recovery UI is
not implemented. Stop sync before choosing a different remote folder.
