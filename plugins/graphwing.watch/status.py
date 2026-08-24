#!/usr/bin/env python3
"""GET /v1/watch from the local graphwing daemon. Prints one JSON object."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def fail(code: str, error: str, **extra: object) -> int:
    payload = {"ok": False, "error": error, "code": code}
    payload.update(extra)
    print(json.dumps(payload))
    return 0


def main() -> int:
    home_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    port_arg = sys.argv[2] if len(sys.argv) > 2 else ""
    home = Path(home_arg or os.environ.get("GRAPHWING_HOME", Path.home() / ".graphwing")).expanduser()
    try:
        port = int(port_arg or os.environ.get("GRAPHWING_PORT", "8645"))
    except ValueError:
        return fail("bad_port", f"invalid port {port_arg!r}")
    key_path = home / "api.key"
    if not key_path.is_file():
        return fail("no_key", f"no api.key in {home}")
    key = key_path.read_text().strip()
    if not key:
        return fail("no_key", "api.key is empty")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/watch",
        headers={"X-Graphwing-Key": key},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.setdefault("ok", False)
        parsed.setdefault("error", f"HTTP {exc.code}")
        parsed.setdefault("code", "http_error")
        print(json.dumps(parsed))
        return 0
    except OSError as exc:
        return fail("unreachable", "daemon down", detail=str(exc)[:120])
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return fail("bad_json", "bad json from daemon")
    if not isinstance(parsed, dict):
        return fail("bad_json", "bad json from daemon")
    print(json.dumps(parsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
