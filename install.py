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
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
MARKETPLACE = "graphwing"
PLUGIN = "graphwing-loop"
OMARCHY_PLUGIN_ID = "graphwing.watch"
CATALOG = (
    "server.py",
    "openapi.json",
    "test_server.py",
    "scripts.json",
    "tests.json",
    "profiles.json",
    "graphs/visual-evidence.json",
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


def install_soul(home: Path) -> None:
    src = REPO / "SOUL.md"
    dest = home / "SOUL.md"
    text = src.read_text().replace("$GRAPHWING_HOME", str(home))
    if src.resolve() == dest.resolve():
        return
    dest.write_text(text)


def apply_openapi_url(home: Path, port: int) -> str:
    from setup_tunnel import set_openapi_server

    meta = home / "cloudflared-meta.json"
    if meta.is_file():
        try:
            host = str(json.loads(meta.read_text()).get("hostname") or "").strip()
        except (OSError, json.JSONDecodeError):
            host = ""
        if host:
            url = host if host.startswith("http") else f"https://{host}"
            set_openapi_server(home, url)
            return url
    url = f"http://127.0.0.1:{port}"
    set_openapi_server(home, url)
    return url


def run_named_tunnel_setup(home: Path) -> None:
    if not (home / "tunnel.env").is_file() and not os.environ.get("GRAPHWING_CF_API_KEY"):
        print("named tunnel: add tunnel.env or GRAPHWING_CF_API_KEY, then python3 setup_tunnel.py")
        return
    env = {**os.environ, "GRAPHWING_HOME": str(home)}
    proc = subprocess.run([python_bin(), str(REPO / "setup_tunnel.py")], env=env, check=False)
    if proc.returncode != 0:
        print(f"setup_tunnel.py exited {proc.returncode}; OpenAPI stays loopback until it succeeds")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def ensure_key(home: Path) -> Path:
    path = home / "api.key"
    if path.is_file() and path.read_bytes().strip():
        return path
    path.write_text(secrets.token_urlsafe(32) + "\n")
    path.chmod(0o600)
    return path


def parse_repo_flag(item: str) -> tuple[str, str]:
    if "=" not in item:
        raise SystemExit(f"--repo must be NAME=PATH, got {item!r}")
    name, raw = item.split("=", 1)
    name = name.strip()
    path = Path(raw.strip()).expanduser()
    if not name or not str(path):
        raise SystemExit(f"--repo must be NAME=PATH, got {item!r}")
    if not path.is_dir() or not (path / ".git").exists():
        raise SystemExit(f"--repo path is not a git checkout: {path}")
    return name, str(path.resolve())


def ensure_repos(
    home: Path,
    repo_root: Path,
    non_interactive: bool,
    extra: list[str] | None = None,
) -> dict[str, str]:
    del repo_root  # wizard / --repo only; catalog does not ship Tim's clones
    path = home / "repos.json"
    repos: dict[str, str] = {}
    if path.is_file():
        data = json.loads(path.read_text() or "{}")
        if isinstance(data, dict):
            repos = {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip() and str(v).strip()}
    dirty = not path.is_file()
    for item in extra or []:
        name, resolved = parse_repo_flag(item)
        repos[name] = resolved
        dirty = True
    if not non_interactive:
        print("Allowlisted git repos (Graph short names). Empty name to finish.")
        while True:
            name = ask("short name", "", False)
            if not name:
                break
            raw = ask(f"path for {name}", "", False)
            if not raw:
                continue
            dest = Path(raw).expanduser()
            if not dest.is_dir() or not (dest / ".git").exists():
                print(f"  skip: not a git checkout: {dest}")
                continue
            repos[name] = str(dest.resolve())
            dirty = True
    if dirty:
        write_json(path, repos)
    return repos


def ensure_stacks(home: Path, port: int) -> None:
    path = home / "stacks.json"
    if path.is_file():
        return
    runners = home / "integration-runners"
    runners.mkdir(parents=True, exist_ok=True)
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
                },
                {
                    "name": "clean-integration",
                    "cwd": "integration-runners",
                    "compose_file": "",
                    "ports": [],
                    "health": [],
                    "role": "clean",
                    "runner": "git-worktree",
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
    dest_dir = Path.home() / ".local/bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "graphwing"
    copy_file(REPO / "bin" / "graphwing", dest)
    dest.chmod(0o755)
    idea = dest_dir / "graphwing-idea"
    copy_file(REPO / "scripts" / "herdr-idea.sh", idea)
    idea.chmod(0o755)
    return dest


def install_claude_plugin(non_interactive: bool, opt_in: bool | None) -> None:
    """Offer to register the operator-loop skills with Claude Code.

    HUMAN-LOOP.md tells the engineer to type /grill-with-docs, /to-spec and
    /to-tickets. Without the plugin the generic mattpocock-skills versions
    answer, and their to-tickets writes a prose "Blocked by:" line that
    sliceFrontier cannot parse. Claude Code owns ~/.claude, so never write
    there unasked: no claude CLI, or no answer, prints the commands instead.
    """
    claude = shutil.which("claude")
    if claude is None:
        return
    if opt_in is None:
        opt_in = False if non_interactive else yes(
            f"Install the {PLUGIN} skills into Claude Code?", True, non_interactive
        )
    if not opt_in:
        print(f"skipped {PLUGIN}; install later with:")
        print(f"  claude plugin marketplace add {REPO}")
        print(f"  claude plugin install {PLUGIN}@{MARKETPLACE}")
        return
    steps = (
        [claude, "plugin", "marketplace", "add", str(REPO)],
        [claude, "plugin", "install", f"{PLUGIN}@{MARKETPLACE}", "--yes"],
    )
    for argv in steps:
        done = subprocess.run(argv, check=False, capture_output=True, text=True)
        if done.returncode != 0:
            print("plugin step failed:", " ".join(argv[1:]))
            print((done.stderr or done.stdout).strip()[:400])
            return
    print(f"installed {PLUGIN}@{MARKETPLACE}")
    print(f"note: a same-named skill in ~/.claude/skills wins over {PLUGIN}; remove it to use this one")


def install_omarchy_plugin(non_interactive: bool, opt_in: bool | None) -> None:
    """Copy the bar watcher into ~/.config/omarchy/plugins and enable it.

    Noninteractive installs skip this. Omarchy watches that directory and
    a rewrite unloads the whole shell.
    """
    src = REPO / "plugins" / OMARCHY_PLUGIN_ID
    dest = Path.home() / ".config/omarchy/plugins" / OMARCHY_PLUGIN_ID
    omarchy = shutil.which("omarchy")
    if opt_in is None:
        if not src.is_dir():
            return
        if omarchy is None and not (Path.home() / ".config/omarchy").is_dir():
            return
        opt_in = False if non_interactive else yes("Install the Omarchy bar watcher?", True, False)
    if not opt_in:
        print(f"skipped {OMARCHY_PLUGIN_ID}")
        return
    if not src.is_dir():
        print(f"missing plugin source {src}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    files = [p for p in src.iterdir() if p.is_file() and p.suffix != ".pyc"]
    if dest.is_dir() and files and all(
        (dest / p.name).is_file() and (dest / p.name).read_bytes() == p.read_bytes()
        for p in files
    ):
        print(f"unchanged {OMARCHY_PLUGIN_ID}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, dest / path.name)
    print(f"copied {OMARCHY_PLUGIN_ID} -> {dest}")
    if omarchy is None:
        print(f"enable later with: omarchy plugin enable {OMARCHY_PLUGIN_ID} --section right --before omarchy.agents")
        return
    check = subprocess.run(
        [omarchy, "plugin", "validate", str(dest)],
        check=False,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        print("omarchy plugin validate failed")
        print((check.stderr or check.stdout).strip()[:400])
        return
    discovered = False
    for _ in range(40):
        listing = subprocess.run(
            [omarchy, "plugin", "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if listing.returncode == 0 and OMARCHY_PLUGIN_ID in (listing.stdout or ""):
            discovered = True
            break
        time.sleep(0.05)
    if not discovered:
        print(f"plugin copied; enable later with: omarchy plugin enable {OMARCHY_PLUGIN_ID}")
        return
    enable = subprocess.run(
        [omarchy, "plugin", "enable", OMARCHY_PLUGIN_ID, "--section", "right", "--before", "omarchy.agents"],
        check=False,
        capture_output=True,
        text=True,
    )
    if enable.returncode != 0:
        print("plugin copied; enable later with:")
        print(f"  omarchy plugin enable {OMARCHY_PLUGIN_ID} --section right --before omarchy.agents")
        print((enable.stderr or enable.stdout).strip()[:400])
        return
    print(f"enabled {OMARCHY_PLUGIN_ID} on the right bar")


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
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="allowlist a git checkout (repeatable). Non-interactive installs stay empty unless this is set.",
    )
    p.add_argument(
        "--claude-plugin",
        dest="claude_plugin",
        action="store_true",
        default=None,
        help=f"install {PLUGIN} into Claude Code without asking",
    )
    p.add_argument(
        "--no-claude-plugin",
        dest="claude_plugin",
        action="store_false",
        help="skip the Claude Code plugin and just print the commands",
    )
    p.add_argument(
        "--omarchy-plugin",
        dest="omarchy_plugin",
        action="store_true",
        default=None,
        help=f"install {OMARCHY_PLUGIN_ID} into Omarchy without asking",
    )
    p.add_argument(
        "--no-omarchy-plugin",
        dest="omarchy_plugin",
        action="store_false",
        help="skip the Omarchy bar watcher",
    )
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
        if name == "SOUL.md":
            continue
        copy_file(REPO / name, home / name)
    install_soul(home)
    copy_file(REPO / "bin" / "graphwing", home / "bin" / "graphwing")
    copy_file(REPO / "setup_tunnel.py", home / "setup_tunnel.py")
    copy_file(REPO / "examples" / "tunnel.env.example", home / "tunnel.env.example")
    copy_file(REPO / "examples" / "rr.example.json", home / "rr.example.json")
    copy_file(REPO / "examples" / "repos.example.json", home / "repos.example.json")
    copy_file(REPO / "examples" / "rewst-install.example.json", home / "rewst-install.example.json")
    copy_file(REPO / "examples" / "doorbell.example.json", home / "doorbell.example.json")

    key_path = ensure_key(home)
    ensure_repos(home, REPO, ni, extra=args.repo)
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
            print("set GRAPHWING_PUBLIC_URL to that hostname if Rewst must import OpenAPI")
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

    if tunnel == "named":
        run_named_tunnel_setup(home)
    public = apply_openapi_url(home, args.port)
    print("openapi servers.url", public)

    install_claude_plugin(ni, args.claude_plugin)
    install_omarchy_plugin(ni, args.omarchy_plugin)

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
