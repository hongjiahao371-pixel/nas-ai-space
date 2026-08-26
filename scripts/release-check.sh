#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_DIR"

required=(
  LICENSE THIRD_PARTY_NOTICES.md SECURITY.md CONTRIBUTING.md CHANGELOG.md
  README.md .env.example requirements.txt requirements.lock.txt
  scripts/nas-ai
)
for path in "${required[@]}"; do
  [ -s "$path" ] || { printf 'Missing required release file: %s\n' "$path" >&2; exit 1; }
done

[ ! -e .env ] || [ "${ALLOW_LOCAL_ENV:-0}" = 1 ] || {
  printf 'Refusing release check with a repository-local .env present.\n' >&2
  exit 1
}

if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  release_files=$(git ls-files)
else
  release_files=$(find . -type f -print | sed 's#^\./##')
fi
if printf '%s\n' "$release_files" | grep -Eq '(^|/)\.env$|\.(db|sqlite|sqlite3)$|(^|/)(uploads|recycle|runtime)/'; then
  printf 'Tracked runtime data or secret configuration detected.\n' >&2
  exit 1
fi

if grep -RInE '/volume[0-9]+/|/Users/[^ /]+' \
    README.md docs scripts deploy compose*.yml docker-compose.yml --exclude='release-check.sh'; then
  printf 'Appliance-specific absolute path detected in public release files.\n' >&2
  exit 1
fi

if grep -RInE '^\s*image:\s*[^#[:space:]]+:latest([^-[:alnum:]]|$)' \
    docker-compose.yml compose*.yml; then
  printf 'Floating latest image tag detected. Pin a release tag.\n' >&2
  exit 1
fi

python3 - <<'PY'
import hashlib
import re
from pathlib import Path

version_source = Path("app/main.py").read_text(encoding="utf-8")
match = re.search(r'FastAPI\(\s*\n\s*title=.*?\n\s*version="([^"]+)"', version_source, re.S)
if not match:
    raise SystemExit("Unable to read app version")
version = match.group(1)
changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
if f"## [{version}]" not in changelog:
    raise SystemExit(f"CHANGELOG has no section for app version {version}")
release_notes = Path(f"docs/RELEASE-NOTES-v{version}.md")
if not release_notes.is_file():
    raise SystemExit(f"Missing release notes: {release_notes}")
compose_source = Path("docker-compose.yml").read_text(encoding="utf-8")
compose_image = re.search(r"ghcr\.io/hongjiahao371-pixel/nas-ai-space:([0-9.]+)", compose_source)
if not compose_image or compose_image.group(1) != version:
    raise SystemExit("Default application image tag does not match app version")
manager = Path("scripts/nas-ai").read_text(encoding="utf-8")
if f"ghcr.io/hongjiahao371-pixel/nas-ai-space:{version}" not in manager:
    raise SystemExit("Doctor image probe does not match app version")
readme = Path("README.md").read_text(encoding="utf-8")
if f"VERSION=v{version}" not in readme:
    raise SystemExit("README release-download example does not match app version")

index = Path("app/static/index.html").read_text(encoding="utf-8")
style = re.search(r'/assets/styles\.css\?v=(\d+)', index)
script = re.search(r'/assets/app\.js\?v=(\d+)', index)
if not style or not script or style.group(1) != script.group(1):
    raise SystemExit("Frontend asset versions are missing or inconsistent")

expected = {
    "models/face_detection_yunet_2023mar.onnx": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "models/face_recognition_sface_2021dec.onnx": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
}
for name, digest in expected.items():
    actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
    if actual != digest:
        raise SystemExit(f"Checksum mismatch: {name}")

release_workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
for fragment in (
    "workflow_dispatch:",
    "RELEASE_TAG:",
    "ref: ${{ env.RELEASE_TAG }}",
    "value=${{ env.RELEASE_TAG }}",
    'gh release create "$RELEASE_TAG" --verify-tag',
    "git archive --format=tar.gz",
    'sha256sum "$archive" > SHA256SUMS',
    '"$archive" SHA256SUMS',
):
    if fragment not in release_workflow:
        raise SystemExit(f"Release workflow is missing manual tagged-release support: {fragment}")
print(f"release metadata ok: v{version}, assets v{style.group(1)}")
PY

printf 'Public release checks passed.\n'
