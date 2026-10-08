# Coordinated recovery sets

The Backups page now creates an integrity-checked recovery archive alongside the SQLite backup when direct-media indexing is enabled. Automatic maintenance creates one at the configured backup interval. Archives include the application database, media-index state and snapshots for both vector collections. The writer is locked and application background tasks are quiesced during capture.

Download/verify requires an administrator. Each archive contains a manifest with SHA-256, exact file sizes, vector configuration and point counts. Verification checks SQLite quick_check and foreign keys; unsafe archive paths and non-regular entries are rejected.

## Restore into a fresh environment

Keep production services stopped when switching restored data. First verify into an independent empty data directory and fresh Qdrant instance. The restore tool refuses live/existing databases and collections, clears old worker leases, and verifies vector counts after snapshot uploads. Example inside the application image, with the source backup directory mounted read-only:

```bash
python scripts/restore-recovery.py recovery-NAME.tar \
  --confirm recovery-NAME.tar --target /restore-data \
  --qdrant-url http://restore-qdrant:6333
```

A `--prefix restore_` option is available for isolated collection names. Use a fresh Qdrant instance for an actual appliance restoration, then point the stopped application at restored volumes and restore the original mounted media/uploads paths before startup. Do not replace databases underneath running services. Preserve the previous deployment and volumes until login, search, projects and index consistency have been checked.

Recovery sets exclude original media, uploads (including artifact/comment attachments), recycle contents, generated clip files, models, executable runtime and `.env`. Preserve those with NAS volume snapshots or a backup suite. An archive alone is not complete appliance disaster recovery.

## Optional external notifications

Configure `NAS_AI_NOTIFICATION_WEBHOOK_URL` in the private deployment environment with a trusted JSON webhook receiver. With no receiver configured, nothing is sent. Deliveries use `event`, `title`, `body`, `created_at`, `source` and an `Idempotency-Key` header. Task completion/failure, full-index completion and backups have durable retries with bounded attempts. Repeated unchanged scan completions are excluded. The generic JSON contract requires an adapter for services with proprietary message formats. The destination is never included in API status/error output.
