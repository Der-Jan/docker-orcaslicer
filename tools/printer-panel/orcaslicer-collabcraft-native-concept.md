# Native CollabCraft storage for OrcaSlicer

Status: proposed implementation concept, 28 October 2026.

OrcaSlicer opens and saves cloud projects through CollabCraft using its native
project commands. CollabCraft handles account linking, provider access and
conditional writes; Nextcloud stores the files. Orca keeps its existing model
importers, 3MF serializer, slicing engine and print workflow.

The implementation belongs in an OrcaSlicer fork or upstream contribution. This
Docker repository packages the resulting build and retires its existing cloud
helpers after the native replacement has been tested. This concept does not
change the running installation.

## Decisions from the discussion

- An empty project can receive a name without creating or opening a 3MF. Empty
  project archive roundtrips are not a requirement for this work.
- The first STL/STEP drop proposes a project name without the file extension and
  asks the user to choose or create a cloud project folder.
- That folder can contain only `Sources/` initially. The 3MF appears on the first
  successful project Save.
- Original models use the normal GUI import, including STEP import options.
  No CLI conversion or prepared project archive is involved.
- Existing project and source associations are explicit. Further imports reuse
  the selected folder; display names do not establish an association.
- Editing should remain available during a cloud upload. Save completion uses
  the normal CollabCraft operation result, with no additional acknowledgement
  protocol or user confirmation dialog.
- Cloud operations run inside Orca. There is no project directory mirror,
  periodic synchronization, custom Nextcloud client or required companion
  process in the target architecture.
- New UI text is English, following [the repository UI guidelines](../UI-GUIDELINES.md).

These decisions refine the earlier concept in
`solidcraft/docs/orcaslicer-collabcraft-native-concept.md`. In particular, its
empty-project Save As/reopen acceptance case is replaced by naming an empty
project without writing a project file. The October 27
[handoff API](nextcloud-handoff.md) describes the existing transitional approach,
not the target architecture.

## User workflow

### Account setup

Orca connects to the configured CollabCraft service and uses its existing
desktop sign-in and account-linking flow. The user links Nextcloud through
CollabCraft. Orca does not obtain a Nextcloud app password or implement WebDAV.
OrcaCloud profile synchronization remains a separate feature.

The client reuses CollabCraft's account and file references. A native wxWidgets
file/folder picker adapts the existing service operations; the Rust/egui Craft
widgets are not embedded into Orca. Credentials and session tokens stay in the
application's private credential storage, outside project files.

### Name an empty project

An action such as **Name project** changes its display name and suggested save
name. Naming alone creates no cloud folder or 3MF, and does not mark the project
as cloud-saved. Creating a named empty project through the first-drop dialog is
also supported.

Name, project folder and print-project file are separate state. A user may name
a project now and choose its folder on the first model import or Save As.

### First STL or STEP drop

For an empty project without an assigned cloud folder:

1. Receive the original file through Orca's native drop entry point. In the
   browser container, Selkies may first place it on the Desktop; the Wayland
   helper only delivers that original file to Orca.
2. Show **Project name** and **Project folder** together. Propose the source
   filename with its final `.stl`, `.step` or `.stp` extension removed, unless
   the user has already named the project.
3. Let the user explicitly choose an existing project folder or create a new
   one under the selected cloud location. The suggested folder name follows
   the project name but remains editable. Folder creation requires acceptance
   of this dialog.
4. Choose the existing source directory associated with that project. Reuse
   `Sources` or `Quellen`; if both exist without an association, ask which one
   to use. New projects use `Sources`.
5. Upload the original file through CollabCraft and retain the returned file
   reference and version. Do not overwrite an existing source automatically.
6. Import the original bytes through Orca's existing GUI loader. Keep normal
   STEP quality, component and cancellation dialogs.
7. Associate the imported geometry with the cloud source reference. The active
   print project is still unsaved; no 3MF has been manufactured to name it.

For example, after the first successful source upload:

```text
/Slicer/Gehäuse/
  Sources/
    Gehäuse.step
```

The original local file remains available until upload and import have been
resolved. A failed upload offers retry or an explicit local-only import; it
must not be presented as cloud storage success. No durable background upload
queue is introduced. If the GUI import is cancelled after a successful upload,
the source remains in the chosen cloud folder. Orca adds no object and does not
delete the uploaded source automatically.

When several files are dropped at once, choose the project folder once and
propose a name from the first file. Use the normal multi-model import questions.
Report individual upload/import failures; no multi-file transaction is assumed.

### Further imports and source collisions

Further drops into a cloud-associated project reuse its source folder and keep
its name. A filename collision offers another filename, or reuse of the exact
existing source when its identity and content have been verified. Importing a
new file does not implicitly replace linked geometry or a SolidCraft export.

Opening an existing local project retains normal local behavior. Cloud
association is a deliberate operation, not something inferred from its name.
Choosing an existing folder also does not silently open a 3MF found there; the
user explicitly selects an existing print project when that is the intention.

### First project Save and Save As

For a project with geometry and an assigned folder but no 3MF entry, **Save**
proposes `<project-name>.3mf` inside that folder. The user can choose another
name. CollabCraft creates the entry using its normal create operation. Only
success establishes the active print-project file identity and saved baseline.

Later **Save** writes the active entry with its last returned version. **Save
As** explicitly chooses another entry/destination and adopts it only after
success. **Save As local file** remains available. A failed Save As preserves
the previous origin and all current edits.

Renaming the active project does not implicitly move or rename cloud entries.
Folder/file rename operations are explicit; source links use stable identities
rather than reconstructed paths.

## Document state and cloud references

The document needs the following distinct information:

| State | Meaning |
| --- | --- |
| Document instance | Identifies this open document; late results cannot affect a different project. |
| Display name | User-selected name, independent of a stored 3MF. |
| Project location | CollabCraft service/account and chosen folder reference; optional before association. |
| Print-project origin | Unsaved, local file, or CollabCraft file reference with its returned version. |
| Source associations | Stable source references attached to the relevant model parts. |
| Save state | Current edits, last saved baseline, and any in-flight save. |

Cloud references reuse the existing Craft/service contract: entry identity,
account/service reference, parent, display name/path and observed version as
available. Do not invent another provider identity scheme. Persist source
associations in compatible Orca 3MF metadata, including the relationship between
source objects/volumes and imported parts. Preserve existing SolidCraft design
IDs and agent session references when a handoff supplies them; credentials are
never persisted there.

Local path fields remain usable by the existing loaders. A path to an internally
materialized source must not become its durable identity. After restart, reload
resolves the saved cloud reference again.

## One native Open and Save operation

Introduce a small document-storage layer between the native project commands
and their destination. Local files keep their existing behavior. Cloud commands
use the same serialization/import machinery through a CollabCraft adapter.

The existing normal save path is synchronous. It exports a local archive and
then resets the saved state. Other callers export directly and reset state
separately, including the save-before-close path. Route these actual project
save commands through one controller so no caller can mark a cloud document
saved merely because temporary serialization succeeded. Keep **export a copy**,
internal backup, printer preparation and ordinary project Save distinct.

### Cloud Open

Resolve the selected CollabCraft entry, obtain its content and load it through
the normal Orca project loader. Apply the user's normal save/discard/cancel
handling for the current document first. Associate the new cloud origin only
after successful loading. A failed or cancelled operation must not masquerade
as a successful open or place a temporary filename into recent projects.

### Cloud Save with editing during upload

1. Capture a consistent document revision and serialize it using the existing
   3MF writer. Short serialization work may initially occupy the UI thread as
   it does today; background serialization is not required for the first phase.
2. Upload that immutable archive in the background through the existing
   CollabCraft write operation, passing the opened entry's version.
3. Keep model editing enabled. Show **Saving project…**, with real transfer
   progress if the client provides it. Permit one in-flight save per document;
   disable additional Save/Save As operations until it finishes. Do not queue
   snapshots or launch competing uploads for the same entry.
4. On normal service success, record the new entry version and the submitted
   revision as the saved baseline. If the document has changed since that
   revision, it remains dirty. An upload result for a previous document cannot
   alter the current document.
5. On failure or version refusal, retain the edits and use the normal native
   error/conflict UI. CollabCraft enforces conditional writes; Orca does not
   add a second conflict detector, merge scheme or unconditional overwrite.

A successful source upload, a successful 3MF serialization and a successful
project cloud Save are separate outcomes. The service's standard write response
is the save result; there is no custom after-save acknowledgement mechanism.
After a transport timeout with an uncertain outcome, inspect the entry/version
before offering a retry rather than assuming the previous write did not happen.

The save revision must cover model changes, selected settings and project/source
metadata. Use the existing undo/redo and dirty-state mechanisms where practical.
The current API marks the current snapshot saved; tracking an earlier submitted
revision may need a narrow extension. Changes to `ProjectDirtyStateManager`
itself are not a predetermined requirement. Tests must establish that saving A
while editing B never marks B saved, including after undo/redo and preset edits.

Closing or switching projects while saving offers waiting, cancellation where
supported, or explicit handling of remaining unsaved work. An unconfirmed upload
must never silently satisfy the existing save-before-close prompt.

## Sources and refresh

Refresh resolves the explicitly linked source through CollabCraft and passes
the downloaded bytes to the existing reload machinery. Reuse the logic that
preserves object placement, instances, plates and print settings. Match by the
saved source/part association, not by display names alone, and do not append a
second object as a substitute for refresh.

Respect changed topology: report any settings or face-based assignments that
cannot be retained. On cancellation, missing sources or a version conflict,
preserve the current model and make the outcome visible.

Original files imported by Orca can be created in `Sources`. Orca does not rewrite
SolidCraft designs, reference inputs or exported source files. SolidCraft may
update its linked sources through its normal Craft cloud flow. An optional
future authenticated in-process handoff selects those project/source entries
and invokes the native commands; it does not synchronize a shared directory.

## Temporary files and completed G-code

Orca's current project and STEP APIs accept filenames. Reuse them with
application-owned temporary archives/source files; no persistent project cache
is required. Keep a materialized file alive while import, reload, preview or a
print job still needs it. Ordinary source files received on the Desktop are
not deleted as a side effect of importing them.

Cloud G-code export is a later phase, separate from project Save. Reuse existing
generation, classic post-processing scripts and slicing-pipeline plugins. Upload
only the completed artifact, using its final output name, into the selected
project folder. Do not run post-processing again just for the cloud write.
Preserve backing files still used by preview or printer upload.

Saving a 3MF never implicitly slices or sends a print job. If archiving the exact
G-code sent to a printer is added, report print submission and cloud archiving
separately; retrying an archive upload must not submit another print job.

## Orca code areas

The inspected main revision is
[`42608687e569e3eb77ffceb49459beca9ac26a14`](https://github.com/OrcaSlicer/OrcaSlicer/commit/42608687e569e3eb77ffceb49459beca9ac26a14),
28 October 2026. These are implementation boundaries, not a mandatory class
hierarchy or a demand to refactor the entire application.

| Area | Existing entry points and proposed work |
| --- | --- |
| Document state/name | `Plater.cpp/.hpp`: retain the existing distinction between `m_project_name` and local folder; add explicit cloud location/origin. Expose naming without creating a file. |
| Native UI | `MainFrame.cpp` and focused wxWidgets dialogs: account access, first-import name/folder selection, cloud Open/Save As and operation status. |
| Drop/import | `PlaterDropTarget::OnDropFiles()`, `Plater::load_files()` and `Plater::priv::load_files()`: source-upload orchestration around existing STL/STEP GUI import. |
| Service adapter | New CollabCraft client adapter and build wiring. Reuse the existing desktop auth/file protocol. Verify folder operations against a pinned service/client revision before implementation. |
| Project persistence | `Plater::load_project()`, `save_project()`, `export_3mf()` and `MainFrame::save_project_as()`: one save completion path, internal archive adaptation and explicit origins. |
| Saved baseline | `ProjectDirtyStateManager`, `UndoRedo` and save controller integration: distinguish the uploaded revision from edits made afterwards; change only the APIs that need it. |
| Sources/reload | `ModelVolume::Source`, model copy/serialization, `bbs_3mf.cpp` and `reload_from_disk()`: persistent cloud associations, resolution and geometry reload. |
| Document lifecycle | New/reset/close, recent projects and recovery: do not retain a previous document's origin or expose a temporary path as a saved destination. |

The full project-storage feature is roughly 20–30 source/build files including
new components, plus tests; this is an initial scope estimate. The service client
adapter and exact native picker integration are the main unresolved sizing
choices. G-code cloud export is additional work. No changes to slicing algorithms
or a rewrite of the STL/STEP parsers are planned.

## Implementation and cutover

1. **Storage foundation and source import:** build the Orca fork, pin the
   CollabCraft contract, add account access and folder operations, expose project
   naming, and implement the first-drop dialog plus source upload/GUI import.
   Exercise this on disposable cloud folders.
2. **Native project persistence:** implement cloud Open, Save, Save As, local Save
   As, returned versions and save-before-close. Make upload progress and errors
   native UI state; allow editing during upload with correct saved baselines.
3. **Persistent source reload:** persist associations in 3MF, reopen after a
   restart, and refresh linked geometry without duplicates or misplaced parts.
4. **Container cutover:** package the tested fork. Before using a cloud root
   natively, explicitly stop the old sync for that root and finish/resolve any
   in-flight legacy transfer. Never let legacy bisync and native cloud writes
   manage the same files concurrently. Keep existing local projects and backups;
   linking/importing them is explicit and does not move or delete them. Replace
   the old Nextcloud panel with the native account/storage flow and remove the
   CLI project preparation path. Retain the Wayland drop transport if needed.
5. **Optional extensions:** cloud G-code export/archiving and entry-based
   SolidCraft handoff through the native operations.

During development, keep the current installation working; use separate accounts
or disposable folders for native integration tests. Retirement of the Python
Nextcloud service, rclone and Project storage plugin follows replacement of
their required functions. Rollback preserves data and requires reconciling cloud
changes before the old sync is enabled again.

## Acceptance

- Naming an empty project creates no 3MF; renaming changes no existing cloud path.
- First STL and STEP drops propose names without extensions, select/create one
  project folder, preserve originals, and use native import options.
- Sources reach that folder before any 3MF exists. The first Save places the
  print project beside them. Further drops preserve the association and title.
- Existing `Quellen`, filename collisions, multiple simultaneous source imports,
  cancelled import, failed folder creation and failed upload have visible,
  non-destructive outcomes.
- Cloud Open, Ctrl+S, cloud/local Save As and save-before-close use the correct
  origin. No temporary filename becomes a recent-project identity.
- Save A/upload while editing B leaves B dirty. Test preset changes, undo/redo,
  another project opening, cancellation, offline service and version refusal.
- Model/source references survive 3MF save, application restart and reopen.
  Refresh preserves placements/settings where applicable and adds no duplicates.
- SolidCraft design, input and exported-source files are never overwritten by
  Orca. No periodic directory sync is needed for the native workflow.
- Existing local projects and OrcaCloud profile sync continue working.
- Optional G-code export uploads the finalized, once-processed bytes and never
  turns an archive retry into a second print submission.

## References

- [CollabCraft service](https://github.com/wdunn001/collab-craft): reuse its
  existing service/client contracts and pin a tested revision.
- [Orca name and local filename handling](https://github.com/OrcaSlicer/OrcaSlicer/blob/42608687e569e3eb77ffceb49459beca9ac26a14/src/slic3r/GUI/Plater.cpp#L14410-L14536).
- [Orca native drop/import](https://github.com/OrcaSlicer/OrcaSlicer/blob/42608687e569e3eb77ffceb49459beca9ac26a14/src/slic3r/GUI/Plater.cpp#L7809-L7838).
- [Orca project Save](https://github.com/OrcaSlicer/OrcaSlicer/blob/42608687e569e3eb77ffceb49459beca9ac26a14/src/slic3r/GUI/Plater.cpp#L16327-L16394)
  and [MainFrame save path](https://github.com/OrcaSlicer/OrcaSlicer/blob/42608687e569e3eb77ffceb49459beca9ac26a14/src/slic3r/GUI/MainFrame.cpp#L2007-L2021).
- [Orca source data](https://github.com/OrcaSlicer/OrcaSlicer/blob/42608687e569e3eb77ffceb49459beca9ac26a14/src/libslic3r/Model.hpp#L836-L853)
  and [reload entry point](https://github.com/OrcaSlicer/OrcaSlicer/blob/42608687e569e3eb77ffceb49459beca9ac26a14/src/slic3r/GUI/Plater.cpp#L12120-L12225).
