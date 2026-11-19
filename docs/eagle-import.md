# Eagle metadata import: limited sample-backed compatibility

## Available now
Use the existing project inbox / collect action (`POST /api/projects/{id}/inbox/collect`, normal authenticated project editing permissions). Put a ZIP-based `.eaglepack` in that project inbox, then collect. No Eagle API connection, browser capture, or URL fetching is performed.

For recognized packs, only originals become project assets, not thumbnails or metadata JSON. New assets receive original title, star rating, and a plain-text description retaining tags, note, source URL and folder context. Tags are preserved in the description, NOT inserted into another user's personal tag namespace. Folder context is text, NOT physical directory creation. Existing assets and later human edits are never overwritten. URLs are text only; only HTTP(S) URLs without embedded credentials are retained. Folder passwords and miscellaneous source fields are not copied into descriptions.

## Exact supported layout, not an official format guarantee
Observed in a real pack linked by Eagle's official blog:
- root `pack.json`, with `images` list and optional single `folder` tree;
- each item has `<id>.info/metadata.json` and exact `<name>.<ext>` original;
- IDs, basename and extension must be safe single components;
- item fields must agree between pack and item metadata (null folder references are ignored);
- original byte count must match; SHA-256 of archive original and extracted original must agree before attaching metadata.

Missing `pack.json` keeps the pre-existing generic file extraction behavior with an explicit task-result warning that Eagle metadata was not imported. A present but malformed/unsupported layout fails closed. Bookmarks without originals, alternative layouts, unresolved folder names and later Eagle versions are not claimed compatible. Unknown folder IDs produce warnings rather than invented context. Deleted records are skipped.

Existing ZIP per-package size and 10,000-file limits remain. Each JSON is bounded to 2 MiB; duplicate JSON keys and archive member names are rejected for recognized Eagle packs. Folder depth is at most 32; up to 100 tags and 100 folder references per item. Source titles/labels are treated as untrusted plain text. This does not add a global recursive ZIP budget or immutable snapshots. The existing inbox job can partially succeed before a later failure; check task outcome before assuming all files imported. Existing previously imported thumbnail/JSON assets are not deleted.

## Evidence and validation
Official field definitions (not pack layout):
- https://developer.eagle.cool/plugin-api/api/item
- https://developer.eagle.cool/plugin-api/api/folder

Official blog referencing the community sample:
https://www.eagle.cool/blog/post/how-to-manage-your-ui-design-inspirations-with-eagle

Sample page (third-party author Doko Zero; this is NOT an Eagle-authored schema):
https://community-en.eagle.cool/resource/208547bd-78a4-4882-bf54-9bab95ce244c

Read-only downloaded sample: `UI Inspiration Folders Base-v3.eaglepack`, 240 ZIP entries, 85 originals. SHA-256 `e4222870f225dbbfa82a7f2ae2d05264ab3ff791d4a5c84835d65e72f0cb6ad7`. Parser matched all 85 originals and metadata records. One item contained a null folder reference in its item metadata; it is ignored without inventing a folder. The sample was not executed, modified, imported into a real NAS, or added to this repository.

Automated tests generate a tiny original synthetic pack. They cover metadata preservation, unsafe values/URLs, conflicts, generic ZIP fallback, ZIP budgets/traversal, actual existing inbox API, auxiliary exclusion, repeat collection preserving edits, and changed extracted bytes failing SHA verification. No claim of a complete Eagle round trip or all-version compatibility.
