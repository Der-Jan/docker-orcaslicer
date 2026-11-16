# SolidCraft source editing from OrcaSlicer

Status: proposal, 9 November 2026. No runtime changes are implemented by this document.

The first consumer step is now implemented separately: the **SolidCraft sources**
plugin dock resolves SCREF1 references and offers **Edit in SolidCraft…** through
the outer browser. See [usage and current limits](README.md#edit-a-solidcraft-source).
Native object context menus, portable 3MF associations and automatic geometry
replacement remain proposed below.

## Goal

Import a SolidCraft model once, configure it in Orca, and return to the original
CAD design for changes. Sending the revised mesh to Orca updates the linked
geometry in the existing print project, preserving its print settings wherever
possible. It does not create another object on every send.

This extends the existing customized **Send to slicer** flow. It follows the
[native CollabCraft storage concept](orcaslicer-collabcraft-native-concept.md)
and the shared storage contract in
`solidcraft/docs/nextcloud-orca-storage-contract.md`. SolidCraft owns the CAD
design and exported sources; Orca owns the print project and its settings.

## User workflow

1. In SolidCraft, open a cloud design and use **Send to slicer**. The exported
   STL carries a reference to its source. Orca imports it and remembers which
   object or part came from that source.
2. Configure orientation, filament, supports and object settings in Orca.
3. Right-click the linked object and choose **Modify in SolidCraft**. This opens
   the original editable design in the user's browser, using its normal cloud
   origin and Save behavior.
4. Make changes and use **Send to slicer** again. The export retains the same
   source identity, with a new revision or content hash.
5. Orca recognizes the existing association and updates that object's geometry.
   A short notification reports **Updated “Bracket” from SolidCraft**. The print
   project becomes modified and can be saved normally. Undo restores the
   previous geometry and source revision.

Editing a design and exporting it are separate operations. An export must not
claim the original CAD document was saved unless its normal cloud Save actually
succeeded. For a new, unassociated design, establish its cloud origin first.
If unsaved CAD changes are exported, describe the export as a snapshot; opening
the saved origin later might otherwise show an older state.

## Where “Modify in SolidCraft” belongs

The preferred location is the **object list context menu**, next to actions
such as reload or replacement. Offer the same action in the 3D canvas context
menu when it resolves to one linked object. For a linked part in a multipart
object, act on that part's source rather than assuming the entire object has
one CAD origin.

Show the action only when a valid source association exists. For multiple
different sources, require a specific object or part. A secondary **Source**
section can show the design name, last imported revision and **Unlink source**.
No additional permanent browser edge tab is needed.

The current Orca Python integration has no verified hook for adding arbitrary
object context-menu items. A native menu therefore requires an Orca extension.
An initial supported plugin action could open a small **SolidCraft sources**
dock panel listing linked objects, with **Modify in SolidCraft** for each one.
Selection tracking should be added only after checking the available API;
otherwise the user explicitly chooses the object in that panel.

For this streamed desktop, the link should open in the user's outer browser,
preferably reusing its SolidCraft tab. A plugin opening a URL inside the
container does not accomplish that. A browser-facing link/action needs a
controlled handoff to the outer UI and a user gesture. The destination must
resolve the design through the normal Craft cloud Open flow. SolidCraft's
existing linked-download path, which opens an unsaved copy, is unsuitable here.

## Identity: design, exported source and Orca destination

Use existing Craft service/account/file identity for the original CAD design.
Do not introduce another identity for the same design. Names, local paths and
folder names are display information, not matching keys.

Design identity alone opens the right CAD document, but cannot distinguish two
different bodies exported from that document. The association therefore needs:

| Reference | Purpose |
| --- | --- |
| Existing cloud origin | Resolve and open the original editable CAD file. |
| Stable export selection | Identify the body, component occurrence or explicitly grouped selection that produced this mesh. |
| Cloud source file reference | Locate the exported STL independently of its current name or folder. |
| Revision and content hash | Detect a new export, an exact retry or a stale update. |
| Orca object/part association | Identify the destination geometry inside this print project. |

Use the producer's SCREF1 source key, derived from the existing cloud design
identity and its export selection, as defined in
`solidcraft/docs/solidcraft-source-roundtrip.md`. An intentionally different
selection is a new source, not an automatic replacement. Deleted and recreated
bodies require explicit relinking if their identity changes.

Accepted assumption: a source key continues to identify the same logical source
during normal editing. Feature-ID reuse after unusual Undo branches or restoring
older CAD history is outside this guarantee. It does not block automatic
replacement or require central ID tracking, stronger body generations, or
history-branch confirmation prompts. A matching key is sufficient to match the
source; the destination and content checks described below still apply. If an
unusual history operation reuses that key for a different body, the consumer
cannot distinguish it and may update the previously linked object. The user can
Undo the update and unlink or explicitly relink the source.

Scope matching to the active print project. Never update other open projects
or every STL with a matching filename. If the same source appears in several
independent objects, offer a choice of destinations. Instances sharing one
underlying Orca object receive that object's geometry update together. Saving
a project under another name retains its links without coupling its future
updates to another project's in-memory objects.

## Carrying the reference in an STL

Stamp the reference in the **custom Send to slicer path**, after binary STL
export and before uploading the exported bytes. The generic STL exporter does
not need application-specific cloud behavior. SolidCraft's unfinished plugin
runtime is not a prerequisite.

Binary STL provides an 80-byte header, but defines no standard structured
metadata schema. Use a versioned marker such as `SCREF1` and a precisely defined
compact encoding. Validate that the file is binary STL, including its triangle
count and length, before changing the header. Leave bytes from offset 80 onward
untouched. ASCII STL must use an accompanying association or another format;
do not treat its first 80 bytes as a binary header.

The full cloud origin and export-selection reference may not fit in 80 bytes.
Before implementation, measure the actual identifier encodings:

- Prefer a compact reference that resolves using existing Craft identities and
  the configured service. Document its scope and version explicitly.
- If the necessary identity cannot fit, carry a compact lookup reference in
  the header and the complete association alongside the exported cloud source
  and in the handoff. Such an STL alone is not a portable, self-contained link;
  its resolver must still be available. This fallback needs an explicit storage
  location rather than an assumed new Craft API.
- A later 3MF export can carry richer source metadata. That is an alternative
  transport, not a requirement for the first STL workflow.

Do not embed credentials, bearer tokens or authenticated download URLs. Treat
references from imported files as untrusted: resolve only configured services
and never execute a supplied command or fetch an arbitrary URL automatically.

## Persistence and source files

Keep the exported source in the project's associated `Sources/` directory
(reuse an existing `Quellen/` association). Retain the reference in the original
source bytes and the explicit cloud handoff. Ordinary Desktop drops still use
the existing import flow; a valid source reference allows linking them too.

For the native implementation, serialize the association in the Orca 3MF
project, mapped to its object/part identifiers. Reload it on project Open and
preserve it through Save As, Undo and redo. Reloading a volume can assign it a
new internal ID, so transfer the association as part of that same operation.
Do not assume arbitrary STL header text becomes Orca project metadata or that
unknown 3MF metadata already survives Orca saves.

A transitional helper may keep associations in a private registry tied to the
print project and its source files. It must clearly report lost or unresolved
links; matching by filename is not a recovery strategy. That registry is not a
substitute for portable project persistence in the target implementation.

## Updating geometry while preserving settings

Build on Orca's native **Reload from disk** machinery. Resolve the exact linked
destination first, then reload its geometry through a parameterized native
operation. Do not delete and reimport the entire object, open a newly generated
project, or recreate the project through the CLI.

The inspected reload implementation retains the parent `ModelObject`, creates
a replacement volume, copies the old volume configuration, type, material and
transform, and takes an Undo snapshot. It also handles unit conversions and
may move geometry back onto the bed. Some branches attempt to remap painting
when `keep_painting` is enabled. These are useful existing mechanisms, not proof
that every setting survives every CAD change.

| State | Intended behavior and limits |
| --- | --- |
| Printer, process and filament presets | Keep the active print-project configuration. |
| Per-object and per-part settings | Retain existing configuration overrides on their associated destinations. |
| Instances, rotation, scale and plate | Preserve them; test translation and bed placement because native reload can adjust height. |
| Materials and part roles | Preserve explicit assignments for the replaced part. |
| Separate modifier volumes | Retain them, but their position may need review after shape changes. |
| Support, seam and material painting | Use native remapping where supported; changed topology can invalidate it. Never promise exact preservation from STL. |
| Slicing results | Invalidate and regenerate for the changed geometry. |
| Source association | Retain the logical source and record the imported revision. |

Keep a consistent export coordinate system and units. Re-centering an export
differently can move features relative to modifiers even when Orca's object
transform is unchanged. STL triangle order and face indices are not stable CAD
face identities.

Parse and validate the new mesh before committing a change. On a parse error,
missing source, ambiguous destination or unsupported topology change, retain
the current geometry and show an actionable error. If applying the update
would discard data that cannot be preserved, explain that loss before applying
it. No silent fallback to duplicate import.

## Dirty projects, retries and concurrent operations

A linked geometry update is an edit to the current print project. Existing
unsaved settings should remain editable and need not force a Save first.
Apply the update through Orca's normal model transaction, dirty-state and Undo
handling; never silently save or discard the project.

Use a handoff request ID and source revision/hash to make retries idempotent.
Sending identical bytes again is a no-op. Do not apply an older revision over a
newer one; an intentional rollback must be explicit. If the source was unlinked
or its destination deleted, ask whether to import again rather than silently
recreating it.

If Orca cannot safely update during a modal operation, loading or slicing, show
**Orca is busy. Try again when the current operation finishes.** A durable
upload queue is not required. Cloud write conflicts continue to use the
existing Craft provider behavior. Do not build a second cloud save protocol.

## Delivery steps and current gaps

1. **Source reference and return link:** extend the existing customized Send
   export; define and test the bounded STL reference; resolve the original cloud
   design; add a plugin/dock action or native menu once supported. Persist the
   association explicitly rather than relying on STL metadata import.
2. **Prove native reload preservation:** update a linked source and use Orca's
   existing manual **Reload from disk**. Test settings, coordinate behavior,
   painting and project reopen with representative models. This is a useful
   interim workflow, but not the requested automatic round trip yet.
3. **Automatic update:** expose a supported native operation that accepts an
   explicit destination and validated replacement source, transfers the link,
   and participates in Undo. Integrate that operation with the handoff and
   native context menu. This belongs in Orca or its supported extension API;
   do not emulate it with blind mouse clicks or `import_model`.

The current Python integration has no verified safe geometry-replacement API.
The existing container handoff consequently reports `unsupported_refresh`.
STL header stamping alone cannot make automatic replacement work. The current
deployed SolidCraft export writes cloud sources, so delivering a validated
update notification and binding the imported object are also explicit work.

## Acceptance cases

- Send a model, change its object settings and orientation, edit it in
  SolidCraft, then send again: one destination remains, its geometry changes,
  settings remain, and Undo restores the previous revision.
- Two bodies from the same CAD design update independently. Renaming the CAD
  file or Orca object does not break identity.
- Multiple instances, independent duplicates and multipart models have explicit
  update scope; unrelated geometry and modifiers remain present.
- Save and reopen the 3MF, restart the container, and use Save As: the correct
  source is still resolvable without a temporary export path.
- Test a dimension-only edit and a major topology change with painting and
  modifiers. Report any preservation limits before relying on the workflow.
- A failed import, duplicate request, stale export or deleted source leaves
  the existing project intact and produces a clear result.
- **Modify in SolidCraft** opens the original cloud-backed editable document
  in the user's browser, with ordinary authentication and Save behavior.

## Implementation references

- Existing customized export: `solidcraft-upstream-20261103/apps/solidcraft-web/src/cloud_slicer.rs`.
- Binary STL writer: `solidcraft-upstream-20261103/crates/io/src/lib.rs`, `stl_binary`.
- Native reload investigation: [`Plater::priv::reload_from_disk`](https://github.com/OrcaSlicer/OrcaSlicer/blob/aa3291b380b97ebdc78554b99f005a28450db352/src/slic3r/GUI/Plater.cpp).
  This is the inspected source snapshot; verify behavior against the exact
  deployed release before implementation.
- [Orca plugin UI documentation](https://github.com/OrcaSlicer/OrcaSlicer/wiki/host_ui)
  and [plugin types](https://github.com/OrcaSlicer/OrcaSlicer/wiki/plugins_types).
- [Existing transitional handoff](nextcloud-handoff.md), including its explicit
  lack of a safe native refresh operation.
