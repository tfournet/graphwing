# Rewst launch-authorization foundation

This prerequisite is intentionally inert: launch contracts are unchanged, and no endpoint or graph consumes it. A later phase combines the API key with a Rewst-only exact-request signature. Secrets never belong in exports or this repository.

## Provisioning

`install.py` creates owner-only `$GRAPHWING_HOME/rewst-hmac.key`. A first install may use a 32-byte `GRAPHWING_REWST_HMAC_SECRET`; a valid file is preserved.

Import `examples/rewst-request-hmac-credential.json` and preserve:

1. A Unix timestamp and 64-character lowercase hexadecimal nonce.
2. HMAC-SHA256 over exact bytes `timestamp + "." + nonce + "." + request.body`, without reserialization.
3. A lowercase signature plus timestamp and nonce headers.

Signatures are fresh for five minutes and one-time. Each retry needs a new authorization and nonce; consumed requests stay burned.

`publish_graphs.py` registers the helper for `--only all` or `--only run-control-authorize`. Publication stays inert and never provisions the secret.
