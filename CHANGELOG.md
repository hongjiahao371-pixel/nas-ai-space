# Changelog

All notable user-facing changes are recorded here. This project follows
Semantic Versioning.

## [1.6.0] - 2026-10-08

### Added
- Direct image query uploads with normalized region cropping and permission/revision checks.
- Independent media index controls (pause/resume, processing window/order, bounded retries and recent throughput/ETA), shared interactive leases and two-service ops monitoring.
- Coordinated metadata and dual-vector recovery sets, integrity verification and restoration into a fresh isolated environment.
- Bounded scene-aware video samples and audio-track vectors, non-destructive video clip export/download.
- Native DOCX, PDF and PPTX artifact downloads and an optional durable webhook outbox.

### Fixed
- Visual retrieval scores no longer depend on generated-caption keyword coverage.
- HEIC/HEIF/AVIF caption upgrades always convert to JPEG; terminal failures can be retried explicitly.
- Coverage separates completed and inapplicable stages, vector backups expire after 48 hours, galleries collapse unused inspectors and unchanged scans can be hidden.

## [1.5.0] - 2026-10-08

### Added

- Optional local EmbeddingGemma 2 direct image, sampled video-frame and sampled
  audio-segment retrieval, stored in a separate 768-dimensional collection.
- A restartable background media indexer with source-change detection, memory
  headroom protection, retry backoff and permission-scoped coverage status.
- Media provenance and matching video/audio timestamps in search results.

### Fixed

- Continue semantic retrieval even when the first lexical search returns no
  candidates, so caption omissions cannot prevent direct-media discovery.
- Preserve direct-media matches when a text-only reranker has insufficient
  caption evidence; retain text search when the optional model is unavailable.
- Update pypdf to 6.19.0 to resolve the eight advisories reported by the release
  dependency audit, including the NAS appliance patch image.

## [1.4.1] - 2026-08-27

### Added

- Preflight checks for Docker-storage headroom, GHCR, Docker Hub, the Ollama
  registry and the exact prebuilt application manifest.
- `scripts/nas-ai restore` for verified SQLite rollback with an automatic
  pre-restore backup and post-restore Qdrant consistency repair.
- A recoverable `scripts/nas-ai uninstall` command that removes containers and
  the Compose network while preserving configuration, files, databases and
  model/vector volumes.
- A versioned release archive and `SHA256SUMS` file attached to every GitHub
  Release.

### Changed

- The quick start now distinguishes five-minute configuration from the longer
  first image/model download, and puts the x86-64 support boundary before the
  install commands.
- Setup rejects media, upload and recycle roots that are equal or nested, which
  prevents an accidental writable overlap with the source library.

### Fixed

- New-user diagnostics now surface low disk space and blocked registries before
  a long first-start attempt.
- SQLite backups no longer fail on NAS bind mounts whose ACL permits file
  creation but rejects changing the backup directory mode; each backup and its
  verification marker remain restricted to mode `0600`.

## [1.4.0] - 2026-08-27

### Added

- Apache-2.0 project license, third-party notices, security policy and
  contribution guide.
- `scripts/nas-ai` guided setup, hardware profile selection, preflight doctor,
  lifecycle commands, application backup and safe update workflow.
- Beginner quick start, hardware decision table, public-release checklist and
  continuous-integration checks.
- Tagged releases publish a prebuilt x86-64 application image; installation
  falls back to a local build if the registry image is unavailable.
- First-use checklist that guides a new administrator from media-library setup
  through scan, indexing and first search.

### Changed

- Public documentation no longer contains private repository names, production
  backup filenames or appliance-specific absolute paths.
- Release and dependency configuration is validated automatically before a
  public build is accepted.
- First-start guidance now includes container-image disk requirements and
  resumable restart instructions.

### Fixed

- `NAS_AI_BUILD_LOCAL=true` is honored when stored in `.env`, while an exported
  shell value still takes precedence.
- Mobile home-page search examples wrap instead of clipping the final example
  beyond a narrow viewport.
- Clean installations run the application with the setup user's UID and GID,
  so private host data directories remain writable without restoring container
  root capabilities.

## [1.3.2] - 2026-08-25

### Fixed

- Full reindex processes the complete requested scope and rejects conflicting
  index tasks.
- Index task deduplication is scoped by library, kind or exact file set.
- Background workers recover from transient SQLite startup and cleanup errors.
- Public project reviews paginate beyond 200 assets.
- Video review uses the real frame rate.
- Concurrent uploads cannot overwrite files with the same name.
