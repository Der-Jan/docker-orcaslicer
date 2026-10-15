# Native Wayland file-drop helper

Standalone source: `drop.c`. Built x86-64 executable: `wayland-file-drop`.
No image, Selkies, labwc, or application modifications. No MCP/API calls.

The helper maps an 80×80 green overlay at the output's top-left corner. It uses
the compositor's virtual-pointer protocol to move onto that source, press the
left button, and obtain a real pointer event serial. It starts a standard Wayland
drag offering `text/uri-list`, moves to the chosen destination, and releases.
The destination requests the percent-encoded file URI and imports it normally.
The source remains mapped until completion, cancellation, or a bounded timeout.

## Try it in the running container

This imports a model into the active project. Select Prepare first. Set scaling
to 100% for the verified configuration, and choose a point inside the 3D canvas,
away from dialogs, settings panes, or other windows. Do not move the pointer
during the short synthetic gesture.

```bash
docker exec slicer-orca-1 mkdir -p /config/tools/wayland-file-drop
docker cp tools/wayland-file-drop/wayland-file-drop \
  slicer-orca-1:/config/tools/wayland-file-drop/wayland-file-drop
docker exec --user abc \
  -e WAYLAND_DISPLAY=wayland-0 -e XDG_RUNTIME_DIR=/config/.XDG \
  slicer-orca-1 /config/tools/wayland-file-drop/wayland-file-drop \
  /config/Desktop/GearGrooveGarmin-fins.stl 2592 1612 1296 806
```

The final four arguments are output width, output height, drop X and drop Y.
Use the current remote desktop dimensions, not the browser window dimensions.
Without those arguments it detects the current output dimensions and drops at
the screen center. `--inspect` reports the detected center without starting a drag.

Requires `wl_compositor`, `wl_shm`, `wl_seat`, `wl_data_device_manager` version 3,
`zwlr_layer_shell_v1`, and `zwlr_virtual_pointer_manager_v1` on the app compositor.
Multi-output layouts and fractional scaling are not covered by this helper.
Automatic center mode refuses sessions with multiple outputs.
The helper changes pointer position and may change focus. It does not retry:
a timeout or cancelled drag does not prove that the application did not import.

## Automatically drop completed uploads

The container's watcher now routes uploads through
`/usr/local/libexec/orcaslicer-project-drop.py`. The bundled **Project storage**
Python plugin reports the actual open project and exports its settings on request.
It is installed and enabled by `init-orcaslicer-plugins`; its status panel hides
after initialization. Run the capability in Orca's Plugins dialog to show it.
The plugin requires a nightly containing `project_path()` and `export_3mf_copy()`.

For a first model in an empty, unnamed editor, the helper creates
`/config/Projects/<model-stem>/<project-name>.3mf` with Orca's headless CLI,
copies the uploaded original into `Sources/`, and opens the complete project.
Printer, process and filament settings and profile differences are retained from
Orca's native snapshot; the CLI uses the selected printer's geometry for arranging.
The project's Description records the original source filename. The model is
already contained in the 3MF and is not imported a second time.

Later model uploads go into `Sources/` alongside the open project's 3MF. They do
not rename that project or automatically save its changes. Existing project
folders and source files receive `-2`, `-3`, etc. suffixes rather than being
overwritten. A dropped 3MF is copied to a new project folder unchanged.
The Desktop original remains available even when preparation/opening fails.
An unnamed editor with existing objects must be saved first.

Keep the main window on **Prepare**. Use **Load All**, or choose **Open as project**
in Orca's normal load dialog. Changed-profile/save/discard prompts are never
confirmed by the helper. Further uploads wait for the actual project path/import
to be confirmed. After cancelling, manually open the prepared 3MF or run the
plugin and choose **Cancel pending import** to release the queue without deleting
anything. Missing, stale or ambiguous plugin sessions fail closed: the Desktop
files are retained. Exactly one editor and one Wayland output are supported.
Runtime messages and CLI staging stay in a private directory under
`/run/orcaslicer-project-storage`; persistent projects stay under `/config`.

This prepares local project folders. Nextcloud synchronization and archiving the
G-code actually sent to a printer require separate integrations. Synchronize
`/config/Projects` for automatically created projects; projects opened from other
directories keep their existing path and place sources beside that file.

Run the association/preservation checks with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tools/wayland-file-drop/test-project-storage.py
```

Both Dockerfiles build and install the helper and watcher under
`/usr/local/libexec`. The s6 service `svc-orcaslicer-desktop-drop` starts the watcher
automatically after the app compositor is available, runs it as `abc`, and
restarts it if it exits. No manual start is needed in a rebuilt image. The service
is disabled in X11 mode. Watcher output appears in `docker logs`.

`watch-desktop.py` watches `/config/Desktop` (or `FILE_MANAGER_PATH`) for the
completed rename of a `.selkies-upload-*.part` file. It ignores files present at
startup, partial uploads, ordinary saves and renames, hidden files, symlinks,
unsupported extensions, and `.gcode.3mf` files. Supported types are STL, OBJ,
STEP/STP, and 3MF. Each completed file is dropped once, sequentially; a failed
or ambiguous drop is logged and never automatically retried. Re-uploading the
same filename is a new event. Uploads made while the watcher is stopped are
not replayed. The uploaded file remains on the desktop.

Keep OrcaSlicer maximized on Prepare with the screen center inside its canvas.
The watcher uses whichever window occupies that point when the upload finishes;
it does not locate OrcaSlicer or dismiss dialogs. A 3MF uses the app's normal
project-opening behavior and may show prompts. The synthetic drag briefly takes
control of the pointer. Folder uploads and grouping several files into one drag
are not implemented.

For development or an older image, install the watcher alongside the helper,
then start it manually for the current session:

```bash
docker cp tools/wayland-file-drop/watch-desktop.py \
  slicer-orca-1:/config/tools/wayland-file-drop/watch-desktop.py
docker exec slicer-orca-1 lsiown -R abc:abc /config/tools/wayland-file-drop
docker exec -d --user abc \
  -e WAYLAND_DISPLAY=wayland-0 -e XDG_RUNTIME_DIR=/config/.XDG \
  slicer-orca-1 sh -c 'exec python3 /config/tools/wayland-file-drop/watch-desktop.py >>/config/tools/wayland-file-drop/watcher.log 2>&1'
```

The manually started watcher must be started again after a container restart.
The image's s6 service starts automatically. Its lock prevents
two watchers for the same directory. Stop it gracefully using the recorded PID:

```bash
docker exec --user abc slicer-orca-1 sh -c \
  'read -r pid </config/Desktop/.wayland-file-drop.lock; kill -TERM "$pid"'
```

Use `--dry-run` to log uploads without importing, and `--once` to stop after the
first eligible upload. `--directory PATH` and `--helper PATH` override defaults.

## Build

Needs a C compiler, pkg-config, libwayland development files, and wayland-scanner.
The XML protocol definitions and their license notices are included here.

```bash
cd tools/wayland-file-drop
sh build.sh
```

## Verified

Tested in a disposable `orcaslicer-mcp:test` container with a copied settings
file and desktop STL. No tunnel was enabled. At 1024×768, a drop at (900,400)
was accepted with the settings pane open. With the pane collapsed, a drop at
the literal window center (512,384) also completed. The app logged
`load_files` and `load_model_objects` for `GearGrooveGarmin-fins.stl`, and the
model appeared on the plate. `center-drop.png` records the result after the
second test (two deliberately imported copies).

Also demonstrated in the user's running `slicer-orca-1` container on 10 October
2026, at 2592×1612 and the window center (1296,806). A single
`GearGrooveGarmin-fins.stl` was imported into the previously empty plate. The
helper reported `Destination finished drag`; the application logged one loaded
object. `live-before.png`, `live-after.png`, and `live-drop.log` record this run.
The helper is installed there at `/config/tools/wayland-file-drop/wayland-file-drop`.

The watcher detected an upload through Selkies' actual HTTP upload endpoint in
dry-run mode. Additional checks confirmed that existing files, ordinary saves
and renames, partial uploads, and excluded file types are ignored. After enabling
the watcher, the user confirmed a real browser upload was imported successfully.
The C helper compiled with `-Wall -Wextra -Werror`; Python syntax checks passed.

An initial drop on the Home page was cancelled and did not import. A valid
application drop target matters; uploading a file or moving the pointer is
insufficient by itself.
