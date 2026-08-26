# Public release checklist

Use this checklist for every public tag.

## Code and legal

- [x] `LICENSE` and `THIRD_PARTY_NOTICES.md` match all bundled assets.
- [x] Model and container image licenses have been reviewed for the selected versions.
- [x] No `.env`, database, media, model cache, backup or appliance-specific path is tracked.
- [x] `SECURITY.md`, `CONTRIBUTING.md` and the release section in `CHANGELOG.md` are current.
- [ ] The application version, release tag and frontend asset versions agree.

## Clean installation

- [x] Start from an isolated clean source tree on an x86-64 host with empty project data and model volumes.
- [x] Run `scripts/nas-ai setup`, `doctor` and `start` without configuration-file edits.
- [x] Create the first administrator, connect `/library`, scan, index a small fixture and search it.
- [x] Restart the stack and confirm accounts, SQLite, Qdrant and model volumes persist.
- [x] Run the CPU profile on x86-64 hardware and parse every claimed hardware Compose profile; record which profiles still need matching physical hardware.

## Verification

```bash
python3 -m unittest discover -s tests -v
node --check app/static/app.js
bash -n scripts/*.sh scripts/nas-ai
scripts/release-check.sh
docker compose config --quiet
```

- [x] Real-browser desktop and mobile checks have zero console errors.
- [x] `/api/ready` has no critical errors.
- [x] A SQLite backup and Qdrant consistency check complete successfully.
- [x] Public share, Range media playback and one real local-AI answer work.
- [x] Release archive excludes `.env`, data, uploads, recycle, runtime and private models.

## Publish

- [ ] Create signed or annotated tag `vX.Y.Z` from the verified commit.
- [ ] Attach checksums and known limitations to the GitHub Release.
- [ ] Confirm the published GHCR application package is public and can be pulled without login.
- [ ] Keep the repository private until all pre-public checks above are complete.
