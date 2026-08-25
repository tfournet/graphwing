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
import tempfile
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
BUILDS_DIR = HOME / "builds"
RUNS_PATH = HOME / "workflow-runs.jsonl"
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
SLICE_TICKET_CORE_KEYS = frozenset({"id", "path", "kind", "status", "blocked_by"})
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

# Pre-PR build state. Bumping BUILD_STATE_VERSION is a promise to migrate:
# a document this service cannot read parks instead of being rewritten.
BUILD_STATE_VERSION = 1
BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
BUILD_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BUILD_HEAD_RE = re.compile(r"^[0-9a-f]{7,64}$")
BUILD_PARK_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# One linear pre-PR path. `evidence` is skipped when the build's declared
# verification says no visual proof is needed, so the writer never has to
# invent a screenshot round to reach review.
BUILD_TRANSITIONS: dict[str, tuple[str, str]] = {
    "start_writer": ("created", "writing"),
    "writer_done": ("writing", "verifying"),
    "verify_passed": ("verifying", "evidence"),
    "evidence_captured": ("evidence", "review"),
    "review_passed": ("review", "ready"),
    "pr_opened": ("ready", "pr_opened"),
}
BUILD_TERMINAL = frozenset({"pr_opened", "parked"})
BUILD_LEASE_MAX_SECONDS = 3600


def build_default_lease_seconds(raw: str | None) -> int:
    """Clamp the environment default into the same range a request must satisfy.

    A request asking for 0 or 100000 seconds is a 400. An environment that
    says the same thing must not become a lease that never expires (locking
    every future caller out) or one that expires instantly (letting two Rewst
    runs drive the same build). Unparseable falls back rather than raising:
    a typo in a unit file should not stop the service from booting.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 300
    return max(1, min(value, BUILD_LEASE_MAX_SECONDS))


BUILD_LEASE_SECONDS = build_default_lease_seconds(os.environ.get("GRAPHWING_BUILD_LEASE_SECONDS", "300"))
BUILD_STATE_MAX_BYTES = 1024 * 1024
# Receipts age out; event ids never do. The two bounds are deliberately
# different because forgetting a receipt loses a report, and forgetting an
# event id lets a retry execute a second time.
BUILD_RECEIPTS_KEPT = 64
BUILD_EVENT_HARD_MAX = 2 * 500
BUILD_RECEIPTS_SHOWN = 32
BUILD_LIST_MAX = 32
BUILD_STACKS_MAX = 16
BUILD_VERIFICATION_MAX = 16
BUILD_JOBS_MAX = 500
BUILD_ROUTE_MAX_KEYS = 32
BUILD_NAME_MAX = 256
BUILD_REDACT_MAX_DEPTH = 6
BUILD_REDACT_MAX_KEYS = 64
BUILD_REDACT_MAX_ITEMS = 64
BUILD_REDACT_MAX_STRING = 512
SECRET_KEY_PARTS = frozenset(
    {
        "secret", "secrets", "token", "tokens", "password", "passwd", "pass",
        "apikey", "key", "keys", "authorization", "auth", "cookie", "cookies",
        "credential", "credentials", "creds", "bearer", "signature", "hook",
        "webhook", "pem", "passphrase", "privkey",
    }
)
# Whole-key names that carry a credential without containing a secret-shaped
# word. `kick_url` and `resume_url` are the two Rewst hands back to a build:
# possessing either one is possessing the right to resume that run.
SECRET_KEY_EXACT = frozenset(
    {
        "kickurl", "resumeurl", "callbackurl", "webhookurl", "hookurl",
        "continueurl", "replyurl", "notifyurl",
        "privatekey", "sshkey", "sshprivatekey", "identityfile",
        "awssecretaccesskey", "awsaccesskeyid", "awssessiontoken",
        "clientsecret", "refreshtoken",
    }
)
SECRET_VALUE_RES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"(?i)bearer\s+\S{8,}"),
    # 64+ hex is a digest or a key. A 40-hex git object id must survive, so
    # the floor sits above it deliberately.
    re.compile(r"[0-9a-fA-F]{64,}"),
    # scheme://user:pass@host — the credential is in the URL itself, so the
    # key name it arrived under says nothing about whether to keep it.
    re.compile(r"(?i)[a-z][a-z0-9+.\-]*://[^/\s?#@]*:[^/\s?#@]*@"),
    # A signed or keyed query string. The parameter list is deliberately
    # narrow: an ordinary public URL with utm_source or page must survive.
    re.compile(
        r"(?i)[a-z][a-z0-9+.\-]*://\S*[?&]"
        r"(?:token|api[_-]?key|access[_-]?key|secret|password|passwd|pwd|sig|"
        r"signature|auth|credential|sas|x-amz-signature|code)=[^&\s]"
    ),
    # A capability URL: an ordinary-looking link whose path carries the right
    # to resume, kick, or call back into a run. SECRET_KEY_EXACT only catches
    # the ones that arrive under a name we predicted, and a Rewst graph author
    # can put the same webhook under `next` or `hop` or nothing at all. So the
    # shape is matched by value, whatever key it came in under.
    #
    # The tail is what keeps this off ordinary public URLs: a capability
    # segment only counts when a later segment looks like an opaque id — has a
    # digit, or is long enough to be a token. A docs page at /guides/webhooks
    # or /webhooks/setup survives; /webhooks/custom/<uuid> does not.
    re.compile(
        r"(?i)[a-z][a-z0-9+.\-]*://[^\s/?#]+"
        r"(?:/[^\s/?#]+)*?"
        r"/(?:(?:web)?hooks?|callbacks?|resumes?|kicks?|continue|notify|reply|triggers?)"
        r"(?:/[^\s/?#]+)*"
        r"/(?:[^\s/?#]*[0-9][^\s/?#]*|[^\s/?#]{16,})"
    ),
)
BUILD_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
RUNS_LOCK = threading.Lock()
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
WATCH_RUN_INPUT_KEYS = ("repo", "pr", "ticket", "branch", "index", "test", "class", "size")
WATCH_WORKFLOW_RECENT = 8


def sanitize_run_input(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in WATCH_RUN_INPUT_KEYS:
        val = inp.get(key)
        if val in (None, ""):
            continue
        if isinstance(val, str):
            out[key] = val[:80]
        elif isinstance(val, (int, float, bool)):
            out[key] = val
        else:
            out[key] = str(val)[:80]
    return out


def extract_run_id(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("id", "run_id", "runId"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:80]
    nested = body.get("data")
    if isinstance(nested, dict):
        return extract_run_id(nested)
    return None


def workflow_run_title(workflow: str, inp: dict[str, Any]) -> str:
    ticket = str(inp.get("ticket") or "").strip()
    if ticket:
        name = Path(ticket).name
        if name.endswith(".md"):
            name = name[:-3]
        return f"{workflow} {name}"
    pr = inp.get("pr")
    if pr not in (None, ""):
        return f"{workflow} PR {pr}"
    repo = str(inp.get("repo") or "").strip()
    if repo:
        return f"{workflow} {repo}"
    return workflow


def public_workflow_run(row: dict[str, Any]) -> dict[str, Any]:
    inp = row.get("input") if isinstance(row.get("input"), dict) else {}
    workflow = str(row.get("workflow") or "workflow")
    return {
        "kind": workflow,
        "title": str(row.get("title") or workflow_run_title(workflow, inp)),
        "status": str(row.get("status") or "fired"),
        "repo": inp.get("repo"),
        "tab": "graph",
        "created_at": row.get("created_at"),
        "started_at": row.get("created_at"),
        "finished_at": None,
        "summary": workflow_run_title(workflow, inp),
        "run_id": row.get("run_id"),
        "source": row.get("source"),
        "error": None,
        "job_id": "",
    }


def record_workflow_run(row: dict[str, Any]) -> None:
    payload = {
        "workflow": str(row.get("workflow") or ""),
        "status": str(row.get("status") or "fired"),
        "source": str(row.get("source") or "fire"),
        "input": row.get("input") if isinstance(row.get("input"), dict) else {},
        "run_id": row.get("run_id"),
        "created_at": str(row.get("created_at") or utcnow()),
    }
    payload["title"] = workflow_run_title(payload["workflow"], payload["input"])
    try:
        with RUNS_LOCK:
            RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with RUNS_PATH.open("a") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except OSError:
        return


def list_install_run_stubs(limit: int) -> list[dict[str, Any]]:
    try:
        install = load_rewst_install()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for key in ("implement_slice", "pr_drive", "pr_status", "verify_stack"):
        item = install.get(key)
        if not isinstance(item, dict):
            continue
        if not item.get("run_id"):
            continue
        slug = str(item.get("slug") or key.replace("_", "-"))
        inp: dict[str, Any] = {}
        rows.append(
            public_workflow_run(
                {
                    "workflow": slug,
                    "status": str(item.get("status") or "recorded"),
                    "source": "install",
                    "input": inp,
                    "run_id": item.get("run_id"),
                    "created_at": None,
                }
            )
        )
        if len(rows) >= limit:
            break
    return rows


def list_workflow_runs(limit: int) -> list[dict[str, Any]]:
    if not RUNS_PATH.is_file():
        return list_install_run_stubs(limit)
    try:
        lines = RUNS_PATH.read_text().splitlines()
    except OSError:
        return list_install_run_stubs(limit)
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or not item.get("workflow"):
            continue
        rows.append(public_workflow_run(item))
        if len(rows) >= limit:
            break
    return rows or list_install_run_stubs(limit)


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
    record_workflow_run(
        {
            "workflow": workflow,
            "status": "fired" if out["ok"] else "failed",
            "source": "fire",
            "input": sanitize_run_input(payload),
            "run_id": extract_run_id(out.get("body")),
            "created_at": utcnow(),
        }
    )
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
    if not out.get("ok"):
        blob = ((out.get("stdout") or "") + (out.get("stderr") or "")).lower()
        # An empty commit is still a failed slice, per the lock. It is just a
        # different failure from a broken commit, and saying so is the
        # difference between "the writer had nothing to do" and "the commit
        # is broken" — which read identically in the trace and cost two rounds
        # of misdiagnosis.
        if "nothing to commit" in blob or "no changes added to commit" in blob:
            return 400, {"ok": False, "repo": name, "code": "nothing_staged",
                         "error": "nothing was staged; the writer made no changes",
                         "stdout": out.get("stdout") or "", "stderr": out.get("stderr") or ""}
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
        # Carry every other key through untouched. sliceComplete and sliceE2e
        # rewrite this file, and rebuilding each ticket from the five
        # normalized fields used to drop the planner's routing and
        # verification metadata (class, size, ac_count, seams, build_id) on the
        # first completion — the pre-PR build then had nothing to route on.
        extra = {k: v for k, v in item.items() if k not in SLICE_TICKET_CORE_KEYS}
        tickets.append(
            {
                **extra,
                "id": tid,
                "path": path.strip(),
                "kind": kind,
                "status": status,
                "blocked_by": list(blocked),
            }
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


PR_MAX_ATTEMPTS = int(os.environ.get("GRAPHWING_PR_MAX_ATTEMPTS", "3"))


def pr_continue(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Kick the next pr-drive attempt, or stop at the ceiling.

    pr-drive took one swing at a red PR and ended. Driving to green means
    looping, and Rewst forbids unbounded cycles inside one run, so the next
    attempt is a fresh run kicked at kick_url, the way slice_continue walks
    the slice map.

    The ceiling is not optional. A writer that cannot fix a finding would
    otherwise re-run forever, and each attempt costs a full agent session.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    name, resolved = repo_from_body(data, repos)
    if name is None:
        return 400, resolved
    number = str(data.get("pr") or data.get("number") or "").strip()
    if not number:
        return 400, {"error": "pr is required", "code": "missing_pr"}

    attempt, aerr = parse_optional_int(data, "attempt", 0, 99)
    if aerr:
        return 400, aerr
    attempt = attempt or 1
    ceiling, cerr = parse_optional_int(data, "max_attempts", 1, 99)
    if cerr:
        return 400, cerr
    ceiling = ceiling or PR_MAX_ATTEMPTS

    kick_url = data.get("kick_url")
    if isinstance(kick_url, str) and kick_url.strip() and not valid_webhook_url(kick_url):
        return 400, {"error": "kick_url must be https", "code": "bad_kick_url"}

    out = {"ok": True, "repo": name, "pr": number, "attempt": attempt,
           "max_attempts": ceiling, "kicked": False}
    if attempt >= ceiling:
        return 200, {**out, "code": "attempts_exhausted",
                     "error": f"{attempt} of {ceiling} attempts used"}
    if not kick_url:
        return 200, {**out, "code": "no_kick_url"}

    token = data.get("kick_token") or None
    payload = {
        "repo": name,
        # The next run reads this as CTX.INPUT.pr_number. A plain `pr` key never
        # arrives: the graph declared one and every reference resolved empty, so
        # pr-drive died at ghPrView with no `number` on the query string.
        "pr_number": number,
        "test": data.get("test"),
        "attempt": attempt + 1,
        "max_attempts": ceiling,
        "auto_merge": bool(data.get("auto_merge")),
        "kick_url": kick_url,
        "kick_token": token,
    }
    hook = post_receipt(kick_url, payload, token=token)
    out["kicked"] = bool(hook.get("ok"))
    out["kick"] = hook
    if not out["kicked"]:
        return 502, {**out, "ok": False, "error": "kick failed", "code": "kick_failed"}
    return 200, out


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
        # Every review crosses vendors, and all three vendors are in the loop:
        # xAI writes mechanical and Anthropic grades it; Anthropic writes these
        # and OpenAI grades them. Sol is excluded because it is the planning
        # session, so Terra carries OpenAI here — same vendor, different model,
        # so the spec's author is never in the review chain.
        launcher, model = "claude", "claude-opus-5"
        reviewer1, reviewer2 = "terra", "none"
    else:
        # Sensitive needs two acks, and both from vendors that are not the
        # writer's: OpenAI and xAI grade what Anthropic wrote.
        launcher, model = "claude", "claude-opus-5"
        reviewer1, reviewer2 = "terra", "grok"
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
        "review": "none" if reviewer1 == "none" else (
            f"{reviewer1}_{reviewer2}" if reviewer2 != "none" else reviewer1),
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


def gh_pr_merge(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Merge a PR, but only after reading its state here and re-deciding."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    repo_name = str(data.get("repo") or "").strip()
    repo_path = repos.get(repo_name)
    if not repo_path:
        return 400, {"error": f"unknown repo '{repo_name}'", "code": "unknown_repo",
                     "allowed": sorted(repos)}
    number = str(data.get("number") or data.get("pr") or "").strip()
    if not number:
        return 400, {"error": "number is required", "code": "missing_number"}
    # Not `method`: the connector reads a field of that name as the HTTP verb,
    # so the graph's method: "squash" made Rewst issue `SQUASH /v1/gh/pr/merge`.
    # Same trap as `path` being read as the URL path.
    how = str(data.get("merge_method") or data.get("method") or "squash").strip()
    if how not in {"squash", "merge", "rebase"}:
        return 400, {"error": "method must be squash, merge, or rebase", "code": "bad_method"}

    view = annotate_pr_view(gh_json(repo_path, ["pr", "view", number, "--json",
        "number,title,state,url,headRefName,baseRefName,mergeable,isDraft,reviewDecision,mergeStateStatus,labels"]))
    if not view.get("ok"):
        return int(view.get("status", 400)), view
    vd = view.get("data") or {}
    checks = annotate_pr_checks(gh_json(repo_path, ["pr", "checks", number, "--json",
        "name,state,bucket,link"]))

    state = {
        # annotate_pr_checks sets all_green on the result, not inside data.
        "all_green": bool(checks.get("all_green")),
        "mergeable": vd.get("mergeable"),
        "is_draft": bool(vd.get("isDraft")),
        "holds": sorted(l.get("name", "") for l in (vd.get("labels") or [])
                        if str(l.get("name", "")).startswith("hold:")),
    }
    allowed, why = pr_merge_allowed(
        state,
        auto_merge=bool(data.get("auto_merge")),
        run_id=str(data.get("run_id") or ""),
    )
    if not allowed:
        assert why is not None
        return 409, {"ok": False, "merged": False, "repo": repo_name, "number": number,
                     "state": state, **why}

    out = gh_text(repo_path, ["pr", "merge", number, "--" + how, "--delete-branch"])
    return (200 if out.get("ok") else int(out.get("status", 400))), {
        "ok": bool(out.get("ok")), "merged": bool(out.get("ok")), "repo": repo_name,
        "number": number, "method": how, "state": state,
        "error": out.get("error"), "code": out.get("code"),
    }


def pr_merge_allowed(
    state: dict[str, Any], auto_merge: bool, run_id: str = ""
) -> tuple[bool, dict[str, Any] | None]:
    """Decide whether a run may merge this PR.

    Off unless the run explicitly asked. The operator lock puts merge on the
    engineer; this exists only for the runs where they said auto-merge, and
    absence of that flag is a refusal rather than a default.

    Every condition is re-checked here against freshly read PR state instead
    of trusting whatever the graph concluded upstream. A node deciding "green"
    on its own is exactly what let a queue receipt pass as a test result.
    """
    if not auto_merge:
        return False, {"error": "auto_merge was not requested for this run",
                       "code": "auto_merge_not_requested"}
    if not str(run_id or "").strip():
        # auto_merge alone only stops a graph merging by accident. Any holder
        # of the API key could still set the flag by hand, which is how
        # riftwing#3523 got merged from a shell. Merge belongs to a run.
        return False, {"error": "merge is only reachable from inside a run; run_id is required",
                       "code": "no_run_id"}
    if state.get("is_draft"):
        return False, {"error": "pull request is a draft", "code": "is_draft"}
    if not state.get("all_green"):
        return False, {"error": "checks are not all green", "code": "not_green"}
    if str(state.get("mergeable") or "").upper() != "MERGEABLE":
        return False, {"error": f"pull request is {state.get('mergeable')}", "code": "not_mergeable"}
    holds = state.get("holds") or []
    if holds:
        return False, {"error": f"blocking labels present: {', '.join(holds)}", "code": "held"}
    return True, None


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
        # "Any finding at all" never converges: at grade A the reviewer still
        # lists nits, so a loop gated on that spends writer sessions forever.
        # review-hold.yml clears hold:pm-review at A/A-, so that is the real
        # merge bar. Findings on an A-graded PR stay in the payload as
        # advisory. No grade means the audit has not spoken yet, which is not
        # the same as clear.
        "blocking": bool(holds) or grade is None or grade.rstrip("+-") != "A",
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
    if reviewer in {"sonnet", "opus", "fable"}:
        if not CLAUDE_BIN.is_file():
            return {"ok": False, "verdict": "NACK", "no_verdict": True,
                    "error": f"claude binary missing: {CLAUDE_BIN}", "code": "not_implemented"}
        model = {"sonnet": "sonnet", "opus": "opus", "fable": "claude-fable-5"}[reviewer]
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
        # Name the model. Without -m every hermes reviewer silently takes the
        # seat's default profile, so "grok reviewed it" and "terra reviewed it"
        # would both mean "whatever config.yaml said today".
        model = {"sol": "gpt-5.6-sol", "grok": "grok-4.6", "terra": "gpt-5.6-terra"}[reviewer]
        cmd = [
            str(HERMES_BIN),
            "chat",
            "-Q",
            "-m",
            model,
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
    if reviewer not in {"sonnet", "opus", "fable", "sol", "grok", "terra"}:
        return 400, {"error": "reviewer must be sonnet, opus, fable, sol, grok, or terra",
                     "code": "bad_reviewer"}
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


def gh_text(path: Path, args: list[str]) -> dict[str, Any]:
    """Run gh where the output is prose. `gh pr merge` prints a sentence, and
    routing that through gh_json reported a successful merge as a failure."""
    return run_cmd(["gh", *args], cwd=path)


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
_ANNOUNCE_SKIP = {
    "/v1/health",
    "/health",
    "/openapi.json",
    "/v1/openapi.json",
    "/v1/herdr/agents",
    "/v1/units/status",
    "/v1/watch",
}


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


WATCH_RECENT = 8
WATCH_TITLE_CHARS = 72
WATCH_ERROR_CHARS = 120
WATCH_ACTIVE = frozenset({"queued", "running"})
WATCH_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")
WATCH_TICKET_SLUG_RE = re.compile(r"^\d{2,}[-_][A-Za-z0-9._-]+")
WATCH_SLICE_PATH_RE = re.compile(r"(?:^|\s)((?:slices|tickets)/[A-Za-z0-9._/-]+\.md)\b")


def clip_text(text: Any, n: int) -> str | None:
    if text is None:
        return None
    first = str(text).strip().splitlines()[0].strip() if str(text).strip() else ""
    if not first:
        return None
    if len(first) > n:
        return first[: n - 1] + "…"
    return first


def prompt_title(prompt: Any) -> str | None:
    text = str(prompt or "").strip()
    if not text:
        return None
    path = WATCH_SLICE_PATH_RE.search(text)
    if path:
        return Path(path.group(1)).stem
    first = text.splitlines()[0].strip()
    heading = WATCH_HEADING_RE.match(first)
    if heading:
        rest = heading.group(1).strip()
        if ":" in rest:
            slug = rest.split(":", 1)[0].strip()
            if WATCH_TICKET_SLUG_RE.match(slug) or "-" in slug:
                return clip_text(slug, WATCH_TITLE_CHARS)
        return clip_text(rest, WATCH_TITLE_CHARS)
    return clip_text(first, WATCH_TITLE_CHARS)


def job_title(job: dict[str, Any]) -> str:
    kind = str(job.get("kind") or "agent")
    if kind in ("script", "test", "rr"):
        return str(job.get("script") or kind)
    if kind == "review":
        who = str(job.get("reviewer") or "").strip()
        return f"review {who}".strip() if who else "review"
    ticket = str(job.get("ticket") or "").strip()
    if ticket:
        name = Path(ticket).name
        if name.endswith(".md"):
            name = name[:-3]
        return clip_text(name, WATCH_TITLE_CHARS) or kind
    pr = job.get("pr")
    if pr not in (None, ""):
        return f"PR {pr}"
    return prompt_title(job.get("prompt")) or kind


def watch_job(job: dict[str, Any]) -> dict[str, Any]:
    tab_id = job.get("herdr_tab_id")
    return {
        "job_id": str(job.get("job_id") or ""),
        "status": str(job.get("status") or ""),
        "kind": str(job.get("kind") or "agent"),
        "title": job_title(job),
        "repo": job.get("repo"),
        "tab": herdr_job_tab_label(job),
        "herdr_tab_id": str(tab_id) if tab_id else None,
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": clip_text(job.get("error"), WATCH_ERROR_CHARS),
        "summary": clip_text(
            (job.get("receipt") or {}).get("summary") if isinstance(job.get("receipt"), dict) else None,
            80,
        ),
    }


def watch_snapshot() -> dict[str, Any]:
    """Compact panel payload: units plus in-flight and recent jobs. No secrets."""
    units = units_status()
    active: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    if JOBS_DIR.is_dir():
        for path in JOBS_DIR.glob("*/job.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            job_id = str(data.get("job_id") or "")
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            row = watch_job(data)
            if row["status"] in WATCH_ACTIVE:
                active.append(row)
            else:
                finished.append(row)
    active.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    finished.sort(key=lambda j: str(j.get("finished_at") or j.get("created_at") or ""), reverse=True)
    recent = finished[:WATCH_RECENT]
    queued = sum(1 for j in active if j["status"] == "queued")
    running = sum(1 for j in active if j["status"] == "running")
    failed_recent = sum(1 for j in recent if j["status"] == "failed")
    api = (units.get("units") or {}).get("graphwing-api") or {}
    api_active = bool(api.get("active"))
    return {
        "ok": True,
        "service": "graphwing",
        "units": units.get("units") or {},
        # herdr and the tunnel are optional. The bar's traffic light is the API.
        "units_healthy": api_active,
        "api_active": api_active,
        "counts": {
            "queued": queued,
            "running": running,
            "active": len(active),
            "failed_recent": failed_recent,
        },
        "active": active,
        "recent": recent,
        "workflows": {"recent": list_workflow_runs(WATCH_WORKFLOW_RECENT)},
    }


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
    record_workflow_run(
        {
            "workflow": "pr-drive",
            "status": "fired",
            "source": "doorbell",
            "input": sanitize_run_input(hook_payload),
            "created_at": utcnow(),
        }
    )
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


# --- pre-PR build state -----------------------------------------------------
#
# graphwing-pre-pr-build is many bounded Rewst invocations, not one run, so the
# build lives on disk under $GRAPHWING_HOME and every call is a transition
# against it. Three rules make the retries safe:
#
#   1. An event id names one requested transition. Replaying it returns the
#      recorded receipt; reusing it for different input is a conflict.
#   2. A lease names the caller that currently owns the build, so two Rewst
#      runs cannot advance the same build past each other.
#   3. Anything the state cannot explain — a moved head, a different worktree,
#      a state version this build does not understand — parks with a named
#      reason. Guessing here would commit real side effects twice.


def build_dir(build_id: str) -> Path:
    return BUILDS_DIR / build_id


def build_path(build_id: str) -> Path:
    return build_dir(build_id) / "build.json"


def valid_build_id(raw: Any) -> bool:
    return isinstance(raw, str) and ".." not in raw and bool(BUILD_ID_RE.fullmatch(raw))


def secret_shaped_key(key: str) -> bool:
    """True when a key name promises a credential.

    Two passes. Segment match, not substring, catches the ordinary names:
    `author` is not `auth`, and dropping the author of a review receipt would
    be a silent data loss bug. Whole-key match catches the ones whose parts
    are all innocent — `kick_url` is `kick` plus `url`, and it is a bearer
    capability to resume a Rewst run.
    """
    lowered = key.lower()
    if re.sub(r"[^a-z0-9]+", "", lowered) in SECRET_KEY_EXACT:
        return True
    return any(part in SECRET_KEY_PARTS for part in re.split(r"[^a-z0-9]+", lowered) if part)


def secret_shaped_value(value: str) -> bool:
    """Search, not fullmatch: a token pasted into a sentence is still a token."""
    return any(pat.search(value) for pat in SECRET_VALUE_RES)


def reject_secret_text(value: str, field: str) -> dict[str, Any] | None:
    """Refuse a credential in an identity field instead of redacting it.

    `story` and `holder` are not blobs — they are compared and displayed.
    Redacting them would be worse than storing them: two different callers
    whose ids both redacted to `[redacted]` would compare equal, and the
    second one would inherit the first one's lease.
    """
    if secret_shaped_value(value):
        return {"error": f"{field} looks like a credential", "code": f"secret_in_{field}"}
    return None


def redact_secrets(value: Any, depth: int = 0) -> Any:
    """Bound and redact a caller-supplied blob before it reaches disk.

    Bounding is part of redaction, not a separate concern: an unbounded blob
    is how a whole environment dump ends up in build.json.
    """
    if depth >= BUILD_REDACT_MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in list(value)[:BUILD_REDACT_MAX_KEYS]:
            name = str(key)[:BUILD_REDACT_MAX_STRING]
            out[name] = "[redacted]" if secret_shaped_key(name) else redact_secrets(value[key], depth + 1)
        if len(value) > BUILD_REDACT_MAX_KEYS:
            out["[truncated]"] = len(value) - BUILD_REDACT_MAX_KEYS
        return out
    if isinstance(value, list):
        return [redact_secrets(v, depth + 1) for v in value[:BUILD_REDACT_MAX_ITEMS]]
    if isinstance(value, str):
        if secret_shaped_value(value):
            return "[redacted]"
        return value[:BUILD_REDACT_MAX_STRING]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:BUILD_REDACT_MAX_STRING]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def event_fingerprint(payload: dict[str, Any]) -> str:
    """Digest the raw request, never the redacted copy.

    Two calls that differ only in a token value are different requests. If the
    fingerprint were taken after redaction they would collide and the second
    one would silently replay the first one's receipt.
    """
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def load_build(build_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read a build, telling missing apart from unreadable.

    Returns (doc, None) when the build is there, (None, None) when no build
    with that id has ever been written, and (None, error) when the file
    exists but this service cannot make sense of it. The third case is the
    one that matters: collapsing it into "missing" is how a truncated
    build.json becomes a brand new build, and the real one — its lease, its
    receipts, its record of which event ids have already run — is gone.
    """
    path = build_path(build_id)
    if not path.exists():
        return None, None
    unreadable = {
        "ok": False,
        "build_id": build_id,
        "stage": "parked",
        "park_reason": "state_unreadable",
        "error": "build state exists but cannot be read; it has been left untouched",
        "code": "state_unreadable",
        "status": 409,
    }
    if not path.is_file():
        return None, unreadable
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, dict(unreadable, detail=str(exc)[:BUILD_REDACT_MAX_STRING])
    if not isinstance(data, dict):
        return None, dict(unreadable, detail="state document is not an object")
    if data.get("build_id") != build_id:
        # The document under this id claims to be a different build. Writing
        # to it would corrupt whichever one is real.
        return None, dict(unreadable, detail="state document names a different build")
    return data, None


def read_build(build_id: str) -> dict[str, Any] | None:
    doc, _err = load_build(build_id)
    return doc


def build_event_seen(doc: dict[str, Any]) -> dict[str, Any]:
    """Every event id this build has ever accepted, mapped to its outcome.

    Separate from `events` because the receipt window ages out and this must
    not. Two different losses hide behind one name here. An id that fell out
    of the map is an id a retry could execute a second time. An id that is
    still in the map but whose outcome fell out is one the retry gets a shrug
    for: 200 with a null receipt, when the caller is asking about a park that
    returned 409. The story promises the recorded receipt, so the outcome is
    kept alongside the fingerprint for every accepted id through
    BUILD_EVENT_HARD_MAX. `events` stays the recent window the state
    endpoint reports.

    Each value is `{fingerprint, status, receipt}`. Documents written before
    that shape existed hold a bare fingerprint string; those are lifted here,
    and their receipt is recovered from `events` when it is still in range.
    """
    seen = doc.get("event_seen")
    if isinstance(seen, dict) and all(isinstance(v, dict) for v in seen.values()):
        return seen
    if not isinstance(seen, dict):
        seen = {
            eid: rec.get("fingerprint")
            for eid, rec in (doc.get("events") or {}).items()
            if isinstance(rec, dict)
        }
    events = doc.get("events") or {}
    migrated: dict[str, Any] = {}
    for eid, entry in seen.items():
        if isinstance(entry, dict):
            record = {k: entry.get(k) for k in ("fingerprint", "status", "receipt")}
        else:
            # Legacy: the entry was the fingerprint and nothing else. A
            # non-string here (an older migration could leave None) becomes a
            # fingerprint that matches nothing, so the id is remembered as
            # spent and any retry is a conflict rather than a second run.
            record = {
                "fingerprint": entry if isinstance(entry, str) else None,
                "status": None,
                "receipt": None,
            }
        if record["receipt"] is None:
            prior = events.get(eid)
            if isinstance(prior, dict) and isinstance(prior.get("receipt"), dict):
                record["receipt"] = prior.get("receipt")
                record["status"] = prior.get("status")
        migrated[eid] = record
    doc["event_seen"] = migrated
    return migrated


def write_build(doc: dict[str, Any], expected_id: str | None = None) -> dict[str, Any] | None:
    """Atomic, private, bounded write. Returns an error dict instead of raising.

    Private because a build document carries the worktree path, the writer
    session handle, and the review record for unshipped work; 0644 under the
    seat umask puts all of that in front of every local account.
    """
    build_id = doc.get("build_id")
    if not valid_build_id(build_id):
        return {
            "ok": False,
            "error": "refusing to write a build document without a valid build_id",
            "code": "bad_build_id",
            "status": 500,
        }
    if expected_id is not None and build_id != expected_id:
        # The document and the request disagree about which build this is.
        # One of them is wrong and writing either way clobbers a real build.
        return {
            "ok": False,
            "error": "build document does not match the requested build_id",
            "code": "build_id_mismatch",
            "status": 500,
        }
    doc["updated_at"] = utcnow()
    order = doc.setdefault("event_order", [])
    events = doc.setdefault("events", {})
    build_event_seen(doc)
    raw = json.dumps(doc, indent=2) + "\n"
    # Only receipts are evictable. `event_seen` is never touched here.
    while len(order) > 1 and (len(order) > BUILD_RECEIPTS_KEPT or len(raw.encode()) > BUILD_STATE_MAX_BYTES):
        events.pop(order.pop(0), None)
        raw = json.dumps(doc, indent=2) + "\n"
    if len(raw.encode()) > BUILD_STATE_MAX_BYTES:
        return {
            "ok": False,
            "error": "build state exceeds the write bound",
            "code": "state_too_large",
            "status": 413,
        }
    path = build_path(build_id)
    try:
        BUILDS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(BUILDS_DIR, 0o700)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".build-", suffix=".json.tmp")
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(path))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # The rename is only durable once the directory entry is. Without
        # this a crash can leave the build with no build.json at all.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"could not persist build state: {exc}",
            "code": "state_write_failed",
            "status": 500,
        }
    return None


def build_needs_visual(doc: dict[str, Any]) -> bool:
    verification = doc.get("verification") or {}
    return bool(verification.get("needs_visual_proof"))


def build_transition_target(doc: dict[str, Any], action: str) -> str:
    to = BUILD_TRANSITIONS[action][1]
    if to == "evidence" and not build_needs_visual(doc):
        return "review"
    return to


def build_next_action(doc: dict[str, Any]) -> str | None:
    stage = doc.get("stage")
    if stage in BUILD_TERMINAL:
        return None
    for action, (frm, _to) in BUILD_TRANSITIONS.items():
        if frm == stage:
            return action
    return None


def build_budget_left(doc: dict[str, Any]) -> int | None:
    budget = doc.get("budget") or {}
    jobs_max = budget.get("jobs_max")
    if not isinstance(jobs_max, int):
        return None
    return max(0, jobs_max - int(budget.get("jobs_used") or 0))


def build_public(doc: dict[str, Any]) -> dict[str, Any]:
    lease = doc.get("lease")
    return {
        "ok": True,
        "version": doc.get("version"),
        "build_id": doc.get("build_id"),
        "stage": doc.get("stage"),
        "next_action": build_next_action(doc),
        "park_reason": doc.get("park_reason"),
        "park_detail": doc.get("park_detail"),
        "story": doc.get("story"),
        "repo": doc.get("repo"),
        "worktree": doc.get("worktree"),
        "branch": doc.get("branch"),
        "head": doc.get("head"),
        "index": doc.get("index"),
        "ticket": doc.get("ticket"),
        "route": doc.get("route"),
        "verification": doc.get("verification"),
        "writer_session": doc.get("writer_session"),
        "stacks": doc.get("stacks") or [],
        "reviews": doc.get("reviews") or [],
        "evidence_rounds": doc.get("evidence_rounds") or [],
        "budget": doc.get("budget"),
        "budget_left": build_budget_left(doc),
        "verified_boundary": doc.get("verified_boundary"),
        "lease": None if not lease else {k: lease.get(k) for k in ("holder", "acquired_at", "expires_at")},
        "pr": doc.get("pr"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "receipts": [
            (doc.get("events") or {}).get(eid, {}).get("receipt")
            for eid in (doc.get("event_order") or [])[-BUILD_RECEIPTS_SHOWN:]
        ],
        # Receipts are a window; this is the count of event ids the build
        # will never accept a second time.
        "event_count": len(doc.get("event_seen") or {}),
        "event_capacity": BUILD_EVENT_HARD_MAX,
    }


def build_version_park(doc: dict[str, Any]) -> dict[str, Any]:
    """Report a park for a document this build cannot read.

    Deliberately does not rewrite the file. A version we do not understand is
    exactly the case where writing our own shape over it would destroy the
    state a newer service still needs.
    """
    return {
        "ok": False,
        "build_id": doc.get("build_id"),
        "stage": "parked",
        "park_reason": "state_version",
        "found_version": doc.get("version"),
        "expected_version": BUILD_STATE_VERSION,
        "error": "build state version is not readable by this service",
        "code": "incompatible_state_version",
        "status": 409,
    }


def build_record_event(
    doc: dict[str, Any], event_id: str, fingerprint: str, receipt: dict[str, Any], status: int
) -> None:
    """Bind one event id to one outcome, forever.

    `event_seen` is the durable half and never loses an entry or its outcome;
    `events` holds the same receipt while it is still recent enough to show up
    in the public window.
    """
    build_event_seen(doc)[event_id] = {
        "fingerprint": fingerprint,
        "status": status,
        "receipt": receipt,
    }
    doc.setdefault("events", {})[event_id] = {
        "fingerprint": fingerprint,
        "receipt": receipt,
        "status": status,
    }
    order = doc.setdefault("event_order", [])
    if event_id not in order:
        order.append(event_id)


def build_park(
    doc: dict[str, Any],
    reason: str,
    detail: str,
    event_id: str | None = None,
    fingerprint: str | None = None,
    holder: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Park the build and record the park under the event id that caused it.

    An automatic park is a real outcome, not an error on the way to one. If
    it is not recorded, the caller's retry finds no event record, re-runs the
    same guard, and parks a second time — with a second receipt, a second
    budget charge, and no way to tell the two apart.
    """
    detail = detail[:BUILD_REDACT_MAX_STRING]
    frm = doc.get("stage")
    doc["stage"] = "parked"
    doc["park_reason"] = reason
    doc["park_detail"] = detail
    doc["lease"] = None
    receipt: dict[str, Any] | None = None
    if event_id and fingerprint:
        receipt = {
            "event_id": event_id,
            "action": "park",
            "from": frm,
            "to": "parked",
            "at": utcnow(),
            "holder": holder,
            "recovered_lease": None,
            "jobs_spent": 0,
            "budget_left": build_budget_left(doc),
            "automatic": True,
            "park_reason": reason,
            "park_detail": detail,
            "error": detail,
            "code": reason,
        }
        build_record_event(doc, event_id, fingerprint, receipt, 409)
    err = write_build(doc, doc.get("build_id"))
    if err:
        return int(err["status"]), err
    out = build_public(doc)
    out["ok"] = False
    out["replayed"] = False
    out["error"] = detail
    out["code"] = reason
    if receipt is not None:
        out["receipt"] = receipt
    return 409, out


def parse_build_object(
    container: dict[str, Any], key: str, code: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Absent is the default; present has to be a real object.

    `container.get(key) or {}` cannot tell an omitted key from a supplied
    `null`, `""`, `false`, `0`, or `[]` -- every one of those reads as an empty
    object and the call proceeds as if the caller had said nothing. That is
    wrong in both directions: a caller that sent `"expect": null` meant to
    state an identity guard and got the type wrong, and silently dropping it
    lets the transition run against a worktree nobody checked. An empty object
    is a different statement -- "I have nothing to add" -- and stays valid.

    Returns `(None, None)` for absent, so the caller picks the default rather
    than having one baked in here.
    """
    if key not in container:
        return None, None
    value = container[key]
    if not isinstance(value, dict):
        return None, {"error": f"{key} must be an object", "code": code}
    return value, None


def parse_build_verification(container: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    raw, shape_err = parse_build_object(container, "verification", "bad_verification")
    if shape_err:
        return None, shape_err
    if raw is None:
        return {"fast": [], "integration": [], "needs_visual_proof": False, "human_visual_review": False}, None
    out: dict[str, Any] = {}
    for key in ("fast", "integration"):
        # Only an omitted key means "no checks". `or []` folded null, "", 0 and
        # false into absent, so a caller that got the type wrong was told the
        # build has no fast tests instead of being told to fix the request.
        if key not in raw:
            out[key] = []
            continue
        names = raw[key]
        if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
            return None, {"error": f"verification.{key} must be a list of names", "code": "bad_verification"}
        if len(names) > BUILD_VERIFICATION_MAX:
            return None, {"error": f"verification.{key} is too long", "code": "bad_verification"}
        out[key] = [n.strip()[:BUILD_NAME_MAX] for n in names]
    for key in ("needs_visual_proof", "human_visual_review"):
        # needs_visual_proof decides whether `evidence` is a real stage, so a
        # coerced "false" would make the build demand a screenshot round the
        # plan never asked for -- and the writer would have to invent one to
        # reach review.
        flag, flag_err = parse_strict_bool(raw, key, "bad_verification")
        if flag_err:
            return None, flag_err
        out[key] = flag
    return out, None


def parse_strict_bool(container: dict[str, Any], key: str, code: str) -> tuple[bool, dict[str, Any] | None]:
    """Omitted is false; present has to be an actual boolean.

    `bool(value)` is the wrong reader for a JSON flag. `bool("false")` is
    True, so a caller that sends the string "false" for `release_lease` drops
    the lease it just asked to keep, and the next Rewst run walks in on a
    build that is still mid-transition. Null is refused with the rest: an
    explicit null is a caller that meant to say something and got the type
    wrong, which is worth a 400 rather than a silent default.
    """
    if key not in container:
        return False, None
    value = container[key]
    # isinstance(True, int) is True, so the bool check has to come first --
    # but isinstance(1, bool) is False, so numbers land here and are refused.
    if not isinstance(value, bool):
        return False, {"error": f"{key} must be true or false", "code": code}
    return value, None


def parse_build_ref(container: dict[str, Any], field: str) -> tuple[str | None, dict[str, Any] | None]:
    """A repo-relative path, or nothing at all.

    `index` and `ticket` name files a later stage opens inside the build's
    worktree. The catalog has always declared them as optional strings, but
    create took whatever JSON arrived, so a list or an object reached the
    state file and every consumer that read them as a path got a shape the
    spec cannot express. Absolute paths and `..` are refused for the reason
    the build id refuses them: these get joined against a worktree, and a
    path that leaves it is not this build's file.

    Only an omitted key means "no file". `null` and `""` are refused rather
    than folded into absent: both are a caller that meant to name a ticket and
    sent nothing usable, and answering with a build that has no ticket sends
    the writer at the whole story instead.
    """
    if field not in container:
        return None, None
    raw = container[field]
    bad = {"error": f"{field} must be a repo-relative path", "code": f"bad_{field}"}
    if not isinstance(raw, str):
        return None, bad
    value = raw.strip()
    if not value:
        return None, bad
    if len(value) > BUILD_NAME_MAX:
        return None, {"error": f"{field} is too long", "code": f"bad_{field}"}
    if value.startswith("/") or value.startswith("~") or "\\" in value or "\x00" in value:
        return None, bad
    if ".." in value:
        return None, {"error": f"{field} must not escape the worktree", "code": f"bad_{field}"}
    return value, None


def parse_build_stacks(container: dict[str, Any]) -> tuple[list[str] | None, dict[str, Any] | None]:
    """Absent is no stacks; present has to be a list. An empty list is valid.

    `null` and `""` are refused with every other wrong type, so a caller that
    meant to name the stacks this build needs up finds out rather than getting
    a build that quietly declares none.
    """
    if "stacks" not in container:
        return [], None
    raw = container["stacks"]
    if not isinstance(raw, list) or not all(isinstance(s, str) and s.strip() for s in raw):
        return None, {"error": "stacks must be a list of names", "code": "bad_stacks"}
    if len(raw) > BUILD_STACKS_MAX:
        return None, {"error": "too many stacks", "code": "bad_stacks"}
    return [s.strip()[:BUILD_NAME_MAX] for s in raw], None


def parse_build_head(raw: Any, field: str) -> tuple[str | None, dict[str, Any] | None]:
    if raw in (None, ""):
        return None, None
    if not isinstance(raw, str) or not BUILD_HEAD_RE.fullmatch(raw.strip()):
        return None, {"error": f"{field} must be a git object id", "code": f"bad_{field}"}
    return raw.strip(), None


def parse_build_route(container: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The route is the sliceRoute row, so it is an object or it is absent.

    The catalog says object; accepting a string, a list, or a `null` here would
    put a shape in the state file that no consumer of the spec can read.

    Validation only: the route comes back as the caller sent it. Redacting
    here would hand the same value to the fingerprint and to disk, and two
    different Rewst capability URLs under one generic key both redact to
    `[redacted]` — so a second create with a different resume URL would
    fingerprint equal to the first and replay its receipt instead of
    conflicting. The caller redacts separately, on the copy it persists.
    """
    raw, shape_err = parse_build_object(container, "route", "bad_route")
    if shape_err:
        return None, shape_err
    if raw is None:
        return None, None
    if len(raw) > BUILD_ROUTE_MAX_KEYS:
        return None, {"error": "route has too many keys", "code": "bad_route"}
    return raw, None


def build_live_identity(worktree: Path) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Ask the repository what branch and commit it is actually on.

    The build's identity cannot come from the caller. A caller that says
    `head: aaa...` and `branch: feature/x` is describing what it believes,
    and every guard built on comparing that belief to a stored copy of the
    same belief agrees with itself no matter what the checkout is doing.
    """
    branch = run_git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
    head = run_git(worktree, ["rev-parse", "HEAD"])
    if not branch.get("ok") or not head.get("ok"):
        detail = str(branch.get("error") or head.get("error") or "git could not read the worktree")
        return None, {
            "error": f"could not resolve the live worktree identity: {detail}"[:BUILD_REDACT_MAX_STRING],
            "code": "identity_unreadable",
        }
    name = branch["stdout"].strip()
    sha = head["stdout"].strip()
    if not BUILD_HEAD_RE.fullmatch(sha):
        return None, {"error": "live HEAD is not a git object id", "code": "identity_unreadable"}
    return {"branch": name, "head": sha}, None


def build_resolve_identity(
    doc: dict[str, Any], repos: dict[str, str]
) -> tuple[dict[str, str] | None, str | None]:
    """Re-resolve the build's worktree through the allowlist, then read it.

    Returns (identity, park_detail). The allowlist is consulted again rather
    than trusting the stored path: a repo dropped from repos.json must stop
    the build, not keep driving a directory the operator revoked.
    """
    name, resolved = resolve_repo(doc.get("repo"), repos)
    if name is None:
        return None, f"repo '{doc.get('repo')}' is no longer an allowlisted worktree"
    if str(resolved) != doc.get("worktree"):
        return None, f"repo '{name}' now resolves to {resolved}, build was opened on {doc.get('worktree')}"
    live, err = build_live_identity(resolved)
    if err:
        return None, str(err["error"])
    return live, None


# Transitions that consume the verification result rather than produce it.
# Past this line the build is asserting that reviewed, verified code is what
# ships, so the commit under it is not allowed to have changed.
BUILD_POST_VERIFY_ACTIONS = frozenset({"evidence_captured", "review_passed", "pr_opened"})


def build_identity_park(
    doc: dict[str, Any],
    action: str,
    expected_worktree: Any,
    expected_branch: str | None,
    expected_head: str | None,
    repos: dict[str, str],
) -> tuple[dict[str, str] | None, tuple[str, str] | None]:
    """Compare all five accounts of where this build is, before it advances.

    The five are the caller's `expect`, the stored document, the allowlist,
    the live checkout, and the verified boundary. Any disagreement parks:
    the whole point of a resumable build is that it stops when it cannot
    explain the ground it is standing on, rather than committing a second
    real side effect on a guess.

    Returns (live_identity, None) when they agree, or (None, (reason, detail)).
    """
    if expected_worktree not in (None, "") and str(expected_worktree) != doc.get("worktree"):
        return None, (
            "worktree_mismatch",
            f"expected worktree {expected_worktree}, build was opened on {doc.get('worktree')}",
        )
    live, detail = build_resolve_identity(doc, repos)
    if live is None:
        return None, ("worktree_mismatch", str(detail))

    # The branch is the durable half of identity: a build never changes it.
    if live["branch"] != doc.get("branch"):
        return None, (
            "branch_mismatch",
            f"worktree is on branch '{live['branch']}', build was opened on '{doc.get('branch')}'",
        )
    if expected_branch not in (None, "") and expected_branch != live["branch"]:
        return None, (
            "branch_mismatch",
            f"caller expected branch '{expected_branch}', worktree is on '{live['branch']}'",
        )

    # The head is the moving half: the writer commits between transitions. So
    # expect.head is checked against the live repository, never against the
    # stored copy of an earlier caller's claim.
    if expected_head and expected_head != live["head"]:
        return None, (
            "head_moved",
            f"caller expected head {expected_head}, worktree HEAD is {live['head']}",
        )

    boundary = doc.get("verified_boundary")
    if action in BUILD_POST_VERIFY_ACTIONS and isinstance(boundary, dict):
        verified_head = boundary.get("head")
        if verified_head and verified_head != live["head"]:
            return None, (
                "head_moved_after_verify",
                f"verification passed at {verified_head}, worktree HEAD is now {live['head']}",
            )
    return live, None


def build_create(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    story = data.get("story")
    if not isinstance(story, str) or not story.strip():
        return 400, {"error": "story is required", "code": "missing_story"}
    story_err = reject_secret_text(story, "story")
    if story_err:
        return 400, story_err
    repo_name, resolved = repo_from_body(data, repos)
    if repo_name is None:
        return 400, resolved
    worktree = str(resolved)
    declared_worktree = data.get("worktree")
    if declared_worktree not in (None, "") and str(declared_worktree) != worktree:
        # The caller thinks it is somewhere else. That is the identity guard
        # firing before any state exists, so refuse rather than park.
        return 400, {
            "error": "worktree does not match the allowlisted repo path",
            "code": "worktree_mismatch",
            "worktree": worktree,
        }
    branch = data.get("branch")
    if not isinstance(branch, str) or not valid_branch(branch.strip()):
        return 400, {"error": "branch is required and must be a plain ref name", "code": "bad_branch"}
    branch = branch.strip()
    head, head_err = parse_build_head(data.get("head"), "head")
    if head_err:
        return 400, head_err
    live, live_err = build_live_identity(resolved)
    if live_err:
        return 409, {**live_err, "worktree": worktree}
    assert live is not None
    if live["branch"] != branch:
        # No state exists yet, so there is nothing to park. Refusing here is
        # the last moment a wrong branch is cheap to say no to.
        return 400, {
            "error": f"worktree is on branch '{live['branch']}', not '{branch}'",
            "code": "branch_mismatch",
            "branch": live["branch"],
            "worktree": worktree,
        }
    if head and not live["head"].startswith(head) and not head.startswith(live["head"]):
        return 400, {
            "error": f"worktree HEAD is {live['head']}, not {head}",
            "code": "head_mismatch",
            "head": live["head"],
            "worktree": worktree,
        }
    # Identity is what the repository says, not what the caller declared.
    head = live["head"]
    verification, ver_err = parse_build_verification(data)
    if ver_err:
        return 400, ver_err
    stacks, stacks_err = parse_build_stacks(data)
    if stacks_err:
        return 400, stacks_err
    raw_budget, budget_shape_err = parse_build_object(data, "budget", "bad_budget")
    if budget_shape_err:
        return 400, budget_shape_err
    jobs_max, budget_err = parse_optional_int(raw_budget or {}, "jobs_max", 1, BUILD_JOBS_MAX)
    if budget_err:
        return 400, budget_err
    route_raw, route_err = parse_build_route(data)
    if route_err:
        return 400, route_err
    # Two copies, deliberately. `route_raw` is fingerprinted and then dropped;
    # only `route` is written or returned, so no raw capability URL leaves
    # this function.
    route = redact_secrets(route_raw) if route_raw is not None else None
    index, index_err = parse_build_ref(data, "index")
    if index_err:
        return 400, index_err
    ticket, ticket_err = parse_build_ref(data, "ticket")
    if ticket_err:
        return 400, ticket_err
    build_id = data.get("build_id")
    if build_id in (None, ""):
        # Generating one here would make every timed-out retry open another
        # build against the same worktree, each with its own lease and its
        # own budget. The caller owns the id because only the caller can
        # repeat it.
        return 400, {
            "error": "build_id is required so a retried create returns the same build",
            "code": "missing_build_id",
        }
    if not valid_build_id(build_id):
        return 400, {"error": "invalid build_id", "code": "bad_build_id"}

    fingerprint = event_fingerprint(
        {
            "story": story.strip(),
            "repo": repo_name,
            "branch": branch,
            "index": index,
            "ticket": ticket,
            # The raw row, not the redacted one: see parse_build_route.
            "route": route_raw,
            "verification": verification,
            "stacks": stacks,
            "jobs_max": jobs_max,
        }
    )
    with BUILD_LOCK:
        existing, read_err = load_build(build_id)
        if read_err:
            # Never recreate over state we could not read. The file may hold
            # the only record of which event ids have already run.
            return int(read_err["status"]), read_err
        if existing is not None:
            if existing.get("version") != BUILD_STATE_VERSION:
                return 409, build_version_park(existing)
            if existing.get("create_fingerprint") != fingerprint:
                return 409, {
                    "error": "build_id already exists with different input",
                    "code": "idempotency_conflict",
                    "build_id": build_id,
                }
            out = build_public(existing)
            out["created"] = False
            return 200, out
        doc = {
            "version": BUILD_STATE_VERSION,
            "build_id": build_id,
            "stage": "created",
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "story": story.strip()[:BUILD_NAME_MAX],
            "repo": repo_name,
            "worktree": worktree,
            "branch": branch,
            "head": head,
            # Already validated to a bounded repo-relative path, so it goes
            # in as itself. Running it through redact_secrets would let a
            # 64-hex path segment turn the ticket into "[redacted]", and the
            # build would have lost the file it was opened to write.
            "index": index,
            "ticket": ticket,
            "route": route,
            "verification": verification,
            "writer_session": None,
            "stacks": stacks,
            "reviews": [],
            "evidence_rounds": [],
            "budget": {"jobs_max": jobs_max, "jobs_used": 0},
            "verified_boundary": None,
            "lease": None,
            "park_reason": None,
            "park_detail": None,
            "pr": None,
            "create_fingerprint": fingerprint,
            "events": {},
            "event_order": [],
            "event_seen": {},
        }
        write_err = write_build(doc, build_id)
        if write_err:
            return int(write_err["status"]), write_err
        out = build_public(doc)
        out["created"] = True
        return 201, out


def build_lease_live(doc: dict[str, Any], now: float) -> dict[str, Any] | None:
    lease = doc.get("lease")
    if not isinstance(lease, dict):
        return None
    try:
        expires = float(lease.get("expires_epoch") or 0)
    except (TypeError, ValueError):
        return None
    return lease if expires > now else None


def build_apply(doc: dict[str, Any], action: str, data: dict[str, Any], target: str) -> None:
    """Fold a transition's payload into the document.

    Every field here is one of the things the story requires the state to keep
    across a service restart, so the merge is explicit rather than a blind
    dict update: an unknown key is dropped, not stored.
    """
    session = data.get("writer_session")
    if action == "start_writer" and isinstance(session, str) and session.strip():
        # Shape already checked by the caller, so no second bound is needed.
        # Only start_writer binds it, and the caller has already been told off
        # if it tried to rebind an existing one.
        doc["writer_session"] = session.strip()
    stacks, _ = parse_build_stacks(data)
    if stacks:
        doc["stacks"] = stacks
    # data.head is deliberately not folded in. Identity comes from the live
    # repository read, so a payload field cannot move the build's head.
    if action == "review_passed" and isinstance(data.get("review"), dict):
        doc["reviews"] = (doc.get("reviews") or [])[-(BUILD_LIST_MAX - 1):] + [redact_secrets(data["review"])]
    if action == "evidence_captured" and isinstance(data.get("round"), dict):
        rounds = (doc.get("evidence_rounds") or [])[-(BUILD_LIST_MAX - 1):]
        doc["evidence_rounds"] = rounds + [redact_secrets(data["round"])]
    if action == "pr_opened" and isinstance(data.get("pr"), dict):
        doc["pr"] = redact_secrets(data["pr"])
    if action == "verify_passed":
        # The last verified boundary. A lease takeover after expiry must not
        # roll this back, so it is written here and never cleared. The stage
        # recorded is the one the build reached, not the one it left: a
        # recovering caller needs to know where verified work ends.
        doc["verified_boundary"] = {
            "stage": target,
            "head": doc.get("head"),
            "at": utcnow(),
            "verification": doc.get("verification"),
        }


def build_advance(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    build_id = data.get("build_id")
    if not valid_build_id(build_id):
        return 400, {"error": "invalid build_id", "code": "bad_build_id"}
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return 400, {"error": "event_id is required", "code": "bad_event_id"}
    action = data.get("action")
    if action not in BUILD_TRANSITIONS and action != "park":
        return 400, {
            "error": "unknown action",
            "code": "bad_action",
            "allowed": sorted(BUILD_TRANSITIONS) + ["park"],
        }
    holder = data.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        return 400, {"error": "holder is required", "code": "missing_holder"}
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return 400, holder_err
    lease_seconds, lease_err = parse_optional_int(data, "lease_seconds", 1, BUILD_LEASE_MAX_SECONDS)
    if lease_err:
        return 400, lease_err
    lease_seconds = lease_seconds or BUILD_LEASE_SECONDS
    jobs_spent, spend_err = parse_optional_int(data, "jobs_spent", 0, BUILD_JOBS_MAX)
    if spend_err:
        return 400, spend_err
    if jobs_spent is None:
        jobs_spent = 0 if action == "park" else 1
    payload, payload_err = parse_build_object(data, "data", "bad_data")
    if payload_err:
        return 400, payload_err
    if payload is None:
        payload = {}
    session = payload.get("writer_session")
    if session not in (None, "") and not (
        isinstance(session, str) and HERMES_SESSION_RE.fullmatch(session.strip())
    ):
        # The writer session is the handle that resumes one agent. Anything
        # that is not a real session id is either a typo that would strand the
        # writer or free text that has no business on disk.
        return 400, {"error": "writer_session must be a gwslice session id", "code": "bad_writer_session"}
    if session not in (None, "") and action != "start_writer":
        # The session handle names the agent this build started. A later
        # transition that repointed it would strand the real writer and hand
        # the build's resume handle to whatever the caller happened to send.
        return 400, {
            "error": "writer_session may only be set by start_writer",
            "code": "writer_session_locked",
        }
    expect, expect_err = parse_build_object(data, "expect", "bad_expect")
    if expect_err:
        return 400, expect_err
    if expect is None:
        expect = {}
    release_lease, release_err = parse_strict_bool(data, "release_lease", "bad_release_lease")
    if release_err:
        return 400, release_err

    # The fingerprint is every input that changes what this call does. Leaving
    # holder, jobs_spent, lease_seconds, or release_lease out of it meant the
    # same event id could be retried asking to spend nine jobs instead of one
    # and be answered with the one-job receipt.
    fingerprint = event_fingerprint(
        {
            "build_id": build_id,
            "action": action,
            "holder": holder,
            "expect": expect,
            "data": payload,
            "jobs_spent": jobs_spent,
            "lease_seconds": lease_seconds,
            "release_lease": release_lease,
        }
    )
    with BUILD_LOCK:
        doc, read_err = load_build(build_id)
        if read_err:
            return int(read_err["status"]), read_err
        if doc is None:
            return 404, {"error": "unknown build", "code": "unknown_build", "build_id": build_id}
        if doc.get("version") != BUILD_STATE_VERSION:
            return 409, build_version_park(doc)

        seen = build_event_seen(doc)
        if event_id in seen:
            # Replay is answered before the lease check on purpose: the caller
            # retrying a timed-out call has usually lost its lease already, and
            # the transition it is asking about has demonstrably run.
            record = seen.get(event_id) or {}
            if record.get("fingerprint") != fingerprint:
                out = build_public(doc)
                out["ok"] = False
                out["replayed"] = False
                out["error"] = "event_id was already used with different input"
                out["code"] = "idempotency_conflict"
                return 409, out
            out = build_public(doc)
            out["replayed"] = True
            prior_receipt = record.get("receipt")
            out["receipt"] = prior_receipt
            if isinstance(prior_receipt, dict):
                # The replay record outlives the receipt window, so this is
                # the original receipt and the original status even when the
                # public window moved on long ago.
                status = int(record.get("status") or 200)
            else:
                # Only reachable for a document written before replay records
                # existed, whose receipt had already aged out of `events` by
                # the time this service read it. Still a replay: what must
                # never happen is running it again.
                out["receipt_available"] = False
                status = 200
            if status != 200:
                receipt = out.get("receipt") or {}
                out["ok"] = False
                out["error"] = receipt.get("error") or doc.get("park_detail")
                out["code"] = receipt.get("code") or doc.get("park_reason")
            return status, out
        if len(seen) >= BUILD_EVENT_HARD_MAX:
            # Accepting this would mean evicting an id, and an evicted id is
            # a replayable one. Refuse without touching the document.
            return 409, {
                "ok": False,
                "build_id": build_id,
                "stage": doc.get("stage"),
                "error": "build has used its whole event history; open a new build",
                "code": "event_history_full",
                "event_count": len(seen),
                "event_capacity": BUILD_EVENT_HARD_MAX,
            }

        now = time.time()
        live = build_lease_live(doc, now)
        if live is not None and live.get("holder") != holder:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "another caller holds the build lease"
            out["code"] = "lease_held"
            return 409, out
        # An expired lease held by someone else means this caller is taking a
        # build over, not continuing its own. Naming that in the receipt is the
        # difference between a recovery and a silent second driver.
        stale = doc.get("lease")
        recovered_from = (
            stale.get("holder")
            if live is None and isinstance(stale, dict) and stale.get("holder") not in (None, holder)
            else None
        )

        expected_head, head_err = parse_build_head(expect.get("head"), "head")
        if head_err:
            return 400, head_err
        expected_branch = expect.get("branch")
        if expected_branch not in (None, "") and not (
            isinstance(expected_branch, str) and valid_branch(expected_branch.strip())
        ):
            return 400, {"error": "expect.branch must be a plain ref name", "code": "bad_expect_branch"}
        live, park = build_identity_park(
            doc,
            action,
            expect.get("worktree"),
            expected_branch.strip() if isinstance(expected_branch, str) else None,
            expected_head,
            repos,
        )
        if park is not None:
            reason, detail = park
            return build_park(doc, reason, detail, event_id, fingerprint, holder)
        assert live is not None

        stage = doc.get("stage")
        if action == "park":
            reason = str(payload.get("reason") or "requested")
            if not BUILD_PARK_REASON_RE.fullmatch(reason):
                return 400, {"error": "park reason must be a short slug", "code": "bad_park_reason"}
            if stage in BUILD_TERMINAL:
                out = build_public(doc)
                out["ok"] = False
                out["error"] = f"build is already {stage}"
                out["code"] = "invalid_transition"
                return 409, out
            target = "parked"
        else:
            frm = BUILD_TRANSITIONS[action][0]
            if stage != frm:
                out = build_public(doc)
                out["ok"] = False
                out["error"] = f"action '{action}' is not allowed from stage '{stage}'"
                out["code"] = "invalid_transition"
                out["expected_action"] = build_next_action(doc)
                return 409, out
            target = build_transition_target(doc, action)

        budget = doc.setdefault("budget", {"jobs_max": None, "jobs_used": 0})
        used = int(budget.get("jobs_used") or 0) + jobs_spent
        jobs_max = budget.get("jobs_max")
        if isinstance(jobs_max, int) and used > jobs_max and target != "parked":
            return build_park(
                doc,
                "budget_exhausted",
                f"{used} jobs requested against a budget of {jobs_max}",
                event_id,
                fingerprint,
                holder,
            )
        budget["jobs_used"] = used

        if target == "parked":
            doc["park_reason"] = str(payload.get("reason") or "requested")
            doc["park_detail"] = redact_secrets(payload.get("detail"))
        else:
            # The head is re-anchored from the live read, so the boundary the
            # next transition is checked against is where the repository
            # actually is, not where an earlier caller said it was.
            doc["head"] = live["head"]
            build_apply(doc, action, payload, target)
        doc["stage"] = target
        doc["lease"] = (
            None
            if release_lease or target in BUILD_TERMINAL
            else {
                "holder": holder,
                "acquired_at": utcnow(),
                "expires_epoch": now + lease_seconds,
                "expires_at": datetime.fromtimestamp(now + lease_seconds, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        )
        receipt = {
            "event_id": event_id,
            "action": action,
            "from": stage,
            "to": target,
            "at": utcnow(),
            "holder": holder,
            "recovered_lease": recovered_from,
            "jobs_spent": jobs_spent,
            "budget_left": build_budget_left(doc),
            "head": doc.get("head"),
            "branch": doc.get("branch"),
        }
        build_record_event(doc, event_id, fingerprint, receipt, 200)
        write_err = write_build(doc, build_id)
        if write_err:
            return int(write_err["status"]), write_err
        out = build_public(doc)
        out["replayed"] = False
        out["receipt"] = receipt
        return 200, out


def build_state_op(qs: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    build_id = first_query(qs, "build_id")
    if not valid_build_id(build_id):
        return 400, {"error": "invalid build_id", "code": "bad_build_id"}
    assert build_id is not None
    doc, read_err = load_build(build_id)
    if read_err:
        return int(read_err["status"]), read_err
    if doc is None:
        return 404, {"error": "unknown build", "code": "unknown_build", "build_id": build_id}
    if doc.get("version") != BUILD_STATE_VERSION:
        return 409, build_version_park(doc)
    return 200, build_public(doc)


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
    if method == "GET" and path == "/v1/watch":
        return json_out(200, watch_snapshot())

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
    if method == "POST" and path == "/v1/build/create":
        status, payload = build_create(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/advance":
        status, payload = build_advance(body, repos)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/build/state":
        status, payload = build_state_op(qs)
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
                # Fold the check state in here. The graph cannot read it from
                # the checks node: that output is ~21KB and Rewst replaces it
                # with an artifact stub, so TASKS.checks.all_green is null.
                ck = annotate_pr_checks(
                    gh_json(repo_path, ["pr", "checks", number, "--json", "name,state,bucket,link"])
                )
                out["all_green"] = bool(ck.get("all_green"))
                out["any_red"] = bool(ck.get("any_red"))
                out["failing"] = (ck.get("failing") or [])[:20]
                # Flat, not nested under "data": the connector already exposes
                # this as TASKS.<node>.data.<field>, so nesting made every path
                # a double .data.data and drive_snap read nulls.
                out["status"] = 200 if out.get("ok") else 422
                # The findings list is for humans reading the trace; the brief
                # is what the writer consumes. Keep the payload small.
                out["findings"] = out.get("findings", [])[:20]
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

    if method == "POST" and path == "/v1/pr/continue":
        status, payload = pr_continue(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/gh/pr/merge":
        status, payload = gh_pr_merge(body, repos)
        return json_out(status, payload)
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
