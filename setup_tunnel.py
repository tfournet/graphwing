#!/usr/bin/env python3
"""Create/update a named Cloudflare tunnel. Prints no secrets.

Credentials (any one path):
  GRAPHWING_CF_API_KEY + GRAPHWING_CF_EMAIL
  or GRAPHWING_CF_BWS_KEY (Bitwarden Secrets lookup)
Optional file: $GRAPHWING_HOME/tunnel.env (KEY=value lines).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("GRAPHWING_HOME", Path.home() / ".graphwing"))
ORIGIN = os.environ.get("GRAPHWING_ORIGIN", "http://127.0.0.1:8645")
TOKEN_FILE = HOME / "cloudflared.token"
META_FILE = HOME / "cloudflared-meta.json"
EMAIL = ""


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, val = raw.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def cfg(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def bws_secret(key: str) -> str:
    token_path = Path.home() / ".config/bws/access_token"
    if not token_path.is_file():
        raise SystemExit(f"BWS token missing at {token_path}; set GRAPHWING_CF_API_KEY instead")
    token = token_path.read_text().strip()
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


def cf_api_key() -> str:
    direct = cfg("GRAPHWING_CF_API_KEY")
    if direct:
        return direct
    bws_key = cfg("GRAPHWING_CF_BWS_KEY")
    if bws_key:
        return bws_secret(bws_key)
    raise SystemExit(
        "named tunnel needs GRAPHWING_CF_API_KEY or GRAPHWING_CF_BWS_KEY "
        f"(see {HOME / 'tunnel.env'} or examples/tunnel.env.example)"
    )


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


def dns_leaf(hostname: str) -> str:
    return hostname.split(".")[0]


def zone_from_hostname(hostname: str) -> str:
    parts = hostname.split(".")
    if len(parts) < 2:
        return ""
    return ".".join(parts[1:])


def main() -> None:
    global EMAIL
    HOME.mkdir(parents=True, exist_ok=True)
    load_env_file(HOME / "tunnel.env")
    meta: dict = {}
    if META_FILE.is_file():
        try:
            loaded = json.loads(META_FILE.read_text())
            if isinstance(loaded, dict):
                meta = loaded
        except json.JSONDecodeError:
            meta = {}

    EMAIL = cfg("GRAPHWING_CF_EMAIL")
    if not EMAIL:
        raise SystemExit("set GRAPHWING_CF_EMAIL")
    tunnel_name = cfg("GRAPHWING_TUNNEL_NAME", str(meta.get("tunnel_name") or "graphwing"))
    hostname = cfg("GRAPHWING_HOSTNAME", str(meta.get("hostname") or ""))
    zone = cfg("GRAPHWING_CF_ZONE") or zone_from_hostname(hostname)
    if not hostname or not zone:
        raise SystemExit("set GRAPHWING_HOSTNAME and GRAPHWING_CF_ZONE")

    key = cf_api_key()
    accounts = cf("GET", "/accounts?per_page=50", key)["result"]
    if not accounts:
        raise SystemExit("no Cloudflare accounts")
    account_id = accounts[0]["id"]
    print("account", account_id, accounts[0].get("name"))

    zones = cf("GET", f"/zones?name={zone}", key)["result"]
    if not zones:
        raise SystemExit(f"zone not found: {zone}")
    zone_id = zones[0]["id"]
    print("zone", zone_id, zone)

    tunnels = cf("GET", f"/accounts/{account_id}/cfd_tunnel?is_deleted=false", key)["result"]
    tunnel = next((t for t in tunnels if t.get("name") == tunnel_name), None)
    if tunnel is None:
        created = cf(
            "POST",
            f"/accounts/{account_id}/cfd_tunnel",
            key,
            {"name": tunnel_name, "config_src": "cloudflare"},
        )["result"]
        tunnel = created
        token = created.get("token")
        print("created tunnel", created.get("id"))
    else:
        print("existing tunnel", tunnel.get("id"))
        token = cf("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel['id']}/token", key)
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
                    {"hostname": hostname, "service": ORIGIN},
                    {"service": "http_status:404"},
                ]
            }
        },
    )
    print("ingress", hostname, "->", ORIGIN)

    records = cf("GET", f"/zones/{zone_id}/dns_records?type=CNAME&name={hostname}", key)["result"]
    content = f"{tunnel_id}.cfargotunnel.com"
    leaf = dns_leaf(hostname)
    if records:
        rec_id = records[0]["id"]
        cf(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{rec_id}",
            key,
            {"type": "CNAME", "name": leaf, "content": content, "proxied": True, "ttl": 1},
        )
        print("updated DNS", hostname)
    else:
        cf(
            "POST",
            f"/zones/{zone_id}/dns_records",
            key,
            {"type": "CNAME", "name": leaf, "content": content, "proxied": True, "ttl": 1},
        )
        print("created DNS", hostname)

    META_FILE.write_text(
        json.dumps(
            {
                "account_id": account_id,
                "zone_id": zone_id,
                "tunnel_id": tunnel_id,
                "tunnel_name": tunnel_name,
                "hostname": hostname,
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
