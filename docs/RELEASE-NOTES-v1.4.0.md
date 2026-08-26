# NAS AI Space v1.4.0

v1.4.0 is the first public-release package. It keeps the v1.3.2 production
workflow and adds a supported path from a clean repository clone to the first
local search.

## Highlights

- Guided `scripts/nas-ai setup` with hardware and memory detection.
- `doctor`, lifecycle, log, online-backup and safe-update commands.
- CPU, Intel, AMD and NVIDIA profile selection without editing Compose commands.
- Four-step first-use checklist in the administrator dashboard.
- Reproducible Python lock file and pinned runtime image tags.
- Apache-2.0 licensing, third-party notices, security and contribution policies.
- CI verification for tests, scripts, release metadata and all Compose profiles.

## Upgrade from v1.3.2

The database schema is unchanged. Create an online SQLite backup, update the
source, keep the existing `.env`, and rebuild the application. Existing
advanced NAS deployments may continue using their current Compose files; do
not run `setup --force` over a tuned production `.env`.

## New installations

Run:

```bash
scripts/nas-ai setup
scripts/nas-ai doctor
scripts/nas-ai start
```

The default public stack downloads models on first start. The specialized
`compose.nas-intel.yml` stack remains an advanced option for hosts that already
have the required local base image, GGUF models and llama.cpp/Qdrant runtimes.

## Known limitations

- The prebuilt application image currently targets x86-64. If it cannot be
  pulled, `scripts/nas-ai start` falls back to a local build automatically.
- ARM NAS hardware has not completed the release acceptance matrix.
- Internet-facing access still requires an external HTTPS reverse proxy or VPN.
- Hardware acceleration must be verified with a real inference or media task;
  device detection alone is not proof that acceleration is active.
