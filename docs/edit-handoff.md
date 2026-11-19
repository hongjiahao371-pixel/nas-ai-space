# Project rough-cut handoff (experimental FCPXML 1.10)

This is distinct from the existing review-export marker list. It builds real media resources and sequential asset clips from project versions. It does not modify media or send it externally.

## Authenticated API

`POST /api/projects/{project_id}/edit-handoff`

```json
{"frame_rate":"25","clips":[{"version_id":1,"start_time":1,"end_time":3,"media_path":"/Volumes/Edit/source.mp4"}]}
```

Use the normal authenticated session. Project owner/manager/editor and current source-library visibility are required, including enabled libraries. Standard bootstrap/local authentication behavior is unchanged. Clip count 1–200; body limit 128 KiB; unknown fields rejected. Sources must be readable under configured scan/upload roots and match current indexed size/mtime. Revision fields are compared again before returning to reject an intervening rescan.

Response: `application/zip`, attachment `edit-handoff.zip`, no-store. Contents: `Timeline.fcpxmld/Info.fcpxml`, `timeline.fcpxml` (identical XML for compatible tools), `manifest.json`, `README.txt`.

Each path is an explicitly supplied editor-machine absolute file path. No NAS path is guessed or copied into the package. Local POSIX and Windows drive paths are accepted; URLs, UNC, traversal, control characters and empty mappings are rejected. Windows paths are serialized, not a claim of Final Cut support on Windows. Map the actual media before import; server cannot verify editor-machine mounts.

Supported frame-rate strings: 24, 25, 30, 50, 60, 24000/1001, 30000/1001, 60000/1001. Sources must have a known matching rate. Mixed frame rates are rejected. Source in is rounded up and out down to complete frames; manifest preserves requested and actual bounds. Timings are relative to file zero, not embedded timecode.

## Verification and limits

Pure service tests cover path escaping, rational rates, in/out bounds, resource reuse, privacy and invalid metadata. API tests use a real generated MP4 through normal ingest, check ZIP output, cross-project/role/library denial, disabled libraries, invalid input, changed sources and intervening rescan. Permission role tests use dependency/method mocks; they are not a browser session acceptance test.

A generated video+audio sample passed Apple's official FCPXML 1.10 DTD with lxml. The DTD was obtained separately, not vendored or added as a dependency. DTD validation is syntax/structure evidence only, NOT an actual Final Cut Pro, Premiere Pro or Resolve import test.

The included `Timeline.fcpxmld` follows Apple's 1.10 bundle layout. Extract the outer ZIP and select the whole bundle in Final Cut Pro → File → Import → XML. Keep your original media at the mapped paths. Do not import both copies. Actual import compatibility with target versions remains to be tested. No Premiere or Resolve compatibility promise. Media, effects, subtitles, color, review comments and proxies are not included. Indexed files are not immutable historical copies: stat/revision checks protect this export window, not every historic version after a prior rescan. VFR, embedded source timecode, multichannel audio and mixed dimensions need real-editor acceptance before production use.

Official references:
- https://developer.apple.com/documentation/professional-video-applications/document-type-definition
- https://developer.apple.com/documentation/professional-video-applications/importing-fcpxml-data
- https://support.apple.com/en-az/guide/final-cut-pro/verdbd66ae/mac

No real NAS or external share was accessed. No upload to an editor or third party occurs.
