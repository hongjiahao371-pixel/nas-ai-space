# NAS AI Space v1.4.1

v1.4.1 completes the public new-user path after the v1.4.0 source release. It
adds earlier diagnostics, recoverable lifecycle commands and verifiable release
downloads without changing the database schema.

## Highlights

- `doctor` checks Docker-storage headroom, GHCR, Docker Hub, the Ollama model
  registry and the exact prebuilt application image before a long first start.
- Setup rejects equal or nested media, upload and recycle directories.
- `scripts/nas-ai restore <backup.db>` validates the selected SQLite backup,
  creates a fresh pre-restore backup, restarts the application and repairs
  SQLite/Qdrant consistency.
- Backup and restore tolerate NAS host ACLs that reject a container-side
  directory `chmod`; individual database backups remain owner-only.
- `scripts/nas-ai uninstall` removes containers and the Compose network without
  deleting configuration, files, databases, uploads, recycle contents or named
  model/vector volumes.
- Every GitHub Release includes a versioned source archive and `SHA256SUMS`.
- Quick-start wording now states the x86-64 boundary and separates configuration
  time from the first image/model download.

## New installations

Use the four-command Git workflow in the README, or download the versioned
`nas-ai-space-v1.4.1.tar.gz` asset and verify it with the attached
`SHA256SUMS`. Reserve at least 15 GB in Docker storage. The first start normally
takes longer than the setup itself because it downloads pinned images and about
2.5 GB of local-AI models.

## Upgrade from v1.4.0

The database schema is unchanged. Run `scripts/nas-ai backup`, update the source
and run `scripts/nas-ai start`. Keep the existing `.env`, `.nas-ai-profile`,
data directories and Docker volumes.

The lifecycle path was exercised on a real x86-64 NAS: online backup, verified
restore, Qdrant consistency repair, recoverable uninstall and restart all
completed while preserving the administrator, indexed fixture, database and
named model/vector volumes.

## Known limitations

- The prebuilt application image targets x86-64; ARM NAS hardware has not
  completed release acceptance.
- CPU/Ollama and Intel NAS execution have real-device coverage. Intel OpenVINO,
  AMD ROCm/Vulkan and NVIDIA profiles pass configuration validation but still
  require verification on matching physical GPUs.
- Internet-facing access requires an external HTTPS reverse proxy or VPN.
- Initial download time depends on access to GHCR, Docker Hub and the Ollama
  model registry; `doctor` reports blocked endpoints but cannot repair an
  upstream network or proxy.
