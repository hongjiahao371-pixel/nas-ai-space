# Changelog

All notable user-facing changes are recorded here. This project follows
Semantic Versioning.

## [1.4.0] - 2026-08-25

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

## [1.3.2] - 2026-08-25

### Fixed

- Full reindex processes the complete requested scope and rejects conflicting
  index tasks.
- Index task deduplication is scoped by library, kind or exact file set.
- Background workers recover from transient SQLite startup and cleanup errors.
- Public project reviews paginate beyond 200 assets.
- Video review uses the real frame rate.
- Concurrent uploads cannot overwrite files with the same name.
