#!/usr/bin/env python3
"""Re-import the graphwing custom integration from the live OpenAPI, then publish.

USING.md calls this a manual step. It is two API calls, so do them here and
record the new version in rewst-install.json rather than clicking through a UI
and hand-copying ids.

    PUT  /custom-integrations/{id}   {"sourceUrl": "<public>/openapi.json"}
    POST /custom-integrations/{id}/publish

Run it whenever openapi.json gains an operation or renames a parameter. The
workflows reference operations by name, so publishing graphs against a stale
integration version fails at request time, not at publish time.

Auth is the same as publish_graphs.py: GRAPHWING_REWST_MCP_TOKEN, or
GRAPHWING_REWST_MCP_BWS_KEY plus BWS_ACCESS_TOKEN.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import publish_graphs as pg  # noqa: E402


def integration_readback_spec(body: dict) -> dict | None:
    """Return only the live integration OpenAPI document, never request input."""
    if not isinstance(body, dict):
        return None
    envelope = body.get("data") if isinstance(body.get("data"), dict) else body
    integration = envelope.get("customIntegration") if isinstance(envelope.get("customIntegration"), dict) else envelope
    for key in ("spec", "openapi", "document"):
        value = integration.get(key)
        if isinstance(value, dict):
            return value
    current = integration.get("currentVersion") or integration.get("current_version")
    if isinstance(current, dict):
        for key in ("spec", "openapi", "document"):
            if isinstance(current.get(key), dict):
                return current[key]
    return None


def verify_integration_parity(mcp: str, integration: str, catalog_spec: dict) -> dict:
    status, live = pg.api(mcp, "GET", f"/custom-integrations/{integration}")
    if status != 200:
        raise SystemExit(f"integration live readback HTTP {status}; no parity receipt")
    return pg.require_catalog_parity(catalog_spec, integration_readback_spec(live), "custom integration")


def main() -> None:
    install = pg.load_install()
    pg.TENANT = pg.tenant_id(install)
    integration = str(install.get("custom_integration_id") or "").strip()
    if not integration:
        raise SystemExit("rewst-install.json missing custom_integration_id")

    spec_url = pg.public_openapi_url()
    mcp = pg.rewst_mcp(install)
    print("integration", integration)
    print("sourceUrl  ", spec_url)

    status, body = pg.api(mcp, "PUT", f"/custom-integrations/{integration}", {"sourceUrl": spec_url})
    pg.must(status, body, ok=(200, 201), label="reimport spec")
    ops = body.get("spec", {}).get("paths") or {}
    print("reimported, paths in spec:", len(ops))

    status, published = pg.api(mcp, "POST", f"/custom-integrations/{integration}/publish", {})
    pg.must(status, published, ok=(200, 201), label="publish integration")

    # The live side is a fresh GET after publish, never the sourceUrl payload
    # or the response to PUT/POST.  Failure deliberately leaves no success
    # receipt for the controller to count.
    catalog_spec = json.loads((pg.ROOT / "openapi.json").read_text())
    parity = verify_integration_parity(mcp, integration, catalog_spec)

    version = published.get("version") or published.get("versionNumber")
    version_id = published.get("versionId") or published.get("id")
    print("published version", version, version_id)

    install["custom_integration_version"] = version
    install["custom_integration_version_id"] = version_id
    pg.save_install(install)
    print("=== DONE ===")
    print(json.dumps({"integration": integration, "version": version, "version_id": version_id, "parity": parity}, indent=2))


if __name__ == "__main__":
    main()
