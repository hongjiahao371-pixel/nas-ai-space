# NAS AI Space v1.6.0

This release completes the direct-media search integration with explicit index controls, query priority, media-model/worker monitoring, coordinated recovery sets, image-region queries and native document/clip downloads. Existing source media are unchanged.

Visual search ranking uses direct-model scores rather than caption keyword coverage. HEIC description upgrades are normalized to JPEG and can be retried without discarding the existing index.

Video coverage remains sampled: up to six uniform plus six scene-change frames by default, with a bounded scene pass and sampled audio. It is not full-frame action recognition. Recovery sets cover application metadata and vector indexes; original media, uploads, recycled files, models and runtime require NAS backups. Webhook notifications are disabled until a receiver is configured.

Validation results are recorded in PROGRESS.md.
