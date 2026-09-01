# Rewst launch-authorization foundation

This prerequisite is intentionally inert. Ordinary agent, review, and continuation launches still use their existing contracts; no current endpoint or graph requires or consumes this authorization yet. The final workflow-enforcement change will connect the two independent trust domains: the existing Graphwing API key and a Rewst-only exact-request signature. Never put either secret in a graph export or this repository.

## Provisioning

`install.py` creates `$GRAPHWING_HOME/rewst-hmac.key` mode `0600`. To supply a managed secret on first install, set `GRAPHWING_REWST_HMAC_SECRET` to at least 32 bytes; an existing valid seat file is preserved. Configure the corresponding Rewst credential field `rewst_hmac_secret` through the tenant secret manager. The API key is a separate credential field.

Import `examples/rewst-request-hmac-credential.json` as a request-aware credential template. Adapt field wiring to the installed Riftwing version, preserving these exact semantics:

1. Unix timestamp.
2. 64 lowercase hexadecimal random characters (256-bit nonce).
3. HMAC-SHA256 over the exact bytes `timestamp + "." + nonce + "." + request.body`—the already serialized outbound body, without parsing or reserialization.
4. Lowercase hexadecimal signature in `X-Graphwing-Rewst-Signature`, with timestamp and nonce in their named headers.

The signature is fresh for five minutes and one-time. Retries must create a new native KV authorization and a new nonce. A request lost after CAS consumption is intentionally burned.

`scripts/publish_graphs.py --only all --no-run` registers `graphwing-run-control-authorize` first, and `--only run-control-authorize --no-run` selects it directly. Publishing this inert helper does not connect it to launch routing. The publisher does not provision or print the HMAC secret.
