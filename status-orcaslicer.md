# OrcaSlicer customization status

Updated: 2026-10-10

## Repository and branches

This repository is the user's customized OrcaSlicer image. The GitHub remote
`personal` points to `git@github.com:Der-Jan/docker-orcaslicer.git`; `origin`
remains the LinuxServer upstream repository.

- `master` uses the official OrcaSlicer 2.5 nightly AppImage and includes Support Fins.
- `mcp-integration` preserves the MaxEllis 2.4.2 MCP fork, separate Python MCP
  server, and optional OpenAI tunnel. It was pushed to the user's repository at
  commit `ef87114` before switching to nightly.
- The running container is `slicer-orca-1`. Its persistent `/config` mount comes
  from `/opt/containers/slicer/config`. The compose service is `orca` in
  `/opt/containers/slicer`.

Image builds and disposable-container tests do not upgrade that running
container. Save any open project before recreating it with the desired image.

## Window decoration and rendering

Both Dockerfiles add a targeted labwc rule that removes the extra desktop
frame from OrcaSlicer's normal main window. Dialog decorations remain enabled.
Commit: `2bf803d`.

The user confirmed that setting Selkies scaling to 100% (96 DPI) resolved the
black 3D axes widget observed in the earlier MCP fork.

## Native Wayland drag and drop

The helper and upload watcher live in `tools/wayland-file-drop`, rather than a
prototype or temporary directory. Both Dockerfiles compile the helper and
install it with the watcher under `/usr/local/libexec`.

The s6 service `svc-orcaslicer-desktop-drop` waits for the application-facing
Wayland socket (`/config/.XDG/wayland-0`), then watches completed Selkies uploads
as `abc`. It offers the uploaded file through native Wayland `text/uri-list`
drag and drop using a small temporary source surface and virtual pointer. No
Selkies, labwc, or application patches are required, and MCP is not involved.

- Completed `.selkies-upload-*.part` renames trigger drops for STL, OBJ,
  STEP/STP, and 3MF files in `FILE_MANAGER_PATH`, default `/config/Desktop`.
- The destination is the center of the single Wayland output. OrcaSlicer must
  be ready on Prepare, with its canvas covering that point.
- Existing files, ordinary saves, unfinished uploads, hidden files, symlinks,
  and `.gcode.3mf` files are ignored.
- Failed or ambiguous drops are never retried automatically. Uploaded files
  remain available for manual import.
- The service is disabled in X11 mode. Multiple outputs are unsupported.
- A lock prevents duplicate watchers. s6 restarts the watcher if it exits.

Native drops and real Selkies uploads were demonstrated successfully in the
running container. An isolated image test also verified upload import,
container restart, and watcher recovery after termination.
Commits: `a1f2ac1` and `1741f99`. See the helper README for limitations and
manual development commands.

## Nightly and Support Fins

Commit `7d67c33` switches both architectures to official nightly AppImages,
checks their published SHA-256 digests, and builds the Support Fins plugin from
`gittrahan/support-fins`. Its build-only esbuild executable and source archive
are removed after packaging.

The default application release is `nightly-builds`. `SUPPORT_FINS_VERSION`
can select a source commit or tag; otherwise the build resolves the current
`main` commit and records it in `/defaults/support-fins/VERSION`.

The oneshot `init-orcaslicer-plugins` installs the bundled plugin as `abc` in
`/config/.config/OrcaSlicer/orca_plugins/SupportFins` before desktop startup.
Existing plugin directories are preserved. Rebuilding the image does not
overwrite an existing user installation; its update source is
`/defaults/support-fins/support_fins_orca.py`.

To use it:

1. Enable Support Fins in OrcaSlicer's Plugins dialog. Orca's bundled `uv`
   installs numpy and mini-racer on first activation, requiring internet access.
2. Select Advanced process settings, then add Support Fins under
   Others > Slicing Pipeline Plugin.
3. Disable Orca's normal supports for the parts that should receive fins and
   approve the plugin permission prompts when slicing.

Fins are generated during slicing and appear in Preview. Activating the plugin
alone does not add it to a process preset.

The amd64 image `orcaslicer-nightly:test` was built successfully. Tests confirmed
fresh and existing configuration startup, Python initialization, plugin
discovery and activation, installation of dependencies, and successful plugin
loading after recreating the test container. The plugin file checksum was
unchanged. A full fin-generating slice was not tested.

The ARM64 Dockerfile was updated, but its build could not run on this host:
`exec format error` because ARM emulation is unavailable.

The running user container was not recreated during these tests. Temporary
test containers and copied configuration files were removed afterwards.

## Preserved MCP configuration

The `mcp-integration` branch installs the MaxEllis AppImage, `orcaslicer-mcp`,
and the OpenAI tunnel client. Set `MCP_TUNNEL_ID` and mount the runtime API key
at `/run/secrets/mcp_runtime_api_key`. The launcher reads the Remote API token
and port from OrcaSlicer's saved configuration; the service waits until that
API is enabled. No credentials are committed.

This integration is absent from the nightly branch. Adding MCP to the newer
application remains future work.

## Cloud login observation

Cloud login data resides in the persistent OrcaSlicer configuration directory.
With encrypted-file storage enabled, `orca_refresh_token.sec` stores the
refresh token and `.orcaslicer_machine_id` holds OrcaSlicer's persistent
identity. The last inspected startup log reported a refresh-token integrity
check failure (HMAC mismatch), and the token file was absent afterwards. The
cause of the mismatch was not established; the `/config` mount was persistent.
