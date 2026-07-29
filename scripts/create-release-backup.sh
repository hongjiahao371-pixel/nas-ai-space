#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=${1:-.}
OUTPUT=${2:?Usage: create-release-backup.sh PROJECT_DIR OUTPUT.tgz}
PROJECT_DIR=$(cd "$PROJECT_DIR" && pwd)
OUTPUT_DIR=$(cd "$(dirname "$OUTPUT")" && pwd)
OUTPUT="$OUTPUT_DIR/$(basename "$OUTPUT")"
TEMPORARY="${OUTPUT}.part"

umask 077
trap 'unlink "$TEMPORARY" 2>/dev/null || true' EXIT

COPYFILE_DISABLE=1 tar --no-xattrs \
  --exclude='./.env' \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./.codex-backup-*' \
  --exclude='./data' \
  --exclude='./uploads' \
  --exclude='./recycle' \
  --exclude='./runtime' \
  --exclude='./models/*.gguf' \
  --exclude='./models/*.bin' \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' \
  -czf "$TEMPORARY" \
  -C "$PROJECT_DIR" .

if tar -tzf "$TEMPORARY" | grep -Eq '(^|/)\.env$'; then
  echo "Refusing to keep an archive containing .env" >&2
  exit 1
fi

chmod 600 "$TEMPORARY"
mv -f "$TEMPORARY" "$OUTPUT"
trap - EXIT
printf '%s\n' "$OUTPUT"
