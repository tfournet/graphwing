#!/usr/bin/env python3
"""Create/update named tunnel zbook-graphwing and DNS graphwing.tfour.net. Prints no secrets."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path("/home/tim/.graphwing")
EMAIL = "tfournet@tfour.net"
TUNNEL_NAME = "zbook-graphwing"
HOSTNAME = "graphwing.tfour.net"
ZONE = "tfour.net"
ORIGIN = "http://127.0.0.1:8645"
BWS_KEY = "cloudflare_web_performance_security__global_api_key"
TOKEN_FILE = HOME / "cloudflared.token"
META_FILE = HOME / "cloudflared-meta.json"


def bws_secret(key: str) -> str:
    token = Path.home().joinpath(".config/bws/access_token").read_text().strip()
    out = subprocess.check_output(
        ["bws", "-t", token, "secret", "list", "-o", "json"],
        text=True,
    )
    for item in json.loads(out):
        if item.get("key") == key:
            val = item.get("value") or ""
            if not val:
                raise SystemExit(f"BWS secret {key} empty")
            return val
    raise SystemExit(f"BWS secret not found: {key}")


def cf(method: str, path: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "X-Auth-Email": EMAIL,
            "X-Auth-Key": key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise SystemExit(f"Cloudflare {method} {path} -> {e.code} {err[:500]}") from e
    if not payload.get("success"):
        raise SystemExit(f"Cloudflare {method} {path} failed: {payload.get('errors')}")
    return payload


def main() -> None:
    key = bws_secret(BWS_KEY)
    accounts = cf("GET", "/accounts?per_page=50", key)["result"]
    if not accounts:
        raise SystemExit("no Cloudflare accounts")
    account_id = accounts[0]["id"]
    print("account", account_id, accounts[0].get("name"))

    zones = cf("GET", f"/zones?name={ZONE}", key)["result"]
    if not zones:
        raise SystemExit(f"zone not found: {ZONE}")
    zone_id = zones[0]["id"]
    print("zone", zone_id, ZONE)

    tunnels = cf("GET", f"/accounts/{account_id}/cfd_tunnel?is_deleted=false", key)["result"]
    tunnel = next((t for t in tunnels if t.get("name") == TUNNEL_NAME), None)
    if tunnel is None:
        created = cf(
            "POST",
            f"/accounts/{account_id}/cfd_tunnel",
            key,
            {"name": TUNNEL_NAME, "config_src": "cloudflare"},
        )["result"]
        tunnel = created
        token = created.get("token")
        print("created tunnel", created.get("id"))
    else:
        print("existing tunnel", tunnel.get("id"))
        token = cf("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel['id']}/token", key)
        # token endpoint may return result as string
        if isinstance(token, dict):
            token = token.get("result") or token.get("token")
    if not token or not isinstance(token, str):
        raise SystemExit("no tunnel token in API response")
    TOKEN_FILE.write_text(token.strip() + "\n")
    TOKEN_FILE.chmod(0o600)

    tunnel_id = tunnel["id"]
    cf(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        key,
        {
            "config": {
                "ingress": [
                    {"hostname": HOSTNAME, "service": ORIGIN},
                    {"service": "http_status:404"},
                ]
            }
        },
    )
    print("ingress", HOSTNAME, "->", ORIGIN)

    records = cf("GET", f"/zones/{zone_id}/dns_records?type=CNAME&name={HOSTNAME}", key)["result"]
    content = f"{tunnel_id}.cfargotunnel.com"
    if records:
        rec_id = records[0]["id"]
        cf(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{rec_id}",
            key,
            {"type": "CNAME", "name": "graphwing", "content": content, "proxied": True, "ttl": 1},
        )
        print("updated DNS", HOSTNAME)
    else:
        cf(
            "POST",
            f"/zones/{zone_id}/dns_records",
            key,
            {"type": "CNAME", "name": "graphwing", "content": content, "proxied": True, "ttl": 1},
        )
        print("created DNS", HOSTNAME)

    META_FILE.write_text(
        json.dumps(
            {
                "account_id": account_id,
                "zone_id": zone_id,
                "tunnel_id": tunnel_id,
                "tunnel_name": TUNNEL_NAME,
                "hostname": HOSTNAME,
            },
            indent=2,
        )
        + "\n"
    )
    print("wrote", TOKEN_FILE, "and", META_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
