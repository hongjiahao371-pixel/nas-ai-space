# EmbeddingGemma 2 media search (v1.5.0)

The new media collection is independent from the existing Qwen text collection.
Images are embedded directly. Video search embeds up to six uniformly sampled
frames and returns the matching timestamp; it is not exhaustive temporal video
understanding. Audio search embeds up to twelve 30-second mono/16kHz WAV segments;
recordings longer than six minutes are sampled across their duration.

The application merges lexical, Qwen text, and direct-media candidates. Text-only
reranking cannot reject a direct media match merely because its caption lacks
keywords. Media vectors with stale size/mtime are excluded. Library permissions,
file scopes, kinds and date/tag filters apply to the new candidates as well.
The existing search remains available when the new model is offline.

## NAS deployment

Use a verified llama.cpp b11480 CPU runtime, mounted separately from the old
Qwen/vision runtime. The runtime/model files are external assets and not included
in Git. Read `deploy/embeddinggemma2-manifest.json` for pinned upstream hashes.
The NAS-specific overlay uses an existing `nas-ai-llamacpp:local` runtime image.
A different host must supply a compatible image via
`NAS_AI_MULTIMODAL_RUNTIME_IMAGE` (glibc and Python healthcheck required).

```sh
NAS_AI_BASE_IMAGE=nas-ai-space:pre-gemma2-20261008 docker compose --env-file .env \
  -f compose.nas-intel.yml -f compose.multimodal.yml build app
NAS_AI_BASE_IMAGE=nas-ai-space:pre-gemma2-20261008 docker compose --env-file .env \
  -f compose.nas-intel.yml -f compose.multimodal.yml up -d --no-deps app multimodal multimodal-indexer
```

The model runs with two CPU threads, a 3 GiB memory limit, and no external port.
The indexer uses one worker with a 768 MiB memory limit and pauses below 2 GiB
of available host memory. Original media mounts are read-only. The source SQLite
database is opened read-only by the indexer; progress is stored separately in
`data/multimodal-index.db`. New and changed media are discovered automatically.
Failures wait one hour before retrying. Successful files are not re-embedded
unless the source signature or index settings change.

`GET /api/system/multimodal` requires normal application authentication and
reports counts restricted to accessible libraries. The task center also shows
media-index coverage. Search results carry `multimodal_score` and `素材语义`
provenance; this similarity is not a calibrated probability.

## Rollback

Before deployment, retain the previous application image, source/config archive,
a SQLite online backup verified with `PRAGMA integrity_check`, and a Qdrant
snapshot. Do not restore the source database unless an actual data repair is
needed: the new indexer does not rewrite existing records or Qwen vectors.

To disable the feature, stop `multimodal-indexer` and `multimodal`, restore the
prior app/config files from the saved archive and recreate only the `app` service
with the retained previous image. Preserve `data/multimodal-index.db` and the new
Qdrant collection for recovery. Do not recreate the old model/Qdrant services.

Official references:
- https://huggingface.co/google/embeddinggemma-2
- https://huggingface.co/ggml-org/embeddinggemma-2-GGUF
- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
