# Contributing

Thank you for improving NAS AI Space.

## Before opening a change

- Use an issue for behavior changes so the expected user flow is clear.
- Keep original media mounts read-only. New write operations need path-boundary
  validation, authorization, audit records and a recovery or undo strategy.
- Never commit `.env`, access tokens, user databases, model caches, personal
  media or production backup paths.
- Keep the default CPU stack working. Hardware overlays must remain optional.

## Local verification

Use Python 3.11, then run:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/python -m unittest discover -s tests -v
node --check app/static/app.js
bash -n scripts/nas-ai scripts/index-orchestrator.sh scripts/create-release-backup.sh
docker compose config
```

Frontend changes must bump the `?v=` asset versions in `app/static/index.html`
and be checked in a real browser at desktop and mobile widths.

## Pull requests

Describe the problem, the chosen behavior, tests added, commands run and any
data migration or rollback impact. Keep generated data and unrelated formatting
out of the change.
