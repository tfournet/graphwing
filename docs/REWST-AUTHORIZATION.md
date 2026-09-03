# Rewst launch-authorization foundation

The daemon's exact HMAC/descriptor/one-time-consumption enforcement primitives remain dormant for launches: existing launch endpoints do not call them. The authenticated challenge read is live and its process-start generator is hard safety. The active v1 graph-side authorization path is `graphwing-run-control-consume-authorization`; the older `graphwing-run-control-authorize` foundation graph is compatibility-only. Neither graph enforces the dormant daemon boundary. The source-derived distinction and migration gates are pinned in the [issue #187 ownership and call-path baseline](notes/run-control-activation-recovery.md#issue-187-ownership-and-call-path-baseline).

## Server-instance challenge

Each Graphwing daemon generates a random 256-bit lowercase-hex challenge once at process start. An ordinary `X-Graphwing-Key` caller may read the current value from `GET /v1/rewst/server-challenge`; the response contains only:

```json
{
  "challenge_version": "graphwing-server-instance-challenge-v1",
  "server_instance_challenge": "<64 lowercase hex characters>"
}
```

The challenge is not launch authority. A final Rewst workflow must fetch it before creating the consumed authorization, copy the exact value into the launch descriptor and consumed authorization, and pass it through the dormant `graphwing-run-control-authorize` helper. That helper rejects descriptor/input drift and binds the value into both issued and consumed CAS values plus its output.

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

Signatures are fresh for five minutes and one-time within one daemon. Every verified authorization, opaque authority, claim, and consume must match the daemon's current challenge. Each retry needs a new consumed authorization and nonce; consumed requests stay burned.

`publish_graphs.py` registers the helper for `--only all` or `--only run-control-authorize`. Publication stays inert and never provisions the secret. This source change does not publish, deploy, call Rewst, or invoke a provider.
