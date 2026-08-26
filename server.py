#!/usr/bin/env python3
"""graphwing OpenAPI server — deterministic laptop nodes + one-job agent loops."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

HOME = Path(os.environ.get("GRAPHWING_HOME", Path.home() / ".graphwing"))
OPENAPI_PATH = HOME / "openapi.json"
REPOS_PATH = HOME / "repos.json"
PROFILES_PATH = HOME / "profiles.json"
SCRIPTS_PATH = HOME / "scripts.json"
STACKS_PATH = HOME / "stacks.json"
TESTS_PATH = HOME / "tests.json"
RR_PATH = HOME / "rr.json"
VISUAL_BROWSERS_PATH = HOME / "visual-browsers.json"
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


CODEX_BIN = resolve_executable("codex", "GRAPHWING_CODEX_BIN", Path.home() / ".local" / "bin" / "codex")
CLAUDE_BIN = resolve_executable("claude", "GRAPHWING_CLAUDE_BIN", Path.home() / ".local" / "bin" / "claude")
GROK_BIN = resolve_executable("grok", "GRAPHWING_GROK_BIN", Path.home() / ".local" / "bin" / "grok")
RR_BIN = resolve_executable("rr", "GRAPHWING_RR_BIN", Path.home() / "go" / "bin" / "rr")
AGENT_MAX_TURNS = 30
AGENT_RUN_BUDGET = 300
AGENT_MAX_CONCURRENT = 3
SCRIPT_SYNC_TIMEOUT = 25
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
HARNESS_SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SLICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SLICE_KINDS = frozenset({"build", "decision"})
SLICE_TICKET_CORE_KEYS = frozenset({"id", "path", "kind", "status", "blocked_by"})
SLICE_STATUSES = frozenset({"open", "done"})
SLICE_CLASSES = frozenset({"mechanical", "visual", "sensitive"})
SLICE_SIZES = ("S", "M", "L")
# Spec-review turn budget. Was hardcoded to 1 for the claude reviewers, so
# every review died on "Reached max turns (1)" and parsed as a NACK without
# reading anything (SC-110290's first run nacked twice that way). Direct
# harnesses get enough turns to read the diff, think, answer, and leave slack for a
# provider hiccup or a dropped connection costing a turn.
REVIEW_MAX_TURNS = int(os.environ.get("GRAPHWING_REVIEW_MAX_TURNS", "12"))
# Spec-review is read-only. The claude reviewers get --permission-mode plan,
# which enforces that in the runner. Codex and Grok are explicitly in
# read-only/plan mode and no path invokes a broker or implicit fallback.
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
# The three transitions that assert a gate was satisfied. On the shared build
# path an API caller may not perform them at all: buildChecks, buildReview,
# buildGate, and buildFinalize record them from the receipts they just wrote,
# after re-verifying that those receipts belong to the change now on disk.
# A caller that could post `action: review_passed` could ship an unreviewed
# change with one HTTP call, which is the whole failure the path exists to stop.
BUILD_GATE_ACTIONS = frozenset({"verify_passed", "review_passed", "pr_opened"})
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
# The mechanical path. One bounded Rewst invocation runs exactly one of these
# steps; which one is read off the durable document, never asserted by the
# caller. `fix` covers both ways a change comes back for correction -- a red
# deterministic check and a reviewer finding -- because the writer's turn is
# the same either way: here is what went wrong, the files are on disk.
BUILD_MECHANICAL_STEPS = (
    "preflight",
    "write",
    "fast_checks",
    "fix",
    "verify_passed",
    "review",
    "review_passed",
    "integration",
    "finalize",
    "done",
    "parked",
)
BUILD_MECHANICAL_CLASS = "mechanical"
BUILD_SENSITIVE_CLASS = "sensitive"
# The classes graphwing-pre-pr-build drives end to end. Sensitive rides the same
# stage machine as mechanical and differs in exactly one place: the `review`
# stage owes two independent acknowledgements instead of one. Adding a second
# workflow for it would have duplicated preflight, the claim, the writer, the
# checks, and finalization -- five places to keep in step -- to express one
# extra reviewer.
BUILD_PATH_CLASSES = (BUILD_MECHANICAL_CLASS, BUILD_SENSITIVE_CLASS)
# The two lenses a review can be. A role is not a reviewer name: the role says
# what the review is answerable for, and the route says who answers it. Keeping
# them apart is what lets a receipt record "the security review passed" rather
# than "grok said ok", which is the only form that survives a routing change.
BUILD_REVIEW_BEHAVIOR_ROLE = "behavior"
BUILD_REVIEW_SECURITY_ROLE = "security"
BUILD_REVIEW_ROLES = (BUILD_REVIEW_BEHAVIOR_ROLE, BUILD_REVIEW_SECURITY_ROLE)
BUILD_REVIEW_ROLE_LABEL = {
    BUILD_REVIEW_BEHAVIOR_ROLE: "behavior-and-test",
    BUILD_REVIEW_SECURITY_ROLE: "security-and-tenant",
}
# Which route stamp names the reviewer for each role. sliceRoute already writes
# reviewer1/reviewer2, so the pre-PR build reads the planner's row rather than
# inventing a second vocabulary for the same two seats.
BUILD_ROLE_ROUTE_STAMP = {
    BUILD_REVIEW_BEHAVIOR_ROLE: "reviewer1",
    BUILD_REVIEW_SECURITY_ROLE: "reviewer2",
}
# What each class owes before `review_passed`. A review recorded on a document
# written before roles existed has no role field and reads as `behavior`, which
# is what it was.
BUILD_CLASS_REVIEW_ROLES = {
    BUILD_MECHANICAL_CLASS: (BUILD_REVIEW_BEHAVIOR_ROLE,),
    BUILD_SENSITIVE_CLASS: BUILD_REVIEW_ROLES,
}
# Sensitive routing is not a preference the planner expresses; it is a fixed
# assignment this service enforces. Opus writes, Terra grades behavior and
# tests, Grok grades security and tenant isolation. Naming them here rather
# than accepting whatever the route says is what makes "the security review
# passed" mean something: a route that swapped Grok for another Anthropic model
# would still satisfy a generic cross-vendor rule while putting the writer's own
# vendor on both sides of the highest-risk class.
BUILD_SENSITIVE_REVIEWER = {
    BUILD_REVIEW_BEHAVIOR_ROLE: "terra",
    BUILD_REVIEW_SECURITY_ROLE: "grok",
}
# Sensitive work is written by Opus. Matched as an alias inside the route's
# model string, the same way BUILD_WRITER_VENDOR_ALIASES matches, so both
# `opus` and `claude-opus-5` are the same declaration.
BUILD_SENSITIVE_WRITER_ALIAS = "opus"
BUILD_SENSITIVE_WRITER_VENDOR = "anthropic"
# A design turn, like a correction, resumes the recorded writer session. It is
# visual feedback rather than a failure and therefore is not a strike; all three
# claim kinds still charge a job before an agent is launched.
BUILD_CLAIM_KINDS = ("initial", "correction", "design")

# A story branch is never one of these. A build that finds itself on trunk is
# about to commit and push straight to it, which is the one outcome the whole
# pre-PR path exists to prevent.
BUILD_TRUNK_BRANCHES = frozenset({"main", "master", "trunk", "develop", "HEAD"})
# The routing stamps a writer cannot be launched without. Each one is a real
# launch argument: an absent max_turns is not "use the default", it is a
# writer that runs until something else kills it.
BUILD_ROUTE_STAMPS = ("launcher", "model", "max_turns", "run_budget_seconds", "reviewer1")
# A sensitive build additionally cannot be launched without the second seat.
# An absent reviewer2 is not "review with one voice"; it is a build that can
# never satisfy its own review gate, discovered after the writer has spent its
# turn rather than at preflight.
BUILD_SENSITIVE_ROUTE_STAMPS = BUILD_ROUTE_STAMPS + ("reviewer2",)
# The recipe the pre-PR plan calls out by name: it is a pre-commit hook, so it
# may run, but it can never be the thing that makes a phase covered. Catalog
# entries can say "coverage": false for themselves; this is the backstop for
# when they forget.
BUILD_HOOK_ONLY_RECIPES = frozenset({"riftwing-local-gates"})
BUILD_CHECK_PHASES = ("fast", "integration")
BUILD_PATH_RULES_MAX = 16
BUILD_CHANGED_PATHS_MAX = 256
# Silence is a provider blip, not an opinion. Retry it; only a reviewer that
# finished and still said no is a finding.
# A lost provider is a terminal attempt outcome.  Re-running it in this
# process conceals the primary failure from the workflow, which is the exact
# decision Rewst—not the daemon—must make for ticket 07.
BUILD_REVIEW_SILENCE_RETRIES = 0
BUILD_REVIEW_ROUNDS_KEPT = 32
# Which vendor answers to which reviewer name. A reviewer from the writer's own
# vendor is not an independent review, so the two are compared before a review
# is allowed to run.
BUILD_REVIEWER_VENDOR = {
    "sonnet": "anthropic",
    "opus": "anthropic",
    "fable": "anthropic",
    "sol": "openai",
    "terra": "openai",
    "grok": "xai",
}
BUILD_REVIEWER_PROVIDER = {
    "sonnet": "anthropic",
    "opus": "anthropic",
    "fable": "anthropic",
    "sol": "openai",
    "terra": "openai",
    "grok": "xai",
}
BUILD_REVIEWER_MODEL = {
    "sonnet": "sonnet",
    "opus": "opus",
    "fable": "claude-fable-5",
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "grok": "grok-4.6",
}
# Which vendor a writer model belongs to. The route names a model the way an
# operator says it out loud -- "sonnet", "grok-4.6", "gpt-5.6-terra" -- so a
# prefix table over full model ids alone left every seat alias unmapped, and an
# unmapped writer meant the independence check never ran: a sonnet writer and
# an opus reviewer both passed as "independent". Longest alias first so
# `claude-sonnet-5` is not decided by a shorter substring.
BUILD_WRITER_VENDOR_ALIASES = (
    ("claude", "anthropic"),
    ("sonnet", "anthropic"),
    ("haiku", "anthropic"),
    ("opus", "anthropic"),
    ("fable", "anthropic"),
    ("grok", "xai"),
    ("gpt", "openai"),
    ("terra", "openai"),
    ("sol", "openai"),
    ("o4", "openai"),
)
# A model that matches no alias. Named rather than None so the two cases stay
# apart: no model at all is a route gap the planner can fix, an unrecognised
# model is a writer whose independence cannot be established and fails closed.
BUILD_VENDOR_UNKNOWN = "unknown"
BUILD_GATE_RECORDS_KEPT = 32
# Ticket 07 is a proof overlay on the existing build receipt.  It deliberately
# lives in the build document rather than introducing a second store, runner,
# or service: the controller supplies external canary evidence and this API
# makes the finalization boundary fail closed until the census is complete.
BUILD_ROLLOUT_TICKET = "github_issue_52"
BUILD_ROLLOUT_RECEIPT_KINDS = (
    "catalog_parity",
    "mechanical",
    "sensitive",
    "visual",
    "checkpoint",
    "lifecycle",
)
BUILD_ROLLOUT_RECEIPT_KIND_SET = frozenset(BUILD_ROLLOUT_RECEIPT_KINDS)
BUILD_ROLLOUT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# What a stack is for, declared by the catalog entry rather than guessed from
# its name. Only `clean` may carry a build's final integration run: it is a
# disposable stack brought up from its own checkout, so a pass on it is a claim
# about a clean tree instead of a claim about whatever the writer left running.
# An entry that declares nothing is `unset`, which is never clean -- the name
# substrings this replaced made `riftwing-ci` look isolated and `dev-clean`
# look like a developer stack, and neither reading had any evidence behind it.
BUILD_STACK_ROLES = ("clean", "developer", "preview", "unset")
BUILD_CLEAN_STACK_ROLE = "clean"
# The stack a visual round drives. `preview` is its own role for the same
# reason `clean` is: a screenshot taken against the writer's developer stack is
# a picture of whatever was left running, and a screenshot against the clean
# integration runner is a picture of a tree with no browser in front of it.
BUILD_PREVIEW_STACK_ROLE = "preview"
# One bounded visual round, step by step. Read off the durable round record the
# same way the mechanical steps are read off the build: an invocation that died
# after its browser ran comes back and is told `inspect`, not `scenario`.
BUILD_VISUAL_STEPS = (
    "preflight",
    "preview",
    "scenario",
    "inspect",
    "review",
    "handoff",
    "publish",
    "human_review",
    "done",
    "parked",
)
# What a captured image is evidence *of*. The first round needs both: a
# candidate with nothing to compare it to is a picture, not proof of a change.
BUILD_VISUAL_ROLES = ("before", "candidate")
BUILD_VISUAL_VIEWPORTS_MAX = 6
BUILD_VISUAL_STEPS_MAX = 24
BUILD_VISUAL_IMAGES_MAX = 16
# Public responses summarize history, but the durable build document keeps every
# round. Supersession pointers are audit references and must never dangle.
BUILD_EVIDENCE_ROUNDS_KEPT = 32
BUILD_VISUAL_RECAPTURE_MAX = 1
# A screenshot smaller than this is a blank or an error card, not a legible
# view of a UI. The width floor is the narrowest viewport worth reviewing.
BUILD_IMAGE_MIN_BYTES = 1024
BUILD_IMAGE_MAX_BYTES = 8 * 1024 * 1024
BUILD_IMAGE_MIN_WIDTH = 320
BUILD_IMAGE_MIN_HEIGHT = 240
BUILD_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# The upload handoff is short-lived by construction: Rewst pulls the bytes once,
# within the window, and the grant is spent. The durable half is the artifact
# record, which outlives every grant ever minted for it.
BUILD_ARTIFACT_TTL_SECONDS = 900
BUILD_ARTIFACT_TTL_MAX = 3600
BUILD_ARTIFACT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
BUILD_ARTIFACT_TOKEN_HEADER = "X-Graphwing-Artifact-Token"
BUILD_ARTIFACT_LOCKS: dict[str, threading.Lock] = {}
BUILD_ARTIFACT_LOCKS_GUARD = threading.Lock()
BUILD_ARTIFACT_GRANT_TOKENS: dict[tuple[str, str], str] = {}
# Where the evidence comment goes. This repository tracks work on GitHub
# issues, and the comment is posted by Rewst's own GitHub integration — the
# laptop never holds a GitHub token, an upload capability, or a callback URL.
BUILD_ISSUE_RE = re.compile(r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/issues/([0-9]+)$")
# One design turn, step by step. Read off the durable document exactly like the
# mechanical and visual steps: a run that died after its writer finished comes
# back and is told `checks`, not `write`.
BUILD_ITERATION_STEPS = (
    "feedback",
    "checkpoint",
    "write",
    "checks",
    "round",
    "report",
    "decision",
    "final_gates",
    "done",
    "parked",
)
# What a turn can be. `open` is notes with no writer result yet, `written` is a
# candidate on disk, `reported` is a candidate the planning session has seen.
# Only a decision closes one.
BUILD_ITERATION_TURN_STATUSES = ("open", "written", "reported", "closed")
BUILD_ITERATION_OPEN_STATUSES = frozenset({"open", "written", "reported"})
# The six answers a planning session can give, each with its own durable state.
# `continue` exists only to answer the checkpoint; the other five close a turn.
BUILD_ITERATION_DECISIONS = ("continue", "design_ready", "reframe", "split", "park", "stop")
BUILD_ITERATION_STATES = (
    "iterating", "reframed", "split", "design_ready", "parked", "stopped",
)
# Which park reason each ending writes. Distinct on purpose: "the user parked
# the design" and "the user stopped the design" are different answers, and a
# shared reason would make the receipts indistinguishable a week later.
BUILD_ITERATION_PARK_REASONS = {"park": "design_parked", "stop": "design_stopped"}
# Advisory, not a ceiling. Turn 20 pauses for a decision because twenty rounds
# of visual feedback with no one asking "is this still the same change?" is how
# a slice becomes an epic. Once answered it never fires again: the story asks
# for a checkpoint, not a limit, and re-arming it at 40 would be a limit worn
# as a question.
BUILD_ITERATION_CHECKPOINT_TURN = 20
BUILD_ITERATION_TURNS_SHOWN = 8
# Notes are stored exactly as typed, so an overlong note is refused rather than
# truncated. A truncated note is a different note, and the writer would be
# resumed with instructions the user never finished giving.
BUILD_ITERATION_NOTES_MAX = 2000
# Where a turn's notes came from. `human` is the planning session; `final_gate`
# is a late check whose fix can change what the page looks like, which the
# story says returns here rather than being quietly patched at the gate.
BUILD_ITERATION_SOURCES = ("human", "final_gate")
BUILD_ITERATION_SPLITS_MAX = 32
# The claim kind a design turn reserves the writer under. Named rather than
# spelled inline because it is the one value that decides which path an op is
# on: `initial` and `correction` stay mechanical, `design` runs on a build that
# declared visual proof whatever class it was routed as.
BUILD_DESIGN_CLAIM_KIND = "design"
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
# Workflow completion supervisor. Rewst's trigger.workflowCompleted delivers
# every terminal source run to one named reconciliation op here; this half owns
# the durable launch registry, the delivery dedupe, the retry ladder, and the
# circuit breaker. Bumping the version is a promise to migrate: a document this
# service cannot read is reported, never rewritten.
SUPERVISOR_STATE_VERSION = 1
SUPERVISOR_DIR = HOME / "supervisor"
SUPERVISOR_LOCK = threading.Lock()
SUPERVISOR_WATCHER_LOCK = threading.Lock()
SUPERVISOR_ACTIVE_WATCHERS: set[tuple[str, str]] = set()
SUPERVISOR_RECOVERY_LOCK = threading.Lock()
SUPERVISOR_RECOVERED_ROOTS: set[str] = set()
# The published listener. It filters supervised source workflows by this tag and
# must never be able to select itself.
SUPERVISOR_LISTENER_SLUG = "graphwing-build-completion-supervisor"
SUPERVISOR_TAG = "graphwing-supervised"
# A Rewst run id is the external correlation key, so it is validated as an
# opaque handle rather than parsed. `..` can never appear because the on-disk
# name is a digest of it, not the value.
SUPERVISOR_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
# Listener identity is workflow slug plus published version, e.g.
# "graphwing-build-completion-supervisor@7". Only the slug half is the dedupe
# key; the version is diagnostics, so republishing the listener cannot re-run a
# decision the previous version already made.
SUPERVISOR_LISTENER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
SUPERVISOR_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SUPERVISOR_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SUPERVISOR_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Failure codes are slugs, never text. A free-text field here is how a raw
# provider error or a stack frame ends up on disk.
SUPERVISOR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SUPERVISOR_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SUPERVISOR_LOOSE_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?$"
)
SUPERVISOR_TERMINAL_STATUSES = ("completed", "failed", "canceled", "timed_out")
SUPERVISOR_EXECUTION_MODES = ("live", "preview", "demo")
SUPERVISOR_FAILURE_CLASSES = ("transient", "deterministic", "unknown")
# The retry class is derived here and nowhere else. It is a function of the
# terminal status the trigger delivered, which the listener cannot choose:
# a timeout is transient, an operator cancel is deterministic because rerunning
# it unchanged is exactly what the operator stopped, and a plain failure is
# unknown. A caller-supplied class would be a caller-supplied retry budget.
SUPERVISOR_STATUS_CLASS = {
    "timed_out": "transient",
    "canceled": "deterministic",
    "failed": "unknown",
    "completed": "unknown",
}
# What a report says when the completion named no node. A stable label rather
# than a blank keeps the operator line readable and keeps "no node" from
# looking like a distinct node on every delivery.
SUPERVISOR_UNKNOWN_NODE = "unknown"
# Transient means the dispatch or the network dropped it, so the same call can
# work. Deterministic means the graph, the schema, or the config is wrong, and
# retrying unchanged only burns another run. Unknown gets exactly one retry:
# enough to shake off a one-off, not enough to loop.
SUPERVISOR_RETRY_BUDGET = {"transient": 3, "deterministic": 0, "unknown": 1}
SUPERVISOR_CIRCUIT_THRESHOLD = 2
SUPERVISOR_RELAUNCH_MAX = 1
SUPERVISOR_REPORTS_KEPT = 32
SUPERVISOR_RUNS_MAX = 500
SUPERVISOR_DELIVERIES_MAX = 2000
SUPERVISOR_CIRCUITS_MAX = 256
SUPERVISOR_WATCHES_MAX = 500
SUPERVISOR_TAGS_MAX = 32
SUPERVISOR_TAGS_JSON_MAX_BYTES = 8192
SUPERVISOR_OUTPUT_MAX_KEYS = 32
SUPERVISOR_STATE_MAX_BYTES = 1024 * 1024
SUPERVISOR_CONTINUATION_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
SUPERVISOR_CONTINUATION_FIELDS = frozenset(
    {
        "repo",
        "writer_prompt",
        "writer_prompt_ref",
        "fast_recipe",
        "integration_recipe",
        "reviewer",
        "class_stamp",
        "route_stamp",
    }
)
# The one supervised source workflow. The listener's continuation starts this
# slug and nothing else, and the reconcile answer names it so the topology has
# a single place the contract is written down.
SUPERVISOR_SOURCE_SLUG = "graphwing-pre-pr-build"
# Actions a retry, a relaunch, a watch handoff, a park, or an open circuit
# produce. Every one of them writes a durable report and attempts the exact
# planning pane; `receipt` and `ignored` do neither, because nothing happened
# an operator has to act on.
SUPERVISOR_REPORTED_ACTIONS = frozenset({"retry_event", "relaunch", "watch", "park", "circuit_open"})
# The actions that come with the input for one bounded source continuation.
# A watch terminates the listener run; its server-owned worker resolves or
# parks the build and never launches another source run.
SUPERVISOR_CONTINUATION_ACTIONS = frozenset({"retry_event", "relaunch"})
# Watch wake-up. The server owns this lifecycle; no callback capability is
# accepted or persisted. The loop is bounded by the registered deadline and a
# short receipt-settle window after the local job goes terminal.
SUPERVISOR_WATCH_POLL_SECONDS = float(os.environ.get("GRAPHWING_WATCH_POLL", "2") or "2")
SUPERVISOR_WATCH_SETTLE_SECONDS = 30
SUPERVISOR_WATCH_OUTCOMES = (
    "receipt",
    "job_finished_without_receipt",
    "job_missing",
    "deadline_exceeded",
)

# The listener sends `error.present`, a boolean. Anything that could carry the
# message, the body, or the stack is refused outright rather than redacted:
# redaction is a bound on accidents, not a licence to post an error blob.
SUPERVISOR_RAW_ERROR_KEYS = frozenset(
    {
        "message", "messages", "detail", "details", "description", "text", "body",
        "trace", "traceback", "stack", "stacktrace", "exception", "exceptions",
        "response", "raw", "output", "cause", "causes", "context", "payload",
    }
)
# Linear pre-PR order, used to tell "the build has not got there yet" from "the
# build moved past this event without ever recording it".
BUILD_STAGE_ORDER = ("created", "writing", "verifying", "evidence", "review", "ready", "pr_opened")
HERDR_PLAN_LABEL = os.environ.get("GRAPHWING_PLAN_TAB", "plan") or "plan"
HERDR_PANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
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
    for key in (
        "implement_slice", "visual_evidence", "visual_iteration", "pr_drive", "pr_status",
        "verify_stack",
    ):
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
                "harness_home": str(resolve_under_home(item.get("harness_home") or HOME)),
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
            # A recipe is coverage unless it says otherwise. A pre-commit hook
            # runs real commands and exits 0, so nothing downstream can tell it
            # apart from a unit suite by looking at the result — the catalog
            # entry is the only place that knows, so it is where the answer
            # lives. See BUILD_HOOK_ONLY_RECIPES for the backstop.
            "coverage": item.get("coverage") is not False,
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
        units: list[str] = []
        for raw in item.get("units") or []:
            unit = str(raw).strip()
            if unit and re.fullmatch(r"[A-Za-z0-9@_.-]{1,128}", unit):
                units.append(unit)
        for raw in item.get("ports") or []:
            try:
                p = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= p <= 65535:
                sport.append(p)
                ports.add(p)
        # An unreadable or unknown role is `unset`, not a rejection: a typo in
        # the catalog must not take every other stack down with it, and `unset`
        # already fails every check that asks for a clean stack.
        role = str(item.get("role") or "").strip().lower()
        if role not in BUILD_STACK_ROLES:
            role = "unset"
        # Where this stack serves pages, for a preview round to point a browser
        # at. Loopback only, and dropped rather than rejected when it is not:
        # a preview round with no reachable URL refuses by name later, which is
        # a better failure than a whole catalog that will not load.
        raw_base = str(item.get("base_url") or "").strip().rstrip("/")
        base_url = raw_base if raw_base and loopback_http_url(raw_base) else None
        stacks[name] = {
            "name": name,
            "cwd": cwd,
            "compose_file": str(item.get("compose_file") or "docker-compose.yml"),
            "health": health,
            "ports": sport,
            "units": units,
            "role": role,
            "base_url": base_url,
            "runner": str(item.get("runner") or "compose").strip().lower(),
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
        launcher, model = "grok", "grok-4.6"
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


CANONICAL_HARNESS_MODELS = {
    "openai": {},
    "anthropic": {"sonnet": "claude-sonnet-5", "opus": "claude-opus-5", "claude-fable-5": "claude-fable-5"},
    "xai": {"grok-4.6": "grok-4.6-build"},
}


def harness_canonical_model(provider: str, model: str) -> str:
    return CANONICAL_HARNESS_MODELS.get(provider, {}).get(model, model)


def _native_error(code: str) -> tuple[str, None, str]:
    return "", None, code


def codex_session_metadata(thread_id: str) -> dict[str, Any] | None:
    """Select one Codex session by its emitted thread id, never latest/time."""
    root = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"
    matches: list[list[dict[str, Any]]] = []
    try:
        paths = root.rglob("*.jsonl")
        for path in paths:
            rows = []
            for line in path.read_text(errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            if any(isinstance(r.get("payload"), dict) and r.get("payload", {}).get("session_id", r.get("payload", {}).get("id")) == thread_id
                   for r in rows):
                matches.append(rows)
    except OSError:
        return None
    if len(matches) != 1:
        return None
    rows = matches[0]
    meta = next((r.get("payload") for r in rows if r.get("type") == "session_meta" and isinstance(r.get("payload"), dict)), None)
    context = next((r.get("payload") for r in rows if r.get("type") == "turn_context" and isinstance(r.get("payload"), dict)), None)
    complete = next(
        (
            r.get("payload")
            for r in reversed(rows)
            if isinstance(r.get("payload"), dict)
            and (
                r.get("type") == "task_complete"
                or (r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "task_complete")
            )
        ),
        None,
    )
    if not isinstance(meta, dict) or not isinstance(context, dict) or not isinstance(complete, dict):
        return None
    return {"session_id": meta.get("session_id", meta.get("id")), "originator": meta.get("originator"),
            "source": meta.get("source"), "model_provider": meta.get("model_provider"),
            "turn_id": context.get("turn_id"), "model": context.get("model"),
            "complete_turn_id": complete.get("turn_id"), "last_agent_message": complete.get("last_agent_message")}


def native_harness_receipt(
    launcher: str, stdout: str, requested_model: str, expected_session: str | None,
    meta_reader: Callable[[str], dict[str, Any] | None] = codex_session_metadata,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Validate a native harness shape and only then normalize it for Graphwing."""
    provider = {"codex": "openai", "claude": "anthropic", "grok": "xai"}.get(launcher)
    if not provider or not requested_model:
        return _native_error("missing_transport_provenance")
    canonical = harness_canonical_model(provider, requested_model)
    try:
        if launcher == "claude":
            raw = json.loads(stdout)
            usage = raw.get("usage") if isinstance(raw, dict) else None
            models = raw.get("modelUsage") if isinstance(raw, dict) else None
            if not isinstance(raw, dict) or raw.get("session_id") != expected_session:
                return _native_error("unrelated_transport_receipt")
            if raw.get("is_error") is not False or raw.get("terminal_reason") != "completed" or raw.get("subtype") != "success":
                return _native_error("fallback_or_incomplete_transport")
            if not isinstance(raw.get("result"), str) or not isinstance(raw.get("num_turns"), int) or raw["num_turns"] < 1:
                return _native_error("malformed_transport_provenance")
            if not isinstance(usage, dict) or not isinstance(usage.get("iterations"), list) or not usage["iterations"]:
                return _native_error("malformed_transport_provenance")
            if not isinstance(models, dict) or len(models) != 1:
                return _native_error("multiple_or_incomplete_transport")
            identity = next(iter(models.values()))
            if not isinstance(identity, dict) or identity.get("provider") != "firstParty" or identity.get("canonicalModel") != canonical:
                return _native_error("unexpected_execution")
            return raw["result"], {"transport": launcher, "session_id": expected_session, "provider": provider,
                "model": requested_model, "canonical_model": canonical, "native_receipt": raw, "fallback_disabled": True}, None
        if launcher == "grok":
            raw = json.loads(stdout)
            models = raw.get("modelUsage") if isinstance(raw, dict) else None
            if not isinstance(raw, dict) or raw.get("sessionId") != expected_session:
                return _native_error("unrelated_transport_receipt")
            if raw.get("stopReason") != "end_turn" or not isinstance(raw.get("requestId"), str) or not raw["requestId"]:
                return _native_error("fallback_or_incomplete_transport")
            if not isinstance(raw.get("text"), str) or not isinstance(raw.get("num_turns"), int) or raw["num_turns"] < 1:
                return _native_error("malformed_transport_provenance")
            if not isinstance(models, dict) or list(models) != [canonical]:
                return _native_error("multiple_or_incomplete_transport")
            identity = models[canonical]
            if not isinstance(identity, dict) or not isinstance(identity.get("modelCalls"), int) or identity["modelCalls"] < 1:
                return _native_error("malformed_transport_provenance")
            return raw["text"], {"transport": launcher, "session_id": expected_session, "provider": provider,
                "model": requested_model, "canonical_model": canonical, "native_receipt": raw, "fallback_disabled": True}, None
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        started = [e for e in events if isinstance(e, dict) and e.get("type") == "thread.started" and isinstance(e.get("thread_id"), str)]
        turns = [e for e in events if isinstance(e, dict) and e.get("type") == "turn.started"]
        completed = [e for e in events if isinstance(e, dict) and e.get("type") == "turn.completed"]
        messages = [e.get("item") for e in events if isinstance(e, dict) and e.get("type") == "item.completed" and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message" and isinstance(e["item"].get("text"), str)]
        if len(started) != 1 or len(turns) != 1 or len(completed) != 1 or len(messages) != 1:
            return _native_error("malformed_transport_provenance")
        thread_id = started[0]["thread_id"]
        if expected_session not in (None, "", thread_id):
            return _native_error("unrelated_transport_receipt")
        meta = meta_reader(thread_id)
        if not isinstance(meta, dict) or any(meta.get(k) != v for k, v in {
            "session_id": thread_id, "originator": "codex_exec", "source": "exec", "model_provider": "openai", "model": canonical,
        }.items()):
            return _native_error("unrelated_transport_receipt")
        if not isinstance(meta.get("turn_id"), str) or meta.get("complete_turn_id") != meta["turn_id"] or meta.get("last_agent_message") != messages[0]["text"]:
            return _native_error("malformed_transport_provenance")
        return messages[0]["text"], {"transport": launcher, "session_id": thread_id, "provider": provider,
            "model": requested_model, "canonical_model": canonical, "native_receipt": {"events": events, "session_meta": meta}, "fallback_disabled": True}, None
    except (ValueError, TypeError):
        return _native_error("malformed_transport_provenance")


def review_transport_provenance(transport: str, receipt_path: Path, attempt_id: str, provider: str, model: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        return None, "missing_transport_provenance"
    if not isinstance(receipt, dict) or receipt.get("transport") != transport:
        return None, "malformed_transport_provenance"
    expected = {"openai": "codex", "anthropic": "claude", "xai": "grok"}.get(provider)
    if expected != transport or (receipt.get("provider"), receipt.get("model")) != (provider, model):
        return None, "unexpected_execution"
    if receipt.get("canonical_model") != harness_canonical_model(provider, model) or not receipt.get("session_id"):
        return None, "unrelated_transport_receipt"
    if transport != "codex" and receipt.get("session_id") != str(uuid.UUID(hex=attempt_id)):
        return None, "unrelated_transport_receipt"
    return receipt, None

def review_result(
    reviewer: str, prompt: str, resolved: Path,
    requested_provider: str | None = None, requested_model: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Run one spec-review to completion and return its verdict payload."""
    diff = git_diff(resolved, "HEAD", None)
    diff_text = str(diff.get("diff") or "")[:12000]
    body_prompt = (
        "Spec-review only. Do not edit files, commit, or push.\n"
        "Return exactly:\nVERDICT: PASS\nor\nVERDICT: NACK\n"
        f"Ticket:\n{prompt[:8000]}\n\nDiff:\n{diff_text}\n"
    )
    provider = requested_provider or BUILD_REVIEWER_PROVIDER.get(reviewer)
    model = requested_model or BUILD_REVIEWER_MODEL.get(reviewer)
    attempt_id = attempt_id or uuid.uuid4().hex
    receipt_path = job_dir(attempt_id) / "transport-provenance.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.unlink(missing_ok=True)
    transport = {"openai": "codex", "anthropic": "claude", "xai": "grok"}.get(provider)
    binary = {"codex": CODEX_BIN, "claude": CLAUDE_BIN, "grok": GROK_BIN}.get(transport or "")
    if not transport or not model or not binary or not binary.is_file():
        return {"ok": False, "verdict": None, "no_verdict": True, "reviewer": reviewer,
                "error": "requested provider/model direct harness is unavailable", "code": "unavailable_provider",
                "executed_provider": None, "executed_model": None}
    session_id = None if transport == "codex" else str(uuid.UUID(hex=attempt_id))
    if transport == "claude":
        cmd = [str(CLAUDE_BIN), "-p", "--output-format", "json", "--permission-mode", "plan",
               "--max-turns", str(REVIEW_MAX_TURNS), "--model", model, "--session-id", str(session_id), body_prompt]
    elif transport == "grok":
        cmd = [str(GROK_BIN), "-p", body_prompt, "--output-format", "json", "--permission-mode", "plan",
               "--no-subagents", "--max-turns", str(REVIEW_MAX_TURNS), "--model", model,
               "--session-id", str(session_id), "--cwd", str(resolved)]
    else:
        # Codex's JSONL trace is the direct OpenAI harness receipt.  It does
        # not offer a fallback option and `--json` keeps every event available
        # for validation; a missing bound session is terminal below.
        cmd = [str(CODEX_BIN), "exec", "--json", "--sandbox", "read-only", "--cd", str(resolved),
               "--model", model, body_prompt]
    env = harness_job_env({"job_id": attempt_id, "cwd": str(resolved)})
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
                "error": "review timed out", "code": "timeout", "reviewer": reviewer,
                "executed_provider": None, "executed_model": None}
    text = (proc.stdout or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        return {"ok": False, "verdict": None, "no_verdict": True, "reviewer": reviewer,
                "error": "review transport exited nonzero", "code": "transport_exit",
                "returncode": proc.returncode, "executed_provider": None, "executed_model": None}
    text, harness_receipt, native_error = native_harness_receipt(transport, text, model, session_id)
    if native_error:
        return {"ok": False, "verdict": None, "no_verdict": True, "reviewer": reviewer,
                "error": native_error, "code": native_error, "executed_provider": None, "executed_model": None}
    assert harness_receipt is not None
    receipt_path.write_text(json.dumps(harness_receipt))
    provenance, provenance_error = review_transport_provenance(transport, receipt_path, attempt_id, provider, model)
    if provenance_error:
        return {"ok": False, "verdict": None, "no_verdict": True, "reviewer": reviewer,
                "error": provenance_error, "code": provenance_error, "returncode": proc.returncode,
                "executed_provider": None, "executed_model": None}
    verdict, summary = parse_review_verdict(text)
    return {
        "ok": verdict == "PASS",
        "verdict": verdict,
        "no_verdict": review_said_nothing(text),
        "reviewer": reviewer,
        # The raw transcript, so a caller that needs a stricter reading than
        # this one can do it without running the reviewer twice. Never stored:
        # build_review_result keeps only the bounded summary and compact.
        "text": text,
        "summary": summary,
        "compact": compact_cmd_signal({"ok": verdict == "PASS", "stdout": text, "stderr": ""}),
        "returncode": proc.returncode,
        # These are the completed launcher's immutable command axes, not a
        # reviewer alias or model-generated prose.  A caller still compares
        # them to its requested axes before a verdict can count.
        "executed_provider": provenance["provider"],
        "executed_model": provenance["model"],
        "fallback_disabled": provenance["fallback_disabled"],
        "transport_provenance": provenance,
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
    result = review_result(job["reviewer"], job["prompt"], Path(job["cwd"]), attempt_id=job_id)
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
    supervised_source_run_id, supervised_err = parse_supervised_source_run_id(data)
    if supervised_err:
        return 400, supervised_err

    if not webhook_url and supervised_source_run_id is None:
        result = review_result(reviewer, prompt, resolved)
        if result.get("code") == "not_implemented":
            return 501, result
        if result.get("code") == "timeout":
            return 504, result
        return 200, result

    job_id = uuid.uuid4().hex
    job = {
            "job_id": job_id,
            "kind": "review",
            "status": "queued",
            "reviewer": reviewer,
            "attempt_id": job_id,
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
        "supervised_source_run_id": supervised_source_run_id,
    }
    persist_err = persist_new_job(job, supervised_source_run_id)
    if persist_err:
        return int(persist_err.get("status") or 409), persist_err
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


def strict_url_authority(parsed: Any, host_pattern: str, allowed_ports: set[int | None] | None) -> bool:
    """Validate the raw authority instead of trusting urllib's normalized hostname."""
    authority = parsed.netloc
    if not isinstance(authority, str) or not authority or not authority.isascii():
        return False
    match = re.fullmatch(rf"(?P<host>{host_pattern})(?::(?P<port>[0-9]{{1,5}}))?", authority, re.IGNORECASE)
    if match is None:
        return False
    raw_port = match.group("port")
    port = int(raw_port) if raw_port is not None else None
    if port is not None and not 1 <= port <= 65535:
        return False
    return allowed_ports is None or port in allowed_ports


def loopback_http_url(url: str) -> bool:
    """True for an http(s) URL on this machine's loopback interface.

    The preview stack a visual round drives runs here, so its URL is a laptop
    URL. Anything else is either a remote environment this build has no claim
    about or a way to point the browser somewhere the operator never declared.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and strict_url_authority(parsed, r"(?:127\.0\.0\.1|localhost|\[::1\])", None)
        and not parsed.query
        and not parsed.fragment
        and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url)
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url_origin(parsed: Any) -> tuple[str, str, int | None]:
    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme == "http" else 443 if parsed.scheme == "https" else None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def probe_loopback(url: str, allow_login: bool = False) -> dict[str, Any]:
    try:
        parsed = urlparse(url)
        origin = _url_origin(parsed)
    except ValueError:
        return {"ok": False, "url": url, "error": "health url is malformed"}
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in ("http", "https")
        or host not in ("127.0.0.1", "localhost", "::1")
        or parsed.username
        or parsed.password
    ):
        return {"ok": False, "url": url, "error": "health url must be loopback http(s)"}
    try:
        req = Request(url, method="GET")
        with build_opener(_NoRedirect()).open(req, timeout=2) as resp:
            code = getattr(resp, "status", 200)
            return {"ok": 200 <= code < 300, "url": url, "status": code,
                    "final_location": url}
    except HTTPError as exc:
        try:
            location = str(exc.headers.get("Location") or "")
            final_location = urljoin(url, location) if location else None
            try:
                target = urlparse(final_location or "")
                same_origin = _url_origin(target) == origin
            except ValueError:
                same_origin = False
                target = urlparse("")
            login_path = same_origin and any(
                marker in target.path.lower() for marker in ("login", "signin", "auth")
            )
            login = bool(
                allow_login
                and exc.code in (301, 302, 303, 307, 308)
                and same_origin
                and target.hostname in ("127.0.0.1", "localhost", "::1")
                and not target.username
                and not target.password
                and login_path
            )
            return {"ok": login, "url": url, "status": exc.code,
                    "final_location": final_location,
                    "login_redirect": final_location if login else None,
                    "error": None if login else f"HTTP {exc.code}"}
        finally:
            exc.close()
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
        # Reported so a caller can see what the catalog declares this stack is
        # for. Health says a stack is up; only the role says whether a pass on
        # it is evidence about a clean checkout.
        "role": spec.get("role") or "unset",
        "healthy": bool(running) and health_ok,
        "containers": containers,
        "health": health,
        "compose_ok": bool(r.get("ok")),
        "compose_error": None if r.get("ok") else (r.get("error") or "compose ps failed"),
    }


def port_observation(port: int) -> dict[str, Any]:
    result = run_cmd(["ss", "-ltnH", f"sport = :{port}"], timeout=5)
    return {"ok": bool(result.get("ok")), "listening": bool((result.get("stdout") or "").strip()) if result.get("ok") else None,
            "error": None if result.get("ok") else str(result.get("error") or "ss failed")[:BUILD_REDACT_MAX_STRING]}


def port_listening(port: int) -> bool:
    observation = port_observation(port)
    return observation.get("listening") is True


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


def herdr_send(pane_id: str, text: str) -> dict[str, Any]:
    return run_cmd(["herdr", "--session", HERDR_SESSION, "pane", "send-text", pane_id, text], timeout=8)


def herdr_exact_pane(pane_id: str) -> bool:
    listed = herdr_cli(["pane", "list"]) or {}
    return any(
        isinstance(pane, dict) and str(pane.get("pane_id") or "") == pane_id
        for pane in listed.get("panes") or []
    )


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


def herdr_pane_known(pane_id: str) -> tuple[bool, str]:
    """Is this the exact pane the saved planning session is on?

    The registration names one pane and supervision reports to that pane or to
    nowhere. Checking it at registration is the only moment a typo is cheap:
    afterwards the build is running and a report that went to the graph
    dashboard instead is a report the planner never saw.

    A host with no human surface has nothing to check against, so the pane is
    taken at face value there and every later delivery against it is recorded
    as attempted-and-failed. That is louder than a silent redirect, which is
    the outcome this whole path exists to prevent.
    """
    if not herdr_enabled():
        return True, ""
    panes = herdr_cli(["pane", "list"])
    if panes is None:
        return False, "plan_pane_unverifiable"
    for pane in panes.get("panes") or []:
        if isinstance(pane, dict) and str(pane.get("pane_id") or "") == pane_id:
            return True, ""
    return False, "unknown_plan_pane"


def herdr_plan_deliver(pane_id: str | None, line: str) -> dict[str, Any]:
    """Post one supervisor outcome to the exact planning pane, and say what happened.

    There is no fallback pane. An earlier draft walked the `plan` tab label and
    then the graph dashboard when the saved pane was gone, which reads as
    robustness and is not: the operator who opened the build stops getting
    reports and nothing anywhere says so. Delivery is therefore a fact with a
    receipt -- attempted, ok, and a code -- and the caller stores it next to
    the report it belongs to.

    Run inline rather than on a thread because the receipt has to reach disk in
    the same write as the report. A backgrounded delivery whose failure lands
    after the state was saved is exactly the silent redirect in another shape.
    """
    receipt: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "pane_id": pane_id,
        "code": None,
        "at": utcnow(),
    }
    if not pane_id:
        receipt["code"] = "missing_plan_pane"
        return receipt
    if not herdr_enabled():
        receipt["code"] = "herdr_disabled"
        return receipt
    receipt["attempted"] = True
    try:
        with HERDR_LOCK:
            known, code = herdr_pane_known(pane_id)
            if not known:
                receipt["code"] = code
                return receipt
            safe = "".join(c if (c.isprintable() and c != "'") else " " for c in line).strip()[:240]
            sent = run_cmd(
                [
                    "herdr", "--session", HERDR_SESSION, "pane", "send-text", pane_id,
                    f"printf '%s\\n' '{utcnow()} {safe}'\n",
                ],
                timeout=8,
            )
            if not sent.get("ok"):
                receipt["code"] = "plan_pane_unreachable"
                return receipt
            herdr_report(pane_id, HERDR_PLAN_LABEL, "idle", line[:120])
    except Exception:
        receipt["code"] = "plan_pane_unreachable"
        return receipt
    receipt["ok"] = True
    return receipt


def plan_delivery_skipped(pane_id: str | None) -> dict[str, Any]:
    """The receipt for an outcome nobody has to act on.

    `receipt` and `ignored` are not reported, so recording them as a failed
    delivery would fill the operator's report with failures that never had a
    message behind them.
    """
    return {"attempted": False, "ok": True, "pane_id": pane_id, "code": "not_reported", "at": utcnow()}


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
    """Swap a finished writer tail to the recorded direct harness session."""
    if HERDR_TAB_LINGER_SECONDS <= 0:
        return
    pane_id = str(job.get("herdr_pane_id") or "")
    session = job.get("harness_session")
    if not pane_id or not isinstance(session, str) or not HARNESS_SESSION_RE.fullmatch(session):
        return
    cwd = str(job.get("cwd") or HOME)
    launcher = str(job.get("launcher") or "")
    binary = {"codex": CODEX_BIN, "claude": CLAUDE_BIN, "grok": GROK_BIN}.get(launcher)
    if not binary:
        return
    # Ctrl-C stops tail -F before the pane runs anything else.
    herdr_send(pane_id, "\x03")
    if launcher == "codex":
        command = f"{binary} resume '{session}'"
    elif launcher == "claude":
        command = f"{binary} --resume '{session}'"
    else:
        command = f"{binary} --resume '{session}' --cwd '{cwd}'"
    herdr_send(pane_id, command + "\n")


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
        "harness_session",
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


def parse_supervised_source_run_id(data: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    raw = data.get("supervised_source_run_id")
    if raw in (None, ""):
        return None, None
    if not isinstance(raw, str) or not SUPERVISOR_RUN_ID_RE.fullmatch(raw.strip()):
        return None, {
            "error": "supervised_source_run_id must be a registered Rewst run id",
            "code": "bad_source_run_id",
        }
    return raw.strip(), None


def persist_new_job(
    job: dict[str, Any],
    supervised_source_run_id: str | None,
    prompt_contents: str | None = None,
) -> dict[str, Any] | None:
    """Persist and bind a job before any worker can observe the queue."""

    def write_new_job() -> dict[str, Any] | None:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return {"error": "too many in-flight agent jobs", "code": "busy", "status": 429}
        job_dir(job["job_id"]).mkdir(parents=True, exist_ok=True)
        if prompt_contents is not None:
            (job_dir(job["job_id"]) / "prompt.txt").write_text(prompt_contents)
        write_job(job)
        return None

    if supervised_source_run_id is None:
        with JOB_LOCK:
            return write_new_job()
    # Keep the same lock order as reconciliation, which enters supervisor state
    # before reading a local job. Binding failure never exposes queued work.
    with SUPERVISOR_LOCK:
        with JOB_LOCK:
            err = write_new_job()
            if err:
                return err
        bind_err = supervisor_bind_job_locked(supervised_source_run_id, job["job_id"])
        if bind_err:
            with JOB_LOCK:
                failed = read_job(job["job_id"]) or job
                failed["status"] = "failed"
                failed["finished_at"] = utcnow()
                failed["error"] = str(bind_err.get("error") or "supervisor binding failed")
                write_job(failed)
            return bind_err
    return None


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


def parse_harness_session(data: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    raw = data.get("harness_session")
    if raw in ("", None):
        return None, None
    if not isinstance(raw, str):
        return None, {"error": "harness_session must be a canonical UUID", "code": "bad_harness_session"}
    name = raw.strip()
    if not HARNESS_SESSION_RE.fullmatch(name):
        return None, {"error": "harness_session must be a canonical UUID", "code": "bad_harness_session"}
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
        "harness_session": job.get("harness_session"),
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


def harness_job_env(job: dict[str, Any]) -> dict[str, str]:
    """Graph cwd wins over inherited terminal state for direct harnesses."""
    cwd = str(Path(job["cwd"]).resolve())
    env = {k: v for k, v in os.environ.items() if k not in ("TERMINAL_CWD", "PWD")}
    env.update(
        {
            "GRAPHWING_JOB_ID": str(job["job_id"]),
            "GIT_TERMINAL_PROMPT": "0",
            "GH_PROMPT_DISABLED": "1",
            "TERMINAL_CWD": cwd,
            "PWD": cwd,
        }
    )
    return env


def spawn_harness(job: dict[str, Any]) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    """Launch exactly one configured provider harness, never a broker."""
    launcher = str(job.get("launcher") or "")
    binary = {"codex": CODEX_BIN, "claude": CLAUDE_BIN, "grok": GROK_BIN}.get(launcher)
    if not binary or not binary.is_file():
        return None, {"error": "configured direct harness is unavailable", "code": "unavailable_provider"}
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
    env = harness_job_env(job)
    session = job.get("harness_session")
    if launcher != "codex" and (not isinstance(session, str) or not HARNESS_SESSION_RE.fullmatch(session)):
        return None, {"error": "missing preassigned harness session", "code": "bad_harness_session"}
    model = str(job.get("model") or "")
    if not model:
        return None, {"error": "missing configured model", "code": "unavailable_provider"}
    resume_session = job.get("claim_kind") in ("correction", "design")
    if launcher == "claude":
        cmd = [str(CLAUDE_BIN), "-p", "--output-format", "json", "--dangerously-skip-permissions",
               "--max-turns", str(job["max_turns"]), "--add-dir", cwd, "--model", model]
        cmd.extend(["--resume", str(session)] if resume_session else ["--session-id", str(session)])
        cmd.append(prompt)
    elif launcher == "grok":
        cmd = [str(GROK_BIN), "--prompt-file", str(prompt_path), "--output-format", "json",
               "--permission-mode", "acceptEdits", "--no-subagents", "--max-turns", str(job["max_turns"]),
               "--model", model]
        cmd.extend(["--resume", str(session)] if resume_session else ["--session-id", str(session)])
        cmd.extend(["--cwd", cwd])
    else:
        if isinstance(session, str) and HARNESS_SESSION_RE.fullmatch(session):
            cmd = [str(CODEX_BIN), "exec", "resume", "--json", "--model", model, session, prompt]
        else:
            cmd = [str(CODEX_BIN), "exec", "--json", "--sandbox", "workspace-write", "--cd", cwd,
                   "--model", model, prompt]
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
    return spawn_harness(job)


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


def harness_terminal_output(job: dict[str, Any], stdout: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """Validate native direct-harness output before accepting its final message."""
    launcher = str(job.get("launcher") or "")
    provider = {"codex": "openai", "claude": "anthropic", "grok": "xai"}.get(launcher)
    model = str(job.get("model") or "")
    raw_session = job.get("harness_session")
    session = raw_session if isinstance(raw_session, str) else None
    if not provider or not model or (launcher != "codex" and (not session or not HARNESS_SESSION_RE.fullmatch(session))):
        return "", None, "missing_transport_provenance"
    return native_harness_receipt(launcher, stdout, model, session)


def run_agent_job(job_id: str) -> None:
    # A launch replay may schedule the same durable queued job again after the
    # original process died between write_job and enqueue_agent. Claim the job
    # under the same lock that guards creation so only one scheduled thread can
    # cross into the process spawn.
    with JOB_LOCK:
        job = read_job(job_id)
        if not job or job.get("status") != "queued":
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
    harness_text, provenance, provenance_error = harness_terminal_output(job, stdout)
    if provenance is not None and job.get("launcher") == "codex":
        # The JSONL's emitted thread id is the only initial Codex session
        # authority. Bind it while this durable job is still exclusively
        # running; a different preexisting value is an unrelated receipt.
        generated = provenance.get("session_id")
        if not isinstance(generated, str) or not HARNESS_SESSION_RE.fullmatch(generated):
            provenance_error = "malformed_transport_provenance"
            provenance = None
        else:
            with JOB_LOCK:
                current = read_job(job_id) or job
                prior = current.get("harness_session")
                if prior not in (None, "", generated):
                    provenance_error = "unrelated_transport_receipt"
                    provenance = None
                else:
                    current["harness_session"] = generated
                    current["harness_session_state"] = "bound"
                    write_job(current)
                    job = current
            build_id = job.get("build_id")
            claim_id = job.get("claim_id")
            if isinstance(build_id, str) and isinstance(claim_id, str):
                with BUILD_LOCK:
                    doc = read_build(build_id)
                    claim = build_claim(doc) if isinstance(doc, dict) else None
                    if not isinstance(doc, dict) or not isinstance(claim, dict) or claim.get("claim_id") != claim_id:
                        provenance_error = "unrelated_transport_receipt"
                        provenance = None
                    elif claim.get("writer_session") not in (None, "", generated):
                        provenance_error = "unrelated_transport_receipt"
                        provenance = None
                    else:
                        claim["writer_session"] = generated
                        doc["writer_session"] = generated
                        write_build(doc, build_id)
    parsed = parse_receipt_text(harness_text) if provenance_error is None else None
    if provenance_error is not None:
        parsed = {"status": "error", "summary": provenance_error}
    receipt = normalize_receipt(job, parsed, returncode, timed_out)
    if provenance_error is not None:
        receipt["status"] = "error"
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["returncode"] = returncode
    job["transport_provenance"] = provenance
    job["receipt"] = receipt
    job["status"] = "completed" if receipt["status"] == "ok" else "failed"
    if timed_out:
        job["error"] = "agent run budget exceeded"
    elif receipt["status"] != "ok" and not job.get("error"):
        job["error"] = receipt.get("summary") or f"direct harness exited {returncode}"
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
    supervised_source_run_id, supervised_err = parse_supervised_source_run_id(data)
    if supervised_err:
        return 400, supervised_err
    timeout = int(spec["timeout_seconds"])
    async_run = bool(spec["async"]) or timeout > SCRIPT_SYNC_TIMEOUT or supervised_source_run_id is not None
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
        "supervised_source_run_id": supervised_source_run_id,
    }
    persist_err = persist_new_job(job, supervised_source_run_id)
    if persist_err:
        return int(persist_err.get("status") or 409), persist_err
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
    harness_home = Path(str(seat.get("harness_home") or HOME)).resolve()
    if profile != HOME_PROFILE or harness_home != HOME.resolve():
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
    supervised_source_run_id, supervised_err = parse_supervised_source_run_id(data)
    if supervised_err:
        return 400, supervised_err
    session, session_err = parse_harness_session(data)
    if session_err:
        return 400, session_err
    launcher = str(data.get("launcher") or "grok").strip()
    if launcher not in {"codex", "claude", "grok"}:
        return 400, {"error": "launcher must be codex, claude, or grok", "code": "bad_launcher"}
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
    binary = {"codex": CODEX_BIN, "claude": CLAUDE_BIN, "grok": GROK_BIN}[launcher]
    if not binary.is_file():
        return 501, {"error": "configured direct harness is unavailable", "code": "unavailable_provider"}
    job_id = uuid.uuid4().hex
    log_ref = str(job_dir(job_id) / "stdout.log")
    harness_session = session or (None if launcher == "codex" else str(uuid.UUID(hex=job_id)))
    job = {
        "job_id": job_id,
        "kind": "agent",
        "status": "queued",
        "profile": profile,
        "repo": repo_name,
        "cwd": str(resolved),
        "prompt": prompt,
        "harness_session": harness_session,
        "harness_session_state": "pending" if launcher == "codex" and harness_session is None else "bound",
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
        "supervised_source_run_id": supervised_source_run_id,
    }
    persist_err = persist_new_job(
        job, supervised_source_run_id, wrap_prompt(job_id, prompt, str(resolved))
    )
    if persist_err:
        return int(persist_err.get("status") or 409), persist_err
    enqueue_agent(job)
    return 202, {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "profile": profile,
        "repo": repo_name,
        "harness_session": harness_session,
        "harness_session_state": "pending" if launcher == "codex" and harness_session is None else "bound",
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
        "base_head": doc.get("base_head"),
        "checks": doc.get("checks") or {},
        "pr_ready": doc.get("pr_ready"),
        "pending_finalize": doc.get("pending_finalize"),
        "writer_claim": doc.get("writer_claim"),
        "writer_result": doc.get("writer_result"),
        "gates": doc.get("gates") or [],
        "index": doc.get("index"),
        "ticket": doc.get("ticket"),
        "rollout": build_rollout_public(doc),
        "route": doc.get("route"),
        "verification": doc.get("verification"),
        "writer_session": doc.get("writer_session"),
        "stacks": doc.get("stacks") or [],
        "reviews": doc.get("reviews") or [],
        "evidence_rounds": build_visual_history_public(doc),
        # The round a caller is currently in the middle of, flattened. The full
        # list stays above as history: a superseded round is never removed, so
        # "which one is live" cannot be read off the length.
        "visual_round": build_visual_round_public(doc),
        # The design conversation over those rounds: which turn is open, what
        # the planning session last decided, and the three strike ledgers a
        # design turn is not allowed to move. None until the first turn.
        "iteration": build_iteration_public(doc),
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


def build_visual_round_summary(rnd: dict[str, Any]) -> dict[str, Any]:
    """Project one durable evidence round onto its path-free public shape."""
    review = rnd.get("review") or {}
    inspection = rnd.get("inspection") or {}
    published = rnd.get("published") or {}
    captures = rnd.get("captures") if isinstance(rnd.get("captures"), list) else rnd.get("shots")
    if not isinstance(captures, list):
        captures = []
    return {
        "round": rnd.get("round", rnd.get("n")),
        "status": rnd.get("status"),
        "change_id": rnd.get("change_id"),
        "supersedes": rnd.get("supersedes"),
        "human_review": rnd.get("human_review"),
        "preview": {
            k: (rnd.get("preview") or {}).get(k) for k in ("preview_id", "url", "healthy")
        }
        if isinstance(rnd.get("preview"), dict)
        else None,
        "captures": len(captures),
        "verified": len(
            [c for c in captures if isinstance(c, dict) and c.get("verified")]
        ),
        "inspection_ok": inspection.get("ok"),
        "recapture": inspection.get("recapture") or [],
        "review_verdict": review.get("verdict"),
        "review_findings": review.get("findings"),
        "published_at": published.get("commented_at") or published.get("at"),
        "comment_url": published.get("comment_url"),
        "prompted_at": rnd.get("prompted_at"),
    }


def build_visual_history_public(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only the most recent bounded window of compact round summaries."""
    rounds = [r for r in (doc.get("evidence_rounds") or []) if isinstance(r, dict)]
    return [build_visual_round_summary(rnd) for rnd in rounds[-BUILD_EVIDENCE_ROUNDS_KEPT:]]


def build_visual_round_public(doc: dict[str, Any]) -> dict[str, Any] | None:
    """A compact view of the latest round, or None when there is none.

    Deliberately small. The stored round carries every image's path, digest and
    inspection reasons, and putting all of that in every state response is how a
    poll loop starts moving megabytes to learn a round number.
    """
    rounds = [r for r in (doc.get("evidence_rounds") or []) if isinstance(r, dict)]
    return build_visual_round_summary(rounds[-1]) if rounds else None


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
    # `requires_red` and `path_rules` are folded in only when the caller
    # declared them. Defaulting them into every parsed block would change the
    # dict that build_create fingerprints, and a build opened before they
    # existed would answer its own retried create with an idempotency
    # conflict rather than the receipt it already has.
    if "requires_red" in raw:
        red, red_err = parse_strict_bool(raw, "requires_red", "bad_verification")
        if red_err:
            return None, red_err
        out["requires_red"] = red
    if "path_rules" in raw:
        rules, rules_err = parse_build_path_rules(raw["path_rules"])
        if rules_err:
            return None, rules_err
        out["path_rules"] = rules
    if "visual" in raw:
        contract, contract_err = parse_build_visual(raw["visual"])
        if contract_err:
            return None, contract_err
        out["visual"] = contract
    return out, None


def parse_build_rollout(container: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Parse the explicit ticket-07 proof overlay, or leave ordinary builds alone.

    A rollout proof is opt-in because this catalog also serves bounded normal
    pre-PR builds.  Once selected it has exactly one tracker identity; accepting
    a free-form tracker would revive the stale Shortcut evidence path.
    """
    if "rollout" not in container:
        return None, None
    raw = container.get("rollout")
    if not isinstance(raw, dict):
        return None, {"error": "rollout must be an object", "code": "bad_rollout"}
    if set(raw) - {"ticket"}:
        return None, {"error": "rollout contains an unsupported field", "code": "bad_rollout"}
    if raw.get("ticket") != BUILD_ROLLOUT_TICKET:
        return None, {
            "error": "ticket-07 rollout proof is bound to GitHub issue #52",
            "code": "bad_rollout_ticket",
        }
    return {"ticket": BUILD_ROLLOUT_TICKET, "status": "open", "receipts": [], "identity": None}, None


def parse_build_path_rules(raw: Any) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Changed-path rules: a glob, and the recipes it makes mandatory.

    Add-only by construction. A rule contributes names to the required set
    and has no way to name one for removal, so a plan cannot use a path rule
    to quietly drop a check it declared -- which is the whole reason the rule
    is a list of recipes rather than a replacement verification block.
    """
    bad = {"error": "verification.path_rules must be a list of {match, recipes}", "code": "bad_verification"}
    if not isinstance(raw, list):
        return None, bad
    if len(raw) > BUILD_PATH_RULES_MAX:
        return None, {"error": "verification.path_rules is too long", "code": "bad_verification"}
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None, bad
        match = item.get("match")
        recipes = item.get("recipes")
        if not isinstance(match, str) or not match.strip():
            return None, bad
        if not isinstance(recipes, list) or not recipes:
            return None, bad
        if not all(isinstance(n, str) and n.strip() for n in recipes):
            return None, bad
        if len(recipes) > BUILD_VERIFICATION_MAX:
            return None, {"error": "verification.path_rules has too many recipes", "code": "bad_verification"}
        rule = {
            "match": match.strip()[:BUILD_NAME_MAX],
            "recipes": [n.strip()[:BUILD_NAME_MAX] for n in recipes],
        }
        # The phase is preserved when the caller declared one and validated
        # here, but never defaulted into the stored rule. Writing a default in
        # would change the verification dict build_create fingerprints, so a
        # build opened before this field existed would answer its own retried
        # create with an idempotency conflict instead of its receipt. Absent is
        # read as `fast` at the point of use, by build_rule_phase.
        if "phase" in item:
            phase = item["phase"]
            if not isinstance(phase, str) or phase.strip() not in BUILD_CHECK_PHASES:
                return None, {
                    "error": "verification.path_rules[].phase must be fast or integration",
                    "code": "bad_verification",
                }
            rule["phase"] = phase.strip()
        out.append(rule)
    return out, None


def parse_build_visual_steps(
    raw: Any, field: str, minimum: int
) -> tuple[list[str] | None, dict[str, Any] | None]:
    """One list of human instructions: setup, navigation, expected, checklist.

    Bounded and secret-checked because every one of these is written down and
    then read out loud — into a browser recipe's arguments, into the GitHub
    comment, into the planning prompt. A login step that carries the password
    inline would be republished in all three places.
    """
    bad = {"error": f"visual.{field} must be a list of instruction strings", "code": "bad_visual"}
    if not isinstance(raw, list) or not all(isinstance(s, str) and s.strip() for s in raw):
        return None, bad
    if len(raw) < minimum:
        return None, {
            "error": f"visual.{field} needs at least {minimum} entr{'y' if minimum == 1 else 'ies'}",
            "code": "bad_visual",
        }
    if len(raw) > BUILD_VISUAL_STEPS_MAX:
        return None, {"error": f"visual.{field} is too long", "code": "bad_visual"}
    out: list[str] = []
    for item in raw:
        text = item.strip()[:BUILD_REDACT_MAX_STRING]
        secret = reject_secret_text(text, "visual")
        if secret:
            return None, {**secret, "code": "secret_in_visual"}
        out.append(text)
    return out, None


def parse_build_visual_viewports(raw: Any) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """The viewports this round is proof at. Named, so an image can claim one."""
    bad = {"error": "visual.viewports must be a list of {name, width, height}", "code": "bad_visual"}
    if not isinstance(raw, list) or not raw:
        return None, bad
    if len(raw) > BUILD_VISUAL_VIEWPORTS_MAX:
        return None, {"error": "visual.viewports is too long", "code": "bad_visual"}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None, bad
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or not SLICE_ID_RE.fullmatch(name.strip()):
            return None, {"error": "visual.viewports[].name must be a short identifier", "code": "bad_visual"}
        name = name.strip()
        if name in seen:
            return None, {"error": f"visual.viewports names a duplicate: {name}", "code": "bad_visual"}
        seen.add(name)
        sizes: dict[str, int] = {}
        for axis, floor in (("width", BUILD_IMAGE_MIN_WIDTH), ("height", BUILD_IMAGE_MIN_HEIGHT)):
            value = item.get(axis)
            if not isinstance(value, int) or isinstance(value, bool) or not floor <= value <= 4096:
                return None, {
                    "error": f"visual.viewports[].{axis} must be an integer between {floor} and 4096",
                    "code": "bad_visual",
                }
            sizes[axis] = value
        out.append({"name": name, **sizes})
    return out, None


def parse_build_visual(raw: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The proof contract. Complete or refused; there is no partial form.

    This is the whole reason a visual build can be trusted later. Every field
    here is something a screenshot round would otherwise decide for itself at
    capture time — which page, which viewport, what the picture has to show,
    whether a human looks at it. Deciding any of them later means the evidence
    is graded against whatever it happens to contain.

    `human_review` is required rather than defaulted. False is a real answer
    with real consequences (the round publishes and the build walks on to
    review), and so is true; an omitted key is a planner who never considered
    the question, and inventing either answer for them is the one outcome that
    cannot be audited afterwards.
    """
    if not isinstance(raw, dict):
        return None, {"error": "verification.visual must be an object", "code": "bad_visual"}
    allowed = {
        "route", "scenario", "setup", "navigation", "expected", "checklist", "viewports",
        "human_review", "preview_stack", "browser_recipe", "tenant", "planning_pane",
        "allow_login_redirect",
    }
    extra = sorted(set(raw) - allowed)
    if extra:
        return None, {"error": f"verification.visual has an unknown field: {extra[0]}", "code": "bad_visual"}

    route = raw.get("route")
    if not isinstance(route, str) or not route.startswith("/") or len(route) > BUILD_NAME_MAX:
        return None, {
            "error": "visual.route must be the served path the proof is taken at, starting with /",
            "code": "bad_visual",
        }
    parsed_route = urlparse(route)
    if (
        route.startswith("//")
        or "://" in route
        or "\\" in route
        or "\x00" in route
        or any(ch.isspace() for ch in route)
        or parsed_route.scheme
        or parsed_route.netloc
        or parsed_route.query
        or parsed_route.fragment
        or "@" in route
        or reject_secret_text(route, "visual.route")
    ):
        # A full URL here would let the contract point the browser off the
        # preview stack entirely, which is the one thing the preview stack
        # identity exists to pin down.
        return None, {"error": "visual.route is a path, not a URL", "code": "bad_visual"}

    names: dict[str, str] = {}
    for field in ("scenario", "preview_stack", "browser_recipe", "tenant"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > BUILD_NAME_MAX:
            return None, {"error": f"visual.{field} is required", "code": "bad_visual"}
        secret = reject_secret_text(value.strip(), "visual")
        if secret:
            return None, {**secret, "code": "secret_in_visual"}
        names[field] = value.strip()

    steps: dict[str, list[str]] = {}
    for field, minimum in (("setup", 0), ("navigation", 1), ("expected", 1), ("checklist", 1)):
        if field not in raw:
            return None, {"error": f"visual.{field} is required", "code": "bad_visual"}
        parsed, step_err = parse_build_visual_steps(raw[field], field, minimum)
        if step_err:
            return None, step_err
        steps[field] = parsed or []

    viewports, viewport_err = parse_build_visual_viewports(raw.get("viewports"))
    if viewport_err:
        return None, viewport_err

    if "human_review" not in raw:
        return None, {
            "error": (
                "visual.human_review must say yes or no; there is no default, because "
                "both answers change what the round does"
            ),
            "code": "missing_human_review",
        }
    human_review, human_err = parse_strict_bool(raw, "human_review", "bad_visual")
    if human_err:
        return None, human_err
    allow_login_redirect = False
    if "allow_login_redirect" in raw:
        allow_login_redirect, login_err = parse_strict_bool(raw, "allow_login_redirect", "bad_visual")
        if login_err:
            return None, login_err

    pane = raw.get("planning_pane")
    if human_review:
        # With review enabled the round ends by prompting a person. A round
        # that cannot name the pane to prompt would publish and then quietly
        # tell nobody, which reads exactly like review-disabled.
        if (
            not isinstance(pane, str)
            or not pane.strip()
            or len(pane.strip()) > BUILD_NAME_MAX
            or not re.fullmatch(r"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+", pane.strip())
        ):
            return None, {
                "error": "visual.planning_pane must be the exact saved Herdr pane ID",
                "code": "missing_planning_pane",
            }
        pane = pane.strip()
        secret = reject_secret_text(pane, "visual")
        if secret:
            return None, {**secret, "code": "secret_in_visual"}
    elif pane is not None and (not isinstance(pane, str) or not pane.strip()):
        return None, {"error": "visual.planning_pane must be a pane name", "code": "bad_visual"}
    else:
        pane = pane.strip() if isinstance(pane, str) else None

    return {
        "route": route[:BUILD_NAME_MAX],
        "scenario": names["scenario"],
        "setup": steps["setup"],
        "navigation": steps["navigation"],
        "expected": steps["expected"],
        "checklist": steps["checklist"],
        "viewports": viewports,
        "human_review": human_review,
        "preview_stack": names["preview_stack"],
        "browser_recipe": names["browser_recipe"],
        "tenant": names["tenant"],
        "planning_pane": pane,
        "allow_login_redirect": bool(allow_login_redirect),
    }, None


def build_rule_phase(rule: dict[str, Any]) -> str:
    """The one phase a changed-path rule applies to. Absent means fast.

    Before this, a rule applied to whichever phase happened to be asking. A
    rule that meant "touching migrations mandates the integration seam" also
    made the fast gate require an integration recipe, and a rule that meant a
    unit check ran again at integration — in both directions the plan got a
    phase it never declared.

    Fast is the default because it is the gate every mechanical build has to
    pass. An integration recipe wrongly required by the fast gate is slow and
    visible; a fast recipe wrongly deferred to integration would let the writer
    reach review with the check its plan mandated never having run.
    """
    phase = rule.get("phase")
    return phase if isinstance(phase, str) and phase in BUILD_CHECK_PHASES else "fast"


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
    rollout, rollout_err = parse_build_rollout(data)
    if rollout_err:
        return 400, rollout_err
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
            "rollout": rollout,
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
            # Where this build started. `head` moves as the writer commits, so
            # it cannot answer "what has this build changed" — the changed-path
            # rules need a fixed floor to diff against.
            "base_head": head,
            # Already validated to a bounded repo-relative path, so it goes
            # in as itself. Running it through redact_secrets would let a
            # 64-hex path segment turn the ticket into "[redacted]", and the
            # build would have lost the file it was opened to write.
            "index": index,
            "ticket": ticket,
            "route": route,
            "verification": verification,
            "rollout": rollout,
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
        claim = build_claim(doc)
        if claim is not None and not claim.get("consumed"):
            # The reservation is spent the moment the session it was holding
            # open exists. Recording the session on it is what makes the
            # journal answer "which run launched this writer" after a restart.
            claim["writer_session"] = session.strip()
            claim["consumed"] = True
            claim["consumed_at"] = utcnow()
    stacks, _ = parse_build_stacks(data)
    if stacks:
        doc["stacks"] = stacks
    # data.head is deliberately not folded in. Identity comes from the live
    # repository read, so a payload field cannot move the build's head.
    #
    # data.review is not folded either, and there is no key here that writes
    # `reviews`. It used to: a caller could post a review blob carrying the
    # current change_id and verdict PASS, and every gate that asks "has this
    # change been reviewed" -- build_last_review, build_finalize_gaps -- would
    # answer yes for a review that never ran. Only build_review_result writes
    # a review round now, from a reviewer it actually launched.
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
        isinstance(session, str) and HARNESS_SESSION_RE.fullmatch(session.strip())
    ):
        # The writer session is the handle that resumes one agent. Anything
        # that is not a real session id is either a typo that would strand the
        # writer or free text that has no business on disk.
        return 400, {"error": "writer_session must be a canonical harness UUID", "code": "bad_writer_session"}
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

        on_path = build_class(doc) in BUILD_PATH_CLASSES
        if on_path and action in ("start_writer", "writer_done"):
            out = build_public(doc)
            out["ok"] = False
            out["error"] = (
                f"'{action}' asserts writer state; on this build path only "
                "buildWriterLaunch and buildWriterComplete may change it"
            )
            out["code"] = "writer_op_required"
            return 409, out
        if on_path and action in BUILD_GATE_ACTIONS:
            # Answered after the replay check so a recorded gate receipt still
            # replays, and before anything is written so a rejected caller
            # spends no budget and moves no stage.
            out = build_public(doc)
            out["ok"] = False
            out["error"] = (
                f"'{action}' asserts a gate was satisfied; on this build path only "
                "buildChecks, buildReview, buildGate, and buildFinalize record it, from "
                "receipts they verified against the change on disk"
            )
            out["code"] = "gate_op_required"
            return 409, out
        if action == "evidence_captured" and build_visual_contract(doc) is not None:
            # A build that carries a proof contract has a real capture path, so
            # a caller asserting `evidence_captured` here is asserting a round
            # that no browser took. The round object it posts would be indis-
            # tinguishable from a captured one at every later step.
            out = build_public(doc)
            out["ok"] = False
            out["error"] = (
                "this build declares a verification.visual contract; only the visual round "
                "operations may record evidence, from images they captured and inspected"
            )
            out["code"] = "visual_op_required"
            return 409, out

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

        if on_path and action == "start_writer":
            claim = build_claim_live(doc, now)
            if claim is None:
                # The writer is already running by the time this transition is
                # posted, so refusing here cannot un-launch it. What it does
                # stop is the launch: buildBrief will not compose a prompt
                # without a live reservation, so a graph that skipped buildClaim
                # never gets as far as an agent.
                out = build_public(doc)
                out["ok"] = False
                out["error"] = "no live writer reservation; buildClaim must run before a writer"
                out["code"] = "claim_required"
                return 409, out
            if claim.get("kind") != "initial":
                out = build_public(doc)
                out["ok"] = False
                out["error"] = f"the live reservation is a '{claim.get('kind')}' claim, not an initial one"
                out["code"] = "claim_mismatch"
                return 409, out

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


# --- pre-PR mechanical path -------------------------------------------------
#
# One bounded Rewst invocation runs exactly one step. Which step is read off
# durable document by build_path_step, never asserted by the caller:
# an invocation that arrives twice with the same intent finds the build has
# already moved and is given the next step instead of repeating the last one.
#
# The graph holds edges. Every judgment lives here, on this side of the
# tunnel, because each one is a place a plausible-looking answer would ship
# unverified work: whether a check produced a verdict or only a queue
# acknowledgement, whether a reviewer disagreed or merely went quiet, whether
# a stack was the clean one, whether this build has already been pushed.


def build_open(build_id: Any) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Load a build for a mechanical-path op, or say precisely why not."""
    if not valid_build_id(build_id):
        return None, (400, {"ok": False, "error": "invalid build_id", "code": "bad_build_id"})
    doc, read_err = load_build(str(build_id))
    if read_err:
        return None, (int(read_err["status"]), read_err)
    if doc is None:
        return None, (404, {"ok": False, "error": "unknown build", "code": "unknown_build",
                            "build_id": build_id})
    if doc.get("version") != BUILD_STATE_VERSION:
        return None, (409, build_version_park(doc))
    return doc, None


def build_class(doc: dict[str, Any]) -> str:
    """The routed class this build was opened as.

    Absent means mechanical: the route row is the planner's stamp, and a build
    that never got one has not been routed anywhere else.
    """
    route = doc.get("route")
    if not isinstance(route, dict):
        return BUILD_MECHANICAL_CLASS
    raw = route.get("class")
    return raw if isinstance(raw, str) and raw in SLICE_CLASSES else BUILD_MECHANICAL_CLASS


def build_open_path(build_id: Any) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Load a build and refuse it if this workflow does not drive its class.

    Mechanical and sensitive both run here. A visual build does not: it has an
    evidence stage this workflow has no way to capture, and letting one through
    would take it past screenshot capture and human review without either
    having happened.
    """
    doc, err = build_open(build_id)
    if err:
        return None, err
    assert doc is not None
    found = build_class(doc)
    if found not in BUILD_PATH_CLASSES:
        return None, (
            409,
            {
                "ok": False,
                "build_id": doc.get("build_id"),
                "stage": doc.get("stage"),
                "step": "not_on_path",
                "error": (
                    f"build is routed '{found}'; this workflow drives "
                    + " and ".join(BUILD_PATH_CLASSES)
                ),
                "code": "not_on_path",
            },
        )
    return doc, None


def build_open_mechanical(
    build_id: Any,
) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Compatibility surface for the historical mechanical-path operations.

    The path now admits both mechanical and sensitive builds; keeping this
    name lets ticket-02 callers share that implementation without restoring
    the old mechanical-only routing or duplicating the class gate.
    """
    return build_open_path(build_id)


def build_is_sensitive(doc: dict[str, Any]) -> bool:
    return build_class(doc) == BUILD_SENSITIVE_CLASS


def build_review_roles(doc: dict[str, Any]) -> tuple[str, ...]:
    """Every review this build's class owes before it may be finalized."""
    return BUILD_CLASS_REVIEW_ROLES.get(build_class(doc), (BUILD_REVIEW_BEHAVIOR_ROLE,))


def build_review_entry_role(entry: dict[str, Any]) -> str:
    """The role a recorded round answered for.

    Rounds written before roles existed carry no field. They were the
    behavior-and-test review, so they read as one -- not as an unlabelled round
    that could be counted for whichever role happens to be missing.
    """
    raw = entry.get("role")
    return raw if isinstance(raw, str) and raw in BUILD_REVIEW_ROLES else BUILD_REVIEW_BEHAVIOR_ROLE


def build_route_reviewer(doc: dict[str, Any], role: str) -> str | None:
    """The reviewer this build's route names for one role, or None.

    `none` is the sliceRoute spelling for an empty seat and is not a reviewer,
    so it answers None here rather than becoming a reviewer name nothing can
    launch.
    """
    route = doc.get("route")
    if not isinstance(route, dict):
        return None
    raw = route.get(BUILD_ROLE_ROUTE_STAMP.get(role, ""))
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or name == "none":
        return None
    return name


def build_route_stamps(doc: dict[str, Any]) -> tuple[str, ...]:
    return BUILD_SENSITIVE_ROUTE_STAMPS if build_is_sensitive(doc) else BUILD_ROUTE_STAMPS


def build_sensitive_route_gaps(doc: dict[str, Any]) -> list[str]:
    """Why this route cannot carry a sensitive build, or nothing.

    Three separate claims, and every one of them fails closed. The writer is
    Opus. Each review seat holds the one reviewer named for its role. And the
    three seats are three vendors, checked rather than assumed: the mapping
    above is the only thing making that true, so a future edit to it has to
    break a test instead of quietly putting Anthropic on both sides of the
    highest-risk class.
    """
    if not build_is_sensitive(doc):
        return []
    gaps: list[str] = []
    route = doc.get("route") if isinstance(doc.get("route"), dict) else {}
    model = str(route.get("model") or "").strip().lower()
    writer = build_writer_vendor(doc)
    if not model:
        gaps.append("route does not name the writer's model; sensitive work is written by Opus")
    elif BUILD_SENSITIVE_WRITER_ALIAS not in model or writer != BUILD_SENSITIVE_WRITER_VENDOR:
        gaps.append(f"sensitive work is written by Opus; route names '{route.get('model')}'")
    vendors: dict[str, str] = {}
    for role in BUILD_REVIEW_ROLES:
        expected = BUILD_SENSITIVE_REVIEWER[role]
        named = build_route_reviewer(doc, role)
        stamp = BUILD_ROLE_ROUTE_STAMP[role]
        if named is None:
            gaps.append(f"route is missing {stamp}, the {BUILD_REVIEW_ROLE_LABEL[role]} reviewer")
            continue
        if named != expected:
            gaps.append(
                f"the {BUILD_REVIEW_ROLE_LABEL[role]} review is '{expected}'; "
                f"route names '{named}' as {stamp}"
            )
            continue
        vendor = BUILD_REVIEWER_VENDOR.get(named)
        if vendor is None:
            gaps.append(f"reviewer '{named}' belongs to no vendor this service knows")
            continue
        if vendor in vendors:
            gaps.append(
                f"'{named}' and '{vendors[vendor]}' are both {vendor}; the two sensitive "
                "reviews would share a vendor"
            )
            continue
        if writer is not None and vendor == writer:
            gaps.append(f"reviewer '{named}' is the writer's own vendor ({vendor})")
            continue
        vendors[vendor] = named
    return gaps
def build_open_writer(build_id: Any, kind: str) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Open a build for a writer turn, on the path that turn belongs to.

    A design turn is the visual iteration's, so it runs on any build that
    declared visual proof — a sensitive UI change writes with Opus and still
    iterates on how the page looks. Every other kind stays on the mechanical
    path, so a visual build still cannot run an `initial` or `correction` turn
    and reach review without its evidence round.
    """
    if kind == BUILD_DESIGN_CLAIM_KIND:
        return build_open_visual(build_id)
    return build_open_mechanical(build_id)


def build_current_claim_kind(build_id: Any) -> str:
    """What kind of turn the current reservation is for, or "" when unreadable.

    Read before the path is chosen, because which opener is correct is a
    property of the reservation on disk rather than of anything the caller
    sent. An unreadable build answers "" and takes the mechanical refusal,
    which is the one that names the real problem.
    """
    doc, err = build_open(build_id)
    if err or doc is None:
        return ""
    claim = build_claim(doc)
    return str(claim.get("kind") or "") if isinstance(claim, dict) else ""


def build_open_checks(build_id: Any) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Open a build for a deterministic check phase, on either path it runs on.

    Checks are the one gate both paths share. A design turn reruns the
    changed-area recipes for every candidate it produces, and refusing them
    here because the build is routed visual would leave the only turn that
    edits code during iteration with no way to be told it broke something.

    Deliberately narrow: the visual build is admitted only at `evidence`, which
    is where iteration happens. Anywhere else it takes the mechanical refusal,
    so nothing here lets a visual build walk the mechanical path.
    """
    doc, err = build_open(build_id)
    if err:
        return None, err
    assert doc is not None
    if build_class(doc) == BUILD_MECHANICAL_CLASS:
        return doc, None
    if build_needs_visual(doc) and doc.get("stage") == "evidence":
        return doc, None
    return build_open_mechanical(build_id)



def build_recipe_cwd(
    spec: dict[str, Any], target: Path, label: str
) -> tuple[Path | None, str | None]:
    """Where this recipe runs for this phase, or why it cannot run at all.

    A catalog entry that says `"cwd": "."` means "the checkout this catalog
    ships with", and `resolve_under_home` turns that into $GRAPHWING_HOME — the
    deployed service, not the tree under test. Running `graphwing-unit` there
    for a build on the `graphwing-pre-pr` worktree produces a green verdict for
    code the build has never touched: the gate reports a pass for a change it
    did not measure. So the catalog default is rebased onto the target.

    The target is the phase's, not the build's. Fast checks measure the
    writer's worktree; final integration measures the declared clean stack's
    checkout, and rebasing its recipes onto `doc.worktree` would run the
    "clean" phase over the writer's half-finished files with a clean stack's
    name on the receipt.

    A recipe pinned somewhere that is neither is run nowhere. It measures a
    tree this change does not appear in, and passing it would be the same lie
    in a different directory, so it fails closed with a reason instead.
    """
    declared = Path(str(spec.get("cwd")))
    if declared == target:
        return target, None
    if declared == HOME.resolve():
        if not target.is_dir():
            return None, f"{label} {target} is not a directory"
        return target, None
    return None, (
        f"recipe '{spec.get('name')}' is pinned to {declared}, which is not {label} "
        f"{target}; it would measure a tree this change is not verified in"
    )


def build_recipe_catalog(
    repos: dict[str, str],
    target: Path | None = None,
    label: str = "this build's worktree",
) -> dict[str, dict[str, Any]]:
    """Every named recipe a build may declare, and where the name came from.

    tests.json is read first and wins a duplicate name, because that is the
    order the existing testRun / scriptRun ops resolve in. A recipe that means
    two different things depending on which op ran it is worse than a name
    collision that is decided once, here.

    With a target, every entry is also re-pointed at that directory or marked
    `unresolvable` with a reason. Without one the catalog is just the list of
    names, which is all "is this name real" needs.
    """
    out: dict[str, dict[str, Any]] = {}
    for kind, loader in (("test", load_tests), ("script", load_scripts), ("rr", load_rr)):
        try:
            catalog = loader(repos)
        except (OSError, ValueError, RuntimeError):
            # A malformed or absent catalog file is not this build's fault and
            # must not take the whole gate down with a traceback. The names it
            # would have supplied simply do not resolve, which the caller is
            # told about by name.
            continue
        for name, spec in catalog.items():
            if name in out:
                continue
            entry = {**spec, "kind": kind, "coverage": build_recipe_is_coverage(name, spec)}
            entry["declared_cwd"] = str(spec.get("cwd"))
            entry["unresolvable"] = None
            if target is not None:
                cwd, why = build_recipe_cwd(entry, target, label)
                entry["unresolvable"] = why
                if cwd is not None:
                    entry["cwd"] = cwd
            out[name] = entry
    return out


def build_visual_recipe_catalog() -> dict[str, dict[str, Any]]:
    """Graphwing-owned browser drivers, pinned to installed bytes by SHA-256."""
    try:
        raw = json.loads(VISUAL_BROWSERS_PATH.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    items = raw.get("browsers") or [] if isinstance(raw, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        argv = item.get("argv")
        integrity_rel = str(item.get("integrity_path") or "").strip()
        expected = str(item.get("sha256") or "").lower()
        if not name or not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            continue
        integrity_path = resolve_under_home(integrity_rel)
        try:
            integrity_path.relative_to(HOME.resolve())
            data = read_regular_file(integrity_path.parent, integrity_path.name)
        except (OSError, ValueError):
            continue
        actual = hashlib.sha256(data).hexdigest()
        resolved_argv = [str(resolve_under_home(v[2:])) if v.startswith("./") else v for v in argv]
        entry = {
            "name": name,
            "argv": resolved_argv,
            "cwd": HOME.resolve(),
            "timeout_seconds": int(item.get("timeout_seconds") or 120),
            "integrity_path": str(integrity_path),
            "integrity_sha256": actual,
            "unresolvable": None,
        }
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or not hmac.compare_digest(actual, expected):
            entry["unresolvable"] = "browser driver integrity does not match its installed pin"
        if Path(resolved_argv[0]).resolve() != integrity_path:
            entry["unresolvable"] = "browser argv does not execute the pinned Graphwing driver"
        out[name] = entry
    return out


def build_recipe_is_coverage(name: str, spec: dict[str, Any]) -> bool:
    """False when a recipe must never be presented as unit, type, or integration coverage."""
    if name in BUILD_HOOK_ONLY_RECIPES:
        return False
    return spec.get("coverage") is not False


def build_changed_paths(worktree: Path, base_head: str | None) -> list[str]:
    """Repo-relative paths this build has touched, committed or not.

    Both halves are needed. The worktree status is the writer's staged and
    unstaged work, which is where a mechanical change spends its whole life
    before finalization. The diff against base_head catches anything an
    earlier round already committed.
    """
    paths: set[str] = set()
    status = run_git(worktree, ["status", "--porcelain=v1"])
    if status.get("ok"):
        for line in (status.get("stdout") or "").splitlines():
            rel = line[3:].strip()
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            rel = rel.strip().strip('"')
            if rel:
                paths.add(rel)
    if base_head:
        diff = run_git(worktree, ["diff", "--name-only", f"{base_head}..HEAD"])
        if diff.get("ok"):
            for line in (diff.get("stdout") or "").splitlines():
                rel = line.strip()
                if rel:
                    paths.add(rel)
    return sorted(paths)[:BUILD_CHANGED_PATHS_MAX]


def build_change_id(worktree: Path, head: str) -> str:
    """Identify the change as it exists right now, committed or not.

    `head` cannot answer this on the mechanical path. The writer stages its
    files and never commits — finalization is the only thing that does — so
    HEAD is the same object id from the moment the build opens until the moment
    it ends. A gate keyed on HEAD alone would accept a fast-check verdict
    recorded before a correction turn rewrote every file that verdict covered,
    which is precisely what the correction turn exists to invalidate.

    Staged and unstaged diffs are hashed in full; untracked files contribute
    their names only. That boundary is deliberate: finalization commits what
    the writer staged, so content the writer never staged is not part of the
    change being gated.
    """
    parts = [head]
    for args in (["diff", "--cached"], ["diff"], ["ls-files", "--others", "--exclude-standard"]):
        out = run_git(worktree, args)
        if not out.get("ok"):
            # Unreadable gets its own identity rather than falling back to the
            # last one. Reusing it would let a broken worktree inherit a green
            # verdict recorded when git could still be read.
            return f"{head}:unreadable"
        parts.append(str(out.get("stdout") or ""))
    digest = hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()
    return f"{head}:{digest[:16]}"


def build_runner_tree(worktree: Path) -> str | None:
    """The tree checked out by a runner, never merely its commit label."""
    result = run_git(worktree, ["rev-parse", "HEAD^{tree}"])
    if not result.get("ok"):
        return None
    tree = str(result.get("stdout") or "").strip()
    return tree if re.fullmatch(r"[0-9a-f]{40,64}", tree) else None


def build_materialize_candidate(
    worktree: Path, base_head: str, change_id: str
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Freeze the exact index as a deterministic, unreferenced Git commit."""
    status = git_status(worktree)
    if not status.get("ok"):
        return None, {
            "code": "candidate_unreadable",
            "error": str(status.get("error") or "git status failed")[:BUILD_REDACT_MAX_STRING],
        }
    dirt = build_commit_dirt(list(status.get("entries") or []))
    if dirt:
        return None, {
            "code": "candidate_not_exact",
            "error": "candidate must be exactly staged; unstaged or untracked paths: " + ", ".join(dirt[:8]),
            "entries": dirt[:8],
        }
    tree_result = run_git(worktree, ["write-tree"])
    if not tree_result.get("ok"):
        return None, {"code": "candidate_unreadable", "error": "git write-tree failed"}
    tree = str(tree_result.get("stdout") or "").strip()
    env = {
        "GIT_AUTHOR_NAME": "graphwing integration",
        "GIT_AUTHOR_EMAIL": "graphwing@localhost",
        "GIT_AUTHOR_DATE": "Thu, 01 Jan 1970 00:00:00 +0000",
        "GIT_COMMITTER_NAME": "graphwing integration",
        "GIT_COMMITTER_EMAIL": "graphwing@localhost",
        "GIT_COMMITTER_DATE": "Thu, 01 Jan 1970 00:00:00 +0000",
    }
    commit_result = run_cmd(
        ["git", "-C", str(worktree), "commit-tree", tree, "-p", base_head, "-m", "graphwing integration candidate"],
        cwd=worktree,
        env=env,
    )
    if not commit_result.get("ok"):
        return None, {"code": "candidate_unreadable", "error": "git commit-tree failed"}
    commit = str(commit_result.get("stdout") or "").strip()
    return {
        "candidate_tree": tree,
        "base_head": base_head,
        "change_id": change_id,
        "materialization": commit,
    }, None


def build_provision_candidate_runner(
    worktree: Path, runner_root: Path, candidate: dict[str, str], attempt_id: str
) -> tuple[Path | None, dict[str, Any] | None]:
    """Create one attempt-owned detached worktree at the synthetic candidate."""
    runner_root.mkdir(parents=True, exist_ok=True)
    safe_attempt = re.sub(r"[^A-Za-z0-9_.-]", "-", attempt_id)[:96]
    runner = runner_root / safe_attempt
    build_cleanup_candidate_runner(worktree, runner)
    result = run_git(
        worktree, ["worktree", "add", "--detach", str(runner), candidate["materialization"]]
    )
    if not result.get("ok"):
        build_cleanup_candidate_runner(worktree, runner)
        return None, {
            "code": "runner_provision_failed",
            "error": str(result.get("stderr") or result.get("error") or "git worktree add failed")[:BUILD_REDACT_MAX_STRING],
        }
    return runner, None


def build_cleanup_candidate_runner(worktree: Path, runner: Path) -> None:
    """Remove an attempt runner; safe after partial setup and safe to repeat."""
    run_git(worktree, ["worktree", "remove", "--force", str(runner)])
    if runner.exists():
        shutil.rmtree(runner, ignore_errors=True)
    run_git(worktree, ["worktree", "prune"])


def build_live_change(
    doc: dict[str, Any], repos: dict[str, str]
) -> tuple[dict[str, str] | None, str | None]:
    """Live branch, head, and change id for a build, or why they cannot be read."""
    live, detail = build_resolve_identity(doc, repos)
    if live is None:
        return None, detail
    worktree = Path(str(doc.get("worktree")))
    return {**live, "change_id": build_change_id(worktree, live["head"])}, None


def build_required_recipes(
    doc: dict[str, Any], phase: str, changed: list[str]
) -> tuple[list[str], dict[str, list[str]]]:
    """Declared recipes for a phase, plus whatever the changed paths mandate.

    Union, always. A path rule can only add: there is no shape it can take
    that removes a declared name, so a rule cannot be used to talk a build out
    of a check its plan promised.

    Returns the ordered names and, for each, why it is required — the graph
    shows that to a human deciding whether a park is the plan's fault or the
    diff's.
    """
    verification = doc.get("verification") or {}
    declared = [n for n in (verification.get(phase) or []) if isinstance(n, str)]
    why: dict[str, list[str]] = {}
    required: list[str] = []
    for name in declared:
        if name not in why:
            required.append(name)
        why.setdefault(name, []).append("declared")
    for rule in verification.get("path_rules") or []:
        if not isinstance(rule, dict):
            continue
        if build_rule_phase(rule) != phase:
            # A rule belongs to the phase it declared and to no other. Applying
            # it everywhere made "this path needs the integration seam" also
            # mean "and the fast gate must run it first".
            continue
        match = str(rule.get("match") or "")
        hits = [p for p in changed if fnmatch.fnmatch(p, match)]
        if not hits:
            continue
        for name in rule.get("recipes") or []:
            if not isinstance(name, str):
                continue
            if name not in why:
                required.append(name)
            why.setdefault(name, []).append(f"path:{match}")
    return required, why


def build_route_stamp_gaps(doc: dict[str, Any]) -> list[str]:
    stamps = build_route_stamps(doc)
    route = doc.get("route")
    if not isinstance(route, dict):
        return list(stamps)
    return [k for k in stamps if route.get(k) in (None, "")]


def build_worktree_conflicts(entries: list[str]) -> list[str]:
    """Porcelain lines for paths git itself cannot resolve.

    An unmerged path is not dirt a writer can be asked to work around: the
    file on disk has conflict markers in it, and every check run against it is
    measuring the conflict rather than the change.
    """
    out = []
    for line in entries:
        xy = line[:2]
        if "U" in xy or xy in ("AA", "DD"):
            out.append(line[:BUILD_NAME_MAX])
    return out


def build_tracked_dirt(entries: list[str]) -> list[str]:
    """Porcelain lines for tracked modifications, ignoring untracked files."""
    return [line[:BUILD_NAME_MAX] for line in entries if not line.startswith("??")]


def build_checks_entry(doc: dict[str, Any], phase: str) -> dict[str, Any] | None:
    entry = (doc.get("checks") or {}).get(phase)
    return entry if isinstance(entry, dict) else None


def build_checks_clean(doc: dict[str, Any], phase: str, change_id: str) -> tuple[bool, str]:
    """True only when every required recipe passed against this exact change.

    Three different things read the same to a caller that only looks at a
    boolean, so each gets its own sentence: nothing has been run here, part of
    it was run, or it was run and something failed. The first is the dangerous
    one — a phase with no verdicts at all is indistinguishable from a green
    phase if the gate only asks "any failures?".
    """
    entry = build_checks_entry(doc, phase)
    if entry is None:
        return False, f"no {phase} checks have been run for this build"
    if entry.get("change_id") != change_id:
        return False, (
            f"{phase} checks were recorded against {entry.get('change_id')}, the worktree now "
            f"holds {change_id}"
        )
    required = [n for n in (entry.get("required") or []) if isinstance(n, str)]
    if not required:
        return False, f"{phase} checks recorded no required recipes"
    verdicts = entry.get("verdicts") or {}
    missing = [n for n in required if not isinstance(verdicts.get(n), dict)]
    if missing:
        return False, f"{phase} checks have no verdict for: {', '.join(missing)}"
    failed = [n for n in required if not verdicts[n].get("ok")]
    if failed:
        return False, f"{phase} checks failed: {', '.join(failed)}"
    if not any(verdicts[n].get("coverage") for n in required):
        return False, f"{phase} checks ran only hooks, which are not coverage"
    if phase == "integration":
        named = entry.get("stack")
        stack_entry, stack_err = build_integration_stack(doc, named)
        if stack_err:
            return False, stack_err
        assert stack_entry is not None
        # The stack name on the record is a label; the runner is where the
        # recipes actually executed. An entry that names a clean stack and ran
        # somewhere else — including one written before the runner was
        # recorded at all — is not evidence about a clean checkout.
        recorded = entry.get("runner")
        try:
            runner_path = Path(str(recorded)).resolve()
            runner_root = Path(str(stack_entry["cwd"])).resolve()
            runner_path.relative_to(runner_root)
            if runner_path == runner_root:
                raise ValueError("runner root is not an attempt worktree")
        except (ValueError, OSError):
            return False, (
                f"integration checks ran in {recorded or 'a directory the record does not name'}, "
                f"outside clean runner root '{named}' at {stack_entry['cwd']}"
            )
        worktree = Path(str(doc.get("worktree")))
        status = git_status(worktree)
        dirt = build_commit_dirt(list(status.get("entries") or [])) if status.get("ok") else ["unreadable"]
        tree_result = run_git(worktree, ["write-tree"]) if not dirt else {"ok": False}
        current_tree = str(tree_result.get("stdout") or "").strip() if tree_result.get("ok") else None
        candidate_tree = entry.get("candidate_tree")
        runner_tree = entry.get("runner_tree")
        if dirt or not current_tree:
            return False, "current candidate is not exactly staged"
        if candidate_tree != current_tree:
            return False, f"integration candidate tree {candidate_tree} does not match staged tree {current_tree}"
        if runner_tree != candidate_tree:
            return False, f"integration runner tree {runner_tree} does not match candidate tree {candidate_tree}"
        if not entry.get("materialization") or entry.get("base_head") != entry.get("head"):
            return False, "integration receipt does not identify its candidate materialization and base head"
    return True, "ok"


def build_stack_catalog() -> dict[str, dict[str, Any]]:
    """Every declared stack, or nothing when the catalog cannot be read.

    An unreadable stacks.json is answered as "no stacks", not as a traceback.
    Every caller here is asking whether a *clean* stack exists, and the honest
    answer when the catalog is broken is no.
    """
    try:
        stacks, _ports = load_stacks()
    except (OSError, ValueError, RuntimeError):
        return {}
    return stacks


def build_clean_stack_names(catalog: dict[str, dict[str, Any]]) -> list[str]:
    """Stacks the catalog declares as clean, in name order."""
    return sorted(
        name for name, spec in catalog.items() if spec.get("role") == BUILD_CLEAN_STACK_ROLE
    )


def build_stack_isolation_gap(doc: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """Why this stack is not a different tree from the build's, or None.

    A catalog entry can declare itself clean and still be pointed at the
    worktree the writer has been editing all afternoon, at which point the role
    is a label rather than a fact. Symlinks are resolved before the comparison,
    and containment is checked both ways: a stack running in a parent of the
    build's worktree is running over the build's files too.
    """
    name = entry.get("name")
    cwd = entry.get("cwd")
    if not isinstance(cwd, Path):
        return f"stack '{name}' does not resolve to a directory"
    try:
        runner = cwd.resolve()
        build_tree = Path(str(doc.get("worktree") or "")).resolve()
    except OSError as exc:
        return f"stack '{name}' could not be resolved against this build's worktree: {exc}"
    if not runner.is_dir():
        return f"stack '{name}' runs in {runner}, which is not a directory"
    if entry.get("role") == BUILD_CLEAN_STACK_ROLE and entry.get("runner") != "git-worktree":
        return (
            f"stack '{name}' does not declare runner 'git-worktree'; a healthy mutable stack "
            "cannot materialize the reviewed candidate"
        )
    if runner == build_tree or runner in build_tree.parents or build_tree in runner.parents:
        return (
            f"stack '{name}' runs in {runner}, which is the tree this build is being written in "
            f"({build_tree}); a stack over the writer's own files is the developer stack under "
            "another name"
        )
    return None


def build_integration_stack(
    doc: dict[str, Any], stack: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """The clean stack this build's final integration may run on, or why none.

    Fails closed on every axis. Health is not isolation: a healthy stack with
    no declared role is the developer stack that happens to be up, and it
    carries the writer's half-applied migrations and whatever was left running
    from the round before. So the catalog has to say `"role": "clean"` out
    loud, the named stack has to be one of those, it cannot be a stack this
    build declared for its own use, and it cannot run in the build's worktree.

    With no clean stack declared anywhere, there is no answer that is not a
    guess, and the refusal names that rather than accepting whatever was
    passed.
    """
    catalog = build_stack_catalog()
    clean = build_clean_stack_names(catalog)
    if not clean:
        return None, (
            'no stack in stacks.json declares "role": "clean"; final integration has nowhere '
            "isolated to run, and a build's own stack cannot stand in for one"
        )
    if not isinstance(stack, str) or not stack.strip():
        return None, (
            "final integration did not name the stack it ran on; stacks declared clean: "
            + ", ".join(clean)
        )
    name = stack.strip()
    entry = catalog.get(name)
    if entry is None:
        return None, (
            f"stack '{name}' is not in stacks.json; stacks declared clean: " + ", ".join(clean)
        )
    role = str(entry.get("role") or "unset")
    if role != BUILD_CLEAN_STACK_ROLE:
        return None, (
            f"stack '{name}' is declared role '{role}', not '{BUILD_CLEAN_STACK_ROLE}'; final "
            "integration needs a disposable clean stack and being healthy is not a substitute"
        )
    if name in [s for s in (doc.get("stacks") or []) if isinstance(s, str)]:
        return None, f"stack '{name}' is one of this build's own stacks, not a separate clean stack"
    gap = build_stack_isolation_gap(doc, entry)
    if gap:
        return None, gap
    return entry, None


def build_integration_stack_gap(doc: dict[str, Any], stack: Any) -> str | None:
    """Why this stack cannot stand in for the final integration run, or None."""
    _entry, gap = build_integration_stack(doc, stack)
    return gap


def build_phase_runner(
    doc: dict[str, Any], phase: str, stack: Any
) -> tuple[Path | None, str, str | None]:
    """The directory a phase's recipes run in, what to call it, or why there is none.

    Fast checks measure what the writer wrote, so they run in the build's
    worktree. Final integration measures a clean checkout, so it runs where the
    declared clean stack runs. Binding integration to `doc.worktree` is how a
    run labelled with a clean stack's name ends up executing over the writer's
    tree and reporting a clean-checkout pass.
    """
    if phase != "integration":
        return Path(str(doc.get("worktree"))), "this build's worktree", None
    entry, gap = build_integration_stack(doc, stack)
    if entry is None:
        return None, "the clean integration stack", gap
    return Path(str(entry["cwd"])), f"clean runner root '{entry['name']}'", None


def build_red_fast_checks(doc: dict[str, Any], change_id: str) -> list[str]:
    """Declared fast recipes that ran against this change and did not pass.

    Empty when the phase has never run here, which is deliberately the same
    answer as "nothing is red": a phase with no verdicts has not failed, it
    simply has not happened, and `build_checks_clean` is the one that refuses
    to call that a pass.
    """
    entry = build_checks_entry(doc, "fast")
    if not isinstance(entry, dict) or entry.get("change_id") != change_id or entry.get("ok"):
        return []
    named = [n for n in (entry.get("failed") or []) if isinstance(n, str)]
    named += [n for n in (entry.get("missing") or []) if isinstance(n, str) and n not in named]
    return named


def build_step_finding(doc: dict[str, Any], step: str, change_id: str) -> str:
    """The exact text a `fix` invocation hands back to the writer.

    Composed here so a resumed writer sees the same finding whether the run
    that produced it is still alive or died an hour ago. A graph that read the
    finding out of its own task history could only correct a failure it had
    personally witnessed, which is every case except the one that matters.
    """
    if step != "fix":
        return ""
    writer = doc.get("writer_result")
    if (
        isinstance(writer, dict)
        and writer.get("status") in ("error", "timeout")
        and writer.get("completed_change_id") == change_id
    ):
        summary = str(writer.get("summary") or writer.get("status") or "writer failed")
        return f"The previous writer turn ended before success.\n{summary}"[:COMPACT_MAX_CHARS]
    # Every reviewer that finished and said no, in role order. A sensitive
    # change can come back from both at once, and handing the writer one
    # finding at a time would spend a correction turn and a review round per
    # lens to learn what one turn could have fixed.
    findings = []
    for role in build_review_roles(doc):
        review = build_last_review(doc, change_id, role)
        if review is None or review.get("verdict") == "PASS":
            continue
        text = str(review.get("compact") or review.get("summary") or "")
        findings.append(
            f"The {BUILD_REVIEW_ROLE_LABEL[role]} review returned findings.\n{text}"
        )
    if findings:
        return "\n\n".join(findings)[:COMPACT_MAX_CHARS]
    red = build_red_fast_checks(doc, change_id)
    if not red:
        return ""
    entry = build_checks_entry(doc, "fast") or {}
    verdicts = entry.get("verdicts") or {}
    detail = "\n".join(
        str(verdicts[n].get("compact") or "") for n in red if isinstance(verdicts.get(n), dict)
    )
    return f"These declared fast checks are red: {', '.join(red)}\n{detail}"[:COMPACT_MAX_CHARS]


def build_last_review(
    doc: dict[str, Any], change_id: str, role: str | None = None
) -> dict[str, Any] | None:
    """The most recent micro-review recorded against this exact change.

    A review of the diff the writer has since rewritten is not a review of what
    would be committed, so it does not answer here at all — the caller sees the
    same "nobody has reviewed this" it would see if no review had ever run.

    With a role, only that lens answers. The two sensitive reviews are separate
    acknowledgements of the same diff, and a security PASS that satisfied the
    behavior seat because it happened to be the latest round is exactly the
    single-voice gate the class exists to avoid.
    """
    for entry in reversed(doc.get("reviews") or []):
        if not isinstance(entry, dict) or entry.get("change_id") != change_id:
            continue
        if not entry.get("verdict"):
            continue
        if role is not None and build_review_entry_role(entry) != role:
            continue
        return entry
    return None


def build_evidence_identity(doc: dict[str, Any], change_id: str) -> str:
    """One name for the evidence set that stands behind this change.

    Two reviewers must acknowledge the same diff *and* the same evidence. The
    change id already pins the diff; this pins everything the visual path
    recorded for it. A behavior review taken before a screenshot round was
    recaptured and a security review taken after are two reviews of two
    different bodies of evidence, and the pair would gate a state neither
    reviewer actually saw.

    `none` when nothing has been captured, which is the honest answer for a
    build whose plan asks for no visual proof.
    """
    rounds = [
        entry
        for entry in (doc.get("evidence_rounds") or [])
        if isinstance(entry, dict) and entry.get("change_id") == change_id
    ]
    if not rounds:
        return "none"
    material = [
        {
            "round": entry.get("round"),
            "change_id": entry.get("change_id"),
            "shots": entry.get("shots"),
            "human_review": entry.get("human_review"),
        }
        for entry in rounds
    ]
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()[:32]


def build_visual_gaps(doc: dict[str, Any], change_id: str) -> list[str]:
    """What the planning record's visual requirements still owe at this change.

    A sensitive change can also be visible, and when it is, the two sensitive
    reviews do not stand in for the screenshot round or the human look — they
    compose with them. Expressed as gaps rather than as a stage so the
    requirement holds at the commit line whatever order the rounds arrived in.
    """
    verification = doc.get("verification") or {}
    if not verification.get("needs_visual_proof") and not verification.get("human_visual_review"):
        return []
    rounds = [
        entry
        for entry in (doc.get("evidence_rounds") or [])
        if isinstance(entry, dict) and entry.get("change_id") == change_id
    ]
    gaps: list[str] = []
    if verification.get("needs_visual_proof") and not rounds:
        gaps.append("the plan requires visual proof and no evidence round was captured for this change")
    if verification.get("human_visual_review"):
        latest = rounds[-1] if rounds else None
        approved = (
            isinstance(latest, dict)
            and isinstance(latest.get("human_review"), dict)
            and latest["human_review"].get("verdict") == "approved"
        )
        if not approved:
            gaps.append(
                "the plan requires human visual review and the latest evidence round is not approved"
            )
    return gaps


def build_review_evidence_gap(
    doc: dict[str, Any], entry: dict[str, Any], change_id: str, label: str
) -> str | None:
    """Reject a review that does not identify the evidence it acknowledged."""
    seen = entry.get("evidence_id")
    live = build_evidence_identity(doc, change_id)
    if build_is_sensitive(doc) and seen != live:
        if not isinstance(seen, str):
            return f"the {label} review has no evidence identity for this sensitive change"
        return (
            f"the {label} review acknowledged evidence {seen}, which is no longer "
            f"what stands behind this change ({live})"
        )
    if isinstance(seen, str) and seen != live:
        return (
            f"the {label} review acknowledged evidence {seen}, which is no longer "
            f"what stands behind this change ({live})"
        )
    return None


def build_review_gaps(doc: dict[str, Any], change_id: str) -> list[str]:
    """Every acknowledgement this build still owes at this exact change.

    One entry per role, in a fixed order, so two runs looking at the same
    document name the same missing review. A role answers only for itself:
    absent, negative, and present-but-not-independent are three different
    sentences because they need three different next moves — run the review,
    resume the writer, fix the route.
    """
    gaps: list[str] = []
    for role in build_review_roles(doc):
        label = BUILD_REVIEW_ROLE_LABEL[role]
        entry = build_last_review(doc, change_id, role)
        if entry is None:
            gaps.append(f"no {label} review has been recorded for this change")
            continue
        evidence_gap = build_review_evidence_gap(doc, entry, change_id, label)
        if evidence_gap:
            gaps.append(evidence_gap)
            continue
        if entry.get("verdict") != "PASS":
            gaps.append(f"the last {label} review returned findings: {entry.get('summary')}")
            continue
        # A PASS is only evidence if the voice behind it could disagree. An
        # unknown reviewer name, an unstamped route, or a reviewer sharing the
        # writer's vendor each make this a self-review wearing a receipt.
        authority = build_review_authority_gap(doc, entry.get("reviewer"), role)
        if authority:
            gaps.append(authority)
            continue
    return gaps


def build_charge(doc: dict[str, Any], jobs: int) -> tuple[int, dict[str, Any]] | None:
    """Spend job budget, or park the build when there is none left.

    The correction loop is what this bounds. A reviewer that keeps finding
    things resumes the writer, reruns the checks, and reviews again; without a
    charge on each round the loop has no floor and the only thing that ever
    ends it is somebody noticing. Parking here preserves the files on disk and
    hands the decision back to planning, which is the point.
    """
    budget = doc.setdefault("budget", {"jobs_max": None, "jobs_used": 0})
    used = int(budget.get("jobs_used") or 0) + jobs
    jobs_max = budget.get("jobs_max")
    if isinstance(jobs_max, int) and used > jobs_max:
        return build_park(
            doc,
            "budget_exhausted",
            f"{used} jobs requested against a budget of {jobs_max}; files are on disk",
        )
    budget["jobs_used"] = used
    return None


def build_claim(doc: dict[str, Any]) -> dict[str, Any] | None:
    claim = doc.get("writer_claim")
    return claim if isinstance(claim, dict) else None


def build_claim_live(doc: dict[str, Any], now: float) -> dict[str, Any] | None:
    """The reservation that currently owns the writer, or None.

    A consumed claim is spent and an expired one is abandoned. Both let the
    next invocation reserve the writer again; neither lets a *concurrent* one
    do it, which is the difference between recovering a dead run and running
    two writers over the same worktree.
    """
    claim = build_claim(doc)
    if claim is None or claim.get("consumed"):
        return None
    try:
        expires = float(claim.get("expires_epoch") or 0)
    except (TypeError, ValueError):
        return None
    return claim if expires > now else None


def build_claim_has_launch(claim: dict[str, Any] | None) -> bool:
    """True once a claim owns a durable launch intent or local job.

    Wall-clock lease expiry may transfer an idle reservation, but it cannot
    make a queued/running writer disappear. Completion is the only operation
    that consumes a launched claim, so a slow writer never gains a concurrent
    replacement merely because its original Rewst invocation timed out.
    """
    if not isinstance(claim, dict) or claim.get("consumed"):
        return False
    return claim.get("launch_state") in ("intent", "queued") or bool(claim.get("job_id"))


def build_writer_ids(build_id: str, claim_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{build_id}\0{claim_id}".encode()).hexdigest()[:32]
    return digest, str(uuid.UUID(hex=digest))


def build_gate_record(doc: dict[str, Any], action: str, target: str, change_id: str, by: str, why: str) -> dict[str, Any]:
    entry = {
        "action": action,
        "from": doc.get("stage"),
        "to": target,
        "at": utcnow(),
        "change_id": change_id,
        "by": by,
        "why": why,
    }
    gates = [g for g in (doc.get("gates") or []) if isinstance(g, dict)]
    doc["gates"] = gates[-(BUILD_GATE_RECORDS_KEPT - 1):] + [entry]
    return entry


def build_gate_transition(
    doc: dict[str, Any], action: str, change_id: str, by: str, why: str
) -> dict[str, Any] | None:
    """Move the build across a gate on the authority of a receipt just verified.

    The caller is an operation that has already established the evidence --
    buildChecks with a green phase, buildReview with an independent PASS,
    buildFinalize with a pushed commit. Nothing here reads a request field, so
    there is no shape a caller can send that reaches this function without the
    receipt existing first.

    Returns the gate record, or None when the build is not at the stage this
    transition leaves (a replay that already moved).
    """
    frm, _to = BUILD_TRANSITIONS[action]
    if doc.get("stage") != frm:
        return None
    target = build_transition_target(doc, action)
    entry = build_gate_record(doc, action, target, change_id, by, why)
    if action == "verify_passed":
        # Same boundary buildAdvance recorded, for the same reason: a later
        # lease takeover must not roll back where verified work ended.
        doc["verified_boundary"] = {
            "stage": target,
            "head": doc.get("head"),
            "at": utcnow(),
            "verification": doc.get("verification"),
        }
    doc["stage"] = target
    if target in BUILD_TERMINAL:
        doc["lease"] = None
    return entry


def build_pending_gate(doc: dict[str, Any], change_id: str) -> tuple[str | None, str]:
    """The one gate transition this build's own receipts already justify.

    Read entirely from durable state at the change now on disk. This is what a
    run that died between recording a green phase and recording the transition
    comes back to: the evidence is there, so the transition is owed, and
    re-running the phase would spend a round of budget to learn it again.
    """
    stage = doc.get("stage")
    if stage == "verifying":
        clean, why = build_checks_clean(doc, "fast", change_id)
        if not clean:
            return None, why
        return "verify_passed", "the declared fast checks are clean for this change"
    if stage == "review":
        clean, why = build_checks_clean(doc, "fast", change_id)
        if not clean:
            return None, why
        gaps = build_review_gaps(doc, change_id) + build_visual_gaps(doc, change_id)
        if gaps:
            return None, gaps[0]
        roles = build_review_roles(doc)
        return "review_passed", (
            "every required review is clean and independent for this change: "
            + ", ".join(BUILD_REVIEW_ROLE_LABEL[role] for role in roles)
        )
    return None, f"stage '{stage}' has no gate waiting on a recorded receipt"


def build_next_review_role(doc: dict[str, Any], change_id: str) -> tuple[str | None, str]:
    """The one review this invocation should run, and why, or None.

    Findings come first, across every role, before any missing review is run.
    A behavior reviewer that said no has already invalidated the diff the
    security reviewer would be reading, so spending a round on it buys a
    verdict about code that is about to be rewritten.
    """
    for role in build_review_roles(doc):
        entry = build_last_review(doc, change_id, role)
        if entry is not None:
            evidence_gap = build_review_evidence_gap(
                doc, entry, change_id, BUILD_REVIEW_ROLE_LABEL[role]
            )
            if evidence_gap:
                return role, evidence_gap
        if entry is not None and entry.get("verdict") != "PASS":
            return None, f"the {BUILD_REVIEW_ROLE_LABEL[role]} review returned findings"
    for role in build_review_roles(doc):
        label = BUILD_REVIEW_ROLE_LABEL[role]
        entry = build_last_review(doc, change_id, role)
        if entry is None:
            return role, f"no {label} review for this change"
        gap = build_review_authority_gap(doc, entry.get("reviewer"), role)
        if gap:
            # A recorded PASS from a reviewer that cannot be independent is not
            # a review. Re-reviewing is the only thing that moves it, and the
            # route has to name a reviewer that can.
            return role, gap
    return None, ""


def build_path_step(doc: dict[str, Any], change_id: str) -> tuple[str, str]:
    """Name the one step this invocation may run, and why.

    Reading the step off the document rather than taking it from the caller is
    what makes the workflow replayable. A Rewst run that timed out after its
    writer finished comes back, is told the build is at `fast_checks`, and
    does not start a second writer.

    Every answer is a function of the durable document and the change currently
    on disk. Nothing here consults the caller, and nothing here writes, so an
    invocation that dies immediately after asking costs the build nothing.
    """
    stage = doc.get("stage")
    if stage == "parked":
        return "parked", str(doc.get("park_reason") or "parked")
    if stage == "pr_opened" or doc.get("pr_ready"):
        return "done", "this build has already produced its PR-ready commit"
    if build_budget_left(doc) == 0:
        return "parked", "budget_exhausted"
    if stage == "created":
        return "preflight", "no writer has been launched yet"
    if stage == "writing":
        result = doc.get("writer_result")
        if isinstance(result, dict) and result.get("status") in ("error", "timeout"):
            return "fix", "the writer stopped before success; its files and session are recoverable"
        return "write", "the writer holds the build"
    if stage == "verifying":
        red = build_red_fast_checks(doc, change_id)
        if red:
            # Rerunning a red phase unchanged is the loop that never ends. The
            # only thing that moves a red check is the writer, so say so.
            return "fix", "fast checks are red for this change: " + ", ".join(red)
        clean, why = build_checks_clean(doc, "fast", change_id)
        if clean:
            # Green and still at `verifying` means the run that got the verdict
            # did not survive to record the transition. Re-running the whole
            # phase to learn the same thing is a round of budget spent on an
            # answer already on disk.
            return "verify_passed", "the declared fast checks are clean for this change"
        return "fast_checks", why
    if stage == "evidence":
        if build_is_sensitive(doc) and build_needs_visual(doc):
            # A sensitive change can also be visible. This workflow does not
            # capture screenshots, so it names the round rather than parking:
            # the visual evidence path owns it, and the two sensitive reviews
            # still have to happen afterwards.
            return "evidence", (
                "the plan requires visual proof; graphwing-visual-evidence owns this round"
            )
        return "parked", (
            "mechanical builds do not capture visual evidence; the round runs on "
            "graphwing-visual-evidence"
        )
    if stage == "review":
        # Checks come first even at review. A correction turn that answered a
        # finding produced a new change id, which invalidated the fast phase as
        # well as the review, and reviewing a diff whose own declared checks
        # have not run is reviewing something that may not even compile.
        clean, why = build_checks_clean(doc, "fast", change_id)
        if not clean:
            red = build_red_fast_checks(doc, change_id)
            if red:
                return "fix", "fast checks are red for this change: " + ", ".join(red)
            return "fast_checks", why
        role, why = build_next_review_role(doc, change_id)
        if role is not None:
            return "review", why
        if why:
            return "fix", why
        visual = build_visual_gaps(doc, change_id)
        if visual:
            return "evidence", visual[0]
        # Not "review" again: a clean review that re-ran itself would spend a
        # round of budget to be told the same thing, and could only ever change
        # its mind for the worse.
        roles = build_review_roles(doc)
        return "review_passed", (
            "every required review is clean for this change: "
            + ", ".join(BUILD_REVIEW_ROLE_LABEL[role] for role in roles)
        )
    if stage == "ready":
        clean, why = build_checks_clean(doc, "integration", change_id)
        if not clean:
            return "integration", why
        return "finalize", "final checks and review are clean"
    return "parked", f"stage '{stage}' is not on this workflow's path"


def build_next_op(qs: dict[str, list[str]], repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """The stage switch. Read durable state, name the next allowed step.

    Deliberately read-only. The graph calls this first on every invocation, so
    it must be safe to call from a run that is about to be abandoned.
    """
    build_id = first_query(qs, "build_id")
    doc, err = build_open_path(build_id)
    if err:
        return err
    assert doc is not None
    live, park_detail = build_live_change(doc, repos)
    if live is None:
        # Not a park: this call did not ask to change anything, and parking on
        # a read would let a monitoring poll kill a build.
        out = build_public(doc)
        out["ok"] = False
        out["step"] = "parked"
        out["step_reason"] = str(park_detail)
        out["code"] = "worktree_mismatch"
        return 409, out
    step, reason = build_path_step(doc, live["change_id"])
    out = build_public(doc)
    out["ok"] = step != "parked"
    out["step"] = step
    out["step_reason"] = reason
    out["live_head"] = live["head"]
    out["live_branch"] = live["branch"]
    out["change_id"] = live["change_id"]
    out["class"] = build_class(doc)
    # Which review is due, named by the server rather than chosen by the graph.
    # A caller that picked the role could run the behavior lens twice and reach
    # `review_passed` on a sensitive build with nobody having looked at tenant
    # scope, which is the one thing the class is for.
    role, _why = build_next_review_role(doc, live["change_id"]) if step == "review" else (None, "")
    out["review_role"] = role
    out["review_reviewer"] = build_route_reviewer(doc, role) if role else None
    out["review_roles"] = list(build_review_roles(doc))
    out["reviews_outstanding"] = build_review_gaps(doc, live["change_id"])
    out["visual_outstanding"] = build_visual_gaps(doc, live["change_id"])
    # The correction text travels with the step, not with the run that produced
    # it. A graph that read the finding out of its own task history could only
    # correct a failure it had personally witnessed, which is every case except
    # the one this workflow exists for: the run before it died.
    out["finding"] = build_step_finding(doc, step, live["change_id"])
    if step == "parked":
        out["code"] = doc.get("park_reason") or "parked"
    return (200 if out["ok"] else 409), out


def build_reachable_recipes(doc: dict[str, Any], phase: str) -> tuple[list[str], dict[str, list[str]]]:
    """Every recipe this phase could ever be asked to run, and why.

    Reachable, not required: a changed-path rule's recipes are only *required*
    when a path matches, but they are reachable the moment the rule exists, and
    a preflight that only looked at the declared list let a rule name a
    hook-only or wrong-tree recipe and discover it the first time a diff
    happened to match — after the writer had already spent its turn.
    """
    verification = doc.get("verification") or {}
    names: list[str] = []
    why: dict[str, list[str]] = {}
    for name in verification.get(phase) or []:
        if not isinstance(name, str):
            continue
        if name not in why:
            names.append(name)
        why.setdefault(name, []).append("declared")
    for rule in verification.get("path_rules") or []:
        if not isinstance(rule, dict) or build_rule_phase(rule) != phase:
            continue
        for name in rule.get("recipes") or []:
            if not isinstance(name, str):
                continue
            if name not in why:
                names.append(name)
            why.setdefault(name, []).append(f"path:{rule.get('match')}")
    return names, why


def build_preflight_recipes(doc: dict[str, Any], repos: dict[str, str]) -> list[dict[str, Any]]:
    """Check every reachable recipe against the directory it will really run in.

    Three separate questions, per phase, and the phase decides where the answer
    comes from. Fast checks resolve against the build's worktree. Integration
    resolves against each stack the catalog declares clean, because that is
    where those recipes will execute — resolving them against the worktree
    would pass a recipe that cannot run on a clean checkout at all.

    Strict across clean stacks on purpose: the graph picks which one to use at
    check time, so a recipe that fails to bind on any of them is a gap the
    operator fixes now rather than a coin toss later.
    """
    failures: list[dict[str, Any]] = []
    worktree = Path(str(doc.get("worktree")))
    stacks = build_stack_catalog()
    clean = build_clean_stack_names(stacks)

    for rule in (doc.get("verification") or {}).get("path_rules") or []:
        if not isinstance(rule, dict):
            continue
        declared = rule.get("phase")
        if declared is not None and declared not in BUILD_CHECK_PHASES:
            failures.append(
                {
                    "check": "verification.path_rules",
                    "code": "bad_rule_phase",
                    "detail": (
                        f"path rule '{rule.get('match')}' declares phase '{declared}', which is "
                        "neither fast nor integration"
                    ),
                }
            )

    for phase in BUILD_CHECK_PHASES:
        names, _why = build_reachable_recipes(doc, phase)
        if not names:
            failures.append(
                {
                    "check": f"verification.{phase}",
                    "code": "no_recipes",
                    "detail": (
                        f"the plan declared no {phase} checks and no path rule can add one; "
                        "finalization requires a clean verdict for every phase, so this build "
                        "could never reach a commit"
                    ),
                }
            )
            continue
        if phase == "integration":
            for name in clean:
                gap = build_stack_isolation_gap(doc, stacks[name])
                if gap:
                    failures.append(
                        {
                            "check": "verification.integration",
                            "code": "not_a_clean_stack",
                            "detail": gap,
                        }
                    )
            targets = [(Path(str(stacks[n]["cwd"])), f"clean stack '{n}'") for n in clean]
            if not targets:
                failures.append(
                    {
                        "check": "verification.integration",
                        "code": "no_clean_stack",
                        "detail": (
                            f"this build reaches integration recipes ({', '.join(names)}) but no "
                            'stack in stacks.json declares "role": "clean"'
                        ),
                        "recipes": names,
                    }
                )
                continue
        else:
            targets = [(worktree, "this build's worktree")]

        for target, label in targets:
            catalog = build_recipe_catalog(repos, target, label)
            unknown = [n for n in names if n not in catalog]
            if unknown:
                failures.append(
                    {
                        "check": f"verification.{phase}",
                        "code": "unknown_recipe",
                        "detail": f"no such recipe: {', '.join(unknown)}",
                        "unknown": unknown,
                        "allowed": sorted(catalog),
                    }
                )
            known = [n for n in names if n in catalog]
            stray = [n for n in known if catalog[n].get("unresolvable")]
            if stray:
                # The one that reads as a pass and is not. A recipe pinned to
                # the deployed $GRAPHWING_HOME copy runs green against code the
                # phase never touched, and the gate believes it.
                failures.append(
                    {
                        "check": f"verification.{phase}",
                        "code": "recipe_wrong_worktree",
                        "detail": "; ".join(str(catalog[n]["unresolvable"]) for n in stray[:4]),
                        "unresolvable": stray,
                    }
                )
            if known and not any(catalog[n]["coverage"] for n in known):
                failures.append(
                    {
                        "check": f"verification.{phase}",
                        "code": "hook_not_coverage",
                        "detail": (
                            f"{phase} reaches only hook recipes ({', '.join(known)}) on {label}; "
                            "a pre-commit hook is not unit, type, or integration coverage"
                        ),
                    }
                )
    return failures


def build_preflight(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Everything that must be true before a writer is allowed to exist.

    Read-only on purpose. A failure here is not a park — the graph decides
    whether the build parks, because a caller with a stale recipe name should
    be able to fix tests.json and try again without having burned the build.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    doc, open_err = build_open_mechanical(data.get("build_id"))
    if open_err:
        return open_err
    assert doc is not None
    stage = doc.get("stage")
    if stage not in ("created", "writing"):
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "stage": stage,
            "error": f"preflight runs before the writer; build is at '{stage}'",
            "code": "invalid_transition",
        }

    failures: list[dict[str, Any]] = []

    gaps = build_route_stamp_gaps(doc)
    if gaps:
        failures.append(
            {
                "check": "route",
                "code": "missing_route_stamp",
                "detail": f"route is missing {', '.join(gaps)}",
                "missing": gaps,
            }
        )
    for detail in build_sensitive_route_gaps(doc):
        failures.append(
            {
                "check": "route",
                "code": "invalid_sensitive_route",
                "detail": detail,
            }
        )

    if build_budget_left(doc) is None:
        # The correction loop is what this bounds. Without a ceiling a reviewer
        # that keeps finding things resumes the writer forever, and the only
        # thing that ever ends the run is somebody noticing.
        failures.append(
            {
                "check": "budget",
                "code": "no_job_budget",
                "detail": "a mechanical build needs a finite budget.jobs_max; the correction loop has no other floor",
            }
        )

    # Names only: this is the list the response reports, and "is the name real"
    # is answered per phase against the directory that phase runs in.
    catalog = build_recipe_catalog(repos)
    verification = doc.get("verification") or {}
    failures.extend(build_preflight_recipes(doc, repos))

    branch = doc.get("branch")
    if isinstance(branch, str) and branch.split("/")[-1] in BUILD_TRUNK_BRANCHES:
        failures.append(
            {
                "check": "branch",
                "code": "trunk_branch",
                "detail": f"'{branch}' is a trunk branch; a pre-PR build needs a story branch",
            }
        )

    live, park_detail = build_resolve_identity(doc, repos)
    worktree_entries: list[str] = []
    if live is None:
        failures.append({"check": "worktree", "code": "worktree_mismatch", "detail": str(park_detail)})
    else:
        if live["branch"] != doc.get("branch"):
            failures.append(
                {
                    "check": "branch",
                    "code": "branch_mismatch",
                    "detail": f"worktree is on '{live['branch']}', build was opened on '{doc.get('branch')}'",
                }
            )
        status = git_status(Path(str(doc.get("worktree"))))
        if not status.get("ok"):
            failures.append(
                {
                    "check": "worktree",
                    "code": "identity_unreadable",
                    "detail": str(status.get("error") or "git status failed")[:BUILD_REDACT_MAX_STRING],
                }
            )
        else:
            worktree_entries = list(status.get("entries") or [])
            conflicts = build_worktree_conflicts(worktree_entries)
            if conflicts:
                failures.append(
                    {
                        "check": "worktree",
                        "code": "worktree_conflicted",
                        "detail": "unmerged paths: " + ", ".join(conflicts[:8]),
                        "entries": conflicts[:8],
                    }
                )
            elif stage == "created":
                # Once the writer is running its own edits are the point. Before
                # it starts there is nobody those edits could belong to, and
                # committing them later would ship somebody else's work under
                # this build's ticket.
                dirt = build_tracked_dirt(worktree_entries)
                if dirt:
                    failures.append(
                        {
                            "check": "worktree",
                            "code": "worktree_dirty",
                            "detail": "tracked changes are already present: " + ", ".join(dirt[:8]),
                            "entries": dirt[:8],
                        }
                    )

    out = {
        "ok": not failures,
        "build_id": doc.get("build_id"),
        "stage": stage,
        "branch": doc.get("branch"),
        "head": live["head"] if live else doc.get("head"),
        "declared": {phase: list(verification.get(phase) or []) for phase in BUILD_CHECK_PHASES},
        "known_recipes": sorted(catalog),
        "failures": failures,
        "budget_left": build_budget_left(doc),
    }
    if failures:
        out["error"] = failures[0]["detail"]
        out["code"] = failures[0]["code"]
        return 409, out
    return 200, out


def build_claim_request(data: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate a reservation request without touching the build."""
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return None, {"ok": False, "error": "event_id is required", "code": "bad_event_id"}
    holder = data.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        return None, {"ok": False, "error": "holder is required", "code": "missing_holder"}
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return None, holder_err
    kind = data.get("kind") if data.get("kind") not in ("", None) else "initial"
    if kind not in BUILD_CLAIM_KINDS:
        return None, {
            "ok": False,
            "error": "kind must be initial, correction, or design",
            "code": "bad_claim_kind",
            "allowed": list(BUILD_CLAIM_KINDS),
        }
    seconds, seconds_err = parse_optional_int(data, "lease_seconds", 1, BUILD_LEASE_MAX_SECONDS)
    if seconds_err:
        return None, seconds_err
    return {"event_id": event_id, "holder": holder, "kind": kind,
            "lease_seconds": seconds or BUILD_LEASE_SECONDS}, None


def build_claim_op(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Reserve the one writer slot, durably, before any agent is launched.

    This is the line the whole path was missing. buildBrief composed a prompt
    and the graph launched an agent off it, and nothing had been written down
    yet -- so a Rewst retry, or two invocations arriving together, each got a
    brief and each started a writer on the same worktree. The second one is
    invisible: it edits the same files, the checks measure whatever the two of
    them left behind, and the budget records one turn.

    Reserving first inverts that. The claim is the side effect, taken under the
    build lock and keyed on the event id, so a replay gets the same reservation
    back and a concurrent call gets `writer_claimed`. The budget is charged
    here rather than at the transition after the writer finished, because an
    invocation that dies mid-writer has still spent the turn.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    request, bad = build_claim_request(data)
    if bad:
        return 400, bad
    assert request is not None
    event_id = str(request["event_id"])
    holder = str(request["holder"])
    kind = str(request["kind"])
    fingerprint = event_fingerprint(
        {"build_id": data.get("build_id"), "action": "claim", "kind": kind, "holder": holder}
    )

    with BUILD_LOCK:
        doc, open_err = build_open_writer(data.get("build_id"), kind)
        if open_err:
            return open_err
        assert doc is not None
        build_id = str(doc.get("build_id"))

        seen = build_event_seen(doc)
        if event_id in seen:
            record = seen.get(event_id) or {}
            if record.get("fingerprint") != fingerprint:
                out = build_public(doc)
                out["ok"] = False
                out["replayed"] = False
                out["error"] = "event_id was already used with different input"
                out["code"] = "idempotency_conflict"
                return 409, out
            out = build_public(doc)
            out["ok"] = int(record.get("status") or 200) == 200
            out["replayed"] = True
            out["receipt"] = record.get("receipt")
            out["claim"] = build_claim(doc)
            return int(record.get("status") or 200), out

        live, park = build_identity_park(doc, "claim", data.get("worktree"), None, None, repos)
        if park is not None:
            reason, detail = park
            return build_park(doc, reason, detail, event_id, fingerprint, holder)
        assert live is not None
        change_id = build_change_id(Path(str(doc.get("worktree"))), live["head"])

        now = time.time()
        lease = build_lease_live(doc, now)
        if lease is not None and lease.get("holder") != holder:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "another caller holds the build lease"
            out["code"] = "lease_held"
            return 409, out

        if build_budget_left(doc) is None:
            # An unbounded correction loop has no floor: a reviewer that keeps
            # finding things resumes the writer forever and the only thing that
            # ends it is somebody noticing. Refuse the launch, not the build.
            #
            # A design turn has the same floor for a different reason. Its turn
            # count is deliberately unbounded — the story says so — so the job
            # budget is the only thing standing between "the user keeps asking
            # for changes" and a laptop that writes code all night.
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "a build needs a finite budget.jobs_max before a writer is launched"
            out["code"] = "no_job_budget"
            return 409, out

        launched = build_claim(doc)
        if build_claim_has_launch(launched):
            out = build_public(doc)
            out["ok"] = False
            out["claim"] = launched
            out["error"] = "the current writer claim already owns a launch intent or job"
            out["code"] = "writer_claimed"
            return 409, out

        if kind == BUILD_DESIGN_CLAIM_KIND:
            # Design turns are governed by the visual-iteration state at the
            # evidence stage. Every other turn remains on the shared
            # mechanical/sensitive path so sensitive routing is never bypassed.
            step, reason = build_iteration_step(doc, change_id)
            wanted = "write"
        else:
            step, reason = build_path_step(doc, change_id)
            wanted = "preflight" if kind == "initial" else "fix"

        if step != wanted:
            # The document, not the caller, says which turn is due. A
            # correction claim on a build whose review is clean is a graph
            # that read its own history instead of asking.
            out = build_public(doc)
            out["ok"] = False
            out["step"] = step
            out["step_reason"] = reason
            out["error"] = f"a '{kind}' claim runs at step '{wanted}'; this build is at '{step}'"
            out["code"] = "claim_not_due"
            return 409, out

        held = build_claim_live(doc, now)
        if held is not None:
            out = build_public(doc)
            out["ok"] = False
            out["claim"] = held
            out["error"] = (
                f"'{held.get('holder')}' already holds the writer reservation for this build "
                f"until {held.get('expires_at')}"
            )
            out["code"] = "writer_claimed"
            return 409, out

        session = doc.get("writer_session")
        if kind != "initial" and not (isinstance(session, str) and session.strip()):
            out = build_public(doc)
            out["ok"] = False
            out["error"] = (
                "a correction turn must resume the writer that made the change"
                if kind == "correction"
                else "a design turn must resume the writer that made the change under review"
            )
            out["code"] = "no_writer_session"
            return 409, out

        charged = build_charge(doc, 1)
        if charged:
            return charged

        stale = build_claim(doc)
        superseded = (
            stale.get("claim_id")
            if isinstance(stale, dict) and stale.get("claim_id") != event_id
            else None
        )
        _job_id, reserved_session = build_writer_ids(build_id, event_id)
        initial_session = None if (doc.get("route") or {}).get("launcher") == "codex" else reserved_session
        claim = {
            "claim_id": event_id,
            "kind": kind,
            "holder": holder,
            "at": utcnow(),
            "stage": doc.get("stage"),
            "step": step,
            "head": live["head"],
            "change_id": change_id,
            # Initial reservations open the session; correction and design
            # reservations bind the one already recorded, so a resumed turn can
            # never be pointed at a different agent than the one that wrote the
            # files. The user's note is about the page that writer built.
            "writer_session": initial_session if kind == "initial" else session,
            "jobs_spent": 1,
            # Only a correction is a strike. A design turn spends a job and
            # nothing else: it is the user iterating, and counting it against
            # the change would make asking for a smaller button look like the
            # writer had failed twice.
            "strike_free": kind == BUILD_DESIGN_CLAIM_KIND,
            "consumed": False,
            "superseded": superseded,
            "expires_epoch": now + int(request["lease_seconds"]),
            "expires_at": datetime.fromtimestamp(
                now + int(request["lease_seconds"]), timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        doc["writer_claim"] = claim
        if kind == BUILD_DESIGN_CLAIM_KIND:
            turn = build_iteration_open_turn(doc)
            if turn is not None:
                turn["claim_id"] = event_id
                turn["claimed_at"] = utcnow()
        doc["lease"] = {
            "holder": holder,
            "acquired_at": utcnow(),
            "expires_epoch": now + int(request["lease_seconds"]),
            "expires_at": claim["expires_at"],
        }
        receipt = {
            "event_id": event_id,
            "action": "claim",
            "kind": kind,
            "at": utcnow(),
            "holder": holder,
            "build_id": build_id,
            "stage": doc.get("stage"),
            "step": step,
            "head": live["head"],
            "change_id": change_id,
            "writer_session": claim["writer_session"],
            "jobs_spent": 1,
            "budget_left": build_budget_left(doc),
            "superseded": superseded,
            "strike_free": claim["strike_free"],
            "strikes": build_strike_counts(doc),
        }
        build_record_event(doc, event_id, fingerprint, receipt, 200)
        write_err = write_build(doc, build_id)
        if write_err:
            return int(write_err["status"]), write_err
        out = build_public(doc)
        out["ok"] = True
        out["replayed"] = False
        out["receipt"] = receipt
        out["claim"] = claim
        out["change_id"] = change_id
        out["step"] = step
        return 200, out


def build_gate_op(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Record the gate transition this build's own receipts already justify.

    The transition buildAdvance is no longer allowed to perform on this path.
    Nothing here reads an outcome from the request: the pending gate is derived
    from the checks and reviews on disk, at the change on disk, and a build
    with no such evidence gets `not_ready` rather than a stage move.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {"build_id", "event_id", "holder", "release_lease", "worktree"}
    extra = sorted(set(data) - allowed)
    if extra:
        return 400, {"ok": False, "error": f"unknown field: {extra[0]}", "code": "extra_field"}
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return 400, {"ok": False, "error": "event_id is required", "code": "bad_event_id"}
    holder = data.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        return 400, {"ok": False, "error": "holder is required", "code": "missing_holder"}
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return 400, holder_err
    release_lease, release_err = parse_strict_bool(data, "release_lease", "bad_release_lease")
    if release_err:
        return 400, release_err
    fingerprint = event_fingerprint(
        {"build_id": data.get("build_id"), "action": "gate", "holder": holder,
         "release_lease": release_lease}
    )

    with BUILD_LOCK:
        doc, open_err = build_open_mechanical(data.get("build_id"))
        if open_err:
            return open_err
        assert doc is not None
        build_id = str(doc.get("build_id"))

        seen = build_event_seen(doc)
        if event_id in seen:
            record = seen.get(event_id) or {}
            if record.get("fingerprint") != fingerprint:
                out = build_public(doc)
                out["ok"] = False
                out["replayed"] = False
                out["error"] = "event_id was already used with different input"
                out["code"] = "idempotency_conflict"
                return 409, out
            out = build_public(doc)
            out["ok"] = int(record.get("status") or 200) == 200
            out["replayed"] = True
            out["receipt"] = record.get("receipt")
            return int(record.get("status") or 200), out

        live, park = build_identity_park(doc, "gate", data.get("worktree"), None, None, repos)
        if park is not None:
            reason, detail = park
            return build_park(doc, reason, detail, event_id, fingerprint, holder)
        assert live is not None
        change_id = build_change_id(Path(str(doc.get("worktree"))), live["head"])

        now = time.time()
        lease = build_lease_live(doc, now)
        if lease is not None and lease.get("holder") != holder:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "another caller holds the build lease"
            out["code"] = "lease_held"
            return 409, out

        expected_applied = None
        for suffix, candidate in (
            (".verify-passed", "verify_passed"),
            (".review-passed", "review_passed"),
        ):
            if event_id.endswith(suffix):
                expected_applied = candidate
                break
        applied = next(
            (
                gate
                for gate in reversed(doc.get("gates") or [])
                if isinstance(gate, dict)
                and gate.get("action") == expected_applied
                and gate.get("change_id") == change_id
                and gate.get("to") == doc.get("stage")
            ),
            None,
        )
        if applied is not None:
            if release_lease:
                doc["lease"] = None
            receipt = {
                "event_id": event_id,
                "action": expected_applied,
                "from": applied.get("from"),
                "to": applied.get("to"),
                "at": applied.get("at"),
                "holder": holder,
                "build_id": build_id,
                "change_id": change_id,
                "head": live["head"],
                "jobs_spent": 0,
                "budget_left": build_budget_left(doc),
                "already_applied": True,
                "applied_by": applied.get("by"),
            }
            build_record_event(doc, event_id, fingerprint, receipt, 200)
            write_err = write_build(doc, build_id)
            if write_err:
                return int(write_err["status"]), write_err
            out = build_public(doc)
            out["ok"] = True
            out["replayed"] = False
            out["receipt"] = receipt
            out["change_id"] = change_id
            out["code"] = "already_applied"
            return 200, out

        action, why = build_pending_gate(doc, change_id)
        if action is None:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = why
            out["code"] = "not_ready"
            return 409, out
        boundary = doc.get("verified_boundary")
        if action in BUILD_POST_VERIFY_ACTIONS and isinstance(boundary, dict):
            verified_head = boundary.get("head")
            if verified_head and verified_head != live["head"]:
                return build_park(
                    doc,
                    "head_moved_after_verify",
                    f"verification passed at {verified_head}, worktree HEAD is now {live['head']}",
                    event_id,
                    fingerprint,
                    holder,
                )
        stage = doc.get("stage")
        entry = build_gate_transition(doc, action, change_id, "buildGate", why)
        if entry is None:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = f"action '{action}' is not allowed from stage '{stage}'"
            out["code"] = "invalid_transition"
            return 409, out
        if release_lease:
            doc["lease"] = None
        receipt = {
            "event_id": event_id,
            "action": action,
            "from": entry["from"],
            "to": entry["to"],
            "at": entry["at"],
            "holder": holder,
            "build_id": build_id,
            "change_id": change_id,
            "head": live["head"],
            "jobs_spent": 0,
            "budget_left": build_budget_left(doc),
            "why": why,
        }
        build_record_event(doc, event_id, fingerprint, receipt, 200)
        write_err = write_build(doc, build_id)
        if write_err:
            return int(write_err["status"]), write_err
        out = build_public(doc)
        out["ok"] = True
        out["replayed"] = False
        out["receipt"] = receipt
        out["change_id"] = change_id
        return 200, out


def build_brief(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """The writer's whole world: one approved ticket and how it will be judged.

    Composed here rather than in the graph so "only the approved ticket" is a
    property of the code. A prompt assembled out of template strings acquires
    the slice index, the neighbouring tickets, and the route internals one
    convenient interpolation at a time, and the writer starts solving the
    epic.

    Read-only, and that is load-bearing. This used to charge a job of budget on
    a correction turn, which made composing a prompt a side effect: a caller
    that asked for the same brief twice paid twice, and the charge landed after
    the point where a second writer could already have been launched. The
    charge moved to buildClaim, which happens before any agent exists, and this
    op now refuses outright unless that reservation is live.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    finding = data.get("finding")
    if finding is not None and not isinstance(finding, str):
        return 400, {"error": "finding must be a string", "code": "bad_finding"}
    with BUILD_LOCK:
        doc, open_err = build_open_writer(
            data.get("build_id"), build_current_claim_kind(data.get("build_id"))
        )
        if open_err:
            return open_err
        assert doc is not None
        claim = build_claim_live(doc, time.time())
        # A design brief carries no finding: the user's note is on the turn, not
        # in the request, so a caller cannot put words in the planning session's
        # mouth by sending its own.
        wanted = (
            BUILD_DESIGN_CLAIM_KIND
            if isinstance(claim, dict) and claim.get("kind") == BUILD_DESIGN_CLAIM_KIND
            else ("correction" if finding else "initial")
        )
        if wanted == BUILD_DESIGN_CLAIM_KIND and finding:
            return 409, {
                "ok": False,
                "build_id": doc.get("build_id"),
                "error": "a design turn's instructions are the recorded user notes, not a supplied finding",
                "code": "claim_mismatch",
            }
        if claim is None:
            return 409, {
                "ok": False,
                "build_id": doc.get("build_id"),
                "error": (
                    "no live writer reservation; buildClaim must run before a brief is composed"
                ),
                "code": "claim_required",
            }
        if claim.get("kind") != wanted:
            return 409, {
                "ok": False,
                "build_id": doc.get("build_id"),
                "error": (
                    f"the live reservation is a '{claim.get('kind')}' claim; this is a "
                    f"'{wanted}' brief"
                ),
                "code": "claim_mismatch",
            }
    ticket = doc.get("ticket")
    if not isinstance(ticket, str) or not ticket.strip():
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "error": "build has no approved ticket to hand the writer",
            "code": "missing_ticket",
        }
    gaps = build_route_stamp_gaps(doc)
    if gaps:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "error": f"route is missing {', '.join(gaps)}",
            "code": "missing_route_stamp",
        }
    head = file_head(Path(str(doc.get("worktree"))), ticket)
    if not head.get("ok"):
        return int(head.get("status", 400)), {
            "ok": False,
            "build_id": doc.get("build_id"),
            "error": head.get("error") or f"could not read ticket {ticket}",
            "code": head.get("code") or "ticket_unreadable",
        }

    session = doc.get("writer_session")
    if (finding or wanted == BUILD_DESIGN_CLAIM_KIND) and not session:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "error": "a correction turn must resume the writer that made the change",
            "code": "no_writer_session",
        }
    turn = build_iteration_open_turn(doc) if wanted == BUILD_DESIGN_CLAIM_KIND else None
    if wanted == BUILD_DESIGN_CLAIM_KIND and turn is None:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "error": "no design turn is open; a design brief has no notes to hand the writer",
            "code": "no_open_turn",
        }

    verification = doc.get("verification") or {}
    lines = [
        str(head.get("text") or ""),
        "",
        "VERIFICATION",
        "These named recipes decide whether this change is done. Graphwing runs them",
        "after you return; do not search for or run the named recipes yourself.",
    ]
    for phase in BUILD_CHECK_PHASES:
        names = [n for n in (verification.get(phase) or []) if isinstance(n, str)]
        lines.append(f"- {phase}: {', '.join(names) if names else 'none declared'}")
    if verification.get("requires_red"):
        lines.append(
            "- This change requires RED evidence: write the failing test first and leave "
            "the run that failed in your summary."
        )
    lines.append(
        "Stage the files you changed and return after staging. Do not commit, do not push, do not open a PR: "
        "finalization is a separate step and runs only after the review is clean."
    )
    if finding:
        # The finding is the whole reason for the resumed turn, so it goes last
        # and is named as a correction. Restating the ticket above it is what
        # keeps a writer that has been resumed three times from drifting into
        # fixing the review instead of the change.
        lines.extend(
            [
                "",
                "REVIEW FINDINGS. The files are on disk. Continue this change in the same",
                "session; do not start over.",
                redact_secrets(finding.strip())[:COMPACT_MAX_CHARS],
            ]
        )
    if turn is not None:
        lines.extend(build_iteration_brief_lines(doc, turn))

    route = doc.get("route") or {}
    return 200, {
        "ok": True,
        "build_id": doc.get("build_id"),
        "repo": doc.get("repo"),
        "ticket": ticket,
        "prompt": "\n".join(lines)[:PROMPT_MAX_CHARS],
        "resume": bool(finding) or turn is not None,
        "turn": turn.get("turn") if turn is not None else None,
        "claim_id": claim.get("claim_id"),
        "harness_session": session,
        "launcher": route.get("launcher"),
        "model": route.get("model"),
        "max_turns": route.get("max_turns"),
        "run_budget_seconds": route.get("run_budget_seconds"),
        "budget_left": build_budget_left(doc),
    }


def build_writer_launch(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Materialize one deterministic local writer job for an exact build claim."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {
        "build_id", "claim_id", "holder", "response_webhook_url", "response_webhook_token"
    }
    extra = sorted(set(data) - allowed)
    if extra:
        return 400, {"ok": False, "error": f"unknown field: {extra[0]}", "code": "extra_field"}
    claim_id = data.get("claim_id")
    if not isinstance(claim_id, str) or not BUILD_EVENT_ID_RE.fullmatch(claim_id):
        return 400, {"ok": False, "error": "claim_id is required", "code": "bad_claim_id"}
    holder = data.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        return 400, {"ok": False, "error": "holder is required", "code": "missing_holder"}
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return 400, holder_err
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err
    if not webhook_url:
        return 400, {"ok": False, "error": "response_webhook_url is required", "code": "missing_webhook_url"}

    # Compose from the durable build and claim rather than accepting a prompt,
    # cwd, session, model, or budget from the graph.
    launch_kind = build_current_claim_kind(data.get("build_id"))
    preview, preview_err = build_open_writer(data.get("build_id"), launch_kind)
    if preview_err:
        return preview_err
    assert preview is not None
    preview_claim = build_claim(preview)
    if not isinstance(preview_claim, dict) or preview_claim.get("claim_id") != claim_id:
        return 409, {"ok": False, "error": "claim is not current for this build", "code": "claim_mismatch"}
    finding = (
        build_step_finding(preview, "fix", str(preview_claim.get("change_id") or ""))
        if preview_claim.get("kind") == "correction"
        else ""
    )
    brief_status, brief = build_brief(
        json.dumps({"build_id": data.get("build_id"), "finding": finding}).encode(), repos
    )
    if brief_status != 200:
        return brief_status, brief
    if brief.get("launcher") not in {"codex", "claude", "grok"}:
        return 409, {
            "ok": False,
            "error": "the build writer must use a direct resumable harness",
            "code": "writer_not_resumable",
        }

    created = False
    with BUILD_LOCK:
        doc, open_err = build_open_writer(data.get("build_id"), launch_kind)
        if open_err:
            return open_err
        assert doc is not None
        build_id = str(doc.get("build_id"))
        claim = build_claim(doc)
        if not isinstance(claim, dict) or claim.get("claim_id") != claim_id:
            return 409, {"ok": False, "error": "claim is not current for this build", "code": "claim_mismatch"}
        if claim.get("holder") != holder:
            return 409, {"ok": False, "error": "claim belongs to another holder", "code": "claim_held"}
        if claim.get("consumed"):
            return 409, {"ok": False, "error": "claim was already completed", "code": "claim_consumed"}
        live, detail = build_live_change(doc, repos)
        if live is None:
            return build_park(doc, "worktree_mismatch", str(detail))
        if live["head"] != claim.get("head"):
            return build_park(
                doc, "head_moved", f"claim reserved {claim.get('head')}, worktree is {live['head']}"
            )

        job_id, reserved_session = build_writer_ids(build_id, claim_id)
        session = claim.get("writer_session")
        codex_initial = brief.get("launcher") == "codex" and claim.get("kind") == "initial"
        if (
            claim.get("job_id") not in (None, job_id)
            or (not codex_initial and (not isinstance(session, str) or not HARNESS_SESSION_RE.fullmatch(session)))
            or (claim.get("kind") == "initial" and not codex_initial and session != reserved_session)
            or (codex_initial and session is not None)
        ):
            return 409, {"ok": False, "error": "claim launch identity conflicts", "code": "launch_conflict"}
        if not claim.get("job_id"):
            claim["job_id"] = job_id
            claim["launch_state"] = "intent"
            claim["launch_intent_at"] = utcnow()
            claim["launch_stage"] = doc.get("stage")
            claim["launch_change_id"] = claim.get("change_id")
            if claim.get("kind") == "initial":
                if doc.get("stage") != "created":
                    return 409, {"ok": False, "error": "initial writer is not due", "code": "claim_not_due"}
                doc["stage"] = "writing"
            write_err = write_build(doc, build_id)
            if write_err:
                return int(write_err["status"]), write_err

        with JOB_LOCK:
            job = read_job(job_id)
            if job is None:
                if active_job_count() >= AGENT_MAX_CONCURRENT:
                    return 429, {"ok": False, "error": "too many in-flight agent jobs", "code": "busy"}
                log_ref = str(job_dir(job_id) / "stdout.log")
                job = {
                    "job_id": job_id,
                    "kind": "build_writer",
                    "status": "queued",
                    "profile": HOME_PROFILE,
                    "repo": doc.get("repo"),
                    "cwd": doc.get("worktree"),
                    "prompt": brief["prompt"],
                    "harness_session": session,
                    "harness_session_state": "pending" if codex_initial else "bound",
                    "launcher": brief.get("launcher"),
                    "model": brief.get("model"),
                    "max_turns": brief.get("max_turns") or AGENT_MAX_TURNS,
                    "run_budget_seconds": brief.get("run_budget_seconds") or AGENT_RUN_BUDGET,
                    "build_id": build_id,
                    "claim_id": claim_id,
                    "claim_kind": claim.get("kind"),
                    "launch_head": claim.get("head"),
                    "launch_change_id": claim.get("change_id"),
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
                (job_dir(job_id) / "prompt.txt").write_text(
                    wrap_prompt(job_id, str(brief["prompt"]), str(doc.get("worktree")))
                )
                write_job(job)
                created = True
            else:
                if (
                    job.get("build_id") != build_id
                    or job.get("claim_id") != claim_id
                    or job.get("harness_session") != session
                ):
                    return 409, {"ok": False, "error": "deterministic job id conflicts", "code": "job_conflict"}
                job["response_webhook_url"] = webhook_url
                job["response_webhook_token"] = webhook_token
                job["resume_url"] = webhook_url
                write_job(job)

        claim["launch_state"] = "queued"
        claim["queued_at"] = claim.get("queued_at") or utcnow()
        write_err = write_build(doc, build_id)
        if write_err:
            return int(write_err["status"]), write_err

    if created or job.get("status") == "queued":
        enqueue_agent(job)
    elif job.get("status") in ("completed", "failed") and isinstance(job.get("receipt"), dict):
        # A replay may attach a fresh Rewst wait to the one completed job. It
        # receives the stored receipt; the writer is never launched again.
        threading.Thread(
            target=deliver_webhook, args=(job, job["receipt"]),
            name=f"graphwing-redeliver-{job_id[:8]}", daemon=True,
        ).start()
    return 202, {
        "ok": True,
        "replayed": not created,
        "build_id": build_id,
        "claim_id": claim_id,
        "job_id": job_id,
        "status": job.get("status"),
        "harness_session": session,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def build_writer_complete(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Consume one local writer result and apply only its authorized transition."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {"build_id", "event_id", "holder", "claim_id", "job_id", "harness_session", "callback"}
    extra = sorted(set(data) - allowed)
    if extra:
        return 400, {"ok": False, "error": f"unknown field: {extra[0]}", "code": "extra_field"}
    event_id = data.get("event_id")
    claim_id = data.get("claim_id")
    job_id = data.get("job_id")
    session = data.get("harness_session")
    holder = data.get("holder")
    callback = data.get("callback")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return 400, {"ok": False, "error": "event_id is required", "code": "bad_event_id"}
    if not isinstance(claim_id, str) or not BUILD_EVENT_ID_RE.fullmatch(claim_id):
        return 400, {"ok": False, "error": "claim_id is required", "code": "bad_claim_id"}
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return 400, {"ok": False, "error": "job_id is invalid", "code": "bad_job_id"}
    if not isinstance(session, str) or not HARNESS_SESSION_RE.fullmatch(session):
        return 400, {"ok": False, "error": "harness_session is invalid", "code": "bad_harness_session"}
    if not isinstance(holder, str) or not holder.strip():
        return 400, {"ok": False, "error": "holder is required", "code": "missing_holder"}
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return 400, holder_err
    if not isinstance(callback, dict) or callback.get("status") not in ("ok", "error", "timeout"):
        return 400, {"ok": False, "error": "callback must be a terminal writer receipt", "code": "bad_callback"}
    fingerprint = event_fingerprint(
        {"build_id": data.get("build_id"), "action": "writer_complete", "holder": holder,
         "claim_id": claim_id, "job_id": job_id, "harness_session": session, "callback": callback}
    )

    with BUILD_LOCK:
        doc, open_err = build_open_writer(
            data.get("build_id"), build_current_claim_kind(data.get("build_id"))
        )
        if open_err:
            return open_err
        assert doc is not None
        seen = build_event_seen(doc)
        if event_id in seen:
            record = seen.get(event_id) or {}
            if record.get("fingerprint") != fingerprint:
                out = build_public(doc)
                out.update(ok=False, replayed=False, error="event_id was already used with different input",
                           code="idempotency_conflict")
                return 409, out
            out = build_public(doc)
            out["replayed"] = True
            out["receipt"] = record.get("receipt")
            return int(record.get("status") or 200), out

        claim = build_claim(doc)
        if not isinstance(claim, dict) or claim.get("claim_id") != claim_id:
            return 409, {"ok": False, "error": "claim is not current for this build", "code": "claim_mismatch"}
        if claim.get("holder") != holder:
            return 409, {"ok": False, "error": "claim belongs to another holder", "code": "claim_held"}
        if claim.get("job_id") != job_id or claim.get("writer_session") != session:
            return 409, {"ok": False, "error": "claim, job, and session do not match", "code": "completion_mismatch"}
        if claim.get("consumed"):
            return 409, {"ok": False, "error": "claim was already completed", "code": "claim_consumed"}
        expected_job_id, reserved_session = build_writer_ids(str(doc.get("build_id")), claim_id)
        codex_initial = (doc.get("route") or {}).get("launcher") == "codex" and claim.get("kind") == "initial"
        if job_id != expected_job_id or (claim.get("kind") == "initial" and not codex_initial and session != reserved_session):
            return 409, {"ok": False, "error": "writer identity is not deterministic", "code": "completion_mismatch"}
        live, detail = build_live_change(doc, repos)
        if live is None:
            return build_park(doc, "worktree_mismatch", str(detail))
        if live["head"] != claim.get("head"):
            return build_park(
                doc, "head_moved", f"writer launched at {claim.get('head')}, worktree is {live['head']}"
            )
        job = read_job(job_id)
        if not isinstance(job, dict):
            return 409, {"ok": False, "error": "writer job is missing", "code": "job_missing"}
        if (
            job.get("build_id") != doc.get("build_id")
            or job.get("claim_id") != claim_id
            or job.get("harness_session") != session
        ):
            return 409, {"ok": False, "error": "job does not belong to this claim", "code": "completion_mismatch"}
        authoritative = job.get("receipt")
        if job.get("status") not in ("completed", "failed") or not isinstance(authoritative, dict):
            return 409, {"ok": False, "error": "writer job has no terminal result", "code": "job_not_complete"}
        if canonical_json(authoritative) != canonical_json(callback):
            return 409, {"ok": False, "error": "callback does not match the local job result", "code": "callback_conflict"}

        status = str(authoritative.get("status"))
        result = {
            "claim_id": claim_id,
            "job_id": job_id,
            "harness_session": session,
            "kind": claim.get("kind"),
            "status": status,
            "summary": str(authoritative.get("summary") or "")[:500],
            "log_ref": authoritative.get("log_ref"),
            "launch_change_id": claim.get("launch_change_id") or claim.get("change_id"),
            "completed_change_id": live["change_id"],
            "at": utcnow(),
        }
        doc["writer_result"] = result
        doc["writer_session"] = session
        claim["consumed"] = True
        claim["consumed_at"] = utcnow()
        claim["launch_state"] = "completed"
        doc["lease"] = None
        frm = doc.get("stage")
        to = frm
        action = "writer_error"
        turn = (
            build_iteration_open_turn(doc)
            if claim.get("kind") == BUILD_DESIGN_CLAIM_KIND
            else None
        )
        if status == "ok" and claim.get("kind") == "initial":
            if frm != "writing":
                return 409, {"ok": False, "error": "initial writer is not at writing", "code": "invalid_transition"}
            action = "writer_done"
            to = build_transition_target(doc, "writer_done")
            doc["stage"] = to
            doc["head"] = live["head"]
        elif status == "ok" and claim.get("kind") == BUILD_DESIGN_CLAIM_KIND:
            # A design turn never moves the stage. The candidate it produced is
            # the next thing a person looks at, so the build stays at `evidence`
            # and the turn owes changed-area checks and a fresh round before
            # anybody is asked what they think of it.
            action = "design_turn_done"
        elif status == "ok":
            action = "correction_done"
            if frm == "writing":
                to = build_transition_target(doc, "writer_done")
                doc["stage"] = to
                doc["head"] = live["head"]
        if turn is not None:
            turn["writer_result"] = {
                k: result.get(k) for k in ("claim_id", "job_id", "status", "summary", "at")
            }
            turn["writer_status"] = status
            if status == "ok":
                turn["status"] = "written"
                turn["change_id"] = live["change_id"]
                turn["written_at"] = utcnow()
                record = build_iteration(doc)
                returned_change = record.get("returned_candidate_change_id")
                if returned_change and live["change_id"] != returned_change:
                    # Only the resumed design writer may discharge a final-gate
                    # return.  A manually moved worktree head cannot turn the
                    # pre-return evidence back into a commit candidate.
                    record["returned_candidate_writer_change_id"] = live["change_id"]

        receipt = {
            "event_id": event_id,
            "action": action,
            "from": frm,
            "to": to,
            "at": utcnow(),
            "holder": holder,
            "build_id": doc.get("build_id"),
            "claim_id": claim_id,
            "job_id": job_id,
            "harness_session": session,
            "status": status,
            "change_id": live["change_id"],
            "jobs_spent": 0,
            "budget_left": build_budget_left(doc),
        }
        build_record_event(doc, event_id, fingerprint, receipt, 200)
        write_err = write_build(doc, str(doc.get("build_id")))
        if write_err:
            return int(write_err["status"]), write_err
        out = build_public(doc)
        out["replayed"] = False
        out["receipt"] = receipt
        return 200, out


def build_run_recipe(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Run one recipe to a completed verdict, in this thread, every time.

    Never enqueued. testRun hands anything slow to the job queue and answers
    202, which is a receipt for having accepted the work — not for the work
    having passed. Routed into a gate that reads `ok`, a 202 is indistinguish-
    able from green, and the whole point of the gate is gone. The recipe's own
    timeout still applies; a check that runs long makes the gate call slow,
    which the caller solves with a response webhook, not by believing an
    acknowledgement.
    """
    result = run_cmd(list(spec["argv"]), cwd=spec["cwd"], timeout=int(spec["timeout_seconds"]))
    code = result.get("code")
    verdict = {
        "name": name,
        "kind": spec.get("kind"),
        "coverage": bool(spec.get("coverage")),
        # Where it actually ran, not where the catalog said. A verdict that
        # cannot name its own directory cannot be audited for the one failure
        # that looks exactly like success.
        "cwd": str(spec.get("cwd")),
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "timed_out": code == "timeout",
        "at": utcnow(),
        "compact": compact_cmd_signal(result),
    }
    if code == "missing_binary":
        # A command that never ran is the "skipped" case. It is not a pass and
        # it is not a normal failure either, so it says which it is.
        verdict["ok"] = False
        verdict["code"] = "missing_binary"
        verdict["compact"] = str(result.get("error") or "missing binary")
    elif code == "timeout":
        verdict["code"] = "timeout"
    return verdict


def build_checks_result(
    doc: dict[str, Any], phase: str, only: list[str] | None, stack: str | None, repos: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    """Run a phase to a verdict per required recipe and record it against HEAD.

    `only` narrows what is *executed*, never what is *required*. A correction
    turn that touched one file reruns that file's checks and inherits the rest
    from the verdicts already recorded at this same head — but if a recipe has
    no verdict at this head at all, the phase is not clean, whatever `only`
    said.
    """
    live, park_detail = build_live_change(doc, repos)
    if live is None:
        return build_park(doc, "worktree_mismatch", str(park_detail))
    head = live["head"]
    change_id = live["change_id"]

    worktree = Path(str(doc.get("worktree")))
    changed = build_changed_paths(worktree, doc.get("base_head"))
    required, why = build_required_recipes(doc, phase, changed)
    if not required:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "phase": phase,
            "error": f"the plan declared no {phase} checks and no changed path mandates one",
            "code": "no_recipes",
        }
    candidate: dict[str, str] | None = None
    runner_tree: str | None = None
    attempt_id: str | None = None
    if phase == "integration":
        stack_entry, runner_gap = build_integration_stack(doc, stack)
        if stack_entry is None:
            return 409, {
                "ok": False, "build_id": doc.get("build_id"), "phase": phase, "stack": stack,
                "error": runner_gap, "code": "not_a_clean_stack",
            }
        candidate, candidate_gap = build_materialize_candidate(worktree, head, change_id)
        if candidate is None:
            assert candidate_gap is not None
            return 409, {
                "ok": False, "build_id": doc.get("build_id"), "phase": phase, "stack": stack,
                **candidate_gap,
            }
        attempt_id = f"{doc.get('build_id')}-{uuid.uuid4().hex}"
        runner, provision_gap = build_provision_candidate_runner(
            worktree, Path(str(stack_entry["cwd"])), candidate, attempt_id
        )
        if runner is None:
            assert provision_gap is not None
            return 409, {
                "ok": False, "build_id": doc.get("build_id"), "phase": phase, "stack": stack,
                **provision_gap,
            }
        runner_label = f"candidate runner '{attempt_id}'"
        runner_tree = build_runner_tree(runner)
        attempt = {
            "attempt_id": attempt_id, "stack": stack, "runner": str(runner), **candidate,
            "runner_tree": runner_tree, "at": utcnow(),
        }
        doc["integration_attempts"] = [
            item for item in (doc.get("integration_attempts") or []) if isinstance(item, dict)
        ][-15:] + [attempt]
        write_err = write_build(doc, str(doc.get("build_id")))
        if write_err:
            build_cleanup_candidate_runner(worktree, runner)
            return int(write_err["status"]), write_err
        if runner_tree != candidate["candidate_tree"]:
            build_cleanup_candidate_runner(worktree, runner)
            return 409, {
                "ok": False, "build_id": doc.get("build_id"), "phase": phase, "stack": stack,
                "runner": str(runner), "candidate_tree": candidate["candidate_tree"],
                "runner_tree": runner_tree,
                "error": "clean runner did not check out the exact staged candidate tree",
                "code": "runner_tree_mismatch",
            }
    else:
        runner = worktree
        runner_label = "this build's worktree"

    # Integration recipe resolution happens only after the attempt runner has
    # checked out and verified the candidate; no baseline catalog is reused.
    catalog = build_recipe_catalog(repos, runner, runner_label)
    unknown = [n for n in required if n not in catalog]
    if unknown:
        if phase == "integration":
            build_cleanup_candidate_runner(worktree, runner)
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "phase": phase,
            "error": f"no such recipe: {', '.join(unknown)}",
            "code": "unknown_recipe",
            "unknown": unknown,
            "allowed": sorted(catalog),
        }
    stray = [n for n in required if catalog[n].get("unresolvable")]
    if stray:
        if phase == "integration":
            build_cleanup_candidate_runner(worktree, runner)
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "phase": phase,
            "error": "; ".join(str(catalog[n]["unresolvable"]) for n in stray[:4]),
            "code": "recipe_wrong_worktree",
            "unresolvable": stray,
        }
    if not any(catalog[n]["coverage"] for n in required):
        if phase == "integration":
            build_cleanup_candidate_runner(worktree, runner)
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "phase": phase,
            "error": (
                f"{phase} would run only hook recipes ({', '.join(required)}); a pre-commit "
                "hook is not unit, type, or integration coverage"
            ),
            "code": "hook_not_coverage",
        }
    if only:
        unrequired = [n for n in only if n not in required]
        if unrequired:
            if phase == "integration":
                build_cleanup_candidate_runner(worktree, runner)
            return 400, {
                "ok": False,
                "build_id": doc.get("build_id"),
                "phase": phase,
                "error": f"only names a recipe this phase does not require: {', '.join(unrequired)}",
                "code": "not_required",
                "required": required,
            }

    prior = build_checks_entry(doc, phase)
    verdicts: dict[str, Any] = {}
    # Inherited only from a run of this same change *in this same directory*.
    # An integration verdict recorded against another stack's checkout is not
    # a verdict about this one, and `only` would otherwise let it be carried
    # forward without ever re-running.
    if (
        isinstance(prior, dict)
        and prior.get("change_id") == change_id
        and prior.get("runner") == str(runner)
    ):
        verdicts = {
            name: value
            for name, value in (prior.get("verdicts") or {}).items()
            if isinstance(value, dict) and name in required
        }
    to_run = [n for n in required if not only or n in only or n not in verdicts]
    try:
        for name in to_run:
            verdicts[name] = build_run_recipe(name, catalog[name])
    finally:
        if phase == "integration":
            build_cleanup_candidate_runner(worktree, runner)

    missing = [n for n in required if not isinstance(verdicts.get(n), dict)]
    failed = [n for n in required if isinstance(verdicts.get(n), dict) and not verdicts[n].get("ok")]
    entry = {
        "phase": phase,
        "head": head,
        "change_id": change_id,
        "at": utcnow(),
        "stack": stack if phase == "integration" else None,
        # Where every recipe in this phase actually ran. build_checks_clean
        # compares it back to the clean stack's directory, so a record cannot
        # claim a stack it did not execute on.
        "runner": str(runner),
        "attempt_id": attempt_id,
        "candidate_tree": candidate.get("candidate_tree") if candidate else None,
        "runner_tree": runner_tree,
        "base_head": candidate.get("base_head") if candidate else None,
        "materialization": candidate.get("materialization") if candidate else None,
        "required": required,
        "required_because": why,
        "changed_paths": changed,
        "verdicts": verdicts,
        "ran": to_run,
        "missing": missing,
        "failed": failed,
        "ok": not missing and not failed,
    }
    doc.setdefault("checks", {})[phase] = entry
    # The gate transition rides on the same write as the verdict that earned
    # it. Recording the verdict and leaving the stage move to a later call is
    # what let a caller post `verify_passed` with no verdict at all; doing both
    # here means there is no window where one exists without the other.
    gate = None
    if phase == "fast" and entry["ok"]:
        pending, why = build_pending_gate(doc, change_id)
        if pending == "verify_passed":
            record = build_gate_transition(doc, pending, change_id, "buildChecks", why)
            gate = record["to"] if record else None
    write_err = write_build(doc, doc.get("build_id"))
    if write_err:
        return int(write_err["status"]), write_err
    out = {
        "ok": entry["ok"],
        "stage": doc.get("stage"),
        "gate": gate,
        "build_id": doc.get("build_id"),
        "phase": phase,
        "head": head,
        "change_id": change_id,
        "stack": entry["stack"],
        "runner": entry["runner"],
        "attempt_id": entry["attempt_id"],
        "candidate_tree": entry["candidate_tree"],
        "runner_tree": entry["runner_tree"],
        "base_head": entry["base_head"],
        "materialization": entry["materialization"],
        "required": required,
        "required_because": why,
        "ran": to_run,
        "ran_in": {n: str(catalog[n]["cwd"]) for n in to_run},
        "verdicts": [verdicts[n] for n in required if isinstance(verdicts.get(n), dict)],
        "missing": missing,
        "failed": failed,
        "compact": "\n".join(
            verdicts[n].get("compact") or "" for n in failed if isinstance(verdicts.get(n), dict)
        )[:COMPACT_MAX_CHARS],
        "budget_left": build_budget_left(doc),
    }
    if not entry["ok"]:
        out["error"] = (
            f"{phase} checks have no verdict for: {', '.join(missing)}"
            if missing
            else f"{phase} checks failed: {', '.join(failed)}"
        )
        out["code"] = "missing_verdict" if missing else "check_failed"
    return 200, out


def build_checks_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    with BUILD_LOCK:
        doc, open_err = build_open_checks(job["build_id"])
        if open_err:
            status, payload = open_err
        else:
            assert doc is not None
            status, payload = build_checks_result(
                doc, job["phase"], job.get("only"), job.get("stack"), load_repos()
            )
    receipt = {
        "status": "ok" if payload.get("ok") else "nack",
        "job_id": job_id,
        "build_id": job["build_id"],
        "phase": job["phase"],
        "http_status": status,
        **{
            k: payload.get(k)
            for k in (
                "ok", "head", "change_id", "required", "missing", "failed", "compact", "code", "error",
                "runner", "attempt_id", "candidate_tree", "runner_tree", "base_head", "materialization",
            )
        },
    }
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["receipt"] = receipt
    job["result"] = payload
    job["status"] = "completed"
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def build_checks(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """The gate. Synchronous by default, webhook-delivered when asked.

    The async form is about delivery, not about the verdict: the job still
    runs every recipe to completion and the answer arrives in the receipt. A
    202 from this op is a queue acknowledgement for the graph's wait node and
    is never itself a passing gate.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    phase = data.get("phase")
    if phase not in BUILD_CHECK_PHASES:
        return 400, {"error": "phase must be fast or integration", "code": "bad_phase",
                     "allowed": list(BUILD_CHECK_PHASES)}
    only = data.get("only")
    if only is not None:
        if isinstance(only, str):
            only = [only]
        if not isinstance(only, list) or not all(isinstance(n, str) and n.strip() for n in only):
            return 400, {"error": "only must be a list of recipe names", "code": "bad_only"}
        only = [n.strip()[:BUILD_NAME_MAX] for n in only]
    stack = data.get("stack")
    if stack is not None and (not isinstance(stack, str) or not stack.strip()):
        return 400, {"error": "stack must be a name", "code": "bad_stack"}
    stack = stack.strip()[:BUILD_NAME_MAX] if isinstance(stack, str) else None
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err

    if not webhook_url:
        with BUILD_LOCK:
            doc, open_err = build_open_checks(data.get("build_id"))
            if open_err:
                return open_err
            assert doc is not None
            return build_checks_result(doc, phase, only, stack, repos)

    doc, open_err = build_open_checks(data.get("build_id"))
    if open_err:
        return open_err
    assert doc is not None
    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "kind": "build_checks",
            "status": "queued",
            "build_id": doc.get("build_id"),
            "phase": phase,
            "only": only,
            "stack": stack,
            "cwd": str(doc.get("worktree")),
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
    herdr_follow_job(job)
    threading.Thread(
        target=build_checks_job, args=(job_id,), name=f"graphwing-checks-{job_id[:8]}", daemon=True
    ).start()
    return 202, {
        # False, on a call that succeeded. `ok` on this op means "the gate
        # passed", and the gate has not run yet. A graph node that branches on
        # `ok` reads a queue acknowledgement as green otherwise, which is the
        # whole failure this gate exists to prevent — so the acknowledgement
        # says no, and the verdict arrives in the receipt.
        "ok": False,
        "queued": True,
        "gate_passed": False,
        "code": "queued",
        "error": "queued: this is a receipt for accepting the work, not for passing the gate",
        "job_id": job_id,
        "kind": "build_checks",
        "status": "queued",
        "build_id": doc.get("build_id"),
        "phase": phase,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def build_writer_vendor(doc: dict[str, Any]) -> str | None:
    """Which vendor wrote this change, `unknown` if unrecognised, None if unstamped.

    Three answers, not two. None means the route never named a model, which is
    a planning gap a human fixes. `unknown` means it named one this service
    cannot place, and that fails closed: an unplaceable writer is one whose
    reviewer could be the same model under another name.
    """
    route = doc.get("route") or {}
    model = str(route.get("model") or "").strip().lower()
    if not model:
        return None
    for alias, vendor in BUILD_WRITER_VENDOR_ALIASES:
        if alias in model:
            return vendor
    return BUILD_VENDOR_UNKNOWN


def build_review_authority_gap(
    doc: dict[str, Any], reviewer: Any, role: str = BUILD_REVIEW_BEHAVIOR_ROLE
) -> str | None:
    """Why this reviewer cannot be the independent voice on this build, or None.

    Two layers. Every class needs a reviewer from a vendor that is not the
    writer's. A sensitive build additionally needs *this* reviewer in *this*
    seat, because the two lenses are not interchangeable: Terra passing the
    security seat would be one vendor holding both acknowledgements, and the
    tenant-isolation questions would never have been asked by anyone.
    """
    if role not in BUILD_REVIEW_ROLES:
        return f"'{role}' is not a review this service knows"
    if role not in build_review_roles(doc):
        return f"a {build_class(doc)} build has no {BUILD_REVIEW_ROLE_LABEL[role]} review"
    name = reviewer.strip() if isinstance(reviewer, str) else ""
    vendor = BUILD_REVIEWER_VENDOR.get(name)
    if vendor is None:
        return f"reviewer '{reviewer}' is not a reviewer this service knows"
    writer = build_writer_vendor(doc)
    if writer is None:
        return "route does not name the writer's model, so reviewer independence cannot be checked"
    if writer == BUILD_VENDOR_UNKNOWN:
        model = (doc.get("route") or {}).get("model")
        return f"writer model '{model}' belongs to no vendor this service knows"
    if writer == vendor:
        return f"reviewer '{name}' is the writer's own vendor ({vendor})"
    if build_is_sensitive(doc):
        gaps = build_sensitive_route_gaps(doc)
        if gaps:
            return gaps[0]
        expected = BUILD_SENSITIVE_REVIEWER[role]
        if name != expected:
            return (
                f"the {BUILD_REVIEW_ROLE_LABEL[role]} review on a sensitive build is "
                f"'{expected}'; '{name}' cannot hold that seat"
            )
    return None


def build_parse_review_verdict(text: str) -> tuple[str | None, str]:
    """The reviewer's verdict, or None when it never gave one.

    After trailing blank lines, the final line must be exactly `VERDICT: PASS`
    or `VERDICT: NACK`. The loose parser this replaced took a substring match
    anywhere in the transcript and, failing that, fell back to any
    receipt-shaped JSON with `status: ok` -- so a writer receipt echoed into
    the reviewer's output was read as a passing review of the writer's own
    change. Prose after a verdict likewise makes the output incomplete rather
    than letting an earlier token ship the change.

    Both verdicts present fails closed to NACK. A reviewer that said pass and
    then nack has not passed anything, and picking either one is a coin toss
    over whether unreviewed code ships.
    """
    lines = (text or "").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[-1] not in ("VERDICT: PASS", "VERDICT: NACK"):
        return None, "no VERDICT line"
    found = [line.removeprefix("VERDICT: ") for line in lines
             if line in ("VERDICT: PASS", "VERDICT: NACK")]
    if len(set(found)) > 1:
        return "NACK", "conflicting verdicts: " + ", ".join(found)
    return found[-1], f"VERDICT: {found[-1]}"


def build_review_round_result(
    reviewer: str, prompt: str, worktree: Path,
    requested_provider: str | None = None, requested_model: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Run one review and read its verdict off the transcript, strictly.

    Deliberately not `review_result`'s own verdict field: that one is shared
    with /v1/review/run and parses leniently on purpose. The pre-PR gate is the
    caller that cannot afford lenient, so it re-reads the raw text here.
    """
    raw = review_result(reviewer, prompt, worktree, requested_provider, requested_model, attempt_id)
    if raw.get("no_verdict") or raw.get("code") in ("timeout", "not_implemented"):
        # The provider never finished. Not an opinion, and never a finding.
        return {**raw, "verdict": None, "no_verdict": True}
    verdict, why = build_parse_review_verdict(str(raw.get("text") or ""))
    if verdict is None:
        return {**raw, "verdict": None, "no_verdict": True, "code": "no_verdict", "summary": why}
    return {
        **raw,
        "verdict": verdict,
        "no_verdict": False,
        "ok": verdict == "PASS",
        "summary": str(raw.get("summary") or why),
    }


def build_behavior_review_prompt(doc: dict[str, Any]) -> list[str]:
    """The behavior-and-test lenses this review has to answer, by name.

    Named individually because a reviewer asked to "review the change" reads
    the diff and grades the code. Every one of these is a way a diff can be
    clean code and still be the wrong change, or the right change with nothing
    holding it in place.
    """
    verification = doc.get("verification") or {}
    integration = [n for n in (verification.get("integration") or []) if isinstance(n, str)]
    lines = [
        "Behavior-and-test micro-review. You did not write this change and you are not",
        "reviewing its style. Answer each lens, then give one verdict.",
        "",
        "1. SCOPE: does the diff do the approved ticket and nothing else? Name anything",
        "   it changes that the ticket did not ask for.",
        "2. PROMISED BEHAVIOR: does the change actually produce the behavior the ticket states,",
        "   or only look like it does? Name the case where it would not.",
    ]
    if verification.get("requires_red"):
        lines.append(
            "3. RED EVIDENCE: this ticket requires a test that failed before the change. "
            "If there is no record of it failing first, that is a finding."
        )
    else:
        lines.append(
            "3. RED EVIDENCE: not required for this ticket. Do not raise its absence as a finding."
        )
    lines.extend(
        [
            "4. PRODUCTION PATH: do the tests exercise the code that actually runs, or a copy,",
            "   a mock, or a helper the production path never calls? Does anything the change",
            "   promises to keep survive a restart, or does it only live in memory?",
            "5. INTEGRATION SEAM: the plan declares this seam as "
            + (", ".join(integration) if integration else "none"),
            "   Is the seam covered by something that runs it for real?",
            "6. HONEST FAILURE: when a step fails, times out, or returns nothing, does the",
            "   change say so, or does it report success anyway? Name any path where a",
            "   failure reads as a pass.",
        ]
    )
    return lines


def build_security_review_prompt(doc: dict[str, Any]) -> list[str]:
    """The security-and-tenant lenses, by name, scoped to this ticket.

    Deliberately not "look for vulnerabilities". A reviewer given that
    instruction audits the whole file and reports the oldest thing in it; these
    six ask what this diff did to the boundaries the ticket touches, which is
    the question a correction turn can actually answer.
    """
    return [
        "Security-and-tenant micro-review. You did not write this change. Judge only what",
        "this diff does to the boundaries the ticket touches. Answer each lens, then give",
        "one verdict.",
        "",
        "1. TENANT SCOPE: can this change read, write, or reveal another tenant's data?",
        "   Name the query, path, or identifier that is not scoped.",
        "2. PERMISSIONS: does every new or changed entry point check what the caller is",
        "   allowed to do, or does it rely on the caller having asked nicely?",
        "3. FORGED INPUT: what happens when a caller sends an id, path, or state it does",
        "   not own? Name any field the change trusts because it arrived in the request.",
        "4. LEAKAGE: can a credential, token, capability URL, or screenshot reach a log,",
        "   a receipt, a state file, or an error message?",
        "5. IDEMPOTENCY: can a retry, a replay, or two concurrent callers make this",
        "   happen twice? Name the side effect that would repeat.",
        "6. DATA LOSS: can this change destroy or overwrite something it cannot restore?",
        "   Name the write that has no recovery.",
    ]


def build_review_prompt(doc: dict[str, Any], role: str = BUILD_REVIEW_BEHAVIOR_ROLE) -> str:
    """One reviewer's whole world: its own lenses and the approved ticket.

    Composed from the ticket and the declared verification only. No recorded
    round, summary, or compact transcript is interpolated, so no reviewer ever
    reads what the other one was thinking — two acknowledgements that agreed
    because the second was shown the first are one acknowledgement.
    """
    if role == BUILD_REVIEW_SECURITY_ROLE:
        lines = build_security_review_prompt(doc)
    else:
        lines = build_behavior_review_prompt(doc)
    return "\n".join(lines + ["", "Ticket:", str(doc.get("ticket") or "(none)")])


def build_review_attempts(doc: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = doc.get("review_attempts")
    if not isinstance(attempts, list):
        attempts = []
        doc["review_attempts"] = attempts
    return attempts


def build_open_review_attempt(
    doc: dict[str, Any], *, attempt_id: str, transition_id: str, role: str,
    reviewer: str, requested_provider: str, requested_model: str,
    head: str, change_id: str, evidence_id: str, source_failure_id: str | None,
) -> dict[str, Any]:
    """Persist the durable dispatch authority before calling a provider."""
    attempt = {
        "attempt_id": attempt_id, "transition_id": transition_id, "status": "open",
        "opened_at": utcnow(),
        "deadline": (datetime.now(timezone.utc) + timedelta(seconds=200)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "role": role, "reviewer": reviewer,
        "requested_provider": requested_provider, "requested_model": requested_model,
        "head": head, "change_id": change_id, "evidence_id": evidence_id,
        "source_failure_id": source_failure_id,
    }
    build_review_attempts(doc).append(attempt)
    return attempt


def build_terminal_review_attempt(
    attempt: dict[str, Any], result: dict[str, Any]
) -> str:
    """Terminalize one open attempt exactly once from launcher evidence."""
    if attempt.get("status") != "open":
        return str(attempt.get("failure_class") or attempt.get("verdict") or "terminal")
    executed_provider = result.get("executed_provider")
    executed_model = result.get("executed_model")
    attempt["executed_provider"] = executed_provider
    attempt["executed_model"] = executed_model
    attempt["fallback_disabled"] = result.get("fallback_disabled") is True
    attempt["transport_provenance"] = result.get("transport_provenance")
    if result.get("code") == "timeout":
        failure = "timeout"
    elif result.get("code") in ("crash", "missing_terminal_receipt"):
        failure = str(result.get("code"))
    elif result.get("fallback_disabled") is not True:
        failure = "missing_transport_provenance"
    elif executed_provider not in (None, attempt.get("requested_provider")) or executed_model not in (None, attempt.get("requested_model")):
        failure = "unexpected_execution"
    elif result.get("code") in ("not_implemented", "unavailable_provider"):
        failure = "unavailable_provider"
    elif result.get("no_verdict"):
        failure = "no_verdict"
    elif result.get("verdict") in {"PASS", "NACK"}:
        failure = None
    else:
        failure = "explicit_failure"
    attempt.update({
        "status": "terminal", "terminal_at": utcnow(), "verdict": result.get("verdict"),
        "failure_class": failure, "summary": str(result.get("summary") or result.get("error") or "")[:BUILD_REDACT_MAX_STRING],
    })
    return failure or "success"


def build_find_review_attempt(doc: dict[str, Any], attempt_id: str) -> dict[str, Any] | None:
    return next((a for a in build_review_attempts(doc) if a.get("attempt_id") == attempt_id), None)


def build_review_result(
    doc: dict[str, Any],
    reviewer: str,
    repos: dict[str, str],
    role: str = BUILD_REVIEW_BEHAVIOR_ROLE,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    transition_id: str | None = None,
    source_failure_id: str | None = None,
    attempt_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run the micro-review, retrying silence, and record the round.

    Silence and disagreement are the two outcomes that must never be confused.
    A reviewer that ran out of turns or lost its connection has not found
    anything; treating that as a finding resumes the writer to fix a problem
    nobody reported, spends a round of budget, and does it again next time.

    The verdict is read strictly, off a trailing VERDICT line and nothing else,
    and a PASS this op recorded is also what moves the build to `ready`: no
    caller ever gets to assert that step.
    """
    gap = build_review_authority_gap(doc, reviewer, role)
    if gap:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "role": role,
            "error": gap,
            "code": "reviewer_is_writer" if "own vendor" in gap else "no_review_authority",
        }
    live, park_detail = build_live_change(doc, repos)
    if live is None:
        return build_park(doc, "worktree_mismatch", str(park_detail))
    head = live["head"]
    change_id = live["change_id"]
    due_role, due_reason = build_next_review_role(doc, change_id)
    if due_role != role:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "role": role,
            "error": due_reason or f"the {BUILD_REVIEW_ROLE_LABEL[role]} review is not due",
            "code": "review_not_due",
        }
    worktree = Path(str(doc.get("worktree")))
    prompt = build_review_prompt(doc, role)
    evidence_id = build_evidence_identity(doc, change_id)
    requested_provider = requested_provider or BUILD_REVIEWER_PROVIDER.get(reviewer)
    requested_model = requested_model or BUILD_REVIEWER_MODEL.get(reviewer)
    if not isinstance(requested_provider, str) or not requested_provider or not isinstance(requested_model, str) or not requested_model:
        return 409, {"ok": False, "build_id": doc.get("build_id"), "code": "unavailable_provider",
                     "error": "the requested provider/model is not available on the authorized reviewer transport"}
    if source_failure_id:
        source = build_find_review_attempt(doc, source_failure_id)
        if not isinstance(source, dict) or source.get("status") != "terminal" or not source.get("failure_class"):
            return 409, {"ok": False, "build_id": doc.get("build_id"), "code": "invalid_source_failure",
                         "error": "source failure does not exist as a terminal failed attempt in this build"}
        if source.get("consumed_by"):
            return 409, {"ok": False, "build_id": doc.get("build_id"), "code": "source_failure_consumed",
                         "error": "source failure was already consumed by a failover dispatch"}
        if any(source.get(key) != value for key, value in {
            "role": role, "head": head, "change_id": change_id, "evidence_id": evidence_id,
        }.items()):
            return 409, {"ok": False, "build_id": doc.get("build_id"), "code": "failover_identity_mismatch",
                         "error": "failover role/head/change/evidence must equal its source failure"}
        if (source.get("requested_provider"), source.get("requested_model")) == (requested_provider, requested_model):
            return 409, {"ok": False, "build_id": doc.get("build_id"), "code": "failover_not_distinct",
                         "error": "workflow failover must name a different configured provider/model"}

    park = build_charge(doc, 1)
    if park:
        return park

    attempt_id = attempt_id or uuid.uuid4().hex
    transition_id = transition_id or attempt_id
    attempt = build_open_review_attempt(
        doc, attempt_id=attempt_id, transition_id=transition_id, role=role, reviewer=reviewer,
        requested_provider=requested_provider, requested_model=requested_model, head=head,
        change_id=change_id, evidence_id=evidence_id, source_failure_id=source_failure_id,
    )
    # This write is deliberately before launch.  A hard death after it leaves
    # an auditable open attempt that buildReviewReconcile must terminalize.
    if source_failure_id:
        source = build_find_review_attempt(doc, source_failure_id)
        assert source is not None
        source["consumed_by"] = attempt_id
        source["consumed_at"] = utcnow()
    write_err = write_build(doc, doc.get("build_id"))
    if write_err:
        return int(write_err["status"]), write_err
    result = build_review_round_result(reviewer, prompt, worktree, requested_provider, requested_model, attempt_id)
    terminal = build_terminal_review_attempt(attempt, result)
    write_err = write_build(doc, doc.get("build_id"))
    if write_err:
        return int(write_err["status"]), write_err

    if terminal != "success":
        return 200, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "role": role,
            "head": head,
            "change_id": change_id,
            "evidence_id": evidence_id,
            "verdict": None,
            "no_verdict": True,
            "finding": False,
            "attempt_id": attempt_id,
            "transition_id": transition_id,
            "requested_provider": requested_provider, "requested_model": requested_model,
            "executed_provider": attempt.get("executed_provider"), "executed_model": attempt.get("executed_model"),
            "attempts": 1,
            "error": str(attempt.get("summary") or terminal),
            "code": terminal,
            "budget_left": build_budget_left(doc),
        }

    rounds = [r for r in (doc.get("reviews") or []) if isinstance(r, dict)]
    entry = {
        "round": len(rounds) + 1,
        "head": head,
        "change_id": change_id,
        "at": utcnow(),
        "reviewer": reviewer,
        "role": role,
        # This field is authored only after Graphwing selected the seat and
        # parsed its completed verdict.  It is deliberately not copied from
        # model output or a Rewst request body.
        "authority": {
            "author": "graphwing",
            "provider": attempt.get("executed_provider"),
            "model": attempt.get("executed_model"),
            "role": role,
        },
        "evidence_id": evidence_id,
        "verdict": result.get("verdict"),
        "no_verdict": False,
        "summary": str(result.get("summary") or "")[:BUILD_REDACT_MAX_STRING],
        "compact": str(result.get("compact") or "")[:COMPACT_MAX_CHARS],
        "attempt_id": attempt_id,
        "transition_id": transition_id,
        "requested_provider": requested_provider,
        "requested_model": requested_model,
        "executed_provider": attempt.get("executed_provider"),
        "executed_model": attempt.get("executed_model"),
        "attempts": 1,
    }
    doc["reviews"] = rounds[-(BUILD_REVIEW_ROUNDS_KEPT - 1):] + [entry]
    gate = None
    if entry["verdict"] == "PASS":
        pending, why = build_pending_gate(doc, change_id)
        if pending == "review_passed":
            record = build_gate_transition(doc, pending, change_id, "buildReview", why)
            gate = record["to"] if record else None
    write_err = write_build(doc, doc.get("build_id"))
    if write_err:
        return int(write_err["status"]), write_err
    return 200, {
        "ok": entry["verdict"] == "PASS",
        "build_id": doc.get("build_id"),
        "stage": doc.get("stage"),
        "gate": gate,
        "reviewer": reviewer,
        "role": role,
        "authority": entry["authority"],
        "head": head,
        "change_id": change_id,
        "evidence_id": evidence_id,
        "round": entry["round"],
        "verdict": entry["verdict"],
        "no_verdict": False,
        "finding": entry["verdict"] != "PASS",
        "summary": entry["summary"],
        "compact": entry["compact"],
        "attempt_id": attempt_id,
        "transition_id": transition_id,
        "requested_provider": requested_provider,
        "requested_model": requested_model,
        "executed_provider": attempt.get("executed_provider"),
        "executed_model": attempt.get("executed_model"),
        "attempts": 1,
        "budget_left": build_budget_left(doc),
    }


def build_review_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    with BUILD_LOCK:
        doc, open_err = build_open_mechanical(job["build_id"])
        if open_err:
            status, payload = open_err
        else:
            assert doc is not None
            status, payload = build_review_result(
                doc, job["reviewer"], load_repos(), job["role"],
                job.get("requested_provider"), job.get("requested_model"),
                job.get("transition_id"), job.get("source_failure_id"), job_id,
            )
    receipt = {
        "status": "ok" if payload.get("ok") else "nack",
        "job_id": job_id,
        "build_id": job["build_id"],
        "http_status": status,
        **{
            k: payload.get(k)
            for k in (
                "ok", "role", "reviewer", "evidence_id", "verdict", "no_verdict",
                "finding", "round", "summary", "compact", "code", "error", "attempt_id",
                "transition_id", "requested_provider", "requested_model", "executed_provider", "executed_model",
            )
        },
    }
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["receipt"] = receipt
    job["result"] = payload
    job["status"] = "completed"
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def build_review(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    doc, open_err = build_open_mechanical(data.get("build_id"))
    if open_err:
        return open_err
    assert doc is not None
    stage = doc.get("stage")
    if stage != "review":
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "stage": stage,
            "error": f"the behavior-and-test review runs at stage 'review'; build is at '{stage}'",
            "code": "invalid_transition",
        }
    live, park_detail = build_live_change(doc, repos)
    if live is None:
        return build_park(doc, "worktree_mismatch", str(park_detail))
    role, due_reason = build_next_review_role(doc, live["change_id"])
    if role is None:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "stage": stage,
            "error": due_reason or "no review is due for this change",
            "code": "review_not_due",
        }
    routed_reviewer = build_route_reviewer(doc, role)
    requested_reviewer = data.get("reviewer")
    if build_is_sensitive(doc) and requested_reviewer not in (None, "", routed_reviewer):
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "role": role,
            "reviewer": requested_reviewer,
            "error": (
                f"the {BUILD_REVIEW_ROLE_LABEL[role]} review is server-routed "
                f"to '{routed_reviewer}'"
            ),
            "code": "no_review_authority",
        }
    reviewer = routed_reviewer if build_is_sensitive(doc) else requested_reviewer or routed_reviewer
    if not isinstance(reviewer, str) or reviewer.strip() not in BUILD_REVIEWER_VENDOR:
        return 400, {
            "ok": False,
            "error": "reviewer must be one of " + ", ".join(sorted(BUILD_REVIEWER_VENDOR)),
            "code": "bad_reviewer",
        }
    reviewer = reviewer.strip()
    gap = build_review_authority_gap(doc, reviewer, role)
    if gap:
        # The review is independent or it is the writer grading its own
        # homework in a second window. There is no third option, and a writer
        # model this service cannot place is the second one until proven
        # otherwise: "sonnet" reviewed by "opus" used to read as independent
        # because neither name matched the full-model-id prefix table.
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "error": gap,
            "code": "reviewer_is_writer" if "own vendor" in gap else "no_review_authority",
        }
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err
    source_failure_id = data.get("source_failure_id")
    if source_failure_id is not None and (not isinstance(source_failure_id, str) or not BUILD_ROLLOUT_ID_RE.fullmatch(source_failure_id)):
        return 400, {"ok": False, "code": "bad_source_failure_id", "error": "source_failure_id must name a server attempt"}
    transition_id = data.get("transition_id")
    if transition_id is not None and (not isinstance(transition_id, str) or not BUILD_ROLLOUT_ID_RE.fullmatch(transition_id)):
        return 400, {"ok": False, "code": "bad_transition_id", "error": "transition_id is invalid"}
    requested_provider = data.get("provider") or BUILD_REVIEWER_PROVIDER.get(reviewer)
    requested_model = data.get("model") or BUILD_REVIEWER_MODEL.get(reviewer)
    if source_failure_id and (not isinstance(data.get("provider"), str) or not data.get("provider").strip()
                              or not isinstance(data.get("model"), str) or not data.get("model").strip()):
        return 400, {"ok": False, "code": "missing_failover_route",
                     "error": "an explicit failover requires configured provider and model"}
    if not isinstance(requested_provider, str) or not requested_provider or not isinstance(requested_model, str) or not requested_model:
        return 400, {"ok": False, "code": "bad_provider_model", "error": "provider and model must be non-empty strings"}

    if not webhook_url:
        with BUILD_LOCK:
            doc, open_err = build_open_mechanical(data.get("build_id"))
            if open_err:
                return open_err
            assert doc is not None
            return build_review_result(doc, reviewer, repos, role, requested_provider, requested_model,
                                       transition_id, source_failure_id)

    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "kind": "build_review",
            "status": "queued",
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "role": role,
            "requested_provider": requested_provider,
            "requested_model": requested_model,
            "transition_id": transition_id or job_id,
            "source_failure_id": source_failure_id,
            "cwd": str(doc.get("worktree")),
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
    herdr_follow_job(job)
    threading.Thread(
        target=build_review_job, args=(job_id,), name=f"graphwing-breview-{job_id[:8]}", daemon=True
    ).start()
    return 202, {
        # Same reason as build_checks: an acknowledgement is not a verdict.
        "ok": False,
        "queued": True,
        "gate_passed": False,
        "code": "queued",
        "error": "queued: this is a receipt for accepting the work, not a review verdict",
        "job_id": job_id,
        "kind": "build_review",
        "status": "queued",
        "build_id": doc.get("build_id"),
        "reviewer": reviewer,
        "role": role,
        "transition_id": job["transition_id"],
        "requested_provider": requested_provider,
        "requested_model": requested_model,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def build_rollout(doc: dict[str, Any]) -> dict[str, Any] | None:
    rollout = doc.get("rollout")
    if not isinstance(rollout, dict) or rollout.get("ticket") != BUILD_ROLLOUT_TICKET:
        return None
    if not isinstance(rollout.get("receipts"), list):
        rollout["receipts"] = []
    return rollout


def build_review_reconcile(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Convert abandoned existing review jobs/open records into terminal receipts.

    This is reconciliation over the existing build and job ledgers; it neither
    launches a model nor routes a retry.  Rewst may call it after a wait
    timeout, and the completion supervisor can call the same deterministic op.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    build_id = data.get("build_id")
    if not valid_build_id(build_id):
        return 400, {"ok": False, "code": "bad_build_id", "error": "invalid build_id"}
    with BUILD_LOCK:
        doc, open_err = load_build(str(build_id))
        if open_err:
            return int(open_err["status"]), open_err
        if doc is None:
            return 404, {"ok": False, "code": "unknown_build", "error": "unknown build"}
        if doc.get("version") != BUILD_STATE_VERSION:
            return 409, build_version_park(doc)
        now = datetime.now(timezone.utc)
        terminalized: list[str] = []
        for attempt in build_review_attempts(doc):
            if attempt.get("status") != "open":
                continue
            job = read_job(str(attempt.get("attempt_id") or ""))
            deadline_raw = str(attempt.get("deadline") or "")
            try:
                deadline = datetime.strptime(deadline_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                deadline = now
            if isinstance(job, dict) and job.get("status") == "completed" and not isinstance(job.get("receipt"), dict):
                result = {"code": "missing_terminal_receipt", "no_verdict": True,
                          "error": "completed job has no terminal receipt"}
            elif now >= deadline:
                result = {"code": "timeout", "no_verdict": True,
                          "error": "attempt deadline elapsed without terminal receipt"}
            else:
                continue
            build_terminal_review_attempt(attempt, result)
            terminalized.append(str(attempt.get("attempt_id")))
        write_err = write_build(doc, str(build_id))
        if write_err:
            return int(write_err["status"]), write_err
        return 200, {**build_public(doc), "ok": True, "terminalized_attempt_ids": terminalized}


def build_rollout_receipts(doc: dict[str, Any]) -> list[dict[str, Any]]:
    rollout = build_rollout(doc)
    if rollout is None:
        return []
    return [entry for entry in rollout["receipts"] if isinstance(entry, dict)]


def build_rollout_public(doc: dict[str, Any]) -> dict[str, Any] | None:
    rollout = build_rollout(doc)
    if rollout is None:
        return None
    receipts = build_rollout_receipts(doc)
    return {
        "ticket": BUILD_ROLLOUT_TICKET,
        "status": rollout.get("status") or "open",
        "identity": rollout.get("identity"),
        "receipt_kinds": [entry.get("kind") for entry in receipts],
        "gaps": build_rollout_gaps(doc),
    }


def rollout_proof_gap(kind: str, proof: dict[str, Any], served_commit_sha: str | None = None) -> str | None:
    """Validate the non-forgeable shape required for one receipt class.

    Values originate with the controller's external readbacks, while this
    server binds their identity and census.  There is intentionally no local
    snapshot/cache of a live integration or workflow.
    """
    if kind == "catalog_parity":
        if proof.get("live_source") != "fresh_post_publish_api_read":
            return "catalog parity did not come from a fresh post-publish API read"
        if proof.get("normalized_catalog_sha") != proof.get("normalized_live_sha"):
            return "normalized live catalog parity mismatched"
    elif kind == "mechanical":
        required = {
            "named_disposable_branch", "fresh_clone", "empty_build_state",
            "correction", "acknowledgement", "merge_base", "remote_ref",
        }
        if any(proof.get(name) in (None, "", False) for name in required):
            return "mechanical proof is missing branch/fresh-state/correction/readback evidence"
        if proof.get("pushed_commit_count") != 1 or proof.get("force_push") is not False:
            return "mechanical proof must show exactly one remote-readback commit and no force-push"
    elif kind == "sensitive":
        if proof.get("final_diff_sha") in (None, "") or proof.get("evidence_id") in (None, ""):
            return "sensitive proof lacks final diff or evidence identity"
        reviews = proof.get("reviews")
        if not isinstance(reviews, list) or len(reviews) != 2:
            return "sensitive proof requires Terra and Grok server-authored receipts"
        expected = {"behavior": "terra", "security": "grok"}
        seen: set[str] = set()
        for review in reviews:
            if not isinstance(review, dict):
                return "sensitive proof receipt is malformed"
            role, reviewer = review.get("role"), review.get("reviewer")
            if expected.get(role) != reviewer or review.get("author") != "graphwing":
                return "sensitive review identity is not server-authored Terra/Grok authority"
            if review.get("no_verdict") or review.get("verdict") != "PASS":
                return "sensitive review is missing, no_verdict, or not passing"
            if review.get("final_diff_sha") != proof.get("final_diff_sha") or review.get("evidence_id") != proof.get("evidence_id"):
                return "sensitive reviewers did not bind the same final diff and evidence"
            seen.add(str(role))
        if seen != set(expected):
            return "sensitive proof does not contain both required review roles"
    elif kind == "visual":
        artifacts = proof.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return "visual proof has no screenshot/a11y artifacts"
        required = {"served_commit_sha", "preview_url", "preview_port", "writer_session", "round", "evidence_id"}
        if any(not isinstance(a, dict) or any(a.get(k) in (None, "") for k in required) for a in artifacts):
            return "visual artifact lacks served SHA, preview, writer session, round, or evidence identity"
        rounds = proof.get("human_rounds")
        if not isinstance(rounds, list) or len(rounds) < 2:
            return "visual proof requires two human feedback rounds"
        first = rounds[0] if isinstance(rounds[0], dict) else {}
        if not first.get("writer_session") or not first.get("preview_url"):
            return "visual human-round identity is incomplete"
        if any(not isinstance(r, dict) or r.get("writer_session") != first.get("writer_session") or r.get("preview_url") != first.get("preview_url") for r in rounds[:2]):
            return "visual human rounds must share writer session and preview"
        if proof.get("final_gate_return") is not True or proof.get("post_change_artifacts") is not True:
            return "visual final-gate return lacks fresh post-change artifacts"
        if served_commit_sha is not None:
            if any(a.get("served_commit_sha") != served_commit_sha for a in artifacts):
                return "visual artifact served SHA is stale for this canary receipt"
            if proof.get("post_change_served_commit_sha") != served_commit_sha:
                return "visual final-gate return lacks fresh artifacts at the current served SHA"
    elif kind == "checkpoint":
        if proof.get("decision_path") != "build_iteration_checkpoint_due":
            return "checkpoint proof did not invoke the live decision path"
        if proof.get("choices") != ["continue", "reframe", "split", "park"]:
            return "checkpoint proof does not cover the four deterministic choices"
        if proof.get("live_advisory") is not True or proof.get("no_later_ceiling") is not True:
            return "checkpoint live canary did not prove advisory continuation without a later ceiling"
    elif kind == "lifecycle":
        if proof.get("pane_exact") is not True or proof.get("no_answer_preserves_preview") is not True:
            return "lifecycle close proof lacks exact-pane or no-answer preservation"
        if proof.get("cleanup_readback") is not True:
            return "lifecycle proof lacks independent cleanup readback"
    return None


def build_rollout_gaps(doc: dict[str, Any], change_id: str | None = None) -> list[str]:
    rollout = build_rollout(doc)
    if rollout is None:
        return []
    receipts = build_rollout_receipts(doc)
    by_kind = {str(entry.get("kind")): entry for entry in receipts}
    gaps: list[str] = []
    if len(by_kind) != len(receipts):
        gaps.append("rollout receipt census contains duplicate kinds")
    source_builds = {entry.get("source_build_id") for entry in receipts if entry.get("source_build_id")}
    if receipts and len(source_builds) < 2:
        gaps.append("rollout census requires distinct source canary builds")
    if any(not isinstance(entry.get("source_build_id"), str) or not entry.get("source_build_id") for entry in receipts):
        gaps.append("rollout receipt lacks its source build identity")
    if any(not isinstance(entry.get("canary_run_id"), str) or not entry.get("canary_run_id") for entry in receipts):
        gaps.append("rollout receipt lacks its current canary run identity")
    transition_ids = [entry.get("transition_id") for entry in receipts]
    if len(transition_ids) != len(set(transition_ids)):
        gaps.append("rollout receipt census reuses a transition identity")
    for kind in BUILD_ROLLOUT_RECEIPT_KINDS:
        receipt = by_kind.get(kind)
        if receipt is None:
            gaps.append(f"rollout receipt census lacks {kind}")
            continue
        if receipt.get("readback_id") in (None, "") or receipt.get("readback_id") == receipt.get("transition_id"):
            gaps.append(f"{kind} receipt lacks a separate post-transition readback")
            continue
        proof = receipt.get("proof")
        if not isinstance(proof, dict):
            gaps.append(f"{kind} receipt has no proof")
            continue
        proof_gap = rollout_proof_gap(kind, proof, str(receipt.get("commit_sha") or ""))
        if proof_gap:
            gaps.append(proof_gap)
    # The holder is an issue-level census, not a source canary.  Its own HEAD
    # must never invalidate a completed source receipt or make every source
    # finish a finalization before it can be counted.
    return gaps


def build_finalize_gaps(doc: dict[str, Any], change_id: str) -> list[str]:
    """Everything still standing between this build and a commit."""
    gaps: list[str] = []
    branch = doc.get("branch")
    if isinstance(branch, str) and branch.split("/")[-1] in BUILD_TRUNK_BRANCHES:
        # Checked again here, not only in preflight. This is the line where the
        # push happens, and a branch that became trunk after preflight passed
        # would otherwise land straight on it.
        gaps.append(f"'{branch}' is a trunk branch; a pre-PR build never commits to trunk")
    gaps.extend(f"route is missing {name}" for name in build_route_stamp_gaps(doc))
    for phase in BUILD_CHECK_PHASES:
        clean, why = build_checks_clean(doc, phase, change_id)
        if not clean:
            gaps.append(why)
    gaps.extend(build_review_gaps(doc, change_id))
    gaps.extend(build_visual_gaps(doc, change_id))
    if build_needs_visual(doc):
        # In addition to the normal evidence contract, a design candidate (and
        # especially a final-gate return) must complete its own human path.
        gaps.extend(build_visual_human_path_gaps(doc, change_id))
    gaps.extend(build_rollout_gaps(doc, change_id))

    return gaps


def build_commit_dirt(entries: list[str]) -> list[str]:
    """Porcelain lines for anything on disk that the commit would leave behind.

    The reviewed change is the staged one. An unstaged tracked edit or an
    untracked file is content that went into the change id every gate was
    recorded against and would *not* go into the commit, so what ships is not
    what passed. Reported rather than staged: `git add` here would sweep in
    whatever else is lying around the worktree, which is the same divergence
    pointing the other way.
    """
    dirt: list[str] = []
    for line in entries:
        if line.startswith("??") or (len(line) > 1 and line[1] not in (" ", "")):
            dirt.append(line[:BUILD_NAME_MAX])
    return dirt


def build_git_authoring_boundary(worktree: Path) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Freeze the identities `git commit` will use, without freezing its clock."""
    boundary: dict[str, str] = {}
    for field, variable in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        result = run_git(worktree, ["var", variable])
        parts = str(result.get("stdout") or "").strip().rsplit(" ", 2)
        if not result.get("ok") or len(parts) != 3 or "<" not in parts[0] or not parts[0].endswith(">"):
            return None, {
                "ok": False,
                "error": f"could not resolve git {field} identity",
                "code": "authoring_boundary_unreadable",
            }
        boundary[field] = parts[0]
    return boundary, None


def build_commit_identity(worktree: Path, rev: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read every immutable field used to adopt a commit after a crash."""
    result = run_git(
        worktree,
        ["show", "-s", "--format=%H%x00%T%x00%P%x00%an <%ae>%x00%cn <%ce>%x00%B", rev],
    )
    if not result.get("ok"):
        return None, {
            "ok": False,
            "error": str(result.get("error") or "could not inspect commit")[:BUILD_REDACT_MAX_STRING],
            "code": "commit_unreadable",
        }
    parts = str(result.get("stdout") or "").split("\x00", 5)
    if len(parts) != 6:
        return None, {"ok": False, "error": "commit metadata was malformed", "code": "commit_unreadable"}
    return {
        "sha": parts[0].strip(),
        "tree": parts[1].strip(),
        "parent": parts[2].strip(),
        "author": parts[3].strip(),
        "committer": parts[4].strip(),
        "message": parts[5].rstrip("\n"),
    }, None


def build_finalize_adoption_gaps(intent: dict[str, Any], commit: dict[str, Any], branch: str) -> list[str]:
    gaps: list[str] = []
    for field, expected in (
        ("parent", intent.get("expected_parent")),
        ("tree", intent.get("candidate_tree")),
        ("message", intent.get("message")),
    ):
        if commit.get(field) != expected:
            gaps.append(f"{field} does not match prepared intent")
    if branch != intent.get("branch"):
        gaps.append("branch does not match prepared intent")
    boundary = intent.get("authoring_boundary")
    boundary = boundary if isinstance(boundary, dict) else {}
    for field in ("author", "committer"):
        if commit.get(field) != boundary.get(field):
            gaps.append(f"{field} does not match prepared intent")
    return gaps


def build_remote_branch_head(worktree: Path, remote: str, branch: str) -> tuple[str | None, dict[str, Any] | None]:
    ref = f"refs/heads/{branch}"
    result = run_git(worktree, ["ls-remote", "--heads", remote, ref])
    if not result.get("ok"):
        return None, {
            "ok": False,
            "error": str(result.get("error") or "could not inspect remote branch")[:BUILD_REDACT_MAX_STRING],
            "code": "remote_unreadable",
            "retryable": True,
            "compact": compact_cmd_signal(result),
        }
    for line in str(result.get("stdout") or "").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref and BUILD_HEAD_RE.fullmatch(fields[0]):
            return fields[0], None
    return None, None


def build_finalize(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Durably prepare, commit, recover, and push one reviewed candidate."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return 400, {"error": "event_id is required", "code": "bad_event_id"}
    holder = data.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        return 400, {"error": "holder is required", "code": "missing_holder"}
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return 400, holder_err
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return 400, {"error": "message is required", "code": "empty_message"}
    message = message.strip()
    if len(message) > BUILD_NAME_MAX:
        return 400, {"error": "message is too long", "code": "message_too_long"}
    remote = data.get("remote") if data.get("remote") not in ("", None) else "origin"
    if not isinstance(remote, str) or not valid_remote(remote):
        return 400, {"error": "remote must be a named remote, not a URL or flag", "code": "bad_remote"}
    fingerprint = event_fingerprint(
        {"build_id": data.get("build_id"), "action": "finalize", "holder": holder,
         "message": message, "remote": remote}
    )

    with BUILD_LOCK:
        doc, open_err = build_open_mechanical(data.get("build_id"))
        if open_err:
            return open_err
        assert doc is not None
        build_id = str(doc.get("build_id"))
        seen = build_event_seen(doc)
        if event_id in seen:
            record = seen.get(event_id) or {}
            if record.get("fingerprint") != fingerprint:
                out = build_public(doc)
                out.update(ok=False, replayed=False, error="event_id was already used with different input",
                           code="idempotency_conflict")
                return 409, out
            out = build_public(doc)
            out["replayed"] = True
            out["receipt"] = record.get("receipt")
            return int(record.get("status") or 200), out

        existing = doc.get("pr_ready")
        if isinstance(existing, dict):
            out = build_public(doc)
            out.update(ok=True, replayed=True, receipt=existing, code="already_finalized")
            return 200, out
        now = time.time()
        lease = build_lease_live(doc, now)
        if lease is not None and lease.get("holder") != holder:
            out = build_public(doc)
            out.update(ok=False, error="another caller holds the build lease", code="lease_held")
            return 409, out

        worktree = Path(str(doc.get("worktree")))
        pending = doc.get("pending_finalize")
        pending = pending if isinstance(pending, dict) else None
        if pending and pending.get("event_id") != event_id:
            out = build_public(doc)
            out.update(ok=False, replayed=False, error="another event owns the durable finalization intent",
                       code="finalize_in_progress")
            return 409, out
        if pending and pending.get("fingerprint") not in (None, fingerprint):
            out = build_public(doc)
            out.update(ok=False, replayed=False, error="event_id was already prepared with different input",
                       code="idempotency_conflict")
            return 409, out
        if pending and pending.get("fingerprint") is None:
            legacy_input = (
                pending.get("message") == message
                and pending.get("remote", "origin") == remote
                and pending.get("holder") == holder
            )
            if not legacy_input:
                out = build_public(doc)
                out.update(ok=False, replayed=False, error="legacy finalization intent has different input",
                           code="idempotency_conflict")
                return 409, out

        if pending:
            live, detail = build_resolve_identity(doc, repos)
            if live is None:
                return build_park(doc, "finalize_adoption_mismatch", str(detail), event_id, fingerprint, holder)
            if data.get("worktree") not in (None, "") and str(data.get("worktree")) != doc.get("worktree"):
                return build_park(doc, "finalize_adoption_mismatch", "worktree does not match prepared intent",
                                  event_id, fingerprint, holder)
            head = live["head"]
            change_id = str(pending.get("verified_change_id") or "")
            remote = str(pending.get("remote") or remote)
            message = str(pending.get("message") or message)
        else:
            live, park = build_identity_park(doc, "review_passed", data.get("worktree"), None, None, repos)
            if park is not None:
                reason, detail = park
                return build_park(doc, reason, detail, event_id, fingerprint, holder)
            assert live is not None
            head = live["head"]
            change_id = build_change_id(worktree, head)
            stage = doc.get("stage")
            if stage != "ready":
                return 409, {"ok": False, "build_id": build_id, "stage": stage,
                             "error": f"finalization runs at stage 'ready'; build is at '{stage}'",
                             "code": "invalid_transition"}
            status = git_status(worktree)
            if not status.get("ok"):
                return 409, {"ok": False, "build_id": build_id,
                             "error": str(status.get("error") or "git status failed")[:BUILD_REDACT_MAX_STRING],
                             "code": "identity_unreadable"}
            dirt = build_commit_dirt(list(status.get("entries") or []))
            if dirt:
                return 409, {"ok": False, "build_id": build_id, "stage": stage, "head": head,
                             "change_id": change_id,
                             "error": "the worktree holds changes the commit would not carry: " + ", ".join(dirt[:8]),
                             "code": "worktree_not_equal", "entries": dirt[:8]}
            gaps = build_finalize_gaps(doc, change_id)
            if gaps:
                return 409, {"ok": False, "build_id": build_id, "stage": stage, "head": head,
                             "change_id": change_id, "error": gaps[0], "code": "not_ready", "gaps": gaps}
            tree_result = run_git(worktree, ["write-tree"])
            candidate_tree = str(tree_result.get("stdout") or "").strip()
            if not tree_result.get("ok") or not BUILD_HEAD_RE.fullmatch(candidate_tree):
                return 409, {"ok": False, "build_id": build_id,
                             "error": "could not freeze the staged candidate tree", "code": "candidate_unreadable"}
            authoring_boundary, boundary_err = build_git_authoring_boundary(worktree)
            if boundary_err:
                return 409, {**boundary_err, "build_id": build_id}
            pending = {
                "version": 1, "state": "prepared", "event_id": event_id, "fingerprint": fingerprint,
                "prepared_at": utcnow(), "holder": holder, "remote": remote, "branch": doc.get("branch"),
                "verified_head": head, "expected_parent": head, "candidate_tree": candidate_tree,
                "verified_change_id": change_id, "message": message, "authoring_boundary": authoring_boundary,
            }
            doc["pending_finalize"] = pending
            write_err = write_build(doc, build_id)
            if write_err:
                return int(write_err["status"]), write_err

        assert pending is not None
        if pending.get("version") is None and pending.get("sha"):
            legacy_commit, legacy_err = build_commit_identity(worktree, str(pending["sha"]))
            if legacy_err:
                return 409, {**legacy_err, "build_id": build_id}
            assert legacy_commit is not None
            legacy_gaps = []
            if live["head"] != pending.get("sha"):
                legacy_gaps.append("legacy intent SHA is not live HEAD")
            if live["branch"] != pending.get("branch"):
                legacy_gaps.append("legacy intent branch is not live")
            if pending.get("verified_head") and legacy_commit["parent"] != pending.get("verified_head"):
                legacy_gaps.append("legacy intent parent does not match verified head")
            if legacy_commit["message"] != pending.get("message"):
                legacy_gaps.append("legacy intent message does not match commit")
            if legacy_gaps:
                return build_park(doc, "finalize_adoption_mismatch", "; ".join(legacy_gaps),
                                  event_id, fingerprint, holder)
            pending = {
                **pending,
                "version": 1,
                "state": "committed",
                "fingerprint": fingerprint,
                "expected_parent": legacy_commit["parent"],
                "candidate_tree": legacy_commit["tree"],
                "tree": legacy_commit["tree"],
                "parent": legacy_commit["parent"],
                "author": legacy_commit["author"],
                "committer": legacy_commit["committer"],
                "authoring_boundary": {
                    "author": legacy_commit["author"],
                    "committer": legacy_commit["committer"],
                },
            }
            doc["pending_finalize"] = pending
            write_err = write_build(doc, build_id)
            if write_err:
                return int(write_err["status"]), write_err
        pending_state = str(pending.get("state") or ("committed" if pending.get("sha") else "prepared"))
        resumed_committed = pending_state == "committed"
        if pending_state == "prepared":
            if head == pending.get("expected_parent"):
                status = git_status(worktree)
                dirt = build_commit_dirt(list(status.get("entries") or [])) if status.get("ok") else ["unreadable"]
                tree_result = run_git(worktree, ["write-tree"]) if not dirt else {"ok": False}
                live_tree = str(tree_result.get("stdout") or "").strip() if tree_result.get("ok") else ""
                if dirt or live_tree != pending.get("candidate_tree") or live["branch"] != pending.get("branch"):
                    return build_park(doc, "finalize_adoption_mismatch",
                                      "the prepared candidate, branch, or worktree changed before commit",
                                      event_id, fingerprint, holder)
                commit = run_git(worktree, ["commit", "-m", message])
                if not commit.get("ok"):
                    blob = ((commit.get("stdout") or "") + (commit.get("stderr") or "")).lower()
                    code = "nothing_staged" if "nothing to commit" in blob or "no changes added" in blob else "commit_failed"
                    return 409, {"ok": False, "build_id": build_id,
                                 "error": "nothing was staged; the writer made no changes" if code == "nothing_staged"
                                 else str(commit.get("error") or "commit failed")[:BUILD_REDACT_MAX_STRING],
                                 "code": code, "compact": compact_cmd_signal(commit)}
                live_after, identity_err = build_live_identity(worktree)
                if identity_err:
                    return 409, {**identity_err, "build_id": build_id}
                assert live_after is not None
                live = live_after
                head = live["head"]
            commit_identity, commit_err = build_commit_identity(worktree, head)
            if commit_err:
                return 409, {**commit_err, "build_id": build_id}
            assert commit_identity is not None
            adoption_gaps = build_finalize_adoption_gaps(pending, commit_identity, live["branch"])
            if adoption_gaps:
                return build_park(doc, "finalize_adoption_mismatch", "; ".join(adoption_gaps),
                                  event_id, fingerprint, holder)
            pending = {
                **pending, "state": "committed", "committed_at": utcnow(), "sha": commit_identity["sha"],
                "tree": commit_identity["tree"], "parent": commit_identity["parent"],
                "message": commit_identity["message"], "author": commit_identity["author"],
                "committer": commit_identity["committer"],
            }
            doc["pending_finalize"] = pending
            doc["head"] = pending["sha"]
            write_err = write_build(doc, build_id)
            if write_err:
                return int(write_err["status"]), write_err
        elif pending_state != "committed":
            return build_park(doc, "finalize_adoption_mismatch",
                              f"unknown finalization intent state '{pending_state}'",
                              event_id, fingerprint, holder)

        live_now, detail = build_resolve_identity(doc, repos)
        if live_now is None:
            return build_park(doc, "finalize_adoption_mismatch", str(detail), event_id, fingerprint, holder)
        commit_identity, commit_err = build_commit_identity(worktree, str(pending.get("sha") or "HEAD"))
        if commit_err:
            return 409, {**commit_err, "build_id": build_id}
        assert commit_identity is not None
        committed_gaps = build_finalize_adoption_gaps(pending, commit_identity, live_now["branch"])
        if commit_identity.get("sha") != live_now.get("head"):
            committed_gaps.append("intent SHA is not live HEAD")
        if committed_gaps:
            return build_park(doc, "finalize_adoption_mismatch", "; ".join(committed_gaps),
                              event_id, fingerprint, holder)

        branch = str(pending.get("branch"))
        ref = f"refs/heads/{branch}"
        remote_head: str | None = None
        if resumed_committed:
            remote_head, remote_err = build_remote_branch_head(worktree, remote, branch)
            if remote_err:
                return 409, {**remote_err, "build_id": build_id, "sha": pending.get("sha")}
        if remote_head != pending.get("sha"):
            push = run_git(worktree, ["push", remote, f"{pending['sha']}:{ref}"])
            if not push.get("ok"):
                return 409, {"ok": False, "build_id": build_id, "sha": pending.get("sha"),
                             "retryable": True, "pending_finalize": pending,
                             "error": str(push.get("error") or "push failed")[:BUILD_REDACT_MAX_STRING],
                             "code": "push_failed", "compact": compact_cmd_signal(push)}

        receipt = {
            "event_id": event_id, "action": "finalize", "at": utcnow(), "holder": holder,
            "build_id": build_id, "repo": doc.get("repo"), "branch": branch, "remote": remote,
            "sha": pending.get("sha"), "verified_head": pending.get("verified_head"),
            "verified_change_id": pending.get("verified_change_id"),
            "candidate_tree": pending.get("candidate_tree"), "message": pending.get("message"),
            "committed": True, "pushed": True, "attempts": int(pending.get("attempts") or 0) + 1,
        }
        doc["pr_ready"] = receipt
        doc["head"] = pending.get("sha") or head
        doc["pending_finalize"] = {**pending, "state": "pushed", "pushed_at": utcnow()}
        build_gate_transition(doc, "pr_opened", change_id, "buildFinalize", "the reviewed commit is pushed")
        build_record_event(doc, event_id, fingerprint, receipt, 200)
        write_err = write_build(doc, build_id)
        if write_err:
            return int(write_err["status"]), write_err
        out = build_public(doc)
        out.update(ok=True, replayed=False, receipt=receipt)
        return 200, out

# --- workflow completion supervisor -----------------------------------------
#
# Rewst ships trigger.workflowCompleted, so every terminal run of a supervised
# source workflow is delivered to one named, authenticated op here. Four rules
# make that delivery worth trusting:
#
#   1. The launcher registers the source run id before monitoring starts. A
#      run that fails before it declares anything is still supervised, because
#      the mapping to build id, event id, workflow version, expected stage and
#      deadline was durable before the run could fail. The declared output is
#      corroboration, never the only way to find the build.
#   2. Delivery is idempotent on source run id plus the listener *slug*, with
#      the published version deliberately left out of the key. Rewst's outbox
#      retries and the listener gets republished; both must return the first
#      answer rather than spend a second retry.
#   3. Rewst `completed` is not success. Only Graphwing's own terminal receipt
#      is, so a graph that reported completed while skipping the intended path
#      is a workflow_fault and parks.
#   4. Retry is bounded and typed by this service. The class comes only from
#      terminal status. Circuit identity starts with the registered workflow
#      id/version, then uses bounded node/code metadata when the authenticated
#      listener has it, or stable status/error-presence when it does not.
#
# Nothing here stores a raw error, a trace body, a Graphwing key, a Rewst
# token, or a callback capability URL: identity fields are refused when they
# look like credentials, blobs are redacted and bounded, and the error channel
# is a boolean.


def supervisor_dir(build_id: str) -> Path:
    return SUPERVISOR_DIR / "builds" / build_id


def supervisor_path(build_id: str) -> Path:
    return supervisor_dir(build_id) / "supervisor.json"


def supervisor_run_key(source_run_id: str) -> str:
    """Name the index file after a digest of the run id, never the id itself.

    A Rewst run id is an opaque external string. Digesting it means no value
    Rewst ever invents can become a path segment, so the traversal question
    does not arise for the one field this service does not control.
    """
    return hashlib.sha256(source_run_id.encode()).hexdigest()


def supervisor_run_index_path(source_run_id: str) -> Path:
    return SUPERVISOR_DIR / "runs" / f"{supervisor_run_key(source_run_id)}.json"


def supervisor_write_json(path: Path, doc: dict[str, Any]) -> dict[str, Any] | None:
    """Atomic, private, bounded. Returns an error dict instead of raising.

    Private for the same reason the build document is: this file names the
    worktree, the stacks, and the local job ids of unshipped work.
    """
    raw = json.dumps(doc, indent=2) + "\n"
    if len(raw.encode()) > SUPERVISOR_STATE_MAX_BYTES:
        return {
            "ok": False,
            "error": "supervisor state exceeds the write bound",
            "code": "supervisor_state_too_large",
            "status": 413,
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".supervisor-", suffix=".json.tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(path))
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"could not persist supervisor state: {exc}"[:BUILD_REDACT_MAX_STRING],
            "code": "supervisor_write_failed",
            "status": 500,
        }
    return None


def supervisor_read_json(path: Path, expect: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read a supervisor file, telling missing apart from unreadable.

    The third case is the one that matters. Collapsing an unreadable file into
    "never registered" would let a completed source run look unsupervised, and
    the whole point of the registry is that the launch record exists before the
    run can fail.
    """
    if not path.exists():
        return None, None
    unreadable = {
        "ok": False,
        "error": "supervisor state exists but cannot be read; it has been left untouched",
        "code": "supervisor_state_unreadable",
        "status": 409,
    }
    if not path.is_file():
        return None, unreadable
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, dict(unreadable, detail=str(exc)[:BUILD_REDACT_MAX_STRING])
    if not isinstance(data, dict):
        return None, dict(unreadable, detail="supervisor state is not an object")
    for key, value in expect.items():
        if data.get(key) != value:
            return None, dict(unreadable, detail=f"supervisor state names a different {key}")
    return data, None


def supervisor_load(build_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    return supervisor_read_json(supervisor_path(build_id), {"build_id": build_id})


def supervisor_new(build_id: str) -> dict[str, Any]:
    return {
        "version": SUPERVISOR_STATE_VERSION,
        "build_id": build_id,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "runs": {},
        "run_order": [],
        "deliveries": {},
        "delivery_order": [],
        # Retry and relaunch counters, keyed by build event id rather than by
        # source run id: every retry is a different Rewst run.
        "events": {},
        "circuits": {},
        # One watch per source run and event id. Keyed rather than listed so a
        # redelivered watch request joins the watcher that is already running
        # instead of putting a second one on the same local job.
        "watches": {},
        "watch_order": [],
        "reports": [],
    }


def supervisor_save(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Persist, evicting only the things that are safe to forget.

    Reports are a window and deliveries are not: forgetting a report loses a
    line an operator can read again on the state endpoint, and forgetting a
    delivery lets Rewst's outbox run the same reconciliation twice. Runs and
    deliveries are refused at their ceiling rather than evicted, for the same
    reason build event ids are.
    """
    doc["updated_at"] = utcnow()
    # A watch is stable identity only. Scrub legacy C1 callback fields on every
    # write so an upgraded service cannot preserve an expiring capability that
    # was already present on disk.
    for watch in (doc.get("watches") or {}).values():
        if isinstance(watch, dict):
            for field in (
                "response_webhook_url",
                "response_webhook_token",
                "resume_url",
                "callback",
                "next",
            ):
                watch.pop(field, None)
    reports = doc.setdefault("reports", [])
    if len(reports) > SUPERVISOR_REPORTS_KEPT:
        doc["reports"] = reports[-SUPERVISOR_REPORTS_KEPT:]
    circuits = doc.setdefault("circuits", {})
    if len(circuits) > SUPERVISOR_CIRCUITS_MAX:
        # Only closed circuits are droppable; an open one is a park an
        # operator still has to clear.
        for key in [k for k, v in circuits.items() if not (isinstance(v, dict) and v.get("open"))][
            : len(circuits) - SUPERVISOR_CIRCUITS_MAX
        ]:
            circuits.pop(key, None)
    return supervisor_write_json(supervisor_path(doc["build_id"]), doc)


def supervisor_write_index(source_run_id: str, build_id: str) -> dict[str, Any] | None:
    """Point the run id at its build. Written after the launch record, and
    rewritten by any later identical registration that finds it missing."""
    return supervisor_write_json(
        supervisor_run_index_path(source_run_id),
        {
            "version": SUPERVISOR_STATE_VERSION,
            "source_run_id": source_run_id,
            "build_id": build_id,
            "registered_at": utcnow(),
        },
    )


def supervisor_run_lookup(source_run_id: str) -> tuple[str | None, dict[str, Any] | None]:
    doc, err = supervisor_read_json(
        supervisor_run_index_path(source_run_id), {"source_run_id": source_run_id}
    )
    if err:
        return None, err
    if doc is None:
        return None, None
    build_id = doc.get("build_id")
    return (build_id if valid_build_id(build_id) else None), None


def parse_supervisor_string(
    container: dict[str, Any],
    key: str,
    pattern: re.Pattern[str],
    code: str,
    required: bool = True,
    label: str | None = None,
    blank_ok: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    """One bounded, pattern-checked identity field.

    Absent is absent. `null` and `""` are refused with every other wrong type
    rather than folded into absent: a caller that meant to name the failed node
    and sent nothing usable should find out, not get a report that says the
    node is unknown.

    `blank_ok` is the one exception, and only for fields a Rewst template
    renders directly. A missing Jinja value renders as an empty string, not as
    an omitted key, so refusing "" for `source_workflow.slug` would reject a
    correct listener for saying nothing about an optional field.
    """
    name = label or key
    bad = {"error": f"{name} must match {pattern.pattern}", "code": code}
    if key not in container or (blank_ok and container[key] in (None, "")):
        if required and key not in container:
            return None, {"error": f"{name} is required", "code": code}
        if required:
            return None, bad
        return None, None
    raw = container[key]
    if not isinstance(raw, str):
        return None, bad
    value = raw.strip()
    if not pattern.fullmatch(value):
        return None, bad
    secret = reject_secret_text(value, name.replace(".", "_"))
    if secret:
        return None, secret
    return value, None


def parse_supervisor_timestamp(
    container: dict[str, Any], key: str, label: str
) -> tuple[str | None, dict[str, Any] | None]:
    # "" is absent here, not a bad timestamp: the listener renders these
    # straight out of the Rewst payload, and a run that never started has no
    # startedAt to render.
    if key not in container or container[key] in (None, ""):
        return None, None
    raw = container[key]
    if not isinstance(raw, str) or not SUPERVISOR_LOOSE_TS_RE.fullmatch(raw.strip()):
        return None, {"error": f"{label} must be an ISO 8601 timestamp", "code": "bad_timestamp"}
    return raw.strip(), None


def parse_supervisor_deadline(container: dict[str, Any]) -> tuple[str | None, float | None, dict[str, Any] | None]:
    """A deadline is required, and it is a UTC instant, not a duration.

    A duration would be measured from whenever this call happened to arrive,
    so a retried registration would silently extend the window it was meant to
    bound.
    """
    raw = container.get("deadline")
    bad = {"error": "deadline must be a UTC timestamp like 2026-08-25T13:00:00Z", "code": "bad_deadline"}
    if not isinstance(raw, str) or not SUPERVISOR_TS_RE.fullmatch(raw.strip()):
        return None, None, bad
    value = raw.strip()
    epoch = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    return value, epoch, None


def parse_supervisor_plan_pane(container: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """The exact planning pane, required, and never defaulted.

    Required because the alternative is a default, and a default here is a
    report delivered to whichever `plan` tab happened to be open, or to the
    graph dashboard nobody is reading. The launch is the only moment the
    caller still knows which planning session asked for this build, so that is
    where the pane is pinned.

    Checked against the live session for the same reason: refusing a pane that
    does not exist costs one 400 at launch, and accepting it costs every later
    park going nowhere.
    """
    raw = container.get("plan_pane")
    if raw in (None, ""):
        return None, {
            "error": (
                "plan_pane is required: supervision reports go to the exact saved planning "
                "session, never to a default tab or the graph dashboard"
            ),
            "code": "missing_plan_pane",
        }
    if not isinstance(raw, str) or not HERDR_PANE_ID_RE.fullmatch(raw.strip()):
        return None, {"error": "plan_pane must be a herdr pane id", "code": "bad_plan_pane"}
    pane = raw.strip()
    known, code = herdr_pane_known(pane)
    if not known:
        return None, {
            "error": "plan_pane is not a pane in this herdr session",
            "code": code,
            "plan_pane": pane,
        }
    return pane, None


def parse_supervisor_tags(container: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
    if "tags" not in container or container["tags"] is None:
        return [], None
    raw = container["tags"]
    # OpenAPI is array-first. The one string form admitted is the exact compact
    # JSON emitted at the custom-action boundary after objectBuilder preserved
    # the trigger's array type. Python reprs, CSV, scalar strings, and malformed
    # JSON remain refusals.
    if isinstance(raw, str):
        if len(raw.encode()) > SUPERVISOR_TAGS_JSON_MAX_BYTES:
            return [], {"error": "source_workflow.tags encoding is too long", "code": "bad_tags"}
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return [], {"error": "source_workflow.tags must encode a JSON array", "code": "bad_tags"}
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        return [], {"error": "source_workflow.tags must be a list of strings", "code": "bad_tags"}
    if len(raw) > SUPERVISOR_TAGS_MAX:
        return [], {"error": "source_workflow.tags is too long", "code": "bad_tags"}
    tags = [t.strip() for t in raw]
    if any(not SUPERVISOR_TAG_RE.fullmatch(tag) for tag in tags):
        return [], {
            "error": f"source_workflow.tags elements must match {SUPERVISOR_TAG_RE.pattern}",
            "code": "bad_tags",
        }
    return tags, None


def parse_supervisor_error_present(data: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """The error channel is a boolean, and it is enforced as one.

    Rewst's payload exposes `error.present` when a public error exists. Every
    neighbouring key on that object is a message, a body, or a stack, so an
    error object carrying anything but `present` is refused instead of being
    quietly redacted. Redaction is what bounds an accident; refusal is what
    keeps the trace body from being sent at all.
    """
    raw = data.get("error", data.get("error_present"))
    return supervisor_bool_flag(raw, "error.present")


def supervisor_bool_flag(raw: Any, label: str) -> tuple[bool, dict[str, Any] | None]:
    """A boolean, or the two strings a Jinja template renders one as.

    A Rewst node config is a string template, so `{{ ...error.present }}`
    arrives as "true", "false", or "" — never as a JSON boolean. Accepting
    exactly those three and nothing else keeps the field a flag: "yes", "1",
    and a sentence are all still refused, so no message can ride in on it.
    """
    if raw is None or raw == "":
        return False, None
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true", None
    if isinstance(raw, dict):
        extra = sorted(str(k) for k in raw if str(k).lower() != "present")
        if extra:
            return False, {
                "error": f"error carries raw failure text ({', '.join(extra[:4])}); send only error.present",
                "code": "raw_error_forbidden",
            }
        return supervisor_bool_flag(raw.get("present"), label)
    return False, {"error": f"{label} must be true or false", "code": "bad_error_present"}


def parse_supervisor_failure(
    data: dict[str, Any], status: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize available failure identity; classify from status alone.

    `node` and `code` are strict, bounded metadata from the authenticated
    completion listener. They label reports and refine canonical circuit
    identity when the trigger or declared workflow metadata actually supplies
    them. They never select the retry budget. Missing detail stays missing for
    identity purposes and uses the stable status/error-presence fallback; no
    failed node or trace detail is inferred.

    So `failure.class` is refused outright rather than ignored. Ignoring it
    would leave a listener quietly believing it still steered the ladder.
    """
    failure_class = SUPERVISOR_STATUS_CLASS[status]
    raw, shape_err = parse_build_object(data, "failure", "bad_failure")
    if shape_err:
        return None, shape_err
    if raw is None:
        return {
            "class": failure_class,
            "node": SUPERVISOR_UNKNOWN_NODE,
            "code": status,
            "declared": False,
        }, None
    extra = sorted(k for k in raw if k not in ("node", "code"))
    if extra:
        return None, {
            "error": (
                f"failure has unsupported keys ({', '.join(extra[:4])}); only node and code are "
                "read, and both are labels: the retry class comes from source_run.status"
            ),
            "code": "bad_failure",
        }
    node, node_err = parse_supervisor_string(
        raw, "node", SUPERVISOR_NODE_RE, "bad_failure_node", required=False, label="failure.node", blank_ok=True
    )
    if node_err:
        return None, node_err
    code, code_err = parse_supervisor_string(
        raw, "code", SUPERVISOR_CODE_RE, "bad_failure_code", required=False, label="failure.code", blank_ok=True
    )
    if code_err:
        return None, code_err
    return {
        "class": failure_class,
        "node": node or SUPERVISOR_UNKNOWN_NODE,
        "code": code or status,
        "declared": node is not None or code is not None,
    }, None


def parse_supervisor_output(data: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The declared source output, bounded and redacted, and never load-bearing.

    A run that fails before it declares anything sends nothing here, which is
    the whole reason the launch registry exists. What arrives is kept only as
    corroboration: a build id or event id that disagrees with the registration
    parks the build rather than repointing it.
    """
    raw, shape_err = parse_build_object(data, "output", "bad_output")
    if shape_err:
        return None, shape_err
    if raw is None:
        return None, None
    if len(raw) > SUPERVISOR_OUTPUT_MAX_KEYS:
        return None, {"error": "output has too many keys", "code": "bad_output"}
    leaked = sorted(k for k in raw if str(k).lower() in SUPERVISOR_RAW_ERROR_KEYS)
    if leaked:
        return None, {
            "error": f"output carries raw failure text ({', '.join(leaked[:4])})",
            "code": "raw_error_forbidden",
        }
    return redact_secrets(raw), None


def supervisor_stage_index(stage: Any) -> int | None:
    try:
        return BUILD_STAGE_ORDER.index(str(stage))
    except ValueError:
        return None


def supervisor_listener_slug(listener_id: str) -> str:
    """The half of the listener identity that survives a republish.

    A Rewst listener is identified as slug@published-version, and republishing
    it changes the version. Keying the delivery dedupe on the whole id would
    let version 8 re-decide every completion version 7 already answered: a
    second retry spent, a second park written, a second tick on the circuit,
    all for one source run. The version stays on the report, where it is
    diagnostics, and stays out of the key, where it would be a bug.
    """
    return listener_id.split("@", 1)[0].strip() or listener_id


def supervisor_circuit_signature(
    workflow_id: str,
    workflow_version: str,
    status: str,
    failure: dict[str, Any],
    error_present: bool,
) -> str:
    """Canonical bounded identity from registered and authenticated metadata.

    Workflow identity is pinned in the launch record. A completion listener may
    refine the identity with the trigger's or workflow's declared node/code,
    but those fields are strict slugs. When they are unavailable, the stable
    fallback is terminal status plus public error presence; no trace is
    invented and no free text enters a key.
    """
    prefix = f"{workflow_id}@{workflow_version}#"
    if failure.get("declared"):
        return f"{prefix}{failure['node']}:{failure['code']}"
    presence = "error" if error_present else "no_error"
    return f"{prefix}unknown:{status}:{presence}"


def supervisor_circuit_overflow_signature(
    workflow_id: str, workflow_version: str, status: str, error_present: bool
) -> str:
    presence = "error" if error_present else "no_error"
    return f"{workflow_id}@{workflow_version}#overflow:{status}:{presence}"


SUPERVISOR_NEXT_ACTION = {
    "receipt": (
        "none: build {build_id} already recorded the terminal receipt for event {event_id}"
    ),
    "watch": (
        "watch local job {job_id} (status {local_job_status}); it still owns event {event_id} "
        "on build {build_id}"
    ),
    "relaunch": (
        "relaunch event {event_id} on build {build_id} at stage {expected_stage}: the claimed "
        "local job {job_id} never started"
    ),
    "retry_event": (
        "re-run graphwing-pre-pr-build with build_id={build_id} event_id={event_id}; attempt "
        "{attempts} of {retry_budget}"
    ),
    "ignored": (
        "none: source run {source_run_id} ran in {execution_mode} mode, which is not supervised"
    ),
}
SUPERVISOR_PARK_NEXT = {
    "workflow_fault": (
        "read the Rewst trace for run {source_run_id}: {workflow_id}@{workflow_version} reported "
        "completed without recording event {event_id} on build {build_id}. Fix the skipped path, "
        "then register a new event id. Worktree {worktree} and stacks {stacks} are untouched."
    ),
    "deterministic_failure": (
        "fix the graph, schema, or config behind node {failed_node} on "
        "{workflow_id}@{workflow_version}, then register a new event id for build {build_id}. "
        "Do not re-run event {event_id} unchanged."
    ),
    "retry_exhausted": (
        "transient retry for event {event_id} on build {build_id} is spent after {attempts} "
        "attempts; check the tunnel and the Rewst integration, then register a new event id"
    ),
    "circuit_open": (
        "stop retrying {workflow_id}@{workflow_version}: it ended {source_status} and was given up "
        "on {circuit_count} times (last reported node {failed_node}, {error_code}). Fix the graph, "
        "then register a new event id for build {build_id}."
    ),
    "state_trace_disagreement": (
        "compare build {build_id} at stage {build_stage} with Rewst run {source_run_id}; they "
        "disagree, so nothing was advanced. Worktree {worktree} and stacks {stacks} are intact."
    ),
    "deadline_exceeded": (
        "build {build_id} passed its supervision deadline {deadline} at stage {build_stage}; "
        "decide to resume or abandon it by hand. Worktree {worktree} and stacks {stacks} are intact."
    ),
    "build_parked": (
        "build {build_id} is already parked ({build_park_reason}); clear that park before "
        "supervising another run"
    ),
    "relaunch_exhausted": (
        "local job {job_id} for event {event_id} on build {build_id} was relaunched and still "
        "never started; check the agent seat by hand"
    ),
    "missing_build": (
        "build {build_id} registered for run {source_run_id} has no state document; re-open the "
        "build before retrying"
    ),
    "build_unreadable": (
        "build {build_id} state cannot be read and has been left untouched; repair it by hand "
        "before supervising run {source_run_id} again"
    ),
}


def supervisor_next_action(action: str, park_reason: str | None, ctx: dict[str, Any]) -> str:
    template = (
        SUPERVISOR_PARK_NEXT.get(park_reason or "", "")
        if action in ("park", "circuit_open")
        else SUPERVISOR_NEXT_ACTION.get(action, "")
    )
    if not template:
        return f"inspect build {ctx.get('build_id')} and Rewst run {ctx.get('source_run_id')} by hand"
    return template.format_map({k: ("" if v is None else v) for k, v in ctx.items()})[:BUILD_REDACT_MAX_STRING]


def supervisor_report_line(report: dict[str, Any]) -> str:
    return (
        f"supervisor {report.get('action')} build={report.get('build_id')} "
        f"run={report.get('source_run_id')} wf={report.get('workflow_id')}@"
        f"{report.get('workflow_version')} node={report.get('failed_node') or '-'} "
        f"stage={report.get('build_stage')} job={report.get('local_job_status')} "
        f"next={report.get('next_operator_action')}"
    )


def supervisor_delivery_intent(report: dict[str, Any], line: str) -> dict[str, Any]:
    identity = {
        "build_id": report.get("build_id"),
        "source_run_id": report.get("source_run_id"),
        "event_id": report.get("event_id"),
        "watch_id": report.get("watch_id"),
        "action": report.get("action"),
    }
    delivery_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "delivery_id": delivery_id,
        "state": "pending",
        "pane_id": report.get("plan_pane"),
        "line": line,
        "attempts": 0,
        "attempted": False,
        "ok": False,
        "code": "pending",
        "at": utcnow(),
        # Herdr send-text has no idempotency key. A crash after send and before
        # this receipt write can repeat the same stable delivery id.
        "at_least_once": True,
    }


def supervisor_attempt_delivery(build_id: str, delivery_id: str) -> dict[str, Any] | None:
    """Attempt one durable intent, marking delivered only after send succeeds."""
    with SUPERVISOR_LOCK:
        doc, read_err = supervisor_load(build_id)
        if read_err or doc is None:
            return None
        report = next(
            (
                row
                for row in doc.get("reports") or []
                if isinstance(row, dict)
                and isinstance(row.get("delivery"), dict)
                and row["delivery"].get("delivery_id") == delivery_id
            ),
            None,
        )
        if not isinstance(report, dict) or report["delivery"].get("state") != "pending":
            return report.get("delivery") if isinstance(report, dict) else None
        pane_id = report["delivery"].get("pane_id")
        line = str(report["delivery"].get("line") or "")
    receipt = herdr_plan_deliver(pane_id, f"[{delivery_id}] {line}")
    with SUPERVISOR_LOCK:
        doc, read_err = supervisor_load(build_id)
        if read_err or doc is None:
            return None
        report = next(
            (
                row
                for row in doc.get("reports") or []
                if isinstance(row, dict)
                and isinstance(row.get("delivery"), dict)
                and row["delivery"].get("delivery_id") == delivery_id
            ),
            None,
        )
        if not isinstance(report, dict) or report["delivery"].get("state") != "pending":
            return report.get("delivery") if isinstance(report, dict) else None
        delivery = report["delivery"]
        delivery["attempts"] = int(delivery.get("attempts") or 0) + 1
        delivery["attempted"] = bool(receipt.get("attempted"))
        delivery["ok"] = bool(receipt.get("ok"))
        delivery["code"] = receipt.get("code")
        delivery["last_attempt_at"] = receipt.get("at") or utcnow()
        delivery["receipt"] = receipt
        if receipt.get("ok"):
            delivery["state"] = "delivered"
            delivery["delivered_at"] = delivery["last_attempt_at"]
        for entry in (doc.get("deliveries") or {}).values():
            result = entry.get("result") if isinstance(entry, dict) else None
            if isinstance(result, dict) and (result.get("report") or {}).get("delivery", {}).get("delivery_id") == delivery_id:
                result["delivery"] = delivery
                result["delivery_ok"] = bool(delivery.get("ok"))
                result["delivery_code"] = delivery.get("code")
        watch_id = report.get("watch_id")
        if watch_id:
            for record in (doc.get("watches") or {}).values():
                if isinstance(record, dict) and record.get("watch_id") == watch_id:
                    record["delivery"] = delivery
                    break
        save_err = supervisor_save(doc)
        if save_err:
            return None
        return dict(delivery)


def supervisor_recover_deliveries() -> int:
    pending: list[tuple[str, str]] = []
    root = SUPERVISOR_DIR / "builds"
    if root.is_dir():
        with SUPERVISOR_LOCK:
            for path in root.glob("*/supervisor.json"):
                build_id = path.parent.name
                if not valid_build_id(build_id):
                    continue
                doc, read_err = supervisor_load(build_id)
                if read_err or doc is None:
                    continue
                for report in doc.get("reports") or []:
                    delivery = report.get("delivery") if isinstance(report, dict) else None
                    if isinstance(delivery, dict) and delivery.get("state") == "pending":
                        pending.append((build_id, str(delivery.get("delivery_id") or "")))
    for build_id, delivery_id in pending:
        if delivery_id:
            supervisor_attempt_delivery(build_id, delivery_id)
    return len(pending)


def supervisor_local_job(job_id: str | None) -> tuple[str, dict[str, Any] | None]:
    """What the local writer/test/reviewer job is actually doing right now.

    `unclaimed` and `missing` are different answers and drive different
    recoveries: nothing was ever claimed, versus a claim that exists on the
    launch record but has no job on disk to go with it.
    """
    if not job_id:
        return "unclaimed", None
    with JOB_LOCK:
        job = read_job(job_id)
    if job is None:
        return "missing", None
    status = str(job.get("status") or "")
    if status in WATCH_ACTIVE:
        return "active", job
    return (status or "unknown"), job


def parse_supervisor_continuation(
    data: dict[str, Any], repos: dict[str, str]
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    raw, shape_err = parse_build_object(data, "continuation", "bad_continuation")
    if shape_err:
        return None, shape_err
    if raw is None:
        return None, {"error": "continuation is required", "code": "bad_continuation"}
    extra = sorted(str(k) for k in raw if k not in SUPERVISOR_CONTINUATION_FIELDS)
    missing = sorted(k for k in SUPERVISOR_CONTINUATION_FIELDS if k not in raw)
    if extra or missing:
        return None, {
            "error": "continuation must contain exactly the bounded source invocation fields",
            "code": "bad_continuation",
            "missing": missing,
            "unsupported": extra,
        }

    repo, _resolved = resolve_repo(raw.get("repo"), repos)
    if repo is None:
        return None, {"error": "continuation.repo must be an allowlisted repo", "code": "bad_continuation"}
    prompt = raw.get("writer_prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > PROMPT_MAX_CHARS:
        return None, {"error": "continuation.writer_prompt is required and bounded", "code": "bad_continuation"}
    prompt = prompt.strip()
    secret = reject_secret_text(prompt, "continuation.writer_prompt")
    if secret:
        return None, secret

    prompt_ref = raw.get("writer_prompt_ref")
    if not isinstance(prompt_ref, str):
        return None, {"error": "continuation.writer_prompt_ref is required", "code": "bad_continuation"}
    prompt_ref = prompt_ref.strip()
    if (
        not SUPERVISOR_CONTINUATION_REF_RE.fullmatch(prompt_ref)
        or ".." in Path(prompt_ref).parts
        or prompt_ref.startswith("/")
    ):
        return None, {"error": "continuation.writer_prompt_ref must be repo-relative", "code": "bad_continuation"}
    secret = reject_secret_text(prompt_ref, "continuation.writer_prompt_ref")
    if secret:
        return None, secret

    out = {"repo": repo, "writer_prompt": prompt, "writer_prompt_ref": prompt_ref}
    for field in ("fast_recipe", "integration_recipe", "class_stamp", "route_stamp"):
        value, value_err = parse_supervisor_string(
            raw, field, SUPERVISOR_SLUG_RE, "bad_continuation", label=f"continuation.{field}"
        )
        if value_err:
            return None, value_err
        assert value is not None
        out[field] = value
    reviewer, reviewer_err = parse_supervisor_string(
        raw, "reviewer", SUPERVISOR_SLUG_RE, "bad_continuation", label="continuation.reviewer"
    )
    if reviewer_err:
        return None, reviewer_err
    assert reviewer is not None
    if reviewer not in {"sonnet", "opus", "fable", "sol", "grok", "terra"}:
        return None, {"error": "continuation.reviewer is not supported", "code": "bad_continuation"}
    out["reviewer"] = reviewer
    return out, None


def supervisor_register(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    source_run_id, run_err = parse_supervisor_string(
        data, "source_run_id", SUPERVISOR_RUN_ID_RE, "bad_source_run_id"
    )
    if run_err:
        return 400, run_err
    assert source_run_id is not None
    build_id = data.get("build_id")
    if not valid_build_id(build_id):
        return 400, {"error": "invalid build_id", "code": "bad_build_id"}
    assert isinstance(build_id, str)
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return 400, {"error": "event_id is required", "code": "bad_event_id"}
    workflow_id, wf_err = parse_supervisor_string(
        data, "workflow_id", SUPERVISOR_SLUG_RE, "bad_workflow_id"
    )
    if wf_err:
        return 400, wf_err
    workflow_version, ver_err = parse_supervisor_string(
        data, "workflow_version", SUPERVISOR_SLUG_RE, "bad_workflow_version"
    )
    if ver_err:
        return 400, ver_err
    expected_stage = data.get("expected_stage")
    if expected_stage not in BUILD_STAGE_ORDER:
        return 400, {
            "error": "expected_stage must be a pre-PR build stage",
            "code": "bad_expected_stage",
            "allowed": list(BUILD_STAGE_ORDER),
        }
    deadline, deadline_epoch, dl_err = parse_supervisor_deadline(data)
    if dl_err:
        return 400, dl_err
    job_id = data.get("job_id")
    if job_id not in (None, "") and not (isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id)):
        return 400, {"error": "job_id must be a graphwing job id", "code": "bad_job_id"}
    job_id = job_id or None
    plan_pane, pane_err = parse_supervisor_plan_pane(data)
    if pane_err:
        return 400, pane_err
    continuation, continuation_err = parse_supervisor_continuation(data, repos)
    if continuation_err:
        return 400, continuation_err
    assert continuation is not None

    record = {
        "source_run_id": source_run_id,
        "build_id": build_id,
        "event_id": event_id,
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "expected_stage": expected_stage,
        "deadline": deadline,
        "deadline_epoch": deadline_epoch,
        "job_id": job_id,
        "plan_pane": plan_pane,
        "continuation": continuation,
    }
    # Everything the registration promises. A second registration that changes
    # any of it is a different launch wearing the same run id, which is exactly
    # the conflict this rejects.
    fingerprint = event_fingerprint({k: record[k] for k in sorted(record) if k != "deadline_epoch"})

    with SUPERVISOR_LOCK:
        mapped, index_err = supervisor_run_lookup(source_run_id)
        if index_err:
            return int(index_err["status"]), index_err
        if mapped is not None and mapped != build_id:
            return 409, {
                "ok": False,
                "error": "source_run_id is already registered to a different build",
                "code": "registration_conflict",
                "source_run_id": source_run_id,
                "build_id": mapped,
            }
        # The build has to exist first. Registering against a build that was
        # never opened would make an unsupervisable run look supervised, and
        # the completion would arrive with nothing to reconcile against.
        with BUILD_LOCK:
            build, build_err = load_build(build_id)
        if build_err:
            return int(build_err["status"]), build_err
        if build is None:
            return 404, {"error": "unknown build", "code": "unknown_build", "build_id": build_id}
        if build.get("version") != BUILD_STATE_VERSION:
            return 409, build_version_park(build)

        doc, read_err = supervisor_load(build_id)
        if read_err:
            return int(read_err["status"]), read_err
        if doc is None:
            doc = supervisor_new(build_id)
        if doc.get("version") != SUPERVISOR_STATE_VERSION:
            return 409, {
                "ok": False,
                "error": "supervisor state version is not readable by this service",
                "code": "incompatible_supervisor_version",
                "found_version": doc.get("version"),
                "expected_version": SUPERVISOR_STATE_VERSION,
                "build_id": build_id,
            }
        runs = doc.setdefault("runs", {})
        existing = runs.get(source_run_id)
        if isinstance(existing, dict):
            if existing.get("fingerprint") != fingerprint:
                return 409, {
                    "ok": False,
                    "error": "source_run_id is already registered with different input",
                    "code": "registration_conflict",
                    "source_run_id": source_run_id,
                    "build_id": build_id,
                }
            # The launch record survived a registration whose index write did
            # not. Nothing else ever recreates that entry, so an identical
            # retry has to: without it the run stays permanently unsupervised,
            # and it looks registered while it does. This is why the retry is
            # worth making idempotent rather than merely cheap.
            if mapped is None:
                repair_err = supervisor_write_index(source_run_id, build_id)
                if repair_err:
                    return int(repair_err["status"]), repair_err
            out = {
                "ok": True,
                "registered": False,
                "supervised": True,
                **{k: v for k, v in record.items() if k != "continuation"},
            }
            out["version"] = SUPERVISOR_STATE_VERSION
            out["index_repaired"] = mapped is None
            out.pop("deadline_epoch", None)
            return 200, out
        if len(runs) >= SUPERVISOR_RUNS_MAX:
            # Evicting a launch record would make an old run unsupervisable,
            # so refuse without touching what is already there.
            return 409, {
                "ok": False,
                "error": "build has registered its whole supervision history; open a new build",
                "code": "supervision_history_full",
                "build_id": build_id,
                "run_count": len(runs),
                "run_capacity": SUPERVISOR_RUNS_MAX,
            }
        runs[source_run_id] = {**record, "fingerprint": fingerprint, "registered_at": utcnow()}
        doc.setdefault("run_order", []).append(source_run_id)
        save_err = supervisor_save(doc)
        if save_err:
            return int(save_err["status"]), save_err
        # The index is written second on purpose. An index entry with no launch
        # record would answer "supervised" for a run this service cannot
        # reconcile; a launch record with no index entry is invisible only
        # until the next registration, which repairs it above.
        index_err = supervisor_write_index(source_run_id, build_id)
        if index_err:
            return int(index_err["status"]), index_err
        out = {
            "ok": True,
            "registered": True,
            "supervised": True,
            **{k: v for k, v in record.items() if k != "continuation"},
        }
        out["version"] = SUPERVISOR_STATE_VERSION
        out["index_repaired"] = False
        out.pop("deadline_epoch", None)
        return 201, out


def supervisor_bind_job_locked(source_run_id: str, job_id: str) -> dict[str, Any] | None:
    """Bind while SUPERVISOR_LOCK is held. None means new or exact replay."""
    build_id, index_err = supervisor_run_lookup(source_run_id)
    if index_err:
        return index_err
    if build_id is None:
        return {
            "ok": False,
            "error": "source run has no launch registration; it is not supervised",
            "code": "unknown_source_run",
            "status": 404,
            "source_run_id": source_run_id,
        }
    doc, read_err = supervisor_load(build_id)
    if read_err:
        return read_err
    launch = (doc or {}).get("runs", {}).get(source_run_id)
    if not isinstance(doc, dict) or not isinstance(launch, dict):
        return {
            "error": "launch registration is missing",
            "code": "supervisor_state_unreadable",
            "status": 409,
        }
    existing = launch.get("job_id")
    if existing not in (None, "", job_id):
        return {
            "ok": False,
            "error": "source run is already bound to a different local job",
            "code": "job_binding_conflict",
            "status": 409,
            "source_run_id": source_run_id,
            "build_id": build_id,
            "job_id": existing,
        }
    if existing in (None, ""):
        launch["job_id"] = job_id
        save_err = supervisor_save(doc)
        if save_err:
            return save_err
    return None


def supervisor_bind(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if set(data) != {"source_run_id", "job_id"}:
        return 400, {"error": "source_run_id and job_id are the only accepted fields", "code": "bad_job_binding"}
    source_run_id, run_err = parse_supervisor_string(
        data, "source_run_id", SUPERVISOR_RUN_ID_RE, "bad_source_run_id"
    )
    if run_err:
        return 400, run_err
    job_id = data.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return 400, {"error": "job_id must be a graphwing job id", "code": "bad_job_id"}
    assert source_run_id is not None

    with SUPERVISOR_LOCK:
        build_id, index_err = supervisor_run_lookup(source_run_id)
        if index_err:
            return int(index_err["status"]), index_err
        doc, read_err = supervisor_load(build_id) if build_id else (None, None)
        existing = ((doc or {}).get("runs", {}).get(source_run_id) or {}).get("job_id")
        bind_err = supervisor_bind_job_locked(source_run_id, job_id)
        if bind_err:
            return int(bind_err.get("status") or 409), bind_err
        assert build_id is not None
        launch = ((doc or {}).get("runs", {}).get(source_run_id) or {})
        bound = existing in (None, "")
        return 200, {
            "ok": True,
            "bound": bound,
            "source_run_id": source_run_id,
            "build_id": build_id,
            "event_id": launch.get("event_id"),
            "job_id": job_id,
        }


def supervisor_public(doc: dict[str, Any], build_id: str) -> dict[str, Any]:
    circuits = doc.get("circuits") or {}
    return {
        "ok": True,
        "version": doc.get("version"),
        "build_id": build_id,
        "reports": (doc.get("reports") or [])[-SUPERVISOR_REPORTS_KEPT:],
        "circuits": [
            {"signature": sig, **{k: rec.get(k) for k in ("count", "open", "first_at", "last_at")}}
            for sig, rec in circuits.items()
            if isinstance(rec, dict)
        ],
        "circuit_threshold": SUPERVISOR_CIRCUIT_THRESHOLD,
        "events": doc.get("events") or {},
        "watches": [
            {
                "key": key,
                **{
                    k: rec.get(k)
                    for k in (
                        "watch_id", "source_run_id", "event_id", "job_id", "state",
                        "action", "outcome", "park_reason", "deadline", "started_at", "finished_at",
                        "job_status", "delivery",
                    )
                },
            }
            for key, rec in (doc.get("watches") or {}).items()
            if isinstance(rec, dict)
        ],
        # The reports an operator was meant to read and this service could not
        # deliver. Surfaced as its own count so a wedged planning pane is a
        # number on the state endpoint, not something you find by reading every
        # report's delivery block.
        "undelivered_report_count": sum(
            1
            for r in (doc.get("reports") or [])
            if isinstance(r, dict) and not ((r.get("delivery") or {}).get("ok", True))
        ),
        "run_count": len(doc.get("runs") or {}),
        "run_capacity": SUPERVISOR_RUNS_MAX,
        "delivery_count": len(doc.get("deliveries") or {}),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def supervisor_report_op(qs: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    """Read the saved planning-session reports back.

    This is the whole human path: there is no wait node in the listener and no
    idle callback, so the report an operator acts on is a durable record they
    can fetch again after the run, the terminal, and the Rewst trace are gone.
    """
    build_id = first_query(qs, "build_id")
    source_run_id = first_query(qs, "source_run_id")
    if not build_id and not source_run_id:
        return 400, {"error": "build_id or source_run_id is required", "code": "missing_selector"}
    if source_run_id:
        if not SUPERVISOR_RUN_ID_RE.fullmatch(source_run_id):
            return 400, {"error": "invalid source_run_id", "code": "bad_source_run_id"}
        mapped, index_err = supervisor_run_lookup(source_run_id)
        if index_err:
            return int(index_err["status"]), index_err
        if mapped is None:
            return 404, {
                "error": "source run is not registered for supervision",
                "code": "unknown_source_run",
                "source_run_id": source_run_id,
            }
        if build_id and build_id != mapped:
            return 409, {
                "error": "source_run_id is registered to a different build",
                "code": "registration_conflict",
                "source_run_id": source_run_id,
                "build_id": mapped,
            }
        build_id = mapped
    if not valid_build_id(build_id):
        return 400, {"error": "invalid build_id", "code": "bad_build_id"}
    assert build_id is not None
    doc, read_err = supervisor_load(build_id)
    if read_err:
        return int(read_err["status"]), read_err
    if doc is None:
        return 404, {"error": "no supervision state for this build", "code": "unknown_supervision", "build_id": build_id}
    out = supervisor_public(doc, build_id)
    if source_run_id:
        out["source_run_id"] = source_run_id
        out["reports"] = [r for r in out["reports"] if r.get("source_run_id") == source_run_id]
    return 200, out


def supervisor_event_counters(doc: dict[str, Any], event_id: str) -> dict[str, Any]:
    """Retry counters live on the build event, not on the source run.

    A retry produces a *new* Rewst run with a new run id and its own launch
    registration, so a counter kept on the launch record would read zero every
    time and the bound would never bind. The build event id is the thing all
    those runs have in common, so that is what the budget is spent against.
    """
    events = doc.setdefault("events", {})
    entry = events.get(event_id)
    if not isinstance(entry, dict):
        entry = {"attempts": 0, "relaunches": 0, "gave_up": 0}
        events[event_id] = entry
    return entry


# The park reasons this supervisor writes when it gives up on a failure shape.
# A build sitting in one of these is parked *by* the ladder, so the ladder is
# still entitled to count the next delivery of that shape against the circuit.
SUPERVISOR_OWNED_PARKS = frozenset(
    {"workflow_fault", "deterministic_failure", "retry_exhausted", "relaunch_exhausted", "circuit_open"}
)


def supervisor_owns_park(build: dict[str, Any]) -> bool:
    """True when the park the build is sitting in is one this supervisor wrote.

    The proof is the build's own receipt log, not a flag kept on the side:
    every automatic park this service writes lands under a `supervisor:<run>`
    event id carrying the reason it parked for. An operator park has no such
    receipt, so an operator-parked build stays short-circuited and its reason
    stays its own.
    """
    reason = str(build.get("park_reason") or "")
    if reason not in SUPERVISOR_OWNED_PARKS:
        return False
    for event_id, entry in (build_event_seen(build) or {}).items():
        if not str(event_id).startswith("supervisor:") or not isinstance(entry, dict):
            continue
        receipt = entry.get("receipt")
        if isinstance(receipt, dict) and receipt.get("park_reason") == reason:
            return True
    return False


def supervisor_prior_park(build: dict[str, Any], source_run_id: str) -> dict[str, Any] | None:
    """The park this exact source run already wrote on this build, if any.

    Two durable writes back one park: the build state, and the supervisor state
    that records the delivery. They cannot be made one write, so the second can
    fail after the first landed. Rewst's outbox then redelivers a completion
    this service has already acted on, and without this the redelivery would
    walk the ladder again -- another give-up charged to the circuit, on the
    strength of a park it wrote itself a moment ago.

    The proof is the receipt `build_park` left under `supervisor:<run>`, which
    is durable, run-scoped, and written in the same transaction as the park.
    """
    entry = (build_event_seen(build) or {}).get(f"supervisor:{source_run_id}")
    if not isinstance(entry, dict):
        return None
    receipt = entry.get("receipt")
    return receipt if isinstance(receipt, dict) and receipt.get("park_reason") else None


def supervisor_decide(
    doc: dict[str, Any],
    launch: dict[str, Any],
    build: dict[str, Any] | None,
    build_err: dict[str, Any] | None,
    status: str,
    failure: dict[str, Any],
    error_present: bool,
    source_run_id: str,
    now: float,
) -> dict[str, Any]:
    """The whole reconciliation ladder, as one decision over durable state.

    Ordered so the cheap, certain answers come first. A recorded terminal
    receipt ends it; an already-parked build ends it; a trace that disagrees
    with the state ends it. Only when none of those apply does this spend a
    retry.

    The circuit counts the times this supervisor *gave up* on one failure
    shape, not the times that shape appeared. Counting appearances would make
    the breaker fire on the second transient blip and the bounded retry above
    it would be dead code. Counting give-ups reads AC6 as one ladder:
    deterministic never retries, so it parks on the first and opens the circuit
    on the second; transient parks only once its budget is spent.
    """
    event_id = launch["event_id"]
    counters = supervisor_event_counters(doc, event_id)
    out: dict[str, Any] = {
        "receipt": None,
        "workflow_fault": False,
        "circuit": None,
        "attempts": int(counters.get("attempts") or 0),
        "retry_budget": SUPERVISOR_RETRY_BUDGET[failure["class"]],
        "local_job_status": "unclaimed",
    }

    def give_up(reason: str) -> dict[str, Any]:
        """Park, and let a second give-up on the same shape open the circuit."""
        signature = supervisor_circuit_signature(
            str(launch.get("workflow_id")),
            str(launch.get("workflow_version")),
            status,
            failure,
            error_present,
        )
        circuits = doc.setdefault("circuits", {})
        # Reserve the final slot for a deterministic overflow bucket. New
        # authoritative node/code identities remain separate until the bound;
        # after it, a caller cannot grow the map by minting more valid slugs.
        if signature not in circuits and len(circuits) >= max(0, SUPERVISOR_CIRCUITS_MAX - 1):
            signature = supervisor_circuit_overflow_signature(
                str(launch.get("workflow_id")),
                str(launch.get("workflow_version")),
                status,
                error_present,
            )
        if signature not in circuits and len(circuits) >= SUPERVISOR_CIRCUITS_MAX:
            closed = sorted(
                key
                for key, value in circuits.items()
                if not (isinstance(value, dict) and value.get("open"))
            )
            if closed:
                circuits.pop(closed[0], None)
            elif circuits:
                # Legacy state may already have filled every slot with open
                # entries before the reserved overflow bucket existed. Reuse a
                # stable existing bucket rather than exceed the hard bound.
                signature = sorted(circuits)[0]
        entry = circuits.get(signature)
        if not isinstance(entry, dict):
            entry = {"count": 0, "open": False, "first_at": utcnow(), "last_at": None}
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["last_at"] = utcnow()
        entry["open"] = entry["count"] >= SUPERVISOR_CIRCUIT_THRESHOLD
        circuits[signature] = entry
        counters["gave_up"] = int(counters.get("gave_up") or 0) + 1
        out["circuit"] = {
            "signature": signature,
            "count": entry["count"],
            "open": entry["open"],
            "threshold": SUPERVISOR_CIRCUIT_THRESHOLD,
        }
        if entry["open"]:
            return {**out, "action": "circuit_open", "park_reason": "circuit_open"}
        return {**out, "action": "park", "park_reason": reason}

    if build_err:
        return {**out, "action": "park", "park_reason": "build_unreadable", "build_stage": None}
    if build is None:
        return {**out, "action": "park", "park_reason": "missing_build", "build_stage": None}
    stage = str(build.get("stage") or "")
    out["build_stage"] = stage

    receipt = supervisor_terminal_event_receipt(build, event_id, launch.get("job_id"))
    job_state, _job = supervisor_local_job(launch.get("job_id"))
    if receipt is None:
        receipt = supervisor_bound_job_receipt(build, _job)
    out["local_job_status"] = job_state
    if job_state == "active":
        # A start/claim receipt is not terminal while its bound local job is
        # still active. Watching must win before the receipt shortcut.
        return {**out, "action": "watch", "park_reason": None}
    if job_state == "missing":
        # Likewise, a claim whose acknowledged job vanished remains eligible
        # for the one bounded relaunch even though the claim receipt exists.
        if int(counters.get("relaunches") or 0) < SUPERVISOR_RELAUNCH_MAX:
            counters["relaunches"] = int(counters.get("relaunches") or 0) + 1
            return {**out, "action": "relaunch", "park_reason": None}
        return give_up("relaunch_exhausted")
    claim_receipt = receipt.get("receipt") if isinstance(receipt, dict) else None
    if (
        job_state == "unclaimed"
        and isinstance(claim_receipt, dict)
        and claim_receipt.get("action") in {"start_writer", "verify_passed", "review_passed"}
    ):
        # The Graph acknowledged the transition but never durably named the
        # local job. Treating this as terminal success is the ACK/bind crash.
        return {**out, "action": "park", "park_reason": "state_trace_disagreement"}
    if isinstance(receipt, dict) and receipt.get("fingerprint"):
        # Graphwing's own terminal receipt is the only proof the transition
        # ran. Having it makes every terminal Rewst status the same answer,
        # including a failure that happened after the receipt was written.
        return {**out, "action": "receipt", "park_reason": None, "receipt": receipt.get("receipt")}

    # No receipt. Rewst calling this run `completed` is therefore a fault, not
    # a success: the graph reported done without doing the thing.
    out["workflow_fault"] = status == "completed"

    if stage == "parked":
        prior = supervisor_prior_park(build, source_run_id)
        if prior is not None:
            # This delivery's own park is already on the build, so the write
            # that records the delivery is what did not land. Returning the
            # recorded outcome is the whole answer: charging the circuit again
            # would count one give-up twice, and re-parking would overwrite the
            # reason with the reason it already has.
            reason = str(prior.get("park_reason"))
            action = "circuit_open" if reason == "circuit_open" else "park"
            return {**out, "action": action, "park_reason": reason, "park_replayed": True}
        if not supervisor_owns_park(build):
            return {**out, "action": "park", "park_reason": "build_parked"}
        # This supervisor parked the build for a fault it owns, and the same
        # graph version just failed again. Ending here would freeze the
        # circuit at one: the shape that keeps recurring after the first park
        # is exactly what the breaker exists to catch. So the shape is
        # counted, but the build is never advanced out of its park.
        return give_up("build_parked")

    expected_index = supervisor_stage_index(launch.get("expected_stage"))
    stage_index = supervisor_stage_index(stage)
    if expected_index is not None and stage_index is not None and stage_index > expected_index:
        # The build is past the stage this run was launched to reach, and it
        # got there without recording this event. Something else advanced it.
        return {**out, "action": "park", "park_reason": "state_trace_disagreement"}

    deadline_epoch = launch.get("deadline_epoch")
    if isinstance(deadline_epoch, (int, float)) and now > float(deadline_epoch):
        return {**out, "action": "park", "park_reason": "deadline_exceeded"}

    if job_state != "unclaimed":
        # The job finished and no terminal receipt exists. The trace and the
        # state disagree, so nothing is advanced and nothing is torn down.
        return {**out, "action": "park", "park_reason": "state_trace_disagreement"}

    if out["workflow_fault"]:
        # Re-running a graph that deterministically skipped its own path just
        # skips it again, so the fault parks with a report instead.
        return give_up("workflow_fault")

    if out["attempts"] < out["retry_budget"]:
        counters["attempts"] = out["attempts"] + 1
        out["attempts"] = counters["attempts"]
        return {**out, "action": "retry_event", "park_reason": None}
    return give_up("deterministic_failure" if failure["class"] == "deterministic" else "retry_exhausted")


def supervisor_terminal_event_receipt(
    build: dict[str, Any], event_id: str, job_id: Any = None
) -> dict[str, Any] | None:
    """Find this source run's terminal Graphwing receipt under its bounded event prefix.

    The Rewst run registers one base event id. Individual Graphwing operations
    append stable suffixes (`.claim-initial`, `.writer-done`, `.fast`, and so
    on), so looking up only the base id misses the receipt the same run wrote.
    A bound local job is the strongest link. Synchronous phases have no job and
    use the latest non-claim receipt under the exact registered prefix.
    """
    seen = build_event_seen(build) or {}
    exact = seen.get(event_id)
    if isinstance(exact, dict) and exact.get("fingerprint"):
        return exact
    prefix = event_id + "."
    candidates: list[dict[str, Any]] = []
    for key in reversed(build.get("event_order") or []):
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        entry = seen.get(key)
        receipt = entry.get("receipt") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not entry.get("fingerprint") or not isinstance(receipt, dict):
            continue
        if receipt.get("action") == "claim":
            continue
        if isinstance(job_id, str) and job_id:
            if receipt.get("job_id") == job_id:
                return entry
            if receipt.get("job_id") in (None, ""):
                candidates.append(entry)
            continue
        return entry
    # A registered job may have a terminal operation receipt that predates the
    # bind field on legacy state. Accept only when there is one unambiguous
    # non-claim candidate under this event prefix.
    return candidates[0] if len(candidates) == 1 else None


def supervisor_bound_job_receipt(
    build: dict[str, Any], job: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Validate a bound async job receipt against the durable build projection."""
    if not isinstance(job, dict):
        return None
    status = str(job.get("status") or "")
    if not status or status in WATCH_ACTIVE:
        return None
    receipt = job.get("receipt")
    job_id = job.get("job_id")
    if not isinstance(receipt, dict) or not isinstance(job_id, str) or receipt.get("job_id") != job_id:
        return None
    if job.get("kind") == "build_checks":
        phase = receipt.get("phase")
        check = (build.get("checks") or {}).get(phase) if phase in BUILD_CHECK_PHASES else None
        if not isinstance(check, dict):
            return None
        expected = {
            "build_id": build.get("build_id"),
            "phase": phase,
            "head": check.get("head"),
            "change_id": check.get("change_id"),
            "ok": check.get("ok") is True,
        }
        actual = {key: receipt.get(key) for key in expected}
        if actual != expected:
            return None
        fingerprint = event_fingerprint({"job_id": job_id, "receipt": actual})
        return {"fingerprint": fingerprint, "receipt": redact_secrets(receipt)}
    if job.get("kind") == "build_review":
        attempt = build_find_review_attempt(build, job_id)
        if not isinstance(attempt, dict) or attempt.get("status") != "terminal":
            return None
        expected = {
            "reviewer": attempt.get("reviewer"),
            "verdict": attempt.get("verdict"),
            "no_verdict": attempt.get("failure_class") == "no_verdict",
        }
        actual = {key: receipt.get(key) for key in expected}
        if job.get("build_id") != build.get("build_id") or actual != expected:
            return None
        fingerprint = event_fingerprint({"job_id": job_id, "receipt": actual})
        return {"fingerprint": fingerprint, "receipt": redact_secrets(receipt)}
    return None


def supervisor_reconcile(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    listener_id, listener_err = parse_supervisor_string(
        data, "listener_id", SUPERVISOR_LISTENER_ID_RE, "bad_listener_id"
    )
    if listener_err:
        return 400, listener_err
    assert listener_id is not None
    workflow_raw, wf_shape_err = parse_build_object(data, "source_workflow", "bad_source_workflow")
    if wf_shape_err:
        return 400, wf_shape_err
    if workflow_raw is None:
        return 400, {"error": "source_workflow is required", "code": "bad_source_workflow"}
    src_workflow_id, src_id_err = parse_supervisor_string(
        workflow_raw, "id", SUPERVISOR_SLUG_RE, "bad_source_workflow", label="source_workflow.id"
    )
    if src_id_err:
        return 400, src_id_err
    src_slug, slug_err = parse_supervisor_string(
        workflow_raw,
        "slug",
        SUPERVISOR_SLUG_RE,
        "bad_source_workflow",
        required=False,
        label="source_workflow.slug",
        blank_ok=True,
    )
    if slug_err:
        return 400, slug_err
    tags, tags_err = parse_supervisor_tags(workflow_raw)
    if tags_err:
        return 400, tags_err
    if src_slug == SUPERVISOR_LISTENER_SLUG or SUPERVISOR_LISTENER_SLUG in tags:
        # The listener cannot supervise itself. The trigger's own filter is the
        # first guard; this is the one that holds when someone edits it.
        return 409, {
            "ok": False,
            "error": "the completion supervisor cannot listen to itself",
            "code": "self_listener",
            "listener_id": listener_id,
        }
    run_raw, run_shape_err = parse_build_object(data, "source_run", "bad_source_run")
    if run_shape_err:
        return 400, run_shape_err
    if run_raw is None:
        return 400, {"error": "source_run is required", "code": "bad_source_run"}
    source_run_id, run_err = parse_supervisor_string(
        run_raw, "id", SUPERVISOR_RUN_ID_RE, "bad_source_run_id", label="source_run.id"
    )
    if run_err:
        return 400, run_err
    assert source_run_id is not None
    status = run_raw.get("status")
    if status not in SUPERVISOR_TERMINAL_STATUSES:
        return 400, {
            "error": "source_run.status must be a terminal status",
            "code": "bad_source_status",
            "allowed": list(SUPERVISOR_TERMINAL_STATUSES),
        }
    # Empty is live: the listener's trigger already filters to live runs, so a
    # template that rendered nothing here has not told us it was preview.
    execution_mode = run_raw.get("execution_mode") or run_raw.get("executionMode") or "live"
    if execution_mode not in SUPERVISOR_EXECUTION_MODES:
        return 400, {
            "error": "source_run.execution_mode must be live, preview, or demo",
            "code": "bad_execution_mode",
        }
    started_at, started_err = parse_supervisor_timestamp(run_raw, "started_at", "source_run.started_at")
    if started_err:
        return 400, started_err
    finished_at, finished_err = parse_supervisor_timestamp(run_raw, "finished_at", "source_run.finished_at")
    if finished_err:
        return 400, finished_err
    error_present, error_err = parse_supervisor_error_present(data)
    if error_err:
        return 400, error_err
    failure, failure_err = parse_supervisor_failure(data, str(status))
    if failure_err:
        return 400, failure_err
    assert failure is not None
    output, output_err = parse_supervisor_output(data)
    if output_err:
        return 400, output_err

    # Source run id plus the listener slug, with the published version left
    # out. The run id alone would dedupe unrelated listeners together; the full
    # id would make every republish a fresh delivery of work already done. The
    # version rides along on the report instead, where it costs nothing.
    listener_slug = supervisor_listener_slug(listener_id)
    delivery_key = f"{source_run_id}|{listener_slug}"
    fingerprint = event_fingerprint(
        {
            "listener_slug": listener_slug,
            "source_run_id": source_run_id,
            "status": status,
            "execution_mode": execution_mode,
            "source_workflow_id": src_workflow_id,
            "error_present": error_present,
            "failure": {k: failure[k] for k in ("class", "node", "code")},
            "output": output,
        }
    )

    with SUPERVISOR_LOCK:
        build_id, index_err = supervisor_run_lookup(source_run_id)
        if index_err:
            return int(index_err["status"]), index_err
        if build_id is None:
            # No launch record, so this run was never supervised. Answering
            # anything else would invent a build for a run nobody registered.
            return 404, {
                "ok": False,
                "error": "source run has no launch registration; it is not supervised",
                "code": "unknown_source_run",
                "source_run_id": source_run_id,
            }
        doc, read_err = supervisor_load(build_id)
        if read_err:
            return int(read_err["status"]), read_err
        if doc is None or doc.get("version") != SUPERVISOR_STATE_VERSION:
            return 409, {
                "ok": False,
                "error": "supervisor state for this build is missing or unreadable",
                "code": "supervisor_state_unreadable",
                "build_id": build_id,
                "source_run_id": source_run_id,
            }
        deliveries = doc.setdefault("deliveries", {})
        prior = deliveries.get(delivery_key)
        if isinstance(prior, dict):
            if prior.get("fingerprint") != fingerprint:
                return 409, {
                    "ok": False,
                    "error": "this delivery was already reconciled with different input",
                    "code": "idempotency_conflict",
                    "source_run_id": source_run_id,
                    "listener_id": listener_id,
                    "listener_slug": listener_slug,
                    "build_id": build_id,
                }
            replay = dict(prior.get("result") or {})
            if replay.get("action") == "watch":
                launch = (doc.get("runs") or {}).get(source_run_id)
                if not isinstance(launch, dict):
                    return 409, {
                        "ok": False,
                        "error": "launch registration is missing from supervisor state",
                        "code": "supervisor_state_unreadable",
                        "build_id": build_id,
                        "source_run_id": source_run_id,
                    }
                key, record, changed, watch_err = supervisor_ensure_watch_record(
                    doc, source_run_id, launch
                )
                if watch_err:
                    return 409, watch_err
                if changed:
                    save_err = supervisor_save(doc)
                    if save_err:
                        return int(save_err["status"]), save_err
                if record is not None and record.get("state") == "watching":
                    supervisor_schedule_watch(build_id, key)
            replay["replayed"] = True
            # The first delivery may have returned retry_event or relaunch and
            # started one bounded source invocation. Replaying that action
            # would launch a second one for the same completion, so a duplicate
            # is an explicit ignored terminal answer with no continuation.
            replay["action"] = "ignored"
            replay["resolution"] = "ignored"
            replay["continuation"] = None
            return int(prior.get("status") or 200), replay
        if len(deliveries) >= SUPERVISOR_DELIVERIES_MAX:
            return 409, {
                "ok": False,
                "error": "build has used its whole delivery history; open a new build",
                "code": "delivery_history_full",
                "build_id": build_id,
                "delivery_count": len(deliveries),
            }
        launch = (doc.get("runs") or {}).get(source_run_id)
        if not isinstance(launch, dict):
            return 409, {
                "ok": False,
                "error": "launch registration is missing from supervisor state",
                "code": "supervisor_state_unreadable",
                "build_id": build_id,
                "source_run_id": source_run_id,
            }
        if launch.get("build_id") != build_id:
            return 409, {
                "ok": False,
                "error": "launch registration names a different build",
                "code": "registration_conflict",
                "build_id": build_id,
                "source_run_id": source_run_id,
            }

        # Corroboration only. The declared output cannot repoint the build; a
        # disagreement is a park, because one of the two accounts is wrong and
        # advancing either one commits a real side effect on a guess.
        declared = output or {}
        disagreement = None
        if declared.get("build_id") not in (None, "", build_id):
            disagreement = "declared output names a different build"
        elif declared.get("event_id") not in (None, "", launch.get("event_id")):
            disagreement = "declared output names a different event"
        elif src_workflow_id != launch.get("workflow_id"):
            disagreement = "completion names a different source workflow"

        with BUILD_LOCK:
            build, build_read_err = load_build(build_id)
            if build is not None and build.get("version") != BUILD_STATE_VERSION:
                build, build_read_err = None, build_version_park(build)
            now = time.time()
            counters = supervisor_event_counters(doc, str(launch.get("event_id")))
            short_circuit = {
                "park_reason": None,
                "build_stage": (build or {}).get("stage"),
                "receipt": None,
                "workflow_fault": False,
                "circuit": None,
                "attempts": int(counters.get("attempts") or 0),
                "retry_budget": SUPERVISOR_RETRY_BUDGET[failure["class"]],
                "local_job_status": supervisor_local_job(launch.get("job_id"))[0],
            }
            if disagreement is not None:
                decision = {
                    **short_circuit,
                    "action": "park",
                    "park_reason": "state_trace_disagreement",
                    "workflow_fault": status == "completed",
                }
            elif execution_mode != "live":
                # Only live runs are supervised. The trigger already filters
                # to live; this keeps a hand-fired preview out of the ladder.
                decision = {**short_circuit, "action": "ignored"}
            else:
                decision = supervisor_decide(
                    doc,
                    launch,
                    build,
                    build_read_err,
                    str(status),
                    failure,
                    error_present,
                    source_run_id,
                    now,
                )
            park_reason = decision.get("park_reason")
            parked = decision["action"] in ("park", "circuit_open")
            # Park the build itself, so no other caller advances work this
            # supervisor has already declared unexplained. An already-parked
            # build keeps its original reason: overwriting it would erase why.
            if parked and build is not None and build.get("stage") != "parked" and park_reason:
                park_status, park_out = build_park(
                    build,
                    park_reason,
                    f"completion supervisor: {supervisor_report_line({'action': decision['action'], 'source_run_id': source_run_id})}",
                    f"supervisor:{source_run_id}",
                    fingerprint,
                    listener_id,
                )
                if park_status != 409:
                    # The park did not reach disk. Recording the delivery now
                    # would answer "parked" forever for a build that is not,
                    # so nothing is recorded and the outbox gets to redeliver.
                    return int(park_status), park_out
            if parked and build is not None and build.get("stage") == "parked":
                decision["build_stage"] = "parked"

        ctx = {
            "build_id": build_id,
            "event_id": launch.get("event_id"),
            "source_run_id": source_run_id,
            "workflow_id": launch.get("workflow_id"),
            "workflow_version": launch.get("workflow_version"),
            "expected_stage": launch.get("expected_stage"),
            "deadline": launch.get("deadline"),
            "job_id": launch.get("job_id"),
            "execution_mode": execution_mode,
            "source_status": status,
            "failed_node": failure["node"],
            "error_code": failure["code"],
            "attempts": decision.get("attempts"),
            "retry_budget": decision.get("retry_budget"),
            "local_job_status": decision.get("local_job_status") or "unclaimed",
            "build_stage": decision.get("build_stage"),
            "build_park_reason": (build or {}).get("park_reason") if build else None,
            "circuit_count": (decision.get("circuit") or {}).get("count"),
            "worktree": (build or {}).get("worktree") if build else None,
            "stacks": ", ".join((build or {}).get("stacks") or []) if build else "",
        }
        next_action = supervisor_next_action(decision["action"], park_reason, ctx)
        report = {
            "at": utcnow(),
            "source_run_id": source_run_id,
            # The full id carries the published listener version, which is
            # diagnostics: the slug beside it is what deduped the delivery.
            "listener_id": listener_id,
            "listener_slug": listener_slug,
            "build_id": build_id,
            "event_id": launch.get("event_id"),
            "workflow_id": launch.get("workflow_id"),
            "workflow_version": launch.get("workflow_version"),
            "source_status": status,
            "execution_mode": execution_mode,
            "error_present": error_present,
            "failure_class": failure["class"],
            "failed_node": failure["node"],
            "error_code": failure["code"],
            "build_stage": decision.get("build_stage"),
            "local_job_status": ctx["local_job_status"],
            "job_id": launch.get("job_id"),
            "action": decision["action"],
            "park_reason": park_reason,
            "workflow_fault": decision.get("workflow_fault", False),
            "attempts": decision.get("attempts"),
            "retry_budget": decision.get("retry_budget"),
            "circuit": decision.get("circuit"),
            "next_operator_action": next_action,
            "worktree": ctx["worktree"],
            "stacks": (build or {}).get("stacks") or [] if build else [],
            "files_intact": True,
            "plan_pane": launch.get("plan_pane"),
        }
        # Durable intent comes first. Herdr has no idempotency key, so recovery
        # is honestly at-least-once across a crash after send and before the
        # receipt write. It is never send-before-intent.
        report["delivery"] = (
            supervisor_delivery_intent(report, supervisor_report_line(report))
            if decision["action"] in SUPERVISOR_REPORTED_ACTIONS
            else plan_delivery_skipped(launch.get("plan_pane"))
        )
        resolution = {
            "receipt": "resolved",
            "watch": "watching",
            "relaunch": "retry",
            "retry_event": "retry",
            "ignored": "ignored",
            "park": "parked",
            "circuit_open": "parked",
        }[decision["action"]]
        result = {
            "ok": not parked,
            "version": SUPERVISOR_STATE_VERSION,
            "replayed": False,
            "source_run_id": source_run_id,
            "listener_id": listener_id,
            "listener_slug": listener_slug,
            "build_id": build_id,
            "event_id": launch.get("event_id"),
            "workflow_id": launch.get("workflow_id"),
            "workflow_version": launch.get("workflow_version"),
            "expected_stage": launch.get("expected_stage"),
            "source_status": status,
            "execution_mode": execution_mode,
            "action": decision["action"],
            "resolution": resolution,
            "parked": parked,
            "park_reason": park_reason,
            "park_replayed": bool(decision.get("park_replayed")),
            "workflow_fault": decision.get("workflow_fault", False),
            "build_stage": decision.get("build_stage"),
            "local_job_status": ctx["local_job_status"],
            "job_id": launch.get("job_id"),
            "attempts": decision.get("attempts"),
            "retry_budget": decision.get("retry_budget"),
            "circuit": decision.get("circuit"),
            "receipt": decision.get("receipt"),
            "report": report,
            "next_operator_action": next_action,
            "files_intact": True,
            "plan_pane": launch.get("plan_pane"),
            "deadline": launch.get("deadline"),
            "source_workflow_slug": SUPERVISOR_SOURCE_SLUG,
            # Everything the listener needs to start exactly one source
            # continuation, and nothing it could invent for itself. The build
            # id and event id are the registered ones, so a continuation runs
            # against the same durable claim rather than opening a new one.
            "continuation": (
                {
                    **(launch.get("continuation") or {}),
                    "workflow_slug": SUPERVISOR_SOURCE_SLUG,
                    "build_id": build_id,
                    "event_id": launch.get("event_id"),
                    "expected_stage": launch.get("expected_stage"),
                    "deadline": launch.get("deadline"),
                    "plan_pane": launch.get("plan_pane"),
                    "repo": (build or {}).get("repo") if build else None,
                    "reason": decision["action"],
                    "attempt": decision.get("attempts"),
                    "bounded": True,
                }
                if decision["action"] in SUPERVISOR_CONTINUATION_ACTIONS
                else None
            ),
            # The handoff to supervisorWatch. Present only when a live local
            # job still owns the event, because that is the only case where
            # there is something to wake up on.
            "watch": (
                {
                    "source_run_id": source_run_id,
                    "build_id": build_id,
                    "event_id": launch.get("event_id"),
                    "job_id": launch.get("job_id"),
                    "deadline": launch.get("deadline"),
                    "plan_pane": launch.get("plan_pane"),
                }
                if decision["action"] == "watch"
                else None
            ),
            "delivery": report["delivery"],
            "delivery_ok": bool(report["delivery"].get("ok")),
            "delivery_code": report["delivery"].get("code"),
        }
        if parked:
            result["error"] = next_action
            result["code"] = park_reason
        # Reported before the answer is returned, not after: a retry, a park,
        # and an open circuit are exactly the outcomes an operator has to see,
        # and the listener has no wait node to hold the run open for them.
        doc.setdefault("reports", []).append(report)
        deliveries[delivery_key] = {
            "fingerprint": fingerprint,
            "status": 200,
            "at": utcnow(),
            "result": result,
        }
        doc.setdefault("delivery_order", []).append(delivery_key)
        watch_schedule = None
        if decision["action"] == "watch":
            watch_key, _record, _changed, watch_err = supervisor_ensure_watch_record(
                doc, source_run_id, launch
            )
            if watch_err:
                return 409, watch_err
            watch_schedule = (build_id, watch_key)
        save_err = supervisor_save(doc)
        if save_err:
            return int(save_err["status"]), save_err
        pending_delivery_id = (
            str(report["delivery"].get("delivery_id") or "")
            if report["delivery"].get("state") == "pending"
            else ""
        )
    if watch_schedule is not None:
        supervisor_schedule_watch(*watch_schedule)
    if pending_delivery_id:
        delivered = supervisor_attempt_delivery(build_id, pending_delivery_id)
        if delivered is not None:
            result["report"]["delivery"] = delivered
            result["delivery"] = delivered
            result["delivery_ok"] = bool(delivered.get("ok"))
            result["delivery_code"] = delivered.get("code")
    return 200, result


# --- durable server-owned watch --------------------------------------------
#
# A watch is durable work owned by this process, not an expiring Rewst callback.
# The request carries one stable source-run id. The registered build, event,
# job, deadline, and exact plan pane are persisted before one daemon thread is
# scheduled. Startup and the first supervisor request recover watching records.


def supervisor_watch_key(source_run_id: str, event_id: str) -> str:
    return f"{source_run_id}|{event_id}"


def supervisor_ensure_watch_record(
    doc: dict[str, Any], source_run_id: str, launch: dict[str, Any]
) -> tuple[str, dict[str, Any] | None, bool, dict[str, Any] | None]:
    """Ensure the durable identity row for a registered launch.

    Callers hold SUPERVISOR_LOCK and include the mutation in their next
    supervisor_save. The launch record is authoritative; no request or Graph
    callback capability contributes fields to the watch.
    """
    build_id = str(doc.get("build_id") or "")
    event_id = str(launch.get("event_id") or "")
    job_id = str(launch.get("job_id") or "")
    deadline_epoch = launch.get("deadline_epoch")
    if not job_id:
        return "", None, False, {
            "ok": False,
            "error": "this launch never claimed a local job, so there is nothing to watch",
            "code": "no_local_job",
            "build_id": build_id,
            "source_run_id": source_run_id,
        }
    if not isinstance(deadline_epoch, (int, float)):
        return "", None, False, {
            "ok": False,
            "error": "launch registration has no usable deadline",
            "code": "bad_deadline",
            "build_id": build_id,
            "source_run_id": source_run_id,
        }

    key = supervisor_watch_key(source_run_id, event_id)
    watches = doc.setdefault("watches", {})
    record = watches.get(key)
    created = not isinstance(record, dict)
    if created:
        if len(watches) >= SUPERVISOR_WATCHES_MAX:
            return key, None, False, {
                "ok": False,
                "error": "build has used its whole watch history; open a new build",
                "code": "watch_history_full",
                "build_id": build_id,
            }
        record = {
            "watch_id": uuid.uuid4().hex,
            "state": "watching",
            "started_at": utcnow(),
            "finished_at": None,
            "action": None,
            "outcome": None,
            "park_reason": None,
            "job_status": None,
            "build_stage": None,
        }
        watches[key] = record
        doc.setdefault("watch_order", []).append(key)

    assert isinstance(record, dict)
    before = dict(record)
    if record.get("state") not in {"watching", "settled"}:
        record["state"] = "watching"
        record["finished_at"] = None
    record.update(
        {
            "watch_id": record.get("watch_id") or uuid.uuid4().hex,
            "source_run_id": source_run_id,
            "build_id": build_id,
            "event_id": event_id,
            "job_id": job_id,
            "deadline": launch.get("deadline"),
            "deadline_epoch": float(deadline_epoch),
            "plan_pane": launch.get("plan_pane"),
        }
    )
    return key, record, created or record != before, None


def supervisor_job_snapshot(job_id: str | None) -> tuple[str, dict[str, Any] | None]:
    """One stable read of the local job, under JOB_LOCK.

    The lock is what makes this a snapshot rather than a sample. `write_job`
    renames into place so a torn file is not the risk; the risk is reading
    between the two writes a job makes as it finishes and reporting a status
    that was never true for longer than an instant.
    """
    if not job_id:
        return "unclaimed", None
    with JOB_LOCK:
        job = read_job(job_id)
    if job is None:
        return "missing", None
    status = str(job.get("status") or "")
    if status in WATCH_ACTIVE:
        return "active", job
    return (status or "unknown"), job


def supervisor_event_receipt(build_id: str, event_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """The build's own terminal receipt for this event, if it has one yet."""
    with BUILD_LOCK:
        build, _err = load_build(build_id)
    if build is None or build.get("version") != BUILD_STATE_VERSION:
        return None, None
    entry = (build_event_seen(build) or {}).get(event_id)
    receipt = entry.get("receipt") if isinstance(entry, dict) and entry.get("fingerprint") else None
    return str(build.get("stage") or ""), receipt if isinstance(receipt, dict) else None


def supervisor_watch_line(record: dict[str, Any]) -> str:
    return (
        f"supervisor watch {record.get('outcome')} build={record.get('build_id')} "
        f"run={record.get('source_run_id')} event={record.get('event_id')} "
        f"job={record.get('job_id')}/{record.get('job_status')} "
        f"stage={record.get('build_stage')} next={record.get('next')}"
    )


def supervisor_watch_settle(
    build_id: str,
    event_id: str,
    job_id: str,
    deadline_epoch: float,
) -> dict[str, Any]:
    """Wait for the local job, then for its receipt, both bounded.

    The exit-between-read-and-return race is the reason there is a re-read at
    every boundary rather than one read per loop. A job that was `running` when
    the deadline arrived may have finished in that same instant, and reporting
    `deadline_exceeded` for a build that actually succeeded would park a green
    build. So the deadline branch reads once more before it decides, and the
    terminal branch keeps checking the build for the receipt the job's own exit
    does not write.
    """
    job_status, job = supervisor_job_snapshot(job_id)
    while job_status == "active":
        if time.time() >= deadline_epoch:
            # Last look. The job may have exited between the read above and
            # this comparison.
            job_status, job = supervisor_job_snapshot(job_id)
            break
        time.sleep(SUPERVISOR_WATCH_POLL_SECONDS)
        job_status, job = supervisor_job_snapshot(job_id)

    settle_until = min(time.time() + SUPERVISOR_WATCH_SETTLE_SECONDS, deadline_epoch)
    stage, receipt = supervisor_event_receipt(build_id, event_id)
    while receipt is None and job_status != "active" and time.time() < settle_until:
        time.sleep(SUPERVISOR_WATCH_POLL_SECONDS)
        stage, receipt = supervisor_event_receipt(build_id, event_id)

    if receipt is not None:
        outcome = "receipt"
    elif job_status == "active":
        outcome = "deadline_exceeded"
    elif job_status == "missing":
        outcome = "job_missing"
    else:
        outcome = "job_finished_without_receipt"
    return {
        "outcome": outcome,
        "job_status": job_status,
        "build_stage": stage,
        "build_receipt": receipt,
        "job": public_job(job) if isinstance(job, dict) else None,
    }


def supervisor_watch_worker(build_id: str, key: str) -> None:
    """Settle one persisted watch, parking before exact-pane notification."""
    retry_needed = False
    try:
        with SUPERVISOR_LOCK:
            doc, read_err = supervisor_load(build_id)
            record = (doc or {}).get("watches", {}).get(key)
            if read_err or not isinstance(record, dict) or record.get("state") != "watching":
                return
            assert doc is not None
            event_id = str(record.get("event_id") or "")
            job_id = str(record.get("job_id") or "")
            deadline_epoch = record.get("deadline_epoch")
            if not isinstance(deadline_epoch, (int, float)):
                deadline = str(record.get("deadline") or "")
                try:
                    deadline_epoch = datetime.strptime(deadline, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    ).timestamp()
                except ValueError:
                    deadline_epoch = 0.0
        settled = supervisor_watch_settle(build_id, event_id, job_id, float(deadline_epoch))

        # Re-read both sources at the settlement boundary. A job can exit and
        # its build receipt can land between the watch loop's last two reads.
        job_status, job = supervisor_job_snapshot(job_id)
        stage, receipt = supervisor_event_receipt(build_id, event_id)
        if receipt is not None:
            settled.update({"outcome": "receipt", "job_status": job_status, "job": job, "build_stage": stage, "build_receipt": receipt})

        with SUPERVISOR_LOCK:
            doc, read_err = supervisor_load(build_id)
            record = (doc or {}).get("watches", {}).get(key)
            if read_err or not isinstance(record, dict) or record.get("state") != "watching":
                return
            assert doc is not None
            action = "receipt" if settled.get("build_receipt") is not None else "park"
            park_reason = None
            if action == "park":
                park_reason = (
                    "deadline_exceeded"
                    if settled.get("outcome") == "deadline_exceeded" or time.time() >= float(deadline_epoch)
                    else "state_trace_disagreement"
                )
                with BUILD_LOCK:
                    build, build_err = load_build(build_id)
                    if build_err or build is None:
                        park_reason = "build_unreadable" if build_err else "missing_build"
                    elif build.get("stage") != "parked":
                        park_status, park_out = build_park(
                            build,
                            park_reason,
                            f"completion watch: job {job_id} ended {settled.get('job_status')} without event {event_id}",
                            f"supervisor-watch:{record.get('watch_id')}",
                            event_fingerprint({"watch_id": record.get("watch_id"), "reason": park_reason}),
                            str(record.get("source_run_id") or "supervisor-watch"),
                        )
                        if park_status != 409:
                            record["settlement_error"] = {"code": park_out.get("code"), "error": park_out.get("error")}
                            supervisor_save(doc)
                            retry_needed = True
                            return
                    if build is not None:
                        stage = str(build.get("stage") or stage or "")
            next_action = (
                "none: expected Graphwing build receipt recorded"
                if action == "receipt"
                else supervisor_next_action(
                    "park",
                    park_reason,
                    {
                        "build_id": build_id,
                        "event_id": event_id,
                        "source_run_id": record.get("source_run_id"),
                        "deadline": record.get("deadline"),
                        "build_stage": stage,
                        "worktree": "preserved",
                        "stacks": "preserved",
                    },
                )
            )
            record.update(
                {
                    "action": action,
                    "outcome": settled.get("outcome"),
                    "park_reason": park_reason,
                    "job_status": settled.get("job_status"),
                    "job_receipt": (job or {}).get("receipt") if isinstance(job, dict) else None,
                    "build_stage": stage,
                    "build_receipt": settled.get("build_receipt"),
                    "next_operator_action": next_action,
                    "finished_at": utcnow(),
                }
            )
            report = {
                "at": utcnow(),
                "source_run_id": record.get("source_run_id"),
                "build_id": build_id,
                "event_id": event_id,
                "job_id": job_id,
                "action": action,
                "outcome": settled.get("outcome"),
                "park_reason": park_reason,
                "job_status": settled.get("job_status"),
                "build_stage": stage,
                "watch_id": record.get("watch_id"),
                "next_operator_action": next_action,
                "plan_pane": record.get("plan_pane"),
                "files_intact": True,
            }
            report["delivery"] = (
                supervisor_delivery_intent(report, supervisor_watch_line(record))
                if action == "park"
                else plan_delivery_skipped(record.get("plan_pane"))
            )
            record["delivery"] = report["delivery"]
            record["state"] = "settled"
            doc.setdefault("reports", []).append(report)
            # Settlement, report, and pending delivery intent are one durable
            # write. Recovery retries only the delivery, never the transition.
            save_err = supervisor_save(doc)
            if save_err:
                retry_needed = True
                return
            delivery_id = str(report["delivery"].get("delivery_id") or "")
        if delivery_id:
            supervisor_attempt_delivery(build_id, delivery_id)
    except Exception as exc:
        retry_needed = True
        with SUPERVISOR_LOCK:
            doc, read_err = supervisor_load(build_id)
            record = (doc or {}).get("watches", {}).get(key)
            if not read_err and isinstance(record, dict) and record.get("state") == "watching":
                assert doc is not None
                record["worker_error"] = {"code": "watch_worker_failed", "error": str(exc)[:240]}
                supervisor_save(doc)
    finally:
        with SUPERVISOR_WATCHER_LOCK:
            SUPERVISOR_ACTIVE_WATCHERS.discard((build_id, key))
        if retry_needed:
            supervisor_schedule_watch_retry(build_id, key)


def supervisor_schedule_watch(build_id: str, key: str) -> bool:
    """Claim and schedule one in-process watcher for one durable record."""
    identity = (build_id, key)
    with SUPERVISOR_WATCHER_LOCK:
        if identity in SUPERVISOR_ACTIVE_WATCHERS:
            return False
        SUPERVISOR_ACTIVE_WATCHERS.add(identity)
    try:
        thread = threading.Thread(
            target=supervisor_watch_worker,
            args=(build_id, key),
            name=f"graphwing-watch-{hashlib.sha256(key.encode()).hexdigest()[:8]}",
            daemon=True,
        )
        thread.start()
    except Exception:
        with SUPERVISOR_WATCHER_LOCK:
            SUPERVISOR_ACTIVE_WATCHERS.discard(identity)
        raise
    return True


def supervisor_schedule_watch_retry(build_id: str, key: str) -> None:
    timer = threading.Timer(
        max(SUPERVISOR_WATCH_POLL_SECONDS, 0.01), supervisor_schedule_watch, args=(build_id, key)
    )
    timer.name = f"graphwing-watch-retry-{hashlib.sha256(key.encode()).hexdigest()[:8]}"
    timer.daemon = True
    timer.start()


def supervisor_recover_watches() -> int:
    """Schedule every durable open watch; the scheduler deduplicates repeats."""
    pending: list[tuple[str, str]] = []
    root = SUPERVISOR_DIR / "builds"
    if root.is_dir():
        with SUPERVISOR_LOCK:
            for path in root.glob("*/supervisor.json"):
                build_id = path.parent.name
                if not valid_build_id(build_id):
                    continue
                doc, read_err = supervisor_load(build_id)
                if read_err or doc is None:
                    continue
                for key, record in (doc.get("watches") or {}).items():
                    if isinstance(record, dict) and record.get("state") == "watching":
                        pending.append((build_id, str(key)))
    for build_id, key in pending:
        supervisor_schedule_watch(build_id, key)
    supervisor_recover_deliveries()
    return len(pending)


def supervisor_recover_once() -> int:
    """Recover at first supervisor access for each configured state root."""
    root = str(SUPERVISOR_DIR.resolve())
    with SUPERVISOR_RECOVERY_LOCK:
        if root in SUPERVISOR_RECOVERED_ROOTS:
            return 0
        SUPERVISOR_RECOVERED_ROOTS.add(root)
    return supervisor_recover_watches()


def supervisor_watch(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if set(data) != {"source_run_id"}:
        return 400, {
            "error": "watch accepts only source_run_id",
            "code": "bad_watch_request",
        }
    source_run_id, run_err = parse_supervisor_string(
        data, "source_run_id", SUPERVISOR_RUN_ID_RE, "bad_source_run_id"
    )
    if run_err:
        return 400, run_err
    assert source_run_id is not None
    with SUPERVISOR_LOCK:
        build_id, index_err = supervisor_run_lookup(source_run_id)
        if index_err:
            return int(index_err["status"]), index_err
        if build_id is None:
            return 404, {
                "ok": False,
                "error": "source run has no launch registration; it is not supervised",
                "code": "unknown_source_run",
                "source_run_id": source_run_id,
            }
        doc, read_err = supervisor_load(build_id)
        if read_err:
            return int(read_err["status"]), read_err
        if doc is None or doc.get("version") != SUPERVISOR_STATE_VERSION:
            return 409, {
                "ok": False,
                "error": "supervisor state for this build is missing or unreadable",
                "code": "supervisor_state_unreadable",
                "build_id": build_id,
                "source_run_id": source_run_id,
            }
        launch = (doc.get("runs") or {}).get(source_run_id)
        if not isinstance(launch, dict):
            return 409, {
                "ok": False,
                "error": "launch registration is missing from supervisor state",
                "code": "supervisor_state_unreadable",
                "build_id": build_id,
                "source_run_id": source_run_id,
            }
        # This is normally an acknowledgement for the row reconcile already
        # saved. Creating it remains supported for launches recorded by an
        # older server, before reconcile owned the durable handoff.
        expected_key = supervisor_watch_key(source_run_id, str(launch.get("event_id") or ""))
        was_present = isinstance((doc.get("watches") or {}).get(expected_key), dict)
        key, record, changed, watch_err = supervisor_ensure_watch_record(doc, source_run_id, launch)
        if watch_err:
            return 409, watch_err
        assert record is not None
        if changed:
            save_err = supervisor_save(doc)
            if save_err:
                return int(save_err["status"]), save_err
        out = {
            "ok": True,
            "started": bool(not was_present and record.get("state") == "watching"),
            "version": SUPERVISOR_STATE_VERSION,
            "watch_id": record.get("watch_id"),
            "build_id": build_id,
            "event_id": record.get("event_id"),
            "source_run_id": source_run_id,
            "job_id": record.get("job_id"),
            "state": record.get("state"),
            "outcome": record.get("outcome"),
            "action": record.get("action"),
            "park_reason": record.get("park_reason"),
            "deadline": record.get("deadline"),
            "plan_pane": record.get("plan_pane"),
            "settle_seconds": SUPERVISOR_WATCH_SETTLE_SECONDS,
            "poll": f"/v1/supervisor/report?build_id={build_id}",
        }
    if record.get("state") == "watching":
        supervisor_schedule_watch(build_id, key)
    return (202 if record.get("state") == "watching" else 200), out


# --- pre-PR visual evidence round -------------------------------------------
#
# One visual round is: preflight the contract, bring up the preview stack,
# drive the real served page, look at the images, have an independent reviewer
# look at them too, hand the bytes to Rewst under a short-lived grant, publish
# a GitHub issue comment, and — when the plan asked for it — prompt a person.
#
# Everything here fails closed toward "this is not proof". The three ways a
# screenshot round lies are all cheap to produce and all indistinguishable from
# success at the far end of a graph: a synthetic server standing in for the
# app, a DOM probe reported as a picture, and a summary graphic generated from
# a description of what the change was supposed to do. So the round never asks
# the capture whether it is real. It probes the URL itself, pins the preview
# stack's identity, refuses an observation that names another mode, and reads
# the PNG header of every file before any of it is called evidence.
#
# Nothing here holds a GitHub credential, an upload capability, or a browser
# session's cookies. Rewst owns the tracker integration; this side owns the
# bytes and hands them over under a grant that expires and is spent once.


def build_visual_contract(doc: dict[str, Any]) -> dict[str, Any] | None:
    """The proof contract this build was opened with, or None."""
    contract = (doc.get("verification") or {}).get("visual")
    return contract if isinstance(contract, dict) else None


def build_open_visual(build_id: Any) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Load a build for the visual round, or say precisely why not.

    Deliberately no class check. Visual proof is independent of routing class:
    a sensitive UI change still routes to sensitive models for its writing and
    its reviews, and still has to show a person what it looks like. Gating this
    on `class == visual` would let exactly the changes that most need a picture
    skip taking one.
    """
    doc, err = build_open(build_id)
    if err:
        return None, err
    assert doc is not None
    if not build_needs_visual(doc):
        return None, (
            409,
            {
                "ok": False,
                "build_id": doc.get("build_id"),
                "stage": doc.get("stage"),
                "step": "no_visual_proof",
                "error": "this build's plan did not declare needs_visual_proof",
                "code": "no_visual_proof",
            },
        )
    return doc, None


def build_visual_contract_gaps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything missing before a browser is allowed to take a picture.

    Read-only, and every entry names the field a planner edits. The contract is
    validated once at create; this re-checks it against the world the round is
    about to run in, because a stack can lose its role and a recipe can leave
    the catalog between planning and capture.
    """
    gaps: list[dict[str, Any]] = []
    contract = build_visual_contract(doc)
    if contract is None:
        gaps.append(
            {
                "check": "verification.visual",
                "code": "missing_contract",
                "detail": (
                    "this build declares needs_visual_proof but carries no verification.visual "
                    "contract; there is nothing that says which page, which viewports, what the "
                    "picture has to show, or whether a person reviews it"
                ),
            }
        )
        return gaps
    if "human_review" not in contract or not isinstance(contract.get("human_review"), bool):
        gaps.append(
            {
                "check": "verification.visual.human_review",
                "code": "missing_human_review",
                "detail": "the contract does not answer yes or no on human visual review",
            }
        )
    catalog = build_stack_catalog()
    name = str(contract.get("preview_stack") or "")
    entry = catalog.get(name)
    if entry is None:
        gaps.append(
            {
                "check": "verification.visual.preview_stack",
                "code": "unknown_stack",
                "detail": f"preview stack '{name}' is not in stacks.json",
                "allowed": sorted(catalog),
            }
        )
    else:
        if entry.get("role") != BUILD_PREVIEW_STACK_ROLE:
            gaps.append(
                {
                    "check": "verification.visual.preview_stack",
                    "code": "not_a_preview_stack",
                    "detail": (
                        f"stack '{name}' is declared role '{entry.get('role')}', not "
                        f"'{BUILD_PREVIEW_STACK_ROLE}'; a screenshot against a stack nobody "
                        "declared as the preview is a picture of whatever was left running"
                    ),
                }
            )
        if not entry.get("base_url"):
            gaps.append(
                {
                    "check": "verification.visual.preview_stack",
                    "code": "no_preview_url",
                    "detail": (
                        f"stack '{name}' declares no loopback base_url, so the round cannot name "
                        "the exact reachable URL a human would open"
                    ),
                }
            )
    recipe = str(contract.get("browser_recipe") or "")
    recipes = build_visual_recipe_catalog()
    spec = recipes.get(recipe)
    if spec is None:
        gaps.append(
            {
                "check": "verification.visual.browser_recipe",
                "code": "unknown_recipe",
                "detail": f"no such recipe: {recipe}",
                "allowed": sorted(recipes),
            }
        )
    elif spec.get("unresolvable"):
        gaps.append(
            {
                "check": "verification.visual.browser_recipe",
                "code": "browser_integrity_failed",
                "detail": str(spec["unresolvable"]),
            }
        )
    if contract.get("human_review") and not contract.get("planning_pane"):
        gaps.append(
            {
                "check": "verification.visual.planning_pane",
                "code": "missing_planning_pane",
                "detail": "human review is enabled but the contract names no planning pane to prompt",
            }
        )
    if build_budget_left(doc) is None:
        gaps.append(
            {
                "check": "budget",
                "code": "no_job_budget",
                "detail": "a visual build needs a finite budget.jobs_max; the feedback loop has no other floor",
            }
        )
    return gaps


def build_visual_rounds(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in (doc.get("evidence_rounds") or []) if isinstance(r, dict)]


def build_visual_round_current(doc: dict[str, Any]) -> dict[str, Any] | None:
    rounds = build_visual_rounds(doc)
    return rounds[-1] if rounds else None


def build_visual_round_captures(
    rnd: dict[str, Any], role: str | None = None, verified: bool | None = None
) -> list[dict[str, Any]]:
    out = [c for c in (rnd.get("captures") or []) if isinstance(c, dict)]
    if role is not None:
        out = [c for c in out if c.get("role") == role]
    if verified is not None:
        out = [c for c in out if bool(c.get("verified")) is verified]
    return out


def build_visual_open_round(doc: dict[str, Any], change_id: str) -> dict[str, Any] | None:
    """The round currently being captured for the change on disk, or None.

    A round whose change id no longer matches is stale by definition: the
    writer edited files after the pictures were taken, so the pictures are of
    something else. It is kept as history and superseded rather than reused.
    """
    rnd = build_visual_round_current(doc)
    if rnd is None or rnd.get("status") != "open":
        return None
    return rnd if rnd.get("change_id") == change_id else None


def build_visual_round_start(doc: dict[str, Any], live: dict[str, str]) -> dict[str, Any]:
    """Open a round, recording which earlier round it replaces.

    Supersession is a pointer, never a deletion. The comment history on the
    issue is the audit trail for a UI that changed three times before somebody
    approved it, and a round that quietly overwrote its predecessor would make
    the last picture look like the only one there had ever been.
    """
    rounds = build_visual_rounds(doc)
    prior = rounds[-1] if rounds else None
    entry = {
        "round": len(rounds) + 1,
        "opened_at": utcnow(),
        "head": live["head"],
        "served_commit_sha": live["head"],
        "change_id": live["change_id"],
        "writer_session": doc.get("writer_session"),
        "evidence_id": hashlib.sha256(
            f"{doc.get('build_id')}\0{len(rounds) + 1}\0{live['change_id']}".encode()
        ).hexdigest()[:32],
        "supersedes": prior.get("round") if isinstance(prior, dict) else None,
        "status": "open",
        "human_review": bool((build_visual_contract(doc) or {}).get("human_review")),
        "preview": None,
        "captures": [],
        "observations": {},
        "inspection": None,
        "review": None,
        "handoff": None,
        "published": None,
        "planning_prompt": None,
        "prompted_at": None,
        "recapture_attempts": {},
        "inspection_queue": [],
    }
    doc["evidence_rounds"] = rounds + [entry]
    return entry


def build_visual_required_roles(rnd: dict[str, Any]) -> list[str]:
    """Which roles this round has to carry.

    The first round needs a before as well as a candidate: a candidate image
    with nothing to compare it against shows what the UI looks like, not what
    the change did to it. Later rounds compare against the round they supersede,
    which is already published and cannot be edited.
    """
    return ["before", "candidate"] if int(rnd.get("round") or 1) == 1 else ["candidate"]


def build_visual_missing_roles(rnd: dict[str, Any]) -> list[str]:
    return [
        role
        for role in build_visual_required_roles(rnd)
        if not build_visual_round_captures(rnd, role=role)
    ]


def build_visual_recapture_queue(
    failures: list[dict[str, Any]], attempts: dict[str, Any] | Any
) -> list[dict[str, Any]]:
    """Group failed viewports into one bounded recapture per role."""
    spent = attempts if isinstance(attempts, dict) else {}
    grouped: dict[str, set[str]] = {role: set() for role in BUILD_VISUAL_ROLES}
    for failure in failures:
        role = str(failure.get("role") or "")
        viewport = str(failure.get("viewport") or "")
        if role in grouped and viewport and int(spent.get(role) or 0) < BUILD_VISUAL_RECAPTURE_MAX:
            grouped[role].add(viewport)
    return [
        {"role": role, "only": sorted(grouped[role])}
        for role in BUILD_VISUAL_ROLES
        if grouped[role]
    ]


def build_visual_recapture_target(rnd: dict[str, Any], role: str) -> list[str] | None:
    attempts = rnd.get("recapture_attempts")
    spent = attempts if isinstance(attempts, dict) else {}
    if int(spent.get(role) or 0) >= BUILD_VISUAL_RECAPTURE_MAX:
        return None
    for item in rnd.get("inspection_queue") or []:
        if isinstance(item, dict) and item.get("role") == role:
            only = item.get("only")
            if isinstance(only, list) and all(isinstance(v, str) for v in only):
                return only
    return None


def build_visual_step(doc: dict[str, Any], change_id: str) -> tuple[str, str]:
    """Name the one step this invocation may run, and why.

    Same contract as the mechanical stage switch and for the same reason: an
    invocation that died after its browser finished must come back and be told
    `inspect`, not run a second capture over the images it already has.
    """
    stage = doc.get("stage")
    if stage == "parked":
        return "parked", str(doc.get("park_reason") or "parked")
    if doc.get("pr_ready") or stage in ("ready", "pr_opened"):
        return "done", "this build is past the evidence stage"
    if build_budget_left(doc) == 0:
        return "parked", "budget_exhausted"
    if stage != "evidence":
        return "parked", f"the visual round runs at stage 'evidence'; this build is at '{stage}'"
    gaps = build_visual_contract_gaps(doc)
    if gaps:
        return "preflight", str(gaps[0]["detail"])

    rnd = build_visual_round_current(doc)
    if rnd is not None and rnd.get("status") == "published" and rnd.get("change_id") == change_id:
        if rnd.get("human_review") and not rnd.get("prompted_at"):
            return "human_review", "the published round still owes the planning session its prompt"
        return "done", f"round {rnd.get('round')} is published for this change"
    open_round = build_visual_open_round(doc, change_id)
    if open_round is None:
        why = "no round is open for the change on disk"
        if rnd is not None and rnd.get("status") == "open":
            why = (
                f"round {rnd.get('round')} was captured at another change; a new round supersedes it"
            )
        return "preflight", why
    if not isinstance(open_round.get("preview"), dict):
        return "preview", "the preview stack has not been resolved for this round"
    missing = build_visual_missing_roles(open_round)
    if missing:
        return "scenario", f"this round still needs a {missing[0]} capture"
    queue = open_round.get("inspection_queue")
    if isinstance(queue, list) and queue:
        roles = [str(item.get("role")) for item in queue if isinstance(item, dict)]
        return "scenario", "inspection requires targeted recapture for " + ", ".join(roles)
    inspection = open_round.get("inspection")
    if not isinstance(inspection, dict) or inspection.get("change_id") != change_id:
        return "inspect", "the captured images have not been inspected for this change"
    if inspection.get("recapture"):
        return "parked", (
            "targeted recapture budget exhausted for: "
            + ", ".join(str(n) for n in inspection["recapture"][:4])
        )
    if not inspection.get("ok"):
        return "inspect", str(inspection.get("error") or "image inspection is not clean")
    if not isinstance(open_round.get("review"), dict):
        return "review", "no automated UI review has been recorded for this round"
    if (open_round.get("review") or {}).get("verdict") != "PASS":
        return "review", "the automated UI review did not return PASS"
    if not isinstance(open_round.get("handoff"), dict):
        return "handoff", "the verified images have not been handed off for upload"
    return "publish", "the round is ready to publish its evidence comment"


def build_visual_next(qs: dict[str, list[str]], repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """The visual stage switch. Read durable state, name the next allowed step.

    Read-only, like buildNext: the graph calls this first on every invocation,
    so it has to be safe to call from a run that is about to be abandoned.
    """
    doc, err = build_open_visual(first_query(qs, "build_id"))
    if err:
        return err
    assert doc is not None
    live, park_detail = build_live_change(doc, repos)
    if live is None:
        out = build_public(doc)
        out["ok"] = False
        out["step"] = "parked"
        out["step_reason"] = str(park_detail)
        out["code"] = "worktree_mismatch"
        return 409, out
    step, reason = build_visual_step(doc, live["change_id"])
    rnd = build_visual_round_current(doc)
    out = build_public(doc)
    out["ok"] = step != "parked"
    out["step"] = step
    out["step_reason"] = reason
    out["live_head"] = live["head"]
    out["live_branch"] = live["branch"]
    out["change_id"] = live["change_id"]
    out["class"] = build_class(doc)
    out["visual"] = build_visual_contract(doc)
    out["round"] = rnd.get("round") if isinstance(rnd, dict) else None
    out["round_status"] = rnd.get("status") if isinstance(rnd, dict) else None
    if step == "parked":
        out["code"] = doc.get("park_reason") or "parked"
    return (200 if out["ok"] else 409), out


def build_visual_preflight(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Everything that must be true before a browser takes a picture.

    Read-only on purpose, exactly like the mechanical preflight: a stack that
    lost its role or a recipe that left the catalog is something the operator
    fixes and retries, not something that burns the build.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    doc, open_err = build_open_visual(data.get("build_id"))
    if open_err:
        return open_err
    assert doc is not None
    stage = doc.get("stage")
    if stage != "evidence":
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "stage": stage,
            "error": f"the visual round runs at stage 'evidence'; this build is at '{stage}'",
            "code": "invalid_transition",
        }
    failures = build_visual_contract_gaps(doc)
    live, park_detail = build_live_change(doc, repos)
    if live is None:
        failures.append({"check": "worktree", "code": "worktree_mismatch", "detail": str(park_detail)})
    contract = build_visual_contract(doc) or {}
    out = {
        "ok": not failures,
        "build_id": doc.get("build_id"),
        "stage": stage,
        "class": build_class(doc),
        "change_id": live["change_id"] if live else None,
        "head": live["head"] if live else doc.get("head"),
        "human_review": contract.get("human_review"),
        "contract": contract or None,
        "failures": failures,
        "rounds": len(build_visual_rounds(doc)),
        "budget_left": build_budget_left(doc),
    }
    if failures:
        out["error"] = failures[0]["detail"]
        out["code"] = failures[0]["code"]
        return 409, out
    return 200, out


def build_visual_event(
    data: dict[str, Any], action: str, extra: dict[str, Any], repos: dict[str, str]
) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Open one idempotent visual transition, or hand back the refusal.

    Every state-changing visual op needs the same four things established before
    it does anything: the build exists and wants visual proof, this event id has
    not already run (and is not being reused for different input), no other
    caller holds the lease, and the worktree is still the one the build was
    opened on. Writing that out per op is how one of them ends up missing the
    replay check and captures a second round for a retried invocation.

    Returns a context on success. The caller finishes with build_visual_commit.
    """
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return None, (400, {"ok": False, "error": "event_id is required", "code": "bad_event_id"})
    holder = data.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        return None, (400, {"ok": False, "error": "holder is required", "code": "missing_holder"})
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return None, (400, holder_err)
    fingerprint = event_fingerprint(
        {"build_id": data.get("build_id"), "action": action, "holder": holder, **extra}
    )

    doc, open_err = build_open_visual(data.get("build_id"))
    if open_err:
        return None, open_err
    assert doc is not None

    seen = build_event_seen(doc)
    if event_id in seen:
        record = seen.get(event_id) or {}
        out = build_public(doc)
        out["replayed"] = record.get("fingerprint") == fingerprint
        if not out["replayed"]:
            out["ok"] = False
            out["error"] = "event_id was already used with different input"
            out["code"] = "idempotency_conflict"
            return None, (409, out)
        out["ok"] = int(record.get("status") or 200) == 200
        out["receipt"] = record.get("receipt")
        if action == "visual_handoff" and out["ok"]:
            receipt = record.get("receipt") or {}
            grants, grant_code = artifact_grant_replay_state(receipt.get("artifacts") or [])
            if grant_code:
                out["ok"] = False
                out["code"] = grant_code
                out["error"] = "this round's issued upload grant is no longer outstanding"
                return None, (409, out)
            out["grants"] = grants
        return None, (int(record.get("status") or 200), out)

    live, park = build_identity_park(doc, action, data.get("worktree"), None, None, repos)
    if park is not None:
        reason, detail = park
        return None, build_park(doc, reason, detail, event_id, fingerprint, holder)
    assert live is not None
    change_id = build_change_id(Path(str(doc.get("worktree"))), live["head"])
    # Round creation consumes the same live identity object.  Keep the derived
    # candidate identity beside head/branch so preview can open a round without
    # trusting a caller-supplied change id.
    live = {**live, "change_id": change_id}

    lease = build_lease_live(doc, time.time())
    if lease is not None and lease.get("holder") != holder:
        out = build_public(doc)
        out["ok"] = False
        out["error"] = "another caller holds the build lease"
        out["code"] = "lease_held"
        return None, (409, out)

    if doc.get("stage") != "evidence":
        out = build_public(doc)
        out["ok"] = False
        out["error"] = f"the visual round runs at stage 'evidence'; this build is at '{doc.get('stage')}'"
        out["code"] = "invalid_transition"
        return None, (409, out)

    return {
        "doc": doc,
        "event_id": event_id,
        "holder": holder,
        "fingerprint": fingerprint,
        "action": action,
        "live": live,
        "change_id": change_id,
    }, None


def build_visual_commit(
    ctx: dict[str, Any], receipt: dict[str, Any], status: int, out: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Record the event and persist, so a retry replays instead of repeating."""
    doc = ctx["doc"]
    receipt = {
        "event_id": ctx["event_id"],
        "action": ctx["action"],
        "at": utcnow(),
        "holder": ctx["holder"],
        "build_id": doc.get("build_id"),
        "head": ctx["live"]["head"],
        "change_id": ctx["change_id"],
        "budget_left": build_budget_left(doc),
        **receipt,
    }
    build_record_event(doc, ctx["event_id"], ctx["fingerprint"], receipt, status)
    write_err = write_build(doc, doc.get("build_id"))
    if write_err:
        return int(write_err["status"]), write_err
    payload = build_public(doc)
    payload.update(out)
    payload["replayed"] = False
    payload["receipt"] = receipt
    payload["change_id"] = ctx["change_id"]
    return status, payload


def build_preview_identity(doc: dict[str, Any], stack: str) -> str:
    """A preview identity that survives a restart and never collides.

    Derived, not generated. The same build and the same stack must resolve to
    the same preview across every round and every service restart, or round 4's
    screenshots would claim to come from a different environment than round 1's
    and the comparison the reviewer is being asked to make would be meaningless.
    """
    digest = hashlib.sha256(f"{doc.get('build_id')}\0{stack}".encode()).hexdigest()[:16]
    return f"gwpreview-{digest}"


def build_preview_stack(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The preview stack this round drives, or why there is none."""
    contract = build_visual_contract(doc)
    if contract is None:
        return None, "this build carries no verification.visual contract"
    name = str(contract.get("preview_stack") or "")
    catalog = build_stack_catalog()
    entry = catalog.get(name)
    if entry is None:
        return None, f"preview stack '{name}' is not in stacks.json"
    role = str(entry.get("role") or "unset")
    if role != BUILD_PREVIEW_STACK_ROLE:
        return None, (
            f"stack '{name}' is declared role '{role}', not '{BUILD_PREVIEW_STACK_ROLE}'; a "
            "screenshot against an undeclared stack is a picture of whatever was left running"
        )
    if not entry.get("base_url"):
        return None, f"stack '{name}' declares no loopback base_url to open"
    return entry, None


def stack_ensure_started(entry: dict[str, Any]) -> dict[str, Any]:
    """Idempotently start the catalogued stack; never stop it after a round."""
    current = stack_status(str(entry.get("name") or ""))
    if current.get("healthy"):
        return {"ok": True, "resumed": True}
    cwd = entry.get("cwd")
    if not isinstance(cwd, Path) or not cwd.is_dir():
        return {"ok": False, "resumed": False, "error": "preview stack has no runnable cwd"}
    args = ["podman", "compose"]
    compose = str(entry.get("compose_file") or "")
    if compose:
        args += ["-f", compose]
    args += ["up", "-d"]
    result = run_cmd(args, cwd=cwd, timeout=120, env=podman_env())
    return {"ok": bool(result.get("ok")), "resumed": False,
            "error": None if result.get("ok") else str(result.get("error") or "compose up failed")}


def build_visual_preview(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Start or resume the preview stack and report the exact reachable URL.

    Never tears anything down, and never brings up a stack the catalog has not
    declared as a preview. The identity is derived from the build, so resuming
    is indistinguishable from starting for every later step — which is what
    makes an interrupted round resumable rather than restartable.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(data, "visual_preview", {}, repos)
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        gaps = build_visual_contract_gaps(doc)
        if gaps:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = str(gaps[0]["detail"])
            out["code"] = str(gaps[0]["code"])
            out["failures"] = gaps
            return 409, out
        entry, gap = build_preview_stack(doc)
        if entry is None:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = str(gap)
            out["code"] = "not_a_preview_stack"
            return 409, out
        contract = build_visual_contract(doc) or {}
        started = stack_ensure_started(entry)
        if not started.get("ok"):
            out = build_public(doc)
            out.update({"ok": False, "code": "preview_start_failed", "error": started.get("error")})
            return 409, out
        status = stack_status(entry["name"])
        url = f"{entry['base_url']}{contract['route']}"
        probe = probe_loopback(url, allow_login=bool(contract.get("allow_login_redirect")))
        healthy = bool(status.get("healthy")) and bool(probe.get("ok"))
        rnd = build_visual_open_round(doc, ctx["change_id"])
        if rnd is None:
            current = build_visual_round_current(doc)
            if (
                isinstance(current, dict)
                and current.get("status") == "published"
                and current.get("change_id") == ctx["change_id"]
            ):
                # The change on disk already has a published round. Opening a
                # second one would post the same pictures under a new number
                # and make the issue history claim an iteration that never
                # happened; a real next round begins when the writer edits.
                out = build_public(doc)
                out["ok"] = False
                out["round"] = current.get("round")
                out["error"] = (
                    f"round {current.get('round')} is already published for this change"
                )
                out["code"] = "round_already_published"
                return 409, out
            rnd = build_visual_round_start(doc, ctx["live"])
        preview = {
            "preview_id": build_preview_identity(doc, entry["name"]),
            "stack": entry["name"],
            "role": entry.get("role"),
            "base_url": entry["base_url"],
            "url": url,
            "port": urlsplit(url).port,
            "served_commit_sha": ctx["live"]["head"],
            "route": contract["route"],
            "healthy": healthy,
            "http_status": probe.get("status"),
            "final_location": probe.get("final_location"),
            "login_redirect": probe.get("login_redirect"),
            "resumed": bool(started.get("resumed")),
            "at": utcnow(),
            "setup": contract.get("setup") or [],
            "navigation": contract.get("navigation") or [],
            "tenant": contract.get("tenant"),
        }
        if not healthy:
            # No capture is attempted against an unreachable page. A browser
            # pointed at a dead port still produces a PNG, and that PNG is an
            # error card that would sail through every later check.
            rnd["preview"] = None
            out = build_public(doc)
            out["ok"] = False
            out["preview"] = preview
            out["error"] = (
                f"preview stack '{entry['name']}' did not serve {url}: "
                + str(probe.get("error") or f"HTTP {probe.get('status')}")
            )
            out["code"] = "preview_unhealthy"
            write_err = write_build(doc, doc.get("build_id"))
            if write_err:
                return int(write_err["status"]), write_err
            return 409, out
        rnd["preview"] = preview
        return build_visual_commit(
            ctx,
            {"preview_id": preview["preview_id"], "url": url, "round": rnd["round"]},
            200,
            {
                "ok": True,
                "preview": preview,
                "round": rnd["round"],
                "supersedes": rnd.get("supersedes"),
                "instructions": {
                    "url": url,
                    "setup": preview["setup"],
                    "navigation": preview["navigation"],
                    "expected": contract.get("expected") or [],
                    "checklist": contract.get("checklist") or [],
                    "tenant": contract.get("tenant"),
                },
            },
        )


def png_geometry(data: bytes) -> tuple[int, int] | None:
    """Width and height straight out of the IHDR chunk, or None.

    No image library, on purpose: this runs on the seat and the question is
    small. A file that is not a PNG, or is truncated before its header, has no
    geometry — and a capture with no geometry is not a screenshot.
    """
    if len(data) < 24 or not data.startswith(BUILD_PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def build_visual_round_dir(build_id: str, round_no: int) -> Path:
    return build_dir(build_id) / "evidence" / f"round-{round_no:03d}"


def build_visual_browser_argv(
    spec: dict[str, Any], contract: dict[str, Any], preview: dict[str, Any],
    role: str, out_dir: Path, viewports: list[dict[str, Any]]
) -> list[str]:
    """The exact arguments the declared browser recipe is run with.

    Composed here rather than baked into the catalog entry so the recipe cannot
    choose its own target. The URL, the route, the tenant and the output
    directory are the round's, and the recipe's only job is to drive them.
    """
    argv = list(spec["argv"]) + [
        "--url", str(preview["url"]),
        "--route", str(contract["route"]),
        "--scenario", str(contract["scenario"]),
        "--role", role,
        "--tenant", str(contract["tenant"]),
        "--preview-id", str(preview["preview_id"]),
        "--out", str(out_dir),
    ]
    for viewport in viewports:
        argv += ["--viewport", f"{viewport['name']}:{viewport['width']}x{viewport['height']}"]
    return argv


def build_visual_observation_gaps(
    obs: Any, contract: dict[str, Any], preview: dict[str, Any]
) -> list[str]:
    """Why this observation is not evidence from the real served page.

    The three refusals here are the whole point of the ticket. A synthetic
    server, a DOM-only probe, and a generated summary graphic all produce a
    tidy artifact that looks exactly like a passing round from the graph's side,
    and each one is cheaper to produce than the real thing — so each one is
    named and refused rather than inferred from a missing field.
    """
    if not isinstance(obs, dict):
        return ["the browser recipe wrote no readable observation document"]
    gaps: list[str] = []
    mode = obs.get("mode")
    if mode != "browser":
        gaps.append(
            f"observation declares mode '{mode}', not 'browser'; a DOM-only probe is not a picture "
            "of what a person would see"
        )
    if obs.get("synthetic") is not False:
        gaps.append(
            "observation does not declare synthetic=false; a synthetic server standing in for the "
            "app proves the fixture works, not the change"
        )
    if str(obs.get("url") or "") != str(preview.get("url")):
        gaps.append(
            f"observation was taken at '{obs.get('url')}', not the preview URL '{preview.get('url')}'"
        )
    if str(obs.get("served_by") or "") != str(preview.get("preview_id")):
        gaps.append("observation does not name this round's preview identity as its server")
    if str(obs.get("route") or "") != str(contract.get("route")):
        gaps.append("observation route does not match the route Graphwing assigned")
    if str(obs.get("tenant") or "") != str(contract.get("tenant")):
        gaps.append("observation tenant does not match the tenant Graphwing assigned")
    status = obs.get("http_status")
    authoritative_status = preview.get("http_status")
    if status != authoritative_status:
        gaps.append("observation HTTP status contradicts Graphwing's probe")
    if "final_location" in obs and str(obs.get("final_location") or "") != str(
        preview.get("final_location") or ""
    ):
        gaps.append("observation final location contradicts Graphwing's no-follow probe")
    accepted_redirect = bool(preview.get("login_redirect"))
    if not isinstance(authoritative_status, int) or not (
        200 <= authoritative_status < 300 or accepted_redirect
    ):
        gaps.append(f"the served page answered HTTP {authoritative_status}, so nothing below it is proof")
    for field in ("accessibility", "console", "requests"):
        section = obs.get(field)
        if not isinstance(section, dict) or not isinstance(section.get("ok"), bool):
            gaps.append(f"observation records no {field} result for this scenario")
    visible = obs.get("visible")
    if not isinstance(visible, list) or not all(isinstance(v, str) for v in visible):
        gaps.append("observation records no visible state for this scenario")
    images = obs.get("images")
    if not isinstance(images, list) or not images:
        gaps.append("observation captured no images")
        return gaps
    if len(images) > BUILD_VISUAL_IMAGES_MAX:
        gaps.append("observation captured more images than a round may carry")
    names = {str(v["name"]) for v in contract.get("viewports") or []}
    for image in images:
        if not isinstance(image, dict):
            gaps.append("an image entry is not an object")
            continue
        if image.get("source") != "browser":
            gaps.append(
                f"image '{image.get('file')}' declares source '{image.get('source')}'; a generated "
                "summary graphic is not primary evidence"
            )
        if image.get("viewport") not in names:
            gaps.append(f"image '{image.get('file')}' claims a viewport the contract never declared")
    return gaps


def build_visual_capture_images(
    obs: dict[str, Any], contract: dict[str, Any], out_dir: Path, role: str, change_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every declared image off disk and describe it, or say why not.

    The recipe's word for what it captured is never taken. The bytes are read
    here, the PNG header is parsed here, and an entry whose file is missing,
    truncated, not a PNG, or smaller than the viewport it claims is a gap — not
    a capture with a caveat.
    """
    viewports = {str(v["name"]): v for v in contract.get("viewports") or []}
    captures: list[dict[str, Any]] = []
    gaps: list[str] = []
    for image in obs.get("images") or []:
        if not isinstance(image, dict):
            continue
        name = str(image.get("file") or "")
        if not name or "/" in name or "\\" in name or ".." in name:
            gaps.append(f"image file name '{name}' is not a plain file in the round directory")
            continue
        path = out_dir / name
        try:
            data = read_regular_file(out_dir, name)
        except OSError:
            gaps.append(f"image '{name}' was declared but not written")
            continue
        if len(data) < BUILD_IMAGE_MIN_BYTES:
            gaps.append(f"image '{name}' is {len(data)} bytes, which is a blank or an error card")
            continue
        if len(data) > BUILD_IMAGE_MAX_BYTES:
            gaps.append(f"image '{name}' is larger than a round may carry")
            continue
        geometry = png_geometry(data)
        if geometry is None:
            gaps.append(f"image '{name}' is not a PNG this service can read")
            continue
        width, height = geometry
        viewport = viewports.get(str(image.get("viewport")))
        if viewport is None:
            gaps.append(f"image '{name}' claims a viewport the contract never declared")
            continue
        if width < int(viewport["width"]) or height < BUILD_IMAGE_MIN_HEIGHT:
            gaps.append(
                f"image '{name}' is {width}x{height}, smaller than the {viewport['name']} viewport "
                f"it claims ({viewport['width']}x{viewport['height']})"
            )
            continue
        captures.append(
            {
                "role": role,
                "file": name,
                "path": str(path),
                "viewport": viewport["name"],
                "width": width,
                "height": height,
                "bytes": len(data),
                "digest": hashlib.sha256(data).hexdigest(),
                "label": str(image.get("label") or "")[:BUILD_REDACT_MAX_STRING],
                "route": str(image.get("route") or ""),
                "tenant": str(image.get("tenant") or ""),
                "obscured": image.get("obscured"),
                "text": [str(t)[:BUILD_NAME_MAX] for t in (image.get("text") or [])][:BUILD_REDACT_MAX_ITEMS],
                "source": image.get("source"),
                "change_id": change_id,
                "at": utcnow(),
                "verified": None,
                "reasons": [],
            }
        )
    return captures, gaps


def build_visual_scenario_result(
    ctx: dict[str, Any], role: str, only: list[str] | None, repos: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    """Drive the declared scenario on the real served page and record it."""
    doc = ctx["doc"]
    contract = build_visual_contract(doc)
    if contract is None:
        return 409, {"ok": False, "error": "no verification.visual contract", "code": "missing_contract"}
    rnd = build_visual_open_round(doc, ctx["change_id"])
    if rnd is None:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "error": "no round is open for the change on disk; run buildPreviewStack first",
            "code": "no_open_round",
        }
    if role not in build_visual_required_roles(rnd):
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "round": rnd.get("round"),
            "role": role,
            "error": (
                f"round {rnd.get('round')} uses its prior published round as before history; "
                f"it must capture candidate only"
            ),
            "code": "role_not_required",
        }
    preview = rnd.get("preview")
    if not isinstance(preview, dict):
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "round": rnd.get("round"),
            "error": "this round has no resolved preview stack",
            "code": "no_preview",
        }
    viewports = [v for v in contract.get("viewports") or []]
    if only:
        viewports = [v for v in viewports if v["name"] in only]
        if not viewports:
            return 400, {
                "ok": False,
                "error": "only names no viewport this contract declares",
                "code": "unknown_viewport",
                "allowed": [v["name"] for v in contract.get("viewports") or []],
            }
    inspection = rnd.get("inspection")
    requested_only = build_visual_recapture_target(rnd, role)
    if isinstance(inspection, dict) and (inspection.get("recapture") or rnd.get("inspection_queue")):
        requested = {f"{role}/{name}" for name in (requested_only or [])}
        actual = {f"{role}/{v['name']}" for v in viewports}
        if not requested or actual != requested:
            return 409, {
                "ok": False,
                "code": "recapture_target_mismatch",
                "error": "recapture must name exactly the failed role and viewport set",
                "requested": sorted(requested),
            }
        attempts = rnd.get("recapture_attempts")
        if not isinstance(attempts, dict):
            attempts = {}
            rnd["recapture_attempts"] = attempts
        if int(attempts.get(role) or 0) >= BUILD_VISUAL_RECAPTURE_MAX:
            return 409, {
                "ok": False,
                "code": "recapture_exhausted",
                "error": "this round has exhausted its targeted recapture budget",
            }
        attempts[role] = int(attempts.get(role) or 0) + 1
    recipes = build_visual_recipe_catalog()
    spec = recipes.get(str(contract.get("browser_recipe")))
    if spec is None:
        return 409, {
            "ok": False,
            "error": f"no such recipe: {contract.get('browser_recipe')}",
            "code": "unknown_recipe",
        }
    if spec.get("unresolvable"):
        return 409, {"ok": False, "error": str(spec["unresolvable"]), "code": "browser_integrity_failed"}

    # Probed here as well as at preview time. The stack was up when the round
    # opened; this is the moment before the picture is taken, and a page that
    # died in between would still yield a PNG.
    probe = probe_loopback(
        str(preview.get("url")), allow_login=bool(contract.get("allow_login_redirect"))
    )
    status = stack_status(str(preview.get("stack") or ""))
    if not status.get("healthy") or not probe.get("ok"):
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "round": rnd.get("round"),
            "url": preview.get("url"),
            "error": f"the preview no longer serves {preview.get('url')}: {probe.get('error') or probe.get('status')}",
            "code": "preview_unhealthy",
        }
    preview["healthy"] = True
    preview["http_status"] = probe.get("status")
    preview["probed_url"] = probe.get("url")
    preview["final_location"] = probe.get("final_location")
    preview["login_redirect"] = probe.get("login_redirect")
    preview["probed_at"] = utcnow()

    park = build_charge(doc, 1)
    if park:
        return park

    out_dir = build_visual_round_dir(str(doc.get("build_id")), int(rnd["round"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    argv = build_visual_browser_argv(spec, contract, preview, role, out_dir, viewports)
    result = run_cmd(argv, cwd=spec["cwd"], timeout=int(spec["timeout_seconds"]))
    obs_path = out_dir / f"observation-{role}.json"
    try:
        observation = json.loads(obs_path.read_text())
    except (OSError, ValueError):
        observation = None

    gaps = build_visual_observation_gaps(observation, contract, preview)
    captures: list[dict[str, Any]] = []
    if not gaps:
        assert isinstance(observation, dict)
        captures, image_gaps = build_visual_capture_images(
            observation, contract, out_dir, role, ctx["change_id"]
        )
        for capture in captures:
            capture.update({
                "served_commit_sha": rnd.get("served_commit_sha"),
                "preview_url": preview.get("url"),
                "preview_port": preview.get("port"),
                "writer_session": rnd.get("writer_session"),
                "round": rnd.get("round"),
                "evidence_id": rnd.get("evidence_id"),
            })
        gaps.extend(image_gaps)
        if not captures:
            gaps.append("no readable screenshot survived capture")
    if not result.get("ok") and not gaps:
        gaps.append(f"the browser recipe exited {result.get('returncode')}")

    entry = {
        "role": role,
        "at": utcnow(),
        "change_id": ctx["change_id"],
        "url": preview.get("url"),
        "served_commit_sha": rnd.get("served_commit_sha"),
        "preview_port": preview.get("port"),
        "writer_session": rnd.get("writer_session"),
        "round": rnd.get("round"),
        "evidence_id": rnd.get("evidence_id"),
        "scenario": contract.get("scenario"),
        "viewports": [v["name"] for v in viewports],
        "recipe": spec.get("name"),
        "returncode": result.get("returncode"),
        "compact": compact_cmd_signal(result),
        "ok": not gaps,
        "gaps": gaps[:8],
        "visible": [str(v)[:BUILD_NAME_MAX] for v in ((observation or {}).get("visible") or [])][:16]
        if isinstance(observation, dict) else [],
        "accessibility": (observation or {}).get("accessibility") if isinstance(observation, dict) else None,
        "console": (observation or {}).get("console") if isinstance(observation, dict) else None,
        "requests": (observation or {}).get("requests") if isinstance(observation, dict) else None,
    }
    rnd.setdefault("observations", {})[role] = redact_secrets(entry)
    if gaps:
        # Nothing is recorded as a capture. A round that kept an unverifiable
        # image would carry it to publication with a caveat nobody reads.
        write_err = write_build(doc, doc.get("build_id"))
        if write_err:
            return int(write_err["status"]), write_err
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "round": rnd.get("round"),
            "role": role,
            "observation": entry,
            "error": gaps[0],
            "code": "not_served_evidence",
            "budget_left": build_budget_left(doc),
        }
    kept = [
        c
        for c in build_visual_round_captures(rnd)
        if not (c.get("role") == role and c.get("viewport") in [v["name"] for v in viewports])
    ]
    rnd["captures"] = kept + captures
    # Wait to reinspect until every failed role has been replaced.
    if requested_only is not None:
        rnd["inspection_queue"] = [
            item for item in (rnd.get("inspection_queue") or [])
            if not isinstance(item, dict) or item.get("role") != role
        ]
        if not rnd["inspection_queue"]:
            rnd["inspection"] = None
    else:
        rnd["inspection"] = None
    return 200, {
        "ok": True,
        "build_id": doc.get("build_id"),
        "round": rnd.get("round"),
        "role": role,
        "url": preview.get("url"),
        "observation": entry,
        "captures": [{k: c[k] for k in ("role", "file", "viewport", "width", "height", "digest")} for c in captures],
        "budget_left": build_budget_left(doc),
    }


def build_visual_scenario_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(
            job["request"], "visual_scenario", {"role": job["role"], "only": job.get("only")}, load_repos()
        )
        if refusal:
            status, payload = refusal
        else:
            assert ctx is not None
            status, payload = build_visual_scenario_result(ctx, job["role"], job.get("only"), load_repos())
            if payload.get("ok") or payload.get("code") == "not_served_evidence":
                status, payload = build_visual_commit(
                    ctx,
                    {"role": job["role"], "round": payload.get("round"), "ok": payload.get("ok")},
                    status,
                    payload,
                )
    receipt = {
        "status": "ok" if payload.get("ok") else "nack",
        "job_id": job_id,
        "build_id": job["build_id"],
        "http_status": status,
        **{k: payload.get(k) for k in ("ok", "round", "role", "url", "code", "error")},
    }
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["receipt"] = receipt
    job["result"] = payload
    job["status"] = "completed"
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def build_visual_scenario(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Run the declared scenario against the real served page.

    Synchronous by default and webhook-delivered when asked, on the same terms
    as buildChecks: the 202 is an acknowledgement of accepted work and says
    ok=false, because a queue receipt read as a captured round is exactly how a
    build reaches review with no picture in it.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    role = data.get("role")
    if role not in BUILD_VISUAL_ROLES:
        return 400, {
            "ok": False,
            "error": "role must be before or candidate",
            "code": "bad_role",
            "allowed": list(BUILD_VISUAL_ROLES),
        }
    only = data.get("only")
    if only is not None:
        if isinstance(only, str):
            only = [only]
        if not isinstance(only, list) or not all(isinstance(n, str) and n.strip() for n in only):
            return 400, {"ok": False, "error": "only must be a list of viewport names", "code": "bad_only"}
        only = [n.strip()[:BUILD_NAME_MAX] for n in only]
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err

    if not webhook_url:
        with BUILD_LOCK:
            ctx, refusal = build_visual_event(data, "visual_scenario", {"role": role, "only": only}, repos)
            if refusal:
                return refusal
            assert ctx is not None
            status, payload = build_visual_scenario_result(ctx, role, only, repos)
            if payload.get("ok") or payload.get("code") == "not_served_evidence":
                return build_visual_commit(
                    ctx,
                    {"role": role, "round": payload.get("round"), "ok": payload.get("ok")},
                    status,
                    payload,
                )
            return status, payload

    doc, open_err = build_open_visual(data.get("build_id"))
    if open_err:
        return open_err
    assert doc is not None
    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "kind": "build_visual_scenario",
            "status": "queued",
            "build_id": doc.get("build_id"),
            "role": role,
            "only": only,
            "request": {k: data.get(k) for k in ("build_id", "event_id", "holder", "worktree")},
            "cwd": str(doc.get("worktree")),
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
    herdr_follow_job(job)
    threading.Thread(
        target=build_visual_scenario_job, args=(job_id,), name=f"graphwing-shot-{job_id[:8]}", daemon=True
    ).start()
    return 202, {
        "ok": False,
        "queued": True,
        "captured": False,
        "code": "queued",
        "error": "queued: this is a receipt for accepting the work, not for a captured round",
        "job_id": job_id,
        "kind": "build_visual_scenario",
        "status": "queued",
        "build_id": doc.get("build_id"),
        "role": role,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def build_visual_image_reasons(capture: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    """Why this image is not publishable, in the words a recapture needs.

    Everything here is a way an image can pass every mechanical check and still
    be worthless to the person it is for: a modal over the thing it is meant to
    show, the wrong route in the address bar, another tenant's data on screen,
    or the label the ticket promised simply absent.
    """
    reasons: list[str] = []
    if capture.get("obscured") is not False:
        reasons.append("the target UI is obscured or the capture does not say it is not")
    route = str(contract.get("route") or "")
    if str(capture.get("route") or "") != route:
        reasons.append(f"the image was taken at '{capture.get('route')}', not '{route}'")
    tenant = str(contract.get("tenant") or "")
    if str(capture.get("tenant") or "") != tenant:
        reasons.append(
            f"the visible tenant or project context is '{capture.get('tenant')}', not '{tenant}'"
        )
    text = [str(t).lower() for t in (capture.get("text") or [])]
    for expected in contract.get("expected") or []:
        needle = str(expected).lower()
        if not any(needle in line for line in text):
            reasons.append(f"the required label or value is not legible in the image: {expected}")
    viewport = next(
        (v for v in contract.get("viewports") or [] if v["name"] == capture.get("viewport")), None
    )
    if viewport is None:
        reasons.append("the image claims a viewport the contract never declared")
    elif int(capture.get("width") or 0) < int(viewport["width"]):
        reasons.append("the image is narrower than the viewport it claims, so it is not legible at it")
    return reasons


def build_visual_inspect(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Grade every captured image against the contract before anything uploads.

    A failed image names the viewport and role to recapture rather than
    disqualifying the round: the fix for an obscured screenshot is another
    screenshot, and the round has already paid for its preview stack.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(data, "visual_inspect", {}, repos)
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        contract = build_visual_contract(doc) or {}
        rnd = build_visual_open_round(doc, ctx["change_id"])
        if rnd is None:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "no round is open for the change on disk"
            out["code"] = "no_open_round"
            return 409, out
        captures = build_visual_round_captures(rnd)
        if not captures:
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = "this round has captured nothing to inspect"
            out["code"] = "no_captures"
            return 409, out
        results: list[dict[str, Any]] = []
        recapture: list[dict[str, Any]] = []
        for capture in captures:
            reasons = build_visual_image_reasons(capture, contract)
            # Re-read the bytes. The digest recorded at capture is what the
            # upload will carry, so an image edited or truncated between capture
            # and inspection must not inherit the earlier verdict.
            try:
                root = build_visual_round_dir(str(doc.get("build_id")), int(rnd.get("round") or 0))
                data_bytes = read_regular_file(root, str(capture.get("file") or ""))
            except OSError:
                reasons.append("the image file is no longer readable")
                data_bytes = b""
            if data_bytes and hashlib.sha256(data_bytes).hexdigest() != capture.get("digest"):
                reasons.append("the image on disk no longer matches the digest recorded at capture")
            capture["verified"] = not reasons
            capture["reasons"] = reasons[:6]
            capture["inspected_at"] = utcnow()
            results.append(
                {
                    "role": capture.get("role"),
                    "file": capture.get("file"),
                    "viewport": capture.get("viewport"),
                    "verified": capture["verified"],
                    "reasons": capture["reasons"],
                }
            )
            if reasons:
                recapture.append({"role": capture.get("role"), "viewport": capture.get("viewport")})
        missing = build_visual_missing_roles(rnd)
        ok = not recapture and not missing
        inspection = {
            "at": utcnow(),
            "change_id": ctx["change_id"],
            "ok": ok,
            "results": results,
            "recapture": [f"{r['role']}/{r['viewport']}" for r in recapture],
            "missing_roles": missing,
            "error": None
            if ok
            else (
                f"missing {', '.join(missing)} capture" if missing
                else f"{len(recapture)} image(s) failed inspection"
            ),
        }
        rnd["inspection"] = inspection
        rnd["inspection_queue"] = build_visual_recapture_queue(
            recapture, rnd.get("recapture_attempts")
        )
        recapture_by_role = {
            str(item["role"]): item["only"] for item in rnd["inspection_queue"]
        }
        recapture_roles = {
            role: role in recapture_by_role for role in BUILD_VISUAL_ROLES
        }
        status = 200 if ok else 409
        return build_visual_commit(
            ctx,
            {"round": rnd.get("round"), "ok": ok, "recapture": inspection["recapture"]},
            status,
            {
                "ok": ok,
                "round": rnd.get("round"),
                "inspection": inspection,
                "recapture": recapture,
                "recapture_by_role": recapture_by_role,
                "recapture_roles": recapture_roles,
                "error": inspection["error"],
                "code": None if ok else ("missing_capture" if missing else "image_rejected"),
            },
        )


def build_visual_review_prompt(doc: dict[str, Any], rnd: dict[str, Any]) -> str:
    """What the automated UI reviewer is asked, by lens.

    It receives the ticket, the diff for this round, what the browser observed,
    and the screenshot paths. A reviewer given only the diff grades the code —
    which the behavior-and-test review already did — and never looks at the
    thing the round exists to show.
    """
    contract = build_visual_contract(doc) or {}
    worktree = Path(str(doc.get("worktree")))
    diff = git_diff(worktree, str(doc.get("base_head") or "HEAD"), None)
    lines = [
        "Automated UI and accessibility review of one visual evidence round.",
        "You did not write this change. Answer each lens, then give one verdict.",
        "",
        "1. LAYOUT: does the captured UI hold together at every viewport, or is something",
        "   clipped, overlapping, or off-screen?",
        "2. ACCESSIBILITY: read the recorded accessibility result. Name any violation that",
        "   would stop a keyboard or screen-reader user.",
        "3. CONSOLE AND REQUESTS: read the recorded console and request results. A failed",
        "   request behind a page that looks fine is still a finding.",
        "4. PROMISE: does the visible state actually show what the ticket says it would?",
        "",
        f"Route: {contract.get('route')}",
        f"Scenario: {contract.get('scenario')}",
        f"Preview URL: {github_markdown_url((rnd.get('preview') or {}).get('url'))}",
        f"Expected visible state: {'; '.join(contract.get('expected') or []) or '(none declared)'}",
        f"Proof checklist: {'; '.join(contract.get('checklist') or []) or '(none declared)'}",
        "",
        "Screenshots (read them; they are the primary evidence):",
    ]
    for index, capture in enumerate(build_visual_round_captures(rnd, verified=True), start=1):
        lines.append(
            f"  {index}. [{capture.get('role')}/{capture.get('viewport')}] {capture.get('path')}"
        )
    lines.extend(
        [
            "",
            "Browser observations:",
            json.dumps(rnd.get("observations") or {}, indent=2)[:4000],
            "",
            "Ticket:",
            str(doc.get("ticket") or "(none)"),
            "",
            "Diff for this round:",
            str(diff.get("diff") or "")[:8000],
            "",
            "End with exactly one line: VERDICT: PASS (acknowledged) or VERDICT: NACK (findings).",
        ]
    )
    return "\n".join(lines)


def build_visual_review_result(
    ctx: dict[str, Any], reviewer: str, repos: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    """Run the independent UI review for this round and record it.

    Silence and disagreement stay apart here for the same reason they do on the
    mechanical path: a reviewer that lost its connection has not found anything,
    and writing that down as findings would send the writer to fix a problem
    nobody reported.
    """
    doc = ctx["doc"]
    rnd = build_visual_open_round(doc, ctx["change_id"])
    if rnd is None:
        return 409, {"ok": False, "error": "no round is open for the change on disk", "code": "no_open_round"}
    inspection = rnd.get("inspection")
    if not isinstance(inspection, dict) or not inspection.get("ok"):
        return 409, {
            "ok": False,
            "round": rnd.get("round"),
            "error": "the round's images have not passed inspection, so there is nothing to review",
            "code": "not_inspected",
        }
    gap = build_review_authority_gap(doc, reviewer)
    if gap:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "error": gap,
            "code": "reviewer_is_writer" if "own vendor" in gap else "no_review_authority",
        }
    park = build_charge(doc, 1)
    if park:
        return park
    prompt = build_visual_review_prompt(doc, rnd)
    worktree = Path(str(doc.get("worktree")))
    attempts: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    for _ in range(BUILD_REVIEW_SILENCE_RETRIES + 1):
        result = build_review_round_result(reviewer, prompt, worktree)
        attempts.append({"no_verdict": bool(result.get("no_verdict")), "code": result.get("code")})
        if not result.get("no_verdict"):
            break
    if result.get("no_verdict"):
        write_err = write_build(doc, doc.get("build_id"))
        if write_err:
            return int(write_err["status"]), write_err
        return 200, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "round": rnd.get("round"),
            "reviewer": reviewer,
            "verdict": None,
            "no_verdict": True,
            "findings": False,
            "attempts": len(attempts),
            "error": f"{reviewer} returned no verdict after {len(attempts)} attempts",
            "code": "no_verdict",
            "budget_left": build_budget_left(doc),
        }
    entry = {
        "reviewer": reviewer,
        "at": utcnow(),
        "change_id": ctx["change_id"],
        "verdict": result.get("verdict"),
        "findings": result.get("verdict") != "PASS",
        "summary": str(result.get("summary") or "")[:BUILD_REDACT_MAX_STRING],
        "compact": str(result.get("compact") or "")[:COMPACT_MAX_CHARS],
        "attempts": len(attempts),
    }
    rnd["review"] = entry
    passed = entry["verdict"] == "PASS"
    return (200 if passed else 409), {
        "ok": passed,
        "recorded": True,
        "build_id": doc.get("build_id"),
        "round": rnd.get("round"),
        "reviewer": reviewer,
        "verdict": entry["verdict"],
        "no_verdict": False,
        "findings": entry["findings"],
        "summary": entry["summary"],
        "compact": entry["compact"],
        "attempts": len(attempts),
        "budget_left": build_budget_left(doc),
    }


def build_visual_review_job(job_id: str) -> None:
    job = read_job(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = utcnow()
    write_job(job)
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(
            job["request"], "visual_review", {"reviewer": job["reviewer"]}, load_repos()
        )
        if refusal:
            status, payload = refusal
        else:
            assert ctx is not None
            status, payload = build_visual_review_result(ctx, job["reviewer"], load_repos())
            if payload.get("ok") or payload.get("recorded"):
                status, payload = build_visual_commit(
                    ctx,
                    {"reviewer": job["reviewer"], "round": payload.get("round"),
                     "verdict": payload.get("verdict")},
                    status,
                    payload,
                )
    receipt = {
        "status": "ok" if payload.get("ok") else "nack",
        "job_id": job_id,
        "build_id": job["build_id"],
        "http_status": status,
        **{k: payload.get(k) for k in ("ok", "round", "verdict", "no_verdict", "findings", "summary", "code", "error")},
    }
    job = read_job(job_id) or job
    job["finished_at"] = utcnow()
    job["receipt"] = receipt
    job["result"] = payload
    job["status"] = "completed"
    hook = deliver_webhook(job, receipt)
    if hook is not None:
        job["webhook"] = hook
    write_job(job)
    herdr_job_done(job)


def build_visual_review(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """The independent automated UI and accessibility review for one round."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    doc, open_err = build_open_visual(data.get("build_id"))
    if open_err:
        return open_err
    assert doc is not None
    route = doc.get("route") or {}
    reviewer = data.get("reviewer") or route.get("reviewer1")
    if not isinstance(reviewer, str) or reviewer.strip() not in BUILD_REVIEWER_VENDOR:
        return 400, {
            "ok": False,
            "error": "reviewer must be one of " + ", ".join(sorted(BUILD_REVIEWER_VENDOR)),
            "code": "bad_reviewer",
        }
    reviewer = reviewer.strip()
    gap = build_review_authority_gap(doc, reviewer)
    if gap:
        return 409, {
            "ok": False,
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "error": gap,
            "code": "reviewer_is_writer" if "own vendor" in gap else "no_review_authority",
        }
    webhook_url, webhook_token, webhook_err = parse_webhook_fields(data)
    if webhook_err:
        return 400, webhook_err

    if not webhook_url:
        with BUILD_LOCK:
            ctx, refusal = build_visual_event(data, "visual_review", {"reviewer": reviewer}, repos)
            if refusal:
                return refusal
            assert ctx is not None
            status, payload = build_visual_review_result(ctx, reviewer, repos)
            if payload.get("ok") or payload.get("recorded"):
                return build_visual_commit(
                    ctx,
                    {"reviewer": reviewer, "round": payload.get("round"), "verdict": payload.get("verdict")},
                    status,
                    payload,
                )
            return status, payload

    with JOB_LOCK:
        if active_job_count() >= AGENT_MAX_CONCURRENT:
            return 429, {"error": "too many in-flight agent jobs", "code": "busy"}
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "kind": "build_visual_review",
            "status": "queued",
            "build_id": doc.get("build_id"),
            "reviewer": reviewer,
            "request": {k: data.get(k) for k in ("build_id", "event_id", "holder", "worktree")},
            "cwd": str(doc.get("worktree")),
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
    herdr_follow_job(job)
    threading.Thread(
        target=build_visual_review_job, args=(job_id,), name=f"graphwing-vreview-{job_id[:8]}", daemon=True
    ).start()
    return 202, {
        "ok": False,
        "queued": True,
        "code": "queued",
        "error": "queued: this is a receipt for accepting the work, not a review verdict",
        "job_id": job_id,
        "kind": "build_visual_review",
        "status": "queued",
        "build_id": doc.get("build_id"),
        "reviewer": reviewer,
        "poll": f"/v1/agent/jobs/{job_id}",
    }


def artifact_root() -> Path:
    """Where durable artifact records live.

    Derived from BUILDS_DIR rather than from HOME directly so a service under
    a relocated home — or a test — keeps its artifacts beside its builds.
    """
    return BUILDS_DIR.parent / "artifacts"


def artifact_dir(artifact_id: str) -> Path:
    return artifact_root() / artifact_id


def artifact_lock(artifact_id: str) -> threading.Lock:
    with BUILD_ARTIFACT_LOCKS_GUARD:
        return BUILD_ARTIFACT_LOCKS.setdefault(artifact_id, threading.Lock())


def read_regular_file(root: Path, name: str) -> bytes:
    """Read one direct child without following a symlink or escaping root."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise OSError("invalid file name")
    resolved_root = root.resolve(strict=True)
    candidate = root / name
    if candidate.parent.resolve(strict=True) != resolved_root:
        raise OSError("file escapes output directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(str(resolved_root), os.O_RDONLY)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("file is not regular")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read(BUILD_IMAGE_MAX_BYTES + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def artifact_read(artifact_id: str) -> dict[str, Any] | None:
    if not BUILD_ARTIFACT_ID_RE.fullmatch(artifact_id or ""):
        return None
    try:
        data = json.loads((artifact_dir(artifact_id) / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def artifact_write(meta: dict[str, Any]) -> dict[str, Any] | None:
    path = artifact_dir(str(meta["artifact_id"]))
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(artifact_root(), 0o700)
        os.chmod(path, 0o700)
        tmp = path / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path / "meta.json")
    except OSError as exc:
        return {
            "ok": False,
            "error": f"could not persist artifact record: {exc}",
            "code": "artifact_write_failed",
            "status": 500,
        }
    return None


def artifact_store(capture: dict[str, Any], doc: dict[str, Any], rnd: dict[str, Any], number: int
                   ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Copy one verified image into a durable artifact record.

    Durable is the point. The grant that lets Rewst fetch these bytes expires
    in minutes; the record does not, because the GitHub comment it produces
    will outlive every one of them and the digest here is the only way to
    answer "is that image still the image this round captured".
    """
    round_dir = build_visual_round_dir(str(doc.get("build_id")), int(rnd.get("round") or 0))
    try:
        data = read_regular_file(round_dir, str(capture.get("file") or ""))
    except OSError as exc:
        return None, {"ok": False, "error": f"image is unreadable: {exc}", "code": "artifact_unreadable"}
    digest = hashlib.sha256(data).hexdigest()
    if digest != capture.get("digest"):
        return None, {
            "ok": False,
            "error": f"image '{capture.get('file')}' no longer matches its capture digest",
            "code": "artifact_digest_mismatch",
        }
    artifact_id = hashlib.sha256(
        f"{doc.get('build_id')}\0{rnd.get('round')}\0{capture.get('role')}\0{capture.get('viewport')}\0{digest}".encode()
    ).hexdigest()[:32]
    path = artifact_dir(artifact_id)
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(artifact_root(), 0o700)
        os.chmod(path, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path / "image.png"), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("artifact target is not regular")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
    except OSError as exc:
        return None, {"ok": False, "error": f"could not store artifact: {exc}", "code": "artifact_write_failed"}
    meta = {
        "artifact_id": artifact_id,
        "build_id": doc.get("build_id"),
        "round": rnd.get("round"),
        "number": number,
        "role": capture.get("role"),
        "viewport": capture.get("viewport"),
        "label": capture.get("label"),
        "media_type": "image/png",
        "bytes": len(data),
        "digest": digest,
        "width": capture.get("width"),
        "height": capture.get("height"),
        "change_id": capture.get("change_id"),
        "created_at": utcnow(),
        # Never the token itself. A grant is a hash plus an expiry, so a copy of
        # this file is not a copy of the capability.
        "grant": None,
    }
    err = artifact_write(meta)
    if err:
        return None, err
    return meta, None


def artifact_mint_grant(meta: dict[str, Any], ttl: int) -> tuple[str, dict[str, Any]]:
    """One short-lived, single-use fetch capability for one artifact."""
    token = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    grant = {
        "digest": hashlib.sha256(token.encode()).hexdigest(),
        "expires_epoch": time.time() + ttl,
        "expires_at": datetime.fromtimestamp(time.time() + ttl, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "consumed_at": None,
        "minted_at": utcnow(),
    }
    BUILD_ARTIFACT_GRANT_TOKENS[(str(meta.get("artifact_id")), grant["digest"])] = token
    return token, grant


def artifact_outstanding_grants(artifact_ids: list[Any]) -> list[dict[str, Any]]:
    return artifact_grant_replay_state(artifact_ids)[0]


def artifact_grant_replay_state(
    artifact_ids: list[Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """Return every still-live grant, or one stable reason no replay is possible."""
    base = public_base_url() or f"http://{LISTEN_HOST}:{LISTEN_PORT}"
    out: list[dict[str, Any]] = []
    states: set[str] = set()
    for raw_id in artifact_ids:
        artifact_id = str(raw_id)
        meta = artifact_read(artifact_id)
        if not isinstance(meta, dict):
            states.add("grant_missing")
            continue
        grant = meta.get("grant")
        if not isinstance(grant, dict) or not grant.get("digest"):
            states.add("grant_missing")
            continue
        if grant.get("consumed_at"):
            states.add("grant_spent")
            continue
        try:
            if float(grant.get("expires_epoch") or 0) <= time.time():
                states.add("grant_expired")
                continue
        except (TypeError, ValueError):
            states.add("grant_missing")
            continue
        token = BUILD_ARTIFACT_GRANT_TOKENS.get((artifact_id, str(grant.get("digest") or "")))
        if not token:
            states.add("grant_missing")
            continue
        out.append({
            "artifact_id": artifact_id,
            "number": meta.get("number"),
            "role": meta.get("role"),
            "viewport": meta.get("viewport"),
            "media_type": meta.get("media_type"),
            "digest": meta.get("digest"),
            "fetch_url": f"{base}/v1/artifact/{artifact_id}",
            "fetch_header": BUILD_ARTIFACT_TOKEN_HEADER,
            "fetch_token": token,
            "expires_at": grant.get("expires_at"),
        })
    if states:
        for code in ("grant_spent", "grant_expired", "grant_missing"):
            if code in states:
                return [], code
    if len(out) != len(artifact_ids) or not artifact_ids:
        return [], "grant_missing"
    return out, None


def artifact_fetch(artifact_id: str, headers: Any) -> tuple[int, dict[str, Any] | bytes, str]:
    """Serve artifact bytes once, to the holder of an unexpired grant.

    Three separate refusals, all fail-closed: no grant, an expired grant, and a
    grant that has already been spent. The token is compared by digest with a
    constant-time check — the plaintext exists exactly once, in the handoff
    response, and is never written down on this side.
    """
    with artifact_lock(artifact_id):
        meta = artifact_read(artifact_id)
        if meta is None:
            return json_out(404, {"ok": False, "error": "unknown artifact", "code": "unknown_artifact"})
        presented = _request_header(headers, BUILD_ARTIFACT_TOKEN_HEADER)
        grant = meta.get("grant")
        if not isinstance(grant, dict) or not grant.get("digest"):
            return json_out(403, {"ok": False, "error": "no upload grant is outstanding for this artifact",
                                  "code": "no_grant"})
        if not presented or not hmac.compare_digest(
            hashlib.sha256(presented.encode()).hexdigest(), str(grant.get("digest"))
        ):
            return json_out(403, {"ok": False, "error": "invalid artifact token", "code": "bad_artifact_token"})
        if grant.get("consumed_at"):
            return json_out(410, {"ok": False, "error": "this upload grant has already been used",
                                  "code": "grant_consumed"})
        try:
            expires = float(grant.get("expires_epoch") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        if expires <= time.time():
            return json_out(410, {"ok": False, "error": "this upload grant has expired",
                                  "code": "grant_expired"})
        try:
            data = read_regular_file(artifact_dir(artifact_id), "image.png")
        except OSError:
            return json_out(410, {"ok": False, "error": "artifact bytes are no longer readable",
                                  "code": "artifact_gone"})
        grant["consumed_at"] = utcnow()
        err = artifact_write(meta)
        if err:
            return json_out(int(err["status"]), err)
        BUILD_ARTIFACT_GRANT_TOKENS.pop((artifact_id, str(grant.get("digest"))), None)
        return 200, data, str(meta.get("media_type") or "image/png")


def build_visual_handoff_gap(rnd: dict[str, Any]) -> str | None:
    inspection = rnd.get("inspection")
    if not isinstance(inspection, dict) or not inspection.get("ok"):
        return "not_inspected"
    review = rnd.get("review")
    if not isinstance(review, dict) or review.get("no_verdict") or review.get("verdict") != "PASS":
        return "review_not_passed"
    return None


def build_visual_handoff(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Hand the round's verified images to Rewst under short-lived grants.

    This is the whole reason the laptop never holds a tracker credential. The
    bytes stay here in a durable record; what crosses is a fetch capability that
    expires in minutes and is spent on first use. Rewst pulls each image with
    its own integration and uploads it under its own GitHub auth.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    ttl, ttl_err = parse_optional_int(data, "ttl_seconds", 60, BUILD_ARTIFACT_TTL_MAX)
    if ttl_err:
        return 400, ttl_err
    ttl = ttl or BUILD_ARTIFACT_TTL_SECONDS
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(data, "visual_handoff", {"ttl_seconds": ttl}, repos)
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        rnd = build_visual_open_round(doc, ctx["change_id"])
        if rnd is None:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "no round is open for the change on disk"
            out["code"] = "no_open_round"
            return 409, out
        handoff_gap = build_visual_handoff_gap(rnd)
        if handoff_gap:
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = "only a clean inspection and explicit PASS review may be handed off"
            out["code"] = handoff_gap
            return 409, out
        existing = rnd.get("handoff")
        if isinstance(existing, dict):
            ids = [str(item.get("artifact_id")) for item in existing.get("artifacts") or []]
            outstanding, grant_code = artifact_grant_replay_state(ids)
            if grant_code:
                out = build_public(doc)
                out.update({
                    "ok": False,
                    "round": rnd.get("round"),
                    "code": grant_code,
                    "error": "this round's issued upload grant is no longer outstanding; open a new visual round",
                })
                return 409, out
            if outstanding:
                status, payload = build_visual_commit(
                    ctx,
                    {"round": rnd.get("round"), "artifacts": ids, "reused_grants": True},
                    200,
                    {"ok": True, "round": rnd.get("round"), "handoff": existing},
                )
                if status == 200:
                    payload["grants"] = outstanding
                return status, payload
        inspection = rnd.get("inspection")
        if not isinstance(inspection, dict) or not inspection.get("ok"):
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = "only images that passed inspection may be handed off"
            out["code"] = "not_inspected"
            return 409, out
        captures = build_visual_round_captures(rnd, verified=True)
        if not captures:
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = "this round has no verified image to hand off"
            out["code"] = "no_captures"
            return 409, out
        issue, issue_gap = build_issue_target(doc)
        if issue is None:
            out = build_public(doc)
            out.update({"ok": False, "code": "no_issue_target", "error": str(issue_gap)})
            return 409, out
        base = public_base_url() or f"http://{LISTEN_HOST}:{LISTEN_PORT}"
        # Numbered here, once, in a stable order. The comment, the artifact
        # records and the planning prompt all refer to "image 3" and have to
        # mean the same picture.
        ordered = sorted(captures, key=lambda c: (str(c.get("role")), str(c.get("viewport"))))
        grants: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        for number, capture in enumerate(ordered, start=1):
            meta, store_err = artifact_store(capture, doc, rnd, number)
            if store_err:
                out = build_public(doc)
                out.update(store_err)
                out["ok"] = False
                return 409, out
            assert meta is not None
            token, grant = artifact_mint_grant(meta, ttl)
            meta["grant"] = grant
            write_err = artifact_write(meta)
            if write_err:
                return int(write_err["status"]), write_err
            capture["artifact_id"] = meta["artifact_id"]
            capture["number"] = number
            records.append(
                {k: meta[k] for k in ("artifact_id", "number", "role", "viewport", "label",
                                      "digest", "bytes", "width", "height", "media_type")}
            )
            grants.append(
                {
                    "artifact_id": meta["artifact_id"],
                    "number": number,
                    "role": meta["role"],
                    "viewport": meta["viewport"],
                    "media_type": meta["media_type"],
                    "digest": meta["digest"],
                    "fetch_url": f"{base}/v1/artifact/{meta['artifact_id']}",
                    "fetch_header": BUILD_ARTIFACT_TOKEN_HEADER,
                    # The only time this value exists outside the caller's hands.
                    # Nothing on this side stores it; the record keeps its digest.
                    "fetch_token": token,
                    "expires_at": grant["expires_at"],
                }
            )
        handoff = {
            "at": utcnow(),
            "change_id": ctx["change_id"],
            "ttl_seconds": ttl,
            "expires_at": grants[0]["expires_at"],
            "artifacts": records,
        }
        rnd["handoff"] = handoff
        status, payload = build_visual_commit(
            ctx,
            {"round": rnd.get("round"), "artifacts": [r["artifact_id"] for r in records]},
            200,
            {"ok": True, "round": rnd.get("round"), "handoff": handoff, "issue": issue},
        )
        # Attached after the state write, deliberately: the tokens are the one
        # part of this response that must never reach disk.
        if status == 200:
            payload["grants"] = grants
        return status, payload


def github_markdown_text(value: Any) -> str:
    """One untrusted value rendered as inert single-line GitHub markdown."""
    text = str(value or "").replace("\r", " ").replace("\n", "<br>").replace("@", "@\u200b")
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", text)[:BUILD_REDACT_MAX_STRING]


def github_markdown_url(value: Any) -> str:
    """Render an accepted durable or preview URL as one inert GitHub autolink."""
    raw = str(value or "")
    if not (durable_github_url(raw) or loopback_http_url(raw)):
        return github_markdown_text("[invalid URL]")
    parsed = urlsplit(raw)
    encoded = urlunsplit((
        parsed.scheme,
        quote(parsed.netloc, safe="[]:."),
        quote(parsed.path, safe="/%:-._~"),
        quote(parsed.query, safe="=&%/:?-._~"),
        quote(parsed.fragment, safe="%/:?-._~"),
    ))
    return f"<{encoded}>"


def durable_github_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or reject_secret_text(value, "upload.url"):
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    github_host = (
        r"(?:github\.com|"
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+githubusercontent\.com)"
    )
    return (
        parsed.scheme == "https"
        and strict_url_authority(parsed, github_host, {None, 443})
        and not parsed.query
        and not parsed.fragment
        and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
    )


def durable_upload_ref(value: Any) -> bool:
    """Accept an opaque upload id, but validate anything shaped like a URL."""
    if not isinstance(value, str) or not value or reject_secret_text(value, "upload.ref"):
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    url_shaped = re.match(r"(?i)^(?:https?(?::|%3a)|//|\\\\)", value) is not None
    return not url_shaped or durable_github_url(value)


def github_comment_url_gap(value: Any, issue: dict[str, Any]) -> str | None:
    if not isinstance(value, str):
        return "comment_url is required"
    try:
        parsed = urlparse(value)
    except ValueError:
        return "comment_url does not belong to this issue"
    expected_path = (
        f"/{issue.get('owner')}/{issue.get('repo')}/issues/{int(issue.get('number') or 0)}"
    )
    valid = (
        parsed.scheme == "https"
        and strict_url_authority(parsed, r"github\.com", {None, 443})
        and parsed.path == expected_path
        and not parsed.params
        and not parsed.query
        and re.fullmatch(r"issuecomment-[0-9]+", parsed.fragment or "") is not None
        and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
    )
    return None if valid else "comment_url does not belong to this issue"


def build_issue_target(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The GitHub issue this build's evidence belongs to, or why there is none.

    The story is the issue URL. Parsed rather than passed in so a caller cannot
    aim a round's evidence at somebody else's issue, and named as owner, repo
    and number so Rewst's GitHub integration has the three fields it needs
    without this side ever holding a token.
    """
    story = str(doc.get("story") or "")
    match = BUILD_ISSUE_RE.fullmatch(story.strip())
    if match is None:
        return None, (
            f"story '{story}' is not a GitHub issue URL; the evidence comment has nowhere to go"
        )
    return {
        "owner": match.group(1),
        "repo": match.group(2),
        "number": int(match.group(3)),
        "url": story.strip(),
    }, None


def build_evidence_comment(doc: dict[str, Any], rnd: dict[str, Any], uploads: list[dict[str, Any]]) -> str:
    """The exact comment body Rewst posts on the GitHub issue.

    Composed here so every round says the same things in the same order: which
    round this is and what it replaces, where the picture was taken, what the
    scenario did, what is visibly true, what the automated checks found, and
    whether a person is looking at it. A body assembled in the graph drifts one
    convenient interpolation at a time until the numbered images no longer match
    the numbered claims.
    """
    contract = build_visual_contract(doc) or {}
    preview = rnd.get("preview") or {}
    review = rnd.get("review") or {}
    observations = rnd.get("observations") or {}
    safe = github_markdown_text
    lines = [
        f"### Visual evidence — round {rnd.get('round')}",
        "",
        f"- **Build**: `{safe(doc.get('build_id'))}` on `{safe(doc.get('branch'))}` at `{safe(rnd.get('head'))}`",
        f"- **Route**: `{safe(contract.get('route'))}`",
        f"- **Scenario**: {safe(contract.get('scenario'))}",
        f"- **Preview**: `{safe(preview.get('preview_id'))}` at {github_markdown_url(preview.get('url'))}",
        f"- **Tenant / project shown**: {safe(contract.get('tenant'))}",
        f"- **Supersedes**: "
        + (f"round {rnd.get('supersedes')} (kept above, not deleted)" if rnd.get("supersedes") else "nothing; this is the first round"),
        f"- **Human visual review**: {'requested' if rnd.get('human_review') else 'not requested'}",
        "",
        "**Images**",
    ]
    for upload in uploads:
        durable = (
            github_markdown_url(upload.get("url"))
            if upload.get("url")
            else safe(upload.get("ref"))
        )
        lines.append(
            f"{upload['number']}. [{safe(upload['role'])} / {safe(upload['viewport'])}] {safe(upload.get('label'))} "
            f"— `{upload['artifact_id']}` {durable}".rstrip()
        )
    lines += ["", "**Visible proof**"]
    for item in contract.get("expected") or []:
        lines.append(f"- {safe(item)}")
    seen: list[str] = []
    for role in ("before", "candidate"):
        entry = observations.get(role)
        if isinstance(entry, dict):
            seen.extend(str(v) for v in (entry.get("visible") or []))
    if seen:
        lines += ["", "**Observed on the served page**"] + [f"- {safe(item)}" for item in seen[:12]]
    candidate = observations.get("candidate") or {}
    lines += [
        "",
        "**Automated checks**",
        f"- accessibility: {safe(json.dumps(candidate.get('accessibility')))}",
        f"- console: {safe(json.dumps(candidate.get('console')))}",
        f"- requests: {safe(json.dumps(candidate.get('requests')))}",
        f"- UI review ({safe(review.get('reviewer'))}): "
        + (
            f"findings — {safe(review.get('summary'))}"
            if review.get("findings")
            else f"acknowledged — {safe(review.get('summary'))}"
        ),
        "",
        "**Review status**: "
        + (
            "awaiting human visual review in the planning session"
            if rnd.get("human_review")
            else "automated only; the plan did not request human visual review"
        ),
        "",
        "_Pre-commit: nothing has been committed or pushed for this round. The preview stack stays up._",
    ]
    return "\n".join(lines)


def build_evidence_publish(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Record the uploaded images and compose the round's evidence comment.

    Every round publishes, whether or not a human was asked to look. The
    comment is the durable record on the issue; the planning prompt is a
    convenience for the person who happens to be at the seat right now.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    if "comment_url" in data:
        return 400, {
            "ok": False,
            "error": "comment_url is recorded only by buildVisualPublishResult after GitHub posts",
            "code": "comment_not_posted",
        }
    raw_uploads = data.get("uploads")
    if not isinstance(raw_uploads, list) or not raw_uploads:
        return 400, {
            "ok": False,
            "error": "uploads must list one {artifact_id, ref|url} per handed-off image",
            "code": "bad_uploads",
        }
    if len(raw_uploads) > BUILD_VISUAL_IMAGES_MAX:
        return 400, {"ok": False, "error": "too many uploads", "code": "bad_uploads"}
    parsed: dict[str, dict[str, Any]] = {}
    for item in raw_uploads:
        if not isinstance(item, dict):
            return 400, {"ok": False, "error": "each upload must be an object", "code": "bad_uploads"}
        artifact_id = item.get("artifact_id")
        if not isinstance(artifact_id, str) or not BUILD_ARTIFACT_ID_RE.fullmatch(artifact_id):
            return 400, {"ok": False, "error": "upload.artifact_id is required", "code": "bad_uploads"}
        ref = item.get("ref")
        url = item.get("url")
        if not any(isinstance(v, str) and v.strip() for v in (ref, url)):
            # A durable link or a durable file id. Without one the comment
            # would name a picture nobody outside this laptop can open.
            return 400, {
                "ok": False,
                "error": "each upload needs the durable ref or url the tracker gave it back",
                "code": "bad_uploads",
            }
        if isinstance(url, str) and not durable_github_url(url):
            return 400, {
                "ok": False,
                "error": "upload.url must be an HTTPS GitHub durable URL without credentials or query data",
                "code": "bad_upload_url",
            }
        if isinstance(ref, str) and not durable_upload_ref(ref):
            return 400, {"ok": False, "error": "upload.ref is unsafe", "code": "bad_uploads"}
        parsed[artifact_id] = {
            "artifact_id": artifact_id,
            "ref": str(ref).strip()[:BUILD_NAME_MAX] if isinstance(ref, str) else None,
            "url": str(url).strip()[:BUILD_NAME_MAX] if isinstance(url, str) else None,
        }
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(
            data, "visual_publish", {"uploads": sorted(parsed)}, repos
        )
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        rnd = build_visual_open_round(doc, ctx["change_id"])
        if rnd is None:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "no round is open for the change on disk"
            out["code"] = "no_open_round"
            return 409, out
        handoff = rnd.get("handoff")
        if not isinstance(handoff, dict):
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = "this round has not handed off its images"
            out["code"] = "no_handoff"
            return 409, out
        if not isinstance(rnd.get("review"), dict):
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = "the automated UI review has not run for this round"
            out["code"] = "no_ui_review"
            return 409, out
        if (rnd.get("review") or {}).get("verdict") != "PASS":
            out = build_public(doc)
            out.update({"ok": False, "round": rnd.get("round"), "code": "review_not_passed",
                        "error": "NACK or no-verdict review cannot be published"})
            return 409, out
        issue, issue_gap = build_issue_target(doc)
        if issue is None:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = str(issue_gap)
            out["code"] = "no_issue_target"
            return 409, out
        records = [r for r in (handoff.get("artifacts") or []) if isinstance(r, dict)]
        expected = {str(r["artifact_id"]) for r in records}
        missing = sorted(expected - set(parsed))
        stray = sorted(set(parsed) - expected)
        if missing or stray:
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = (
                f"uploads do not cover this round's handoff; missing {missing}, unknown {stray}"
            )
            out["code"] = "upload_mismatch"
            return 409, out
        uploads = [
            {
                "number": record["number"],
                "artifact_id": record["artifact_id"],
                "role": record["role"],
                "viewport": record["viewport"],
                "label": record.get("label"),
                "digest": record["digest"],
                **parsed[str(record["artifact_id"])],
            }
            for record in sorted(records, key=lambda r: int(r.get("number") or 0))
        ]
        comment = build_evidence_comment(doc, rnd, uploads)
        published = {
            "prepared_at": utcnow(),
            "issue": issue,
            "uploads": uploads,
            "comment": comment,
            "comment_url": None,
            "round": rnd.get("round"),
            "supersedes": rnd.get("supersedes"),
            "publish_id": hashlib.sha256(
                f"{doc.get('build_id')}\0{rnd.get('round')}\0{ctx['change_id']}".encode()
            ).hexdigest()[:24],
        }
        rnd["published"] = published
        return build_visual_commit(
            ctx,
            {"round": rnd.get("round"), "issue": issue["url"], "publish_id": published["publish_id"]},
            200,
            {
                "ok": True,
                "prepared": True,
                "round": rnd.get("round"),
                "supersedes": rnd.get("supersedes"),
                "issue": issue,
                "uploads": uploads,
                "comment": comment,
                "publish_id": published["publish_id"],
                "human_review": bool(rnd.get("human_review")),
                "pre_commit": not doc.get("pr_ready"),
            },
        )


def build_evidence_publish_result(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Authoritatively record the GitHub comment result after Rewst posts it."""
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    comment_url = data.get("comment_url")
    artifact_ids = data.get("artifact_ids")
    if not isinstance(artifact_ids, list) or not all(
        isinstance(value, str) and BUILD_ARTIFACT_ID_RE.fullmatch(value) for value in artifact_ids
    ):
        return 400, {"ok": False, "code": "bad_artifact_ids", "error": "artifact_ids are required"}
    extra = {"comment_url": comment_url, "artifact_ids": sorted(artifact_ids)}
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(data, "visual_publish_result", extra, repos)
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        rnd = build_visual_open_round(doc, ctx["change_id"])
        if rnd is None:
            return 409, {"ok": False, "code": "no_open_round", "error": "no round awaits a comment result"}
        prepared = rnd.get("published")
        if not isinstance(prepared, dict) or not prepared.get("comment"):
            return 409, {"ok": False, "code": "not_prepared", "error": "publish payload was not prepared"}
        issue = prepared.get("issue") or {}
        gap = github_comment_url_gap(comment_url, issue)
        if gap:
            return 400, {"ok": False, "code": "bad_comment_url", "error": gap}
        expected = {str(item.get("artifact_id")) for item in prepared.get("uploads") or []}
        if set(artifact_ids) != expected:
            return 409, {"ok": False, "code": "artifact_result_mismatch",
                         "error": "comment result artifact IDs do not match the prepared round"}
        prepared["comment_url"] = str(comment_url)
        prepared["commented_at"] = utcnow()
        rnd["status"] = "published"
        gate = None
        if not rnd.get("human_review"):
            record = build_gate_transition(
                doc, "evidence_captured", ctx["change_id"], "buildEvidencePublishResult",
                f"round {rnd.get('round')} comment recorded without human visual review",
            )
            gate = record["to"] if record else None
        return build_visual_commit(
            ctx,
            {"round": rnd.get("round"), "comment_url": comment_url,
             "artifact_ids": sorted(artifact_ids), "gate": gate},
            200,
            {"ok": True, "round": rnd.get("round"), "comment_url": comment_url,
             "artifact_ids": sorted(artifact_ids), "gate": gate,
             "human_review": bool(rnd.get("human_review")), "pre_commit": not doc.get("pr_ready")},
        )


def build_visual_planning_prompt(doc: dict[str, Any], rnd: dict[str, Any]) -> str:
    """The exact text the planning agent is handed, saved verbatim on the round.

    Saved because it is the thing a person answered. A prompt reconstructed
    later from state would drift from what was actually on screen, and the
    feedback turn that follows would be answering a question nobody asked.
    """
    contract = build_visual_contract(doc) or {}
    preview = rnd.get("preview") or {}
    published = rnd.get("published") or {}
    review = rnd.get("review") or {}
    safe = github_markdown_text
    lines = [
        f"Visual review — round {rnd.get('round')} of build {safe(doc.get('build_id'))}.",
        "",
        f"Open: {github_markdown_url(preview.get('url'))}",
        f"Route: {safe(contract.get('route'))}   Scenario: {safe(contract.get('scenario'))}",
        f"Tenant / project shown: {safe(contract.get('tenant'))}",
        "",
        "How to view it:",
    ]
    lines += [f"  - {safe(step)}" for step in (contract.get("setup") or [])]
    lines += [f"  - {safe(step)}" for step in (contract.get("navigation") or [])]
    lines += ["", "Proof checklist:"] + [f"  [ ] {safe(item)}" for item in (contract.get("checklist") or [])]
    lines += ["", "Screenshots:"]
    for upload in published.get("uploads") or []:
        durable = (
            github_markdown_url(upload.get("url"))
            if upload.get("url")
            else safe(upload.get("ref"))
        )
        lines.append(
            f"  {upload.get('number')}. [{safe(upload.get('role'))}/{safe(upload.get('viewport'))}] "
            f"{durable}"
        )
    lines += [
        "",
        f"Automated UI review ({review.get('reviewer')}): "
        + ("findings" if review.get("findings") else "acknowledged")
        + f" — {safe(review.get('summary'))}",
        f"Evidence comment: {github_markdown_url(published.get('comment_url'))}",
        "",
        "Nothing is committed and the preview stack stays up.",
        "Reply with one of: approve, change <what to change>, split, park.",
    ]
    return "\n".join(lines)


def build_visual_prompt(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Prompt the saved planning agent, pre-commit, with the preview still up.

    Refuses when the contract did not ask for human review: sending the prompt
    anyway would put a question in front of a person that the plan already
    answered, and their reply would have nowhere to go.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    with BUILD_LOCK:
        ctx, refusal = build_visual_event(data, "visual_prompt", {}, repos)
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        contract = build_visual_contract(doc) or {}
        rnd = build_visual_round_current(doc)
        if not isinstance(rnd, dict) or rnd.get("change_id") != ctx["change_id"] or rnd.get("status") != "published":
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "the round for the change on disk has not published its evidence yet"
            out["code"] = "not_published"
            return 409, out
        if not rnd.get("human_review"):
            out = build_public(doc)
            out["ok"] = False
            out["round"] = rnd.get("round")
            out["error"] = "this build's contract did not request human visual review"
            out["code"] = "human_review_disabled"
            return 409, out
        if doc.get("pr_ready"):
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "human visual review happens before any commit; this build is past it"
            out["code"] = "not_pre_commit"
            return 409, out
        pane = str(contract.get("planning_pane") or "")
        if not pane:
            out = build_public(doc)
            out["ok"] = False
            out["error"] = "the contract names no planning pane to prompt"
            out["code"] = "missing_planning_pane"
            return 409, out
        prompt = build_visual_planning_prompt(doc, rnd)
        preview = rnd.get("preview") or {}
        # Checked, never touched. The whole point of the round staying open is
        # that the person can open the URL themselves; a teardown here would
        # leave them reading about a page that no longer exists.
        status = stack_status(str(preview.get("stack") or ""))
        probe = probe_loopback(
            str(preview.get("url") or ""),
            allow_login=bool(contract.get("allow_login_redirect")),
        )
        if not status.get("healthy") or not probe.get("ok"):
            out = build_public(doc)
            out.update({"ok": False, "code": "preview_unhealthy",
                        "error": "the saved preview is not healthy for human review"})
            return 409, out
        if not herdr_enabled() or not herdr_exact_pane(pane):
            out = build_public(doc)
            out.update({"ok": False, "code": "planning_pane_missing",
                        "error": "the exact saved planning pane is not live"})
            return 409, out
        sent = herdr_send(pane, prompt)
        delivered = bool(sent.get("ok"))
        if not delivered:
            out = build_public(doc)
            out.update({"ok": False, "code": "planning_send_failed",
                        "error": str(sent.get("error") or "Herdr send failed")})
            return 409, out
        rnd["planning_prompt"] = prompt
        rnd["prompted_at"] = utcnow()
        rnd["prompted_pane"] = pane
        rnd["prompt_delivered"] = delivered
        return build_visual_commit(
            ctx,
            {"round": rnd.get("round"), "pane": pane, "delivered": delivered},
            200,
            {
                "ok": True,
                "round": rnd.get("round"),
                "pane": pane,
                "delivered": delivered,
                "prompt": prompt,
                "url": preview.get("url"),
                "actions": ["approve", "change", "split", "park"],
                "pre_commit": not doc.get("pr_ready"),
                "preview_running": bool(status.get("healthy")),
                "preview": {k: preview.get(k) for k in ("preview_id", "stack", "url")},
            },
        )


# --- ticket-07 rollout proof ------------------------------------------------


def build_rollout_receipt(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Accept one controller-read receipt and bind it to this live build.

    This is intentionally a receipt census, not a canary runner.  Rewst owns
    the external calls and people own their review input; Graphwing only
    accepts a post-transition readback for the live checkout and refuses a
    final push until every required class is present and coherent.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {"build_id", "event_id", "kind", "source_build_id", "canary_run_id", "transition_id", "readback_id", "proof"}
    stray = sorted(set(data) - allowed)
    if stray:
        return 400, {"ok": False, "code": "extra_field", "error": f"unknown field: {stray[0]}"}
    build_id, event_id, kind = data.get("build_id"), data.get("event_id"), data.get("kind")
    if not valid_build_id(build_id):
        return 400, {"ok": False, "code": "bad_build_id", "error": "invalid build_id"}
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return 400, {"ok": False, "code": "bad_event_id", "error": "event_id is required"}
    if kind not in BUILD_ROLLOUT_RECEIPT_KIND_SET:
        return 400, {"ok": False, "code": "bad_rollout_kind", "error": "unknown rollout receipt kind"}
    source_build_id = data.get("source_build_id")
    if not valid_build_id(source_build_id):
        return 400, {"ok": False, "code": "bad_source_build_id", "error": "source_build_id is required"}
    for field in ("canary_run_id", "transition_id", "readback_id"):
        if not isinstance(data.get(field), str) or not BUILD_ROLLOUT_ID_RE.fullmatch(data[field]):
            return 400, {"ok": False, "code": f"bad_{field}", "error": f"{field} is required"}
    if data["readback_id"] == data["transition_id"]:
        return 400, {"ok": False, "code": "readback_not_separate", "error": "readback_id must name a separate post-transition read"}
    proof = data.get("proof")
    if not isinstance(proof, dict):
        return 400, {"ok": False, "code": "bad_rollout_proof", "error": "proof must be an object"}
    # Hashes are deliberately hex strings and would trip the generic
    # credential-shaped-value guard; reject secret-shaped *keys* instead.
    # Proof data is bounded facts, not a capability transport.
    if len(canonical_json(proof)) > FILE_MAX_BYTES:
        return 400, {"ok": False, "code": "proof_too_large", "error": "rollout proof is too large"}
    def proof_has_secret_key(value: Any) -> bool:
        if isinstance(value, dict):
            return any(secret_shaped_key(str(key)) or proof_has_secret_key(item) for key, item in value.items())
        if isinstance(value, list):
            return any(proof_has_secret_key(item) for item in value)
        return False
    if proof_has_secret_key(proof):
        return 400, {"ok": False, "code": "secret_in_rollout_proof", "error": "rollout proof contains a secret-shaped field"}
    fingerprint = event_fingerprint({
        "build_id": build_id, "source_build_id": source_build_id, "kind": kind, "canary_run_id": data["canary_run_id"],
        "transition_id": data["transition_id"], "readback_id": data["readback_id"], "proof": proof,
    })
    with BUILD_LOCK:
        doc, open_err = load_build(str(build_id))
        if open_err:
            return int(open_err["status"]), open_err
        if doc is None:
            return 404, {"ok": False, "code": "unknown_build", "error": "unknown build", "build_id": build_id}
        if doc.get("version") != BUILD_STATE_VERSION:
            return 409, build_version_park(doc)
        rollout = build_rollout(doc)
        if rollout is None:
            return 409, {"ok": False, "code": "rollout_not_requested", "error": "this build did not request ticket-07 rollout proof"}
        seen = build_event_seen(doc)
        if event_id in seen:
            previous = seen[event_id]
            if previous.get("fingerprint") != fingerprint:
                return 409, {**build_public(doc), "ok": False, "code": "idempotency_conflict", "error": "event_id was already used with different input"}
            return int(previous.get("status") or 200), {**build_public(doc), "replayed": True, "receipt": previous.get("receipt")}
        if len(seen) >= BUILD_EVENT_HARD_MAX:
            return 409, {"ok": False, "code": "event_history_full", "error": "build has used its whole event history"}
        if source_build_id == build_id:
            return 409, {**build_public(doc), "ok": False, "code": "rollout_holder_is_source",
                         "error": "issue-level rollout holder cannot count itself as a source canary"}
        source_doc, source_err = load_build(str(source_build_id))
        if source_err:
            return int(source_err["status"]), source_err
        if source_doc is None or source_doc.get("version") != BUILD_STATE_VERSION:
            return 409, {**build_public(doc), "ok": False, "code": "unknown_source_build",
                         "error": "source canary build is missing or unreadable"}
        live, live_error = build_live_change(source_doc, repos)
        if live is None:
            return 409, {**build_public(doc), "ok": False, "code": "source_identity_unreadable",
                         "error": str(live_error)}
        source_transition = build_event_seen(source_doc).get(data["transition_id"])
        if not isinstance(source_transition, dict) or not isinstance(source_transition.get("receipt"), dict):
            return 409, {**build_public(doc), "ok": False, "code": "missing_source_transition",
                         "error": "transition_id is not a durable source-build transition receipt"}
        source_evidence_id = build_evidence_identity(source_doc, live["change_id"])
        if kind == "sensitive":
            # Do not accept provider/model/role claims from the reviewing
            # model or controller.  Project only the receipt fields this
            # server authored when it routed and completed the two reviews.
            authority_reviews = []
            for role in BUILD_REVIEW_ROLES:
                review = build_last_review(source_doc, live["change_id"], role)
                authority = review.get("authority") if isinstance(review, dict) else None
                if not isinstance(authority, dict):
                    authority_reviews = []
                    break
                authority_reviews.append({
                    "author": authority.get("author"), "provider": authority.get("provider"),
                    "model": authority.get("model"), "role": authority.get("role"),
                    "reviewer": review.get("reviewer"), "verdict": review.get("verdict"),
                    "no_verdict": bool(review.get("no_verdict")),
                    "final_diff_sha": proof.get("final_diff_sha"), "evidence_id": review.get("evidence_id"),
                })
            proof = {**proof, "final_diff_sha": live["change_id"], "evidence_id": source_evidence_id,
                     "reviews": authority_reviews}
        proof_gap = rollout_proof_gap(str(kind), proof, live["head"])
        if proof_gap:
            return 409, {"ok": False, "code": "invalid_rollout_proof", "error": proof_gap}
        identity = {"commit_sha": live["head"], "change_id": live["change_id"],
                    "evidence_id": source_evidence_id}
        receipts = build_rollout_receipts(doc)
        if any(entry.get("kind") == kind for entry in receipts):
            return 409, {**build_public(doc), "ok": False, "code": "duplicate_rollout_kind", "error": "a receipt kind may satisfy the census once only"}
        if any(entry.get("transition_id") == data["transition_id"] for entry in receipts):
            return 409, {**build_public(doc), "ok": False, "code": "duplicate_transition_receipt", "error": "a transition receipt may satisfy the census once only"}
        receipt = {
            "event_id": event_id, "action": "rollout_receipt", "kind": kind, "at": utcnow(),
            "source_build_id": source_build_id, **identity, "canary_run_id": data["canary_run_id"], "transition_id": data["transition_id"], "readback_id": data["readback_id"],
            "readback_after_transition": True, "proof": proof,
        }
        rollout["receipts"] = receipts + [receipt]
        rollout["status"] = "open"
        build_record_event(doc, event_id, fingerprint, receipt, 200)
        write_err = write_build(doc, str(build_id))
        if write_err:
            return int(write_err["status"]), write_err
        out = build_public(doc)
        out.update(ok=True, replayed=False, receipt=receipt)
        return 200, out


# --- PR lifecycle -----------------------------------------------------------
#
# A lifecycle event is a durable build event, not a second controller.  The
# event reporter names a build and PR; resources come only from the recorded
# preview evidence and the role-separated stack catalog.


def parse_lifecycle_pr(raw: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw, dict) or set(raw) - {"number", "url"}:
        return None, {"ok": False, "code": "bad_pr", "error": "pr must contain only number and optional url"}
    number = raw.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 2**31 - 1:
        return None, {"ok": False, "code": "bad_pr", "error": "pr.number must be a positive integer"}
    out: dict[str, Any] = {"number": number}
    url = raw.get("url")
    if url is not None:
        if not isinstance(url, str) or not url.strip() or len(url) > BUILD_NAME_MAX:
            return None, {"ok": False, "code": "bad_pr", "error": "pr.url must be a short URL when present"}
        out["url"] = url.strip()
    return out, None


def lifecycle_pr_url_gap(doc: dict[str, Any], pr: dict[str, Any]) -> str | None:
    url = pr.get("url")
    if url is None:
        return None
    story = str(doc.get("story") or "")
    try:
        story_parts = urlparse(story).path.strip("/").split("/")
        parsed = urlparse(str(url))
        parts = parsed.path.strip("/").split("/")
    except ValueError:
        return "pr.url is malformed"
    if len(story_parts) < 4 or story_parts[2] not in ("issues", "pull"):
        return "build has no durable GitHub repository identity for pr.url"
    if not (parsed.scheme == "https" and strict_url_authority(parsed, r"github\.com", {None, 443}) and not parsed.query and not parsed.fragment and not parsed.username and not parsed.password and len(parts) == 4 and parts[:2] == story_parts[:2] and parts[2] == "pull" and parts[3] == str(pr["number"])):
        return "pr.url must be the exact GitHub pull URL for this build repository and PR"
    return None


def build_recorded_preview(doc: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the one build-derived preview recorded by visual evidence."""
    for rnd in reversed(doc.get("evidence_rounds") or []):
        preview = rnd.get("preview") if isinstance(rnd, dict) else None
        if not isinstance(preview, dict):
            continue
        stack = preview.get("stack")
        if not isinstance(stack, str):
            continue
        entry = build_stack_catalog().get(stack)
        if not entry or entry.get("role") != BUILD_PREVIEW_STACK_ROLE:
            continue
        if preview.get("preview_id") != build_preview_identity(doc, stack):
            continue
        if not isinstance(preview.get("url"), str) or not preview.get("url"):
            continue
        return {k: preview.get(k) for k in ("preview_id", "stack", "url", "healthy")}, entry
    return None, None


def stack_stop(entry: dict[str, Any]) -> dict[str, Any]:
    """Stop only the catalogued preview stack; verification happens separately."""
    args = ["podman", "compose"]
    compose = str(entry.get("compose_file") or "")
    if compose:
        args.extend(["-f", compose])
    args.append("down")
    result = run_cmd(args, cwd=entry.get("cwd"), timeout=120, env=podman_env())
    unit_results = [
        run_cmd(["systemctl", "--user", "stop", str(unit)], timeout=30)
        for unit in entry.get("units") or []
    ]
    return {**result, "units_stop_ok": all(row.get("ok") for row in unit_results)}


def unit_absence_observation(unit: str) -> dict[str, Any]:
    """Interpret systemctl's authoritative state, not just its exit status."""
    result = run_cmd(["systemctl", "--user", "is-active", unit], timeout=5)
    state = str(result.get("stdout") or "").strip().lower()
    if state in ("inactive", "unknown"):
        return {"unit": unit, "ok": True, "absent": True, "active": False, "state": state}
    if state in ("active", "activating", "reloading", "deactivating", "failed"):
        return {"unit": unit, "ok": True, "absent": False, "active": True, "state": state}
    return {"unit": unit, "ok": False, "absent": False, "active": False, "state": state or None,
            "error": str(result.get("error") or "unit status unreadable")[:BUILD_REDACT_MAX_STRING]}


def preview_teardown(entry: dict[str, Any]) -> dict[str, Any]:
    """Stop then prove absence of exactly the declared preview resources."""
    stopped = stack_stop(entry)
    status = stack_status(str(entry.get("name") or ""))
    stack_observed = status.get("ok") is True and status.get("compose_ok") is True
    stack_gone = stack_observed and not (status.get("containers") or []) and not bool(status.get("healthy"))
    ports = []
    survivors = []
    if not stack_gone:
        survivors.append({"kind": "stack", "name": entry.get("name")})
    if not stack_observed:
        survivors.append({"kind": "stack_observation", "name": entry.get("name"), "error": status.get("compose_error") or "stack status unreadable"})
    for port in entry.get("ports") or []:
        observation = port_observation(int(port))
        ports.append({"port": int(port), **observation})
        if observation.get("ok") is not True or observation.get("listening") is True:
            survivors.append({"kind": "port" if observation.get("ok") else "port_observation", "port": int(port), "error": observation.get("error")})
    units = []
    for unit in entry.get("units") or []:
        observation = unit_absence_observation(str(unit))
        units.append(observation)
        if not observation["absent"]:
            survivors.append({"kind": "unit" if observation.get("ok") else "unit_observation", "name": str(unit), "error": observation.get("error") or observation.get("state")})
    return {
        "verified": not survivors,
        "stop_acknowledged": bool(stopped.get("ok")),
        "stack": {"name": entry.get("name"), "gone": stack_gone, "containers": status.get("containers") or []},
        "ports": ports,
        "units": units,
        "survivors": survivors,
    }


def build_clean_cleanup_ready(doc: dict[str, Any]) -> bool:
    """Authorize only the durable integration receipt for the finalized candidate."""
    integration = build_checks_entry(doc, "integration")
    final = doc.get("pr_ready")
    return bool(
        isinstance(final, dict) and isinstance(integration, dict)
        and integration.get("ok") is True
        and integration.get("change_id") == final.get("verified_change_id")
        and integration.get("candidate_tree") == final.get("candidate_tree")
        and isinstance(integration.get("stack"), str) and integration.get("stack")
    )


def lifecycle_clean_cleanup(doc: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any] | None:
    """Reclaim only a proven final clean runner; prove the preview survived."""
    if not build_clean_cleanup_ready(doc):
        return None
    integration = build_checks_entry(doc, "integration") or {}
    entry, gap = build_integration_stack(doc, integration.get("stack"))
    if entry is None:
        return {"attempted": False, "verified": False, "code": "clean_stack_unavailable", "survivors": [{"kind": "clean_stack", "name": integration.get("stack"), "error": gap}]}
    before = {k: preview.get(k) for k in ("preview_id", "url", "healthy")}
    cleanup = preview_teardown(entry)
    live_preview = stack_status(str(preview.get("stack") or ""))
    preview_observed = live_preview.get("ok") is True and live_preview.get("compose_ok") is True
    preview_unchanged = preview_observed and bool(live_preview.get("healthy")) == bool(before["healthy"])
    survivors = list(cleanup.get("survivors") or [])
    if not preview_unchanged:
        survivors.append({"kind": "preview_observation", "name": preview.get("stack"), "error": "preview health changed or could not be observed"})
    return {"attempted": True, "verified": not survivors, "stack": entry.get("name"), "stop_acknowledged": cleanup.get("stop_acknowledged"), "preview": before, "preview_unchanged": preview_unchanged, "survivors": survivors, "teardown": cleanup, "code": None if not survivors else "clean_cleanup_unverified"}


def lifecycle_response(doc: dict[str, Any], event_id: str, fingerprint: str, receipt: dict[str, Any], status: int) -> tuple[int, dict[str, Any]]:
    build_record_event(doc, event_id, fingerprint, receipt, status)
    error = write_build(doc, doc.get("build_id"))
    if error:
        return int(error["status"]), error
    out = build_public(doc)
    out.update(ok=status == 200, replayed=False, receipt=receipt)
    if status != 200:
        out.update(code=receipt.get("code"), error=receipt.get("error"))
    if "preview" in receipt:
        out["preview"] = receipt["preview"]
    if "teardown" in receipt:
        out["teardown"] = receipt["teardown"]
    teardown = receipt.get("teardown") if isinstance(receipt.get("teardown"), dict) else {}
    herdr_log(f"lifecycle build={doc.get('build_id')} event={receipt.get('event')} pr={((receipt.get('pr') or {}).get('number'))} status={status} outcome={receipt.get('outcome') or receipt.get('code')} verified={teardown.get('verified')} survivors={len(teardown.get('survivors') or [])}")
    return status, out


def build_lifecycle(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    data, error = parse_json_object(body)
    if error:
        return 400, error
    assert data is not None
    build_id, event_id, event = data.get("build_id"), data.get("event_id"), data.get("event")
    if not valid_build_id(build_id):
        return 400, {"ok": False, "code": "bad_build_id", "error": "invalid build_id"}
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return 400, {"ok": False, "code": "bad_event_id", "error": "event_id is required"}
    if event not in ("opened", "merged", "closed"):
        return 400, {"ok": False, "code": "bad_lifecycle_event", "error": "event must be opened, merged, or closed"}
    pr, pr_error = parse_lifecycle_pr(data.get("pr"))
    if pr_error:
        return 400, pr_error
    assert pr is not None
    if set(data) - {"build_id", "event_id", "event", "pr", "choice", "plan_pane"}:
        return 400, {"ok": False, "code": "unexpected_lifecycle_field", "error": "lifecycle input contains an unsupported field"}
    choice = data.get("choice")
    if choice not in (None, "keep", "stop") or (choice is not None and event != "closed"):
        return 400, {"ok": False, "code": "bad_close_choice", "error": "choice is keep or stop only for closed"}
    plan_pane = data.get("plan_pane")
    if plan_pane is not None and (not isinstance(plan_pane, str) or not plan_pane.strip()):
        return 400, {"ok": False, "code": "bad_plan_pane", "error": "plan_pane must be a non-empty exact pane id"}
    plan_pane = plan_pane.strip() if isinstance(plan_pane, str) else None
    fingerprint = event_fingerprint({"build_id": build_id, "event": event, "pr": pr, "choice": choice, "plan_pane": plan_pane})
    with BUILD_LOCK:
        doc, read_error = load_build(build_id)
        if read_error:
            return int(read_error["status"]), read_error
        if doc is None:
            return 404, {"ok": False, "code": "unknown_build", "error": "unknown build", "build_id": build_id}
        if doc.get("version") != BUILD_STATE_VERSION:
            return 409, build_version_park(doc)
        url_gap = lifecycle_pr_url_gap(doc, pr)
        if url_gap:
            return 400, {"ok": False, "code": "bad_pr_url", "error": url_gap}
        seen = build_event_seen(doc)
        if event_id in seen:
            record = seen[event_id]
            if record.get("fingerprint") != fingerprint:
                return 409, {**build_public(doc), "ok": False, "replayed": False, "code": "idempotency_conflict", "error": "event_id was already used with different input"}
            receipt = record.get("receipt")
            status = int(record.get("status") or 200)
            out = build_public(doc)
            out.update(ok=status == 200, replayed=True, receipt=receipt)
            if isinstance(receipt, dict):
                out.update(preview=receipt.get("preview"), teardown=receipt.get("teardown"))
                if status != 200:
                    out.update(code=receipt.get("code"), error=receipt.get("error"))
            return status, out
        if len(seen) >= BUILD_EVENT_HARD_MAX:
            return 409, {"ok": False, "code": "event_history_full", "error": "build has used its whole event history"}
        lifecycle = doc.setdefault("lifecycle", {})
        receipt_base = {"event_id": event_id, "event": event, "pr": pr, "at": utcnow()}
        if lifecycle.get("state") == "merged_verified" and event in ("opened", "closed"):
            receipt = {**receipt_base, "code": "lifecycle_out_of_order", "error": "verified merged teardown is terminal; event starts nothing, prompts nobody, and cleans nothing"}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
        if lifecycle.get("state") == "partial_cleanup" and event != "merged":
            receipt = {**receipt_base, "code": "partial_cleanup_pending", "error": "partial merged cleanup accepts only a fresh merged retry; this event prompts nobody and cleans nothing"}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
        if lifecycle.get("state") == "closed_partial_cleanup":
            receipt = {**receipt_base, "code": "closed_partial_cleanup", "error": "closed-stop partial cleanup requires the recorded operator action; lifecycle events perform no further cleanup"}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
        bound = doc.get("pr")
        if event != "opened" and not isinstance(bound, dict):
            receipt = {**receipt_base, "code": "pr_not_opened", "error": "merged or closed requires the durable PR identity recorded by opened"}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
        if isinstance(bound, dict) and bound != pr:
            receipt = {**receipt_base, "code": "pr_identity_mismatch", "error": "lifecycle PR does not match the durable build PR"}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
        preview, entry = build_recorded_preview(doc)
        if event == "opened":
            if preview is None:
                receipt = {**receipt_base, "code": "preview_missing", "error": "no durable recorded preview exists; opened creates nothing", "refused": True}
                return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
            doc["pr"] = pr
            doc["stage"] = "pr_opened"
            clean_cleanup = lifecycle_clean_cleanup(doc, preview)
            if isinstance(clean_cleanup, dict) and clean_cleanup.get("attempted") and not clean_cleanup.get("verified"):
                receipt = {**receipt_base, "preview": preview, "clean_cleanup": clean_cleanup, "code": "clean_partial_cleanup", "error": "clean integration cleanup is incomplete", "next_action": "resolve surviving clean resources and retry with a fresh opened event"}
                return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
            receipt = {**receipt_base, "preview": preview, "clean_cleanup": clean_cleanup, "outcome": "opened_recorded"}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 200)
        if preview is None or entry is None:
            receipt = {**receipt_base, "code": "preview_missing", "error": "no durable recorded preview exists to reclaim"}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
        if event == "closed" and choice is None:
            contract = build_visual_contract(doc) or {}
            pane = str(contract.get("planning_pane") or "")
            if not pane:
                receipt = {**receipt_base, "code": "missing_planning_pane", "error": "closed without merge has no exact saved planning pane"}
                return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
            delivery = herdr_plan_deliver(pane, f"PR #{pr['number']} closed without merge for build {build_id}; choose keep or stop for {preview['url']}.")
            receipt = {
                **receipt_base, "preview": preview, "pane": pane,
                "outcome": "close_choice_requested", "delivery": delivery,
                # A missing answer is intentionally not a stop instruction.
                # The pending record below is durable and no teardown path is
                # reached until a later event supplies keep/stop at this pane.
                "no_answer_preserves_preview": True,
            }
            if not delivery.get("ok"):
                receipt.update(code="planning_send_failed", error="the exact planning pane could not be reached")
                return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
            lifecycle["close_pending"] = {"pr": pr, "pane": pane, "preview": preview}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 200)
        if event == "closed" and choice is not None:
            pending = lifecycle.get("close_pending")
            if not isinstance(pending, dict) or pending.get("pr") != pr or pending.get("pane") != plan_pane:
                receipt = {**receipt_base, "code": "close_choice_not_pending", "error": "no choice from the exact saved planning pane is pending"}
                return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
            if choice == "keep":
                lifecycle["close_pending"] = None
                return lifecycle_response(doc, event_id, fingerprint, {**receipt_base, "preview": preview, "pane": plan_pane, "outcome": "close_kept"}, 200)
        teardown = preview_teardown(entry)
        if not teardown["verified"]:
            lifecycle["state"] = "partial_cleanup"
            if event == "merged":
                lifecycle["state"] = "partial_cleanup"
                next_action = "resolve surviving preview resources and retry with a fresh merged event"
            else:
                lifecycle["state"] = "closed_partial_cleanup"
                next_action = "resolve surviving preview resources through the recorded close decision; no merged retry is implied"
            receipt = {**receipt_base, "preview": preview, "teardown": teardown, "code": "partial_cleanup", "error": "preview cleanup is incomplete", "next_action": next_action}
            return lifecycle_response(doc, event_id, fingerprint, receipt, 409)
        lifecycle["state"] = "merged_verified" if event == "merged" else "closed_verified"
        lifecycle["close_pending"] = None
        receipt = {**receipt_base, "preview": preview, "teardown": teardown, "outcome": "merged_cleaned" if event == "merged" else "closed_stopped"}
        return lifecycle_response(doc, event_id, fingerprint, receipt, 200)


# --- pre-PR visual iteration ------------------------------------------------
#
# A visual round proves what the page looks like. This is what happens next: a
# person looks at it and wants it different, and says so, and the change goes
# round again. The story calls that expected design work, so three things hold
# here that do not hold anywhere else on the pre-PR path.
#
# Feedback is not a strike. A red check and a reviewer finding are both the
# change failing to be what it claimed; "make the button smaller" is not. The
# strike ledgers stay exactly where they were across a design turn, and the
# only thing a turn spends is one job of budget — which is the floor, because
# the turn count deliberately has none.
#
# There is no turn ceiling. Turn 20 pauses and asks continue, reframe, split,
# or park, and once that is answered it never asks again. A checkpoint that
# re-armed at 40 would be a limit wearing a question's clothes.
#
# Nothing here tears down the preview or commits anything. Park, stop, split
# and reframe all leave the branch, the state, the stack, and every published
# round exactly where they are, because the whole value of stopping is being
# able to pick it back up.


def build_iteration(doc: dict[str, Any]) -> dict[str, Any]:
    """The durable design-iteration record, created in memory if absent.

    Lazily shaped rather than written at create, so a build opened before this
    existed reads as an iteration that has not started instead of parking on a
    state version it cannot understand.
    """
    record = doc.get("iteration")
    if not isinstance(record, dict):
        record = {"state": "iterating"}
        doc["iteration"] = record
    for key in ("turns", "decisions", "splits", "returns"):
        if not isinstance(record.get(key), list):
            record[key] = []
    if record.get("state") not in BUILD_ITERATION_STATES:
        record["state"] = "iterating"
    return record


def build_iteration_turns(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Every design turn this build has ever had, oldest first.

    Never trimmed. The turn list is the record of what a person asked for and
    what came back, and a build that dropped turn 3 would be unable to say why
    the button is the size it is.
    """
    return [t for t in build_iteration(doc)["turns"] if isinstance(t, dict)]


def build_iteration_open_turn(doc: dict[str, Any]) -> dict[str, Any] | None:
    """The turn currently in flight, or None.

    Only the last turn can be open: a decision closes one before the next set
    of notes may be recorded, so two turns are never in flight at once and the
    writer is never resumed twice for two different sets of instructions.
    """
    turns = build_iteration_turns(doc)
    last = turns[-1] if turns else None
    if isinstance(last, dict) and last.get("status") in BUILD_ITERATION_OPEN_STATUSES:
        return last
    return None


def build_iteration_checkpoint_due(doc: dict[str, Any]) -> bool:
    """True when the advisory checkpoint is owed an answer.

    Once answered, never again. `continue` is a decision about whether this is
    still one change, not a quota that refills.
    """
    record = build_iteration(doc)
    checkpoint = record.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("decision"):
        return False
    return len(build_iteration_turns(doc)) >= BUILD_ITERATION_CHECKPOINT_TURN


def build_strike_counts(doc: dict[str, Any]) -> dict[str, int]:
    """The three ledgers a design turn must never move.

    Derived from the durable record rather than incremented alongside it. A
    parallel counter is one more thing that can disagree with what it is
    supposed to summarize, and the claim this makes — "feedback cost the change
    nothing" — is only worth making if it is read off the same evidence a
    reviewer would read.
    """
    checks = doc.get("checks") or {}
    reviews = [r for r in (doc.get("reviews") or []) if isinstance(r, dict)]
    corrections = 0
    for record in (doc.get("event_seen") or {}).values():
        receipt = record.get("receipt") if isinstance(record, dict) else None
        if (
            isinstance(receipt, dict)
            and receipt.get("action") == "claim"
            and receipt.get("kind") == "correction"
        ):
            corrections += 1
    return {
        "test": len(
            [
                phase
                for phase in BUILD_CHECK_PHASES
                if isinstance(checks.get(phase), dict) and not checks[phase].get("ok")
            ]
        ),
        "reviewer": len([r for r in reviews if r.get("verdict") != "PASS"]),
        "retry": corrections,
    }


def build_iteration_checks_clean(doc: dict[str, Any], change_id: str) -> tuple[bool, str]:
    """Whether a design candidate has its two non-substitutable fast proofs.

    Plans and path rules still determine the full fast recipe union.  Design
    iteration adds one narrow invariant: its candidate cannot be shown as a
    completed turn on the strength of a generic unit receipt.  Both named
    proofs must therefore be among that union and have passed for this exact
    change; additional declared or path-mandated recipes remain required by
    ``build_checks_clean`` as usual.
    """
    clean, why = build_checks_clean(doc, "fast", change_id)
    if not clean:
        return clean, why
    entry = build_checks_entry(doc, "fast") or {}
    required = {name for name in (entry.get("required") or []) if isinstance(name, str)}
    missing = [name for name in ("changed-area", "typecheck") if name not in required]
    if missing:
        return False, (
            "design iteration requires changed-area tests and typecheck; fast checks "
            "did not require: " + ", ".join(missing)
        )
    return True, ""


def build_iteration_design_ready(doc: dict[str, Any], change_id: str) -> dict[str, Any] | None:
    """The design-ready decision for the candidate on disk, or None.

    Pinned to the change id for the same reason a review is: design ready for
    the candidate the user saw says nothing about the one the writer produced
    afterwards.
    """
    record = doc.get("iteration")
    ready = record.get("design_ready") if isinstance(record, dict) else None
    if isinstance(ready, dict) and ready.get("change_id") == change_id:
        return ready
    return None


def build_visual_human_path_gaps(doc: dict[str, Any], change_id: str) -> list[str]:
    """What the visible candidate on disk still owes a person before a commit.

    This is the line the story draws at the end: a final gate that required a
    fix which could change the UI sends the build back to iteration, and the
    resulting candidate cannot be committed until it has been through the human
    path its own plan asked for. Enforced here, at the commit, because that is
    the only place where skipping it would actually ship something.
    """
    record = doc.get("iteration")
    if isinstance(record, dict) and record.get("returned_candidate_change_id"):
        replacement = record.get("returned_candidate_writer_change_id")
        if replacement != change_id:
            return [
                "a final-gate return still requires a new writer-produced candidate before any "
                "candidate can complete the human visual path"
            ]
    rnd = build_visual_round_current(doc)
    if not isinstance(rnd, dict) or rnd.get("change_id") != change_id:
        return [
            "the candidate on disk has no visual evidence round; a visible change commits "
            "only after its own round is published"
        ]
    if rnd.get("status") != "published":
        return [f"evidence round {rnd.get('round')} for this change is not published"]
    if not rnd.get("human_review"):
        return []
    if not rnd.get("prompted_at"):
        return [f"round {rnd.get('round')} still owes the planning session its report"]
    if build_iteration_design_ready(doc, change_id) is None:
        return [
            f"round {rnd.get('round')} has no design-ready decision for this candidate; "
            "the human visual path is not complete"
        ]
    return []


def build_iteration_step(doc: dict[str, Any], change_id: str) -> tuple[str, str]:
    """Name the one step this invocation may run, and why.

    Same contract as the mechanical and visual switches: a function of the
    durable document and the change on disk, consulting nothing the caller
    sent and writing nothing, so an invocation that dies right after asking has
    cost the build nothing.
    """
    stage = doc.get("stage")
    record = build_iteration(doc)
    if stage == "parked":
        return "parked", str(doc.get("park_reason") or "parked")
    if doc.get("pr_ready") or stage == "pr_opened":
        return "done", "this build has already produced its PR-ready commit"
    if record.get("state") == "stopped":
        return "done", "the planning session stopped design iteration"
    if stage in ("verifying", "review", "ready"):
        return "final_gates", (
            f"the design is ready and the build is at '{stage}'; it runs its final gates, and a "
            "fix that could change the UI returns here"
        )
    if stage != "evidence":
        return "parked", f"visual iteration runs at stage 'evidence'; this build is at '{stage}'"
    if build_budget_left(doc) == 0:
        return "parked", "budget_exhausted"
    turn = build_iteration_open_turn(doc)
    if turn is None:
        rnd = build_visual_round_current(doc)
        if (
            isinstance(rnd, dict)
            and rnd.get("status") == "published"
            and rnd.get("change_id") == change_id
        ):
            return "feedback", (
                f"round {rnd.get('round')} is published and awaits the planning session's notes"
            )
        return "feedback", "no design turn is open; the next one starts with the user's exact notes"
    if build_iteration_checkpoint_due(doc):
        return "checkpoint", (
            f"turn {turn.get('turn')} reached the advisory checkpoint; continue, reframe, split, "
            "or park before any more code is written"
        )
    status = turn.get("status")
    if status == "open":
        why = "the open turn's notes have not been handed to the writer yet"
        if turn.get("writer_status") in ("error", "timeout"):
            why = "the writer stopped before success; its files and session are recoverable"
        return "write", why
    if status == "written":
        if turn.get("change_id") != change_id:
            return "write", "the writer has not produced the current candidate yet"
        clean, why = build_iteration_checks_clean(doc, change_id)
        if not clean:
            return "checks", why
        rnd = build_visual_round_current(doc)
        if not (
            isinstance(rnd, dict)
            and rnd.get("status") == "published"
            and rnd.get("change_id") == change_id
        ):
            return "round", (
                "the changed-area checks are clean; this turn still owes a published visual "
                "evidence round for its candidate"
            )
        return "report", (
            f"round {rnd.get('round')} is published; the planning session owes its exact report"
        )
    return "decision", (
        "the planning session has this turn's report and owes continue, design ready, reframe, "
        "split, park, or stop"
    )


def build_iteration_public(doc: dict[str, Any]) -> dict[str, Any] | None:
    """A compact view of the design conversation, or None when none has started.

    Deliberately small and deliberately read-only: this rides on every state
    response, and the durable turns carry the user's full notes and every
    writer summary. A poll loop should not move all of that to learn a number.
    """
    record = doc.get("iteration")
    if not isinstance(record, dict):
        return None
    turns = [t for t in (record.get("turns") or []) if isinstance(t, dict)]
    last = turns[-1] if turns else None
    checkpoint = record.get("checkpoint") if isinstance(record.get("checkpoint"), dict) else None
    return {
        "state": record.get("state"),
        "turns": len(turns),
        "turn": last.get("turn") if last else None,
        "turn_status": last.get("status") if last else None,
        "turn_source": last.get("source") if last else None,
        "checkpoint_turn": BUILD_ITERATION_CHECKPOINT_TURN,
        "checkpoint": (
            {k: checkpoint.get(k) for k in ("turn", "decision", "at")} if checkpoint else None
        ),
        # No `turn_max`: the story says there is no total ceiling, and a field
        # named like one would be read as a limit by the first graph author who
        # saw it.
        "turn_ceiling": None,
        "history": [
            {
                k: t.get(k)
                for k in ("turn", "source", "status", "round", "decision", "at", "notes_digest")
            }
            for t in turns[-BUILD_ITERATION_TURNS_SHOWN:]
        ],
        "splits": [s for s in (record.get("splits") or []) if isinstance(s, dict)][
            -BUILD_ITERATION_TURNS_SHOWN:
        ],
        "returns": len([r for r in (record.get("returns") or []) if isinstance(r, dict)]),
        "design_ready": record.get("design_ready"),
        "strikes": build_strike_counts(doc),
    }


def build_open_iteration(
    build_id: Any, stages: tuple[str, ...]
) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Load a build for an iteration op and check it is at a stage that runs one.

    No class check, exactly like the visual round and for the same reason: a
    sensitive UI change routes to sensitive models and still has a person
    looking at the page. Gating this on class would exclude the changes that
    most need the conversation.
    """
    doc, err = build_open_visual(build_id)
    if err:
        return None, err
    assert doc is not None
    stage = doc.get("stage")
    if stage not in stages:
        return None, (
            409,
            {
                "ok": False,
                "build_id": doc.get("build_id"),
                "stage": stage,
                "error": (
                    f"this transition runs at stage {' or '.join(stages)}; this build is at '{stage}'"
                ),
                "code": "invalid_transition",
            },
        )
    return doc, None


def build_iteration_event(
    data: dict[str, Any],
    action: str,
    extra: dict[str, Any],
    repos: dict[str, str],
    stages: tuple[str, ...],
) -> tuple[dict[str, Any] | None, tuple[int, dict[str, Any]] | None]:
    """Open one idempotent iteration transition, or hand back the refusal.

    Same four things every state-changing op needs established first — the
    build exists and wants visual proof, this event id has not already run and
    is not being reused for different input, nobody else holds the lease, and
    the worktree is still the one the build was opened on — plus the stage,
    because the ops in this section deliberately do not all run at the same one.
    """
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not BUILD_EVENT_ID_RE.fullmatch(event_id):
        return None, (400, {"ok": False, "error": "event_id is required", "code": "bad_event_id"})
    holder = data.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        return None, (400, {"ok": False, "error": "holder is required", "code": "missing_holder"})
    holder = holder.strip()[:BUILD_NAME_MAX]
    holder_err = reject_secret_text(holder, "holder")
    if holder_err:
        return None, (400, holder_err)
    fingerprint = event_fingerprint(
        {"build_id": data.get("build_id"), "action": action, "holder": holder, **extra}
    )

    doc, open_err = build_open_iteration(data.get("build_id"), stages)
    if open_err:
        return None, open_err
    assert doc is not None

    seen = build_event_seen(doc)
    if event_id in seen:
        record = seen.get(event_id) or {}
        out = build_public(doc)
        out["replayed"] = record.get("fingerprint") == fingerprint
        if not out["replayed"]:
            out["ok"] = False
            out["error"] = "event_id was already used with different input"
            out["code"] = "idempotency_conflict"
            return None, (409, out)
        out["ok"] = int(record.get("status") or 200) == 200
        out["receipt"] = record.get("receipt")
        return None, (int(record.get("status") or 200), out)

    live, park = build_identity_park(doc, action, data.get("worktree"), None, None, repos)
    if park is not None:
        reason, detail = park
        return None, build_park(doc, reason, detail, event_id, fingerprint, holder)
    assert live is not None
    change_id = build_change_id(Path(str(doc.get("worktree"))), live["head"])
    live = {**live, "change_id": change_id}

    lease = build_lease_live(doc, time.time())
    if lease is not None and lease.get("holder") != holder:
        out = build_public(doc)
        out["ok"] = False
        out["error"] = "another caller holds the build lease"
        out["code"] = "lease_held"
        return None, (409, out)

    return {
        "doc": doc,
        "event_id": event_id,
        "holder": holder,
        "fingerprint": fingerprint,
        "action": action,
        "live": live,
        "change_id": change_id,
    }, None


def build_iteration_notes(data: dict[str, Any], field: str) -> tuple[str | None, dict[str, Any] | None]:
    """Validate the exact text a person typed, without changing a character.

    Refused rather than truncated or redacted. The story says the event records
    the user's exact notes, so a note this service would have to alter is a note
    it declines to accept: a truncated instruction is a different instruction,
    and a note with a token in it is one nobody should be storing at all.
    """
    notes = data.get(field)
    if not isinstance(notes, str) or not notes.strip():
        return None, {"ok": False, "error": f"{field} is required", "code": f"missing_{field}"}
    if len(notes) > BUILD_ITERATION_NOTES_MAX:
        return None, {
            "ok": False,
            "error": (
                f"{field} is longer than {BUILD_ITERATION_NOTES_MAX} characters; it is stored "
                "exactly as written, so it is refused rather than shortened"
            ),
            "code": f"{field}_too_long",
        }
    secret = reject_secret_text(notes, field)
    if secret:
        return None, secret
    return notes, None


def build_iteration_stale_gap(
    doc: dict[str, Any], rnd: dict[str, Any] | None, round_no: int, change_id: str
) -> tuple[str, str] | None:
    """Why this feedback is about something other than the candidate on disk.

    Read-only and called before anything is written. Feedback naming an older
    round is a person answering a page that has already been replaced, and
    acting on it would resume the writer with instructions about a UI that no
    longer exists.
    """
    if not isinstance(rnd, dict):
        return "stale_feedback", "this build has published no visual evidence round to respond to"
    if rnd.get("status") != "published":
        return "stale_feedback", (
            f"round {rnd.get('round')} has not published its evidence yet; there is nothing to "
            "give feedback on"
        )
    if rnd.get("change_id") != change_id:
        return "stale_feedback", (
            f"round {rnd.get('round')} was captured at another change; the files on disk have "
            "moved since that page was photographed"
        )
    if int(rnd.get("round") or 0) != round_no:
        return "stale_feedback", (
            f"this feedback answers round {round_no}; the live round is {rnd.get('round')}"
        )
    return None


def build_iteration_feedback(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Record one design turn's exact notes, or refuse without touching a file.

    The refusals are the point. Stale feedback answers a page that has been
    replaced; duplicate feedback is the same reply arriving twice through a
    retried graph; conflicting feedback is two different instructions racing for
    one writer. Each one returns before any claim, any charge, and any writer,
    so the worktree is exactly as it was.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {"build_id", "event_id", "holder", "notes", "round", "worktree"}
    stray = sorted(set(data) - allowed)
    if stray:
        return 400, {"ok": False, "error": f"unknown field: {stray[0]}", "code": "extra_field"}
    notes, notes_err = build_iteration_notes(data, "notes")
    if notes_err:
        return 400, notes_err
    assert notes is not None
    round_no, round_err = parse_optional_int(data, "round", 1, BUILD_JOBS_MAX)
    if round_err:
        return 400, round_err
    if round_no is None:
        return 400, {
            "ok": False,
            "error": "round is required; feedback names the round it is answering",
            "code": "missing_round",
        }
    digest = hashlib.sha256(notes.encode()).hexdigest()

    with BUILD_LOCK:
        ctx, refusal = build_iteration_event(
            data,
            "visual_feedback",
            {"notes": notes, "round": round_no},
            repos,
            ("evidence",),
        )
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        record = build_iteration(doc)
        session = doc.get("writer_session")
        if not (isinstance(session, str) and session.strip()):
            out = build_public(doc)
            out.update(
                ok=False,
                code="no_writer_session",
                error="a design turn resumes the writer that made the change; this build has none",
            )
            return 409, out
        rnd = build_visual_round_current(doc)
        stale = build_iteration_stale_gap(doc, rnd, round_no, ctx["change_id"])
        if stale:
            code, detail = stale
            out = build_public(doc)
            out.update(ok=False, code=code, error=detail, files_unchanged=True)
            return 409, out
        assert rnd is not None
        open_turn = build_iteration_open_turn(doc)
        if open_turn is not None:
            same = open_turn.get("notes_digest") == digest
            out = build_public(doc)
            out.update(
                ok=False,
                code="duplicate_feedback" if same else "feedback_conflict",
                turn=open_turn.get("turn"),
                error=(
                    f"turn {open_turn.get('turn')} already carries this note and is still open"
                    if same
                    else (
                        f"turn {open_turn.get('turn')} is open with different notes; one turn "
                        "answers one set of instructions"
                    )
                ),
                files_unchanged=True,
            )
            return 409, out
        answered = rnd.get("feedback")
        if isinstance(answered, dict):
            out = build_public(doc)
            out.update(
                ok=False,
                code="duplicate_feedback",
                turn=answered.get("turn"),
                error=(
                    f"round {rnd.get('round')} was already answered by turn {answered.get('turn')}; "
                    "the next note answers the round that turn produces"
                ),
                files_unchanged=True,
            )
            return 409, out

        turn = {
            "turn": len(build_iteration_turns(doc)) + 1,
            "source": "human",
            "status": "open",
            "notes": notes,
            "notes_digest": digest,
            "feedback_id": ctx["event_id"],
            "holder": ctx["holder"],
            "responds_to_round": int(rnd.get("round") or round_no),
            "responds_to_change_id": ctx["change_id"],
            "writer_session": session,
            "at": utcnow(),
            "claim_id": None,
            "writer_result": None,
            "writer_status": None,
            "change_id": None,
            "round": None,
            "report": None,
            "reported_at": None,
            "decision": None,
            # Stated on the record, not only in the docstring. A later reader
            # asking "did all this feedback count against the change?" gets the
            # answer from the turn itself.
            "strike_free": True,
            "strikes_at_open": build_strike_counts(doc),
        }
        record["turns"].append(turn)
        record["state"] = "iterating"
        rnd["feedback"] = {"turn": turn["turn"], "at": turn["at"], "notes_digest": digest}
        checkpoint_due = build_iteration_checkpoint_due(doc)
        if checkpoint_due:
            record["checkpoint"] = {
                "turn": turn["turn"],
                "at": utcnow(),
                "decision": None,
                "why": (
                    f"turn {turn['turn']} reached the advisory checkpoint; the planning session "
                    "chooses continue, reframe, split, or park before more code is written"
                ),
            }
        step, reason = build_iteration_step(doc, ctx["change_id"])
        return build_iteration_commit(
            ctx,
            {
                "turn": turn["turn"],
                "action": "visual_feedback",
                "notes_digest": digest,
                "responds_to_round": turn["responds_to_round"],
                "writer_session": session,
                "jobs_spent": 0,
                "strike_free": True,
                "strikes": build_strike_counts(doc),
                "checkpoint": checkpoint_due,
            },
            200,
            {
                "ok": True,
                "turn": turn["turn"],
                "notes": notes,
                "notes_digest": digest,
                "responds_to_round": turn["responds_to_round"],
                "writer_session": session,
                "checkpoint": checkpoint_due,
                "step": step,
                "step_reason": reason,
                "strikes": build_strike_counts(doc),
                "pre_commit": not doc.get("pr_ready"),
            },
        )


def build_iteration_commit(
    ctx: dict[str, Any], receipt: dict[str, Any], status: int, out: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Record the event and persist, so a retry replays instead of repeating."""
    doc = ctx["doc"]
    receipt = {
        "event_id": ctx["event_id"],
        "action": ctx["action"],
        "at": utcnow(),
        "holder": ctx["holder"],
        "build_id": doc.get("build_id"),
        "head": ctx["live"]["head"],
        "change_id": ctx["change_id"],
        "stage": doc.get("stage"),
        "budget_left": build_budget_left(doc),
        **receipt,
    }
    build_record_event(doc, ctx["event_id"], ctx["fingerprint"], receipt, status)
    write_err = write_build(doc, doc.get("build_id"))
    if write_err:
        return int(write_err["status"]), write_err
    payload = build_public(doc)
    payload.update(out)
    payload["replayed"] = False
    payload["receipt"] = receipt
    payload["change_id"] = ctx["change_id"]
    return status, payload


def build_iteration_brief_lines(doc: dict[str, Any], turn: dict[str, Any]) -> list[str]:
    """The design-turn half of the writer's prompt.

    The notes go in verbatim and last. Everything above them is the standing
    contract — the ticket, the recipes, the route — and a design turn that
    paraphrased the user's words would be a writer solving a slightly different
    request every round until nobody could say what was asked for.
    """
    contract = build_visual_contract(doc) or {}
    rnd = build_visual_round_current(doc)
    lines = [
        "",
        f"DESIGN FEEDBACK — turn {turn.get('turn')}. This is visual iteration, not a correction.",
        "Nothing failed. A person looked at the page you built and wants it different.",
        "The files are on disk. Continue this change in the same session; do not start over.",
        f"Route under review: {contract.get('route')}   Scenario: {contract.get('scenario')}",
    ]
    if isinstance(rnd, dict) and rnd.get("round"):
        lines.append(f"They are responding to evidence round {rnd.get('round')}.")
    if turn.get("source") == "final_gate":
        lines.append(
            "This turn came back from a final gate: the fix can change what the page looks "
            "like, so it returns through visual iteration rather than being patched at the gate."
        )
    lines += ["", "Their exact words:", str(turn.get("notes") or "")]
    lines.append(
        "Change only what they asked for. The round's checks and its browser scenario run "
        "again over whatever you leave on disk."
    )
    return lines


def build_iteration_report_text(
    doc: dict[str, Any], turn: dict[str, Any], rnd: dict[str, Any]
) -> str:
    """The exact text the planning session is handed, saved verbatim on the turn.

    Five things, because the story names five: where to open it, how to get to
    the state, what changed in this round, what to look at, and the pictures.
    Saved rather than reconstructed later, for the same reason the round's first
    prompt is: it is the thing a person answered.
    """
    contract = build_visual_contract(doc) or {}
    preview = rnd.get("preview") or {}
    published = rnd.get("published") or {}
    review = rnd.get("review") or {}
    checks = build_checks_entry(doc, "fast") or {}
    writer = turn.get("writer_result") or {}
    safe = github_markdown_text
    lines = [
        f"Design turn {turn.get('turn')} — round {rnd.get('round')} of build {safe(doc.get('build_id'))}.",
        "",
        f"Open: {github_markdown_url(preview.get('url'))}",
        f"Route: {safe(contract.get('route'))}   Scenario: {safe(contract.get('scenario'))}",
        f"Tenant / project shown: {safe(contract.get('tenant'))}",
        "",
        "How to get there:",
    ]
    lines += [f"  - {safe(step)}" for step in (contract.get("setup") or [])]
    lines += [f"  - {safe(step)}" for step in (contract.get("navigation") or [])]
    lines += [
        "",
        "What changed in this round:",
        f"  You asked: {safe(turn.get('notes'))}",
    ]
    if writer.get("summary"):
        lines.append(f"  Writer: {safe(writer.get('summary'))}")
    for path in [p for p in (checks.get("changed_paths") or []) if isinstance(p, str)][:12]:
        lines.append(f"  changed: {safe(path)}")
    lines += ["", "What to inspect:"]
    lines += [f"  [ ] {safe(item)}" for item in (contract.get("checklist") or [])]
    lines += [f"  [ ] {safe(item)}" for item in (contract.get("expected") or [])]
    lines.append(
        f"  Changed-area checks ({safe(', '.join(str(n) for n in (checks.get('required') or [])))}): "
        + ("clean" if checks.get("ok") else "red")
    )
    lines.append(
        f"  Automated UI review ({safe(review.get('reviewer'))}): "
        + ("findings" if review.get("findings") else "acknowledged")
        + f" — {safe(review.get('summary'))}"
    )
    lines += ["", "Screenshots:"]
    for upload in published.get("uploads") or []:
        durable = (
            github_markdown_url(upload.get("url"))
            if upload.get("url")
            else safe(upload.get("ref"))
        )
        lines.append(
            f"  {upload.get('number')}. [{safe(upload.get('role'))}/{safe(upload.get('viewport'))}] "
            f"{durable}"
        )
    lines += [
        "",
        f"Evidence comment: {github_markdown_url(published.get('comment_url'))}",
        "",
        "Nothing is committed and the preview stack stays up.",
        f"This is design turn {turn.get('turn')}. Feedback is iteration, not a strike, and there "
        "is no total turn limit.",
    ]
    if build_iteration_checkpoint_due(doc):
        lines.append(
            f"Turn {BUILD_ITERATION_CHECKPOINT_TURN} checkpoint: choose continue, reframe, split, "
            "or park before any more code is written."
        )
    lines.append(
        "Reply with one of: change <what to change>, design ready, reframe, split <what to "
        "split off>, park, stop."
    )
    return "\n".join(lines)


def build_iteration_report(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Hand the planning session this turn's candidate, with the preview alive.

    Refuses unless the turn has actually produced one: clean changed-area
    checks and a published round for the change on disk. Reporting a candidate
    whose checks have not run would put a person in front of a page that may not
    even compile and ask them what they think of the spacing.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {"build_id", "event_id", "holder", "worktree"}
    stray = sorted(set(data) - allowed)
    if stray:
        return 400, {"ok": False, "error": f"unknown field: {stray[0]}", "code": "extra_field"}
    with BUILD_LOCK:
        ctx, refusal = build_iteration_event(data, "iteration_report", {}, repos, ("evidence",))
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        contract = build_visual_contract(doc) or {}
        turn = build_iteration_open_turn(doc)
        if turn is None:
            out = build_public(doc)
            out.update(ok=False, code="no_open_turn", error="no design turn is open to report")
            return 409, out
        step, reason = build_iteration_step(doc, ctx["change_id"])
        if step != "report":
            out = build_public(doc)
            out.update(
                ok=False,
                code="report_not_due",
                step=step,
                step_reason=reason,
                turn=turn.get("turn"),
                error=f"this turn is at step '{step}', not 'report'",
            )
            return 409, out
        rnd = build_visual_round_current(doc)
        assert isinstance(rnd, dict)
        pane = str(contract.get("planning_pane") or "")
        if not pane:
            out = build_public(doc)
            out.update(
                ok=False,
                code="missing_planning_pane",
                error="the contract names no planning pane to report to",
            )
            return 409, out
        if doc.get("pr_ready"):
            out = build_public(doc)
            out.update(
                ok=False,
                code="not_pre_commit",
                error="design iteration happens before any commit; this build is past it",
            )
            return 409, out
        preview = rnd.get("preview") or {}
        # Checked, never touched. The person is going to open this URL, and a
        # teardown here would leave them reading about a page that is gone.
        status = stack_status(str(preview.get("stack") or ""))
        probe = probe_loopback(
            str(preview.get("url") or ""),
            allow_login=bool(contract.get("allow_login_redirect")),
        )
        if not status.get("healthy") or not probe.get("ok"):
            out = build_public(doc)
            out.update(
                ok=False,
                code="preview_unhealthy",
                error="the saved preview is not serving the round's URL for this report",
            )
            return 409, out
        if not herdr_enabled() or not herdr_exact_pane(pane):
            out = build_public(doc)
            out.update(
                ok=False,
                code="planning_pane_missing",
                error="the exact saved planning pane is not live",
            )
            return 409, out
        report = build_iteration_report_text(doc, turn, rnd)
        sent = herdr_send(pane, report)
        if not sent.get("ok"):
            out = build_public(doc)
            out.update(
                ok=False,
                code="planning_send_failed",
                error=str(sent.get("error") or "Herdr send failed"),
            )
            return 409, out
        turn["report"] = report
        turn["reported_at"] = utcnow()
        turn["reported_pane"] = pane
        turn["round"] = rnd.get("round")
        turn["status"] = "reported"
        # The round's own human-review debt is settled by this report. Recording
        # it here keeps the visual switch from asking for a second prompt about
        # the same pictures, and it is what the commit gate reads later.
        rnd["planning_prompt"] = report
        rnd["prompted_at"] = turn["reported_at"]
        rnd["prompted_pane"] = pane
        rnd["prompt_delivered"] = True
        return build_iteration_commit(
            ctx,
            {
                "turn": turn["turn"],
                "round": rnd.get("round"),
                "pane": pane,
                "delivered": True,
                "jobs_spent": 0,
                "strike_free": True,
            },
            200,
            {
                "ok": True,
                "turn": turn["turn"],
                "round": rnd.get("round"),
                "pane": pane,
                "delivered": True,
                "report": report,
                "url": preview.get("url"),
                "decisions": list(BUILD_ITERATION_DECISIONS),
                "checkpoint": build_iteration_checkpoint_due(doc),
                "pre_commit": not doc.get("pr_ready"),
                "preview_running": True,
                "preview": {k: preview.get(k) for k in ("preview_id", "stack", "url")},
            },
        )


def build_iteration_decision_gap(
    doc: dict[str, Any], decision: str, turn: dict[str, Any] | None, change_id: str
) -> tuple[str, str] | None:
    """Why this answer cannot be given right now, or None.

    Split out because each decision is refused for its own reason and folding
    them into one condition is how `stop` quietly acquires `design_ready`'s
    evidence requirement.
    """
    if decision == "continue":
        if not build_iteration_checkpoint_due(doc):
            return "no_checkpoint", (
                "continue answers the advisory checkpoint, and no checkpoint is waiting"
            )
        return None
    if decision == "design_ready":
        record = build_iteration(doc)
        returned_change = record.get("returned_candidate_change_id")
        if returned_change and record.get("returned_candidate_writer_change_id") != change_id:
            return "return_requires_new_candidate", (
                "the final-gate return still requires a new writer-produced candidate before it can "
                "become design ready again"
            )
        if turn is not None and turn.get("status") != "reported":
            return "turn_in_flight", (
                f"turn {turn.get('turn')} has not been reported to the planning session yet; "
                "design ready would skip the candidate nobody has seen"
            )
        gaps = build_visual_human_path_gaps(doc, change_id)
        # The design-ready decision is itself the missing piece here, so the
        # gap that asks for one is the one answer this must not require.
        gaps = [g for g in gaps if "design-ready decision" not in g]
        if gaps:
            return "not_design_ready", gaps[0]
        clean, why = build_iteration_checks_clean(doc, change_id)
        if not clean:
            return "not_design_ready", why
        return None
    if decision in ("reframe", "split") and turn is None:
        return "no_open_turn", f"{decision} closes the open design turn, and none is open"
    return None


def build_iteration_decision(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Record what the planning session decided, in its own durable state.

    Six answers, six states, six receipts. They are deliberately not collapsed:
    a build that was split is not a build that was parked, and a reader six
    weeks later needs to be able to tell which one happened without inferring it
    from what did not happen next.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {"build_id", "event_id", "holder", "decision", "note", "worktree"}
    stray = sorted(set(data) - allowed)
    if stray:
        return 400, {"ok": False, "error": f"unknown field: {stray[0]}", "code": "extra_field"}
    decision = data.get("decision")
    if decision not in BUILD_ITERATION_DECISIONS:
        return 400, {
            "ok": False,
            "error": "decision must name one of the recorded planning answers",
            "code": "bad_decision",
            "allowed": list(BUILD_ITERATION_DECISIONS),
        }
    note: str | None = None
    if decision == "split" or data.get("note") is not None:
        note, note_err = build_iteration_notes(data, "note")
        if note_err:
            return 400, note_err

    with BUILD_LOCK:
        ctx, refusal = build_iteration_event(
            data,
            "iteration_decision",
            {"decision": decision, "note": note},
            repos,
            ("evidence",),
        )
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        record = build_iteration(doc)
        turn = build_iteration_open_turn(doc)
        gap = build_iteration_decision_gap(doc, decision, turn, ctx["change_id"])
        if gap:
            code, detail = gap
            out = build_public(doc)
            out.update(ok=False, code=code, error=detail, decision=decision)
            return 409, out

        rnd = build_visual_round_current(doc)
        entry = {
            "decision": decision,
            "at": utcnow(),
            "holder": ctx["holder"],
            "event_id": ctx["event_id"],
            "turn": turn.get("turn") if turn else None,
            "round": rnd.get("round") if isinstance(rnd, dict) else None,
            "change_id": ctx["change_id"],
            "note": note,
        }
        record["decisions"] = [
            d for d in (record.get("decisions") or []) if isinstance(d, dict)
        ] + [entry]

        if decision == "continue":
            checkpoint = record.get("checkpoint")
            if not isinstance(checkpoint, dict):
                checkpoint = {"turn": turn.get("turn") if turn else None}
                record["checkpoint"] = checkpoint
            checkpoint["decision"] = "continue"
            checkpoint["decided_at"] = entry["at"]
            checkpoint["decided_by"] = ctx["holder"]
            record["state"] = "iterating"
        else:
            # Every other answer closes the turn it was given about. The
            # checkpoint, if one was waiting, is answered by the same act: a
            # user who reframed at turn 20 has said the thing the checkpoint was
            # asking for, and asking again would be a loop.
            if turn is not None:
                turn["status"] = "closed"
                turn["decision"] = decision
                turn["closed_at"] = entry["at"]
            checkpoint = record.get("checkpoint")
            if isinstance(checkpoint, dict) and not checkpoint.get("decision"):
                checkpoint["decision"] = decision
                checkpoint["decided_at"] = entry["at"]
                checkpoint["decided_by"] = ctx["holder"]

        gate = None
        if decision == "design_ready":
            record["state"] = "design_ready"
            record["returned_candidate_change_id"] = None
            record["returned_candidate_writer_change_id"] = None
            record["design_ready"] = {
                "at": entry["at"],
                "turn": entry["turn"],
                "round": entry["round"],
                "change_id": ctx["change_id"],
                "holder": ctx["holder"],
                "head": ctx["live"]["head"],
            }
            moved = build_gate_transition(
                doc,
                "evidence_captured",
                ctx["change_id"],
                "buildIterationDecision",
                f"the planning session called round {entry['round']} design ready",
            )
            gate = moved["to"] if moved else None
        elif decision == "reframe":
            record["state"] = "reframed"
        elif decision == "split":
            record["state"] = "split"
            record["splits"] = (
                [s for s in (record.get("splits") or []) if isinstance(s, dict)]
                + [{**entry, "split_id": hashlib.sha256(
                    f"{doc.get('build_id')}\0{ctx['event_id']}".encode()
                ).hexdigest()[:16]}]
            )[-BUILD_ITERATION_SPLITS_MAX:]

        if decision in BUILD_ITERATION_PARK_REASONS:
            record["state"] = "parked" if decision == "park" else "stopped"
            reason = BUILD_ITERATION_PARK_REASONS[decision]
            detail = (
                f"the planning session chose {decision} at design turn {entry['turn']}; the "
                "branch, the build state, the preview stack, and every published round are "
                "preserved"
            )
            # build_park writes the document, records the receipt under this
            # event id, and answers 409. The mutations above are already on the
            # in-memory document, so they persist with it.
            status, out = build_park(
                doc, reason, detail, ctx["event_id"], ctx["fingerprint"], ctx["holder"]
            )
            out["decision"] = decision
            out["turn"] = entry["turn"]
            out["preserved"] = {
                "branch": doc.get("branch"),
                "worktree": doc.get("worktree"),
                "preview": (rnd.get("preview") or {}).get("preview_id") if isinstance(rnd, dict) else None,
                "rounds": len(build_visual_rounds(doc)),
                "turns": len(build_iteration_turns(doc)),
            }
            return status, out

        step, reason = build_iteration_step(doc, ctx["change_id"])
        return build_iteration_commit(
            ctx,
            {
                "decision": decision,
                "turn": entry["turn"],
                "round": entry["round"],
                "state": record["state"],
                "gate": gate,
                "note": note,
                "jobs_spent": 0,
                "strike_free": True,
                "checkpoint": record.get("checkpoint"),
            },
            200,
            {
                "ok": True,
                "decision": decision,
                "turn": entry["turn"],
                "round": entry["round"],
                "state": record["state"],
                "gate": gate,
                "step": step,
                "step_reason": reason,
                "strikes": build_strike_counts(doc),
                "pre_commit": not doc.get("pr_ready"),
            },
        )


def build_iteration_return(body: bytes, repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Send a final-gate fix that can change the UI back through iteration.

    The alternative is patching it at the gate, which produces a candidate that
    passed every check and that no person has ever seen. That candidate then
    commits on the strength of a human review of a different picture, which is
    the exact failure the visual path exists to prevent — so the return is a
    real transition with a receipt, and the commit gate refuses until the new
    candidate has been through the human path itself.
    """
    data, err = parse_json_object(body)
    if err:
        return 400, err
    assert data is not None
    allowed = {"build_id", "event_id", "holder", "finding", "gate", "worktree"}
    stray = sorted(set(data) - allowed)
    if stray:
        return 400, {"ok": False, "error": f"unknown field: {stray[0]}", "code": "extra_field"}
    finding, finding_err = build_iteration_notes(data, "finding")
    if finding_err:
        return 400, finding_err
    assert finding is not None
    gate_name = data.get("gate")
    if gate_name is not None and (not isinstance(gate_name, str) or not gate_name.strip()):
        return 400, {"ok": False, "error": "gate must be a name", "code": "bad_gate"}
    gate_name = gate_name.strip()[:BUILD_NAME_MAX] if isinstance(gate_name, str) else None

    with BUILD_LOCK:
        ctx, refusal = build_iteration_event(
            data,
            "iteration_return",
            {"finding": finding, "gate": gate_name},
            repos,
            ("verifying", "review", "ready"),
        )
        if refusal:
            return refusal
        assert ctx is not None
        doc = ctx["doc"]
        if doc.get("pr_ready"):
            out = build_public(doc)
            out.update(
                ok=False,
                code="already_committed",
                error="this build already produced its PR-ready commit; there is nothing pre-commit to return",
            )
            return 409, out
        session = doc.get("writer_session")
        if not (isinstance(session, str) and session.strip()):
            out = build_public(doc)
            out.update(
                ok=False,
                code="no_writer_session",
                error="a returned turn resumes the writer that made the change; this build has none",
            )
            return 409, out
        record = build_iteration(doc)
        if build_iteration_open_turn(doc) is not None:
            out = build_public(doc)
            out.update(
                ok=False,
                code="turn_in_flight",
                error="a design turn is already open; the return would be a second set of instructions",
            )
            return 409, out
        frm = doc.get("stage")
        rnd = build_visual_round_current(doc)
        turn = {
            "turn": len(build_iteration_turns(doc)) + 1,
            "source": "final_gate",
            "status": "open",
            "notes": finding,
            "notes_digest": hashlib.sha256(finding.encode()).hexdigest(),
            "feedback_id": ctx["event_id"],
            "holder": ctx["holder"],
            "gate": gate_name,
            "returned_from": frm,
            "responds_to_round": rnd.get("round") if isinstance(rnd, dict) else None,
            "responds_to_change_id": ctx["change_id"],
            "writer_session": session,
            "at": utcnow(),
            "claim_id": None,
            "writer_result": None,
            "writer_status": None,
            "change_id": None,
            "round": None,
            "report": None,
            "reported_at": None,
            "decision": None,
            "strike_free": True,
            "strikes_at_open": build_strike_counts(doc),
        }
        record["turns"].append(turn)
        record["state"] = "iterating"
        record["design_ready"] = None
        # A reframe or split can close this turn, but it cannot resurrect the
        # candidate that final gates returned.  Keep the exact returned change
        # as durable debt until another candidate completes the full path and
        # earns a new design-ready decision.
        record["returned_candidate_change_id"] = ctx["change_id"]
        record["returned_candidate_writer_change_id"] = None
        record["returns"] = [
            r for r in (record.get("returns") or []) if isinstance(r, dict)
        ] + [
            {
                "at": turn["at"],
                "from": frm,
                "gate": gate_name,
                "turn": turn["turn"],
                "event_id": ctx["event_id"],
                "change_id": ctx["change_id"],
            }
        ]
        build_gate_record(
            doc,
            "visual_return",
            "evidence",
            ctx["change_id"],
            "buildIterationReturn",
            f"a {gate_name or frm} finding can change visible output; the candidate returns to iteration",
        )
        doc["stage"] = "evidence"
        # The verified boundary pinned where verification ended, so that a lease
        # takeover could not roll it back. This is the authorized rollback: the
        # change is deliberately going back to be rewritten, and leaving the
        # boundary would park the next gate on a head that is supposed to move.
        doc["returned_boundary"] = doc.get("verified_boundary")
        doc["verified_boundary"] = None
        doc["pending_finalize"] = None
        step, reason = build_iteration_step(doc, ctx["change_id"])
        return build_iteration_commit(
            ctx,
            {
                "action": "visual_return",
                "from": frm,
                "to": "evidence",
                "turn": turn["turn"],
                "gate": gate_name,
                "jobs_spent": 0,
                "strike_free": True,
            },
            200,
            {
                "ok": True,
                "turn": turn["turn"],
                "from": frm,
                "to": "evidence",
                "gate": gate_name,
                "step": step,
                "step_reason": reason,
                "commit_blocked_until": "the returned candidate completes its required human path",
                "pre_commit": not doc.get("pr_ready"),
            },
        )


def build_iteration_next(qs: dict[str, list[str]], repos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """The iteration stage switch. Read durable state, name the next allowed step.

    Read-only, like the other two switches: the graph calls this first on every
    invocation, so it has to be safe to call from a run that is about to be
    abandoned.
    """
    doc, err = build_open_visual(first_query(qs, "build_id"))
    if err:
        return err
    assert doc is not None
    live, park_detail = build_live_change(doc, repos)
    if live is None:
        out = build_public(doc)
        out.update(ok=False, step="parked", step_reason=str(park_detail), code="worktree_mismatch")
        return 409, out
    step, reason = build_iteration_step(doc, live["change_id"])
    turn = build_iteration_open_turn(doc)
    rnd = build_visual_round_current(doc)
    out = build_public(doc)
    out["ok"] = step != "parked"
    out["step"] = step
    out["step_reason"] = reason
    out["live_head"] = live["head"]
    out["live_branch"] = live["branch"]
    out["change_id"] = live["change_id"]
    out["class"] = build_class(doc)
    out["turn"] = turn.get("turn") if turn else None
    out["turn_status"] = turn.get("status") if turn else None
    out["turn_notes"] = turn.get("notes") if turn else None
    out["turn_source"] = turn.get("source") if turn else None
    out["round"] = rnd.get("round") if isinstance(rnd, dict) else None
    out["round_status"] = rnd.get("status") if isinstance(rnd, dict) else None
    out["checkpoint"] = build_iteration_checkpoint_due(doc)
    out["checkpoint_turn"] = BUILD_ITERATION_CHECKPOINT_TURN
    out["decisions"] = list(BUILD_ITERATION_DECISIONS)
    out["strikes"] = build_strike_counts(doc)
    if step == "parked":
        out["code"] = doc.get("park_reason") or "parked"
    return (200 if out["ok"] else 409), out


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
    if method == "GET" and path.startswith("/v1/artifact/"):
        # The one-time artifact grant is the authorization for this route.
        # Rewst's GitHub integration does not receive the catalog-wide API key.
        return artifact_fetch(path.removeprefix("/v1/artifact/").strip("/"), headers)

    if not authed:
        return json_out(401, {"error": "invalid or missing X-Graphwing-Key", "code": "unauthorized"})

    if path.startswith("/v1/supervisor/"):
        supervisor_recover_once()

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
    if method == "POST" and path == "/v1/build/lifecycle":
        status, payload = build_lifecycle(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/rollout/receipt":
        status, payload = build_rollout_receipt(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/review/reconcile":
        status, payload = build_review_reconcile(body, repos)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/build/state":
        status, payload = build_state_op(qs)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/build/next":
        status, payload = build_next_op(qs, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/preflight":
        status, payload = build_preflight(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/claim":
        status, payload = build_claim_op(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/gate":
        status, payload = build_gate_op(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/brief":
        status, payload = build_brief(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/writer/launch":
        status, payload = build_writer_launch(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/writer/complete":
        status, payload = build_writer_complete(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/checks":
        status, payload = build_checks(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/review":
        status, payload = build_review(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/finalize":
        status, payload = build_finalize(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/supervisor/register":
        status, payload = supervisor_register(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/supervisor/bind":
        status, payload = supervisor_bind(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/supervisor/reconcile":
        status, payload = supervisor_reconcile(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/supervisor/watch":
        status, payload = supervisor_watch(body, repos)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/supervisor/report":
        status, payload = supervisor_report_op(qs)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/build/visual/next":
        status, payload = build_visual_next(qs, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/preflight":
        status, payload = build_visual_preflight(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/preview":
        status, payload = build_visual_preview(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/scenario":
        status, payload = build_visual_scenario(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/inspect":
        status, payload = build_visual_inspect(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/review":
        status, payload = build_visual_review(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/handoff":
        status, payload = build_visual_handoff(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/publish":
        status, payload = build_evidence_publish(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/publish-result":
        status, payload = build_evidence_publish_result(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/visual/prompt":
        status, payload = build_visual_prompt(body, repos)
        return json_out(status, payload)
    if method == "GET" and path == "/v1/build/iteration/next":
        status, payload = build_iteration_next(qs, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/iteration/feedback":
        status, payload = build_iteration_feedback(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/iteration/report":
        status, payload = build_iteration_report(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/iteration/decision":
        status, payload = build_iteration_decision(body, repos)
        return json_out(status, payload)
    if method == "POST" and path == "/v1/build/iteration/return":
        status, payload = build_iteration_return(body, repos)
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
    supervisor_recover_once()
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"graphwing listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
