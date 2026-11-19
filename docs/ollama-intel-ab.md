# Isolated Intel Ollama A/B

This draft does not change the default Ollama image (0.30.8), Compose profiles,
existing runtime scripts or native amd64/arm64 release gates.

Evidence motivating a comparison, not a diagnosis:
- https://github.com/ollama/ollama/issues/17249 describes virtualized Intel AMX first-inference failures.
- https://github.com/ollama/ollama/pull/17244 merged a GCC 13 toolchain fix.
- https://github.com/ollama/ollama/releases/tag/v0.32.3 is the official candidate release.

Run `python3 scripts/test-ollama-ab.py` on a disposable native Linux Intel AMX
Docker host. The draft workflow runs only on a PR from `test/ollama-intel-ab`
when its own files change; checkout tests the PR merge SHA. No secrets or writes
are granted. Manual dispatch requires this workflow to exist on default first;
this patch does not put it on default. Standard hosted runners may not expose
Intel AMX: that is an inconclusive nonzero result, never a successful test.

Both versions run sequentially in one job on the same host. Official Docker tags
0.30.8 and 0.32.3 are resolved to immutable digests before each execution; actual
server version is checked. Model tag digests from the baseline are locked before
inference and compared before candidate inference. Drift aborts. Evidence records
all identities. These are within-run pins, not cross-run hardcoded release pins.
For replay, retain artifacts and use their exact image/model identities rather
than interpreting a future mutable tag run as the identical experiment.

The wrapper reuses the unchanged native test's same synthetic inputs, resource
limits, CPU-only check, 1024-vector checks and vision completion checks. There are
no CPU backend overrides, emulation, increased privileges or security disabling.
The unchanged test stops a phase at its first failure; if baseline embedding
fails, baseline vision is NOT tested. The candidate must pass both. All trial
containers/volumes are disposable and cleaned by the original runner. Any cleanup or evidence-collection error stops before the next phase and makes the comparison inconclusive, even if inference had already failed.

A green result means only: baseline POST /api/embed for qwen3-embedding:0.6b returned HTTP 500 with SIGSEGV/segmentation fault and candidate passed
both ordinary workloads with identical model identities on this host. It does not
prove AMX was the cause, or that all NAS hardware is fixed. Inspect bounded HTTP
failure evidence and container logs to separate server errors from timeouts or
resource failures. Baseline success is `inconclusive_baseline_not_reproduced`;
setup failure, novel baseline failures (including timeouts), missing Intel AMX, model drift and candidate failure are nonzero. No repeated-runner retry is scheduled.
No model quality, latency, performance, production/NAS or exploit testing is done.

## Bounded CPU diagnostic (no gate change)

The A/B wrapper records the same `/api/ps` response object consumed by the
unchanged CPU gate, without another request. `cpu_process_samples` keeps at most
two samples of eight rows, with only known Qwen names (including official fully
qualified variants), validated hex digests and bounded integer size/context
fields. Unknown strings are replaced; arbitrary URLs, tokens and nested fields
are never copied. This projection is diagnostic only, not acceptance input.
A nonzero `size_vram` still fails. No automatic retry or continuation to vision.

The previous candidate log reports a 498.75 MiB AMX model buffer. Official
v0.32.3 code has an AMX accounting hypothesis to verify, not assume:
- https://github.com/ollama/ollama/blob/v0.32.3/llm/llama_server.go#L2680-L2693 (`isGPUBuffer` does not exclude AMX)
- https://github.com/ollama/ollama/blob/v0.32.3/llm/llama_server.go#L2755-L2785 (accumulates `memGPU`)
- https://github.com/ollama/ollama/blob/v0.32.3/llm/llama_server.go#L2572-L2609 (`MemorySize`)
- https://github.com/ollama/ollama/blob/v0.32.3/server/routes.go#L2262-L2307 (`PsHandler` assigns `size_vram`)
The predicted value is 522977280 bytes; the next sample may confirm or refute it.

## Explicit CPU A/B with independently audited AMX accounting

This follow-up supersedes only the isolated wrapper's diagnostic-only handling.
Production/default native tests remain unchanged. Both A/B inference requests now
add `options.num_gpu=0`; official v0.32.3 translates that to `-ngl 0`:
https://github.com/ollama/ollama/blob/v0.32.3/llm/llama_server.go#L410-L419
Because this changes both workloads, the specified baseline SIGSEGV must recur
in the new run; the previous run cannot supply the baseline success condition.

Every request first verifies the actual Docker container: runc, not privileged,
ALL capabilities dropped and no capabilities added, no host IPC/user/UTS/cgroup namespaces, no GPU device request/mapping/cgroup rule, no bind or
inherited mounts, private PID namespace, and only named-volume mounts. A read-only
`find /dev -type c` permits only standard null/zero/random/tty/pts nodes; unexpected
nodes fail. No permissions, service configuration or devices are changed.

Every process sample must have exactly one loaded model, exact name/model/digest,
and integer nonnegative VRAM. Zero VRAM retains the original strict check. Only
v0.32.3 with nonzero VRAM can take the independent proof path. It requires:
- unchanged isolation before request and after response;
- explicit CPU request and exactly one fresh logged llama-server using `-ngl 0`;
- discovery logs showing only CPU, no GPU device or positive layer-offload evidence;
- complete bounded buffer accounting, using the official component/backend/kind
  key and last-observation replacement semantics;
- only CPU, CPU_*, BLAS and AMX buffer labels; any unknown buffer line fails;
- sum of all AMX allocations equals the unchanged reported VRAM exactly, with a
  positive AMX amount and CPU buffers also present.

Each model has its own pre-request stdout/stderr log boundary. A second runner,
truncated log, absent buffer detail, extra GPU buffer, changed device boundary or
unexplained byte causes failure. Vision may use multiple AMX components; these
are counted separately, never guessed from the embedding amount. Raw logs remain
in existing per-phase artifacts; safe numerical proof is recorded per model as
`amx_accounting_cpu_proof`, not a forged zero-VRAM sample. Successful generation
is not a test of visual answer accuracy. No default upgrade or deployment occurs.
