#!/usr/bin/env python3
"""Install graphwing catalog into GRAPHWING_HOME and user systemd units.

Does not copy secrets from another home. rr is a drop-in plugin (rr.json),
never installed. Named Cloudflare tunnel is optional.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
CATALOG = (
    "server.py",
    "openapi.json",
    "test_server.py",
    "scripts.json",
    "tests.json",
    "profiles.json",
    "SOUL.md",
    "README.md",
)
LOCAL_KEEP = (
    "api.key",
    "auth.json",
    "cloudflared.token",
    "cloudflared-meta.json",
    "repos.json",
    "stacks.json",
    "rr.json",
    "tunnel.env",
)


def ask(prompt: str, default: str, non_interactive: bool) -> str:
    if non_interactive:
        return default
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def yes(prompt: str, default: bool, non_interactive: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    val = ask(f"{prompt} ({hint})", "y" if default else "n", non_interactive)
    return val.lower() in ("y", "yes", "1", "true")


def unit_path() -> str:
    extras = [
        str(Path.home() / ".local/bin"),
        str(Path.home() / "go/bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    parts: list[str] = []
    seen: set[str] = set()
    for item in extras + os.environ.get("PATH", "").split(":"):
        if item and item not in seen:
            seen.add(item)
            parts.append(item)
    return ":".join(parts)


def which_or(name: str, fallback: Path) -> Path:
    env = os.environ.get(f"GRAPHWING_{name.upper()}_BIN", "").strip()
    if env:
        return Path(env)
    found = shutil.which(name)
    if found:
        return Path(found)
    return fallback


def python_bin() -> str:
    usr = Path("/usr/bin/python3")
    return str(usr) if usr.is_file() else sys.executable


def copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        return
    shutil.copy2(src, dest)


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def ensure_key(home: Path) -> Path:
    path = home / "api.key"
    if path.is_file() and path.read_bytes().strip():
        return path
    path.write_text(secrets.token_urlsafe(32) + "\n")
    path.chmod(0o600)
    return path


def ensure_repos(home: Path, repo_root: Path, non_interactive: bool) -> dict[str, str]:
    path = home / "repos.json"
    if path.is_file():
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data:
            return {str(k): str(v) for k, v in data.items()}
    repos = {"graphwing": str(repo_root)}
    if not non_interactive and yes("Add another allowlisted git repo?", False, False):
        while True:
            name = ask("short name (empty to finish)", "", False)
            if not name:
                break
            raw = ask(f"path for {name}", "", False)
            if raw:
                repos[name] = raw
    write_json(path, repos)
    return repos


def ensure_stacks(home: Path, port: int) -> None:
    path = home / "stacks.json"
    if path.is_file():
        return
    write_json(
        path,
        {
            "ports": [port],
            "stacks": [
                {
                    "name": "graphwing",
                    "cwd": ".",
                    "compose_file": "",
                    "ports": [port],
                    "health": [{"name": "api", "url": f"http://127.0.0.1:{port}/v1/health"}],
                }
            ],
        },
    )


def render_unit(name: str, mapping: dict[str, str]) -> str:
    src = REPO / "systemd" / name
    text = src.read_text()
    for key, val in mapping.items():
        text = text.replace(f"@{key}@", val)
    leftover = [tok for tok in text.split() if tok.startswith("@") and tok.endswith("@")]
    if leftover:
        raise SystemExit(f"unsubstituted placeholders in {name}: {leftover}")
    return text


def write_unit(unit_dir: Path, name: str, body: str) -> Path:
    dest = unit_dir / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    return dest


def mapping_for(home: Path, port: int) -> dict[str, str]:
    return {
        "GRAPHWING_HOME": str(home),
        "HOME": str(Path.home()),
        "PATH": unit_path(),
        "PYTHON": python_bin(),
        "HERDR": str(which_or("herdr", Path.home() / ".local/bin/herdr")),
        "CLOUDFLARED": str(which_or("cloudflared", Path.home() / ".local/bin/cloudflared")),
        "HERDR_SESSION": os.environ.get("GRAPHWING_HERDR_SESSION", "graphwing") or "graphwing",
        "PORT": str(port),
    }


def bin_ok(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def install_cli(home: Path) -> Path:
    src = REPO / "bin" / "graphwing"
    dest = Path.home() / ".local/bin/graphwing"
    dest.parent.mkdir(parents=True, exist_ok=True)
    copy_file(src, dest)
    dest.chmod(0o755)
    return dest


def systemd_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Install graphwing catalog and user units")
    p.add_argument("--home", type=Path, default=None, help="GRAPHWING_HOME (default ~/.graphwing)")
    p.add_argument("--unit-dir", type=Path, default=None, help="systemd user unit dir")
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--no-units", action="store_true")
    p.add_argument("--no-cli", action="store_true")
    p.add_argument("--tunnel", choices=("none", "demo", "named"), default=None)
    p.add_argument("--start", action="store_true", help="enable --now units after writing them")
    p.add_argument("--port", type=int, default=int(os.environ.get("GRAPHWING_PORT", "8645")))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    home = (args.home or Path(os.environ.get("GRAPHWING_HOME", Path.home() / ".graphwing"))).expanduser()
    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    unit_dir = (
        args.unit_dir or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    ).expanduser()
    ni = args.non_interactive

    for name in CATALOG:
        copy_file(REPO / name, home / name)
    copy_file(REPO / "bin" / "graphwing", home / "bin" / "graphwing")
    copy_file(REPO / "setup_tunnel.py", home / "setup_tunnel.py")
    copy_file(REPO / "examples" / "tunnel.env.example", home / "tunnel.env.example")
    copy_file(REPO / "examples" / "rr.example.json", home / "rr.example.json")

    key_path = ensure_key(home)
    ensure_repos(home, REPO, ni)
    ensure_stacks(home, args.port)

    if not args.no_cli:
        cli = install_cli(home)
        print("cli", cli)

    mapping = mapping_for(home, args.port)
    cf_bin = Path(mapping["CLOUDFLARED"])
    tunnel = args.tunnel
    if tunnel is None:
        default_tunnel = "demo" if (not ni and bin_ok(cf_bin)) else "none"
        tunnel = ask("Tunnel mode none/demo/named", default_tunnel, ni)
    if tunnel not in ("none", "demo", "named"):
        raise SystemExit(f"unknown tunnel mode: {tunnel}")

    wrote_units: list[Path] = []
    if not args.no_units:
        wrote_units.append(write_unit(unit_dir, "graphwing-api.service", render_unit("graphwing-api.service", mapping)))
        herdr = Path(mapping["HERDR"])
        if bin_ok(herdr) and yes("Install graphwing-herdr user unit?", True, ni):
            wrote_units.append(
                write_unit(unit_dir, "graphwing-herdr.service", render_unit("graphwing-herdr.service", mapping))
            )
        elif not bin_ok(herdr):
            print("skip herdr unit (herdr not found)")
        if tunnel == "none":
            print("skip tunnel unit (loopback only)")
        elif not bin_ok(cf_bin):
            print("skip tunnel unit (cloudflared not found)")
        elif tunnel == "demo":
            wrote_units.append(
                write_unit(unit_dir, "graphwing-tunnel.service", render_unit("graphwing-tunnel-demo.service", mapping))
            )
            print("demo tunnel: ephemeral trycloudflare.com hostname is in the unit journal")
            print("Rewst Graph SSRF-blocks loopback; demo URLs rotate and are for short-lived demos only")
        else:
            wrote_units.append(
                write_unit(unit_dir, "graphwing-tunnel.service", render_unit("graphwing-tunnel.service", mapping))
            )
            token = home / "cloudflared.token"
            if not token.is_file():
                print(f"named tunnel unit written; add {token} or run setup_tunnel.py with tunnel.env")

        start = args.start or yes("Enable and start units now?", False, ni)
        if start and wrote_units:
            systemd_user("daemon-reload")
            names = [p.name for p in wrote_units]
            systemd_user("enable", "--now", *names)

    print("GRAPHWING_HOME", home)
    print("api key file", key_path, "(mode 600); header X-Graphwing-Key")
    print("or: export GRAPHWING_KEY=$(cat %s)" % key_path)
    print("did not copy", ", ".join(LOCAL_KEEP), "from another home")
    if wrote_units:
        print("units", ", ".join(str(p) for p in wrote_units))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
