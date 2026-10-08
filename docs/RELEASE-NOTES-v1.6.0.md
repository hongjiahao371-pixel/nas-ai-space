# NAS AI Space v1.6.0

This release completes the direct-media search integration with explicit index controls, query priority, media-model/worker monitoring, coordinated recovery sets, image-region queries and native document/clip downloads. Existing source media are unchanged.

Visual search ranking uses direct-model scores rather than caption keyword coverage. HEIC description upgrades are normalized to JPEG and can be retried without discarding the existing index.

Video coverage remains sampled: up to six uniform plus six scene-change frames by default, with a bounded scene pass and sampled audio. It is not full-frame action recognition. Recovery sets cover application metadata and vector indexes; original media, uploads, recycled files, models and runtime require NAS backups. Webhook notifications are disabled until a receiver is configured.

Validation: all 197 local/CI tests passed; the NAS ran the initial 195-test suite in an isolated network-free container, then six targeted tests for the final API/backup fixes. Real browser image-region search returned its source image first. Desktop and mobile-size pages were checked for script errors and overflow. Nine previously failing HEIC caption upgrades completed successfully on the NAS.

A recovery set was restored into an isolated Qdrant and data directory. Both collections (12,824 and 556 points at capture) matched every point ID, vector and payload, with valid SQLite integrity and foreign keys. Source media are excluded from recovery sets and remain read-only.

The frozen 95-image, 16-query comparison returned a relevant first result in 14/16 cases for raw Qwen and 16/16 for Gemma and the corrected fusion search. This is a labelled subset result, not a full-library quality or latency guarantee. Full indexing and broader video/audio quality comparisons continue. Detailed evidence and limits are recorded in PROGRESS.md.
