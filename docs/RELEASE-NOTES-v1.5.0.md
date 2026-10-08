# NAS AI Space v1.5.0

This version adds an optional local EmbeddingGemma 2 media-search service and an
independent index. Existing Qwen text search and all original media are retained.

Images are embedded directly. Videos are indexed through six sampled frames with
matching timestamps. Audio uses up to twelve 30-second sampled segments. These
limits are visible in the deployment documentation; this is not full-video or
full-audio coverage for arbitrary-length recordings.

The first text search no longer prevents subsequent semantic retrieval when no
lexical results exist. Direct media matches are not rejected solely by a
text-only reranker operating on incomplete captions. Permission and filter scope
are preserved; stale vectors are excluded; an unavailable optional service does
not prevent existing search.

The additional NAS services and verified external assets are documented in
`docs/MULTIMODAL.md` and `deploy/embeddinggemma2-manifest.json`. The optional
NAS-specific overlay has its own runtime requirements. Downloaded models,
credentials, databases and appliance backups are not included in the release.
