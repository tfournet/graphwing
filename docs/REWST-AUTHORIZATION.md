# Rewst exact launch authorization

Agent dispatch has an additive exact-authorization path. A request containing the closed `rewst_authorization` object is authenticated over its exact body bytes, reconstructed against the daemon-observed launch, claimed once under the server job ID, and consumed immediately before the queued job is persisted. Published v1 callers that omit `rewst_authorization` remain temporarily compatible until the workflow cutover is separately deployed and live-proven. The active v1 graph-side authorization path is `graphwing-run-control-consume-authorization`; the older `graphwing-run-control-authorize` graph remains compatibility-only. The source-derived call graph and migration gates remain pinned in the [issue #187 baseline](notes/run-control-activation-recovery.md#issue-187-ownership-and-call-path-baseline).

`POST /v1/rewst/launch-authority-facts` is the non-launching v2 preparation
boundary. Given one closed unsigned `agent_run` request, it resolves the current
daemon challenge, actual launcher artifact fingerprint, normalized effort and git
identity, callback/request/prompt hashes, permission profile, validity window, and
per-launch hard ceilings. Rewst persists that closed preparation, adds only the
authorization record identity produced by its readback/CAS chain, consumes it once,
and lets the request-aware credential sign the resulting exact body. A changed
launcher artifact, worktree head, callback, request field, or daemon challenge is
rejected by the existing descriptor comparison; the endpoint never chooses a route,
budget, retry, or lifecycle outcome.

This path is enforcement, not routing policy. Rewst still owns continuation, cross-model handoff, restructure, park, aggregate budgets, and durable attempt history. Every Rewst-authorized fresh launch, same-model resume, or cross-model launch needs a new consumed authorization and nonce. `/v1/pr/continue` and `/v1/slice/continue` only request later workflow executions; they do not mint or substitute launch authority.

## Server-instance challenge

Each Graphwing daemon generates a random 256-bit lowercase-hex challenge once at process start. An ordinary `X-Graphwing-Key` caller may read the current value from `GET /v1/rewst/server-challenge`; the response contains only:

```json
{
  "challenge_version": "graphwing-server-instance-challenge-v1",
  "server_instance_challenge": "<64 lowercase hex characters>"
}
```

The challenge is not launch authority. A Rewst workflow must fetch it before creating the consumed authorization, copy the exact value into the launch descriptor and consumed authorization, and send that closed authorization on `agentRun`. The daemon verifies the request HMAC before parsing launch intent. After resolving the allowlisted repository, exact branch/head, closed route and effort profile, launcher fingerprint, writer permission profile, trimmed prompt hash, resume parent, callback hash, and per-launch bounds, it reconstructs the complete descriptor and requires exact equality. It then claims the authorization under the newly allocated server job ID and consumes it before persisting queue authority.

A daemon restart rotates the challenge and clears process-local replay/authority state. Therefore an identical still-fresh request from the prior daemon fails HMAC verification before claim. If Rewst consumed its CAS authorization and Graphwing restarted before launch, that consumed authorization remains burned and cannot authorize the replacement daemon.

## Provisioning

`install.py` creates owner-only `$GRAPHWING_HOME/rewst-hmac.key`. A first install may use a 32-byte `GRAPHWING_REWST_HMAC_SECRET`; a valid file is preserved.

Import `examples/rewst-request-hmac-credential.json` and preserve:

1. Fetch the current challenge with the ordinary Graphwing API key.
2. Put that exact challenge in both the descriptor and consumed authorization.
3. Create a Unix timestamp and 64-character lowercase hexadecimal nonce.
4. HMAC-SHA256 these exact bytes, without reserialization:

   ```text
   graphwing-rewst-request-v2\n
   + server_instance_challenge + "\n"
   + timestamp + "\n"
   + nonce + "\n"
   + exact_request_body_bytes
   ```

5. Send the lowercase signature plus timestamp and nonce headers.

Signatures are fresh for five minutes and one-time within one daemon. Both the signed timestamp and the daemon's current time must be inside the authorization issue/expiry window. Authorization IDs are also one-time within that daemon, so changing only the nonce cannot reuse a consumed authorization. Every verified authorization, opaque authority, claim, and consume must match the daemon's current challenge. Each retry, resume, and handoff needs a new consumed authorization and nonce; consumed requests stay burned. A claimed authorization is burned if later continuity or queue preparation fails.

`agentRun` accepts `max_tokens` and fixed-decimal `max_cost_usd` only with `rewst_authorization`. Descriptor validation rejects a launch above 80 turns, 1,800 wall seconds, one billion tokens, or USD 100,000. The current agent adapter remains stricter at 1,200 execution seconds. These are daemon safety ceilings for one launch, not aggregate product budgets and not a next-state decision.

For `agent_run`, descriptor hashing is exact and versioned:

- `request_sha256` is SHA-256 of the request object with `rewst_authorization` removed, serialized as UTF-8 JSON with sorted keys, no insignificant whitespace, and non-ASCII characters preserved;
- `prompt_sha256` hashes the UTF-8 bytes of the trimmed prompt actually passed to the writer;
- `callback_sha256` is SHA-256 of `graphwing/callback/v1\0` followed by canonical JSON containing exactly `response_webhook_token` and the normalized `response_webhook_url` (including the deprecated `resume_url` alias);
- `diff_sha256` is null on this ordinary agent compatibility path; branch, starting head, and the existing writer baseline remain separate daemon checks;
- route, effort, launcher fingerprint, permission profile `workspace-write-v1`, resume parent, effective turn/time values, token/cost envelopes, challenge, and consumed CAS metadata must all equal the daemon reconstruction.

The HMAC still covers the original request bytes exactly. Reformatting or reordering the body therefore requires a new signature even when the canonical descriptor request hash is unchanged.

`publish_graphs.py` registers the helper for `--only all` or `--only run-control-authorize`. Publication stays inert and never provisions the secret. This source change does not publish, deploy, call Rewst, or invoke a provider.
