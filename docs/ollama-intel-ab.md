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
containers/volumes are disposable and cleaned by the original runner.

A green result means only: baseline inference request failed and candidate passed
both ordinary workloads with identical model identities on this host. It does not
prove AMX was the cause, or that all NAS hardware is fixed. Inspect bounded HTTP
failure evidence and container logs to separate server errors from timeouts or
resource failures. Baseline success is `inconclusive_baseline_not_reproduced`;
setup failure, missing Intel AMX, model drift and candidate failure are nonzero.
No model quality, latency, performance, production/NAS or exploit testing is done.
