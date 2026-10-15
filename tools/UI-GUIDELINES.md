# UI requirements

All user-facing UI added or maintained in this repository must be in English.
This applies to browser panels, buttons, headings, descriptions, tooltips,
accessibility labels, injected controls, validation errors, and status messages
returned by backend services. Set the document language to English and format
displayed dates in an English locale. Follow this rule for future changes even
when the conversation or feature request is in German.

Preserve user-provided names and paths. Upstream OrcaSlicer, Selkies, Nextcloud
and printer interfaces keep their own language settings; this policy applies
to the custom UI maintained here.

Actions must explain their effect. **Sync now** requests an immediate run of
the existing bidirectional sync, bypassing the normal 30-second interval. It
does not bypass project protection or enable a stopped sync. Show immediate
acknowledgement, the current phase, actual transfer progress when available,
and the outcome. Distinguish waiting from running, errors from success, and
unknown progress from a measured percentage. Never show a fabricated percentage.
