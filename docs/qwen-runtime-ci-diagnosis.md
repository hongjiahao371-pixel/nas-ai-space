# Native amd64 Qwen CPU failure: evidence, not a passing workaround

Observed on 2026-11-19. No rerun, push, deployment or production/NAS access performed.

## Compared evidence

| PR | Run | amd64 job | Runtime artifact | Qwen result / CPU |
|---|---|---|---|---|
| #20 | 44457971576 | 135341816413 | 14498808754 (latest attempt) | failed / Intel Xeon 6975P-C |
| #21 | 44464886794 | 135360175009 | 14502355224 | passed / AMD EPYC 9V45 |
| #22 | 44465541198 | 135362347319 | 14501804026 | failed / Intel Xeon 6975P-C |

#21's amd64 native job passed; the entire #21 workflow did NOT pass (its separate verify job failed). Do not conflate these results.

All three used identical amd64 Ollama image ID `sha256:f69c4d49ddb7a3222a60e466774a745b5afb272e2eeacdfd84031f19104d2feb` (tag `ollama/ollama:0.30.8`), embedding model digest `ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d`, and vision model digest `afc393e3572c07db0bc84aa460d928a16c75f3fbb9ca9e0dccc4fbd2dafb573e`. Neither `scripts/test-ollama-runtime.py` nor its native workflow changed between #20 and #22.

## Exact failing request

Local disposable Ollama, `POST /api/embed`, timeout 300 seconds:

```json
{"model":"qwen3-embedding:0.6b","input":["synthetic red square on a white background"],"keep_alive":"1m"}
```

#22 passed all 386 native-image tests and real media/application CPU acceptance first. Pulling both Qwen models succeeded. Embedding failed before vision generation ran. The old client discarded the HTTP 500 response body; it is therefore not available and must not be reconstructed as a verbatim response.

However, archived `ollama.log` proves the underlying error:
`llama-server process has terminated: signal: segmentation fault (core dumped)`.

Both failing runs selected an AMX model buffer (498.75 MiB), had Flash Attention auto enabled, and ran on Intel Xeon 6975P-C. Ollama's parent container remained Running with OOMKilled=false. These facts support a CPU/native-backend-dependent inference failure rather than a new application feature regression or download/network error. They do NOT alone isolate AMX versus another backend/kernel feature as the root cause. OOMKilled=false also does not prove absence of all possible resource pressure.

#21's AMD runner produced a finite 1024-dimensional embedding and completed the synthetic vision request on CPU. This demonstrates hardware-correlated variability, not universal amd64 compatibility.

## Minimal change in this branch

- Preserve a bounded 8192-byte HTTP error response, method, path without query, model and truncation flag in runtime evidence. No input images/text or authorization headers are recorded.
- Redact runtime token; preserve the original HTTPError and failing exit. No retry, alternate model, fallback, skip or weakened success assertion.
- Record allowlisted public host CPU fields and image RepoDigests for cross-run comparison.
- Existing container logs and OOM state collection remain intact.

## Next verification required

Reproduce the exact image/model/request on the failing Intel CPU class in a disposable runner. Then compare one upstream-supported backend/runtime change at a time while retaining the same vector/vision assertions and CPU checks. Obtain the native crash backtrace or upstream issue confirmation before attributing solely to AMX. Validate any candidate on both Intel and AMD plus arm64 before changing shipped runtime defaults. Do not simply rerun until assigned an AMD host and call the problem resolved.

This commit improves observability only. It does not fix or claim successful real Qwen inference on the failing CPU. Local automated tests exercise failure handling; no real Ollama/Docker model rerun was performed here.
