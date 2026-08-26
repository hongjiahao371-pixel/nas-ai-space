# Public release checklist

Use this checklist for every public tag.

## Code and legal

- [ ] `LICENSE` and `THIRD_PARTY_NOTICES.md` match all bundled assets.
- [ ] Model and container image licenses have been reviewed for the selected versions.
- [ ] No `.env`, database, media, model cache, backup or appliance-specific path is tracked.
- [ ] `SECURITY.md`, `CONTRIBUTING.md` and the release section in `CHANGELOG.md` are current.
- [ ] The application version, release tag and frontend asset versions agree.

## Clean installation

- [ ] Start from a new clone on an x86-64 host with no project images or model volumes.
- [ ] Run `scripts/nas-ai setup`, `doctor` and `start` without manual file edits.
- [ ] Create the first administrator, connect `/library`, scan, index a small fixture and search it.
- [ ] Restart the stack and confirm accounts, SQLite, Qdrant and model volumes persist.
- [ ] Test the CPU profile plus every hardware profile claimed in the release notes.

## Verification

```bash
python3 -m unittest discover -s tests -v
node --check app/static/app.js
bash -n scripts/*.sh scripts/nas-ai
scripts/release-check.sh
docker compose config --quiet
```

- [ ] Real-browser desktop and mobile checks have zero console errors.
- [ ] `/api/ready` has no critical errors.
- [ ] A SQLite backup and Qdrant consistency check complete successfully.
- [ ] Public share, Range media playback and one real local-AI answer work.
- [ ] Release archive excludes `.env`, data, uploads, recycle, runtime and private models.

## Publish

- [ ] Create signed or annotated tag `vX.Y.Z` from the verified commit.
- [ ] Attach checksums and known limitations to the GitHub Release.
- [ ] Confirm the published GHCR application package is public and can be pulled without login.
- [ ] Keep the repository private until all pre-public checks above are complete.
