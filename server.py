#!/usr/bin/env python3
"""graphwing OpenAPI server — deterministic laptop nodes + one-job agent loops."""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import threading
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
UNITS = ("graphwing-api", "graphwing-tunnel", "graphwing-herdr")
HERDR_SESSION = os.environ.get("GRAPHWING_HERDR_SESSION", "graphwing") or "graphwing"
HERDR_AGENT = os.environ.get("GRAPHWING_HERDR_AGENT", "graphwing") or "graphwing"
HERDR_GRAPH_LABEL = "graph"
HERDR_SOURCE = "graphwing"
HERDR_JOB_TAB_MAX = 8
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
RR_BIN = resolve_executable("rr", "GRAPHWING_RR_BIN", Path.home() / "go" / "bin" / "rr")
HERMES_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"
AGENT_MAX_TURNS = 30
AGENT_RUN_BUDGET = 300
AGENT_MAX_CONCURRENT = 3
SCRIPT_SYNC_TIMEOUT = 25
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REF_NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
JOB_LOCK = threading.Lock()


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
    data = json.loads(REPOS_PATH.read_text())
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"repos.json must be a non-empty object: {REPOS_PATH}")
    return {str(k): str(v) for k, v in data.items()}


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
        cwd = resolve_under_home(item.get("cwd"))
        if str(cwd) not in allowed_cwds:
            raise RuntimeError(f"{name} cwd is not allowlisted: {cwd}")
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


def default_repo_name(repos: dict[str, str]) -> str:
    preferred = os.environ.get("GRAPHWING_DEFAULT_REPO", "").strip()
    if preferred and preferred in repos:
        return preferred
    if "riftwing" in repos:
        return "riftwing"
    return next(iter(repos))


def resolve_repo(name: str | None, repos: dict[str, str]) -> tuple[str, Path] | tuple[None, dict[str, Any]]:
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
        out = run_git(resolved, ["checkout", "-b", branch])
    else:
        verify = run_git(resolved, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if not verify.get("ok"):
            return 400, {"error": f"unknown branch '{branch}'", "code": "unknown_branch", "repo": name}
        out = run_git(resolved, ["checkout", branch])
    return git_write_result(name, out)


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
    if add is not None:
        if not isinstance(add, list) or not all(isinstance(x, str) for x in add):
            return 400, {"error": "add must be a list of relative paths", "code": "bad_add"}
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


def herdr_prune_job_tabs() -> None:
    listed = herdr_cli(["tab", "list"]) or {}
    tabs = [t for t in (listed.get("tabs") or []) if isinstance(t, dict) and str(t.get("label") or "").startswith("gw-")]
    extra = len(tabs) - HERDR_JOB_TAB_MAX
    if extra <= 0:
        return
    for tab in tabs[:extra]:
        tid = tab.get("tab_id")
        if tid:
            herdr_cli(["tab", "close", str(tid)])


def herdr_follow_job(job: dict[str, Any]) -> None:
    if not herdr_enabled():
        return
    job_id = str(job.get("job_id") or "")
    if not JOB_ID_RE.fullmatch(job_id):
        return
    kind = {"script": "s", "test": "t", "rr": "r"}.get(str(job.get("kind") or ""), "s" if job.get("script") else "a")
    label = f"gw-{kind}-{job_id[:8]}"
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


def herdr_job_done(job: dict[str, Any]) -> None:
    if not herdr_enabled():
        return
    receipt = job.get("receipt") if isinstance(job.get("receipt"), dict) else {}
    summary = str(receipt.get("summary") or job.get("status") or "done")[:80]
    kind = job.get("kind") or job.get("script") or "agent"
    herdr_log(f"done {kind} {str(job.get('job_id') or '')[:8]} {job.get('status')} {summary}")
    pane_id = job.get("herdr_pane_id")
    if pane_id:
        tag = {"script": "s", "test": "t", "rr": "r"}.get(str(job.get("kind") or ""), "s" if job.get("script") else "a")
        label = f"gw-{tag}-{str(job.get('job_id') or '')[:8]}"
        herdr_report(str(pane_id), label, "idle", summary)


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


def wrap_prompt(job_id: str, prompt: str, cwd: str) -> str:
    root = str(Path(cwd).resolve())
    return (
        f"GRAPHWING_JOB {job_id}\n"
        "You are a Graph node on this laptop, not the story owner. One job only. "
        "Do not research+write+review+ship.\n"
        f"Working directory is {root}. Create and edit files only inside that directory. "
        "Do not write to any other path.\n"
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
    proc, err = spawn_hermes(job)
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
            }
        return 200, {
            "ok": bool(result.get("ok")),
            "name": name,
            "kind": kind,
            "returncode": result.get("returncode"),
            "stdout": result.get("stdout") or "",
            "stderr": result.get("stderr") or "",
            "truncated": bool(result.get("truncated")),
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
    if not HERMES_BIN.is_file():
        return 501, {"error": f"hermes binary missing: {HERMES_BIN}", "code": "not_implemented"}
    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        log_ref = str(job_dir(job_id) / "stdout.log")
        job = {
            "job_id": job_id,
            "status": "queued",
            "profile": profile,
            "repo": repo_name,
            "cwd": str(resolved),
            "prompt": prompt,
            "response_webhook_url": webhook_url,
            "response_webhook_token": webhook_token,
            "resume_url": webhook_url,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "max_turns": AGENT_MAX_TURNS,
            "run_budget_seconds": AGENT_RUN_BUDGET,
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
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def json_out(status: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    return status, payload, "application/json"


def repo_or_error(qs: dict[str, list[str]], repos: dict[str, str]):
    name, resolved = resolve_repo(first_query(qs, "repo"), repos)
    if name is None:
        return None, json_out(400, resolved)
    return (name, resolved), None


def dispatch_inner(method: str, path: str, qs: dict[str, list[str]], authed: bool, body: bytes) -> tuple[int, dict[str, Any] | bytes, str]:
    repos = load_repos()
    if method == "GET" and path in ("/v1/health", "/health"):
        return json_out(200, {"ok": True, "service": "graphwing", "repos": sorted(repos)})
    if method == "GET" and path in ("/openapi.json", "/v1/openapi.json"):
        return 200, OPENAPI_PATH.read_bytes(), "application/json"

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
    if method == "POST" and path == "/v1/git/commit":
        status, payload = git_commit(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/git/push":
        status, payload = git_push(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/script/run":
        status, payload = script_run(body, repos)
        return json_out(status, payload)

    if path.startswith("/v1/git/") or path.startswith("/v1/gh/") or path == "/v1/file/head":
        got, err = repo_or_error(qs, repos)
        if err:
            return err
        repo_name, repo_path = got
        if method == "GET" and path == "/v1/git/status":
            out = git_status(repo_path)
        elif method == "GET" and path == "/v1/git/diff":
            out = git_diff(repo_path, first_query(qs, "ref"), first_query(qs, "path"))
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
            rel = first_query(qs, "path")
            if not rel:
                return json_out(400, {"error": "path is required", "code": "missing_path"})
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
            out = gh_json(repo_path, ["pr", "view", number, "--json", "number,title,state,url,headRefName,baseRefName,mergeable,isDraft,body"])
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


def dispatch(method: str, path: str, qs: dict[str, list[str]], authed: bool, body: bytes) -> tuple[int, dict[str, Any] | bytes, str]:
    status, payload, ctype = dispatch_inner(method, path, qs, authed, body)
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
        status, payload, ctype = dispatch("GET", parsed.path, parse_qs(parsed.query), self._authed(), b"")
        self._send(status, payload, ctype)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        if length > 1_000_000:
            self._send(413, {"error": "body too large", "code": "too_large"}, "application/json")
            return
        body = self.rfile.read(length) if length else b""
        status, payload, ctype = dispatch("POST", parsed.path, parse_qs(parsed.query), self._authed(), body)
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
