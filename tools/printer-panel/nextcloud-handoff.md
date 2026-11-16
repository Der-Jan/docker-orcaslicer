# Nextcloud / SolidCraft handoff, version 1

This implements the Orca side of the 2026-10-27 SolidCraft storage contract.
SolidCraft uses the standard Craft/CollabCraft cloud flow; no local directory mirror
is needed. Enabling this feature does not migrate or delete existing projects.
The existing converter upload and legacy sync remain available.

## Authentication and deployment

The SolidCraft handoff caller can reach the Orca container internally, for
example `http://orca:3000/printer-panel/nextcloud/handoff/`. For deployments with
`SUBFOLDER`, prefix that path accordingly. This is a server-to-server API; it
does not grant CORS access. Keep the existing Authentik browser routes in place.
The caller must authenticate its own users before invoking Orca. Cloud Open/Save
and account sessions remain the standard Craft / CollabCraft implementation;
this API does not introduce another SolidCraft cloud gateway or save protocol.

Requests require `Authorization: Bearer <token>`. Orca creates a private token
once at `/data/orcaslicer/handoff/token`. Provision a read-only copy of that
single secret into the server-side handoff caller; do not mount the entire Orca data volume and do
not return the token to the browser. Rotating the token requires restarting the
Orca Nextcloud service. No token appears in request results or access logs.

Optionally provision a private `account.json` alongside the token, mode `0600`:

```json
{
  "server": "https://cloud.example.com",
  "userId": "nextcloud-account-id",
  "loginName": "login@example.com",
  "appPassword": "provision-as-a-secret",
  "folder": "/Slicer",
  "account_ref": "optional-existing-craft-account-reference"
}
```

This dedicated configuration does not depend on the internal Nextcloud login
file. During transition, omitting it uses the existing Login Flow v2 connection.
CollabCraft's own Nextcloud account configuration is independent and must refer
to the same deployment, user ID and root. Fetch `GET handoff/account` for the
non-secret `account` reference and `root`. Credentials never belong in designs
or handoff requests. Changing the server, account ID or allowed root changes
the default reference, preventing a request from targeting another account accidentally.
When `account_ref` is provisioned, use the existing Craft account reference
verbatim; the operator must map it to this exact server, user and root. No
competing origin metadata is written into `.solidcraft` files.

The handoff caller in SolidCraft is a separate integration; this image
does not modify that application's deployment, file picker or export flow.

## Requests and results

After every source upload has succeeded, submit `POST handoff/requests` with
`Content-Type: application/json`:

```json
{
  "version": 1,
  "request_id": "unique-request-uuid",
  "design_id": "stable-design-uuid",
  "account": "reference-from-the-account-endpoint",
  "project": "Chosen project",
  "print_project": "Chosen project/Print.3mf",
  "sources": [
    {
      "path": "Chosen project/Sources/Model.step",
      "etag": "\"post-upload-etag\"",
      "sha256": "optional-64-character-lowercase-hex-digest"
    }
  ],
  "target_source": "Chosen project/Sources/Model.step",
  "intent": "open"
}
```

Paths are exact and relative to the configured root, never URL-encoded. Both
`Sources` and existing `Quellen` paths work. No folder name is guessed from a
design title. The project folder must already exist in Nextcloud, selected or
created explicitly in SolidCraft. Include the provider `file_id` in a source reference when available. Each source
needs its provider ETag or the SHA-256 of the exported bytes; checksums are
verified after download, and observed ETags pin the transfer in either case.
`session_ref` may preserve a non-secret agent session reference. Private
request/result journals exist only inside Orca, not as a new SolidCraft save
queue or public request-file convention.

A missing linked 3MF is prepared locally;
`print_project: null` uses `<chosen-project>/<folder-name>.3mf` for first save.
Multiple sources are downloaded, but `target_source` selects the one model for
the first import. To open a previously assembled multi-model print project,
link its exact 3MF. No additional sources are silently appended to an existing
3MF. Request bodies are limited to 64 KiB, 32 sources and 2 GiB per file.

`POST` returns HTTP 202 and an initial status. Poll
`GET handoff/requests/<request_id>` for the durable outcome:

```json
{
  "request_id": "unique-request-uuid",
  "status": "complete",
  "synchronization": "complete",
  "operation": "opened",
  "save": "manual_required",
  "message": "Project opened. Save changes in Orca; opening is not a cloud save acknowledgement."
}
```

`status` is `accepted`, `synchronizing`, `opening`, `complete`, `failed` or
`ambiguous`. Synchronization and operation outcomes are separate; a verified
download does not imply an import or save succeeded. `save` is `not_requested`,
`manual_required` or `unchanged`. A native Save is not exposed by the plugin API;
the request never claims a normal project save occurred. Periodic sync has its
own status in the Nextcloud panel and only acknowledges success after accepted
conditional cloud writes.

Repeating the same ID and identical request returns the recorded status without
replaying it. Reusing an ID for different content returns HTTP 409. Mutation
timeouts and interrupted requests after restart are `ambiguous`; inspect Orca
and query this ID before attempting anything else. Native import dialogs remain
under user control. The API never confirms a save/discard dialog for the user.
Keep Orca on **Prepare**, with the Project storage plugin enabled: the native
drop target and status panel must be initialized. On the Home page the
conservative project guard may keep synchronization waiting for editor status.
Dirty, busy or different open projects block `open` and show instructions in
Orca. Save/close that work, open an empty project and submit a new request only
after inspecting the previous result.

Failures include `authentication`, `account_mismatch`, `invalid_path`, `busy`,
`unsaved_work`, `project_open`, `local_conflict`, `missing_file`, `offline`,
`version_mismatch`, `link_changed` and `unsupported_refresh`. HTTP rejection
and background-operation failure are distinct; always inspect the result.

## Refresh and ownership

For `intent: refresh`, the exact linked 3MF must already be open and clean.
Sources are downloaded with `If-Match`, SHA-256 checked when supplied, and
ETags rechecked. The result is then `unsupported_refresh`, with synchronization
complete: the official API exposes no safe geometry replacement. Orca displays
instructions to use **Reload from disk**. No new object is imported, and print
settings, instances, plates and placements are not rewritten by this helper.
Face-based settings after a manual reload remain Orca's responsibility.

Stable design IDs and exact links persist in private `handoff/projects.json`.
Changing a display name keeps the links. Relocating a project or changing source
links requires explicit reconciliation; this implementation rejects silent
relinking. Multiple designs can share the same project and 3MF baseline.

Linked folders are permanently excluded from legacy bisync. The exclusion is
installed under the same interprocess lock as both synchronization paths, so
there are never two synchronization writers for those folders. The legacy
comparison baseline is separate for each exclusion set. Existing unlinked
projects keep their prior sync; no migration, purge or folder move runs.

In linked projects only `.3mf` uploads are allowed, using `If-Match` for tracked
files and `If-None-Match: *` for new ones. Designs, sources and inputs are never
uploaded or deleted by Orca's managed sync. Source downloads happen on explicit
handoff. Local deletions do not delete cloud files. A cloud-side 3MF edit causes
a conditional-write conflict instead of being overwritten. Existing local
files are first adopted only if byte-identical to the cloud version. Temporary
download files are atomically installed, never imported half-written.

Periodic 3MF uploads use the existing **Start syncing** setting and 30-second
worker. Open named projects pause it; after saving, close the project or open a
new one to sync. The independent handoff account is used for managed files;
legacy folders continue to use the existing login. Do not enable an additional
CAD source mirror during cutover. The first request is an explicit association,
not a request to reorganize an existing managed SolidCraft workspace.

## Validation

`python3 tools/printer-panel/test-handoff.py` tests real local HTTP/WebDAV
transport, conditional writes, versions-before-import ordering, first open,
repeat delivery, existing 3MF, safe refresh rejection, multiple sources,
design rename, local/cloud conflicts, unsaved/busy state, missing uploads,
offline cloud, authentication, restart and ambiguous timeout. Native UI and
CLI behavior must additionally be tested in an isolated container; the tests
do not replace the manual native import checks.
