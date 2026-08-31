# Pin and supervise the Codex code-mode companion

## Problem

Graphwing pins the Codex 0.151.0 launcher to immutable memfd bytes. Codex resolves its sibling `codex-code-mode-host` relative to the pinned `/proc/self/fd/*` launcher path, producing `/codex-code-mode-host`. The temporary workaround at HEAD disables `code_mode_host`; that allows text-only execution but makes repository tool calls fail, so Phase 6 code-off review cannot inspect the candidate.

A provider-free manual proof established that Codex tool calls work when the installed `codex-code-mode-host` is started on an ephemeral loopback gRPC endpoint and that endpoint is supplied through `CODE_MODE_HOST`.

## Required behavior

Implement the smallest production-safe fix for every native Codex invocation (writer and reviewer, synchronous and asynchronous):

1. Resolve the companion only from the accepted Codex launcher installation before execution authority is sealed.
2. Read and pin the exact companion bytes independently in an immutable memfd. Never execute a later disk re-resolution.
3. Record and bind the companion SHA-256 fingerprint into the sealed/server-authored native execution authority so missing, replaced, cross-pinned, or coordinated launcher/companion drift fails before Codex starts.
4. Start the exact pinned companion on a fresh loopback-only endpoint, wait for bounded readiness, and inject only that endpoint as `CODE_MODE_HOST` into the Codex environment.
5. Keep the companion alive for the complete Codex process lifetime and terminate/reap it on success, provider error, timeout, cancellation, exception, and startup failure. Close all descriptors and prevent process/FD leaks.
6. Fail closed with a stable sanitized diagnostic if the companion is missing, cannot be pinned, does not become ready, exits early, or its authority mismatches.
7. Remove the `code_mode_host` disable workaround. Do not weaken launcher pinning, repository/diff identity, read-only reviewer permissions, callback/CAS authority, or existing effort/budget limits.
8. Do not contact providers or Rewst from tests.

## Tests

Use strict RED/GREEN TDD with hard-coded provider-free tests that prove:

- exact launcher and companion fingerprints are independent and authority-bound;
- the executed companion path is the pinned `/proc/self/fd/*` descriptor, not disk;
- `CODE_MODE_HOST` is loopback and reaches a ready fake companion;
- Codex receives the environment for both writer and reviewer paths;
- missing companion, byte replacement, cross-pin substitution, coordinated identity drift, readiness timeout, early exit, Codex timeout, and exceptions produce zero unauthorized Codex launches or fail closed as appropriate;
- cleanup leaves no companion process or leaked descriptor after every terminal path;
- existing native adapter, Phase 6 code-off identity/economics, review CAS, normal, and missing-launcher suites remain green.

Run the complete provider-free test suite, the supported missing-launcher suite, Python compilation, JSON parsing, actionlint, `git diff --check`, and sensitive-data scans. Commit the implementation on the current branch. Do not push, publish, or modify another checkout.
