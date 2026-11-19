# Independent one-shot AMD comparison

Base: main dc08954d9373716bf569ef4f535312ec67852c59, whose existing native
container helper is reused unchanged. No #23 feature or #24 Intel diagnostic
commit is needed. Production defaults, native gates and release workflows are
unchanged. This is not an attempt to rerun or bypass the Intel AMX job.

Only a push to `test/ollama-amd-once` changing the new experimental files triggers
the new workflow. Do not open a PR or dispatch/retry other workflows. Existing CI
push matches main only, and release push matches version tags only. The job uses
read-only permissions, no persistent checkout credential and no deployment.
GitHub's ubuntu-24.04 pool does not guarantee AMD. An ineligible host writes
`inconclusive_not_amd`, exits 2, and does not retry to draw another machine.

On one eligible native Linux AuthenticAMD amd64 host, execute four independent
trials sequentially: 0.30.8 embedding, 0.30.8 vision, 0.32.3 embedding, 0.32.3
vision. Each uses a fresh container, network and model volume. Each attempts its
single workload once, with no fallback. Any trial failure is recorded; the next
independent trial may continue only if cleanup and diagnostic collection succeed.
A failed cleanup stops everything. Cancellation cleans the current trial without
starting another. Success requires all four trials, not an expected old-version
crash. A setup failure is identified by stage, not described as model inference.

The official image and Qwen model digests in the script were observed in prior
Intel artifacts; these are fixed identity pins, NOT proof of AMD compatibility.
The runtime version API must match. A changed model tag must match its pinned
manifest digest or fails before inference. No mutable image tag is executed.
Models are qwen3-embedding:0.6b and qwen3-vl:2b-instruct-q4_K_M. Synthetic input is
a red square on white; vision output completion does not establish answer accuracy.

All requests use num_gpu:0. Containers must be runc, no privileges/cap additions,
ALL caps dropped, no GPU/device requests, mapped devices, inherited/bind mounts,
or host namespaces. Enumerated character devices must be standard CPU-only
container devices. After inference, the same bounded /api/ps sample must name
exactly the selected model/digest with integer size_vram == 0; there is no AMX
exception. Isolation is checked before and after. Embeddings must be one finite,
nonzero 1024-vector; vision must complete with positive token count and nonempty
text. Resource limits and inputs are identical between versions (2 CPU, 6 GiB,
single parallel/single loaded model). Timeouts and downloads are bounded.

Evidence: comparison.json enumerates all trials and their stages; each trial's
runtime.json includes immutable identities, real inference result, bounded CPU
sample and isolation; failure.log and container logs/state persist on failure.
No NAS, user media, Windows/editor access, GPU performance or production-readiness
claim is part of this experiment. Local unit tests use synthetic data and mocked
trial scheduling; they do not prove real inference. Actual CI has not been run
when this draft is prepared.
