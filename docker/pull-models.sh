#!/bin/sh
set -eu

until ollama list >/dev/null 2>&1; do
  sleep 2
done

ollama pull "${NAS_AI_EMBEDDING_MODEL:-qwen3-embedding:0.6b}"
ollama pull "${NAS_AI_VISION_MODEL:-qwen3-vl:2b}"
