#!/usr/bin/env python3
"""graphwing OpenAPI server — deterministic laptop nodes + one-job agent loops."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HOME = Path(os.environ.get("GRAPHWING_HOME", Path.home() / ".graphwing"))
OPENAPI_PATH = HOME / "openapi.json"
REPOS_PATH = HOME / "repos.json"
PROFILES_PATH = HOME / "profiles.json"
SCRIPTS_PATH = HOME / "scripts.json"
STACKS_PATH = HOME / "stacks.json"
TESTS_PATH = HOME / "tests.json"
RR_PATH = HOME / "rr.json"
KEY_PATH = HOME / "api.key"
JOBS_DIR = HOME / "jobs"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("GRAPHWING_PORT", "8645"))
CMD_TIMEOUT = 30
CMD_MAX_BYTES = 256 * 1024
FILE_MAX_BYTES = 32 * 1024
PROMPT_MAX_CHARS = 64 * 1024
COMPACT_MAX_CHARS = 2000
COMPACT_TAIL_LINES = 40
COMPACT_FAIL_RE = re.compile(r"FAIL:|ERROR:|Error:|AssertionError|FAILED")
UNITS = ("graphwing-api", "graphwing-tunnel", "graphwing-herdr")
HERDR_SESSION = os.environ.get("GRAPHWING_HERDR_SESSION", "graphwing") or "graphwing"
HERDR_AGENT = os.environ.get("GRAPHWING_HERDR_AGENT", "graphwing") or "graphwing"
HERDR_GRAPH_LABEL = "graph"
HERDR_SOURCE = "graphwing"
HERDR_JOB_TAB_MAX = 8
# Leave a finished job's tab open long enough to actually read it. Closing on
# completion meant the interesting output vanished the moment it appeared.
# The tab-count cap still wins: a burst of jobs evicts lingering tabs early.
HERDR_TAB_LINGER_SECONDS = int(os.environ.get("GRAPHWING_HERDR_TAB_LINGER", "180"))
HERDR_LOCK = threading.Lock()
HOME_PROFILE = "graphwing"


def resolve_executable(name: str, env_var: str, fallback: Path) -> Path:
    raw = os.environ.get(env_var, "").strip()
    if raw:
        return Path(raw)
    found = shutil.which(name)
    if found:
        return Path(found)
    return fallback


HERMES_BIN = resolve_executable("hermes", "GRAPHWING_HERMES_BIN", Path.home() / ".local" / "bin" / "hermes")
CLAUDE_BIN = resolve_executable("claude", "GRAPHWING_CLAUDE_BIN", Path.home() / ".local" / "bin" / "claude")
RR_BIN = resolve_executable("rr", "GRAPHWING_RR_BIN", Path.home() / "go" / "bin" / "rr")
HERMES_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"
AGENT_MAX_TURNS = 30
AGENT_RUN_BUDGET = 300
AGENT_MAX_CONCURRENT = 3
SCRIPT_SYNC_TIMEOUT = 25
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
HERMES_SESSION_RE = re.compile(r"^gwslice-[0-9a-f]{32}$")
SLICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SLICE_KINDS = frozenset({"build", "decision"})
SLICE_STATUSES = frozenset({"open", "done"})
SLICE_CLASSES = frozenset({"mechanical", "visual", "sensitive"})
SLICE_SIZES = ("S", "M", "L")
# Spec-review turn budget. Was hardcoded to 1 for the claude reviewers, so
# every review died on "Reached max turns (1)" and parsed as a NACK without
# reading anything (SC-110290's first run nacked twice that way). The hermes
# reviewer already used 8. Read the diff, think, answer, and leave slack for a
# provider hiccup or a dropped connection costing a turn.
REVIEW_MAX_TURNS = int(os.environ.get("GRAPHWING_REVIEW_MAX_TURNS", "12"))
# Spec-review is read-only. The claude reviewers get --permission-mode plan,
# which enforces that in the runner. Hermes has no equivalent flag, so the sol
# reviewer is restricted by toolset instead: the ticket and diff are already in
# the prompt, so it needs no file or terminal tools to answer. An empty -t is
# silently ignored (hermes falls back to the config default), so name a real
# harmless toolset.
REVIEW_TOOLSETS = os.environ.get("GRAPHWING_REVIEW_TOOLSETS", "todo")
SLICE_BUDGET = {
    ("mechanical", "S"): (10, 120),
    ("mechanical", "M"): (30, 300),
    ("mechanical", "L"): (50, 600),
    ("visual", "S"): (10, 180),
    ("visual", "M"): (30, 600),
    ("visual", "L"): (50, 900),
    ("sensitive", "S"): (10, 180),
    ("sensitive", "M"): (30, 600),
    ("sensitive", "L"): (50, 900),
}
REF_NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
JOB_LOCK = threading.Lock()
HERDR_LINGER_LOCK = threading.Lock()
HERDR_LINGER: dict[str, float] = {}
JWKS_LOCK = threading.Lock()
JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"
DOORBELL_PROMPT = "Doorbell: checks or review changed. If checks red, one fix slice. If mergeable, stop. Do not merge."


def public_base_url() -> str | None:
    env = os.environ.get("GRAPHWING_PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    meta_path = HOME / "cloudflared-meta.json"
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    host = str(data.get("hostname") or "").strip().rstrip("/")
    if not host:
        return None
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return f"https://{host}"


def openapi_bytes() -> bytes:
    raw = OPENAPI_PATH.read_bytes()
    url = public_base_url()
    if not url:
        return raw
    spec = json.loads(raw)
    spec["servers"] = [{"url": url}]
    return (json.dumps(spec, indent=2) + "\n").encode()


def resolve_under_home(raw: str | Path | None) -> Path:
    path = Path("." if raw in (None, "") else raw)
    if not path.is_absolute():
        path = HOME / path
    return path.resolve()


def load_key() -> bytes:
    env = os.environ.get("GRAPHWING_KEY", "").encode()
    if env.strip():
        return env.strip()
    if not KEY_PATH.is_file():
        raise RuntimeError(f"missing API key: set GRAPHWING_KEY or write {KEY_PATH}")
    raw = KEY_PATH.read_bytes().strip()
    if not raw:
        raise RuntimeError(f"empty API key at {KEY_PATH}")
    return raw


def load_repos() -> dict[str, str]:
    if not REPOS_PATH.is_file():
        return {}
    raw = REPOS_PATH.read_text().strip() or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"repos.json must be an object: {REPOS_PATH}")
    return {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip() and str(v).strip()}


def load_doorbell() -> dict[str, Any]:
    config: dict[str, Any] = {
        "audience": public_base_url() or "https://graphwing.tfour.net",
        "issuer": GITHUB_OIDC_ISSUER,
        "jwks_url": GITHUB_OIDC_JWKS_URL,
        "allow_repos": {
            "RewstApp/riftwing": {
                "repo": "riftwing-drive",
                "test": "go-fmt",
                "prompt": DOORBELL_PROMPT,
                "commit_message": "pr-drive doorbell",
            },
            "tfournet/graphwing": {
                "repo": "graphwing",
                "test": "catalog-compile",
                "prompt": DOORBELL_PROMPT,
                "commit_message": "pr-drive doorbell",
            },
        },
        "allow_actors": ["tfournet"],
    }
    path = HOME / "doorbell.json"
    if not path.is_file():
        return config
    overlay = json.loads(path.read_text())
    if not isinstance(overlay, dict):
        raise RuntimeError(f"doorbell.json must be an object: {path}")
    config.update(overlay)
    return config


def load_rewst_install() -> dict[str, Any]:
    path = HOME / "rewst-install.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"rewst-install.json must be an object: {path}")
    return data


REWST_HOOK_URL_KEYS = {
    "implement-slice": "implement_slice_hook_url",
    "pr-drive": "pr_drive_hook_url",
}


def rewst_fire(body: bytes) -> tuple[int, dict[str, Any]]:
    """Proxy a run request to a Rewst webhook trigger.

    The caller sends a workflow name and its input. This process holds the
    trigger URL and the shared secret, both in $GRAPHWING_HOME, so nothing
    that fires a run needs the webhook key. Same reason `repos.json` lives
    here and not in a graph: secrets belong to the seat, not the caller.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    workflow = str(data.get("workflow") or "").strip()
    if workflow not in REWST_HOOK_URL_KEYS:
        return 400, {
            "error": f"unknown workflow '{workflow}'",
            "code": "unknown_workflow",
            "allowed": sorted(REWST_HOOK_URL_KEYS),
        }
    payload = data.get("input")
    if not isinstance(payload, dict):
        return 400, {"error": "input must be an object", "code": "bad_input"}

    install = load_rewst_install()
    url = str(install.get(REWST_HOOK_URL_KEYS[workflow]) or "").strip()
    if not url:
        return 503, {
            "error": f"no trigger url recorded for '{workflow}'",
            "code": "no_hook_url",
            "hint": f"set {REWST_HOOK_URL_KEYS[workflow]} in rewst-install.json",
        }
    if not valid_webhook_url(url):
        return 500, {"error": "recorded trigger url is not https", "code": "bad_hook_url"}
    secret = str(install.get("hook_secret") or "").strip()
    if not secret:
        return 503, {"error": "no hook_secret recorded", "code": "no_hook_secret"}

    req = Request(
        url,
        data=json.dumps({"input": payload}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-rewst-secret": secret},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", "replace")[:2000]
    except Exception as exc:
        return 502, {"ok": False, "error": str(exc)[:300], "code": "hook_failed", "workflow": workflow}
    out: dict[str, Any] = {"ok": 200 <= status < 300, "workflow": workflow, "status": status}
    try:
        out["body"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        out["body"] = {"raw": raw}
    return (200 if out["ok"] else 502), out


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_jwks(url: str) -> dict[str, Any]:
    now = time.time()
    with JWKS_LOCK:
        cached = JWKS_CACHE.get(url)
        if cached and cached[0] > now:
            return cached[1]
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise ValueError("invalid JWKS")
        JWKS_CACHE[url] = (now + 3600, data)
        return data


def verify_github_oidc(token: str, config: dict[str, Any]) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT")
    header = json.loads(_base64url_decode(parts[0]))
    claims = json.loads(_base64url_decode(parts[1]))
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256":
        raise ValueError("invalid JWT")
    kid = header.get("kid")
    jwks = _load_jwks(str(config["jwks_url"]))
    jwk = next(
        (
            item
            for item in jwks["keys"]
            if isinstance(item, dict) and item.get("kid") == kid and item.get("kty") == "RSA"
        ),
        None,
    )
    if jwk is None:
        raise ValueError("unknown signing key")
    n = int.from_bytes(_base64url_decode(str(jwk["n"])), "big")
    e = int.from_bytes(_base64url_decode(str(jwk["e"])), "big")
    signature = _base64url_decode(parts[2])
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        raise ValueError("invalid signature")
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    padding_length = size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("invalid signing key")
    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    actual = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    if not hmac.compare_digest(actual, expected):
        raise ValueError("invalid signature")
    if claims.get("iss") != config.get("issuer"):
        raise ValueError("invalid issuer")
    audience = config.get("audience")
    claim_audience = claims.get("aud")
    audiences = claim_audience if isinstance(claim_audience, list) else [claim_audience]
    if audience not in audiences:
        raise ValueError("invalid audience")
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)) or expires <= time.time():
        raise ValueError("expired JWT")
    return claims


def load_profiles() -> list[dict[str, Any]]:
    data = json.loads(PROFILES_PATH.read_text())
    if isinstance(data, dict) and isinstance(data.get("profiles"), list):
        raw = data["profiles"]
    elif isinstance(data, list):
        raw = data
    else:
        raise RuntimeError(f"profiles.json must list profiles: {PROFILES_PATH}")
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        profiles.append(
            {
                "id": pid,
                "kind": str(item.get("kind") or "home"),
                "hermes_home": str(resolve_under_home(item.get("hermes_home") or HOME)),
                "herdr_session": str(item.get("herdr_session") or ""),
                "herdr_agent": str(item.get("herdr_agent") or pid),
                "runnable": bool(item.get("runnable")),
            }
        )
    if not profiles:
        raise RuntimeError(f"profiles.json has no profiles: {PROFILES_PATH}")
    return profiles


def _allowed_cwds(repos: dict[str, str] | None = None) -> set[str]:
    repos = repos if repos is not None else load_repos()
    allowed = {str(Path(p).resolve()) for p in repos.values()}
    allowed.add(str(HOME.resolve()))
    return allowed


def load_recipes(path: Path, key: str, repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get(key), list):
        raw = data[key]
    elif isinstance(data, list):
        raw = data
    else:
        raise RuntimeError(f"{path} must list {key}")
    allowed_cwds = _allowed_cwds(repos)
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        argv = item.get("argv")
        if not name or not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise RuntimeError(f"{path} entry missing name/argv: {item!r}")
        raw_cwd = item.get("cwd")
        repos_map = repos if repos is not None else load_repos()
        if isinstance(raw_cwd, str) and raw_cwd.strip() in repos_map:
            cwd = Path(repos_map[raw_cwd.strip()]).resolve()
        else:
            cwd = resolve_under_home(raw_cwd)
        if str(cwd) not in allowed_cwds:
            continue
        try:
            timeout = int(item.get("timeout_seconds"))
        except (TypeError, ValueError):
            raise RuntimeError(f"{name} timeout_seconds must be an int") from None
        if timeout < 1:
            raise RuntimeError(f"{name} timeout_seconds must be >= 1")
        resolved_argv = list(argv)
        if resolved_argv[0] == "rr":
            resolved_argv[0] = str(RR_BIN)
        out[name] = {
            "name": name,
            "argv": resolved_argv,
            "cwd": cwd,
            "timeout_seconds": timeout,
            "async": bool(item.get("async")),
        }
    return out


def load_scripts(repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    return load_recipes(SCRIPTS_PATH, "scripts", repos)


def load_tests(repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    return load_recipes(TESTS_PATH, "tests", repos)


def load_rr(repos: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    if not RR_PATH.is_file():
        return {}
    data = json.loads(RR_PATH.read_text())
    if isinstance(data, dict) and not data.get("recipes"):
        return {}
    if isinstance(data, list) and not data:
        return {}
    return load_recipes(RR_PATH, "recipes", repos)


def load_stacks() -> tuple[dict[str, dict[str, Any]], set[int]]:
    if not STACKS_PATH.is_file():
        return {}, set()
    data = json.loads(STACKS_PATH.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"stacks.json must be an object: {STACKS_PATH}")
    ports: set[int] = set()
    for raw in data.get("ports") or []:
        try:
            p = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= p <= 65535:
            ports.add(p)
    stacks: dict[str, dict[str, Any]] = {}
    for item in data.get("stacks") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        cwd = resolve_under_home(item.get("cwd") or ".")
        if not name or not cwd.is_dir():
            continue
        health = []
        for h in item.get("health") or []:
            if isinstance(h, dict) and h.get("name") and h.get("url"):
                health.append({"name": str(h["name"]), "url": str(h["url"])})
        sport: list[int] = []
        for raw in item.get("ports") or []:
            try:
                p = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= p <= 65535:
                sport.append(p)
                ports.add(p)
        stacks[name] = {
            "name": name,
            "cwd": cwd,
            "compose_file": str(item.get("compose_file") or "docker-compose.yml"),
            "health": health,
            "ports": sport,
        }
    if not stacks and not ports:
        raise RuntimeError(f"stacks.json has no stacks or ports: {STACKS_PATH}")
    return stacks, ports


def first_query(qs: dict[str, list[str]], name: str) -> str | None:
    vals = qs.get(name)
    if not vals:
        return None
    v = vals[0].strip()
    return v or None


def query_rel(qs: dict[str, list[str]]) -> str | None:
    """Repo-relative file for fileHead and gitDiff.

    Named `rel`, not `path`. Rewst's custom-integration connector treats a
    parameter called `path` as the request URL path, so it overwrote the
    endpoint with the value and sent GET /slices/01-ticket.md instead of
    GET /v1/file/head?... The whole walk 404'd at its first fileHead.

    `path` stays accepted so integration v9 keeps working until v10 ships.
    """
    return first_query(qs, "rel") or first_query(qs, "path")


def default_repo_name(repos: dict[str, str]) -> str:
    preferred = os.environ.get("GRAPHWING_DEFAULT_REPO", "").strip()
    if preferred and preferred in repos:
        return preferred
    return next(iter(repos), "")


def resolve_repo(name: str | None, repos: dict[str, str]) -> tuple[str, Path] | tuple[None, dict[str, Any]]:
    if not repos:
        return None, {
            "error": "no allowlisted repos; add short names in repos.json",
            "code": "no_repos",
            "allowed": [],
        }
    key = (name or default_repo_name(repos)).strip()
    if key not in repos:
        return None, {
            "error": f"unknown repo '{key}'",
            "code": "unknown_repo",
            "allowed": sorted(repos),
        }
    path = Path(repos[key])
    if not path.is_dir():
        return None, {"error": f"repo path missing: {path}", "code": "repo_missing"}
    probe = run_cmd(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], timeout=5)
    if not probe.get("ok"):
        return None, {"error": f"not a git repo: {path}", "code": "not_git"}
    return key, path


def run_cmd(args: list[str], cwd: Path | None = None, timeout: int = CMD_TIMEOUT, env: dict[str, str] | None = None) -> dict[str, Any]:
    merged = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=merged,
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"missing binary: {args[0]}", "code": "missing_binary", "status": 501}
    except subprocess.TimeoutExpired as exc:
        raw_out = exc.stdout or b""
        raw_err = exc.stderr or b""
        return {
            "ok": False,
            "error": "command timed out",
            "code": "timeout",
            "status": 504,
            "stdout": raw_out[:CMD_MAX_BYTES].decode("utf-8", "replace"),
            "stderr": raw_err[:CMD_MAX_BYTES].decode("utf-8", "replace"),
            "truncated": len(raw_out) > CMD_MAX_BYTES or len(raw_err) > CMD_MAX_BYTES,
        }
    stdout = proc.stdout[:CMD_MAX_BYTES]
    stderr = proc.stderr[:CMD_MAX_BYTES]
    truncated = len(proc.stdout) > CMD_MAX_BYTES or len(proc.stderr) > CMD_MAX_BYTES
    text = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    out: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": text,
        "stderr": err,
        "truncated": truncated,
    }
    if proc.returncode != 0:
        out["error"] = err.strip() or text.strip() or f"{args[0]} exited {proc.returncode}"
        out["code"] = "cmd_failed"
        out["status"] = 400
    return out


def run_git(cwd: Path, args: list[str]) -> dict[str, Any]:
    return run_cmd(["git", "-C", str(cwd), *args], cwd=cwd)


def git_status(path: Path) -> dict[str, Any]:
    r = run_git(path, ["status", "--porcelain=v1", "-b"])
    if not r.get("ok"):
        return r
    lines = r["stdout"].splitlines()
    branch = ""
    if lines and lines[0].startswith("## "):
        branch = lines[0][3:]
        lines = lines[1:]
    return {"ok": True, "branch": branch, "dirty": bool(lines), "entries": lines, "truncated": r["truncated"]}


def git_diff(path: Path, ref: str | None, file_path: str | None) -> dict[str, Any]:
    args = ["diff"]
    if ref:
        args.append(ref)
    if file_path:
        args.extend(["--", file_path])
    r = run_git(path, args)
    if not r.get("ok"):
        return r
    return {"ok": True, "diff": r["stdout"], "truncated": r["truncated"]}


def git_log(path: Path, n: int, ref: str | None) -> dict[str, Any]:
    args = ["log", f"-n{n}", "--format=%H%x09%an%x09%ad%x09%s", "--date=iso-strict"]
    if ref:
        args.append(ref)
    r = run_git(path, args)
    if not r.get("ok"):
        return r
    commits = []
    for line in r["stdout"].splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        commits.append({"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return {"ok": True, "commits": commits, "truncated": r["truncated"]}


def git_branch(path: Path) -> dict[str, Any]:
    r = run_git(path, ["branch", "--list", "--no-color"])
    if not r.get("ok"):
        return r
    current = None
    names = []
    for line in r["stdout"].splitlines():
        name = line[2:].strip()
        if not name:
            continue
        names.append(name)
        if line.startswith("* "):
            current = name
    return {"ok": True, "current": current, "branches": names}


def git_show(path: Path, rev: str) -> dict[str, Any]:
    r = run_git(path, ["show", "--stat", "--format=fuller", rev])
    if not r.get("ok"):
        return r
    return {"ok": True, "rev": rev, "show": r["stdout"], "truncated": r["truncated"]}


def git_rev_parse(path: Path, rev: str) -> dict[str, Any]:
    r = run_git(path, ["rev-parse", rev])
    if not r.get("ok"):
        return r
    return {"ok": True, "rev": rev, "sha": r["stdout"].strip()}


def git_worktree_list(path: Path) -> dict[str, Any]:
    r = run_git(path, ["worktree", "list", "--porcelain"])
    if not r.get("ok"):
        return r
    trees = []
    cur: dict[str, Any] = {}
    for line in r["stdout"].splitlines():
        if not line:
            if cur:
                trees.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:]
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
    if cur:
        trees.append(cur)
    return {"ok": True, "worktrees": trees}


def safe_relpath(repo: Path, rel: str) -> tuple[Path, None] | tuple[None, dict[str, Any]]:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or rel.startswith("~") or "\x00" in rel:
        return None, {"error": "path must be relative and stay inside the repo", "code": "bad_path"}
    if ".." in Path(rel).parts:
        return None, {"error": "path must be relative and stay inside the repo", "code": "bad_path"}
    target = (repo / rel).resolve()
    try:
        target.relative_to(repo.resolve())
    except ValueError:
        return None, {"error": "path escapes repo", "code": "bad_path"}
    return target, None


def safe_file(repo: Path, rel: str) -> tuple[Path, None] | tuple[None, dict[str, Any]]:
    target, err = safe_relpath(repo, rel)
    if err:
        return None, err
    if not target.is_file():
        return None, {"error": f"not a file: {rel}", "code": "not_file"}
    return target, None


def valid_branch(name: str) -> bool:
    if not isinstance(name, str) or not REF_NAME_RE.fullmatch(name):
        return False
    if name.startswith(".") or name.endswith(".") or name.endswith("/") or name.endswith(".lock"):
        return False
    if ".." in name or "//" in name or "\\" in name:
        return False
    if Path(name).is_absolute() or name.startswith("/") or name.startswith("~"):
        return False
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return False
    return True


def valid_push_ref(ref: str) -> bool:
    if not isinstance(ref, str) or not ref.strip():
        return False
    ref = ref.strip()
    if ref.startswith("+") or ref.startswith(":") or ":" in ref or ref.startswith("-"):
        return False
    if "*" in ref or "?" in ref:
        return False
    if ref == "HEAD":
        return True
    return valid_branch(ref)


def valid_remote(name: str) -> bool:
    return isinstance(name, str) and bool(REMOTE_NAME_RE.fullmatch(name)) and not name.startswith("-")


def repo_from_body(data: dict[str, Any], repos: dict[str, str]):
    raw = data.get("repo")
    if not isinstance(raw, str) or not raw.strip():
        return None, {"error": "repo is required", "code": "missing_repo"}
    key = raw.strip()
    if "/" in key or key.startswith(".") or key.startswith("~") or "\\" in key:
        return None, {
            "error": "repo must be an allowlisted short name, not a filesystem path",
            "code": "bad_repo",
            "allowed": sorted(repos),
        }
    return resolve_repo(key, repos)


def git_write_result(name: str, r: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    payload = {
        "ok": bool(r.get("ok")),
        "repo": name,
        "stdout": r.get("stdout") or "",
        "stderr": r.get("stderr") or "",
        "truncated": bool(r.get("truncated")),
        "returncode": r.get("returncode"),
    }
    if not r.get("ok"):
        payload["error"] = r.get("error")
        payload["code"] = r.get("code")
        return int(r.get("status", 400)), payload
    return 200, payload


def git_checkout(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    branch = data.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        return 400, {"error": "branch is required", "code": "missing_branch"}
    branch = branch.strip()
    if not valid_branch(branch):
        return 400, {"error": "branch looks like a path or is not a safe ref", "code": "bad_branch"}
    create = data.get("create")
    if create not in (None, False, True):
        return 400, {"error": "create must be a boolean", "code": "bad_create"}
    if create:
        # "create if needed", not "create or fail". Structure commits the slice
        # map on the story branch before the run fires, so by the time Graph
        # checks out, the branch already exists and `checkout -b` dies with
        # returncode 128. Switch to it instead.
        exists = run_git(resolved, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if exists.get("ok"):
            out = run_git(resolved, ["checkout", branch])
        else:
            out = run_git(resolved, ["checkout", "-b", branch])
    else:
        verify = run_git(resolved, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if not verify.get("ok"):
            return 400, {"error": f"unknown branch '{branch}'", "code": "unknown_branch", "repo": name}
        out = run_git(resolved, ["checkout", branch])
    return git_write_result(name, out)


def git_restore(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if "force" in data or "force_with_lease" in data:
        return 400, {"error": "force is not allowed", "code": "force_rejected"}
    for field in ("remote", "ref", "delete"):
        if field in data:
            return 400, {"error": f"{field} is not accepted", "code": "extra_field"}
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    clean_untracked = data.get("clean_untracked", False)
    if not isinstance(clean_untracked, bool):
        return 400, {"error": "clean_untracked must be a boolean", "code": "bad_clean_untracked"}
    out = run_git(resolved, ["reset", "--hard", "HEAD"])
    if not out.get("ok") or not clean_untracked:
        return git_write_result(name, out)
    return git_write_result(name, run_git(resolved, ["clean", "-fd"]))


def git_commit(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return 400, {"error": "message is required", "code": "empty_message"}
    if data.get("add_all") is True:
        return 400, {"error": "add_all is not enabled; pass add as relative paths or commit staged files", "code": "add_all_disabled"}
    add = data.get("add")
    if isinstance(add, str):
        add = [add]
    if add is not None:
        if not isinstance(add, list) or not all(isinstance(x, str) for x in add):
            return 400, {"error": "add must be a list of relative paths or one path string", "code": "bad_add"}
        rels: list[str] = []
        for rel in add:
            _target, perr = safe_relpath(resolved, rel)
            if perr:
                perr["ok"] = False
                return 400, perr
            rels.append(rel)
        if rels:
            added = run_git(resolved, ["add", "--", *rels])
            if not added.get("ok"):
                return git_write_result(name, added)
    out = run_git(resolved, ["commit", "-m", message.strip()])
    return git_write_result(name, out)


def git_push(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    if data.get("force") or data.get("force_with_lease") or data.get("delete"):
        return 400, {"error": "force/delete push is not allowed", "code": "force_rejected"}
    remote = data.get("remote") if data.get("remote") not in ("", None) else "origin"
    if not isinstance(remote, str) or not valid_remote(remote):
        return 400, {"error": "remote must be a named remote, not a URL or flag", "code": "bad_remote"}
    remotes = run_git(resolved, ["remote"])
    if not remotes.get("ok"):
        return git_write_result(name, remotes)
    known = {line.strip() for line in (remotes.get("stdout") or "").splitlines() if line.strip()}
    if remote not in known:
        return 400, {"error": f"unknown remote '{remote}'", "code": "unknown_remote", "allowed": sorted(known)}
    ref = data.get("ref")
    if ref in ("", None):
        ref = "HEAD"
    if not isinstance(ref, str) or not valid_push_ref(ref):
        return 400, {"error": "ref rejected (no force, no delete, no refspec)", "code": "bad_ref"}
    out = run_git(resolved, ["push", remote, ref])
    return git_write_result(name, out)


def file_head(repo: Path, rel: str) -> dict[str, Any]:
    target, err = safe_file(repo, rel)
    if err:
        err["ok"] = False
        err["status"] = 400
        return err
    data = target.read_bytes()[:FILE_MAX_BYTES]
    truncated = target.stat().st_size > FILE_MAX_BYTES
    return {
        "ok": True,
        "path": rel,
        "bytes": len(data),
        "truncated": truncated,
        "text": data.decode("utf-8", "replace"),
    }


def parse_slice_index(data: Any) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    if not isinstance(data, dict):
        return None, {"ok": False, "error": "index must be an object", "code": "bad_index", "status": 400}
    raw = data.get("tickets")
    if not isinstance(raw, list) or not raw:
        return None, {"ok": False, "error": "index.tickets must be a non-empty list", "code": "bad_index", "status": 400}
    tickets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None, {"ok": False, "error": "ticket must be an object", "code": "bad_ticket", "status": 400}
        tid = item.get("id")
        path = item.get("path")
        if not isinstance(tid, str) or not SLICE_ID_RE.fullmatch(tid):
            return None, {"ok": False, "error": "ticket id is invalid", "code": "bad_ticket_id", "status": 400}
        if tid in seen:
            return None, {"ok": False, "error": f"duplicate ticket id '{tid}'", "code": "dup_ticket_id", "status": 400}
        if not isinstance(path, str) or not path.strip() or ".." in path or path.startswith("/"):
            return None, {"ok": False, "error": "ticket path is invalid", "code": "bad_ticket_path", "status": 400}
        kind = item.get("kind") or "build"
        status = item.get("status") or "open"
        blocked = item.get("blocked_by") or []
        if kind not in SLICE_KINDS:
            return None, {"ok": False, "error": f"unknown kind '{kind}'", "code": "bad_ticket_kind", "status": 400}
        if status not in SLICE_STATUSES:
            return None, {"ok": False, "error": f"unknown status '{status}'", "code": "bad_ticket_status", "status": 400}
        if not isinstance(blocked, list) or not all(isinstance(x, str) for x in blocked):
            return None, {"ok": False, "error": "blocked_by must be a list of ids", "code": "bad_blocked_by", "status": 400}
        seen.add(tid)
        tickets.append(
            {"id": tid, "path": path.strip(), "kind": kind, "status": status, "blocked_by": list(blocked)}
        )
    ids = {t["id"] for t in tickets}
    for t in tickets:
        for b in t["blocked_by"]:
            if b not in ids:
                return None, {
                    "ok": False,
                    "error": f"blocked_by unknown id '{b}'",
                    "code": "bad_blocked_by",
                    "status": 400,
                }
    return tickets, None


def load_slice_index(repo: Path, rel: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    target, err = safe_file(repo, rel)
    if err:
        err["ok"] = False
        err["status"] = 400
        return None, err
    try:
        data = json.loads(target.read_text())
    except json.JSONDecodeError:
        return None, {"ok": False, "error": "index is not JSON", "code": "bad_index", "status": 400}
    return parse_slice_index(data)


def write_slice_index(
    repo: Path, rel: str, tickets: list[dict[str, Any]], extra: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    target, err = safe_relpath(repo, rel)
    if err:
        err["ok"] = False
        err["status"] = 400
        return err
    target.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = dict(extra or {})
    doc["tickets"] = tickets
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(target)
    return None


def index_extra(repo: Path, rel: str) -> dict[str, Any]:
    target, err = safe_file(repo, rel)
    if err or target is None:
        return {}
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != "tickets"}


def slice_ready(ticket: dict[str, Any], done: set[str]) -> bool:
    return ticket["status"] == "open" and all(b in done for b in ticket["blocked_by"])


def slice_frontier(tickets: list[dict[str, Any]], ticket_path: str | None = None) -> dict[str, Any]:
    done = {t["id"] for t in tickets if t["status"] == "done"}
    if ticket_path:
        matches = [t for t in tickets if t["path"] == ticket_path]
        if not matches:
            return {"ok": False, "error": f"unknown ticket path '{ticket_path}'", "code": "unknown_ticket", "status": 400}
        t = matches[0]
        if not slice_ready(t, done):
            return {"ok": False, "error": "ticket is not on the frontier", "code": "ticket_blocked", "status": 409}
        return {"ok": True, "kind": t["kind"], "id": t["id"], "path": t["path"]}
    for t in tickets:
        if slice_ready(t, done):
            return {"ok": True, "kind": t["kind"], "id": t["id"], "path": t["path"]}
    return {"ok": True, "kind": "empty"}


def slice_frontier_op(qs: dict[str, list[str]], repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    name, resolved = resolve_repo(first_query(qs, "repo"), repos)
    if name is None:
        return 400, resolved
    index = first_query(qs, "index")
    if not index:
        return 400, {"error": "index is required", "code": "missing_index"}
    tickets, err = load_slice_index(resolved, index)
    if err:
        return int(err.get("status", 400)), err
    assert tickets is not None
    out = slice_frontier(tickets, first_query(qs, "ticket") or None)
    out["repo"] = name
    out["index"] = index
    return (200 if out.get("ok") else int(out.get("status", 400))), out


def slice_complete(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    index = data.get("index")
    if not isinstance(index, str) or not index.strip():
        return 400, {"error": "index is required", "code": "missing_index"}
    tid = data.get("id")
    if not isinstance(tid, str) or not tid.strip():
        return 400, {"error": "id is required", "code": "missing_ticket_id"}
    tickets, load_err = load_slice_index(resolved, index.strip())
    if load_err:
        return int(load_err.get("status", 400)), load_err
    assert tickets is not None
    found = None
    for t in tickets:
        if t["id"] == tid.strip():
            found = t
            break
    if found is None:
        return 400, {"error": f"unknown ticket id '{tid}'", "code": "unknown_ticket"}
    found["status"] = "done"
    write_err = write_slice_index(resolved, index.strip(), tickets, extra=index_extra(resolved, index.strip()))
    if write_err:
        return int(write_err.get("status", 400)), write_err
    return 200, {"ok": True, "repo": name, "index": index.strip(), "id": found["id"], "path": found["path"]}


def slice_continue(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    index = data.get("index")
    if not isinstance(index, str) or not index.strip():
        return 400, {"error": "index is required", "code": "missing_index"}
    tickets, load_err = load_slice_index(resolved, index.strip())
    if load_err:
        return int(load_err.get("status", 400)), load_err
    assert tickets is not None
    nxt = slice_frontier(tickets)
    out = {
        "ok": True,
        "repo": name,
        "index": index.strip(),
        "kind": nxt.get("kind"),
        "id": nxt.get("id"),
        "path": nxt.get("path"),
        "kicked": False,
    }
    if nxt.get("kind") != "build":
        return 200, out
    kick_url = data.get("kick_url")
    if kick_url in ("", None):
        return 200, out
    if not isinstance(kick_url, str) or not valid_webhook_url(kick_url):
        return 400, {"error": "kick_url must be https", "code": "bad_kick_url"}
    token = data.get("kick_token")
    if token in ("", None):
        token = None
    elif not isinstance(token, str):
        return 400, {"error": "kick_token must be a string", "code": "bad_kick_token"}
    payload = {
        "repo": name,
        "branch": data.get("branch"),
        "index": index.strip(),
        "ticket": nxt["path"],
        "test": data.get("test"),
        "commit_message": data.get("commit_message"),
        "iters_left": data.get("iters_left"),
        "class": data.get("class"),
        "size": data.get("size"),
        "ac_count": data.get("ac_count"),
        "seams": data.get("seams"),
        "kick_url": kick_url,
        "kick_token": token,
    }
    hook = post_receipt(kick_url, payload, token=token)
    out["kicked"] = bool(hook.get("ok"))
    out["kick"] = hook
    if not out["kicked"]:
        return 502, {**out, "ok": False, "error": "kick failed", "code": "kick_failed"}
    return 200, out


def slice_e2e(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    test_name = data.get("test")
    if test_name in ("", None):
        return 200, {"ok": True, "kind": "skip"}
    if not isinstance(test_name, str):
        return 400, {"error": "test must be a string", "code": "bad_test"}
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    index = data.get("index")
    if not isinstance(index, str) or not index.strip():
        return 400, {"error": "index is required", "code": "missing_index"}
    index = index.strip()
    tickets, load_err = load_slice_index(resolved, index)
    if load_err:
        return int(load_err.get("status", 400)), load_err
    assert tickets is not None
    extra = index_extra(resolved, index)
    catalog = load_tests(repos)
    spec = catalog.get(test_name.strip())
    if spec is None:
        return 400, {"error": f"unknown test '{test_name}'", "code": "unknown_test", "allowed": sorted(catalog)}
    result = run_cmd(list(spec["argv"]), cwd=spec["cwd"], timeout=int(spec["timeout_seconds"]))
    compact = compact_cmd_signal(result)
    if result.get("ok"):
        return 200, {"ok": True, "kind": "green", "repo": name, "compact": "ok"}
    reds = int(extra.get("e2e_reds") or 0) + 1
    extra["e2e_reds"] = reds
    if reds >= 3:
        write_err = write_slice_index(resolved, index, tickets, extra=extra)
        if write_err:
            return int(write_err.get("status", 400)), write_err
        return 200, {"ok": True, "kind": "park", "repo": name, "e2e_reds": reds, "compact": compact}
    hits = [ln.split("FAIL:", 1)[-1].strip() for ln in compact.splitlines() if "FAIL:" in ln]
    rel_dir = str(Path(index).parent)
    if hits:
        tid = f"e2e-{reds}"
        rel_md = f"{rel_dir}/{tid}.md"
        md_target, perr = safe_relpath(resolved, rel_md)
        if perr:
            return 400, perr
        assert md_target is not None
        md_target.parent.mkdir(parents=True, exist_ok=True)
        body_md = "E2E failed. Ticket from failing names.\n\n" + "\n".join(f"- {h}" for h in hits[:8]) + "\n"
        md_target.write_text(body_md)
        tickets.append(
            {"id": tid, "path": rel_md, "blocked_by": [], "kind": "build", "status": "open"}
        )
        write_err = write_slice_index(resolved, index, tickets, extra=extra)
        if write_err:
            return int(write_err.get("status", 400)), write_err
        return 200, {
            "ok": True,
            "kind": "auto",
            "repo": name,
            "id": tid,
            "path": rel_md,
            "e2e_reds": reds,
            "compact": compact,
        }
    draft = f"{rel_dir}/e2e-draft-{reds}.md"
    draft_path, perr = safe_relpath(resolved, draft)
    if perr:
        return 400, perr
    assert draft_path is not None
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text("E2E failed. Draft — ACK before it becomes a ticket.\n\n" + compact + "\n")
    write_err = write_slice_index(resolved, index, tickets, extra=extra)
    if write_err:
        return int(write_err.get("status", 400)), write_err
    return 200, {
        "ok": True,
        "kind": "draft",
        "repo": name,
        "path": draft,
        "e2e_reds": reds,
        "compact": compact,
    }


def parse_optional_int(data: dict[str, Any], key: str, lo: int, hi: int) -> tuple[int | None, dict[str, Any] | None]:
    raw = data.get(key)
    if raw in ("", None):
        return None, None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None, {"error": f"{key} must be an integer", "code": f"bad_{key}"}
    if n < lo or n > hi:
        return None, {"error": f"{key} out of range", "code": f"bad_{key}"}
    return n, None


def bump_slice_size(size: str, class_name: str, ac_count: int | None, seams: int | None) -> str:
    i = SLICE_SIZES.index(size)
    bump = 0
    if ac_count is not None and ac_count >= 6:
        bump = 1
    if class_name == "visual" and (seams is None or seams == 1):
        bump = max(bump, 1)
    return SLICE_SIZES[min(i + bump, len(SLICE_SIZES) - 1)]


def slice_route_lookup(
    class_name: str, size_floor: str, ac_count: int | None = None, seams: int | None = None
) -> dict[str, Any]:
    sized = bump_slice_size(size_floor, class_name, ac_count, seams)
    turns, wait = SLICE_BUDGET[(class_name, sized)]
    if class_name == "mechanical":
        launcher, model = "hermes", "grok-4.6"
        reviewer1, reviewer2 = ("none", "none") if sized == "S" else ("sonnet", "none")
    elif class_name == "visual":
        launcher, model = "claude", "claude-opus-5"
        reviewer1, reviewer2 = "sol", "none"
    else:
        launcher, model = "claude", "claude-opus-5"
        reviewer1, reviewer2 = "sol", "opus"
    return {
        "ok": True,
        "class": class_name,
        "size_floor": size_floor,
        "size": sized,
        "launcher": launcher,
        "model": model,
        "max_turns": turns,
        "run_budget_seconds": wait,
        "reviewer1": reviewer1,
        "reviewer2": reviewer2,
        "review": "none" if reviewer1 == "none" else ("sol_opus" if reviewer2 == "opus" else reviewer1),
    }


def slice_route(body: bytes) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    class_name = str(data.get("class") or "mechanical").strip()
    size_floor = str(data.get("size") or "M").strip()
    if class_name not in SLICE_CLASSES:
        return 400, {"error": "class must be mechanical, visual, or sensitive", "code": "bad_class"}
    if size_floor not in SLICE_SIZES:
        return 400, {"error": "size must be S, M, or L", "code": "bad_size"}
    ac_count, ac_err = parse_optional_int(data, "ac_count", 0, 99)
    if ac_err:
        return 400, ac_err
    seams, seams_err = parse_optional_int(data, "seams", 0, 20)
    if seams_err:
        return 400, seams_err
    return 200, slice_route_lookup(class_name, size_floor, ac_count, seams)


FINDINGS_MARKER = "<!-- engineering-findings-json"
FINDINGS_SEVERITIES = ("blocker", "critical", "major", "minor")


def pr_findings_from(labels: list[str], comment_bodies: list[str]) -> dict[str, Any]:
    """Turn a PR's labels and review comments into a fix brief.

    pr-drive used to take its instructions from CTX.INPUT.prompt, so getting a
    PR green started with a human reading the review. Both reviewers already
    publish a machine-readable block; parse that and skip the prose.

    Two reviewers routinely raise the same defect, so findings are deduped by
    fingerprint: that is one thing to fix, not two. A malformed block is an
    error rather than an empty list, because "nothing found" and "could not
    read what was found" must not look alike to the walker.
    """
    grade = next((l[len("grade-"):] for l in labels if l.startswith("grade-")), None)
    holds = sorted(l for l in labels if l.startswith("hold:"))

    findings: dict[str, dict[str, Any]] = {}
    for body in comment_bodies:
        start = 0
        while True:
            i = body.find(FINDINGS_MARKER, start)
            if i < 0:
                break
            end = body.find("-->", i)
            if end < 0:
                return {"ok": False, "blocking": True, "code": "unparsable_findings",
                        "error": "findings marker is not closed", "grade": grade, "holds": holds}
            raw = body[i + len(FINDINGS_MARKER):end]
            start = end + 3
            try:
                block = json.loads(raw)
            except ValueError as exc:
                return {"ok": False, "blocking": True, "code": "unparsable_findings",
                        "error": f"findings block is not JSON: {exc}", "grade": grade, "holds": holds}
            for f in block.get("findings") or []:
                fp = str(f.get("fingerprint") or "")
                if fp and fp not in findings:
                    findings[fp] = f

    ordered = sorted(
        findings.values(),
        key=lambda f: (
            FINDINGS_SEVERITIES.index(f.get("severity"))
            if f.get("severity") in FINDINGS_SEVERITIES
            else len(FINDINGS_SEVERITIES),
            str(f.get("fingerprint")),
        ),
    )
    counts = {s: sum(1 for f in ordered if f.get("severity") == s) for s in FINDINGS_SEVERITIES}
    return {
        "ok": True,
        "grade": grade,
        "holds": holds,
        "blocking": bool(ordered or holds),
        "findings": ordered,
        "brief": render_findings_brief(ordered),
        **counts,
    }


def render_findings_brief(findings: list[dict[str, Any]]) -> str:
    """The text the writer sees. Remedies only; reviewer reasoning stays out."""
    if not findings:
        return "No blocking findings."
    lines = ["Fix every finding below. Do not restructure anything else.", ""]
    for i, f in enumerate(findings, 1):
        loc = f.get("location") or {}
        where = str(loc.get("path") or "?")
        if loc.get("line"):
            where += ":%s" % loc["line"]
        lines.append("%d. [%s/%s] %s" % (i, f.get("severity", "?"), f.get("category", "?"),
                                         f.get("fingerprint", "?")))
        lines.append("   where:  %s" % where)
        lines.append("   remedy: %s" % (f.get("remedy") or "(none given)"))
        lines.append("")
    return "\n".join(lines).rstrip()


def review_said_nothing(text: str) -> bool:
    """True when the reviewer never emitted a verdict.

    A reviewer that ran out of turns, lost its connection, or died is not a
    reviewer that said no. The lock treats "timeout, no verdict" as a retry,
    and only a finished chain that still nacks as a strike. Callers get this
    as `no_verdict` so a provider blip is diagnosable in the trace instead of
    looking like an opinion.
    """
    upper = (text or "").upper()
    if any(t in upper for t in ("VERDICT: PASS", "VERDICT:PASS", '"VERDICT": "PASS"')):
        return False
    if any(t in upper for t in ("VERDICT: NACK", "VERDICT: REQUEST_CHANGES", "VERDICT: FAIL")):
        return False
    return parse_receipt_text(text or "") is None


def parse_review_verdict(text: str) -> tuple[str, str]:
    raw = text or ""
    upper = raw.upper()
    if "VERDICT: PASS" in upper or '"VERDICT": "PASS"' in upper or "VERDICT:PASS" in upper:
        verdict = "PASS"
    elif "VERDICT: NACK" in upper or "VERDICT: REQUEST_CHANGES" in upper or "VERDICT: FAIL" in upper:
        verdict = "NACK"
    else:
        parsed = parse_receipt_text(raw)
        if parsed and str(parsed.get("verdict") or parsed.get("status") or "").upper() in {"PASS", "OK"}:
            verdict = "PASS"
        elif parsed:
            verdict = "NACK"
        else:
            verdict = "NACK"
    summary = raw.strip().splitlines()
    line = next((ln.strip() for ln in summary if ln.strip()), "no review output")
    return verdict, line[:500]


def review_result(reviewer: str, prompt: str, resolved: Path) -> dict[str, Any]:
    """Run one spec-review to completion and return its verdict payload."""
    diff = git_diff(resolved, "HEAD", None)
    diff_text = str(diff.get("diff") or "")[:12000]
    body_prompt = (
        "Spec-review only. Do not edit files, commit, or push.\n"
        "Return exactly:\nVERDICT: PASS\nor\nVERDICT: NACK\n"
        f"Ticket:\n{prompt[:8000]}\n\nDiff:\n{diff_text}\n"
    )
    if reviewer in {"sonnet", "opus"}:
        if not CLAUDE_BIN.is_file():
            return {"ok": False, "verdict": "NACK", "no_verdict": True,
                    "error": f"claude binary missing: {CLAUDE_BIN}", "code": "not_implemented"}
        model = "sonnet" if reviewer == "sonnet" else "opus"
        cmd = [
            str(CLAUDE_BIN),
            "-p",
            "--output-format",
            "text",
            "--permission-mode",
            "plan",
            "--max-turns",
            str(REVIEW_MAX_TURNS),
            "--model",
            model,
            body_prompt,
        ]
        env = {k: v for k, v in os.environ.items()}
    else:
        if not HERMES_BIN.is_file():
            return {"ok": False, "verdict": "NACK", "no_verdict": True,
                    "error": f"hermes binary missing: {HERMES_BIN}", "code": "not_implemented"}
        cmd = [
            str(HERMES_BIN),
            "chat",
            "-Q",
            "--query",
            body_prompt,
            "--in",
            str(resolved),
            "--no-restore-cwd",
            "-t",
            REVIEW_TOOLSETS,
            "--max-turns",
            str(REVIEW_MAX_TURNS),
            "--run-budget",
            "180",
            "--yolo",
            "--source",
            "tool",
        ]
        env = hermes_job_env({"job_id": "review", "cwd": str(resolved)})
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(resolved),
            env=env,
            capture_output=True,
            timeout=200,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "verdict": "NACK", "no_verdict": True,
                "error": "review timed out", "code": "timeout", "reviewer": reviewer}
    text = (proc.stdout or b"").decode("utf-8", "replace")
    verdict, summary = parse_review_verdict(text)
    return {
        "ok": verdict == "PASS",
        "verdict": verdict,
        "no_verdict": review_said_nothing(text),
        "reviewer": reviewer,
        "summary": summary,
        "compact": compact_cmd_signal({"ok": verdict == "PASS", "stdout": text, "stderr": ""}),
        "returncode": proc.returncode,
    }


def review_receipt(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok" if result.get("ok") else "nack",
        "job_id": job["job_id"],
        "verdict": result.get("verdict"),
        "no_verdict": bool(result.get("no_verdict")),
        "reviewer": job.get("reviewer"),
        "summary": str(result.get("summary") or result.get("error") or "")[:500],
        "compact": result.get("compact") or "",
    }


def run_review_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    result = review_result(job["reviewer"], job["prompt"], Path(job["cwd"]))
    receipt = review_receipt(job, result)
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["receipt"] = receipt
    job["result"] = result
    job["status"] = "completed"
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def enqueue_review(job: dict[str, Any]) -> None:
    herdr_follow_job(job)
    thread = threading.Thread(
        target=run_review_job, args=(job["job_id"],), name=f"graphwing-review-{job['job_id'][:8]}", daemon=True
    )
    thread.start()


def review_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Spec-review. Async when a webhook is supplied, otherwise synchronous.

    A real review reads a 12KB diff and thinks, which routinely outlives the
    Cloudflare tunnel's 120s proxy read timeout: SC-110290's second run died
    with a 524 mid-review. agentRun already solves this by returning 202 and
    POSTing the verdict back to a wait node. Do the same here. The sync path
    stays for direct calls that are not behind the tunnel.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    reviewer = str(data.get("reviewer") or "").strip()
    if reviewer not in {"sonnet", "opus", "sol"}:
        return 400, {"error": "reviewer must be sonnet, opus, or sol", "code": "bad_reviewer"}
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 400, {"error": "prompt is required", "code": "missing_prompt"}
    prompt = prompt.strip()
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err

    if not webhook_url:
        result = review_result(reviewer, prompt, resolved)
        if result.get("code") == "not_implemented":
            return 501, result
        if result.get("code") == "timeout":
            return 504, result
        return 200, result

    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "kind": "review",
            "status": "queued",
            "reviewer": reviewer,
            "repo": name,
            "cwd": str(resolved),
            "prompt": prompt,
            "response_webhook_url": webhook_url,
            "response_webhook_token": webhook_token,
            "resume_url": webhook_url,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "receipt": None,
            "log_ref": str(job_dir(job_id) / "stdout.log"),
            "error": None,
            "webhook": None,
        }
        job_dir(job_id).mkdir(parents=True, exist_ok=True)
        write_job(job)
    enqueue_review(job)
    return 202, {
        "ok": True,
        "job_id": job_id,
        "kind": "review",
        "status": "queued",
        "reviewer": reviewer,
        "repo": name,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def gh_json(path: Path, args: list[str]) -> dict[str, Any]:
    r = run_cmd(["gh", *args], cwd=path)
    if not r.get("ok"):
        return r
    try:
        parsed = json.loads(r["stdout"] or "null")
    except json.JSONDecodeError:
        return {"ok": False, "error": "gh returned non-JSON", "code": "gh_json", "status": 502}
    return {"ok": True, "data": parsed, "truncated": r["truncated"]}


GH_CHECK_PASS = frozenset({"pass", "skipping", "skip", "success"})
GH_CHECK_FAIL = frozenset({"fail", "failure", "cancel", "cancelled", "cancelling", "error"})


def annotate_pr_view(out: dict[str, Any]) -> dict[str, Any]:
    """Expose review state at the action result level for Graph switches."""
    if not out.get("ok"):
        return out
    data = out.get("data")
    if not isinstance(data, dict):
        data = {}
    review_decision = str(data.get("reviewDecision") or "")
    reviews_blocking = review_decision in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}
    out["review_decision"] = review_decision
    out["merge_state"] = str(data.get("mergeStateStatus") or "")
    out["reviews_blocking"] = reviews_blocking
    out["reviews_ok"] = not reviews_blocking
    return out


def annotate_pr_checks(out: dict[str, Any]) -> dict[str, Any]:
    """HTTP 200 means gh ran. Graph gates on all_green / any_red, not the agent."""
    if not out.get("ok"):
        return out
    rows = out.get("data")
    if not isinstance(rows, list):
        rows = []
    failing: list[str] = []
    pending: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bucket = str(row.get("bucket") or row.get("state") or "").strip().lower()
        name = str(row.get("name") or "")
        if bucket in GH_CHECK_FAIL:
            failing.append(name)
        elif bucket not in GH_CHECK_PASS:
            pending.append(name)
    out["failing"] = failing
    out["pending"] = pending
    out["any_red"] = bool(failing)
    out["all_green"] = not failing and not pending
    return out


def units_status() -> dict[str, Any]:
    units = {}
    all_ok = True
    for name in UNITS:
        r = run_cmd(["systemctl", "--user", "is-active", name], timeout=5)
        state = (r.get("stdout") or r.get("error") or "unknown").strip()
        units[name] = {"active": r.get("ok") is True and state == "active", "state": state}
        if not units[name]["active"]:
            all_ok = False
    return {"ok": True, "healthy": all_ok, "units": units}


def podman_env() -> dict[str, str]:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    docker_host = os.environ.get("DOCKER_HOST") or f"unix://{runtime}/podman/podman.sock"
    return {
        **os.environ,
        "HOME": str(Path.home()),
        "XDG_RUNTIME_DIR": runtime,
        "DOCKER_HOST": docker_host,
        "GIT_TERMINAL_PROMPT": "0",
    }


def probe_loopback(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost"):
        return {"ok": False, "url": url, "error": "health url must be loopback http(s)"}
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=2) as resp:
            code = getattr(resp, "status", 200)
            return {"ok": 200 <= code < 500, "url": url, "status": code}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)[:160]}


def parse_compose_ps(raw: str) -> list[dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict):
        rows = [parsed]
    else:
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "name": row.get("Name") or row.get("Names") or row.get("Service"),
                "service": row.get("Service"),
                "state": row.get("State") or row.get("Status"),
                "health": row.get("Health"),
                "exit_code": row.get("ExitCode"),
            }
        )
    return out


def stack_status(name: str | None) -> dict[str, Any]:
    stacks, _ports = load_stacks()
    if not stacks:
        return {"ok": False, "error": "stacks.json is not configured", "code": "not_configured", "status": 501}
    key = (name or "").strip()
    if not key:
        key = "riftwing" if "riftwing" in stacks else next(iter(stacks))
    if key not in stacks:
        return {"ok": False, "error": f"unknown stack '{key}'", "code": "unknown_stack", "allowed": sorted(stacks), "status": 400}
    spec = stacks[key]
    cwd: Path = spec["cwd"]
    compose = spec["compose_file"]
    args = ["podman", "compose"]
    if compose:
        args.extend(["-f", compose])
    args.extend(["ps", "-a", "--format", "json"])
    r = run_cmd(args, cwd=cwd, timeout=20, env=podman_env())
    containers = parse_compose_ps(r.get("stdout") or "") if r.get("ok") else []
    health = [{"name": h["name"], **probe_loopback(h["url"])} for h in spec["health"]]
    health_ok = all(h.get("ok") for h in health) if health else bool(containers)
    running = [c for c in containers if str(c.get("state") or "").lower() in ("running", "up")]
    return {
        "ok": True,
        "stack": key,
        "cwd": str(cwd),
        "healthy": bool(running) and health_ok,
        "containers": containers,
        "health": health,
        "compose_ok": bool(r.get("ok")),
        "compose_error": None if r.get("ok") else (r.get("error") or "compose ps failed"),
    }


def port_listening(port: int) -> bool:
    r = run_cmd(["ss", "-ltnH", f"sport = :{port}"], timeout=5)
    if not r.get("ok"):
        return False
    return bool((r.get("stdout") or "").strip())


def port_check(raw: str | None) -> dict[str, Any]:
    _stacks, allowed = load_stacks()
    if not allowed:
        return {"ok": False, "error": "stacks.json is not configured", "code": "not_configured", "status": 501}
    if not raw or not raw.strip():
        return {"ok": False, "error": "port or ports is required", "code": "missing_port", "status": 400}
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    ports: list[int] = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            return {"ok": False, "error": f"port must be an integer: {p}", "code": "bad_port", "status": 400}
        if n not in allowed:
            return {
                "ok": False,
                "error": f"port {n} is not allowlisted",
                "code": "unknown_port",
                "allowed": sorted(allowed),
                "status": 400,
            }
        ports.append(n)
    results = [{"port": n, "listening": port_listening(n)} for n in ports]
    return {"ok": True, "ports": results}


def herdr_agents() -> dict[str, Any]:
    r = run_cmd(["herdr", "--session", HERDR_SESSION, "agent", "list"], timeout=10)
    if not r.get("ok"):
        return r
    try:
        parsed = json.loads(r["stdout"] or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "herdr returned non-JSON", "code": "herdr_json", "status": 502}
    agents = (parsed.get("result") or {}).get("agents") or []
    return {"ok": True, "session": HERDR_SESSION, "agents": agents}


def herdr_enabled() -> bool:
    return os.environ.get("GRAPHWING_HERDR", "1").strip().lower() not in ("0", "false", "off", "no")


def herdr_cli(args: list[str], timeout: int = 10) -> dict[str, Any] | None:
    r = run_cmd(["herdr", "--session", HERDR_SESSION, *args], timeout=timeout)
    if not r.get("ok"):
        return None
    try:
        parsed = json.loads(r["stdout"] or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    result = parsed.get("result")
    return result if isinstance(result, dict) else parsed


def herdr_send(pane_id: str, text: str) -> None:
    run_cmd(["herdr", "--session", HERDR_SESSION, "pane", "send-text", pane_id, text], timeout=8)


def herdr_echo(pane_id: str, line: str) -> None:
    safe = "".join(c if (c.isprintable() and c != "'") else " " for c in line).strip()[:240]
    if not safe:
        return
    herdr_send(pane_id, f"printf '%s\\n' '{safe}'\n")


def herdr_report(pane_id: str, agent: str, state: str, message: str | None = None) -> None:
    args = ["pane", "report-agent", pane_id, "--source", HERDR_SOURCE, "--agent", agent, "--state", state]
    if message:
        args.extend(["--message", message[:120]])
    herdr_cli(args)


def herdr_ensure_graph_pane() -> str | None:
    listed = herdr_cli(["tab", "list"]) or {}
    tabs = listed.get("tabs") if isinstance(listed.get("tabs"), list) else []
    tab_id = None
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        label = str(tab.get("label") or "")
        if label in (HERDR_GRAPH_LABEL, "graphwing-probe"):
            tab_id = str(tab.get("tab_id") or "")
            if label != HERDR_GRAPH_LABEL and tab_id:
                herdr_cli(["tab", "rename", tab_id, HERDR_GRAPH_LABEL])
            break
    if not tab_id:
        created = herdr_cli(["tab", "create", "--label", HERDR_GRAPH_LABEL, "--no-focus", "--cwd", str(HOME)])
        if not created:
            return None
        root = created.get("root_pane") if isinstance(created.get("root_pane"), dict) else {}
        pane_id = str(root.get("pane_id") or "")
        if pane_id:
            herdr_report(pane_id, HERDR_GRAPH_LABEL, "idle", "graphwing")
            return pane_id
        tab_id = str((created.get("tab") or {}).get("tab_id") or "")
    if not tab_id:
        return None
    panes = herdr_cli(["pane", "list"]) or {}
    for pane in panes.get("panes") or []:
        if isinstance(pane, dict) and pane.get("tab_id") == tab_id:
            return str(pane.get("pane_id") or "") or None
    return None


def herdr_log(line: str) -> None:
    if not herdr_enabled():
        return
    with HERDR_LOCK:
        pane_id = herdr_ensure_graph_pane()
        if not pane_id:
            return
        herdr_echo(pane_id, f"{utcnow()} {line}")
        herdr_report(pane_id, HERDR_GRAPH_LABEL, "idle", line[:120])


def herdr_job_tab_label(job: dict[str, Any]) -> str:
    job_id = str(job.get("job_id") or "")
    tag = {"script": "s", "test": "t", "rr": "r"}.get(str(job.get("kind") or ""), "s" if job.get("script") else "a")
    return f"gw-{tag}-{job_id[:8]}"


def herdr_close_tab(tab_id: str) -> None:
    if tab_id:
        with HERDR_LINGER_LOCK:
            HERDR_LINGER.pop(tab_id, None)
        herdr_cli(["tab", "close", tab_id])


def herdr_tab_lingering(tab_id: str) -> bool:
    """True while a finished tab is inside its reading window."""
    with HERDR_LINGER_LOCK:
        deadline = HERDR_LINGER.get(tab_id)
    return bool(deadline and time.time() < deadline)


def herdr_close_tab_later(tab_id: str, delay: float) -> None:
    if not tab_id:
        return
    if delay <= 0:
        herdr_close_tab(tab_id)
        return
    with HERDR_LINGER_LOCK:
        HERDR_LINGER[tab_id] = time.time() + delay
    timer = threading.Timer(delay, herdr_close_tab, args=(tab_id,))
    timer.daemon = True
    timer.start()


def herdr_prune_job_tabs() -> None:
    listed = herdr_cli(["tab", "list"]) or {}
    tabs = [t for t in (listed.get("tabs") or []) if isinstance(t, dict) and str(t.get("label") or "").startswith("gw-")]
    live: list[dict[str, Any]] = []
    for tab in tabs:
        tid = str(tab.get("tab_id") or "")
        if str(tab.get("agent_status") or "") == "done":
            if not herdr_tab_lingering(tid):
                herdr_close_tab(tid)
                continue
        live.append(tab)
    extra = len(live) - HERDR_JOB_TAB_MAX
    if extra <= 0:
        return
    for tab in live[:extra]:
        herdr_close_tab(str(tab.get("tab_id") or ""))


def herdr_follow_job(job: dict[str, Any]) -> None:
    if not herdr_enabled():
        return
    job_id = str(job.get("job_id") or "")
    if not JOB_ID_RE.fullmatch(job_id):
        return
    label = herdr_job_tab_label(job)
    log_path = Path(job.get("log_ref") or (job_dir(job_id) / "stdout.log"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
    except OSError:
        return
    with HERDR_LOCK:
        herdr_prune_job_tabs()
        created = herdr_cli(
            ["tab", "create", "--label", label, "--no-focus", "--cwd", str(job.get("cwd") or HOME)]
        )
        if not created:
            return
        root = created.get("root_pane") if isinstance(created.get("root_pane"), dict) else {}
        pane_id = str(root.get("pane_id") or "")
        tab_id = str((created.get("tab") or {}).get("tab_id") or root.get("tab_id") or "")
        if not pane_id:
            return
        job["herdr_pane_id"] = pane_id
        job["herdr_tab_id"] = tab_id
        write_job(job)
        herdr_report(pane_id, label, "working", job.get("script") or job.get("profile") or "job")
        herdr_send(pane_id, f"tail -F '{log_path}'\n")


def herdr_show_session(job: dict[str, Any]) -> None:
    """Swap a finished writer's pane from the log tail to its real session.

    While the job runs, the headless process owns that Hermes session, so the
    pane tails stdout.log instead. Once it is done the tail is a frozen file
    and the session is fully readable, which is what the linger window is for.
    Only writer jobs have one; test and review are not Hermes, so they keep
    the tail.
    """
    if HERDR_TAB_LINGER_SECONDS <= 0:
        return
    pane_id = str(job.get("herdr_pane_id") or "")
    session = job.get("hermes_session")
    if not pane_id or not isinstance(session, str) or not HERMES_SESSION_RE.fullmatch(session):
        return
    cwd = str(job.get("cwd") or HOME)
    # Ctrl-C stops tail -F before the pane runs anything else.
    herdr_send(pane_id, "\x03")
    herdr_send(pane_id, f"{HERMES_BIN} --resume '{session}' --in '{cwd}' --no-restore-cwd\n")


def herdr_job_done(job: dict[str, Any]) -> None:
    if not herdr_enabled():
        return
    receipt = job.get("receipt") if isinstance(job.get("receipt"), dict) else {}
    summary = str(receipt.get("summary") or job.get("status") or "done")[:80]
    kind = job.get("kind") or job.get("script") or "agent"
    herdr_log(f"done {kind} {str(job.get('job_id') or '')[:8]} {job.get('status')} {summary}")
    tab_id = str(job.get("herdr_tab_id") or "")
    label = herdr_job_tab_label(job)
    if not tab_id:
        listed = herdr_cli(["tab", "list"]) or {}
        for tab in listed.get("tabs") or []:
            if isinstance(tab, dict) and str(tab.get("label") or "") == label:
                tab_id = str(tab.get("tab_id") or "")
                break
    herdr_show_session(job)
    herdr_close_tab_later(tab_id, HERDR_TAB_LINGER_SECONDS)


def summarize_op(path: str, status: int, payload: dict[str, Any] | bytes) -> str:
    bits = [path, str(status)]
    if not isinstance(payload, dict):
        return " ".join(bits)
    for key in ("repo", "name", "profile", "job_id", "branch", "code"):
        val = payload.get(key)
        if val not in (None, ""):
            bits.append(str(val)[:48])
    if payload.get("ok") is False and payload.get("error"):
        bits.append(str(payload["error"])[:60])
    rec = payload.get("receipt")
    if isinstance(rec, dict) and rec.get("summary"):
        bits.append(str(rec["summary"])[:60])
    return " ".join(bits)


_ANNOUNCE_SKIP_PREFIX = ("/v1/agent/jobs/",)
_ANNOUNCE_SKIP = {"/v1/health", "/health", "/openapi.json", "/v1/openapi.json", "/v1/herdr/agents", "/v1/units/status"}


def herdr_announce(method: str, path: str, status: int, payload: dict[str, Any] | bytes) -> None:
    if not herdr_enabled():
        return
    if status == 401 or path in _ANNOUNCE_SKIP or path.startswith(_ANNOUNCE_SKIP_PREFIX):
        return
    if method == "GET" and path == "/v1/agent/profiles":
        return
    line = summarize_op(path, status, payload)

    def _run() -> None:
        try:
            herdr_log(f"{method} {line}")
        except Exception:
            return

    threading.Thread(target=_run, name="graphwing-herdr", daemon=True).start()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def job_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def read_job(job_id: str) -> dict[str, Any] | None:
    path = job_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_job(job: dict[str, Any]) -> None:
    path = job_path(job["job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    tmp.replace(path)


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "job_id",
        "status",
        "profile",
        "repo",
        "cwd",
        "prompt",
        "hermes_session",
        "created_at",
        "started_at",
        "finished_at",
        "receipt",
        "log_ref",
        "error",
        "webhook",
    )
    out = {k: job.get(k) for k in keys}
    out["ok"] = True
    return out


def list_agent_profiles() -> dict[str, Any]:
    return {"ok": True, "profiles": load_profiles()}


def active_job_count() -> int:
    if not JOBS_DIR.is_dir():
        return 0
    n = 0
    for path in JOBS_DIR.glob("*/job.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("status") in ("queued", "running"):
            n += 1
    return n


def resolve_run_cwd(raw: str | None, repos: dict[str, str]) -> tuple[str, Path] | tuple[None, dict[str, Any]]:
    key = (raw or default_repo_name(repos)).strip()
    if not key:
        key = default_repo_name(repos)
    if "/" in key or key.startswith("."):
        return None, {
            "error": "cwd must be an allowlisted repo short name, not a filesystem path",
            "code": "bad_cwd",
            "allowed": sorted(repos),
        }
    return resolve_repo(key, repos)


def parse_json_object(body: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not body:
        return {}, None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {"error": "body must be JSON", "code": "bad_json"}
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, {"error": "body must be a JSON object", "code": "bad_json"}
    return data, None


def _request_header(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    direct = headers.get(name) if hasattr(headers, "get") else None
    if direct is not None:
        return str(direct)
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return ""


def doorbell_pr_drive(body: bytes, headers: Any) -> tuple[int, dict[str, Any]]:
    authorization = _request_header(headers, "Authorization").strip()
    if not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        return 401, {"error": "GitHub Actions OIDC bearer token required", "code": "missing_oidc"}
    token = authorization[7:].strip()
    try:
        config = load_doorbell()
        claims = verify_github_oidc(token, config)
    except Exception:
        return 401, {"error": "invalid GitHub Actions OIDC token", "code": "invalid_oidc"}

    repository = str(claims.get("repository") or "")
    actor = str(claims.get("actor") or "")
    allow_repos = config.get("allow_repos")
    if not isinstance(allow_repos, dict) or repository not in allow_repos:
        return 403, {"error": "repository is not allowed", "code": "repo_not_allowed"}
    allow_actors = config.get("allow_actors")
    if not isinstance(allow_actors, list) or actor not in allow_actors:
        return 403, {"error": "actor is not allowed", "code": "actor_not_allowed"}

    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    pr = data.get("pr")
    if pr in (None, ""):
        return 200, {"ok": True, "skipped": True, "reason": "missing_pr"}

    mapping = allow_repos[repository]
    if isinstance(mapping, str):
        mapping = {"repo": mapping}
    if not isinstance(mapping, dict) or not str(mapping.get("repo") or "").strip():
        return 403, {"error": "repository is not allowed", "code": "repo_not_allowed"}
    mapped_repo = str(mapping["repo"]).strip()
    defaults = {
        "riftwing-drive": {"test": "go-fmt"},
        "graphwing": {"test": "catalog-compile"},
    }
    settings = defaults.get(mapped_repo, {})
    hook_payload = {
        "repo": mapped_repo,
        "pr": pr,
        "test": str(mapping.get("test") or settings.get("test") or ""),
        "prompt": str(mapping.get("prompt") or DOORBELL_PROMPT),
        "commit_message": str(mapping.get("commit_message") or "pr-drive doorbell"),
    }
    try:
        install = load_rewst_install()
    except Exception:
        install = {}
    hook_url = str(install.get("pr_drive_hook_url") or "").strip()
    hook_secret = str(install.get("hook_secret") or install.get("pr_drive_hook_secret") or "").strip()
    if not hook_url or not hook_secret:
        return 503, {"error": "pr-drive hook is not configured", "code": "hook_unconfigured"}

    request = Request(
        hook_url,
        data=json.dumps(hook_payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-rewst-secret": hook_secret},
    )
    try:
        response = urlopen(request, timeout=20)
        rewst_status = int(getattr(response, "status", 200))
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except Exception:
        return 502, {"error": "pr-drive hook failed", "code": "hook_failed"}
    if not 200 <= rewst_status < 300:
        return 502, {"error": "pr-drive hook failed", "code": "hook_failed"}
    return 200, {
        "ok": True,
        "mapped_repo": mapped_repo,
        "pr": pr,
        "rewst_status": rewst_status,
    }


def valid_webhook_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.netloc.startswith(".")


def parse_webhook_fields(data: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any] | None]:
    url = data.get("response_webhook_url")
    if url in ("", None):
        url = data.get("resume_url")
    if url in ("", None):
        url = None
    elif not isinstance(url, str) or not valid_webhook_url(url):
        return None, None, {"error": "response_webhook_url must be https", "code": "bad_webhook_url"}
    token = data.get("response_webhook_token")
    if token in ("", None):
        token = None
    elif not isinstance(token, str):
        return None, None, {"error": "response_webhook_token must be a string", "code": "bad_webhook_token"}
    else:
        token = token.strip() or None
        if token and len(token) > 2048:
            return None, None, {"error": "response_webhook_token too large", "code": "bad_webhook_token"}
    return url, token, None


def parse_hermes_session(data: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    raw = data.get("hermes_session")
    if raw in ("", None):
        return None, None
    if not isinstance(raw, str):
        return None, {"error": "hermes_session must be gwslice-<32 hex>", "code": "bad_hermes_session"}
    name = raw.strip()
    if not HERMES_SESSION_RE.fullmatch(name):
        return None, {"error": "hermes_session must be gwslice-<32 hex>", "code": "bad_hermes_session"}
    return name, None


def compact_cmd_signal(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "ok"
    blob = "\n".join(
        [
            str(result.get("error") or ""),
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
        ]
    )
    lines = [ln.rstrip() for ln in blob.splitlines() if ln.strip()]
    hits = [ln for ln in lines if COMPACT_FAIL_RE.search(ln)]
    tail = lines[-COMPACT_TAIL_LINES:]
    seen: set[str] = set()
    out: list[str] = []
    for ln in hits + tail:
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    text = "\n".join(out).strip() or "failed (no compact lines)"
    return text[:COMPACT_MAX_CHARS]


def wrap_prompt(job_id: str, prompt: str, cwd: str) -> str:
    root = str(Path(cwd).resolve())
    return (
        f"GRAPHWING_JOB {job_id}\n"
        "You are a Graph node on this laptop, not the story owner. One job only. "
        "Do not research+write+review+ship.\n"
        f"Working directory is {root}. Create and edit files only inside that directory. "
        "Do not write to any other path.\n"
        "Stage the files you changed with `git add --` and relative paths only. No `git add -A`.\n"
        "Do not git commit, git push, or open a PR. Graph owns those ops.\n"
        "Do not `git checkout` another branch. Graph already checked out the job branch.\n"
        "Finish with a single JSON object and nothing after it:\n"
        '{"status":"ok"|"error","sha":null,"pr_url":null,"summary":"<one line>"}\n\n'
        f"Task:\n{prompt}\n"
    )


def parse_receipt_text(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    candidates: list[str] = [raw]
    if "```" in raw:
        for part in raw.split("```"):
            p = part.strip()
            if p.lower().startswith("json"):
                p = p[4:].lstrip()
            if p.startswith("{"):
                candidates.append(p)
    start = raw.rfind("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(raw)):
            ch = raw[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start : i + 1])
                    break
    for candidate in reversed(candidates):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("status") in ("ok", "error", "timeout"):
            return obj
    return None


def normalize_receipt(job: dict[str, Any], parsed: dict[str, Any] | None, returncode: int, timed_out: bool) -> dict[str, Any]:
    status = "timeout" if timed_out else ("ok" if returncode == 0 else "error")
    if parsed and parsed.get("status") in ("ok", "error", "timeout") and not timed_out:
        status = str(parsed["status"])
    rec = {
        "status": status,
        "job_id": job["job_id"],
        "profile": job["profile"],
        "hermes_session": job.get("hermes_session"),
        "sha": None,
        "pr_url": None,
        "log_ref": job["log_ref"],
        "summary": None,
    }
    if parsed:
        sha = parsed.get("sha")
        pr_url = parsed.get("pr_url")
        summary = parsed.get("summary")
        if sha not in (None, ""):
            rec["sha"] = str(sha)[:64]
        if pr_url not in (None, ""):
            rec["pr_url"] = str(pr_url)[:500]
        if summary not in (None, ""):
            rec["summary"] = str(summary)[:500]
    return rec


def post_receipt(url: str, receipt: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Rewst-Token"] = token
    data = json.dumps(receipt).encode()
    req = Request(url, data=data, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=15) as resp:
            return {"ok": 200 <= getattr(resp, "status", 200) < 300, "status": getattr(resp, "status", 200)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def deliver_webhook(job: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any] | None:
    url = job.get("response_webhook_url") or job.get("resume_url")
    if not url:
        return None
    return post_receipt(str(url), receipt, token=job.get("response_webhook_token"))


def hermes_job_env(job: dict[str, Any]) -> dict[str, str]:
    """Graph cwd wins over Hermes config.yaml terminal.cwd and inherited TERMINAL_CWD."""
    cwd = str(Path(job["cwd"]).resolve())
    env = {k: v for k, v in os.environ.items() if k not in ("TERMINAL_CWD", "PWD")}
    env.update(
        {
            "HERMES_HOME": str(HOME),
            "GRAPHWING_JOB_ID": str(job["job_id"]),
            "GIT_TERMINAL_PROMPT": "0",
            "GH_PROMPT_DISABLED": "1",
            "TERMINAL_CWD": cwd,
            "PWD": cwd,
        }
    )
    return env


def spawn_hermes(job: dict[str, Any]) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    if not HERMES_BIN.is_file():
        return None, {"error": f"missing hermes binary: {HERMES_BIN}", "code": "missing_binary"}
    prompt_path = job_dir(job["job_id"]) / "prompt.txt"
    stdout_path = job_dir(job["job_id"]) / "stdout.log"
    stderr_path = job_dir(job["job_id"]) / "stderr.log"
    stdout_f = stdout_path.open("wb")
    stderr_f = stderr_path.open("wb")
    cwd = str(Path(job["cwd"]).resolve())
    env = hermes_job_env(job)
    cmd = [
        str(HERMES_BIN),
        "chat",
        "-Q",
        "--query-file",
        str(prompt_path),
        "--in",
        cwd,
        "--no-restore-cwd",
        "--max-turns",
        str(job["max_turns"]),
        "--run-budget",
        str(job["run_budget_seconds"]),
        "--yolo",
        "--source",
        "tool",
    ]
    session = job.get("hermes_session")
    if isinstance(session, str) and HERMES_SESSION_RE.fullmatch(session):
        cmd.extend(["--continue", session, "--create-if-missing"])
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_f,
            stderr=stderr_f,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        stdout_f.close()
        stderr_f.close()
        return None, {"error": f"spawn failed: {exc}", "code": "spawn_failed"}
    stdout_f.close()
    stderr_f.close()
    return proc, None


def spawn_claude(job: dict[str, Any]) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    if not CLAUDE_BIN.is_file():
        return None, {"error": f"missing claude binary: {CLAUDE_BIN}", "code": "missing_binary"}
    prompt_path = job_dir(job["job_id"]) / "prompt.txt"
    stdout_path = job_dir(job["job_id"]) / "stdout.log"
    stderr_path = job_dir(job["job_id"]) / "stderr.log"
    try:
        prompt = prompt_path.read_text()
    except OSError as exc:
        return None, {"error": f"missing prompt: {exc}", "code": "missing_prompt"}
    stdout_f = stdout_path.open("wb")
    stderr_f = stderr_path.open("wb")
    cwd = str(Path(job["cwd"]).resolve())
    env = {k: v for k, v in os.environ.items()}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "PWD": cwd})
    cmd = [
        str(CLAUDE_BIN),
        "-p",
        "--output-format",
        "text",
        "--permission-mode",
        "acceptEdits",
        "--max-turns",
        str(job["max_turns"]),
        "--add-dir",
        cwd,
        "--model",
        str(job.get("model") or "claude-opus-5"),
        prompt,
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_f,
            stderr=stderr_f,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        stdout_f.close()
        stderr_f.close()
        return None, {"error": f"spawn failed: {exc}", "code": "spawn_failed"}
    stdout_f.close()
    stderr_f.close()
    return proc, None


def spawn_writer(job: dict[str, Any]) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    if job.get("launcher") == "claude":
        return spawn_claude(job)
    return spawn_hermes(job)


def _kill_proc(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(timeout=5)


def run_agent_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    proc, err = spawn_writer(job)
    if err:
        job["status"] = "failed"
        job["finished_at"] = utcnow()
        job["error"] = err.get("error")
        job["receipt"] = normalize_receipt(job, {"status": "error", "summary": err.get("error")}, 1, False)
        hook = deliver_webhook(job, job["receipt"])
        if hook is not None:
            job["webhook"] = hook
        write_job(job)
        herdr_job_done(job)
        return
    assert proc is not None
    job["pid"] = proc.pid
    write_job(job)
    timed_out = False
    wait_for = int(job.get("run_budget_seconds") or AGENT_RUN_BUDGET) + 60
    try:
        returncode = proc.wait(timeout=wait_for)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_proc(proc)
        returncode = proc.returncode if proc.returncode is not None else -9
    stdout = ""
    stdout_path = job_dir(job_id) / "stdout.log"
    try:
        stdout = stdout_path.read_text(errors="replace")[-CMD_MAX_BYTES:]
    except OSError:
        stdout = ""
    parsed = parse_receipt_text(stdout)
    receipt = normalize_receipt(job, parsed, returncode, timed_out)
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["returncode"] = returncode
    job["receipt"] = receipt
    job["status"] = "completed" if receipt["status"] == "ok" else "failed"
    if timed_out:
        job["error"] = "agent run budget exceeded"
    elif receipt["status"] != "ok" and not job.get("error"):
        job["error"] = receipt.get("summary") or f"hermes exited {returncode}"
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def enqueue_agent(job: dict[str, Any]) -> None:
    herdr_follow_job(job)
    thread = threading.Thread(target=run_agent_job, args=(job["job_id"],), name=f"graphwing-job-{job['job_id'][:8]}", daemon=True)
    thread.start()


def enqueue_script(job: dict[str, Any]) -> None:
    herdr_follow_job(job)
    thread = threading.Thread(target=run_script_job, args=(job["job_id"],), name=f"graphwing-script-{job['job_id'][:8]}", daemon=True)
    thread.start()


def script_receipt(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    timed_out = result.get("code") == "timeout"
    if timed_out:
        status = "timeout"
        summary = "script timed out"
    elif result.get("ok"):
        status = "ok"
        summary = f"{job.get('script')} exited {result.get('returncode', 0)}"
    else:
        status = "error"
        summary = str(result.get("error") or f"{job.get('script')} failed")[:500]
    return {
        "status": status,
        "job_id": job["job_id"],
        "log_ref": job["log_ref"],
        "summary": summary[:500],
    }


def run_script_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    result = run_cmd(list(job["argv"]), cwd=Path(job["cwd"]), timeout=int(job["timeout_seconds"]))
    jdir = job_dir(job_id)
    try:
        (jdir / "stdout.log").write_text(result.get("stdout") or "")
        (jdir / "stderr.log").write_text(result.get("stderr") or result.get("error") or "")
    except OSError:
        pass
    receipt = script_receipt(job, result)
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["returncode"] = result.get("returncode")
    job["receipt"] = receipt
    job["status"] = "completed" if receipt["status"] == "ok" else "failed"
    if receipt["status"] != "ok":
        job["error"] = receipt.get("summary")
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def named_cmd_run(
    kind: str,
    catalog: dict[str, dict[str, Any]],
    body: bytes,
    catalog_name: str,
    unknown_code: str,
) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name is required", "code": "missing_name"}
    name = name.strip()
    if data.get("argv") is not None:
        return 400, {"error": f"argv is not accepted; use a {catalog_name} name", "code": "argv_forbidden"}
    spec = catalog.get(name)
    if spec is None:
        return 400, {"error": f"unknown {kind} '{name}'", "code": unknown_code, "allowed": sorted(catalog)}
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err
    timeout = int(spec["timeout_seconds"])
    async_run = bool(spec["async"]) or timeout > SCRIPT_SYNC_TIMEOUT
    if not async_run:
        result = run_cmd(list(spec["argv"]), cwd=spec["cwd"], timeout=timeout)
        if result.get("code") == "missing_binary":
            return 501, {"error": result.get("error"), "code": "not_implemented"}
        if result.get("code") == "timeout":
            return 504, {
                "ok": False,
                "name": name,
                "kind": kind,
                "error": result.get("error"),
                "code": "timeout",
                "stdout": result.get("stdout") or "",
                "stderr": result.get("stderr") or "",
                "truncated": bool(result.get("truncated")),
                "compact": compact_cmd_signal(result),
            }
        return 200, {
            "ok": bool(result.get("ok")),
            "name": name,
            "kind": kind,
            "returncode": result.get("returncode"),
            "stdout": result.get("stdout") or "",
            "stderr": result.get("stderr") or "",
            "truncated": bool(result.get("truncated")),
            "compact": compact_cmd_signal(result),
        }
    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        log_ref = str(job_dir(job_id) / "stdout.log")
        job = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "script": name,
            "argv": list(spec["argv"]),
            "cwd": str(spec["cwd"]),
            "timeout_seconds": timeout,
            "response_webhook_url": webhook_url,
            "response_webhook_token": webhook_token,
            "resume_url": webhook_url,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "receipt": None,
            "log_ref": log_ref,
            "error": None,
            "webhook": None,
        }
        job_dir(job_id).mkdir(parents=True, exist_ok=True)
        write_job(job)
    enqueue_script(job)
    return 202, {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "kind": kind,
        "name": name,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def script_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return named_cmd_run("script", load_scripts(repos), body, "scripts.json", "unknown_script")


def test_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return named_cmd_run("test", load_tests(repos), body, "tests.json", "unknown_test")


def rr_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    catalog = load_rr(repos)
    if not catalog:
        return 501, {"error": "rr is not configured", "code": "not_configured"}
    return named_cmd_run("rr", catalog, body, "rr.json", "unknown_rr")


def agent_run(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    profile = str(data.get("profile") or HOME_PROFILE).strip() or HOME_PROFILE
    if profile in ("home", "default"):
        profile = HOME_PROFILE
    seats = {p["id"]: p for p in load_profiles()}
    if profile not in seats:
        return 400, {
            "error": f"unknown profile '{profile}'",
            "code": "unknown_profile",
            "allowed": [p["id"] for p in load_profiles()],
        }
    seat = seats[profile]
    if not seat.get("runnable"):
        return 400, {"error": f"profile '{profile}' is not runnable", "code": "not_runnable"}
    hermes_home = Path(str(seat.get("hermes_home") or HOME)).resolve()
    try:
        hermes_home.relative_to(HERMES_PROFILES_ROOT.resolve())
        under_hermes_profiles = True
    except ValueError:
        under_hermes_profiles = False
    if under_hermes_profiles or profile != HOME_PROFILE or hermes_home != HOME.resolve():
        return 400, {"error": f"profile '{profile}' cannot be executed", "code": "not_runnable"}
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 400, {"error": "prompt is required", "code": "missing_prompt"}
    prompt = prompt.strip()
    if len(prompt) > PROMPT_MAX_CHARS:
        return 400, {"error": "prompt too large", "code": "prompt_too_large"}
    cwd_raw = data.get("cwd")
    if cwd_raw is not None and not isinstance(cwd_raw, str):
        return 400, {"error": "cwd must be a repo short name", "code": "bad_cwd"}
    repo_name, resolved = resolve_run_cwd(cwd_raw, repos)
    if repo_name is None:
        return 400, resolved
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err
    session, session_err = parse_hermes_session(data)
    if session_err:
        return 400, session_err
    launcher = str(data.get("launcher") or "hermes").strip()
    if launcher not in {"hermes", "claude"}:
        return 400, {"error": "launcher must be hermes or claude", "code": "bad_launcher"}
    turns, turns_err = parse_optional_int(data, "max_turns", 1, 80)
    if turns_err:
        return 400, turns_err
    budget, budget_err = parse_optional_int(data, "run_budget_seconds", 30, 1200)
    if budget_err:
        return 400, budget_err
    model = data.get("model")
    if model in ("", None):
        model = None
    elif not isinstance(model, str) or not model.strip() or len(model) > 80:
        return 400, {"error": "model is invalid", "code": "bad_model"}
    else:
        model = model.strip()
    if launcher == "hermes" and not HERMES_BIN.is_file():
        return 501, {"error": f"hermes binary missing: {HERMES_BIN}", "code": "not_implemented"}
    if launcher == "claude" and not CLAUDE_BIN.is_file():
        return 501, {"error": f"claude binary missing: {CLAUDE_BIN}", "code": "not_implemented"}
    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        log_ref = str(job_dir(job_id) / "stdout.log")
        hermes_session = session or f"gwslice-{job_id}"
        job = {
            "job_id": job_id,
            "status": "queued",
            "profile": profile,
            "repo": repo_name,
            "cwd": str(resolved),
            "prompt": prompt,
            "hermes_session": hermes_session,
            "launcher": launcher,
            "model": model,
            "max_turns": turns or AGENT_MAX_TURNS,
            "run_budget_seconds": budget or AGENT_RUN_BUDGET,
            "response_webhook_url": webhook_url,
            "response_webhook_token": webhook_token,
            "resume_url": webhook_url,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "receipt": None,
            "log_ref": log_ref,
            "error": None,
            "webhook": None,
        }
        job_dir(job_id).mkdir(parents=True, exist_ok=True)
        (job_dir(job_id) / "prompt.txt").write_text(wrap_prompt(job_id, prompt, str(resolved)))
        write_job(job)
    enqueue_agent(job)
    return 202, {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "profile": profile,
        "repo": repo_name,
        "hermes_session": hermes_session,
        "launcher": launcher,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def json_out(status: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    return status, payload, "application/json"


def repo_or_error(qs: dict[str, list[str]], repos: dict[str, str]):
    name, resolved = resolve_repo(first_query(qs, "repo"), repos)
    if name is None:
        return None, json_out(400, resolved)
    return (name, resolved), None


def dispatch_inner(
    method: str,
    path: str,
    qs: dict[str, list[str]],
    authed: bool,
    body: bytes,
    headers: Any = None,
) -> tuple[int, dict[str, Any] | bytes, str]:
    if method == "POST" and path == "/v1/doorbell/pr-drive":
        status, payload = doorbell_pr_drive(body, headers)
        return json_out(status, payload)

    repos = load_repos()
    if method == "GET" and path in ("/v1/health", "/health"):
        return json_out(200, {"ok": True, "service": "graphwing", "repos": sorted(repos)})
    if method == "GET" and path in ("/openapi.json", "/v1/openapi.json"):
        return 200, openapi_bytes(), "application/json"

    if not authed:
        return json_out(401, {"error": "invalid or missing X-Graphwing-Key", "code": "unauthorized"})

    if method == "GET" and path == "/v1/units/status":
        out = units_status()
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)

    if method == "GET" and path == "/v1/herdr/agents":
        out = herdr_agents()
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)

    if method == "GET" and path == "/v1/stack/status":
        out = stack_status(first_query(qs, "stack"))
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)
    if method == "GET" and path == "/v1/port/check":
        out = port_check(first_query(qs, "port") or first_query(qs, "ports"))
        return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)
    if method == "POST" and path == "/v1/test/run":
        status, payload = test_run(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/rr/run":
        status, payload = rr_run(body, repos)
        return json_out(status, payload)

    if method == "POST" and path == "/v1/git/checkout":
        status, payload = git_checkout(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/git/restore":
        status, payload = git_restore(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/git/commit":
        status, payload = git_commit(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/git/push":
        status, payload = git_push(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/script/run":
        status, payload = script_run(body, repos)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/slice/frontier":
        status, payload = slice_frontier_op(qs, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/complete":
        status, payload = slice_complete(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/continue":
        status, payload = slice_continue(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/rewst/fire":
        status, payload = rewst_fire(body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/route":
        status, payload = slice_route(body)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/review/run":
        status, payload = review_run(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/slice/e2e":
        status, payload = slice_e2e(body, repos)
        return json_out(status, payload)

    if path.startswith("/v1/git/") or path.startswith("/v1/gh/") or path == "/v1/file/head":
        got, err = repo_or_error(qs, repos)
        if err:
            return err
        repo_name, repo_path = got
        if method == "GET" and path == "/v1/git/status":
            out = git_status(repo_path)
        elif method == "GET" and path == "/v1/git/diff":
            out = git_diff(repo_path, first_query(qs, "ref"), query_rel(qs))
        elif method == "GET" and path == "/v1/git/log":
            try:
                n = int(first_query(qs, "n") or "20")
            except ValueError:
                return json_out(400, {"error": "n must be an integer", "code": "bad_n"})
            out = git_log(repo_path, max(1, min(n, 100)), first_query(qs, "ref"))
        elif method == "GET" and path == "/v1/git/branch":
            out = git_branch(repo_path)
        elif method == "GET" and path == "/v1/git/show":
            rev = first_query(qs, "rev")
            if not rev:
                return json_out(400, {"error": "rev is required", "code": "missing_rev"})
            out = git_show(repo_path, rev)
        elif method == "GET" and path == "/v1/git/rev-parse":
            out = git_rev_parse(repo_path, first_query(qs, "rev") or "HEAD")
        elif method == "GET" and path == "/v1/git/worktrees":
            out = git_worktree_list(repo_path)
        elif method == "GET" and path == "/v1/file/head":
            rel = query_rel(qs)
            if not rel:
                return json_out(400, {"error": "rel is required", "code": "missing_rel"})
            out = file_head(repo_path, rel)
        elif method == "GET" and path == "/v1/gh/pr/list":
            try:
                n = int(first_query(qs, "n") or "10")
            except ValueError:
                return json_out(400, {"error": "n must be an integer", "code": "bad_n"})
            n = max(1, min(n, 50))
            state = first_query(qs, "state") or "open"
            out = gh_json(repo_path, ["pr", "list", "--limit", str(n), "--state", state, "--json", "number,title,state,url,headRefName,baseRefName"])
        elif method == "GET" and path == "/v1/gh/pr/view":
            number = first_query(qs, "number")
            if not number:
                return json_out(400, {"error": "number is required", "code": "missing_number"})
            out = annotate_pr_view(
                gh_json(
                    repo_path,
                    [
                        "pr",
                        "view",
                        number,
                        "--json",
                        "number,title,state,url,headRefName,baseRefName,mergeable,isDraft,reviewDecision,mergeStateStatus",
                    ],
                )
            )
        elif method == "GET" and path == "/v1/gh/pr/findings":
            number = first_query(qs, "number")
            if not number:
                return json_out(400, {"error": "number is required", "code": "missing_number"})
            raw = gh_json(repo_path, ["pr", "view", number, "--json", "labels,comments"])
            if not raw.get("ok"):
                out = raw
            else:
                d = raw.get("data") or {}
                out = pr_findings_from(
                    labels=[l.get("name", "") for l in (d.get("labels") or [])],
                    comment_bodies=[c.get("body", "") for c in (d.get("comments") or [])],
                )
                out = {"ok": out.get("ok"), "data": out, "status": 200 if out.get("ok") else 422}
        elif method == "GET" and path == "/v1/gh/pr/checks":
            number = first_query(qs, "number")
            if not number:
                return json_out(400, {"error": "number is required", "code": "missing_number"})
            out = annotate_pr_checks(
                gh_json(repo_path, ["pr", "checks", number, "--json", "name,state,bucket,link"])
            )
        else:
            out = None
        if out is not None:
            out["repo"] = repo_name
            return json_out(200 if out.get("ok") else int(out.get("status", 400)), out)

    if method == "GET" and path == "/v1/agent/profiles":
        return json_out(200, list_agent_profiles())
    if method == "POST" and path == "/v1/agent/run":
        status, payload = agent_run(body, repos)
        return json_out(status, payload)
    if method == "GET" and path.startswith("/v1/agent/jobs/"):
        job_id = path.removeprefix("/v1/agent/jobs/").strip("/")
        if not JOB_ID_RE.fullmatch(job_id):
            return json_out(400, {"error": "invalid job_id", "code": "bad_job_id"})
        job = read_job(job_id)
        if not job:
            return json_out(404, {"error": "job not found", "code": "not_found"})
        return json_out(200, public_job(job))

    return json_out(404, {"error": "not found", "code": "not_found"})


def dispatch(
    method: str,
    path: str,
    qs: dict[str, list[str]],
    authed: bool,
    body: bytes,
    headers: Any = None,
) -> tuple[int, dict[str, Any] | bytes, str]:
    status, payload, ctype = dispatch_inner(method, path, qs, authed, body, headers)
    herdr_announce(method, path, status, payload)
    return status, payload, ctype


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"graphwing {self.address_string()} {fmt % args}", file=__import__("sys").stderr)

    def _authed(self) -> bool:
        got = (self.headers.get("X-Graphwing-Key") or "").encode()
        try:
            expected = load_key()
        except Exception:
            return False
        return hmac.compare_digest(got, expected)

    def _send(self, status: int, payload: dict[str, Any] | bytes, content_type: str) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        status, payload, ctype = dispatch(
            "GET", parsed.path, parse_qs(parsed.query), self._authed(), b"", self.headers
        )
        self._send(status, payload, ctype)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        if length > 1_000_000:
            self._send(413, {"error": "body too large", "code": "too_large"}, "application/json")
            return
        body = self.rfile.read(length) if length else b""
        status, payload, ctype = dispatch(
            "POST", parsed.path, parse_qs(parsed.query), self._authed(), body, self.headers
        )
        self._send(status, payload, ctype)


def main() -> None:
    try:
        load_key()
    except Exception as exc:
        raise SystemExit(f"API key missing: set GRAPHWING_KEY or write {KEY_PATH}") from exc
    load_repos()
    load_profiles()
    load_scripts()
    load_tests()
    if RR_PATH.is_file():
        load_rr()
    if STACKS_PATH.is_file():
        load_stacks()
    if not OPENAPI_PATH.is_file():
        raise SystemExit(f"missing {OPENAPI_PATH}")
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"graphwing listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
