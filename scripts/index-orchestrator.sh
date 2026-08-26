#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=${PROJECT_DIR:-}
API_BASE=${API_BASE:-http://127.0.0.1:8766}
STATE_DIR=${STATE_DIR:-/var/lib/nas-ai-space}
REPAIR_BATCH=${REPAIR_BATCH:-50}
PENDING_BATCH=${PENDING_BATCH:-50}
CAPTION_BATCH=${CAPTION_BATCH:-50}
MIN_AVAILABLE_MEMORY_MB=${MIN_AVAILABLE_MEMORY_MB:-768}
RECYCLE_SWAP_BELOW_MB=${RECYCLE_SWAP_BELOW_MB:-4096}
MAX_FAILURE_BACKOFF_SECONDS=${MAX_FAILURE_BACKOFF_SECONDS:-3600}

if [ -z "$PROJECT_DIR" ]; then
  SCRIPT_PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  if [ -f "$SCRIPT_PROJECT_DIR/.env" ]; then
    PROJECT_DIR=$SCRIPT_PROJECT_DIR
  else
    printf 'PROJECT_DIR is required when the orchestrator is installed outside the repository.\n' >&2
    exit 1
  fi
fi

cd "$PROJECT_DIR"
install -d -m 0750 "$STATE_DIR"
exec 9>"$STATE_DIR/index-orchestrator.lock"
flock -n 9 || exit 0

TOKEN=$(python3 - <<'PY'
from pathlib import Path

for line in Path(".env").read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and key.strip() == "NAS_AI_API_TOKEN":
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        print(value)
PY
)
[ -n "$TOKEN" ] || exit 1
[ "${TOKEN//$'\n'/}" = "$TOKEN" ] || exit 1

AUTH_CONFIG="$STATE_DIR/curl-auth.conf"
umask 077
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$AUTH_CONFIG"
trap 'rm -f "$AUTH_CONFIG"' EXIT

api_get() {
  curl -fsS --connect-timeout 5 --max-time 60 --retry 2 --retry-delay 1 \
    --config "$AUTH_CONFIG" "$API_BASE$1"
}

api_post() {
  curl -fsS --connect-timeout 5 --max-time 60 --retry 2 --retry-delay 1 \
    --config "$AUTH_CONFIG" -X POST -H 'Content-Type: application/json' \
    -d "$2" "$API_BASE$1"
}

report() {
  local state=$1
  local message=$2
  local task_id=${3:-}
  local payload
  payload=$(STATE="$state" MESSAGE="$message" TASK_ID="$task_id" STATUS_JSON="${STATUS:-}" python3 - <<'PY'
import json
import os

try:
    status = json.loads(os.environ.get("STATUS_JSON") or "{}")
except json.JSONDecodeError:
    status = {}
stages = status.get("stages") or {}
pending = status.get("pending") or {}
caption_upgrades = status.get("caption_upgrades") or {}
resources = status.get("resources") or {}
task_id = os.environ.get("TASK_ID") or None
print(json.dumps({
    "state": os.environ["STATE"],
    "message": os.environ["MESSAGE"],
    "task_id": int(task_id) if task_id and task_id.isdigit() else None,
    "repairable": int(stages.get("repairable") or 0),
    "retry_waiting": int(stages.get("retry_waiting") or 0),
    "terminal_failures": int(stages.get("terminal_failures") or 0),
    "pending": int(pending.get("total") or 0),
    "caption_pending": int(caption_upgrades.get("pending") or 0),
    "available_memory_bytes": int(resources.get("available_memory_bytes") or 0),
    "free_swap_bytes": int(resources.get("free_swap_bytes") or 0),
}, ensure_ascii=False, separators=(",", ":")))
PY
)
  api_post '/api/index/controller/report' "$payload" >/dev/null 2>&1 || true
}

FAILURE_STATE="$STATE_DIR/index-orchestrator-failure"
if [ -f "$FAILURE_STATE" ]; then
  read -r _ NEXT_ATTEMPT < "$FAILURE_STATE" || true
  if [ "${NEXT_ATTEMPT:-0}" -gt "$(date +%s)" ]; then
    exit 0
  fi
fi

record_failure() {
  local message=$1
  local count=1
  if [ -f "$FAILURE_STATE" ]; then
    read -r count _ < "$FAILURE_STATE" || count=0
    count=$((count + 1))
  fi
  local delay=$((60 * (2 ** (count > 6 ? 6 : count - 1))))
  [ "$delay" -le "$MAX_FAILURE_BACKOFF_SECONDS" ] || delay=$MAX_FAILURE_BACKOFF_SECONDS
  printf '%s %s\n' "$count" "$(( $(date +%s) + delay ))" > "$FAILURE_STATE"
  report error "$message"
  printf '%s state=error failures=%s retry_in=%ss message=%s\n' \
    "$(date -Iseconds)" "$count" "$delay" "$message"
  exit 1
}

STATUS=$(api_get '/api/index/status') || record_failure "索引状态接口不可用"
read -r REPAIRABLE RETRY_WAITING TERMINAL PENDING CAPTION_PENDING ACTIVE < <(
  python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("stages") or {}; print(int(s.get("repairable") or 0), int(s.get("retry_waiting") or 0), int(s.get("terminal_failures") or 0), int((d.get("pending") or {}).get("total") or 0), int((d.get("caption_upgrades") or {}).get("pending") or 0), int(d.get("active_tasks") or 0))' <<<"$STATUS"
)

if [ "$ACTIVE" -gt 0 ]; then
  TASK_ID=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("active") or {}).get("id") or "")' <<<"$STATUS")
  report running "索引任务正在运行" "$TASK_ID"
  rm -f "$FAILURE_STATE"
  exit 0
fi

if [ "$REPAIRABLE" -eq 0 ] && [ "$PENDING" -eq 0 ] && [ "$CAPTION_PENDING" -eq 0 ]; then
  if [ "$RETRY_WAITING" -gt 0 ]; then
    report waiting "$RETRY_WAITING 个文件等待退避重试"
    exit 0
  fi
  if [ "$TERMINAL" -gt 0 ]; then
    report degraded "全库处理完成，$TERMINAL 个文件需要人工检查"
  else
    report complete "全库深度索引已完成"
  fi
  if [ ! -f "$STATE_DIR/index-orchestrator-complete" ]; then
    date -Iseconds > "$STATE_DIR/index-orchestrator-complete"
    logger -t nas-ai-space-index "Full deep indexing completed with $TERMINAL terminal failures"
  fi
  rm -f "$FAILURE_STATE"
  exit 0
fi

rm -f "$STATE_DIR/index-orchestrator-complete"
MEM_AVAILABLE_KB=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
SWAP_FREE_KB=$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)
if [ "$MEM_AVAILABLE_KB" -lt $((MIN_AVAILABLE_MEMORY_MB * 1024)) ] || \
   [ "$SWAP_FREE_KB" -lt $((RECYCLE_SWAP_BELOW_MB * 1024)) ]; then
  report recycling "资源水位偏低，正在回收模型服务"
  docker restart nas-ai-space-vision-1 nas-ai-space-embedding-1 nas-ai-space-speech-1 >/dev/null \
    || record_failure "模型服务重启失败"
  VISION=
  EMBEDDING=
  SPEECH=
  for _ in $(seq 1 60); do
    VISION=$(docker inspect -f '{{.State.Health.Status}}' nas-ai-space-vision-1 2>/dev/null || true)
    EMBEDDING=$(docker inspect -f '{{.State.Health.Status}}' nas-ai-space-embedding-1 2>/dev/null || true)
    SPEECH=$(docker inspect -f '{{.State.Health.Status}}' nas-ai-space-speech-1 2>/dev/null || true)
    [ "$VISION$EMBEDDING$SPEECH" = healthyhealthyhealthy ] && break
    sleep 2
  done
  [ "$VISION$EMBEDDING$SPEECH" = healthyhealthyhealthy ] || record_failure "模型服务未在时限内恢复健康"
  logger -t nas-ai-space-index 'Recycled model services before the next index batch'
fi

if [ "$REPAIRABLE" -gt 0 ]; then
  RESULT=$(api_post '/api/index/repair' "{\"limit\":$REPAIR_BATCH}") \
    || record_failure "修复任务提交失败"
  TYPE=repair
elif [ "$PENDING" -gt 0 ]; then
  RESULT=$(api_post '/api/index' "{\"limit\":$PENDING_BATCH,\"library_id\":null,\"kind\":\"\",\"order\":\"balanced\"}") \
    || record_failure "索引任务提交失败"
  TYPE=pending
else
  RESULT=$(api_post '/api/vision/upgrade' "{\"limit\":$CAPTION_BATCH}") \
    || record_failure "描述升级任务提交失败"
  TYPE=caption
fi

TASK_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("task_id", ""))' <<<"$RESULT")
report running "已提交 $TYPE 批次" "$TASK_ID"
rm -f "$FAILURE_STATE"
printf '%s submitted=%s task=%s repairable=%s retry_waiting=%s terminal=%s pending=%s caption_pending=%s\n' \
  "$(date -Iseconds)" "$TYPE" "$TASK_ID" "$REPAIRABLE" "$RETRY_WAITING" "$TERMINAL" "$PENDING" "$CAPTION_PENDING"
